"""Layered guardrails.

* **Pre-LLM** (fast, deterministic, in the hot path before every model call):
  PII redaction and prompt-injection detection via regex/heuristics. Cheap and
  synchronous — no LLM-as-judge on the critical path.
* **Post-LLM** (before the answer reaches the user): a groundedness check that
  verifies each answer sentence is supported by the retrieved evidence, catching
  hallucination on high-stakes paths.

Heavier LLM-as-judge evaluation is reserved for the async evaluator, not here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import GuardrailConfig
from ..core import ScoredChunk
from ..observability.tracing import Trace
from ..retrieval.embeddings import tokenize

# --- PII patterns (illustrative; production uses a vetted PII library) ------
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_BEARER = re.compile(r"\b(?:sk|xoxb|ghp|ghs)-[A-Za-z0-9_-]{10,}\b")

# --- Prompt-injection heuristics -------------------------------------------
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard (your|the) (system|previous) prompt", re.I),
    re.compile(r"you are now (?:a|an|dan|in developer mode)", re.I),
    re.compile(r"reveal (your|the) (system prompt|instructions)", re.I),
    re.compile(r"print (your|the) (system prompt|api key|secret)", re.I),
]


@dataclass
class PreCheck:
    ok: bool
    redacted_query: str
    injection_detected: bool = False
    redactions: list[str] = field(default_factory=list)
    reason: str | None = None


def redact_pii(text: str) -> tuple[str, list[str]]:
    redactions: list[str] = []
    for label, pat in (("EMAIL", _EMAIL), ("SSN", _SSN), ("AWS_KEY", _AWS_KEY), ("TOKEN", _BEARER)):
        def _sub(m, label=label):
            redactions.append(label)
            return f"[REDACTED_{label}]"
        text = pat.sub(_sub, text)
    return text, redactions


def detect_injection(text: str) -> bool:
    return any(p.search(text) for p in _INJECTION_PATTERNS)


class Guardrails:
    def __init__(self, config: GuardrailConfig) -> None:
        self.config = config

    # --- pre-LLM -----------------------------------------------------------
    def pre_check(self, query: str, trace: Trace) -> PreCheck:
        with trace.span("guardrail.pre", "guardrail") as sp:
            if len(query) > self.config.max_query_chars:
                sp.attributes["blocked"] = "too_long"
                return PreCheck(ok=False, redacted_query=query[: self.config.max_query_chars],
                                reason="query exceeds maximum length")
            injection = detect_injection(query)
            redacted, redactions = redact_pii(query)
            sp.attributes["injection"] = injection
            sp.attributes["redactions"] = redactions
            if injection and self.config.block_on_injection:
                return PreCheck(ok=False, redacted_query=redacted, injection_detected=True,
                                redactions=redactions,
                                reason="possible prompt-injection detected in the query")
            return PreCheck(ok=True, redacted_query=redacted, injection_detected=injection,
                            redactions=redactions)

    # --- post-LLM ----------------------------------------------------------
    def groundedness(self, answer_text: str, evidence: list[ScoredChunk], trace: Trace) -> float:
        """Fraction of substantive answer sentences supported by the evidence."""
        with trace.span("guardrail.post", "guardrail") as sp:
            evidence_terms = set()
            for e in evidence:
                evidence_terms |= set(tokenize(e.chunk.text + " " + e.chunk.title))

            sentences = [s.strip() for s in re.split(r"[.\n]", answer_text) if s.strip()]
            substantive = []
            for s in sentences:
                terms = set(tokenize(s))
                # Skip meta/citation-only lines and trivially short fragments.
                if len(terms) < 3:
                    continue
                substantive.append(terms)

            if not substantive:
                sp.attributes["groundedness"] = 1.0
                return 1.0

            supported = 0
            for terms in substantive:
                covered = len(terms & evidence_terms) / len(terms)
                if covered >= 0.5:
                    supported += 1
            score = supported / len(substantive)
            sp.attributes["groundedness"] = round(score, 3)
            sp.attributes["floor"] = self.config.groundedness_floor
            return score
