# 14 — End-to-End Sequence Flows

The worked example ([00](00-overview.md)) as concrete sequence diagrams. This is the "watch it
run" view that ties every doc together.

---

## 1. Happy path: detect → diagnose → remediate → verify → document

```mermaid
sequenceDiagram
  autonumber
  participant PD as PagerDuty
  participant IC as IC Supervisor (state machine)
  participant CP as Control Plane (registry/RBAC/cost/audit)
  participant PL as Investigation Planner
  participant AG as Investigation Agents (×N, parallel)
  participant EC as Evidence Collector
  participant HR as Hypothesis + Debate + Confidence
  participant GT as Approval Gateway
  participant EX as Runbook Executor
  participant HV as Health Verifier
  participant PM as Postmortem Generator

  PD->>IC: alert PaymentAPI-HighErrorRate
  IC->>CP: open incident INC-4471 (audit + budget alloc)
  IC->>IC: TRIAGE → severity SEV2, topology(payment-api)
  IC->>PL: PLAN
  PL->>CP: which agents are PRODUCTION + authorized?
  CP-->>PL: allowed read-only agent set
  PL-->>IC: select 10/16 + investigation DAG
  IC->>AG: INVESTIGATE (fan-out, per-agent timeout)
  par concurrent
    AG->>AG: K8s, Prometheus, Helm, Argo, GitHub (wave 1)
  and
    AG->>AG: Incident History (recall similar)
  end
  AG-->>EC: bounded, cited evidence
  Note over EC: deploy 2.3.1 @14:29 · PoolTimeoutError ×10412 · db 100/100
  EC->>AG: wave 2 targeted (Loki around 14:29, Postgres pool)
  AG-->>EC: confirming evidence
  EC-->>IC: correlated timeline (6 salient events)
  IC->>HR: HYPOTHESIZE → DEBATE → SCORE
  HR->>AG: skeptic: check Redis latency (falsify H1)
  AG-->>HR: Redis flat → H1 survives, H2 and H3 falsified
  HR-->>IC: H1 conf 0.88 (3 independent classes, no contradictions)
  IC->>GT: APPROVE (render root cause + proposed rollback + dry-run)
  GT->>EX: dry-run rollback 2.3.0
  EX-->>GT: dry-run clean (1 release, reversible, no data)
  GT->>CP: authorize(principal, runbook, resources)
  CP-->>GT: signed ExecutionGrant
  Note over GT: human @alice approves
  GT->>EX: execute(grant)  [REMEDIATE]
  EX->>EX: apply helm rollback (idempotency key)
  EX-->>IC: applied
  IC->>HV: VERIFY (watch SLOs, stabilization window)
  HV-->>IC: recovered + stable 3m ✅
  IC->>PM: DOCUMENT
  PM-->>IC: blameless postmortem draft
  IC->>CP: close incident + audit, label golden incident
```

---

## 2. Unhappy path A — remediation didn't work (auto-rollback)

```mermaid
sequenceDiagram
  autonumber
  participant IC as IC Supervisor
  participant EX as Executor
  participant HV as Health Verifier
  participant GT as Approval Gateway
  IC->>EX: execute approved runbook
  EX-->>IC: applied
  IC->>HV: VERIFY
  HV-->>HV: SLOs still breached (or worse) over window
  HV->>EX: auto-rollback (runbook.rollback)
  EX-->>HV: rolled back
  HV-->>HV: baseline restored ✅
  HV->>GT: re-escalate: fix ineffective + new evidence
  GT->>IC: back to APPROVE with a note (human drives)
  Note over IC: trajectory flagged as remediation-safety failure for evaluator
```

---

## 3. Unhappy path B — low confidence / inconclusive

```mermaid
sequenceDiagram
  autonumber
  participant IC as IC Supervisor
  participant HR as Hypothesis+Debate+Confidence
  participant PL as Planner
  participant GT as Approval Gateway
  IC->>HR: SCORE
  HR-->>IC: leading conf 0.46 (contradiction present, 1 agent down)
  alt bounded extra round available
    IC->>PL: PLAN (targeted: retry failed agent, add DNS/Network)
    PL-->>IC: extra evidence
    IC->>HR: re-SCORE
    HR-->>IC: conf 0.71
    IC->>GT: APPROVE (recommend, human decides)
  else rounds exhausted
    IC->>GT: present evidence + top candidates, NO proposed fix
    Note over GT: page human to drive — IC assisted, didn't decide
  end
```

---

## 4. Unhappy path C — budget/injection short-circuit

```mermaid
sequenceDiagram
  autonumber
  participant IC as IC Supervisor
  participant CP as Cost Governor / Guardrails
  participant OP as On-call human
  IC->>CP: check_budget() each transition
  alt budget exceeded
    CP-->>IC: BudgetExceeded
    IC->>IC: checkpoint() → SUSPENDED(BUDGET)
    IC->>OP: page with partial cited investigation-so-far
  else injection detected in a log/Slack source
    CP-->>IC: quarantine flagged content (kept for human, excluded from model)
    IC->>IC: annotate incident, continue with clean evidence
    Note over IC: gate is the backstop — human still sees proposed action
  end
```

---

## 5. The one-glance lifecycle (recap)

```mermaid
flowchart LR
  D[DETECT]-->T[TRIAGE]-->P[PLAN]-->I[INVESTIGATE]-->C[CORRELATE]-->H[HYPOTHESIZE]-->DB[DEBATE]-->S[SCORE]
  S-->|low conf, bounded retry|P
  S-->A[APPROVE]
  A-->|approve|R[REMEDIATE]-->V[VERIFY]
  A-->|recommend-only|DOC[DOCUMENT]
  V-->|healthy|DOC
  V-->|not healthy|R
  V-->|needs re-decision|A
  DOC-->CL[CLOSE]
  I-.->SUS[SUSPENDED]
  DB-.->SUS
```

Continue to [15 — Failure modes & resilience](15-failure-modes.md).
