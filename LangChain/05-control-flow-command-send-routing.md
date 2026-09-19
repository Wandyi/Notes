# 05 — Control Flow: Conditional Edges, `Send`, `Command`, Loops

## 1. Concepts

LangGraph gives you four routing mechanisms, in increasing order of dynamism:

| Mechanism | Decides | Can update state? | Use when |
|---|---|---|---|
| `add_edge(a, b)` | Static topology | No | The order is fixed |
| `add_conditional_edges(a, fn, path_map)` | Next node(s) from state | No | Branching on a computed value |
| `Send("node", payload)` | Dynamic fan-out with **per-branch state** | Implicitly (the payload is the branch's input) | Map-reduce, unknown-width parallelism |
| `Command(update=..., goto=...)` | Both at once | Yes | Handoffs, agent routing, error recovery |

### `Send`

Returned from a conditional edge, `Send(node_name, state)` schedules `node_name` with a *different*
state object than the shared graph state. This is how map-reduce works when the number of branches
is unknown at build time.

```python
from langgraph.types import Send

def fan_out(state: Overall):
    return [Send("summarize", {"doc": d}) for d in state["docs"]]

builder.add_conditional_edges("load", fan_out, ["summarize"])
```

The target node's results fan back into the parent state, so the receiving channel **must have a
reducer** ([03](03-state-channels-and-reducers.md)).

`Send` also accepts a per-dispatch `timeout=` (1.2+), overriding the node's static timeout:

```python
Send("process_item", {"item": item}, timeout=TimeoutPolicy(idle_timeout=15))
```

### `Command`

Four parameters:

- `update` — state update (same as returning a dict)
- `goto` — next node(s)
- `graph` — `Command.PARENT` to route into the parent graph from a subgraph
- `resume` — only as **input** to `invoke`/`stream`, to resume an interrupt

```python
def triage(state: State) -> Command[Literal["billing", "technical"]]:
    label = classify(state)
    return Command(update={"label": label}, goto=f"{label}")
```

Two rules that cause real bugs:

1. **You must annotate the return type** with the reachable node names
   (`Command[Literal["a", "b"]]`) — the graph renderer and validator depend on it.
2. **`Command` adds dynamic edges; static edges still fire.** If `node_a` returns
   `Command(goto="x")` *and* you declared `add_edge("node_a", "node_b")`, both `x` and `node_b`
   run. Pick one mechanism per node.

Also: `Command(update=...)` is **not** a way to continue a conversation. Passing any `Command` as
input resumes from the latest checkpoint, so on a finished thread the graph appears stuck. To add a
new user turn, pass a plain dict.

```python
# WRONG
graph.invoke(Command(update={"messages": [...]}), config)
# RIGHT
graph.invoke({"messages": [{"role": "user", "content": "follow up"}]}, config)
```

### `Command.PARENT` (handoffs)

```python
def escalate(state: State) -> Command[Literal["human_review"]]:
    return Command(update={"reason": "low confidence"},
                   goto="human_review", graph=Command.PARENT)
```

If the updated key exists in both parent and subgraph schemas, the **parent must define a reducer**
for it.

### `Command` from tools

Tools can return `Command` to update agent state and route — the mechanism behind multi-agent
handoffs:

```python
from langchain.tools import tool, InjectedToolCallId
from langgraph.types import Command
from typing import Annotated

@tool
def transfer_to_billing(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    return Command(
        goto="billing_agent",
        update={"messages": [ToolMessage("Transferred", tool_call_id=tool_call_id)]},
        graph=Command.PARENT,
    )
```

### Loops

A loop is just a cycle in the graph plus a conditional edge that can exit. Bound it with:

- `recursion_limit` (hard stop, raises `GraphRecursionError`)
- `RemainingSteps` (graceful degradation)
- Domain counters in state (`attempt`, `refinements`)
- `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware` for agent loops

## 2. How to implement

### Map-reduce with deferred aggregation

```python
import operator
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

class Overall(TypedDict):
    docs: list[str]
    summaries: Annotated[list[str], operator.add]
    report: str

class DocState(TypedDict):
    doc: str

def summarize(state: DocState) -> dict:
    return {"summaries": [llm.invoke(f"Summarize: {state['doc']}").content]}

def fan_out(state: Overall):
    return [Send("summarize", {"doc": d}) for d in state["docs"]]

def reduce_(state: Overall) -> dict:
    return {"report": synthesize(state["summaries"])}

builder = StateGraph(Overall)
builder.add_node("summarize", summarize)
builder.add_node("reduce", reduce_, defer=True)      # wait for every branch
builder.add_conditional_edges(START, fan_out, ["summarize"])
builder.add_edge("summarize", "reduce")
builder.add_edge("reduce", END)
graph = builder.compile()
```

`defer=True` is what makes the aggregator wait for all branches even when they have different depths.

### Chunked fan-out (production-safe)

```python
BATCH = 25

def fan_out(state: Overall):
    docs = state["docs"]
    return [
        Send("summarize_batch", {"docs": docs[i:i + BATCH]})
        for i in range(0, len(docs), BATCH)
    ]

async def summarize_batch(state: BatchState) -> dict:
    sem = asyncio.Semaphore(5)
    async def one(d):
        async with sem:
            return (await llm.ainvoke(f"Summarize: {d}")).content
    return {"summaries": list(await asyncio.gather(*(one(d) for d in state["docs"])))}
```

This keeps task count, pending-write count and downstream QPS bounded — the difference between a
graph that works at 50 docs and one that works at 50,000.

### Ordering results deterministically

```python
def summarize(state: DocState) -> dict:
    return {"summaries": [(state["index"], summary)]}

def reduce_(state: Overall) -> dict:
    ordered = [s for _, s in sorted(state["summaries"])]
```

### Retry-with-refinement loop

```python
def route(state: State) -> Literal["refine", "done"]:
    if state["score"] >= 0.8 or state["attempt"] >= 3:
        return "done"
    return "refine"

builder.add_conditional_edges("evaluate", route, {"refine": "generate", "done": END})
```

Always bound loops with **both** a quality condition and a hard attempt cap.

## 3. Scenarios

| Scenario | Mechanism |
|---|---|
| Classify then route to one of six specialist agents | Conditional edge, or `Command(update, goto)` if you also record the label |
| Analyse 500 log files and produce one report | `Send` (chunked) + reducer channel + `defer=True` aggregator |
| Customer-support handoff between agents | Tool returning `Command(goto=..., graph=Command.PARENT)` |
| Retry a generation until a grader approves | Cycle + `attempt` counter + `RemainingSteps` fallback |
| Compensating transaction after a failed payment | `error_handler` returning `Command(goto="compensate")` ([17](17-durability-fault-tolerance-idempotency.md)) |
| Per-item deadline in a fan-out | `Send(..., timeout=TimeoutPolicy(...))` |

## 4. Staff-level considerations

- **Prefer static edges wherever the domain is static.** Every dynamic edge is a place where the
  topology diagram lies, and where a trace cannot be predicted. Auditors and on-call engineers read
  topology.
- **`Command` couples a node to its successors' names.** In a large graph this creates a rename
  hazard and hidden coupling. For cross-team boundaries, prefer conditional edges with an explicit
  `path_map` so the routing table is in one place.
- **Fan-out width is a capacity decision, not a code detail.** Document the max width and the
  downstream rate limit it implies. Put a guard in the fan-out function
  (`if len(items) > MAX: raise`) rather than discovering it in production.
- **`defer=True` changes failure semantics**: the aggregator sees partial results if branches fail
  and errors are swallowed. Decide explicitly whether a branch failure fails the run or degrades it,
  and encode per-branch errors as values in the reducer channel.
- **Handoffs are hard to observe.** With `Command.PARENT` in a deeply nested graph, the trace shows
  a jump with no edge. Add explicit metadata/tags to every handoff for LangSmith filtering.

## 5. Anti-patterns

- Mixing `Command(goto=...)` and a static `add_edge` on the same node (both fire).
- `Command(update=...)` as invoke input to continue a conversation (resumes from last checkpoint;
  looks hung).
- Unbounded `Send` derived directly from user input (`Send` per row of an uploaded CSV).
- Routing logic duplicated in both a conditional edge and a node.
- Missing `Command[Literal[...]]` annotations — silently breaks visualisation and validation.
- Loops bounded only by `recursion_limit`.

## 6. Design-review questions

1. What is the maximum fan-out width, and what rate limit does it hit downstream?
2. Does every loop have a domain-level bound in addition to `recursion_limit`?
3. Which nodes route with `Command`, and do any of them also have static outgoing edges?
4. If one branch of a fan-out fails, does the run fail, degrade, or silently drop data?
5. Can an on-call engineer reconstruct the executed path from the trace alone?

## References

- `/oss/python/langgraph/graph-api` (`Send`, `Command`, conditional edges)
- `/oss/python/langgraph/use-graph-api` (map-reduce, branches, defer, loops)
- `/oss/python/langchain/multi-agent/handoffs`
