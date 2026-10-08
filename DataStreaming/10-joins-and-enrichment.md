# Joins and Enrichment

Scenarios `J-01` … `J-11`.

In a database, a join is a query-planner decision you mostly do not think about. In a stream, a
join is a **decision about how much history to hold in memory, and for how long**, and getting
it wrong is how a 200 MB job becomes a 4.2 TB one.

There are four distinct things called "join" in streaming, they have nothing in common
operationally, and the first step in any design discussion is establishing which one is meant.

## 1. The four joins

### 1. Stream-to-stream, windowed

Two unbounded streams, matched on a key, within a time bound.

```
impressions ──┐
              ├──► "which impression caused this click?"
clicks ───────┘     match on ad_id, click within 24 h of impression
```

State: **both streams, for the window duration.** This is the expensive one.

### 2. Stream-to-table, temporal (versioned)

A stream joined against a slowly-changing table, **as of the stream record's event time**.

```
orders ──────┐
             ├──► "what was this SKU's price when the order was placed?"
price CDC ───┘     not "what is it now"
```

State: **every version of every row in the retention window.** More than you expect, because you
need history, not just current state.

### 3. Stream-to-table, processing-time lookup

A stream joined against whatever the table says *now*.

```
events ──────┐
             ├──► "what is this member's current segment?"
member store ┘     as of the moment we looked
```

State: **zero**, if you look up remotely. Or the whole table, if you materialise it.

### 4. Stream-to-stream, interval

A special case of (1) where the bound is relative and asymmetric: "a payment between 0 and 30
minutes after its order."

State: both streams for the interval, but the asymmetry lets the engine expire one side sooner.

**The most common design error is using (1) when (2) or (3) would do**, because "join" is one
word and the state cost differs by three orders of magnitude.

## 2. Stream-to-stream: the state arithmetic

The formula:

```
state = (rate_left × window × bytes_left) + (rate_right × window × bytes_right)
      × RocksDB space amplification (≈ 1.23)
```

Worked for `corridor-ad-attribution`, Meridian's largest job:

```
left  — ad impressions:  180,000/s × 86,400 s × 220 B = 3.42 TB
right — ad clicks:         2,400/s × 86,400 s × 180 B = 0.037 TB
                                                        ────────
                                                        3.46 TB
× 1.23                                                  4.25 TB
```

Two things fall out immediately.

**The state is entirely the impressions side.** Clicks are 1.3% of the impression rate, so they
contribute 1% of the state. Any optimisation must attack the impressions.

**The window dominates linearly.** Halving the attribution window from 24 hours to 12 halves the
state. That is a *product* decision — how long after seeing an ad does a click still count? — and
it is worth putting the 2.1 TB saving in front of whoever owns it. On Meridian that conversation
happened, the answer was "7 days for brand campaigns", and the compromise was a 24-hour
streaming window plus a nightly batch pass over the lake for the long tail. Which is the lambda
pattern, arrived at honestly, for a reason —
[17](17-reprocessing-backfill-and-the-lambda-question.md) discusses when that is the right call.

### Reducing stream-to-stream state

Four techniques, in order of how much they save.

**Project before joining.** The impression record is 640 bytes; the attribution join needs
`ad_id`, `campaign_id`, `member_id`, `surface`, `timestamp` — 220 bytes. Projecting first is a
2.9× saving and it is one line:

```java
impressions
  .map(e -> new ImpressionKey(e.getAdId(), e.getCampaignId(), e.getMemberId(),
                              e.getSurface(), e.getEventTime()))
  .keyBy(ImpressionKey::getAdId)
  ...
```

**Always do this, always, before any keyed operator.** It reduces state, shuffle bytes,
serialization cost and checkpoint size simultaneously. It is the cheapest optimisation in
streaming and it is skipped constantly because the full record is convenient.

**Filter before joining.** Only 9.7% of `corridor.tracking.events` are ad impressions. Filtering
before the `keyBy` means the shuffle carries 180,000/s instead of 1,850,000/s — a 10× reduction
in network *and* in the work the join operator does.

The ordering rule: **filter, then project, then key, then join.** Each step should reduce the
data before the next. Reversing any two costs you the difference.

**Asymmetric expiry.** In an interval join `click.time BETWEEN impression.time AND impression.time
+ 24h`, the click side only needs to be retained long enough to match an impression that has not
yet arrived — which, since clicks always follow impressions, is zero. Flink's `IntervalJoin`
computes these bounds and expires each side independently. Expressing the join as an interval
join rather than a window join saves the entire click-side state, which here is 1% and elsewhere
can be half.

**Sketch instead of storing.** If the join's purpose is a count rather than an enumeration, a
sketch may suffice. "How many clicks per campaign were attributable?" does not need every
impression; it needs a structure that can answer "was `ad_id` shown?" — a Bloom filter over
impression IDs, at 1.44 bits per element:

```
15.552e9 impressions × 1.44 bits ÷ 8 = 2.8 GB    (against 3.42 TB)
```

A factor of 1,221, at the cost of 0.1% false positives — attributing clicks to campaigns that
did not show the ad. For a billing system that is unacceptable; for a "campaign reach" dashboard
it is noise. **Ask what the join's output is used for before assuming it needs to be exact.**

## 3. Temporal joins: the one people get wrong silently

A temporal join answers "what did the table say **at the time of this event**", which is
different from "what does the table say now", and the difference is invisible in a live pipeline
and catastrophic in a backfill.

The example that makes it concrete. A SKU's price:

```
10:00   price = $24.99
14:30   price = $19.99  (sale starts)
22:00   price = $24.99  (sale ends)
```

An order placed at 15:10 should join to $19.99. With a **processing-time lookup**, a live
pipeline running at 15:10 gets $19.99 (correct, by luck), and a **backfill run tomorrow gets
$24.99** — because "now" is tomorrow, and the sale is over. The backfill silently produces
different, wrong revenue figures for every order during the sale.

This is the deepest reason streaming and batch results diverge (`W-07`), and it is invisible
until someone reconciles.

### What a temporal join requires

**Versioned history, not current state.** You need every version with its validity interval:

```
sku_id   price    valid_from   valid_to
K-8841   24.99    09:00        14:30
K-8841   19.99    14:30        22:00
K-8841   24.99    22:00        ∞
```

This comes from a CDC stream ([18](18-cdc-and-streaming-from-databases.md)) or from an
application that emits price-change events. It does **not** come from periodically snapshotting
a table, because snapshots miss changes between snapshots.

**A watermark on the table side.** The join must know that the table has caught up to the
stream's event time before it can answer. That means the CDC stream needs its own watermark, and
the join's watermark is the minimum of both (`E-09`), so a stalled CDC stream stalls the join.

**State proportional to versions in the retention window**, not to the number of rows:

```
2.4 million SKUs × average 3.1 price changes per week × 1 week retention × 180 B
  = 1.34 GB
```

Whereas current-state-only would be `2.4M × 180 B = 432 MB`. Three times more, which is
affordable — but for a table with high churn the multiplier is large and worth computing.

### When processing-time lookup is fine

It is fine when the table is genuinely static for the purposes of the computation, or when the
answer only needs to be right at the time it is computed and will never be recomputed. A member's
country for a live geo-dashboard: fine. A member's country for a billing report that will be
regenerated: not fine.

**The test:** *if I re-run this job over the same input tomorrow, must I get the same answer?*
If yes, you need a temporal join. If no, a lookup is fine and much cheaper.

## 4. Lookup joins and the async I/O arithmetic

A lookup join calls out to an external store per record. The naive implementation is synchronous
and it is disastrous:

```
sync lookup, 8 ms p99 to DynamoDB
one subtask processes: 1 ÷ 0.008 = 125 records/s
to process 1,850,000/s you need 14,800 subtasks
```

Fourteen thousand eight hundred. The job is not viable.

**Async I/O** fixes it by allowing many in-flight requests per subtask:

```
capacity 200 in flight, 8 ms p99
one subtask processes: 200 ÷ 0.008 = 25,000 records/s
to process 1,850,000/s you need 74 subtasks
```

A factor of 200, exactly the capacity. **Throughput is capacity ÷ latency, and the latency
cancels out of the machine count** — which is why async lookup can beat materialised state on a
per-core basis (`K-10`).

### The four things that make async I/O go wrong

**Unordered versus ordered.** `AsyncDataStream.unorderedWait` emits results as they complete,
which is faster and reorders records. `orderedWait` preserves order by buffering completed
results until their predecessors finish — so one slow request stalls everything behind it, and
the buffer grows. Use unordered unless you have a specific ordering requirement (`D-07`).

**Capacity is a memory bound.** 200 in-flight requests, each holding the input record and a
future, is 200 × (record size + overhead) per subtask. At 640-byte records and 360 subtasks:

```
200 × 700 B × 360 = 50 MB
```

Trivial here. At capacity 5,000 and 4 KB records it is 7.2 GB and it is not trivial.

**The timeout must be set and must have a defined action.** Flink's async operator requires a
timeout; the default action on timeout is to fail the job. For a lookup where a miss is
tolerable, override `timeout()` to complete with a null or a default and count it:

```java
@Override
public void timeout(Event input, ResultFuture<Enriched> rf) {
    lookupTimeoutCounter.inc();
    rf.complete(Collections.singleton(Enriched.withoutLookup(input)));
}
```

**Without this override, one slow DynamoDB partition restarts your job.**

**Backpressure from the remote store is now your problem.** If DynamoDB throttles, your capacity
fills, the operator backpressures, and the whole job slows. That is correct behaviour, but it
means **the remote store's availability is now part of your job's availability**, which was not
true before you added the lookup. `J-05` covers what to do about it.

### Caching in front of the lookup

Most lookup workloads are skewed — the same keys repeat. A local cache in the operator turns
most lookups into memory hits:

```
member segment lookup, measured access distribution:
  top 1% of members = 34% of lookups
  top 10%           = 71% of lookups

cache of 100,000 entries per subtask (of 100M members):
  hit rate measured: 68%
  effective lookup rate: 1,850,000 × 0.32 = 592,000/s
  subtasks needed: 592,000 ÷ 25,000 = 24 (was 74)
```

The cache costs memory (100,000 × 120 B = 12 MB per subtask) and introduces staleness bounded by
the TTL. **Set a TTL, always, and make it a stated property** — "member segments are up to 5
minutes stale in this pipeline" belongs in the stream contract.

⚠️ A cache also makes your job non-deterministic under replay: the same record processed twice
may hit a cache with different contents. If the job must be replay-deterministic, a cache
disqualifies it — use a temporal join instead.

## 5. Broadcast state: small tables, every subtask

For a table small enough to fit in every subtask's memory — under a few hundred megabytes — the
broadcast pattern replicates it everywhere and removes the join entirely:

```java
MapStateDescriptor<String, Rule> rulesDesc = ...;
BroadcastStream<Rule> rules = ruleStream.broadcast(rulesDesc);

events.connect(rules)
      .process(new BroadcastProcessFunction<>() {
          public void processElement(Event e, ReadOnlyContext ctx, Collector<Out> out) {
              Rule r = ctx.getBroadcastState(rulesDesc).get(e.getType());
              ...
          }
          public void processBroadcastElement(Rule r, Context ctx, Collector<Out> out) {
              ctx.getBroadcastState(rulesDesc).put(r.getType(), r);
          }
      });
```

Excellent for: feature flags, detection rules, small lookup tables, A/B test assignments,
configuration.

Three hazards:

**There is no ordering guarantee between the broadcast side and the data side.** A rule update
and an event arriving "at the same time" may be processed in either order, and different
subtasks may resolve it differently. So **the same event processed by two subtasks at the same
wall-clock moment can get different rules.** For most uses this is acceptable; for anything where
a rule change must apply atomically across the fleet, it is not, and the fix is to version the
rules and have events carry the version they expect.

**Broadcast state is not keyed**, so it cannot be redistributed on rescale by key. Flink handles
it by giving every new subtask the full copy, which is correct and means the state is duplicated
`parallelism` times in the checkpoint:

```
200 MB of rules × 360 subtasks = 72 GB in the checkpoint
```

That is a real cost and it caps the practical size well below "what fits in memory."

**Bootstrapping is manual.** When the job starts, the broadcast state is empty until the control
stream delivers it. Events processed in the meantime see no rules. Either the control topic is
compacted and read from the beginning (so bootstrap is a full read), or the job must block until
bootstrapped — and Flink has no built-in way to express "wait for the broadcast side." The usual
implementation buffers data-side records in keyed state until a `bootstrap_complete` marker
arrives on the control stream.

## 6. Scenarios

### J-01 — The join produces nothing

**What you see.** A stream-stream join emits zero records. Both inputs are flowing.

**Mechanism.** In order of frequency:

1. **The watermark is stalled on one side** (`E-09`), so the join never advances and never
   emits. The state grows, which is the distinguishing symptom.
2. **The keys do not actually match.** One side keys on `ad_id` as a string with a prefix
   (`"ad-8841"`) and the other as a bare number (`8841`). The join is structurally correct and
   semantically empty.
3. **The time bound excludes everything.** An interval join with the sign reversed —
   `click.time BETWEEN impression.time - 24h AND impression.time` — asks for clicks *before* the
   impression, which never happens.
4. **The two streams are not co-partitioned and Flink is joining subtask-locally.** This happens
   with a manual implementation, not with the DataStream/Table join APIs, which shuffle
   correctly.

**Confirm it.** For cause 2, which is the most common and the most embarrassing, sample the keys
from both sides and compare:

```
left  keys sample:  ad-8841, ad-9902, ad-1183
right keys sample:  8841, 9902, 1183
```

**Prevent.** A canary: inject a matching pair through both sides and assert it joins. Run it
continuously. A join that silently produces nothing is otherwise indistinguishable from one
whose inputs have no matches.

### J-02 — The join state grows without bound

**What you see.** State grows linearly, indefinitely, on a windowed join.

**Mechanism.** Window-based joins expire state based on the watermark. If the watermark does not
advance (`E-01`, `E-03`), nothing expires. So this is usually a watermark failure wearing a
state-growth costume.

The other cause: a **regular join** (Flink SQL's plain `JOIN` without a time attribute), which
has *no* expiry at all. It keeps both sides forever, because a row from either side might match
a row arriving in five years. Flink offers `table.exec.state.ttl` to bound it, and **that TTL
defaults to zero, meaning infinite**.

**Prevent.** In Flink SQL, always set `table.exec.state.ttl` explicitly. In the DataStream API,
use `intervalJoin` or a windowed join, never a hand-rolled `CoProcessFunction` without a timer
that clears state.

### J-03 — Unmatched records vanish

**What you see.** An inner join between orders and payments. 3% of orders never get a payment
(cancelled, failed, abandoned), and those orders simply do not appear in the output. The
business wanted to count them.

**Mechanism.** An inner join emits only matches. The unmatched side is silently dropped when its
window expires.

**Prevent.** **Use an outer join and handle the null side explicitly**, or use a
`CoProcessFunction` with a timer:

```java
// on left record: store it, register a timer at eventTime + window
// on right record: if left exists, emit joined, cancel timer, clear
// on timer fire:   emit left with null right, clear
```

That timer-fire branch is the entire value of the pattern: **it converts "nothing happened" into
a record**, which is the only way a downstream system can count it. An inner join makes
non-events invisible, and non-events are frequently what the business is asking about.

This is the same principle as `E-08`'s late-record counting and `W-05`'s empty windows: **make
absence explicit.**

### J-04 — Duplicate matches from a one-to-many join

**What you see.** Joining orders to shipments produces four rows per order and the revenue sum is
4× too high.

**Mechanism.** An order has four shipments. The join is correct; the aggregation downstream
assumed one row per order.

**Prevent.** Establish the cardinality of every join in the design — one-to-one, one-to-many,
many-to-many — and state what the output grain is. `S-11`'s stream contract should name the
grain of every derived stream: "one row per (order_id, shipment_id)", not "orders joined to
shipments."

### J-05 — The lookup store is down and the job is down

**What you see.** DynamoDB has elevated latency for eight minutes. Three Flink jobs restart.

**Mechanism.** The async lookup's timeout action defaulted to failing the job, or the timeout was
long enough that capacity filled and backpressure propagated to the source.

**Prevent.**

- **Timeout action that degrades rather than fails**, with a counter (Section 4).
- **A circuit breaker around the lookup.** After N consecutive failures, stop calling for a
  cooldown period and emit records unenriched with a flag. The flag matters: downstream must be
  able to see that enrichment was skipped, or you have silently changed the data's meaning.
- **A local fallback.** For a lookup whose values change slowly, a stale cache is much better
  than nothing. Serve from cache past its TTL when the store is unavailable, and count it.
- **Decide what unenriched means.** Drop the record? Emit with nulls? Emit to a retry topic for
  later enrichment? All three are valid and the choice must be made before the outage, because
  during the outage there is no time.

### J-06 — Enrichment data arrives after the events it should enrich

**What you see.** A new member signs up and their first 40 events are enriched with null profile
data, because the profile CDC record had not arrived yet.

**Mechanism.** Two streams, no ordering between them (`T-10`). The event stream is faster than
the CDC stream, which goes through a database commit, a CDC connector, and a separate topic.

**Prevent.** Three options with different costs:

- **A temporal join with a proper watermark**, which forces the event stream to wait until the
  table side has caught up. Correct, and it adds the CDC stream's lag to the pipeline's latency.
- **A retry path.** Unenriched records go to a delay topic and are retried after 30 seconds. Most
  succeed. Cheap, and it makes the output arrive out of order.
- **Accept it and mark it.** Emit with `enrichment_status: "not_found"` and let downstream decide.
  Combined with a nightly reconciliation this is often the right trade.

The wrong option is to emit nulls with no marker, which is what happens by default.

### J-07 — A broadcast rule update applied inconsistently

**What you see.** A detection rule was disabled. For ninety seconds, some records were evaluated
against it and some were not, with no pattern.

**Mechanism.** Section 5's first hazard: no ordering between broadcast and data sides, and each
subtask receives the broadcast update at a slightly different moment.

**Prevent.** For rules where atomicity matters, **version the rule set and have records specify
the version**:

```
rule set v41 published at 10:00:00, effective from 10:05:00
records with event_time >= 10:05:00 use v41
records before use v40
```

Each subtask holds both versions until every record before the boundary has passed, then
discards the old one. This makes the switch deterministic on event time rather than on
processing time, and it makes the job replay-deterministic — which the naive version is not.

### J-08 — A join that should have been a union

**What you see.** A "join" of `orders.created` and `orders.paid` on `order_id` holds 200 GB of
state.

**Mechanism.** The two streams are lifecycle events of the same entity, and what is actually
wanted is a *state machine* fed by both, not a join. A state machine keyed by `order_id` holds
one record per in-flight order (68 MB, from
[07](07-state-the-state-backend-and-keying.md) Section 4); a join holds every record from both
sides for the window.

**Prevent.** Ask whether the output is "rows where both sides matched" (a join) or "the current
status of each entity, updated by whichever event arrives" (a state machine). If the streams are
lifecycle events of one entity, it is a state machine, and they should probably have been on one
topic anyway (`S-03`).

### J-09 — Co-partitioning avoided a shuffle, and then a repartition destroyed it

**What you see.** A job that was designed to avoid a shuffle has a shuffle.

**Mechanism.** Two topics keyed identically with the same partition count can, in principle, be
joined without a network shuffle — subtask *i* reads partition *i* of both. Flink's Table API
does not exploit this automatically for Kafka sources; the DataStream API does not either,
unless you use `reinterpretAsKeyedStream`, which asserts to the runtime that the stream is
already correctly partitioned.

```java
DataStreamUtils.reinterpretAsKeyedStream(source, Event::getOrderId)
```

⚠️ This is an assertion the runtime cannot check. If it is wrong — different partition counts,
different hash functions, a different key — records go to the wrong subtask and the results are
silently incorrect. Producer and consumer must use the same hash (Kafka's `murmur2` versus
Flink's `murmur3` are different), which in practice means a custom partitioner on the producer
side.

**The saving:** at Corridor's 1.85 million events a second and 330 bytes, avoiding one shuffle
saves `2,376 MB/s × 2/3 cross-zone × $0.02/GB` monthly, which is part of the $41,656 in
[01](01-requirements-and-the-capacity-model.md) Section 9. Real money, real risk. Meridian uses
it on two jobs, both with an assertion in the job's startup that samples records and verifies
the partition assignment.

### J-10 — The join's watermark comes from the wrong side

**What you see.** An interval join emits results 4 minutes late even though both streams are
fresh.

**Mechanism.** `E-09`. The join's watermark is the minimum of its inputs. If one side is a
low-rate stream with `withIdleness` not configured, its watermark advances only when records
arrive, which for a 40-events-per-second stream with 6 partitions means some partitions are
always behind.

**Prevent.** `withIdleness` on every source (`E-03`), and monitor the join operator's input
watermark against each upstream's output watermark.

### J-11 — Enriching with data derived from the same stream

**What you see.** A job enriches events with "this member's event count in the last hour",
computed by another job from the same stream, and the result has a feedback loop: the enrichment
changes what is emitted, which changes what is counted.

**Mechanism.** A cycle in the dataflow, usually introduced accidentally through Kafka rather than
within one job graph (Flink forbids cycles in a job graph; it cannot see cycles through topics).

**Prevent.** Maintain a lineage graph of topics and jobs, and check it for cycles. Meridian's job
manifest declares inputs and outputs, and a platform check rejects a deploy that would create a
cycle. Without that check, cycles are found by their symptoms, which are slow oscillations that
look like a traffic pattern.

## 7. The decision table

Given "I need data from B while processing A", the answer:

| Situation | Use | State cost |
|---|---|---|
| B is small (< 100 MB) and changes rarely | Broadcast state | `size × parallelism` in checkpoints |
| B is large, and you need B as of A's event time | Temporal join over a CDC stream | Versions in the retention window |
| B is large, current value is fine, replay determinism not needed | Async lookup + cache + TTL | Zero (plus cache) |
| B is large, current value is fine, and the store cannot take the load | Materialise B into keyed state via CDC | Full table |
| A and B are both unbounded event streams matched on time | Interval join | Both streams × window |
| A and B are lifecycle events of one entity | Not a join — a keyed state machine | One record per in-flight entity |
| The join's output is a count, not an enumeration | Sketch (Bloom, HLL, t-digest) | Bits per element |

And the checklist for any join:

1. **Which of the four is it?**
2. **What is the state, by the formula?** Multiply it out before writing the code.
3. **Have you filtered and projected before the key?**
4. **What happens to unmatched records?** If the answer is "they disappear", is that what you
   want?
5. **What is the output grain**, and does the downstream aggregation know it?
6. **Does either side's watermark drag the other?**
7. **Is the result the same if you replay it tomorrow?** If not, you have a processing-time
   lookup where you needed a temporal join.

## What to take away

1. **Four different things are called "join"** — windowed stream-stream, temporal, lookup, and
   interval — and their state costs differ by three orders of magnitude. Establish which one is
   meant before anything else.
2. **Stream-stream join state is `rate × window × bytes` on both sides.** The window is a
   product decision worth putting in front of whoever owns it: halving Corridor's attribution
   window saves 2.1 TB.
3. **Filter, then project, then key, then join.** Projecting a 640-byte record to the 220 bytes
   the join needs is a 2.9× saving on state, shuffle, serialization and checkpoints at once, and
   it is one line.
4. **A processing-time lookup gives a different answer on replay than it gave live.** The test
   is: *must re-running this job tomorrow produce the same answer?* If yes, you need a temporal
   join over versioned history, not a lookup.
5. **Async I/O throughput is capacity ÷ latency**, so the remote latency cancels out of the
   machine count — 25,000 records/s per subtask at capacity 200 and 8 ms, against 125 for a
   synchronous call.
6. **Always override the async timeout action**, or one slow partition in your lookup store
   restarts your job. And a lookup makes the remote store's availability part of yours.
7. **Broadcast state has no ordering guarantee against the data side**, is duplicated per
   subtask in checkpoints, and has no built-in bootstrap. Version the rules if atomicity
   matters.
8. **An inner join makes non-events invisible.** Use an outer join or a timer so that "no
   payment arrived" becomes a record rather than an absence.
9. **State the output grain of every join.** "One row per (order_id, shipment_id)" prevents the
   4× revenue error that "orders joined to shipments" invites.
10. **If the two streams are lifecycle events of one entity, it is a state machine, not a
    join** — 68 MB against 200 GB.
11. **Ask what the join's output is used for.** If it is a count, a Bloom filter at 2.8 GB may
    replace 3.42 TB of exact state, at a stated 0.1% false-positive rate.
