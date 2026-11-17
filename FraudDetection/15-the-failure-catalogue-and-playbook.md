# The Playbook: What to Do in the First Ten Minutes, and the Actions That Make It Worse

This document is for when something is happening now. It assumes you have read nothing else, so it
repeats the essential numbers rather than referring you elsewhere, and it puts the triage order
first.

Scenarios are `X-01` through `X-09` and are platform-level operating failures — the ones that are
not about a model or a feature but about the system as a whole. Everything else is cross-referenced
to its owning document.

---

## The first question, always

**Did we change anything in the last 72 hours?**

Ask it before forming any hypothesis. Across Ironvale's incident history, **61% of apparent fraud
anomalies were caused by a change** — their own or an upstream team's — and the median time to
resolution was 14 minutes when the change was identified first and 3 hours 40 minutes when it was
identified last. The annotated dashboard from doc
[14](14-observability-and-operating-the-service.md)'s `O-11` answers this in seconds; without it,
check in this order: fraud service deploys, model and threshold and rule changes, feature-pipeline
deploys, upstream producer deploys (authorization, device SDK, core banking), infrastructure and
configuration changes.

---

## Triage: identifying the shape in two minutes

Six shapes cover nearly everything. Identify which by the answers to three questions: **is the
decline rate up or down, is it narrow or broad, and did the score distribution move?**

| Decline rate | Breadth | Score distribution | Shape | Go to |
|---|---|---|---|---|
| Up sharply | Narrow | Unchanged | A rule is firing on a population it should not | [`R-01`, `R-02`, `R-08`](03-rules-and-why-they-never-go-away.md) |
| Up sharply | Broad | Shifted up | Feature degradation, or a bad model or threshold release | `X-01` below, [`D-05`](02-the-decision-path-and-the-latency-budget.md) |
| Up sharply | Broad | Unchanged | Threshold or policy change; or step-up channel failing so challenges become declines | [`E-09`](07-thresholds-and-the-economics-of-errors.md), `X-04` |
| Down | Narrow | Unchanged | Attack in progress, or an allow-list entry | [`A-01`](12-adversaries-drift-and-the-arms-race.md), [`R-06`](03-rules-and-why-they-never-go-away.md) |
| Down | Broad | Compressed or flat | Model has stopped discriminating, or features are absent and imputed | `X-01`, `X-02` |
| Unchanged | — | Unchanged | Look at freshness and availability; the system may be deciding on stale data | [`P-05`](05-the-streaming-pipeline.md), `X-03` |

**The last row is the dangerous one.** "Nothing has changed" is the signature of the failures that
do their damage silently, and it is the state in which most of this collection's worst scenarios
sit.

---

## The action catalogue, with blast radius and reversal time

Know these before you need them. The column that matters at 03:00 is the last one.

| Action | Time to effect | Blast radius | Reversal | Who may do it |
|---|---|---|---|---|
| Raise or lower a segment threshold one band | 2 min | That segment | 2 min | On-call, up to one band |
| Enable a pre-approved incident rule | 2 min | As scoped | 2 min | On-call |
| Suppress a rule | 2 min | That rule's population | 2 min | On-call |
| Switch a segment from decline to step-up | 2 min | That segment | 2 min | On-call |
| Roll back the model to the previous artefact | 90 s | Global | 90 s | On-call |
| Roll back the calibration layer only | 90 s | Global, score scale | 90 s | On-call |
| Disable a whole rule pack | 2 min | Large | 2 min | Fraud manager |
| Block a merchant or BIN range | 2 min | Very large and blunt | 2 min, but damage done | Fraud manager |
| Enter static fallback deliberately | 1 min | Global | 1 min | On-call |
| Set the caller to fail closed | 1 min | Global, catastrophic | 1 min | **Fraud manager plus operations sign-off** |
| Rebuild feature state, 24-hour windows | 4 min | Global | n/a | On-call |
| Rebuild feature state, 30-day windows | 1 h 57 min | Global | n/a | On-call |
| Ship a new rule | 1–4 h | As designed | 2 min once shipped | Fraud team |
| Ship a retrained model | 3 days minimum | Global | 90 s | Fraud team plus validation |

**Two structural points about this table.** Nearly everything reversible in under two minutes is
available to the on-call engineer, and nearly everything slow is not an incident response at all.
And the one genuinely catastrophic action — fail closed — requires two sign-offs including
operations, for the reason in `X-05`.

---

## ⚠️ The consolidated list of actions that make things worse

Collected from across the collection. Every one of these is the intuitive move in its situation.

1. **⚠️ Switching to fail closed during a scoring outage.** 583,200 declines per 20 minutes at
   Ironvale, of which 583,124 are good customers, at a direct cost of $1.12 million and a
   contact centre that cannot absorb 1% of the volume. Use the static fallback instead. Doc
   [02](02-the-decision-path-and-the-latency-budget.md), `D-04`.
2. **⚠️ Retrying harder when the fraud service is slow.** Retries are pure amplification under
   saturation; a 300 ms timeout with two retries triples the load on the thing already failing.
   Turn retries *off*. Doc [02](02-the-decision-path-and-the-latency-budget.md), `D-06`.
3. **⚠️ Hard-blocking a card-testing attack immediately.** Tells the attacker to move to another of
   your 40,000 merchants, where you are not watching. Throttle first, instrument, then block. Doc
   [12](12-adversaries-drift-and-the-arms-race.md), `A-02`.
4. **⚠️ Shipping a narrow rule as the first response to an attack.** Slower than a threshold change,
   covers only the observed variant, and tells the adversary exactly what you detected. Lower the
   segment threshold or escalate to step-up first. Doc
   [12](12-adversaries-drift-and-the-arms-race.md), `A-04`, `A-12`.
5. **⚠️ Resetting a stream consumer's offset without resetting its state.** Reapplies hours of
   events to state that already contains them, inflating every velocity counter permanently. Doc
   [05](05-the-streaming-pipeline.md), `P-01`.
6. **⚠️ Correcting inflated aggregates in place.** Rebuild into a second keyspace and swap a
   pointer; in-place mutation during a rebuild leaves the system serving a mixture of old and new
   state with no way to tell which. Doc [05](05-the-streaming-pipeline.md), `P-01`.
7. **⚠️ Applying an analyst set-action to a component without checking its size.** A graph artefact
   component can hold 14,000 entities. Require a second approval above 50. Doc
   [11](11-case-management-and-the-human-loop.md), `H-02`; doc
   [09](09-graphs-rings-and-linked-entities.md), `G-01`.
8. **⚠️ Making the decision log best-effort and dropping it under load.** It is the only record of
   why you declined a customer, and it is needed for the dispute, the regulator and the next model.
   Buffer and backpressure; never drop. Doc
   [02](02-the-decision-path-and-the-latency-budget.md), `D-12`.
9. **⚠️ Loosening thresholds during a volume peak to protect approval rates.** Exactly what the
   attack calendar predicts the adversary is waiting for. Freeze threshold changes in both
   directions for 72 hours around known peaks. Doc
   [12](12-adversaries-drift-and-the-arms-race.md), `A-07`.
10. **⚠️ Disabling the holdout to stop the "fraud we are knowingly approving".** It is the
    measurement instrument for the entire system, including your false-positive rate and your
    fairness testing. Doc [06](06-models-labels-and-training.md), `M-03`; doc
    [14](14-observability-and-operating-the-service.md), `O-10`.
11. **⚠️ Deleting a never-firing rule.** A rule can show zero fires because the attack it blocks is
    currently not being attempted. Move it to shadow. Doc
    [03](03-rules-and-why-they-never-go-away.md), `R-12`.
12. **⚠️ Autoscaling the stateful aggregation layer during a traffic peak.** Scaling moves state,
    which makes the job slower before faster, during the hour you least want stale features. Doc
    [05](05-the-streaming-pipeline.md), `P-04`.
13. **⚠️ Removing a proxy feature to fix a fairness finding, and then declaring it fixed.** The
    model reconstructs it from 411 others; you have lost diagnosability and changed nothing. Doc
    [13](13-explainability-compliance-and-fairness.md), `C-08`.
14. **⚠️ Giving a precise explanation of the detection to the declined party.** A fraudster is
    entitled to the same explanation as a customer. Explain at the level of the factor, not the
    threshold. Doc [13](13-explainability-compliance-and-fairness.md), `C-02`.

---

## X-01 through X-09: platform-level failures

### X-01 · The model release that is worse and passed the canary

**What you see.** A model at 50% of traffic. Offline metrics were better. Decline rate is within
tolerance. Three weeks later, catch rate on one segment is 9 points worse.

**Mechanism.** The canary gates watched aggregate decline rate, latency and score distribution, all
of which were fine, because the degradation was confined to one segment that is 4% of volume.
Aggregate tolerances are wide enough to hide a segment-level catastrophe.

**Recover.** Roll back the artefact (90 seconds) and investigate from the shadow comparison.

**Prevent.** Canary gates must be **per segment**, with tolerances scaled to each segment's own
variation. Ironvale gates on: per-segment decline rate, per-segment score distribution shift, and
per-segment agreement rate against the champion, with the tightest tolerance on the segments that
carry the most fraud rather than the most volume. The two are different sets and gating on volume
is the error.

### X-02 · The whole feature store returned nothing, and the system approved everything

**What you see.** Decline rate falls to near zero. No errors. Latency improves.

**Mechanism.** The feature store became unreachable. The client returned empty maps. The imputation
layer filled defaults. The model, given a vector that looks like a brand-new quiet card, produced
low scores for everything. The system approved its way through the outage with a healthy dashboard
and faster-than-usual responses.

**The latency improving is the tell.** A fraud system that gets faster has usually stopped doing
something.

**Recover.** Enter static fallback immediately (1 minute). Do not wait for the feature store — a
cached per-card risk tier and the block list is a far better decision than a model scoring on
nothing.

**Prevent.** Everything in doc [02](02-the-decision-path-and-the-latency-budget.md)'s `D-05`: absent
must not be representable as zero, the policy layer rather than the model owns degradation, and
feature availability is an alarmed metric. Plus the one that catches it even if all of those fail:
**alarm on decline rate falling as well as rising.** Most fraud alerting is one-sided, because
over-blocking is what gets people shouted at, and the one-sided alarm misses the failure that
costs more.

### X-03 · The pipeline that stopped and nobody noticed for nine hours

**What you see.** Catch rate for the day is 6 points down. The aggregation job's dashboards are
green.

**Mechanism.** The job was running and consuming, but its writes to the online store were failing —
a credential expiry, a network policy change, a full disk — and the write failures were logged and
counted but not alarmed. Features froze at their 09:00 values and the scorer used them all day.

**Confirm it.** The freshness heartbeat (doc [05](05-the-streaming-pipeline.md), `P-05`) shows it
immediately. Without it: pick a key, read it, compare its `last_updated` against now.

**Recover.** Rebuild short windows first — 4 minutes for the 24-hour aggregates, which carry most of
the detection power — and let the 30-day windows follow over the next two hours.

**Prevent.** The freshness heartbeat, alarmed, and the write-failure counter alarmed independently.
**A counter that is logged but not alarmed is a counter that exists to explain the incident
afterwards**, which is a real but much smaller value than preventing it.

### X-04 · The step-up channel outage that became a decline storm

**What you see.** Decline rate triples. Score distribution unchanged. Nothing in the fraud stack
changed.

**Mechanism.** The push-notification provider is down. 1,155,000 daily step-ups cannot be issued.
The policy layer's rule for "channel cannot challenge" falls back to the configured action, which
for most segments is decline. The fraud system is working exactly as designed and is declining a
million transactions.

**Recover.** Switch the affected segments' challenge-unavailable fallback from decline to approve
for the duration, accepting the fraud exposure, which is bounded and calculable:

```
Step-ups affected per hour:        48,125
Implied fraud rate in the band:     1.4%
Fraud exposure per hour:            674 transactions × $179 = $120,600
Against declining them:            48,125 × 0.986 = 47,451 good customers × $1.92 = $91,100
                                   plus a contact centre receiving 11,000 calls
```

So approving is marginally worse on direct cost and dramatically better on everything else — and the
right answer is usually a middle course: approve below the segment's own amount threshold, decline
above it, which captures most of the exposure for a fraction of the customer harm.

**Prevent.** Two challenge channels, always, with automatic failover, and the fallback action per
segment reviewed as a deliberate decision rather than inherited from a default. And **monitor
challenge delivery success as a fraud metric**, not only as a notification-platform metric, since
it is the fraud system that fails when it fails.

### X-05 · The incident that became a company incident

**What you see.** A well-intentioned flip to fail closed at 02:00. By 02:40 the contact centre has a
queue it cannot clear in a day, social media has noticed, and the payments regulator's duty officer
has been informed.

**Mechanism.** The contact centre's capacity is a fraction of a percent of the decline volume a
global fail-closed produces. Ironvale's: 40 concurrent calls, roughly 3,200 calls absorbable in a
night shift, against 583,000 declines in 20 minutes producing a conservative 134,000 calls.

**Prevent.** The two-sign-off requirement on fail closed, and — more useful — **a published table of
what each action does to contact-centre volume**, so that the person making the decision at 02:00 is
looking at the number rather than estimating it. Ironvale's incident checklist requires predicted
call volume to be stated before any action expected to move decline volume by more than 5%.

### X-06 · The mule recall race

Swiftline-specific and included because it is a genuinely different operational shape. A scam is
confirmed. The money has settled. The median time from receipt to full withdrawal at a mule account
is **under 9 minutes**.

**The runbook is a race and it is mostly about who you can reach:**

```
t+0      Confirmation received
t+0:30   Automated freeze request to the receiving institution via the scheme's
         recall mechanism — automated, not a phone call
t+1:00   Freeze the sending customer's remaining exposure: block further transfers
         to that payee and to the payee's known network (doc 09)
t+2:00   Push the payee identifiers to the scheme's shared watchlist — this is what
         protects the next victims, and it is the highest-value action in the runbook
t+5:00   Human escalation to the receiving institution if no automated response
t+15:00  If funds not frozen, they are gone; the process becomes reimbursement
```

**The thing worth taking from this even if you never work on instant payments: the highest-value
action in the runbook is not recovering this victim's money, which usually fails. It is publishing
the payee identifiers so the next forty victims are protected.** Design the runbook around the
action with the largest expected value, not around the most emotionally urgent one.

### X-07 · The region that diverged

Northwind-specific. A region's feature store stopped receiving cross-region updates. For six hours,
a card could transact in all three regions and each region saw only its own history, so
cross-region velocity was zero everywhere.

**Confirm it.** A synthetic probe that writes in one region and reads in the others, continuously,
reporting propagation delay per region pair. This is the multi-region equivalent of the freshness
heartbeat and the same argument applies.

**Recover.** The aggregates will converge once replication resumes; the decisions made during the
window will not. Route the affected period's high-value approvals to retrospective review.

**Prevent.** Alarm on cross-region propagation delay per region pair, and treat "no cross-region
view" as a degradation requiring a policy response — widening the step-up band — rather than as a
silently absent feature.

### X-08 · The incident where the fraud team and the payments team disagree

**What you see.** The fraud team sees an attack and wants to tighten. The payments team sees an
approval-rate drop and wants to loosen. Both have dashboards supporting their position. Forty
minutes pass.

**Mechanism.** Two teams optimising two halves of one trade-off, with no shared objective and no
pre-agreed arbitration.

**Prevent.** This is an organisational control and it is as important as any technical one.
Ironvale's: a single named incident commander with decision authority regardless of which team they
come from (the general pattern is in
[`../AISystemDesign`](../AISystemDesign/README.md)'s incident-commander material); a **shared
objective stated in advance** — the expected-cost framing of doc
[07](07-thresholds-and-the-economics-of-errors.md), so both teams argue about inputs to one formula
rather than about two different metrics; and a **pre-agreed default**, which at Ironvale is
"escalate to step-up rather than decline, and keep approving below the segment amount threshold",
because it is the action both teams can live with and it can be taken immediately while the
argument continues.

### X-09 · The post-incident review that produces a rule instead of a control

**What you see.** Every incident review produces a new rule. The rule estate grows. The same class
of incident recurs in a different form.

**Mechanism.** A rule addresses the instance; a control addresses the class. "Add a rule for this
merchant category" is a response to an instance. "Declared-versus-actual fire-rate alarming" is a
response to the class that includes it.

**Prevent.** Require every incident review to answer two questions separately: **what stops this
exact thing** and **what would have detected the class of thing this belongs to**. The second
answer is usually a monitoring or a process change rather than a detection rule, and it is where the
compounding value is. Checking this collection's scenarios against each other, **the great majority
of the 130-odd failures would have been caught by about a dozen controls**, which are collected in
doc [18](18-golden-defaults-and-checklists.md).

---

## What to take away

1. **Ask "did we change anything in the last 72 hours" before forming any hypothesis.** 61% of
   apparent fraud anomalies are self-inflicted, and identifying the change first takes 14 minutes
   against 3 hours 40 minutes when it is identified last.
2. **Triage on three questions**: is the decline rate up or down, is it narrow or broad, and did the
   score distribution move. Six shapes cover nearly everything.
3. **"Nothing has changed" is the dangerous triage answer**, because it is the signature of the
   failures that do their damage silently. Check freshness and availability next.
4. **Keep an action catalogue with blast radius and reversal time**, and make everything reversible
   in under two minutes available to the on-call engineer. The one catastrophic action — fail closed
   — needs two sign-offs including operations.
5. **Alarm on the decline rate falling, not only rising.** One-sided alerting is the norm, because
   over-blocking is what gets people shouted at, and it misses the failure that costs more.
6. **A fraud system that suddenly gets faster has usually stopped doing something.**
7. **Rebuild short windows first.** Four minutes gets back the 24-hour aggregates that carry most of
   the detection power; the 30-day windows can follow over two hours.
8. **A step-up channel outage is a decline storm**, because "cannot challenge" falls back to the
   configured action. Run two channels, and review each segment's fallback action as a deliberate
   decision rather than a default.
9. **Know your contact centre's capacity as a number before the incident.** 3,200 calls a night
   against 134,000 produced by a 20-minute fail-closed is the arithmetic that stops a fraud incident
   becoming a company incident.
10. **Design a runbook around the highest-expected-value action, not the most urgent-feeling one.**
    In a mule recall, publishing the payee identifiers protects the next forty victims; recovering
    this victim's money usually fails.
11. **Give the incident one commander and one shared objective stated in advance**, so the fraud and
    payments teams argue about inputs to one formula rather than about two dashboards — with a
    pre-agreed default that can be applied while they argue.
12. **Make every incident review answer two questions**: what stops this exact thing, and what would
    have detected its whole class. About a dozen controls would have caught the great majority of
    the failures in this collection.
