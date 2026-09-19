# 26 — Migration, Versioning & Backward Compatibility

> LangGraph does **not** pin a run to the code version it started with. The latest deployed graph
> runs against *every* thread, including ones resuming from an old checkpoint. Every deploy is
> therefore a backward-compatible API change with respect to persisted state.

## 1. Concepts

Three categories of compatibility, in the order you'll meet them:

1. **Technical compatibility** — the new code must load and execute against existing state.
2. **Business compatibility** — technically valid, but existing runs should keep the *old* logic.
3. **Non-determinism** — Functional API only (and `@task`/`interrupt` inside Graph API nodes).

### What the runtime supports out of the box

| Change | Completed threads | Interrupted / in-flight threads |
|---|---|---|
| Any topology change (add/remove/rename nodes, edges) | ✅ | ⚠️ everything **except renaming/removing a node** |
| Adding or removing a state key | ✅ (full backward + forward compat) | ✅ |
| **Renaming** a state key | Loses saved state for existing threads | Loses saved state |
| **Incompatible type change** on a state key | May break | May break |

Edge topology is **not persisted** — adding, removing or rerouting edges between nodes that still
exist is safe for in-flight threads. The only topology change that breaks an interrupted thread is
renaming or removing a node (execution resumes at the *start of the node* where it stopped; a
missing node has nowhere to resume from).

### Common technical breakages

- Renaming/removing a node while threads are parked at (or routing to) it.
- Renaming/removing a state key that old checkpoints contain or downstream nodes read.
- **Tightening** a state field: making `Optional` required, narrowing a type, adding a required
  field with no default.

## 2. How to implement

### Recommended patterns (technical compatibility)

```python
from typing import NotRequired
from typing_extensions import TypedDict

class State(TypedDict):
    messages: list
    summary: NotRequired[str]        # new fields are always NotRequired / Optional with a default
```

1. **Add new state fields as `NotRequired`** (or `Optional[...] = None`).
2. **Treat removals as deprecations** — keep the field defined for at least one drain cycle.
3. **Rename via add-then-remove**: add the new field/node alongside the old, dual-write or route to
   both for a deprecation window, then remove.
4. **Keep nodes tolerant of unknown keys** (`TypedDict` ignores extras at runtime).
5. **Spot-check in staging** with `get_state` and time travel against real old checkpoints before
   rollout.

### Detecting in-flight threads before a risky change

- **On LangSmith Deployment**: thread search by `status` — `idle`, `busy`, `interrupted`, `error`.
  Bulk-query `interrupted` and `busy`, optionally narrowed by metadata.
- **Anywhere**: LangSmith tracing tells you which nodes are still being entered in production — the
  most reliable signal that a node or field is no longer reachable.
- **For a known thread**: inspect it directly with `graph.get_state(config)`.

### Business compatibility — pin a behavioural version in state

```python
class State(TypedDict):
    request: str
    flow_version: NotRequired[int]
    response: NotRequired[str]

def intake(state: State) -> dict:
    # stamp NEW threads; resuming threads keep whatever was saved
    return {"flow_version": state.get("flow_version", 2)}

def after_triage(state: State) -> str:
    return "policy_check" if state.get("flow_version", 1) >= 2 else "respond"

builder.add_conditional_edges("triage", after_triage, ["policy_check", "respond"])
```

Old threads resume past `triage`, read their saved (or defaulted) `flow_version=1`, and skip the new
step. New threads get `flow_version=2` and the full flow. Remove the flag once all v1 threads drain.

**This only works if the version is stamped at thread start**, before any branch that depends on it.

### Functional API non-determinism

An `@entrypoint` replays its body from the top on resume, matching cached `@task` results and
`interrupt` resume values **by position**. Two changes break it:

- Adding, removing or reordering `@task` / `interrupt` calls **before** the resume point.
- Introducing non-deterministic operations (`time.time()`, `random`, inline network calls) outside a
  `@task`.

Safe options for non-trivial changes to an entrypoint with in-flight runs:

1. Let in-flight runs drain before deploying.
2. Wrap new logic in a **new `@task`** so its result is checkpointed independently.
3. Register a **new entrypoint under a new graph name** in `langgraph.json` and route new threads
   to it.

### Assistant versioning (config, not code)

Prompt/model/tool-config changes should ship as **assistant versions**, not code deploys:

```python
await client.assistants.update(assistant_id, config={"configurable": {"prompt": NEW_PROMPT}})
await client.assistants.set_latest(assistant_id, version=7)   # promote or roll back instantly
```

Canary: create a second assistant with the new config, route a percentage of traffic, compare eval
scores and cost, then promote.

### Framework upgrades (LangChain / LangGraph v1)

- Read `/oss/python/migrate/langchain-v1` and `/oss/python/migrate/langgraph-v1`; `create_agent`
  replaces the older prebuilt agent, and `langgraph-supervisor` / `-swarm` are superseded by the
  multi-agent patterns in [12](12-multi-agent-architecture.md).
- Pin exact versions in the deployment image.
- Keep archived real checkpoints as test fixtures and assert they still deserialize after each bump
  ([21](21-testing-strategy.md)).
- `DeltaChannel` is a **one-way door**: `langgraph>=1.2` writes a checkpoint format earlier versions
  cannot read. Migrate or discard affected threads before any downgrade.

## 3. Scenarios

| Scenario | Playbook |
|---|---|
| Rename a node with paused approval threads | Add the new node, keep the old as an alias that forwards, drain, then remove |
| Add a mandatory compliance step | Business-compatibility flag; only new threads get it (or explicitly migrate old ones with `update_state`) |
| Change a state field type | Add a new field, dual-write, backfill via a migration job over threads, remove the old field |
| Upgrade LangGraph minor version | Staging soak with production-shaped traffic + archived-checkpoint deserialization tests + eval suite |
| New prompt | Assistant version + canary + eval gate; instant rollback via `set_latest` |
| Deprecating a tool | Keep it registered returning a deprecation message for one cycle; monitor call counts; remove |

## 4. Staff-level considerations

- **Your state schema is a published API with unknown clients** — every historical checkpoint. Apply
  the same discipline you'd apply to a wire protocol: additive changes, deprecation windows,
  tolerant readers.
- **Deploys are hot-swaps over in-flight state.** Combine graceful drain
  ([17](17-durability-fault-tolerance-idempotency.md)) with the add-then-remove pattern; drain alone
  doesn't help with threads paused for days on human approval.
- **Long-lived HITL threads are your hardest compatibility constraint.** A thread parked for two
  weeks will resume onto code that's a dozen deploys newer. Decide a maximum supported staleness
  (e.g. 30 days) and enforce it with TTLs so compatibility windows are bounded.
- **Separate the two release trains**: code (graph topology, nodes, tools) and config (prompts,
  models, per-tenant settings). Config should ship many times a day via assistants with instant
  rollback; code goes through normal review and canary.
- **Migration jobs are legitimate.** A cron/graph that walks `interrupted` threads and applies
  `update_state` is often cleaner than carrying a compatibility branch forever — but record an audit
  entry for each mutation.
- **Version everything that shapes behaviour**: prompts, tool schemas, skills, `AGENTS.md`, retrieval
  index version. Stamp them into trace metadata so a quality regression can be attributed.

## 5. Anti-patterns

- Renaming nodes or state keys "because it's cleaner" while threads are paused.
- Adding a required state field with no default.
- Assuming old threads will pick up new business logic correctly.
- Shipping prompt changes as code deploys (slow, no instant rollback, no A/B).
- No inventory of in-flight threads before a breaking change.
- Reordering `@task`/`interrupt` calls in an entrypoint with live runs.
- Adopting `DeltaChannel` with no downgrade plan.
- Upgrading the framework without running the eval suite.

## 6. Design-review questions

1. Is this change additive with respect to persisted state? If not, what's the deprecation plan?
2. How many threads are currently `interrupted` or `busy`, and can they tolerate this change?
3. Should existing threads get the new behaviour, or do we need a `flow_version` flag?
4. What is the maximum thread staleness we support, and is it enforced by TTL?
5. Is this a config change (assistant version) or a code change? Why?
6. What proves old checkpoints still deserialize after this dependency bump?

## References

- `/oss/python/langgraph/backward-compatibility`
- `/oss/python/langgraph/graph-api#graph-migrations`
- `/oss/python/migrate/langchain-v1`, `/migrate/langgraph-v1`, `/migrate/langgraph-supervisor`
- `/langsmith/assistants#versioning`, `/langsmith/use-threads`
- `/versioning`, `/release-policy`, `/releases/changelog`
