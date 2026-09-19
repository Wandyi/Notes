# Staff-Level LangGraph / LangChain Architecture — Master Index

> Curated from the official docs at [docs.langchain.com](https://docs.langchain.com) (LangChain v1.x, LangGraph v1.2+, Deep Agents, LangSmith Deployment / Agent Server).
> Target audience: staff / principal engineers designing agent platforms that must run in production at scale.

## ⚠️ New to LangChain or LangGraph? Do not start here.

**The files in this directory are dense reference notes.** They assume you already know what a
graph, a node, a reducer, `Command`, `Send`, a checkpointer, and `interrupt()` are. Read them to look
something up, not to learn it.

👉 **Start with [`primer/`](primer/README.md)** — a ground-up introduction that assumes only Python
and one prior LLM API call, and defines every term the first time it appears. About 4,000 lines
across eight files, in reading order.

Then the two fully worked designs, which turn the primer's mechanisms into engineering judgment:
[Supervisor vs. Swarm](../AISystemDesign/SupportAgent/EXPLAINED.md) and
[Model Tiering](../AISystemDesign/ModelTiering/EXPLAINED.md). Plus
[25 design problems](../AISystemDesign/SCENARIOS.md) for practice.

## How this repo is organised

Every file follows the same skeleton so it is skimmable and reviewable:

1. **Concepts** — the mental model and the primitives, stated precisely.
2. **How to implement** — minimal but real Python, with the current v1 API surface.
3. **Scenarios** — where this actually matters, phrased as production situations.
4. **Scaling / staff-level considerations** — failure modes, cost, blast radius, org impact.
5. **Anti-patterns** — the things that look fine in a demo and break at 500 rps.
6. **Design-review questions** — what to ask (or be asked) in an architecture review.

## Reading order

### Track 1 — Foundations (you cannot skip these)

| # | File | What it unlocks |
|---|------|-----------------|
| 01 | [Ecosystem & mental model](01-ecosystem-and-mental-model.md) | LangChain vs LangGraph vs Deep Agents vs LangSmith; which layer to build on |
| 02 | [Graph API core](02-graph-api-core.md) | `StateGraph`, nodes, edges, compile, schema boundaries |
| 03 | [State, channels & reducers](03-state-channels-and-reducers.md) | The data model — the single biggest determinant of scalability |
| 04 | [Pregel runtime & execution model](04-pregel-runtime-and-execution-model.md) | Super-steps, BSP, parallelism, determinism, recursion limits |
| 05 | [Control flow: Command, Send, routing](05-control-flow-command-send-routing.md) | Map-reduce, dynamic edges, handoffs, deferred nodes |
| 06 | [Functional API](06-functional-api.md) | `@entrypoint` / `@task` — when imperative beats a graph |

### Track 2 — State, memory and context

| # | File | What it unlocks |
|---|------|-----------------|
| 07 | [Persistence & checkpointers](07-persistence-and-checkpointers.md) | Threads, checkpoints, durability modes, serialization, encryption |
| 08 | [Stores & long-term memory](08-stores-and-long-term-memory.md) | Cross-thread memory, namespaces, semantic search, TTLs |
| 09 | [Context engineering](09-context-engineering.md) | The discipline that decides whether your agent works at all |
| 10 | [Agents & middleware](10-agents-and-middleware.md) | `create_agent`, the hook system, composition rules |
| 11 | [Tools & tool execution](11-tools-and-tool-execution.md) | Tool contracts, injection, errors, dynamic selection, MCP |

### Track 3 — Topology

| # | File | What it unlocks |
|---|------|-----------------|
| 12 | [Multi-agent architecture](12-multi-agent-architecture.md) | Subagents, handoffs, router, skills — and their cost curves |
| 13 | [Subgraphs & composition](13-subgraphs-and-composition.md) | Team boundaries, checkpointer scoping, namespace hygiene |
| 24 | [Deep Agents](24-deep-agents.md) | The batteries-included harness: filesystem, planning, skills, sandboxes |
| 25 | [RAG & knowledge architecture](25-rag-and-knowledge.md) | Agentic retrieval, knowledge bases, evaluation of retrieval |

### Track 4 — Runtime behaviour under real users

| # | File | What it unlocks |
|---|------|-----------------|
| 14 | [Streaming](14-streaming.md) | Stream modes, v2 protocol, token streaming, reconnect |
| 15 | [Human-in-the-loop & interrupts](15-human-in-the-loop-and-interrupts.md) | `interrupt()`, resume semantics, the idempotency rules |
| 16 | [Time travel & debugging](16-time-travel-and-debugging.md) | Replay, fork, `as_node`, production forensics |
| 17 | [Durability, fault tolerance & idempotency](17-durability-fault-tolerance-idempotency.md) | Retries, timeouts, error handlers, drain, saga/compensation |

### Track 5 — Platform, scale and operations

| # | File | What it unlocks |
|---|------|-----------------|
| 18 | [Deployment & Agent Server architecture](18-deployment-and-agent-server.md) | `langgraph.json`, API servers, queue workers, assistants, crons |
| 19 | [Scaling & performance](19-scaling-and-performance.md) | Throughput math, `N_JOBS_PER_WORKER`, DB sizing, autoscaling |
| 20 | [Observability & evaluation](20-observability-and-evaluation.md) | Tracing, metadata, cost tracking, offline + online evals |
| 21 | [Testing strategy](21-testing-strategy.md) | Unit, integration, trajectory, regression, CI gates |
| 22 | [Security, guardrails & multi-tenancy](22-security-guardrails-multitenancy.md) | AuthN/Z, tenant isolation, PII, prompt injection, sandboxing |
| 23 | [Cost & token economics](23-cost-and-token-economics.md) | Prompt caching, routing, compaction, node caching |
| 26 | [Migration & versioning](26-migration-and-versioning.md) | Live-thread schema evolution, assistant versions, v1 migration |

### Track 6 — Putting it together

| # | File | What it unlocks |
|---|------|-----------------|
| 27 | [Reference architectures](27-reference-architectures.md) | Five end-to-end blueprints with sizing and failure analysis |
| 28 | [Staff design-review checklist](28-staff-design-review-checklist.md) | The single page to bring to an architecture review |
| 29 | [Glossary & API quick reference](29-glossary-and-api-reference.md) | Every primitive, one line each |

## The 60-second mental model

```
LangSmith Deployment / Agent Server   ← runtime platform: API servers, queue workers, Postgres, Redis
        │
        ├── Assistants (versioned config over a graph)
        │
Deep Agents        ← opinionated harness: planning, filesystem, subagents, skills
        │
LangChain          ← create_agent + middleware + models + tools (the "standard agent")
        │
LangGraph          ← Pregel runtime: StateGraph, channels, checkpoints, interrupts, streaming
        │
LangGraph Checkpoint / Store   ← durable state: Postgres, SQLite, custom backends
```

**Choose the highest layer that still gives you the control you need.** Staff-level mistakes cluster
at both extremes: hand-rolling a Pregel graph when `create_agent` + two middleware would do, or
shipping `create_agent` when the domain demands deterministic, auditable, resumable orchestration.

## The five properties that make a LangGraph architecture "scalable"

1. **Bounded state.** Checkpoint size per super-step is the hidden cost driver. See [03](03-state-channels-and-reducers.md) and [07](07-persistence-and-checkpointers.md).
2. **Bounded context.** Tokens per model call determine latency, cost and quality. See [09](09-context-engineering.md) and [23](23-cost-and-token-economics.md).
3. **Resumability.** Every long-running step must survive a pod eviction. See [17](17-durability-fault-tolerance-idempotency.md).
4. **Independent scaling of request path and run path.** See [18](18-deployment-and-agent-server.md) and [19](19-scaling-and-performance.md).
5. **Observability with a feedback loop.** Traces → datasets → evals → regression gates. See [20](20-observability-and-evaluation.md) and [21](21-testing-strategy.md).

## Version notes

- LangGraph **1.2+** introduced per-node `timeout`, node-level `error_handler`, `set_node_defaults`,
  `DeltaChannel` (beta) and graceful drain. Several patterns here depend on that.
- Streaming **v2** (`version="v2"`) is the unified `StreamPart` format; v1 output shapes differ.
- Default `recursion_limit` is **1000** as of 1.0.6.
- `create_agent` (LangChain v1) replaces the older `create_react_agent` prebuilt; the
  `langgraph-supervisor` / `langgraph-swarm` packages are superseded by the middleware +
  multi-agent patterns in [12](12-multi-agent-architecture.md).

Always re-verify API details against <https://docs.langchain.com/llms.txt> before shipping —
the OSS surface moves fast.
