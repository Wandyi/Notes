# Batch Versus Streaming, as a Decision Procedure Rather Than a Preference

The outline in the question handles this in four lines: batch is cheaper and easier, streaming is
expensive but critical for fraud, and showing you can tell when to use which demonstrates good
judgment.

That last part is right. This document is about actually demonstrating it, which means having a
procedure you can apply to a consumer the interviewer invents on the spot, rather than a pair of
adjectives.

---

## Why the usual framing does not survive a follow-up

"Batch is cheaper, streaming is for fraud" collapses as soon as the interviewer asks a second
question. Three of the obvious ones:

**"The compliance team wants to see suspicious-activity candidates as they happen. Streaming?"**
The adjectives say yes — it sounds urgent and it is about fraud-adjacent risk. The right answer is
no, and the reason is that a Suspicious Activity Report is filed within 30 calendar days of
detection and its determination requires reviewing an account's full history, including months of
prior behaviour. A nightly batch job is not a compromise here; it is a better fit, because the
computation genuinely needs a global view that streaming would have to reconstruct.

**"Your merchant reporting is streaming already since the data is in Kafka. Why not keep it
streaming and get fresher reports for free?"** Because it is not free — the derivation below puts
it at 23 times the cost — and because the consumer reads the output once a day at 06:00, so the
extra freshness is delivered to nobody.

**"Fraud is real-time, so is the fraud model trained in real-time?"** No. Model training is the
most batch-shaped workload in the system: it needs months of labelled history, labels that only
arrive 45 to 120 days later when chargebacks resolve, and a reproducible training set. Streaming
would be actively harmful.

The adjectives give the wrong answer to all three. So here is something that does not.

---

## The procedure: four questions

For any consumer, in order. The first two usually settle it.

### Question 1: What is the freshness SLO, stated in units of time?

Not "real-time." Not "as fresh as possible." A number, with a unit, that someone will agree to.

Forcing this question produces useful disagreement immediately. When Monarch asked the merchant
analytics team, the first answer was "ideally real-time." The second question was "what time do
you look at it?" The answer was "around nine in the morning, with coffee." The freshness SLO is
T+1 by 06:00.

If nobody can state a number, the consumer does not have a freshness requirement — it has a
preference, and preferences get batch.

### Question 2: What does one SLO period of staleness cost, in money or in a named consequence?

This is the question that does the work, and it must be answered with a quantity.

**Fraud features.** A card-testing run is 200–600 authorizations over 15–25 minutes. Monarch's
average loss per successfully tested-then-used card is $840. With 15-minute-stale features
(Harbinger's behaviour), detection happens after the run completes. With 2-second features, the
velocity counter crosses its threshold at roughly transaction 30 of 400.

```
attacks per month (measured)                       ~110
cards compromised per attack, average                38
loss per card if the test completes                $840
                                                  --------
monthly loss with 15-minute features  = 110 × 38 × 840            = $3,511,200
monthly loss with 2-second features   = 110 × 38 × 840 × (30/400) =   $263,340
difference                                                        = $3,247,860
```

**Streaming buys $3.2 million a month here.** No further analysis is required; nothing in the
streaming infrastructure costs a fraction of that.

**Merchant reporting.** What does it cost if the partner's file arrives at 06:00 containing data
through midnight, versus containing data through 06:00? The partner's analyst opens it at 09:00
and looks at yesterday. The answer is genuinely zero. Not "small" — zero. There is no decision
anyone makes differently.

**Finance close.** Six hours stale means the general ledger feed arrives at 10:00 instead of 04:00,
and the close process starts six hours late once a day. Named consequence, no dollar figure,
moderate annoyance. Not worth streaming; worth making the batch reliable.

**Regulatory extract.** Six hours stale costs nothing. Six hours *wrong* costs a finding. The cost
is entirely in the correctness dimension, which is question 4.

### Question 3: Is the computation expressible incrementally?

Some computations update cleanly as each event arrives. Others require seeing a whole population.

**Incremental:** counts, sums, minimums and maximums, approximate distinct counts, any windowed
aggregate, any per-key state machine. Each new event updates the answer without revisiting the
old ones.

**Not incremental, or only awkwardly:** exact percentiles across a population, ranking ("the top
500 merchants by decline rate"), anything requiring a join against data that arrives later,
anything needing a full-population sort, and training a model.

Monarch has a concrete example worth knowing. The risk team wanted "merchants whose decline rate
is in the top 1% today." As a streaming computation this requires maintaining a distribution over
410,000 merchants and recomputing a percentile threshold continuously — expensive, and the answer
is unstable early in the day when counts are tiny. As a batch job at 00:30 over the completed day
it is a single `PERCENTILE_CONT` and it is exact.

**If the computation is not incremental, streaming makes it harder and usually approximate.** That
is a reason to batch regardless of freshness, and it is the question most people skip.

### Question 4: What is the correctness bar, and can a streaming job meet it?

Three bars appear in this system:

- **Approximate is fine.** Dashboard counts, clickstream funnels, HyperLogLog distinct counts.
  Streaming is comfortable here.
- **Exact, but recomputable.** Merchant daily totals. If a number is wrong you fix the job and
  re-run the day; nobody has acted on it irreversibly. Either approach works, so this bar does not
  decide.
- **Exact, auditable, and reproducible as of a past date.** The ledger, the regulatory extracts,
  the general ledger feed. Here the requirement is not only that the number be right but that you
  can demonstrate *how* it was derived, years later, and get the same answer.

The third bar is where streaming becomes genuinely harder — not impossible, but harder in a way
that matters. A streaming aggregate's result depends on watermark behaviour, on which events had
arrived when, and on whether the job restarted mid-window. Reproducing it requires reproducing
the arrival timing, which you cannot. A batch job over a closed, immutable partition of the lake
is reproducible by construction: same input files, same code version, same answer.

**So the strongest correctness bar pushes toward batch, which is the opposite of the intuition
that important things need streaming.** This is the point document 00 made about the compliance
consumer, stated as a rule.

---

## Applying it to every Monarch consumer

| Consumer | 1. SLO | 2. Cost of staleness | 3. Incremental? | 4. Bar | Verdict |
|---|---|---|---|---|---|
| Fraud feature store | 2 s | $3.2 M/month | Yes | Approximate fine | **Streaming** |
| App "pending transactions" | 5 s | Support calls; ~14,000/month at $6 each | Yes | Approximate fine | **Streaming** |
| Analyst risk dashboard | 60 s | Misjudged attack response | Yes | Approximate fine | **Streaming** |
| Ledger postings | 5 min | Customer sees wrong available credit | Yes, but order-dependent | Exact + auditable | **Streaming, with exactly-once** |
| Merchant daily reporting | T+1 06:00 | Zero | Yes | Exact, recomputable | **Batch** |
| Partner co-brand file | T+1 06:00 | Contractual, but T+1 is the contract | Yes | Exact, recomputable | **Batch** |
| General ledger feed | T+1 04:00 | Close starts late | Yes | Exact + auditable | **Batch** |
| Top-1% merchant decline rate | T+1 | Zero | **No** | Exact | **Batch** |
| SAR candidate generation | Daily | None — 30-day filing window | **No** (needs full history) | Exact + auditable | **Batch** |
| Model training sets | Weekly | None | **No** | Reproducible | **Batch** |
| Regulatory extracts | None | Zero | No | Exact + reproducible | **Batch** |

Four streaming consumers, seven batch. And the four streaming consumers are all served by **one
streaming topology** — the one in document 04 — because they all need the same windowed aggregates
over the same keys. That is the shape to aim for: **not "a streaming platform," but one streaming
job that exists because a handful of consumers have a quantified cost of staleness, and
everything else in batch.**

---

## The cost difference, derived on one aggregate

Take `curated.merchant_daily` — counts and volumes by merchant, category, response code, and
card-present flag — and cost it both ways. The computation is identical; only the execution model
differs.

**As a batch job.** A Spark job reads one day of `conformed.fact_authorization` (20.5 GB
compressed) and aggregates it.

```
data read                          20.5 GB
Spark aggregation throughput       ≈ 35 GB per node-hour including shuffle
node-hours                         20.5 / 35 = 0.59
plus job startup and write          ≈ 0.4
total node-hours                   ≈ 1.0
r6g.2xlarge spot                   $0.155/hour
                                  ---------
cost per day                       $0.16
cost per month                     $4.80
```

Add EMR overhead and the cluster's fixed minute of startup, and call it **$19 a month**, generous.

**As a streaming job.** A Flink job maintains per-merchant, per-day counters continuously, emitting
to the curated table.

```
task managers (minimum for HA)      2 × r6g.xlarge   $0.2016/hr on-demand
job manager                         1 × r6g.large    $0.1008/hr
                                                    ----------
hourly                              2(0.2016) + 0.1008 = $0.5040
monthly (730 hours)                 $367.92
checkpoint storage and S3 requests  ≈ $14
monitoring, log retention           ≈ $22
                                                    ----------
monthly                             $403.92
```

Call it **$440 a month** with the state backend's EBS.

```
$440 / $19 = 23×
```

**Twenty-three times the cost, for an output read once a day at 06:00.**

Two notes on why the comparison is fair in the way that matters. On-demand pricing is used for
streaming and spot for batch deliberately: a stateful streaming job on spot instances restarts
whenever a node is reclaimed, and each restart means a state restore, so spot is not a realistic
option for the streaming version. And the batch job's cost scales with data volume while the
streaming job's scales with uptime — so at 10× the data the batch job costs $190 a month and the
streaming job still costs $440. **The gap narrows with volume, which means the argument for batch
is strongest exactly where people reach for streaming.**

---

## The costs of streaming that do not appear on the invoice

The $440 is the smaller half. The larger half:

**Someone is on call for it.** A batch job that fails at 02:00 is re-run at 08:00 and nobody was
woken. A streaming job that fails at 02:00 is accumulating lag, and whether that matters depends
on the SLO — which means someone must be paged to make that judgment. Monarch's streaming jobs
account for 61% of data-platform pages while being four of eleven consumers.

**Backfill is asymmetric.** Re-running a batch job for a day you got wrong is the same operation
as running it normally; the code does not know the difference. Backfilling a streaming job means
replaying history through a job designed for live traffic, which means event-time processing that
actually works, watermarks that behave under a 1,000× speed-up, and a sink that tolerates the
rewrite. Teams discover their streaming job cannot backfill at the exact moment they need to
backfill it.

**State is an operational object.** Six hundred and forty gigabytes of keyed state must be
checkpointed, restored, rescaled, and occasionally migrated across a schema change. Document 04
covered the mechanics; the point here is that none of it exists in the batch version.

**Debugging is harder in a specific way.** A batch job's input is a set of files you can read. A
streaming job's input is an interleaving that happened once. "Why did this aggregate come out
wrong at 14:32 last Tuesday" requires reconstructing arrival order, and often the honest answer is
that you cannot.

**Two implementations of the same logic.** Which is the next section.

---

## Lambda, Kappa, and the duplicate-logic problem

The classic failure in a system with both paths: the streaming job computes an approximate
`merchant_daily` for the dashboard, and the batch job computes the exact one for reporting. Two
implementations of one definition. They drift. Someone compares the dashboard to the report,
finds a 0.4% difference, and three engineers spend a week finding out which is wrong.

That design has a name — the **Lambda architecture**, a speed layer plus a batch layer, with
results reconciled at query time — and its reputation is bad for exactly this reason.

**Kappa** is the proposed alternative: delete the batch layer, do everything in streaming, and
handle reprocessing by replaying the log through the streaming job. It solves the duplicate-logic
problem and creates two others: everything now pays streaming's operational cost including the
seven consumers that did not need it, and the non-incremental computations (percentiles, rankings,
model training) do not have a streaming form.

Monarch does neither. The arrangement that works:

**One computation, one implementation, at one layer.** The dashboard does not read a streaming
approximation of `merchant_daily`; it reads `fact_authorization` in ClickHouse and aggregates at
query time. There is no streaming merchant-daily job to drift from the batch one, because there
is no streaming merchant-daily job.

**The streaming layer produces things the batch layer does not produce at all** — feature-store
values, and the ClickHouse facts. The batch layer produces things the streaming layer does not —
every curated aggregate. **No output is produced twice by two paths**, so there is nothing to
reconcile.

**Where an output genuinely must exist in both forms, generate both from one definition.** The
feature definitions in document 04 are the example: one declarative specification, from which both
the Flink job and the training-set query are generated. This is the only acceptable form of
duplication, and the mechanism is code generation, not documentation.

> The way to avoid the Lambda architecture's problem is not to pick one layer. It is to ensure no
> single number is computed by two different pieces of code.

---

## Micro-batch, and when it is the right answer

Between "every event as it arrives" and "once a night" there is a middle: run the batch job every
5 or 15 minutes.

Micro-batch gets you most of batch's advantages — the same code path as the nightly run, trivial
backfill, no long-lived state, spot instances, failures that retry rather than page — with
latency in minutes instead of hours.

The consumers it fits are the ones whose SLO is in minutes rather than seconds:

| Consumer | SLO | Micro-batch viable? |
|---|---|---|
| Fraud features | 2 s | No — a 5-minute batch is 150× too slow |
| Analyst dashboard | 60 s | Marginal — a 1-minute cycle means the job must finish in well under a minute |
| Lake writer, money streams | 15 min | **Yes, and this is what Monarch does** |
| Internal fraud-ops reports | 30 min | **Yes** |

The lake writers are worth noting, because people assume a Kafka-to-lake path must be streaming.
Monarch's money-stream lake writer is a Spark Structured Streaming job in **trigger-interval
mode**: it wakes every 15 minutes, reads the offsets accumulated since last time, writes an
Iceberg commit, and exits the micro-batch. It holds no keyed state, so a restart costs nothing
beyond the interval, and it produces appropriately-sized files by construction — which is the
small-file problem from document 05 solved by the execution model rather than by a compactor.

**When the SLO is in minutes, micro-batch is usually the right answer and it is rarely the one
proposed.** Being the person who proposes it is a good use of thirty seconds in an interview.

---

## What to take away

1. **"Batch is cheap, streaming is for fraud" fails on the second question.** Replace it with a
   four-question procedure you can apply to a consumer invented on the spot.
2. **Question 1: state the freshness SLO as a number with a unit.** If nobody can, the consumer has
   a preference rather than a requirement, and preferences get batch.
3. **Question 2: quantify the cost of one SLO period of staleness.** For fraud features it is $3.2
   million a month; for merchant reporting it is exactly zero, because the consumer opens the file
   at 09:00 and looks at yesterday.
4. **Question 3: ask whether the computation is incremental.** Percentiles, rankings, joins against
   later-arriving data, and model training are not, and streaming makes them harder and
   approximate regardless of freshness.
5. **Question 4: the strongest correctness bar pushes toward batch**, because a batch job over an
   immutable partition is reproducible by construction while a streaming result depends on arrival
   timing you cannot recreate.
6. **Four of Monarch's eleven consumers stream, and all four are served by one streaming
   topology.** Aim for one streaming job that exists for quantified reasons, not a streaming
   platform.
7. **The same aggregate costs $19 a month in batch and $440 in streaming — 23× — and the gap
   narrows as data grows**, so the batch argument is strongest at exactly the volumes where people
   reach for streaming.
8. **The invoice is the smaller cost.** Streaming brings on-call, asymmetric backfill, state as an
   operational object, and debugging that requires reconstructing an interleaving that happened
   once. Monarch's four streaming consumers generate 61% of its data-platform pages.
9. **Avoid Lambda not by choosing a layer but by ensuring no single number is computed by two
   pieces of code.** Where an output must exist in both forms, generate both from one declarative
   definition — code generation, not documentation.
10. **When the SLO is in minutes, micro-batch is usually right and rarely proposed.** Monarch's
    money-stream lake writer runs on a 15-minute trigger interval, which solves the small-file
    problem through the execution model instead of a compactor.
