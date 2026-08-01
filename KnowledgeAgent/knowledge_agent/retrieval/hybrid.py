"""Hybrid retriever: dense + sparse → RRF fusion → re-rank → freshness blend.

Pipeline (the retrieval data path):

    chunks ─┬─ dense (embedding cosine) ─┐
            └─ sparse (BM25 lexical)  ───┴─ Reciprocal Rank Fusion (RRF)
                                              │
                                              ├─ cross-encoder re-rank (top-N)
                                              └─ freshness blend + staleness flag
                                                     │
                                                     └─ top-k ScoredChunks

RRF is used for fusion (rather than score normalization) because dense cosine
and BM25 scores live on different scales; fusing by *rank* is robust and needs
no per-corpus tuning.
"""
from __future__ import annotations

from ..config import RetrievalConfig
from ..core import Chunk, ScoredChunk
from .embeddings import EmbeddingProvider
from .freshness import blend_final_score, freshness_score, is_stale
from .reranker import Reranker
from .sparse import BM25Index
from .vector_store import DenseVectorStore


def reciprocal_rank_fusion(
    dense: list[tuple[int, float]],
    sparse: list[tuple[int, float]],
    k: int,
) -> dict[int, float]:
    """Fuse two ranked lists by reciprocal rank. Returns {chunk_index: rrf_score}."""
    fused: dict[int, float] = {}
    for ranked in (dense, sparse):
        for rank, (idx, _score) in enumerate(ranked):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return fused


class HybridRetriever:
    def __init__(
        self,
        embedder: EmbeddingProvider,
        reranker: Reranker,
        config: RetrievalConfig,
    ) -> None:
        self.config = config
        self.reranker = reranker
        self.dense = DenseVectorStore(embedder)
        self.sparse = BM25Index()
        self.chunks: list[Chunk] = []

    def index(self, chunks: list[Chunk]) -> None:
        self.chunks = list(chunks)
        self.dense.index(self.chunks)
        self.sparse.index(self.chunks)

    def retrieve(self, query: str, top_k: int, now=None) -> list[ScoredChunk]:
        if not self.chunks:
            return []
        cfg = self.config

        dense_hits = self.dense.search(query, cfg.dense_candidates)
        sparse_hits = self.sparse.search(query, cfg.sparse_candidates)
        dense_map = dict(dense_hits)
        sparse_map = dict(sparse_hits)

        fused = reciprocal_rank_fusion(dense_hits, sparse_hits, cfg.rrf_k)
        if not fused:
            return []

        # Re-rank only the strongest fused candidates (precision, bounded cost).
        candidates = sorted(fused.items(), key=lambda x: x[1], reverse=True)
        candidates = candidates[: cfg.rerank_top_n]

        scored: list[ScoredChunk] = []
        for idx, rrf in candidates:
            chunk = self.chunks[idx]
            rerank = self.reranker.score(query, chunk)
            # Lexical-relevance floor: a chunk with no genuine query overlap is
            # not evidence, even if a hashed-embedding collision gave it a dense
            # score. This is what lets the "no grounded evidence" path fire.
            if rerank < cfg.min_rerank:
                continue
            fresh = freshness_score(chunk.updated_at, cfg.freshness_half_life_days, now)
            stale = is_stale(chunk.updated_at, cfg.staleness_days, now)
            final = blend_final_score(rerank, fresh, cfg)
            scored.append(
                ScoredChunk(
                    chunk=chunk,
                    dense_score=dense_map.get(idx, 0.0),
                    sparse_score=sparse_map.get(idx, 0.0),
                    fused_score=rrf,
                    rerank_score=rerank,
                    freshness_score=fresh,
                    final_score=final,
                    is_stale=stale,
                )
            )

        scored.sort(key=lambda s: s.final_score, reverse=True)
        return scored[:top_k]
