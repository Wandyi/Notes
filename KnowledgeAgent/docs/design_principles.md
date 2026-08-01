# Design-principle mapping

How the eight system-design areas map onto this codebase. The mental model
throughout: the **control plane governs the data plane**, and observability,
safety, and evaluation are designed in from the start.

### 1. Agent runtime & execution model
* **Explicit state machine** — `data_plane/runtime.py` runs `PERCEIVE → PLAN →
  ACT → OBSERVE → SYNTHESIZE → COMPLETE` with explicit termination conditions,
  not free-form reasoning.
* **Concurrency & resource limits** — `BudgetConfig` caps steps, tool calls, and
  wall-clock per request; `RunContext.check_budget` enforces them and forces
  `TERMINATED(BUDGET)`.
* **Long-running / resumable** — `RunContext.snapshot()` is a checkpoint of the
  blackboard + counters for durable, resumable execution.
* **Isolation & multi-tenancy** — every request carries a `Principal(tenant_id)`;
  cost, cache, and long-term memory are all tenant-scoped.

### 2. Orchestration & coordination
* **Topology as a choice** — `data_plane/orchestrator.py` implements the
  supervisor/orchestrator-worker pattern with a *shallow* one-level fan-out,
  chosen because the workload is "consult N sources and merge", and to keep
  interactive latency low (no deep hierarchy).
* **Bounded handoffs** — `Orchestrator.reduce` collapses the fan-out to a small
  re-ranked top-k *before* anything downstream sees it, so context/cost don't
  grow non-linearly with source count (the classic orchestrator-overflow bug).

### 3. Memory & context management
* **Short- vs long-term split** — `data_plane/memory.py`: `ShortTermMemory`
  (bounded session ring buffer) and `LongTermMemory` (durable, per-tenant).
* **Selective recall over stuffing** — long-term recall is embedding-similarity
  based and returns only the top few facts (avoids "lost in the middle").

### 4. Tool & integration layer
* **Standardized interface + manifest** — `data_plane/tools/base.py`
  (`ToolManifest`) is the MCP-style declarative contract per connector.
* **Dynamic selection** — `ToolRegistry.select` ranks tools by manifest
  relevance and returns only the top-N (you can't put every schema in context).
* **Robust error handling** — `ToolRegistry.gather` runs connectors concurrently
  with per-tool timeouts; failures/timeouts become warnings, never cascades
  (tested with `FailingConnector` / `SlowConnector`).

### 5. Safety & guardrails
* **Layered guardrails** — `data_plane/guardrails.py`: fast, deterministic
  **pre-LLM** checks (PII redaction, prompt-injection detection) in the hot path;
  **post-LLM** groundedness check before the answer reaches the user. Heavy
  LLM-as-judge work is deferred to the async evaluator, not the hot path.
* **Action authorization** — RBAC bounds which sources a principal may read; the
  access-**scoped** semantic cache prevents cross-role/cross-tenant answer leaks.
* **Safety designed in** — role decomposition, memory scoping, and the cache
  scope key were designed up front, not retrofitted.

### 6. Evaluation & observability
* **Observability as foundation** — `observability/tracing.py` emits a span for
  every retrieval, tool call, LLM call, guardrail, and state transition.
* **Trajectory-level analysis** — `evaluation/evaluator.py` scores the whole
  trajectory (retrieval ran, multi-source, grounded, cited, clean, conflicts
  handled), not just the final string.
* **Continuous + offline** — the same evaluator scores live answers *and* gates a
  fixed eval set (`run_offline_eval`); groundedness is a post-LLM check.

### 7. Platform governance & lifecycle (control plane)
* **Agent registry as control plane** — `control_plane/registry.py` manages
  identity, capabilities, and an evaluation-gated lifecycle state machine
  (`REGISTERED → EVALUATING → STAGED → PRODUCTION → RETIRED`) with staleness
  detection.
* **Discovery & ownership** — registration *requires* a named owner (unowned
  agents are rejected).
* **RBAC & policy enforcement** — `control_plane/rbac.py`, enforced centrally.
* **Audit & compliance** — `control_plane/audit.py` records every
  governance-relevant event (access decisions, refusals, answers, cost).

### 8. Cost & performance
* **Model tiering & routing** — `llm/client.py::ModelRouter` routes to a cheap
  model by default and escalates to a strong model on thin or conflicting
  evidence.
* **Caching** — `SemanticCache` short-circuits repeated/near-identical questions
  (access-scoped for safety).
* **Cost visibility per agent/tenant** — `control_plane/cost.py` prices every
  call from the tier and enforces per-request and per-tenant/day budgets.

---

## Honest limitations (what a reference impl deliberately stubs)

* **Embeddings** are feature-hashed bag-of-words, not a trained encoder — good
  enough to exercise the dense path deterministically; swap
  `HashingEmbeddingProvider` for a hosted encoder in production.
* **The re-ranker** is a lexical scorer, not a real cross-encoder.
* **Claims** for conflict detection are attached to seed documents; production
  extracts them with an NER/LLM pass over chunk text.
* **The synthesizer** defaults to a deterministic extractive `LocalSynthesizer`;
  `AnthropicSynthesizer` (claude-opus-5, adaptive thinking) is the production
  drop-in.
* **Connectors** read an in-memory seed corpus rather than live source APIs.

Every one of these sits behind an interface that production code replaces
without touching the control-plane or orchestration logic.
