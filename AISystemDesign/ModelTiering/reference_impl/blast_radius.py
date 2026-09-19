"""
Ledgerline — the blast-radius rubric, scored over the real DAG (docs/02).

Implements docs/02-blast-radius-tiering.md: the A / D / R bands with §2's exact thresholds,
and ``tier_floor`` as the MAX of what amplification demands and what (detectability,
reversibility) demands (§1: "independent reasons, not weights to be averaged").

Two things the doc explicitly asks the reference implementation to make structural:

  * **A is computed FROM THE DAG**, never stored. ``amplification`` walks the transitive
    successors and divides their baseline spend by the node's own, so a topology change
    re-scores automatically (§8 limitation 5: "the reference implementation recomputes
    floors from the DAG rather than storing them").
  * **D is derived from DECLARED DETECTORS**, per error class. §6 point 1: "The conditional
    must be enforced, not documented ... removing the coverage check *mechanically* raises
    the floor rather than leaving a stale comment behind." Removing ``risk_flag``'s
    ``playbook_coverage_check`` moves its floor from ``small`` to ``large`` here, in code.

TWO DISCREPANCIES IN DOC 02 THAT THIS FILE RESOLVES RATHER THAN HIDES — see §5/§6 notes
printed by ``main()``:

  1. §2C gives an R-only floor table (LOW→none, MED→mid, HIGH→large). Applied as an
     independent third term it would force ``risk_flag`` (R=MED per §5) to ``mid``, which
     contradicts §5's ``small*`` and breaks docs/00 §6's $0.5175 arithmetic. §2B is the
     joint (D,R) table and subsumes §2C, so ``tier_floor`` maxes exactly two terms.
     ``floor_from_reversibility`` is kept as a printed diagnostic so the gap is visible.
  2. §5's R column is the reversibility GIVEN the node's declared detectors hold
     (``risk_flag`` = MED). §6's diagram gives R for the same node when the omission class
     is uncovered (HIGH). Both are modelled; ``effective_reversibility`` picks by coverage.

Nothing here calls a model.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional

from tiers import Capabilities, LANG_24, Tier, TokenProfile

ZERO = Decimal("0")
BASELINE_TIER = Tier.MID       # docs/00 §5: "everything on mid" is the costing baseline


# --- The three bands, with docs/02 §2's exact thresholds ------------------- #
class Amplification(Enum):
    """A = downstream spend / this node's spend (docs/02 §2A)."""

    CONTAINED = "<2x"            # "an error costs about what the node costs" -> no floor
    WASTES_MONEY = "2-20x"       # -> mid
    WASTES_DOCUMENT = ">20x"     # -> large

    @staticmethod
    def classify(a: Decimal) -> "Amplification":
        if a < 2:
            return Amplification.CONTAINED
        if a <= 20:
            return Amplification.WASTES_MONEY
        return Amplification.WASTES_DOCUMENT


class Detectability(Enum):
    """D = fraction of this node's errors caught before delivery (docs/02 §2B)."""

    HIGH = ">=95%"
    MEDIUM = "60-95%"
    LOW = "<60%"

    @staticmethod
    def classify(d: Decimal) -> "Detectability":
        if d >= Decimal("0.95"):
            return Detectability.HIGH
        if d >= Decimal("0.60"):
            return Detectability.MEDIUM
        return Detectability.LOW


class Reversibility(Enum):
    """R = what escape costs (docs/02 §2C)."""

    LOW = "re-run the node"
    MED = "re-run the document"
    HIGH = "reaches the customer"

    @property
    def reaches_customer(self) -> bool:
        """docs/02 §2B splits on exactly this: "re-run only" vs. "reaches the customer"."""
        return self is Reversibility.HIGH


# --- Detectors: D is a design variable, not a measurement (docs/02 §4) ----- #
@dataclass(frozen=True)
class Detector:
    """One declared check, the error classes it covers, and its measured coverage."""

    name: str
    covers: frozenset[str]
    coverage: Decimal
    cost_per_call: Decimal = ZERO   # docs/02 §4: a coverage check costs O(1) or nano rates


DETECTORS: Mapping[str, Detector] = MappingProxyType({
    d.name: d for d in (
        # Deterministic JSON-schema validation: cheap and near-total on malformed rows.
        Detector("schema_validator", frozenset({"malformed_row"}), Decimal("0.999")),
        # docs/02 §5: extract's measured D is 92%. A single row is only visible to the
        # verifier through the memo, which is why it scores below the whole-memo check.
        Detector("verify_span_grounding", frozenset({"unsupported_claim"}), Decimal("0.92")),
        # docs/02 §5: synthesize's measured D is 95% — the verifier reads the memo whole.
        Detector("verify_memo_grounding",
                 frozenset({"unsupported_claim", "bad_judgement"}), Decimal("0.95")),
        Detector("classify_confidence_margin", frozenset({"wrong_schema"}), Decimal("0.50")),
        Detector("segment_boundary_overlap", frozenset({"boundary_omission"}), Decimal("0.55")),
        Detector("human_review_sample", frozenset({"bad_judgement"}), Decimal("0.92")),
        # docs/02 §6: THE conditional detector. "every rule evaluated against every clause",
        # deterministic, ~$0.024/doc against a $0.315/doc tier-up it makes unnecessary.
        Detector("playbook_coverage_check", frozenset({"rule_omission"}), Decimal("0.97"),
                 Decimal("0.0002")),
        Detector("redact_rule_replay", frozenset({"missed_redaction"}), Decimal("0.40")),
    )
})


@dataclass(frozen=True)
class NodeSpec:
    """One model node. ``detectors`` is a declaration; D is derived from it, never given."""

    name: str
    token_profile: TokenProfile
    error_classes: frozenset[str]
    detectors: frozenset[str]
    reversibility: Reversibility               # docs/02 §5's R column (detectors holding)
    requires: Capabilities
    reversibility_if_undetected: Optional[Reversibility] = None   # docs/02 §6's diagram

    def __post_init__(self) -> None:
        # docs/01 §3 consequence 2, restated in docs/02 §9's cold-start policy: a node that
        # does not state what it needs cannot be resolved, so this is a construction error.
        if self.requires.is_unconstrained():
            raise ValueError(f"node {self.name!r} has an unconstrained `requires` (docs/01 §3)")
        if not self.error_classes:
            raise ValueError(
                f"node {self.name!r} declares no error classes; docs/02 §9 scores D per error "
                "class, so a node with none would score D=1 by vacuum"
            )
        unknown = self.detectors - set(DETECTORS)
        if unknown:
            raise ValueError(f"node {self.name!r} declares unknown detector(s) {sorted(unknown)}")

    @property
    def calls_per_doc(self) -> int:
        return self.token_profile.calls_per_doc

    def without_detector(self, name: str) -> "NodeSpec":
        """docs/02 §6 point 1, executable: drop a detector and watch the floor move."""
        return replace(self, detectors=self.detectors - {name})


@dataclass(frozen=True)
class Dag:
    """The pipeline topology as DATA, so A is a graph property rather than a constant."""

    nodes: Mapping[str, NodeSpec]
    edges: Mapping[str, tuple[str, ...]]
    order: tuple[str, ...]

    def downstream(self, name: str) -> tuple[str, ...]:
        """Transitive successors. Cycle-safe, and deliberately forward-only.

        docs/00 §2's ``verify --reject--> synthesize`` edge is a RETRY, not an amplification
        path: it is priced as the 4% memo re-run in economics.py. Including it here would
        make every node downstream of every other and collapse the rubric.
        """
        seen: set[str] = set()
        stack = list(self.edges.get(name, ()))
        while stack:
            cur = stack.pop()
            if cur in seen or cur == name:
                continue
            seen.add(cur)
            stack.extend(self.edges.get(cur, ()))
        return tuple(n for n in self.order if n in seen)

    def node_cost_per_doc(self, name: str, tier: Tier = BASELINE_TIER) -> Decimal:
        return self.nodes[name].token_profile.cost_per_doc(tier)

    def downstream_cost_per_doc(self, name: str, tier: Tier = BASELINE_TIER) -> Decimal:
        return sum((self.node_cost_per_doc(n, tier) for n in self.downstream(name)), ZERO)

    def total_cost_per_doc(self, tier: Tier = BASELINE_TIER) -> Decimal:
        return sum((self.node_cost_per_doc(n, tier) for n in self.order), ZERO)


# --- The scoring functions ------------------------------------------------- #
def amplification(node: NodeSpec, dag: Dag) -> Decimal:
    """A = downstream spend / this node's spend, both at the docs/00 §5 baseline.

    Computed from the DAG on every call (docs/02 §8 limitation 5 / §10 q1: "what is A, and
    was it computed from the DAG or guessed?"). Per-doc and per-call forms give the same
    ratio, which is why the fan-out rows in §5 read $0.0008 / $0.0035 and still score 0.23x.
    """
    own = dag.node_cost_per_doc(node.name)
    if own == 0:
        raise ZeroDivisionError(f"node {node.name!r} costs nothing; A is undefined")
    return dag.downstream_cost_per_doc(node.name) / own


def uncovered_error_classes(node: NodeSpec) -> tuple[str, ...]:
    declared = [DETECTORS[d] for d in node.detectors]
    return tuple(sorted(c for c in node.error_classes
                        if not any(c in d.covers for d in declared)))


def detectability(node: NodeSpec) -> Decimal:
    """D from DECLARED detectors, as the MINIMUM over the node's error classes.

    Two doc requirements are structural in this one expression:

      * docs/02 §9: "Assume D = 0 until a detector is declared." An error class with no
        declared detector contributes 0, so the min collapses to 0 and the floor rises.
      * docs/02 §6 point 2: "The coverage check verifies process, not judgement ... That
        residual is real." A node's D is set by its WEAKEST covered class, not its best
        detector — which is why risk_flag scores 0.92 (judgement) and not 0.97 (coverage),
        and why adding a strong detector never launders a weak one.
    """
    declared = [DETECTORS[d] for d in node.detectors]
    per_class = [
        max((d.coverage for d in declared if cls in d.covers), default=ZERO)
        for cls in sorted(node.error_classes)
    ]
    return min(per_class) if per_class else ZERO


def effective_reversibility(node: NodeSpec) -> Reversibility:
    """docs/02 §5's R when every error class is covered; docs/02 §6's R when one is not.

    R is defined as "cost if it escapes", so it is conditional on what can escape. With the
    coverage check, a missed playbook rule is caught in-pipeline and localised to a
    (rule, clause) pair -> re-run the document (MED). Without it the miss ships -> HIGH,
    which is exactly what §6's false-negative branch asserts.
    """
    if uncovered_error_classes(node) and node.reversibility_if_undetected is not None:
        return node.reversibility_if_undetected
    return node.reversibility


def floor_from_amplification(a: Decimal) -> Tier:
    """docs/02 §2A: <2x none, 2-20x mid, >20x large."""
    band = Amplification.classify(a)
    return {
        Amplification.CONTAINED: Tier.NANO,          # "none" = no floor above the cheapest
        Amplification.WASTES_MONEY: Tier.MID,
        Amplification.WASTES_DOCUMENT: Tier.LARGE,
    }[band]


def floor_from_detect_reversibility(d: Decimal, r: Reversibility) -> Tier:
    """docs/02 §2B's joint table, verbatim."""
    band = Detectability.classify(d)
    if band is Detectability.HIGH:
        return Tier.NANO                                    # ">=95% | anything | none"
    if band is Detectability.MEDIUM:
        return Tier.LARGE if r.reaches_customer else Tier.SMALL
    return Tier.LARGE if r.reaches_customer else Tier.MID   # "<60% | re-run only | mid"


def floor_from_reversibility(r: Reversibility) -> Tier:
    """docs/02 §2C's R-only table, kept as a DIAGNOSTIC and not as a third max term.

    Applying it independently forces risk_flag (R=MED per §5) to ``mid``, contradicting
    §5's ``small*`` and docs/00 §6's $0.5175. §2B already conditions on R, so C is
    subsumed. main() prints both columns so the divergence is visible, not silent.
    """
    return {Reversibility.LOW: Tier.NANO,
            Reversibility.MED: Tier.MID,
            Reversibility.HIGH: Tier.LARGE}[r]


def tier_floor(node: NodeSpec, dag: Dag) -> Tier:
    """max(floor demanded by A, floor demanded by (D, R)) — docs/02 §1."""
    return max(
        floor_from_amplification(amplification(node, dag)),
        floor_from_detect_reversibility(detectability(node), effective_reversibility(node)),
    )


@dataclass(frozen=True)
class ScoredNode:
    """Satisfies ``tiers.NodeLike``: the floor travels with the node, freshly computed."""

    name: str
    token_profile: TokenProfile
    requires: Capabilities
    tier_floor: Tier
    amplification: Decimal
    detectability: Decimal
    reversibility: Reversibility
    detectors: frozenset[str]


def score(dag: Dag) -> dict[str, ScoredNode]:
    out: dict[str, ScoredNode] = {}
    for name in dag.order:
        n = dag.nodes[name]
        out[name] = ScoredNode(
            name=name, token_profile=n.token_profile, requires=n.requires,
            tier_floor=tier_floor(n, dag), amplification=amplification(n, dag),
            detectability=detectability(n), reversibility=effective_reversibility(n),
            detectors=n.detectors,
        )
    return out


# --- The Ledgerline DAG as data (docs/00 §2 topology, §4 token profile) ---- #
def _req(ctx: int, struct: str, tool: str, instr: str, refusal: bool = True) -> Capabilities:
    return Capabilities.required(
        context_floor=ctx,
        structured_output_conformance=Decimal(struct),
        tool_call_conformance=Decimal(tool),
        long_prompt_instruction_following=Decimal(instr),
        languages=LANG_24,          # docs/01 §2: contracts arrive in the governing language
        refusal_profile_ok=refusal,
        max_cost_in=Decimal("3.00"),   # docs/00 §3: nothing in Ledgerline justifies frontier
        max_cost_out=Decimal("15.00"),
    )


_NODES = (
    NodeSpec("classify", TokenProfile(1, 4_000, 300), frozenset({"wrong_schema"}),
             frozenset({"classify_confidence_margin"}), Reversibility.HIGH,
             _req(8_000, "0.990", "0.000", "0.900")),
    NodeSpec("segment", TokenProfile(1, 12_000, 2_000), frozenset({"boundary_omission"}),
             frozenset({"segment_boundary_overlap"}), Reversibility.HIGH,
             _req(32_000, "0.990", "0.000", "0.930")),
    NodeSpec("extract", TokenProfile(120, 1_500, 400),
             frozenset({"malformed_row", "unsupported_claim"}),
             frozenset({"schema_validator", "verify_span_grounding"}), Reversibility.LOW,
             _req(8_000, "0.990", "0.000", "0.900")),
    NodeSpec("risk_flag", TokenProfile(120, 2_000, 300),
             frozenset({"bad_judgement", "rule_omission"}),
             frozenset({"human_review_sample", "playbook_coverage_check"}), Reversibility.MED,
             _req(16_000, "0.990", "0.000", "0.930"),
             reversibility_if_undetected=Reversibility.HIGH),
    NodeSpec("synthesize", TokenProfile(1, 15_000, 3_000), frozenset({"unsupported_claim"}),
             frozenset({"verify_memo_grounding"}), Reversibility.LOW,
             _req(64_000, "0.990", "0.990", "0.950")),
    # docs/00 §2: "NOTHING CHECKS THIS" -> an empty detector set, and D falls out as 0.
    NodeSpec("verify", TokenProfile(1, 25_000, 2_000), frozenset({"false_accept"}),
             frozenset(), Reversibility.HIGH, _req(200_000, "0.990", "0.000", "0.970")),
    NodeSpec("redact", TokenProfile(1, 5_000, 5_000), frozenset({"missed_redaction"}),
             frozenset({"redact_rule_replay"}), Reversibility.HIGH,
             _req(32_000, "0.990", "0.000", "0.970")),
)

LEDGERLINE = Dag(
    nodes=MappingProxyType({n.name: n for n in _NODES}),
    edges=MappingProxyType({
        "classify": ("segment",),
        "segment": ("extract", "risk_flag"),
        "extract": ("synthesize",),
        "risk_flag": ("synthesize",),
        "synthesize": ("verify",),
        "verify": ("redact",),        # the reject edge back to synthesize is a retry, not a
        "redact": (),                 # dependency -- see Dag.downstream's docstring
    }),
    order=tuple(n.name for n in _NODES),
)

# docs/02 §5's table: (A, D as printed, R, floor). The gate main() exits non-zero on.
DOC_02_S5: Mapping[str, tuple[str, str, Reversibility, Tier]] = MappingProxyType({
    "classify": ("174", "~50%", Reversibility.HIGH, Tier.LARGE),
    "segment": ("42", "<60%", Reversibility.HIGH, Tier.LARGE),
    "extract": ("0.23", "92%", Reversibility.LOW, Tier.SMALL),
    "risk_flag": ("0.23", "contested", Reversibility.MED, Tier.SMALL),
    "synthesize": ("2.2", "95%", Reversibility.LOW, Tier.MID),
    "verify": ("0.86", "~0%", Reversibility.HIGH, Tier.LARGE),
    "redact": ("0", "~40%", Reversibility.HIGH, Tier.LARGE),
})
A_REL_TOLERANCE = Decimal("0.02")   # doc prints A at 0-2 sf; 42.5 vs "42x" is 1.2% off


def _a_matches(computed: Decimal, documented: Decimal) -> bool:
    if documented == 0:
        return computed == 0
    return abs(computed - documented) / documented <= A_REL_TOLERANCE


def main() -> int:
    dag, failures = LEDGERLINE, []
    print("docs/02 §5 — the blast-radius rubric scored over the Ledgerline DAG")
    print(f"baseline (all-{BASELINE_TIER}) pipeline cost/doc: ${dag.total_cost_per_doc():.4f}\n")
    head = (f"{'node':<11}{'calls':>6}{'$/call':>10}{'down$/call':>12}{'A':>9}{'A(doc)':>8}"
            f"{'D':>7}{'band':>8}{'R':>6}{'floor':>7}{'doc':>7}{'':>4}{'§2C':>6}")
    print(head)
    print("-" * len(head))
    for name in dag.order:
        node = dag.nodes[name]
        doc_a, doc_d, doc_r, doc_floor = DOC_02_S5[name]
        a = amplification(node, dag)
        d = detectability(node)
        r = effective_reversibility(node)
        floor = tier_floor(node, dag)
        ok = floor is doc_floor and r is doc_r and _a_matches(a, Decimal(doc_a))
        if not ok:
            failures.append(f"{name}: A={a:.2f}(doc {doc_a}) R={r.name}(doc {doc_r.name}) "
                            f"floor={floor}(doc {doc_floor})")
        print(f"{name:<11}{node.calls_per_doc:>6}"
              f"{node.token_profile.cost_per_call(BASELINE_TIER):>10.6f}"
              f"{dag.downstream_cost_per_doc(name) / node.calls_per_doc:>12.6f}"
              f"{a:>9.2f}{doc_a:>8}{d:>7.2f}{Detectability.classify(d).name:>8}"
              f"{r.name:>6}{str(floor):>7}{str(doc_floor):>7}"
              f"{'  OK' if ok else '  MISMATCH':>4}{str(floor_from_reversibility(r)):>6}")

    print("\n§2C column above is the R-only floor. Applied as an independent third term it")
    print("would force risk_flag to 'mid' and break docs/00 §6's $0.5175 — §2B subsumes it.")

    print("\ndocs/02 §6, executable: the conditional is enforced, not documented")
    rk = dag.nodes["risk_flag"]
    for label, variant in (("with playbook_coverage_check   ", rk),
                           ("without playbook_coverage_check", rk.without_detector(
                               "playbook_coverage_check"))):
        print(f"   {label}: D={detectability(variant):.2f} "
              f"R={effective_reversibility(variant).name:<4} "
              f"uncovered={list(uncovered_error_classes(variant))} "
              f"-> floor {tier_floor(variant, dag)}")

    # docs/02 §4: "Buying detectability is almost always cheaper than buying capability --
    # and the gap scales with fan-out width." The doc's two figures, recomputed.
    detector_cost = (DETECTORS["playbook_coverage_check"].cost_per_call
                     * Decimal(rk.calls_per_doc))
    tier_up_cost = (rk.token_profile.cost_per_doc(Tier.MID)
                    - rk.token_profile.cost_per_doc(Tier.SMALL))
    d_ok = detector_cost.quantize(Decimal("0.001")) == Decimal("0.024")
    t_ok = tier_up_cost.quantize(Decimal("0.0001")) == Decimal("0.3150")
    print(f"   add a detector: {rk.calls_per_doc} x "
          f"${DETECTORS['playbook_coverage_check'].cost_per_call} = ${detector_cost:.4f}/doc "
          f"(doc $0.024)  {'OK' if d_ok else 'MISMATCH'}")
    print(f"   tier up small->mid on the same 120-wide node: ${tier_up_cost:.4f}/doc "
          f"(doc $0.315)  {'OK' if t_ok else 'MISMATCH'}")
    if not (d_ok and t_ok):
        failures.append("docs/02 §4 detector-vs-tier-up arithmetic")

    print("\ndocs/02 §8 limitation 5, executable: A is a DAG property, not a constant")
    rewired = replace(dag, edges=MappingProxyType({**dag.edges, "verify": ()}))
    sy = dag.nodes["synthesize"]
    print(f"   drop the verify -> redact edge: synthesize A {amplification(sy, dag):.2f} -> "
          f"{amplification(sy, rewired):.2f} (crosses the 2x band), floor "
          f"{tier_floor(sy, dag)} -> {tier_floor(sy, rewired)} -- no stored constant to update")

    if failures:
        print("\nMISMATCH vs docs/02 §5:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nAll 7 rows reproduce docs/02 §5 (A within 2%, R and floor exact).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
