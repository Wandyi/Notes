"""
Ledgerline — a tier is a capability contract (reference implementation of docs/01).

Implements docs/01-tier-as-contract.md: the capability vector (§2), the *satisfies-check*
that makes capability a partial order rather than a rank comparison (§3), the registry and
its dependency graph (§4), and per-pipeline pins over a moving fleet default with mandatory
expiry (§5). Rates are docs/00-overview.md §3's illustrative $/Mtok figures.

Five invariants are STRUCTURAL here — they raise, they are not linted:

  1. A ``Binding`` cannot name a moving alias. ``…-latest``, ``stable``, ``*``, or a
     ``version`` of ``None`` raise at construction (docs/01 §7: binding to an alias means
     "the provider re-points your tier without your consent").
  2. A ``Pin`` must state a reason, and must expire inside 90 days, renewable once
     (docs/01 §5: "a pin without an expiry is not a pin, it is a fork").
  3. An empty ``requires`` is a validation error, never a permissive default
     (docs/01 §3 consequence 2, §7 anti-pattern table).
  4. ``resolve`` raises ``NoSatisfyingTier`` rather than silently falling back to whatever
     is cheapest (docs/01 §3).
  5. An *expired* pin resolves to the fleet default, never to nothing (docs/01 §5:
     "expiry falls back to default, never to nothing" — the fail-safe row).

``satisfies`` is dimension-wise domination, so ``next_satisfying_tier`` can skip a rank or
return ``None``. That is docs/01 §3 made executable: "escalate to the next tier up" is not
a capability statement.

Nothing here calls a model or a provider API.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from functools import total_ordering
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Protocol

UTC = timezone.utc
MTOK = Decimal(1_000_000)
MAX_PIN_DAYS = 90        # docs/01 §5: "Pins expire (90 days, renewable once)"
MAX_PIN_RENEWALS = 1     # docs/01 §5: ... renewable ONCE
PIN_WARNING_DAYS = 30    # docs/01 §5: T-30 expiry warning to the owning team


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- Errors ---------------------------------------------------------------- #
class AliasBindingError(ValueError):
    """A binding named a moving target instead of an exact version (docs/01 §7)."""


class PinError(ValueError):
    """A pin was reasonless, immortal, or renewed past its allowance (docs/01 §5)."""


class UnstatedRequirements(ValueError):
    """A node asked to be resolved without stating what it needs (docs/01 §3)."""


class NoSatisfyingTier(LookupError):
    """No tier at or above the node's floor satisfies its capability vector."""

    def __init__(self, node: str, floor: "Tier", failures: Mapping[str, tuple[str, ...]]):
        self.node, self.floor, self.failures = node, floor, failures
        detail = "; ".join(f"{t}: {', '.join(d)}" for t, d in failures.items()) or "no candidates"
        super().__init__(
            f"node {node!r} has floor {floor.name.lower()!r} and no tier satisfies it ({detail})"
        )


# --- Tiers and their illustrative rates (docs/00 §3) ----------------------- #
@total_ordering
class Tier(Enum):
    """Price is a TOTAL order (docs/01 §3, left panel). Capability is not; see Capabilities."""

    NANO = 1
    SMALL = 2
    MID = 3
    LARGE = 4
    FRONTIER = 5

    @property
    def rank(self) -> int:
        return self.value

    @property
    def rate_in(self) -> Decimal:
        return RATES_USD_PER_MTOK[self][0]

    @property
    def rate_out(self) -> Decimal:
        return RATES_USD_PER_MTOK[self][1]

    def __lt__(self, other: object) -> bool:
        return self.rank < other.rank if isinstance(other, Tier) else NotImplemented

    def __str__(self) -> str:
        return self.name.lower()


RATES_USD_PER_MTOK: Mapping[Tier, tuple[Decimal, Decimal]] = MappingProxyType({
    Tier.NANO: (Decimal("0.10"), Decimal("0.40")),
    Tier.SMALL: (Decimal("0.25"), Decimal("1.25")),
    Tier.MID: (Decimal("1.00"), Decimal("5.00")),
    Tier.LARGE: (Decimal("3.00"), Decimal("15.00")),
    Tier.FRONTIER: (Decimal("10.00"), Decimal("40.00")),
})


def token_cost(tier: Tier, tokens_in: int, tokens_out: int) -> Decimal:
    """Exact $/call at docs/00 §3 rates. Decimal throughout; rounding is presentation."""
    return (Decimal(tokens_in) * tier.rate_in + Decimal(tokens_out) * tier.rate_out) / MTOK


@dataclass(frozen=True)
class TokenProfile:
    """docs/00 §4's per-document token profile for one node."""

    calls_per_doc: int
    tokens_in: int
    tokens_out: int

    def cost_per_call(self, tier: Tier) -> Decimal:
        return token_cost(tier, self.tokens_in, self.tokens_out)

    def cost_per_doc(self, tier: Tier) -> Decimal:
        return self.cost_per_call(tier) * Decimal(self.calls_per_doc)


# --- The capability vector (docs/01 §2) ------------------------------------ #
LANG_6 = frozenset({"en", "de", "fr", "es", "it", "nl"})
LANG_24 = LANG_6 | frozenset(
    {"pt", "pl", "sv", "da", "fi", "cs", "ja", "ko", "zh", "ar",
     "he", "tr", "ru", "uk", "ro", "hu", "el", "no"}
)
LANG_32 = LANG_24 | frozenset({"th", "vi", "id", "ms", "hi", "bn", "ta", "fa"})


@dataclass(frozen=True)
class Capabilities:
    """A VECTOR, not a score. Used two ways: what a tier provides, what a node requires.

    ``max_cost_*`` is a rate on the providing side and a ceiling on the requiring side —
    hence the reversed comparison sense in ``satisfies``, which is commented there.
    """

    context_floor: int = 0
    structured_output_conformance: Decimal = Decimal("0")
    tool_call_conformance: Decimal = Decimal("0")
    long_prompt_instruction_following: Decimal = Decimal("0")
    languages: frozenset[str] = frozenset()
    refusal_profile_ok: bool = False
    max_cost_in: Decimal = Decimal("0")
    max_cost_out: Decimal = Decimal("0")

    @classmethod
    def required(cls, **kw: object) -> "Capabilities":
        """Factory for the REQUIRING side; rejects a vector that constrains nothing.

        docs/01 §3 consequence 2 / §7: "Empty ``requires`` defaulting to permissive →
        silent mis-resolution; make it a validation error."
        """
        cap = cls(**kw)  # type: ignore[arg-type]
        assert_stated(cap)
        return cap

    def is_unconstrained(self) -> bool:
        return (
            self.context_floor <= 0
            and self.structured_output_conformance == 0
            and self.tool_call_conformance == 0
            and self.long_prompt_instruction_following == 0
            and not self.languages
            and not self.refusal_profile_ok
        )

    def failing_dimensions(self, required: "Capabilities") -> tuple[str, ...]:
        """Every dimension on which self fails to dominate required. Order is stable."""
        out: list[str] = []
        for attr in ("context_floor", "structured_output_conformance",
                     "tool_call_conformance", "long_prompt_instruction_following"):
            mine, theirs = getattr(self, attr), getattr(required, attr)
            if mine < theirs:                     # capability: higher dominates
                out.append(f"{attr} {mine}<{theirs}")
        for attr in ("max_cost_in", "max_cost_out"):
            mine, theirs = getattr(self, attr), getattr(required, attr)
            if mine > theirs:                     # price: LOWER dominates (rate vs. ceiling)
                out.append(f"{attr} {mine}>{theirs}")
        if required.languages - self.languages:
            out.append(f"languages missing {sorted(required.languages - self.languages)}")
        if required.refusal_profile_ok and not self.refusal_profile_ok:
            out.append("refusal_profile_ok")
        return tuple(out)

    def satisfies(self, required: "Capabilities") -> bool:
        """Dimension-wise domination — NOT ``self.rank >= required.rank``.

        docs/01 §3: capability is a PARTIAL order. A tier with a larger context window and
        weaker structured-output conformance neither dominates nor is dominated by its
        neighbour. Every dimension must hold independently: there is no averaging, and no
        dimension may be traded against another. This is the single reason
        ``next_satisfying_tier`` can skip a rank or fail outright.
        """
        return not self.failing_dimensions(required)


def assert_stated(required: Capabilities) -> None:
    # docs/01 §3 consequence 2: a node with unstated requirements gets silently
    # mis-resolved, so an unconstrained requirement vector is a validation error.
    if required.is_unconstrained():
        raise UnstatedRequirements(
            "requires is unconstrained; docs/01 §3 makes an empty capability vector a "
            "validation error rather than a permissive default"
        )


# --- Contract / binding / pin --------------------------------------------- #
@dataclass(frozen=True)
class TierContract:
    """Floors on capability, ceilings on price, and no vendor name (docs/01 §2)."""

    tier: Tier
    provides: Capabilities
    cost_ceiling_in: Decimal
    cost_ceiling_out: Decimal

    @property
    def name(self) -> str:
        return self.tier.name.lower()

    @property
    def rank(self) -> int:
        return self.tier.rank

    def satisfies(self, required: Capabilities) -> bool:
        return self.provides.satisfies(required)

    def expected_cost(self, profile: TokenProfile) -> Decimal:
        """docs/01 §3: candidates are ranked by expected cost on the node's own profile."""
        return profile.cost_per_doc(self.tier)


_ALIAS_TOKENS = frozenset({"latest", "stable", "current", "preview", "ga", "head", "prod"})


@dataclass(frozen=True)
class Binding:
    """tier → provider · model_id · EXACT version (docs/01 §4)."""

    tier: Tier
    provider: str
    model_id: str
    version: Optional[str]

    def __post_init__(self) -> None:
        # docs/01 §7 anti-pattern: "Binding to a provider alias (…-latest) — the provider
        # re-points your tier without your consent; tier drift you cannot detect."
        # docs/01 §8 q3: "Is any binding an alias rather than an exact version?"
        if self.version is None or not str(self.version).strip():
            raise AliasBindingError(
                f"{self.provider}/{self.model_id} has version={self.version!r}; a missing "
                "version is a moving alias by another name (docs/01 §7)"
            )
        for field_name, raw in (("model_id", self.model_id), ("version", str(self.version))):
            low = raw.lower()
            if "*" in low:
                raise AliasBindingError(f"{field_name}={raw!r} contains a wildcard (docs/01 §7)")
            tokens = {t for t in "".join(c if c.isalnum() else " " for c in low).split() if t}
            hit = tokens & _ALIAS_TOKENS
            if hit:
                raise AliasBindingError(
                    f"{field_name}={raw!r} names the moving alias {sorted(hit)[0]!r}; "
                    "bind to an exact version (docs/01 §7)"
                )
        if not any(c.isdigit() for c in str(self.version)):
            raise AliasBindingError(
                f"version={self.version!r} has no digits, so it is a channel name and not a "
                "version; the provider can move it under you (docs/01 §7)"
            )

    def __str__(self) -> str:
        return f"{self.provider}/{self.model_id}@{self.version}"


@dataclass(frozen=True)
class Pin:
    """A pipeline-scoped binding override, over a moving fleet default (docs/01 §5)."""

    pipeline: str
    binding: Binding
    granted_at: datetime
    expires_at: datetime
    reason: str
    renewals: int = 0

    def __post_init__(self) -> None:
        for label, ts in (("granted_at", self.granted_at), ("expires_at", self.expires_at)):
            if ts.tzinfo is None:
                raise PinError(f"{label} must be timezone-aware UTC")
        # docs/01 §5: "Renewal requires naming the failing eval, which converts silent debt
        # into a tracked item." A pin whose reason is blank is untracked debt.
        if not self.reason.strip():
            raise PinError(
                f"pin on {self.pipeline!r} states no reason; docs/01 §5 requires naming the "
                "failing eval so the debt is tracked rather than silent"
            )
        if self.expires_at <= self.granted_at:
            raise PinError("a pin that expires at or before it is granted is a typo, not a pin")
        # docs/01 §5: "Pins expire (90 days, renewable once)" — the row the doc says to
        # defend hardest. "A pin without an expiry is not a pin, it is a fork."
        lifetime = self.expires_at - self.granted_at
        if lifetime > timedelta(days=MAX_PIN_DAYS):
            raise PinError(
                f"pin on {self.pipeline!r} lives {lifetime.days}d > {MAX_PIN_DAYS}d; "
                "an unexpiring pin is a fork wearing a pin's clothing (docs/01 §5, §7)"
            )
        if self.renewals > MAX_PIN_RENEWALS:
            raise PinError(
                f"pin on {self.pipeline!r} renewed {self.renewals}x; docs/01 §5 allows "
                f"{MAX_PIN_RENEWALS}"
            )

    def active_at(self, now: datetime) -> bool:
        return self.granted_at <= now < self.expires_at

    def days_remaining(self, now: datetime) -> int:
        return (self.expires_at - now).days

    def in_warning_window(self, now: datetime) -> bool:
        return self.active_at(now) and self.days_remaining(now) <= PIN_WARNING_DAYS

    def renew(self, reason: str, now: datetime, ttl_days: int = MAX_PIN_DAYS) -> "Pin":
        return replace(
            self, granted_at=now, expires_at=now + timedelta(days=ttl_days),
            reason=reason, renewals=self.renewals + 1,
        )


# --- What resolution needs from a node ------------------------------------- #
class NodeLike(Protocol):
    """Structural type satisfied by ``blast_radius.ScoredNode``.

    ``tier_floor`` is an attribute here but is COMPUTED from the DAG in blast_radius
    (docs/02 §8 limitation 5: floors are recomputed, never stored in the design's sense).
    """

    name: str
    tier_floor: Tier
    requires: Capabilities
    token_profile: TokenProfile


@dataclass(frozen=True)
class Resolution:
    node: str
    tier: Tier
    binding: Binding
    source: str      # "pin" | "fleet_default"
    note: str = ""

    def __str__(self) -> str:
        tail = f" ({self.note})" if self.note else ""
        return f"{self.node}: {self.tier} via {self.source} -> {self.binding}{tail}"


# --- The registry (docs/01 §4) -------------------------------------------- #
class TierRegistry:
    """Contracts, the fleet default per tier, pipeline pins, and the dependency graph."""

    def __init__(self, contracts: Iterable[TierContract]) -> None:
        self.contracts: dict[Tier, TierContract] = {c.tier: c for c in contracts}
        self._fleet_default: dict[Tier, Binding] = {}
        self._pins: dict[tuple[str, Tier], Pin] = {}
        self._deps: dict[Tier, dict[str, set[str]]] = {}

    # -- bindings ----------------------------------------------------------- #
    def set_fleet_default(self, binding: Binding) -> None:
        if binding.tier not in self.contracts:
            raise LookupError(f"no contract for tier {binding.tier}")
        self._fleet_default[binding.tier] = binding

    def fleet_default(self, tier: Tier) -> Binding:
        # docs/01 §5 fail-safe row: "Expiry falls back to *default*, never to nothing."
        # A tier with no conformance-passed default is an unresolvable runtime binding.
        if tier not in self._fleet_default:
            raise LookupError(
                f"tier {tier} has no fleet default; docs/01 §5 requires pin expiry to fall "
                "back to a conformance-passed default, so this state is unshippable"
            )
        return self._fleet_default[tier]

    def add_pin(self, pin: Pin) -> None:
        self._pins[(pin.pipeline, pin.binding.tier)] = pin

    def pin_for(self, pipeline: str, tier: Tier) -> Optional[Pin]:
        return self._pins.get((pipeline, tier))

    # -- dependency graph: tier -> nodes -> pipelines (docs/01 §4, docs/07) -- #
    def register_dependency(self, tier: Tier, node: str, pipeline: str) -> None:
        self._deps.setdefault(tier, {}).setdefault(node, set()).add(pipeline)

    def dependents(self, tier: Tier) -> tuple[tuple[str, tuple[str, ...]], ...]:
        graph = self._deps.get(tier, {})
        return tuple((n, tuple(sorted(p))) for n, p in sorted(graph.items()))

    def repoint_blast_radius(self, tier: Tier) -> dict[str, int]:
        """docs/07: how many nodes and pipelines a re-point of this tier touches."""
        graph = self._deps.get(tier, {})
        return {"nodes": len(graph), "pipelines": len({p for ps in graph.values() for p in ps})}

    # -- the satisfies-check (docs/01 §3) ----------------------------------- #
    def satisfying(self, node: NodeLike, *, at_or_above: Optional[Tier] = None
                   ) -> list[TierContract]:
        floor = node.tier_floor if at_or_above is None else at_or_above
        assert_stated(node.requires)
        return [c for c in self.contracts.values()
                if c.rank >= floor.rank and c.satisfies(node.requires)]

    def resolve(self, node: NodeLike, pipeline: str, now: datetime) -> Resolution:
        """Cheapest tier that satisfies every requirement AND meets the node's floor.

        This is docs/01 §3's code block, with the two consequences it names enforced:
        candidates are filtered by the capability VECTOR (not by rank), and the empty case
        raises ``NoSatisfyingTier`` instead of quietly returning the cheapest tier.
        """
        candidates = self.satisfying(node)
        if not candidates:
            failures = {
                c.name: c.provides.failing_dimensions(node.requires)
                for c in sorted(self.contracts.values(), key=lambda c: c.rank)
                if c.rank >= node.tier_floor.rank
            }
            raise NoSatisfyingTier(node.name, node.tier_floor, failures)
        tier = min(candidates, key=lambda c: (c.expected_cost(node.token_profile), c.rank)).tier
        pin = self.pin_for(pipeline, tier)
        if pin is not None and pin.active_at(now):
            note = f"expires in {pin.days_remaining(now)}d: {pin.reason}"
            if pin.in_warning_window(now):
                note = "T-30 WARNING, " + note
            return Resolution(node.name, tier, pin.binding, "pin", note)
        note = "pin expired -> fleet default (docs/01 §5)" if pin is not None else ""
        return Resolution(node.name, tier, self.fleet_default(tier), "fleet_default", note)


def next_satisfying_tier(node: NodeLike, current: Tier,
                         registry: Optional[TierRegistry] = None) -> Optional[Tier]:
    """The next tier BY RANK THAT ALSO SATISFIES — docs/01 §3 consequence 1.

    Returns ``None`` when nothing above ``current`` satisfies the node, and may skip a rank
    when the intervening tier is not dominant on some dimension. This is why docs/04's
    ladder walks *this* function rather than incrementing a rank.
    """
    registry = registry if registry is not None else LEDGERLINE_REGISTRY
    assert_stated(node.requires)
    higher = [c for c in registry.contracts.values()
              if c.rank > current.rank and c.satisfies(node.requires)]
    return min(higher, key=lambda c: c.rank).tier if higher else None


# --- The Ledgerline registry (docs/00 §3 rates + docs/01 §2 contract shape) - #
def _contract(tier: Tier, ctx: int, struct: str, tool: str, instr: str,
              langs: frozenset[str], refusal_ok: bool) -> TierContract:
    return TierContract(
        tier=tier,
        provides=Capabilities(
            context_floor=ctx,
            structured_output_conformance=Decimal(struct),
            tool_call_conformance=Decimal(tool),
            long_prompt_instruction_following=Decimal(instr),
            languages=langs,
            refusal_profile_ok=refusal_ok,
            max_cost_in=tier.rate_in,
            max_cost_out=tier.rate_out,
        ),
        cost_ceiling_in=tier.rate_in,
        cost_ceiling_out=tier.rate_out,
    )


def default_registry(*, with_doc_pin: bool = False) -> TierRegistry:
    """Five tiers with a >=3x price gap and a separating conformance dimension (docs/01 §6).

    Note two deliberate non-monotonicities, both taken from docs/01 §2-§3:
      * ``small`` has HIGHER structured-output conformance than ``mid`` — docs/00 §3 scopes
        it to "narrow schema-constrained extraction" while ``mid`` is "general drafting".
        A node needing >=0.995 conformance therefore escalates small -> large, SKIPPING mid.
      * ``frontier`` fails ``refusal_profile_ok`` — docs/01 §2's last row: "a more cautious
        model reads an aggressive limitation-of-liability clause and declines to summarise
        it. Higher tier, worse outcome." Escalation off ``large`` therefore FAILS.
    """
    reg = TierRegistry([
        _contract(Tier.NANO, 16_000, "0.900", "0.850", "0.800", LANG_6, True),
        _contract(Tier.SMALL, 32_000, "0.996", "0.970", "0.930", LANG_24, True),
        _contract(Tier.MID, 200_000, "0.992", "0.990", "0.960", LANG_24, True),
        _contract(Tier.LARGE, 200_000, "0.998", "0.995", "0.975", LANG_24, True),
        _contract(Tier.FRONTIER, 400_000, "0.999", "0.998", "0.990", LANG_32, False),
    ])
    for tier, provider, model, version in (
        (Tier.NANO, "vendor-z", "vendor-z-1", "2026-02-11"),
        (Tier.SMALL, "vendor-y", "vendor-y-lite-4", "2026-05-19"),
        (Tier.MID, "vendor-y", "vendor-y-2", "2026-06-01"),      # docs/01 §1 / §5
        (Tier.LARGE, "vendor-y", "vendor-y-pro-3", "2026-06-01"),
        (Tier.FRONTIER, "vendor-w", "vendor-w-max-1", "2026-04-22"),
    ):
        reg.set_fleet_default(Binding(tier, provider, model, version))
    if with_doc_pin:
        # docs/01 §5's mermaid, to the day: mid -> vendor-x-3@2026-01-15, expires 2026-09-15.
        reg.add_pin(Pin(
            pipeline="ledgerline",
            binding=Binding(Tier.MID, "vendor-x", "vendor-x-3", "2026-01-15"),
            granted_at=datetime(2026, 6, 17, tzinfo=UTC),
            expires_at=datetime(2026, 9, 15, tzinfo=UTC),
            reason="synthesize eval LG-SYNTH-07 regresses 4 pts on vendor-y-2",
        ))
    return reg


LEDGERLINE_REGISTRY = default_registry()


# --------------------------------------------------------------------------- #
def main() -> int:  # smoke test: `python3 tiers.py`
    from blast_radius import LEDGERLINE, score  # local import: blast_radius -> tiers only

    reg = default_registry(with_doc_pin=True)
    now = datetime(2026, 8, 19, tzinfo=UTC)
    scored = score(LEDGERLINE)

    print("tier      rank  ctx      struct  tool   instr  langs  refusal  $in/$out")
    for c in sorted(reg.contracts.values(), key=lambda c: c.rank):
        p = c.provides
        print(f"{c.name:9s} {c.rank:4d}  {p.context_floor:<8,} {p.structured_output_conformance} "
              f" {p.tool_call_conformance}  {p.long_prompt_instruction_following}  "
              f"{len(p.languages):5d}  {str(p.refusal_profile_ok):7s}  "
              f"{c.cost_ceiling_in}/{c.cost_ceiling_out}")

    print("\nresolution (docs/01 §3 satisfies-check over the Ledgerline DAG):")
    # docs/00 §1: verify and redact are SHARED subgraphs bound into 10-40 pipelines, so a
    # re-point of their tier is not a Ledgerline-local decision. Registered here so
    # repoint_blast_radius() below reports something a docs/07 reviewer would recognise.
    shared = {"verify": ("ledgerline", "claimsdesk", "vendorwatch"),
              "redact": ("ledgerline", "claimsdesk", "vendorwatch", "kyc-inline")}
    for name in LEDGERLINE.order:
        node = scored[name]
        for pipe in shared.get(name, ("ledgerline",)):
            reg.register_dependency(node.tier_floor, name, pipe)
        print("  ", reg.resolve(node, "ledgerline", now))

    print("\nre-point blast radius (docs/07):")
    for tier in (Tier.SMALL, Tier.MID, Tier.LARGE):
        print(f"   {tier:<9}{reg.repoint_blast_radius(tier)}  {reg.dependents(tier)}")

    print("\nnon-monotonic escalation (docs/01 §3 consequence 1):")
    strict = replace(scored["extract"], name="strict_table_writer",
                     requires=replace(scored["extract"].requires,
                                      structured_output_conformance=Decimal("0.995")))
    for label, node, cur, why in (
        ("extract", scored["extract"], Tier.SMALL, "the ordinary case"),
        ("strict_table_writer", strict, Tier.SMALL, "SKIPS mid: mid conformance 0.992 < 0.995"),
        ("verify", scored["verify"], Tier.LARGE, "FAILS: frontier refuses adversarial clauses"),
    ):
        print(f"   {label:<20}{cur} -> {next_satisfying_tier(node, cur, reg)}   ({why})")

    print("\nexpired pin falls back to the fleet default, never to nothing (docs/01 §5):")
    print("  ", reg.resolve(scored["synthesize"], "ledgerline", datetime(2026, 10, 1, tzinfo=UTC)))
    return 0


if __name__ == "__main__":
    # Re-import under the canonical name before running: `python3 tiers.py` would otherwise
    # load this file as __main__ AND blast_radius would load it again as `tiers`, giving two
    # distinct Tier enums whose members are unequal as dict keys.
    import tiers

    sys.exit(tiers.main())
