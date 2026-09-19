# Delivery Semantics and Ordering

"Exactly-once" is the most misunderstood phrase in streaming, and the misunderstanding is
expensive: teams either enable a feature that does not cover what they need, or avoid a feature
that would have. This doc derives the three delivery semantics from the mechanics rather than
asserting them, shows exactly what Kafka transactions do and do not cover, and then catalogues
the ways ordering is lost — which is the guarantee people assume they have and check least often.

## Deriving the three semantics

Forget the names for a moment. A consumer does two things with every record: it **processes** the
record (writes to a database, calls an API, produces a derived record), and it **commits** the
offset. The only question is the order, and there are exactly two options.

**Commit first, then process.**

```
poll() → commit offset → process record
```

If the consumer crashes between the commit and the processing, the record's offset is already
committed, so nobody will ever process it. It is lost. But no record can ever be processed twice,
because the commit happens before any work. This is **at-most-once**: zero or one.

**Process first, then commit.**

```
poll() → process record → commit offset
```

If the consumer crashes between the processing and the commit, the offset is not committed, so
the next owner of that partition reads the record again and processes it a second time. But no
record can be skipped, because the offset only advances after the work is done. This is
**at-least-once**: one or more.

There is no third ordering. Those are the only two choices available to a consumer that does two
separate operations, and each has exactly one failure mode.

**So what is exactly-once?** It is what you get when the two operations stop being separate —
when processing and committing happen **atomically**, as a single operation that either fully
happens or fully does not. That is not a Kafka feature and it is not a configuration flag. It is
a property of whether the side effect and the offset live inside the same transactional system.

This gives you the test that answers every exactly-once question you will ever be asked:

> **Can the side effect and the offset commit be made atomic?** If yes, exactly-once is
> achievable. If no, it is not, and the answer is at-least-once plus idempotent processing.

Applied to Riverbend:

| Consumer | Side effect | Same transactional system as the offset? | Achievable |
|---|---|---|---|
| `clickstream-rollup-group` | Produces aggregates to another Kafka topic | **Yes** — Kafka transactions cover both | Exactly-once |
| `order-processor-group` | Writes to `orders-db` (Postgres) | No — two systems | At-least-once + idempotence |
| `fraud-scorer-group` | Calls a third-party HTTPS API | No — and you do not control it | At-least-once + idempotence |
| `analytics-sink-group` | Writes files to S3 | No, but S3 writes are idempotent by key | At-least-once, effectively once |

Only the first row can use Kafka's exactly-once semantics. The other three are the majority of
real consumers, and for them the phrase that describes the achievable goal is **effectively
once**: at-least-once delivery combined with idempotent processing, so duplicates are delivered
and have no observable effect. That is what almost every correct production system does, and it
is a better target than exactly-once because it survives bugs, replays, and manual intervention,
none of which a transaction protects you from.

---

## Failure catalogue

| Class | The question it answers | Scenarios |
|---|---|---|
| **A. The semantics you have are not the ones you wanted** | What am I actually running? | `D-01` … `D-02` |
| **B. Exactly-once, correctly and incorrectly** | Does the transaction cover what I think? | `D-03` … `D-05` |
| **C. Making duplicates harmless** | The practical answer for most consumers | `D-06` |
| **D. Ordering, lost quietly** | I keyed everything. Why is it out of order? | `D-07` … `D-09` |

---

## Class A — the semantics you have are not the ones you wanted

### D-01 · At-most-once by accident

**What you see.** Records present in Kafka and absent from the destination, in small numbers,
correlated with restarts and rebalances.

**Mechanism.** Almost nobody chooses at-most-once deliberately. They arrive at it three ways, and
all three are defaults or reasonable-looking code:

1. **`enable.auto.commit=true`** (the default) commits inside `poll()`, before you have finished
   processing the previous batch. Doc 04, `C-06`.
2. **Committing at the top of the loop** — `commitSync()` then process — which looks tidy and is
   wrong.
3. **A `ConsumerRebalanceListener.onPartitionsRevoked` that commits the current position** while
   in-flight work from that batch has not finished. The commit races the work.

**Confirm it.** Read the loop. The question is whether any code path can commit an offset whose
record has not completed its side effect. If yes, you are at-most-once on that path regardless of
what the rest of the code does.

**Prevent.** Commit strictly after the side effect is durable, and only for records that are
actually complete. For a consumer processing records concurrently, that means tracking which
offsets are finished and committing only the contiguous completed prefix — the same watermark
logic as `goQuestions/q1` in this repository, and the reason that problem is worth solving
properly.

### D-02 · At-least-once, and the duplicates you agreed to

**What you see.** The same record processed twice. Usually after a rebalance, a deployment, or an
`OffsetCommit` that failed.

**Mechanism.** This is at-least-once working correctly, and it is worth enumerating every source
so that "how often" becomes a number rather than a worry:

| Source | Frequency at Riverbend | Bounded by |
|---|---|---|
| Consumer crash between processing and commit | Every ungraceful pod termination | One poll batch (`max.poll.records`) |
| Rebalance revoking a partition mid-batch | Every deployment, ×2 per pod if eager (doc 04, `C-02`) | One poll batch per moving partition |
| Zombie consumer still processing after revocation (doc 04, `C-13`) | Every rebalance with slow processing | One in-flight batch |
| Producer retry of a write whose response was lost (doc 03, `P-05`) | Rare; eliminated within a session by idempotence | One batch |
| Deliberate offset reset or replay | Whenever an operator does it | Whatever you reset to |

The last row is the one people forget when designing for exactly-once. Even a perfect
transactional pipeline gets replayed by a human during an incident, and if your consumer cannot
tolerate that, your recovery options are much narrower than they should be. **Design for
duplicates even if you have transactions**, because operations will produce them.

**Prevent.** You do not prevent them; you make them harmless. `D-06`.

---

## Class B — exactly-once, correctly and incorrectly

### D-03 · Kafka transactions: what they actually cover

**How they work.** A transactional producer sets a stable `transactional.id` and wraps a
read-process-write cycle:

```java
producer.initTransactions();                       // once at startup: fences any older
                                                   // producer with this transactional.id
while (running) {
    var records = consumer.poll(Duration.ofMillis(500));
    producer.beginTransaction();
    for (var record : records) {
        producer.send(deriveAggregate(record));    // output records
    }
    // The offsets become part of the transaction, not a separate commit
    producer.sendOffsetsToTransaction(currentOffsets(consumer), consumer.groupMetadata());
    producer.commitTransaction();
}
```

The critical line is `sendOffsetsToTransaction`. It writes the consumer's offsets to
`__consumer_offsets` **inside the same transaction as the output records**. Because both the
outputs and the offsets are in one atomic unit, there is no window in which one exists without
the other. That is the atomicity the derivation above required, and it is why the input consumer
must **not** commit offsets itself — `enable.auto.commit` must be `false`.

Underneath: a **transaction coordinator** (the broker leading the relevant `__transaction_state`
partition) tracks the transaction and, on commit, writes **transaction markers** — special
control records — into every partition the transaction touched. Consumers with
`isolation.level=read_committed` use those markers to decide what is visible.

**What it covers.** Atomicity across: multiple partitions, multiple topics, and the consumer's
offset commit — **all within one Kafka cluster**.

**What it does not cover, and this is the part that matters:**

- **Any side effect outside Kafka.** A database write, an HTTP call, a file, an email. The
  transaction commits or aborts Kafka state; it has no influence over anything else. A pipeline
  that reads Kafka, writes Postgres, and commits a Kafka transaction has exactly-once Kafka state
  and at-least-once Postgres state.
- **More than one cluster.** There is no cross-cluster transaction.
- **Anything a human does.** An offset reset replays committed transactions.
- **Non-determinism in your code.** If processing a record twice produces different outputs —
  because it reads the clock, calls a random number generator, or depends on external state — the
  aborted attempt and the retried attempt differ, and only the committed one counts, which may
  not be the one you expected.

**The cost.** Transactions add latency and throughput overhead: two extra coordinator round trips
per transaction, plus marker writes to every partition involved. The overhead is per
*transaction*, not per record, so it is amortised by batching more records per transaction — at
the cost of latency, because a `read_committed` consumer sees nothing from an open transaction.
For `clickstream-rollup-group` aggregating five-minute windows, that trade is free. For a
low-latency pipeline committing every 10 ms, the overhead is severe.

**Confirm what you have.**
```bash
# Which producers are transactional, and what state are their transactions in?
kafka-transactions.sh --bootstrap-server $BS list
kafka-transactions.sh --bootstrap-server $BS describe --transactional-id payments-writer-3
```

**Prevent misuse.** If you are using Kafka Streams, set `processing.guarantee=exactly_once_v2`
and let it manage all of the above; hand-rolling the protocol is error-prone and the library
version is better. (`exactly_once_v2` replaced the original `exactly_once` in Kafka 2.6 and uses
far fewer producer instances.) If you are writing to a database, do not use Kafka transactions at
all — use `D-05`.

### D-04 · The hanging transaction that freezes `read_committed` consumers

**What you see.** `settlement-group`'s lag climbs steadily and never recovers. Producers are
writing to `payments.settled` normally. Brokers are healthy. Every diagnostic is green. A
consumer with `isolation.level=read_uncommitted` can read the records fine; the production one
cannot.

**Mechanism.** This is the most confusing Kafka failure in this collection, and it is worth
understanding precisely because every ordinary diagnostic says the system is fine.

A `read_committed` consumer does not read up to the high watermark. It reads up to the **last
stable offset (LSO)**, defined as the offset of the **first still-open transaction** on that
partition. Everything below the LSO has been decided — committed or aborted — and can be filtered
correctly. Everything at or above it might still be aborted, so it cannot be shown.

Now suppose a transaction opens at offset 4,201,338 and is never resolved. Producers keep
appending; the high watermark climbs past 4.3 million, 4.4 million, 4.5 million. **The LSO stays
at 4,201,338 forever.** A `read_committed` consumer is permanently stuck there, and its lag grows
at exactly the rate the topic is written.

Transactions are supposed to be resolved by timeout: `transaction.timeout.ms` (producer-side,
60 s by default, capped by the broker's `transaction.max.timeout.ms` of 15 minutes) causes the
coordinator to abort an abandoned transaction. A **hanging** transaction is one where that
mechanism did not fire — historically from a narrow set of protocol edge cases around producer
crashes and coordinator failovers, which is what KIP-890 (Kafka 3.6 onwards) exists to close. If
you are running an older broker, this is a live risk.

⚠️ The reason this incident lasts hours is that the symptom — "one consumer group lagging" — leads
everyone to investigate the consumer. The consumer is fine. Its lag is not a consumer problem and
no amount of scaling it will help.

**Confirm it.** The diagnostic is specific and it should be in your runbook, because nothing else
finds this:
```bash
# Scan for transactions that are open far longer than they should be
kafka-transactions.sh --bootstrap-server $BS find-hanging --broker-id 3

# For a suspect partition, compare the high watermark against the LSO.
# A large, non-shrinking gap is the confirmation.
kafka-get-offsets.sh --bootstrap-server $BS --topic payments.settled --time -1   # high watermark
```
A quicker heuristic during an incident: run a throwaway console consumer with
`--isolation-level read_uncommitted`. If it sees recent records and your production consumer does
not, you have an LSO problem and not a consumer problem.

**Recover.** Abort the hanging transaction explicitly. This discards its records, which is the
correct outcome — they were never committed and no `read_committed` consumer ever saw them:
```bash
kafka-transactions.sh --bootstrap-server $BS abort \
  --topic payments.settled --partition 4 --start-offset 4201338
```
The LSO advances immediately and the stalled consumer drains at whatever rate it can manage.

**Prevent.** Run Kafka 3.6 or later so KIP-890's fixes apply. Alert on the high-watermark-minus-LSO
gap per partition for every topic with transactional producers, which is the only alert that
catches this before a human does. And keep `transaction.timeout.ms` low — 60 seconds is
reasonable — so that ordinary abandonment resolves quickly and only genuine protocol failures
reach you.

⚠️ One more consequence of transactions worth knowing: **control records occupy offsets**. A
transactional partition's offsets are not contiguous, so "number of records" computed as
`endOffset − startOffset` overcounts, and per-partition lag figures include markers. It is a small
effect, and it confuses people comparing Kafka's lag against an application's own counter.

### D-05 · Exactly-once to an external system: the transactional outbox

**The problem.** `order-processor` must write an order to `orders-db` and must not write it twice
or skip it. Kafka transactions cannot help — Postgres is not part of them. Two-phase commit
across Kafka and Postgres is theoretically possible, practically miserable, and unsupported by
the Kafka client.

**The pattern, from the producer's side.** Instead of writing to the database and publishing to
Kafka as two operations, make the publish a *consequence* of the database write:

1. In **one database transaction**, write the business row and an `outbox` row describing the
   event.
2. A separate process reads the `outbox` table and publishes to Kafka, marking rows published (or
   using change data capture — Debezium reading the Postgres write-ahead log — which avoids the
   polling entirely).

The event exists in the outbox **if and only if** the business change committed, because they are
the same transaction. Publishing is then at-least-once — the publisher can crash after publishing
and before marking — so consumers still need `D-06`. What you have eliminated is the far worse
failure where the database and Kafka disagree about whether something happened.

**The pattern, from the consumer's side** — the inbox, which is the same idea reflected:

```sql
BEGIN;
  INSERT INTO processed_offsets (consumer_group, topic, partition, offset_value)
       VALUES ('order-processor-group', 'orders.created', 7, 8412901)
  ON CONFLICT (consumer_group, topic, partition) DO UPDATE
       SET offset_value = EXCLUDED.offset_value
     WHERE processed_offsets.offset_value < EXCLUDED.offset_value;

  INSERT INTO orders (order_id, tenant, amount_cents, ...) VALUES (...)
  ON CONFLICT (order_id) DO NOTHING;
COMMIT;
```

The offset is stored **in the destination database**, in the same transaction as the business
write. Now the two are atomic, and by the derivation at the top of this doc you have genuine
exactly-once for this consumer — achieved by moving the offset into the transactional system
rather than by any Kafka feature.

On startup, the consumer reads its position from `processed_offsets` and calls `seek()` instead of
relying on Kafka's committed offsets. Kafka's own offsets become advisory, useful for lag
monitoring and nothing else.

⚠️ This is more work than `enable.idempotence=true` and it is the only approach that actually
delivers exactly-once against an external store. Use it where the data warrants it — orders,
payments, ledgers — and use `D-06` everywhere else.

---

## Class C — making duplicates harmless

### D-06 · Idempotent consumers, and the dedup key nobody sizes

**The principle.** Processing a record twice should produce the same result as processing it
once. Four ways to get there, in order of preference:

1. **Natural idempotence.** The operation is already repeatable: `SET status='shipped'`, an S3
   `PutObject` to a deterministic key, a `DELETE`. Nothing to build. Always check for this first —
   a surprising number of operations can be reshaped into it.
2. **Upsert on a business key.** `INSERT ... ON CONFLICT (order_id) DO NOTHING` or
   `DO UPDATE SET ...`. Requires a unique key that is stable across retries, which the producer
   must supply — `order_id`, not an auto-generated row id.
3. **Conditional update with a version.** `UPDATE orders SET status=$1, version=$2
   WHERE order_id=$3 AND version < $2`. Handles both duplicates and out-of-order delivery, which
   makes it the strongest of the four. Requires a monotonic version in the record.
4. **An explicit dedup table** keyed on `(topic, partition, offset)` or a producer-supplied event
   id, consulted before processing. The fallback when the operation genuinely cannot be made
   repeatable — an outbound email, a payment capture.

**The part people get wrong: the dedup table's retention.** A dedup table needs a TTL or it grows
without bound, and the TTL must be **longer than the largest replay you would ever perform**. If
`orders.created` retains 72 hours and you might replay the whole topic during an incident, a
24-hour dedup TTL means a replay of anything older than a day is not deduplicated at all — and
the moment you discover this is during the incident where you needed it.

Size it deliberately:

```
orders.created retention:              72 hours
largest plausible replay:              72 hours (the whole topic)
safety factor for a slow replay:       ×2
dedup table TTL:                       144 hours = 6 days
```

Then check the cost, because six days of dedup keys is a real table:

```
640 records/s × 86,400 s/day × 6 days = 331,776,000 rows
at ~60 bytes per row (key + timestamp + index)
                                      ≈ 20 GB
```

Twenty gigabytes is affordable; two hundred would change the design. The point is to compute it
before committing to the approach rather than discovering it when the table stops fitting in
memory.

⚠️ **Deduplicating on `(topic, partition, offset)` breaks if the topic is ever repartitioned or
mirrored.** Offsets are not preserved across clusters (doc 09, `M-03`) and partition assignment
changes with partition count (`D-07`). A producer-supplied event id is more work up front and
survives both.

---

## Class D — ordering, lost quietly

Ordering is the guarantee people assume and verify least. Kafka's actual guarantee is narrow:
**records within one partition are delivered in the order they were appended.** Everything below
is a way that guarantee remains technically true while your application's ordering breaks.

### D-07 · Adding partitions re-maps every key

**What you see.** After a partition increase, records for the same entity are processed out of
order or concurrently by two different consumers. Duplicated side effects that idempotence keyed
on `order_id` should have caught, but did not, because the two records were genuinely different
events.

**Mechanism.** The default partitioner computes `murmur2(key) % numPartitions`. The modulus is the
partition count. Change the count and nearly every key moves:

```
orders.created grows from 24 to 48 partitions
murmur2("ord_8f2a91") = 1,847,203,551
    before:  1,847,203,551 % 24 = 15   → partition 15
    after:   1,847,203,551 % 48 = 39   → partition 39
```

Records for `ord_8f2a91` written before the change are on partition 15; records written after are
on partition 39. Two different consumer instances own those partitions, and they process
concurrently with no ordering relationship at all. An order's `created` event on partition 15 can
be processed *after* its `cancelled` event on partition 39.

⚠️ **This is not a transient effect during the resize.** It persists for as long as the old
records remain in retention — 72 hours for `orders.created` — and for the entire backlog if a
consumer is lagging. Case study CS-6 in doc 11 is this incident.

**Prevent.** Adding partitions to a keyed topic where ordering matters is a **data migration**,
not a configuration change. The options, in order of preference:

1. **Do not.** Size partition count for the topic's whole life (doc 08, `S-02`). Over-provisioning
   partitions costs far less than this.
2. **Create a new topic** with the target partition count, dual-write or migrate consumers, and
   retire the old one. More work, and it is correct.
3. **Drain first.** Stop producers, let consumers reach the end of every partition, add
   partitions, then resume. Correct, and it requires a write outage of however long draining
   takes.
4. **Use a custom partitioner** whose mapping is stable under resize — consistent hashing, or an
   explicit key-to-partition table. Worth it only if you know in advance you will resize
   repeatedly.

⚠️ Note that Kafka will not warn you. `kafka-topics.sh --alter --partitions 48` succeeds
immediately and says nothing about ordering.

### D-08 · Ordering lost inside the consumer

**What you see.** Per-partition ordering is intact in the log, and the application still applies
updates out of order.

**Mechanism.** The consumer polls a batch and hands records to a thread pool for parallel
processing. Records from the same partition — therefore the same key — are now processed
concurrently, and whichever thread finishes first wins.

This is an easy accident because the parallelism is added for throughput reasons, usually by
someone solving `C-01`, and the ordering loss is invisible until a specific pair of events for
one key happens to race.

**Prevent.** If you process records concurrently, shard the work **by key**, not round-robin:
route each record to a worker by `hash(key) % workers` so that one key is always handled by one
worker, in order. Different keys still run in parallel, so you keep the throughput. Doc 06
(`L-06`) covers the full pattern including how to commit offsets safely under it — which is the
harder half, because completions arrive out of order and you may only commit the contiguous
completed prefix.

### D-09 · Ordering lost by a retry topic

**What you see.** A record that failed and was retried is applied after records that came
*after* it, for the same key.

**Mechanism.** The common retry pattern sends failed records to `orders.retry.5m` and reconsumes
them later. That is a different topic, consumed independently, so the retried record rejoins the
stream at an arbitrary later point. For one key, event 3 fails and is retried at 14:05 while
events 4 and 5 were processed at 14:00 — so the final state reflects event 3, not event 5.

⚠️ This means **retry topics and per-key ordering are fundamentally incompatible.** The pattern is
excellent for independent records (doc 06, `L-05`) and silently wrong for ordered ones, and
nothing about it announces which you have.

**Prevent.** For ordered streams, retry **in place**: keep the consumer on the record, retrying
with backoff, and accept that the partition is blocked while you do. That is head-of-line
blocking, it is the price of ordering, and doc 06 (`L-04`) covers bounding it. If head-of-line
blocking is unacceptable and ordering is required, the only remaining option is to make ordering
unnecessary — version every event and apply conditionally (`D-06`, option 3), so a late-arriving
older event is rejected by the version check rather than overwriting a newer one.

That last option is worth emphasising because it is the way out of the dilemma: **a
version-conditional write turns an ordering requirement into an idempotence requirement**, and
idempotence is much easier to guarantee in a distributed system than ordering.

---

## Choosing, in one table

| Your situation | Use | Do not use |
|---|---|---|
| Kafka → Kafka, both in one cluster | Transactions, or Kafka Streams `exactly_once_v2` | An external dedup table you do not need |
| Kafka → relational database | Offsets stored in the database, in the business transaction (`D-05`) | Kafka transactions — they do not cover the database |
| Kafka → third-party API | At-least-once + the API's idempotency key | Any claim of exactly-once |
| Kafka → object storage | At-least-once + deterministic object keys | A dedup table; the key is the dedup |
| Ordering matters per entity | One key per entity, fixed partition count, retry in place | Retry topics, unkeyed records, partition resizes |
| Ordering does not matter | Retry topics, parallel processing, resize freely | Paying for ordering you will not use |

---

## What to take away

1. **There are only two orderings of process-and-commit,** and they give you at-most-once and
   at-least-once. Exactly-once is what you get when they stop being two operations.
2. **The test for exactly-once is one question:** can the side effect and the offset commit be
   made atomic? Kafka transactions make it true for Kafka-to-Kafka; nothing makes it true for a
   third-party API.
3. **Kafka transactions cover multiple partitions, multiple topics, and the offset commit — in
   one cluster.** They cover no database, no HTTP call, and no second cluster.
4. **`sendOffsetsToTransaction` is the line that makes it work,** and the input consumer must not
   commit offsets itself.
5. **A hanging transaction freezes `read_committed` consumers permanently** by pinning the last
   stable offset, while every ordinary diagnostic stays green. `kafka-transactions.sh find-hanging`
   is the only thing that finds it.
6. **Design for duplicates even with transactions,** because operators replay topics and no
   protocol protects you from that.
7. **The outbox pattern is the real answer for database destinations:** store the offset in the
   destination, in the business transaction.
8. **Size your dedup table's TTL from your largest plausible replay,** not from a round number.
   For Riverbend that is six days and roughly 20 GB.
9. **Adding partitions to a keyed topic is a data migration.** Every key re-maps, and the old and
   new records are processed concurrently by different consumers for the whole retention period.
10. **Retry topics and per-key ordering are incompatible.** If you need both, stop needing
    ordering: version every event and apply conditionally, which converts an ordering problem into
    an idempotence problem you can actually solve.

Next: [06-lag-backpressure-and-poison-messages.md](06-lag-backpressure-and-poison-messages.md),
which is about what to do when consumption cannot keep up — including the patterns this doc kept
pointing at.
