# Lag, Backpressure, and Poison Messages

Consumer lag is the most-watched Kafka metric and the most misread. This doc explains what lag
actually measures, derives the number that matters more (how long recovery will take), and works
through the ten ways consumption falls behind or stops — including the patterns for processing
faster without giving up the guarantees from doc 05.

## Lag is a queue depth, and a queue depth alone tells you nothing

**Consumer lag** for a partition is `logEndOffset − committedOffset`: how many records have been
produced that this group has not yet committed. It is a *saturation* signal in the sense of the
USE method — see `[Observability/02-the-use-method.md](../Observability/02-the-use-method.md)`,
which treats `order-processor`'s lag as exactly that.

Here is why the raw number is not enough. Take a lag of 10,000,000 records on
`clickstream.events`:

```
during the evening peak, 85,000 records/s:   10,000,000 ÷ 85,000 =   118 s ≈ 2 minutes of data
at 03:00, roughly 8,000 records/s:           10,000,000 ÷  8,000 = 1,250 s ≈ 21 minutes of data
```

The same number means two minutes of delay or twenty-one, depending on the hour. An alert
threshold of "lag > 10 million" fires for a trivial condition in the evening and stays silent
through a serious one overnight. This is why record-count thresholds age badly and why every
team eventually replaces them.

**Lag in time is the signal you actually want**, and Kafka does not export it. You compute it in
the consumer, because only the consumer holds both halves:

```java
// After processing each record, export the age of the record you just handled.
long lagMillis = System.currentTimeMillis() - record.timestamp();
consumerLagSeconds.labels(topic, String.valueOf(partition)).set(lagMillis / 1000.0);
```

This one gauge answers the question people are really asking — "how stale is the data
downstream?" — and it is comparable across topics with wildly different rates. It also has a
property the record count lacks: it is meaningful at zero traffic. A partition receiving nothing
has zero record lag whether the consumer is healthy or dead; time lag keeps climbing if the
consumer is stuck, because the last record it processed keeps getting older.

### The number that actually predicts recovery

Lag tells you where you are. **Drain time** tells you when it ends, and it is the number to put
in an incident channel:

```
drain time = lag ÷ (consumer capacity − arrival rate)
```

The denominator is *surplus* capacity, and it is the reason lag recovery feels so much slower
than lag accumulation. Work through a Riverbend flash sale:

```
order-processor-group:  12 pods × 120 records/s = 1,440 records/s capacity
normal arrival                                  =   640 records/s
peak arrival during a 20-minute flash sale      = 3,400 records/s

During the sale — arrival exceeds capacity, so lag grows:
    (3,400 − 1,440) × 1,200 s  = 2,352,000 records of lag accumulated

After the sale — surplus is what is left over after keeping up with live traffic:
    1,440 − 640                =   800 records/s of surplus
    2,352,000 ÷ 800            = 2,940 s = 49 minutes to recover
```

**A twenty-minute overload produces a forty-nine-minute recovery.** That asymmetry is the single
most useful thing to internalise about lag, and it has two consequences worth acting on:

- **Capacity must be sized for peak, not average**, or every peak leaves a recovery tail that
can outlast the peak by a factor of two or more.
- **As capacity approaches arrival rate, drain time approaches infinity.** A group running at 95%
of arrival rate has 20× the drain time of one running at 50%. Headroom is not a luxury here;
it is what makes recovery finite.

Doc 10 turns both of these into alerts.

---



## Failure catalogue


| Class                                            | The question it answers                          | Scenarios       |
| ------------------------------------------------ | ------------------------------------------------ | --------------- |
| **A. The lag number is lying to you**            | Is this lag real, and is it bad?                 | `L-01` … `L-03` |
| **B. Consumption has stopped on one partition**  | Everything else is fine. Why is this one stuck?  | `L-04` … `L-05` |
| **C. Going faster without breaking correctness** | How do I actually fix this?                      | `L-06` … `L-08` |
| **D. Lag that damages the cluster**              | Why did one lagging consumer slow down everyone? | `L-09` … `L-10` |


---



## Class A — the lag number is lying to you



### L-01 · A sum across partitions hides the one that is stuck

**What you see.** Total group lag looks acceptable and stable. Downstream, a subset of entities
has not been updated in hours.

**Mechanism.** `sum(kafka_consumergroup_lag)` over 24 partitions is dominated by the partitions
that are working. One partition stuck at 400,000 records while the other 23 sit near zero
produces a total that looks like ordinary backlog. If total lag typically fluctuates by a few
hundred thousand during peaks, the stuck partition is invisible.

And because partitions are keyed, a single stuck partition is not a random 1/24 of your data —
it is a specific, coherent slice. For `inventory.adjustments`, it is every SKU that hashes to
that partition, and someone in the warehouse notices before your dashboard does.

**Confirm it.** Always look at the maximum alongside the sum:

```promql
# What you probably alert on
sum(kafka_consumergroup_lag{consumergroup="order-processor-group"})

# What you should also alert on
max(kafka_consumergroup_lag{consumergroup="order-processor-group"}) by (partition)

# The shape of the distribution — one outlier is a stuck partition,
# uniform elevation is a capacity problem
topk(5, kafka_consumergroup_lag{consumergroup="order-processor-group"})
```

**Prevent.** Alert on per-partition maximum, not on the group sum. The sum answers "do we have
enough capacity"; the maximum answers "is anything broken", and the second question is the one
that pages.

### L-02 · Lag that is intentional, and alerts that do not know

**What you see.** `analytics-sink-group` permanently shows 15–20 million records of lag. It has
alerted every day since it was deployed, and everyone has learned to ignore it — including on
the day it was real.

**Mechanism.** The S3 sink batches for ten minutes before committing, by design: writing a
600 MB object every ten minutes is dramatically cheaper and more queryable than writing 3,000
small objects. Its committed offset is therefore always up to ten minutes behind, and at
34,000 records/s that is:

```
34,000 records/s × 600 s = 20,400,000 records of expected, healthy lag
```

An alert threshold below 20 million fires constantly. The team raises it to 25 million, and now
a genuine stall must accumulate 25 million records — about twelve minutes at peak — before
anyone hears about it.

**Prevent.** Alert on **rate of change**, not on absolute lag, for any consumer with structurally
non-zero lag:

```promql
# Is the backlog growing, sustained? That is the real signal.
deriv(sum(kafka_consumergroup_lag{consumergroup="analytics-sink-group"})[15m:]) > 0
  and sum(kafka_consumergroup_lag{consumergroup="analytics-sink-group"}) > 25000000
```

Better still, have the consumer export its own health directly — for a batching sink, "seconds
since last successful flush" is unambiguous, needs no threshold tuning, and cannot be confused by
traffic shape.

### L-03 · Lag that is zero because nothing is being produced

**What you see.** Zero lag. Everything green. No data has arrived for two hours.

**Mechanism.** `logEndOffset − committedOffset = 0` when the consumer is caught up **and** when
the producer has stopped. Lag cannot distinguish a healthy pipeline from a dead one, because in
both cases there is nothing waiting.

This is the failure mode of monitoring only the consumer side, and it is common in pipelines
where the producer is someone else's system — a partner feed, an upstream team's service, a
change-data-capture connector that silently lost its replication slot.

**Prevent.** Pair every lag alert with a **throughput floor** on the producing side:

```promql
# No records produced to orders.created for 10 minutes during business hours
sum(rate(kafka_server_brokertopicmetrics_messagesinpersec_total{topic="orders.created"}[10m])) == 0
```

And on the consumer side, time-based lag (from the introduction) keeps climbing when a consumer
is stuck, which record-based lag does not — another reason to export it.

---



## Class B — consumption has stopped on one partition



### L-04 · Head-of-line blocking from in-place retries

**What you see.** One partition's lag climbing linearly. The consumer instance owning it is
logging the same error repeatedly, for the same offset.

**Mechanism.** A partition is consumed strictly in order by one member. If record 8,412,901 fails
and your code retries it in place, **every record behind it waits**, however healthy those records
are. A partition is a single-file queue, and one stuck item stops the line.

This is not a bug — it is the cost of ordering, and doc 05 (`D-09`) explains why retrying in
place is the only ordering-preserving retry. But it must be **bounded**, or a single bad record
stops that partition indefinitely (`L-05`).

The arithmetic for bounding it:

```
orders.created, 24 partitions, 640 records/s total ≈ 27 records/s per partition
retry policy: 5 attempts with exponential backoff, 1 s → 16 s, total ≈ 31 s of retrying

Worst case lag added by one failing record: 27 × 31 ≈ 840 records on that partition
```

840 records of transient lag on one partition is acceptable. Ten attempts with a 30-second cap
would be `27 × 300 = 8,100` records and several minutes of delay for everything behind it —
which is where you should start asking whether the record is ever going to succeed.

**Prevent.** Cap total retry time per record at a value you have derived, not a round number. Make
the cap a function of "how much delay can the records behind this one tolerate", and when it
expires, apply `L-05`'s decision.

### L-05 · The poison message that wedges a partition forever

**What you see.** A partition's lag growing without bound for hours. The same offset in every log
line. Restarting the consumer does not help — it resumes at the same record and fails the same
way.

**Mechanism.** A record that can never be processed successfully: a malformed payload, a schema
the consumer cannot deserialise, a reference to a deleted entity, a value that triggers a bug.
Retries cannot fix it because nothing about it will change. The consumer cannot advance past it
without deciding to skip it, and skipping it is a decision the code must be written to make.

⚠️ The worst version is a **deserialisation failure**, because it happens inside `poll()` before
your code sees the record. Your `try/catch` around `process()` never runs, the exception
propagates out of `poll()`, and the consumer cannot even get to the record to skip it. The fix is
to use a byte-array deserialiser and deserialise inside your own code, where you can catch it —
or to configure an error-handling deserialiser that yields a null payload you can detect.

**Confirm it.**

```bash
# Where exactly is the group stuck?
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group order-processor-group \
  | awk '$6 > 100000 {print}'          # rows with large LAG

# Read the offending record without disturbing the group
kafka-console-consumer.sh --bootstrap-server $BS --topic orders.created \
  --partition 7 --offset 8412901 --max-messages 1 \
  --property print.key=true --property print.headers=true
```

**Recover.** Three options, in the order you should consider them:

1. **Fix the consumer** if the record is valid and the code is wrong. Deploy and it drains. This
  is the only option that does not lose anything.
2. **Route it to the dead-letter queue** and advance. This is what `orders.created.dlq` is for,
  and it should already be automatic (`L-08`).
3. **Skip it manually** if there is no DLQ path. This loses the record, so record what you
  skipped:

**Prevent.** Every consumer that processes records it does not fully control needs a terminal
path: after N attempts, publish to a DLQ with the original record, the error, and the source
coordinates, then commit and move on. Without that path, one malformed record is an unbounded
outage on 1/24th of your data, and the recovery requires a human with cluster credentials at
three in the morning.

---



## Class C — going faster without breaking correctness



### L-06 · Parallel processing inside the consumer, done safely

**The problem.** `fraud-scorer-group` calls an API with a p99 of 1.4 s. Sequential processing
gives one record per 1.4 s per partition — with 24 partitions, about **17 records/s** against a
peak arrival of 3,400/s. Two hundred times too slow. Adding consumers does not help beyond 24
members (doc 04, `C-11`), and adding partitions has the ordering cost from doc 05 (`D-07`).

The only remaining axis is **concurrency within a member**: keep polling, and process many
records at once. The work is almost entirely waiting on a remote call, so concurrency is nearly
free in CPU terms.

**The three things that make it correct**, each of which is a way people get it wrong:

**1. Offsets may only advance over the contiguous completed prefix.** With concurrent processing,
record 104 finishes while 100 is still in flight. Committing 105 because it was the highest
completed offset marks 100 as done, and a crash then loses it permanently — you have silently
become at-most-once (doc 05, `D-01`). You must track which offsets are complete and commit only
up to the lowest incomplete one.

**2. Concurrency must be bounded, and the bound must apply backpressure.** An unbounded
`executor.submit()` per record turns a slow downstream into an out-of-memory error. The pool must
be bounded, and when it is full the consumer must stop fetching — `consumer.pause(partitions)` —
and resume when capacity returns. Pausing is what makes the backlog stay in Kafka, where it is
durable and visible as lag, rather than in your heap.

**3. Ordering, if you need it, must be preserved by sharding on the key** rather than by
round-robin dispatch (doc 05, `D-08`).

**The shape:**

```java
// Bounded pool, bounded queue. The bound is the backpressure.
var pool = new ThreadPoolExecutor(32, 32, 0L, MILLISECONDS, new ArrayBlockingQueue<>(64));
var tracker = new OffsetWatermarkTracker();   // per-partition contiguous-prefix tracking

while (running) {
    var records = consumer.poll(Duration.ofMillis(200));   // keeps the member alive
    for (var record : records) {
        tracker.track(record);                             // BEFORE dispatch
        if (!pool.getQueue().offer(task(record, tracker))) {
            consumer.pause(consumer.assignment());         // full: stop fetching
            break;
        }
    }
    if (pool.getQueue().remainingCapacity() > 32) {
        consumer.resume(consumer.paused());
    }
    consumer.commitSync(tracker.committableOffsets());     // contiguous prefix only
}
```

⚠️ The subtlety that makes this safe is that `poll()` **is still called on every loop iteration**,
even when everything is paused. `poll()` on a fully paused consumer returns no records and still
sends heartbeats and processes rebalances, which is what keeps `max.poll.interval.ms` from
evicting the member. A design that blocks instead of pausing reintroduces `C-01`.

A complete, race-tested implementation of this pattern — the watermark tracker, the bounded pool,
the adaptive downstream limiter, and graceful shutdown — is in
`[goQuestions/q1/reference_impl](../goQuestions/q1/reference_impl)` in this repository. The parts
that are easy to get wrong are the offset tracker and the shutdown ordering, and both are covered
there with tests.

**What it buys.** With 32 concurrent calls at p99 1.4 s, one member handles roughly
`32 ÷ 1.4 ≈ 23 records/s` instead of 0.7. Across 6 members that is 138 records/s, and across 24
members (one per partition) about 550 records/s — enough for normal load, and still short of the
3,400/s peak, which tells you the flash-sale path needs either more concurrency or an async
client rather than a thread per call.

### L-07 · The retry topic that became a second production topic

**The pattern.** Failed records are published to `orders.retry.30s`, consumed by a delayed
consumer, and either succeed or move to `orders.retry.5m`, then `orders.retry.30m`, then the DLQ.
It removes head-of-line blocking completely, because the main partition advances immediately.

**What you see when it goes wrong.** The retry topics carry a significant fraction of production
traffic. Nobody can say what the end-to-end latency of the pipeline is any more. A record has been
circulating for six hours.

**Mechanism.** Retry topics are genuinely useful and they introduce four costs that are rarely
counted at design time:

1. **Ordering is gone** for anything routed through them (doc 05, `D-09`).
2. **Each tier is a topic with partitions, consumers, and monitoring.** A three-tier retry ladder
  turns one pipeline into four, and the retry tiers are the ones with no dashboard.
3. **A downstream outage floods them.** If the scoring API is down for ten minutes, every record
  in that window goes to the retry tier, and the retry tier's consumer hits the same outage. You
   have moved the backlog, not reduced it — with the difference that it is now in a topic nobody
   alerts on.
4. **Delayed consumption is awkward to implement.** The common approach — consume, check the
  timestamp, and `sleep` until the record is due — blocks the partition and reintroduces
   head-of-line blocking inside the retry tier. The correct approach is to `pause()` the partition
   and `seek()` back to it later, which is more code than anyone expects.

**Prevent.** Use retry topics where they fit — unordered, independently-failing records with a
downstream that fails per record rather than wholesale. Prefer **in-place retry with a circuit
breaker** when failures are correlated: if the downstream is down, retrying in place and letting
lag accumulate in the source topic is simpler, preserves ordering, and keeps one backlog instead
of four. Monitor retry topics exactly as you monitor the main one, with an alert on any record
older than the full ladder's budget.

### L-08 · The dead-letter queue nobody reads

**What you see.** `orders.created.dlq` contains 4,000 records. The oldest is from March.

**Mechanism.** A DLQ solves the availability problem — the pipeline keeps moving — by converting
it into a correctness problem that is deferred rather than solved. Each record in a DLQ is a
business event that did not happen: an order not fulfilled, an inventory adjustment not applied.
Deferred forever, it is data loss with extra steps.

**Prevent.** A DLQ needs four things, and a DLQ with fewer than four is a place where data goes
to be forgotten:

1. **An alert on non-zero depth**, with a named owner. Not on a threshold — on *any* record. A
  DLQ should normally be empty, which makes "empty" a usable alert condition and removes all
   threshold tuning.
2. **Enough context to act.** Publish the original key and value, plus headers carrying the source
  topic, partition, offset, the exception, and the timestamp of the final attempt. A DLQ record
   without its origin cannot be investigated, and it certainly cannot be replayed.
3. **A replay path** that has been tested. Reprocessing a DLQ record must be a routine operation,
  not an improvisation. It usually means a small tool that reads the DLQ and re-publishes to the
   source topic, and it depends on consumers being idempotent (doc 05, `D-06`).
4. **Retention at least as long as your incident response.** `orders.created.dlq` keeps 14 days;
  a 7-day retention on a DLQ means a record that arrives on a Friday before a holiday can expire
   before anyone triages it.

---



## Class D — lag that damages the cluster



### L-09 · One lagging consumer degrades everyone

**What you see.** Produce latency across the whole cluster rises. Multiple unrelated consumer
groups start lagging. Broker CPU is normal; broker disk read throughput has gone from nearly zero
to hundreds of megabytes per second.

**Mechanism.** This is the page-cache cliff from doc 00, and it is the most important
cluster-wide effect in this collection because the cause and the symptom are in different places.

A healthy Kafka cluster does almost no disk reads. Consumers read data that was written seconds
ago, which is still in the operating system's page cache, so a fetch is a memory copy. Riverbend's
residency window, derived in doc 00:

```
page cache per broker:  ~24 GiB ≈ 25,770 MB
bytes written per broker at peak:     40.5 MB/s
residency:              25,770 ÷ 40.5 = 636 s ≈ 11 minutes
```

A consumer lagging by less than eleven minutes reads from memory and costs the cluster almost
nothing. A consumer lagging by more than that reads from **disk** — and the damage is not the
disk read itself. It is that those reads pull old pages into the cache, **evicting the recent
pages** that every other consumer and the replication fetchers depend on. Groups that were
comfortably inside the window now miss too, so they also read from disk, which evicts more. The
cliff is a feedback loop.

⚠️ The consequence that surprises people: **a batch job replaying a topic from the beginning can
degrade every producer on the cluster.** Nothing about the batch job looks like a problem — it is
read-only, it uses a separate consumer group, and it touches no production code path. But it
streams hundreds of gigabytes of cold data through a shared 24 GiB cache.

**Confirm it.**

```promql
# The signal: physical reads on brokers, which should normally be near zero
rate(node_disk_read_bytes_total{instance=~"kafka-.*"}[5m])

# Who is reading old data? Compare lag in time across groups.
max(kafka_consumer_lag_seconds) by (consumergroup)
```

If disk reads correlate with one group's lag, you have found it.

**Recover.** Throttle or pause the offending consumer. Kafka supports client quotas, which is the
durable fix rather than asking a team to stop:

```bash
kafka-configs.sh --bootstrap-server $BS --alter \
  --add-config 'consumer_byte_rate=20971520' \
  --entity-type clients --entity-name analytics-backfill
```

**Prevent.**

- **Client quotas on every batch and backfill consumer**, set by default rather than added after
an incident.
- **Alert on broker disk read throughput.** It is near zero in a healthy Kafka cluster, which
makes it an unusually clean signal — any sustained non-zero value means someone is reading cold
data.
- **Consider a separate cluster for replay-heavy workloads,** or read from a tiered-storage tier
where available, so that historical reads do not share a page cache with live traffic.
- Keep the derived residency number on the capacity dashboard, and recompute it when instance
types or traffic change. It moves, and nobody notices when it does.



### L-10 · Scaling the consumer did nothing

**What you see.** Pods doubled. Lag unchanged.

**Mechanism.** There are five distinct reasons, and the diagnosis matters because the fixes are
unrelated:


| Reason                                       | Signal                                    | Fix                           | Covered in       |
| -------------------------------------------- | ----------------------------------------- | ----------------------------- | ---------------- |
| More members than partitions                 | Members with zero partitions assigned     | Add partitions (carefully)    | doc 04, `C-11`   |
| Key skew — one partition has the work        | One partition's lag dominates             | Salt the key, or aggregate    | doc 03, `P-10`   |
| The downstream is the bottleneck             | Consumer CPU low, downstream latency high | Fix or scale the downstream   | this doc, `L-06` |
| Rebalance storm — the group never stabilises | High rebalance rate                       | `max.poll.records` arithmetic | doc 04, `C-01`   |
| A stuck partition, not a slow one            | One partition's lag grows linearly        | Poison message                | this doc, `L-05` |


⚠️ Adding pods makes two of these five actively **worse**. More members means more rebalances,
which deepens a rebalance storm, and more members means more concurrent load on a downstream that
is already the bottleneck. "Scale it up" is the wrong first move often enough that the diagnosis
should always come first.

**Confirm it.** Start with the shape of the lag distribution across partitions, because it
distinguishes three of the five rows immediately: uniform lag means capacity, one hot partition
means skew, one linearly-growing partition means stuck.

---



## What to take away

1. **Lag in records is not comparable across time of day.** Ten million records is two minutes at
  peak and twenty-one minutes at three in the morning. Export lag in **time**, computed in the
   consumer from the record timestamp.
2. **Drain time = lag ÷ (capacity − arrival rate).** A twenty-minute Riverbend flash sale produces
  a forty-nine-minute recovery, and as capacity approaches arrival, recovery time approaches
   infinity.
3. **Alert on per-partition maximum, not on the group sum.** The sum answers a capacity question;
  the maximum answers whether something is broken.
4. **For consumers with structural lag, alert on the derivative,** or have the consumer export its
  own health directly.
5. **Zero lag can mean nothing is being produced.** Pair every lag alert with a throughput floor
  on the producer side.
6. **Head-of-line blocking is the price of ordering.** Bound it deliberately: derive the retry
  budget from how much delay the records behind it can tolerate.
7. **Every consumer needs a terminal path for a poison message,** and deserialisation failures
  need special handling because they happen inside `poll()` before your code can catch them.
8. **Concurrent processing inside a consumer needs three things:** commit only the contiguous
  completed prefix, bound the pool and `pause()` when it is full, and shard by key if ordering
   matters.
9. **A dead-letter queue without an alert, context, a tested replay path, and generous retention
  is a place where data is forgotten.**
10. **One lagging consumer can degrade the entire cluster** by evicting the page cache. Broker
  disk read throughput is near zero when healthy, which makes it one of the cleanest alerts you
    can have.

Next: [07-retention-compaction-and-schema.md](07-retention-compaction-and-schema.md), which covers
what happens to data while it is sitting in the log.