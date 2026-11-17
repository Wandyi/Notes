# Device, Identity and Behaviour: The Signals That Are Quietly Getting Weaker

Device fingerprinting is the part of fraud detection that sounds most like science fiction and is
most often described as though it were 2016. This document covers what the signals actually are,
how much each is worth, and the uncomfortable trend: **the signal families that powered a decade of
fraud detection are being systematically dismantled by privacy engineering, and a system that
depends on them has been losing capability every year without anybody logging a defect.**

It also covers the adjacent question nobody asks until a regulator does: which of these signals are
you actually allowed to collect, and what happens to your catch rate when the answer changes.

Scenarios are `I-01` through `I-12`.

---

## What a device signal is, concretely

When a customer's browser or app reaches a merchant or a bank, a script or SDK collects attributes
and derives an identifier. The attribute families, with what each actually tells you:

**Hard identifiers, where available.** On a native mobile app, the platform provides a
vendor-scoped or app-scoped identifier that is stable until the app is reinstalled. This is the
strongest device signal that exists, and it is available only to the app's own publisher — a bank
gets it for its own app, and nobody gets it for a web session.

**Soft fingerprint attributes.** Screen dimensions, colour depth, timezone, installed fonts,
language list, hardware concurrency, the set of supported codecs, WebGL renderer string, canvas
rendering differences, audio-stack rendering differences. Individually weak; in combination
historically enough to identify a browser with high probability.

**Network attributes.** IP address and its derived properties — autonomous system, geolocation,
whether it belongs to a hosting provider, VPN or residential proxy pool — plus TLS fingerprint
(the ordering and composition of the client's cipher suites and extensions, which differs between
real browsers and automation tooling) and HTTP header ordering.

**Behavioural attributes.** Typing cadence, mouse movement entropy, touch pressure and swipe shape,
how long the customer spends on each field, whether a value was typed or pasted, whether the form
was completed in a plausible order, and — the single most informative behavioural signal — the time
between page load and submission.

**Possession and account attributes.** Whether the session is on a device that has previously been
authenticated to this account, whether the app's push token matches, whether a hardware key or
platform biometric was used.

### What each is worth, measured

Ironvale's ablation study (remove the family, retrain, measure catch rate at fixed decline rate),
split by channel because the answer differs enormously:

| Signal family | Own mobile app | Own web | Third-party merchant checkout |
|---|---|---|---|
| Hard device identifier | 14.1% | not available | not available |
| Soft fingerprint | 2.2% | 6.8% | 9.1% |
| Network and IP intelligence | 3.1% | 5.4% | 7.2% |
| Behavioural | 1.9% | 4.0% | 1.1% |
| Possession / prior authentication | 11.4% | 7.9% | not available |

Two conclusions that matter for design.

**Where you control the app, possession beats everything.** A hard device identifier plus "this
device has been authenticated to this account for 14 months" is worth 25.5 percentage points of
catch rate, and it makes the soft-fingerprint and behavioural families nearly redundant. The
engineering instinct to invest in sophisticated behavioural biometrics on your own app is usually
misdirected: the cheap, boring possession signal dominates it by an order of magnitude.

**At a third-party merchant's checkout you have almost nothing**, which is exactly where
card-not-present fraud happens. The issuer sees an authorization message with no device context at
all unless the merchant chose to send it. This is the real reason 3-D Secure exists — it is a
mechanism for moving the authentication to a context the issuer controls — and it is why a card
issuer's device strategy is mostly a strategy for *getting* device data rather than for analysing
it.

---

## The decay: why these numbers were higher five years ago

This is the part that is usually missing. Soft fingerprinting worked because browsers leaked a lot
of entropy. They leak much less than they did, and the trend is one-directional:

- **Third-party cookies** are gone or going in every major browser, which removes the simplest
  cross-site identifier.
- **Canvas, WebGL and audio fingerprinting** are actively defended against: some browsers add
  per-session noise to canvas readback, round or quantise audio-stack output, and return a generic
  renderer string.
- **Font enumeration** has been narrowed to a system-font allowlist in several browsers.
- **User-agent strings** have been frozen and reduced; detailed platform data is now behind an
  opt-in hints mechanism that the site must request and the browser may refuse.
- **IP addresses** are increasingly not identifying: carrier-grade NAT puts tens of thousands of
  mobile users behind one address, and private-relay features route traffic through an
  intermediary so the observed address belongs to the relay, not the user.
- **Mobile advertising identifiers** require explicit per-app permission, with most users
  declining.
- **Mail privacy features** break open-tracking and the inferences that depended on it.

The practical effect, measured by Ironvale over four years on its web channel: **the share of
sessions it can confidently link to a previously seen device fell from 71% to 43%**, with no change
to its own code. Nothing broke. No alarm fired. The feature
`device_previously_seen_on_account` simply became false more often, and the model — retrained
weekly on data in which it was false more often — adapted by relying on it less. **The capability
drained away under a system that was working exactly as designed.**

### I-01 · The silent capability loss

**What you see.** Nothing, for years. Then an audit asks why catch rate on the web channel is below
the app channel by more than it used to be.

**Confirm it.** Monitor **signal availability as a first-class metric**, separately from signal
value: for each signal family, the share of sessions in which it is present and usable, trended
over months. This is a different panel from feature distribution monitoring (doc
[14](14-observability-and-operating-the-service.md)) and it is the one that catches structural
decay rather than a deployment.

**Prevent, insofar as you can.** You cannot stop the trend, and attempting to circumvent privacy
controls is both an arms race you lose and, increasingly, a regulatory exposure. The strategic
responses that actually work:

- **Move the authentication into a context you control.** Push the customer to your app; use 3-D
  Secure where the liability rules make it worthwhile; adopt platform passkeys, which give a strong,
  privacy-respecting possession signal that is *better* than a fingerprint and is not being taken
  away.
- **Shift weight to signals that are not degrading** — transaction and merchant history, velocity,
  the graph. These are yours; nobody can deprecate them.
- **Treat consortium and network-level data as increasingly important**, because shared signals
  about a card or merchant survive a browser's privacy changes entirely.

**The strategic framing worth stating in an interview:** device fingerprinting is a *depreciating
asset*. Budget for its replacement rather than its expansion.

---

## I-02 through I-06: how device signals are actually used

### I-02 · The device that is new because the library was upgraded

**What you see.** On a single day, `device_first_seen_hours` is near zero for the entire
population, step-up volume triples, and the contact centre melts.

**Mechanism.** The fingerprinting SDK was upgraded and the new version computes identifiers
differently, so every device in the world is new. This is doc
[04](04-features-and-the-feature-store.md)'s `F-05` in its most spectacular form, and it has
happened to nearly every large organisation at least once.

**Prevent.** Two things. **Version the identifier and carry both** — the new SDK emits identifiers
under both the old and new schemes for a transition period, so continuity is preserved and you can
measure the mapping. And **gate the deployment on a fingerprint-stability check**: before rolling
out, measure what share of devices receive the same identifier from both versions, and refuse the
rollout below a threshold. Ironvale's gate is 95% and has blocked two upgrades.

### I-03 · The device graph: one device, many cards

The highest-value derived signal from device data is not "is this the customer's device" but
**"how many unrelated identities has this device touched."** A device seen with 17 cards in 24
hours is a fraud workstation regardless of anything else about it, and this single feature is among
the most precise in Ironvale's estate — precision 1 in 2.8 as a standalone rule.

Implementation: maintain device-keyed aggregates (doc [05](05-the-streaming-pipeline.md)) holding a
distinct-count sketch of cards, accounts and emails seen, over several windows, and fetch the
device key in the scoring path's batched multi-get.

**The symmetric feature is equally valuable and more often forgotten: cards per device, and devices
per card.** A card that has been used on 40 devices in a week is as suspicious as a device used
with 40 cards. The two are different queries over the same bipartite graph and both belong in the
feature set. Doc [09](09-graphs-rings-and-linked-entities.md) generalises this.

**The legitimate exceptions to design for**, because each will otherwise generate false positives at
volume: shared family devices, a household tablet used for everybody's accounts; shared corporate
devices and kiosks; the device of a carer or relative managing several accounts; and
privacy-relay traffic, which makes many unrelated customers appear to share network attributes.
Ironvale maintains an explicit "shared device" classification — devices whose card set is stable
over months, which is characteristic of a household and not of a fraud workstation, where the set
churns.

### I-04 · The IP address that identifies a carrier rather than a person

Covered as `F-09` in doc [04](04-features-and-the-feature-store.md). The operational practice:
derive from the IP the things that are still informative — the autonomous system, whether it is a
hosting provider, a known VPN exit, a residential-proxy pool, or a mobile carrier range — and treat
the address itself as a weak key. **Hosting-provider origin for a consumer card transaction is the
most useful single derivation** and it remains strong because a datacentre address is a datacentre
address no matter what privacy features exist.

Residential proxies are the counter-move and they are effective: fraud tooling now routes through
compromised home routers and consenting-but-paid residential connections, so the traffic appears to
come from an ordinary household in the right city. There is no reliable detection from the address
alone. What does work is **inconsistency across layers**: a residential IP in Ohio, a timezone of
UTC+7, a system language of Russian, and a TLS fingerprint matching an automation toolkit is a
contradiction, and contradiction across independently-sourced attributes is the detection that
survives when each individual attribute can be faked.

### I-05 · Behavioural biometrics, and the honest assessment

Typing cadence and mouse dynamics are genuinely informative for **account takeover** and for
**bot detection** and are of modest value for transaction fraud. Three honest caveats:

**The most valuable behavioural signals are the crude ones.** "Time from page load to submission
under 1.4 seconds" and "the card number was pasted rather than typed" each outperform sophisticated
cadence modelling on Ironvale's data, because they separate automation from humans, and automation
is what most volume fraud actually is. Build the crude ones first.

**Continuous behavioural authentication has a consent problem.** Collecting a continuous biometric
profile of how a person types is, under several privacy regimes, the processing of biometric data,
which has a materially higher bar than ordinary fraud-prevention processing. Doc
[13](13-explainability-compliance-and-fairness.md) covers this; the short version is that it needs
a lawful basis established before engineering starts, and "legitimate interest in preventing fraud"
is not automatically sufficient for biometric categories in every jurisdiction.

**It discriminates against people whose interaction differs for reasons unrelated to fraud.**
Motor impairments, assistive technology, unfamiliarity with the interface, an older device, and
simply being elderly all produce behavioural patterns that differ from the trained norm. A model
that treats "interacts unusually" as "risky" will systematically disadvantage disabled and older
customers, which is both a fairness problem and, in several jurisdictions, unlawful. **If you build
behavioural scoring, segment its performance by assistive-technology use and by age band and look
at the result**, and be prepared to exclude it for some populations.

### I-06 · The one signal that beats everything for scams, and the problem with it

Swiftline's dominant fraud type is the authorized push payment scam, where every device and
behavioural signal is clean because the customer really is the customer (doc
[01](01-the-running-systems-and-their-numbers.md)). The signals with actual power are:

| Signal | Lift for scam detection |
|---|---|
| Payee is new to this customer | strong, but true of most legitimate first payments too |
| Payee has received transfers from several unconnected customers this week | very strong — see doc [09](09-graphs-rings-and-linked-entities.md) |
| Amount is near a round number and far above this customer's distribution | moderate |
| Customer is on a phone call, or has been in the last 20 minutes | **the strongest single signal available** |
| The transfer follows a recent change to the account's contact details | strong |
| The payment reference matches scam-typical language | moderate, and easily evaded |

**The phone-call signal is worth about 19 percentage points of scam catch rate on Swiftline's
measurement and it is the most ethically fraught signal in this collection.** On a mobile banking
app, the operating system will tell the app whether a call is in progress. A customer being
instructed by somebody posing as their bank's fraud team is, by construction, on a call while
making the transfer, and the combination of "on a call" and "new payee" and "large round amount" is
close to diagnostic.

The problems, which you should raise yourself rather than wait to be asked:

- **It requires a permission the customer grants for a different reason**, and the lawful basis for
  using call state for risk scoring is not obviously the same as for the feature the permission was
  requested for. It needs a specific, disclosed purpose.
- **It is a signal about something outside the payment entirely.** A customer who makes a transfer
  while on a call with their builder is on the same side of the feature as a scam victim.
- **It cannot be explained to the customer without revealing the detection**, which conflicts with
  the explanation duties in doc [13](13-explainability-compliance-and-fairness.md).

**Swiftline's resolution, which is the defensible one:** use it to choose the *intervention*, not
to decline. A transfer with this signature is never blocked on the signal; it triggers a specific
in-app interruption that names the risk — "people are sometimes asked to move money while on a
call with someone claiming to be from a bank; we will never ask you to do that" — and requires an
explicit acknowledgement. **Measured effect: 11.2% of transfers in that cohort are abandoned by the
customer**, which in a population where the scam rate is around 14% means the interruption is
reaching a substantial share of the victims. No decline, no explanation problem, and the customer
retains the decision. This is the pattern Swiftline's whole design follows and it is worth stating
as a principle: **where a signal is strong but using it to block is indefensible, use it to ask a
better question.**

---

## I-07 through I-12, briefly

### I-07 · Identity resolution, and the merge that was wrong

Linking `j.smith@example.com`, `jsmith@example.com` and `j.smith+shop@example.com` as one identity
is correct for Gmail-style addresses and wrong for providers that treat dots as significant.
Merging two customers who are genuinely different propagates one's risk to the other, and is very
hard to unpick afterwards because downstream aggregates have already been computed on the merged
entity. **Practice:** keep resolution as a reversible *mapping layer* rather than by rewriting
identifiers in the event stream, record the evidence for each link with a confidence, and require a
higher confidence for links that *raise* risk than for links that lower it.

### I-08 · The emulator and the rooted device

Automation at scale runs on emulators, instrumented browsers and rooted phones. Detection signals
exist — missing hardware sensors, implausible sensor values, debugger presence, known emulator
artefacts — and they are an arms race with a well-funded other side. **What holds up better than
detection is consistency checking**: an emulator can claim any screen size, but making a claimed
screen size, device model, GPU renderer, and sensor availability mutually consistent *and*
consistent with the behavioural timing is much harder than faking any one of them.

### I-09 · The SIM swap and the one-time passcode

If your step-up is an SMS code, an attacker who has transferred the victim's number to their own
SIM passes it. The mitigations are specific: query the mobile network operator's **SIM-change API**
where available and treat a swap in the last 72 hours as a strong risk signal; prefer in-app push
or a passkey over SMS; and **never allow a contact-detail change and a high-value payment within
the same short window without a second, independent factor** — this combination is the signature
of nearly every takeover-then-drain sequence.

### I-10 · The step-up that the fraudster passes because they control the device

Device-bound authentication assumes the device is the customer's. In a remote-access scam the
fraudster is operating the customer's own device, through a screen-sharing tool the customer
installed on instruction. Every possession signal passes. **Detection:** remote-access tooling
leaves signals — accessibility-service usage on mobile, specific processes, input events with
machine-like timing, screen-sharing APIs in use — and these are among the few signals that are
*growing* in value, because the attack is growing. Several banking apps now refuse to operate
while a screen-sharing session is active, which is a blunt control and an effective one.

### I-11 · The fingerprint that is a protected characteristic

Device price tier correlates with income. Language settings correlate with ethnicity and national
origin. Accessibility-feature usage correlates with disability, directly. Each can be predictive
and each is a proxy. See doc
[13](13-explainability-compliance-and-fairness.md); the rule applied here is that
**accessibility-related attributes are excluded from scoring outright** at Ironvale, documented as
an exclusion with a stated reason, because the proxy relationship is not a correlation but a
definition.

### I-12 · The vendor score you cannot explain

Most organisations buy at least one device-risk or identity-risk score from a specialist vendor.
They are often genuinely good — the vendor sees traffic across thousands of customers, which is
information you cannot obtain. The costs to go in with your eyes open: **you cannot explain it**
(which matters for doc [13](13-explainability-compliance-and-fairness.md) and for a regulator);
**you cannot reproduce a historical decision** unless the vendor versions its scores and you log
them, which many do not; **its errors are correlated across the industry**, so a vendor failure is
everybody's failure simultaneously; and **it may silently change**, since the vendor retrains on its
own schedule with no release note to you. **Practices:** log the vendor score with its version in
your decision log, monitor its distribution as carefully as your own features, never let it be the
sole basis for a decline, and contractually require notice of material model changes. Ironvale caps
any single vendor score's contribution so that it cannot by itself move a decision across a band.

---

## What to take away

1. **Where you control the app, possession beats fingerprinting by an order of magnitude.** A hard
   device identifier plus a long authentication history is worth 25.5 percentage points of catch
   rate; sophisticated behavioural biometrics on the same channel is worth 1.9.
2. **At a third-party checkout the issuer has almost no device context at all**, which is precisely
   where card-not-present fraud happens. An issuer's device strategy is mostly a strategy for
   obtaining device data, not for analysing it — which is what 3-D Secure is for.
3. **Soft fingerprinting is a depreciating asset.** Ironvale's confident device-linkage rate on web
   fell from 71% to 43% in four years with no code change, and the weekly retrain absorbed the loss
   silently. Budget for replacement, not expansion.
4. **Monitor signal availability as a metric in its own right**, trended over months. Feature
   distribution monitoring catches deployments; availability monitoring catches structural decay.
5. **Version device identifiers across SDK upgrades and gate the rollout on a stability check.**
   An upgrade that makes every device in the world new triples step-up volume overnight.
6. **The best derived device signal is cardinality, not identity** — cards per device and devices
   per card. A device seen with 17 cards in a day is a fraud workstation whatever else is true, at
   1-in-2.8 standalone precision. Classify genuinely shared household devices explicitly, or they
   will generate false positives at volume.
7. **Against faked attributes, check consistency rather than authenticity.** Any single attribute
   can be spoofed; making screen size, device model, GPU string, sensor availability, timezone,
   language and interaction timing mutually consistent is much harder.
8. **The crude behavioural signals beat the sophisticated ones.** Time-to-submit under 1.4 seconds
   and paste-versus-type outperform cadence modelling, because most volume fraud is automation.
9. **Behavioural scoring systematically disadvantages disabled and older customers**, and
   accessibility-related attributes are a definitional proxy rather than a correlated one. Segment
   performance by assistive-technology use and age band, and be prepared to exclude.
10. **Where a signal is strong but blocking on it is indefensible, use it to ask a better
    question.** Swiftline never declines on call state; it interrupts with a specific, named warning
    and gets 11.2% voluntary abandonment in the cohort, leaving the decision with the customer.
