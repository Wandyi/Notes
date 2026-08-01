"""Sparse retrieval via BM25.

A from-scratch BM25 Okapi implementation over the in-memory chunk corpus. This
is the "keyword / lexical" half of hybrid search — it shines on exact matches
(service names, error codes, flags) that dense embeddings can wash out.
"""
from __future__ import annotations

import math
from collections import Counter

from ..core import Chunk
from .embeddings import tokenize


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.chunks: list[Chunk] = []
        self.doc_tokens: list[list[str]] = []
        self.doc_freqs: list[Counter] = []
        self.df: Counter = Counter()
        self.idf: dict[str, float] = {}
        self.avgdl: float = 0.0

    def index(self, chunks: list[Chunk]) -> None:
        self.chunks = list(chunks)
        self.doc_tokens = [tokenize(c.text + " " + c.title) for c in chunks]
        self.doc_freqs = [Counter(toks) for toks in self.doc_tokens]
        self.df = Counter()
        for toks in self.doc_tokens:
            for term in set(toks):
                self.df[term] += 1
        n = max(1, len(chunks))
        # BM25 idf with the standard +0.5 smoothing.
        self.idf = {
            term: math.log(1 + (n - df + 0.5) / (df + 0.5))
            for term, df in self.df.items()
        }
        total = sum(len(toks) for toks in self.doc_tokens)
        self.avgdl = total / n if n else 0.0

    def search(self, query: str, top_n: int) -> list[tuple[int, float]]:
        """Return [(chunk_index, score)] ranked descending, top_n only."""
        q_terms = tokenize(query)
        scores: list[tuple[int, float]] = []
        for idx, freqs in enumerate(self.doc_freqs):
            dl = len(self.doc_tokens[idx])
            score = 0.0
            for term in q_terms:
                if term not in freqs:
                    continue
                idf = self.idf.get(term, 0.0)
                tf = freqs[term]
                denom = tf + self.k1 * (1 - self.b + self.b * (dl / self.avgdl if self.avgdl else 1))
                score += idf * (tf * (self.k1 + 1)) / denom if denom else 0.0
            if score > 0:
                scores.append((idx, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_n]
