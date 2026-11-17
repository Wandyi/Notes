# Explanation, Governance and Fairness: The Constraints That Are Not Negotiable, and What They Cost

A fraud system declines people. In a regulated institution that means several bodies of law apply
to it, several of which require you to be able to say *why*, in terms a person can understand, about
a decision made by a model with 412 inputs. Treating this as paperwork to be handled after the
engineering is how a fraud programme ends up with a model it cannot deploy.

This document covers what is actually required, how to produce an explanation that is true rather
than merely plausible, how model governance works in a bank and how to make it compatible with a
weekly release cadence, and the fairness testing that nobody does until somebody makes them.

It is written from the engineering side. **It is not legal advice, the requirements differ by
jurisdiction and change, and every statement below should be checked with your own counsel.** What
it does offer is the shape of the requirements and their engineering consequences, which is what
you need in order to design something that can satisfy them.

Scenarios are `C-01` through `C-12`.

---

## What is actually required, roughly

Four distinct obligations, frequently conflated:

**An explanation of an automated decision.** Several privacy regimes give a person subject to a
solely automated decision with significant effects the right to obtain meaningful information about
the logic involved, to contest it, and to obtain human intervention. In the European framework this
sits in the automated-decision-making provisions; similar duties exist elsewhere. What "meaningful
information about the logic" requires is contested, and the defensible reading is **the factors that
drove this decision, in plain language, specific to this case** — not the model's architecture and
not a generic statement that risk factors were considered.

**Adverse action notice.** In the United States, declining *credit* on the basis of a model
generally requires telling the applicant the principal reasons. Transaction fraud declines are
usually not credit decisions, which is why this obligation often does not bite on authorization —
but it bites hard on account opening, on limit reductions, and on account closure, all of which
fraud systems trigger. **Know which of your actions are credit decisions**, because the answer
determines which obligations apply, and the boundary is not where engineers assume it is.

**Model risk management.** Supervisory guidance on model risk — the Federal Reserve and OCC's
SR 11-7 being the canonical statement, with equivalents in other jurisdictions — requires that
models used in material decisions be documented, independently validated, monitored, and governed
with clear ownership. This is the obligation that collides with a weekly release cadence, and doc
[06](06-models-labels-and-training.md)'s `M-14` is the resolution.

**Non-discrimination.** Fair-lending and equal-treatment rules prohibit discrimination on protected
characteristics, including through facially neutral criteria with a disparate impact that cannot be
justified. **Note that the prohibition does not require intent and does not require the protected
characteristic to be in the model.** A model that has never seen ethnicity can still produce a
disparate impact through postcode, and "we do not collect that attribute" is not a defence — it is
an inability to detect the problem.

---

## Producing an explanation that is actually true

### C-01 · The reason code that does not correspond to the decision

**What you see.** A customer is declined. The reason given is "unusual transaction amount". The
transaction was for $34 on a card whose median is $31. The actual driver was that the device had
been seen with 17 cards.

**Mechanism.** Reason codes were implemented as a static mapping from score band to a small set of
customer-friendly phrases, chosen for tone rather than derived from the decision. This is extremely
common, it is what most "reason codes" in practice are, and it is indefensible if anybody looks.

**Prevent.** Derive the explanation from the decision, per decision:

- **If a rule fired**, the explanation is the rule's own plain-language description. Rules explain
  themselves, which is one of the five jobs from doc
  [03](03-rules-and-why-they-never-go-away.md).
- **If the model drove it**, compute per-feature attributions for this prediction. For
  gradient-boosted trees, **Tree SHAP gives exact Shapley values in time linear in the tree
  structure** — Ironvale measures 4 ms for its 1,870-tree model, which fits inside the decision
  budget, so it is computed for every decision rather than on demand. Computing it later requires
  reconstructing the exact feature vector and model version, which works only if your decision log
  is complete, and is a reason the decision log must be complete.
- **Map features to human-readable statements once, in the feature registry**, with the mapping
  reviewed by somebody who talks to customers. `card_cnp_attempts_incl_declined_24h` becomes "the
  number of online attempts on this card today". The mapping is part of the feature definition, not
  an afterthought.
- **Render the top contributors as sentences with the actual values**: "the amount is 9.0 times this
  card's usual purchase", "this device was first seen 41 seconds ago", "17 different cards have been
  used on this device today". Three to five of these is an explanation a person can contest, which
  is the test.

### C-02 · The explanation that reveals the detection

**What you see.** A detailed explanation is given to a customer; the pattern it describes is being
probed within a week.

**Mechanism.** Doc [12](12-adversaries-drift-and-the-arms-race.md)'s `A-04` in explanation form. A
precise explanation is precise intelligence, and a fraudster whose transaction was declined is
entitled to the same explanation a genuine customer is.

**This is a real tension and the resolution is not "explain less".** What works:

- **Explain at the level of the factor, not the threshold.** "The number of recent attempts on this
  card was unusually high" is meaningful and does not disclose the limit. "More than 4 attempts in
  10 minutes" is a specification.
- **Separate the customer-facing explanation from the internal record.** The internal record — which
  is what a regulator, an auditor or a court would examine — has the exact values, versions and
  attributions. The customer-facing explanation is a faithful summary of it, not a different
  story. **The test for whether this is honest rather than evasive: the summary must be derivable
  from the record and must not be contradicted by it.**
- **Give the specific detail through the human channel**, where identity has been verified, rather
  than in an automated message that any holder of the card receives.

### C-03 · The right to human review, implemented as a dead end

**What you see.** A customer contests an automated decision. The contact-centre agent's screen
shows a risk score and a generic reason, and the agent has no authority to change anything.

**Mechanism.** The right to human intervention was interpreted as a right to speak to a human,
rather than as a right to have a human reconsider the decision. These are not the same and only the
second satisfies the obligation.

**Prevent.** A contested decision must reach somebody who **can see the actual reason** (the case
file from doc [11](11-case-management-and-the-human-loop.md)), **can reverse it**, and whose
reversal is recorded as a decision with its own reason. Ironvale routes contested decisions into
the review queue with an elevated priority and a distinct case type, and reports the reversal rate
monthly — a reversal rate near zero is evidence that the review is not real, which is worth
knowing before somebody else points it out.

---

## Model governance, and making it compatible with weekly releases

Doc [06](06-models-labels-and-training.md)'s `M-14` stated the resolution: separate the control
plane from the data plane, so that shipping a model is a governed configuration change rather than a
deployment. Here is what the governance itself has to contain for that to be acceptable.

**A model inventory.** Every model in use, with its purpose, owner, version, training data,
performance, validation status, and the decisions it influences. For Ironvale this includes the
primary model, the sequence model, the first-party-dispute classifier, the calibration layer, and
every vendor score — **the vendor scores are models in your inventory even though you did not build
them**, which is a point frequently missed and raised by every examiner.

**Independent validation.** A function separate from the one that built the model reviews its
conceptual soundness, its data, its testing, and its monitoring. In practice this is where the
holdout from doc [06](06-models-labels-and-training.md) earns much of its keep: an independent
validator's first question is "how do you know your false positive rate", and an answer that is not
"we measure it on a randomised holdout" is an answer that does not survive.

**Documented limitations and assumptions.** Where the model should not be relied on — cold-start
populations, segments with thin data, conditions outside the training distribution — stated
explicitly. This is the document that, when an incident happens, determines whether the response is
"a known limitation behaved as documented" or "nobody knew".

**Ongoing monitoring with defined thresholds**, which is doc
[14](14-observability-and-operating-the-service.md), and defined actions when they are breached.

**A change process proportionate to the change.** This is the negotiation. Ironvale's agreed tiers:

| Change | Governance | Lead time |
|---|---|---|
| Threshold within a pre-approved range | Two-person approval, logged | Minutes |
| Rule enablement from a pre-approved incident pack | On-call analyst, logged, 30-day expiry | Minutes |
| New rule, discretionary pack | Fraud team review with measured shadow results | 1–3 days |
| Recalibration, same model | Model owner plus monitoring review | Hours |
| Retrain, same features, same architecture | Abbreviated validation against a standing protocol | 3 days |
| New features or architecture | Full independent validation | 4–8 weeks |
| Scoring service code | Full change process, change window | Quarterly |

**The argument that won this, and it is worth memorising because the same argument works in most
institutions:** the governance burden should be proportionate to the blast radius and inversely
proportionate to the reversibility. A retrain on the same features and architecture, validated
against a standing protocol, with a one-click rollback to the previous artefact, has a smaller blast
radius and a faster reversal than a code deployment — so requiring it to pass through a code
deployment's process makes the system *less* safe, because it forces changes to be batched, large
and infrequent. **Frequent small reversible changes are safer than infrequent large ones, and a
governance process that does not distinguish them produces the latter.**

### C-04 · The vendor model in your inventory that you cannot document

Covered as `I-12` in doc [08](08-device-identity-and-behavioural-signals.md) from the signal side.
From the governance side: you are accountable for a decision influenced by a model you cannot
inspect. **Practices that make this defensible:** contractually require model documentation and
notice of material changes; log the vendor score and its version in your decision log; monitor its
distribution as a model you own; validate it empirically on your own population even without access
to its internals; **and cap its influence so that it cannot by itself move a decision across a
band.** The last is the one that converts an unexplainable input into a manageable one.

---

## Fairness testing, which almost nobody does

### C-05 · The disparate impact you cannot see because you do not collect the attribute

**What you see.** Nothing. You do not collect ethnicity, so you cannot measure outcomes by
ethnicity, so you report that there is no evidence of disparity.

**Mechanism.** Absence of measurement is being reported as absence of effect. This is the single
most common state of affairs and it is not a defensible position — in a fair-lending examination it
is the finding, not the defence.

**Prevent.** Two approaches, both in use at real institutions:

- **Collect it where you lawfully may, for the purpose of monitoring**, with appropriate separation
  — held by a compliance function, not available to the model or to analysts, used only for
  aggregate outcome testing. Several jurisdictions explicitly permit or require this for
  equal-treatment monitoring, and the processing basis is distinct from using it in a decision.
- **Proxy it where you may not.** Bayesian Improved Surname Geocoding — inferring a probability
  distribution over ethnicity from surname and geography — is the standard method in US fair-lending
  analysis and is used by regulators themselves. It is imprecise at the individual level and
  adequate in aggregate, which is exactly the shape of the question being asked.

**The point to be clear about: the purpose of collecting or proxying is to measure, never to
decide.** The data must be architecturally separated from the scoring path, and the separation must
be demonstrable rather than policy-based.

### C-06 · What to measure, concretely

For each protected or sensitive group, and for the population as a whole:

| Metric | Why |
|---|---|
| Decline rate | The headline disparity |
| Step-up rate | A less visible friction that may carry the disparity instead |
| False-decline rate (requires the holdout) | A decline rate difference may be justified by a real base-rate difference; a *false*-decline rate difference cannot be |
| Catch rate | Under-protection is also a harm — a group whose fraud is caught less often is being served worse |
| Time to resolution when blocked | Disparity in remedy, which is invisible in decision metrics |
| Appeal and reversal rate | Disparity in who can effectively contest |

**The third and fourth rows are the ones that matter most and they are the ones nobody measures.**
A higher decline rate for a group may be entirely explained by a higher genuine fraud rate in that
group's transaction mix — that is a legitimate difference. A higher *false*-decline rate is a
disparity in how the system errs, which is the thing the law is about. **Distinguishing them
requires the randomised holdout from doc [06](06-models-labels-and-training.md), which is therefore
a fairness-testing instrument as well as a modelling one** — a second argument for its $950,000 a
year.

The symmetric harm is worth stating because it is routinely forgotten: a group that is *under*
protected — whose fraud is caught less often because the model has less data about them — is also
being treated worse. Fairness in fraud detection is two-sided, and measuring only decline rates sees
one side.

### C-07 · The proxy you did not intend

Features that proxy protected characteristics, from doc
[04](04-features-and-the-feature-store.md)'s `F-14` and doc
[08](08-device-identity-and-behavioural-signals.md)'s `I-11`:

| Feature | Proxies for |
|---|---|
| Postal code, and anything derived from it | Ethnicity, in most countries, strongly |
| Device price tier, operating-system version age | Income |
| Browser or system language | National origin, ethnicity |
| Accessibility-feature usage | Disability, definitionally |
| Behavioural interaction timing | Disability and age |
| Merchant category of certain purchases | Religion, health status, pregnancy |
| Time-of-day patterns | Occupation, and through it income |
| Graph distance via shared address | Neighbourhood, and through it ethnicity (doc [09](09-graphs-rings-and-linked-entities.md), `G-03`) |

**Practice:** the feature registry records a proxy-risk review per feature — assessed by whom, when,
and with what conclusion. Features assessed as high-risk are either excluded (Ironvale excludes
accessibility attributes outright and address-based graph edges from decline decisions), or retained
with a documented justification and a specific monitoring obligation. **The documented
justification is the part that matters**: a feature with a genuine, demonstrable predictive
relationship that is not merely a proxy can usually be retained, and the demonstration is what you
need to have done in advance.

### C-08 · The fairness fix that made things worse

**What you see.** A postcode feature is removed to address a disparity. The disparity does not
change.

**Mechanism.** The model reconstructs the information from correlated features — merchant mix,
transaction timing, device tier, the graph. Removing a single proxy from a model with 412 features
removes a label, not information. **This is the most common failed intervention** and it produces a
model that is equally disparate and harder to diagnose, because the proxy is now implicit.

**What works instead**, in increasing order of difficulty: **measure first and keep measuring**, so
you know whether anything changed; **constrain the outcome rather than the input** — monitor the
disparity metric and treat a breach as an incident with a required response, which is a control
rather than a hope; **use segment-specific thresholds deliberately**, which can equalise
false-decline rates and which has its own legal complexity because it is explicitly
group-conscious, so it needs counsel; and **fix the underlying data problem**, which is usually that
a group is thinly represented in the training data and therefore poorly served — the cold-start
problem from doc [04](04-features-and-the-feature-store.md) wearing a different hat, and the only
fix that improves the system rather than redistributing its errors.

---

## C-09 through C-12, briefly

### C-09 · Privacy: the data you collect to detect fraud

Fraud prevention is a recognised legitimate purpose for processing personal data in most privacy
regimes, which is often read as permission to collect anything. It is not: the processing still has
to be necessary and proportionate to that purpose, and some categories — biometric data, health
data, and data revealing ethnicity or religion — have a higher bar that fraud prevention does not
automatically clear. **The engineering consequences:** behavioural biometrics need a specific lawful
basis established before you build (doc
[08](08-device-identity-and-behavioural-signals.md), `I-05`); purchase data that reveals a special
category is special-category data regardless of why you hold it; and the purpose limitation means
data collected for fraud prevention is not automatically available for marketing or credit
decisions, which requires architectural separation rather than a policy document.

### C-10 · Erasure versus retention, and the fraud-prevention exemption

A request to erase personal data collides with a statutory retention obligation and with the need to
keep fraud records. Most regimes resolve this in favour of retention where there is a legal
obligation or an overriding legitimate interest, and fraud-prevention registers generally qualify.
**The engineering requirements this creates:** you must be able to find everything about one person
across all four storage tiers (doc [10](10-storage-hot-cold-and-what-it-costs.md)) in order to
respond at all; you must be able to erase what is not covered by the exemption while retaining what
is, which means record-level rather than table-level granularity; and **you must be able to state
which exemption applies to each retained item**, which means the retention reason is a field, not a
policy. Designing for this late is extremely expensive.

### C-11 · The audit question you will actually be asked

In the author's experience the examination question is rarely "explain your model architecture". It
is: **"show me the decision on this specific transaction on this specific date, tell me exactly why,
and show me that the model and rules in force then were the ones that had been approved."**

Answering it requires: the decision log with the full feature vector (doc
[04](04-features-and-the-feature-store.md)), the model and rule-estate and policy versions in the
record (doc [10](10-storage-hot-cold-and-what-it-costs.md)), the artefacts for those versions still
retrievable, the approval record for each, and the ability to recompute the attribution. **Every one
of those is cheap to build in advance and impossible to construct retrospectively.** A team that can
answer this question in an hour has a fundamentally different relationship with its regulator than
one that needs three weeks.

### C-12 · The compliance requirement treated as a constraint rather than a design input

The recurring failure in this document, stated once: each of these obligations has an engineering
consequence that is cheap at design time and expensive later. Logging feature values, versioning
artefacts, separating monitoring attributes from scoring attributes, structuring outcome
vocabularies, recording retention reasons, computing attributions inline — each is hours of work
during design and a multi-month programme once the system is in production with years of data in a
shape that does not support them. **The recommendation is not "involve compliance early" as a
platitude; it is a specific list, and it is the one in the checklist below.**

---

## The compliance-driven engineering checklist

Everything in this document reduces to fifteen things in the build. None of them is expensive at the
start.

1. Log the full feature vector with every decision.
2. Log the model, rule-estate, calibration and policy versions with every decision.
3. Retain the artefact for every version that ever made a decision, for the retention period.
4. Compute per-decision attributions inline, at 4 ms, rather than on demand.
5. Maintain the feature-to-plain-language mapping in the feature registry.
6. Maintain a proxy-risk review per feature, with assessor and date.
7. Keep protected or proxied attributes architecturally separate from the scoring path, available
   only to aggregate monitoring.
8. Measure false-decline rate and catch rate by group, not only decline rate — which requires the
   holdout.
9. Measure time-to-resolution and reversal rate by group.
10. Route contested decisions to a human who can see the reason and reverse it, and report the
    reversal rate.
11. Keep every model in an inventory, including vendor scores, and cap any single vendor score's
    influence.
12. Document each model's limitations and the populations it should not be relied on for.
13. Make retention reason a per-record field so that erasure and retention can coexist.
14. Define change tiers by blast radius and reversibility, and get them agreed.
15. Rehearse the audit question — one transaction, one date, full explanation, proof of approval —
    and time yourself.

---

## What to take away

1. **Four distinct obligations get conflated**: explanation of automated decisions, adverse action
   notice, model risk management, and non-discrimination. Know which of your actions are credit
   decisions, because that determines which apply, and the boundary is not where engineers assume.
2. **Derive explanations from the decision, not from a static band-to-phrase mapping.** Tree SHAP is
   exact and costs 4 ms, so compute it inline for every decision; reconstructing it later needs a
   complete decision log.
3. **Explain at the level of the factor, not the threshold.** "Unusually high number of recent
   attempts" is meaningful; "more than 4 in 10 minutes" is a specification handed to an adversary.
   The customer-facing summary must be derivable from the internal record and never contradicted by
   it.
4. **A right to human review means a human who can reverse the decision**, not a human who can
   explain it. A reversal rate near zero is evidence the review is not real.
5. **Vendor scores are models in your inventory.** Document them contractually, log their versions,
   monitor their distributions, validate them on your own population, and cap their influence so
   none can alone move a decision across a band.
6. **"We do not collect that attribute" is not a defence; it is an inability to detect the
   problem.** Collect it for monitoring under separation, or proxy it, and architecturally separate
   it from scoring.
7. **Measure false-decline rate and catch rate by group, not decline rate.** A decline-rate
   difference can be justified by a genuine base-rate difference; a false-decline-rate difference
   cannot. This makes the randomised holdout a fairness instrument as well as a modelling one.
8. **Under-protection is a harm too.** A group whose fraud is caught less often is being served
   worse, and measuring only decline rates sees one side of the problem.
9. **Removing a proxy feature usually changes nothing** except your ability to diagnose, because 412
   features reconstruct it. Constrain the outcome, equalise deliberately with counsel, or fix the
   thin-data problem underneath.
10. **The audit question is "this transaction, this date, why, and prove the versions were
    approved."** Every component of the answer is cheap in advance and impossible retrospectively.
11. **Governance should scale with blast radius and inversely with reversibility.** That argument is
    what wins the right to ship a model weekly — and it is true: frequent small reversible changes
    are safer than infrequent large ones.
12. **Fifteen things in the build satisfy nearly all of this**, and all fifteen are hours of work at
    design time and a multi-month programme afterwards.
