"""Dense vector store.

An in-memory cosine-similarity store standing in for a production vector DB
(pgvector / Pinecone / Weaviate / Qdrant). The interface — `index` and
`search` — is what a real backend implements; the brute-force cosine scan here
is fine for the reference corpus and keeps the system dependency-free.
"""
from __future__ import annotations

from ..core import Chunk
from .embeddings import EmbeddingProvider, cosine


class DenseVectorStore:
    def __init__(self, embedder: EmbeddingProvider) -> None:
        self.embedder = embedder
        self.chunks: list[Chunk] = []
        self.vectors: list[list[float]] = []

    def index(self, chunks: list[Chunk]) -> None:
        self.chunks = list(chunks)
        self.vectors = [self.embedder.embed(c.text + " " + c.title) for c in chunks]

    def search(self, query: str, top_n: int) -> list[tuple[int, float]]:
        """Return [(chunk_index, cosine_score)] ranked descending, top_n only."""
        q = self.embedder.embed(query)
        scored = [(i, cosine(q, v)) for i, v in enumerate(self.vectors)]
        scored = [s for s in scored if s[1] > 0.0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]
