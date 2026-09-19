# reference_impl — executable contracts

Dependency-free Python (3.11+, stdlib only) that makes the design's invariants *executable*
rather than aspirational, and that reproduces the docs' cost arithmetic so the numbers cannot
silently rot. This is not a running system; it is the typed skeleton the docs refer to, so a
reviewer can check the three claims the design actually rests on — that a tier is a contract
you cannot accidentally bind to an alias, that a node's tier floor is *derived* from the DAG
and its declared detectors rather than asserted in prose, and that
**$0.9625 → $0.6237/doc (−35.2%)** is arithmetic rather than a slide.

| File | What it pins down | Docs |
|---|---|---|
| [tiers.py](tiers.py) | The capability **vector** and its `satisfies` **partial order** (dimension-wise domination, not a rank comparison); `TierContract`; `Binding` with the no-moving-alias invariant; `Pin` with a mandatory reason and a 90-day ceiling; `TierRegistry.resolve` (cheapest tier ≥ floor **that satisfies**, raising rather than falling back); `next_satisfying_tier`, which can skip a rank or return `None`. | [00 §3](../docs/00-overview.md), [01](../docs/01-tier-as-contract.md) |
| [blast_radius.py](blast_radius.py) | The A / D / R bands at doc 02 §2's exact thresholds; the Ledgerline DAG as data; `amplification` computed **from the DAG**; `detectability` derived **from declared detectors, per error class**; `tier_floor` = max(A, (D,R)). Reproduces doc 02 §5's seven-row table and exits non-zero if it stops matching. | [02](../docs/02-blast-radius-tiering.md), [00 §4–§5](../docs/00-overview.md) |
| [routing.py](routing.py) | The deterministic, zero-inference feature router; pre-emptive escalation to `mid` on clause tokens / cross-refs / OCR / doc type; `EscalationLadder` (2 rungs, walking *satisfying* tiers) behind a `detectability_gate`; an `EscalationBreaker` that returns `QUEUE` instead of `ESCALATE` once the escalation **rate** trips. | [03](../docs/03-routing-layer.md), [04](../docs/04-escalation-ladder.md), [01 §3](../docs/01-tier-as-contract.md) |
| [economics.py](economics.py) | The cost model at doc 00 §3 rates; doc 00 §5's baseline table; doc 00 §6's tiered table with **both** escalation lines derived, not asserted; doc 00 §8's p50/p90/p99 fan-out tail; doc 05's `cost_per_accepted` and the break-even solver. Prints `OK`/`MISMATCH` per figure and exits non-zero on drift. | [00 §5–§8](../docs/00-overview.md), [05](../docs/05-cost-per-outcome.md) |

The wiring matters as much as the contents: `economics.py` does not hardcode which tier a node
runs on. It calls `blast_radius.tier_floor` (from the DAG and the declared detectors), resolves
that floor through `tiers.TierRegistry.resolve` (the capability satisfies-check), and derives the
escalation target from `tiers.next_satisfying_tier`. Delete a detector and the bill moves on its
own — which is doc 02 §6 point 1 ("the conditional must be enforced, not documented") holding
across three files.

## Invariants you can run

All commands assume you are inside `reference_impl/`.

```bash
python3 -c "
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import tiers as t, blast_radius as b, routing as r, economics as e
now = datetime(2026, 8, 19, tzinfo=timezone.utc)
scored = b.score(b.LEDGERLINE)

# 1) A binding may not name a moving alias — the provider does not get to re-point
#    your tier without consent (docs/01 §7).
for bad in (('v','vendor-y-latest','2026-06-01'), ('v','vendor-y-2',None),
            ('v','vendor-y-*','2026-06-01'), ('v','vendor-y-2','stable')):
    try: t.Binding(t.Tier.MID, *bad)
    except t.AliasBindingError as exc: print('OK 1 alias binding  :', exc)

# 2+3) A pin must state a reason and must die inside 90 days (docs/01 §5).
p = dict(pipeline='ledgerline', granted_at=now,
         binding=t.Binding(t.Tier.MID,'v','vendor-x-3','2026-01-15'))
try: t.Pin(reason='  ', expires_at=now+timedelta(days=90), **p)
except t.PinError as exc: print('OK 2 reasonless pin :', exc)
try: t.Pin(reason='eval LG-SYNTH-07 regresses 4 pts', expires_at=now+timedelta(days=91), **p)
except t.PinError as exc: print('OK 3 immortal pin   :', exc)

# 4) An empty 'requires' is a validation error, not a permissive default (docs/01 §3).
try: t.Capabilities.required()
except t.UnstatedRequirements as exc: print('OK 4 unstated needs :', exc)

# 5) Resolution raises; it never silently falls back to whatever is cheapest (docs/01 §3).
try: t.LEDGERLINE_REGISTRY.resolve(replace(scored['verify'], tier_floor=t.Tier.FRONTIER),
                                   'ledgerline', now)
except t.NoSatisfyingTier as exc: print('OK 5 no fallback    :', exc)

# 6) Cheap-first with no detector is not cheap-first, it is being wrong cheaply (docs/04).
try: r.EscalationLadder.build(scored['verify'])
except r.NoDetectorError as exc: print('OK 6 no detector    :', exc)

# 7) Removing a detector MECHANICALLY raises the floor (docs/02 §6 point 1).
rk = b.LEDGERLINE.nodes['risk_flag']
print('OK 7 detector gates : risk_flag floor', b.tier_floor(rk, b.LEDGERLINE), '->',
      b.tier_floor(rk.without_detector('playbook_coverage_check'), b.LEDGERLINE))

# 8) Money is Decimal, never float (docs/05).
try: e.cost_per_accepted(0.6237, Decimal('0.97'))
except TypeError as exc: print('OK 8 float money    :', exc)
"
```

```bash
python3 tiers.py         # contract table, the satisfies-check per node, re-point blast radius,
                         # a rank-skipping escalation, a failing one, and pin expiry -> default
python3 blast_radius.py  # doc 02 §5's table with OK/MISMATCH; non-zero exit on any drift
python3 routing.py       # feature routing with zero model calls, the 2-rung ladder, the breaker
python3 economics.py     # doc 00 §5/§6/§8 and doc 05, reproduced; non-zero exit on any drift
```

The two `main()`s worth reading the output of are the falsifiable ones.

`blast_radius.py` prints A, D, R and the floor for all seven nodes against doc 02 §5 and only
prints `OK` while they agree. It then shows the two conditionals the doc says must be structural:
dropping `risk_flag`'s `playbook_coverage_check` moves its floor `small → large` (D falls to 0,
R rises to HIGH), and dropping the `verify → redact` edge moves `synthesize`'s A across the 2×
band and its floor `mid → nano`. Neither required editing a stored constant, because there is
none. It also recomputes doc 02 §4's central claim — buying a detector on the 120-wide node costs
**$0.0240/doc** against **$0.3150/doc** to buy the capability instead.

`economics.py` reproduces every documented figure: the $0.9625 baseline row by row with its
87.3% fan-out share, the $0.5175 tiered subtotal, the +$0.1008 clause escalation at 12% and
+$0.0054 memo re-run at 4%, the $0.6237 total at −35.2%, the $86,625/day → $31.6M/year baseline
and the $11.1M/year saving, the p50/p90/p99 fan-out tail at both tiers with its 7.5× spread, and
doc 05's $0.9923/accepted baseline with a **62.9%** break-even. Perturb any token count or rate
and rows go `MISMATCH` with a non-zero exit.

### Two places the implementation disagrees with the prose

Both are printed by the code rather than hidden in it.

1. **Doc 02 §2C, applied as a third independent term, contradicts doc 02 §5.** §1 says a floor is
   "the maximum of what each part demands", and §2C demands `mid` for R = MED. `risk_flag`'s R is
   MED in §5 — so a literal three-way max gives `risk_flag` a `mid` floor, which contradicts §5's
   `small*` and raises the pipeline from $0.5175 to $0.8325 before escalation. §2B is the joint
   (D,R) table and already conditions on R, so `tier_floor` maxes exactly two terms and
   `floor_from_reversibility` is kept as a printed `§2C` diagnostic column instead.
2. **Doc 02 §6's "$0.94/doc" figure corresponds to `mid`, not to `large`.** §6 concludes that
   without the coverage check "the honest floor is large and the pipeline costs $0.94/doc". Those
   two clauses cannot both be true: `risk_flag` at `large` is 120 × $0.0105 = **$1.26/doc**, giving
   a pipeline of **$1.7787**. The doc's $0.94 is $0.6237 + $0.3150 — exactly `risk_flag` at **mid**
   ($0.9387, which rounds to $0.94). `economics.py` prints both so the gap is visible. The floor
   itself is unaffected: `blast_radius.py` scores the detector-free `risk_flag` as `large`, on
   §6's own grounds (D → 0, R → HIGH), and only the dollar figure attached to it is off by a tier.

Two smaller notes, neither a bug: doc 02 §5 prints `segment`'s A as `42×` where the DAG gives
**42.5×** (same `>20×` band, so the floor is unchanged — the A column is compared at a 2% relative
tolerance for this reason); and doc 05's `0.6237/0.9923` ratio implies a **97%** baseline
acceptance rate, which the code takes as the input and from which both $0.9923 and 62.9% are
derived. Docs 03, 04 and 05 are referenced by the README's document map but not yet written, so
`routing.py`'s thresholds and the escalation-breaker settings are illustrative in the same sense
as doc 00's rates, and are named as such at the top of the file.

## What is deliberately stubbed

Every model call is a rate table and every detector is a declared coverage number. Nothing here
talks to a provider.

- **The models.** There is no inference anywhere. `token_cost` prices a call from doc 00 §3's
  illustrative rates; `TokenProfile` is doc 00 §4's token counts. Substituting your own rates and
  counts is the intended first act, and the `MISMATCH` columns will then tell you which of the
  docs' conclusions survive.
- **The conformance suite** (doc 01 §4) — the load-bearing part of the registry. `TierContract`
  states the floors a candidate must clear; the provider-agnostic suite that *decides* whether a
  candidate clears them is a platform-owned eval harness, not a dataclass. `Binding` is what
  exists after it passes.
- **Detector implementations.** `DETECTORS` gives each check the error classes it covers and its
  measured coverage; the schema validator, the span-grounding verifier, the playbook coverage
  check, and the human review sample are all just numbers here. The *declaration* is the contract
  — that a floor is a function of what is declared, and that D = 0 until something is (doc 02 §9).
- **The escalation detector.** `run_ladder` takes a sequence of pass/fail verdicts. In production
  that is the same detector the floor was scored against, which is exactly why
  `detectability_gate` exists: the two must not be allowed to drift apart.
- **Per-tenant attribution, budgets, and the outcome ledger.** Docs 06, 08 and 09. `economics.py`
  computes a per-document bill; who pays for a prompt cache warmed by another tenant, and where
  the accepted/rejected signal is durably recorded, are separate systems.
- **Persistence and the eval gate.** The registry is in-memory and its binding history is absent.
  Doc 01 §4's append-only "who/when/eval-run" log and doc 07's canary machinery are deployment
  concerns; the invariant this code protects is that a *resolution* is reconstructible from
  (node floor, requires, pin state, clock).

Production supplies all of that without touching `satisfies`, the pin invariants, the rubric, or
the cost arithmetic — which is the point of drawing the seams here.
