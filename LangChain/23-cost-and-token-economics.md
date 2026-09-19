# 23 — Cost & Token Economics

> The metric that matters is **cost per successfully completed task**, not cost per token. An agent
> that costs 3× per call but halves the number of retries is cheaper.

## 1. Concepts

### Where the money goes

```
cost_per_run ≈ Σ over model calls of ( input_tokens × in_rate + output_tokens × out_rate )
             + infra (workers, Postgres, Redis, tracing)
```

Input tokens usually dominate, because context is resent on **every** call in the loop. An agent
with 8 model calls and a 30k-token context pays for ~240k input tokens per run — even though the
"conversation" is short.

### The four multipliers

| Multiplier | Driver | Lever |
|---|---|---|
| **Loop count** | How many model calls per run | Better prompts/tools, fewer retries, call limits |
| **Context size** | Tokens per call | Trimming, summarisation, offloading, tool-output shaping |
| **Model tier** | Price per token | Routing, fallbacks, cheap models for cheap steps |
| **Cache miss rate** | Repeated prefixes not cached | Prompt ordering, stable prefixes, provider caching |

Attack them in that order — loop count is usually the biggest and the most neglected.

## 2. How to implement

### Prompt caching (the highest-ROI change)

Providers cache stable prompt prefixes. Structure the prompt so the stable part comes first:

```
[ system prompt        ]  stable  ─┐
[ tool schemas         ]  stable   ├─ cacheable prefix
[ long policy document ]  stable  ─┘
[ conversation history ]  grows
[ retrieved context    ]  volatile
[ current user turn    ]  volatile
```

Never put a timestamp, request id or random value at the top — it invalidates the whole prefix.

```python
from langchain.agents import create_agent
# Anthropic prompt-caching middleware (see /oss/python/integrations/middleware/)
agent = create_agent(model="claude-sonnet-4-6", tools=tools,
                     middleware=[AnthropicPromptCachingMiddleware()])
```

### Model routing

```python
from langchain.agents.middleware import wrap_model_call

@wrap_model_call
def route_by_difficulty(request, handler):
    if is_simple(request.state):          # classifier or heuristic
        return handler(request.override(model="claude-haiku-4-5"))
    return handler(request)
```

Use the cheap model for: classification, routing, summarisation, extraction, tool-argument
formatting, judges. Reserve the frontier model for the reasoning that actually needs it. This is
routinely a 40–70% saving with no quality loss on the cheap paths.

### Hard caps

```python
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

middleware = [
    ModelCallLimitMiddleware(thread_limit=60, run_limit=15),
    ToolCallLimitMiddleware(thread_limit=40, run_limit=20),
    ToolCallLimitMiddleware(tool_name="web_search", run_limit=5),
]
```

Caps are cost *insurance*, not cost *optimisation* — but they're what stops a single pathological
run from costing $200.

### Budget guard in state

```python
class State(AgentState):
    cost_usd: Annotated[float, operator.add]

@after_model
@hook_config(can_jump_to=["end"])
def enforce_budget(state, runtime):
    if state.get("cost_usd", 0) > runtime.context.budget_usd:
        return {"messages": [AIMessage("Budget for this task is exhausted.")],
                "jump_to": "end"}
```

Accumulate `cost_usd` from `usage_metadata` in a `wrap_model_call`.

### Context compression

```python
SummarizationMiddleware(max_tokens_before_summary=60_000, messages_to_keep=20)
ContextEditingMiddleware()        # prunes/clears stale tool results
```

Use a cheap model for the summariser. Offload the full transcript to the store/files
([09](09-context-engineering.md)).

### Node and result caching

```python
builder.add_node("embed_corpus", embed_corpus, cache_policy=CachePolicy(ttl=3600))
graph = builder.compile(cache=InMemoryCache())
```

Plus application-level caches: retrieval results per query hash, tool responses for idempotent reads,
classification results per input hash.

### Infra cost levers

| Lever | Effect |
|---|---|
| `durability="exit"` where safe | Fewer checkpoint writes → smaller Postgres |
| `DeltaChannel` on append-heavy channels | Linear→sublinear checkpoint storage growth |
| TTLs on threads and store items | Bounded storage |
| Trace sampling | Bounded observability spend |
| Right-sized `N_JOBS_PER_WORKER` | Fewer worker replicas for the same throughput |
| Batch/async work on cheaper spot capacity | Compute cost |

## 3. Scenarios

| Scenario | Cost strategy |
|---|---|
| Support chat, 50k conversations/month | Prompt caching + routing (haiku for triage) + summarisation at 60k tokens + per-run call caps |
| Deep research agent, $/report matters | Subagent isolation (parallel + isolated context), file offloading, cheap model for sub-summaries, hard model-call cap |
| Batch classification, 1M rows | No agent loop at all — structured output, one call per row, batch API if available, `durability="exit"` |
| Multi-tenant platform | Per-tenant budgets + attribution via trace metadata; expose spend in the tenant admin UI |
| Cost spike investigation | Group traces by `metadata.agent_name`/`tenant_id`; look at tokens/run trend, cache-hit rate, loop count |

## 4. Staff-level considerations

- **Instrument cost per run from day one.** `usage_metadata` → state channel → trace metadata →
  dashboard. Retrofitting attribution after the bill arrives is painful.
- **Track cache-hit rate as a first-class metric.** A middleware change that reorders the prompt can
  silently destroy caching and double your bill with no other symptom.
- **Loop count is a quality metric and a cost metric.** Rising average tool calls per run usually
  means the agent is confused — cheaper prompts and better tool descriptions fix both.
- **Set budgets per run class**, enforce them in middleware, and make exceeding them a visible event
  rather than an invisible charge.
- **Model your unit economics before scaling.** cost/run × runs/month vs. revenue or savings per
  task. Many agent projects are technically successful and economically unviable; find out at
  prototype stage.
- **Beware the eval bill.** A 200-example suite with an LLM judge, run per PR, is a real line item.
  Fast subset per PR, full suite nightly.
- **Negotiate on measured volume.** Provider discounts and batch/priority tiers matter at scale; you
  need tokens-per-month telemetry to negotiate at all.

## 5. Anti-patterns

- Volatile content (timestamps, request ids) at the top of the prompt → zero cache hits.
- Frontier model for classification and summarisation.
- No cap on tool calls or model calls — one pathological run can cost hundreds.
- Retries at three layers multiplying token spend on failures.
- Dumping full tool outputs into context ("the model might need it").
- Optimising infra costs while ignoring a 10× token inefficiency.
- Measuring cost per call instead of cost per completed task.

## 6. Design-review questions

1. What is the measured cost per run at p50 and p95, and what is the target?
2. What is the prompt cache-hit rate, and what would break it?
3. Which steps use a cheaper model? Which must use the frontier model, and why?
4. What are the hard caps, and what does the user see when they trip?
5. How is cost attributed per tenant/feature/assistant version?
6. What do the unit economics look like at 10× current volume?

## References

- `/oss/python/langchain/middleware/built-in` (model call limit, tool call limit, model fallback, summarization, context editing)
- `/oss/python/integrations/middleware/index` (provider prompt caching)
- `/langsmith/cost-tracking`, `/langsmith/caching`
- `/oss/python/langgraph/graph-api#node-caching`
- `/oss/python/langchain/context-engineering`
