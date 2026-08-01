"""LLM abstraction, model-tier router, and semantic cache.

Two implementations behind one interface:

* `LocalSynthesizer` — deterministic, extractive, dependency-free. It composes
  a consolidated answer directly from the retrieved evidence so the reference
  system runs and is testable with no API key. Because it is extractive, its
  output is grounded by construction.
* `AnthropicSynthesizer` — the production path. Uses the official `anthropic`
  SDK, defaults to `claude-opus-5` with adaptive thinking, and asks the model
  to synthesize *only* from the supplied evidence with inline citation markers.

`ModelRouter` implements cost-tier routing (cheap first, escalate to a strong
model on thin/conflicting evidence). `SemanticCache` short-circuits repeated or
near-identical questions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..config import ModelTier, RoutingConfig
from ..core import Conflict, ScoredChunk, Source
from ..observability.tracing import Trace
from ..retrieval.embeddings import HashingEmbeddingProvider, cosine


@dataclass
class Evidence:
    marker: str          # "[1]"
    text: str
    source: Source
    title: str


@dataclass
class LLMResult:
    text: str
    model_id: str
    input_tokens: int
    output_tokens: int


class Synthesizer(Protocol):
    def synthesize(
        self,
        query: str,
        evidence: list[Evidence],
        conflicts: list[Conflict],
        tier: ModelTier,
        trace: Trace,
    ) -> LLMResult:
        ...


def _first_sentence(text: str) -> str:
    for sep in (". ", "\n"):
        if sep in text:
            return text.split(sep, 1)[0].strip().rstrip(".") + "."
    return text.strip()


class LocalSynthesizer:
    """Deterministic extractive synthesizer (no network, grounded by design)."""

    def synthesize(self, query, evidence, conflicts, tier, trace) -> LLMResult:
        with trace.span("llm.synthesize", "llm", model=tier.model_id, backend="local") as sp:
            lines: list[str] = []
            lines.append(
                f"Based on {len(evidence)} sources across the engineering knowledge base, "
                f"here is the consolidated answer to: \"{query.strip()}\"."
            )
            lines.append("")
            for ev in evidence:
                point = _first_sentence(ev.text)
                lines.append(f"- {point} {ev.marker}")
            if conflicts:
                lines.append("")
                lines.append("Conflicting information was detected and resolved:")
                for cf in conflicts:
                    competitors = ", ".join(
                        f"{c['source']}={c['value']}" for c in cf.competing
                    )
                    lines.append(
                        f"- `{cf.key}`: using **{cf.resolved_value}** from the freshest source "
                        f"({cf.winning_source.value}); other sources said {competitors}."
                    )
            text = "\n".join(lines)
            input_tokens = sum(len(ev.text.split()) for ev in evidence) + len(query.split())
            output_tokens = len(text.split())
            sp.attributes["output_tokens"] = output_tokens
            return LLMResult(text=text, model_id=tier.model_id,
                             input_tokens=input_tokens, output_tokens=output_tokens)


class AnthropicSynthesizer:
    """Production synthesizer using the official Anthropic SDK.

    Not exercised in the offline test suite (no API key); shown as the drop-in
    production implementation. Defaults to claude-opus-5 with adaptive thinking.
    """

    SYSTEM = (
        "You are an experienced staff engineer answering operational questions. "
        "Synthesize a single consolidated answer using ONLY the numbered evidence "
        "provided. Cite every claim with its bracketed marker, e.g. [2]. If sources "
        "conflict, prefer the freshest and state the conflict explicitly. Never use "
        "outside knowledge; if the evidence is insufficient, say so."
    )

    def __init__(self, client=None) -> None:
        # Lazily import so the package imports without the anthropic dependency.
        if client is None:
            import anthropic  # type: ignore
            client = anthropic.Anthropic()
        self.client = client

    def synthesize(self, query, evidence, conflicts, tier, trace) -> LLMResult:
        with trace.span("llm.synthesize", "llm", model=tier.model_id, backend="anthropic") as sp:
            evidence_block = "\n\n".join(
                f"{ev.marker} ({ev.source.value} — {ev.title}):\n{ev.text}"
                for ev in evidence
            )
            conflict_block = ""
            if conflicts:
                conflict_block = "\n\nPre-detected conflicts (freshest already chosen):\n" + "\n".join(
                    f"- {cf.key}: {cf.resolved_value} (from {cf.winning_source.value})"
                    for cf in conflicts
                )
            user = f"Question: {query}\n\nEvidence:\n{evidence_block}{conflict_block}"
            resp = self.client.messages.create(
                model=tier.model_id,
                max_tokens=1500,
                thinking={"type": "adaptive"},
                system=self.SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            usage = getattr(resp, "usage", None)
            in_tok = getattr(usage, "input_tokens", 0) if usage else 0
            out_tok = getattr(usage, "output_tokens", 0) if usage else 0
            sp.attributes["output_tokens"] = out_tok
            return LLMResult(text=text, model_id=tier.model_id,
                             input_tokens=in_tok, output_tokens=out_tok)


class ModelRouter:
    """Cost-tier routing: cheap by default, escalate on uncertainty."""

    def __init__(self, config: RoutingConfig) -> None:
        self.config = config

    def choose(self, evidence_count: int, has_conflict: bool, trace: Trace) -> ModelTier:
        escalate = evidence_count < self.config.escalate_below_evidence or (
            has_conflict and self.config.escalate_on_conflict
        )
        tier = self.config.strong if escalate else self.config.cheap
        trace.event(
            "router.choose_model", "orchestration",
            tier=tier.name, model=tier.model_id, escalated=escalate,
            evidence_count=evidence_count, has_conflict=has_conflict,
        )
        return tier


@dataclass
class _CacheEntry:
    vector: list[float]
    result: LLMResult


class SemanticCache:
    """Prompt/semantic cache keyed by query embedding similarity.

    **Access-scoped**: entries are partitioned by a `scope` string (tenant +
    the caller's allowed-source set). A cached answer is only ever served to a
    principal with the same access scope — otherwise the cache would leak an
    answer that referenced sources the new caller is not permitted to see
    (a cross-tenant / cross-role data-exposure hole). Never cache across scopes.
    """

    def __init__(self, threshold: float = 0.97, dim: int = 256) -> None:
        self.threshold = threshold
        self.embedder = HashingEmbeddingProvider(dim=dim)
        self.by_scope: dict[str, list[_CacheEntry]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, query: str, scope: str) -> Optional[LLMResult]:
        q = self.embedder.embed(query)
        for entry in self.by_scope.get(scope, []):
            if cosine(q, entry.vector) >= self.threshold:
                self.hits += 1
                return entry.result
        self.misses += 1
        return None

    def put(self, query: str, result: LLMResult, scope: str) -> None:
        self.by_scope.setdefault(scope, []).append(
            _CacheEntry(vector=self.embedder.embed(query), result=result)
        )
