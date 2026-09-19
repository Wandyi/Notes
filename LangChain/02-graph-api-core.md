# 02 — Graph API Core (`StateGraph`)

## 1. Concepts

A LangGraph application is three things:

1. **State** — a shared, typed data structure (`TypedDict`, Pydantic model, or dataclass) whose
   fields are *channels*.
2. **Nodes** — functions `(state) -> partial_state_update`, optionally taking `runtime` / `config`.
3. **Edges** — which node runs next: static (`add_edge`), conditional (`add_conditional_edges`), or
   dynamic (`Command(goto=...)`, `Send(...)`).

> Nodes do the work, edges decide what happens next. If your edge function is doing work, it belongs
> in a node; if your node is doing routing, consider returning `Command`.

`START` and `END` are virtual nodes. `builder.compile()` produces a `Pregel` object exposing
`invoke` / `stream` / `ainvoke` / `astream` / `batch`, plus `get_state`, `get_state_history`,
`update_state`.

### Schemas: the state is not one blob

You get four distinct schema surfaces, and using them is what keeps a large graph maintainable:

| Schema | Purpose |
|---|---|
| **State schema** | The full internal state (all channels) |
| **Input schema** | What callers may pass in (`input_schema=`) |
| **Output schema** | What callers see (`output_schema=`) |
| **Context schema** | Runtime dependencies, not state (`context_schema=`) |

Nodes may also declare **narrower private schemas** so that intermediate data flows between two
specific nodes without polluting the public state.

### Compile-time vs runtime

Compile time: topology, node defaults, cache, checkpointer, store, interrupts.
Runtime: `config` (`thread_id`, `recursion_limit`, tags, metadata), `context` (typed deps),
`durability`, `stream_mode`.

Nothing that varies per request should be baked in at compile time — that is what `context` is for.

## 2. How to implement

### Minimal graph

```python
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END

class State(TypedDict):
    question: str
    answer: str

def retrieve(state: State) -> dict:
    return {"context": search(state["question"])}

def answer(state: State) -> dict:
    return {"answer": llm.invoke(state["question"]).content}

graph = (
    StateGraph(State)
    .add_node("retrieve", retrieve)
    .add_node("answer", answer)
    .add_edge(START, "retrieve")
    .add_edge("retrieve", "answer")
    .add_edge("answer", END)
    .compile()
)
```

`add_node(fn)` infers the node name from the function name; `add_node("name", fn)` is explicit —
prefer explicit names in shared codebases, because node names are part of your **observable API**
(they appear in traces, streams, `update_state(as_node=...)` and checkpoint metadata).

### Input / output schema separation

```python
class InputState(TypedDict):
    question: str

class OutputState(TypedDict):
    answer: str

class OverallState(InputState, OutputState):
    retrieved_docs: list[str]
    scratch: dict

graph = StateGraph(OverallState, input_schema=InputState, output_schema=OutputState).compile()
```

Callers cannot inject `scratch`, and never see it. This is a security boundary as much as an
ergonomic one ([22](22-security-guardrails-multitenancy.md)).

### Private channels between two nodes

```python
class Overall(TypedDict):
    user_input: str
    final: str

class Node1Output(TypedDict):
    private_payload: str          # not part of Overall

class Node2Input(TypedDict):
    private_payload: str

def node_1(state: Overall) -> Node1Output:
    return {"private_payload": heavy_intermediate(state["user_input"])}

def node_2(state: Node2Input) -> Overall:
    return {"final": summarize(state["private_payload"])}
```

Node 3 never sees `private_payload`. Use this to keep large intermediates out of the public
contract — but note they are still checkpointed ([07](07-persistence-and-checkpointers.md)).

### Runtime context (dependency injection)

```python
from dataclasses import dataclass
from langgraph.runtime import Runtime

@dataclass
class Context:
    llm_provider: str = "openai"
    tenant_id: str | None = None

def node_a(state: State, runtime: Runtime[Context]):
    model = get_llm(runtime.context.llm_provider)
    ...

graph = StateGraph(State, context_schema=Context).compile()
graph.invoke(inputs, context={"llm_provider": "anthropic", "tenant_id": "acme"})
```

Context is **not** persisted in checkpoints and **not** part of state — exactly right for
connections, tenant ids, feature flags and provider selection.

### Node caching

```python
from langgraph.cache.memory import InMemoryCache
from langgraph.types import CachePolicy

builder.add_node("expensive", expensive_node, cache_policy=CachePolicy(ttl=300))
graph = builder.compile(cache=InMemoryCache())
```

`CachePolicy` takes `key_func` (defaults to a pickle hash of the node input) and `ttl` seconds.
Cached results are flagged in the stream: `{'__metadata__': {'cached': True}}`.

Use for deterministic, expensive, side-effect-free nodes (embedding a fixed corpus, schema lookups,
pure enrichment). Never cache nodes that write externally.

### Recursion limit and graceful degradation

Default is **1000 super-steps** (since 1.0.6). Set per invocation, at the top level of `config`
(not inside `configurable`):

```python
graph.invoke(inputs, config={"recursion_limit": 25})
```

Prefer proactive handling with the `RemainingSteps` managed value over catching
`GraphRecursionError`:

```python
from langgraph.managed import RemainingSteps

class State(TypedDict):
    messages: Annotated[list, add_messages]
    remaining_steps: RemainingSteps

def route(state: State) -> Literal["reason", "wrap_up"]:
    return "wrap_up" if state["remaining_steps"] <= 2 else "reason"
```

The raw counter is also at `config["metadata"]["langgraph_step"]`.

### Visualisation

`graph.get_graph().draw_mermaid()` / `.draw_mermaid_png()`. Commit the Mermaid output to the repo
and diff it in code review — topology changes then become visible in PRs.

## 3. Scenarios

- **Public API boundary**: `input_schema` / `output_schema` let you evolve internal state without
  breaking clients, and stop a caller from smuggling in `is_admin: true`.
- **Multi-tenant deployment**: tenant id, DB handle and model choice go in `context_schema`, never
  in state; state stays tenant-agnostic and checkpoints stay portable.
- **Expensive deterministic preprocessing** (OCR, parsing, embedding) → node cache with a stable
  `key_func` over a content hash.
- **Runaway agent loops** in production → `RemainingSteps` + a `wrap_up` node produces a graceful
  best-effort answer instead of a 500.

## 4. Staff-level considerations

- **Node names are an API.** Renaming a node invalidates `update_state(as_node=...)` calls,
  dashboards, alert rules and any stored resume logic. Treat renames as breaking changes.
- **Keep node granularity aligned with checkpoint boundaries.** A checkpoint is written per
  super-step; five tiny nodes cost five checkpoint writes. Merge trivial nodes; split nodes whose
  failure you want to retry independently.
- **Every node should be re-executable.** Retries, replay and resume all re-run nodes. Design for
  idempotency from day one ([17](17-durability-fault-tolerance-idempotency.md)).
- **Compile once, at import time.** The Agent Server loads graphs at startup; building a graph per
  request wastes CPU and defeats caching. If you need per-request variation, use `context` or an
  assistant, not a new graph.
- **Prefer a graph factory only when you must** (e.g. per-tenant tool sets). `langgraph.json` can
  point at a function that returns a graph, but be aware you lose static discoverability of
  subgraph state.

## 5. Anti-patterns

| Anti-pattern | Consequence | Fix |
|---|---|---|
| One giant `State` with 40 optional fields | Huge checkpoints, unclear ownership, merge conflicts | Split into private schemas + store-backed data |
| Business logic in conditional-edge functions | Invisible in traces, untestable, no retries | Move to a node; return `Command` |
| Passing DB connections through state | Serialization failures, secrets in checkpoints | `context_schema` |
| `recursion_limit` bumped to silence errors | Cost explosions, runaway loops | Bound the loop; use `RemainingSteps` |
| Rebuilding the graph per request | Latency, memory churn | Compile at module scope |

## 6. Design-review questions

1. What is in the input schema, and can a hostile caller set anything they shouldn't?
2. Which channels are large, and do they need to be in state at all?
3. Which nodes are pure? Are they cached? Which are side-effecting? Are they idempotent?
4. What happens at `recursion_limit`? Is there a graceful terminal node?
5. Does the compiled topology diff show up in code review?

## References

- `/oss/python/langgraph/graph-api`
- `/oss/python/langgraph/use-graph-api`
- `/oss/python/langgraph/thinking-in-langgraph`
