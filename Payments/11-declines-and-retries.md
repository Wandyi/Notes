# Declines, Retries and Dunning: What to Do When the Issuer Says No

Document 05 catalogued the response codes. This document is about what you do with them, and it is
the most commercially consequential engineering in the entire system.

Here is why. Northwind Pay's approval rate is 87.3%, which means 12.7% of 1.2 billion attempts are
declined — 152.4 million declined authorizations a year. At an average ticket of $47 that is
152,400,000 × $47 = **$7.2 billion of attempted purchases that did not complete.** Recovering even
2% of that is $143 million of volume, which at Northwind Pay's $0.631 of margin per transaction is
roughly $1.9 million of revenue. No other piece of engineering in this collection has that
leverage.

It is also the area where doing the obvious thing — retry until it works — causes real harm, both
to cardholders and to your standing with issuers. So the design is about retrying *correctly*, which
means knowing what a decline actually means.

---

## The distinction everything rests on

Document 05 introduced hard and soft declines. Make the distinction sharp, because every retry
decision follows from it.

**A hard decline is a statement about the card.** The card is expired, cancelled, reported stolen,
or does not exist. These facts will not change in an hour. Retrying is futile, and for the
fraud-related codes it is worse than futile: the networks monitor repeated attempts against cards
reported lost or stolen, and a processor that generates them acquires a reputation that depresses
its approval rate across the board.

**A soft decline is a statement about this moment.** Insufficient funds right now. Too many
transactions today. A velocity limit tripped. These facts genuinely change, and a retry at the
right time genuinely succeeds.

Northwind Pay's classification, with the observed retry success rate for each — the fraction of
retried attempts that are eventually approved:

| Code | Meaning | Class | Retry success |
|---|---|---|---|
| 51 | Insufficient funds | Soft | 31% |
| 61 | Exceeds amount limit | Soft | 24% |
| 65 | Exceeds frequency limit | Soft | 41% |
| 05 | Do not honour | Soft, ambiguous | 18% |
| 91 | Issuer inoperative | Retryable, not a decline | 86% |
| 96 | System malfunction | Retryable, not a decline | 89% |
| 54 | Expired card | Hard, recoverable with new data | 4% |
| 14 | Invalid card number | Hard | 1% |
| 41 | Lost card | Hard, never retry | — |
| 43 | Stolen card | Hard, never retry | — |
| 59 | Suspected fraud | Hard, never retry | — |
| 57 | Not permitted to cardholder | Hard | 2% |

Three readings of that table are worth making explicit.

**Codes 91 and 96 are the cheapest wins available.** An 86–89% retry success rate means these are
not declines at all — the issuer simply could not answer. Any system that classifies them with the
rest of the declines is throwing away the easiest recovery in payments. The correct handling is an
immediate retry, within seconds, often to a different network route.

**Code 54, expired card, is hard but recoverable.** Retrying the same card number fails. Retrying
with *updated* card data succeeds, and the account updater services described below are exactly
that mechanism. The 4% success rate shown is for naive retries; with an account updater it rises
substantially.

**Code 05's 18% is a real number and a trap.** 18% of retried "do not honour" declines eventually
succeed, which looks worth pursuing. But 05 is also where issuers hide fraud declines, and
aggressive retrying against it is precisely the behaviour that damages your issuer relationships.
The resolution is a conservative schedule, discussed below, rather than either ignoring 05 or
hammering it.

---

## Two different retry problems

The word "retry" covers two situations that need completely different designs, and conflating them
is a common structural error.

**Technical retry.** The request did not get a clean answer — a timeout, a 91, a 96, a connection
failure. The customer is still present, waiting. The goal is to get an answer within seconds.
Document 06 covered the ambiguity management; the retry itself is immediate, bounded to one or two
attempts, and may route differently.

**Commercial retry, also called dunning.** The issuer gave a definite soft decline. The customer
may or may not still be present. The goal is to get paid eventually, over hours or days. This is
scheduled, not immediate.

The design differences are complete:

| | Technical retry | Dunning |
|---|---|---|
| Trigger | No answer, or 91/96 | Definite soft decline |
| Timescale | Seconds | Hours to weeks |
| Customer present | Yes | Usually not |
| Attempts | 1–2 | 3–8 |
| Mechanism | In-request | Scheduled job |
| Idempotency | Same key (document 03) | New attempt each time |
| Success rate | 86–89% | 18–41% |

Note the idempotency row especially. A technical retry is *the same logical attempt* and reuses
the idempotency key. A dunning attempt is a *new attempt* and must use a new key, or the
deduplication will return the original decline forever. Teams that build dunning on top of the
technical retry path hit exactly this and conclude that dunning does not work.

---

## Designing the dunning schedule

Here is where judgement is required, because the schedule trades recovery rate against three
costs: issuer reputation, per-attempt fees, and customer irritation.

Start from what actually changes between attempts. For code 51, insufficient funds, the thing that
changes is **the cardholder's balance**, and balances change on paydays. So a schedule that
retries at intervals unrelated to pay cycles is leaving money on the table, and the single
highest-value heuristic in dunning is to **retry a 51 shortly after the dates on which people are
paid** — which in most markets clusters around the 1st, the 15th, and the last working day of the
month.

Northwind Pay's schedule for a subscription renewal declined with code 51:

| Attempt | Timing | Rationale |
|---|---|---|
| 1 | Original | — |
| 2 | +3 days | Short-term balance fluctuation |
| 3 | +7 days | Approaching a likely pay date |
| 4 | +14 days | Second pay cycle |
| 5 | +21 days, or next 1st/15th | Final, aligned to pay date |

Five attempts over three weeks, then stop. The stopping rule matters as much as the schedule: a
subscription that cannot be collected after five attempts is not going to be collected, and
continuing to attempt it generates network fees and issuer irritation for no return.

For code 05, the schedule is deliberately shorter and sparser — **two attempts, at +1 day and +5
days** — because of the fraud-decline ambiguity. And for codes 41, 43 and 59, there is no schedule:
the attempt chain stops permanently and the stored payment method is marked dead.

### The per-attempt economics

Worth deriving, because it bounds the schedule. Each attempt costs Northwind Pay roughly $0.025 in
fixed network message fees (document 00) plus a few tenths of a cent of compute. Call it $0.03.

A $47 subscription with a 31% eventual recovery rate across five attempts: the expected recovery is
0.31 × $47 = $14.57 of volume, yielding 0.31 × $0.631 = $0.196 of margin. The attempts cost up to 5
× $0.03 = $0.15.

That is a margin of $0.046 per dunned subscription — thin, and it goes negative below about a $12
ticket or below a 24% recovery rate. Which means **the schedule should be a function of the ticket
size**, and Northwind Pay's is: subscriptions under $15 get three attempts rather than five. A
single fixed schedule applied to all tickets loses money on the small ones.

---

## Account updaters: fixing the card rather than retrying it

For the hard-but-recoverable declines — expired card, card reissued after a breach — retrying is
pointless and updating is the answer.

Visa calls theirs the **Visa Account Updater** (VAU); Mastercard's is the **Automatic Billing
Updater** (ABU). Both work the same way: you submit a list of stored card numbers, and the service
returns updated numbers and expiry dates for any that the issuer has reissued.

For a PSP holding 310 million stored card tokens, this is a significant ongoing process. Cards
expire on a roughly three-year cycle, so in steady state about a third of stored cards need
updating each year — 310,000,000 ÷ 3 ≈ **103 million updates a year**, or 283,000 a day.

Three implementation notes that matter.

**Run it ahead of the charge, not after the decline.** A weekly batch over cards with upcoming
subscription renewals catches the expiry before the customer sees a failure. Running it reactively
after a 54 means the customer already got a failed-payment email.

**An updated card number is a new PAN and the old token must map to it.** The merchant stored
`pm_1KqX9z` and must continue to use it; the vault's mapping from token to PAN changes underneath.
This is one of the clearest arguments for tokenisation as the merchant-facing interface, discussed
in document 15 — a merchant that stored the PAN itself cannot benefit from an account updater
without a data migration.

**Network tokens largely eliminate the problem.** A network token (document 15) is automatically
kept current by the network when the underlying card is reissued, so the expiry-driven decline
class mostly disappears. Northwind Pay's network-tokenised volume has a 54-code rate about 94%
lower than its PAN-based volume, which is one of the strongest arguments for pushing merchants
toward network tokens.

---

## Routing as a recovery mechanism

Not every decline is the issuer's final word on the transaction, because not every decline came
from the issuer. A 91 or 96 may originate from a network switch or an intermediate processor, and
retrying over a different path can succeed.

Where a card is **dual-badged** — carrying both a local scheme and an international one, which is
common in several European and Asian markets — you have a genuine routing choice and the approval
rates differ measurably by route. Northwind Pay observes approval-rate differences of 1 to 3
percentage points between routes for the same card population, which at scale is a large number.

The mechanism is a routing policy evaluated per authorization: given the BIN, the amount, the
merchant, the currency and the current health of each route, choose a path. The policy needs:

- **Health signals per route**, updated continuously — approval rate, latency, error rate, over a
  short window.
- **A hysteresis rule**, so a route that dips for ninety seconds is not abandoned and then
  stampeded back onto. Northwind Pay requires 5 minutes of recovered health before restoring a
  route's share.
- **An audit record of which route was used**, because a merchant asking why approval rates
  dropped on Tuesday needs an answer, and because reconciliation (document 10) must know which
  network's files to expect the record in.

Be careful here: routing is also the mechanism by which a bug becomes systemic. A policy that
shifts all traffic onto one route because its approval rate looked best for ten minutes will
overwhelm that route and degrade everything. Cap the rate of share change, not just the share.

---

## What the merchant and the customer are told

The engineering is only half of it. The decline is a customer-facing event, and the message
determines whether the sale is recoverable.

**Never show the raw code.** "Your payment was declined: code 05" is useless to everybody.

**Map to an action the customer can take.** Northwind Pay's mapping:

| Internal class | Customer-facing message |
|---|---|
| Insufficient funds | "Your card was declined. Please try a different card or contact your bank." |
| Expired card | "This card has expired. Please update the expiry date or use a different card." |
| Invalid number | "We couldn't read that card number. Please check and try again." |
| Suspected fraud | "Your bank declined this payment. Please contact them or use a different card." |
| Issuer unavailable | "We couldn't reach your bank. We're trying again — please don't re-enter your card." |

That last one is important and usually missing. During an issuer outage, telling the customer their
card was declined causes them to try another card, then another, generating a cluster of attempts
across multiple cards that looks exactly like card testing to your own fraud system (document 16).
Telling them the truth — that the bank is unreachable and you are retrying — prevents that.

**Do not tell the customer why their bank declined, when their bank did not say.** It is tempting to
map 05 to "insufficient funds" because that is often what it means. It is also often a fraud
decline, and telling a customer with money in their account that they have insufficient funds
sends them to their bank to complain about a problem that does not exist.

---

## The monitoring that catches the problems

Approval rate is the headline metric, and watching it in aggregate is nearly useless, because a
single aggregate number moves for dozens of reasons. The decomposition that works:

- **By issuer BIN.** A single issuer's approval rate collapsing is the most common real alert, and
  it is invisible in the aggregate — one issuer is rarely more than 2% of volume.
- **By decline code.** A shift in the *mix* of codes is more informative than the total. A sudden
  rise in code 14, invalid card number, almost always means a bug in your own message construction,
  not a change in customer behaviour.
- **By merchant.** A single merchant's approval rate dropping usually means they changed their
  checkout, or they are under a card-testing attack.
- **By route**, for the reasons above.
- **By 3DS outcome**, because document 07's step-up path has its own failure modes.

Northwind Pay's most valuable single alert is **"approval rate for any BIN with more than 500 daily
attempts dropped more than 10 percentage points week-over-week."** It fires a few times a month, and
roughly a third of those are a real problem on Northwind Pay's side — usually a message-construction
change that one issuer validates more strictly than others.

---

## What to take away

1. **12.7% of attempts decline, which is $7.2 billion of attempted purchases a year.** Recovery
   engineering has more commercial leverage than anything else in the system.
2. **Hard declines are statements about the card; soft declines are statements about the moment.**
   Every retry decision follows from which one you have.
3. **Codes 91 and 96 are not declines and retry at 86–89% success.** Classifying them with real
   declines throws away the cheapest recovery available.
4. **Never retry 41, 43 or 59.** The networks monitor repeated attempts against lost, stolen and
   suspected-fraud cards, and it depresses your approval rate everywhere.
5. **Technical retry and dunning are different systems.** Seconds versus weeks, customer present
   versus absent, same idempotency key versus a new one each time. Building dunning on the
   technical retry path makes it silently return the original decline.
6. **For insufficient funds, what changes between attempts is the cardholder's balance**, so align
   the schedule to pay dates rather than to uniform intervals.
7. **Scale the schedule to the ticket size.** At $0.03 per attempt and a 31% recovery, dunning goes
   margin-negative below about a $12 ticket.
8. **Fix expired cards with an account updater rather than retrying them**, run ahead of the
   renewal rather than after the decline. Network tokens reduce that decline class by about 94%.
9. **Routing recovers real volume but can turn a dip into an outage.** Cap the rate of share
   change, require sustained recovery before restoring a route, and record which route was used.
10. **During an issuer outage, tell the customer the truth.** "Declined" sends them to try three
    more cards, which looks like card testing to your own fraud system.
11. **Watch approval rate decomposed by BIN, code, merchant and route.** The aggregate hides every
    actionable signal, and a shift in the decline-code mix usually means your own bug.

Next: [12-webhooks-and-the-outbox.md](12-webhooks-and-the-outbox.md), on getting events out of
your system and into the merchant's reliably.
