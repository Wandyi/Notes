# 24 — Deep Agents (the agent harness)

## 1. Concepts

Deep Agents (`deepagents`) is an **agent harness**: the same core tool-calling loop as
`create_agent`, plus the capabilities you would otherwise build yourself for long-horizon work. It
is built on LangChain and runs on the LangGraph runtime, so durable execution, streaming, HITL and
persistence all come along.

### Four capability groups

| Group | Contents |
|---|---|
| **Execution environment** | Tools + MCP, virtual filesystem, filesystem permissions, sandboxed shell & JS interpreter, typed event streaming |
| **Context management** | Skills, memory (`AGENTS.md`), summarisation + context offloading, automatic prompt caching |
| **Delegation** | Subagents (static, dynamic, async), task planning / todo list |
| **Steering** | Human-in-the-loop approval and interrupts |

### The pieces that matter architecturally

**Virtual filesystem + backends.** File tools backed by pluggable backends: in-memory state, local
disk, LangGraph **store**, composite routing, or a custom backend. This is the mechanism for
*context offloading* — the agent writes intermediates to files and keeps only paths in context.
Backend choice determines durability and scope:

| Backend | Scope | Use |
|---|---|---|
| `StateBackend` | Thread (checkpointed) | Working scratch space for a single task |
| `StoreBackend` | Cross-thread | Durable notes, long-term memory |
| `FilesystemBackend` | Host/sandbox disk | Coding agents, real repos |
| Composite | Route paths to different backends | `/memory/**` → store, `/tmp/**` → state |

**Filesystem permissions** are declarative: which paths may be read or written. Treat as an ACL.

**Skills** follow the Agent Skills standard: a directory with `SKILL.md` plus scripts, templates and
reference docs. Loading is **progressive** — frontmatter at startup, full content only when needed.
This keeps startup context small while making a large capability library available.

**Memory** uses `AGENTS.md` files passed via `memory=`. Unlike skills, memory is **always loaded**,
and content lives in the configured backend. The agent can update it, so preferences carry forward.

**Prompt caching** is automatic for Anthropic and Bedrock (Claude/Nova) models: static system
sections — base instructions, memory, skills — are made cache-eligible with no configuration.

**Subagents** quarantine heavy subtasks and return only the final result. Static, dynamic
(constructed at runtime) and async variants exist.

## 2. How to implement

### Minimal

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    tools=[search, fetch_page, run_query],
    system_prompt="You are a research analyst.",
)
agent.invoke({"messages": [{"role": "user", "content": "Research X and write a brief."}]})
```

### Production-shaped configuration

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    tools=[*mcp_tools, search, run_query],
    system_prompt=SYSTEM_PROMPT,
    memory=["./AGENTS.md"],                 # always loaded
    skills=["./skills"],                    # progressively disclosed
    subagents=[
        {"name": "researcher", "description": "Deep web research on one question",
         "prompt": RESEARCH_PROMPT, "tools": [search, fetch_page]},
        {"name": "critic", "description": "Review a draft against the rubric",
         "prompt": CRITIC_PROMPT},
    ],
    backend=composite_backend,              # /memory → store, /work → state
    checkpointer=checkpointer,
    store=store,
)
```

An existing compiled LangGraph graph can be registered as a subagent via `CompiledSubAgent`, which
is how you reuse team-owned workflows inside a deep agent.

### Going to production — the checklist from the docs

1. **Deploy on LangSmith Deployment / Agent Server** for durable execution, background runs,
   streaming and HITL; or self-host the same runtime.
2. **Fault tolerance**: configure retries/timeouts; deep agents inherit LangGraph's mechanisms
   ([17](17-durability-fault-tolerance-idempotency.md)).
3. **Memory**: choose the backend deliberately (state vs store vs filesystem) and set TTLs.
4. **Execution environment**: run shell/code in a **sandbox**, never on the app host; scope
   filesystem permissions.
5. **Guardrails**: PII middleware, HITL on destructive tools, permission rules.
6. **Observability**: LangSmith tracing with per-subagent metadata.
7. **Frontend**: typed event streaming — subagent streams, todo list, sandbox views.

### Steering

```python
from langchain.agents.middleware import HumanInTheLoopMiddleware

agent = create_deep_agent(..., middleware=[
    HumanInTheLoopMiddleware(interrupt_on={"run_shell": True, "write_file": {"allow_edit": True}}),
])
```

## 3. Scenarios

| Scenario | Fit |
|---|---|
| Multi-hour research → written report | Excellent. Planning + subagents + file offloading are exactly this workload |
| Coding / repo automation | Excellent, with `FilesystemBackend` + sandbox + permissions + HITL on commits |
| Data analysis over files | Good: interpreter + filesystem + subagents for per-dataset analysis |
| Content generation pipelines | Good: skills for house style, memory for brand rules |
| Fixed 5-step deterministic workflow | **Bad fit** — use a `StateGraph`; the harness adds tokens and nondeterminism |
| Sub-second latency chat | **Bad fit** — the harness's startup context and planning overhead cost latency |

## 4. Staff-level considerations

- **Deep Agents is an opinionated context-engineering strategy in a box.** Its value is that the
  offloading/isolation/compaction decisions are already made and tested. Adopt it when your problem
  is long-horizon; don't adopt it for short interactive tasks.
- **Backend choice is a durability and privacy decision.** `StateBackend` content is in checkpoints
  (encrypted at rest, TTL'd); `StoreBackend` crosses threads (namespace = tenant boundary);
  `FilesystemBackend` touches real disk (sandbox it). Map each path prefix to its backend and
  document why.
- **Skills are a governance surface.** A skills directory is executable instructions that shape
  agent behaviour. Version it, review changes like code, and be explicit about who may add skills —
  an unreviewed skill is an unreviewed prompt injection.
- **Memory is always loaded → it's always paid for.** `AGENTS.md` files consume tokens on every
  turn. Keep them tight; push anything conditional into skills.
- **Subagents multiply cost and latency**; they save context. Measure both. Give each subagent its
  own budget cap and trace metadata.
- **The virtual filesystem is state.** Files in `StateBackend` grow checkpoints; files in
  `StoreBackend` need TTLs and deletion paths for GDPR.
- **Compare against the alternatives explicitly.** The docs ship a comparison with the Claude Agent
  SDK; if your org already standardises on another harness, the question is which runtime and
  platform you deploy on, not just which SDK.

## 5. Anti-patterns

- Using Deep Agents for short, deterministic workflows (paying harness overhead for nothing).
- Running shell/code execution unsandboxed with production credentials.
- Unbounded `AGENTS.md` growth (a 5,000-token memory file on every turn).
- Skills added ad hoc by anyone, unreviewed.
- Filesystem with no permission rules — the agent can read anything the process can.
- Subagents with no cap on model calls.
- Treating the virtual filesystem as free storage — it lands in checkpoints or the store.

## 6. Design-review questions

1. Why the harness rather than `create_agent` + three middleware? What specifically do we need?
2. Which backend serves which path prefix, and what is the retention for each?
3. Where does code execution run, with what network and credential access?
4. Who can add or edit skills and `AGENTS.md`, and what's the review process?
5. What is the per-run budget, and how is it enforced across subagents?
6. What does a production trace look like — can we see per-subagent cost and failures?

## References

- `/oss/python/deepagents/overview`, `/quickstart`, `/customization`
- `/oss/python/deepagents/backends`, `/permissions`, `/sandboxes`, `/interpreters`
- `/oss/python/deepagents/skills`, `/memory`, `/context-engineering`
- `/oss/python/deepagents/subagents`, `/dynamic-subagents`, `/async-subagents`
- `/oss/python/deepagents/going-to-production`, `/fault-tolerance`, `/human-in-the-loop`
- `/oss/python/deepagents/comparison` (vs Claude Agent SDK)
