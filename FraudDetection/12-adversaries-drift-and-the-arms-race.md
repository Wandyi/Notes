# The Adversary: Why This Is Not an Ordinary Prediction Problem, and What Drifts

Every other machine-learning system in a bank predicts something that does not care what the model
thinks. Churn does not read your model. Default risk does not probe your threshold. Demand
forecasting does not pay somebody to find the boundary of your decision surface.

Fraud does all of those things. **The data-generating process is an intelligent agent whose
objective is the negation of yours**, which breaks the assumption — stationarity — that almost every
technique in the previous documents quietly relies on. This document covers what the adversary
actually does, how to tell adversarial adaptation from ordinary drift, and the specific design
decisions that follow from being in a game rather than in a regression.

Scenarios are `A-01` through `A-12`.

---

## Four things drift, and they need different responses

The word "drift" covers four distinct phenomena. Conflating them produces the wrong fix.

**Population drift.** The customers change — new segments acquired, a product launched, a
geographic expansion, a demographic shift. The relationship between features and fraud is
unchanged; the distribution of features has moved. **Response:** retrain. This is the benign case
and it is the one most drift literature is about.

**Behavioural drift.** Legitimate behaviour changes. Contactless limits are raised, so a transaction
size that was unusual becomes ordinary. A pandemic moves everybody online in three weeks. A
buy-now-pay-later product makes transaction splitting normal. **Response:** retrain, and recalibrate
urgently, because the base rate within segments has moved and your thresholds are stale.

**Adversarial drift.** The fraudster changes in response to you. **Response:** retraining is partly
self-defeating, because you are fitting to a behaviour the adversary has already abandoned; the
response is architectural, and most of this document is about it.

**Your own drift.** You changed something — a rule, a threshold, a feature definition, an upstream
service — and the resulting shift looks like the world changing. **Response:** look at your own
change log first. This is the most common cause of an apparent drift event and the first hypothesis
to eliminate, and it is why the change annotations in doc
[14](14-observability-and-operating-the-service.md) are the highest-return observability
investment available.

### A-01 · Telling adversarial drift from the other three

**What you see.** Catch rate falls 6 percentage points over eleven days.

The diagnostic questions, in order, and what each distinguishes:

| Question | If yes |
|---|---|
| Did we change anything in the last three weeks? | Your own drift. Check first, always. |
| Is the change concentrated in a narrow slice — one merchant category, one channel, one country, one amount band? | Adversarial. Population and behavioural drift are broad; attacks are narrow. |
| Did the *fraud* distribution move while the *legitimate* distribution stayed put? | Adversarial. Population drift moves both. |
| Did the shift appear over days rather than weeks or months? | Adversarial. Organic drift is slow. |
| Does the new pattern sit just outside a specific rule's or threshold's boundary? | Adversarial, and specifically probing — see `A-03`. |
| Did a competitor or peer institution see it too? | Adversarial, and industry-wide; the consortium channel is how you find this out. |

**The single most discriminating signal is narrowness.** Ordinary drift is a tide; an attack is a
spike in one place. A monitoring system that looks only at aggregate metrics will see a 6-point
drop and not be able to tell you which of four things it is; one that computes metrics per segment
answers the question in minutes. Doc
[14](14-observability-and-operating-the-service.md) builds the per-segment view for this reason.

---

## What the adversary actually does

### A-02 · Card testing, the reconnaissance phase

Before using a stolen card for anything valuable, an attacker establishes it is live. The method is
a tiny authorization against the least-friction endpoint available — a donation form, a gift-card
top-up, a trial subscription — because such endpoints accept card-not-present transactions with
minimal validation and the merchant has no incentive to add friction to a $1 donation.

The signature at Northwind Pay, which is where it lands:

```
One merchant, 14,000 authorization attempts in 40 minutes
Average amount $1.07
Decline rate 91% (the cards are mostly dead)
Distinct cards: 13,800 — almost no repeats
Distinct BINs: 340 — the attacker's list spans many issuers
Device: one fingerprint, or a few, often with mismatched attributes
```

**Detection is easy and the response is where the difficulty is.** Northwind's controls:

- **Rate-limit by merchant and by card-count-per-merchant**, not just by card. The anomaly is many
  cards at one merchant, which is invisible in card-keyed features.
- **Throttle rather than block**, initially, because a hard block tells the attacker immediately and
  they move to another of your 40,000 merchants. Slowing responses to several seconds makes the
  enumeration uneconomic while keeping the attacker in a place you are watching.
- **Require a minimum amount or a CAPTCHA on endpoints that accept arbitrary small amounts**, which
  is a merchant-side change Northwind must persuade them to make, and the persuasion is easier once
  you show them their own authorization-ratio costs.
- **Share the BIN list and device fingerprints with the consortium immediately**, because the same
  list is being tested against every acquirer that week.

**For the issuer side, the important realisation is that a card tested today is used tomorrow.** A
$1.07 authorization that was *declined* is a strong predictor that the card will be attacked, and
Ironvale treats "this card appeared in a probable enumeration pattern in the last 72 hours" as a
feature — contributing 2.4 percentage points of catch rate for essentially no cost. Most issuers do
not compute it, because the signal lives in declines, which teams instinctively treat as
uninteresting.

### A-03 · Probing for the boundary

**What you see.** A sequence of transactions on related cards at amounts of $480, $495, $505, $512,
$498, $501 — clustered tightly around a threshold.

**Mechanism.** The attacker is performing a binary search for your limit. Each attempt costs them
almost nothing and tells them something, and after a dozen attempts they know your rule's boundary
to within a few dollars. They then operate just inside it, indefinitely.

This is the fundamental weakness of any **hard, deterministic, static boundary**, and it applies to
amount limits, velocity limits, count limits and geographic rules alike.

**Prevent, and these are real techniques rather than aphorisms:**

- **Randomise the boundary slightly.** If the amount limit is $500 ± a per-card random offset of up
  to 8%, drawn deterministically from a hash of the card token so it is stable per card and
  reproducible, the attacker cannot learn a single number and cannot test on one card and exploit on
  another. The cost is a small loss of precision and a large loss of attacker information.
- **Make the response to a near-boundary transaction different from the response to a far-from-
  boundary one.** A transaction at $498 after three declines at $505 is not a $498 transaction; it
  is a probe, and "distance from a recently declined attempt on a related entity" is a feature that
  turns probing into evidence.
- **Detect the probe pattern itself.** A tight cluster of attempts straddling any threshold is a
  signature in its own right, independent of which threshold. Ironvale has one rule — "three or more
  attempts on related entities within 10% of each other and within 20% of any active decision
  boundary, in 30 minutes" — which is a general-purpose probe detector and has caught eleven
  distinct campaigns.
- **Prefer a continuous score to a hard limit where you can.** A probability that rises smoothly
  with amount is much harder to binary-search than a cliff, because there is no cliff to find. This
  is a genuine argument for the model over the rule and it is the counterweight to doc
  [03](03-rules-and-why-they-never-go-away.md)'s argument in the other direction.

### A-04 · The attack that works because you deployed a fix

**What you see.** A rule is shipped at 11:00 to stop an ongoing attack. Within 90 minutes a variant
appears that the rule does not match.

**Mechanism.** Deploying a narrow fix tells the adversary precisely what you detected. If your rule
keys on "amount over $500 at merchant category 5732 from a new device", the attacker learns, from
the pattern of what now fails, which of those four conditions to change. **Every action you take is
an observation they get for free**, and a narrow rule is a more informative observation than a broad
one.

**The strategic response is to make your controls less informative:**

- **Prefer broad to narrow where the precision cost is acceptable.** A rule keyed on one of four
  attributes rather than all four is harder to diagnose from the outside and covers the variants.
- **Do not respond at the same granularity you were attacked at.** If the attack is on one merchant
  category, respond with a control covering the behaviour rather than the category.
- **Delay and batch visible responses where the ongoing loss permits it.** Shipping four
  countermeasures at once, at an unpredictable time, gives much less information than shipping
  four over four days. This trades current loss against future information and the trade is
  sometimes worth making — a point that is only available to somebody thinking of it as a game.
- **Use step-up rather than decline where it works.** A declined transaction is a clear signal; a
  challenged transaction that the fraudster fails looks, from their side, much more like ordinary
  friction, and gives them less to work with.

### A-05 · The slow ramp that the adaptive baseline absorbs

**What you see.** Nothing, for six weeks. Then a quarterly review finds a merchant whose fraud rate
rose from 0.1% to 3.8% without ever triggering an alert.

**Mechanism.** Every adaptive baseline — "alert when this merchant's fraud rate exceeds twice its
trailing average" — can be defeated by ramping slower than the baseline adapts. With a 30-day
trailing window, an attacker who increases volume 10% a week never triggers it, because the
baseline follows them up.

This is the general form of doc [03](03-rules-and-why-they-never-go-away.md)'s `R-08`. **An adaptive
baseline is an attack-tolerance setting, not a free improvement**, and the tolerance is exactly "any
ramp slower than the adaptation rate".

**Prevent:** pair every adaptive baseline with an **absolute ceiling** that does not adapt. "Alert at
twice the trailing average *or* above 0.9% in absolute terms, whichever comes first." The absolute
limit is what catches the slow ramp, and choosing it is a business decision — the point at which
this is unacceptable regardless of history — rather than a statistical one.

### A-06 · The adversary who knows your model, because they probed it

A sufficiently motivated adversary with a few thousand cheap probes can learn a great deal about a
model's decision surface — which features matter, roughly where the boundary is, and what
combinations pass. This is model extraction, and card-testing infrastructure makes the probes
essentially free.

The mitigations, honestly assessed:

- **Rate-limit and detect probing** (`A-03`), which raises the cost. This is the main defence and it
  is partial.
- **Do not expose the score.** Returning a reason code is necessary; returning a numeric risk score
  to a merchant is handing the adversary a gradient. Several processors do return scores to
  merchants, which is a product feature with a security cost that is rarely priced.
- **Vary the model.** Multiple models with different feature subsets, selected per transaction by a
  hash, mean that what passes for one transaction does not reliably pass for the next. This is
  genuinely effective and it costs you calibration consistency and explainability, which in a
  regulated environment may be too much to pay. Ironvale does not do it; Riverbend, which is
  unregulated, does.
- **Keep some signals unobservable.** A feature the adversary cannot observe or manipulate —
  consortium data, the graph, the card's own long history — cannot be probed around. **This is the
  strongest argument for investing in the signal families from docs
  [04](04-features-and-the-feature-store.md) and
  [09](09-graphs-rings-and-linked-entities.md) rather than in model sophistication**: the adversary
  can adapt their behaviour, and they cannot adapt what forty other issuers have reported about
  the card.

### A-07 · The seasonal attack calendar

Attacks are not uniformly distributed in time and the pattern is exploitable in both directions.
Volume peaks — Black Friday, major on-sales, the days after a large data breach is disclosed, the
hours after a bank's fraud system has a publicised outage — are when attacks concentrate, because
the noise is highest and the staffing lowest.

**Practice:** staff the review operation to the attack calendar rather than to the volume calendar,
and resist the instinct to loosen controls during a volume peak to protect approval rates. The
pressure to loosen on Black Friday is intense, commercially reasonable, and exactly what the
calendar predicts the adversary is counting on. Ironvale's policy is that threshold changes are
**frozen** for the 72 hours around a known peak, in either direction, unless an incident forces one
— and the freeze is as much about resisting a loosening as a tightening.

---

## A-08 through A-12, briefly

### A-08 · The fraud that moves to the weakest channel

Strengthen card-not-present and fraud moves to account takeover. Strengthen authentication and it
moves to scams, where the customer authenticates willingly. Strengthen the issuer and it moves to
the merchant. **Displacement is the normal outcome of a successful control**, and the metric that
captures it is total loss across all channels rather than catch rate in the channel you improved. A
fraud programme that reports per-channel catch rates and not a consolidated loss figure will
celebrate improvements that moved the money rather than stopping it.

### A-09 · The insider

A meaningful minority of fraud involves somebody with access — a contact-centre agent changing a
contact detail, an analyst allow-listing a merchant, a developer with production access. The
controls are the ordinary ones (least privilege, two-person approval for high-blast-radius actions
per doc [11](11-case-management-and-the-human-loop.md), audit logging) plus one that is specific:
**monitor the fraud system's own operators as entities in the graph.** An analyst who repeatedly
clears cases that later chargeback is a detectable pattern, and the detection belongs in the same
system that detects everything else.

### A-10 · The adversary who uses your appeals process

If a declined customer can get the decline reversed by calling and asserting it was legitimate,
that is a channel. Fraudsters use it, and contact-centre agents under handle-time pressure are
incentivised to resolve the call. **Practice:** an override from the contact centre is a decision
with an owner and a record, it is subject to the same monthly cost report as an allow-list entry
(doc [03](03-rules-and-why-they-never-go-away.md), `R-06`), and overrides that later chargeback are
reported back to the agent and their manager. This is uncomfortable and it is the only thing that
works.

### A-11 · Generative tooling and what it actually changed

The honest assessment, because it comes up and is mostly answered badly. What has genuinely changed:
**scam narratives are better** — fewer language errors, more plausible context, voice cloning making
impersonation viable, synthetic documents passing visual inspection in onboarding. What has not
changed: the *structure* of the fraud. A scam still requires a payment to a payee, a mule account
still receives from unconnected senders, a stolen card still needs testing. **So the signals that
degrade are the content-based ones — language quality, document appearance, voice — and the signals
that hold are the structural and relational ones.** The design implication is the same as `A-06`'s:
weight toward the graph, the velocity, the consortium, and away from anything that depends on
fraudsters being sloppy.

### A-12 · Reacting to an attack without breaking everything

The response playbook is in doc [15](15-the-failure-catalogue-and-playbook.md). The strategic point
here: the available responses have different blast radii and different information leakage, and
choosing well matters.

| Response | Speed | Blast radius | Tells the adversary |
|---|---|---|---|
| Enable a pre-approved shadow rule | 2 min | Narrow | A lot, specifically |
| Lower a segment threshold one band | 5 min | That segment | Little, diffusely |
| Step up instead of declining | 5 min | That segment | Very little |
| Block a merchant or BIN range | 2 min | Wide and blunt | Everything |
| Ship a new rule | 1–4 hours | As designed | A lot, specifically |
| Retrain | 2–6 weeks | Global | Little |

**Lowering a threshold or escalating to step-up in the affected segment is usually the right first
move**, because it is fast, reversible, broad enough to catch variants, and nearly silent. The
instinct to write a precise rule is often wrong as a *first* response: it takes longer, it covers
only the observed variant, and it tells the adversary exactly what you saw.

---

## The strategic posture, stated plainly

Pulling the document together into the position worth taking into an interview:

**You cannot win, and you are not trying to.** The objective is not zero fraud; it is to keep the
adversary's return on effort below what they can get elsewhere. Fraud is a business with
alternatives, and it moves to the easiest available target. This reframes several decisions: a
control that makes attacks uneconomic is better than one that makes them impossible, and being
somewhat harder than your peers is worth more than being absolutely secure.

**Prefer signals the adversary cannot observe or manipulate.** Their behaviour is theirs to change;
your consortium data, your graph, and the card's own history are not. This is the single most
important architectural consequence of adversariality and it argues for spending on features and
data partnerships over model sophistication.

**Prefer controls whose response is uninformative.** Step-up over decline, broad over narrow,
continuous over cliff-edged, randomised over fixed. Every response you make is free intelligence for
them.

**Pair every adaptive mechanism with an absolute limit**, because adaptation is a tolerance for any
ramp slower than the adaptation rate.

**Measure total loss, not per-channel catch rate**, because the normal outcome of a successful
control is displacement.

**And accept that retraining is a response to drift, not to adversaries.** By the time you have
enough labels to retrain on an attack, the attack has changed. The fast loop is rules and
thresholds (doc [03](03-rules-and-why-they-never-go-away.md), hours); the medium loop is features
(doc [04](04-features-and-the-feature-store.md), weeks); the slow loop is models (doc
[06](06-models-labels-and-training.md), a month to know). **A system with only the slow loop is
permanently fighting the last campaign.**

---

## What to take away

1. **Four different things are called drift** — population, behavioural, adversarial, and your own
   changes — and they need different responses. Check your own change log first; it is the most
   common cause.
2. **Narrowness is the most discriminating signal of adversarial drift.** Ordinary drift is broad
   and slow; an attack is a spike in one segment over days. Aggregate metrics cannot tell them
   apart, per-segment metrics can.
3. **A card tested today is used tomorrow.** "Appeared in a probable enumeration pattern in the last
   72 hours" is worth 2.4 points of catch rate and most issuers do not compute it, because the
   signal lives in declines.
4. **Throttle card testing rather than blocking it**, at first — a hard block tells the attacker to
   move to another of your 40,000 merchants, while slow responses make enumeration uneconomic while
   keeping them somewhere you are watching.
5. **Any hard static boundary will be binary-searched.** Randomise it per entity with a stable hash,
   detect the probe pattern itself as a general signature, and prefer a continuous score to a cliff
   — which is the real counterweight to the case for rules.
6. **Every response you make is free intelligence for the adversary**, and a narrow rule is more
   informative than a broad one. Prefer broad controls, respond at a different granularity than you
   were attacked at, and consider batching visible countermeasures.
7. **Every adaptive baseline tolerates any ramp slower than its adaptation rate.** Pair it with an
   absolute ceiling, chosen as a business judgment rather than a statistical one.
8. **Prefer signals the adversary cannot observe or manipulate** — consortium data, the graph, long
   card history. This is the strongest architectural consequence of adversariality, and it argues
   for data over model sophistication.
9. **Generative tooling improved scam content, not fraud structure.** Content-based signals degrade;
   structural and relational ones hold. The design response is the same as point 8.
10. **Displacement is the normal outcome of a successful control**, so measure total loss across
    channels rather than catch rate in the channel you improved.
11. **Threshold change or step-up escalation is usually the right first response to an attack** —
    fast, reversible, broad, and nearly silent — while writing a precise rule is slower, narrower
    and loud.
12. **Retraining answers drift, not adversaries.** The fast loop is rules and thresholds in hours,
    the medium loop is features in weeks, the slow loop is models in a month. With only the slow
    loop you are permanently fighting the last campaign.
