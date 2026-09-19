# 04 — The Pregel Runtime & Execution Model

## 1. Concepts

Compiling a `StateGraph` (or creating an `@entrypoint`) produces a **`Pregel`** object. Pregel is
named after Google's Pregel algorithm and implements **Bulk Synchronous Parallel (BSP)** execution
over actors and channels.

### The three phases of a super-step

1. **Plan** — determine which actors run. Step 0: actors subscribed to input channels. Step N:
   actors subscribed to channels updated in step N-1.
2. **Execute** — run all selected actors **in parallel**, until all complete, one fails, or a
   timeout fires. During execution, channel updates are **invisible** to peers.
3. **Update** — apply all writes to channels (through reducers).

Repeat until no actors are selected, or `recursion_limit` super-steps have elapsed.

### Why this matters practically

| BSP property | Consequence you will hit |
|---|---|
| Writes are invisible within a step | A node cannot read another node's output from the same step |
| All selected actors run in parallel | Two nodes writing one channel need a reducer |
| Step boundary = checkpoint boundary | Checkpoint frequency == super-step count, not node count |
| Failure aborts the step, successful writes are kept as **pending writes** | On resume, completed nodes in that step are not re-run |
| Time travel resumes only at step boundaries | You cannot resume "halfway through a node" |

### Pending writes

As each node finishes inside a super-step, its output is written to the checkpointer's
`checkpoint_writes` table as a task entry linked to the in-progress checkpoint. The full state
snapshot is committed once the step completes. So if node B fails while node A succeeded in the same
step, resuming re-runs only B. This is the mechanism that makes durable execution cheap — and the
reason node side effects must be idempotent only *within* a node, not across the whole step.

### Checkpoint namespaces

`checkpoint_ns` identifies which graph a checkpoint belongs to:

- `""` — the root graph.
- `"node_name:uuid"` — a subgraph invoked as that node.
- Nested: joined with `|`, e.g. `"outer:uuid|inner:uuid"`.

Available inside a node via `config["configurable"]["checkpoint_ns"]`. This is what makes
per-thread subgraph parallel calls conflict ([13](13-subgraphs-and-composition.md)).

### Determinism

The runtime replays by re-executing nodes after a checkpoint. That imposes a contract:

- Nodes must be **deterministic given the same state** in everything that affects control flow.
- Randomness, wall-clock reads and unstable ids must either be captured into state at write time or
  isolated into `@task`s (functional API) whose results are persisted.
- Interrupts are **always re-triggered on replay** — see [15](15-human-in-the-loop-and-interrupts.md).

## 2. How to implement

### Using Pregel directly (rare, but it clarifies the model)

```python
from langgraph.channels import EphemeralValue
from langgraph.pregel import Pregel, NodeBuilder

node = (
    NodeBuilder()
    .subscribe_only("input")
    .do(lambda x: x + x)
    .write_to("output")
)

app = Pregel(
    nodes={"node": node},
    channels={"input": EphemeralValue(str), "output": EphemeralValue(str)},
    input_channels=["input"],
    output_channels=["output"],
)
app.invoke({"input": "foo"})
```

You will almost never write this. Read it once so that "actors subscribe to channels" stops being
an abstraction.

### Controlling parallelism

Parallelism is structural: fan out with multiple edges from one node, or with `Send`.

```python
# Structural fan-out: b and c run in the same super-step
builder.add_edge("a", "b")
builder.add_edge("a", "c")
builder.add_edge("b", "d")
builder.add_edge("c", "d")   # d runs in the next step, after both
```

There is no `max_parallelism` knob inside a run. To bound concurrency:

- **Batch inside a node** (`asyncio.Semaphore` + `asyncio.gather`) instead of fanning out 1000 `Send`s.
- Bound run-level concurrency at the platform layer with `N_JOBS_PER_WORKER` ([19](19-scaling-and-performance.md)).
- Use rate-limiting middleware / model call limits ([10](10-agents-and-middleware.md)).

### Deferred nodes (fan-in that waits for *all* paths)

```python
builder.add_node("aggregate", aggregate, defer=True)
```

A deferred node waits until all other pending paths in the graph have completed before executing —
the correct primitive for map-reduce aggregation when branches have different lengths. Without it, a
short branch can trigger the aggregator early.

### Async everywhere

The Agent Server executes runs on an event loop. Synchronous blocking calls inside a node block the
loop, degrading *every other run on that worker*.

```python
# Bad
def node(state): return {"x": requests.get(url).json()}

# Good
async def node(state):
    async with httpx.AsyncClient() as c:
        return {"x": (await c.get(url)).json()}

# Acceptable for unavoidable blocking libraries
async def node(state):
    return {"x": await asyncio.to_thread(legacy_blocking_call)}
```

### Recursion / step accounting

- `config["recursion_limit"]` (top-level key, default 1000) caps super-steps.
- `config["metadata"]["langgraph_step"]` is the current counter.
- `RemainingSteps` managed value enables graceful degradation ([02](02-graph-api-core.md)).

Note that a `Send` fan-out of 500 items is **one** super-step, not 500 — recursion limit bounds
depth, not width.

## 3. Scenarios

- **Fan-out of 2,000 documents**: prefer chunked `Send` batches (e.g. 50 `Send`s each handling 40
  docs with internal `asyncio.gather` + semaphore) over 2,000 `Send`s. Fewer tasks → fewer pending
  writes, smaller checkpoints, controlled downstream QPS.
- **A node needs the output of a sibling**: it doesn't; restructure so the sibling runs in an
  earlier super-step, or merge the two nodes.
- **Long external job (10-minute render)**: don't block a super-step for 10 minutes. Either use
  `interrupt()` and resume on webhook, or poll in a node with an `idle_timeout` +
  `runtime.heartbeat()` ([17](17-durability-fault-tolerance-idempotency.md)).
- **Cost accounting per run**: accumulate in a `BinaryOperatorAggregate` channel written by
  middleware; it composes correctly under parallel fan-in.

## 4. Staff-level considerations

- **Super-steps are your unit of durability, latency and cost.** A graph with 30 sequential nodes
  writes 30 checkpoints per run. At 500 runs/sec that's 15k checkpoint writes/sec at the DB. Merge
  nodes whose failure you would not retry independently.
- **Width is cheap, depth is expensive.** Wide fan-out costs one step; long chains cost one
  checkpoint each and serialise latency.
- **Parallel does not mean isolated.** Parallel branches share the same checkpoint namespace and
  write to the same channels. Isolation comes from subgraphs or from disjoint channels.
- **The event loop is a shared resource.** One `time.sleep` in a node degrades every co-tenant run
  on that worker. Enforce with lint rules (`flake8-async`, `asyncio` blocking detectors) in CI.
- **Replay is a feature with a cost**: every re-executed node re-issues its side effects unless you
  make it idempotent. Budget for it in the design, not in the postmortem.

## 5. Anti-patterns

| Anti-pattern | Why it fails |
|---|---|
| Reading a sibling's write inside the same step | Writes are invisible until the update phase |
| Unbounded `Send` fan-out | Thundering herd on downstream APIs; huge pending-write sets |
| Blocking I/O in nodes | Starves the worker's event loop; inflates p99 for unrelated runs |
| Long sleeps/polling loops inside a node | Holds a run slot; use interrupts or heartbeats |
| Raising `recursion_limit` to fix a loop | Masks a control-flow bug, multiplies cost |
| Assuming node execution order within a step | Order is not guaranteed; encode order in edges |

## 6. Design-review questions

1. How many super-steps does a typical run take? What does that cost in checkpoint writes?
2. Where do we fan out, how wide, and what bounds the downstream QPS?
3. Is any node blocking the event loop? How do we detect it in CI?
4. If a node is re-executed (retry, replay, resume), what external effect happens twice?
5. Do we ever depend on the order of parallel nodes?

## References

- `/oss/python/langgraph/pregel`
- `/oss/python/langgraph/checkpointers` (super-steps, pending writes, namespaces)
- `/oss/python/langgraph/use-graph-api` (branches, defer, async)
- `/langsmith/agent-server-scale` (avoid synchronous blocking operations)
