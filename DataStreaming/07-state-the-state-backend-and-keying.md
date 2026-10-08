# State, the State Backend, and Keying

Scenarios `K-01` … `K-12`.

State is what makes a stream processor more than a fancy `map()`. It is also what makes it
expensive to run, slow to recover, and hard to change. On Meridian, 106 of 180 jobs hold state,
31 TB in total, and those jobs account for roughly 80% of the operational load.

This document covers what state actually is, where it physically lives, how big it gets, and the
dozen ways it grows in a direction nobody planned.

## 1. Three kinds of state

### Keyed state

The common case. The stream is partitioned by a key, and each parallel subtask owns a disjoint
set of keys. State is accessed implicitly for whatever key the current record has:

```java
// inside a KeyedProcessFunction, processing a record for member "m-4471"
ValueState<Long> count = getRuntimeContext().getState(descriptor);
count.update(count.value() + 1);   // reads and writes m-4471's counter
```

You never name the key. The runtime knows it because the record is keyed, which is what makes
keyed state scale: the runtime can move a key's state to a different machine without the code
knowing.

Flink's keyed state primitives:

| Primitive | Holds | Typical use | Cost note |
|---|---|---|---|
| `ValueState<T>` | One value per key | A counter, a last-seen timestamp | Cheapest |
| `ListState<T>` | A list per key | Buffered records awaiting a match | Appends are cheap; full reads are not |
| `MapState<K,V>` | A map per key | A per-key index, a set of seen IDs | **Individual entries are addressable** — see below |
| `ReducingState<T>` | One value, folded on write | A running sum | Same cost as `ValueState` |
| `AggregatingState<I,A,O>` | An accumulator | A window aggregate | Same |

The `MapState` note is the most important practical detail in this section. With the RocksDB
backend, **`MapState` stores each entry under its own RocksDB key**, so reading or updating one
entry touches only that entry. `ValueState<HashMap<K,V>>` — which is what people write when they
do not know about `MapState` — serializes and deserializes the *entire map* on every access.

The arithmetic for a per-member map of 400 recently-seen item IDs at 40 bytes each:

```
ValueState<HashMap>: every read deserializes 400 × 40 B = 16 KB
                     every write serializes 16 KB
                     at 1,850,000 records/s that is 29.6 GB/s of ser/de
                     ≈ 59 µs per record of pure serialization
                     → ~110 cores just for this

MapState:            every read/write touches one 40 B entry
                     ≈ 0.4 µs
                     → under 1 core
```

**A factor of roughly 140.** If you take one implementation detail from this document, take this
one.

### Operator state

Per-subtask, not per-key. Small, and mostly used by connectors: the Kafka source's list of
assigned partitions and offsets is operator state. You rarely write it yourself. Its
characteristic property is how it is redistributed on rescale — either split across new subtasks
(`ListState`) or copied to all of them (`UnionListState`), and the second scales badly.

### Broadcast state

A small dataset replicated to every subtask, updated by a control stream. Feature flags,
configuration, a small lookup table, a set of rules. Every subtask has the full copy, so it is
bounded by what fits in one subtask's memory — practically, under a few hundred megabytes.

`J-07` covers using broadcast state for enrichment, including the ordering hazard that makes it
subtly non-deterministic.

## 2. Where state physically lives

Flink offers two backends and the choice is not close.

**HashMapStateBackend** keeps state as Java objects on the heap. Access is a hash-map lookup —
about 0.1 µs, no serialization. Snapshots serialize the whole thing.

**EmbeddedRocksDBStateBackend** keeps state in RocksDB, an embedded log-structured merge tree on
local disk, with an in-memory block cache. Access requires serialization and possibly a disk
read — 4 µs warm, 110 µs cold. Snapshots can be incremental.

| | Heap | RocksDB |
|---|---|---|
| Access latency | ~0.1 µs | 4 µs warm, 110 µs cold |
| Max practical size per slot | ~4 GB (GC pressure) | 100+ GB |
| Incremental checkpoints | No — full snapshot every time | Yes |
| Object serialization | Only at checkpoint | On every access |
| GC behaviour | Large heaps mean long pauses | State is off-heap; GC stays small |
| Spill behaviour | OOM | Disk |

**The rule:** heap below about 1 GB per slot, RocksDB above. Meridian uses RocksDB for
everything except three small jobs, because the incremental-checkpoint property matters more than
the latency.

That last point is worth spelling out, because the latency comparison makes heap look obviously
better. A job with 40 GB of state on the heap must serialize and upload 40 GB **every
checkpoint**. At a 60-second interval and 2 GB/s of upload bandwidth, that is 20 seconds of every
minute spent checkpointing — and the checkpoint cannot be incremental because the heap backend
has no way to know what changed. RocksDB's incremental checkpoints upload only new SST files:
5.78 GB per checkpoint for a 4.2 TB job (from
[01](01-requirements-and-the-capacity-model.md) Section 8). **Incremental checkpointing is the
reason to use RocksDB, and the access-latency cost is the price.**

## 3. The RocksDB internals that actually matter

You do not need to be a RocksDB expert. You need five facts, because each one shows up as a
production symptom.

### Fact 1 — writes go to memory, then to sorted files

A write goes into a **memtable** (in memory). When the memtable fills (`write_buffer_size`,
default 64 MB in Flink), it is flushed to disk as an immutable **SST file** at level 0. Level 0
files are merged into level 1, level 1 into level 2, and so on, with each level roughly 10× the
size of the one above.

Consequence: **a write is cheap and a read may not be.** A read checks the memtable, then each
level, until it finds the key. Bloom filters let it skip levels that certainly do not contain the
key, which is why Bloom filters matter enormously (Fact 4).

### Fact 2 — compaction rewrites data many times

Merging level *n* into level *n+1* rewrites both. A given byte is rewritten once per level it
descends through. This is **write amplification**, and for a default 7-level configuration it is
roughly 10–30×.

Consequence: **the disk write rate is 10–30× your logical write rate.** For a job doing 180,000
state writes a second at 220 bytes:

```
logical:  180,000 × 220 B = 39.6 MB/s
physical: × 20 (measured on this job) = 792 MB/s of disk writes
```

An `r6id.4xlarge`'s NVMe sustains roughly 1.1 GB/s of writes, so this job is using 72% of its
disk write capacity — and if amplification rises (it does, as the level structure deepens), it
saturates. This is the real reason `K-06` happens.

### Fact 3 — compaction steals CPU from your operators

Compaction runs on background threads that compete with the task threads for cores. During a
heavy compaction, throughput drops — not because your code got slower, but because RocksDB is
using the CPU.

Consequence: **a job's throughput is not constant even under constant input.** You will see
sawtooth patterns in busyness that correlate with nothing in your code. Do not chase them; size
for the trough.

### Fact 4 — the block cache determines whether reads hit disk

RocksDB caches decompressed data blocks in memory. A read that hits the cache is 4 µs; a miss is
110 µs on NVMe. The cache is shared across all state in a slot.

Consequence: **the single most effective RocksDB tuning is making the working set fit the block
cache.** Flink's managed memory does this automatically if you give it enough:

```
r6id.4xlarge: 128 GiB, 16 slots
  JVM heap per slot:         2 GiB
  managed memory per slot:   4 GiB  (block cache + write buffers)
  → block cache ≈ 2.8 GiB per slot
```

For the attribution job at 4.2 TB over 320 subtasks, each slot holds 13.1 GB and caches 2.8 GB
— a 21% cache-to-state ratio. The measured hit rate is 94%, which is much better than 21% because
access is heavily skewed toward recent impressions. **The hit rate depends on your access
pattern, not on the ratio**, which is why you must measure rather than compute it.

### Fact 5 — space amplification means the disk holds more than the data

Superseded versions of a key live in higher levels until compaction removes them. At any moment
the disk holds roughly 1.1–1.5× the live data. Meridian measures 1.23× on the attribution job,
which is the number used in
[01](01-requirements-and-the-capacity-model.md) Section 8.

Consequence: **size local disk at 2× live state**, not 1.23×, because compaction itself needs
scratch space to write the merged output before deleting the inputs.

## 4. Deriving state size

The formula is always the same:

```
state = (number of keys)
      × (windows or entries open per key)
      × (bytes per entry, serialized)
      × (RocksDB space amplification ≈ 1.23)
```

and every state incident is one of those four being larger than assumed. Worked for Meridian's
four largest stateful jobs:

### `corridor-ad-attribution` — 4.2 TB

```
keys:              impression_id, all impressions in a 24 h window
                   180,000/s × 86,400 s = 15,552,000,000
entries per key:   1
bytes per entry:   220 B (impression attributes needed for attribution)
raw:               3.42 TB
× 1.23:            4.21 TB
```

### `corridor-sessionize` — 3.92 GB

```
keys:              member_id, active in a 30-minute gap window
                   ~14,000,000
entries per key:   1 session accumulator
bytes per entry:   280 B
raw:               3.92 GB
× 1.23:            4.82 GB
```

### `waypoint-trip-state` — 1.1 GB

```
keys:              trip_id, concurrent active trips: 260,000
entries per key:   1 trip state machine + a bounded path of last 120 positions
bytes per entry:   260,000 × (180 B state + 120 × 32 B path) = 260,000 × 4,020 B
raw:               1.05 GB
× 1.23:            1.29 GB
```

### `riverbend-order-state` — 68 MB

```
keys:              order_id, orders not yet terminal
                   240,000 orders/hour × ~2 h median lifecycle = 480,000
entries per key:   1
bytes per entry:   116 B
raw:               55.7 MB
× 1.23:            68.5 MB
```

Note the spread: 4.2 TB against 68 MB, on the same platform, for jobs that look structurally
similar. **The difference is entirely `keys × retention`**, and that is where to look first when
a state estimate surprises you.

## 5. Key groups and the rescaling constraint you set once

Flink does not assign keys to subtasks directly. It assigns keys to **key groups**, and key
groups to subtasks:

```
key_group = murmur3(key) % maxParallelism
subtask   = key_group × parallelism ÷ maxParallelism
```

`maxParallelism` defaults to 128 for small jobs and `min(1.5 × parallelism rounded up to a power
of 2, 32768)` otherwise. **It is fixed at the job's first run and cannot be changed without
discarding state.**

Three consequences:

**You can never scale above `maxParallelism`.** A job started with the default 128 can never run
at parallelism 200, ever, without a full state rebuild.

**Parallelism should divide `maxParallelism` evenly**, or subtasks get unequal numbers of key
groups. With `maxParallelism=128` and `parallelism=48`, some subtasks get 3 key groups and some
get 2 — a 50% skew, permanently, for no reason.

**A very large `maxParallelism` costs a little.** Each key group has metadata in the checkpoint;
32,768 key groups means 32,768 entries per stateful operator. It is not free but it is small.

The obvious choice is a power of two — 1024 — and it is the wrong one. 1024's only divisors are
powers of two, so the useful parallelisms are 2, 4, 8, 16, 32, 64, 128, 256, 512. If you want a
job at parallelism 320, `1024 ÷ 320 = 3.2`: 192 subtasks get 3 key groups and 128 get 4, a 33%
skew that never goes away.

The correct choice is a number with many divisors, exactly as for partition counts (`T-02`).
**Meridian uses 1440** (`2⁵ × 3² × 5`), which divides evenly by 2, 3, 4, 5, 6, 8, 9, 10, 12, 15,
16, 18, 20, 24, 30, 32, 36, 40, 45, 48, 60, 72, 80, 90, 96, 120, 144, 160, 180, 240, 288, 360,
480, 720 and 1440 — thirty-four usable parallelisms against nine.

This is why `corridor-ad-attribution` runs at parallelism **360** rather than the 320 you would
naively pick from its core count: 360 divides 1440 and 320 does not.

This is a small, fiddly, one-time decision that you cannot revisit, and getting it wrong costs
you a permanent skew or a permanent ceiling. Write it down in the job template.

## 6. Scenarios

### K-01 — State grows forever because nothing removes it

**What you see.** A job's state grows linearly from launch. At 90 days it is 400 GB, checkpoints
take 8 minutes, and the job is one bad deploy away from an unrecoverable restart.

**Mechanism.** The job keeps per-key state and no key is ever removed. Typical instances:

- A deduplication set of "event IDs seen" with no expiry.
- A `MapState` of "last value per SKU" for SKUs that were delisted years ago.
- A `ValueState` keyed by `session_id` where sessions end but the state is not cleared.

The last is the most common: developers clear state at the *expected* end of an entity's life
and never handle the case where that end does not arrive.

**Confirm it.**

```
flink_taskmanager_job_task_operator_rocksdb_estimate_live_data_size
```

plotted over weeks. Linear growth with no plateau is unmistakable. If you do not have weeks of
history for this metric, that is the first thing to fix.

**Recover.** There is no online way to purge state. Options, all bad:

- Deploy a version that clears state on access for keys meeting a condition, and wait for every
  key to be accessed — which for dead keys never happens.
- Deploy a version with a `KeyedProcessFunction` that registers a timer on every key access and
  clears on fire; existing keys with no pending timer still never fire.
- Discard state and rebuild from the log. Only possible within retention.

**Prevent.** **Every piece of keyed state has a TTL or an explicit deletion path, decided at
design time.** Flink's state TTL:

```java
StateTtlConfig ttl = StateTtlConfig
    .newBuilder(Time.hours(26))
    .setUpdateType(UpdateType.OnCreateAndWrite)
    .setStateVisibility(StateVisibility.NeverReturnExpired)
    .setTtlTimeCharacteristic(TtlTimeCharacteristic.EventTime)   // not the default
    .cleanupInRocksdbCompactFilter(1000)
    .build();
```

Four of those five lines are non-default and all four matter:

- `OnCreateAndWrite` versus `OnReadAndWrite`: whether reading refreshes the TTL. For a dedupe
  set you want `OnCreateAndWrite` — an ID seen once should expire 26 hours later regardless of
  how often you check for it.
- `NeverReturnExpired`: without it, expired-but-not-yet-cleaned state is still returned, so your
  TTL is advisory.
- `EventTime`: the default is processing time, which breaks replay determinism (`E-10`).
- `cleanupInRocksdbCompactFilter`: without an explicit cleanup strategy, expired state is removed
  only when read. Dead keys are never read, so they are never removed, and **the TTL does
  nothing for exactly the keys it exists for.** This is the single most-missed line.

### K-02 — Unbounded key space

**What you see.** State grows and TTL does not help, because the *number of distinct keys* grows
without bound.

**Mechanism.** The key includes something high-cardinality and ever-increasing: a request ID, a
timestamp, a URL with query parameters, a session ID. Every record introduces a new key, so even
with a TTL the state holds `arrival rate × TTL` keys.

```
keyed by request_id, TTL 24 h, 1,850,000 records/s
  keys held: 1,850,000 × 86,400 = 159.8 billion
  at 80 B each: 12.8 TB
```

The TTL is working perfectly and the state is still 12.8 TB.

**Confirm it.** Distinct keys over a window, computed from the source stream rather than from
the job. If distinct keys ≈ record count, the key space is unbounded.

**Prevent.** Design the bound before you write the job:

- **Bound by entity, not by event.** Key by `member_id` (100 million, bounded) rather than
  `event_id` (unbounded).
- **If you must key by an event, bound the retention hard** and accept the size — which is
  exactly what `corridor-ad-attribution` does, and its 4.2 TB is the honest price of a 24-hour
  attribution window.
- **Use a probabilistic structure** where exactness is not required. A deduplication check over
  159.8 billion IDs in a Bloom filter with a 0.1% false-positive rate is 1.44 bits per element:
  `159.8e9 × 1.44 ÷ 8 = 28.8 GB` instead of 12.8 TB — a factor of 444, in exchange for
  incorrectly discarding 0.1% of records as duplicates. Whether that trade is acceptable is a
  product question, and it should be asked rather than assumed either way.

### K-03 — One hot key saturates one subtask

**What you see.** A job with parallelism 360 has one subtask at 100% busy and backpressuring the
whole job; the other 359 are at 30%.

**Mechanism.** Keyed state processes one key at a time within a subtask. A key receiving 40,000
records a second must be processed by one thread, and if that thread can do 14,000 a second, the
key is a hard bottleneck. No amount of parallelism helps, because the key cannot be split.

From `I-05`'s Corridor distribution: the largest member account produces 474 events/s, which a
single subtask absorbs easily. The problem is worse with different keys:

```
keyed by surface:  "feed" is 61% of all interactions
                   1,850,000 × 0.61 = 1,128,500 events/s on one key
                   one subtask can do 14,000/s
                   → 80× over capacity
```

**Confirm it.** Subtask busyness distribution. One subtask pinned at 100% with the rest low is
unambiguous. Then find the key: a sampling counter of key frequencies inside the hot subtask, or
a separate lightweight job that counts keys on the source stream.

**Recover.** Nothing in the moment except shedding.

**Prevent.** **Two-phase aggregation with a salted key.** Split the hot key artificially:

```java
// phase 1: partial aggregation over (key, salt)
stream
  .keyBy(e -> new Tuple2<>(e.getSurface(), ThreadLocalRandom.current().nextInt(64)))
  .window(TumblingEventTimeWindows.of(Time.seconds(10)))
  .aggregate(new PartialCount())

// phase 2: combine the 64 partials
  .keyBy(t -> t.surface)
  .window(TumblingEventTimeWindows.of(Time.seconds(10)))
  .aggregate(new CombineCount());
```

Phase 1 runs at 64× the parallelism for the hot key, and its output is 64 records per 10 seconds
per surface — trivial for phase 2.

Three notes:

- **Only works for decomposable aggregations.** Sum, count, min, max, and sketches. Not for
  "the last value" or anything order-dependent.
- **The salt must be random per record, not derived from the record**, or the same records
  concentrate again.
- **It adds a window.** Phase 1 must buffer before phase 2 can combine, which adds the window
  length to your latency. Ten seconds is usually acceptable; if it is not, use a smaller
  phase-1 window and accept more phase-2 volume.

**Adaptive salting** is the refinement: salt only keys observed to be hot, using a
count-min-sketch of key frequencies maintained in each subtask. Cold keys pay nothing; hot keys
get spread. It is roughly eighty lines and it is worth it on a job where the hot set changes.

### K-04 — Key skew that is invisible until a rescale

**What you see.** A job runs fine at parallelism 240 and becomes badly skewed at 360.

**Mechanism.** Key groups. Two moderately-hot keys happened to land in different key groups that
were assigned to different subtasks at parallelism 240, and land in key groups assigned to the
*same* subtask at 360. The skew was always in the key distribution; the assignment hid it.

**Prevent.** Nothing reliable at the assignment level — you do not control the hash. What you can
do is measure key skew directly on the source stream, independently of any job, and know your
worst keys before they surprise you. Meridian runs a small job per Tier 1 stream that emits the
top 100 keys by frequency every minute.

### K-05 — A single state entry grows unbounded

**What you see.** A checkpoint fails with `java.lang.OutOfMemoryError` during serialization of a
single key's state.

**Mechanism.** A `ListState` or a collection inside a `ValueState` with no bound:

- "All positions in this trip" for a trip that has been running for nine days (`W-01`).
- "All items in this cart" for a cart that a bot has added 400,000 items to.
- "All errors seen for this order" where a retry loop generated 2 million.

**Prevent.** **Bound every collection in state, with an explicit overflow behaviour and a
counter.** Waypoint's trip path keeps the last 120 positions in a ring buffer and a separate
count of total positions; when a consumer needs the full path it reads it from the log, not from
state. The bound and the counter together mean a nine-day trip has the same state footprint as a
nine-minute one and you know it happened.

### K-06 — RocksDB compaction saturates the disk

**What you see.** A job's throughput drops 40% with no change in input. Disk write utilisation is
at 100%. Checkpoint duration triples.

**Mechanism.** Write amplification (Fact 2). Logical writes of 39.6 MB/s became physical writes
of 792 MB/s, and something pushed it over the NVMe's capacity — more state, a deeper level
structure, or a second job scheduled onto the same node.

**Confirm it.**

```
rocksdb_compaction_pending
rocksdb_num_running_compactions
rocksdb_actual_delayed_write_rate       ← nonzero means RocksDB is throttling you
node_disk_write_bytes_total
```

`actual_delayed_write_rate` is the decisive one: RocksDB deliberately slows writes when level 0
has too many files, and that throttle is invisible from Flink's own metrics.

**Recover.** Reduce write amplification:

- **Increase `write_buffer_size`** so fewer, larger memtables are flushed. 64 MB → 256 MB cuts
  level-0 file count 4×.
- **Increase `max_write_buffer_number`** so flushes can queue.
- **Use level compaction with a larger `max_bytes_for_level_base`**, which reduces the number of
  levels and therefore the amplification.
- **Consider universal compaction** for write-heavy, read-light state: much lower write
  amplification, much higher space amplification. For a dedupe set that is mostly written and
  rarely read, this is a good trade.

**Prevent.** Size local disk write bandwidth from logical write rate × measured amplification,
not from logical write rate. And measure amplification per job rather than assuming a number —
it varies from 5× to 40× depending on the key distribution and the access pattern.

### K-07 — The serializer is the bottleneck

**What you see.** A job is CPU-bound and profiling shows 60% of the time in
`KryoSerializer.serialize`.

**Mechanism.** Flink falls back to Kryo for types it cannot analyse — anything that is not a POJO
by its rules (a public no-arg constructor, public or getter/setter-accessible fields), a Scala
case class, a Tuple, or a registered Avro type. Kryo is roughly 5–10× slower than Flink's
generated serializers and produces larger output.

Worse: **Kryo without registered classes writes the full class name with every instance.** A
`HashMap<String, MyEvent>` in state writes `com.corridor.tracking.MyEvent` — 33 bytes — per
entry.

**Confirm it.** Enable the fallback warning at startup:

```java
env.getConfig().disableGenericTypes();   // fail instead of falling back to Kryo
```

Run the job; anything that fails is using Kryo. Run this in CI for every job.

**Prevent.**

- **Use Avro-generated classes for state types**, the same ones used on the wire. Flink has a
  native Avro serializer, it is fast, and it gives you schema evolution for state
  (`C-08`) as a free consequence.
- **Or use POJOs** that satisfy Flink's rules, and verify with `disableGenericTypes()`.
- **Never store a Java `Serializable` object in state.** Java serialization in a hot path is
  20–50× slower than the alternatives.

### K-08 — State schema changed and the job will not restore

**What you see.** A deploy fails with
`StateMigrationException: The new state serializer is not compatible`.

**Mechanism.** A field was added to a class stored in state. Flink must read the old bytes with
the new class, and whether it can depends entirely on the serializer:

| Serializer | Add a field | Remove a field | Change a type |
|---|---|---|---|
| Avro | Yes, with a default | Yes | Widening only |
| POJO | Yes | Yes | No |
| Kryo | **No** | **No** | **No** |
| Java serialization | Only with a matching `serialVersionUID` | Fragile | No |

Kryo's row is the reason `K-07`'s advice matters beyond performance: **a job using Kryo for state
cannot evolve its state schema at all.** Every change means discarding state.

**Recover.** Discard state and rebuild, or write a custom migration using the State Processor
API — a batch Flink job that reads a savepoint, transforms it, and writes a new one. It works
and it is a day of effort.

**Prevent.** Avro for state types, and a CI check that loads a savepoint from the previous
release with the new job's serializers before allowing a deploy. That check is perhaps two hours
to build and it prevents the single most common failed streaming deploy.

### K-09 — State on the heap and the garbage collector

**What you see.** A job with 6 GB of heap state has 4-second GC pauses every 90 seconds.
Checkpoints time out during pauses, the job restarts, and after restart the same thing happens.

**Mechanism.** The heap backend holds state as live objects. A 6 GB live set means every full GC
must trace 6 GB of object graph. G1's concurrent phases help, but a mixed collection over a large
live set is slow, and the pause blocks the task thread — which blocks the checkpoint barrier,
which fails the checkpoint.

**Prevent.** RocksDB above 1 GB per slot. The state moves off-heap, the JVM keeps a small heap
with short pauses, and the checkpoint becomes incremental. This is the primary reason the
heap backend is not the default choice.

### K-10 — State that should have been a lookup

**What you see.** A job holds 3.1 TB of state that is a near-exact copy of a DynamoDB table.

**Mechanism.** Somebody needed enrichment data in the job and materialised the whole table into
keyed state via a CDC stream. It works, it is fast, and it costs 3.1 TB of local disk plus the
recovery time that comes with it.

**Prevent.** Ask whether the data needs to be *in* the job. The comparison:

| | Materialised in state | Async lookup |
|---|---|---|
| Latency per record | 4 µs | 2–8 ms, amortised to ~0.05 ms with async I/O |
| Throughput per core | 14,000/s | 22,000/s (bounded by capacity ÷ latency, not CPU) |
| State | 3.1 TB | 0 |
| Recovery | Must restore 3.1 TB | Instant |
| Consistency | As of the last CDC record | As of now |
| Failure mode | None (self-contained) | The remote store's availability is yours |
| Cost | Local NVMe + checkpoint storage | DynamoDB read capacity |

**The async lookup is faster per core**, which surprises people. Async I/O with a capacity of 200
in-flight requests at 8 ms p99 supports `200 ÷ 0.008 = 25,000` requests/s per subtask — the
latency does not appear in the throughput calculation at all, only the concurrency does.

Materialise into state when: the lookup rate is very high relative to the update rate, the remote
store cannot take the load, or you need point-in-time consistency with the stream (a temporal
join — see [10](10-joins-and-enrichment.md)). Otherwise look it up.

### K-11 — Two jobs sharing one machine, one starving the other

**What you see.** A small job's latency triples whenever a large job on the same TaskManager
compacts.

**Mechanism.** Slots on a TaskManager share the JVM, the managed memory pool, the local disk, and
the page cache. RocksDB's block cache is per-slot by default in recent Flink versions, but disk
bandwidth is not, and compaction is not.

**Prevent.** **Slot sharing groups and separate TaskManagers for heavy-state jobs.** Meridian
runs the 21 large-state jobs on a dedicated node pool with 4 slots per node instead of 16,
giving each more disk bandwidth and block cache. It costs more nodes and it removes an entire
class of unexplained latency.

### K-12 — Queryable state, and why not to

**What you see.** A team wants to read a job's state directly from an API instead of writing it
to a sink.

**Mechanism.** Flink had a Queryable State feature; it was deprecated in 1.18 and removed. The
reasons generalise beyond Flink:

- It makes the job's availability the query path's availability. A job restart is a query outage.
- It makes rescaling a query outage, because keys move.
- It has no access control, no rate limiting, no query planner and no caching.
- It couples a system optimised for sequential throughput to a random-access read workload.

**Prevent.** Write state you want to query into a store designed for querying — a key-value
store, an OLAP engine, a cache. The extra hop costs milliseconds and buys you an independent
availability and scaling story. [13-serving-the-results.md](13-serving-the-results.md) covers the
choices.

## 7. The state design checklist

Run this before writing a stateful operator. It is eight questions and it prevents most of the
scenarios above.

1. **What is the key, and is its cardinality bounded?** If unbounded, what bounds the retention?
2. **How many entries per key, and is that bounded?** Every collection needs a maximum.
3. **How many bytes per entry, serialized?** Measure, do not guess — serialization overhead is
   30–60% for small values.
4. **Multiply them, times 1.23.** Is the result acceptable per slot at your intended
   parallelism?
5. **What removes state?** A TTL (with an explicit cleanup strategy and event-time
   characteristic), an explicit clear, or a window close. "The key stops appearing" is not an
   answer.
6. **What serializer?** Avro or POJO. Run `disableGenericTypes()` in CI.
7. **What is `maxParallelism`?** Set it explicitly, to a number with many divisors, and pick
   parallelisms that divide it.
8. **What is the restore time at this size?** `state ÷ (parallelism × 90 MB/s)` plus 60 seconds.
   If it is over ten minutes, consider task-local recovery (`C-06`) before you need it.

## What to take away

1. **Use `MapState`, not `ValueState<HashMap>`.** RocksDB addresses map entries individually;
   the alternative serializes the entire map on every access, which on a 400-entry map is a
   factor of 140.
2. **RocksDB over heap above about 1 GB per slot** — not for the latency, which is worse, but
   for incremental checkpoints and for keeping the state off the JVM heap.
3. **Five RocksDB facts matter:** writes are cheap and reads may not be; compaction amplifies
   disk writes 10–30×; compaction steals CPU so throughput is not constant; the block cache
   determines whether reads hit disk; and the disk holds ~1.23× the live data.
4. **State = keys × entries-per-key × bytes × 1.23**, and every incident is one of those being
   larger than assumed. The spread on one platform is 68 MB to 4.2 TB for structurally similar
   jobs.
5. **Set `maxParallelism` explicitly, to a number with many divisors.** It is fixed forever at
   first run, it caps your scaling, and a poor choice gives permanent 33% subtask skew.
6. **Every piece of state needs a TTL with `cleanupInRocksdbCompactFilter` and
   `TtlTimeCharacteristic.EventTime`.** Without the cleanup strategy, expired state is removed
   only on read — so dead keys, which are never read, are never removed.
7. **An unbounded key space defeats TTL.** Keying by `event_id` with a 24-hour TTL still holds
   159.8 billion keys. Key by entity, or bound the retention deliberately, or use a Bloom filter
   and accept a named false-positive rate.
8. **A hot key cannot be parallelised.** Two-phase aggregation with a random salt is the fix;
   it works only for decomposable aggregations and it costs you a window of latency.
9. **Kryo in state means your job can never evolve its state schema.** Use Avro, run
   `disableGenericTypes()` in CI, and test a savepoint restore against the previous release
   before every deploy.
10. **An async lookup is often faster per core than materialised state** — 22,000/s against
    14,000/s — because async I/O's throughput depends on concurrency, not latency. Materialise
    only when you need point-in-time consistency or the remote store cannot take the load.
11. **Do not query a job's state directly.** It couples your query availability to your job's
    lifecycle. Write it to a store built for reading.
