# 06 — Functional API (`@entrypoint` / `@task`)

## 1. Concepts

The Functional API expresses a durable workflow as ordinary Python control flow rather than a graph.
Both APIs compile to the **same Pregel runtime** and can be mixed in one application.

| | Graph API | Functional API |
|---|---|---|
| Control flow | Nodes + edges, declared | Plain Python (`if`, `for`, `try`) |
| State | Explicit `State` schema + reducers | Function-scoped locals; `previous` for cross-run state |
| Checkpointing | New checkpoint **per super-step** | Task results saved into the **entrypoint's** checkpoint |
| Visualisation | `draw_mermaid()` works | Not supported (topology is dynamic) |
| Best for | Fixed topology, fan-out, HITL gates, auditability | Linear/branchy imperative pipelines, batch jobs, migration of existing scripts |

### `@entrypoint`

- Decorating a function produces a `Pregel` instance (so `invoke`/`stream`/`ainvoke` all work).
- Takes **exactly one positional argument** — use a dict for multiple inputs.
- Inputs and outputs must be **JSON-serializable** (checkpointing requirement).
- Injectable parameters: `previous` (state from the previous checkpoint on this thread), `store`
  (a `BaseStore`), `writer` (stream writer for async on Python < 3.11), `config`, `runtime`.

### `@task`

- A discrete, checkpointed unit of work. Returns a **future** immediately; `.result()` or `await`
  to get the value.
- Callable only from inside an entrypoint, another task, or a graph node — never from application code.
- **Task outputs must be JSON-serializable.**
- Results are restored from the checkpointer on resume instead of being recomputed.

### The determinism contract (this is the whole game)

On resume, execution does **not** continue from the line where it stopped. The entrypoint **replays
from the top**, and LangGraph restores completed task and subgraph results from the checkpointer
rather than recomputing them.

Therefore:

- Any **side effect or non-deterministic value** placed directly in the entrypoint body executes
  again on every resume.
- Any side effect wrapped in a `@task` executes once and is replayed from the checkpoint.
- A task that *started but did not finish* may run again on resume → **tasks must be idempotent**
  (idempotency keys, or check-before-write).

## 2. How to implement

### Basic workflow with HITL

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.func import entrypoint, task
from langgraph.types import interrupt

@task
def write_essay(topic: str) -> str:
    return llm.invoke(f"Write an essay about {topic}").content

@entrypoint(checkpointer=InMemorySaver())
def workflow(topic: str) -> dict:
    essay = write_essay(topic).result()
    approved = interrupt({"essay": essay, "action": "Approve or reject"})
    return {"essay": essay, "approved": approved}
```

### Parallel tasks

```python
@entrypoint(checkpointer=checkpointer)
async def enrich(record: dict) -> dict:
    crm, billing, usage = await asyncio.gather(
        fetch_crm(record["id"]),      # each is a @task
        fetch_billing(record["id"]),
        fetch_usage(record["id"]),
    )
    return merge(crm, billing, usage)
```

Each task is independently checkpointed, so a crash after two of three completes only re-runs the
third.

### Cross-run state with `previous`

```python
@entrypoint(checkpointer=checkpointer)
def chat(message: str, previous: list | None = None) -> entrypoint.final:
    history = (previous or []) + [message]
    reply = llm.invoke(history).content
    return entrypoint.final(value=reply, save=history + [reply])
```

`entrypoint.final(value=..., save=...)` separates what the caller receives from what is persisted for
the next run on the thread — the functional analogue of an output schema.

### Side effects: the canonical mistake

```python
# WRONG — re-executes on every resume
@entrypoint(checkpointer=checkpointer)
def wf(inputs: dict):
    send_email(inputs["to"])            # runs again after the interrupt resumes
    return interrupt("confirm?")

# RIGHT
@task
def send_email_once(to: str, idem_key: str) -> str:
    return mailer.send(to, idempotency_key=idem_key)

@entrypoint(checkpointer=checkpointer)
def wf(inputs: dict):
    send_email_once(inputs["to"], inputs["request_id"]).result()
    return interrupt("confirm?")
```

### Retries and error handling

Retry policies are configured on `@task` the same way they are on nodes:

```python
from langgraph.types import RetryPolicy

@task(retry_policy=RetryPolicy(max_attempts=3))
def call_flaky_api(payload: dict) -> dict:
    ...
```

### Mixing with the Graph API

A `@task` can be called from inside a `StateGraph` node, and an `@entrypoint` result is a `Pregel`
object that can be embedded as a node. Common pattern: a `StateGraph` for the auditable outer
workflow, `@task`s inside nodes for parallel, individually-checkpointed I/O.

## 3. Scenarios

| Scenario | Why functional API fits |
|---|---|
| Nightly batch enrichment of 200k records | No fixed topology to visualise; per-record durable tasks; `durability="exit"` |
| Migrating an existing Python ETL script to durable execution | Wrap I/O in `@task`, add a checkpointer, get resume for free |
| Document ingestion pipeline with branchy business rules | `if/elif` chains that would be 15 conditional edges in a graph |
| A workflow with a human approval in the middle | `interrupt()` works identically; determinism rules apply |
| Deeply nested logic where a graph would be 40 nodes | Readability |

**When *not* to use it**: anything you want to visualise, anything an auditor must read as a
diagram, anything with wide dynamic fan-in that needs reducers, anything where product/compliance
stakeholders review the flow.

## 4. Staff-level considerations

- **Checkpoint economics differ.** The graph API writes a checkpoint per super-step; the functional
  API writes task results into the entrypoint's checkpoint. For long linear pipelines this is
  cheaper, but a single entrypoint checkpoint can grow large — keep task outputs small (return URIs,
  not payloads).
- **Replay cost is proportional to entrypoint body cost.** Everything outside tasks re-executes on
  every resume. If your entrypoint does 200 ms of pure computation before an interrupt, you pay that
  every resume. Push work into tasks.
- **Loss of static topology is a real operational cost.** No Mermaid diagram, harder onboarding,
  harder to diff in review, harder to reason about blast radius. For platform-level code that other
  teams depend on, prefer the graph API.
- **Serialization is stricter in practice.** Everything crossing an entrypoint or task boundary must
  be JSON-serializable — this is a good forcing function, but it will reject your ORM objects.
- **Team convention beats per-engineer preference.** Pick one API as the default for a codebase and
  document the exceptions; mixing both styles arbitrarily makes review and debugging harder.

## 5. Anti-patterns

- Side effects (emails, payments, file writes, DB inserts) in the entrypoint body instead of tasks.
- Non-deterministic values (`uuid4()`, `datetime.now()`, `random`) computed in the entrypoint body
  and used for control flow.
- Tasks that are not idempotent — a task that started and died re-runs on resume.
- Returning large blobs from tasks (they land in the checkpoint).
- Calling a `@task` from application code (it only works inside an entrypoint/task/node).
- Using the functional API for a flow that compliance needs to see as a diagram.

## 6. Design-review questions

1. What executes twice if this workflow resumes? Have we enumerated it?
2. Is every side-effecting task idempotent, and what is the idempotency key?
3. How large is the entrypoint checkpoint at the end of a typical run?
4. How much pure computation lives outside tasks, and what does replaying it cost?
5. Do we lose anything (audit, onboarding, visualisation) by not having a graph here?

## References

- `/oss/python/langgraph/functional-api`
- `/oss/python/langgraph/use-functional-api`
- `/oss/python/langgraph/choosing-apis`
