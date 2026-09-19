# 12 — Multi-Agent Architecture

> Start from the docs' own warning: **not every complex task needs multi-agent.** A single agent
> with dynamic tools and a good prompt often matches a four-agent system at a fraction of the cost.

## 1. Concepts

### What people actually mean by "we need multi-agent"

Three separable needs. Name yours before choosing a pattern:

1. **Context management** — specialised knowledge without blowing the window.
2. **Distributed development** — different teams own different capabilities independently.
3. **Parallelisation** — run subtasks concurrently for latency.

If it's only #1, skills or dynamic tool selection may be enough. If it's only #3, `Send` fan-out in
one graph may be enough. #2 is the one that genuinely forces a multi-agent boundary — and it is an
*org* reason, not a model reason.

### The four patterns

| Pattern | How it works | Distributed dev | Parallel | Multi-hop | Direct user interaction |
|---|---|:--:|:--:|:--:|:--:|
| **Subagents** | Main agent calls subagents **as tools**; all routing goes through the main agent | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐ |
| **Handoffs** | Tool calls flip a state variable; control transfers between agents, each can talk to the user | – | – | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **Skills** | One agent loads specialised prompts/knowledge on demand | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **Router** | A classification step directs input to one or more specialists; results synthesised | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | – | ⭐⭐⭐ |
| **Custom workflow** | Bespoke `StateGraph`, other patterns embedded as nodes | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ |

### Cost model (from the docs' benchmark)

| Pattern | One-shot | Repeat request (2 turns) | Multi-domain (3 domains, ~2k tokens of docs each) |
|---|:--:|:--:|:--:|
| Subagents | 4 calls | 8 calls (4+4) | **5 calls, ~9K tokens** |
| Handoffs | **3 calls** | **5 calls (3+2)** | 7+ calls, ~14K+ tokens |
| Skills | **3 calls** | **5 calls (3+2)** | 3 calls, ~15K tokens |
| Router | **3 calls** | 6 calls (3+3) | **5 calls, ~9K tokens** |

Read the three insights carefully:

- **Single tasks**: handoffs / skills / router win (3 calls); subagents pay +1 call because results
  route back through the main agent — that overhead *buys* centralised control.
- **Repeat requests**: stateful patterns (handoffs, skills) save 40–50% because the specialist is
  already active / the skill is already loaded. Subagents are stateless by design, so cost is
  constant per request — predictable, isolated, but repeated.
- **Multi-domain**: parallel patterns (subagents, router) win on **tokens** (~9K vs ~15K) because of
  context isolation; skills look cheap on call count but every subsequent call reprocesses all the
  loaded documentation. Handoffs are worst here — inherently sequential.

The general law: **stateful patterns optimise repeated single-domain work; isolated patterns
optimise multi-domain and long-horizon work.**

## 2. How to implement

### Subagents (agent-as-tool)

```python
from langchain.agents import create_agent
from langchain.tools import tool

billing_agent = create_agent("claude-sonnet-4-6", tools=[lookup_invoice, issue_refund],
                             system_prompt="You are a billing specialist...")

@tool
def ask_billing(question: str) -> str:
    """Delegate a billing question. Provide full context; the specialist sees nothing else."""
    result = billing_agent.invoke({"messages": [{"role": "user", "content": question}]})
    return result["messages"][-1].content

supervisor = create_agent("claude-sonnet-4-6", tools=[ask_billing, ask_technical, ask_shipping])
```

Or declaratively with `SubAgentMiddleware` / Deep Agents ([24](24-deep-agents.md)). A
`CompiledSubAgent` wrapper lets you register an arbitrary compiled LangGraph graph as a subagent.

**Key property**: the subagent's internal churn never enters the parent's window. That is the
context-isolation win.

### Handoffs

```python
from langchain.tools import tool, InjectedToolCallId
from langgraph.types import Command

@tool
def transfer_to_billing(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Transfer this conversation to the billing specialist."""
    return Command(
        goto="billing_agent",
        graph=Command.PARENT,
        update={"active_agent": "billing",
                "messages": [ToolMessage("Transferred to billing", tool_call_id=tool_call_id)]},
    )
```

The specialist then talks to the user directly and stays active across turns — which is why turn 2
is cheap.

### Router

```python
class Route(BaseModel):
    destinations: list[Literal["billing", "technical", "shipping"]]

def classify(state) -> Command:
    route = router_model.with_structured_output(Route).invoke(state["messages"])
    return Command(update={"routes": route.destinations},
                   goto=[Send(d, state) for d in route.destinations])   # parallel fan-out
```

Add a `synthesize` node with `defer=True` to combine results.

### Skills

A skill is a prompt + knowledge bundle loaded on demand into a single agent's context. Cheapest for
repeated single-domain work; watch the accumulation cost across turns. Deep Agents formalises this
with skill files ([24](24-deep-agents.md)).

### Mixing

Patterns compose: a subagent architecture whose subagents are routers; a custom workflow whose nodes
are agents; skills used inside a subagent. Composition is normal, not exotic.

## 3. Scenarios

| Scenario | Pattern | Why |
|---|---|---|
| Customer support with specialist teams who own their own tools/prompts | **Subagents** | Distributed development + isolation; supervisor keeps the audit trail |
| Live chat where a specialist should keep talking to the user | **Handoffs** | Direct user interaction, cheap repeats |
| One assistant covering 12 knowledge domains, one at a time | **Skills** | No extra model calls, simple ops |
| "Compare X, Y and Z" style fan-out queries | **Router** or **Subagents** | Parallel + context isolation → fewer tokens |
| Regulated pipeline with mandatory stages and approvals | **Custom workflow** with agents as nodes | Determinism + auditability where required |
| Deep research over hours | **Subagents** (Deep Agents) | Isolation is the only way to survive the context budget |

## 4. Staff-level considerations

- **Every agent boundary is an interface you now have to version, test and observe.** Budget for it:
  each subagent needs its own eval set, its own prompt ownership, and a contract for what it
  receives and returns.
- **Delegation quality is the failure mode.** Subagents see only what you pass them. Vague task
  descriptions produce confidently wrong results with no visible error. Enforce a structured task
  contract (goal, constraints, inputs, expected output shape) at the delegation boundary.
- **Latency compounds multiplicatively.** Supervisor call → subagent loop (3–8 calls) → supervisor
  synthesis. A 4-agent chain with 6-call subagents is 25+ sequential model calls. Parallelise or
  flatten.
- **Cost attribution needs design.** Tag every model call with `agent_name` and `run_id` metadata so
  LangSmith can tell you which subagent is burning the budget ([20](20-observability-and-evaluation.md)).
- **Statelessness is a feature.** Subagents' constant per-request cost makes capacity planning
  possible. Stateful handoffs are cheaper on average but have a long tail you cannot predict.
- **Per-thread subagents don't parallelise.** Compiling a subagent with `checkpointer=True` means
  parallel tool calls to it conflict on the checkpoint namespace — guard with
  `ToolCallLimitMiddleware` or disable parallel tool calls ([13](13-subgraphs-and-composition.md)).
- **The migration path matters**: `langgraph-supervisor` / `langgraph-swarm` users should move to
  these patterns (see `/oss/python/migrate/langgraph-supervisor`).

## 5. Anti-patterns

- Adopting multi-agent to solve a prompt problem. Try one agent with better context first.
- A supervisor that just forwards the user's message verbatim — you added a model call for nothing.
- Agents chatting to each other in a loop with no termination proof.
- Passing the entire parent conversation into every subagent (destroys the isolation benefit; you
  now pay 2× for the same context).
- Shared mutable state between agents without reducers → parallel-write errors.
- No per-agent observability: one flat trace where you cannot tell which agent failed.
- Handoffs for multi-domain fan-out (sequential by construction — the docs measure ~14K+ tokens vs
  ~9K).

## 6. Design-review questions

1. Which of the three needs (context, org, parallelism) is driving this? Can we solve it with one agent?
2. What exactly does each subagent receive, and is that contract written down and tested?
3. What is the worst-case model-call count for a single user request?
4. How do we attribute cost and failures per agent in traces?
5. What terminates an agent-to-agent loop?
6. Which agents are stateful, and what does that do to our capacity model?

## References

- `/oss/python/langchain/multi-agent/index` (patterns, performance comparison)
- `/oss/python/langchain/multi-agent/subagents`, `/handoffs`, `/router`, `/skills`, `/custom-workflow`
- `/oss/python/migrate/langgraph-supervisor`
- `/oss/python/deepagents/subagents`
