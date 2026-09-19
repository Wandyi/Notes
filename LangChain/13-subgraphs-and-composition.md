# 13 — Subgraphs & Composition

## 1. Concepts

A subgraph is a compiled graph used inside another graph. It is the unit of **modularity, team
ownership and reuse** — and the place where checkpoint namespaces, state schemas and persistence
scoping interact in ways that surprise people.

### Two ways to attach a subgraph

| Method | When | State handling |
|---|---|---|
| **As a node**: `builder.add_node("sub", subgraph)` | Parent and subgraph **share state keys** | Shared keys flow automatically; unshared keys are invisible to the subgraph |
| **Called inside a node**: `def node(state): return transform(subgraph.invoke(map_(state)))` | Schemas **differ** | You explicitly map parent state → subgraph input and back |

Adding as a node is cleaner and keeps the subgraph statically discoverable (needed for
`get_state(subgraphs=True)` and Studio visualisation). Calling inside a node gives you an
anti-corruption layer between two teams' schemas — usually the right choice across org boundaries.

### Checkpoint namespaces

Subgraph checkpoints get `checkpoint_ns = "node_name:uuid"`, nested with `|`. This namespace is what
isolates (or collides) parallel subgraph invocations.

### Persistence scoping — the three modes

`subgraph_builder.compile(checkpointer=...)`:

| Mode | `checkpointer=` | Behaviour |
|---|---|---|
| **Per-invocation** (default) | `None` | Fresh each call; inherits the parent's checkpointer within a call, so interrupts and durable execution work |
| **Per-thread** | `True` | State accumulates across calls on the same thread |
| **Stateless** | `False` | No checkpointing at all; a plain function call |

Capability matrix:

| Feature | Per-invocation | Per-thread | Stateless |
|---|:--:|:--:|:--:|
| Interrupts (HITL) | ✅ | ✅ | ❌ |
| Multi-turn memory | ❌ | ✅ | ❌ |
| Multiple calls (different subgraphs) | ✅ | ⚠️ namespace conflicts | ✅ |
| Multiple calls (same subgraph, parallel) | ✅ | ❌ | ✅ |
| State inspection | ⚠️ current invocation only | ✅ | ❌ |

**Per-invocation is the right default**, including for multi-agent systems where subagents handle
independent requests.

The parent graph must be compiled with a checkpointer for any of the stateful features to work.

### The per-thread parallel-call trap

If an LLM has a per-thread subagent as a tool, it may call it twice in parallel ("ask the fruit
expert about apples *and* bananas"). Both calls write to the same checkpoint namespace → conflict.
Mitigations:

- `ToolCallLimitMiddleware` to prevent parallel invocation
- Disable parallel tool calling on the model
- Or just use per-invocation persistence

## 2. How to implement

### Shared-schema subgraph as a node

```python
class State(TypedDict):
    messages: Annotated[list, add_messages]
    findings: Annotated[list[str], operator.add]

sub = StateGraph(State).add_node(...).compile()          # shares `messages`, `findings`

parent = (
    StateGraph(State)
    .add_node("prepare", prepare)
    .add_node("analysis", sub)                            # attached as a node
    .add_edge(START, "prepare")
    .add_edge("prepare", "analysis")
    .compile(checkpointer=checkpointer)
)
```

### Different-schema subgraph called inside a node (anti-corruption layer)

```python
class ParentState(TypedDict):
    ticket: dict
    resolution: str

class SubState(TypedDict):
    question: str
    answer: str

sub = StateGraph(SubState)...compile()

def analysis_node(state: ParentState) -> dict:
    out = sub.invoke({"question": render_question(state["ticket"])})
    return {"resolution": out["answer"]}
```

Every schema change on either side is now a deliberate, reviewable mapping change.

### Routing from a subgraph into the parent

```python
def escalate(state: SubState) -> Command[Literal["human_review"]]:
    return Command(update={"reason": "low confidence"},
                   goto="human_review", graph=Command.PARENT)
```

If the updated key exists in both schemas, the **parent must define a reducer** for it.

### Inspecting subgraph state

```python
snap = graph.get_state(config, subgraphs=True)
for task in snap.tasks:
    if task.state:
        print(task.name, task.state.values)
```

Only works when the subgraph is **statically discoverable** — added as a node or called directly in
a node. It does **not** work when the subgraph is invoked from inside a tool function (the subagents
pattern). Interrupts still propagate to the top-level graph regardless of nesting.

### Streaming subgraph output

```python
for chunk in graph.stream(inputs, stream_mode="updates", subgraphs=True, version="v2"):
    ns, payload = chunk["namespace"], chunk["data"]
```

Without `subgraphs=True`, the parent stream shows the subgraph node as a single opaque update.

## 3. Scenarios

| Scenario | Composition |
|---|---|
| Platform team owns "retrieval", product teams consume it | Subgraph called inside a node, versioned package, explicit input/output schema |
| Reusable "approve → execute → verify" pattern across 6 workflows | Subgraph as a node with a shared control schema |
| Specialist subagent invoked as a tool | Per-invocation persistence; accept that `get_state(subgraphs=True)` won't see it; rely on LangSmith traces |
| Research assistant that must remember across calls in one session | Per-thread (`checkpointer=True`) + `ToolCallLimitMiddleware` to prevent parallel calls |
| Pure function step (formatting, validation) | Stateless (`checkpointer=False`) — no checkpoint overhead |
| Migrating a monolith graph | Extract cohesive node clusters into subgraphs one at a time; keep the parent's state stable |

## 4. Staff-level considerations

- **Subgraph boundaries should follow team boundaries and deploy cadence**, not code aesthetics. If
  two clusters of nodes always change together, don't split them.
- **The schema at the boundary is your API.** Prefer explicit mapping (called-inside-a-node) over
  shared state across org boundaries — shared state means a field rename in one team breaks another.
- **Each stateful subgraph invocation adds checkpoint writes** in its own namespace. Nested
  subgraphs multiply this. For hot paths made of pure transformations, use `checkpointer=False`.
- **Observability degrades with nesting.** Deeply nested subgraphs plus `Command.PARENT` jumps make
  traces hard to read. Add consistent `tags`/`metadata` per subgraph and stream with
  `subgraphs=True` in debugging tools.
- **Interrupts inside subgraphs propagate to the top level** — good, but the resume payload must be
  routed back correctly. Test HITL inside nested subgraphs explicitly; it is a common gap.
- **Versioning**: a subgraph published as a package needs semver, a changelog and compatibility
  guarantees for state it persists. Treat the state schema as part of the public API
  ([26](26-migration-and-versioning.md)).

## 5. Anti-patterns

- Per-thread subagents exposed as parallel-callable tools (checkpoint namespace conflicts).
- Sharing the entire parent state with every subgraph "for convenience" — creates invisible coupling
  and cross-team breakage.
- Nesting four levels deep. Two is usually the practical limit for debuggability.
- Compiling a subgraph with its own concrete checkpointer instance (rather than `True`/`False`/`None`)
  inside a deployed app — it bypasses the platform's managed persistence.
- Expecting `get_state(subgraphs=True)` to work for tool-invoked subgraphs.
- Stateless subgraphs for long, expensive work — a crash restarts them from zero.

## 6. Design-review questions

1. Why is this a subgraph? Which team owns it, and on what release cadence?
2. Is the boundary shared-state or explicitly mapped? What breaks on a rename?
3. What persistence mode does each subgraph use, and was that a decision or a default?
4. Can any per-thread subgraph be invoked in parallel? What prevents it?
5. How deep does nesting go, and can we still read a production trace?
6. Do interrupts inside subgraphs resume correctly? Is there a test?

## References

- `/oss/python/langgraph/use-subgraphs`
- `/oss/python/langgraph/checkpointers` (checkpoint namespaces)
- `/oss/python/langchain/middleware/overview` (agents as nodes in a workflow)
