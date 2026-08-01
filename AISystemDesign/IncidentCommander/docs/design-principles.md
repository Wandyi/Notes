# Design-principle mapping

How the eight system-design areas map onto the Incident Commander. The mental model
throughout, shared with the KnowledgeAgent design: the **control plane governs the data
plane**, and observability, safety, and evaluation are designed in from the start.

### 1. Agent runtime & execution model
* **Explicit state machine** — the supervisor runs `DETECT → TRIAGE → PLAN → INVESTIGATE →
  CORRELATE → HYPOTHESIZE → DEBATE → SCORE → APPROVE → REMEDIATE → VERIFY → DOCUMENT → CLOSE`
  with enumerated legal transitions and explicit termination — not free-form reasoning.
  ([02](02-agent-runtime.md))
* **Concurrency & resource limits** — per-incident budgets cap steps, tool-calls, wall-clock,
  and dollars (scaled by severity); a breach forces `SUSPENDED(BUDGET)` and pages a human,
  rather than crashing. ([02](02-agent-runtime.md), [09](09-cost-performance.md))
* **Long-running / resumable** — stateless workers over a durable Incident Registry;
  `checkpoint()` after every transition; the one dangerous step (`REMEDIATE`) is idempotency-
  keyed so a resume can't double-execute. ([02](02-agent-runtime.md))
* **Isolation & multi-tenancy** — every incident carries a `Principal(tenant/team/service)`;
  credentials, state, memory, and budgets are all tenant-scoped. ([02](02-agent-runtime.md))

### 2. Orchestration & coordination
* **Topology as a choice** — supervisor/orchestrator-worker with a *shallow* one-level
  fan-out, chosen because the workload is "consult N sources and correlate," keeping latency,
  cost, and blast radius bounded and the audit trail linear. ([03](03-orchestration.md))
* **Bounded handoffs** — the Evidence Collector reduces the union of agent outputs to a
  single correlated timeline + top-k salient events *before* any LLM sees it, so context and
  cost don't grow with log volume (the orchestrator-overflow bug). ([03](03-orchestration.md),
  [04](04-memory-context.md))
* **Dynamic, conditional fan-out** — the Planner selects a *relevant subset* of the 16 agents
  and expresses conditional wave-2 probes as a small DAG (fetch Loki *around* the suspect
  deploy, probe Postgres *only if* saturation seen). ([03](03-orchestration.md))

### 3. Memory & context management
* **Short- vs long-term split** — a bounded short-term reasoning ring buffer vs. the durable,
  cross-incident **Incident History** store. ([04](04-memory-context.md))
* **Selective recall over stuffing** — history recall returns the top-3 *similar* past
  incidents by fingerprint, entered as *labeled priors* (must be re-confirmed against current
  evidence). Each LLM stage gets a purpose-built, compact context, not the whole history.
  ([04](04-memory-context.md))
* **Structured blackboard, not a transcript** — the incident is a graph of typed objects
  (evidence, hypotheses, decisions), which is why the postmortem is a reconstruction, not a
  generation. ([04](04-memory-context.md), [13](13-data-model.md))

### 4. Tool & integration layer
* **Standardized interface + manifest** — 16 investigation agents behind a uniform,
  MCP-style `ToolManifest` (typed I/O, capabilities, cost hint, scope, timeout, output cap).
  ([05](05-tool-integration.md))
* **Dynamic selection** — the Planner ranks manifests and selects top-N; you can't put every
  source's schema/output in context. ([05](05-tool-integration.md), [03](03-orchestration.md))
* **Robust error handling** — concurrent invocation with per-agent timeouts, circuit breakers,
  and rate limiters; failures become `degraded` timeline notes, never cascades — and they
  *lower confidence* rather than being hidden. ([05](05-tool-integration.md))
* **Read/write firewall** — all agents are read-only with read-only credentials; the manifest
  has no write capability. ([05](05-tool-integration.md))

### 5. Safety & guardrails
* **Layered guardrails** — fast deterministic checks in the hot path (alert validation,
  injection defense treating source content as data, PII/secret redaction, groundedness);
  heavy LLM-judge scoring deferred to the async evaluator. ([06](06-safety-guardrails.md))
* **Action authorization** — three gates before any write: RBAC, blast-radius policy, and the
  approval policy engine (human gate vs. narrow auto-allowlist). The Executor only accepts a
  signed `ExecutionGrant`. ([06](06-safety-guardrails.md), [08](08-governance-lifecycle.md))
* **Reversible & self-checking remediation** — mandatory dry-run shown pre-approval, bounded
  blast radius, stabilization-window verification, and auto-rollback of the IC's own fix.
  ([11](11-remediation-and-verification.md))
* **Safety designed in** — the read/write split, separate Executor identity, gates, tenant
  scoping, and audit were part of the first structure; there is no code path to the Executor
  that bypasses them. ([01](01-architecture.md), [06](06-safety-guardrails.md))

### 6. Evaluation & observability
* **Observability as foundation** — a span for every agent call, LLM call, guardrail, state
  transition, approval, runbook step, and health check; the trace *is* the postmortem's raw
  material. ([07](07-evaluation-observability.md))
* **Trajectory-level analysis** — scores coverage, groundedness, correlation quality,
  falsification effort, calibration, root-cause accuracy, remediation safety, and efficiency —
  not just the final answer. ([07](07-evaluation-observability.md))
* **Continuous + offline** — the same evaluator scores live incidents (calibration/drift) and
  gates changes via **golden incident replay** (recorded evidence bundles replayed
  deterministically). ([07](07-evaluation-observability.md))

### 7. Platform governance & lifecycle (control plane)
* **Agent & runbook registry as control plane** — identity, mandatory ownership, versioning,
  and an eval-gated lifecycle (`REGISTERED → EVALUATING → STAGED → PRODUCTION → RETIRED`) with
  staleness detection. ([08](08-governance-lifecycle.md))
* **RBAC & policy enforced centrally** — authorization decided in one auditable place, consumed
  as signed grants by the data plane. ([08](08-governance-lifecycle.md))
* **Governed remediation** — runbooks are versioned, signed, code-owned artifacts declaring
  blast radius, dry-run, verify spec, and rollback. ([08](08-governance-lifecycle.md),
  [11](11-remediation-and-verification.md))
* **Audit & compliance** — append-only record of every access decision, approval, execution,
  auto-remediation, budget breach, and publication; change-freeze aware. ([08](08-governance-lifecycle.md))

### 8. Cost & performance
* **Model tiering & routing** — cheap/fast model by default (triage, summarization,
  classification); escalate to the strong model for judgment (hypothesis/debate) and on
  thin/conflicting evidence or high severity. ([09](09-cost-performance.md))
* **Caching** — incident/tenant-scoped evidence-query and reasoning caches; historical recall
  as an institutional cache; cached diagnoses re-confirmed before driving a gate.
  ([09](09-cost-performance.md))
* **Parallelism for latency** — the investigation is `max(agent latency)`, not the sum; the
  bounded reduce keeps hypothesis latency flat regardless of log volume; SEV1 speculative
  fetch. ([09](09-cost-performance.md), [03](03-orchestration.md))
* **Cost visibility & budgets** — every span priced; per-incident and per-tenant/day ceilings;
  "what did this incident cost to diagnose?" is a query. ([09](09-cost-performance.md))

---

## Honest limitations (what a reference impl deliberately stubs)

* **Investigation agents** read mock/seed source data in the reference impl; production
  connectors hit live K8s/Prometheus/Loki/etc. APIs behind read-only credentials.
* **The Executor** targets a mock cluster; production uses a separate, scoped *write* identity
  and real dry-run backends (helm diff, `kubectl --dry-run=server`, TF plan).
* **The confidence calculator** uses a transparent evidence-weighted formula; production may
  fit weights against the calibration data the golden set produces.
* **The debate skeptic** and **hypothesis generator** default to strong-model prompts; the
  reference impl includes deterministic stand-ins so the pipeline runs offline.
* **LLM-as-judge** for RCA accuracy runs offline only (never the hot path); early on,
  thresholds are set conservatively until calibration data accumulates.
* **State machine** runs single-process for clarity; production runs it on a durable workflow
  engine for exactly-once transitions.

Every one of these sits behind an interface that production code replaces without touching the
control-plane, orchestration, or safety logic.
