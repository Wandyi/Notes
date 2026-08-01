"""Cross-encoder-style re-ranker.

A production system runs fused candidates through a cross-encoder (query+chunk
scored jointly) which is far more precise than the first-stage retrievers but
too expensive to run over the whole corpus — hence rerank only the top-N fused
candidates.

This deterministic stand-in scores query/chunk relevance with signals a cross
encoder approximates: term overlap (Jaccard), exact phrase presence, and a
title-match bonus. Swap `LexicalReranker` for a hosted cross-encoder in prod.
"""
from __future__ import annotations

from typing import Protocol

from ..core import Chunk
from .embeddings import tokenize


class Reranker(Protocol):
    def score(self, query: str, chunk: Chunk) -> float:
        ...


class LexicalReranker:
    def score(self, query: str, chunk: Chunk) -> float:
        q_terms = set(tokenize(query))
        if not q_terms:
            return 0.0
        c_terms = set(tokenize(chunk.text))
        title_terms = set(tokenize(chunk.title))

        overlap = q_terms & c_terms
        jaccard = len(overlap) / len(q_terms | c_terms) if (q_terms | c_terms) else 0.0
        coverage = len(overlap) / len(q_terms)     # fraction of query covered

        # Exact contiguous phrase bonus (strong cross-encoder-like signal).
        phrase_bonus = 0.2 if query.strip().lower() in chunk.text.lower() else 0.0
        title_bonus = 0.15 * (len(q_terms & title_terms) / len(q_terms))

        return min(1.0, 0.6 * coverage + 0.25 * jaccard + phrase_bonus + title_bonus)
