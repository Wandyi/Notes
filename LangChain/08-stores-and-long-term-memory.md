# 08 — Stores & Long-Term Memory

## 1. Concepts

A **store** (`BaseStore`) is cross-thread, application-defined persistence: JSON documents organised
by `namespace` (a tuple, like a folder path) and `key` (like a filename).

```
namespace = ("acme-corp", "user-42", "memories")
key       = "pref-timezone"
value     = {"timezone": "Asia/Kolkata", "source": "conversation"}
```

Item fields: `value`, `key`, `namespace`, `created_at`, `updated_at`.

### The three memory types (CoALA framing, used throughout the docs)

| Type | Stores | Agent example | Typical implementation |
|---|---|---|---|
| **Semantic** | Facts | "User is a Go developer at Infoblox" | Store items — *profile* (one document, updated) or *collection* (many documents, searched) |
| **Episodic** | Experiences | "Last time this ticket type appeared, we did X" | Few-shot examples retrieved from a store or LangSmith dataset |
| **Procedural** | Rules / how-to | The agent's own instructions | System prompt + skills + reflection-updated instruction documents |

> Note the terminology trap: **semantic memory** (a kind of content) ≠ **semantic search** (a
> retrieval technique). You can store semantic memories and retrieve them by exact key.

### Profile vs collection

- **Profile**: a single document per user/entity, continuously updated. Easy to read (one `get`),
  bounded size, but updates require careful merge logic and risk information loss.
- **Collection**: many small documents, appended and searched. Scales to arbitrary knowledge, but
  shifts complexity to search quality, dedup and conflict resolution.

Pick per domain: user preferences → profile; observed facts and events → collection.

### Hot-path vs background writes

- **Hot path**: the agent decides to save a memory during the conversation (a `save_memory` tool).
  Immediate, transparent to the user, but costs latency and tokens and biases toward over-saving.
- **Background**: a separate process (a cron, or a post-run job) reflects over completed threads and
  distils memories. No user-facing latency, better quality, but eventually consistent and needs its
  own scheduling and failure handling.

Production systems usually do both: hot-path for explicit "remember that", background for distillation.

## 2. How to implement

### Wiring a store

```python
from langgraph.store.postgres.aio import AsyncPostgresStore

async with AsyncPostgresStore.from_conn_string(DB_URI) as store:
    await store.setup()
    graph = builder.compile(checkpointer=checkpointer, store=store)
```

With `create_agent`:

```python
agent = create_agent("claude-sonnet-4-6", tools=[...], store=store)
```

On the Agent Server the store is provided by the platform; do not construct your own.

### CRUD + search

```python
ns = ("user-42", "memories")

store.put(ns, "pref-1", {"food_preference": "I like pizza"})
item = store.get(ns, "pref-1")

items = store.search(ns, limit=10)                       # list (insertion order for InMemoryStore)
items = store.search(ns, filter={"kind": "preference"})  # content filter
namespaces = store.list_namespaces(prefix=("user-42",), max_depth=2)
store.delete(ns, "pref-1")
```

### Semantic search

```python
from langchain.embeddings import init_embeddings
from langgraph.store.memory import InMemoryStore

store = InMemoryStore(index={
    "embed": init_embeddings("openai:text-embedding-3-small"),
    "dims": 1536,
    "fields": ["food_preference", "$"],     # "$" = embed the whole document
})

hits = store.search(ns, query="What does the user like to eat?", limit=3)
```

Control what gets embedded per item:

```python
store.put(ns, key, {"food_preference": "...", "context": "..."}, index=["food_preference"])
store.put(ns, key, {"system_info": "..."}, index=False)   # retrievable, not searchable
```

### Reading/writing memory from tools

```python
from langchain.tools import tool
from langgraph.runtime import get_runtime

@tool
def recall_preferences(topic: str) -> str:
    """Look up what we know about the user for a topic."""
    rt = get_runtime()
    ns = (rt.context.user_id, "memories")
    hits = rt.store.search(ns, query=topic, limit=5)
    return "\n".join(str(h.value) for h in hits)

@tool
def remember(fact: str) -> str:
    """Save a durable fact about the user."""
    rt = get_runtime()
    rt.store.put((rt.context.user_id, "memories"), str(uuid.uuid4()), {"fact": fact})
    return "saved"
```

### Namespace design (the part people get wrong)

Namespaces are your **authorization and isolation boundary**. Design them first:

```
(tenant_id, "users", user_id, "profile")
(tenant_id, "users", user_id, "memories")
(tenant_id, "shared", "playbooks")
(tenant_id, "agents", assistant_id, "instructions")
```

Rules:

- **Tenant id is always the first element.** Never allow a namespace to be constructed from
  unvalidated model output or user input — that is a cross-tenant read primitive.
- Keep depth stable; `list_namespaces(prefix=..., max_depth=...)` depends on it.
- Put the memory *kind* in the namespace, not only in the value, so you can search and expire by kind.

### TTL for store items (LangSmith deployments)

```json
{
  "store": {
    "ttl": {
      "refresh_on_read": true,
      "sweep_interval_minutes": 120,
      "default_ttl": 10080
    }
  }
}
```

- `default_ttl` is in **minutes** (10080 = 7 days).
- `refresh_on_read: true` (default) resets expiry on `get`/`search` — good for "keep what's used".
- Applies only to items created after deployment; older items must be deleted manually.

### Background memory distillation (sketch)

```python
# Cron-triggered assistant that reflects over recent threads
@entrypoint(checkpointer=checkpointer)
async def distill(payload: dict, store) -> dict:
    thread = await load_thread(payload["thread_id"])
    facts = await extractor.ainvoke(thread.messages)   # structured output: list[Fact]
    ns = (payload["tenant_id"], "users", payload["user_id"], "memories")
    for f in facts:
        existing = await store.asearch(ns, query=f.text, limit=3)
        if not is_duplicate(f, existing):
            await store.aput(ns, stable_id(f), f.model_dump())
    return {"written": len(facts)}
```

## 3. Scenarios

| Scenario | Design |
|---|---|
| Personal assistant remembering preferences | Profile document per user, hot-path `remember` tool + nightly distillation |
| Support agent that learns resolutions | Collection of episodic memories, semantic search keyed by symptom, retrieved as few-shot examples |
| Agent whose instructions improve over time | Procedural memory: an instructions document in the store, loaded by a `before_model` middleware, updated by a reflection job with human review |
| Org-wide shared knowledge | `(tenant, "shared", ...)` namespace, written only by an admin flow, read by all agents |
| Right-to-be-forgotten | `list_namespaces(prefix=(tenant, "users", user_id))` → delete all; plus thread deletion and trace scrubbing |

## 4. Staff-level considerations

- **Memory is a product decision with a correctness risk.** A wrong "fact" persists across every
  future conversation and is nearly invisible in a trace. Require provenance (`source_thread_id`,
  `confidence`, `written_at`) on every memory item and show it in an admin UI.
- **Conflict resolution needs a policy**: last-write-wins, confidence-weighted, or human-reviewed.
  Write it down. Silent contradictions are the #1 cause of "the agent suddenly got worse".
- **Search recall is the bottleneck, not storage.** Embed the right field (not `"$"` by default),
  measure recall@k against a labelled set, and treat memory retrieval as a component you evaluate
  ([20](20-observability-and-evaluation.md)).
- **Store reads are on the hot path.** Every `search` before a model call adds latency and DB load.
  Cache per-run, batch reads, and prefer one profile `get` over five `search`es.
- **TTL is a privacy control, not just a cost control.** Default to expiring memories unless a
  product reason says otherwise; `refresh_on_read` gives you LRU semantics for free.
- **Scope creep into "our RAG index"**: the store is a document KV with optional vector search, not
  a full retrieval platform. If you need hybrid search, reranking, or heavy filtering, use a real
  vector DB behind a retrieval tool and keep the store for agent memory ([25](25-rag-and-knowledge.md)).

## 5. Anti-patterns

- Namespaces built from model output or unvalidated request fields (cross-tenant leakage).
- Saving whole conversation turns as "memories" — unbounded growth, poor recall, high cost.
- Using the store as the agent's working scratchpad (that's state, or the Deep Agents filesystem).
- No dedup: the same fact stored 40 times, then all 40 retrieved into context.
- `InMemoryStore` in production.
- Embedding entire documents (`"$"`) when only one field is semantically meaningful.
- Long-term memory with no deletion path (a GDPR problem waiting to happen).

## 6. Design-review questions

1. What is the namespace schema, and where is the tenant id enforced?
2. Which memories are written on the hot path vs. in the background, and why?
3. What is the dedup and conflict-resolution policy?
4. How do we measure whether memory retrieval is helping? What's the eval?
5. What is the TTL, and how does a user delete everything we know about them?
6. How many store reads happen per model call, and what do they cost in p95 latency?

## References

- `/oss/python/langgraph/stores`
- `/oss/python/concepts/memory`
- `/oss/python/langchain/long-term-memory`
- `/langsmith/configure-ttl`
- `/oss/python/integrations/long-term-memory/index`
