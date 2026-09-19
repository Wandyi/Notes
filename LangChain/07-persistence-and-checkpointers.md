# 07 — Persistence & Checkpointers

## 1. Concepts

LangGraph has **two** persistence systems. Conflating them is the most common architectural error.

| | **Checkpointer** | **Store** |
|---|---|---|
| Persists | Graph state snapshots | Application-defined key/value items |
| Scope | One thread | Across threads |
| Memory type | Short-term, thread-scoped | Long-term, cross-thread |
| Used for | Conversation continuity, HITL, time travel, fault tolerance | Preferences, facts, shared knowledge |
| Access | `thread_id` in config | `store.get/put/search` from nodes/tools |

Most production systems use both. Stores are covered in [08](08-stores-and-long-term-memory.md).

### Threads

A `thread_id` is the primary key of the checkpointer. It accumulates state across runs.

```python
config = {"configurable": {"thread_id": "conv-123"}}
graph.invoke(inputs, config)
```

Without a `thread_id`, a checkpointer cannot save or resume. Keep ids **under 255 characters**
(`PostgresSaver` stores them in a bounded column) — use a UUID or a hash of your natural key.

### Checkpoints

A checkpoint is a `StateSnapshot` of the thread at a super-step boundary:

| Field | Meaning |
|---|---|
| `values` | Channel values at this checkpoint |
| `next` | Nodes to execute next; `()` means complete |
| `config` | `thread_id`, `checkpoint_ns`, `checkpoint_id` |
| `metadata` | `source` (`input`/`loop`/`update`), `writes` (node outputs), `step` |
| `created_at` | ISO-8601 timestamp |
| `parent_config` | Config of the previous checkpoint |
| `tasks` | `PregelTask`s with `id`, `name`, `error`, `interrupts`, optional subgraph `state` |

A simple `START → A → B → END` run produces **four** checkpoints (input, pre-A, pre-B, final).

### Durability modes

Set per execution call — this is one of the highest-leverage performance knobs in the stack.

| Mode | Behaviour | Trade-off |
|---|---|---|
| `"exit"` | Persist only when execution exits (success, error, or interrupt) | Fastest; **no mid-run crash recovery** |
| `"async"` (default) | Persist asynchronously while the next step runs | Good balance; small risk of losing a checkpoint on hard crash |
| `"sync"` | Persist synchronously before the next step | Highest durability; per-step latency cost |

```python
graph.stream(inputs, config, durability="sync")
```

### Serialization

Default is `JsonPlusSerializer` (ormsgpack + JSON): handles LangChain/LangGraph primitives,
datetimes, enums. For unsupported types (e.g. pandas DataFrames):

```python
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
graph.compile(checkpointer=InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=True)))
```

`pickle_fallback` couples checkpoints to your Python class definitions — a real upgrade hazard.

### Encryption at rest

```python
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.postgres import PostgresSaver

serde = EncryptedSerializer.from_pycryptodome_aes()   # reads LANGGRAPH_AES_KEY
checkpointer = PostgresSaver.from_conn_string("postgresql://...", serde=serde)
checkpointer.setup()
```

On LangSmith deployments, encryption turns on automatically when `LANGGRAPH_AES_KEY` is present.
Custom schemes: implement `CipherProtocol` and pass it to `EncryptedSerializer`.

### Checkpointer implementations

| Package | Class | Use |
|---|---|---|
| `langgraph-checkpoint` (bundled) | `InMemorySaver` | Tests, experiments. **Loses everything on restart** |
| `langgraph-checkpoint-sqlite` | `SqliteSaver` / `AsyncSqliteSaver` | Local dev, single-process tools |
| `langgraph-checkpoint-postgres` | `PostgresSaver` / `AsyncPostgresSaver` | Production; what LangSmith uses |
| `langchain-azure-cosmosdb` | `CosmosDBSaver(Sync)` | Azure production, Entra ID auth |

Async graph execution (`ainvoke`/`astream`) requires an async-capable checkpointer
(`InMemorySaver`, `AsyncSqliteSaver`, `AsyncPostgresSaver`).

## 2. How to implement

### Production wiring

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore

async with (
    AsyncPostgresSaver.from_conn_string(DSN) as checkpointer,
    AsyncPostgresStore.from_conn_string(DSN) as store,
):
    await checkpointer.setup()          # creates tables + indexes (run once, via migration)
    await store.setup()
    graph = builder.compile(checkpointer=checkpointer, store=store)
```

On the Agent Server you do **not** wire these — the platform injects them. Compiling with your own
checkpointer inside a deployed graph is a common bug: it shadows the managed one.

### Inspecting and manipulating state

```python
snap = graph.get_state(config)                       # latest
snap = graph.get_state({"configurable": {"thread_id": "1", "checkpoint_id": cid}})
history = list(graph.get_state_history(config))      # newest first

before_b = next(s for s in history if s.next == ("node_b",))
forks    = [s for s in history if s.metadata["source"] == "update"]
paused   = next(s for s in history if s.tasks and any(t.interrupts for t in s.tasks))

graph.update_state(config, {"answer": "corrected"}, as_node="answer")
```

`update_state` creates a **new** checkpoint; it never mutates history. Updates flow through
reducers, so a reduced channel accumulates rather than replaces (use `Overwrite` to replace).

### Custom checkpointer (when you must)

Implement `BaseCheckpointSaver`: `put`, `put_writes`, `get_tuple`, `list`, `delete_thread` (+ async
variants `aput`, `aput_writes`, `aget_tuple`, `alist`, `adelete_thread`).

Key design points from the docs:

- **Row key / index design**: primary key is `(thread_id, checkpoint_ns, checkpoint_id)`; you need
  an index supporting descending `checkpoint_id` scans per thread for `list`, and writes keyed by
  `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)`.
- **Delta channel support** requires exposing the per-step writes so the runtime can reconstruct
  values; the base class provides a default, and you can override it for performance.
- Validate against the **conformance test suite** shipped with `langgraph-checkpoint` before you
  trust it.

Only build one if you have a hard platform constraint (a mandated datastore, extreme scale, or data
residency). Otherwise use Postgres.

### TTL / retention

On LangSmith deployments, configure in `langgraph.json`:

```json
{
  "checkpointer": {
    "ttl": {
      "strategy": "delete",
      "sweep_interval_minutes": 60,
      "default_ttl": 43200
    }
  }
}
```

- `strategy`: `"delete"` (whole thread + runs + checkpoints) or `"keep_latest"` (keep the thread and
  its latest checkpoint, prune older ones).
- `default_ttl` is in **minutes** (43200 = 30 days).
- `delete` windows do **not** refresh with activity and do **not** apply retroactively to existing
  threads; `keep_latest` refreshes when a run finishes or state is updated.
- `sweep_limit` bounds threads processed per sweep (default 10000 on v0.12+).

Self-managed Postgres: write your own reaper job. Deleting a thread cascades to its runs and
checkpoints.

## 3. Scenarios

| Scenario | Configuration |
|---|---|
| Chat assistant, must survive pod restarts mid-turn | `AsyncPostgresSaver`, `durability="async"`, thread TTL `keep_latest` |
| Regulated workflow requiring a full audit trail | `durability="sync"`, TTL disabled or very long, `EncryptedSerializer`, state history exported to a warehouse |
| High-volume stateless classification (500 rps) | `durability="exit"`; consider stateless runs (no thread) entirely |
| Long research run (30+ min, expensive tools) | `durability="sync"` for the expensive segment; `DeltaChannel` on the transcript |
| Multi-tenant SaaS | `thread_id = hash(tenant_id, conversation_id)`; authorization filters on every thread read ([22](22-security-guardrails-multitenancy.md)) |
| GDPR delete request | `delete_thread(thread_id)` + store namespace purge; verify no PII leaked into traces |

## 4. Staff-level considerations

- **Checkpoint write volume is your dominant DB load.** Writes ≈ `runs/sec × super_steps_per_run`
  under `async`/`sync`. Reduce by merging nodes, choosing `exit` where safe, and shrinking channels.
- **`durability` is a per-run decision, not a global one.** Expose it in your run-creation API:
  interactive chat → `async`; batch enrichment → `exit`; money-moving workflows → `sync`.
- **Checkpoints contain everything in state — including PII.** Encryption at rest, TTLs, and the
  discipline of not putting raw documents in state are all compliance controls, not optimisations.
- **`InMemorySaver` in production is a recurring incident.** Ban it outside tests with a lint rule
  or a startup assertion on the environment.
- **Schema migrations for live threads are a real project.** Old checkpoints deserialize into new
  code. See [26](26-migration-and-versioning.md) — add fields with defaults, never rename or retype
  a channel in place, and gate reads with tolerant deserialization.
- **Backups and PITR matter more than usual**: the checkpoint DB *is* your users' conversation
  history and in-flight work. Losing it loses running jobs, not just history.
- **One checkpointer per deployment, injected, not embedded.** Multiple services sharing a
  checkpoint DB must share a version policy.

## 5. Anti-patterns

| Anti-pattern | Consequence |
|---|---|
| `InMemorySaver` in prod | Total state loss on deploy/restart |
| Unbounded thread growth, no TTL | Postgres bloat, slow `list`, rising cost |
| `pickle_fallback=True` as a default | Checkpoints become undeserializable after refactors |
| Compiling your own checkpointer inside a deployed graph | Shadows the platform's managed persistence |
| `durability="sync"` everywhere | 2–3× DB write latency added to every step |
| Reusing a `thread_id` across tenants/users | Cross-tenant data leak |
| Treating `update_state` as an edit-in-place | It appends a checkpoint; history still shows the original |

## 6. Design-review questions

1. What is the expected checkpoint write rate at target load, and has the DB been sized for it?
2. What `durability` does each run type use, and who decided?
3. What is the thread retention policy, and how is it enforced (TTL config vs. cron)?
4. Is anything in state that we would not want in a database dump?
5. What happens to in-flight threads when we deploy a state-schema change?
6. Who can read a thread? Is that enforced at the API layer, not just by obscure `thread_id`s?

## References

- `/oss/python/langgraph/persistence`
- `/oss/python/langgraph/checkpointers`
- `/langsmith/configure-checkpointer`, `/langsmith/configure-ttl`
- `/oss/python/integrations/checkpointers/index`
