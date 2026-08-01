"""Core domain types shared across the data plane and control plane.

These are deliberately dependency-free (stdlib only) so the whole system runs
and tests without any external packages, vector DB, or API keys. Production
implementations swap the local providers (embeddings, LLM, connectors) behind
the same interfaces defined here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --- Knowledge sources the assistant federates over ------------------------

class Source(str, Enum):
    GITHUB = "github"
    HELM = "helm"
    TERRAFORM = "terraform"
    SLACK = "slack"
    JIRA = "jira"
    RUNBOOK = "runbook"
    KUBERNETES = "kubernetes"
    ARCH_DOCS = "arch_docs"
    CONFLUENCE = "confluence"


@dataclass
class Document:
    """A raw document retrieved from a single knowledge source."""
    id: str
    source: Source
    title: str
    content: str
    url: str
    updated_at: datetime
    # Structured, source-of-truth claims used for conflict detection, e.g.
    # {"replicas": "6", "namespace": "payments-prod"}. In production these are
    # extracted by an NER/LLM pass; here they are attached for determinism.
    claims: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Chunk:
    """A retrieval unit produced by the chunking strategy."""
    id: str
    doc_id: str
    source: Source
    title: str
    text: str
    url: str
    updated_at: datetime
    position: int
    claims: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScoredChunk:
    """A chunk with the full set of retrieval scores attached for observability."""
    chunk: Chunk
    dense_score: float = 0.0
    sparse_score: float = 0.0
    fused_score: float = 0.0
    rerank_score: float = 0.0
    freshness_score: float = 0.0
    final_score: float = 0.0
    is_stale: bool = False

    @property
    def source(self) -> Source:
        return self.chunk.source


@dataclass
class Citation:
    """A citation attached to a synthesized answer."""
    marker: str            # e.g. "[1]"
    source: Source
    title: str
    url: str
    chunk_id: str
    updated_at: datetime
    is_stale: bool = False


@dataclass
class Conflict:
    """A detected disagreement between sources about the same claim."""
    key: str                       # e.g. "replicas"
    resolved_value: str            # value chosen (freshest source wins)
    winning_source: Source
    competing: list[dict[str, Any]]  # [{source, value, url, updated_at}]


@dataclass
class Answer:
    """The consolidated answer returned to the caller."""
    request_id: str
    query: str
    text: str
    citations: list[Citation] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    sources_consulted: list[Source] = field(default_factory=list)
    groundedness: float = 0.0
    trajectory_score: float = 0.0
    refused: bool = False
    refusal_reason: Optional[str] = None
    cost_usd: float = 0.0
    model_used: Optional[str] = None
    trace_id: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "query": self.query,
            "text": self.text,
            "citations": [
                {
                    "marker": c.marker,
                    "source": c.source.value,
                    "title": c.title,
                    "url": c.url,
                    "updated_at": c.updated_at.isoformat(),
                    "is_stale": c.is_stale,
                }
                for c in self.citations
            ],
            "conflicts": [
                {
                    "key": cf.key,
                    "resolved_value": cf.resolved_value,
                    "winning_source": cf.winning_source.value,
                    "competing": cf.competing,
                }
                for cf in self.conflicts
            ],
            "sources_consulted": [s.value for s in self.sources_consulted],
            "groundedness": round(self.groundedness, 3),
            "trajectory_score": round(self.trajectory_score, 3),
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "cost_usd": round(self.cost_usd, 6),
            "model_used": self.model_used,
            "trace_id": self.trace_id,
            "warnings": self.warnings,
        }


@dataclass
class Principal:
    """The caller identity used for RBAC and audit."""
    user_id: str
    tenant_id: str
    roles: list[str] = field(default_factory=list)


@dataclass
class QueryRequest:
    query: str
    principal: Principal
    request_id: str = field(default_factory=lambda: new_id("req"))
    max_sources: int = 8
    top_k: int = 6
