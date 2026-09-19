# 09 — Context Engineering

> "Agents fail because of context, not intelligence." Most production agent bugs are context bugs:
> the model was given the wrong information, too much information, or the wrong tools.

## 1. Concepts

### The agent loop and what you control

```
   ┌──────────────► model call ──────────────┐
   │        (system prompt, messages,        │
   │         tools, response format)         │
   │                                         ▼
 state/store/files  ◄──── tool execution ◄───┘
```

At every model call you control four things — this is the entire surface:

1. **System prompt** — instructions, persona, policy, dynamically assembled context
2. **Messages** — the conversation history you choose to send
3. **Tools** — which tools are visible for *this* call
4. **Response format** — free text or a structured schema

Everything else (memory, RAG, subagents, skills, compaction) is a strategy for filling those four
slots well.

### Three kinds of context

| Kind | Lives in | Examples |
|---|---|---|
| **Model context** | The request to the LLM | System prompt, messages, tool schemas, output schema |
| **Tool context** | What tools read and write | State, store, files, external APIs |
| **Life-cycle context** | Transformations between steps | Summarisation, trimming, PII redaction, context editing |

### The four failure modes of long context

| Failure | Symptom | Mitigation |
|---|---|---|
| **Poisoning** | A hallucination enters context and is treated as fact forever | Provenance, validation nodes, compaction that drops unverified claims |
| **Distraction** | Model over-focuses on history instead of the task | Trim, summarise, re-state the goal near the end |
| **Confusion** | Irrelevant tools/content change behaviour | Dynamic tool selection, fewer tools per call |
| **Clash** | Contradictory information in context | Dedup memory, conflict resolution policy, single source of truth per fact |

### The four strategies

1. **Write** — offload out of the context window (files, store, state).
2. **Select** — retrieve only what's relevant (RAG, memory search, tool selection).
3. **Compress** — summarise, trim, edit older tool results.
4. **Isolate** — put work in a subagent/subgraph with its own window; return only the result.

## 2. How to implement

### Dynamic system prompt (middleware)

```python
from langchain.agents.middleware import wrap_model_call, ModelRequest, ModelResponse

@wrap_model_call
def inject_context(request: ModelRequest, handler) -> ModelResponse:
    rt = request.runtime
    profile = rt.store.get((rt.context.user_id, "profile"), "main")
    request = request.override(
        system_prompt=(
            f"{BASE_PROMPT}\n\n"
            f"<user_profile>{profile.value if profile else '{}'}</user_profile>\n"
            f"<today>{rt.context.today}</today>"
        )
    )
    return handler(request)
```

Put volatile content (dates, retrieved snippets, user profile) in delimited blocks so it is easy to
strip, cache-bust deliberately, and audit.

### Message management

```python
from langchain.agents.middleware import SummarizationMiddleware, ContextEditingMiddleware

agent = create_agent(
    model="claude-sonnet-4-6",
    tools=tools,
    middleware=[
        SummarizationMiddleware(max_tokens_before_summary=60_000, messages_to_keep=20),
        ContextEditingMiddleware(),   # prunes/clears stale tool results
    ],
)
```

Manual trimming in a raw graph:

```python
from langchain_core.messages.utils import trim_messages, count_tokens_approximately

def prepare(state):
    return {"messages": trim_messages(
        state["messages"],
        max_tokens=40_000,
        token_counter=count_tokens_approximately,
        strategy="last",
        start_on="human",
        include_system=True,
    )}
```

`start_on="human"` and keeping tool-call/tool-result pairs together matter: orphaned `ToolMessage`s
cause provider errors.

### Tool selection

```python
from langchain.agents.middleware import LLMToolSelectorMiddleware

agent = create_agent(model=..., tools=all_50_tools,
                     middleware=[LLMToolSelectorMiddleware(max_tools=8)])
```

Or deterministically, based on state:

```python
@wrap_model_call
def scope_tools(request: ModelRequest, handler) -> ModelResponse:
    stage = request.state["stage"]
    return handler(request.override(tools=TOOLS_BY_STAGE[stage]))
```

Deterministic scoping is cheaper and more predictable than an LLM selector; use the selector only
when the tool space is genuinely open-ended.

### Context offloading to files

The Deep Agents filesystem (or your own equivalent) lets the agent write large intermediates to
files and keep only a path in context:

```
research_notes.md   ← 40k tokens of raw findings
context:            "Findings saved to research_notes.md (12 sections)"
```

The agent re-reads selectively. This is the single most effective technique for long-horizon tasks.
See [24](24-deep-agents.md).

### Isolation with subagents

```python
@tool
def deep_research(question: str) -> str:
    """Run an isolated research pass and return only the synthesis."""
    result = research_agent.invoke({"messages": [{"role": "user", "content": question}]})
    return result["messages"][-1].content     # 200 tokens returned, 80k consumed internally
```

The parent's window never sees the intermediate tool churn.

### Tool context: reads and writes

Tools can read state, store and runtime context via injection, and write back via `Command`:

```python
from langchain.tools import tool, InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

@tool
def record_finding(finding: str,
                   tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Record a finding without dumping it into the conversation."""
    return Command(update={
        "findings": [finding],
        "messages": [ToolMessage("recorded", tool_call_id=tool_call_id)],
    })
```

The model sees `"recorded"`; the data lives in state. This "write to state, acknowledge in context"
pattern is how you keep large tool outputs out of the window.

## 3. Scenarios

| Scenario | Strategy stack |
|---|---|
| 4-hour research agent | Isolate (subagents) + Write (files) + Compress (summarisation) |
| Support agent with 60 tools | Select (stage-scoped tool sets) + Isolate (specialist subagents) |
| Coding agent on a large repo | Write (files) + Select (grep/search tools instead of dumping files) + Compress (context editing on old tool results) |
| Compliance assistant | Select (strict RAG with citations) + no memory writes without review + provenance in every prompt block |
| Chat with 500-turn history | Compress (rolling summary + last-K) + Write (transcript in store) + `DeltaChannel` for checkpoint size |

## 4. Staff-level considerations

- **Token budget is a design constraint you write down.** Example budget for a 200k window:
  system 4k, tools 3k, retrieved 20k, history 40k, headroom 20k, output 8k. Enforce it with
  middleware, alert when p95 approaches it.
- **Every context decision is a latency and cost decision.** Time-to-first-token scales with prompt
  size; cost scales with total tokens. Halving context often improves quality *and* p95 *and* spend.
- **Prompt caching changes the calculus.** Stable prefixes (system prompt + tool schemas) can be
  cached by the provider; volatile content must go at the **end**. Ordering your prompt for cache
  hits is a real, measurable win ([23](23-cost-and-token-economics.md)).
- **Context engineering is testable.** Snapshot the exact rendered prompt in unit tests. Prompt
  drift caused by a middleware change is otherwise invisible until quality regresses.
- **Isolation has a cost**: a subagent cannot see the parent's context, so ambiguous delegation
  produces confidently wrong answers. Write explicit, self-contained task descriptions for subagents.
- **Compaction is lossy and irreversible in-thread.** Persist the pre-compaction transcript
  (store/warehouse) before overwriting, both for debugging and for compliance.

## 5. Anti-patterns

- Dumping full tool outputs (API JSON, file contents, SQL result sets) into messages.
- One system prompt that grows to 6,000 tokens of accumulated edge-case instructions.
- Giving one agent 40 tools and hoping the model picks correctly.
- Summarising with the same expensive model you use for reasoning.
- Putting volatile values (timestamps, request ids) at the top of the prompt, destroying cache hits.
- Retrieval that always returns k=10 chunks regardless of relevance score.
- Treating the context window size as the budget. Quality degrades well before the limit.

## 6. Design-review questions

1. What is the token budget per model call, by section? How is it enforced and monitored?
2. What is the largest thing a tool can return, and what stops it from entering the window?
3. Which parts of the prompt are stable enough to be cached, and are they at the front?
4. When we compact, what is lost, and where is the original kept?
5. How many tools are visible per call at p95? Can we scope them by stage?
6. Do we have a test that asserts on the rendered prompt?

## References

- `/oss/python/langchain/context-engineering`
- `/oss/python/deepagents/context-engineering`
- `/oss/python/concepts/context`
- `/oss/python/langchain/middleware/built-in` (summarization, context editing, tool selector)
