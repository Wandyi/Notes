# 27 — Reference Architectures

Five end-to-end blueprints. Each includes topology, state design, persistence choices, platform
sizing, failure analysis and the decisions that would be challenged in a design review.

---

## A. Conversational assistant (customer-facing chat)

**Shape**: `create_agent` + middleware, deployed on Agent Server, one thread per conversation.

```
Client ──SSE──► API servers ──► queue workers ──► agent graph
                     │                              ├── retrieval tool  → vector store
                     │                              ├── domain tools    → internal APIs
                     └──► Postgres (threads, checkpoints, store)
                          Redis (stream pub/sub)
```

| Decision | Choice | Why |
|---|---|---|
| Topology | Single agent + dynamic tool scoping | One domain; multi-agent would add calls without benefit |
| State | `messages` (`DeltaChannel` + `add_messages`), `stage`, `cost_usd` | Bounded checkpoint growth |
| Persistence | Postgres checkpointer, `durability="async"` | Survives pod loss mid-turn; not paying `sync` cost |
| Memory | Store, `(tenant, "users", user_id, "profile")`, TTL 90d, refresh on read | Personalisation with a deletion path |
| Context | `SummarizationMiddleware` at ~60k tokens + trimming; prompt caching | Bounded cost per turn |
| Streaming | `messages` filtered to the answer node + `custom` progress; v2 | Good UX, no leaked internals |
| HITL | Only on destructive tools | Minimal friction |
| Double texting | `rollback` | New message supersedes the old in chat |
| TTL | Threads `keep_latest`, 30 days | Storage bounded, latest state retained |

**Sizing (50 rps read / 50 rps write, ~4 s runs)**: 3 API replicas, 5 queue workers,
`N_JOBS_PER_WORKER=25`, Postgres 4 CPU/16 Gi, Redis 2 Gi; autoscaling on both tiers.

**Failure analysis**

| Failure | Behaviour | Mitigation |
|---|---|---|
| Model provider 5xx | Node retry → fallback model | `ModelRetryMiddleware` + `ModelFallbackMiddleware` |
| Worker eviction mid-turn | Resumes from last checkpoint | `durability="async"` + drain on SIGTERM |
| Redis loss | Streams drop; runs continue | Client reconnects via join-stream |
| Runaway loop | Capped | `ModelCallLimitMiddleware(run_limit=15)` + `RemainingSteps` fallback |
| Retrieval outage | Degraded answer with disclosure | Tool returns an explicit error string; agent tells the user |

---

## B. Background workflow / batch pipeline

**Shape**: `@entrypoint` functional API (or a linear `StateGraph`), cron-triggered, no HITL.

```
Cron ──► create N runs ──► queue ──► workers ──► tasks (fetch, transform, write)
                                       └──► Postgres (final state only)
```

| Decision | Choice | Why |
|---|---|---|
| API | Functional API | Imperative branchy logic; no diagram needed |
| Durability | `"exit"` | Only the final state matters; minimises write amplification |
| Idempotency | Deterministic keys per record (`{batch_id}:{record_id}:{step}`) | Safe re-runs |
| Concurrency | `N_JOBS_PER_WORKER=50` (I/O-bound), semaphore inside tasks | Bounded downstream QPS |
| Isolation | **Separate deployment** from interactive traffic | Batch must not starve chat |
| Double texting | `reject` | Duplicate submissions are bugs |
| Observability | Sampled tracing (1%) + full metrics; 100% of failures traced | Cost-controlled |

**Sizing (100k runs/night, ~6 s each, 6-hour window)**: ≈ 4.6 runs/sec →
`4.6 × 6 / 50 ≈ 1` worker steady, provision 4 for headroom and retries; 2 API replicas.

**Failure analysis**: poison records retry then hit an attempt cap and land in a dead-letter table;
a failed batch resumes because each `@task` result is checkpointed; a deploy mid-batch drains and
resumes.

---

## C. Human-approval pipeline (money / infrastructure / compliance)

**Shape**: `StateGraph` with explicit stages, `interrupt()` gates, compensation nodes.

```
START → validate → enrich → risk_score → [interrupt: approve?] → execute → verify → END
                                                │ reject
                                                └────────► cancelled
                       execute failure ─────────────────► compensate
```

| Decision | Choice | Why |
|---|---|---|
| Topology | Graph API, static edges | Auditors read diagrams; the path must be provable |
| Durability | `"sync"` | Every step must survive a crash |
| Approval | `interrupt()` with a versioned payload schema | Renderable UI, notifications, audit |
| Authorization | Enforced in the resume API against the IdP, never from the payload | Prevents privilege escalation |
| Idempotency | Every side-effecting node keyed on `{request_id}:{step}` | Nodes re-run on resume |
| Compensation | `error_handler` → `Command(goto="compensate")` per node | Saga semantics |
| Retention | No TTL (or ≥ 7 years), encrypted serializer | Compliance |
| Notifications | Webhook on interrupt → Slack/email with `thread_id` + `interrupt_id` | Approvals happen where people are |

**Failure analysis**: approval SLA breach → escalation cron scans `interrupted` threads older than
N hours; execute failure → compensate; approver disputes → time travel replay from the pre-approval
checkpoint in a sandbox.

---

## D. Multi-tenant agent platform (8 product teams)

**Shape**: shared platform, per-team graphs, per-tenant assistants.

```
                    ┌─────────── Platform team ───────────┐
                    │ middleware library (auth, PII,      │
                    │ budget, tracing, caching)           │
                    │ base graph + shared subgraphs       │
                    │ Agent Server deployment(s)          │
                    └───────────────┬─────────────────────┘
       ┌───────────────┬────────────┼────────────┬───────────────┐
   team A graph    team B graph  team C graph  ...          team H graph
       │                                   
   assistants: acme-prod, acme-staging, globex-prod, ...
```

| Concern | Design |
|---|---|
| Isolation | `@auth.authenticate` → tenant claims; `@auth.on` filters on `owner`/`tenant_id`; store namespaces tenant-first |
| Configuration | One graph per use case, one **assistant per tenant** — prompts/models/tools vary without code |
| Cost attribution | `metadata.tenant_id` + `agent_name` + `assistant_version` on every trace |
| Quotas | Per-tenant budget in context + `ModelCallLimitMiddleware` + provider key per tier |
| Deployment | Separate deployments per risk tier (internal / customer-facing / regulated); shared image |
| Governance | Platform owns the middleware stack; teams own tools and prompts; eval gate in shared CI |
| Onboarding | Template repo: `langgraph.json`, base middleware, eval harness, tracing metadata helper |

**The main risk** is thread granularity: if a tenant funnels all traffic into one thread, the
"one run per thread at a time" rule creates a per-tenant bottleneck. Model threads per conversation
or per task, never per tenant.

---

## E. Deep research / long-horizon agent

**Shape**: Deep Agents harness, background runs, subagents, file offloading.

```
supervisor (planning + todos)
   ├── researcher subagent × N (parallel, isolated context)
   ├── analyst subagent
   └── critic subagent (rubric grading)
        └── virtual filesystem: /work (state) · /memory (store) · /out (store)
```

| Decision | Choice | Why |
|---|---|---|
| Harness | Deep Agents | Planning, filesystem, subagents, compaction are the requirements |
| Context | Isolate (subagents) + Write (files) + Compress (summarisation) | Task exceeds any single window |
| Backends | Composite: `/work` → state, `/memory` and `/out` → store | Right durability per path |
| Execution | Run mode = background run; client uses join-stream | Runs for 20–60 min |
| Budget | Hard model-call cap per subagent + total run budget in state | Cost containment |
| Quality | `RubricGradingMiddleware` critic loop with an iteration cap | Self-correction without infinite loops |
| Durability | `"async"`; expensive tool segments individually retried | Long runs must survive restarts |
| Sandbox | Code execution in a sandbox with no prod credentials, egress allowlist | Untrusted content is everywhere |

**Failure analysis**: subagent failure → supervisor records a partial result and continues (degrade,
don't abort); context exhaustion → compaction + offload; run exceeds budget → graceful stop with a
partial report and an explicit note about what's missing.

---

## Cross-cutting sizing cheat-sheet

| Load (read/write rps) | API replicas | Queue workers | `N_JOBS_PER_WORKER` | Postgres |
|---|---|---|---|---|
| 5 / 5 | 1 | 1 | 10 | 2 CPU / 8 Gi |
| 5 / 500 | 6 | 10 | 50 | 4 CPU / 16 Gi |
| 500 / 5 | 10 | 1 | 10 | 4 CPU / 16 Gi + 2 read replicas |
| 50 / 50 | 3 | 5 | 10 | 4 CPU / 16 Gi |
| 500 / 500 | 15 | 10 | 50 | 8 CPU / 32 Gi |

(1 CPU / 2 Gi per API server and queue worker; Redis 2 Gi; assumes ~1 s average runs — scale worker
count linearly with actual run duration.)

## References

- `/langsmith/agent-server-scale`, `/langsmith/agent-server`
- `/oss/python/langgraph/case-studies`
- `/oss/python/deepagents/deep-research`, `/data-analysis`, `/content-builder`
- `/oss/python/langchain/multi-agent/*` (worked examples per pattern)
