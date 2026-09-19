# 10 — Agents (`create_agent`) & the Middleware System

## 1. Concepts

`create_agent` builds the standard agent loop — model → tools → model → … until no tool calls — and
compiles it to a LangGraph graph. Middleware is the extension mechanism, and it is the reason you
almost never need to fork the loop.

```python
from langchain.agents import create_agent

agent = create_agent(
    model="claude-sonnet-4-6",
    tools=[search, lookup_order],
    system_prompt="You are a support agent...",
    middleware=[...],
    checkpointer=checkpointer,
    store=store,
    response_format=OrderSummary,     # optional structured output
)
```

**Middleware is not a separate runtime.** Hooks run inside the compiled graph, so an agent (with all
its middleware) drops into a larger `StateGraph` as a node and everything still works.

### Hook taxonomy

**Node-style hooks** run sequentially at fixed points and return a state update (or `None`):

| Hook | When |
|---|---|
| `before_agent` | Once, before the agent starts |
| `before_model` | Before each model call |
| `after_model` | After each model response |
| `after_agent` | Once, after the agent completes |

**Wrap-style hooks** wrap a call and decide whether/how often to invoke the handler — zero times
(short-circuit), once (normal), or many (retry):

| Hook | When |
|---|---|
| `wrap_model_call` | Around each model call |
| `wrap_tool_call` | Around each tool call |

### Execution order (memorise this)

With `middleware=[m1, m2, m3]`:

- `before_*`: **m1 → m2 → m3** (declaration order)
- `wrap_*`: **nested** — m1 wraps m2 wraps m3 wraps the model
- `after_*`: **m3 → m2 → m1** (reverse order)

So the *first* middleware in the list is the outermost: it sees the request first and the response
last. Put cross-cutting concerns that must never be bypassed (auth checks, budget caps, PII
redaction of inputs) **first**.

### Agent jumps

A node-style hook can short-circuit by returning `jump_to`:

```python
from langchain.agents.middleware import before_model, hook_config, AgentState

@before_model(can_jump_to=["end"])
def budget_guard(state: AgentState, runtime) -> dict | None:
    if state.get("cost_usd", 0) > 2.0:
        return {"messages": [AIMessage("Budget exceeded.")], "jump_to": "end"}
    return None
```

Targets: `"end"`, `"tools"`, `"model"`. `can_jump_to` must be declared (decorator arg or
`@hook_config`) so the graph knows the extra edges.

## 2. How to implement

### Built-in middleware catalogue (provider-agnostic)

| Middleware | Purpose | Typical production use |
|---|---|---|
| `SummarizationMiddleware` | Summarise history near token limits | Long chats |
| `HumanInTheLoopMiddleware` | Pause for approval on named tools | Any side-effecting tool |
| `ModelCallLimitMiddleware` | Cap model calls per run/thread | Cost containment |
| `ToolCallLimitMiddleware` | Cap tool calls globally or per tool | Runaway loops, per-thread subagent safety |
| `ModelFallbackMiddleware` | Fall back to another model on failure | Provider outage resilience |
| `ModelRetryMiddleware` | Retry model calls with backoff | Transient 5xx / rate limits |
| `ToolRetryMiddleware` | Retry failed tool calls with backoff | Flaky downstreams |
| `ToolErrorMiddleware` | Convert tool exceptions into model-visible messages | Let the agent self-correct |
| `PIIMiddleware` | Detect/redact/mask PII on input and output | Compliance |
| `TodoListMiddleware` | Planning / task tracking | Long-horizon tasks |
| `LLMToolSelectorMiddleware` | Pre-select relevant tools | Large tool catalogues |
| `ContextEditingMiddleware` | Trim/clear stale tool results | Long tool-heavy runs |
| `ProviderToolSearchMiddleware` | Defer tools behind provider-side tool search | 100+ tools |
| `ShellToolMiddleware` / `FilesystemMiddleware` / `FilesystemFileSearchMiddleware` | Execution environment | Coding/ops agents |
| `SubAgentMiddleware` | Spawn subagents | Delegation |
| `LLMToolEmulatorMiddleware` | Emulate tools with an LLM | Testing without real side effects |
| `RubricGradingMiddleware` (beta) | LLM-as-judge self-evaluation loop | Quality gates |

Provider-specific middleware (Anthropic prompt caching, OpenAI, AWS, …) lives under
`/oss/python/integrations/middleware/`.

### A realistic production stack

```python
from langchain.agents import create_agent
from langchain.agents.middleware import (
    PIIMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware,
    SummarizationMiddleware, HumanInTheLoopMiddleware,
    ModelFallbackMiddleware, ToolRetryMiddleware, ToolErrorMiddleware,
)

agent = create_agent(
    model="claude-sonnet-4-6",
    tools=[search, lookup_order, issue_refund],
    middleware=[
        # outermost: guards that must never be bypassed
        PIIMiddleware("email", strategy="redact", apply_to_input=True),
        PIIMiddleware("credit_card", strategy="mask", apply_to_input=True),
        ModelCallLimitMiddleware(thread_limit=40, run_limit=15),
        ToolCallLimitMiddleware(tool_name="issue_refund", run_limit=1, exit_behavior="error"),
        # context shaping
        SummarizationMiddleware(max_tokens_before_summary=60_000, messages_to_keep=20),
        # resilience (innermost, closest to the call)
        ModelFallbackMiddleware("gpt-5.5", "claude-haiku-4-5"),
        ToolRetryMiddleware(max_retries=3, on_failure="error"),
        ToolErrorMiddleware(),
        # steering
        HumanInTheLoopMiddleware(interrupt_on={"issue_refund": True}),
    ],
    checkpointer=checkpointer,
    store=store,
)
```

Order rationale: PII and budget caps must wrap everything; retries/fallbacks belong closest to the
call they protect; HITL must see the final proposed tool call.

### Custom middleware — decorator style

```python
from langchain.agents.middleware import wrap_model_call, ModelRequest, ModelResponse

@wrap_model_call
def route_by_complexity(request: ModelRequest, handler) -> ModelResponse:
    """Cheap model for short turns, frontier model for long ones."""
    n = len(request.messages)
    model = "claude-haiku-4-5" if n < 6 else "claude-sonnet-4-6"
    return handler(request.override(model=model))
```

### Custom middleware — class style with custom state

```python
from langchain.agents.middleware import AgentMiddleware, AgentState
from typing_extensions import NotRequired

class BudgetState(AgentState):
    cost_usd: NotRequired[float]

class BudgetMiddleware(AgentMiddleware):
    state_schema = BudgetState

    def __init__(self, limit: float): 
        super().__init__()
        self.limit = limit

    def wrap_model_call(self, request, handler):
        response = handler(request)
        usage = getattr(response, "usage_metadata", None) or {}
        # accumulate via state update returned from after_model, or emit a custom stream event
        return response
```

Middleware may extend the agent's state schema (`state_schema`) — that is how `TodoListMiddleware`
and `FilesystemMiddleware` add channels without you touching the agent.

### Best practices from the docs

1. One concern per middleware.
2. Handle errors inside middleware — a middleware exception crashes the agent.
3. Node-style for sequential logic; wrap-style for control flow.
4. Document custom state keys.
5. Unit-test middleware in isolation.
6. Order matters — critical middleware first.
7. Prefer built-ins.

## 3. Scenarios

| Scenario | Middleware design |
|---|---|
| Cost runaway from a looping agent | `ModelCallLimitMiddleware` + `ToolCallLimitMiddleware` + a `before_model` budget guard that `jump_to: end` |
| Provider outage | `ModelFallbackMiddleware` across two providers, plus `ModelRetryMiddleware` for transient errors |
| Regulated data | `PIIMiddleware` on input and output, plus LangSmith anonymizers so traces are clean |
| Tool catalogue of 120 tools | `ProviderToolSearchMiddleware` or `LLMToolSelectorMiddleware`, plus stage-scoped tool overrides |
| Agent must not act without approval | `HumanInTheLoopMiddleware(interrupt_on={...})` + a checkpointer + a resume API ([15](15-human-in-the-loop-and-interrupts.md)) |
| Platform team serving 8 product teams | A shared `middleware/` package: auth, budget, PII, tracing metadata, prompt caching — imported by every agent |

## 4. Staff-level considerations

- **Middleware is your platform's policy layer.** The right org design is: platform team owns a
  vetted middleware stack (security, cost, observability, resilience); product teams add tools and
  prompts. This turns cross-cutting requirements into a dependency bump instead of 8 PRs.
- **Order is a contract; test it.** Write a test that asserts the effective middleware order and
  fails if someone inserts a middleware before your PII redaction.
- **Wrap-style hooks can retry — which means side effects can repeat.** A `wrap_tool_call` retry
  re-executes the tool. Ensure tool idempotency, or restrict retries to read-only tools
  (`ToolRetryMiddleware(tools=[...])`).
- **Middleware that calls an LLM adds a model call.** `LLMToolSelectorMiddleware`,
  `RubricGradingMiddleware` and summarisation all cost tokens and latency. Measure the delta; use a
  cheap model for these.
- **Custom state from middleware is still checkpointed.** `TodoListMiddleware` and filesystem
  middleware add channels that grow — they count against checkpoint size ([03](03-state-channels-and-reducers.md)).
- **`create_agent` is not a black box.** When you need a topology it can't express, embed it in a
  `StateGraph` rather than reimplementing the loop.

## 5. Anti-patterns

- Reimplementing retries/summarisation/HITL by hand when a built-in exists.
- Middleware that swallows exceptions silently (turns failures into confusing model behaviour).
- Putting PII redaction after summarisation (the summary already contains the PII).
- Long-running blocking work inside `before_model` (blocks the event loop for the whole worker).
- Using `jump_to` without `can_jump_to` (edges not declared → routing errors).
- Stacking six LLM-calling middleware and then wondering why p95 latency is 40 s.

## 6. Design-review questions

1. What is the exact middleware order, and what breaks if it changes?
2. Which middleware make additional model calls? What do they add to p95 and cost?
3. What are the hard caps (model calls, tool calls, budget) and what happens when they trip?
4. Which tools are retried, and are they idempotent?
5. Is the middleware stack shared across teams, or copy-pasted per service?
6. Do we have a test asserting PII redaction happens before anything else touches the messages?

## References

- `/oss/python/langchain/agents`
- `/oss/python/langchain/middleware/overview`
- `/oss/python/langchain/middleware/built-in`
- `/oss/python/langchain/middleware/custom`
- `/oss/python/integrations/middleware/index`
