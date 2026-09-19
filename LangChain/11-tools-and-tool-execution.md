# 11 — Tools & Tool Execution

## 1. Concepts

A tool is a function plus a schema the model can call. In LangChain v1 the schema is derived from
the Python signature, type hints and docstring — which means **the docstring is prompt engineering,
not documentation**.

```python
from langchain.tools import tool

@tool
def lookup_order(order_id: str) -> str:
    """Look up the status of a customer order by its ID.

    Args:
        order_id: The order identifier, e.g. "ORD-10231".
    """
    ...
```

### Tool context: what a tool can see

| Injection | How | Gives access to |
|---|---|---|
| Runtime | `get_runtime()` or `runtime: Runtime[Ctx]` param | `context` (deps), `store`, `stream_writer` |
| Agent state | `Annotated[AgentState, InjectedState]` | Current messages and custom channels |
| Tool call id | `Annotated[str, InjectedToolCallId]` | Needed to construct `ToolMessage` in a `Command` |
| Config | `config: RunnableConfig` | `thread_id`, tags, metadata |

Injected parameters are **hidden from the model's schema** — the model never sees or supplies them.
This is the mechanism for passing tenant ids, user ids and DB handles into tools **without letting
the model choose them**. It is a security control ([22](22-security-guardrails-multitenancy.md)).

### Tool return values

- **A string / JSON-serializable value** → becomes a `ToolMessage` in context.
- **A `Command`** → updates state and/or routes; the model sees only the `ToolMessage` you include.
- **`ToolMessage` with artifacts** → separate the model-visible summary from the full payload.

The `Command` return is how you keep large results out of context while still persisting them.

### Errors

Tools fail. You have three levers, in order of preference:

1. **Return an error string the model can act on** ("Order not found; ask the user to confirm the
   ID"). The agent self-corrects. Best for user-fixable errors.
2. **`ToolRetryMiddleware`** for transient failures (network, 5xx) with exponential backoff.
3. **Raise** and let node-level `retry_policy` / `error_handler` handle it — for infrastructure
   failures that should not be exposed to the model.

`ToolErrorMiddleware` converts exceptions into model-visible messages; combine it with
`ToolRetryMiddleware` so retries happen before the model sees anything.

## 2. How to implement

### Tool with injected tenant context

```python
from typing import Annotated
from langchain.tools import tool
from langgraph.runtime import get_runtime

@tool
def search_tickets(query: str, limit: int = 10) -> str:
    """Search support tickets matching a natural-language query."""
    rt = get_runtime()
    return ticket_api.search(
        tenant_id=rt.context.tenant_id,      # from context, never from the model
        actor=rt.context.user_id,
        query=query,
        limit=min(limit, 50),                # clamp model-supplied values
    )
```

### Tool that writes to state instead of context

```python
from langchain_core.messages import ToolMessage
from langchain.tools import tool, InjectedToolCallId
from langgraph.types import Command

@tool
def fetch_report(report_id: str,
                 tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Fetch a report. The full text is stored; a summary is returned."""
    text = reports.get(report_id)             # could be 200 KB
    return Command(update={
        "documents": [{"id": report_id, "text": text}],
        "messages": [ToolMessage(
            f"Fetched report {report_id} ({len(text)} chars). "
            f"Use `query_report` to ask questions about it.",
            tool_call_id=tool_call_id,
        )],
    })
```

### Human approval on a specific tool

```python
from langgraph.types import interrupt

@tool
def issue_refund(order_id: str, amount_cents: int) -> str:
    """Issue a refund. Requires human approval."""
    decision = interrupt({
        "action": "issue_refund",
        "order_id": order_id,
        "amount_cents": amount_cents,
    })
    if decision.get("approved") is not True:
        return f"Refund rejected: {decision.get('reason', 'no reason given')}"
    return payments.refund(order_id, amount_cents,
                           idempotency_key=f"refund:{order_id}:{amount_cents}")
```

Note the idempotency key: this tool re-executes on every resume before the `interrupt` returns
([15](15-human-in-the-loop-and-interrupts.md)).

Prefer `HumanInTheLoopMiddleware(interrupt_on={"issue_refund": True})` when you want approval
declared centrally rather than embedded in the tool.

### Dynamic tool selection

```python
from langchain.agents.middleware import wrap_model_call

TOOLS_BY_STAGE = {
    "triage":  [search_tickets, lookup_order],
    "resolve": [search_tickets, lookup_order, issue_refund, escalate],
    "verify":  [lookup_order],
}

@wrap_model_call
def scope_tools(request, handler):
    return handler(request.override(tools=TOOLS_BY_STAGE[request.state["stage"]]))
```

### MCP tools

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
                   "transport": "stdio"},
    "internal": {"url": "https://mcp.internal/mcp", "transport": "streamable_http"},
})
tools = await client.get_tools()
agent = create_agent("claude-sonnet-4-6", tools=tools)
```

MCP is how you consume tools you don't own. Treat every MCP server as **untrusted input**: its tool
descriptions enter your prompt and can carry injection payloads. Pin versions, allowlist servers,
and review tool descriptions in code review.

### Parallel tool calls

Models may emit several tool calls in one turn; they execute concurrently. Consequences:

- Tools must be **safe to run concurrently** (no shared mutable state, no per-agent singletons).
- Two tools returning `Command(update=...)` on the same channel need a reducer.
- Per-thread subgraph tools **cannot** be called in parallel (checkpoint namespace conflict) — guard
  with `ToolCallLimitMiddleware` or disable parallel tool calling on the model.

## 3. Scenarios

| Scenario | Design |
|---|---|
| SQL agent | A `run_sql` tool with a read-only role, statement timeout, row cap, and an allowlist of schemas; return truncated results + a pointer |
| File-heavy analysis | Filesystem tools (read/write/glob/grep) rather than dumping content; agent greps instead of reading whole files |
| Third-party API with strict rate limits | Tool wraps a token-bucket limiter; `ToolRetryMiddleware` with backoff; `ToolCallLimitMiddleware` per run |
| Destructive operations (delete, deploy, refund) | HITL interrupt + idempotency key + audit log write in the same transaction |
| 120 internal tools | `ProviderToolSearchMiddleware` / selector middleware + stage scoping + subagents by domain |
| Tools that need per-user credentials | Credentials resolved from `runtime.context` (injected), never in the tool schema; short-lived tokens |

## 4. Staff-level considerations

- **Tool surface area is the agent's blast radius.** Every tool is a capability grant. Review tools
  the way you review IAM policies: who can call it, with what arguments, and what's the worst case?
- **Never let the model supply authorization-relevant arguments.** `tenant_id`, `user_id`,
  `account_id`, file paths outside a sandbox — inject them. A model that can pass `tenant_id` is a
  model that can be prompt-injected into cross-tenant access.
- **Clamp and validate everything the model does supply.** `limit`, date ranges, amounts, SQL. Use
  Pydantic argument schemas with constraints; the model will eventually send `limit=1000000`.
- **Tool descriptions are shared prompt real estate.** 120 tools × 80 tokens = 9,600 tokens on every
  call. Audit descriptions for length as you would audit a hot loop for allocations.
- **Tool latency is agent latency, multiplied.** An agent making 6 tool calls per run at 800 ms each
  is a 5 s floor. Cache, batch, and parallelise inside tools.
- **Design tool outputs for a model, not a human or a machine.** Truncate, summarise, and include
  next-step hints ("3 of 47 results shown; refine with `status=` filter"). This measurably reduces
  loop count.
- **Version tool contracts.** Changing a tool's argument names changes agent behaviour and
  invalidates your evals. Treat it as an API change with a deprecation path.

## 5. Anti-patterns

| Anti-pattern | Consequence |
|---|---|
| Tool returns raw 200 KB JSON | Context blow-up, cost, distraction |
| `tenant_id` as a model-supplied argument | Cross-tenant data access via prompt injection |
| Vague docstrings ("does stuff with orders") | Model picks the wrong tool; more loops |
| One `execute_query` tool that accepts arbitrary SQL against a write-capable role | Data loss, injection |
| Tool that mutates without an idempotency key | Duplicate side effects on retry/replay |
| 40 tools with overlapping purposes | Selection confusion, unpredictable trajectories |
| Silent tool failures returning `""` | Agent hallucinates around the gap |

## 6. Design-review questions

1. For each tool: what is the worst thing it can do, and who authorised that?
2. Which arguments are model-supplied? Are they validated and clamped?
3. What is the p95 size of each tool's return value?
4. Which tools are retried, and are they idempotent? What's the idempotency key?
5. How many tools are in the schema at p95, and what do they cost in tokens?
6. Are any MCP servers third-party? Who reviews their tool descriptions?

## References

- `/oss/python/langchain/tools`
- `/oss/python/langchain/mcp`
- `/oss/python/langchain/middleware/built-in` (tool error, tool retry, tool selector, tool call limit)
- `/oss/python/langchain/sql-agent`, `/oss/python/langgraph/sql-agent`
