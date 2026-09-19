# 22 — Security, Guardrails & Multi-Tenancy

## 1. Concepts

### The threat model that's actually different

Classic app security still applies, but agents add three novel properties:

1. **The model is an untrusted decision-maker with your credentials.** Anything in context —
   retrieved documents, tool outputs, MCP tool descriptions, web pages, user messages — can attempt
   to steer it. Treat every token that did not come from your code as **data, not instruction**.
2. **Tools are capability grants.** The agent's blast radius is the union of what its tools can do,
   under whatever identity they run as.
3. **State, checkpoints and traces are new PII stores.** Conversations persist in Postgres and in
   your observability backend by default.

### Defense in depth

```
Identity        →  @auth.authenticate, IdP-issued tokens
Authorization   →  @auth.on handlers, resource filters, store namespaces
Least privilege →  per-tool scopes, read-only DB roles, sandboxes
Input control   →  PII redaction, injection heuristics, content filters
Action control  →  HITL approval, tool call limits, allowlists, permissions
Egress control  →  network policy, domain allowlists for fetch/browse tools
Data control    →  encryption at rest, TTLs, trace anonymizers, retention
```

No single layer is sufficient; the model *will* eventually be talked into something.

### Authentication vs authorization on the Agent Server

- **AuthN** — `@auth.authenticate` runs as middleware on every request; validate the token, return a
  user object. Whatever you return is added to the run context, so agents can act with **user-scoped
  credentials** (delegated access).
- **AuthZ** — `@auth.on` handlers run per resource/action (threads, assistants, crons, store) and can
  return **filters** that are applied to *all* operations (create, read, update, search).

Defaults: LangSmith requires an API key (`x-api-key`); **self-hosted has no default authentication**
— you own it entirely.

## 2. How to implement

### Custom auth with owner scoping

```python
from langgraph_sdk import Auth

auth = Auth()

@auth.authenticate
async def authenticate(headers: dict) -> Auth.types.MinimalUserDict:
    token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ")
    claims = await verify_jwt(token)                     # your IdP
    return {
        "identity": claims["sub"],
        "is_authenticated": True,
        "permissions": claims.get("scopes", []),
        "tenant_id": claims["tenant_id"],                # custom fields land in the run context
    }

@auth.on
async def scope_to_owner(ctx: Auth.types.AuthContext, value: dict):
    filters = {"owner": ctx.user.identity, "tenant_id": ctx.user.tenant_id}
    metadata = value.setdefault("metadata", {})
    metadata.update(filters)
    return filters      # applied to create/read/update/search on every resource
```

Resource-specific handlers (`@auth.on.threads`, `@auth.on.threads.create`,
`@auth.on.threads.read`, `@auth.on.assistants`, …) override the generic one; **most specific wins**.

Register it in `langgraph.json`:

```json
{ "auth": { "path": "./src/auth.py:auth" } }
```

### Tenant isolation checklist

| Surface | Control |
|---|---|
| Threads / runs / crons / assistants | `@auth.on` filters on `owner`/`tenant_id` |
| Store | Tenant id as the **first namespace element**, derived from the auth context, never from model output |
| Checkpoints | Follow thread ownership; `thread_id` must not be guessable-and-readable |
| Tools | `tenant_id` **injected** from `runtime.context`, never a model-supplied argument |
| Retrieval | Filter at the vector-store query level with the tenant id; never post-filter |
| Traces | `metadata.tenant_id`; scope who can view which projects |
| Models | Per-tenant keys/quotas where required; avoid one shared key with no attribution |

### Guardrails (built-in)

```python
from langchain.agents.middleware import PIIMiddleware, HumanInTheLoopMiddleware

agent = create_agent(
    model=..., tools=[...],
    middleware=[
        PIIMiddleware("email", strategy="redact", apply_to_input=True),
        PIIMiddleware("credit_card", strategy="mask", apply_to_input=True),
        PIIMiddleware("employee_id", strategy="block",
                      detector=r"EMP-\d{6}", apply_to_input=True),
        HumanInTheLoopMiddleware(interrupt_on={"issue_refund": True, "delete_record": True}),
    ],
    checkpointer=checkpointer,
)
```

Strategies include `redact`, `mask`, `block` and `hash`; custom PII types can be a regex string, a
compiled pattern, or a detector function. Apply to input **and** output.

### Custom guardrail via middleware

```python
from langchain.agents.middleware import after_model, hook_config

@after_model
@hook_config(can_jump_to=["end"])
def block_unsafe(state, runtime):
    last = state["messages"][-1]
    if classifier.is_unsafe(last.content):
        return {"messages": [AIMessage("I can't help with that.")], "jump_to": "end"}
    return None
```

### Prompt-injection posture

You cannot fully prevent injection; you can bound its consequences:

1. **Structural separation** — put untrusted content in delimited blocks and state in the system
   prompt that content inside them is data, never instructions.
2. **No authorization decisions from context.** Permissions come from the auth context.
3. **Human approval for irreversible actions.**
4. **Egress allowlists** — a tool that fetches URLs is an exfiltration channel; restrict domains.
5. **Output validation** — structured output + schema validation before acting on it.
6. **Least-privilege identities** per tool (read-only DB role, scoped API tokens, short TTLs).
7. **Treat MCP servers as third-party code** — their tool descriptions enter your prompt.

### Sandboxing code execution

If the agent runs code or shell commands, use a real sandbox (the Deep Agents sandbox/interpreter
backends, or your own container/VM), with: no host filesystem access, no credentials in the
environment, network egress denied by default, CPU/memory/time limits, and ephemeral lifetime.
`ShellToolMiddleware` and filesystem tools must never run unsandboxed with production credentials.

### Data protection

- **Encryption at rest**: `EncryptedSerializer` + `LANGGRAPH_AES_KEY` ([07](07-persistence-and-checkpointers.md)).
- **TTLs** on threads and store items ([08](08-stores-and-long-term-memory.md)).
- **Trace anonymizers** before data leaves the process ([20](20-observability-and-evaluation.md)).
- **Deletion path**: thread delete + store namespace purge + trace deletion, exercised and timed.
- **Data residency**: self-hosted control/data plane if the region matters; check where model
  providers process data too.

## 3. Scenarios

| Scenario | Controls |
|---|---|
| B2B SaaS, strict tenant isolation | JWT → `@auth.on` filters; tenant-first store namespaces; per-tenant assistants; tenant id in traces; isolation tests in CI |
| Agent with production DB access | Read-only role + row-level security + statement timeout + result caps; writes only via approved, HITL-gated tools |
| Agent that browses the web | Egress allowlist; fetched content wrapped as untrusted data; no credentials in the browsing tool's identity |
| Coding agent | Sandboxed execution, no prod credentials, HITL on any push/deploy, permissions model |
| Healthcare/finance | Encryption at rest, PII middleware, trace anonymizers, short TTLs, audit log of every tool invocation and approval |
| Internal agent with employee data | Delegated access: the agent uses the *caller's* token so it can only see what the caller can |

## 4. Staff-level considerations

- **Delegated access beats service accounts.** If the agent calls downstream APIs with the user's
  token (via the auth context), authorization is enforced by systems that already do it correctly. A
  god-mode service account makes every prompt injection a full compromise.
- **Write the capability matrix.** Tool × identity × worst-case effect × approval requirement.
  Review it like an IAM policy. This one artefact prevents most agent security incidents.
- **Assume the model will be manipulated once per quarter.** Design so that a fully-compromised
  model still cannot exfiltrate another tenant's data or perform an unapproved destructive action.
- **Traces and checkpoints are in scope for compliance.** They frequently contain more PII than the
  production database. Include them in the data map, DPIA and retention schedule from day one.
- **Rate limit per tenant, not just globally.** One tenant's runaway agent should not consume the
  shared model quota. Enforce with per-tenant budgets and `ModelCallLimitMiddleware`.
- **Audit the approval path.** Who approved what, when, and on what evidence — stored outside the
  checkpoint, immutable.
- **Self-hosted means you own AuthN entirely.** There is no default. Verify this explicitly in any
  self-hosted design review; "we'll add auth later" has shipped more than once.

## 5. Anti-patterns

| Anti-pattern | Consequence |
|---|---|
| `tenant_id` as a model-supplied tool argument | Cross-tenant access via injection |
| One service account with broad scopes for all tools | Injection = full compromise |
| Relying on prompt instructions alone to prevent bad actions | Bypassed reliably |
| Unsandboxed code/shell execution | RCE with your credentials |
| No default auth on a self-hosted deployment | Open agent API on the network |
| PII in interrupt payloads, trace metadata, or error messages | Leakage into logs and third-party tools |
| Unrestricted URL-fetch tool | Data exfiltration channel |
| Post-filtering retrieval results by tenant | Leaks via ranking, counts and errors |

## 6. Design-review questions

1. What is the worst thing this agent can do if the model is fully adversarial?
2. Where does `tenant_id` come from at every layer? Prove it never originates from model output.
3. Do tools run as the user (delegated) or as a service account? Why?
4. Which actions require human approval, and where is that enforced?
5. What PII lands in checkpoints, interrupt payloads and traces, and what's the retention?
6. If a customer requests deletion, what is the exact runbook and how long does it take?
7. For self-hosted: what authenticates a request today?
8. Is there an automated test that a tenant cannot read another tenant's thread or memory?

## References

- `/langsmith/auth`, `/langsmith/add-auth-server`, `/langsmith/agent-auth`
- `/oss/python/langchain/guardrails`
- `/oss/python/langchain/middleware/built-in#pii-detection`
- `/oss/python/deepagents/permissions`, `/oss/python/deepagents/sandboxes`
- `/oss/python/langgraph/checkpointers#encryption`
- `/langsmith/mask-inputs-outputs`, `/security-policy`
