# Ten Incidents, as Timelines

Each of these is a composite, assembled from patterns that recur across real fraud programmes and
set in the four running systems so the numbers reconcile with the rest of the collection. They are
told as timelines because that is how they are experienced, and because the interesting question in
each is not "what was the bug" but "why did it take so long to see it".

Each ends with the scenario identifiers it is an instance of, so you can read the mechanism in
detail where it is derived.

---

## CS-1 · The twenty minutes in which Ironvale approved everything

**Monday, 14:02.** A routine certificate rotation on the feature store's client credentials is
applied. The new credential has the wrong scope. Reads begin failing.

**14:02:10.** The feature-store client returns empty maps rather than errors — the convenience API
conflates "key not present" with "cannot read". The imputation layer fills the training-set means.

**14:02:40.** Scoring latency *improves* from 118 ms p99 to 71 ms, because the feature fetch is
returning immediately.

**14:03.** The decline rate begins falling. Over four minutes it goes from 0.145% to 0.011%.

**14:06.** No alarm. The service-level dashboards are green and improving. The decline-rate alarm is
configured one-sided, for increases only.

**14:22.** A fraud analyst working the queue notices the queue is nearly empty, which it never is,
and asks in the team channel whether something is wrong.

**14:24.** An engineer checks a feature value for a known card and finds it at the population mean.

**14:26.** Static fallback engaged manually.

**14:31.** Credential corrected, reads resume, normal scoring restored.

**The cost.**

```
Duration of degraded scoring:        14:03 to 14:26 = 23 minutes
Transactions:                        486 × 1,380 = 670,680
Expected fraud among them:           670,680 / 7,692 = 87
Normally prevented (93%):            81
Prevented by the degraded system:    the static rules were not running either — 0
Fraud let through:                   81 × $179 = $14,499
```

**Why it took 24 minutes.** Four independent protections were absent and each would have caught it
within a minute: the client did not distinguish absent from unreadable; absence was imputed rather
than typed; feature availability was not an alarmed metric; and the decline-rate alarm was
one-sided. **The thing that actually detected it was a human noticing an empty queue** — the most
expensive detector in the organisation, and the one that only works during a staffed shift.

`D-05`, `X-02`, `S-04`, `O-03`.

---

## CS-2 · The model that evaluated at 1-in-2 and shipped at 1-in-19

**Week 1.** A new model is trained. Offline evaluation: AUC 0.991, precision at the operating point
of 1 in 2.1, against the champion's 1 in 12. The improvement is so large that the team's main
concern is whether the decline rate can be reduced to capture it.

**Week 2.** Shadow mode. Score distributions look different from the champion's but plausibly so.
Agreement rate with the champion is 71%, which is lower than usual and is interpreted as the new
model being better.

**Week 3.** Canary at 1%. Decline rate within tolerance. No obvious problem.

**Week 6.** 50% of traffic. The first mature labels for canary-period transactions begin arriving.

**Week 9.** Measured precision on the new model: **1 in 19**, worse than the champion it replaced.
Catch rate 4 points lower.

**Week 10.** Rolled back. Investigation finds the training set was built by joining transactions to
the current aggregate tables, so every training row's velocity features included the fraud that
followed that transaction.

**Why it took nine weeks.** Nothing in weeks 1 to 6 could have detected it, because every
observable signal was consistent with a genuinely better model, and the only disconfirming evidence
is labels. **The one check that would have caught it in week 1 is comparing the training set's
feature values against the production decision log's values for the same transactions**, which
differs by construction when the join is contaminated. That check now runs daily on a
10,000-row sample.

`F-02` on the leakage mechanism, `M-12` on what shadow cannot tell you, `M-13` on the release path.

---

## CS-3 · The outage somebody caused on purpose

**Thursday, 02:14.** Ironvale's fraud service begins timing out. Inbound request rate at the service
is normal; CPU is saturated.

**02:16.** The authorization service's retries take the inbound rate to 3.1× normal. Partial
degradation becomes total.

**02:18.** Fail-open behaviour engages as designed. Every transaction is approved.

**02:19 to 02:51.** Fraud volume in the window is **41 times** the base rate. The transactions are
concentrated in card-not-present electronics and gift cards, in ticket sizes between $400 and $900,
across 3,100 distinct cards that had been tested in small amounts over the preceding nine days.

**02:51.** Retries disabled. Service recovers in 70 seconds.

**03:05.** Root cause of the saturation: a crafted pattern of authorization requests with an unusual
field combination that triggered an expensive path in the rule engine — a regular expression over a
free-text field, in a rule added eleven months earlier.

**The cost.**

```
Window:                              32 minutes
Transactions:                        486 × 1,920 = 933,120
Fraud at 41× base rate:              933,120 × 41 / 7,692 = 4,973
Average ticket in the attack:        $620
Net cost at 25% recovery plus handling: 4,973 × ($620 × 0.75 + 39.40) = $2.51 million
```

**The two lessons.** The nine days of small-value testing were visible in Ironvale's own declines
and nobody was looking — `A-02`'s enumeration feature did not exist at the time. And **fail-open was
chosen on arithmetic that assumed the normal fraud rate during the outage**, which is exactly the
assumption an attacker who can cause the outage invalidates. The static fallback was built after
this incident; at 61% catch it would have reduced the loss to about $980,000.

`D-01`, `D-04`, `D-06`, `R-04`, `A-02`.

---

## CS-4 · The rule that declined every traveller in Southeast Asia

**Day 1.** A rule is added to the core detection pack: escalate when the merchant's country differs
from the card's home country and the device was first seen within 24 hours. Shadow results over
four days look fine — it fires on 340 transactions a day at 1-in-9 precision.

**Day 12.** Promoted to active.

**Day 19.** A relationship manager escalates: a corporate client's employees travelling in Thailand
and Vietnam cannot use their cards at all.

**Day 19, investigation.** The new rule is one of three that fire for a traveller using a newly
registered phone: it escalates, a rule about merchants with high historic decline rates escalates,
and a rule about first-use-in-country escalates. The policy layer applied all three cumulatively,
moving the band from clear to block in two steps. The decline rate for that population is **14%**.

**Day 20.** Escalation capped at one step from discretionary packs. Overall decline rate falls 11%
with no measurable change in catch rate.

**Why shadow mode did not catch it.** Shadow evaluated the rule's own firing population and
precision in isolation. **The failure was an interaction, and interactions are invisible to
per-rule shadow evaluation.** Pairwise co-occurrence analysis now runs nightly.

`R-02`, `R-07`, and the one-step escalation cap that doc
[03](03-rules-and-why-they-never-go-away.md) recommends.

---

## CS-5 · The nine hours of frozen features

**Tuesday, 09:04.** A network policy change in the platform team's quarterly maintenance removes
the aggregation job's write path to the online feature store. Reads are unaffected.

**09:04 onwards.** The job continues consuming its topics and computing correctly. Writes fail. The
failure is counted in a metric named `store_write_errors` and logged. Neither is alarmed.

**All day.** Every scoring decision uses feature values frozen at 09:00. Velocity counters do not
advance; a card making forty transactions appears to have made none since 09:00.

**18:11.** The daily operations review notices catch rate for the day tracking 6 points low.

**18:30.** A `last_updated` timestamp on a sampled key is nine hours old.

**18:34.** Network policy corrected. Short-window rebuild completes at 18:38; 30-day windows at
20:35.

**The cost.**

```
Degraded window:                     9 h 30 min
Transactions:                        486 × 34,200 = 16.6 million
Expected fraud:                      2,160
Catch rate during the window:        87% against 93%
Additional fraud let through:        2,160 × 0.06 = 130
Cost:                                130 × $179 = $23,270
```

**The instructive part is how small the loss is relative to how alarming the failure sounds.** Nine
hours of stale features cost $23,270, because the model's other 248 features still worked and
because 09:00 values are not *wrong*, merely old. This is worth knowing for the inverse reason too:
**it means the detection pressure on this failure mode is weak, which is exactly why it ran for
nine hours.** The freshness heartbeat that now exists alarms at 5 seconds.

`P-05`, `X-03`, `O-03`.

---

## CS-6 · Northwind's merchant at 1.4% and the fine schedule

**January.** A merchant onboards: a digital-goods seller with a plausible business and a modest
projected volume. Underwriting approves.

**March.** Volume is 11× the projection. Chargeback ratio 0.31%, below the 0.65% internal ceiling.
The account manager is pleased.

**May.** Chargeback ratio 0.78%. An automated alert fires. The merchant explains it as a
subscription-billing descriptor problem and commits to a fix.

**June.** 1.4%. The card network places the merchant into its monitoring programme. Northwind is
now accruing monthly fines and owes a remediation plan.

**July.** Investigation finds the merchant was knowingly accepting stolen-card traffic, with payouts
structured to stay under the review threshold. The merchant stops responding. Northwind holds
$140,000 of unpaid-out balance against $1.9 million of incoming chargebacks.

**The three failures.** **Underwriting and transaction monitoring were separate systems** — the
11× volume deviation from projection was visible in March in one system and the chargeback trend in
another, and nobody joined them. **The ratio was treated as a metric rather than a constraint**
(`E-06`), so a rising trend did not trigger action until a threshold was crossed. And **the payout
schedule was not risk-adjusted**: a merchant whose chargeback ratio is rising should have payouts
held longer, which is the only control that actually bounds the loss, and it is a product decision
the risk team did not own.

`E-06`, `G-01` on the linkage that eventually identified the related merchant accounts, and doc
[09](09-graphs-rings-and-linked-entities.md)'s bust-out discussion.

---

## CS-7 · The flatmate

**Day 0.** A customer of nine years, perfect history, is declined on a $240 online purchase. They
call; the agent can see a risk score and a generic reason and cannot reverse it. The customer pays
with another bank's card.

**Day 3.** Declined again. Calls again. Same outcome.

**Day 11.** Formal complaint.

**Day 24.** Investigation finds the driver was `distance_to_nearest_known_fraud = 1`. The edge is a
shared postal address with a former flatmate who committed fraud four months earlier and moved out
two years before that. The address edge was created from an old application record.

**Day 26.** The complaint is upheld. The customer closes the account.

**Day 31.** A review finds 2,900 customers with a materially elevated score driven primarily by an
address edge, of whom 410 have had at least one decline attributable to it.

**The changes made.** Address edges excluded from decline-affecting features entirely, costing 0.9
percentage points of catch rate; known-fraud influence decayed with a 45-day half-life; association
features made incapable of moving a decision from clear to block on their own; and
association-driven declines routed to human review with the subgraph visible.

**The point.** The most powerful feature in the system is a statement about somebody else. The
0.9 points of catch rate was the right price and the complaint is what established it.

`G-03`, `C-01`, `C-03`, `I-11`.

---

## CS-8 · Swiftline, the forty minutes on the phone

**14:12.** A Swiftline customer receives a call from somebody presenting as their bank's fraud team,
using a spoofed number and a plausible script referencing a real recent transaction.

**14:48.** The customer, following instructions, initiates a transfer of £9,400 to a "safe account".

**14:48:02.** Swiftline's scoring sees: correct device, correct location, correct biometric
authentication, normal network, and a customer who has banked here for six years. Its device and
behavioural features are all at their most reassuring values. The payee is new. The amount is far
above the customer's distribution and is not round. The customer has been on a call for 36 minutes.

**14:48:03.** The combined score places it in the intervention band. The app presents a full-screen
interruption: "Is someone on the phone telling you to make this payment? We will never ask you to
move money to a safe account. If someone has told you to, this is a scam."

**14:48:40.** The customer cancels the transfer.

**14:52.** The customer calls the bank on the number from the back of their card. The case is
confirmed as an attempted scam. The payee identifiers are pushed to the scheme watchlist within
four minutes.

**Over the following nine days.** The same payee account appears in 31 further attempts across 14
institutions, all blocked by the watchlist entry.

**Why this is in the collection.** **The detection did not come from any of the signals the card
playbook relies on, and the response was not a decline.** The signal was call state plus payee
novelty plus amount deviation; the response was a specific, named warning that let the customer
make the decision; and the highest-value outcome was not saving this customer's £9,400 but the 31
subsequent attempts the watchlist entry stopped.

`I-06`, `X-06`, and doc [08](08-device-identity-and-behavioural-signals.md)'s argument that a strong
but indefensible signal should be used to ask a better question.

---

## CS-9 · The holdout that was off for eleven days

**Day 0.** A refactor of the policy layer consolidates several bypass conditions. The holdout
bypass is one of them and is reimplemented with a subtly different condition that is never
satisfied.

**Day 0 to 11.** The holdout decisions are computed and logged with a `held_out: true` flag, exactly
as before, and then applied. From every dashboard's point of view nothing changed. The log even
contains the flag, so a later analysis would conclude the holdout was running.

**Day 11.** A weekly review notices that the measured marginal precision has been unusually stable
— three decimal places unchanged across two weeks — and investigates.

**Day 11.** Found. The bug is corrected in twenty minutes.

**The consequences, which are larger than the eleven days suggest.** The holdout's purpose is to
measure the blocked region, and for eleven days there was no sample from it. That is 154 missing
fraud events and 2,013 missing would-have-been-declined observations, which is a material gap in
the training data for the region the model is least certain about, and it cannot be reconstructed.
**The fairness testing for that period is also void**, because false-decline rates by group depend
on the same sample.

**The changes made.** Holdout sample volume is now a first-class alarmed metric, compared against
expectation (0.3% of decisions, ±15%) every hour. The flag is set by the component that applies the
bypass rather than by the component that computes it, so the flag cannot be true when the bypass did
not happen. And an end-to-end test asserts that a transaction in the holdout hash range with a
blocking score is actually approved, which runs on every deploy.

`M-03`, `O-10`.

---

## CS-10 · The four years in which the web channel quietly got worse

This one has no timeline, which is the point.

**Year 1.** Ironvale's web channel catch rate is 91%, 2 points below the app channel. The gap is
attributed to the app's possession signals and considered understood.

**Year 2.** 90%. The weekly retrain absorbs a steady decline in the availability of soft
fingerprint attributes. No alarm; each week's distribution is within tolerance of the last week's.

**Year 3.** 88%. A browser release removes font enumeration. A second quantises audio-stack output.
Device linkage confidence falls below 55%. Still no alarm — the feature distribution monitoring
compares against last week, and nothing moves meaningfully in any one week.

**Year 4.** 85%. The gap to the app channel is now 8 points. A new head of fraud asks why.

**Investigation.** The share of web sessions linkable with confidence to a previously seen device
has fallen from **71% to 43%**. Six features in the top thirty by importance have lost most of their
discriminative power. No code changed; no defect was ever filed; every weekly retrain performed
correctly.

**The point, and the reason this closes the chapter.** **A system that adapts weekly to a slowly
eroding input will hide the erosion perfectly.** The retrain is not the problem — it is doing its
job, which is to make the best model from the data available. The problem is that "the data
available" was a shrinking quantity and nothing was watching that quantity.

The control that would have caught it in year 1 is one quarterly report: **signal availability by
family, trended over months, not weeks.** It is the cheapest item in doc
[18](18-golden-defaults-and-checklists.md) and the one whose absence is most expensive, because the
cost accrues silently for years.

`I-01`, `O-03`, `A-01`'s distinction between the four kinds of drift.

---

## What the ten have in common

Reading them together, five patterns recur and are worth naming:

1. **Eight of the ten were detected by a human noticing something odd, not by an alarm** — an empty
   queue, a suspiciously stable number, a relationship manager's escalation, a new manager's
   question. In every case an alarm was possible and cheap and did not exist.
2. **The detection delay, not the failure, determined the cost.** CS-1 cost $14,499 over 23 minutes
   and CS-10 cost perhaps $40 million over four years, and the failure in CS-10 was less severe.
3. **Four of the ten involved a protection that existed and was configured permissively** — the
   one-sided decline alarm, the counted-but-unalarmed write error, the uncapped escalation, the
   client API that conflates absent with unreadable. These are not missing features; they are
   defaults nobody revisited.
4. **Three involved a correct component behaving correctly in a situation nobody had considered** —
   the weekly retrain in CS-10, fail-open in CS-3, the challenge-unavailable fallback in doc
   [15](15-the-failure-catalogue-and-playbook.md)'s `X-04`. The lesson is that the dangerous
   failures are rarely bugs.
5. **The two incidents with the best outcomes — CS-8's intervention and CS-7's remediation — were
   improved by taking something away**: not declining, and removing a feature worth 0.9 points of
   catch rate. Restraint was the correct engineering decision in both.
