# Reprocessing, Backfill, and the Lambda Question

Scenarios `R-01` … `R-10`.

Every streaming job will eventually be wrong, and the question that decides whether that is an
afternoon or a quarter is: **can you run it again over history?**

This document is about replay — how it works, what it costs, what it breaks — and about the
architectural argument that has run for a decade over whether you need two code paths (lambda)
or one (kappa). That argument has a real answer, and it is more nuanced than either camp
usually admits.

## 1. Five reasons to reprocess, and they need different answers

Lumping these together is why reprocessing discussions go badly.

**1. A bug.** The job computed the wrong thing for three days. You need to recompute those three
days and replace the output.

**2. A new consumer.** A job is added that needs history — 30 days of sessions to bootstrap a
model. The existing output is fine; you need a new output over old input.

**3. A schema or logic change.** The definition of "an active user" changed. All historical
outputs must be recomputed under the new definition, or the time series has a discontinuity.

**4. Lost state.** A job's state was corrupted or discarded and must be rebuilt from the log.

**5. Late-arriving data.** A correction for events that arrived after their window closed
(`E-06`). Small, routine, and covered by the reconciliation path.

These differ in three dimensions that determine the design:

| | How far back | Output disposition | Can it run offline? |
|---|---|---|---|
| Bug | Days | Replace a range | Yes |
| New consumer | Weeks–months | New output | Yes |
| Logic change | Months–years | Replace everything | Yes |
| Lost state | Hours–days (the window depth) | Rebuild state, output unchanged | **No** — the live job needs it |
| Late data | Minutes–hours | Correct a range | Yes, batched |

The "lost state" row is the constraining one: it must complete quickly, while the live job is
down, which sets the **retention requirement** from
[04](04-the-transport-log.md) Section 3. The others can be slow.

## 2. The replay arithmetic

Replaying Corridor's 7 days:

```
events:       1,850,000/s × 604,800 s = 1,118,880,000,000   (1.12 trillion)
bytes:        640 B each               = 716 TB uncompressed
              ÷ 3.2 compression        = 224 TB to read from storage

at 5× real-time replay (9.25M events/s):
  duration = 604,800 ÷ 5 = 120,960 s = 33.6 hours

resources needed at 5×:
  from doc 01 §7, this job is a keyed join at 4,500 events/s per core
  9,250,000 ÷ 4,500 = 2,056 cores at 100%
  ÷ 0.60 utilisation = 3,427 cores = 215 nodes
```

Two hundred and fifteen nodes for a day and a half, against a steady-state cluster of 96. **A
replay is a second cluster.** That is the number to put in front of whoever asked, and it
reframes "can we just reprocess?" immediately.

Lower replay rates are cheaper in machines and longer in wall clock:

| Replay rate | Wall clock for 7 days | Nodes needed | Node-hours |
|---|---|---|---|
| 1× | 7.0 days | 43 | 7,224 |
| 2× | 3.5 days | 86 | 7,224 |
| 5× | 33.6 h | 215 | 7,224 |
| 10× | 16.8 h | 430 | 7,224 |
| 20× | 8.4 h | 860 | 7,224 |

The node-hours are constant, which is the useful insight: **the compute cost of a replay does
not depend on how fast you run it.** What does depend on the rate is:

- Whether the *source* can deliver it (Section 3).
- Whether the *sink* can absorb it.
- Whether the live system survives sharing the infrastructure (`R-03`).
- Whether you can get 860 nodes at all.

So choose the rate by what the surrounding system tolerates, not by what is cheapest, because
they cost the same.

## 3. Where history comes from

Three sources, three different replay characteristics.

**The log's local retention (6 h).** Full speed, page-cache warm for 24 minutes. Only useful for
the "lost state" case.

**The log's tiered storage (7 d).** Roughly 40% of local speed
([04](04-the-transport-log.md) Section 5), subject to S3 request throttling (`T-08`). Replay at
5× from the tier means reading `224 TB ÷ 120,960 s = 1.85 GB/s` from S3, which is achievable but
needs the prefix spreading from `T-08`.

**The lake (400 d).** Parquet in Iceberg, read as a bounded stream. Fast to read — columnar and
projectable — and **it is not the same data**: the lake holds the *processed* records, after
whatever the ingestion pipeline did to them. If the bug was in the ingestion pipeline, the lake
has the bug baked in.

That last point is the single most important thing about designing for replay:

**Land the raw records in the lake before any transformation**, in addition to whatever
processed form you keep. Meridian writes `corridor.tracking.events` to Iceberg directly from the
transport tier, untransformed, and every derived dataset is recomputable from it. The cost:

```
138.3 TB/day uncompressed
  → Parquet with dictionary encoding and zstd, measured 7.4:1
  = 18.7 TB/day = 7.48 PB over 400 days
  × $0.023/GB/month for S3 Standard-IA ≈ but with lifecycle to Glacier IR
    after 90 days: ~$1.9M/year at Standard, ~$640,000/year tiered
```

That is a large number and it is worth challenging. Meridian's actual policy:

| Age | Storage class | Contents |
|---|---|---|
| 0–7 d | Kafka tiered | Raw, replayable as a stream |
| 0–90 d | S3 Standard-IA, Iceberg | Raw, all streams |
| 90–400 d | S3 Glacier Instant Retrieval | Raw, Tier 1 streams only |
| 400 d+ | Deleted, except compliance-retained aggregates | |

which brings it to roughly $340,000 a year — still significant, and it is the price of being
able to answer "can we recompute that?" with yes.

## 4. The lambda / kappa argument, settled

**Lambda architecture:** two paths. A streaming path for fresh, approximate results and a batch
path for correct, complete ones. Queries merge them.

**Kappa architecture:** one path. Everything is a stream; reprocessing is replaying the same
streaming job over history.

### The case for kappa

One implementation means one set of semantics. Every divergence in `W-07` — window boundaries,
time zones, deduplication, lateness handling — exists because two implementations of the same
logic were written by different people at different times. One codebase makes those impossible
by construction.

And modern engines genuinely support it: Flink runs the same job in batch mode over a bounded
source, with the same operators and the same semantics. This is not aspiration; it works.

### Where kappa breaks

**Replay rate is bounded by state.** Replaying at 20× means event time advances 20× faster than
wall clock, so a 24-hour window fills in 72 minutes — but it still holds 24 hours of data. State
is unchanged by replay speed, and it must fit. For the attribution job that is 4.2 TB regardless
of how fast you replay.

Worse, **during an unaligned replay the state is larger than in the live job**, because the
spread between the fastest and slowest source is much wider (`E-07`). Without watermark
alignment, a replay can need many times the live job's state.

**Replay rate is bounded by the sink.** Writing 33 hours of results in 33 hours is fine; writing
7 days of results in 8 hours means the sink takes 21× its normal write rate. ClickHouse's parts
problem (`B-03`) appears immediately.

**Batch engines are genuinely better at batch.** Spark over 224 TB of Parquet with a shuffle and
a sort will beat Flink's streaming operators over the same data, often by 3–5×, because the
algorithms are different: a sort-merge join over bounded data beats a hash join maintaining
state over an unbounded one. Flink's batch execution mode narrows this and does not close it.

**Some computations are not streamable at all.** A global median over 400 days, a model training
run, an all-pairs similarity. These are batch computations and dressing them as streams helps
nobody.

### What Meridian actually does

**Kappa for the semantics, lambda for the execution.** Concretely:

1. **One definition of the logic**, expressed once — in Flink SQL where possible, in a shared
   library of operators where not.
2. **Run it as a stream for the live path**, at 60-second checkpoints, against Kafka.
3. **Run the same job in Flink's batch execution mode** against the Iceberg table for backfills
   under about 30 days.
4. **Fall back to a separate Spark implementation only for the two computations that genuinely
   need it** — the 400-day model training set and the quarterly all-member similarity — and
   accept that those two have two implementations, documented as such.

180 jobs, 2 with a second implementation. **The kappa argument wins for 99% of the work and
loses for the 1% where it should.** Claiming it wins for 100% is what makes people distrust it.

## 5. Scenarios

### R-01 — The replay saturated the live cluster

**What you see.** A backfill was started. Live jobs' lag grows. Checkpoints slow. Two jobs
restart.

**Mechanism.** The replay shares brokers, TaskManagers, S3 and network with the live path. At 5×
it is reading 1.85 GB/s from the brokers — three times the entire platform's steady ingest rate
— which consumes broker network and page cache. Page-cache residency
([01](01-requirements-and-the-capacity-model.md) Section 6) collapses because the replay's reads
evict the live data.

That last effect is the subtle one and it is the worst. **A replay reading old data evicts
recent data from the page cache, which makes every live consumer read from disk.** The replay's
cost is not paid by the replay; it is paid by everyone else.

**Prevent.**

- **Replay on a separate cluster.** A dedicated Kafka cluster fed by MirrorMaker, or replay
  directly from the lake rather than from Kafka, which is Meridian's default and avoids the
  broker entirely.
- **Rate-limit replays centrally** (`T-08`), with a token that must be acquired. Two large
  replays at once is a platform incident.
- **Separate node pool** for backfill jobs.
- **Run them at night**, which is a real and underrated mitigation when the diurnal ratio is
  1.75×.

### R-02 — The replay produced different results from the live run

**What you see.** A backfill over a period that was also processed live produces different
numbers.

**Mechanism.** The causes, in order of frequency:

1. **A processing-time dependency** (`E-10`) — a wall-clock call, a processing-time timer, or
   a state TTL using processing time.
2. **A processing-time lookup** where a temporal join was needed
   ([10](10-joins-and-enrichment.md) Section 3). The enrichment data has changed since.
3. **Different watermark behaviour.** During replay, records arrive in a burst, so the
   out-of-orderness is different and different records are late.
4. **A different partition assignment**, which changes floating-point summation order (`W-09`).
5. **The live run dropped late records; the replay includes them.** The replay is *more*
   correct, and the difference is the late-record ratio.

Cause 5 is worth calling out because it is a *good* result being mistaken for a bug. A replay
that includes data the live run missed is the whole point of replaying.

**Confirm it.** Run the replay twice over the same range. If the two replays agree with each
other and disagree with the live run, causes 3 or 5. If the two replays disagree with each
other, causes 1, 2 or 4.

**That two-replay test is the fastest diagnostic here** and it takes only the second replay.

**Prevent.** The determinism CI test from `E-10`: run the job twice over a fixed offset range
and compare byte for byte.

### R-03 — The replay is too slow to matter

**What you see.** A bug is found on Thursday affecting data since Monday. The replay will take
five days.

**Mechanism.** The replay rate was set by whatever the job happened to do at its live
parallelism, which is 1× by construction — a job provisioned for its input rate replays at
roughly 1.67× (the surplus at 60% busyness).

**Prevent.** **Design the replay path before you need it**, and know its rate. For each Tier 1
job, Meridian records:

```
job: corridor-ad-attribution
  live parallelism:        360
  replay parallelism:      1,080  (bounded by 480 source partitions
                                   → rebalance() after the source)
  measured replay rate:    8.4×
  state at replay:         4.2 TB (unchanged)
  7-day replay duration:   20.0 hours
  nodes required:          212
  cost per replay:         $3,180
```

Having that table means the Thursday conversation is "twenty hours and $3,180" rather than a
day of investigation followed by bad news.

### R-04 — The replay ran out of retention halfway

**What you see.** A replay from 7 days ago reaches day 6 and the source starts returning
`OffsetOutOfRange`.

**Mechanism.** Retention is measured from *now*, and now keeps moving. A replay that takes 33
hours and starts at the retention boundary will run past it.

**Prevent.**

- **Start the replay at least `replay duration` inside the retention boundary**, not at it.
- **Or extend retention before starting.** A one-command change that costs disk for a few days.
- **Or replay from the lake**, which does not move.

### R-05 — The backfill's output collided with the live output

**What you see.** A backfill and the live job both write to `corridor.attributions`. Downstream
consumers see interleaved results from two different logic versions.

**Prevent.** **Backfills write to a separate output, always**, and the switch-over is explicit:

```
live:    corridor.attributions
backfill: corridor.attributions.backfill.2026-09-30
```

Then one of three cut-over patterns:

**Replace a range.** Delete the affected range from the sink and insert the backfill's output.
Requires a sink that supports ranged delete — a lakehouse table does, an append-only topic does
not.

**Version the dataset.** Downstream reads a view that points at either version. Switch the view
atomically when the backfill is verified. This is the safest and it is what Meridian does for
Tier 1 datasets.

**Merge by key.** If the output is keyed and the sink upserts, the backfill simply overwrites
the affected keys. Works when the key space of the backfill exactly matches what needs
replacing, and silently leaves stale rows when it does not — for instance, if the bug caused
rows to be *emitted that should not have been*, overwriting will not remove them.

That last failure is common enough to name: **an upsert backfill cannot delete.** If the
correction involves removing rows, you need the ranged delete or the versioned dataset.

### R-06 — Parallel run and cut-over

**What you see.** A job's logic is changing and it holds 24 hours of state, so the new version
must build state before it can produce correct output.

**Mechanism.** Covered in `C-08`. The pattern:

```
T+0:   start v2 alongside v1, same source, different consumer group,
       output to a shadow topic
T+24h: v2's state is warm. Compare v1 and v2 outputs for a period.
       Expect a difference — that is the change. Verify it is the
       intended difference.
T+25h: switch the downstream view to v2's output
T+32h: stop v1
```

Cost: double resources for 25 hours, plus the shadow topic.

**The comparison step at T+24h is the whole value of the pattern**, and it is the step people
skip because the new version "obviously works." Meridian requires a diff report before the
switch: how many rows differ, by how much, and a sample of differing keys with an explanation.

### R-07 — Reprocessing that cannot be idempotent

**What you see.** A job sends notifications. Reprocessing sends them again.

**Mechanism.** External side effects cannot be replayed. This is the `D-`-series limit: when the
effect leaves your system, replay means doing it again.

**Prevent.** **Separate the computation from the effect.** The job computes "these users should
be notified" into a stream or table; a separate, stateful notification service reads it and
maintains "have I sent this?" That service is not replayed; it is the boundary.

This is the same architectural move as the outbox (`I-12`) and as `D-06`'s "derive one sink from
the other." The general form: **push side effects to the edge and make everything upstream of
them a pure function of the input.** A pipeline built this way is replayable; one with effects
in the middle is not.

### R-08 — A replay that changes history

**What you see.** A backfill corrects a metric for last quarter. A report published to
regulators used the old value.

**Mechanism.** The data was authoritative at the time it was used, and now it is different.

**Prevent.** For anything that has been published, **corrections are additive, not
destructive**:

- Keep the original values with their `as_of` timestamp.
- Publish the correction as a new version with its own `as_of`.
- Queries specify which vintage they want; the default is "latest."

This is bitemporality, and it is more machinery than most datasets need. The rule for deciding:
**if a number has ever been published outside the team, it needs a vintage.** Internal
dashboards do not.

### R-09 — The lake and the stream disagree about what happened

**What you see.** The lake has 41 more records for an hour than the stream's consumer processed.

**Mechanism.** They were written by different consumers with different failure histories. The
lake sink was up during a window when the streaming consumer was down, or vice versa.

**Prevent.** **The lake's landing job is the reference**, and its completeness is measured
independently (`I-13`'s techniques). Every other consumer's completeness is measured *against
the lake*, not against the source. This gives you one number to trust and makes the others'
divergence measurable.

### R-10 — The backfill that should have been a batch job

**What you see.** A 400-day backfill is being planned as a Flink replay. It will take three
weeks.

**Mechanism.** The kappa argument taken past its limits (Section 4).

**Prevent.** The decision rule Meridian uses:

| Range | Approach |
|---|---|
| < 6 h | Replay from the Kafka log, live cluster, at surplus rate |
| 6 h – 7 d | Replay from Kafka tiered storage, separate node pool |
| 7 d – 30 d | Same job, Flink batch execution mode, over Iceberg |
| > 30 d | Consider a Spark implementation. Measure first — Flink batch may still win |

The "measure first" in the last row is not a hedge. Flink's batch mode over Iceberg has closed
much of the gap, and a second implementation costs a permanent divergence risk. Reach for it
when a measurement says to, not when the range crosses a number.

## 6. Designing a job to be replayable

Seven properties. A job with all seven can be replayed on a Thursday afternoon; a job missing
any one of them turns a bug into a project.

1. **No wall-clock dependency.** No `System.currentTimeMillis()`, no processing-time timers, no
   processing-time state TTL (`E-10`).
2. **No processing-time lookups against mutable data.** Temporal joins over versioned history
   ([10](10-joins-and-enrichment.md) Section 3).
3. **No side effects.** Effects at the edge, behind a stateful boundary (`R-07`).
4. **Deterministic keys.** No random salt that is re-randomised; if you need salting, derive it
   from the record.
5. **Output is absolute, not incremental** (`W-03`, `D-05`). A replay overwrites rather than
   adding.
6. **The output destination is parameterised**, so a backfill can write elsewhere (`R-05`).
7. **The source range is parameterised** — start and end offsets or timestamps — so the job can
   be run over a bounded slice.

Properties 6 and 7 are configuration, take an hour, and are the two most often missing. A job
that hard-codes its output topic cannot be backfilled safely at all.

## What to take away

1. **Five reasons to reprocess and they need different designs.** "Lost state" must be fast and
   sets your retention requirement; the others can be slow.
2. **A replay is a second cluster.** 215 nodes for 33 hours to replay Corridor's 7 days, against
   a steady-state 96.
3. **The compute cost of a replay is the same at any speed** — node-hours are constant — so pick
   the rate by what the surrounding system tolerates, not by cost.
4. **Land raw records in the lake before any transformation.** If the bug is in the ingestion
   pipeline, the processed lake has the bug baked in, and you have nothing to recompute from.
5. **A replay reading old data evicts recent data from the broker page cache**, so its cost is
   paid by every live consumer. Replay from the lake, or on a separate cluster.
6. **Kappa wins for the semantics and loses for a small fraction of the execution.** One
   definition of the logic, run as a stream live and in batch mode for backfills, with a second
   implementation only where a measurement demands it — 2 jobs out of 180.
7. **Replay state is unchanged by replay speed**, and without watermark alignment it is *larger*
   than the live job's because the source spread is wider.
8. **Two replays over the same range is the fastest diagnostic** when a replay disagrees with
   the live run: agreement between replays points at lateness, disagreement points at
   non-determinism.
9. **Backfills write to a separate output, always**, and cut over by switching a view. An upsert
   backfill cannot delete rows that should not exist.
10. **Push side effects to the edge.** Everything upstream of a side effect can be a pure
    function of the input and is therefore replayable; anything with an effect in the middle is
    not.
11. **Design the replay path before you need it and write down its rate and cost**, so the
    Thursday conversation is "twenty hours and $3,180" instead of a day of investigation.
12. **Seven properties make a job replayable**, and the two most often missing — parameterised
    output and parameterised source range — take an hour to add.
