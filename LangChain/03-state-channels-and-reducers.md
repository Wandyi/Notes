# 03 — State, Channels & Reducers

> This is the highest-leverage file in the set. State design determines checkpoint size, write
> amplification, replay cost, parallel-write correctness and how much of your context window you
> burn. Get it wrong and no amount of infra tuning saves you.

## 1. Concepts

### Channels

Each key in your state schema is a **channel**. A channel has a value type, an update type and an
update function. Built-in channel types:

| Channel | Semantics | Use for |
|---|---|---|
| `LastValue` (default) | Stores the last write; overwrites | Scalars, current step's output, config-like fields |
| `Topic` | PubSub; `accumulate=True` keeps all writes across steps; can deduplicate | Fan-in of many producers, event logs |
| `BinaryOperatorAggregate` | Applies a binary op at **write time** (`operator.add`, `max`, …) | Running totals, counters, cost accumulators |
| `EphemeralValue` | Value visible only for the next step | Transient hand-offs |
| `DeltaChannel` (1.2+, beta) | Persists only per-step deltas; reducer runs at **read/reconstruction** time | Large append-heavy channels (long message histories) |

### Reducers

A reducer answers: *when two writes land on this channel in one super-step, what is the result?*

```python
from typing import Annotated
from operator import add

class State(TypedDict):
    foo: str                          # LastValue: last write wins
    bar: Annotated[list[str], add]    # concatenate
```

Without a reducer, **two parallel nodes writing the same channel in the same super-step is an
error** (`InvalidUpdateError`). With a reducer, order of application is not guaranteed to be
meaningful — so reducers must be **commutative and associative** if parallel writes are possible.

`add_messages` (used by `MessagesState` / `AgentState`) is the important special case: it appends,
but **upserts by message `id`**, so re-emitting a message with the same id replaces it rather than
duplicating. That is what makes message editing, deletion (`RemoveMessage`) and replay work.

### Bypassing a reducer

`Overwrite` lets a node replace a reduced channel instead of accumulating:

```python
from langgraph.types import Overwrite

def compact(state: State) -> dict:
    return {"messages": Overwrite([summary_message])}
```

Essential for summarisation/compaction — otherwise your "compaction" appends to the thing you were
trying to shrink.

### `DeltaChannel` — the scaling primitive

Default checkpointing writes the **full value of every channel at every super-step**. A 50-turn
conversation with a 200 KB message list therefore writes ~200 KB × N times. `DeltaChannel` stores
only the writes from each step and reconstructs on read.

```python
from langgraph.channels import DeltaChannel

def list_reducer(state: list, writes: Sequence[list]) -> list:
    result = list(state)
    for w in writes:
        result.extend(w)
    return result

class State(TypedDict):
    messages: Annotated[list[str], DeltaChannel(list_reducer, snapshot_frequency=5)]
```

Hard rules (from the docs, and they bite):

- The reducer is a **bulk reducer**: `(state, Sequence[writes]) -> state`, not pairwise.
- It **must be associative**: `reducer(reducer(s, xs), ys) == reducer(s, [*xs, *ys])`.
- It runs **on reconstruction, not on write**. So it must be pure — no `uuid4()`, no
  `datetime.now()`, no mutation of incoming writes. Assign stable ids **upstream**.
- Reads without snapshots are O(N) in thread length; `snapshot_frequency=K` bounds it to K steps at
  the cost of periodic full writes.
- **Downgrading LangGraph after using `DeltaChannel` leaves those threads unreadable.** Treat
  adoption as a one-way door per thread; plan a migration/dump path before enabling it.

## 2. How to implement — state design rules

### Rule 1: classify every field

For each field, answer three questions and record the answer in the code:

| Question | If yes → |
|---|---|
| Must it survive a process crash mid-run? | State (checkpointed) |
| Must it survive across threads/sessions? | Store ([08](08-stores-and-long-term-memory.md)) |
| Is it a dependency, not data? | `context_schema` |
| Is it large and referenced rarely? | External blob store; keep a pointer in state |

```python
class State(TypedDict):
    # --- conversation (checkpointed, delta-friendly) ---
    messages: Annotated[list[AnyMessage], add_messages]
    # --- control (small, LastValue) ---
    stage: Literal["triage", "resolve", "verify"]
    attempt: int
    # --- accumulators (must be commutative for parallel fan-in) ---
    cost_usd: Annotated[float, operator.add]
    findings: Annotated[list[Finding], operator.add]
    # --- pointers, not payloads ---
    report_uri: str | None        # s3://... not the 4 MB report
```

### Rule 2: make parallel fan-in explicit

Map-reduce with `Send` writes N results into one channel in one super-step. That channel **must**
have a reducer, and the reducer must not care about order:

```python
class Overall(TypedDict):
    subjects: list[str]
    jokes: Annotated[list[str], operator.add]   # required for Send fan-in
```

If order matters, write `(index, value)` tuples and sort in the aggregation node — do not rely on
completion order.

### Rule 3: use Pydantic for validated boundaries, TypedDict for hot paths

```python
from pydantic import BaseModel

class State(BaseModel):
    question: str
    score: int = 0
```

Pydantic gives you runtime validation of node outputs (catches a node returning garbage early), at
the cost of validation overhead per super-step and stricter serialization. A common split: Pydantic
for the input schema, `TypedDict` for the internal overall state.

### Rule 4: compact deliberately

```python
from langchain.agents.middleware import SummarizationMiddleware

agent = create_agent(
    model="claude-sonnet-4-6",
    tools=tools,
    middleware=[SummarizationMiddleware(max_tokens_before_summary=60_000)],
)
```

For raw graphs, do it yourself in a node that returns `Overwrite([...])` with a summary message plus
the last K turns, and stash the full transcript in the store or a file backend.

### Rule 5: prune messages, don't just append

```python
from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

def trim(state: State) -> dict:
    keep = state["messages"][-20:]
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *keep]}
```

## 3. Scenarios

| Scenario | State design |
|---|---|
| Long-running chat (hundreds of turns) | `DeltaChannel` on messages + `snapshot_frequency`, summarisation middleware, thread TTL |
| Parallel document analysis (fan-out 500) | `Send` + `Annotated[list, add]` results channel; results carry an index; per-item errors captured as values, not exceptions |
| Multi-stage approval workflow | Small `LastValue` control channels (`stage`, `approver`, `decision`); the payload lives in object storage |
| Cost-capped agent | `Annotated[float, operator.add]` cost channel written by a `wrap_model_call` middleware; a `before_model` hook jumps to `end` when exceeded |
| Multi-tenant | No tenant data in state schema defaults; tenant id in context; store namespaces keyed by tenant |

## 4. Staff-level considerations

- **Checkpoint size is a first-class SLO.** Instrument it. If checkpoint bytes grow linearly with
  thread length for a channel, that channel is a `DeltaChannel` candidate — or shouldn't be in state.
- **Serialization is a compatibility contract.** The default `JsonPlusSerializer` (ormsgpack + JSON)
  handles LangChain primitives, datetimes, enums. Custom classes in state either need to be
  serializable or you fall back to `pickle_fallback=True` — which then couples your checkpoints to
  your Python class definitions. Avoid putting rich domain objects in state; put dicts.
- **Reducers are distributed-systems code.** Non-associative reducers produce results that depend on
  batching. Under `DeltaChannel` this becomes observable as *state that changes when you replay it*.
- **Message ids are the idempotency key of the conversation.** Generate them upstream and keep them
  stable across retries, or `add_messages` will duplicate on replay.
- **Beware reducers that silently grow.** `operator.add` on a list in a loop is an unbounded memory
  leak wearing a type annotation. Every accumulator needs a documented bound.

## 5. Anti-patterns

- Storing raw file/document bytes, dataframes or embeddings in state.
- A single `messages` channel used both as the model's context and as the audit log. Split them:
  the model sees a trimmed view, the audit log lives in the store or a warehouse.
- Adding a reducer "just in case" — it hides genuine parallel-write bugs that `LastValue` would
  surface as a loud error.
- Non-deterministic reducers (timestamps, uuids) — breaks replay, forking and delta channels.
- Using state as a cross-thread cache. That's what the store is for.

## 6. Design-review questions

1. For each channel: who writes it, can two writers collide in one super-step, and is the reducer
   commutative?
2. What is the p50/p99 serialized checkpoint size, and how does it grow with thread length?
3. Which channels would break if we replayed the thread tomorrow on a new code version?
4. Is there any object in state whose class definition we might rename or delete?
5. What is the largest single value we ever put in state, and why isn't it a URI?

## References

- `/oss/python/langgraph/graph-api` (State, Reducers, Messages)
- `/oss/python/langgraph/use-graph-api` (Overwrite, private state, Pydantic state)
- `/oss/python/langgraph/pregel` (channel types, `DeltaChannel`)
- `/oss/python/langgraph/checkpointers` (Optimize checkpoint storage, serializers)
