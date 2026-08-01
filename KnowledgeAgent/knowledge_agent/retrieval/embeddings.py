"""Embedding provider.

Production would call a hosted embedding model (e.g. Voyage/OpenAI/Cohere) or a
self-hosted encoder. To keep the reference implementation deterministic and
dependency-free, `HashingEmbeddingProvider` produces a fixed-dimension bag-of-
words vector via feature hashing with L2 normalization. It captures lexical
semantics (token overlap → cosine similarity) well enough to exercise the whole
dense-retrieval path without any network call.

The interface (`EmbeddingProvider.embed`) is what production code swaps out.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# A small stopword set keeps hashed vectors focused on content terms.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "are",
    "be", "with", "how", "do", "i", "you", "it", "this", "that", "we", "our",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


class EmbeddingProvider(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]:
        ...


class HashingEmbeddingProvider:
    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _bucket(self, token: str) -> int:
        h = hashlib.md5(token.encode("utf-8")).hexdigest()
        return int(h, 16) % self.dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in tokenize(text):
            vec[self._bucket(tok)] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


def cosine(a: list[float], b: list[float]) -> float:
    # Both vectors are L2-normalized, so cosine == dot product.
    return sum(x * y for x, y in zip(a, b))
