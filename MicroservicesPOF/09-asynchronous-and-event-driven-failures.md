# Asynchronous and Event-Driven Failures — The Path With No One Waiting

Every failure so far had a user waiting for an answer. That waiting user is a monitoring system:
they generate an error, a latency measurement, a retry, a support ticket. The asynchronous path
has none of that.

**When an event consumer stops, its error rate is zero.** It has no requests, so it has no
failures. Its latency is undefined. Its CPU is idle, which looks healthy. Its pods are `Running`.
Every dashboard is green, and no work is being done.

That is the defining property of this doc, and the reason async failures are measured in hours
while synchronous failures are measured in minutes. Riverbend's longest-ever incident was a
consumer that stopped for eleven hours on a Saturday; the longest synchronous outage in the same
year was 23 minutes.

This doc covers the failure points of asynchronous work at an architectural level: what queues
do and do not give you, how the failures differ from the synchronous ones, and what you have to
build because the request path's defences do not apply. Broker-level detail — partition
mechanics, ISR, rebalance protocols, delivery semantics, retention — is in
[`../Kafka`](../Kafka/README.md) and this doc cites it rather than repeating it.

## What a queue actually buys, and what it does not

A queue between producer and consumer buys four things:

1. **Temporal decoupling.** The producer does not wait for the consumer. Checkout returns in 40 ms
   instead of 400 ms because it does not wait for the invoice, the email, and the analytics write.
2. **Availability decoupling.** The consumer can be down and the producer keeps working. This is
   the big one: by doc 00's arithmetic, moving a dependency from the synchronous path to an
   asynchronous one removes it from the availability product entirely.
3. **Rate decoupling (buffering).** A burst can be absorbed and processed at a steady rate.
4. **Fan-out.** One event, many independent consumers, added without changing the producer.

And the thing it does **not** buy, which is the most common misconception:

> **A queue does not add processing capacity.** If your consumer can do 500 messages/s and your
> producer generates 800/s, the queue does not fix that. It converts an immediate failure into a
> growing delay — which is often better, and is sometimes much worse, because the immediate
> failure would have told you.

The distinction that matters: **a queue absorbs a *burst*; it does not absorb a *deficit*.** A
burst is a temporary excess with an end — Riverbend's flash sale pushes 3,400 events/s for twenty
minutes against a consumer that does 1,000/s, and the queue holds `(3,400 − 1,000) × 1,200 = 2.9
million messages` which then drain over the next 48 minutes. That works.

A deficit is a permanent excess: the producer averages 1,200/s and the consumer does 1,000/s.
The queue grows forever. There is no amount of buffering that fixes it, and the queue's only
effect is to delay the moment you find out, from "immediately" to "when the disk fills." See
`Q-03`.

## When to use async, and when it is the wrong answer

| Use async when | Use sync when |
|---|---|
| The caller does not need the result to respond | The caller needs the result |
| The work can be retried later without the user present | The user must be told the outcome now |
| The work is a side effect (email, index update, analytics) | The work is part of the answer |
| Load is bursty and the work is deferrable | A backlog would be meaningless (a stale ranking is worse than none) |
| You want fan-out to multiple independent consumers | There is exactly one consumer and you own both sides |
| The consumer's availability should not affect the producer's | Failure must be visible to the caller immediately |

The anti-pattern worth naming, because it is common and produces the worst of both: **synchronous
request/response implemented over a queue.** The caller publishes a message and then blocks
waiting for a reply message on another queue. You now have the latency of a queue, the complexity
of correlation IDs and reply-to addresses, the failure modes of both models, and none of async's
benefits — the caller is still coupled to the consumer's availability. If the caller must wait,
make an RPC.

## The failure catalogue

### Q-01 · The silent backlog

**What you see.** Nothing, for hours. Then a customer asks why their order from this morning has
not shipped.

**Mechanism.** The consumer stopped, or slowed below the production rate. There is no error
signal because there is no request. The specific ways a consumer stops without erroring:

- It crashed and the pod is in `CrashLoopBackOff`. The deployment shows `0/12 Ready`, which
  nobody is looking at because the *service* has no traffic to fail.
- It is running but stuck — a deadlock, an infinite retry on one message (`Q-05`), a blocked call
  to a dependency with no timeout (`R-01`).
- Its consumer group was rebalanced and one partition was never assigned.
- Its scaling went to zero because the scaler used CPU, and a consumer waiting on a slow
  downstream has low CPU.
- It is processing, correctly, at a rate below production (`Q-03`).

**Confirm it.** There is exactly one signal that works, and it is not error rate, CPU, or pod
count. It is **the age of the oldest unprocessed message**:

```
# Kafka: consumer group lag in messages
kafka_consumergroup_lag{group="order-processor-group"}

# Better: lag in TIME, which is what actually matters (see Q-02)
kafka_consumergroup_lag_seconds{group="order-processor-group"}

# SQS
aws cloudwatch get-metric-statistics --namespace AWS/SQS \
  --metric-name ApproximateAgeOfOldestMessage \
  --dimensions Name=QueueName,Value=riverbend-invoices \
  --start-time "$(date -u -v-1H +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
  --period 300 --statistics Maximum
```

**Prevent.** Three things, and the first is non-negotiable:

1. **Every queue and every consumer group has an age-of-oldest-message alert.** This is the async
   path's equivalent of an error-rate alert, and it is the only alert that detects the whole
   class. If you have one async monitoring rule, make it this one.
2. **A heartbeat that proves processing, not liveness.** Emit a metric each time a message is
   *successfully processed*. Alert when that rate is zero for longer than the expected quiet
   period. This catches the case where the queue happens to be empty for a good reason versus the
   consumer being dead.
3. **End-to-end canary messages.** Inject a synthetic message every minute and measure how long
   it takes to come out the other end. This tests the whole path — producer, broker, consumer,
   and the consumer's downstream — and it works even when the queue is otherwise empty, which is
   exactly when lag metrics tell you nothing.

### Q-02 · Lag in messages is the wrong unit

**What you see.** An alert on "lag > 10,000" that fires constantly during normal bursts and does
not fire during a real outage on a low-volume topic.

**Mechanism.** Lag measured in messages has no fixed relationship to how far behind you are.

```
clickstream.events at 34,000/s:  10,000 messages of lag = 0.3 seconds behind. Fine.
payments.settled at 180/s:       10,000 messages of lag = 56 seconds behind. A problem.
catalog.changes at 40/s:         10,000 messages of lag = 4.2 minutes behind. An incident.
```

The same number means three different things. An alert threshold in messages is either too
sensitive for the high-volume topic or too insensitive for the low-volume one, and you cannot
pick a number that works for both.

**Prevent.** Alert on **time lag**: how old is the oldest unprocessed message. That number has a
consistent meaning across every topic and maps directly onto a business requirement ("orders must
be processed within 5 minutes").

Computing it varies by broker. Kafka does not expose it directly; you derive it from the
timestamp of the record at the committed offset, which is what `kafka-lag-exporter` and Burrow
do. SQS gives it to you directly as `ApproximateAgeOfOldestMessage`. It is worth the plumbing.

And express the SLO in those terms: "99% of `order.created` events are processed within 60
seconds" is an SLO you can defend, alert on, and report. "Lag below 10,000" is not.

### Q-03 · The permanent deficit

**What you see.** Lag that grows steadily, forever, at a constant rate. Scaling the consumer
helps for a while and then it resumes.

**Mechanism.** Consumption capacity is below production rate, structurally. The queue converts
this into a delay that grows without bound instead of an error.

Work out Riverbend's numbers to see how the ceiling appears. `order-processor` consumes
`orders.created` (24 partitions) with 12 pods:

```
Per-pod throughput:  80 messages/s (limited by a 12 ms database write plus overhead)
12 pods:             960 messages/s
Production rate:     640/s steady → fine
Flash-sale peak:     3,400/s      → deficit of 2,440/s
```

Scale up. But **consumer parallelism in Kafka is capped by partition count**: 24 partitions means
at most 24 useful consumers. Beyond that, extra pods sit idle.

```
Maximum: 24 pods × 80 msg/s = 1,920 messages/s
Still below the 3,400/s peak.
```

So the ceiling is structural, and the only ways past it are: more partitions (which requires
repartitioning, and breaks ordering guarantees for existing keys — see
[`../Kafka/04-consumer-groups-and-rebalance-failures.md`](../Kafka/04-consumer-groups-and-rebalance-failures.md)),
or faster per-message processing, or in-consumer parallelism (`Q-11`).

**Confirm it.** The decisive measurement is the **drain rate**: how fast does lag fall when
production stops? If lag is 2 million and it falls at 300/s, you are 1.9 hours from recovery and
you can say so, precisely. If lag is 2 million and *not* falling during a quiet period, you have
a deficit, not a backlog, and no amount of waiting will clear it.

```
# Is lag growing or shrinking? The derivative is the diagnosis.
deriv(kafka_consumergroup_lag{group="order-processor-group"}[10m])
```

**Prevent.**

- **Over-partition from the start.** Partitions are cheap up to a point (a few thousand per
  broker); repartitioning a live topic is not. 24 partitions for a topic that needs 8 today gives
  you room for 3× growth in consumer parallelism with no migration.
- **Autoscale consumers on lag, not on CPU.** KEDA's Kafka and SQS scalers do this. CPU is the
  wrong signal for a consumer blocked on a downstream write, which is most consumers.
- **Capacity-plan the consumer against peak production, not average.** Or explicitly accept a
  drain window and state it: "at flash-sale peak we fall behind by up to 2,440/s for 20 minutes,
  accumulating 2.9M messages, draining in 48 minutes, so worst-case order latency is 68 minutes."
  That is a decision. Discovering it during a sale is not.

### Q-04 · The backlog drains into a downstream that cannot take it

**What you see.** The consumer is fixed, lag starts falling — and now the database, or the
downstream API, or the third-party service falls over.

**Mechanism.** `F-09`, and it is the most predictable second outage in this collection.

During the outage, 2.9 million messages accumulated. The consumer is restored and scaled up to
24 pods to catch up. It now processes at its maximum rate — 1,920 messages/s — into `orders-db`,
which has been receiving 640/s and is sized for maybe 1,200/s.

**The recovery generates 3× the normal write load, sustained for 25 minutes.** Nothing about the
original incident did that; the recovery did.

The third-party version is worse, because you cannot scale their side: draining a backlog of
email notifications at maximum rate gets you rate-limited, blocked, or classified as a spam
source — and the block outlasts the backlog.

**Prevent.** **Rate-limit the drain deliberately.** Cap consumer throughput at a multiple of
steady state — 1.5× or 2× — so recovery is slower and guaranteed:

```
Backlog: 2,900,000 messages
Steady production: 640/s
Drain at 2× steady state = 1,280/s, of which 640/s is new production
Net drain rate: 640/s
Time to clear: 2,900,000 / 640 = 4,531 s = 75 minutes
```

Seventy-five minutes of known, safe recovery beats twenty-five minutes of recovery that takes the
database down and restarts the incident. **Compute this number before you need it**, because
during the incident someone will ask "how long?" and "we are scaling up to go as fast as
possible" is the wrong answer.

Implement it as a token bucket in the consumer, or by limiting `max.poll.records` and the
consumer count. And make the drain rate a configurable value you can raise if the downstream
turns out to be fine.

### Q-05 · The poison message

**What you see.** A consumer that appears to be running and is making no progress. Lag grows.
CPU may be high (a crash loop) or low (a stuck retry). The same log line repeats.

**Mechanism.** One message cannot be processed — malformed payload, a schema it does not
understand, a referenced entity that does not exist, a bug triggered by an edge case. The
consumer fails and does not commit the offset. The broker redelivers the same message. Forever.

**Head-of-line blocking** is what makes this severe: in an ordered partition, the consumer cannot
skip ahead. One bad message blocks every message behind it in that partition. A poison message in
partition 7 of 24 stops 1/24 of your traffic completely — and if the partition key is the
customer ID, it stops a specific set of customers entirely while everyone else is fine, which is
a confusing support pattern.

The crash-loop version escalates it to `F-07`: if the message crashes the process rather than
throwing, the pod restarts, re-reads the same message, and crashes again, and the whole consumer
group can end up in `CrashLoopBackOff`.

**Confirm it.** The consumer's committed offset is not advancing while messages are available.

```bash
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group order-processor-group
# A partition whose CURRENT-OFFSET is static while LOG-END-OFFSET grows is the one.
```

Then read the message at that offset:

```bash
kafka-console-consumer.sh --bootstrap-server $BS --topic orders.created \
  --partition 7 --offset 4821993 --max-messages 1 --property print.headers=true
```

**Recover.** In order of preference: fix the consumer to handle the message; move the offset past
it (`kafka-consumer-groups.sh --reset-offsets --to-offset N --execute`, having first copied the
message somewhere for later analysis); or, if the message class is broad, deploy a version that
routes unparseable messages to a DLQ.

⚠️ Skipping an offset discards the message permanently. Copy it first. For a payment or an order,
skipping is a correctness failure, and the right answer is to fix the consumer.

**Prevent.**

- **Bounded retries with a dead-letter queue.** A message that has failed N times (3–5) is routed
  to a DLQ and the offset is committed. The partition unblocks. This is the single most important
  defence and it is the difference between one lost message and a stopped pipeline.
- **Distinguish retryable from non-retryable failures**, exactly as in doc 02's error table. A
  deserialisation error will never succeed — send it to the DLQ on the first attempt. A
  downstream timeout might — retry that one. Retrying a permanent failure N times just delays
  the inevitable and consumes capacity.
- **Never let message processing crash the process.** Catch at the message boundary.
- **Validate at the producer.** A schema registry with compatibility enforcement stops most
  poison messages from being written at all, which is a much better place to stop them.

### Q-06 · The dead-letter queue nobody reads

**What you see.** A DLQ with 400,000 messages in it, the oldest from fourteen months ago.

**Mechanism.** The DLQ solved `Q-05` by moving the problem somewhere the pipeline does not care
about. If nothing consumes or monitors the DLQ, those messages are lost — with an extra step that
makes everyone feel the problem was handled.

For `orders.created`, every message in the DLQ is a customer order that was never fulfilled.

**Prevent.** A DLQ is a work queue for humans and it needs the same treatment as any other:

- **Alert on DLQ depth > 0**, not on some threshold. A single dead-lettered order is worth
  knowing about. If your DLQ routinely contains messages, either the threshold should be a
  different number for a documented reason, or you have a bug you have normalised.
- **Alert on DLQ *age*** as well as depth, so a slow trickle does not hide under a depth
  threshold.
- **A replay mechanism that exists and is tested.** After fixing the bug, you need to reprocess
  the DLQ — which requires the messages to have retained enough context (original topic,
  partition, offset, headers, failure reason, attempt count) to be replayed. Design the DLQ
  record format for replay, not just for storage.
- **A per-message owner.** The DLQ record should carry enough to route it to whoever can act:
  which consumer failed, with what error.
- **Retention long enough to act** — 14 days minimum, and remember that a DLQ with a 7-day
  retention silently deletes evidence of a problem you have not noticed yet.

### Q-07 · Retry topics that loop

**What you see.** Messages circulating between a main topic and retry topics indefinitely. Volume
that is mostly retries.

**Mechanism.** The retry-topic pattern (fail → publish to `topic.retry.5m` → a delayed consumer
republishes to the main topic) is a good pattern, and it has two failure modes:

- **No attempt counter**, so a permanently-failing message loops forever, and each loop adds load.
- **The retry consumer publishes back to the main topic**, so the retried message is now behind
  all the new messages *and* competes with them. At high failure rates the main topic fills with
  retries and new messages are starved.

**Prevent.** Carry an attempt count in a header, increment it on each retry, and route to the DLQ
at the maximum. Use escalating delay tiers (`retry.1m`, `retry.5m`, `retry.30m`) so a struggling
downstream is not hammered. Cap total retry volume as a fraction of main-topic volume — the same
retry-budget idea as doc 02, applied to queues.

### Q-08 · Ordering that was never guaranteed

**What you see.** Events applied in the wrong order, producing a wrong final state.

**Mechanism.** Ordering guarantees are narrower than people assume, and the assumption is usually
implicit. What you actually get:

| System | Ordering guarantee |
|---|---|
| Kafka | **Within one partition only.** Across partitions: none. |
| SQS standard | **None at all.** Not even approximately. |
| SQS FIFO | Within a message group ID; throughput-limited (300 tps, or 3,000 with batching) |
| RabbitMQ | Per queue, with a single consumer. Multiple consumers break it. |
| Google Pub/Sub | Per ordering key, if you enable it |
| Kinesis | Per shard |

And even with per-partition ordering, four things break it downstream: parallel processing inside
the consumer (a worker pool), retries (a failed message reprocessed after later ones), a
consumer that batches and processes out of order, and any repartitioning.

**Prevent.** Doc 07 (`T-10`) has the full treatment. The short version: partition by entity key
for ordering *and* apply with a version guard for correctness, because the version guard is what
survives retries, duplicates, and replays — and the partitioning alone does not.

### Q-09 · Duplicate delivery to a consumer that assumed otherwise

**What you see.** Doubled effects: two emails, two ledger entries, a counter that is too high.

**Mechanism.** Every broker worth using is at-least-once by default. Duplicates occur when: the
consumer processes a message and crashes before committing the offset; a rebalance reassigns a
partition mid-batch; the producer retries a send whose ack was lost; a replay is run.

This is not an error condition — it is the contract. A consumer that is not idempotent is
incorrect, not unlucky.

**Prevent.** Doc 07 (`T-05`, `T-14`). Deduplicate in the consumer's own store, in the same
transaction as the effect. That is the only place the guarantee actually holds.

### Q-10 · No end-to-end visibility across the queue

**What you see.** A trace that ends at the producer and a separate trace that starts at the
consumer, with no link between them. Debugging requires correlating by timestamp and hope.

**Mechanism.** Distributed tracing propagates context in request headers. A queue breaks the
request, so unless the context is explicitly carried in the message, the causal chain is lost.

**Prevent.** Inject the W3C `traceparent` into message headers at produce time and extract it at
consume time. Every major tracing SDK supports this and it is a few lines of middleware. Then
model the consumer's work as a span with a **link** to the producer's span rather than as a child
(they are causally related but not synchronously nested, and the distinction matters for how the
trace renders).

Also carry, in headers: the producing service and version, a message ID, a schema version, and
the attempt count. All four are things you will want during an incident and cannot reconstruct
afterwards.

### Q-11 · Fan-out amplification

**What you see.** One user action produces a load spike disproportionate to the action. The event
bus carries far more traffic than anyone modelled.

**Mechanism.** An event triggers consumers, each of which emits events, which trigger more
consumers. The amplification is multiplicative, exactly like doc 02's retry amplification and
equally invisible to any individual team.

Riverbend: one `order.created` event fans out to 11 consumers. Three of them publish their own
events (`inventory.reserved`, `invoice.generated`, `loyalty.accrued`), each consumed by 4–6 more
services, two of which publish again.

```
Level 0: 1 event
Level 1: 11 consumers, 3 publish        → 3 events
Level 2: 3 × 5 = 15 consumers, 2 publish → 2 events
Level 3: 2 × 4 = 8 consumers
Total message deliveries per order: 1 + 11 + 15 + 8 = 35
```

At 67 orders/s that is 2,345 message deliveries/s from 67 user actions. At the flash-sale peak of
350 orders/s it is 12,250/s. Nobody designed a 35× amplifier; it accumulated one consumer at a
time.

The catastrophic version is a **cycle**: service A's event triggers B, whose event triggers A.
With no cycle detection, this is an infinite loop that saturates the broker in seconds and is
very hard to diagnose because each individual hop looks correct. This is `R-16` in event form,
and it is easier to create accidentally because nobody has to write the second half deliberately.

**Confirm it.** Build the event-flow graph from actual traffic (topic → consumer group → topics
that group produces to) rather than from documentation, and compute amplification per root event.
Look explicitly for cycles.

**Prevent.** Treat the event graph as an architectural artefact that is reviewed. Detect cycles
in CI. Put a hop-count header on every event and drop (loudly) anything exceeding a maximum —
this is the TTL field in IP, and for the same reason. And resist the pattern where every service
publishes an event for every state change "in case someone needs it": each one is a permanent
contract and a multiplier.

### Q-12 · Schema evolution that breaks consumers

**What you see.** A producer deploys; consumers start failing. Or, worse, consumers keep working
and silently drop a field.

**Mechanism.** The event is a contract between a producer and N consumers who deploy
independently, on their own schedules, some of whom the producer team does not know about.

The breaking changes, in order of how often they cause incidents:

| Change | Breaks |
|---|---|
| Remove a field | Consumers that read it |
| Rename a field | Everyone (it is a remove plus an add) |
| Change a type (`int` → `string`) | Everyone deserialising strictly |
| Add a required field | Old *producers* (if consumers validate) and replays of old messages |
| Change the meaning of a field without changing its name | **Everyone, silently** — the worst case, because nothing errors |
| Change the unit (seconds → milliseconds) | Everyone, silently, and this has caused real financial incidents |

**Prevent.**

- **A schema registry with enforced compatibility.** Backward compatibility (new consumers can
  read old messages) is the minimum; **full compatibility** (both directions) is what you want for
  events, because consumers and producers deploy independently in both orders.
- **Only ever add optional fields, and never reuse a field name or a tag number.** This is
  Protobuf's discipline and it is correct for JSON and Avro too.
- **Version the schema in a header**, so consumers can branch and so replays of old messages work.
- **For a genuinely breaking change, publish a new topic**, migrate consumers one at a time, and
  retire the old topic when its consumer count reaches zero. This costs a topic and removes the
  coordinated-deploy requirement entirely.
- **Never change a unit or a meaning in place.** Add `amount_minor_units` alongside `amount`; do
  not redefine `amount`. Silent semantic changes produce correctness failures that reconciliation
  finds months later.

Doc 11 (`G-12`) covers contract change more generally, including the synchronous case.

### Q-13 · Time-based work at scale: the timer problem

**What you see.** Delayed or scheduled work firing late, all at once, or not at all.

**Mechanism.** "Cancel this order if unpaid in 30 minutes" and "retry this in 5 minutes" and
"send this reminder tomorrow" are all the same problem: a large number of timers, each of which
must fire approximately once at approximately the right time.

The implementations and their failures:

- **A database polled for due rows.** `SELECT ... WHERE due_at < now() LIMIT 1000` every second.
  Simple and correct, and it becomes a hot query on a hot index as volume grows, plus `S-02`'s
  right-edge contention on the `due_at` index. Workable to maybe tens of thousands of pending
  timers.
- **Per-item delayed messages** (SQS delay, RabbitMQ TTL+DLX, Kafka with a delay topic). Each has
  a maximum delay (SQS: 15 minutes) and per-message cost.
- **In-memory timers.** Fastest, and **lost entirely on restart**. Acceptable only for timers
  whose loss is harmless.
- **A dedicated scheduler** (a timing wheel, Quartz, Temporal). Correct and operationally
  heavier.

The two failure modes all of them share:

1. **Synchronised firing.** A million orders placed during a sale all have a 30-minute expiry, so
   a million timers fire in the same second — `F-02` in the timer layer. Jitter the deadline
   (30 minutes ± 2) and rate-limit the firing.
2. **The timer fires but the work does not happen**, and there is no second chance because the
   timer is consumed. Timers must be idempotent and re-derivable: better to store the *deadline*
   as state and sweep for expired rows than to rely on a fired-once event. Then a missed sweep is
   caught by the next one.

The design that holds up: **store the deadline as a column, sweep for expiries, and make the
sweep idempotent.** It is boring, survives restarts, is queryable ("how many orders are about to
expire?"), and degrades to "slightly late" rather than "never" — and `T-07`'s expiry-checked-on-
read makes correctness independent of the sweeper running at all.

### Q-14 · The queue that is a database

**What you see.** A broker used as long-term storage, with consumers reading from the beginning.
Or a "queue" that is actually a table being polled.

**Mechanism.** Kafka's retention makes it tempting to treat a topic as a source of truth. That is
a legitimate architecture (event sourcing) and it has requirements most teams using it
accidentally do not meet: infinite or compacted retention, a schema you can still read in five
years, snapshots so you do not replay from the beginning, and a plan for how a new consumer
bootstraps without reading 4 TB.

The accidental version: retention is 7 days, someone builds a consumer that assumes full history,
and it works in testing (where the topic is 3 days old) and fails in production.

**Prevent.** Decide explicitly whether a topic is a **transport** (short retention; the source of
truth is a database) or a **log** (long or compacted retention; the topic is the source of
truth). Write it down per topic. They need different retention, different schema discipline,
different backup, and different bootstrap paths. See
[`../Kafka/07-retention-compaction-and-schema.md`](../Kafka/07-retention-compaction-and-schema.md).

## The async monitoring set

The synchronous defaults (error rate, latency, saturation) do not work here. This is the
replacement set, and it is short enough to implement completely.

| Signal | Why | Alert on |
|---|---|---|
| **Age of oldest unprocessed message** | The only signal that detects every failure in this doc | Above the SLO (e.g. 5 min for orders) |
| **Lag derivative** | Distinguishes a burst (will drain) from a deficit (will not) | Positive for > 15 min |
| **Successful-process rate** | Detects a stopped consumer even when the queue is empty | Zero for longer than the expected quiet period |
| **DLQ depth and age** | Every DLQ message is lost work | Depth > 0 |
| **Consumer group member count** | Detects partial assignment and rebalance loops | Below expected |
| **Rebalance rate** | A group rebalancing continuously processes nothing | > 1 per 10 min |
| **End-to-end canary latency** | Tests the whole path including the consumer's downstream | Above SLO |
| **Duplicate rate** | Detects a broken offset commit before it becomes a correctness incident | Above baseline |
| **Amplification factor** | Messages produced per root event | Growing month over month |

The two most valuable of these are the first and the last-but-one: **age of oldest** catches the
outage, and **the canary** catches the case where the queue looks empty because the producer also
stopped.

## What to take away

1. **An async failure has no error rate, no latency, and idle CPU.** Every dashboard is green
   while no work is being done, which is why async incidents last hours and synchronous ones last
   minutes.
2. **A queue does not add capacity.** It absorbs a burst (a temporary excess with an end) and
   does nothing about a deficit (a permanent one) except delay the moment you find out.
3. **Age of the oldest unprocessed message is the one alert that detects the whole class.** If you
   add one thing from this doc, add that.
4. **Lag in messages is meaningless across topics** — 10,000 messages is 0.3 s on one topic and
   4 minutes on another. Alert on time, and express the SLO in time.
5. **Consumer parallelism is capped by partition count.** Over-partition from the start;
   repartitioning a live topic is painful and breaks key ordering.
6. **Autoscale consumers on lag, not CPU.** A consumer blocked on a slow downstream has low CPU
   and infinite lag.
7. **Compute your drain time before the incident and rate-limit the drain.** A backlog released at
   maximum rate is 3× normal load into a downstream sized for normal — the most predictable second
   outage there is. Seventy-five safe minutes beats twenty-five that re-break the database.
8. **A poison message blocks its whole partition.** Bounded retries plus a DLQ, distinguishing
   retryable from permanent failures, and never letting a message crash the process.
9. **Alert on DLQ depth > 0, not on a threshold.** Design the DLQ record for *replay* — original
   topic, offset, headers, error, attempt count — and test the replay path.
10. **Ordering guarantees are narrower than you think** and are broken downstream by parallel
    consumers, retries, and batching. Partition by entity key *and* guard application by version.
11. **At-least-once is the contract, so a non-idempotent consumer is incorrect, not unlucky.**
    Deduplicate in the consumer's own store, in the same transaction as the effect.
12. **Propagate `traceparent` in message headers**, or your traces stop at the queue and every
    async incident becomes timestamp archaeology.
13. **Event fan-out multiplies**: Riverbend's 35 message deliveries per order accumulated one
    consumer at a time. Build the event graph from real traffic, detect cycles in CI, and carry a
    hop count.
14. **An event schema is a permanent contract with consumers you do not know about.** Enforce full
    compatibility, only add optional fields, and never change a unit or a meaning in place — that
    last one produces silent correctness failures that reconciliation finds months later.
15. **Store deadlines as state and sweep, rather than relying on fired-once timers**, and jitter
    every deadline so a million orders do not expire in the same second.

Next: [10-state-coordination-and-time.md](10-state-coordination-and-time.md), which covers what
happens when two processes both believe they are responsible for the same thing — and why the
distributed lock you wrote is probably not one.
