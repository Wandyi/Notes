# Retention, Compaction, and Schema

Everything so far has been about getting records into the log and out of it. This doc is about
what happens to them while they sit there: when Kafka deletes them, when it rewrites them, and
what happens when their meaning changes underneath a consumer.

These failures share a character that makes them harder than the earlier ones. They are **slow**.
A rebalance storm announces itself in minutes; a compaction problem takes three weeks to fill a
disk, and a schema problem surfaces the first time someone replays history. By the time you see
them, the decision that caused them was made by someone who has forgotten making it.

## How deletion actually works

Retention is not a background process that scans records and deletes expired ones. It operates on
**segments**, and understanding that explains most of the surprises.

A partition's log is a sequence of segment files. Exactly one is **active** — currently being
appended to. A segment is closed and a new one started when either:

- it reaches `segment.bytes` (**1 GB** by default), or
- `segment.ms` (**7 days** by default) has elapsed since the segment was created.

Every `log.retention.check.interval.ms` (**5 minutes**) a background thread examines each
partition and deletes whole segments that are eligible. A segment is eligible when its
**largest record timestamp** is older than `retention.ms`, or when deleting it would bring the
partition under `retention.bytes`.

Three properties follow, and each one is a scenario below:

1. **The active segment is never deleted.** Data cannot leave until its segment closes.
2. **Deletion is per segment, so it is coarse.** A 1 GB segment goes all at once, meaning the
   actual retained data is always somewhat more than `retention.ms` worth.
3. **Time is measured by record timestamps, which producers control** — not by arrival time.

---

## Failure catalogue

| Class | The question it answers | Scenarios |
|---|---|---|
| **A. Deletion happening when it should not** | Where did my data go? | `T-01` … `T-03` |
| **B. Deletion not happening when it should** | Why is the disk full? | `T-04` … `T-05` |
| **C. Compaction** | The topic is compacted. Why does it not look compacted? | `T-06` … `T-09` |
| **D. The records survived; their meaning did not** | Nothing was deleted and nothing works | `T-10` … `T-11` |

---

## Class A — deletion happening when it should not

### T-01 · Retention deleted data a lagging consumer still needed

**What you see.** A consumer that was behind comes back and logs
`OffsetOutOfRangeException: Fetch position FetchPosition{offset=8390112...} is out of range for
partition orders.created-7`. Then, depending on `auto.offset.reset`, it either silently skips to
the end or reprocesses everything.

**Mechanism.** Retention and consumption are in a race, and retention always wins because it does
not know consumption exists. Kafka deletes on a timer regardless of whether every group has read
the data — that is the design (doc 00), and it is why Kafka scales to many independent consumers,
but it means **your retention must exceed your worst-case consumer outage**.

Riverbend's numbers make the exposure concrete:

| Topic | Retention | Longest survivable consumer outage |
|---|---|---|
| `orders.created` | 72 h | 3 days |
| `payments.settled` | 7 d | 7 days |
| `clickstream.events` | 24 h | **1 day** |

One day is not much. A consumer broken on Friday evening and fixed Monday morning has lost
between 36 and 60 hours of clickstream events, permanently, and by default
(`auto.offset.reset=latest`) it will resume silently as though nothing happened.

⚠️ The interaction with doc 04 (`C-10`) is what makes this a silent failure rather than a loud
one. The `OffsetOutOfRangeException` is caught by the client library, which applies
`auto.offset.reset` and continues. Unless you set `auto.offset.reset=none`, the only trace is a
log line that nobody reads.

**Confirm it.** Compare each group's committed offset against the partition's earliest available
offset. When the committed offset is lower, the data it wanted is gone:
```bash
# Earliest available offset per partition
kafka-get-offsets.sh --bootstrap-server $BS --topic orders.created --time -2

# The group's committed offsets
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group order-processor-group
```
The proactive version of the same check is a **time-to-expiry** signal: how long a consumer has
before its position ages out of retention. That is the alert you want, because it fires while
there is still time to act:
```promql
# Hours of margin before this group's position is deleted
(72 * 3600 - max(kafka_consumer_lag_seconds{consumergroup="order-processor-group"})) / 3600 < 12
```

**Recover.** The records are gone. Recovery means an upstream source — replaying from
`orders-db`, or from the producer's outbox table (doc 05, `D-05`). This is a concrete reason the
outbox pattern is worth the effort: it converts "retention expired" from unrecoverable to
inconvenient.

**Prevent.** Set retention from the **maximum tolerable consumer outage**, not from a storage
budget, then check the storage cost and negotiate if it is too high. Doing it the other way round
produces a number with no relationship to your recovery requirements. And alert on time-to-expiry
rather than on lag alone.

### T-02 · Retention that appears not to work

**What you see.** A topic with `retention.ms=3600000` (one hour) still holding four days of data.

**Mechanism.** The active segment is never deleted, so data cannot expire until its segment
closes — which happens at `segment.bytes` (1 GB) **or** `segment.ms` (7 days), whichever comes
first. On a low-traffic topic, neither happens quickly.

Take a topic receiving one record a minute at 500 bytes:

```
to fill a 1 GB segment:  1,073,741,824 ÷ (500 bytes/min) = 2,147,484 minutes ≈ 4 years
so the segment rolls on segment.ms instead:                                     7 days
maximum age of retained data:  7 days (to close the segment) + 1 hour (retention)
```

The topic holds a week of data despite asking for an hour, and nothing is broken. This bites
hardest on exactly the topics where short retention was chosen for a reason — a topic holding
personal data with a deletion commitment, or a control topic someone wants kept small.

**Confirm it.** List segment files and their modification times:
```bash
# From a broker host, for one partition
ls -la /var/kafka-logs/orders.created-7/ | head -20
# Many .log files = segments rolling normally. One large recent one = it has not rolled.
```

**Recover and prevent.** Set `segment.ms` on any topic whose retention is short relative to its
traffic. A reasonable rule is that a segment should close at least as often as the retention
period:
```bash
kafka-configs.sh --bootstrap-server $BS --alter --entity-type topics \
  --entity-name control.commands --add-config segment.ms=600000     # 10 minutes
```
⚠️ Do not apply short `segment.ms` to high-traffic topics. `clickstream.events` at
40.8 MB/s would roll a segment every 25 seconds at `segment.ms=600000`— no, it rolls on
`segment.bytes` long before that, which is fine. The hazard is the reverse: a small `segment.ms`
on a topic that also has small `segment.bytes` produces enormous numbers of tiny files, which
costs file descriptors (doc 01, `B-08`) and index memory. Segments are a per-partition resource;
tens of thousands of them across a broker is a problem.

### T-03 · Retention driven by producer clocks

**What you see.** A backfill publishes historical records and they vanish within minutes. Or the
opposite: a topic never expires anything and grows without bound.

**Mechanism.** `message.timestamp.type` defaults to **`CreateTime`**, meaning the timestamp used
for retention is the one the **producer** set — usually the producer's wall clock at `send()`,
but freely settable by the application.

Two failure directions:

- **Timestamps in the past.** A backfill replaying two-year-old orders with their original
  timestamps produces records whose segment is immediately older than any sane retention. The
  segment closes and is deleted on the next retention check, typically within five minutes. The
  backfill "succeeded" — every `send()` was acknowledged — and the data is gone.
- **Timestamps in the future.** A producer with a broken clock, or one that mistakenly sets
  milliseconds where seconds were expected, stamps records years ahead. Those segments are never
  eligible for deletion, and the partition grows forever.

**Confirm it.** Read timestamps directly and compare them to now:
```bash
kafka-console-consumer.sh --bootstrap-server $BS --topic orders.created \
  --from-beginning --max-messages 5 --property print.timestamp=true
```

**Prevent.** Two mechanisms, and the choice between them is a real design decision:

- **`message.timestamp.type=LogAppendTime`** on the topic makes the broker overwrite the
  timestamp with its own clock. Retention then reflects arrival time, which is what most people
  assume. The cost is that you lose the event-time timestamp, which matters if anything downstream
  does windowed processing on it — so this is wrong for `clickstream.events` and right for most
  operational topics.
- **Bound the skew** with `message.timestamp.before.max.ms` and
  `message.timestamp.after.max.ms` (Kafka 3.6+; earlier versions have the single
  `message.timestamp.difference.max.ms`). The broker rejects records whose timestamp is too far
  from its own clock. This keeps event time and catches broken producers loudly, which is usually
  the better trade.

⚠️ If you are backfilling historical data with original timestamps, set the destination topic's
retention *before* the backfill, and check it afterwards. This one has destroyed a lot of
carefully prepared migrations.

---

## Class B — deletion not happening when it should

### T-04 · `retention.bytes` is per partition

**What you see.** A topic configured with `retention.bytes` far exceeding the intended cap.

**Mechanism.** `retention.bytes` limits **each partition**, not the topic. Someone intending to
cap `clickstream.events` at 500 GB sets `retention.bytes=536870912000` and gets:

```
500 GB per partition × 200 partitions × RF 2 = 200 TB of cluster storage
```

against a cluster with 12 TB total. The setting is not enforced as intended and the operator has
no idea, because the value they typed looks right.

The correct calculation runs the other way — start from the cluster budget:

```
target for this topic across the cluster:  7,000 GB
÷ replication factor 2                   = 3,500 GB of distinct data
÷ 200 partitions                         =    17.5 GB per partition
retention.bytes                          = 18,790,481,920
```

**Prevent.** Prefer `retention.ms` as the primary control, because time is what people reason
about and what recovery requirements are stated in. Use `retention.bytes` as a **safety cap**
against a traffic surge, computed per partition, and re-derive it whenever the partition count
changes — which nobody remembers to do, so write it into the partition-change procedure.

⚠️ When both are set, **whichever triggers first wins**. A `retention.bytes` cap that is
accidentally tight silently shortens your retention below the documented value, which turns into
`T-01`.

### T-05 · Deleting a topic did not free the disk

**What you see.** A large topic was deleted and disk usage did not change.

**Mechanism.** Topic deletion is asynchronous and has several stages. The controller marks the
topic for deletion; brokers rename each partition directory with a `-delete` suffix; the files
are removed only after `log.segment.delete.delay.ms` (60 seconds). That accounts for a minute,
not a day.

The longer stalls come from two places:

- **A broker was down when you deleted.** Deletion completes on a broker only when it is running.
  A broker that returns a week later still holds the data, and its disk usage is unchanged until
  it processes the deletion.
- **Open file handles.** A segment file being read by a consumer is unlinked but not freed until
  the handle closes, so `df` and `du` disagree. This resolves on its own.

**Confirm it.**
```bash
# From a broker host
ls -d /var/kafka-logs/*-delete 2>/dev/null
df -h /var/kafka-logs; du -sh /var/kafka-logs    # a large gap means unlinked-but-open files
```

**Prevent.** Do not delete topics during an incident in which brokers are down — it produces a
deferred surprise. Verify the space was actually reclaimed rather than assuming it.

---

## Class C — compaction

### How compaction works

**Log compaction** (`cleanup.policy=compact`) keeps the **most recent value for each key** and
deletes older values for the same key. It turns a topic into a durable, replayable snapshot of
current state — a table that happens to be a log. `inventory.adjustments` and `catalog.changes`
use it, as does `__consumer_offsets`.

The **log cleaner** is a set of background threads (`log.cleaner.threads`, default 1) that, for
each eligible partition:

1. Splits the log into a cleaned **tail** and a dirty **head**. The head always includes the
   active segment.
2. Builds an in-memory map of key to latest offset for the head, in a shared
   `log.cleaner.dedupe.buffer.size` buffer (128 MB by default).
3. Rewrites the tail, retaining only records whose offset matches the latest for that key.

A partition becomes eligible when its **dirty ratio** — dirty bytes divided by total bytes —
exceeds `min.cleanable.dirty.ratio`, which defaults to **0.5**.

Three consequences, each a scenario below: the active segment is never compacted; a topic can be
twice its compacted size and that is normal; and if the cleaner stops, nothing tells the producer
or the consumer.

### T-06 · Compaction never touches the active segment

**What you see.** A consumer reading a compacted topic from the beginning receives several values
for the same key, at the end of the log. Code that assumed one record per key misbehaves.

**Mechanism.** The active segment is excluded from compaction, because it is being appended to.
So the newest data — up to 1 GB per partition, or whatever `segment.bytes` is — always contains
every update, uncompacted.

For `inventory.adjustments` at 400-byte records and 1 GB segments, that is up to **2.7 million
uncompacted records per partition** at the head of the log, across 48 partitions.

⚠️ This means **"a compacted topic has one record per key" is false**, and any consumer that
relies on it is wrong. A compacted topic guarantees only that *the latest value for a key is
present*; it never guarantees that older values are absent. Consumers must apply
last-write-wins, which for a single partition means simply processing in offset order.

**Prevent.** Write consumers of compacted topics to be last-write-wins by construction. If a
bootstrapping consumer must not see stale values at all, it cannot get that from compaction — it
needs a snapshot mechanism.

### T-07 · Compaction has not run because the dirty ratio was not met

**What you see.** A compacted topic much larger than the number of distinct keys implies. Disk
growing steadily.

**Mechanism.** With `min.cleanable.dirty.ratio=0.5`, a partition is only cleaned once half of it
is dirty. A topic with a low update rate relative to its size takes a long time to reach that,
and in the meantime it simply grows.

There is also a subtler version. If the same small set of keys is updated repeatedly, the dirty
ratio rises quickly and compaction is frequent — fine. If updates are spread thinly over a very
large key space, the ratio rises slowly and compaction is rare, so the topic stays near twice its
compacted size indefinitely.

**Confirm it.**
```promql
# Has the cleaner run recently at all?
kafka_log_logcleanermanager_time_since_last_run_ms

# Compare the topic's actual size to the distinct key count you expect
```

**Recover and prevent.** Lower `min.cleanable.dirty.ratio` (0.1 makes compaction much more
eager, at the cost of more cleaner I/O), and set **`max.compaction.lag.ms`**, which forces
compaction after a bounded time regardless of the dirty ratio:
```bash
kafka-configs.sh --bootstrap-server $BS --alter --entity-type topics \
  --entity-name inventory.adjustments \
  --add-config min.cleanable.dirty.ratio=0.2,max.compaction.lag.ms=3600000
```
⚠️ `max.compaction.lag.ms` (Kafka 2.3+) is the setting to use when compaction is not merely a
storage optimisation but a **deletion guarantee** — for example, when a tombstone must physically
remove personal data within a contractual window. Without it, you cannot state any bound on when
a tombstone takes effect, because the dirty ratio might not be reached for months.

### T-08 · A tombstone deleted before a slow consumer saw it

**What you see.** A key deleted from `inventory.adjustments` months ago reappears in a
newly-bootstrapped consumer's state, and stays there forever.

**Mechanism.** Deletion in a compacted topic is expressed as a **tombstone**: a record with the
key and a `null` value. Consumers interpret it as "remove this key."

Tombstones cannot be kept forever — that would defeat compaction — so they are retained for
`delete.retention.ms`, **24 hours by default**, after which compaction removes them too.

Now consider a consumer that bootstraps by reading the whole compacted topic, and takes 30 hours
to do it because the topic is large:

1. At hour 0 it starts reading from the beginning.
2. At hour 5 a tombstone for `sku-88431` is written to the head of the log.
3. At hour 29 the cleaner runs, and the tombstone is older than 24 hours, so it is removed.
4. At hour 30 the consumer reaches the head. **The tombstone is not there.** It never saw the
   value being deleted, and it never saw the deletion. Its state contains a key that does not
   exist.

The consumer is now permanently wrong about that key, with nothing in any log to indicate it.

⚠️ This is the least-known hazard in this doc and it is a real correctness bug in a very common
pattern — bootstrapping state from a compacted topic. The 24-hour default is generous enough that
most consumers never hit it, and completely inadequate for a large topic or a slow consumer.

**Prevent.** Set `delete.retention.ms` longer than your slowest full-topic read, with margin:
```bash
# Measure a full bootstrap first, then multiply
kafka-configs.sh --bootstrap-server $BS --alter --entity-type topics \
  --entity-name inventory.adjustments --add-config delete.retention.ms=604800000   # 7 days
```
Seven days costs almost nothing — tombstones are tiny — and removes the class of bug entirely.
Measure how long a full bootstrap actually takes rather than assuming, because it grows with the
topic.

### T-09 · The log cleaner died and every compacted topic grew

**What you see.** Multiple compacted topics growing steadily, including `__consumer_offsets`.
Disks filling across all brokers at a similar rate. No errors from producers or consumers.

**Mechanism.** The log cleaner runs on its own threads. If a thread encounters an unrecoverable
error — historically a corrupt record, or a partition whose key set exceeds what the dedupe buffer
can handle — **the thread dies and is not restarted**. Compaction stops for every compacted
partition that thread was responsible for, and there is no signal on the produce or consume path.

The consequences compound:

- Compacted topics grow at their full uncompacted rate.
- `__consumer_offsets` is compacted, and on a cluster with many groups committing every five
  seconds it is one of the busiest topics you have. Uncompacted, it grows quickly (doc 08,
  `S-07` derives the rate).
- Disk fills, brokers take log directories offline (doc 01, `B-02`), and the incident that
  finally pages you is a storage incident several weeks downstream of the actual failure.

**Confirm it.** These are the metrics nobody has until the first time this happens:
```promql
# Should be well under the check interval. A large or rising value means the cleaner is not running.
kafka_log_logcleanermanager_time_since_last_run_ms > 600000

# Partitions the cleaner has given up on, and how much data they hold
kafka_log_logcleanermanager_uncleanable_partitions_count > 0
kafka_log_logcleanermanager_uncleanable_bytes
```
```bash
grep -iE "cleaner|LogCleaner" /var/log/kafka/server.log | grep -iE "error|exception|shutdown"
```

**Recover.** Restart the affected broker — the cleaner threads start with it and resume. If a
specific partition is uncleanable, it will fail again, and you need to identify it from the log
and deal with the underlying corruption or key-space size. Raising
`log.cleaner.dedupe.buffer.size` and `log.cleaner.threads` addresses the capacity variant.

**Prevent.** Alert on `time_since_last_run_ms` and `uncleanable_partitions_count`. These two
metrics are absent from most Kafka dashboards, and this is the failure that justifies adding
them — it is silent, it is slow, and it ends in a full disk across every broker at once.

### T-10 · A compacted topic with null keys

**What you see.** Producers failing with `RecordTooLargeException`… no — with
`InvalidRecordException: Compacted topic cannot accept message without key`.

**Mechanism.** Compaction identifies records by key. A record with a null key has no identity, so
the broker rejects it outright on a compacted topic. The rejection is immediate and clear, which
makes this the friendliest failure in this doc — the problem is that it usually appears when
someone changes `cleanup.policy` on an existing topic that has been accepting null keys happily
for months.

**Prevent.** Treat `cleanup.policy` as part of a topic's contract, set at creation. Changing a
topic from `delete` to `compact` is a semantic change to what the topic means, not a storage
tweak, and it requires checking that every producer sets a key and every consumer applies
last-write-wins.

---

## Class D — the records survived; their meaning did not

### T-11 · A schema change that broke every consumer

**What you see.** After a producer deploy, consumers across several teams start failing to
deserialise. Or worse, they succeed and produce wrong values.

**Mechanism.** Kafka stores bytes and has no opinion about their structure. The contract between
producer and consumer is entirely external, and if it is not enforced by a schema registry it is
enforced by nothing.

Even with a registry, the **compatibility mode decides who can deploy first**, and this is where
teams get caught:

| Mode | Guarantees | Deployment order | Typical use |
|---|---|---|---|
| `BACKWARD` (**default**) | New schema can read data written with the **previous** schema | **Consumers first**, then producers | Most common |
| `FORWARD` | Old schema can read data written with the new schema | **Producers first**, then consumers | When consumers are many and slow to upgrade |
| `FULL` | Both | Either | Strictest, and most restrictive on changes |
| `NONE` | Nothing | Chaos | Development only |

⚠️ The default is `BACKWARD`, which means **consumers must be upgraded before producers**. A team
that deploys the producer first — the natural instinct, since the producer owns the schema —
breaks every consumer that has not yet been updated. The registry does not prevent this; it only
validates that the schema *could* be read by an updated consumer.

⚠️ A second trap: `BACKWARD` checks the new schema against the **immediately previous version
only**. A consumer reading 72 hours of `orders.created` history may encounter three or four
schema versions, and pairwise compatibility between consecutive versions does not imply
compatibility with all of them. `BACKWARD_TRANSITIVE` checks against every prior version, and it
is what you want whenever consumers read history rather than only the tail — which, for any topic
that can be replayed, is all of them.

What is safe under `BACKWARD`:

- **Adding a field with a default value.** Old data lacks the field; the new reader supplies the
  default.
- **Removing a field that had a default.** New data lacks it; the new reader does not need it.

What is not:

- **Adding a required field** with no default. The new reader cannot read old records.
- **Renaming a field.** This is a remove plus an add, and it breaks in both directions.
- **Changing a type**, including widening that looks harmless. `int` to `long` is backward
  compatible in Avro; `long` to `int` is not; `string` to `int` is never.

**Confirm it.** Test compatibility before deploying, as a build step rather than a habit:
```bash
curl -s -X POST -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  --data @schema-payload.json \
  http://schema-registry:8081/compatibility/subjects/orders.created-value/versions/latest
# {"is_compatible": true}
```

**Prevent.**

- Use a schema registry, set `BACKWARD_TRANSITIVE` for any topic that can be replayed, and make
  the compatibility check a required CI gate rather than an optional step.
- Document the deployment order per topic and put it in the topic's metadata, because "consumers
  first" is not memorable and gets it wrong once per team per year.
- Never remove a field in the same release that stops writing it. Stop writing it, wait for every
  consumer to stop reading it, then remove it — the two-phase discipline that makes schema changes
  boring.

---

## What to take away

1. **Retention deletes whole segments, not records,** so nothing expires until its segment closes
   — at `segment.bytes` or `segment.ms`, whichever comes first.
2. **Retention must exceed your worst-case consumer outage.** For `clickstream.events` at 24
   hours, a Friday-evening failure fixed on Monday has lost the weekend permanently.
3. **Alert on time-to-expiry, not on lag.** How long until this consumer's position is deleted is
   the question that leaves you time to act.
4. **Retention uses producer-supplied timestamps by default.** A backfill carrying historical
   timestamps can be deleted within five minutes of a successful publish.
5. **`retention.bytes` is per partition.** Multiply by partitions and by replication factor before
   believing the number, and re-derive it whenever partition count changes.
6. **The active segment is never compacted,** so a compacted topic always contains duplicate keys
   at its head. Consumers must be last-write-wins.
7. **Set `max.compaction.lag.ms` whenever compaction is a deletion guarantee** rather than a
   storage optimisation. Without it there is no bound on when a tombstone takes effect.
8. **`delete.retention.ms` must exceed your slowest full-topic read,** or a bootstrapping consumer
   can miss a tombstone entirely and hold a deleted key forever. The 24-hour default is often too
   short.
9. **The log cleaner can die silently** and take every compacted topic with it, including
   `__consumer_offsets`. Alert on `time_since_last_run_ms` and `uncleanable_partitions_count`;
   almost nobody does until the first incident.
10. **Schema compatibility mode decides deployment order.** The default `BACKWARD` means consumers
    deploy first, and it only checks the previous version — use `BACKWARD_TRANSITIVE` for any
    topic you might replay.

Next: [08-how-kafka-breaks-at-scale.md](08-how-kafka-breaks-at-scale.md), which takes every
mechanism so far and asks what changes when Riverbend triples.
