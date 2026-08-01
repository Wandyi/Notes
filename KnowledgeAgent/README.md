# KnowledgeAgent

A **RAG-based enterprise knowledge assistant**. It answers engineering and
operational questions by searching across multiple internal knowledge sources,
reasoning over the results, resolving conflicting information, and producing one
consolidated answer **with citations** — instead of searching a single document,
it behaves like an experienced engineer.

> **How do I deploy `payment-service` to production?**
>
> Instead of searching only Confluence, it gathers from **GitHub · Helm ·
> Terraform · Slack · Jira · Runbooks · Kubernetes · Architecture docs ·
> Confluence** and produces a single answer — resolving conflicts (e.g. a stale
> runbook that says `replicas: 3` vs. live Helm/Kubernetes that say `6`) and
> flagging stale sources.

The system is built around the split most mature agentic designs converge on: a
governed **control plane** (registry, RBAC, audit, cost, evaluation) over a
**data plane** (runtime state machine, orchestration, retrieval, memory, tools,
guardrails, LLM). See [`docs/architecture.md`](docs/architecture.md),
[`docs/flow.md`](docs/flow.md), and
[`docs/design_principles.md`](docs/design_principles.md).

## Highlights

| Area | What's implemented |
|---|---|
| **Chunking** | structure-aware sliding-window chunking with overlap |
| **Embeddings** | pluggable provider (deterministic local; Claude/hosted in prod) |
| **Hybrid search** | dense (cosine) + sparse (BM25) fused with Reciprocal Rank Fusion |
| **Re-ranking** | cross-encoder-style precision re-rank over top-N candidates |
| **Freshness / staleness** | exponential-decay weighting + stale-source flagging |
| **Conflict resolution** | claim-level, freshest-source-wins, fully surfaced |
| **Runtime** | explicit bounded state machine (steps / tools / wall-clock budgets) |
| **Orchestration** | supervisor/worker fan-out with bounded handoffs |
| **Memory** | short-term session + selective long-term recall |
| **Guardrails** | pre-LLM PII redaction + injection detection; post-LLM groundedness |
| **Governance** | agent registry lifecycle, owner, staleness; RBAC; audit log |
| **Cost** | model-tier routing + semantic cache + per-request/tenant budgets |
| **Observability & eval** | span tracing + trajectory-level scoring (continuous + offline) |

## Run it

No dependencies, no API key — the reference backend is deterministic and
stdlib-only.

```bash
cd KnowledgeAgent
python examples/deploy_payment_service.py
```

## Test it

```bash
cd KnowledgeAgent
python -m unittest discover -s tests
```

(89 tests, `unittest`-based so they run without `pytest`; they are also
`pytest`-compatible if you have it: `pytest tests`.)

## Use it

```python
from datetime import datetime, timezone
from knowledge_agent import KnowledgeAssistant, Principal, build_corpus

asst = KnowledgeAssistant(build_corpus())
alice = Principal(user_id="alice", tenant_id="acme", roles=["engineer"])

answer = asst.ask("How do I deploy payment-service to production?", alice)
print(answer.text)
for c in answer.citations:
    print(c.marker, c.source.value, c.url, "STALE" if c.is_stale else "")
for cf in answer.conflicts:
    print("conflict:", cf.key, "→", cf.resolved_value, "from", cf.winning_source.value)
print("model:", answer.model_used, "cost $", answer.cost_usd,
      "grounded:", answer.groundedness, "trajectory:", answer.trajectory_score)
```

### Switching to real Claude models

The LLM layer is pluggable. Install `anthropic`, set `ANTHROPIC_API_KEY`, and
pass the production synthesizer (defaults to `claude-opus-5` with adaptive
thinking):

```python
from knowledge_agent.llm.client import AnthropicSynthesizer
asst = KnowledgeAssistant(build_corpus(), synthesizer=AnthropicSynthesizer())
```

Nothing in the control plane, orchestration, or retrieval changes — only the
synthesizer implementation behind the `Synthesizer` interface.

## Layout

```
knowledge_agent/
├── assistant.py            # composition root (control plane over data plane)
├── config.py               # budgets, model tiers, thresholds — one place
├── core.py                 # domain types (Document, Chunk, Answer, Conflict…)
├── control_plane/          # registry · rbac · audit · cost
├── data_plane/             # runtime · orchestrator · memory · guardrails · conflict
│   └── tools/              # manifests · registry (dynamic selection) · connectors
├── retrieval/              # chunking · embeddings · sparse · dense · hybrid · rerank · freshness
├── llm/                    # synthesizer(s) · model router · semantic cache
├── observability/          # tracing / spans
├── evaluation/             # trajectory + groundedness evaluator
└── seed/                   # cross-source demo corpus
docs/                       # architecture + flow diagrams + principle mapping
examples/                   # runnable end-to-end demo
tests/                      # 89 unittest cases
```
