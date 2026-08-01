"""Memory & context management.

Two stores with different lifetimes and retrieval policies:

* **Short-term memory** — scoped to a single session. Holds intermediate
  results (the last few questions/answers) so follow-ups have context. Bounded
  in size; oldest entries are evicted.
* **Long-term memory** — durable facts/preferences/prior decisions across
  sessions, keyed by tenant. Retrieval is *selective*: we embed the query and
  return only the few most-similar facts rather than stuffing everything into
  the window ("lost in the middle" avoidance — store once, recall on demand).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..core import utcnow
from ..retrieval.embeddings import HashingEmbeddingProvider, cosine


@dataclass
class MemoryItem:
    text: str
    kind: str                      # "qa" | "fact" | "preference" | "decision"
    tenant_id: str
    created_at: str = field(default_factory=lambda: utcnow().isoformat())


class ShortTermMemory:
    def __init__(self, max_items: int = 6) -> None:
        self._items: deque[MemoryItem] = deque(maxlen=max_items)

    def add(self, item: MemoryItem) -> None:
        self._items.append(item)

    def recent(self, n: int = 3) -> list[MemoryItem]:
        return list(self._items)[-n:]

    def clear(self) -> None:
        self._items.clear()


class LongTermMemory:
    """Selective, embedding-based recall over durable facts (per tenant)."""

    def __init__(self, dim: int = 256) -> None:
        self.embedder = HashingEmbeddingProvider(dim=dim)
        self._items: list[MemoryItem] = []
        self._vectors: list[list[float]] = []

    def add(self, item: MemoryItem) -> None:
        self._items.append(item)
        self._vectors.append(self.embedder.embed(item.text))

    def recall(self, query: str, tenant_id: str, top_k: int = 3, min_sim: float = 0.15) -> list[MemoryItem]:
        q = self.embedder.embed(query)
        scored = [
            (cosine(q, self._vectors[i]), item)
            for i, item in enumerate(self._items)
            if item.tenant_id == tenant_id
        ]
        scored = [(s, it) for s, it in scored if s >= min_sim]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _s, it in scored[:top_k]]

    def __len__(self) -> int:
        return len(self._items)
