# Golden Defaults, Checklists, and What to Build at Your Scale

This document is the operational residue of the collection: the configuration values worth starting
from, the checklists worth running, what to build in which order, and the dozen controls that would
have prevented most of the 130-odd failures described elsewhere.

It is deliberately the last document, because every number in it is derived somewhere earlier and
adopting them without the derivation is how you end up with a threshold in a config file and a
commit message saying "tuned".

---

## The triage order, repeated

When something is happening now, in this order:

1. **Did we change anything in the last 72 hours?** Ours or an upstream team's. 61% of apparent
   anomalies are this, and identifying it first takes 14 minutes against 3 hours 40.
2. **Is the decline rate up or down, narrow or broad, and did the score distribution move?** Six
   shapes, in doc [15](15-the-failure-catalogue-and-playbook.md)'s triage table.
3. **Is feature availability and freshness normal?** If "nothing changed", this is where the answer
   usually is.
4. **Pick the action with the smallest blast radius that could work**, from the action catalogue,
   and prefer reversible to precise.
5. **State the predicted contact-centre impact before acting** if the action moves decline volume
   more than 5%.

---

## What to build, in three tiers

The single most useful thing in this document, because most teams are at tier 1 and are being sold
tier 3.

### Tier 1 — under about 10,000 transactions a day

At one or two fraud events a day you cannot train a model; forty positive examples will be
memorised. Build, in this order, over about three weeks:

1. **A block list you can edit in under a minute**, with an audit record.
2. **Six to ten velocity rules**: attempts per card per 10 minutes and per hour, distinct cards per
   device per day, amount against the card's own trailing maximum, first-ever transaction in a new
   country, and a decline-count trigger. Each with an owner and a declared expected fire rate.
3. **Your processor's or a vendor's fraud score**, used as one input, capped so it cannot alone
   decline.
4. **A review queue a human looks at every morning**, with a structured outcome vocabulary from day
   one, because those become your labels.
5. **A decision log with the feature values.** Even at this scale. It costs nothing and it is the
   only thing you cannot create retrospectively.
6. **Your base rate and your two error costs, written down.**

**Stop there.** Items 1 to 6 are a few weeks of work and will outperform a model for a long time. If
somebody proposes a feature store, ask what the base rate is first.

### Tier 2 — roughly 10,000 to 1,000,000 transactions a day

Now there is enough signal to train on. Add, roughly in order:

7. **A streaming aggregation layer and an online feature store**, with hopping windows and one
   packed value per entity key.
8. **A gradient-boosted tree model**, trained on time-split data with a label maturity cutoff.
9. **Calibration, measured with a reliability diagram**, and recalibrated on its own schedule.
10. **Segmented thresholds**, derived from per-segment costs — typically the biggest single
    improvement available at this stage, worth several points of catch rate for no modelling work.
11. **Step-up, through the channel your customers already trust**, which is usually your own app.
12. **A randomised holdout**, at the smallest percentage that yields a few hundred fraud events a
    quarter.
13. **The daily recompute-and-compare check** between offline and online feature values.
14. **Score distribution, feature availability and freshness monitoring**, with the decline-rate
    alarm configured in both directions.

### Tier 3 — above about 1,000,000 transactions a day

15. **A graph layer**, if and only if the structure test in doc
    [09](09-graphs-rings-and-linked-entities.md) passes — at least 15% of your last 200 confirmed
    cases sharing an attribute with another.
16. **A cascade**, routing only the uncertain middle to expensive models.
17. **A sequence model**, if the ablation justifies it against the GPU cost.
18. **Per-rule marginal contribution, monthly.**
19. **Consortium or network data sharing.**
20. **Formal model governance with change tiers by blast radius**, and a case-management tool with
    the 2-hop subgraph in it.

**The ordering principle, stated once:** each tier's items make the next tier's items measurable.
You cannot tune a threshold without knowing your costs (tier 1, item 6); you cannot evaluate a model
without a holdout (tier 2, item 12); you cannot justify a sequence model without an ablation
(tier 3, item 17, needing tier 2's harness).

---

## The dozen controls that would have prevented most of this collection

Audited against the scenarios across all documents. Each is hours to days of work.

| # | Control | Prevents |
|---|---|---|
| 1 | Absent features are a distinct type, never imputed silently; the policy layer, not the model, decides what to do about them | `D-05`, `X-02`, `CS-1` — the single most dangerous failure class |
| 2 | Decline-rate alarms configured in **both** directions | `X-02`, `CS-1` |
| 3 | End-to-end feature freshness heartbeat, alarmed; consumer lag demoted to a diagnostic | `P-05`, `X-03`, `CS-5` |
| 4 | Feature availability per entity type, alarmed at 0.5% | `D-05`, `CS-1` |
| 5 | Every rule carries a declared expected fire rate, alarmed at 3× actual | `R-01`, `R-08` |
| 6 | Discretionary rule escalation capped at one step per decision | `R-02`, `CS-4` |
| 7 | Incident rules auto-expire to shadow at 30 days; allow-list entries expire at 90 with a named owner and a monthly cost report | `R-03`, `R-06`, `CS-6` |
| 8 | Full feature vector plus model, rule, calibration and policy versions in every decision record | `F-02`, `C-11`, `CS-2`, and every harness in doc [17](17-testing-fraud-systems.md) |
| 9 | Daily recompute-and-compare on a 10,000-row sample | `F-04`, `F-05`, `P-08`, `CS-2` |
| 10 | Holdout volume as an alarmed first-class metric, with the flag set by the component that applies the bypass | `M-03`, `O-10`, `CS-9` |
| 11 | Change annotations on every fraud dashboard, including upstream teams' changes | `A-01`, `O-11`, and the 61% statistic |
| 12 | Signal availability by family, trended quarterly over months rather than weeks | `I-01`, `CS-10` — the most expensive failure in the collection |

---

## Golden configuration

Starting points, not answers. Each is derived in the document cited.

### Latency and resilience (doc [02](02-the-decision-path-and-the-latency-budget.md))

```
Fraud decision p99 objective            120 ms
Caller hard timeout                     300 ms
Per-stage deadlines                     features 70, rules 20, primary model 25,
                                        sequence 45, assembly 15 (deliberately
                                        overcommitted; they do not peak together)
Retry policy                            budget of 10% of requests, not a count
Circuit breaker                          open after 20 failures in 10 s; reopen at
                                        1% → 5% → 25% with a health check between
Backoff jitter                          uniform 0.5× to 1.5×, always
Behaviour on timeout                    static fallback, never fail open or closed
Hedge                                   feature reads at p95; never expensive
                                        computation on shared capacity
Deadline propagation                    absolute instant, checked before each
                                        expensive stage
```

### Feature pipeline (docs [04](04-features-and-the-feature-store.md) and [05](05-the-streaming-pipeline.md))

```
Window implementation                   hopping, buckets per the doc 04 table
Aggregate semantics                     exclusive of the current event, stated once
Store writes                            absolute bucket values, never deltas
Duplicate protection                    exactly-once in the processor AND a 64-event
                                        ID ring per key
Late events                             upsert into buckets, not watermarked windows;
                                        count and alarm on drops by lateness
Freshness objective                     p99 under 1.5 s, page at 5 s
Event time                              the issuer's own receipt timestamp
Hot keys                                dynamic salting above an observed rate, with
                                        mergeable sketches for distinct counts
Stateful autoscaling                    off; provision for the known peak
Per-task state cap                      2 GB, restorable in 40 s
```

### Storage (doc [10](10-storage-hot-cold-and-what-it-costs.md))

```
Online store                            one key per entity, one packed fixed-layout
                                        value, schema version in the first two bytes
TTLs                                    card 95 d, account 95 d, device 95 d,
                                        merchant 400 d, IP prefix 35 d, email 95 d
Expired buckets                         dropped inside the normal write path
Partition availability                  do NOT serve reads before restore completes
Client                                  must distinguish key-absent from
                                        partition-unavailable
Decision log                            columnar, partitioned by date, subpartitioned
                                        by hash of card token
Operational window                       30 days, row-oriented, separate store
Retention                               per regulation; retention reason as a
                                        per-record field; legal hold by tag, not age
Multi-region                            read locally, write globally
```

### Models and thresholds (docs [06](06-models-labels-and-training.md) and [07](07-thresholds-and-the-economics-of-errors.md))

```
Primary model                           gradient-boosted trees, monotonic constraints
                                        where the direction is known
Imbalance handling                      class weighting; undersample negatives if
                                        training time demands it AND correct the prior
                                        afterwards; never SMOTE
Split                                   by time, with a gap equal to the label
                                        maturity cutoff; never random
Label maturity cutoff                   the p95 label delay (71 days at Ironvale)
Labels                                  is_third_party_fraud separate from
                                        is_first_party_dispute, plus label_source
Calibration                             its own schedule, more often than retraining
Holdout                                 0.3%, by hash of transaction ID, mandatory
                                        pack excluded, volume alarmed
Thresholds                              derived from costs held in a reviewed
                                        configuration object, per segment, with the
                                        amount in the decision rule
Threshold changes                       ramp 1% → 10% → 50% by hash of card token,
                                        with predicted decline-volume delta stated
Peak freeze                             72 hours either side of a known peak, in both
                                        directions
```

### Rules (doc [03](03-rules-and-why-they-never-go-away.md))

```
Packs                                   mandatory, policy, core, incident,
                                        experimental, redundant-with-model, allow-list
Precedence                              mandatory final; rules may escalate only;
                                        allow-lists may de-escalate only; channel
                                        capability applied last
Escalation cap                          one step from discretionary packs
Shadow period                           measured in fraud events, minimum 30, not days
Expression language                      may only read features that were fetched
Evaluation order                        packs by precedence with early exit; rules by
                                        observed selectivity of the cheapest predicate
Allow-list entries                      90-day expiry, named owner, narrowest scope,
                                        still scored and logged, monthly cost report
```

---

## The pre-flight checklist for a model release

```
[ ] Trained on a time split with a gap equal to the label maturity cutoff
[ ] First-party disputes classified and excluded from the positive class
[ ] Holdout samples up-weighted by 1 / holdout_rate
[ ] Evaluated at four operating points, per segment, with intervals from the
    fraud-event count
[ ] Reliability diagram and calibration error reported
[ ] Decision-change matrix against the champion
[ ] Performance by protected or proxied group: false-decline rate AND catch rate
[ ] Ablation re-run; degradation-ladder percentages updated
[ ] Documented limitations and the populations it should not be relied on for
[ ] Recompute-and-compare clean for every feature it uses
[ ] Inference latency measured at the p99 of the real feature distribution
[ ] Reason-code mapping updated for any new feature
[ ] Rollback artefact resident and tested
[ ] Canary gates set per segment, weighted to fraud share not volume share
[ ] Validation sign-off appropriate to the change tier
```

## The pre-flight checklist for a rule

```
[ ] Owner, creation reason, review date
[ ] Declared expected fire rate
[ ] Replayed against 7 days of logged feature vectors
[ ] Fire count and precision by segment, with intervals
[ ] Marginal contribution, not just matches
[ ] Twenty boundary transactions read by a human
[ ] Co-occurrence checked against existing rules firing on the same population
[ ] Pack assigned, and correct for its governance level
[ ] Evaluation cost measured against the pack budget
[ ] Plain-language description that could be shown to a customer
[ ] Expiry set, if it came through the incident path
```

## The quarterly review agenda

```
[ ] Signal availability by family, trended over the last eight quarters
[ ] Rule estate: count, marginal-contribution distribution, net-negative rules
[ ] Allow-list: entry count, cost report, expiries not renewed
[ ] Threshold input costs re-confirmed with disputes, operations and product
[ ] Recovery rate re-measured (it moves the threshold more than the model does)
[ ] Fairness metrics by group: false-decline rate, catch rate, time-to-resolution,
    reversal rate
[ ] Holdout integrity: volume against expectation, and the end-to-end assertion
[ ] Failure-mode verification: block the fraud service in a replay environment and
    confirm the fallback behaviour
[ ] Degradation ladder percentages refreshed from the latest ablation
[ ] Contact-centre capacity re-confirmed as a number
```

---

## The numbers worth remembering

| Quantity | Value |
|---|---|
| Ironvale base rate | 1 in 7,692 (1.3 bps of count, 5.0 bps of value) |
| Net fraud loss, well-run issuer | 3.5 bps of volume |
| Good transactions declined | 0.145%, one in 690 |
| Precision, average versus marginal | 1 in 12 versus 1 in 88 |
| Break-even precision formula | 1 : (C_miss / C_false) |
| Ironvale's break-even | 1 in 94 |
| Cost of a false decline, issuer | $1.92 (and plausibly up to $8.49) |
| Cost of a missed fraud, issuer | $179 net of a 25% recovery |
| Fraud decision budget | 120 ms p99 inside a 420 ms allowance |
| Catch rate on a card's first CNP transaction | 54% against 93% overall |
| Segmented thresholds gain | 88.1% → 93.0% catch at equal decline volume |
| Step-up band, Ironvale | 0.24% < p < 21.3% |
| In-app challenge pass rates | 94.1% genuine, 2.8% fraudster |
| Implied fraud rate from pass rate | f = (0.941 − observed) / 0.913 |
| Online state | 200.9M keys × 1.4 KB = 281 GB |
| Hot tier cost | $30,000–$100,000/year, 0.11–0.38% of the loss |
| Decision log | 80 GB/day, 29 TB/year, $71,000 over seven years |
| Feature-state rebuild | 4 minutes for 24-hour windows, 1 h 57 for 30-day |
| Holdout cost | 0.3% = about $950,000/year, 3.7% of the loss |
| Median label delay | 27 days, p95 71 days |
| Safe model release | 34 days, 21 of them waiting for labels |
| Random versus time split | overstates precision by 62% |
| Analyst operation | 1,900 cases/day, 31 analysts, $2.4M/year |
| Queue reordering by expected loss | +34% value prevented per analyst-hour |
| Graph: component sizes | 71% under 4 nodes; the 290 giants are artefacts |
| Graph compute | $19,700/year for the most valuable feature family |
| Mule withdrawal window | under 9 minutes |
| Share of anomalies that are self-inflicted | 61% |

---

## The twelve rules

1. **Name which of the five frauds you are designing for**, before drawing a box.
2. **State the base rate as arithmetic**, and recompute precision from it rather than quoting
   accuracy or ROC-AUC.
3. **Ask who pays for each error, specifically** — and notice that at an issuer most of the cost of
   a false decline lands on the merchant and the customer, who are not in your loss report.
4. **The threshold belongs to the cost structure, not the model.** Derive it; review the inputs
   quarterly with the people who own them.
5. **Approvals produce labels and declines do not**, so run a randomised holdout. It is the only
   source of your false-positive rate, your blocked-region precision, and your fairness testing.
6. **Log the full feature vector and every version with every decision.** It is the one thing you
   cannot create retrospectively, and four separate capabilities depend on it.
7. **Absent is not zero.** The most dangerous output in the system is a confident score computed from
   missing inputs, and it has no signal anywhere except in labels a month late.
8. **Fail static, not open or closed** — and verify quarterly which one you are actually in.
9. **Prefer a feature to a model, and prefer a signal the adversary cannot observe.** Their behaviour
   is theirs to change; your consortium data, your graph and the card's own history are not.
10. **Every response you make is free intelligence for the adversary.** Prefer broad to narrow,
    step-up to decline, continuous to cliff-edged, randomised to fixed.
11. **The human loop is half the system.** It produces your labels, it finds your new patterns, and
    its capacity — not your model — sets the operating point for your highest-risk band.
12. **Measure the harm you cause, not only the harm you prevent**, and put both numbers on the same
    page in the same monthly review. When you over-block, every fraud metric goes green.
