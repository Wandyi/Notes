# KnowledgeAgent — Flow Diagrams

## 1. End-to-end request sequence

How a single `assistant.ask(...)` call flows through the control and data planes.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant A as KnowledgeAssistant
    participant AU as Audit
    participant RB as RBAC
    participant G as Guardrails
    participant RT as Runtime (state machine)
    participant OR as Orchestrator
    participant TR as Tool Registry
    participant RE as Retriever
    participant CF as ConflictResolver
    participant RO as ModelRouter
    participant CA as SemanticCache
    participant LL as Synthesizer
    participant CO as CostAccountant
    participant EV as Evaluator

    U->>A: ask(query, principal)
    A->>CO: open_request(budget)
    A->>AU: request.received
    A->>RT: run(state machine)

    Note over RT: PERCEIVE
    RT->>RB: evaluate(principal)
    RB-->>RT: allowed_sources
    alt denied
        RT-->>A: terminate(ACCESS_DENIED)
    end
    RT->>G: pre_check (PII redaction, injection)
    alt injection
        RT-->>A: terminate(GUARDRAIL)
    end

    Note over RT: PLAN
    RT->>OR: plan = select(tools) ∩ allowed_sources

    Note over RT: ACT
    RT->>OR: gather()
    OR->>TR: concurrent fan-out (per-tool timeout)
    TR-->>OR: documents (+ warnings for failed tools)

    Note over RT: OBSERVE
    RT->>OR: reduce()
    OR->>RE: chunk → hybrid → rerank → freshness
    RE-->>OR: top-k ScoredChunks
    OR->>CF: resolve_conflicts()
    CF-->>OR: conflicts (freshest wins)

    Note over RT: SYNTHESIZE
    RT->>RO: choose model tier (escalate on conflict/thin evidence)
    RT->>CA: get(query, scope)
    alt cache miss
        RT->>LL: synthesize(evidence, conflicts)
        LL-->>RT: cited answer
        RT->>CO: charge(tier, tokens)
        RT->>CA: put(query, result, scope)
    end
    RT->>G: post_check groundedness
    RT-->>A: answer + citations + conflicts

    A->>EV: evaluate trajectory + groundedness
    A->>AU: request.answered (cost, sources, scores)
    A-->>U: consolidated cited answer
```

---

## 2. Runtime state machine

```mermaid
stateDiagram-v2
    [*] --> PERCEIVE
    PERCEIVE --> PLAN: authorized & clean
    PERCEIVE --> TERMINATED: access denied / injection
    PLAN --> ACT: workers selected
    PLAN --> TERMINATED: no permitted sources
    ACT --> OBSERVE: documents gathered
    ACT --> TERMINATED: no documents
    OBSERVE --> SYNTHESIZE: evidence found
    OBSERVE --> TERMINATED: no grounded evidence
    SYNTHESIZE --> COMPLETE: answer synthesized
    SYNTHESIZE --> TERMINATED: budget exceeded
    COMPLETE --> [*]
    TERMINATED --> [*]

    note right of PERCEIVE
        Every transition is bounded by
        step / tool-call / wall-clock
        budgets; a breach forces
        TERMINATED(BUDGET).
    end note
```

---

## 3. Conflict resolution & freshness

Why the stale runbook's `replicas: 3` never wins over live `replicas: 6`.

```mermaid
flowchart TB
    START["Retrieved chunks with structured claims"]
    START --> GROUP["Group claims by key<br/>(e.g. 'replicas')"]
    GROUP --> CHECK{"≥ 2 distinct<br/>values for a key?"}
    CHECK -- no --> AGREE["Agreement — not a conflict"]
    CHECK -- yes --> FRESH["Pick value from the<br/>freshest source (max updated_at)"]
    FRESH --> RECORD["Record Conflict:<br/>resolved_value + winning_source<br/>+ all competing values"]
    RECORD --> WARN["Answer notes the conflict;<br/>stale sources flagged in citations"]
```

Example resolution for *"How many replicas does payment-service run in
production?"*:

| Source | `replicas` | Age | Stale? |
|---|---|---|---|
| Helm (values-prod) | **6** | ~15d | no ← **winner (freshest)** |
| Kubernetes (live) | 6 | ~20d | no |
| Runbook | 3 | ~200d | **yes** |

The router also *escalates to the strong model* when a conflict is present,
because conflicting evidence is exactly when careful synthesis matters most.

---

## 4. Tool fan-out with graceful degradation

```mermaid
flowchart LR
    Q["query"] --> SEL["dynamic selection<br/>(rank by manifest relevance,<br/>backfill for breadth,<br/>cap at budget)"]
    SEL --> POOL["concurrent fan-out<br/>(thread pool, per-tool timeout)"]
    POOL --> T1["github ✓"]
    POOL --> T2["helm ✓"]
    POOL --> T3["slack ✓"]
    POOL --> T4["jira ✗ timeout"]
    POOL --> T5["terraform ✗ error"]
    T1 --> MERGE["merge documents"]
    T2 --> MERGE
    T3 --> MERGE
    T4 -. warning .-> MERGE
    T5 -. warning .-> MERGE
    MERGE --> NEXT["retrieval pipeline"]
```

A tool that errors or times out is recorded as a warning and skipped — a single
dead connector never stalls or fails the whole request.
