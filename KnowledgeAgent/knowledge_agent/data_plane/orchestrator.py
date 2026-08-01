"""Orchestration & coordination.

Topology: **supervisor / orchestrator-worker** (the workhorse pattern). The
orchestrator plans which source connectors ("workers") to consult, fans out to
them concurrently, and gathers the results. It then runs the retrieval pipeline
over the union of returned documents.

Key discipline — **bounded handoffs**: the orchestrator does NOT accumulate the
full text of every worker's output into a growing context. It reduces the fan-out
to a small, re-ranked top-k of chunks before anything downstream sees it. That
keeps context (and cost) from growing non-linearly with the number of sources —
the most common multi-agent failure mode.

Topology is a first-class choice: this is a shallow (one-level) fan-out, chosen
because the workload is "consult N sources and merge", not a deep hierarchy.
Deep hierarchies are avoided to keep interactive latency low.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..core import Chunk, Conflict, Document, ScoredChunk, Source
from ..observability.tracing import Trace
from ..retrieval.chunking import chunk_documents
from ..retrieval.hybrid import HybridRetriever
from .conflict import resolve_conflicts
from .tools.registry import ToolRegistry


@dataclass
class OrchestrationResult:
    scored: list[ScoredChunk] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    sources_consulted: list[Source] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tool_calls: int = 0
    documents_gathered: int = 0


class Orchestrator:
    def __init__(self, registry: ToolRegistry, retriever: HybridRetriever, config: Config) -> None:
        self.registry = registry
        self.retriever = retriever
        self.config = config

    def plan(self, query: str, allowed_sources: set[Source], max_sources: int, trace: Trace):
        """Select the worker set: dynamic tool selection ∩ RBAC-allowed sources."""
        candidates = self.registry.select(query, max_sources * 2, trace)
        selected = [t for t in candidates if t.manifest.source in allowed_sources][:max_sources]
        trace.event(
            "orchestrator.plan", "orchestration",
            workers=[t.manifest.name for t in selected],
            allowed=sorted(s.value for s in allowed_sources),
        )
        return selected

    def gather(self, workers, query: str, trace: Trace):
        """Fan-out to the worker connectors (bounded per-tool doc limit)."""
        with trace.span("orchestrator.fanout", "orchestration", workers=len(workers)):
            return self.registry.gather(workers, query, per_tool_limit=4, trace=trace)

    def reduce(self, query: str, documents: list[Document], top_k: int,
               trace: Trace, now=None) -> tuple[list[ScoredChunk], list[Conflict], list[Source]]:
        """Reduce fan-out to a small, precise top-k BEFORE anything downstream sees it.

        This bounded handoff is what keeps context and cost from growing
        non-linearly with the number of sources consulted.
        """
        with trace.span("orchestrator.retrieve", "retrieval", docs=len(documents)) as sp:
            chunks: list[Chunk] = chunk_documents(documents, self.config.chunking)
            self.retriever.index(chunks)
            scored = self.retriever.retrieve(query, top_k=top_k, now=now)
            sp.attributes["chunks"] = len(chunks)
            sp.attributes["retrieved"] = len(scored)

        conflicts = resolve_conflicts(scored)
        seen: list[Source] = []
        for sc in scored:
            if sc.chunk.source not in seen:
                seen.append(sc.chunk.source)
        return scored, conflicts, seen

    def run(self, query: str, allowed_sources: set[Source], max_sources: int,
            top_k: int, trace: Trace, now=None) -> OrchestrationResult:
        """Convenience end-to-end orchestration (plan → gather → reduce)."""
        result = OrchestrationResult()

        with trace.span("orchestrator.plan", "orchestration"):
            workers = self.plan(query, allowed_sources, max_sources, trace)
        if not workers:
            result.warnings.append("no sources are permitted for this principal")
            return result

        gathered = self.gather(workers, query, trace)
        result.tool_calls = gathered.tool_calls
        result.warnings.extend(gathered.warnings)
        result.documents_gathered = len(gathered.documents)
        if not gathered.documents:
            return result

        scored, conflicts, sources = self.reduce(query, gathered.documents, top_k, trace, now)
        result.scored = scored
        result.conflicts = conflicts
        result.sources_consulted = sources
        return result
