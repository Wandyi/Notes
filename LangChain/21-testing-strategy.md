# 21 — Testing Strategy

## 1. Concepts

Agent systems have a testing pyramid with an extra, unusual layer:

```
        ┌───────────────────────────────┐
        │  Online evals (production)    │  sampled judges, user feedback
        ├───────────────────────────────┤
        │  Offline evals (datasets)     │  non-deterministic, scored, gated on aggregates
        ├───────────────────────────────┤
        │  Integration tests            │  real graph, stubbed models/tools, deterministic
        ├───────────────────────────────┤
        │  Unit tests                   │  nodes, tools, reducers, middleware — fully deterministic
        └───────────────────────────────┘
```

The rule that keeps this tractable: **everything below the eval line must be deterministic.** If a
test can fail because a model felt different today, it is an eval, not a test, and it belongs in the
scored suite with a threshold — not in the pass/fail gate on every commit.

### What to test where

| Layer | Test | Determinism |
|---|---|---|
| Reducers | Associativity, commutativity, bounds | Pure |
| Nodes | Given state → expected update; error paths | Pure (stub I/O) |
| Tools | Argument validation, clamping, error strings, idempotency | Pure/fake backends |
| Middleware | Hook order, jumps, state updates, short-circuit behaviour | Pure |
| Graph topology | Reachability, no orphan nodes, expected edges | Pure |
| Graph execution | Routing on canned model outputs, interrupts, resume, retries | Deterministic via fake model |
| Prompts | Snapshot of the rendered prompt | Pure |
| End-to-end quality | Trajectory + response evals on a dataset | Scored |

## 2. How to implement

### Deterministic model stubs

```python
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

def scripted_model(*responses):
    return GenericFakeChatModel(messages=iter(responses))

model = scripted_model(
    AIMessage(content="", tool_calls=[{"id": "1", "name": "get_weather",
                                       "args": {"city": "SF"}}]),
    AIMessage(content="It's sunny in SF."),
)
agent = create_agent(model, tools=[get_weather])
```

Now routing, tool execution, middleware and interrupts are all testable with zero API calls and zero
flakiness.

For a middle ground, `LLMToolEmulatorMiddleware` emulates tool execution with an LLM so you can test
agent behaviour without hitting real side effects.

### Testing a node

```python
def test_triage_routes_billing():
    state = {"messages": [HumanMessage("I was charged twice")], "stage": "triage"}
    out = triage(state)
    assert out.goto == "billing"
    assert out.update["label"] == "billing"
```

### Testing reducers (the ones people skip)

```python
import operator
from hypothesis import given, strategies as st

@given(st.lists(st.integers()), st.lists(st.integers()), st.lists(st.integers()))
def test_reducer_is_associative(a, b, c):
    assert list_reducer(list_reducer(a, [b]), [c]) == list_reducer(a, [b, c])
```

Critical if you use `DeltaChannel`, where non-associativity produces state that changes on replay
([03](03-state-channels-and-reducers.md)).

### Testing interrupts and resume

```python
def test_approval_flow():
    cfg = {"configurable": {"thread_id": "t1"}}
    result = graph.invoke({"amount": 5000}, cfg, version="v2")
    assert result.interrupts
    assert result.interrupts[0].value["action"] == "issue_refund"

    result = graph.invoke(Command(resume={"approved": False, "reason": "fraud"}),
                          cfg, version="v2")
    assert result.value["status"] == "cancelled"
```

Also test **resume idempotency**: run the pre-interrupt node twice and assert the side effect
happened once.

### Testing middleware order

```python
def test_pii_runs_before_everything():
    names = [type(m).__name__ for m in agent_middleware_stack()]
    assert names.index("PIIMiddleware") < names.index("SummarizationMiddleware")
```

Cheap, and it prevents a whole class of compliance regressions.

### Snapshot-testing prompts

```python
def test_system_prompt_snapshot(snapshot):
    request = build_model_request(state=FIXTURE_STATE, runtime=FIXTURE_RUNTIME)
    assert request.system_prompt == snapshot
```

Prompt drift caused by a middleware change is otherwise invisible until quality drops.

### Integration tests with real infrastructure

Use a throwaway Postgres (testcontainers) and `AsyncPostgresSaver` to test:

- checkpoint write/read round-trips with your actual state schema (serialization bugs!)
- resume after a simulated crash
- concurrent runs on different threads
- TTL/reaper behaviour

### CI pipeline shape

```yaml
on: [pull_request]
jobs:
  fast:            # < 2 min, blocking
    - unit tests (nodes, tools, reducers, middleware)
    - topology assertions
    - prompt snapshots
    - lint: no blocking I/O in async nodes, no InMemorySaver outside tests
  integration:     # < 10 min, blocking
    - graph execution with scripted models
    - postgres checkpointer round-trip + resume
  evals:           # nightly + on demand, blocking for prompt/model changes
    - trajectory + response evals on the golden dataset
    - compare against the baseline experiment; fail on regression beyond threshold
    - report cost and latency deltas
```

Gate model/prompt/middleware changes on the eval job; gate everything else on the fast jobs.

## 3. Scenarios

| Scenario | Test design |
|---|---|
| Upgrading LangGraph 1.2 → 1.3 | Full integration suite against a real Postgres + replay of archived checkpoints (deserialization compatibility) |
| Changing a state channel | Property test the reducer; load old checkpoints in a test; assert tolerant deserialization |
| Adding a destructive tool | Unit test argument validation and clamping; test HITL interrupt is required; test idempotency key |
| Prompt change | Snapshot diff + eval suite; canary via assistant version |
| New subagent | Its own unit + eval suite; contract test on the delegation payload |
| Provider outage drill | Force the primary model to raise; assert `ModelFallbackMiddleware` engages and the run completes |

## 4. Staff-level considerations

- **Fake the model, not the graph.** The most common mistake is mocking `create_agent` itself, which
  tests nothing. Script the model's outputs and let the real graph run.
- **Fixtures are your real asset.** A corpus of realistic states, message histories and tool
  responses is reusable across unit tests, integration tests and evals. Invest in it once.
- **Test the failure paths harder than the happy path.** Retry exhaustion, timeouts, error handlers,
  compensation, drain/resume, double-texting. These are what page you at 3 a.m.
- **Every incident adds a test.** Non-negotiable. Otherwise the same class of failure recurs because
  the system is non-deterministic and "we fixed the prompt" is unverifiable.
- **Serialization tests catch upgrade breakage.** Store a few real checkpoint blobs as fixtures and
  assert they still deserialize after every dependency bump.
- **Budget eval cost.** A 200-example suite with a judge is real money per run. Run the full suite
  nightly and a fast subset per PR.
- **Test with the assistant configs you actually ship**, not defaults — config drift is a common
  source of "works in CI, fails in prod".

## 5. Anti-patterns

- Tests that call real LLMs on every PR (slow, flaky, expensive).
- Asserting exact model text (`assert response == "..."`) instead of properties/structure.
- Mocking so deeply that the graph never executes.
- No tests for interrupt/resume — the highest-risk, least-tested path in most codebases.
- Evals that produce a score nobody blocks on.
- `InMemorySaver` in every test, so serialization bugs surface only in production.
- Testing only the supervisor in a multi-agent system.

## 6. Design-review questions

1. Can the whole suite run without network access? What percentage requires a real model?
2. Is there a test for resume-after-interrupt idempotency?
3. Do we test against a real Postgres checkpointer with our real state schema?
4. Which changes are gated on evals, and what is the regression threshold?
5. When we upgrade LangGraph, what proves old checkpoints still load?
6. Does every past incident have a corresponding test or eval case?

## References

- `/oss/python/langchain/test/index`, `/unit-testing`, `/integration-testing`, `/evals`
- `/oss/python/langgraph/test`
- `/langsmith/pytest`, `/langsmith/cicd-pipeline-example`
- `/oss/python/langchain/middleware/built-in#llm-tool-emulator`
