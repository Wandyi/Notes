# 15 — Human-in-the-Loop & Interrupts

## 1. Concepts

`interrupt(payload)` pauses a run and surfaces `payload` to the caller. The run stays paused —
durably, in the checkpoint — until you resume with `Command(resume=value)`, at which point
`interrupt()` **returns** that value inside the node.

Requirements: a **checkpointer** and a **`thread_id`**. Without persistence there is nothing to
resume.

### The mechanism (and why every rule follows from it)

`interrupt()` pauses by **raising a special exception**. The runtime catches it, checkpoints, and
returns control. On resume, the runtime **re-executes the node from the beginning** — it does not
continue from the line after `interrupt()`. LangGraph keeps a per-task list of resume values and
matches them to `interrupt()` calls **strictly by index**.

Everything below is a consequence of those two facts.

### The five rules

| Rule | Why |
|---|---|
| **1. Never wrap `interrupt()` in a bare `try/except`** | You'd swallow the pause exception. Catch specific exception types, or put `interrupt()` outside the try block |
| **2. Never reorder, conditionally skip, or dynamically loop `interrupt()` calls in a node** | Resume matching is index-based; a changed order or count returns the wrong answer to the wrong question |
| **3. Only pass JSON-serializable payloads** | The payload is checkpointed; functions, class instances and closures will not serialize |
| **4. Side effects before an `interrupt()` must be idempotent** | They re-run on every resume. Use upserts and idempotency keys, or move the side effect *after* the interrupt, or into its own node |
| **5. Interrupts are always re-triggered on replay** | Time travel and replay re-enter the node and pause again ([16](16-time-travel-and-debugging.md)) |

For input validation, do **not** use a `while True` loop around `interrupt()` — the number of
interrupts becomes non-deterministic. Use a validation node plus a conditional edge that routes back
to the asking node.

### Multiple parallel interrupts

If two parallel nodes both interrupt, the run surfaces **both** payloads and you resume **all of
them at once**:

```python
# stream.interrupts == (Interrupt(value='question_a', id='...'),
#                       Interrupt(value='question_b', id='...'))
graph.invoke(Command(resume={i.id: answer_for(i) for i in interrupts}), config)
```

## 2. How to implement

### Approve / reject

```python
from langgraph.types import interrupt, Command
from typing import Literal

def approval(state: State) -> Command[Literal["execute", "cancelled"]]:
    decision = interrupt({
        "action": "deploy",
        "service": state["service"],
        "version": state["version"],
        "question": "Approve this deployment?",
    })
    if decision["approved"]:
        return Command(goto="execute", update={"approver": decision["user"]})
    return Command(goto="cancelled", update={"reason": decision.get("reason", "")})
```

### Review and edit state

```python
def review_draft(state: State) -> dict:
    edited = interrupt({"kind": "edit", "draft": state["draft"]})
    return {"draft": edited["draft"], "edited_by": edited["user"]}
```

### Interrupt inside a tool (agent flows)

```python
@tool
def issue_refund(order_id: str, amount_cents: int) -> str:
    """Issue a refund. Requires approval."""
    decision = interrupt({"action": "issue_refund",
                          "order_id": order_id, "amount_cents": amount_cents})
    if not decision.get("approved"):
        return f"Refund rejected: {decision.get('reason', '')}"
    return payments.refund(order_id, amount_cents,
                           idempotency_key=f"refund:{order_id}:{amount_cents}")
```

Or centrally, without touching the tool:

```python
from langchain.agents.middleware import HumanInTheLoopMiddleware

agent = create_agent(
    model=..., tools=[issue_refund, send_email],
    middleware=[HumanInTheLoopMiddleware(interrupt_on={
        "issue_refund": True,                       # always ask
        "send_email": {"allow_edit": True},         # ask, allow editing args
    })],
    checkpointer=checkpointer,
)
```

`HumanInTheLoopMiddleware` matches on the tool's `.name` (for `@tool` functions, the function name).

### Driving it from a client (v2)

```python
config = {"configurable": {"thread_id": thread_id}}

result = graph.invoke(inputs, config, version="v2")
if result.interrupts:
    payload = result.interrupts[0].value        # render this as a form
    ...
    graph.invoke(Command(resume={"approved": True, "user": "vaibhav"}), config, version="v2")
```

Over the Agent Server, the paused run is durable: any process can resume it later by `thread_id`.
That is what makes HITL work across days, devices and restarts.

### Validating human input correctly

```python
def ask(state: State) -> dict:
    return {"candidate": interrupt({"question": "Enter an amount in cents"})}

def validate(state: State) -> Command[Literal["ask", "proceed"]]:
    if not isinstance(state["candidate"], int) or state["candidate"] <= 0:
        return Command(goto="ask", update={"error": "must be a positive integer"})
    return Command(goto="proceed", update={"amount": state["candidate"]})
```

One `interrupt()` per node execution; the *edge* creates the loop. Deterministic and replay-safe.

## 3. Scenarios

| Scenario | Design |
|---|---|
| Refunds above a threshold need a supervisor | `HumanInTheLoopMiddleware` + role check on the resume API + audit record with approver identity |
| Draft email/report review | Interrupt returns the edited draft; store both original and edited for eval data |
| Infrastructure change approval | Interrupt → approval → execute with idempotency key; a separate `cancelled` path with compensation |
| Approvals that may take days | Background run, durable interrupt, notification (Slack/email) carrying `thread_id` + `interrupt_id`; TTL long enough to cover SLA |
| Ambiguous user request | Interrupt to ask a clarifying question rather than guessing |
| Compliance "four-eyes" | Two sequential interrupt nodes with different required roles; enforce roles server-side, not in the graph |

## 4. Staff-level considerations

- **HITL turns your agent into a workflow engine.** You now need: an inbox/queue of pending
  approvals, notification delivery, SLA timers, escalation, and an audit trail. That is a product,
  not a code path. Plan it.
- **Authorisation lives outside the graph.** The graph knows *that* approval is needed; your API
  layer must verify *who* is allowed to resume this thread with this decision. Never trust the
  resume payload's claimed identity ([22](22-security-guardrails-multitenancy.md)).
- **Idempotency is the number-one bug source.** Node re-execution on resume means any pre-interrupt
  API call, DB insert, email or payment fires again. Audit every node containing an `interrupt()`
  for pre-interrupt side effects.
- **Paused runs consume storage, not compute.** Good: they don't hold a worker slot. But they do
  hold thread rows and checkpoints — set TTLs that exceed your approval SLA, and reap abandoned
  approvals explicitly.
- **Interrupt payloads are checkpointed and often logged.** Don't put secrets or full PII in them;
  put a reference plus the minimum the approver needs to decide.
- **Design the payload as a stable schema.** `{"action", "resource", "diff", "risk", "options"}` —
  your approval UI, notifications and audit log all key off it. Version it.
- **Test HITL inside subgraphs.** Interrupts propagate up through nesting, but resume routing is a
  common gap; write an explicit integration test.

## 5. Anti-patterns

| Anti-pattern | Consequence |
|---|---|
| `try: interrupt(...) except Exception:` | Pause swallowed; graph runs on unapproved |
| Conditional or looping `interrupt()` calls in one node | Index-mismatched resume values — silently wrong answers |
| Non-idempotent side effect before `interrupt()` | Duplicate refunds/emails on every resume |
| Complex objects in the interrupt payload | Serialization failure at pause time |
| `while True` validation loop around `interrupt()` | Non-deterministic interrupt count; broken resume |
| Trusting the resume payload for identity | Privilege escalation |
| No TTL / no escalation on pending approvals | Zombie threads and silent SLA breaches |

## 6. Design-review questions

1. List every side effect that executes before an `interrupt()`. Is each idempotent, and what is its key?
2. Who is authorised to resume this thread, and where is that enforced?
3. What is the approval SLA, and what happens on timeout — escalate, auto-reject, or hang?
4. Is the interrupt payload schema versioned, and is it free of secrets and unnecessary PII?
5. How does a user find their pending approvals? Is there an index of paused threads?
6. Do interrupts inside subgraphs resume correctly? Where is that tested?

## References

- `/oss/python/langgraph/interrupts` (rules of interrupts)
- `/oss/python/langchain/human-in-the-loop`
- `/oss/python/langchain/middleware/built-in#human-in-the-loop`
- `/langsmith/add-human-in-the-loop`
