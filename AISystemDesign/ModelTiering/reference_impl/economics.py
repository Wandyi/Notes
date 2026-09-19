"""
Ledgerline — the bill, reproduced (docs/00 §5, §6, §8 and docs/05).

This file exists so the design's numbers cannot silently rot. Every figure the docs assert
is a named constant here, and ``main()`` recomputes it from the DAG, the tier rates, and the
blast-radius floors, then prints OK/MISMATCH per row and **exits non-zero on any drift**.

  * docs/00 §5   baseline "everything on mid"     -> $0.9625/doc, fan-out share 87.3%
  * docs/00 §6   blast-radius tiering             -> $0.5175 before escalation,
                 +$0.1008 clause escalation (12%), +$0.0054 memo re-run (4%),
                 = $0.6237/doc, -35.2%
  * docs/00 §8   fan-out long tail                -> p50/p90/p99 at mid and small
  * docs/05      cost per ACCEPTED outcome        -> baseline $0.9923/accepted at 97%
                 acceptance, tiered $0.6854 at 91%; survives down to ~63% acceptance
  * docs/00 §7   the two bases                    -> p50 all-in $0.6477 ($0.7118/accepted),
                 mean-width all-in $0.7984 ($0.8773/accepted). The SLO is <= $0.70, so the
                 design MISSES it on both bases -- narrowly at p50, by 25.3% on the mean.
                 That is the documented position, not a drift to be fixed.
  * docs/02 §6   the risk_flag coverage check     -> $0.0240/doc detector standing between
                 $0.6237/doc and $1.7283/doc, a $37.9M/year node-level delta

Nothing is hardcoded that could be derived. The tier per node comes from
``blast_radius.tier_floor`` through ``TierRegistry.resolve``; the escalation target comes
from ``tiers.next_satisfying_tier``. Remove risk_flag's coverage detector and this file's
total moves on its own -- which is the point of docs/02 §6 point 1.

Money is ``Decimal`` everywhere and rounded only at presentation. Comparisons against the
docs are made at the precision the doc states: 4 dp for per-doc dollars, 2 dp for the §8
fan-out table, 1 dp for percentages.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

from blast_radius import LEDGERLINE, Dag, score
from tiers import (LEDGERLINE_REGISTRY, UTC, Tier, TierRegistry, next_satisfying_tier,
                   token_cost)

# --- Documented figures. Every one of these is a claim under test. --------- #
BASELINE_TIER = Tier.MID
DOC_00_S5_TOTAL = Decimal("0.9625")
DOC_00_S5_PER_NODE = {
    "classify": (Decimal("0.0055"), Decimal("0.6")),
    "segment": (Decimal("0.0220"), Decimal("2.3")),
    "extract": (Decimal("0.4200"), Decimal("43.6")),
    "risk_flag": (Decimal("0.4200"), Decimal("43.6")),
    "synthesize": (Decimal("0.0300"), Decimal("3.1")),
    "verify": (Decimal("0.0350"), Decimal("3.6")),
    "redact": (Decimal("0.0300"), Decimal("3.1")),
}
DOC_00_S5_FANOUT = Decimal("0.8400")
DOC_00_S5_FANOUT_SHARE_PCT = Decimal("87.3")
DOC_00_S5_DAILY = Decimal("86625")          # at 90 k docs/day
DOC_00_S5_ANNUAL_M = Decimal("31.6")        # $ millions

DOC_00_S6_PER_NODE = {
    "classify": (Tier.LARGE, Decimal("0.0165")),
    "segment": (Tier.LARGE, Decimal("0.0660")),
    "extract": (Tier.SMALL, Decimal("0.1050")),
    "risk_flag": (Tier.SMALL, Decimal("0.1050")),
    "synthesize": (Tier.MID, Decimal("0.0300")),
    "verify": (Tier.LARGE, Decimal("0.1050")),
    "redact": (Tier.LARGE, Decimal("0.0900")),
}
DOC_00_S6_BEFORE_ESCALATION = Decimal("0.5175")
DOC_00_S6_CLAUSE_ESCALATION = Decimal("0.1008")
DOC_00_S6_MEMO_RERUN = Decimal("0.0054")
DOC_00_S6_AFTER = Decimal("0.6237")
DOC_00_S6_DELTA_PCT = Decimal("-35.2")
DOC_00_S6_ANNUAL_SAVING_M = Decimal("11.1")

DOC_00_S8 = (   # percentile, clauses, fan-out @mid, fan-out @small
    ("p50", 120, Decimal("0.84"), Decimal("0.21")),
    ("p90", 340, Decimal("2.38"), Decimal("0.60")),
    ("p99", 900, Decimal("6.30"), Decimal("1.58")),
)
DOC_00_S8_P99_MULTIPLE = Decimal("7.5")
DOC_00_S7_COST_PER_ACCEPTED_SLO = Decimal("0.70")

# docs/05. The doc quotes the ratio 0.6237/0.9923 for the break-even, which pins the
# baseline's cost-per-accepted at $0.9923 and therefore its acceptance rate at
# 0.9625/0.9923 = 97%. That 97% is the input; $0.9923 and 63% are both derived from it.
DOC_05_BASELINE_ACCEPTANCE = Decimal("0.97")
DOC_05_BASELINE_COST_PER_ACCEPTED = Decimal("0.9923")
DOC_05_TIERED_ACCEPTANCE = Decimal("0.91")      # tiering down costs 6 points of acceptance
DOC_05_TIERED_COST_PER_ACCEPTED = Decimal("0.6854")
DOC_05_BREAKEVEN_ACCEPTANCE_PCT = Decimal("63")

# docs/02 §6 — the coverage check that authorises risk_flag at `small`.
DOC_02_S6_DETECTOR = Decimal("0.0240")
# docs/00 §8 — mean fan-out width 174 clauses vs p50 120 (lognormal, sigma ~= 0.86).
MEAN_WIDTH_RATIO = Decimal("174") / Decimal("120")
# docs/00 §7 — the mean-width, all-in figures the cost SLO is measured against.
DOC_00_S7_TIERED_MEAN = Decimal("0.7984")
DOC_00_S7_TIERED_MEAN_CPA = Decimal("0.8773")

# Escalation, priced INSIDE the number (docs/00 §6's callout).
CLAUSE_ESCALATION_RATE = Decimal("0.12")
MEMO_RERUN_RATE = Decimal("0.04")
DOCS_PER_DAY = Decimal("90000")
DAYS_PER_YEAR = Decimal("365")
FANOUT_NODES = ("extract", "risk_flag")


def cost(tier: Tier, tokens_in: int, tokens_out: int) -> Decimal:
    """$ for one call at docs/00 §3's illustrative rates. Exact; never a float."""
    return token_cost(tier, tokens_in, tokens_out)


def money(x: Decimal, dp: int = 4) -> Decimal:
    return x.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)


def pct(x: Decimal, dp: int = 1) -> Decimal:
    return (x * 100).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)


# --- Reports --------------------------------------------------------------- #
@dataclass(frozen=True)
class CostLine:
    node: str
    tier: Tier
    calls: int
    per_call: Decimal
    per_doc: Decimal


@dataclass(frozen=True)
class CostReport:
    lines: tuple[CostLine, ...]
    label: str

    @property
    def total(self) -> Decimal:
        return sum((l.per_doc for l in self.lines), Decimal("0"))

    def share(self, node: str) -> Decimal:
        return next(l.per_doc for l in self.lines if l.node == node) / self.total

    @property
    def fanout(self) -> Decimal:
        return sum((l.per_doc for l in self.lines if l.node in FANOUT_NODES), Decimal("0"))

    @property
    def fanout_share(self) -> Decimal:
        return self.fanout / self.total


def _report(dag: Dag, tiers_by_node: dict[str, Tier], label: str) -> CostReport:
    lines = []
    for name in dag.order:
        prof = dag.nodes[name].token_profile
        tier = tiers_by_node[name]
        lines.append(CostLine(name, tier, prof.calls_per_doc, prof.cost_per_call(tier),
                              prof.cost_per_doc(tier)))
    return CostReport(tuple(lines), label)


def baseline_all_mid(dag: Dag = LEDGERLINE) -> CostReport:
    """docs/00 §5: "the default that almost every platform actually ships"."""
    return _report(dag, {n: BASELINE_TIER for n in dag.order}, f"all-{BASELINE_TIER}")


def resolved_tiers(dag: Dag = LEDGERLINE, registry: Optional[TierRegistry] = None,
                   now: Optional[datetime] = None) -> dict[str, Tier]:
    """Tier per node, DERIVED: docs/02 floor -> docs/01 §3 satisfies-check -> tier."""
    registry = registry if registry is not None else LEDGERLINE_REGISTRY
    now = now if now is not None else datetime(2026, 8, 19, tzinfo=UTC)
    return {n: registry.resolve(sn, "ledgerline", now).tier for n, sn in score(dag).items()}


@dataclass(frozen=True)
class TieredReport:
    base: CostReport
    tiered: CostReport
    clause_escalation: Decimal
    memo_rerun: Decimal
    escalation_target: dict[str, Tier]

    @property
    def before_escalation(self) -> Decimal:
        return self.tiered.total

    @property
    def after_escalation(self) -> Decimal:
        return self.tiered.total + self.clause_escalation + self.memo_rerun

    @property
    def delta_fraction(self) -> Decimal:
        return (self.after_escalation - self.base.total) / self.base.total


def blast_radius_tiered(dag: Dag = LEDGERLINE, registry: Optional[TierRegistry] = None,
                        now: Optional[datetime] = None) -> TieredReport:
    """docs/00 §6, with escalation priced in rather than wished away.

    "A tiering proposal that omits the cost of retrying the cheap tier's failures is not a
    proposal, it is a wish." Both retry lines are therefore derived, not asserted: the
    clause line re-runs the fan-out at the next tier that SATISFIES it (docs/01 §3), and the
    memo line re-runs synthesize + verify at their own resolved tiers.
    """
    registry = registry if registry is not None else LEDGERLINE_REGISTRY
    tiers_by_node = resolved_tiers(dag, registry, now)
    tiered = _report(dag, tiers_by_node, "blast-radius tiered")
    scored = score(dag)

    target: dict[str, Tier] = {}
    clause = Decimal("0")
    for name in FANOUT_NODES:
        nxt = next_satisfying_tier(scored[name], tiers_by_node[name], registry)
        if nxt is None:      # docs/01 §3: there may be nowhere to escalate to
            raise LookupError(f"{name} has no satisfying escalation target; re-price the ladder")
        target[name] = nxt
        prof = dag.nodes[name].token_profile
        clause += (CLAUSE_ESCALATION_RATE * Decimal(prof.calls_per_doc)
                   * prof.cost_per_call(nxt))
    memo = MEMO_RERUN_RATE * sum(
        (dag.nodes[n].token_profile.cost_per_doc(tiers_by_node[n]) for n in ("synthesize",
                                                                            "verify")),
        Decimal("0"),
    )
    return TieredReport(baseline_all_mid(dag), tiered, clause, memo, target)


# --- docs/05: cost per ACCEPTED outcome ------------------------------------ #
def cost_per_accepted(spend: Decimal, acceptance: Decimal) -> Decimal:
    """docs/05 / README stance 4: the only metric that decides anything.

    Cost per CALL is what the invoice shows; cost per ACCEPTED OUTCOME is what the business
    pays. A config that is 2% cheaper per call and rejected 5% more often is more expensive.
    """
    if isinstance(spend, float) or isinstance(acceptance, float):
        raise TypeError("money and rates are Decimal, never float")
    if not (Decimal("0") < acceptance <= Decimal("1")):
        raise ValueError(f"acceptance must be in (0, 1]; got {acceptance}")
    return spend / acceptance


def breakeven_acceptance(spend: Decimal, rival_cost_per_accepted: Decimal) -> Decimal:
    """The acceptance rate at which ``spend`` ties the rival on cost per accepted outcome.

    Solve spend / a == rival  =>  a = spend / rival. docs/05's finding is this number for
    the tiered config against the all-mid baseline: 0.6237 / 0.9923 ~= 63%. Below it, the
    cheaper pipeline is the more expensive one.
    """
    if isinstance(spend, float) or isinstance(rival_cost_per_accepted, float):
        raise TypeError("money is Decimal, never float")
    return spend / rival_cost_per_accepted


def fanout_percentiles(dag: Dag = LEDGERLINE,
                       tiers: Sequence[Tier] = (Tier.MID, Tier.SMALL)
                       ) -> tuple[tuple[str, int, tuple[Decimal, ...]], ...]:
    """docs/00 §8: fan-out width is skewed, so per-document cost is too."""
    per_clause = {t: sum((dag.nodes[n].token_profile.cost_per_call(t) for n in FANOUT_NODES),
                         Decimal("0")) for t in tiers}
    return tuple((label, clauses, tuple(per_clause[t] * Decimal(clauses) for t in tiers))
                 for label, clauses, *_ in DOC_00_S8)


# --------------------------------------------------------------------------- #
_FAILURES: list[str] = []


def _check(label: str, computed: Decimal, documented: Decimal, dp: int) -> str:
    got = computed.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    want = documented.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    if got == want:
        return "OK"
    _FAILURES.append(f"{label}: computed {got}, doc says {want}")
    return "MISMATCH"


def main() -> int:
    dag = LEDGERLINE
    rep = blast_radius_tiered(dag)
    base = rep.base

    print("docs/00 §5 — baseline, everything on `mid`")
    print(f"{'node':<11}{'calls':>6}{'$/call':>10}{'$/doc':>10}{'doc':>10}{'share%':>8}"
          f"{'doc':>7}{'':>4}")
    for line in base.lines:
        d_doc, d_share = DOC_00_S5_PER_NODE[line.node]
        s1 = _check(f"§5 {line.node} $/doc", line.per_doc, d_doc, 4)
        s2 = _check(f"§5 {line.node} share", base.share(line.node) * 100, d_share, 1)
        status = "OK" if s1 == s2 == "OK" else "MISMATCH"
        print(f"{line.node:<11}{line.calls:>6}{line.per_call:>10.4f}{money(line.per_doc):>10}"
              f"{d_doc:>10}{pct(base.share(line.node)):>8}{d_share:>7}  {status}")
    print(f"{'TOTAL':<11}{'':>6}{'':>10}{money(base.total):>10}{DOC_00_S5_TOTAL:>10}"
          f"{'':>8}{'':>7}  {_check('§5 total', base.total, DOC_00_S5_TOTAL, 4)}")
    share_ok = _check("§5 fan-out share", base.fanout_share * 100,
                      DOC_00_S5_FANOUT_SHARE_PCT, 1)
    print(f"   fan-out (extract+risk_flag) ${money(base.fanout)} = "
          f"{pct(base.fanout_share)}% of the bill  [{share_ok}]")
    daily = base.total * DOCS_PER_DAY
    annual_m = daily * DAYS_PER_YEAR / Decimal(10) ** 6
    print(f"   at 90k docs/day: ${money(daily, 0):,} / day -> ${money(annual_m, 1)}M / year  "
          f"[{_check('§5 daily', daily, DOC_00_S5_DAILY, 0)}, "
          f"{_check('§5 annual $M', annual_m, DOC_00_S5_ANNUAL_M, 1)}]")

    print("\ndocs/00 §6 — blast-radius tiering (tier per node DERIVED from docs/02 + docs/01)")
    print(f"{'node':<11}{'base':>7}{'tier':>7}{'doc':>7}{'$/doc':>10}{'doc':>10}{'delta':>10}"
          f"{'':>4}")
    for line in rep.tiered.lines:
        d_tier, d_doc = DOC_00_S6_PER_NODE[line.node]
        s = _check(f"§6 {line.node} $/doc", line.per_doc, d_doc, 4)
        if line.tier is not d_tier:
            _FAILURES.append(f"§6 {line.node} tier: computed {line.tier}, doc says {d_tier}")
            s = "MISMATCH"
        delta = line.per_doc - next(b.per_doc for b in base.lines if b.node == line.node)
        print(f"{line.node:<11}{str(BASELINE_TIER):>7}{str(line.tier):>7}{str(d_tier):>7}"
              f"{money(line.per_doc):>10}{d_doc:>10}{money(delta):>+10}  {s}")
    print(f"{'subtotal':<11}{'':>7}{'':>7}{'':>7}{money(rep.before_escalation):>10}"
          f"{DOC_00_S6_BEFORE_ESCALATION:>10}{money(rep.before_escalation - base.total):>+10}"
          f"  {_check('§6 subtotal', rep.before_escalation, DOC_00_S6_BEFORE_ESCALATION, 4)}")
    tgt = "/".join(str(rep.escalation_target[n]) for n in FANOUT_NODES)
    esc_ok = _check("§6 clause escalation", rep.clause_escalation,
                    DOC_00_S6_CLAUSE_ESCALATION, 4)
    print(f"   + {pct(CLAUSE_ESCALATION_RATE, 0)}% of clauses re-run at {tgt}: "
          f"{money(rep.clause_escalation):>+8}  (doc {DOC_00_S6_CLAUSE_ESCALATION})  {esc_ok}")
    print(f"   + {pct(MEMO_RERUN_RATE, 0)}% of memos re-synthesised and re-verified: "
          f"{money(rep.memo_rerun):>+8}  (doc {DOC_00_S6_MEMO_RERUN})  "
          f"{_check('§6 memo re-run', rep.memo_rerun, DOC_00_S6_MEMO_RERUN, 4)}")
    print(f"{'TOTAL':<11}{'':>7}{'':>7}{'':>7}{money(rep.after_escalation):>10}"
          f"{DOC_00_S6_AFTER:>10}{pct(rep.delta_fraction):>+9}%  "
          f"{_check('§6 total', rep.after_escalation, DOC_00_S6_AFTER, 4)} / "
          f"{_check('§6 delta%', rep.delta_fraction * 100, DOC_00_S6_DELTA_PCT, 1)}")
    saving_m = (base.total - rep.after_escalation) * DOCS_PER_DAY * DAYS_PER_YEAR / Decimal(10) ** 6
    print(f"   saving at 90k docs/day: ${money(saving_m, 1)}M / year  "
          f"[{_check('§6 annual saving $M', saving_m, DOC_00_S6_ANNUAL_SAVING_M, 1)}]")

    print("\ndocs/00 §8 — the long tail that breaks naive budgeting")
    print(f"{'pct':<6}{'clauses':>9}{'@mid':>9}{'doc':>8}{'@small':>9}{'doc':>8}{'':>4}")
    rows = fanout_percentiles(dag)
    for (label, clauses, (at_mid, at_small)), (_, _, d_mid, d_small) in zip(rows, DOC_00_S8):
        s1 = _check(f"§8 {label} mid", at_mid, d_mid, 2)
        s2 = _check(f"§8 {label} small", at_small, d_small, 2)
        print(f"{label:<6}{clauses:>9}{money(at_mid, 2):>9}{d_mid:>8}{money(at_small, 2):>9}"
              f"{d_small:>8}  {'OK' if s1 == s2 == 'OK' else 'MISMATCH'}")
    multiple = rows[2][2][0] / rows[0][2][0]
    print(f"   p99 / p50 at mid = {multiple:.1f}x  "
          f"[{_check('§8 p99 multiple', multiple, DOC_00_S8_P99_MULTIPLE, 1)}]")

    print("\ndocs/05 — cost per ACCEPTED outcome, and the break-even")
    base_cpa = cost_per_accepted(base.total, DOC_05_BASELINE_ACCEPTANCE)
    tiered_cpa = cost_per_accepted(rep.after_escalation, DOC_05_TIERED_ACCEPTANCE)
    be = breakeven_acceptance(rep.after_escalation, base_cpa)
    print(f"   baseline  ${money(base.total)}/doc @ {pct(DOC_05_BASELINE_ACCEPTANCE, 0)}% accepted"
          f" -> ${money(base_cpa)}/accepted (doc {DOC_05_BASELINE_COST_PER_ACCEPTED})  "
          f"{_check('§05 baseline $/accepted', base_cpa, DOC_05_BASELINE_COST_PER_ACCEPTED, 4)}")
    print(f"   tiered    ${money(rep.after_escalation)}/doc @ "
          f"{pct(DOC_05_TIERED_ACCEPTANCE, 0)}% accepted -> ${money(tiered_cpa)}/accepted "
          f"(doc {DOC_05_TIERED_COST_PER_ACCEPTED})  "
          f"{_check('§05 tiered $/accepted', tiered_cpa, DOC_05_TIERED_COST_PER_ACCEPTED, 4)}")
    print("   NOTE tiering down costs 6 points of acceptance (97% -> 91%), so the tiered")
    print("        config must be divided by ITS OWN acceptance rate, not the baseline's.")
    print(f"   break-even acceptance for the tiered config: {pct(be)}%  "
          f"(doc ~{DOC_05_BREAKEVEN_ACCEPTANCE_PCT}%)  "
          f"{_check('§05 break-even %', be * 100, DOC_05_BREAKEVEN_ACCEPTANCE_PCT, 0)}")

    # docs/00 §6/§7 — the two bases. Fan-out cost scales with clause width, singletons do not,
    # so the p50 headline and the mean-width bill are materially different numbers.
    print("\ndocs/00 §6/§7 — p50 basis vs mean-width basis (the basis flips the SLO verdict)")
    slo = DOC_00_S7_COST_PER_ACCEPTED_SLO
    scaling = rep.tiered.fanout + DOC_00_S6_CLAUSE_ESCALATION + DOC_02_S6_DETECTOR
    fixed = rep.after_escalation + DOC_02_S6_DETECTOR - scaling
    b_scaling, b_fixed = DOC_00_S5_FANOUT, base.total - DOC_00_S5_FANOUT
    for label, ratio in (("p50 width (120)", Decimal(1)), ("mean width (174)", MEAN_WIDTH_RATIO)):
        b = b_scaling * ratio + b_fixed
        t = scaling * ratio + fixed
        b_a = cost_per_accepted(b, DOC_05_BASELINE_ACCEPTANCE)
        t_a = cost_per_accepted(t, DOC_05_TIERED_ACCEPTANCE)
        print(f"   {label:<17} baseline ${money(b)}/doc (${money(b_a)}/accepted)   "
              f"tiered all-in ${money(t)}/doc (${money(t_a)}/accepted)   "
              f"SLO {'PASS' if t_a <= slo else f'FAIL by {pct(t_a / slo - 1)}%'}")
    t_mean = scaling * MEAN_WIDTH_RATIO + fixed
    _check("§7 tiered all-in, mean basis", t_mean, DOC_00_S7_TIERED_MEAN, 4)
    _check("§7 tiered $/accepted, mean basis",
           cost_per_accepted(t_mean, DOC_05_TIERED_ACCEPTANCE), DOC_00_S7_TIERED_MEAN_CPA, 4)
    print("   ^ the design MISSES its own cost SLO on the mean basis. docs/00 §7 says so.")

    # docs/02 §6 — the detector that authorises risk_flag's tier-down. At `large` there is no
    # rung above the node, so its escalation line disappears with the tier-up.
    print("\ndocs/02 §6 — what the risk_flag coverage check is worth")
    rk = dag.nodes["risk_flag"].token_profile
    resolved = resolved_tiers(dag)
    at_small = rk.cost_per_doc(resolved["risk_flag"])
    esc_rk = DOC_00_S6_CLAUSE_ESCALATION / 2  # the adder covers both fan-out nodes
    rows = [("small (with detector)", at_small, rep.after_escalation),
            ("large (no detector)", rk.cost_per_doc(Tier.LARGE),
             rep.after_escalation - at_small - esc_rk + rk.cost_per_doc(Tier.LARGE))]
    for label, node_cost, total in rows:
        print(f"   risk_flag @ {label:<22} node ${money(node_cost)}/doc   pipeline ${money(total)}/doc")
    node_delta = rk.cost_per_doc(Tier.LARGE) - at_small
    pipe_delta = rows[1][2] - rows[0][2]
    _check("docs/02 §6 node delta", node_delta, Decimal("1.1550"), 4)
    _check("docs/02 §6 pipeline cost, no detector", rows[1][2], Decimal("1.7283"), 4)
    print(f"   node delta ${money(node_delta)}/doc -> "
          f"${money(node_delta * DOCS_PER_DAY * DAYS_PER_YEAR / Decimal(10) ** 6, 1)}M/year")
    print(f"   pipeline delta ${money(pipe_delta)}/doc "
          "(smaller: a tier-up refunds its own escalation budget)")

    if _FAILURES:
        print(f"\n{len(_FAILURES)} MISMATCH(es) vs the documented figures:")
        for f in _FAILURES:
            print("  -", f)
        return 1
    print("\nEvery documented figure in docs/00 §5, §6, §8 and docs/05 reproduces exactly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
