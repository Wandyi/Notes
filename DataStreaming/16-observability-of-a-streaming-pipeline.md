# Observability of a Streaming Pipeline

A streaming platform's failures are quiet. A job dropping 0.3% of its records because a
watermark is slightly too tight looks identical, on every default metric, to a healthy one:
green, low lag, no errors, throughput matching input. That property is why this document exists
and why it is longer than "add Prometheus."

The methods for observability in general — RED, USE, SLIs, SLOs, error budgets, cardinality
control — are derived in [`../Observability`](../Observability/README.md) and are not repeated
here. This document applies them to a system whose failures are silent, and names **which metric
misleads during which failure**, which is the part that is specific to streaming.

## 1. The four properties to measure

Everything worth alerting on is an instance of one of four questions.

**Freshness — how old is the newest data a consumer can see?**
Not "what is the consumer lag" and not "what is the pipeline's processing latency." The question
is about what a user experiences at the far end, and the answer is the worst-case formula from
[13](13-serving-the-results.md) Section 2.

**Completeness — what fraction of the events that happened are reflected in the result?**
This is the one nobody measures and the one that defines silent degradation. Late-record drops,
skipped records, dead-lettered records, sampled records and failed enrichments all reduce it,
and none of them produce an error.

**Correctness — does the result equal what an exhaustive recomputation would produce?**
Only answerable by comparison, which means running the comparison.

**Progress — is the system advancing, and at what rate relative to the input?**
Lag, watermark lag, throughput, and the derivative that matters more than any of them: time to
drain.

## 2. The metrics that mislead, and when

This is the section to remember. Each of these metrics is the first one people look at, and each
is misleading during a specific, common failure.

### Consumer lag

**Misleads when:** the watermark is stalled. A job with a frozen watermark (`E-03`) consumes
records normally, so lag is near zero, while producing nothing and growing state indefinitely.
Lag says healthy; the job is dead.

**Also misleads when:** a Flink job commits offsets only for monitoring. Flink assigns
partitions itself and commits to `__consumer_offsets` on checkpoint; if checkpointing is
failing, the committed offsets stop advancing and the broker-side lag metric climbs even though
the job is processing fine. The lag is an artefact of the checkpoint failure, not of processing.

**Pair it with:** watermark lag, and `estimated_time_to_drain` rather than raw lag.

### Throughput (records/s out)

**Misleads when:** the job is filtering more than it should. A change that accidentally drops
90% of records shows as *lower* output throughput, which looks like a performance problem, and
people investigate capacity while the actual problem is a predicate.

**Also misleads when:** the job is keeping up perfectly at exactly the input rate while carrying
40 million records of permanent lag (`T-01`).

**Pair it with:** the input-to-output ratio, per operator, alerted on change rather than on
level. A filter that normally passes 9.7% and starts passing 0.9% is visible immediately in the
ratio and invisible in the throughput.

### Busyness

**Misleads when:** the bottleneck is a sink or an external call (`B-03`, `B-01`). The operator is
blocked, not busy, and busyness reads low while nothing is moving.

**Also misleads when:** the job is idle because it is input-starved (`B-04`), which reads
identically to "the job has plenty of capacity."

**Pair it with:** `backPressuredTime` and `idleTime`, and check that all three sum to ~1,000
ms/s.

### Checkpoint success rate

**Misleads when:** checkpoints are succeeding but taking 55 seconds out of every 60. The success
rate is 100% and the job is spending 92% of its time snapshotting.

**Pair it with:** `checkpoint_duration ÷ checkpoint_interval`, alerted above 0.25.

### CPU utilisation

**Misleads when:** RocksDB compaction is consuming the CPU (`K-06`). The node is at 90% and your
operators are getting 40% of it.

**Pair it with:** Flink's own busyness, which measures task-thread time rather than process time.

### "The job is RUNNING"

**Misleads when:** it is running and stalled, running and poisoned, running and dropping
everything. `RUNNING` is a statement about the scheduler, not about the work.

**Pair it with:** all of the above. This is the point of the whole document: **no single metric
distinguishes a healthy streaming job from a broken one, and the ones that look like they should
are the most misleading.**

## 3. The metrics that actually matter

Twelve, per job. If you have these and their alerts, you have covered most of this collection's
failure modes.

| # | Metric | Derivation | Alert |
|---|---|---|---|
| 1 | **Serving freshness** | `now() − max(window_start)` queried against the serving store | > 3× contracted p99 |
| 2 | **Watermark lag** | `now() − currentInputWatermark` | > 3× configured delay |
| 3 | **Watermark spread** | `max(watermark) − min(watermark)` across subtasks | > 60 s |
| 4 | **Time to drain** | `lag ÷ (throughput − input rate)` | negative, or > 30 min |
| 5 | **Late-record ratio** | `numLateRecordsDropped ÷ numRecordsIn` | warn 0.05%, page 0.5% |
| 6 | **Checkpoint duty cycle** | `checkpoint duration ÷ interval` | > 0.25 |
| 7 | **Checkpoint alignment time** | direct | > 5 s |
| 8 | **Restart rate** | `numRestarts` rate | > 3/hour |
| 9 | **Busy-time skew** | `max(busyTime) ÷ avg(busyTime)` across subtasks | > 2.5 |
| 10 | **State size growth** | derivative of `rocksdb_estimate_live_data_size` | positive over 7 days with no plateau |
| 11 | **Dead-letter rate** | records/s to the DLQ | > 0, or > baseline × 3 |
| 12 | **Input-to-output ratio** | `numRecordsOut ÷ numRecordsIn` per operator | change > 20% from the 7-day median |

Five of those twelve — 1, 3, 4, 5, 12 — are **derived** metrics that no system emits by default.
They are the ones that catch silent failure, and building them is the difference between an
observable platform and an instrumented one.

## 4. Measuring end-to-end latency honestly

The temptation is to compute it from metrics: producer timestamp minus sink timestamp, averaged.
This does not work, for a reason worth understanding.

Each stage's latency is measured on a different machine with a different clock. Summing them
gives a number that is off by the clock skew (`I-13`), which at 250 milliseconds across a fleet
is larger than the whole pipeline latency on the fast path. And percentiles cannot be summed
anyway ([01](01-requirements-and-the-capacity-model.md) Section 10).

**The honest measurement is a canary.**

```
1. A canary producer emits one record per partition per second, carrying
   its own send timestamp, into the real stream.
2. The record flows through the real pipeline with real records.
3. A canary consumer at the far end — reading the serving store, not the
   output topic — computes (observed_at − sent_at).
4. Both timestamps come from the same clock if the canary producer and
   consumer are the same process, which removes the skew problem entirely.
```

That fourth point is the trick that makes it work: **one process, one clock, both ends.**

The canary measures what a user experiences, including the serving store's contribution, which
no pipeline metric captures. On Meridian it runs per Tier 1 dataset at 1 record/s and it is the
source of every latency number published in a freshness contract.

**What it does not catch:** partial degradation. If 3% of real records are 40 seconds slow, the
canary — being 1 record per second, uniformly — has a 3% chance of being one of them, so its p99
will eventually show it but its p50 will not. For the distribution you need the real records,
measured with the same-clock caveat, which in practice means computing
`ingest_time − event_time` (both of which are on the record, from a trusted clock) for the
ingestion half, and accepting that the processing half is measured per-stage.

## 5. Cardinality: the trap that makes observability expensive

A streaming platform generates enormous metric cardinality by default:

```
Flink's per-subtask, per-operator metrics:
  180 jobs × ~9 operators × ~200 subtasks average × ~40 metrics
  = 12,960,000 time series
```

Thirteen million series is more than most monitoring systems will hold, and far more than anyone
will look at. Worse, the high-cardinality dimension — `subtask_index` — is the one you *need*
for `E-02` (one slow subtask) and `K-03` (one hot key), so you cannot simply drop it.

**Meridian's approach, which is a compromise and should be treated as one:**

| Tier | Retention | Dimensions kept |
|---|---|---|
| Aggregated (per job, per operator) | 400 days | job, operator, min/max/avg across subtasks |
| Per-subtask | **6 hours** | job, operator, subtask_index |
| Per-subtask, Tier 1 jobs | 14 days | job, operator, subtask_index |

The six-hour per-subtask retention is the key decision. **Subtask-level detail is for debugging
a live incident, not for trend analysis**, and six hours covers an incident. Keeping the min and
max across subtasks at full retention preserves the ability to see skew historically without
storing every subtask.

And the derived metrics from Section 3 are computed as **recording rules** at the aggregated
level, so the alerts do not depend on the short-retention tier.

## 6. Logs and traces in a streaming pipeline

**Logs.** A stream processor at 1.85 million records a second cannot log per record; that would
be 1.85 million log lines a second, which is 33% of a 10 TB/hr log platform's entire capacity
(see [`../LogCollector`](../LogCollector/README.md)) from one job. So:

- **Log per *operation*, not per record**: checkpoint completed, restart, rescale, schema
  change, watermark advanced past a boundary.
- **Sample per-record logging** where it is needed for debugging, at a rate set dynamically so
  it can be raised during an incident without a deploy.
- **Log the exceptional record in full**, always. A dead-lettered record's full contents at
  `WARN` is a few hundred a day and is exactly what you need.

**Traces.** Distributed tracing across a streaming pipeline is genuinely hard and partially
useful:

- A `trace_id` in the envelope lets you follow one record through the pipeline, which is
  valuable for debugging correctness.
- But an operator that aggregates 100,000 records into one output has 100,000 parent traces for
  one span, which no tracing system models well. The usual compromise is to carry a sample of
  parent IDs (say, 8) and count the rest.
- And sampling must be **head-based and consistent** — the decision made at the producer and
  carried in the envelope — or a traced record is dropped halfway through.

Meridian traces 1 in 10,000 records end to end, which is 185 traces/s from Corridor, and uses
them almost exclusively for "why did *this specific record* produce *that* result", which is a
question no metric answers.

## 7. The SLOs

Four, per Tier 1 dataset, with the reasoning for each target.

**Freshness: 99.9% of minutes have data no older than the contracted p99.**

Why not 99.99%: a single job restart is 9.8 minutes on the largest job
([01](01-requirements-and-the-capacity-model.md) Section 8). Three restarts a month is 29.4
minutes, which against 43,800 minutes is 99.93%. **Four nines of freshness would require never
restarting**, which is not achievable while deploying. Committing to it means either lying or
never shipping.

**Completeness: ≥ 99.9% of events reflected, measured as `1 − late_record_ratio − dlq_ratio`.**

Why it stops at three nines: from
[05](05-event-time-watermarks-and-lateness.md) Section 3's completeness curve, reaching 99.99%
requires a watermark delay of roughly 300 seconds on Corridor, which makes every dashboard five
minutes stale. **The fourth nine costs 150× the latency.** The correct design is three nines in
the streaming path plus a reconciliation path that reaches effectively 100% within a day, and
that is what the SLO should say.

**Correctness: the daily reconciliation against the lake differs by < 0.1%.**

Why a reconciliation rather than an assertion: correctness is not observable from inside the
system (`W-07`). This SLO is the only one that requires running a second computation, and it is
the only one that catches the silent-degradation class.

**Availability: 99.9% of produce requests accepted within 100 ms.**

Note this is about *ingest*, not about the whole pipeline. Ingest availability is what the tenant
controls nothing about and depends on completely; everything downstream is covered by freshness.

### The error budget conversation

Three nines of freshness is 43.8 minutes a month. That budget is spent on:

```
deploys                 3 × 9.8 min = 29.4 min    (67%)
unplanned restarts      1 × 9.8 min =  9.8 min    (22%)
remainder                             4.6 min    (11%)
```

**Deploys consume two thirds of the error budget**, which reframes every conversation about
deployment frequency and about recovery time. Halving recovery time — with task-local recovery,
say — doubles the number of deploys you can afford. That is a much more concrete argument for
investing in recovery speed than "reliability is important."

## 8. The single-glance dashboard

One screen per job, six panels, designed so that a healthy job looks obviously healthy.

```
┌─────────────────────────┬─────────────────────────┐
│ 1. FRESHNESS            │ 2. PROGRESS             │
│ serving-store age       │ input rate vs output     │
│ vs contracted p99       │ rate, time to drain      │
├─────────────────────────┼─────────────────────────┤
│ 3. TIME                 │ 4. COMPLETENESS         │
│ watermark lag           │ late ratio, DLQ rate,    │
│ watermark spread        │ enrichment skip rate     │
│ (min and max, not avg)  │                          │
├─────────────────────────┼─────────────────────────┤
│ 5. HEALTH               │ 6. CHANGE               │
│ checkpoint duty cycle,  │ annotations: deploys,    │
│ restart rate,           │ schema registrations,    │
│ busy-time skew          │ config and infra changes │
└─────────────────────────┴─────────────────────────┘
```

Panel 6 is not a metric and it is the most valuable panel. **Most incidents are caused by a
change, and the time from page to cause is dominated by finding out what changed.** Overlaying
deploys, schema registrations, producer deploys, config changes and infrastructure events on
every graph turns a twenty-minute investigation into a glance.

Panel 3 uses `min` and `max` across subtasks, never `avg`, because averaging is precisely how
`E-02` (one slow subtask) is hidden.

## What to take away

1. **Measure four properties: freshness, completeness, correctness, progress.** Completeness is
   the one nobody measures and the one that defines silent degradation.
2. **Consumer lag misleads when the watermark is stalled** — the job consumes normally,
   produces nothing, and reads as healthy.
3. **Throughput misleads when a predicate changed.** Watch the input-to-output ratio per
   operator, alerted on change rather than level.
4. **Checkpoint success rate misleads at 100%** if each checkpoint takes 55 of every 60 seconds.
   Alert on duty cycle.
5. **"RUNNING" is a statement about the scheduler, not about the work.** No single metric
   distinguishes a healthy streaming job from a broken one.
6. **Five of the twelve metrics that matter are derived** and no system emits them by default.
   Building them is the difference between observable and instrumented.
7. **Measure end-to-end latency with a canary whose producer and consumer share a process and
   therefore a clock**, and measure it against the serving store, not the output topic.
8. **Keep per-subtask metrics for six hours and aggregates for 400 days.** Subtask detail is for
   debugging a live incident; keeping min and max at full retention preserves skew visibility.
9. **Log per operation, not per record.** One job at 1.85 million records a second would be a
   third of a 10 TB/hr log platform.
10. **Freshness stops at three nines because deploys cost 9.8 minutes each.** Completeness stops
    at three nines because the fourth costs 150× the latency. State both reasons in the SLO
    document rather than picking round numbers.
11. **Deploys consume two thirds of the error budget**, which makes halving recovery time a
    concrete argument for doubling deployment frequency.
12. **The change-annotation panel is the most valuable one on the dashboard**, because the time
    from page to cause is dominated by finding out what changed.
