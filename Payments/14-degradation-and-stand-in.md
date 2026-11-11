# Degradation: What You Are Allowed to Do When Things Are Unreachable

Document 13 ended on an uncomfortable note: when a region's ledger is unavailable, payments in that
region cannot be written, and that is a real outage with no correctness-preserving workaround.

This document is about the space between "everything works" and "nothing works," which is where
most of a payment system's actual operating life is spent. The organising question is a specific
one, and it is a risk question rather than a technical one:

> **When you cannot verify that a payment is safe to accept, are you permitted to accept it anyway,
> and who absorbs the loss if you were wrong?**

Different dependencies give different answers, and the discipline is in deciding each one
deliberately, in advance, rather than discovering the answer during an incident.

---

## Classifying your dependencies first

Before designing any degradation, sort every dependency into one of three categories. The sorting
is the design.

**Category one: required for correctness.** If this is unavailable, accepting the payment risks
recording something false or losing money irrecoverably. You must fail.

- The ledger for the merchant's shard. Accepting a payment you cannot record means the merchant is
  owed money nobody knows about.
- The idempotency store (document 03). Without it you cannot deduplicate, and the double-charge
  rate goes from near zero to whatever the retry rate is.

**Category two: required for the transaction, but substitutable.** Unavailable means you cannot
complete this payment the normal way, but an alternative path exists.

- A specific network route. Document 11 covered rerouting.
- The card vault, if a cached detokenisation is available for a stored card.
- The issuer, where stand-in processing applies.

**Category three: enhances the decision but is not required.** Unavailable means you accept more
risk, and that risk has a known price.

- The fraud scoring service.
- The velocity index.
- The 3DS authentication service, with caveats below.
- Analytics, reporting, webhooks. These are not in the authorization path at all and must not be —
  if a webhook failure can block an authorization, your dependency classification is wrong before
  you start.

The single most valuable thing this classification produces is the realisation that **most
dependencies are category three**, and a system that fails closed on all of them is failing far
more often than it needs to. Northwind Pay's incident history before this work was formalised
showed roughly 40% of authorization-path incidents were caused by category-three dependencies
being treated as required.

---

## Category three: failing open, with a price

The fraud scorer is the clearest case. It has a 30 ms budget (document 16). It becomes unavailable,
or slow enough to blow the budget.

**Fail closed** — decline everything — and you reject essentially all traffic. At 9,000
transactions per second, a five-minute fraud-scorer outage declines 2.7 million payments, which is
$127 million of volume and a merchant-facing catastrophe far larger than any fraud it prevents.

**Fail open** — approve without scoring — and you accept whatever fraud arrives during the window.
Northwind Pay's fraud rate on unscored traffic is about 0.4%, against 0.042% on scored traffic. So
five minutes of unscored traffic at 2.7 million transactions and $47 average carries an expected
excess fraud loss of:

2,700,000 × $47 × (0.004 − 0.00042) = 2,700,000 × $47 × 0.00358 ≈ **$454,000**

That is a large number, and the comparison is not "fail open is cheap". The comparison is $454,000
of fraud against $127 million of declined volume, and failing open is better by two orders of
magnitude. But $454,000 is enough to justify something smarter than either extreme.

**The middle path, which is what to actually build:** fail open with a degraded policy. When the
scorer is unavailable, apply a static rule set computed from the last known model — amount
thresholds, country and BIN blocklists, velocity caps from whatever cache is still warm — and
decline the clearly-bad tail while approving the bulk. Northwind Pay's static fallback policy
catches roughly 60% of the fraud the full model would have, at the cost of declining 0.3% of
legitimate traffic.

Rerun the arithmetic with the fallback: excess fraud loss drops to about $182,000, and the
false-decline cost is 2,700,000 × 0.003 × $47 × (margin at risk) — which is a sales loss for the
merchant rather than a loss for Northwind Pay, but a real cost nonetheless.

**The rule to extract: for every category-three dependency, the fallback is not "on" or "off" but a
pre-computed degraded policy, and it must be exercised regularly.** A fallback path that has never
run in production does not work. Northwind Pay forces the fraud scorer into fallback mode for 60
seconds every Tuesday at a low-traffic hour, which is the only reason the fallback is known to
function.

### The one category-three dependency that is not

3DS authentication (document 07) looks like category three and is not, in Europe. Approving a
European transaction without SCA is not a risk decision you may make — the issuer will soft-decline
it, and if it somehow approves, the liability shift is absent and the merchant carries the fraud.

So in Europe, 3DS unavailability is category one and you fail. Elsewhere it is genuinely category
three and you may proceed without it, accepting the merchant's liability. **The same dependency has
different criticality in different jurisdictions**, which means the classification is per-region,
not global.

---

## Category two: stand-in processing

Stand-in is the most interesting degradation in payments: **somebody authorizes on the issuer's
behalf when the issuer cannot answer.**

The networks do this themselves. Visa's Stand-In Processing (STIP) and Mastercard's equivalent
activate when an issuer is unreachable: the network applies limits the issuer has pre-registered —
a maximum amount, a maximum number of transactions per card per day, blocklists — and approves or
declines on the issuer's behalf. The issuer is then obliged to honour the approval, which is why
issuers set the parameters conservatively.

Two things matter for your design.

**You will receive approvals from stand-in and they look like ordinary approvals.** The response
carries an indicator that the transaction was processed under stand-in, and you should record it,
because stand-in approvals have a slightly elevated rate of subsequent problems — the issuer may
later find the account had insufficient funds. Northwind Pay stores the stand-in indicator and
excludes those transactions from certain merchant-facing guarantees.

**You may not invent your own stand-in for issuer traffic.** You are not permitted to approve on an
issuer's behalf; only the network may. A processor that approved a transaction the issuer never saw
has created a transaction with no authorization, which clears as unauthorized and becomes an
unwinnable dispute (document 17).

So the correct handling of codes 91 and 96 is document 11's retry, possibly over an alternative
route, not local approval.

---

## Store-and-forward: the card-present exception

There is one situation where accepting a payment without an authorization is legitimate, and it is
worth understanding because it is the clearest example of a deliberate, priced risk acceptance.

A card-present terminal — a shop till, an aircraft seat-back payment terminal, a bus fare reader —
loses connectivity. The network rules permit it to accept the transaction offline, store it, and
forward it when connectivity returns. The terminal applies floor limits: amounts below a threshold
are accepted offline, above it declined.

The arrangement works because:

- The card is physically present and the chip has performed an offline authentication, which gives
  some assurance that the card is genuine.
- The floor limit bounds the per-transaction exposure.
- The merchant accepts the risk for amounts under the limit, and the network rules say so.

The economics are explicit: an airline accepting a $14 drink purchase offline at 38,000 feet is
choosing a few percent of uncollectable transactions over zero sales. The floor limit is set where
the expected loss equals the expected lost revenue.

**This does not generalise to card-not-present.** There is no offline authentication, no physical
card, no floor-limit rule, and an e-commerce merchant that "accepts now, authorizes later" is simply
shipping goods on an unverified promise. Northwind Pay is 94% card-not-present and so
store-and-forward applies to a small slice of its volume — but the slice exists, and its records
arrive hours or days after the transaction, which the capture and reconciliation paths must handle.

---

## Load shedding: choosing what to drop

At 237 times the average load (document 00), capacity planning cannot mean provisioning for peak
multiplied by a safety factor. Some events exceed what you provisioned, and the system must choose
what to drop rather than collapsing uniformly.

Uniform collapse is the failure to avoid. When a system at capacity slows down for everybody,
timeouts rise across the board, clients retry, the retries add load, and the system goes from
degraded to unavailable. This is the standard congestion collapse, and in payments it has the extra
sting that every shed request is a transaction whose outcome is now ambiguous (document 06).

Northwind Pay sheds by priority, with the order decided in advance:

| Priority | Traffic | Behaviour under shed |
|---|---|---|
| 1 | Authorization, capture, refund | Never shed |
| 2 | Reversals and dispute responses | Never shed — these are time-bounded obligations |
| 3 | Webhook delivery | Queued, delivery delayed |
| 4 | Dashboard and reporting reads | Rate-limited, 429 with retry-after |
| 5 | Bulk exports, search, analytics | Rejected with a clear error |

The priority-2 row is easy to get wrong. A reversal (document 06) looks like cleanup and is
actually urgent: it is the mechanism that resolves an ambiguous authorization, and shedding it
converts a recoverable ambiguity into a stuck hold on a customer's card. Dispute responses have a
hard deadline set by the network (document 17), and a shed dispute response is a lost dispute.

The shedding must also be **per-merchant, not global**. A card-testing attack against one merchant
generating 40,000 requests per second must not shed traffic for the other 39,999. Per-merchant rate
limits with a global ceiling, rather than a global ceiling alone, is the arrangement that works —
the same bulkhead reasoning as documents 12 and 13.

---

## The decision table, written down in advance

The artefact this document argues for is a table, agreed between engineering, risk and finance,
that says what happens for each dependency. Northwind Pay's, abbreviated:

| Dependency unavailable | Category | Behaviour | Who absorbs the risk |
|---|---|---|---|
| Merchant shard ledger | 1 | Fail, retryable 503 | Nobody — no payment is accepted |
| Idempotency store | 1 | Fail, retryable 503 | Nobody |
| Card vault | 2 | Cached token resolution for stored cards; fail for new cards | Northwind Pay, bounded by cache TTL |
| A network route | 2 | Reroute | Nobody |
| Issuer | 2 | Retry; accept network stand-in | Issuer, per network rules |
| Fraud scorer | 3 | Static fallback policy | Northwind Pay, approx. $182k per 5 min outage |
| Velocity index | 3 | Proceed, flag for async review | Northwind Pay |
| 3DS, EEA | 1 | Fail | Nobody |
| 3DS, non-EEA | 3 | Proceed without liability shift | Merchant, disclosed in the agreement |
| Webhook delivery | 3 | Queue, deliver late | Nobody |

Three properties of that table matter more than its contents.

**It has a "who absorbs the risk" column.** A degradation decision without a named loss-bearer is
not a decision; it is an engineer choosing on behalf of the business without being asked to. The
fraud-scorer row commits Northwind Pay to a specific expected loss, and that commitment was signed
off by someone with the authority to sign it off.

**It is exercised.** Each row has a corresponding test — a game day, a scheduled fallback window, or
a fault injection — and a row that has not been exercised in the last quarter is assumed broken.

**It is per-region.** The 3DS rows show why. A single global table would have to pick one answer for
a dependency whose criticality is a function of jurisdiction.

---

## The thing not to build

One pattern is worth warning against explicitly, because it is appealing and produces unbounded
loss: **queueing authorizations to send later.**

The reasoning goes: the issuer is unreachable, so rather than declining, store the authorization
request and send it when the issuer recovers. The customer has a good experience, and the payment
completes eventually.

What actually happens: you have told the customer and the merchant that a payment succeeded, when
no authorization exists. The merchant ships. Some fraction of the queued authorizations are then
declined when they are finally sent — insufficient funds, stolen card, closed account — and the
goods are gone with no money. The loss is unbounded in the sense that it scales with how long the
outage lasted and how much volume you queued, and you have converted a bounded availability problem
into an unbounded financial one.

The exception is store-and-forward above, which is legitimate *because* it is bounded: card
present, chip-authenticated, under a floor limit, with network rules allocating the risk. Those
four conditions are what make it safe, and an e-commerce version has none of them.

The general rule worth carrying: **never report success for a financial operation you have not
performed.** Degradation may reduce what you can do. It may not change what you say you did.

---

## What to take away

1. **Classify every dependency as required-for-correctness, substitutable, or enhancing**, before
   designing any degradation. About 40% of Northwind Pay's authorization-path incidents came from
   treating enhancing dependencies as required.
2. **Failing closed on the fraud scorer declines $127 million of volume to prevent $454,000 of
   fraud.** Do the arithmetic; it usually favours failing open.
3. **The fallback for an enhancing dependency is a pre-computed degraded policy, not an on/off
   switch** — and it must be exercised on a schedule, because an untested fallback does not work.
4. **The same dependency has different criticality in different jurisdictions.** 3DS is required in
   the EEA and optional elsewhere, so the classification is per-region.
5. **Only the network may stand in for an issuer.** Record the stand-in indicator when you receive
   one; never invent local approval, which creates an unauthorized transaction and an unwinnable
   dispute.
6. **Store-and-forward is legitimate only because four conditions bound it:** card present, chip
   authentication, a floor limit, and network rules that allocate the risk. Card-not-present has
   none of them.
7. **Shed by pre-agreed priority, and never shed reversals or dispute responses** — both are
   time-bounded obligations whose loss is permanent.
8. **Shed per merchant, not globally**, or one merchant under attack degrades everyone.
9. **The degradation table needs a "who absorbs the risk" column with a real name in it.** Without
   one, an engineer is making a business decision nobody asked them to make.
10. **Never report success for a financial operation you have not performed.** Degradation changes
    what you can do, not what you claim to have done.

Next: [15-pci-scope-and-tokenization.md](15-pci-scope-and-tokenization.md), on keeping card data in
one place and everything else out of scope.
