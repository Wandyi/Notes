# Credit Card Payments: A System Design Collection

Twenty documents on designing a card payment system, from what actually moves when a card is
charged through to the failure catalogue. Written to be read in order, each document building on the
ones before it, with every number derived rather than asserted.

---

## The running system

Every document derives its figures from one imaginary company, so the numbers stay consistent and
you can carry a single mental picture through the whole collection.

**Northwind Pay** is a payment service provider and its own acquirer-processor, serving 40,000
merchants.

| Quantity | Value |
|---|---|
| Authorization attempts per year | 1.2 billion |
| Average throughput | 38 per second |
| Peak throughput | 9,000 per second (237× average) |
| Approval rate | 87.3% |
| Average ticket | $47 |
| Annual authorized volume | $49.2 billion |
| p99 end-to-end authorization | under 800 ms |
| Ledger entries per year | about 13 billion |
| Stored card tokens | 310 million |
| Dispute rate | 0.08% of captures |
| Fraud rate | 0.042% |
| Regions | 3 |
| Services in PCI scope | 2 of 180 |

Supporting cast: **Harbour Athletics** is the example merchant, **Meridian Bank** the example
issuer, and a $47 pair of running shoes the example purchase.

---

## The documents

### Foundations

**[00 — What actually moves when a card is charged](00-what-actually-moves.md)**
The four-party model built one party at a time, where a PSP sits, who takes what out of $47, and why
no party in the flow trusts any other. Establishes the running system's numbers.

**[01 — The life of one $47 payment, with the clock attached](01-the-life-of-one-payment.md)**
Authorization, capture, clearing, settlement, payout — six calendar days, with what changes at each
stage and the three flows that run backwards.

**[02 — Money is not a float](02-money-is-not-a-float.md)**
Integer minor units, currencies without two decimal places, presentment versus settlement currency,
rounding rules, and splitting an amount so the parts sum exactly to the whole.

### The hot path

**[03 — The API and idempotency](03-the-api-and-idempotency.md)**
The primitive that makes retries safe: atomic claim, request hashing, leases, in-progress conflicts,
and why the response must commit with the effect.

**[04 — The payment state machine](04-the-payment-state-machine.md)**
Why status must be derived rather than stored, what the real states are, the transitions that must
be refused, and why `unknown` is mandatory.

**[05 — Talking to the networks](05-talking-to-the-networks.md)**
ISO 8583, persistent multiplexed connections, response codes, and the two deadlines you need
instead of one.

**[06 — The timeout problem](06-the-timeout-problem.md)**
What to do when you do not know whether money moved. The reversal, the in-flight request you must
not abandon, and why ambiguity is resolved by forcing a known state rather than discovering truth.

**[07 — 3-D Secure and the async interruption](07-3d-secure-and-the-async-interruption.md)**
The liability shift, the human inserted into a backend flow, challenge abandonment as the largest
conversion loss in European payments, and exemptions.

### Correctness and money

**[08 — The ledger](08-the-ledger.md)**
Why you never update a balance. Double-entry derived from the problem, the chart of accounts,
balances as projections, and an account for everything that does not balance.

**[09 — Settlement, payouts, reserves and negative balances](09-settlement-and-payouts.md)**
Clearing files with deterministic identifiers, what settlement actually delivers, computing what is
genuinely available to pay out, and what happens when a merchant's balance goes negative.

**[10 — Reconciliation](10-reconciliation.md)**
Establishing truth between three parties who each keep their own books. Two matches, the 0.6% that
does not match exactly, breaks as managed work, and the daily close as a checkable assertion.

### Failure and scale

**[11 — Declines, retries and dunning](11-declines-and-retries.md)**
$7.2 billion of declined attempts a year. Hard versus soft, the two different retry problems,
pay-date-aligned dunning schedules, account updaters, and routing as recovery.

**[12 — Webhooks and the outbox](12-webhooks-and-the-outbox.md)**
Exactly-once is a lie. The transactional outbox, per-merchant delivery isolation, why ordering
should be recoverable rather than guaranteed, and the four things that make duplicate-safe
consumption achievable.

**[13 — Partitioning, hot keys and multi-region](13-partitioning-and-hot-keys.md)**
Partition key selection, the merchant who is 11% of volume, what cannot be partitioned, and why a
ledger is single-writer per merchant.

**[14 — Degradation and stand-in](14-degradation-and-stand-in.md)**
What you are allowed to do when dependencies are unreachable. Dependency classification, failing
open with a priced fallback, store-and-forward's four bounding conditions, and the degradation
table with a named loss-bearer.

### Around the edges

**[15 — PCI scope and tokenization](15-pci-scope-and-tokenization.md)**
Scope as an architectural choice, the vault's three operations, keeping the PAN out of your network
entirely, holding the boundary by mechanism, and network tokens.

**[16 — Fraud as a control plane](16-fraud-as-a-control-plane.md)**
Why a system that catches nearly all fraud is losing money, what fits in 30 ms, the approve-and-flag
decision, feature freshness against an adversary, and card testing.

**[17 — Disputes and chargebacks](17-disputes-and-chargebacks.md)**
The longest state machine in the system. Reason codes grouped by what evidence wins, deadlines that
run from someone else's clock, the monitoring programmes, and why threshold effects dominate the
economics.

**[18 — Observability for money](18-observability-for-money.md)**
When the most important signals are not technical. Four signal families, approval rate decomposed,
financial correctness as assertions, monitoring for absence, and the 540-day payment timeline.

### Close

**[19 — The failure catalogue, and the whole design end to end](19-the-failure-catalogue.md)**
Failure modes organised by symptom, the six decisions that define the design, what was deliberately
not built, the build order, and three case studies.

---

## If you are reading selectively

**Interviewing on payments.** Read 00, 01, 06, 08 and 19. Those five carry the structural argument;
the rest is depth you can reach for.

**Integrating against a PSP as a merchant.** Read 01, 03, 04, 06, 07 and 12. These cover everything
that will bite your integration.

**Building the ledger.** Read 02, 08, 09 and 10 together. They are one argument split across four
documents and none of them stands alone.

**Operating an existing system.** Read 18 and 19 first, then follow the cross-references back.

---

## The through-line

If the collection has one argument, it is this. A payment system moves other people's money between
organisations that share no database and will never participate in a distributed transaction with
you. Everything difficult follows from that:

- you cannot know synchronously whether a remote effect happened, so **ambiguity is a modelled state
  rather than an error** (documents 04, 06);
- your own records cannot validate themselves, so **truth is established by comparison against
  external records** (document 10);
- any number you report must be explicable later, so **balances are projections over an immutable
  journal** (document 08);
- and every one of those properties is cheap to build in at the start and effectively impossible to
  retrofit, because each of them is about **recording information you will need later** — and the
  information is destroyed the moment you choose not to record it (document 19).
