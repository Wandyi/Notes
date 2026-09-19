# 17 — Durability, Fault Tolerance & Idempotency

> Requires `langgraph>=1.2` for per-node timeouts, node-level error handlers, `set_node_defaults`
> and graceful drain.

## 1. Concepts

Three composable mechanisms, applied in a fixed order:

```
attempt → exception (incl. NodeTimeoutError) → retry_policy? → retries exhausted → error_handler? → bubble up
```

| Mechanism | Parameter on `add_node` | Answers |
|---|---|---|
| **Retries** | `retry_policy=RetryPolicy(...)` | Should we try again? |
| **Timeouts** | `timeout=` (seconds / `timedelta` / `TimeoutPolicy`) | How long may one attempt run? |
| **Error handling** | `error_handler=` | What do we do when it's definitively failed? |

Plus two runtime-level facilities: **durability modes** (how often state is persisted) and
**graceful drain** (stop cleanly at a super-step boundary and resume later).

### Retry defaults

`RetryPolicy` defaults: `max_attempts=3`, `initial_interval=0.5`, `backoff_factor=2.0`,
`max_interval=128.0`, `jitter=True`, `retry_on=default_retry_on`.

`default_retry_on` retries **any** exception **except** `ValueError`, `TypeError`,
`ArithmeticError`, `ImportError`, `LookupError`, `NameError`, `SyntaxError`, `RuntimeError`,
`ReferenceError`, `StopIteration`, `StopAsyncIteration`, `OSError`. For `requests`/`httpx`
exceptions it retries only on **5xx**. `NodeTimeoutError` is retryable by default.

That default is well-chosen: programming errors are not retried, transient infrastructure errors are.

### Timeouts

- `run_timeout` — hard wall-clock cap on one attempt; never refreshed.
- `idle_timeout` — fires only when the node stops making observable progress; the clock resets on
  progress signals.

Under `refresh_on="auto"`, progress = state writes, yielded stream chunks, child-task scheduling,
stream-writer calls, or **any** LangChain callback event from the node or its descendants (LLM
tokens, tool calls, chain start/end). With `refresh_on="heartbeat"`, only explicit
`runtime.heartbeat()` calls count.

**Timeouts are async-only.** Sync nodes with a `timeout` are rejected at compile time. Wrap blocking
I/O in `asyncio.to_thread` inside an async node.

On timeout, writes from the failed attempt are cleared and the retry policy decides.

`NodeTimeoutError` carries `node`, `elapsed`, `kind` (`"idle"`/`"run"`), `idle_timeout`, `run_timeout`.

### Error handlers

Run after retries are exhausted (or immediately if there's no retry policy). Signature:
`(state, error: NodeError) -> state update | Command`. `NodeError` has `.node` and `.error`.

Key semantics:

- **Failure provenance is checkpointed** — if the process crashes after a node fails but before the
  handler completes, the handler sees the same `NodeError` on resume.
- **`interrupt()` is not routed to error handlers.** It uses `GraphBubbleUp` and bypasses both
  retries and handlers.
- **Subgraph failures** surface to the parent node; the parent's handler fires with the subgraph's
  exception in `error.error`.
- **Handler failures bubble up** as if there were no handler. One handler per node.

### Graph-wide defaults

```python
graph = (
    StateGraph(State)
    .set_node_defaults(
        retry_policy=RetryPolicy(max_attempts=3),
        timeout=TimeoutPolicy(run_timeout=30),
        error_handler=default_error_handler,
    )
    .add_node("a", a)
    .add_node("b", b, error_handler=custom_handler)   # per-node wins
    .compile()
)
```

Applicability: `retry_policy` and `timeout` apply to error-handler nodes too; `error_handler` and
`cache_policy` do **not** (handlers must not catch themselves; caching handler results is unsafe).
**Defaults are not inherited by subgraphs** — each graph sets its own.

### Graceful drain

```python
import signal
from langgraph.runtime import RunControl
from langgraph.errors import GraphDrained

control = RunControl()
signal.signal(signal.SIGTERM, lambda *_: control.request_drain("sigterm"))

try:
    result = graph.invoke(inputs, config, control=control)
except GraphDrained as e:
    log.info("drained: %s", e.reason)     # checkpoint saved; resume later
```

Semantics: drain is **cooperative and between super-steps**. A running node completes; a retrying
node finishes its retry loop; if more super-steps remain, `GraphDrained` is raised with a resumable
checkpoint. Subgraph drains bubble up. Resume with `graph.invoke(None, config)` on the same thread.

Nodes can read `runtime.drain_requested` / `runtime.drain_reason` and skip expensive work.

`request_drain()` does **not** cancel asyncio tasks — pair it with a hard timeout for an upper bound.

## 2. How to implement

### A production-grade node configuration

```python
from langgraph.types import RetryPolicy, TimeoutPolicy, Command, default_retry_on
from langgraph.errors import NodeError

def retry_on_transient(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, ValidationError):     # our own; never retry
        return False
    return default_retry_on(exc)

def payment_handler(state: State, error: NodeError) -> Command:
    return Command(update={"status": f"failed: {error.error}", "needs_compensation": True},
                   goto="compensate")

builder.add_node(
    "charge_payment", charge_payment,
    timeout=TimeoutPolicy(run_timeout=45, idle_timeout=15),
    retry_policy=RetryPolicy(max_attempts=4, retry_on=retry_on_transient, max_interval=30),
    error_handler=payment_handler,
)
```

### Fallback on later attempts

```python
from langgraph.runtime import Runtime

async def call_model(state: State, runtime: Runtime) -> dict:
    if runtime.execution_info.node_attempt > 1:
        return {"answer": await fallback_model.ainvoke(state["prompt"])}
    return {"answer": await primary_model.ainvoke(state["prompt"])}
```

`execution_info` exposes `node_attempt`, `node_first_attempt_time`, `thread_id`, `run_id`,
`checkpoint_id`, `task_id` — available even without a retry policy (`node_attempt` defaults to 1).

### Long-running work with heartbeats

```python
async def bulk_process(state: State, runtime: Runtime) -> dict:
    for batch in batches(state["items"]):
        await process(batch)
        runtime.heartbeat()          # resets the idle clock; no-op outside an idle-timed attempt
    return {"done": True}

builder.add_node("bulk_process", bulk_process,
                 timeout=TimeoutPolicy(idle_timeout=60, refresh_on="heartbeat"))
```

### Idempotency patterns

Nodes re-execute on retry, resume and replay. Make effects safe:

```python
def stable_key(state: State, suffix: str) -> str:
    # thread_id + logical step is stable across attempts
    return f"{state['request_id']}:{suffix}"

async def create_ticket(state: State) -> dict:
    key = stable_key(state, "create_ticket")
    existing = await tickets.find_by_idempotency_key(key)      # check-then-act
    if existing:
        return {"ticket_id": existing.id}
    t = await tickets.create(..., idempotency_key=key)          # or provider-native idempotency
    return {"ticket_id": t.id}
```

Rules of thumb:

1. Prefer **upserts** over inserts.
2. Use a **deterministic idempotency key** derived from state (never `uuid4()` at call time).
3. Put non-idempotent effects **after** any `interrupt()` in the node, or in their own node.
4. Record the effect's result in state so a retry can short-circuit.

### Saga / compensation

```python
builder.add_node("reserve_inventory", reserve, error_handler=lambda s, e: Command(goto="fail"))
builder.add_node("charge", charge, error_handler=lambda s, e: Command(goto="release_inventory"))
builder.add_node("release_inventory", release)      # compensating action
builder.add_node("ship", ship, error_handler=lambda s, e: Command(goto="refund"))
builder.add_node("refund", refund)
```

Compensation nodes must themselves be idempotent and should have their own retry policies.

## 3. Scenarios

| Scenario | Configuration |
|---|---|
| Flaky third-party API | `RetryPolicy(max_attempts=5, max_interval=30)` + `run_timeout` + circuit-breaker in the client |
| LLM provider rate limits | Retry with jitter + `ModelFallbackMiddleware` to a second provider |
| Model call that occasionally hangs | `TimeoutPolicy(idle_timeout=30)` — token streaming refreshes the clock, a true hang doesn't |
| 20-minute batch node | `idle_timeout` + `refresh_on="heartbeat"` + explicit `runtime.heartbeat()` |
| K8s rolling deploy | SIGTERM → `request_drain()` → `GraphDrained` → resume on the next pod; `terminationGracePeriodSeconds` > longest node |
| Payment flow | `durability="sync"`, idempotency keys, compensation nodes, HITL on high amounts |
| Every run maps to a job row | `set_node_defaults(error_handler=mark_job_failed)` so no failure goes unrecorded |

## 4. Staff-level considerations

- **Retry budgets multiply.** Middleware retries × node retries × client-library retries × queue
  redelivery = 3×3×3 attempts against a struggling downstream. Choose **one** layer to own retries
  per dependency and disable the others.
- **Timeouts must be layered and consistent**: client timeout < node `run_timeout` < worker run
  budget < queue lease < HTTP gateway timeout. Write the ladder down; misordered timeouts produce
  duplicate work and orphaned runs.
- **`durability` is your crash-recovery dial** ([07](07-persistence-and-checkpointers.md)). `exit`
  means a crash loses the whole run; `sync` means every step survives. Choose per run type.
- **Drain is the missing piece for Kubernetes.** Without it, a rolling deploy kills in-flight runs
  mid-super-step and they restart from the last checkpoint (or from zero under `durability="exit"`).
  Wire SIGTERM → drain in every worker, and set the grace period above your longest node timeout.
- **Idempotency is a design property, not a library.** Enumerate every external effect in the graph
  and document its key. Do this in the design doc; retrofitting is painful.
- **Error handlers change the shape of your metrics.** A handled failure is no longer an exception —
  make sure handlers emit metrics/traces, or your dashboards will show a healthy system quietly
  failing every run.
- **Poison messages**: a run that fails deterministically will retry, drain, resume and fail forever.
  Add an attempt cap in state and a dead-letter path.

## 5. Anti-patterns

| Anti-pattern | Consequence |
|---|---|
| Bare `except Exception` around `interrupt()` | Swallows the pause |
| Retrying non-idempotent side effects | Duplicate charges/emails/tickets |
| `retry_on=Exception` (retry everything) | Retries `ValueError` bugs 3× and hides them |
| Sync node + `timeout=` | Compile-time rejection (and blocking the loop anyway) |
| No timeout on external calls | One hung call holds a run slot indefinitely |
| Error handler that swallows and returns success | Silent data loss |
| `set_node_defaults` assumed to apply to subgraphs | Subgraph nodes run unprotected |
| No SIGTERM handling | Every deploy kills in-flight work |

## 6. Design-review questions

1. Draw the timeout ladder end-to-end. Is it monotonic?
2. Which layer owns retries for each dependency? Are the others disabled?
3. List every external side effect and its idempotency key.
4. What happens to in-flight runs during a rolling deploy?
5. What is the `durability` setting for each run type, and why?
6. Where do handled errors show up in metrics and alerts?
7. What stops a deterministically-failing run from retrying forever?

## References

- `/oss/python/langgraph/fault-tolerance`
- `/oss/python/langgraph/use-graph-api` (retry policies, node timeouts, error handling, defaults)
- `/oss/python/deepagents/fault-tolerance`
- `/oss/python/langgraph/checkpointers#durability-modes`
