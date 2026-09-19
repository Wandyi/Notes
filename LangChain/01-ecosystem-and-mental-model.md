# 01 — Ecosystem & Mental Model

## 1. Concepts

### The four products and what each one owns

| Layer | Package / product | Owns | Does **not** own |
|---|---|---|---|
| Orchestration runtime | **LangGraph** (`langgraph`) | Execution model (Pregel/BSP), state channels, checkpoints, interrupts, streaming, retries/timeouts | Prompting, model abstractions, tool schemas |
| Agent framework | **LangChain** (`langchain`, `langchain-core`) | `create_agent`, middleware, model + tool + message abstractions, structured output, retrieval primitives | Durable execution, thread state, deployment |
| Agent harness | **Deep Agents** (`deepagents`) | An opinionated agent: planning/todos, virtual filesystem, subagents, skills, permissions, sandboxes, context compaction | The runtime (delegates to LangGraph) |
| Platform | **LangSmith** (observability + **Agent Server** / Deployment) | Tracing, evals, datasets, prompts; API servers, queue workers, Postgres/Redis, assistants, crons, TTLs, auth | Your business logic |

They compose downward: Deep Agents is built on LangChain's `create_agent`, which compiles to a
LangGraph `Pregel` graph, which the Agent Server executes and persists.

### The core abstraction: a graph is actors + channels

LangGraph is an implementation of **Pregel / Bulk Synchronous Parallel**:

- **Actors** (`PregelNode`, i.e. your nodes) read from channels and write to channels.
- **Channels** hold state; each has a value type, an update type and an update function (a reducer).
- Execution proceeds in **super-steps**: *plan* which actors run → *execute* them in parallel →
  *update* channels. Writes are invisible to peers until the next step.

Everything else — `StateGraph`, `create_agent`, Deep Agents — is sugar over this. Internalising it
explains parallel-write conflicts, checkpoint boundaries, replay semantics and why interrupts must
be idempotent.

### Workflows vs agents

- **Workflow**: control flow is written by you (edges are known ahead of time). Predictable, cheap,
  auditable, easy to evaluate.
- **Agent**: control flow is decided by the model (loop of model → tools → model). Flexible, more
  expensive, harder to bound.

LangGraph's actual selling point is that you can **mix them in one graph**: deterministic,
hand-coded steps where the domain demands correctness, agentic steps where it demands flexibility.
That is the argument you make in a design review when someone says "just use an agent framework".

## 2. How to implement — the decision procedure

```
Is the task a fixed sequence with occasional LLM calls?
  → Plain workflow: StateGraph with static edges (02) or @entrypoint (06).

Is it "loop until the model stops calling tools", with <~15 tools and one domain?
  → create_agent + middleware (10).

Does it need files, planning, subagents, skills, sandboxed code execution?
  → Deep Agents (24).

Does it need custom topology — fan-out, deterministic gates, multi-stage pipelines,
   compensating transactions, human approval between stages?
  → StateGraph, optionally embedding create_agent instances as nodes (13).

Does it need durable multi-turn state, background runs, crons, HITL queues, multi-tenancy?
  → Any of the above + Agent Server / LangSmith Deployment (18).
```

### The composition trick that matters most

A `create_agent` result **is** a compiled LangGraph graph, so it drops into a `StateGraph` as a node,
and every middleware hook still runs:

```python
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langgraph.graph import START, StateGraph

email_agent = create_agent(
    model="claude-sonnet-4-6",
    tools=[read_email, send_email],
    middleware=[HumanInTheLoopMiddleware(interrupt_on={"send_email": True})],
)

graph = (
    StateGraph(AgentState)
    .add_node("classify", classify_node)      # deterministic
    .add_node("email_agent", email_agent)     # agentic
    .add_edge(START, "classify")
    .add_conditional_edges("classify", route)
    .compile()
)
```

This is the single most important architectural fact in the stack: **you never have to choose
between "framework agent" and "custom orchestration"**. Reach for it whenever the surrounding
topology is more than "loop until done".

## 3. Scenarios

| Scenario | Right layer | Why |
|---|---|---|
| Customer-support triage with SLA and audit trail | `StateGraph` + embedded `create_agent` | Deterministic routing and gates must be provable; only the reply drafting is agentic |
| Internal "ask the docs" assistant | `create_agent` + retrieval tool | Single domain, low blast radius |
| Multi-hour research report generation | Deep Agents | Needs planning, file offloading, subagent isolation |
| Nightly batch enrichment of 200k records | `@entrypoint` functional API + queue workers, `durability="exit"` | No conversation, no HITL; minimise checkpoint writes |
| Incident-response copilot that executes remediation | `StateGraph` + HITL interrupts + permissions | Side effects need approval and compensation |
| Agent platform used by 8 product teams | Agent Server + assistants per team + shared subgraph library | Independent deploy cadence, versioned config |

## 4. Staff-level considerations

- **Layer choice is an org decision, not just a technical one.** Deep Agents gives you speed and a
  shared harness across teams; raw `StateGraph` gives control but every team reinvents retries,
  summarisation and HITL. Prefer: one platform team owns the harness (middleware library, base
  graph, deployment), product teams own tools and prompts.
- **Beware "multi-agent" as a first instinct.** The docs are blunt about it: most "we need
  multi-agent" requests are really *context management*, *distributed development* or
  *parallelisation*. Solve the actual one. A single agent with dynamic tool selection is often
  cheaper and better than four agents passing messages ([12](12-multi-agent-architecture.md)).
- **The runtime is the product boundary.** Anything that must survive a restart lives in
  checkpoints/stores; anything ephemeral lives in memory. Decide this per field, in writing,
  before you code ([03](03-state-channels-and-reducers.md)).
- **Lock the versions.** LangChain/LangGraph v1 moves fast; pin exact versions in the deployment
  image and gate upgrades on your eval suite ([21](21-testing-strategy.md), [26](26-migration-and-versioning.md)).

## 5. Anti-patterns

- Building a bespoke orchestration engine on top of LangGraph because "we need control" — you
  usually needed `Command`, `Send` and middleware.
- Using `create_agent` for a workflow that has exactly one valid execution order.
- Treating LangSmith as optional. Without traces you cannot debug a non-deterministic system; the
  first serious production incident will cost more than the instrumentation would have.
- Mixing framework versions across services that share a checkpoint database.

## 6. Design-review questions

1. What fraction of this flow is genuinely model-decided? Can we push the rest into static edges?
2. If the process is killed mid-run, what does the user see, and what re-executes?
3. Which layer owns retries — the node, the middleware, the model client, or the queue?
4. Who owns this graph in six months, and can they change one tool without redeploying everything?
5. What is the p95 token count per run, and what does that cost at projected volume?

## References

- Overview: `/oss/python/langgraph/overview`, `/oss/python/langchain/overview`
- Philosophy: `/oss/python/langchain/philosophy`
- Choosing APIs: `/oss/python/langgraph/choosing-apis`
- Thinking in LangGraph: `/oss/python/langgraph/thinking-in-langgraph`
- Workflows and agents: `/oss/python/langgraph/workflows-agents`
