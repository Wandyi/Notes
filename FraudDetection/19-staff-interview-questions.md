# The Interview: One Full Walkthrough, Eighteen Questions, and What Separates a Strong Answer

"How would you design a fraud detection service?" is a question that rewards a specific shape of
answer, and most candidates give a different shape. This document works through the full question
once, then gives eighteen follow-ups with model answers, then names the five patterns that
distinguish a strong answer from a merely complete one.

The framing throughout is a card issuer, because that is the common setting — it is the setting at
Capital One, Chase, Amex, Barclays and every bank that issues cards — but the structure transfers.

---

## The full walkthrough: "Design a fraud detection service"

### Minute 1–3: establish what you are being asked

Do not start drawing. Ask four questions, and say why each matters, because the reason is the part
being assessed:

> "Before I design anything I want to pin down four things, because the answers change the
> architecture rather than just the parameters.
>
> **First, which fraud?** Transaction fraud, account takeover, first-party dispute, scams, or policy
> abuse — they are detected by different signals on different clocks. I will assume card transaction
> fraud at an issuer unless you want otherwise.
>
> **Second, where in the flow do I sit, and what is my deadline?** If I am an issuer in the
> authorization path, something is holding a socket open and the network's stand-in processing will
> answer for me if I am late. That gives me perhaps 120 milliseconds. If I am a marketplace holding
> an order before fulfilment I have ninety seconds, which is six hundred times as much, and the
> design is completely different.
>
> **Third, who pays for each error?** A missed fraud lands on the issuer's loss line. A false
> decline mostly lands on the merchant, who loses the sale, and the customer, who is embarrassed at
> a checkout. That asymmetry is why card declines feel aggressive, and I will need it to set the
> threshold.
>
> **Fourth, what is the base rate?** I will assume something like 1 in 7,000 to 1 in 10,000 of
> transactions, which I will use in a moment to show why some obvious approaches do not work."

**What this establishes.** That you know the problem has variants, that latency is derived rather
than assumed, that you think in terms of cost rather than accuracy, and that you will quantify. Most
candidates reach straight for Kafka. Three minutes spent here changes the rest of the conversation.

### Minute 3–6: the arithmetic that shapes everything

> "Let me take 42 million authorizations a day with a base rate of 1 in 7,692, which is about 5,460
> fraud attempts.
>
> Suppose I build a model that catches 90% of fraud with a 1% false positive rate. One percent sounds
> good. It flags 90% of 5,460, which is 4,914 frauds, and 1% of the remaining 42 million, which is
> 420,000 good transactions. **Precision is 1.16% — eighty-five blocked customers per fraud
> caught.** That system is unusable, and the problem is not the model: 1% of 42 million is just a
> very large number.
>
> Two things follow. **Accuracy and ROC-AUC are close to meaningless here** — approving everything
> is 99.987% accurate. And **the lever that matters is usually a feature, not a model**: moving the
> false positive rate from 1% to 0.1% takes precision from 1.16% to 10.5%, a ninefold improvement,
> and that comes from knowing this card has never been used in this country before rather than from
> a deeper network."

**Why to do this early.** It is the fastest available demonstration that you understand the domain
rather than the toolkit, and every later design decision now has a reason.

### Minute 6–14: the architecture, in four layers

Draw it, and narrate the reasons rather than the boxes.

> "I will build four layers, because 'real-time' means four different things here.
>
> **The inline path, 120 milliseconds.** Transaction arrives, I derive the entity keys — card,
> account, device, merchant, IP prefix, email — and do **one batched multi-get** to the feature
> store. One, not six: six sequential round trips is 70 milliseconds of pure overhead and it is the
> commonest performance bug in this path. Then the rule engine, then a gradient-boosted tree model,
> then a policy layer turns a probability into approve, step up or decline.
>
> **I will use a cascade rather than running everything on everything.** Rules and the primary model
> are cheap, so they run always. The expensive sequence model and any graph enrichment run only on
> the uncertain middle, which is under 3% of traffic — that is 34 times less expensive compute for
> about 0.4% of catch rate. The precondition is that the cheap stage's scores are calibrated well
> enough to know when they are uncertain, which I will come back to.
>
> **The streaming layer.** Every transaction, login and device sighting goes to Kafka, partitioned by
> the aggregation key, and a stream processor maintains windowed counters per entity. This is where
> about a third of my detection power comes from, and it is engineering rather than cleverness —
> counting is trivial, counting across six entity types and six window lengths at 3,900 events a
> second with a three-millisecond read is the whole problem.
>
> **The retrospective layer.** Nightly graph computation, label collection, training, monitoring
> aggregations.
>
> **And the human layer**, which most answers omit and which is the second largest line in the
> budget. A review queue, a case tool, and — critically — **a structured outcome vocabulary, because
> those outcomes become training labels.**
>
> The component I would call out as most important is **not the model. It is the decision log.** For
> every decision I record every feature value as it was at decision time, the model version, the rule
> versions, the policy version, the score and the reason. Training, monitoring, dispute defence and
> regulatory explanation all depend on it, and it is the one thing you cannot create
> retrospectively."

**The three things that score here**: the batched fetch, the cascade with its arithmetic, and
naming the decision log as the load-bearing component.

### Minute 14–20: the parts that distinguish the answer

Spend this time on the things most candidates do not reach. In rough order of value:

**The threshold, derived rather than tuned.**

> "To turn a score into a decision I need two costs. A false decline costs the issuer about $1.92 —
> 23% of them generate a $6.10 call, 41% of the transactions are never re-attempted so I lose the
> interchange, plus a measured 90-day spend reduction. A missed fraud costs about $179 — $186 of
> gross value, less a 25% chargeback recovery, plus $39 of case handling.
>
> So I decline when `p × 179 > (1 − p) × 1.92`, which is `p > 1.06%`. **The break-even precision is
> 1 : (C_miss / C_false), which here is 1 in 94.**
>
> And I would add immediately: that applies to the *marginal* transaction, not the average. A system
> whose average precision is 1 in 12 may be exactly at the optimum if its marginal precision is 1
> in 88, which is the case at the issuer I have in mind. Confusing average with marginal precision
> is the most expensive mistake available here."

**The holdout, unprompted.**

> "There is a structural problem I should raise. **I only get labels for transactions I approve.**
> Everything I decline simply has no outcome, which means next month's training data describes only
> what last month's model allowed through — so the model becomes confident and untested exactly
> where it blocks, and I cannot measure my own false positive rate at all.
>
> The only fix is a **randomised holdout**: approve a random 0.3% of transactions I would have
> declined, excluding anything mandatory like a sanctions match, and observe what happens. At this
> scale it costs about $950,000 a year — 3.7% of the fraud loss — and it is the sole source of my
> precision estimate, my false-positive rate, my unbiased training data, and my fairness testing. I
> would fight hard for it."

**The failure mode.**

> "The most important configuration decision in the whole system is what the authorization service
> does when I do not answer. Fail closed costs over a million dollars per twenty-minute outage at
> this volume — 583,000 declines of which essentially all are good customers. Fail open costs about
> $14,000 of fraud. So everybody fails open, which is right, **and an attacker who can cause a
> twenty-minute outage knows it.**
>
> I would build a third option: **fail static.** A cached per-card risk tier and about forty lines of
> rules, resident on the authorization host. It approves 99.2% of good transactions and blocks 61%
> of fraud — far worse than the full stack, enormously better than a constant. It costs about the
> same as failing open and it removes the incentive to cause the outage, which is the actual point."

**The silent failure.**

> "And the most dangerous thing the system can do is produce a confident score from missing features.
> If one entity lookup fails and the client returns a map with that entity absent, and the pipeline
> imputes a zero, the model sees what looks like a quiet card and scores it safe. There is no error,
> no latency change, and no alarm — it surfaces in labels a month later. So: **absent is a distinct
> type, never imputed silently; the policy layer rather than the model decides what to do about it;
> and feature availability is an alarmed metric.**"

### Minute 20–25: trade-offs, stated as trade-offs

If asked for trade-offs, do not list speed-versus-accuracy. Give these:

| Trade-off | Position, and the price |
|---|---|
| Rules versus model | Both, in a policy layer. Rules give reach (hours, not weeks), obligations, readable policy, reasons and a 60-second incident control; the model gives discrimination. The price of rules is an estate that rots, which needs marginal-contribution measurement and auto-expiry. |
| Synchronous versus deferred | Split on "irreversible now" versus "irreversible in ninety seconds". The deferred path gets a dramatically better decision; the price is two code paths and a cancellation mechanism. |
| Precision versus recall | Neither; expected cost, at the margin, per segment, with the amount in the decision rule. The price is that you must know your costs, which means asking the disputes team for the recovery rate. |
| Exactness versus throughput in the pipeline | Exactness. An increment is not idempotent, a double-applied counter is permanently wrong and undetectable, and it corrupts the feature family carrying a third of the detection power. The price is about 4% of throughput. |
| Hot storage cost | Not actually a constraint at issuer scale — $30,000 to $100,000 a year against a $26 million loss, 0.2%. It *is* a constraint at small scale and where entity count far exceeds customer count. |
| Fingerprinting versus possession | Possession, and invest in app adoption. Fingerprinting is a depreciating asset — confident device linkage on web fell from 71% to 43% in four years with no code change. |
| Graph features versus fairness | Use them, with hard constraints: never sufficient alone, weighted by edge strength, decayed over time, address edges excluded from declines. The price is about 0.9 points of catch rate and it is the right price. |

---

## Eighteen questions, with model answers

### 1. Why not just use a neural network?

Because the features are heterogeneous tabular values with frequent and meaningful missingness,
and gradient-boosted trees handle that natively while learning a default branch direction for
absence. They also permit **monotonic constraints** — risk must be non-decreasing in declined
attempts — which is both a correctness guard against sparse-data inversions and a governance asset
you can state to a model-risk committee. They give exact SHAP attributions in milliseconds, which
you need for reason codes. They infer in 11 ms on CPU. And they retrain in 40 minutes, which is what
makes a weekly cadence possible. Where a neural model genuinely wins is **sequences** — the card's
last 64 authorizations as an ordered sequence carries structure no hand-designed aggregate captures
— and I would use one there, behind a cascade, with an ablation showing it is worth its 31 ms.

### 2. How do you handle the class imbalance?

Mostly by not reaching for resampling. Class-weight the loss; undersample negatives if training
time requires it **and then correct the prior**, because a model trained at an altered base rate
emits probabilities that are wrong by a known factor and a threshold set on the raw output means
nothing. I would specifically avoid SMOTE: interpolating between two fraud transactions in a
412-dimensional space of counts and ratios produces points corresponding to no possible
transaction — a card with 3.5 declined attempts — so you learn a boundary around a region that
cannot occur, and improve a metric on a synthetically balanced test set that does not exist in
production. And evaluate with precision-recall at the actual operating point, never ROC-AUC, which
is dominated by the enormous negative class.

### 3. Your model looked great offline and is mediocre in production. What happened?

Most likely **temporal leakage**. The training set was probably built by joining historical
transactions to the current aggregate tables, so each row's velocity features included the fraud
that happened *after* it. There is no error, every test passes, and the effect is large — at the
issuer I have in mind, evaluation precision of 1 in 2.1 against production's 1 in 19. It also gets
worse as your features get better, because the more history-dependent they are the more there is to
leak.

The fix is to **log the feature vector at decision time and train on that**, which makes leakage
structurally impossible. For features you were not yet computing, keep a strictly causal backfill
and verify it by recomputing a daily sample and comparing against the production log. Two secondary
suspects worth naming: a random rather than time-based train/test split, which overstates precision
by about 62% because it puts the same week, card and fraud ring on both sides; and a dimension table
joined at its current value, where the merchant's risk tier was set *because of* the fraud you are
predicting.

### 4. How do you know your false positive rate?

I cannot, from production data, without a randomised holdout — which is exactly why I would have
one. Every declined transaction has no outcome, so the blocked region is unobservable. A 0.3%
holdout sampled by hash of transaction ID, with mandatory rules excluded, yields the precision of
the blocked region, unbiased training data covering the full feature space, and the ability to
evaluate a challenger where it differs from production. It costs about $950,000 a year against a
$26 million loss. The binding constraint on its size is the number of **fraud events** observed, not
transactions: at 14 a day you need a month to say anything about a segment.

### 5. What would you do if you had 120 ms and your model takes 200?

In order: first check whether the decision has to be synchronous at all — a large share of
decisions in many systems do not and can be deferred by ninety seconds. Second, cascade: run the
200 ms model only on the uncertain middle, which is under 3% of traffic, which makes its weighted
contribution about 6 ms. Third, give it a deadline and a typed "unavailable" result so it can be
shed, with a measured degradation figure for what that costs. Fourth, reduce the model — fewer
trees, shallower, quantised. What I would **not** do is spend the whole budget on it and leave
nothing for the feature fetch, because the features are worth more than the model.

### 6. The fraud service is timing out. Do you fail open or closed?

Neither, and I would push back on the framing. Fail closed at 486 transactions a second is 583,000
declines per twenty minutes, essentially all of good customers, at over a million dollars direct and
a contact centre that can absorb maybe 3,200 calls a night against 134,000 produced. Fail open is
$14,000 of fraud at the normal rate — **and that is the flaw, because an attacker who caused the
outage invalidates the normal rate.** So: a static fallback on the authorization host, about forty
lines, using a cached risk tier and the block list. Comparable cost to failing open, and it removes
the incentive to cause outages. Also: during the incident, turn retries **off** — under saturation
they are pure amplification, and a 300 ms timeout with two retries triples the load on the thing
already failing.

### 7. How do you stop a rule estate from becoming unmanageable?

Measure **marginal contribution**, not precision — a rule with excellent precision contributes
nothing if the model blocks the same transactions anyway. Replaying a month of labelled traffic with
each rule suppressed takes four hours of batch compute monthly. The distribution is always
revealing: at the issuer I have in mind, 44% of rules block nothing the model does not, 18% never
fire at all, and 8.8% are **net negative** and are the urgent group.

Then three governance controls. Incident rules auto-expire to shadow at 30 days, so removal is the
default and keeping requires an action. Every rule declares an expected fire rate and alarms at 3×
actual, which catches the decay mode where a predicate's population shifted underneath it. And
discretionary escalation is capped at one step per decision — three individually sensible rules
escalating cumulatively produced a 14% decline rate for travellers, and capping cut the overall
decline rate 11% with no loss of catch rate.

One warning: **never-firing rules are the dangerous ones to delete**, not the safe ones, because a
rule can show zero fires precisely because the attack it blocks is currently not being attempted.
Move them to shadow.

### 8. How do you handle a brand new customer with no history?

Accept that this is where a disproportionate share of fraud is, not an edge case — catch rate on a
card's first card-not-present transaction is about 54% against 93% overall, because every
deviation-from-profile feature is undefined.

Four things. **Back off to a less specific entity** — no card history, use the account; no account,
use the segment — and carry the backoff level as a feature, because a deviation from a segment
profile should be weighted differently from a deviation from the card's own. **Treat absence as
signal** rather than imputing; trees handle this natively if training data contains realistic
absence. **Lean on the families that are defined**: "device first seen 41 seconds ago" is not an
absence of signal, it is a strong signal. And **step up aggressively** — you have no information, so
acquire some, and a first CNP transaction on a new device is the ideal challenge candidate because
asking is cheap and the information gained is high.

### 9. What is your most important metric?

Operationally, the **score distribution per segment per minute, compared against the same hour last
week** — because it moves before any decision metric and it catches the failure nothing else does: a
model that has stopped discriminating has an unchanged mean and a collapsed variance.

At a programme level, four numbers on one page: net loss in basis points, **false-decline rate from
the holdout**, time-to-resolution for blocked customers, and holdout sample integrity. The second is
the one almost nobody reports and the one that makes the first honest — **when you over-block, your
fraud loss improves and every fraud metric goes green.**

### 10. How would you detect an attack in progress?

Narrowness. Ordinary drift is broad and slow; an attack is a spike in one segment over days or
hours. So the detection requires per-segment metrics, not aggregate ones — an aggregate dashboard
shows a six-point catch-rate drop and cannot tell you which of four things caused it.

The fastest specific detector I would build is the **implied fraud rate from the step-up pass
rate**. If genuine customers pass at 94.1% and fraudsters at 2.8%, then an observed pass rate in a
band implies a fraud rate of `(0.941 − observed) / 0.913`. A drop from 92.8% to 88.1% means the
band's fraud rate went from 1.4% to 6.6% — a fivefold rise, detected in minutes, from a quantity
involving no labels at all. The caveat is that a challenge-delivery problem moves the same number, so
read it alongside delivery telemetry.

### 11. An attack is in progress. What do you do first?

**Lower the affected segment's threshold one band, or switch it from decline to step-up.** Two
minutes, reversible in two minutes, broad enough to cover variants, and nearly silent. What I would
*not* do first is write a precise rule: it takes one to four hours, it covers only the variant I
have observed, and it tells the adversary exactly what I detected — **every response I make is free
intelligence for them**, and a narrow rule is a more informative response than a broad one. I would
also check our own change log before concluding it is an attack at all: 61% of apparent anomalies
are self-inflicted.

### 12. How do you explain a decline to a customer?

Derive the explanation from the decision, never from a static score-band-to-phrase mapping, which is
what most "reason codes" actually are. If a rule fired, its own plain-language description is the
explanation. If the model drove it, Tree SHAP gives exact attributions in about 4 ms, so compute it
inline for every decision and render the top three to five as sentences with real values — "the
amount is nine times this card's usual purchase", "this device was first seen 41 seconds ago".

Two constraints. **Explain at the level of the factor, not the threshold** — "an unusually high
number of recent attempts" is meaningful; "more than four in ten minutes" is a specification handed
to an adversary, who is entitled to the same explanation a customer is. And a right to human review
means **a human who can reverse the decision**, not one who can only explain it; a reversal rate
near zero is evidence the review is not real.

### 13. How do you test any of this?

Staging cannot reproduce the base rate, the adversary or the label delay, and making it bigger does
not help, so the strategy is a set of harnesses rather than an environment.

The foundation is **replay against logged feature vectors** — real values, real frequencies, real
base rate — which is only possible if the decision log is complete. On top of that: a **daily
recompute-and-compare** on a 10,000-row sample, which is the only evidence your offline and online
feature implementations agree and would have caught the leakage incident on day one; a **synthetic
scenario library** of about forty crafted event sequences with asserted outcomes, including
window-boundary and duplicate-delivery cases; and an **ablation study** monthly, which is what makes
the degradation ladder real numbers instead of guesses.

And one assertion worth four lines of code: **a transaction in the holdout hash range with a
blocking score must actually be approved.** The holdout fails silently, and that test is the
difference between eleven days of lost measurement and none.

### 14. How is designing this for an instant-payment scheme different?

Four inversions. **No chargeback** — settlement is irrevocable in about four and a half seconds and
the mule account is emptied within nine minutes, so there is no recovery path. **The dominant fraud
is the scam**, where the victim authorized it from their own device in their own location having
correctly authenticated, so every device and behavioural signal says legitimate. **The cost of a
miss is an order of magnitude higher** — about $2,900 against $186. And **a false block is a missed
obligation** — somebody's rent — which is reportable in several markets, so you cannot adopt the
issuer's strategy of quietly accepting a 93-to-1 ratio.

The design consequence is that it is **mostly not a classifier**. The only signals with power are
about the payee and the context — a new payee, a payee receiving from several unconnected senders,
an unusual round amount, and the strongest single signal, whether the customer is on a phone call.
And the response is not a decline but a specific interruption that names the risk, because the best
available outcome is the customer deciding to stop. That gets about 11% voluntary abandonment in the
high-risk cohort.

I would also raise that **a reimbursement regulation moved the correct threshold threefold
overnight** without any model changing, which is the cleanest demonstration that the threshold is a
property of the cost structure rather than of the model.

### 15. Where does a graph fit, and when is it not worth it?

It finds what single-transaction scoring cannot see by construction — mule networks, collusion
rings, bust-outs — where every individual event is ordinary.

Three engineering points. **Degree-cap every node type**, or a shared proxy IP bridges unrelated
subgraphs and you flag 5.5% of customers as one ring; the giant components are always artefacts.
**Do not traverse the graph inline** — precompute about a dozen per-entity summary values and serve
them from the key-value store; a graph database is an investigation tool, which is a different
product. And **split the computation on whether it needs more than one hop**: degrees and
unconnected-sender counts stream continuously, component structure runs in batch with incremental
propagation on a confirmed hit.

When not to: if your fraud is opportunistic rather than organised there is no structure to find.
Test it in an afternoon — take your last 200 confirmed cases and count how many share any attribute
with another. Under 15% means no graph problem.

And I would raise the fairness constraint unprompted: distance-to-known-fraud is the most valuable
graph feature and the one that will decline a nine-year customer because of a former flatmate. Never
sufficient alone, weighted by edge strength, decayed over time, address edges excluded from decline
decisions, and always human-reviewable.

### 16. How do you ship a model weekly in a bank with quarterly change windows?

**Separate the control plane from the data plane.** The scoring service — the code — changes rarely
and goes through the full change process. The model artefact, the rule definitions, the thresholds
and the calibration are versioned *data*, loaded at runtime, with governance proportionate to each:
model-risk review for the artefact, two-person approval for a threshold, an automated gate for a
rule.

The argument that wins this is not a convenience argument. **Governance should scale with blast
radius and inversely with reversibility.** An artefact swap with a one-click rollback has a smaller
blast radius and a faster reversal than any code deployment, so forcing it through a code
deployment's process makes the system *less* safe by making changes large, batched and infrequent.
Frequent small reversible changes are safer than infrequent large ones. Getting that accepted is
months of political work and it is the highest-value thing a staff engineer in that seat does.

### 17. What would you build first if you were starting from nothing?

Depends entirely on volume, and I would ask. Under about 10,000 transactions a day you have one or
two fraud events a day and **cannot train a model** — forty positive examples get memorised. Build:
an editable block list, six to ten velocity rules, your processor's score capped so it cannot alone
decline, a review queue somebody works each morning with a **structured outcome vocabulary from day
one**, and a decision log with the feature values. Three weeks, and it will beat a model for a long
time.

The two things I would do at any scale because they cost nothing and almost nobody has them:
**write down the base rate and the two error costs**, and **log the feature vector with every
decision** — because that is the one thing you can never create retrospectively.

### 18. What is the hardest part?

That you find out you were wrong a month late, and only about half of it. Chargeback labels arrive
at a median of 27 days; roughly 38% of them are first-party disputes mislabelled as fraud,
concentrated precisely where the device and location signals are perfect; your negative class
contains the quarter of real fraud nobody ever reported; and everything you declined has no outcome
at all.

So you are training on censored, delayed, partly mislabelled data against an adversary who adapts in
hours. That is why the architecture has three loops at different speeds — rules and thresholds in
hours, features in weeks, models in a month — and why **a system with only the model loop is
permanently fighting the last campaign.**

---

## The five patterns that separate a strong answer from a complete one

**One. Quantify unprompted.** Not "the base rate is low" but "1 in 7,692, and here is what a 1% false
positive rate does to precision at that base rate". Not "hot storage is expensive" but "$30,000 to
$100,000 a year, which is 0.2% of the loss, so it is not the constraint — but it is 400% of the loss
at a small merchant's scale, where it is". Every number in this collection is derived in visible
steps for this reason: the arithmetic is the argument.

**Two. Raise the uncomfortable thing yourself.** The holdout that knowingly approves $2,604 of fraud
a day. The graph feature that declines somebody because of their flatmate. The call-state signal
that is powerful and ethically fraught. The 93-to-1 ratio that exists because most of the cost of a
false decline lands on somebody who is not you. A candidate who raises these before being asked is
demonstrating that they have operated a system rather than read about one.

**Three. Name the silent failure.** Anybody can design the happy path. The thing that distinguishes
experience is knowing that the dangerous failures produce no error, no latency change and no alarm —
the score computed from missing features, the counter that was double-applied, the capability that
drained away over four years while every weekly retrain performed correctly. And knowing the
specific cheap control for each.

**Four. Separate what is a cost from what is a constraint.** A chargeback ratio ceiling cannot be
traded against revenue. A sanctions match is not a feature with a weight. Review capacity is a
bound, not a preference. Candidates who put everything into one expected-cost formula are half
right; candidates who know which things do not belong in it are the ones you want designing the
policy layer.

**Five. Know what you would not build.** "At under 10,000 transactions a day I would not build any
of this" is a stronger answer than a full architecture, because the hardest judgment in this field is
proportionality, and the most common failure of a fraud programme is building tier 3 while sitting
at tier 1.
