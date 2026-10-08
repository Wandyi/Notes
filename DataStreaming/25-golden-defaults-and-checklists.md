# Golden Defaults and Checklists

The reference page. Triage order first, because that is what you need when something is broken.
Then the annotated default configuration for every tier, four checklists, and the numbers worth
memorising.

Everything here is derived elsewhere in the collection; this is the condensed form with pointers
back.

## 1. If something is broken right now

```
1. Check the SERVING STORE's freshness, not the pipeline's metrics.
     SELECT max(window_start) FROM <table>
   Fresh and correct → the page is a leading indicator. You have time.

2. One job or many?
     many → platform. What do they share? (node pool, Kafka cluster,
            S3 prefix, an upstream stream, a recent platform change, a zone)
     one  → step 3.

3. Lagging, stalled, or wrong?
     lagging (consuming, behind, watermark advancing)  → doc 11 §3
     stalled (not consuming, or watermark frozen)      → runbook 3, doc 15 §4
     wrong   (keeping up, output incorrect)            → runbook 4, doc 15 §4

4. What changed? deploys, schema registrations, producer deploys, config,
   platform components, infrastructure — in that order of likelihood.

5. ⚠️ IS LAG APPROACHING RETENTION?
     If yes: extend retention NOW, before any diagnosis. One command,
     buys hours, and the alternative is permanent data loss.

6. Mitigate reversibly before diagnosing: roll back, shed, or stop a
   consumer. On a log-based platform, stopping a consumer defers data
   rather than losing it.
```

⚠️ **Three actions that usually make it worse:**

- **Restarting a job** that is stalled on a watermark (it restores the same state) or restoring
  (it discards download progress).
- **Reducing the checkpoint interval** when checkpoints are slow.
- **Adding capacity** to a metastable system, which spends its first minutes restoring state.

## 2. Golden configuration

Annotated, with the reasoning. Copy the values, keep the comments.

### Producer

```properties
acks=all                              # durability is min.insync.replicas
enable.idempotence=true               # stops retry duplicates, not restart ones
max.in.flight.requests.per.connection=5   # safe with idempotence; 1 costs 60-80% throughput
compression.type=zstd                 # level 3; pick by which resource is scarce
linger.ms=<per stream: 5 to 500>      # the lever that improves under load
batch.size=<256KB-512KB for high rate>
buffer.memory=<delivery.timeout × peak rate>   # 32MB default is not a considered value
max.block.ms=<less than your request timeout>  # so a stuck producer fails the send, not the request
delivery.timeout.ms=120000
retry.backoff.ms=50
retry.backoff.max.ms=1000             # exponential with jitter, not fixed
request.timeout.ms=15000              # abandon a stalled broker faster than the 30s default
max.request.size=1048576              # do not raise without a written reason
```

Plus, in code: **a failure handler on every `send()`**, required by the wrapper library rather
than optional, incrementing a metric tagged with topic and exception class.

### Topic

```properties
replication.factor=3
min.insync.replicas=2                 # this, not RF, is your durability
retention.ms=<longest consumer window + time to notice state loss>
segment.ms=3600000                    # 1h, so the log cleaner has work to do
compression.type=producer             # do not recompress on the broker
delete.retention.ms=604800000         # 7d, longer than any consumer can be down
min.cleanable.dirty.ratio=0.1         # for compacted topics backing a bootstrap path
```

Partition count: a number with many divisors (24, 48, 96, 240, 288, 360, 480). Immutable once
the topic carries keyed data.

### Consumer (hand-written, not Flink)

```properties
enable.auto.commit=false              # commit after processing, never before
auto.offset.reset=none                # ⚠️ fail loudly rather than skipping or replaying everything
partition.assignment.strategy=CooperativeStickyAssignor   # incremental rebalance
group.instance.id=<stable per instance>   # static membership: restarts do not rebalance
max.poll.records=2000
fetch.min.bytes=<1 for latency, 65536 for throughput>
receive.buffer.bytes=1048576          # matters for cross-zone fetches
isolation.level=read_committed        # only if you need it; it inherits 2PC's stall risk
client.rack=<az>                      # rack-aware fetch — worth 20% of the bill
```

### Flink job

```yaml
# Parallelism and keys
parallelism: <divides both partition count and maxParallelism>
pipeline.max-parallelism: 1440        # ⚠️ fixed at first run, forever. Many divisors.

# State
state.backend: rocksdb
state.backend.incremental: true
state.backend.local-recovery: true    # if the disk arithmetic allows (doc 08 §4)
state.backend.rocksdb.memory.managed: true

# Checkpointing
execution.checkpointing.interval: <>= 10x p99 duration>
execution.checkpointing.min-pause: <half the interval>
execution.checkpointing.timeout: <generous; a slow checkpoint beats four failed ones>
execution.checkpointing.mode: EXACTLY_ONCE   # this is about checkpointing, NOT your sink
execution.checkpointing.aligned-checkpoint-timeout: 30s   # cheap path normally, robust under pressure
execution.checkpointing.externalized-checkpoint-retention: RETAIN_ON_CANCELLATION  # ⚠️
state.checkpoints.num-retained: 3
state.checkpoints.dir: s3://bucket/_entropy_/...          # ⚠️ entropy, or restores stampede

# Restarts
restart-strategy: exponential-delay
restart-strategy.exponential-delay.initial-backoff: 10s
restart-strategy.exponential-delay.max-backoff: 5min
restart-strategy.exponential-delay.backoff-multiplier: 2.0
# plus a failure-rate limit: stop and page rather than loop forever

# Network
taskmanager.network.memory.buffer-debloat.enabled: true   # 2.4s -> 310ms p99, no throughput cost
taskmanager.network.memory.buffer-debloat.target: 1s
cluster.evenly-spread-out-slots: true

# Diagnostics
rest.flamegraph.enabled: true         # turn it on before you need it
```

And in code:

```java
// uid on EVERY stateful operator, from version one
.uid("corridor-sessionize-v1")

// watermarks: clamp, and handle idleness
WatermarkStrategy
    .<Event>forBoundedOutOfOrderness(Duration.ofSeconds(<p99.5 of measured lateness>))
    .withTimestampAssigner(clampedAssigner)     // future/ancient -> ingest time + counter
    .withIdleness(Duration.ofSeconds(30));

// state TTL: four non-default settings that all matter
StateTtlConfig.newBuilder(Time.hours(26))
    .setUpdateType(OnCreateAndWrite)
    .setStateVisibility(NeverReturnExpired)
    .setTtlTimeCharacteristic(TtlTimeCharacteristic.EventTime)   // not the default
    .cleanupInRocksdbCompactFilter(1000)                          // ⚠️ or TTL does nothing for dead keys
    .build();

// async I/O: always override the timeout action
public void timeout(In in, ResultFuture<Out> rf) {
    lookupTimeoutCounter.inc();
    rf.complete(singleton(Out.withoutLookup(in)));   // degrade, do not fail the job
}

// CI: fail on Kryo fallback
env.getConfig().disableGenericTypes();
```

### Serving

```sql
-- ClickHouse, for a streaming sink
CREATE TABLE metrics_raw (...)
ENGINE = ReplacingMergeTree(version)
ORDER BY (window_start, surface, country);   -- ⚠️ IMMUTABLE columns only

-- the unqualified name is the SAFE view
CREATE VIEW metrics AS
  SELECT ... FROM metrics_deduped WHERE completeness_class = 'final';
CREATE VIEW metrics_realtime AS
  SELECT ... FROM metrics_deduped;           -- the risky one is qualified
```

Batch inserts to 100,000 rows or use `async_insert`; 1,850 inserts/s is two orders of magnitude
past what merges sustain.

## 3. Checklist: designing a new stream

```
[ ] Owner, team, and on-call named
[ ] Access pattern declared → this picks the serving store
[ ] Event rate, average and peak, with the peak's cause
[ ] Record size, and the encoding (Avro unless there is a reason)
[ ] Partition count: many divisors, sized for max parallelism, immutable after
[ ] Key: what specifically must not be reordered? (usually finer than the entity)
[ ] Key skew measured or estimated
[ ] Retention: >= longest consumer window + time to notice state loss
[ ] Compacted or not: is this state or history?
[ ] Guarantee, per this stream, with the sink's method (A/B/C/D)
[ ] Schema registered, FULL_TRANSITIVE, units in field names
[ ] Envelope present and complete
[ ] Every unbounded field bounded, with a truncation counter
[ ] PII classification per field
[ ] Lateness distribution measured → watermark delay chosen at p99.5
[ ] Stream contract written, including "what does null mean" and the
    lateness distribution
[ ] Cost estimate shown to the requester
```

## 4. Checklist: designing a new job

```
STATE
[ ] Key cardinality bounded, or retention bounded
[ ] Entries per key bounded (every collection has a maximum)
[ ] Bytes per entry measured, not guessed
[ ] state = keys x entries x bytes x 1.23 — computed, and acceptable per slot
[ ] TTL with EventTime + cleanupInRocksdbCompactFilter, or an explicit clear
[ ] Serializer is Avro or POJO; disableGenericTypes() passes in CI
[ ] maxParallelism set explicitly, many divisors, parallelism divides it

TIME
[ ] Event time from a meaningful producer timestamp (if not, say so and use ingest time)
[ ] Timestamp assigner clamps future and ancient values, with counters
[ ] withIdleness configured
[ ] Watermark delay >= p99.5 lateness + tolerated clock skew
[ ] No wall-clock calls, no processing-time timers, no processing-time TTL

CORRECTNESS
[ ] Guarantee chosen per the decision tree; sink method named
[ ] Output is absolute, not a delta
[ ] Aggregations use integers for sums
[ ] Accumulator is O(1) in record count (sketches where not)
[ ] Unmatched / late / empty cases each have a defined, visible behaviour
[ ] Deserialization failure: fail, skip-and-count, or dead-letter — decided

OPERATIONS
[ ] uid on every stateful operator
[ ] Output destination and source range parameterised (so it can be backfilled)
[ ] Restore time computed: state ÷ (parallelism × 90 MB/s) + 60s + catch-up
[ ] Checkpoint interval >= 10x expected p99 duration
[ ] Job manifest written (inputs, outputs, guarantees, grain, SLA, runbook)
[ ] Dashboard: the six panels, with min/max not avg on watermarks
[ ] Alerts: watermark lag, watermark spread, time to drain, late ratio,
    checkpoint duty cycle, restart rate, busy skew, state growth, DLQ rate
[ ] "What if the sink is down?" answered before launch
```

## 5. Checklist: a twenty-question design review

Ask these of any streaming design. A design that answers all twenty is unusually good.

```
 1. Is 'real time' a number, with a reason?
 2. Average or peak, and what causes the peak?
 3. What is an event, and how many per user action?
 4. How far back must this be replayable, and why that number?
 5. What guarantee, per stream, and which sink method provides it?
 6. What is the binding constraint on the transport tier?
 7. What is the lateness distribution, measured?
 8. What happens to records later than the watermark?
 9. What is the state, by the formula, and what bounds it?
10. What is maxParallelism, and does the parallelism divide it?
11. How long from crash to correct output, including catch-up?
12. What is the cross-zone bill, and is rack-aware fetch on?
13. Which stores hold a copy, and which is the system of record?
14. Can every disposable store be rebuilt, and has it been?
15. Is the job replay-deterministic? (No wall clock, no lookups, no side effects.)
16. Which metric would be green during the failure you most fear?
17. Who is paged, and what does their runbook's step 1 say?
18. What is the completeness, published where?
19. What compares this against an independent source, how often?
20. At what volume, state size or job count does this design expire?
```

Question 16 is the one that produces the most useful conversation.

## 6. What to build at your scale

Three tiers. Build only the row you are in, plus everything above it.

### Under 50,000 events/s, under 15 jobs

```
BUILD
  a Kafka cluster (managed), 3 brokers
  Kafka Streams in your services, or a handful of Flink jobs
  a schema registry with FULL_TRANSITIVE
  the uniform envelope
  producer failure handlers + record-error-rate alerts
  watermark lag and late-record-ratio alerts
  uid on every operator, pre-deploy savepoints
  one reconciliation against an independent source

DO NOT BUILD
  an ingestion gateway (unless you have untrusted producers)
  tiered storage, multiple clusters, autoscaling
  cost attribution, node pools, multi-region
  a cells architecture
```

### 50,000 – 2,000,000 events/s, 15–100 jobs

```
ADD
  a Flink cluster you operate, with the golden config
  the job manifest, and ACLs generated from it
  tiered storage (when broker count for disk exceeds ~15)
  a second Kafka cluster if workload shapes genuinely conflict
  quotas, including a small default for unregistered principals
  rack-aware fetch  ← do this early; retrofitting moves partition assignment
  per-job dashboards, the six panels
  the daily reconciliation for Tier 1 datasets
  a dead-letter path with a tested replay tool
  monthly gamedays
  the threshold table for your own numbers
```

### Above 2,000,000 events/s

```
ADD
  an ingestion gateway with the admission ladder
  zone-local placement for tolerant jobs
  the cost-attribution report, per tenant, per event
  node pools by workload shape
  autoscaling on true processing rate, with a dead band and slow scale-down
  tiered restore priority and checkpoint-path entropy
  a load-shedding switch driven by a control topic
  crypto-shredding for identifier fields
  a regional rollup topology if you are in more than one region
  cells, when blast radius or the control plane forces it
```

## 7. The numbers worth remembering

Meridian's, as reference points. Yours will differ; the ratios usually will not.

**Volume**

```
4.0M events/s  ×  400 B  =  1.6 GB/s  =  138.3 TB/day  =  345.6 billion events/day
compression 3.2:1 (Avro + zstd-3)  →  500 MB/s onto the log
derived streams add 47.5% of the record count  →  5.9M/s, 607 MB/s
read fan-out 3.4  →  2,064 MB/s of broker egress
```

**Per-core throughput by operator class** (the 9× spread is the point)

```
stateless map/filter                55,000/s
keyed aggregation, small state      14,000/s
keyed aggregation, large state       6,000/s
stream-stream join, 24h window       4,500/s
async lookup, capacity 200 @ 8ms    22,000/s   (capacity ÷ latency)
```

**State**

```
state = keys × entries-per-key × bytes × 1.23 (RocksDB space amplification)
RocksDB write amplification: 10-30×
restore: state ÷ (parallelism × 90 MB/s), plus 60s open, plus catch-up
catch-up surplus = input × (1/busyness − 1)
```

**Latency**

```
stateless, at-least-once:           p50 36 ms,    p99 78 ms
same, exactly-once @ 60s checkpoint: p50 31.8 s,  p99 62.1 s     ← a factor of 870
windowed result freshness = window + watermark delay + sink flush + store visibility
```

**The three constraints that surprise people**

```
page-cache residency = page cache per broker ÷ write rate per broker
  → 48 brokers where bytes say 12, because residency is the recovery budget

cross-zone network = 66.7% of the bill
  → rack-aware fetch + zone-local placement = 31.6% saved, as configuration

completeness curve is flat past p99.5
  → 2s → 60s of watermark delay buys 0.12% more data
```

**Cost**

```
$34 per billion events     $0.086 per GB ingested     $2,007 per job (median $310)
```

## 8. Ten things that are almost always wrong in a first design

A shortcut version of this whole collection.

1. **`CheckpointingMode.EXACTLY_ONCE` assumed to mean the pipeline is exactly-once.** It is
   about checkpointing. The sink determines your guarantee.
2. **A watermark delay copied from a tutorial** instead of measured from the stream's lateness
   distribution.
3. **`withIdleness` not configured**, so a quiet partition freezes time.
4. **No `uid` on stateful operators**, so the first savepoint restore after a graph change
   silently starts with empty state.
5. **`ValueState<HashMap>` instead of `MapState`**, serializing the whole map on every access.
6. **State with no TTL, or a TTL without `cleanupInRocksdbCompactFilter`** — which does nothing
   for exactly the dead keys it exists for.
7. **`auto.offset.reset=latest`**, which silently skips data after a long outage.
8. **Emitting deltas instead of absolute values**, which makes every replay wrong.
9. **No measurement of what was dropped** — late records, dead letters, skipped enrichments —
   so completeness is unknown rather than high.
10. **Partition count chosen as a round number** rather than one with many divisors, producing
    permanent subtask skew that no amount of scaling fixes.
