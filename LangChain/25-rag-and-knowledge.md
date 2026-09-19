# 25 — RAG & Knowledge Architecture

## 1. Concepts

### Three levels of retrieval in an agent

| Level | Shape | When |
|---|---|---|
| **Static RAG** | Retrieve once → stuff into the prompt → answer | Single-hop factual Q&A; lowest latency and cost |
| **Agentic RAG** | Retrieval is a **tool**; the model decides when and what to search, and can iterate | Multi-hop questions, ambiguous queries, mixed sources |
| **Deep research** | Subagents each run their own retrieval loop; results synthesised | Long reports, many sources, needs context isolation |

Most production systems should start static and add agency only where measurement shows it helps.
Agentic retrieval typically costs 3–6× more calls.

### The retrieval quality chain

```
corpus → chunking → embedding → index → query transform → retrieve → filter → rerank → context assembly → generation → citation
```

Failures at any stage look identical from the outside ("the answer was wrong"). Instrument each
stage separately or you will tune the wrong one. In practice the biggest wins are usually **chunking**
and **reranking**, not the LLM.

### Knowledge vs memory

- **Knowledge** (this file): shared, curated corpora — docs, tickets, code, policies. Read-mostly,
  versioned, evaluated for recall.
- **Memory** ([08](08-stores-and-long-term-memory.md)): per-user/per-org facts learned from
  interaction. Write-heavy, needs conflict resolution and deletion.

Keep them in different systems with different lifecycles. Conflating them is why "the agent
remembered something from the docs that was actually another user's data" happens.

## 2. How to implement

### Retrieval as a tool (agentic RAG)

```python
from langchain.tools import tool
from langgraph.runtime import get_runtime

@tool
def search_knowledge(query: str, top_k: int = 5) -> str:
    """Search the product documentation. Use precise, keyword-rich queries."""
    rt = get_runtime()
    docs = vectorstore.similarity_search(
        query,
        k=min(top_k, 10),
        filter={"tenant_id": rt.context.tenant_id},   # filter AT the store, not after
    )
    return "\n\n".join(
        f"[{d.metadata['doc_id']}#{d.metadata['chunk']}] {d.page_content}" for d in docs
    )
```

Two non-negotiables: **tenant filtering at query time**, and **citable ids in the output** so the
model can attribute and you can verify.

### Grade-and-retry loop (the classic agentic RAG graph)

```python
def route_after_grade(state) -> Literal["generate", "rewrite", "give_up"]:
    if state["relevance"] >= 0.7:
        return "generate"
    return "rewrite" if state["attempts"] < 2 else "give_up"

builder.add_node("retrieve", retrieve)
builder.add_node("grade", grade_documents)         # cheap model
builder.add_node("rewrite", rewrite_query)
builder.add_node("generate", generate_answer)
builder.add_node("give_up", say_not_found)
builder.add_conditional_edges("grade", route_after_grade)
builder.add_edge("rewrite", "retrieve")
```

Bound the loop (`attempts`) and always provide an honest "I couldn't find it" terminal state — an
agent that never says "not found" will hallucinate instead.

### Parallel multi-source retrieval

```python
def fan_out(state):
    return [Send("retrieve_source", {"query": state["query"], "source": s})
            for s in ["docs", "tickets", "code"]]

class State(TypedDict):
    hits: Annotated[list[Hit], operator.add]     # reducer required for fan-in
```

Then dedupe and rerank in a `defer=True` aggregation node.

### Keeping retrieved content out of the window

```python
@tool
def fetch_document(doc_id: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Fetch a full document into working memory."""
    text = corpus.get(doc_id)
    return Command(update={
        "documents": [{"id": doc_id, "text": text}],
        "messages": [ToolMessage(f"Loaded {doc_id} ({len(text)} chars). "
                                 f"Use `query_document` to ask about it.",
                                 tool_call_id=tool_call_id)],
    })
```

For very large corpora, write to the Deep Agents filesystem and let the agent grep
([24](24-deep-agents.md)).

### Hybrid search and reranking

Dense-only retrieval misses exact identifiers (error codes, SKUs, function names). Combine BM25 +
dense with reciprocal-rank fusion, then rerank the top ~50 to the top ~5 with a cross-encoder. This
is usually the single largest quality improvement available.

### Ingestion pipeline (build it as a LangGraph workflow)

```
load → detect changes (content hash) → chunk → enrich (title, section path, metadata)
     → embed (batch) → upsert → verify counts → publish index version
```

Run it as a durable graph with retries and idempotency keys. Version the index so a bad ingest can
be rolled back without re-embedding.

## 3. Scenarios

| Scenario | Architecture |
|---|---|
| Product-docs assistant | Static RAG + hybrid + rerank + citations; agentic only for follow-ups |
| Support agent over tickets + docs + code | Agentic RAG with three source-specific tools; grade-and-retry; parallel fan-out for "compare" questions |
| Compliance Q&A | Strict retrieval, mandatory citations, refuse when confidence is low, human review queue, full audit of retrieved chunks |
| Multi-tenant knowledge | Per-tenant filters enforced server-side; separate namespaces/collections for high-isolation tenants |
| Code assistant on a large repo | Grep/glob tools + file reads over embeddings; embeddings only for natural-language docs |
| Deep research report | Subagents per subtopic, each with its own retrieval loop; synthesis with citation preservation |

## 4. Staff-level considerations

- **Retrieval is the most common root cause of "the agent is dumb".** Before touching prompts,
  measure recall@k on a labelled query set. If the right chunk isn't retrieved, no prompt saves you.
- **Chunking is a product decision.** Chunk on semantic boundaries (headings, functions, clauses),
  keep a parent-document pointer, and include the section path in the chunk text so the model knows
  where it came from.
- **Freshness has an SLA.** "How stale can knowledge be?" determines your ingestion architecture
  (batch nightly vs CDC/event-driven). Write it down; it drives cost.
- **Access control must be enforced in the query, not the prompt.** Post-filtering leaks through
  result counts, ranking and error messages.
- **Citations are a safety feature, not a UI feature.** They make hallucination detectable, enable
  automated groundedness checks, and give users a verification path.
- **Evaluate retrieval separately from generation**: recall@k, MRR, groundedness (is every claim
  supported by a retrieved chunk?), citation accuracy ([20](20-observability-and-evaluation.md)).
- **Index versioning and rollback**: treat the index like a database schema. Blue/green index
  versions let you roll back a bad chunking change in minutes.
- **The store is not a vector database.** LangGraph's store supports semantic search and is right for
  agent memory; for a large curated corpus with hybrid search, filtering and reranking, use a real
  vector store behind a tool.

## 5. Anti-patterns

- Always retrieving `k=10` regardless of relevance score.
- Dense-only retrieval on a corpus full of identifiers and error codes.
- Embedding whole documents instead of chunks (or 200-token chunks with no context).
- Post-filtering by tenant after retrieval.
- No "not found" path — the agent always answers.
- Re-embedding the entire corpus on every ingest run.
- Putting retrieved documents into state permanently instead of transiently.
- Evaluating only end-to-end answers, so you never learn whether retrieval or generation failed.

## 6. Design-review questions

1. What is recall@k on our labelled query set today? When did we last measure it?
2. Is hybrid search and reranking in place? What did they buy us?
3. How is tenant/permission filtering enforced, and at which layer?
4. What is the freshness SLA, and what is the ingestion architecture that meets it?
5. Can we roll back a bad index build? How long does that take?
6. Does the agent cite sources, and do we automatically check groundedness?
7. How many tokens of retrieved context enter the window at p95?

## References

- `/oss/python/langgraph/agentic-rag`
- `/oss/python/langchain/retrieval`, `/oss/python/langchain/knowledge-base`
- `/oss/python/deepagents/rag`, `/oss/python/deepagents/retrieval`, `/oss/python/deepagents/deep-research`
- `/oss/python/integrations/vectorstores/index`, `/retrievers/index`, `/splitters/index`, `/embeddings/index`
