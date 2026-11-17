# Designing a Transaction Data Pipeline at a Card Issuer

A staff-level collection built around one interview question:

> "How would you design a data pipeline to process billions of transactions daily?"

It is the question a bank or card issuer asks, and the reason it gets asked is that analytics and
reporting at a bank are not a convenience. They are how fraud is stopped, how regulators are
answered, and how the books are closed. A broken dashboard at a social network is an annoyance. A
broken general-ledger feed at a bank is a material weakness in internal controls, which is a phrase
that appears in an annual report.

**The organising claim:** the word "transactions" in that question hides a factor of thirty, and
almost every weak answer comes from not taking it apart. At the issuer this collection designs
for, there are **99 million card authorizations a day and 3.06 billion events a day**. Both numbers
are real. Three percent of the volume is money, and it needs exactly-once semantics, double-entry
validation, and seven-year immutable retention. Ninety-seven percent is not money, and losing a
tenth of a percent of it harms nobody. Build one pipeline for both and you either make the
clickstream ruinously expensive or make the ledger unsafe.

So the answer is tiers, not a pipeline. Four properties drive nearly everything that follows:

- **Freshness requirements span four orders of magnitude** — 2 seconds for fraud features, no
  deadline at all for regulatory extracts. No single design serves both ends of that range well.
- **The compliance consumer has the weakest latency requirement and the strongest correctness
  requirement.** This is the opposite of what most designs assume, and it constrains the
  architecture more than the fraud path does.
- **The pipeline gets 8 milliseconds of the authorization's 150-millisecond budget**, and it
  spends them on a key-value read rather than a computation. The pipeline is therefore never in the
  request path.
- **The cheap failure is loud and immediate; the expensive one is silent.** Over five years at this
  issuer, eleven silent failures caused more damage than one hundred and ninety-four loud ones.
  Most of the controls in this collection exist to convert the second kind into the first.

## The running system

Every number in every document comes from one fictional company, **Monarch Card Services** — a US
consumer card issuer with a national bank charter, 62 million accounts, and 95 million cards. The
legacy system being replaced is called **Harbinger**: nightly `rsync` of authorization logs,
seventy Informatica workflows into Teradata, fraud features computed every 15 minutes, and
retention implemented as 23 cron-driven delete scripts, four of which have been failing silently
since a hostname change in 2023.

The numbers you will see repeatedly, all derived in
[01](01-the-running-system-and-its-numbers.md):

```
 99.4 M    authorizations per day  (1,150/s average, 6,400/s peak)
 3.06 B    total events per day    (2.1 B of them mobile clickstream)
  212 M    ledger postings per day (double entry)
 2.48 TB   raw bytes per day → 365 GB compressed at 6.8:1
  300 TB   lake at steady state    (933 TB without per-stream retention)
  150 ms   authorization budget; 8 ms of it for the data platform
 $41,600   monthly platform cost — 1.3% of the fraud loss it avoids
```

## Who this is for

Read this if two or more are true:

- You are preparing for a data or platform engineering interview at a bank, card issuer, payment
  processor, or fintech, where "design a pipeline for billions of transactions" is a standard
  question.
- You work on a system where some data is money and some is not, and the same pipeline currently
  treats both the same way.
- Something you build feeds a decision inside a request path with a latency budget in
  milliseconds.
- A regulator, auditor, or large counterparty can ask you to reproduce a figure from two years ago
  and explain how it was derived.
- You have a streaming job whose operational cost you suspect exceeds what its freshness is worth,
  and you want a way to decide rather than an argument.

**If your transaction volume is under roughly 5 million a day, most of this is describing problems
you do not have.** Five million rows a day is 1.8 billion a year, which fits comfortably in one
Postgres instance with partitioned tables and read replicas, serving dashboards, reporting and
compliance extracts from the same place. Read
[00](00-what-the-question-is-really-asking.md), the decision procedure in
[07](07-batch-versus-streaming-as-a-decision-procedure.md), and stop. Adding Kafka, Flink, a lake
and a warehouse to that situation does not buy capability — it buys four more systems that can
page you at 03:00.

## Start here, in this order

1. **[00-what-the-question-is-really-asking.md](00-what-the-question-is-really-asking.md)** — read
   first. Why the obvious "extract, transform, load, then dashboards and reports" answer scores
   poorly, the four consumers that are the real structure, and the four things the interviewer is
   actually testing.
2. **[01-the-running-system-and-its-numbers.md](01-the-running-system-and-its-numbers.md)** — the
   arithmetic, derived in visible steps. When a later document says "recall the 2.1 billion
   clickstream events," it means this page. Read it slowly once.

Then the real-time path:

3. **[02-the-ingestion-edge-and-the-card-data-boundary.md](02-the-ingestion-edge-and-the-card-data-boundary.md)**
   — the four genuinely different sources, why the authorization switch publishes asynchronously
   through a disk-backed ring buffer, and the single most consequential decision in the
   architecture: tokenizing the card number *before* the log rather than masking it after, which
   keeps the entire analytical estate out of PCI scope.
4. **[03-the-transport-log-topics-keys-and-ordering.md](03-the-transport-log-topics-keys-and-ordering.md)**
   — topic boundaries by ownership rather than cardinality, why the partition key is
   `account_id` (by card breaks ledger ordering; by merchant gives 96× skew), partition counts
   derived from consumer parallelism rather than throughput, and why lag is measured in seconds
   rather than messages.
5. **[04-streaming-transforms-and-the-feature-store.md](04-streaming-transforms-and-the-feature-store.md)**
   — the 8-millisecond constraint and everything that follows from it: bucketed counters instead of
   sliding windows (12.3 KB per card, not terabytes), per-consumer lateness policy, why the feature
   job uses at-least-once deliberately, and the training-serving skew problem that logging the
   feature vector solves by construction.

Then the analytical path:

6. **[05-the-lake-table-formats-and-the-small-file-problem.md](05-the-lake-table-formats-and-the-small-file-problem.md)**
   — why there is storage under the warehouse, what Iceberg provides that Parquet-in-folders does
   not, the small-file problem that is the most common operational failure in this architecture,
   and the three zones named for what they guarantee.
7. **[06-the-warehouse-the-dashboards-and-the-three-query-shapes.md](06-the-warehouse-the-dashboards-and-the-three-query-shapes.md)**
   — the three consumers differ by 100× in concurrency and 2,500× in rows per query, which is why
   three stores rather than one; fact-table grain; and why type 2 slowly changing dimensions are
   the only way a historical report stays stable.
8. **[07-batch-versus-streaming-as-a-decision-procedure.md](07-batch-versus-streaming-as-a-decision-procedure.md)**
   — the four questions, applied to all eleven of Monarch's consumers, with the cost of the same
   aggregate derived both ways ($19 against $440 a month).

Then the two things banks care about more than anything above:

9. **[08-correctness-duplicates-and-three-way-reconciliation.md](08-correctness-duplicates-and-three-way-reconciliation.md)**
   — the five sources of duplication, why no single ISO 8583 field identifies a transaction, how
   the ledger is made safe, and the three-way reconciliation against the network's clearing file,
   which is the only thing that proves the pipeline works.
10. **[09-compliance-reproducibility-retention-and-access.md](09-compliance-reproducibility-retention-and-access.md)**
    — the four mechanisms needed to reproduce a past decision, lineage as a traversal rather than a
    diagram, the genuine conflict between seven-year retention and deletion requests and how it
    actually resolves, and the three independent access layers.

Then operations:

11. **[10-failure-modes-and-the-operations-playbook.md](10-failure-modes-and-the-operations-playbook.md)**
    — thirteen failure modes with symptom, cause, detection, immediate response and prevention,
    plus what to do in the first ten minutes before you know what is wrong.
12. **[11-what-it-costs-and-what-breaks-at-ten-times-the-volume.md](11-what-it-costs-and-what-breaks-at-ten-times-the-volume.md)**
    — the bill component by component, where the money actually goes (storage is 4.4% of it), and
    the first wall at 10× volume, which turns out to be a partition count chosen on day one.

And finally:

13. **[12-the-interview-itself-forty-five-minutes.md](12-the-interview-itself-forty-five-minutes.md)**
    — the minute-by-minute allocation, the opening nearly verbatim, the whiteboard in three passes,
    the questions in four tiers with full spoken answers, signals and anti-signals, and twelve
    numbers worth memorising.

## If you have twenty minutes

Read [01](01-the-running-system-and-its-numbers.md) for the numbers, the four-question procedure in
[07](07-batch-versus-streaming-as-a-decision-procedure.md), and the opening script and memorisable
numbers in [12](12-the-interview-itself-forty-five-minutes.md). That is enough to open well and to
answer the batch-versus-streaming question with a procedure instead of an adjective, which are the
two highest-value things in the whole collection.

## Theses this collection argues, in one place

Each is developed somewhere above; collected here so they can be disagreed with.

1. **The log is the only system of record. Every store is a materialisation of it, is rebuildable
   from it, and is therefore disposable.** This single property answers most follow-up questions
   about bugs, new consumers, and stores that disagree.
2. **Tier by consequence, not by volume.** The expensive thing is never the number of records; it
   is the latency requirement and the correctness bar attached to them. The clickstream is 69% of
   events and 18% of cost.
3. **Scope reduction is the highest-return decision in payments engineering.** Tokenizing before
   the log costs one service and keeps the lake, warehouse, dashboard store, feature store, and
   every dead-letter topic out of PCI scope. It is only available at the start.
4. **The pipeline is never in a request path.** Pre-compute into a key-value store, so that a
   pipeline failure degrades decisions gradually rather than stopping the bank.
5. **Batch versus streaming is decided per consumer by the quantified cost of staleness**, not by
   the cost of the technology and not by taste. Four of eleven consumers stream, and all four are
   served by one streaming job.
6. **Exactly-once processing is a technique for making non-idempotent writes safe.** Where the
   write can be made idempotent instead — a feature upsert, a posting with a unique key — do that;
   it is cheaper and survives more failure modes.
7. **Internal consistency proves nothing, because a systematic bug is internally consistent.** Only
   comparison against a party who did not share your bug detects systematic loss.
8. **The signal is a change in the rate of an expected difference, not the existence of a
   difference.** This is what makes a reconciliation survive its first month.
9. **Retention is control-plane data enforced by a reconciler that exports drift, never a cron job
   that deletes.** Legal holds are why.
10. **Monitor the state of the world a job maintains, not the job's exit code.** A compactor can
    succeed and compact nothing; a Flink job can report healthy and write nothing.
11. **The batch tier scales by spending more money; the streaming tier scales by requiring a
    migration.** That asymmetry is a better argument for a small streaming tier than cost is.
12. **In a ledger you never fix a record — you add a reversing entry and a correct one.**
    Overwriting turns a correctable error into a control failure.
