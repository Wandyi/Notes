# 20 — Observability & Evaluation

> A non-deterministic system without traces and evals is unfalsifiable. You cannot debug it, you
> cannot safely change it, and you cannot tell whether last week's prompt tweak helped.

## 1. Concepts

### Two loops

- **Observability loop** (production): traces → metrics → alerts → incidents. "What happened?"
- **Evaluation loop** (pre-production + online): datasets → experiments → scores → regression gates.
  "Is it getting better?"

They connect: production traces become eval datasets; eval failures become production guards.

### Tracing model

A **trace** is one run; **spans** are nested steps (nodes, model calls, tool calls, subgraphs, retriever
calls). LangSmith auto-instruments LangChain/LangGraph. Key knobs:

| Knob | Purpose |
|---|---|
| `LANGSMITH_TRACING=true` | Enable tracing |
| `LANGSMITH_PROJECT` | Route to a project (per env/service) |
| `metadata` / `tags` in config | Filter, group, and attribute cost |
| Anonymizers | Redact sensitive values before they leave the process |
| Conditional tracing / sampling | Cost control at high volume |
| Distributed tracing | Correlate agent traces with your existing APM |

### Evaluation types

| Type | What it scores | Tooling |
|---|---|---|
| **Final response** | Correctness/quality of the answer | LLM-as-judge, exact/fuzzy match, structured assertions |
| **Trajectory** | The path taken (which tools, in what order) | `agentevals` trajectory match: `strict`, `unordered`, `subset`, `superset` |
| **Single step** | One node/tool in isolation | Unit tests, code evaluators |
| **Retrieval** | Recall/precision of retrieved context | Recall@k, groundedness/citation checks |
| **Online** | Live production quality | Sampled LLM-as-judge on real traces, user feedback |

Trajectory modes matter:

- `strict` — same structure and tool calls **in the same order** (content may differ). Use to enforce
  "policy lookup before authorisation".
- `unordered` — same set of tool calls, any order.
- `subset` — agent used only tools from the reference (no extras) → scope containment.
- `superset` — agent used at least the reference tools → minimum required actions.

## 2. How to implement

### Tracing with useful metadata

```python
config = {
    "configurable": {"thread_id": thread_id},
    "tags": ["prod", "support-agent", f"tenant:{tenant_id}"],
    "metadata": {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "assistant_version": assistant_version,
        "release": GIT_SHA,
        "run_class": "interactive",
    },
}
graph.invoke(inputs, config)
```

Make this a helper in one place. Consistent metadata is what turns LangSmith from a log viewer into
an analytics tool: cost per tenant, p95 per release, error rate per assistant version.

### Redacting sensitive data from traces

```python
from langsmith import Client
from langsmith.anonymizer import create_anonymizer
from langchain_core.tracers.langchain import LangChainTracer

anonymizer = create_anonymizer([
    {"pattern": r"\b\d{3}-?\d{2}-?\d{4}\b", "replace": "<ssn>"},
    {"pattern": r"[\w.+-]+@[\w-]+\.[\w.]+", "replace": "<email>"},
])
tracer = LangChainTracer(client=Client(anonymizer=anonymizer))

graph = builder.compile().with_config({"callbacks": [tracer]})
```

Redaction happens client-side, before data leaves your process — pair with `PIIMiddleware`, which
redacts what the *model* sees ([22](22-security-guardrails-multitenancy.md)).

### Trajectory evaluation

```python
from agentevals.trajectory.match import create_trajectory_match_evaluator

evaluator = create_trajectory_match_evaluator(trajectory_match_mode="subset")
result = agent.invoke({"messages": [HumanMessage("What's the weather in SF?")]})
evaluation = evaluator(outputs=result["messages"], reference_outputs=reference_trajectory)
assert evaluation["score"] is True
```

### LLM-as-judge on trajectories

```python
from agentevals.trajectory.llm import (
    create_trajectory_llm_as_judge, TRAJECTORY_ACCURACY_PROMPT,
    TRAJECTORY_ACCURACY_PROMPT_WITH_REFERENCE,
)

judge = create_trajectory_llm_as_judge(model="openai:o3-mini",
                                       prompt=TRAJECTORY_ACCURACY_PROMPT)
assert judge(outputs=result["messages"])["score"] is True
```

Async variants: `create_async_trajectory_llm_as_judge`, `create_async_trajectory_match_evaluator`.

### Running evals in LangSmith

```python
import pytest
from langsmith import testing as t

@pytest.mark.langsmith
def test_trajectory_accuracy():
    result = agent.invoke({"messages": [HumanMessage("What's the weather in SF?")]})
    t.log_inputs({})
    t.log_outputs({"messages": result["messages"]})
    t.log_reference_outputs({"messages": reference_trajectory})
    assert judge(outputs=result["messages"])["score"] is True
```

Or the dataset-driven `evaluate()` API for larger suites, with experiment comparison in the UI.

### The metrics that matter

| Layer | Metric | Alert on |
|---|---|---|
| Platform | Queue depth, pending-run age, worker CPU/mem, Postgres CPU/IOPS, Redis memory | Pending age > SLO |
| Run | p50/p95/p99 duration, success rate, error rate by node, recursion-limit hits | p95 regression, error-rate step change |
| Model | Tokens in/out per run, cost per run, 429 rate, provider latency, fallback rate | Cost/run drift, 429 spike |
| Agent quality | Tool-call count per run, loop count, HITL rejection rate, online judge score | Judge score drop, rejection-rate rise |
| Business | Task completion rate, escalation rate, user feedback | Anything moving with a deploy |

**Cost per successful task** is the single best composite metric. Track it per assistant version.

## 3. Scenarios

| Scenario | Approach |
|---|---|
| "Quality dropped after Tuesday's deploy" | Compare experiments across releases; filter traces by `metadata.release`; diff trajectories |
| New prompt proposal | Run the offline suite; require no regression on the golden set + judge score ≥ baseline before merge ([21](21-testing-strategy.md)) |
| Model upgrade (provider bump) | Same suite, new model; compare cost, latency and quality; canary via assistant version |
| Cost investigation | Group by `metadata.tenant_id` / `agent_name`; find the subagent burning tokens |
| Regulated workload | Anonymizers + conditional tracing + retention policy + audit of who reads traces |
| Continuous quality | Sample 1–5% of production traces into an online LLM-judge; alert on score drift |

## 4. Staff-level considerations

- **Build the dataset before the agent.** 30–50 labelled examples covering the real distribution
  (including the ugly cases) is worth more than a month of prompt tuning. Grow it from production
  failures — every incident should add a test case.
- **Evaluate trajectories, not just answers.** An agent that gets the right answer via 14 tool calls
  and a policy violation is not correct. `subset` mode is your scope-containment guard.
- **Judges need their own validation.** An LLM judge is a model with its own error rate. Measure
  agreement with human labels on a sample before trusting it as a gate.
- **Sampling strategy at scale.** Full tracing of 500 rps is expensive. Sample by rule: 100% of
  errors, 100% of HITL rejections, 100% of a canary tenant, 1–5% of the rest.
- **Traces contain everything users said.** They are a PII store with a UI. Apply retention,
  anonymisation and access control, and include them in your DPIA/data map.
- **Instrument the seams**: tool latency, retrieval recall, and context size per call are the three
  measurements that explain most quality problems, and none are visible by default.
- **Wire evals into CI as a gate, not a report.** A suite nobody blocks on decays within a quarter.

## 5. Anti-patterns

- Tracing enabled in dev only.
- No metadata → traces you cannot slice by tenant, release or run class.
- Evals as a one-off notebook.
- Only end-to-end evals: when they fail you learn nothing about *where*.
- LLM judge with a vague prompt ("is this good?") and no human calibration.
- Alerting on averages (agent latency is long-tailed; watch p95/p99).
- Sending raw PII to a trace backend without redaction or a retention policy.

## 6. Design-review questions

1. What metadata is on every trace, and can we compute cost per tenant and per release from it?
2. What is in the golden dataset, how big is it, and how does it grow?
3. Which evals gate a merge? Which gate a production promotion?
4. Have we measured judge–human agreement?
5. What is the tracing sampling policy at target volume, and what does it cost?
6. What is the trace retention period, and who can read traces?
7. What are the five alerts that would catch a bad deploy within 10 minutes?

## References

- `/oss/python/langchain/observability`, `/oss/python/langgraph/observability`
- `/oss/python/langchain/test/evals`, `/langsmith/evaluation-concepts`
- `/langsmith/observability-concepts`, `/langsmith/add-metadata-tags`, `/langsmith/mask-inputs-outputs`
- `/langsmith/cost-tracking`, `/langsmith/alerts`, `/langsmith/agent-server-distributed-tracing`
- `/langsmith/pytest`, `/langsmith/annotation-queues`
