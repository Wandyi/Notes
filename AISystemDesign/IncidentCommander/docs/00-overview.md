# 00 — Overview & Problem Framing

## The job to be done

Production incidents are resolved in three phases: **detect**, **diagnose**, **remediate**.
Empirically, diagnosis dominates MTTR — not because the fix is hard, but because the
*evidence is scattered* across a dozen systems and the on-call human is a slow, serial,
fatigued query planner. The AI DevOps Incident Commander (IC) is a stateful multi-agent
system that automates the *evidence-gathering and reasoning* of diagnosis, and executes the
remediation **only behind a human approval gate**.

The IC is explicitly **not** a chatbot: it is triggered by machine events (alerts), it holds
durable per-incident state, it runs a bounded workflow with real side effects, and it is
accountable through an audit trail. A chatbot answers a question and forgets; the IC drives
an incident from `DETECT` to a signed-off `POSTMORTEM`.

---

## Users

| Tier | Persona | What they get from the IC |
|---|---|---|
| Primary | **SRE / Platform / DevOps engineer** (on-call) | Auto-triage, ranked root cause with evidence, a proposed runbook to approve, auto-verification |
| Secondary | **Backend engineer** | "Your deploy `v2.3.1` 6 min before the spike is the leading suspect — here's the diff and the error signature" |
| Secondary | **Incident Commander (human role)** | A live, structured incident timeline and a one-click approval console |
| Secondary | **Engineering Manager / Director** | Blameless postmortems generated automatically; MTTR/MTTD trends |

The **human Incident Commander** role and this **software** Incident Commander coexist: the
software runs the investigation and drafts decisions; the human role owns the *approval* and
the incident's external comms.

---

## Goals & non-goals

### Goals
- **Reduce MTTR** by parallelizing evidence collection and pre-computing a ranked, cited RCA.
- **Reduce MTTD** by correlating an alert with recent changes (deploys, config, infra) the
  instant it fires.
- **Safe automation**: execute *pre-authored, versioned* runbooks, never free-form `kubectl`.
- **Evidence-based recommendations**: no hypothesis without citations; confidence is
  explainable.
- **Human approvals**: a mandatory gate before any production mutation (with a narrow,
  explicitly configured auto-remediation allowlist).
- **Incident documentation**: a blameless postmortem synthesized from the actual trajectory.

### Non-goals (deliberately out of scope for v1)
- **Autonomous production changes by default.** Write actions are gated; the "lights-out"
  auto-remediation set is opt-in and blast-radius-limited.
- **Replacing monitoring/alerting.** The IC *consumes* alerts; it is not the alert source.
- **Arbitrary code execution / free-form shell.** Remediation is runbook-shaped only.
- **Being the system of record for dashboards.** It reads them; Grafana/Prometheus stay
  authoritative.
- **Multi-cloud infra provisioning.** It can *read* Terraform state and *propose* changes;
  applying IaC is a separate, heavily gated runbook class.

---

## The worked example incident (used throughout the docs)

> **PagerDuty:** `PaymentAPI-HighErrorRate` — error rate `2% → 35%`, p99 latency `120ms →
> 4.1s`, started `14:32 UTC`.

A human would now open eight tabs. The IC instead:

1. **Triage** — dedupe/enrich the alert, pull the service topology for `payment-api`,
   classify severity (`SEV2`), open incident `INC-4471`.
2. **Plan** — the Investigation Planner picks a *relevant subset* of the 16 agents:
   Kubernetes, Prometheus, Loki, GitHub, FluxCD/Argo, Helm, PostgreSQL, Redis, Runbook,
   Incident History. (It skips DNS/Network/Cost agents — no signal suggests them yet.)
3. **Investigate (parallel)** — all selected agents query their sources concurrently, each
   returning bounded, timestamped **evidence** with citations.
4. **Correlate** — the Evidence Collector aligns everything on a timeline: a Helm release
   `payment-api-2.3.1` at `14:29`, a new `PoolTimeoutError` log signature at `14:31`, Postgres
   `active_connections` pinned at `max_connections=100` from `14:31`.
5. **Hypothesize** — candidates: (a) bad deploy exhausts the DB pool; (b) Postgres degraded
   independently; (c) Redis eviction storm; (d) upstream dependency.
6. **Debate** — a skeptic agent tries to falsify (a): "if it's the pool, Redis latency
   should be flat" → confirmed flat → (a) survives; (b) dies (Postgres CPU normal); (c) dies
   (no eviction metrics).
7. **Score** — confidence in (a) = `0.88` (deploy time correlation + new error signature +
   saturation metric, three independent evidence classes).
8. **Approve** — the Human Approval Gateway shows: *"Root cause: `2.3.1` set
   `db.pool.max=5` (was `50`). Proposed fix: runbook `rollback-helm-release` to `2.3.0`.
   Blast radius: 1 service, reversible. Approve?"*
9. **Remediate** — on approval, the Executor runs `helm rollback payment-api 2.3.0`.
10. **Verify** — the Health Verifier watches error rate + latency return to baseline for a
    stabilization window; if not, it auto-rolls-back the rollback and re-escalates.
11. **Document** — the Postmortem Generator emits a blameless writeup: timeline, root cause,
    the pool-size diff, detection/resolution gaps, and action items.

This single trajectory exercises all eight design principles; the docs dissect it from each
angle, and [docs/14-sequence-flows.md](14-sequence-flows.md) shows it as sequence diagrams.

---

## Severity model (drives budgets and autonomy)

| Sev | Trigger example | Fan-out width | Wall-clock budget | Autonomy |
|---|---|---|---|---|
| SEV1 | Full outage, revenue-critical | Max (all relevant agents, escalate model tier) | Aggressive (minimize latency, spend more) | Human gate always; page immediately |
| SEV2 | Major degradation (the example) | Wide | Balanced | Human gate; auto-remediation allowlist eligible |
| SEV3 | Minor/partial, single tenant | Narrow (targeted agents) | Cost-optimized | Human gate; often "recommend only" |
| SEV4 | Noise / flapping | Minimal (dedupe + history) | Cheapest tier | Usually auto-close as known-flap |

Severity is set at triage and **re-evaluated** as evidence arrives; it is the single knob
that ties together budget ([09](09-cost-performance.md)), autonomy
([06](06-safety-guardrails.md)), and orchestration width ([03](03-orchestration.md)).

---

## What "distinguished-staff level" means here

Three things a senior reviewer will probe, and where each is answered:

- **"Show me the plane separation."** → [01](01-architecture.md), [08](08-governance-lifecycle.md).
  You cannot execute a runbook that bypasses RBAC/approval/audit; it is structurally
  impossible, not policy.
- **"Show me it won't make the outage worse."** → [06](06-safety-guardrails.md),
  [11](11-remediation-and-verification.md), [15](15-failure-modes.md). Read/write split,
  dry-run, blast-radius caps, stabilization windows, auto-rollback of its own remediation.
- **"Show me it won't confidently lie."** → [10](10-hypothesis-and-debate.md),
  [07](07-evaluation-observability.md). Mandatory falsification debate, evidence-weighted
  confidence, and offline replay against golden incidents that gate any change to the RCA
  logic.
