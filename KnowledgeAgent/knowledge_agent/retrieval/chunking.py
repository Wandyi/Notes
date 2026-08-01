"""Chunking strategy.

Design choice: structure-aware, sliding-window chunking with overlap.

* We split on paragraph/heading boundaries first (structure-aware) so a chunk
  rarely straddles two unrelated topics.
* Long sections are further split with a fixed target size and a sliding
  overlap so a fact that spans a boundary is still fully present in at least
  one chunk (mitigates the "answer split across the seam" failure mode).
* Chunk claims/metadata are inherited from the parent document so downstream
  conflict detection and citation keep provenance.
"""
from __future__ import annotations

import re

from ..config import ChunkingConfig
from ..core import Chunk, Document, new_id

_PARA_SPLIT = re.compile(r"\n\s*\n")


def _tokenize_words(text: str) -> list[str]:
    return text.split()


def _split_long_block(words: list[str], target: int, overlap: int) -> list[list[str]]:
    if len(words) <= target:
        return [words]
    step = max(1, target - overlap)
    windows: list[list[str]] = []
    i = 0
    while i < len(words):
        windows.append(words[i:i + target])
        if i + target >= len(words):
            break
        i += step
    return windows


def chunk_document(doc: Document, config: ChunkingConfig) -> list[Chunk]:
    """Split a single document into overlapping, structure-aware chunks."""
    blocks = [b.strip() for b in _PARA_SPLIT.split(doc.content) if b.strip()]
    if not blocks:
        blocks = [doc.content.strip()] if doc.content.strip() else []

    chunks: list[Chunk] = []
    position = 0
    for block in blocks:
        words = _tokenize_words(block)
        for window in _split_long_block(words, config.target_tokens, config.overlap_tokens):
            if len(window) < config.min_tokens and chunks:
                # Merge a too-small trailing fragment into the previous chunk
                prev = chunks[-1]
                prev.text = f"{prev.text} {' '.join(window)}".strip()
                continue
            text = " ".join(window)
            chunks.append(
                Chunk(
                    id=new_id("chunk"),
                    doc_id=doc.id,
                    source=doc.source,
                    title=doc.title,
                    text=text,
                    url=doc.url,
                    updated_at=doc.updated_at,
                    position=position,
                    claims=dict(doc.claims),
                    metadata=dict(doc.metadata),
                )
            )
            position += 1
    return chunks


def chunk_documents(docs: list[Document], config: ChunkingConfig) -> list[Chunk]:
    out: list[Chunk] = []
    for doc in docs:
        out.extend(chunk_document(doc, config))
    return out
