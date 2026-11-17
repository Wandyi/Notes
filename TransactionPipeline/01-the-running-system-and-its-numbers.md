# The Running System: Monarch Card Services, and Every Number Derived

Document 00 argued that "billions of transactions daily" is a phrase you have to take apart
before you can design anything. This document does the taking apart, on one specific fictional
company, and it derives every number the rest of the collection reuses.

Read it slowly once. After this, when document 07 says "recall that the clickstream is 2.1
billion events a day and the authorizations are 99 million," it means this page, and you should
be able to reconstruct the arithmetic rather than trusting it.

The reason to do this at all — rather than designing against "billions" as an abstraction — is
that *the design changes completely depending on which stream you are talking about*. A stream
of 99 million money-moving records a day and a stream of 2.1 billion button-click records a day
want opposite things from every component. If you only carry one number in your head, you will
give one answer, and it will be wrong for six of the seven streams.

---

## The company

**Monarch Card Services** is a US consumer card issuer with a national bank charter. It issues
credit cards under its own brand and co-brands with two retailers. It is an *issuer*, which
means:

- When a customer taps a card at a store, Monarch is the party that decides approve or decline.
- Monarch carries the credit risk — if the customer never pays, Monarch eats it.
- Monarch is a regulated depository institution, so it answers to the OCC, the CFPB, and FinCEN,
  and it files reports on a schedule it does not control.

It is deliberately *not* a payment service provider like Stripe. The distinction matters because
an issuer's hardest real-time problem is **deciding** (approve this $340 charge in under 150
milliseconds, correctly), while a PSP's hardest real-time problem is **routing**. The pipeline
you build is shaped by which of those it feeds.

**Scale of the customer base:**

- 62 million active accounts
- 95 million active cards (many accounts have an authorized user, so cards exceed accounts)
- 18 million monthly active users of the mobile app
- 410,000 merchants appear in any given month's transaction data — Monarch does not have a
  relationship with them, it just sees them

---

## Step 1: how many authorizations per day

An **authorization** is one request from a merchant's acquiring bank, through Visa or
Mastercard, asking Monarch "may this card be charged $47.12 at this merchant right now?", plus
Monarch's answer.

Start with the average rate and work out the day:

```
average authorization rate          1,150 per second
seconds in a day                   86,400
                                   ----------------------
authorizations per day             1,150 × 86,400 = 99,360,000
```

**So: 99.4 million authorizations per day.** Round it to 99 million in conversation and do not
apologise for the rounding.

Sanity-check that against the customer base, because an interviewer will:

```
99,360,000 authorizations / 95,000,000 cards = 1.05 authorizations per card per day
```

Roughly one transaction per card per day. For a general-purpose credit card portfolio that is a
believable figure — some cards are used for a daily coffee, many sit in a drawer for weeks. If
your arithmetic had produced 14 transactions per card per day you would know you had made an
error somewhere, which is the whole point of running the check.

**Approvals.** Monarch approves 93.7% of authorizations; the rest are declined for insufficient
credit, fraud rules, expired cards, or bad CVV.

```
approvals per day = 99,360,000 × 0.937 = 93,100,000
declines per day  = 99,360,000 × 0.063 =  6,260,000
```

The declines matter more than their share suggests, because every false decline is a customer
calling the contact centre and sometimes cutting up the card, and because the *reason* for each
decline is the data the fair-lending and model-governance reviewers will eventually ask for.
Never design the pipeline as if declines were waste records.

---

## Step 2: the peaks, which are what you actually size for

Averages size your storage bill. Peaks size your compute, your partitions, and your pager.

Monarch has three distinct peak shapes and they are not the same:

| When | Rate | Shape |
|---|---|---|
| Ordinary weekday evening, 18:30 ET | 2,900 auth/s | Broad, two-hour plateau — dinner and evening retail |
| Black Friday, 12:40 ET | 6,400 auth/s | Sharp, holds roughly 40 minutes |
| First of the month, 00:00–00:05 ET | 2,100 auth/s of a *different kind* | Recurring subscription charges, batch-submitted |

The ratio that matters:

```
peak / average = 6,400 / 1,150 = 5.6×
```

**Design every online component for 6,400 authorizations per second, not 1,150.** And note the
third row: the midnight subscription burst is a fleet of merchants submitting stored-credential
charges on a cron schedule. It is lower in rate but far more concentrated in *merchant* — a
single streaming service can be 40% of that burst. That is a hot-key problem, and document 03 is
mostly about it.

For the mobile app, the peak is driven by a different thing entirely — push notifications. When
Monarch sends a statement-ready push to 18 million users, app opens spike for roughly eight
minutes:

```
clickstream average  = 2,100,000,000 / 86,400 = 24,300 events/second
clickstream peak     = 68,000 events/second   (2.8× average)
```

So the system has two unrelated peaks that do not coincide, which is a small mercy and worth
saying out loud in an interview: it means you do not have to size one cluster for the sum.

---

## Step 3: where the "billions" comes from

Here is the arithmetic that answers the interview question's premise. Ninety-nine million
authorizations is not billions. But one authorization does not produce one record. It produces a
family of records, and separately there are streams that have nothing to do with
authorizations at all.

**What one approved authorization produces:**

| Record | Count per auth | Why it exists |
|---|---|---|
| Authorization event (request + response joined) | 1 | The transaction itself |
| Risk decision | 1 | Which model scored it, what score, what action |
| Rule hits | 4.2 average | Each fraud rule that fired, with its inputs |
| Ledger postings | 2 (approvals only) | Double-entry: memo-post the charge, reduce available credit |
| Notification event | 0.31 | Only if the customer has alerts on for this threshold |

And the independent streams:

| Stream | Volume per day | What it is |
|---|---|---|
| Mobile and web clickstream | 2.10 billion | Screen views, taps, scrolls, API calls from 18M app users |
| Core banking CDC | 41 million | Row-level changes from the account master database |
| Network clearing records | 94 million | What the networks say actually settled, arriving in files |

Now total the events:

```
authorization events                            99,360,000
risk decisions                                  99,360,000
rule hits        99,360,000 × 4.2            = 417,300,000
ledger postings  (see step 4)                  212,000,000
mobile/web clickstream                       2,100,000,000
core banking CDC                                41,000,000
network clearing records                        94,000,000
                                             --------------
total                                        3,063,020,000
```

**About 3.06 billion events per day.** The interviewer's "billions of transactions" is true, and
it is true because of clickstream and rule hits, not because of card transactions. Of the 3.06
billion, only 99 million — **3.2%** — are things a customer would call a transaction.

This is the single most useful sentence you can say in the first five minutes of this interview:

> Before I design anything: 97% of this volume is not money. The 3% that is money has a
> correctness bar the other 97% does not, and a latency bar the other 97% does not. If I build
> one pipeline for all of it, I will either make the clickstream ruinously expensive or make the
> ledger unsafe. So I am going to design tiers.

---

## Step 4: why 212 million ledger postings

This number is worth deriving separately because candidates routinely get it wrong, and because
it introduces double-entry bookkeeping, which document 08 depends on.

A ledger posting is one line in one account. **Double-entry** means every economic event
produces at least two postings whose amounts sum to zero — something is debited, something is
credited. If they do not sum to zero, the event is rejected. That constraint is the only reason
you can trust a ledger at all.

Monarch's postings come from:

```
approved authorizations     93,100,000 × 2 postings  = 186,200,000
clearing of those auths     (next day, 1 adjustment posting pair on 11% that
                            changed amount)  10,200,000 × 2  =  20,400,000
refunds and reversals       2,140,000 × 2             =   4,280,000
fees and interest accrual   (daily batch, per account) 620,000 × 2 = 1,240,000
                                                       ----------------
total postings per day                                   212,120,000
```

Round to **212 million postings per day**. Note the composition: the daily fee and interest
accrual is tiny in row count but it is the one that runs as a batch job at 02:00 and must either
complete entirely or not at all, which makes it the most operationally annoying of the four.

---

## Step 5: bytes, which decide your storage design

Event counts decide your partition counts; bytes decide your storage bill and your scan costs.
Average serialized record sizes, measured on real payloads rather than guessed:

| Stream | Events/day | Avg bytes | Raw GB/day |
|---|---:|---:|---:|
| `auth.events` | 99.4 M | 1,400 | 139.1 |
| `risk.decisions` | 99.4 M | 900 | 89.4 |
| `risk.rule_hits` | 417.3 M | 300 | 125.2 |
| `ledger.postings` | 212.1 M | 600 | 127.2 |
| `app.clickstream` | 2,100 M | 900 | 1,890.0 |
| `core.cdc` | 41.0 M | 1,100 | 45.1 |
| `network.clearing` | 94.0 M | 700 | 65.8 |
| **Total** | **3,063 M** | | **2,481.8** |

One worked example so the arithmetic is visible:

```
app.clickstream = 2,100,000,000 events × 900 bytes
                = 1,890,000,000,000 bytes
                = 1,890 GB  (decimal GB, 10⁹ bytes)
                = 1.89 TB per day
```

**Raw total: 2.48 TB per day.** Again, the clickstream is 76% of the bytes. Again, it is the
least valuable 76%.

Why `auth.events` is 1,400 bytes when the useful content is maybe 200: an ISO 8583 authorization
message carries about 60 fields, most of which you must retain because a dispute two years from
now will turn on terminal entry mode or the merchant category code. You do not get to trim it.

---

## Step 6: compression and what the lake actually costs

Stored as Parquet with ZSTD compression and columns sorted sensibly, these shapes compress well
— they are full of repeated merchant IDs, repeated response codes, and timestamps that are
nearly sorted. Measured ratio across the seven streams: **6.8 to 1**.

```
compressed = 2,481.8 GB / 6.8 = 365 GB per day
```

Now the part most candidates skip, which is where the real money is. **Retention is not one
number.** Each stream has a different legally or operationally mandated life:

| Stream | Retention | Why that number | Compressed GB/day | Days | Total TB |
|---|---|---|---:|---:|---:|
| `auth.events` | 7 years | Bank Secrecy Act recordkeeping | 20.5 | 2,557 | 52.4 |
| `risk.decisions` | 7 years | Model governance, fair-lending evidence | 13.1 | 2,557 | 33.5 |
| `risk.rule_hits` | 25 months | Operational tuning only; no mandate | 18.4 | 761 | 14.0 |
| `ledger.postings` | 7 years | SOX, and it is the books | 18.7 | 2,557 | 47.8 |
| `app.clickstream` | 13 months | Product analytics year-over-year | 277.9 | 396 | 110.0 |
| `core.cdc` | 7 years | Reconstruct account state as of any date | 6.6 | 2,557 | 16.9 |
| `network.clearing` | 7 years | The counterparty's version of the truth | 9.7 | 2,557 | 24.8 |
| **Total** | | | **365.0** | | **299.4** |

**Steady-state lake size: about 300 TB.**

Compare that to the number you would have got by applying seven years uniformly:

```
365 GB/day × 2,557 days = 933,305 GB = 933 TB
```

**Per-stream retention cuts the lake from 933 TB to 300 TB — a 3.1× reduction — and it is a
configuration decision, not an engineering project.** That is the cheapest win available in this
whole design, and almost nobody mentions it unprompted.

What 300 TB costs in S3, using published list prices:

```
Standard storage      $0.023 per GB-month
Glacier Instant       $0.004 per GB-month

Hot window (90 days of everything, queried often):
  365 GB/day × 90 = 32,850 GB × $0.023 = $756/month

Cold remainder (300,000 − 32,850 = 267,150 GB):
  267,150 GB × $0.004 = $1,069/month

Total storage                            ≈ $1,825/month
```

**Under $22,000 a year to store everything the bank knows about every transaction for seven
years.** Storage is not the cost of this system. Compute is, and the fact that storage is nearly
free is exactly why engineers let compute get expensive — they keep all of it hot. Document 11
does the full cost model.

---

## Step 7: the freshness contracts

This is the table that drives every batch-versus-streaming decision in document 07. Each
consumer gets a **freshness SLO**: the maximum acceptable age of the data it reads, measured
from when the event happened in the world to when that consumer can see it.

| Consumer | Freshness SLO | What breaks if it slips | Measured how |
|---|---|---|---|
| Fraud decisioning, inside the auth path | **2 seconds** p99 | Fraudster runs 40 more charges on a stolen card before the velocity counter catches up | Time from auth event timestamp to counter visible in the feature store |
| Analyst risk dashboard | **60 seconds** | Analyst watching an attack in progress is working from stale data and misjudges whether a block worked | Query-time max(event_time) lag |
| Customer-facing app, pending transactions | **5 seconds** | Customer taps a card, opens the app, does not see the charge, calls support | Same as fraud, different read path |
| Merchant and co-brand partner reporting | **T+1, 06:00 ET** | Partner's own morning meeting has no numbers; contractual | Did the 06:00 file land, with the full prior day |
| Finance close and the general ledger feed | **T+1, 04:00 ET** | Finance cannot close the day | Completeness check passes |
| Regulatory extracts (BSA, CCAR, dispute evidence) | **No latency SLO at all** | Nothing — but if it is *wrong* or not reproducible, that is a regulatory finding | Reproducibility: same query, same as-of date, same answer |

Look at the spread: **2 seconds to "whenever, but it must be provably correct."** Four orders of
magnitude. No single pipeline design serves both ends of that table well, and pretending
otherwise is the most common way this interview goes wrong.

Note especially the last row. The regulatory consumer has the *weakest* latency requirement and
the *strongest* correctness requirement. Candidates who equate "important" with "real-time" put
their most important consumer in the wrong tier.

---

## Step 8: the authorization latency budget

One more number, because it is the one that makes the real-time tier a hard problem rather than
a preference.

Visa and Mastercard enforce a response-time requirement on issuers. If Monarch does not answer in
time, the network **stands in** — it answers on Monarch's behalf using rules Monarch configured
months ago, with no view of the customer's current balance or the fraud signals of the last five
minutes. Stand-in approvals are how issuers lose money in bulk.

Monarch's internal budget for the whole decision, p99.9:

```
total budget                                        150 ms
  network ingress, ISO 8583 parse, validate          18 ms
  account state lookup (balance, status, limits)     22 ms
  feature store read (velocity counters, history)      8 ms
  fraud model inference                               31 ms
  rules engine evaluation                             24 ms
  decision assembly, ledger memo-post enqueue         19 ms
  response serialize and egress                      14 ms
                                                    -------
  sum                                                136 ms
  headroom                                            14 ms
```

**The pipeline gets 8 milliseconds of that 150.** Not 8 milliseconds to compute a feature —
8 milliseconds to *read* a feature someone already computed. This is the single most important
structural fact in the whole design, and document 04 is built on it:

> The fraud model does not query your streaming pipeline. It queries a key-value store that your
> streaming pipeline wrote to, seconds earlier, asynchronously, off the critical path. The
> pipeline is never in the request path. If it were, a pipeline outage would be a card-network
> outage.

---

## The legacy system being replaced

Every real version of this question is a migration, not a greenfield build, and saying so earns
you credit. Monarch's existing arrangement, called **Harbinger**, is:

- Authorization logs written to local disk on each of 340 auth servers, shipped by a nightly
  `rsync` to a landing zone
- A set of 70-odd Informatica workflows that load those files into a Teradata warehouse between
  01:00 and 07:00
- Fraud "real-time" features computed by a job that runs every 15 minutes against Teradata
- Merchant reporting generated by stored procedures, emailed as CSV
- Retention implemented as 23 separate cron-driven delete scripts, four of which have been
  failing silently since a hostname change in 2023

Three properties of Harbinger will come up repeatedly:

1. **Its fraud features are 15 minutes to 8 hours stale**, depending on where in the cycle the
   authorization lands. A card-testing attack — where a fraudster validates stolen card numbers
   with hundreds of tiny charges — runs to completion inside one Harbinger cycle.
2. **It has no replay.** If a workflow drops records, the source files are gone after 14 days,
   so the warehouse is simply wrong and nobody can prove by how much.
3. **It cannot answer "what did the system know at 14:00 on 3 March 2024?"** Every table is
   current-state-overwritten. When a regulator asks why a particular application was declined,
   the answer is reconstructed by hand from logs, over three weeks.

Property 3 is the one that costs the most and the one engineers notice last. Document 09 is
largely about not reproducing it.

---

## What to take away

1. **99.4 million card authorizations a day** — derived as 1,150/second × 86,400 seconds, and
   sanity-checked at 1.05 transactions per card per day.
2. **6,400 authorizations per second at peak**, 5.6× the average. Size online components for the
   peak; size storage for the average.
3. **3.06 billion events a day in total**, of which only 3.2% are money. The "billions" in the
   question comes from a 2.1-billion-event clickstream and 417 million fraud rule-hit rows.
4. **212 million ledger postings a day**, because double-entry means every economic event
   produces at least two balanced lines.
5. **2.48 TB of raw bytes a day, 365 GB after compression at 6.8:1.** The clickstream is 76% of
   the bytes and the least valuable part of them.
6. **300 TB of lake, not 933 TB**, because retention is set per stream — 13 months for
   clickstream, 7 years for anything a regulator can ask about. That configuration decision is a
   3.1× saving.
7. **Storage costs about $1,825 a month.** Storage is never the reason this system is expensive.
8. **Freshness requirements span 2 seconds to "no deadline, but provably correct."** That spread
   is why the answer is tiers rather than a pipeline.
9. **The pipeline gets 8 milliseconds of the 150-millisecond authorization budget, and it spends
   them on a key-value read, not a computation.** The pipeline is never in the request path.
10. **The real task is replacing Harbinger**, whose three defects — stale features, no replay, no
    as-of-date reconstruction — are the three things the new design has to be judged against.
