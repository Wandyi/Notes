# Observability for Money: When the Most Important Signals Are Not Technical

Standard service monitoring — request rate, error rate, latency, saturation — tells you whether your
payment system is *running*. It does not tell you whether it is *working*, and in payments those are
genuinely different questions.

A payment system can be perfectly healthy by every technical measure while declining every
transaction from one issuer, double-posting a class of refunds, or silently failing to include a
day's captures in the clearing file. None of those produce an error, elevated latency or a
saturation signal. This document is about the monitoring that catches them.

---

## The outage that every dashboard misses

Start with a concrete scenario, because it motivates everything below.

At 02:14 UTC, a deploy changes how Northwind Pay populates data element 48 in the authorization
message — an optional field carrying merchant descriptor information. The change is valid per the
specification. Most issuers ignore the field.

Meridian Bank does not ignore it. Its validation rejects the message format, and it begins declining
every Northwind Pay transaction with code 14, "invalid card number".

What the technical dashboards show at 02:30:

- Request rate: normal.
- Error rate: **zero**. A decline is a successful API response. The 0100 got a 0110; the exchange
  worked perfectly.
- Latency: normal, slightly improved — declines are faster than approvals.
- Saturation: normal.
- Service health checks: all green.

Every single standard signal says the system is healthy. Meanwhile Meridian Bank is 1.9% of
Northwind Pay's volume, so at an average of 38 transactions per second overnight, about 0.7
transactions per second are being wrongly declined — 2,600 an hour, roughly $122,000 of volume an
hour, and every one of those customers is being told their card number is invalid.

Nothing pages anyone. The problem is found at 09:40 when a merchant complains.

**The gap is structural:** in a payment system, the most consequential failures are *correct
responses carrying wrong outcomes*. No amount of refinement to error-rate monitoring detects them,
because there is no error.

---

## The four signal families

Organise payment observability into four families. The first is the standard one and the other
three are what this document adds.

**Family one: technical health.** Request rate, error rate, latency, saturation, dependency health.
Necessary, insufficient, and well covered elsewhere.

**Family two: business outcomes.** Approval rate, decline-code mix, authorization-to-capture
conversion, 3DS challenge and abandonment rate. These are the signals that catch the Meridian Bank
scenario.

**Family three: financial correctness.** Ledger balance assertions, reconciliation break
population, settlement variance, the unresolved-authorization residue. These catch silent money
bugs.

**Family four: pipeline liveness.** Did the clearing file go out? Did the bank statement arrive? Did
the payout job run? Did the dispute file get ingested? These catch the batch failures that produce
no error because nothing ran.

Family four is the one most often missing entirely, and it has a distinctive property: **the failure
is the absence of an event, and absence does not emit a metric.** A clearing file that is not
submitted produces no log line, no error and no latency spike. The only way to detect it is to
assert positively that it happened, which means monitoring for the *presence* of expected events
rather than for the presence of problems.

---

## Approval rate as the primary business SLI

Approval rate is the single most useful payment metric and is nearly useless in aggregate, for the
reason document 11 gave: the aggregate moves for dozens of reasons and a serious single-issuer
problem is invisible inside it.

Meridian Bank at 1.9% of volume going from 88% approval to 0% moves the portfolio approval rate
from 87.3% to 87.3% − (0.019 × 0.88 × 100) ≈ **85.6%**. A 1.7-point drop. On a metric that varies by
more than that between a Tuesday and a Saturday, and between 03:00 and 20:00. No static threshold
catches it, and a dynamic one tuned loosely enough to avoid false alarms will not catch it either.

The decomposition that works, with the alert that goes with each dimension:

**By issuer BIN.** The highest-value dimension. Northwind Pay alerts on any BIN with more than 500
daily attempts whose approval rate drops more than 10 percentage points week-over-week, comparing
the same hour of the same weekday. With roughly 3,400 BINs above that volume threshold, the alert
fires a few times a month, and about a third of those are a Northwind Pay problem — usually, as in
the scenario, a message-construction change that one issuer validates more strictly.

**By decline code.** Monitor the *mix*, not the total. A rise in code 14 means your message
construction; a rise in code 51 means economic conditions or a change in customer mix; a rise in
code 65 or 1A means an SCA configuration problem (document 07). The mix shifting is informative
even when the total is flat, and a flat total with a shifting mix is a signature of a real change
that aggregate monitoring cannot see.

**By merchant.** Catches checkout changes and card-testing attacks (document 16). Alert on a
merchant's own week-over-week delta, since merchants' baselines differ enormously.

**By route** (document 11), **by region**, **by card brand**, **by 3DS outcome**.

The practical structure is a metric with those labels and a set of comparison-based alerts rather
than threshold-based ones. **Comparison to the same hour last week is the right baseline** in
payments, because the traffic has strong daily and weekly seasonality and any fixed threshold is
either too loose at peak or too tight at trough.

---

## Financial correctness monitoring

These are the signals that catch money bugs, and they are assertions rather than measurements.

**The ledger balances.** Document 08's constraint guarantees each transaction sums to zero, but the
*aggregate* identity is worth asserting independently and continuously: total assets minus total
liabilities minus equity equals zero. A violation means a bug in entry generation that the
per-transaction check missed — typically an entry written outside the normal path. Northwind Pay
asserts this every 15 minutes over the current business date's entries and pages on any
non-zero result.

**Reconciliation break population.** From document 10: the open count, the aged count (target zero
over 30 days), and the total unresolved value as a fraction of settled value (target under 0.002%).
These three are correctness SLIs and belong on the primary dashboard, not in a finance team's
spreadsheet.

**Settlement variance.** The difference between expected and received settlement, per batch, as a
fraction. Document 09 set the target below 0.001%. A sudden widening is the earliest signal of a
systematic fee or clearing problem.

**The authorization memo residue.** Document 08 put authorizations into memo accounts that net to
zero over a complete lifecycle. A non-zero residue older than 7 days means the expiry job is not
completing. This is a cheap, sharp, purely internal signal and it catches a whole class of
lifecycle bugs.

**Duplicate detection.** A direct query for the double-charge pattern: the same card token, the same
merchant, the same amount, within a short window, with different payment ids. Some of these are
legitimate — a customer buying two identical coffees — so the signal is a rate rather than an
incident, and a rise in the rate is what matters. Northwind Pay's baseline is 0.004% of
transactions and an alert fires above 0.012%.

**Refund-to-capture ratio by merchant.** A sudden rise means either a merchant problem or a
double-refund bug, and both are worth knowing about within hours rather than at month-end.

---

## Pipeline liveness: monitoring for absence

The pattern for family four is a **heartbeat with an expected deadline**: each pipeline stage
publishes a completion record, and a separate checker asserts that the record exists by the time it
should.

| Expected event | Deadline | If missing |
|---|---|---|
| Clearing file submitted, per network per region | 23:30 UTC | Page immediately |
| Network acknowledgement received | 01:00 UTC | Page |
| Network response file ingested | 07:00 UTC | Page |
| Bank statement ingested | 08:30 UTC | Page |
| Reconciliation completed and day closed | 11:00 UTC | Page |
| Payout batch submitted | 07:00 local | Page |
| Dispute file ingested | 06:00 UTC | Page — deadlines are running, document 17 |
| Account updater batch | Weekly, Sunday | Ticket, not a page |

Two points about this table.

**The deadlines are derived from downstream obligations, not from convenience.** The dispute file
must be ingested by 06:00 because document 17's response deadlines run from the network's date and
every hour of ingestion lag is an hour removed from the merchant's response window. The clearing
file must go by 23:30 because a missed cut-off delays settlement for every merchant in it by a day.

**A page for a missing batch is correct.** Engineers resist paging on batch jobs because they feel
less urgent than a service outage. In payments they are frequently more consequential: a missed
clearing file delays $130 million of settlement, and a missed dispute ingestion silently loses
disputes.

---

## Tracing, with a payments-specific requirement

Distributed tracing works as it does elsewhere, with one addition: **the trace must be joinable to
the money.**

A trace identifies a request. A payment identifies money. Investigating "why did this merchant's
payment behave oddly" starts from a payment id, not a trace id, so the mapping must be stored and
queryable in both directions. Northwind Pay indexes `payment_id → trace_id` and retains it for 400
days, matching the retention of the submitted clearing records from document 10.

The retention is the unusual part. Ordinary tracing retains for days. A dispute arriving 120 days
after a transaction, escalating to arbitration 200 days later, generates the question "what exactly
happened at authorization time" **540 days** after the fact. A trace that has expired cannot answer
it, and the alternative is reconstructing from logs that have also expired.

The resolution is tiered: full traces for 14 days, and a permanent, compact **payment timeline** —
every state transition, every outbound message's key fields, every decision and its reason, hashed
message bodies — retained for 540 days. The timeline is not a trace; it is a purpose-built record
designed to answer the dispute-era question, and it is a few kilobytes per payment rather than the
hundreds that a full trace costs.

At 1.2 billion payments a year and, say, 3 KB per timeline, that is 1,200,000,000 × 3,000 bytes ≈
**3.6 TB a year**, which is an entirely acceptable cost for being able to answer any question about
any payment for eighteen months.

---

## What to put in front of a human

Dashboards accumulate panels until nobody reads them. The discipline that works is to have exactly
three levels and to be strict about what qualifies for each.

**Level one: is money moving correctly right now?** Five numbers, no more.

- Approval rate against the same hour last week
- Authorization p99 latency
- The count of payments currently in `unknown` state
- Open critical reconciliation breaks
- Pipeline liveness: green or not

**Level two: the decompositions.** Approval rate by BIN, by code, by merchant, by route. The
reconciliation break population by type and age. Settlement variance by batch. This is where
on-call goes after level one shows something.

**Level three: everything else.** Per-service technical metrics, queue depths, database
performance. Needed during an incident, not watched.

The point of this hierarchy is that **level one must contain no technical metrics except latency**.
Every other number on it is about outcomes. A dashboard whose top-level view is CPU and memory
trains the on-call to think about the wrong failures, and the Meridian Bank scenario is the one it
will miss.

---

## Alerting on what actually pages

The alerts worth having, in rough order of how often they catch something real at Northwind Pay:

1. **Per-BIN approval rate drop**, as above. Catches issuer-specific message problems, which is the
   most common real defect.
2. **Pipeline liveness failures.** Deterministic, no false positives, high consequence.
3. **Ledger identity violation.** Rare and always real.
4. **`unknown` state population above baseline.** Indicates a network or connectivity problem before
   anything else shows it, because document 06's resolution path absorbs the symptom.
5. **Duplicate-charge rate above threshold.** Catches idempotency regressions.
6. **Reconciliation break creation rate.** A step change means a new bug, several hours before
   anyone could find it by other means.
7. **Per-merchant distinct-card rate.** Card testing, per document 16.
8. **Settlement variance widening.**

Notice how far down the list conventional error-rate and latency alerts appear: they are not on it,
because they belong to family one and catch a class of failure that is both less common and more
obvious. They should still exist. They should not be what the payment on-call primarily watches.

---

## What to take away

1. **The most consequential payment failures are correct responses carrying wrong outcomes.** A
   single-issuer decline storm produces zero errors, normal latency and green health checks.
2. **Four signal families: technical health, business outcomes, financial correctness, and
   pipeline liveness.** The last three are what payment-specific monitoring means.
3. **Pipeline failures are the absence of an event, and absence emits no metric.** Assert positively
   that expected events happened, with deadlines derived from downstream obligations.
4. **Approval rate in aggregate is nearly useless** — a total single-issuer failure moves it 1.7
   points, inside normal daily variation. Decompose by BIN, code, merchant and route.
5. **Compare to the same hour last week, not to a fixed threshold.** Payment traffic has strong
   daily and weekly seasonality, and any static threshold is wrong at one end of it.
6. **A shifting decline-code mix with a flat total is a real signal** that aggregate monitoring
   cannot see; a rise in code 14 is your own message construction.
7. **Financial correctness signals are assertions, not measurements** — the ledger identity, the
   break population, settlement variance, the authorization memo residue.
8. **Page on missing batches.** A missed clearing file delays $130 million of settlement; a missed
   dispute ingestion silently loses disputes.
9. **Keep a compact payment timeline for 540 days**, because arbitration asks what happened at
   authorization time eighteen months later. Full traces for 14 days, timelines at about 3 KB each
   for 3.6 TB a year.
10. **The top-level dashboard contains no technical metrics except latency.** Everything else on it
    is an outcome, or the on-call learns to watch the wrong failures.

Next: [19-the-failure-catalogue.md](19-the-failure-catalogue.md), which collects the failure modes
from across the collection and walks the whole design end to end.
