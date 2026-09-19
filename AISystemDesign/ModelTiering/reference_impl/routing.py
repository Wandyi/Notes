"""
Ledgerline — the routing layer and the escalation ladder (docs/03 and docs/04).

WHY THERE IS NO MODEL CALL IN THIS FILE
    README stance 2 / docs/03: "Never spend an inference call to decide an inference call."
    A router *model* on the hot path is (a) a tax on 100% of traffic to save on a fraction
    of it, (b) a brand-new failure mode with no detector of its own, and (c) recursive --
    the router needs a tier, and choosing it needs a router. So every routing decision here
    is a pure function of features that ``intake`` already computed while parsing:
    clause_tokens, cross_refs, language, ocr_confidence, doc_type. All are free, all are
    available BEFORE the call, and all are replayable from the call record.

    The static blast-radius floor (docs/02) is the lower bound; routing may only move a node
    UP from it, and only to a tier that still *satisfies* the node's capability vector
    (docs/01 §3). Routing cannot tier a node below its floor -- that is the one direction
    the rubric forbids, and ``route`` structurally cannot express it.

Two invariants are STRUCTURAL here -- they raise, they are not linted:

  1. ``detectability_gate`` / ``EscalationLadder.build``: escalation credit may only be
     claimed on a node that DECLARES a detector. README stance 3 / docs/04: "Without a
     detector, escalation never fires and 'cheap-first' is just 'be wrong cheaply.'"
     Building a ladder on ``verify`` (no detector, docs/00 §2 "NOTHING CHECKS THIS") raises.
  2. A ladder walks ``tiers.next_satisfying_tier``, never ``rank + 1``, so a rung may skip a
     tier or the ladder may come up short (docs/01 §3 consequence 1).

Thresholds below are illustrative in the same sense as docs/00's rates: named so you can
substitute your own. The one taken from the docs is CLAUSE_TOKENS_PREEMPT (docs/01 §3's
"1,200 tokens of nested cross-references").
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional, Sequence

from blast_radius import LEDGERLINE, ScoredNode, score
from tiers import (LEDGERLINE_REGISTRY, UTC, NoSatisfyingTier, Tier, TierRegistry,
                   next_satisfying_tier, utcnow)

# --- Pre-call feature thresholds (docs/03) --------------------------------- #
CLAUSE_TOKENS_PREEMPT = 1_200          # docs/01 §3's worked example clause
CROSS_REFS_PREEMPT = 4                 # illustrative
OCR_CONFIDENCE_FLOOR = Decimal("0.85")  # illustrative
HIGH_STAKES_DOC_TYPES = frozenset({"msa", "credit_agreement", "spa"})
PREEMPT_TARGET = Tier.MID              # "routes straight to mid" -- not to large

# --- Escalation policy (docs/04) ------------------------------------------- #
MAX_RUNGS = 2
"""docs/04: two rungs, and no more.

Cheap-first only pays while E[retry cost] < the price gap it avoids. Each rung adds the
FULL cost of the attempt below it, so at a >=3x price gap (docs/01 §6) a third rung costs
more in expectation than starting at the top -- and it adds two extra serialised round
trips to a document already carrying a 4-minute p95 SLO (docs/00 §7).
"""

BREAKER_WINDOW = timedelta(minutes=5)
BREAKER_MAX_RATE = Decimal("0.30")
"""docs/04 / docs/10 (escalation storms).

docs/00 §6 prices escalation at 12% of clauses. A sustained rate above ~30% means the cheap
tier is broken -- a prompt deploy, an unreviewed re-point, or a tenant corpus out of
distribution -- and the pipeline is now paying for BOTH tiers on a third of the fan-out.
The correct response is to stop escalating and queue, not to keep buying retries.
"""
BREAKER_MIN_SAMPLES = 20


class NoDetectorError(RuntimeError):
    """A ladder was built on a node whose failures nothing detects (docs/04)."""


class UnroutableLanguage(NoSatisfyingTier):
    """No tier in the registry covers the document's governing language."""

    def __init__(self, language: str):
        self.language = language
        RuntimeError.__init__(self, f"no tier covers language {language!r}")


# --- Features: cheap, pre-call, replayable (docs/03) ----------------------- #
@dataclass(frozen=True)
class Features:
    """Everything ``intake`` already knows. No model produced any of it."""

    clause_tokens: int = 0
    cross_refs: int = 0
    language: str = "en"
    ocr_confidence: Decimal = Decimal("1.00")
    doc_type: str = "nda"


@dataclass(frozen=True)
class RoutingDecision:
    node: str
    tier: Tier
    blast_radius_floor: Tier
    feature_floor: Tier
    reasons: tuple[str, ...]

    def __str__(self) -> str:
        why = "; ".join(self.reasons) if self.reasons else "floor only"
        return (f"{self.node}: {self.tier} (floor {self.blast_radius_floor}, "
                f"features -> {self.feature_floor}) [{why}]")


def _language_floor(language: str, registry: TierRegistry) -> Tier:
    covering = [c for c in registry.contracts.values() if language in c.provides.languages]
    if not covering:
        raise UnroutableLanguage(language)
    return min(covering, key=lambda c: c.rank).tier


def feature_tier_floor(features: Features, registry: TierRegistry
                       ) -> tuple[Tier, tuple[str, ...]]:
    """Deterministic pre-emptive escalation: raise the floor BEFORE spending the cheap call.

    docs/04: escalating after a detected failure costs the cheap attempt plus the expensive
    one. When a *free* feature already predicts the failure, paying once at the higher tier
    is strictly cheaper. That is the whole argument for a pre-emptive rule existing at all.
    """
    floor, reasons = Tier.NANO, []
    if features.clause_tokens >= CLAUSE_TOKENS_PREEMPT:
        floor = max(floor, PREEMPT_TARGET)
        reasons.append(f"clause_tokens {features.clause_tokens}>={CLAUSE_TOKENS_PREEMPT}")
    if features.cross_refs >= CROSS_REFS_PREEMPT:
        floor = max(floor, PREEMPT_TARGET)
        reasons.append(f"cross_refs {features.cross_refs}>={CROSS_REFS_PREEMPT}")
    if features.ocr_confidence < OCR_CONFIDENCE_FLOOR:
        floor = max(floor, PREEMPT_TARGET)
        reasons.append(f"ocr_confidence {features.ocr_confidence}<{OCR_CONFIDENCE_FLOOR}")
    if features.doc_type in HIGH_STAKES_DOC_TYPES:
        floor = max(floor, PREEMPT_TARGET)
        reasons.append(f"doc_type {features.doc_type!r} is high-stakes")
    lang_floor = _language_floor(features.language, registry)
    if lang_floor > floor:
        floor = lang_floor
        reasons.append(f"language {features.language!r} first covered at {lang_floor}")
    return floor, tuple(reasons)


def explain(node: ScoredNode, features: Features, registry: Optional[TierRegistry] = None,
            *, pipeline: str = "ledgerline", now: Optional[datetime] = None) -> RoutingDecision:
    registry = registry if registry is not None else LEDGERLINE_REGISTRY
    now = now if now is not None else utcnow()
    fl, reasons = feature_tier_floor(features, registry)
    # The floor can only go UP. docs/02 owns the lower bound; docs/03 owns the raise.
    effective = node if fl <= node.tier_floor else replace(node, tier_floor=fl)
    tier = registry.resolve(effective, pipeline, now).tier
    return RoutingDecision(node.name, tier, node.tier_floor, fl, reasons)


def route(node: ScoredNode, features: Features, registry: Optional[TierRegistry] = None,
          *, pipeline: str = "ledgerline", now: Optional[datetime] = None) -> Tier:
    """The hot-path entry point. Pure, total, and free -- zero model calls (docs/03)."""
    return explain(node, features, registry, pipeline=pipeline, now=now).tier


# --- The escalation ladder (docs/04) --------------------------------------- #
class EscalationOutcome(Enum):
    PROCEED = "proceed"          # detector passed; the cheap tier was right
    ESCALATE = "escalate"        # detector failed; step to the next satisfying tier
    QUEUE = "queue"              # breaker tripped; stop paying twice, shed to a queue
    EXHAUSTED = "exhausted"      # rungs spent and still failing -> human / dead letter


def detectability_gate(node: ScoredNode) -> None:
    # README stance 3 / docs/02 §2B / docs/04: cheap-first is only valid where failure is
    # DETECTABLE. Without a declared detector the escalation branch is unreachable, so the
    # cost model's retry line is fiction and the node just ships wrong answers cheaply.
    if not node.detectors:
        raise NoDetectorError(
            f"node {node.name!r} declares no detector, so an escalation ladder can never "
            "fire; docs/04 forbids claiming cheap-first credit here (tier it up instead)"
        )


@dataclass(frozen=True)
class EscalationLadder:
    node: ScoredNode
    start: Tier
    rungs: tuple[Tier, ...]

    @classmethod
    def build(cls, node: ScoredNode, start: Optional[Tier] = None,
              registry: Optional[TierRegistry] = None) -> "EscalationLadder":
        detectability_gate(node)
        registry = registry if registry is not None else LEDGERLINE_REGISTRY
        start = start if start is not None else node.tier_floor
        rungs: list[Tier] = []
        cur = start
        while len(rungs) < MAX_RUNGS:
            nxt = next_satisfying_tier(node, cur, registry)   # never `rank + 1`
            if nxt is None:
                break                                        # docs/01 §3: it can just stop
            rungs.append(nxt)
            cur = nxt
        return cls(node=node, start=start, rungs=tuple(rungs))

    def __str__(self) -> str:
        chain = " -> ".join([str(self.start)] + [str(r) for r in self.rungs])
        tail = "" if len(self.rungs) == MAX_RUNGS else f"  (only {len(self.rungs)} rung(s))"
        return f"{self.node.name}: {chain}{tail}"


@dataclass
class EscalationBreaker:
    """Trips on escalation RATE over a window, not on a count (docs/04 storms, docs/10)."""

    window: timedelta = BREAKER_WINDOW
    max_rate: Decimal = BREAKER_MAX_RATE
    min_samples: int = BREAKER_MIN_SAMPLES
    _events: list[tuple[datetime, bool]] = field(default_factory=list, repr=False)

    def observe(self, now: datetime, escalated: bool) -> None:
        self._events.append((now, escalated))
        cutoff = now - self.window
        self._events = [(t, e) for t, e in self._events if t >= cutoff]

    def _window(self, now: datetime) -> list[tuple[datetime, bool]]:
        return [(t, e) for t, e in self._events if t >= now - self.window]

    def rate(self, now: datetime) -> Decimal:
        w = self._window(now)
        if not w:
            return Decimal("0")
        return Decimal(sum(1 for _, e in w if e)) / Decimal(len(w))

    def tripped(self, now: datetime) -> bool:
        w = self._window(now)
        return len(w) >= self.min_samples and self.rate(now) > self.max_rate

    def decide(self, now: datetime) -> EscalationOutcome:
        """QUEUE rather than ESCALATE once tripped -- shedding is cheaper than retrying."""
        return EscalationOutcome.QUEUE if self.tripped(now) else EscalationOutcome.ESCALATE


@dataclass(frozen=True)
class Attempt:
    rung: int
    tier: Tier
    detector_passed: bool
    outcome: EscalationOutcome


def run_ladder(ladder: EscalationLadder, detector_verdicts: Sequence[bool],
               breaker: Optional[EscalationBreaker] = None,
               now: Optional[datetime] = None) -> tuple[Attempt, ...]:
    """Walk the ladder against a stubbed detector. ``detector_verdicts[i]`` is rung i's pass.

    The detector is the only thing that can advance a rung -- which is the executable form
    of "detectability gates the escalation policy" (README stance 3).
    """
    now = now if now is not None else utcnow()
    chain = [ladder.start, *ladder.rungs]
    out: list[Attempt] = []
    for i, tier in enumerate(chain):
        passed = detector_verdicts[i] if i < len(detector_verdicts) else True
        if passed:
            out.append(Attempt(i, tier, True, EscalationOutcome.PROCEED))
            if breaker is not None:
                breaker.observe(now, escalated=False)
            return tuple(out)
        if i == len(chain) - 1:
            out.append(Attempt(i, tier, False, EscalationOutcome.EXHAUSTED))
            return tuple(out)
        decision = breaker.decide(now) if breaker is not None else EscalationOutcome.ESCALATE
        out.append(Attempt(i, tier, False, decision))
        if breaker is not None:
            breaker.observe(now, escalated=True)
        if decision is EscalationOutcome.QUEUE:
            return tuple(out)
    return tuple(out)


# --------------------------------------------------------------------------- #
def main() -> int:
    scored = score(LEDGERLINE)
    reg = LEDGERLINE_REGISTRY
    now = datetime(2026, 8, 19, tzinfo=UTC)
    extract, risk, verify = scored["extract"], scored["risk_flag"], scored["verify"]

    print("docs/03 — deterministic feature routing (zero model calls)")
    cases = (
        ("p50 clause", Features(clause_tokens=420, cross_refs=1)),
        ("long clause", Features(clause_tokens=1_400, cross_refs=1)),
        ("cross-ref nest", Features(clause_tokens=600, cross_refs=6)),
        ("bad scan", Features(clause_tokens=300, ocr_confidence=Decimal("0.71"))),
        ("MSA", Features(clause_tokens=300, doc_type="msa")),
        ("Japanese", Features(clause_tokens=300, language="ja")),
    )
    for label, f in cases:
        print(f"   {label:<16}{explain(extract, f, reg, now=now)}")

    print("\n   the floor is a floor: features cannot route BELOW docs/02's demand")
    print(f"   {'p50 clause':<16}{explain(verify, Features(clause_tokens=10), reg, now=now)}")

    print("\n   a capability gap is loud, not silently degraded:")
    try:
        route(extract, Features(clause_tokens=300, language="hi"), reg, now=now)
    except NoSatisfyingTier as exc:
        print(f"   Hindi -> {type(exc).__name__}: {exc}")

    print("\ndocs/04 — the escalation ladder walks *satisfying* tiers, max 2 rungs")
    for node in (extract, risk):
        print(f"   {EscalationLadder.build(node, registry=reg)}")
    print("   verify has no detector, so it has no ladder:")
    try:
        EscalationLadder.build(verify, registry=reg)
    except NoDetectorError as exc:
        print(f"   NoDetectorError: {exc}")

    ladder = EscalationLadder.build(extract, registry=reg)
    print("\n   walk 1 — cheap tier is right (the 88% case, docs/00 §6):")
    for a in run_ladder(ladder, [True], now=now):
        print(f"      rung {a.rung} @{a.tier:<6}detector={'pass' if a.detector_passed else 'FAIL'}"
              f" -> {a.outcome.name}")
    print("   walk 2 — cheap tier fails, escalate once (the 12% case):")
    for a in run_ladder(ladder, [False, True], now=now):
        print(f"      rung {a.rung} @{a.tier:<6}detector={'pass' if a.detector_passed else 'FAIL'}"
              f" -> {a.outcome.name}")

    print("\ndocs/04 — the breaker trips on RATE and queues instead of escalating")
    breaker = EscalationBreaker()
    for i in range(30):                      # 40% escalation rate: well above the 12% budget
        breaker.observe(now, escalated=(i % 5 in (0, 1)))
    print(f"   window rate {breaker.rate(now):.2f} > {BREAKER_MAX_RATE} -> "
          f"tripped={breaker.tripped(now)}, decide={breaker.decide(now).name}")
    for a in run_ladder(ladder, [False, False, False], breaker=breaker, now=now):
        print(f"      rung {a.rung} @{a.tier:<6}detector={'pass' if a.detector_passed else 'FAIL'}"
              f" -> {a.outcome.name}")
    calm = EscalationBreaker()
    for i in range(30):
        calm.observe(now, escalated=(i % 9 == 0))
    print(f"   calm window rate {calm.rate(now):.2f} -> decide={calm.decide(now).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
