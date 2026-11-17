# Testing: What Staging Cannot Reproduce, and the Six Harnesses That Earn Their Keep

A fraud system is unusually hard to test, for a reason that is structural rather than cultural:
**its correctness is statistical, its inputs are adversarial, and its most important output — the
decision it would have made differently — has no observable consequence in a test environment.** A
unit test can verify that a rule's predicate evaluates correctly. Nothing in a conventional test
suite can tell you that the rule will fire 400 times more often than you expect, or that your
training set is contaminated, or that your system approves everything when the feature store is
unreachable.

This document covers what to build instead. Several of the harnesses described here have been
referenced throughout the collection; this is where they are specified.

---

## What staging fails to reproduce, line by line

Worth being explicit, because "we tested it in staging" is the most common answer to the question
this document asks.

| Property | In staging | In production |
|---|---|---|
| Traffic volume | Tens per second | 486/s, 3,900/s at peak |
| Entity population | Thousands of synthetic cards | 200.9 million entity keys |
| Feature store state | Minutes old, tiny, no history | 281 GB, 90 days of accumulated windows |
| Base rate | Whatever the test data has, usually 50% | 1 in 7,692 |
| Peak-to-average ratio | 1× | 8× at Ironvale, 237× at Northwind |
| Upstream behaviour | A stub returning a fixed message | Eleven producers, each with its own deployment schedule |
| Adversary | Absent | Present, adapting, and probing |
| Label availability | Immediate, by construction | Median 27 days |
| Device population | A handful of test devices | 54 million, with a long tail of weirdness |
| Latency correlation structure | Uncorrelated, idle hosts | Shared causes under load (doc [02](02-the-decision-path-and-the-latency-budget.md)) |

**Nothing in that table is fixable by making staging bigger.** The base rate, the adversary and the
label delay are properties of the world. So the testing strategy cannot be "a more faithful staging
environment"; it has to be a set of techniques that each attack one row.

---

## Harness one: replay against logged feature vectors

**What it is.** Take the decision log (doc [10](10-storage-hot-cold-and-what-it-costs.md)), which
contains the exact feature vector used for each historical decision, and run a candidate — a rule,
a model, a threshold, a whole policy — against it. Report what each would have decided.

**What it tests.** Fire counts, precision against known labels, decision changes relative to
production, and the distribution of all of those by segment.

**Why it is the foundation.** It uses real feature values at real frequencies with the real base
rate, which eliminates five rows of the table above at once. Every other harness in this document is
less important than this one.

**The outputs that matter, for a rule:**

```
Fire count, per day, and by segment
Fire rate against the declared expectation      (doc 03, R-01)
Precision against mature labels, with a confidence interval
Marginal contribution: decisions changed, not just transactions matched
The 20 transactions nearest the predicate's boundary, for a human to read
```

**That last line is not a statistic and it catches more defects than the statistics do.** The most
common rule bug is a unit error — amount in cents rather than dollars, a window in hours rather than
minutes, a percentage expressed as 0.05 versus 5 — and a human looking at twenty concrete
transactions spots it immediately while a precision figure of 1-in-9 looks fine.

**The cost to build.** If the decision log exists with full feature vectors, the harness is a few
days of work. If it does not, the harness is impossible and no amount of effort substitutes, which
is the strongest practical argument for the logging decision in doc
[04](04-features-and-the-feature-store.md).

---

## Harness two: the daily recompute-and-compare

**What it is.** Each day, take a sample of production decisions — Ironvale uses 10,000 — and
recompute every feature from the offline store and the raw event log. Compare against the values
logged at decision time. Report, per feature, the share that differ by more than the known
staleness tolerance.

**What it tests.** Training-serving skew (doc [04](04-features-and-the-feature-store.md), `F-04`),
point-in-time correctness of the backfill (`F-02`), and silent upstream contract changes (`F-05`,
`P-07`).

**Why it is the highest-value recurring check in the collection.** It is the only evidence that your
two implementations of 412 features agree. Ironvale's one-time audit before this check existed
found 19 features differing materially, four of them in the top twenty by importance — and the
leakage incident in doc [16](16-case-studies.md)'s `CS-2`, which cost nine weeks and a rolled-back
model, would have been caught on day one by this check.

**The threshold to use.** Any feature with more than 0.5% of samples differing beyond tolerance is
treated as broken until proven otherwise. Setting it looser than that lets the interesting cases
through; setting it tighter produces noise from legitimate staleness.

---

## Harness three: synthetic scenario injection

**What it is.** A library of crafted event sequences, replayed through the real pipeline into a
shadow keyspace, with asserted expected outcomes. Not test data — *scenarios*, each encoding a
specific failure mode.

**The library worth having**, which is about forty sequences and takes a week to write:

| Scenario | Asserts |
|---|---|
| Two transactions 400 ms apart on one card | The second sees the first in its velocity counters |
| A transaction straddling a window boundary | Hopping-window arithmetic is correct at the edge |
| An event arriving 14 seconds late | It lands in the correct bucket, not a later one |
| The same event delivered twice | Counters increment once (doc [05](05-the-streaming-pipeline.md), `P-01`) |
| Events out of order within a partition | Event time, not processing time, determines the bucket |
| 4,100 events/s to one merchant key | Salting engages; distinct-count sketches merge correctly |
| A device with 40 cards in 10 minutes | The device cardinality feature reflects it within the freshness target |
| A card's first-ever transaction | Cold-start backoff engages at the correct hierarchy level |
| All six entity lookups fail | The decision is marked degraded; policy escalates; nothing is imputed silently |
| One of six entity lookups fails | Same, scoped to that entity |
| Feature store unreachable entirely | Static fallback engages within one second |
| A transaction in the holdout hash range with a blocking score | **It is actually approved** (doc [16](16-case-studies.md), `CS-9`) |
| A card on the block list with the rule engine unavailable | It is declined (the one correct fail-closed) |
| Challenge channel unavailable | The configured per-segment fallback applies, not a default decline |
| A probe sequence straddling a threshold | The probe-detection rule fires |

**The twelfth row is the one to copy first.** The holdout is the measurement instrument for
everything and it fails silently; an assertion that a high-scoring transaction in the holdout range
is approved is four lines of test code and would have saved eleven days of lost measurement.

---

## Harness four: the ablation study

**What it is.** For each feature family, each model component and each rule pack, remove it,
retrain or re-evaluate, and measure catch rate at a fixed decline rate on the holdout sample.

**What it produces.** The numbers that appear throughout this collection — the feature-family
contribution table in doc [04](04-features-and-the-feature-store.md), the degradation ladder in doc
[02](02-the-decision-path-and-the-latency-budget.md), the signal-family values in doc
[08](08-device-identity-and-behavioural-signals.md), the rule marginal-contribution distribution in
doc [03](03-rules-and-why-they-never-go-away.md).

**Why it matters beyond curiosity.** Without it, the degradation ladder is a list of guesses and the
wrong component gets protected. A team that cannot say what it loses when the sequence model is
unavailable cannot make a sensible decision at 03:00 about whether to shed it.

**Cost.** At Ironvale: 7 feature families plus 5 model components plus 7 rule packs = 19 runs, each
a retrain or re-evaluation, batched monthly. Roughly 14 hours of compute. The per-rule version —
1,100 runs with one rule suppressed each — is four hours monthly, and it is the single best value in
the collection for what it tells you about an estate nobody understands.

---

## Harness five: the adversarial exercise

**What it is.** A small team, given the production system's documented behaviour and a modest budget
of test cards, tries to get fraudulent transactions through. Time-boxed, scoped, authorised in
writing, with a defined blast radius.

**Why conventional testing cannot substitute.** Every other harness tests against patterns you
already know. The adversary's entire advantage is that they look for the ones you do not, and the
only way to simulate that is to have somebody look.

**What to make them attack, in order of return:**

1. **The boundaries.** Can they find a threshold by probing? How many attempts does it take? This
   directly measures the exposure in doc
   [12](12-adversaries-drift-and-the-arms-race.md)'s `A-03`.
2. **The degradation modes.** Can they cause the scorer to degrade — a slow dependency, an expensive
   rule path, a feature-store timeout — and does the degraded behaviour help them? This is how
   doc [16](16-case-studies.md)'s `CS-3` would have been found before it happened.
3. **The step-up channels.** Can they pass a challenge they should not? SIM swap, social
   engineering, remote access.
4. **The appeals path.** Can they get a decline reversed by calling (doc
   [12](12-adversaries-drift-and-the-arms-race.md), `A-10`)?
5. **The allow-lists.** Can they get onto one?

**The single most valuable item**, from experience across such exercises, is the second: **the
degradation modes are where the systems are weakest and where nobody tests, because degraded
behaviour is a configuration rather than a feature and configurations are not in the test plan.**

---

## Harness six: the gameday

**What it is.** A deliberate, announced, production exercise in which a failure is induced and the
on-call responds, with the real tooling, at the real time of day, with the clock running.

**What to induce**, in rough order of value:

1. **Block the feature store at the network level** and watch what happens. This is the single most
   valuable gameday because it exercises the most dangerous failure mode in the collection, and
   teams routinely discover their fail-static is a fail-open with extra steps.
2. **Kill the step-up channel** and observe whether it becomes a decline storm (doc
   [15](15-the-failure-catalogue-and-playbook.md), `X-04`).
3. **Stop the aggregation job's writes** while leaving it consuming, and time the detection against
   the nine hours of `CS-5`.
4. **Roll back a model under time pressure** and measure the elapsed time against the claimed 90
   seconds.
5. **Rebuild the feature state** and measure it against the claimed 1 hour 57 minutes.

**The item that matters most, and it is not on the list above:** confirm that the person on call
knows where the action catalogue is and has authority to use it. The commonest gameday finding is
not a technical failure; it is that the responder spent eleven minutes looking for the runbook and
then needed an approval from somebody asleep.

---

## The CI gates worth having

Cheap, automated, on every change:

| Gate | Blocks |
|---|---|
| Rule lint: every rule has an owner, a declared fire rate, an expiry or a review date, and a plain-language description | An estate that becomes unknowable |
| Rule cost: pack evaluation cost measured against a declared budget | Doc [03](03-rules-and-why-they-never-go-away.md)'s `R-04` |
| Feature registry completeness: every feature referenced by a rule or model exists in the registry with null semantics and current-event semantics declared | Doc [04](04-features-and-the-feature-store.md)'s `F-03` and `F-12` |
| Schema compatibility: no model-input field may become optional or be renamed | Doc [05](05-the-streaming-pipeline.md)'s `P-07` |
| Fingerprint stability: an SDK upgrade must produce the same identifier for at least 95% of a sample of devices | Doc [08](08-device-identity-and-behavioural-signals.md)'s `I-02` |
| The holdout assertion from harness three | Doc [16](16-case-studies.md)'s `CS-9` |
| Degradation assertions: with each dependency stubbed as unavailable, the service returns a decision marked degraded rather than an imputed score | Doc [02](02-the-decision-path-and-the-latency-budget.md)'s `D-05` |

**Four of those seven are a day's work each and would have prevented a named incident in doc
[16](16-case-studies.md).**

---

## The evaluation protocol, written down once

Because "how do you evaluate a model" deserves a precise answer rather than a gesture:

```
1.  Split by time. Train on an earlier period, test on a later one, with a gap
    between them equal to the label maturity cutoff (71 days at Ironvale).
    Never a random split: it overstated Ironvale's precision by 62%.
2.  Apply the first-party-dispute classifier to the labels; train on third-party
    fraud only.
3.  Up-weight holdout samples by 1 / holdout_rate so they represent the
    population they were drawn from.
4.  Evaluate at four operating points — decline rates of 0.10%, 0.145%, 0.20%,
    0.30% — not with a single summary statistic.
5.  Report catch rate, precision and false-decline rate at each point, per
    segment, with confidence intervals derived from the number of fraud events
    in that cell, not from the number of transactions.
6.  Report a reliability diagram and expected calibration error.
7.  Report the decision-change matrix against the champion: how many approvals
    become declines and vice versa, and the measured outcome of each cell where
    labels exist.
8.  Report performance by protected or proxied group, specifically
    false-decline rate and catch rate (doc 13).
9.  State the populations the model should not be relied on for.
```

**Step five's parenthesis is the one people get wrong.** A segment with 4 million transactions and
27 fraud events has a precision estimate with an enormous confidence interval, and quoting it to
three decimal places because the denominator is large is the commonest statistical error in fraud
reporting. **The uncertainty is governed by the positive count, essentially always.**

---

## What to take away

1. **Staging cannot reproduce the base rate, the adversary or the label delay**, and no amount of
   making it bigger changes that. The strategy is a set of techniques that each attack one row of
   the gap, not a more faithful environment.
2. **Replay against logged feature vectors is the foundation**, and it is only possible if the
   decision log carries the full feature vector. Everything else is less important than this.
3. **Always show a human twenty boundary cases.** Unit errors — cents for dollars, hours for minutes
   — are the commonest rule bug and are invisible in a precision figure.
4. **The daily recompute-and-compare on a 10,000-row sample is the highest-value recurring check**,
   and it would have caught a nine-week leakage incident on day one. Treat any feature over 0.5%
   divergent as broken.
5. **Build the synthetic scenario library** — forty crafted sequences with asserted outcomes — and
   start with the assertion that a high-scoring transaction in the holdout range is actually
   approved. Four lines of code against eleven days of lost measurement.
6. **The ablation study is what makes the degradation ladder real.** Nineteen runs monthly for the
   components, four hours for the full per-rule contribution, and without it your 03:00 decisions
   are guesses.
7. **Have somebody attack it deliberately**, and point them at the degradation modes first —
   degraded behaviour is a configuration rather than a feature, so it is never in the test plan and
   is where systems are weakest.
8. **The most valuable gameday is blocking the feature store**, because teams routinely discover
   their fail-static is a fail-open with extra steps.
9. **The commonest gameday finding is not technical**: the responder could not find the runbook and
   needed an approval from somebody asleep.
10. **Never evaluate on a random split**, and never on a single summary statistic — four operating
    points, per segment, with confidence intervals derived from the fraud-event count rather than
    the transaction count.
