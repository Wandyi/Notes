# The Transport Log

Scenarios `T-01` … `T-12`.

This document is about the durable, ordered log that sits between producers and processors — on
Meridian, Kafka — but it is deliberately **not** a Kafka internals document. Broker failures,
replication mechanics, in-sync replica behaviour, consumer group rebalancing and delivery
semantics at the broker level are derived in depth in
[`../Kafka`](../Kafka/README.md), and this collection cites that rather than repeating it.

What this document covers is the part that collection does not: **the design decisions that
change when the log's primary consumer is a stream-processing platform rather than a set of
microservices.** Those decisions differ more than you would expect, and the reason is that a
stream processor's relationship with the log is not "read messages" but "own a position in a
parallel history that must survive being restarted, rescaled and replayed."

## 1. What the log is actually for here

Ask why the log exists and you get four answers, and only three of them are about transport.

**Decoupling.** Producers do not know consumers. Adding the thirty-second consumer to
`corridor.tracking.events` requires no change from Corridor. This is the answer everyone gives
and it is the least interesting one.

**Buffering.** The log absorbs the difference between the producer's rate and the consumer's
rate. When the attribution job restarts for 4.2 minutes, 45.8 million events accumulate; the log
holds them. Without it, those 4.2 minutes are data loss.

**Replay.** The log is not a queue; records are not consumed away. Two consumer groups can read
the same records, and one group can read them again tomorrow. This is what makes reprocessing
possible, and reprocessing is what makes streaming operationally survivable — see
[17-reprocessing-backfill-and-the-lambda-question.md](17-reprocessing-backfill-and-the-lambda-question.md).

**Being the unit of parallelism.** This is the one specific to a processing platform. A Flink
job's parallelism is bounded by its source's partition count, its state is keyed the same way
its source is partitioned (or else there is a shuffle), and its recovery granularity is a
partition. The log's partitioning is not just how throughput scales; it is **the skeleton the
processing topology is built on.**

That fourth answer is why partition-count decisions in this document look different from
partition-count decisions in a microservices context.

## 2. Partitions: three constraints, one number

A partition is the unit of ordering, of parallelism, and of assignment. Choosing the count means
satisfying three separate requirements with a single integer that you then cannot change (`I-06`).

### Constraint 1 — the parallelism ceiling

A Flink source cannot usefully run at higher parallelism than the partition count; extra
subtasks sit idle. So:

```
partitions ≥ maximum parallelism this stream will ever need
```

For `corridor.tracking.events`, the most expensive consumer is the attribution join at 4,500
events/s per core:

```
1,850,000/s ÷ 4,500 = 412 cores needed at average
× 1.75 for peak                     = 721
```

but the job also holds state, and rescaling stateful jobs is expensive
([12](12-scaling-parallelism-and-rescaling.md)), so you want headroom you will not have to use
often. 480 partitions sits below the 721 that a peak-sized attribution job would want, which is
a deliberate choice: that job runs at parallelism 320 and uses a second level of keying
internally to get more parallelism than the source provides. `P-03` explains that pattern.

### Constraint 2 — per-partition throughput

A single partition is written by one leader and read sequentially by one consumer task. Kafka
sustains roughly 30 to 60 MB/s per partition on modern hardware, but the practical ceiling is
much lower for a *processing* consumer, because the consumer must also do work:

```
a keyed aggregation at 14,000 events/s per core
means one task can drain one partition at 14,000 events/s
```

So the partition's event rate must stay below what one task can process:

```
partitions ≥ peak events/s ÷ per-task processing rate
          = 3,100,000 ÷ 14,000 = 222 for an aggregation
          = 3,100,000 ÷  4,500 = 689 for the join
```

### Constraint 3 — the per-broker ceiling

From [01](01-requirements-and-the-capacity-model.md) Section 6: roughly 400 partition-replicas
per broker before controller failover and leader election become slow. With 48 brokers and RF3:

```
48 × 400 ÷ 3 = 6,400 partitions platform-wide
```

Meridian uses 4,000, leaving room to grow. **Partitions are a platform-level budget**, which
means a team that asks for 2,000 partitions for one stream is spending a third of everyone's
budget. Meridian's stream-creation flow requires an explicit justification above 256.

### The defaults that follow

| Stream event rate | Partitions | Reasoning |
|---|---|---|
| < 1,000/s | 6–12 | Enough for consumer redundancy, small enough to batch |
| 1,000 – 50,000/s | 24–96 | Matches typical job parallelism |
| 50,000 – 500,000/s | 96–288 | Per-partition rate stays under 3,000/s |
| > 500,000/s | 288–720 | Per-partition rate under 4,000/s; check the per-broker budget |

And two rules that matter more than the table:

**Round to a number with many divisors.** 480 = 2⁵ × 3 × 5, so parallelisms of 2, 3, 4, 5, 6, 8,
10, 12, 16, 20, 24, 30, 32, 40, 48, 60, 80, 96, 120, 160, 240 and 480 all divide it evenly. A
prime-ish number like 250 gives you parallelism 125 or 250 and nothing useful in between, and
uneven division means some subtasks get two partitions and others one — a 2× skew in task load
for no reason. This is the cheapest optimisation in the entire collection and it is free at
creation time and impossible later.

**Do not over-provision "just in case" beyond about 2×.** Partitions cost: per-partition memory
on the broker, an open file handle set, a metadata entry replicated to every broker, a producer
batch buffer in every producer, and controller work on every leadership change. A stream with 20
events/s and 500 partitions is a real and common mistake; it batches terribly (`I-07`), it wastes
a slice of the platform budget, and it slows down every controller operation.

## 3. Retention is a recovery budget, not a storage policy

Most retention discussions are about cost. The useful framing is different: **retention is how
far back in time you can start over.**

Concretely, retention must exceed the largest of:

```
1. maximum tolerable consumer downtime
     - a job restart:                        4.2 min  (largest job, doc 01 §8)
     - a rolling session-cluster upgrade:    8 min
     - a bad deploy discovered and rolled back: 45 min
     - a weekend outage nobody noticed:      60 h     ← the real driver

2. maximum time to discover a processing bug
     - caught by monitoring:                 minutes
     - caught by a downstream consumer:      hours
     - caught by monthly reconciliation:     30 days  ← not affordable on the log

3. maximum time to onboard a new consumer that needs history
     - "start from the beginning" for a new job:  the whole retention
```

The honest resolution is that item 2 is not solvable by log retention at any sane cost, which is
why Meridian has two tiers of history:

| Tier | Where | Depth | Cost | Replay rate |
|---|---|---|---|---|
| Local log | Broker NVMe | 6 h | included in the 48 brokers | full speed, page-cache warm for 24 min |
| Tiered log | S3, read through the broker | 7 days | 33.6 TB × $0.023 = $773/mo | ~40% of local, bounded by S3 |
| Lake | Iceberg on S3, Parquet | 400 days | $14,200/mo | batch only, not a stream replay |

A bug discovered within seven days is fixed by replaying the log. A bug discovered after seven
days is fixed by a batch backfill from the lake, which is a different pipeline with different
code — and the fact that it is different code is the strongest argument in favour of the
"kappa" position that says you should be able to run the *same* job over the lake as a bounded
stream. [17](17-reprocessing-backfill-and-the-lambda-question.md) takes that argument seriously
and also explains where it fails.

### Per-stream retention on Meridian, and the reasoning

| Stream | Retention | Why that number |
|---|---|---|
| `northlight.playback.heartbeats` | 6 h | Purely operational. Nobody backfills a heartbeat |
| `waypoint.location.updates` | 12 h | Live map plus same-day trip reconstruction |
| `corridor.tracking.events` | 24 h local-tiered, 7 d in tiered storage | The 24 h attribution window must be replayable in full |
| `riverbend.orders.lifecycle` | 72 h | Order state machine; a stuck order is investigated within 3 days |
| `riverbend.payments.settled` | 7 d | Financial reconciliation runs weekly |
| `riverbend.inventory.adjustments` | compacted, no time limit | It is a table, not a history — Section 4 |

Note that `corridor.tracking.events` retention is **set by the longest window of any consumer.**
The attribution job holds 24 hours of state; if it loses that state and has to rebuild, it must
read 24 hours of history. Retention shorter than the largest window means a job whose state can
never be rebuilt, which means a job whose state must never be lost, which is not a property you
can guarantee. **The rule: retention ≥ the longest window of any consumer, plus the time to
notice the state is gone.**

## 4. Compaction: turning a stream into a table

A compacted topic keeps, for each key, the most recent record, and eventually deletes the
earlier ones. The topic stops being a history and becomes a **changelog of a table** — read it
from the beginning and you materialise the current state of every key.

This is the mechanism underneath stream-table duality, and it is used in three distinct ways on
Meridian.

**As a dimension table for enrichment.** `riverbend.catalog.changes` carries one record per SKU
whenever its attributes change. A Flink job reads it into broadcast state or a keyed table and
uses it to enrich the order stream. Because it is compacted, a job starting from scratch reads
~2.4 million records (one per SKU) rather than three years of changes.

**As a job's own changelog.** Kafka Streams stores its state in a compacted topic. Flink does
not — it uses checkpoints — but Flink's Table API upsert-kafka connector does the same thing for
its output.

**As the current-state view of an entity.** `riverbend.inventory.adjustments` is compacted on
`sku`, so a consumer that needs "the current stock level of everything" reads from the beginning
and gets exactly that.

### What compaction does not give you

Three misunderstandings, each of which has caused a production incident somewhere:

**Compaction is not immediate.** The log cleaner runs on segments that are eligible, which
excludes the active segment and anything newer than `min.compaction.lag.ms`. A key updated three
times in the last minute has all three records present. A consumer reading the whole topic gets
duplicates for recent keys and must be idempotent — which is fine if you expected it and a bug
if you did not.

**Compaction does not bound the topic's size to the key count.** It bounds it to the key count
*plus everything in the uncompacted region*. If the cleaner is behind — and it will be, because
it is a background thread competing with the request path — the topic can be many times larger
than its key space. `T-06` covers the failure.

**Deletion requires a tombstone, and tombstones expire.** To remove a key you write a record
with a `null` value. The cleaner then removes both the tombstone and all prior records for that
key, but only after `delete.retention.ms` (24 h by default). A consumer that is more than 24
hours behind can miss the tombstone entirely and keep the deleted key forever. For a GDPR
deletion flow this is not a curiosity, it is a compliance failure — see `Z-05`.

### Choosing compaction

**Compact when the topic represents state.** "What is true now" — prices, inventory, user
profiles, feature flags, configuration.

**Do not compact when the topic represents history.** "What happened" — orders, clicks,
payments, page views. Compacting a history topic destroys it, silently, over hours, and the
records are not recoverable.

The mistake is usually made by keying a history topic and then enabling compaction to control
its size. If you are compacting to control size, what you want is retention.

## 5. Tiered storage: the change that makes long retention affordable

Tiered storage moves closed log segments to object storage, leaving the broker with an index and
the recent segments. It changed the economics of the log enough to be worth its own section.

Meridian's numbers:

```
without tiering, 7-day retention on all streams:
  607.2 MB/s × 604,800 s = 367 TB × RF3 = 1,101 TB
  ÷ (5 TB × 0.7 per broker)            = 315 brokers

with tiering, 6 h local + 7 d in S3:
  local:  607.2 MB/s × 21,600 s = 13.1 TB × RF3 = 39.3 TB  → 12 brokers
  S3:     607.2 MB/s × 583,200 s = 354 TB, single copy     → $8,142/mo
```

From 315 brokers to 48 (which are sized by page cache, not disk — see
[01](01-requirements-and-the-capacity-model.md) Section 6) plus $8,142 a month. The saving is
roughly $110,000 a month.

What it costs you, and these are not small:

**Replay from the tier is slower.** Reading from S3 through the broker runs at roughly 40% of
local speed on Meridian, and it is subject to S3's request latency, which is 20 to 60 ms per
range read against microseconds from page cache. A backfill that would take 33 hours from local
disk takes 84 hours from the tier.

**The failure modes are new.** S3 throttling, S3 partial availability, and the broker's tiered-
read path having a different code maturity from the local path. `T-08` covers what this looks
like.

**Consumer lag alerting becomes ambiguous.** A consumer reading from the tier is *supposed* to
be slow. A lag of 4 million records means something different for a backfill job reading tiered
data than for a live job reading page cache, and a single threshold cannot distinguish them.
Meridian tags consumer groups as `live` or `replay` and alerts differently.

## 6. Scenarios

### T-01 — Consumer lag is high and throughput is fine

**What you see.** `kafka_consumergroup_lag` for a job is 40 million records and rising. The job's
records-per-second matches its input rate exactly. No backpressure, no errors, checkpoints
healthy.

**Mechanism.** The job is processing at exactly the input rate, so it will never catch up — it
accumulated the lag earlier (a restart, a deploy, a brief slowdown) and has no surplus capacity
to drain it.

This is the most common misreading of lag on a streaming platform. **Lag is a stock; throughput
is a flow.** A healthy flow with a large stock means the stock is permanent.

**Confirm it.** Compute the derivative:

```
drain rate = job throughput − input rate
if drain rate ≤ 0, lag will never reach zero
time to drain = lag ÷ drain rate
```

Meridian's dashboards show `estimated_time_to_drain` rather than raw lag, precisely because raw
lag does not distinguish "recovering, 6 minutes left" from "will never recover."

**Recover.** Give it surplus capacity: raise parallelism if the source partitions allow, or
temporarily raise the job's resource allocation. For a stateful job, rescaling means a savepoint
and restore, which costs downtime and therefore adds lag — so the arithmetic has to show the
rescale pays for itself. For the attribution job:

```
wait it out:
  surplus drain at parallelism 360 = 121,600/s
  40,000,000 ÷ 121,600 =                       329 s  (5.5 min)

rescale to parallelism 480:
  savepoint                                     95 s
  restore 4.2 TB ÷ 480 × (1/90 MB/s)            97 s
  RocksDB open                                  60 s
                                              ──────
  downtime                                     252 s
  extra lag accrued: 182,400 × 252 =     45,965,000 records
  new ceiling 304,000 × (480/360) =        405,333/s
  new surplus                              222,933/s
  drain (40.0M + 46.0M) ÷ 222,933 =            386 s
                                              ──────
  total                                        638 s  (10.6 min)
```

So on a 40-million lag, **rescaling is roughly twice as slow as waiting.** Setting the two
expressions equal gives the crossover: rescaling wins above about **94 million records** of lag.
Having that number written down is what makes the decision fast at 3 a.m. rather than a
twenty-minute argument while the lag grows.

**Prevent.** Provision at 60% busyness so there is always 40% drain surplus
([01](01-requirements-and-the-capacity-model.md) Section 8), and alert on time-to-drain rather
than on lag.

### T-02 — Partition skew makes half the cluster idle

**What you see.** A job with parallelism 480 has 12 subtasks at 100% busy and 468 at 8%. Overall
throughput is a quarter of what the cluster could do.

**Mechanism.** Records are not evenly distributed across partitions. Three causes, which need
different fixes:

1. **Key skew** — a few keys dominate (`I-05`, `K-03`).
2. **Partition-count skew** — parallelism does not divide the partition count, so some subtasks
   own two partitions and others one. With 480 partitions and parallelism 320, 160 subtasks own
   2 and 160 own 1: a 2× skew, permanently.
3. **Producer skew** — one producer instance is much hotter than the others and its sticky
   partitioner concentrates it.

**Confirm it.** Per-partition record rate over a 5-minute window:

```bash
for t in $(seq 0 2); do
  kafka-run-class.sh kafka.tools.GetOffsetShell \
    --bootstrap-server broker:9092 --topic corridor.tracking.events --time -1
  sleep 60
done
```

Difference consecutive runs. If the *same* partitions are hot, it is key skew. If the hot set
rotates, it is producer stickiness. If the rates are even but subtask busyness is not, it is
partition-count skew and the fix is arithmetic.

**Recover.** For partition-count skew, set parallelism to a divisor of the partition count. This
is a rescale, so the `T-01` arithmetic applies.

**Prevent.** Choose partition counts with many divisors (Section 2), and monitor
`max_partition_rate ÷ mean_partition_rate` per stream, alerting above 3.

### T-03 — A rebalance storm stalls every consumer in the group

**What you see.** A consumer group with 320 members spends 40% of its time in `PREPARING_REBALANCE`
and `COMPLETING_REBALANCE`. Throughput oscillates between full and zero on a roughly 90-second
cycle.

**Mechanism.** One member is being evicted for missing heartbeats (`session.timeout.ms` exceeded)
or for exceeding `max.poll.interval.ms`. With the eager rebalance protocol, every eviction causes
every member to give up every partition and re-acquire — a full stop-the-world across 320
members. If the underlying cause (a slow member) persists, the cycle repeats.

The 320-member scale is what makes it a storm rather than a hiccup: a rebalance that takes 200 ms
with 6 members takes 14 seconds with 320, because the coordinator serialises parts of it.

**Confirm it.**

```
kafka.consumer:type=consumer-coordinator-metrics
  rebalance-rate-per-hour   → should be ~0
  rebalance-latency-avg
  failed-rebalance-rate-per-hour
```

and on the broker side, the group coordinator's logs name the member that triggered it.

**Recover.** Find and fix the slow member. In the moment, raising `max.poll.interval.ms` buys
time if the cause is a long processing pause.

**Prevent.**

- **Use cooperative-sticky assignment** (`partition.assignment.strategy=CooperativeStickyAssignor`).
  It rebalances incrementally — only the affected partitions move — which turns a 14-second
  stop-the-world into a sub-second adjustment. This is the single highest-value consumer setting
  on a large group.
- **Use static group membership** (`group.instance.id`). A member that restarts within
  `session.timeout.ms` reclaims its partitions without triggering a rebalance at all, which makes
  rolling deploys nearly free.
- **Note that Flink does not use consumer groups for assignment.** Flink's Kafka source assigns
  partitions to subtasks itself and commits offsets only for monitoring. So Flink jobs are immune
  to this entire class — which is a real and underappreciated argument for a stream processor
  over a hand-written consumer group at high parallelism. It is also why a "consumer lag" metric
  read from the broker's `__consumer_offsets` can be stale or absent for a Flink job; see
  [16](16-observability-of-a-streaming-pipeline.md).

### T-04 — Offsets committed, work not done

**What you see.** After a job restart, a range of records was never processed. No errors.

**Mechanism.** The consumer committed offsets before the work completed. With `enable.auto.commit=true`
(the client default), offsets are committed on a timer every `auto.commit.interval.ms` — from
whatever position `poll()` last returned, regardless of whether the records were processed. A
crash between the commit and the processing loses everything in between.

**Confirm it.** `enable.auto.commit=true` in a consumer that does non-trivial work per record is
the confirmation; you do not need evidence.

**Recover.** Reset the group's offsets backwards and reprocess, if the sink is idempotent.

**Prevent.** `enable.auto.commit=false` and commit after processing. For Flink, offsets are part
of the checkpoint and this is handled — which is, again, an argument for the framework.

### T-05 — `auto.offset.reset` silently skips or replays everything

**What you see.** A job restarts after a 9-hour outage and reports normal lag immediately. Nine
hours of data were never processed.

**Mechanism.** The group's committed offset was older than the retention, so it no longer
exists. `auto.offset.reset=latest` (a very common setting) makes the consumer jump to the end.
Nine hours of records are skipped, silently, and the only sign is that the lag went to zero
faster than physics allows.

The opposite setting, `earliest`, has the opposite failure: a new consumer group, or one whose
offsets were lost, reads the entire retention from the beginning — which for
`corridor.tracking.events` is 24 hours at 1.85 million events a second, an unplanned 160-billion-
record replay that saturates the cluster.

**Confirm it.** The broker logs `OffsetOutOfRangeException` before the reset. Also: lag dropping
to zero in a single step rather than draining.

**Recover.** If you reset to `latest` and skipped data, the data is still in the log if you are
inside retention — find the offset by timestamp:

```bash
kafka-consumer-groups.sh --bootstrap-server broker:9092 \
  --group attribution-job --topic corridor.tracking.events \
  --reset-offsets --to-datetime 2026-09-28T02:00:00.000 --execute
```

**Prevent.** **Set `auto.offset.reset=none` for production jobs.** The consumer then fails
loudly with an exception when its offset is invalid, and a human decides. This converts a silent
data-loss event into a page, which is the trade you want. It is the "prefer a loud stall to a
quiet wrong answer" principle applied to offsets, and almost nobody does it.

### T-06 — The log cleaner is behind and a compacted topic is enormous

**What you see.** `riverbend.inventory.adjustments` is compacted on `sku` with 2.4 million SKUs,
and it is 340 GB. A new consumer takes 40 minutes to bootstrap instead of 90 seconds.

**Mechanism.** The log cleaner is a small pool of threads (`log.cleaner.threads`, default 1) that
must rewrite segments to remove superseded records. Rewriting 340 GB at the cleaner's throughput
— which is deliberately throttled to avoid competing with the request path — takes a long time.
Meanwhile the topic keeps growing.

Contributing causes, in order of frequency:

- `min.cleanable.dirty.ratio` default 0.5 means the cleaner will not touch a partition until half
  of it is superseded records. On a topic with 2.4 million keys updated 8,000 times a second,
  that is fine; on one with low update rates, segments sit uncleaned for a long time.
- `segment.ms` too large: the active segment is never cleaned, and if segments roll once a day,
  a day's writes are always uncleanable.
- Too few cleaner threads for the number of compacted partitions.

**Confirm it.**

```
kafka.log:type=LogCleanerManager,name=max-dirty-percent
kafka.log:type=LogCleaner,name=cleaner-recopy-percent
kafka.log:type=LogCleanerManager,name=time-since-last-run-ms
```

`max-dirty-percent` persistently high is the signal.

**Recover.** Increase `log.cleaner.threads` and lower `min.cleanable.dirty.ratio` for the
affected topic, then wait. The cleaner is not fast and there is no way to make it fast.

**Prevent.** For compacted topics that back a bootstrap path, set `segment.ms` to something like
an hour and `min.cleanable.dirty.ratio` to 0.1, accepting more cleaner I/O in exchange for a
topic that stays close to its key count.

### T-07 — A tombstone was missed and a deleted key came back

**What you see.** A SKU that was deleted six weeks ago reappears in the enrichment job's state
after the job is restarted from scratch.

**Mechanism.** The tombstone (null-valued record) was written, the cleaner removed the prior
records for that key, and then after `delete.retention.ms` (24 h) the cleaner removed the
tombstone itself. A consumer reading the topic from the beginning today never sees either the
value or the tombstone — but a consumer that had the value in state from before never received
the delete.

The specific failure is a consumer that was **down for longer than `delete.retention.ms`**, or a
consumer whose state predates the tombstone and which was restored from a checkpoint rather than
rebuilt from the topic.

**Confirm it.** Compare the job's state key set against the topic's current key set. The
difference is the set of keys deleted while the job was not watching.

**Recover.** Rebuild the state from the topic.

**Prevent.**

- **Set `delete.retention.ms` longer than the maximum time any consumer can be down** — for
  Meridian, 7 days rather than 24 hours. It costs a little storage.
- **For anything where deletion is legally meaningful (GDPR), do not rely on compaction
  tombstones alone.** Section 5 of [20](20-security-governance-and-pii.md) covers crypto-
  shredding, which does not depend on a background thread having run.

### T-08 — Tiered storage read path throttles a backfill

**What you see.** A backfill job reading 5-day-old data from the tier runs at 18% of its expected
rate. Broker CPU is low, network is low, and the brokers log `S3 SlowDown` responses.

**Mechanism.** S3 rate-limits per prefix — roughly 5,500 GET requests per second per prefix. A
backfill reading 480 partitions in parallel, each issuing range reads of 1 MB against segments
under the same topic prefix, can exceed that easily:

```
480 partitions × 90 MB/s target ÷ 1 MB per range read = 43,200 GET/s
against a per-prefix limit of ~5,500/s
```

**Confirm it.** The broker's remote-storage metrics:

```
kafka.server:type=RemoteStorageThreadPool,name=RemoteLogReaderTaskQueueSize
kafka.log.remote:type=RemoteLogManager,name=RemoteFetchErrorPerSec
```

and `SlowDown` / `503` in the tiered-storage logs.

**Recover.** Reduce the backfill's parallelism. Counter-intuitive but correct: fewer readers
each get more throughput than many readers being throttled.

**Prevent.**

- **Spread the object-key prefix.** Most tiered-storage implementations let you configure the
  key layout; putting a hash prefix before the topic name spreads across S3 partitions. Some do
  this by default; check yours.
- **Larger range reads.** Reading 8 MB at a time instead of 1 MB cuts the request rate 8×.
- **Rate-limit replays at the platform level.** Meridian's replay tool requests a token from a
  central limiter before starting; only two large replays run at once. This also protects the
  live path — `R-03`.

### T-09 — Compression changed and page-cache residency halved

**What you see.** Nothing, for three weeks. Then a routine job restart takes 4× longer than the
runbook says and produces a disk-read storm that slows every consumer on the cluster.

**Mechanism.** A team switched their producer from zstd-3 to lz4 to reduce producer CPU. The
stream's compressed rate rose 33%. Because it is 74% of the platform's bytes, the cluster's
total write rate rose 24%, and page-cache residency fell:

```
before: 55,800 MB ÷ 38.0 MB/s = 1,468 s = 24.5 min
after:  55,800 MB ÷ 47.1 MB/s = 1,185 s = 19.7 min
```

Then a second stream did the same thing, and residency reached 14 minutes at average and 8 at
peak. A job down for 12 minutes now reads from disk.

**Confirm it.** Residency is not a metric Kafka exposes; you have to compute it:

```
residency_seconds = page_cache_bytes_per_broker / bytes_written_per_broker_per_second
```

Meridian emits it as a recording rule and alerts below 15 minutes.

**Recover.** Revert the compression change, or add brokers.

**Prevent.** **Treat compression codec as a platform-level setting, not a team-level one**, for
any stream above 1% of platform bytes. And emit residency as a first-class metric so that the
coupling is visible — this is the same lesson as
[`../LogCollector`](../LogCollector/README.md)'s buffering chapter, where an unrelated
compression decision moved the residency number and nobody noticed.

### T-10 — Ordering assumed across partitions

**What you see.** A consumer processes `OrderShipped` before `OrderCreated` for the same order,
despite the topic being keyed by `order_id`.

**Mechanism.** Several possibilities, and distinguishing them matters:

- The events are on *different topics* (`orders.created` and `orders.shipped`). No ordering
  exists between topics, ever, regardless of keys. This is `S-03`'s argument.
- The partition count changed (`I-06`).
- The consumer processes partitions in parallel and merges results without preserving order —
  which is correct per-partition and wrong across them.
- The producer used a different key for the two events. `OrderCreated` keyed by `order_id`,
  `OrderShipped` keyed by `shipment_id`.

**Confirm it.** Log the topic-partition-offset of both records. Same partition with ascending
offsets means the consumer reordered; different partitions means the producer did.

**Prevent.** State the ordering guarantee in the stream contract (`S-11`) and make consumers
depend on a logical sequence rather than on arrival order where the guarantee does not cover
them (`I-13`).

### T-11 — One cluster, or several?

**What you see.** A decision, usually forced by an incident in which one tenant's mistake
affected everyone.

**Mechanism.** A single Kafka cluster is a shared failure domain for: broker capacity, controller
state, the partition budget, the request-handler thread pool, and the page cache. Quotas
(Section 7) mitigate the first and last; nothing mitigates a controller problem.

Meridian runs **three clusters**, split by *blast radius*, not by tenant:

| Cluster | Brokers | Contents | Why separate |
|---|---|---|---|
| `meridian-bulk` | 34 | Corridor tracking, Northlight heartbeats, Waypoint locations | 96% of the bytes. Loud, tolerant of degradation |
| `meridian-txn` | 8 | Riverbend orders and payments, Gateline tickets | Transactional, low volume, high value. Must not be affected by a bulk incident |
| `meridian-derived` | 6 | Job outputs, changelogs, dead-letter topics | Platform-internal; can be rebuilt from the others |

The split costs: cross-cluster joins require mirroring (`G-02`), three clusters to operate
instead of one, and a routing decision every time a stream is created.

It buys: the 8-broker transactional cluster is small enough to reason about, its p99 produce
latency is 4 ms rather than the bulk cluster's 18, and the failure of the bulk cluster — which
is where all the interesting failures happen — does not stop payments.

**The rule:** split by blast radius and by *workload shape*, not by team. A cluster whose
streams all have similar latency requirements, similar record sizes and similar value is
tunable; one that mixes 67-events-per-second payments with 1.85-million-per-second tracking is
tuned for neither.

### T-12 — Choosing the log: Kafka, Pulsar, Kinesis, Redpanda

Not a failure — the decision, with the properties that actually differentiate them for a
*processing* platform.

| | Kafka | Pulsar | Kinesis | Redpanda |
|---|---|---|---|---|
| Storage model | Partition = file set on the broker | Segments in BookKeeper, brokers stateless | Managed shards | Partition = file set, no JVM |
| Rescaling partitions | Manual, breaks keyed ordering | Same for ordering, but broker rebalance is free | Shard split/merge, ordering preserved per shard lineage | Same as Kafka |
| Max retention | Tiered to object storage | Tiered, and native — BookKeeper offload | 365 days | Tiered |
| Ops burden | High, well-documented | Higher — two systems (brokers + bookies + ZooKeeper) | None | Lower than Kafka, same API |
| Per-partition throughput | 30–60 MB/s | Similar | 1 MB/s **in**, 2 MB/s out, hard | 60–100 MB/s |
| Tail latency p99 | 15–25 ms typical | 10–20 ms | 30–80 ms | 3–8 ms (no JVM GC) |
| Flink connector maturity | Reference implementation | Good | Good | Uses the Kafka connector |
| Cost at Meridian's scale | $19,973/mo self-managed | ~$28,000 (more components) | ~$224,000/mo | ~$16,000/mo |

The Kinesis row is worth stopping on. **1 MB/s per shard is a hard limit**, and at Meridian's
607 MB/s that means 607 shards minimum for throughput alone, before any parallelism
consideration — and Kinesis charges per shard-hour plus per record. The arithmetic:

```
607 shards × $0.015/shard-hour × 730 h        = $6,647
5.9M records/s × 2,629,800 s = 15.52 trillion records
  = 15,516,000 million PUT payload units
  × $0.014 per million                          = $217,224
                                                  ─────────
                                                  $223,871
```

which is why the table says ~$224,000 — the per-record charge dominates completely at high rates
with small records. Kinesis is excellent at 50,000 events/s and financially absurd at 5.9
million. **Managed services price by the unit that is cheap for them and expensive for you;
check which unit before adopting.**

Meridian uses Kafka, for the boring reason: the Flink connector is the reference implementation,
the operational knowledge is the most widely available, and the ecosystem (Schema Registry,
Connect, MirrorMaker, the metrics) is deepest. Redpanda's latency numbers are genuinely better
and its operational model is simpler; the argument against it at Meridian's scale is the smaller
pool of people who have operated it at 48 nodes, which is an organisational argument and a
legitimate one.

## 7. Quotas: stopping one tenant from consuming the cluster

Kafka's quota system limits per-client-id or per-user throughput at the broker. It is the only
mechanism that works *inside* a shared cluster, and it has one property that makes it better than
most rate limiters: **it throttles by delaying the response, not by rejecting the request.** A
throttled producer simply sees higher latency and naturally slows down; no retry storm, no error
handling path.

Meridian's quotas:

| Principal | Produce | Fetch | Request % |
|---|---|---|---|
| `corridor-tracking` | 500 MB/s | 1,200 MB/s | 40% |
| `northlight-*` | 120 MB/s | 300 MB/s | 15% |
| `waypoint-*` | 60 MB/s | 180 MB/s | 10% |
| `riverbend-*` (txn cluster) | 40 MB/s | 120 MB/s | 25% |
| default (anything unregistered) | 10 MB/s | 30 MB/s | 2% |

Three notes on getting this right:

**The default quota is the important one.** A new, unregistered client should get a small
allowance, not an unlimited one. Most clusters have no default quota, which means every mistake
is unbounded until someone notices.

**`request_percentage` matters more than bandwidth at high message rates.** A client sending
small records at a high rate uses little bandwidth and a lot of request-handler time. The
bandwidth quota does not constrain it; the request quota does. This is the one people forget.

**Quotas are applied per broker, not per cluster.** A quota of 500 MB/s means 500 MB/s *per
broker*, so with 48 brokers the effective ceiling is 24 GB/s. Set them from the per-broker
arithmetic or they do nothing.

## What to take away

1. **On a processing platform, the log's partitioning is the skeleton the topology is built
   on** — it fixes parallelism, ordering and recovery granularity at once, and you cannot change
   it afterwards.
2. **Partition count must satisfy three constraints:** the parallelism ceiling, per-partition
   processing rate, and the per-broker budget. Pick a number with many divisors; uneven division
   means permanent 2× subtask skew.
3. **Retention is a recovery budget.** It must exceed the longest window of any consumer plus
   the time to notice the state is gone — not a storage-cost calculation.
4. **Tiered storage turns 315 brokers into 48 plus $8,000 a month**, at the cost of slower
   replay, S3 throttling as a new failure mode, and lag metrics that need a `live`/`replay` tag.
5. **Compact when the topic is state, never when it is history.** Compaction is not immediate,
   does not bound the topic to its key count, and loses tombstones after `delete.retention.ms` —
   which is a compliance problem, not a curiosity.
6. **Lag is a stock, throughput is a flow.** Alert on estimated time to drain. And know the
   crossover point at which rescaling to recover beats waiting — on Meridian's largest job it is
   94 million records.
7. **`auto.offset.reset=none` in production.** `latest` silently skips data after a long
   outage; `earliest` triggers an unplanned full replay. Both are worse than a page.
8. **Cooperative-sticky assignment and static membership** turn a 14-second stop-the-world
   rebalance into a sub-second adjustment at 320 members. Flink's source avoids the class
   entirely, which is a real argument for the framework.
9. **Compression is a platform-level setting** for any stream above 1% of platform bytes,
   because it silently moves page-cache residency, which is what determines whether a recovery
   is cheap.
10. **Split clusters by blast radius and workload shape, not by team.** Meridian's 8-broker
    transactional cluster has a 4 ms p99 because it does not share request handlers with 1.85
    million tracking events a second.
11. **Managed log services price by the unit that is cheap for them.** Kinesis at Meridian's
    record rate is $212,000 a month against $20,000 for self-managed Kafka, almost entirely in
    per-record charges.
12. **Set a small default quota and set `request_percentage`**, because a high-rate small-record
    client is constrained by request-handler time rather than by bandwidth.
