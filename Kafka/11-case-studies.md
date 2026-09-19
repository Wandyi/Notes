# Case Studies: Six Incidents at Riverbend

Six incidents, each walked from first symptom through mechanism to recovery and prevention. They
are chosen to cover distinct classes, and three of them share a property worth noticing in
advance: **the Kafka cluster was healthy the entire time.** Every broker metric was green. In two
of them the cluster was not merely healthy but was doing precisely what it had been configured to
do, and the configuration was the incident.

Each case ends with the scenario IDs it maps to, so you can follow the mechanism back into the
reference docs.

| | Incident | Duration | Class | Cluster healthy? |
|---|---|---|---|---|
| CS-1 | Rebalance storm from a third-party slowdown | 6 h | Stalled | Yes |
| CS-2 | 211,200 order events acknowledged and lost | 51 min, found 3 weeks later | Lost | Partly |
| CS-3 | A hanging transaction froze settlement for nine hours | 9 h | Stalled | Yes |
| CS-4 | A read-only backfill took down checkout | 23 min | Rejected | No — it caused it |
| CS-5 | The log cleaner died in March and filled a disk in March | 11 days, then 4 h | Rejected | No |
| CS-6 | A partition expansion shipped 31 cancelled orders | 90 min, found next day | Reordered | Yes |

---

## CS-1 · The rebalance storm that a third party started and a default caused

**Duration:** 6 hours 10 minutes. **Impact:** fraud scoring stopped; 1.4 million order events
unscored; manual review queue for two days afterwards.

### What happened

At 09:12 on a Tuesday, Riverbend's third-party fraud scoring vendor deployed a change that moved
their API's p99 response time from 1.4 seconds to 4.2 seconds. They did not consider this an
incident — their SLA is 5 seconds — and they did not notify anyone.

At 09:18, `fraud-scorer-group` stopped making progress. Not slowed: stopped. Lag on
`orders.created` began climbing at the full arrival rate, as if the six consumer pods had been
deleted. They had not; all six were running, healthy, and logging.

The on-call engineer's first four hypotheses were all reasonable and all wrong: the pods were
not out of memory, the brokers were not under-replicated, the network policy had not changed,
and scaling to twelve pods made the situation measurably worse.

### The signal that identified it

Not lag — lag said "behind," which everyone already knew. The signal was rebalance rate, which
nobody was watching:

```promql
rate(kafka_consumer_coordinator_rebalance_total{consumergroup="fraud-scorer-group"}[5m]) * 3600
# Normal: 0 to 2 per hour
# During the incident: 94 per hour
```

And the consumer group's state, checked three times in a row:

```bash
kafka-consumer-groups.sh --bootstrap-server $BS --describe --group fraud-scorer-group --state
# PreparingRebalance ... CompletingRebalance ... PreparingRebalance
```

The group never reached `Stable`. It was spending its entire existence rebalancing.

### Mechanism

The arithmetic had been wrong since the group was deployed. The vendor's slowdown only made it
visible.

```
max.poll.records        = 500        (default, never changed)
max.poll.interval.ms    = 300,000    (default, never changed)

Before the vendor's change:
    500 records × 1.4 s = 700 s to process one poll batch — already over 300 s

After:
    500 records × 4.2 s = 2,100 s — seven times over
```

⚠️ Note that the configuration was **already broken before the incident**. At p99 1.4 s the group
needed 700 seconds per batch against a 300-second limit. It survived because p99 is not the mean:
most batches contained mostly fast records, so the *average* batch completed in around 90 seconds
and stayed inside the limit. The vendor's change moved the whole distribution, and the batches
that had been completing in 90 seconds now took 270, then 310, and the margin was gone.

Then the self-amplification from `C-01` took over:

1. One member exceeded the interval and was evicted. Its 4 partitions went to the other 5.
2. Those members now held 4.8 partitions each. Fuller batches, longer processing.
3. The next member was evicted. The remaining 4 held 6 partitions each.
4. The evicted members finished their batches, got `CommitFailedException`, and rejoined —
   triggering yet another rebalance.

Scaling from 6 pods to 12 at 11:40 made it worse for two reasons: twelve members produced more
rebalances, and twelve members each calling the vendor concurrently pushed the vendor's p99 from
4.2 s to 6.8 s.

### Recovery

At 15:22, `max.poll.records` was set to 50 and the deployment rolled:

```
50 records × 4.2 s = 210 s, against a 300 s limit — 30% margin
```

The group reached `Stable` within one rebalance and began draining at 15:28. The backlog of 1.4
million events took a further 4 hours to clear, because drain time is governed by surplus
capacity (doc 06) and the surplus was small.

### Prevention

1. **`max.poll.records` was set from the arithmetic**, not from the default, for every consumer
   that calls an external service. The rule adopted:
   `max.poll.records × worst-case-per-record < 0.5 × max.poll.interval.ms`.
2. **`fraud-scorer-group` was rewritten to decouple polling from processing** (doc 06, `L-06`),
   with 32 concurrent in-flight calls and an adaptive concurrency limit that halves when the
   vendor slows down. This also removed the amplification that scaling caused.
3. **Rebalance rate was added to alerting** (doc 10, alert 9). In the post-incident review this
   was the single most valuable change: it would have fired at 09:24, six hours before the
   problem was identified.
4. **The vendor's latency was added to Riverbend's own dashboards.** A dependency whose p99 can
   move by 3× without anyone being told is a dependency you monitor yourself.

**Maps to:** `C-01` (rebalance storm), `C-08` (`CommitFailedException`), `L-06` (concurrent
processing), `L-10` (scaling made it worse).

---

## CS-2 · Two latent misconfigurations, 211,200 lost order events

**Duration:** 51 minutes of degradation, discovered 19 days later. **Impact:** approximately
22,000 orders missing from `orders-db` with no record anywhere in Kafka.

### What happened

At 14:02, the cloud provider began an EBS impairment affecting a subset of volumes in
`us-east-1c`. Riverbend's brokers 5 and 6 are in that zone. Their volumes did not fail; they
became slow — write latency went from 2 ms to roughly 900 ms.

At 14:03, both brokers began falling behind as followers. Thirty seconds later
(`replica.lag.time.max.ms`) the leaders removed them from the ISR of the partitions they
followed.

At 14:18 the under-replication alert fired. It was configured as a **ticket**, correctly, because
under-replication by itself is a durability concern rather than an outage (doc 01, `B-05`). The
on-call engineer acknowledged it and began investigating at a reasonable pace.

At 14:47, broker 2 — in `us-east-1a`, entirely unaffected by the EBS event — was terminated by an
unrelated autoscaling group action during a routine instance refresh.

At 14:47:04, three partitions of `orders.created` went **completely offline**. Produce failures
began at `checkout-api` for approximately 12.5% of orders.

At 14:53, with checkout partially failing and no in-sync replica available for those partitions,
the on-call engineer performed an unclean leader election to restore service. It worked
immediately. Orders resumed.

At 14:53:12, 211,200 order-lifecycle events ceased to exist.

Nobody noticed until the month-end reconciliation on the 19th.

### Mechanism

Two independent latent misconfigurations, neither of which was visible in normal operation, plus
one entirely reasonable decision under pressure.

**Latent problem one: replica placement predated rack awareness.** `broker.rack` was configured
in year 1, six weeks after `orders.created` was created. Rack awareness is not retroactive (doc
01, `B-11`), so `orders.created`'s replica placement was assigned round-robin by broker id. Three
of its 24 partitions had **two replicas in `us-east-1c`** — brokers 5 and 6 — and one in
`us-east-1a` on broker 2.

Every topic created after the `broker.rack` change was placed correctly. The oldest and most
important topic was not, and that is the usual shape of this problem.

**Latent problem two: `min.insync.replicas` was never set.** The topic inherited the broker
default of 1. The producer used `acks=all`, which everyone had checked, and which is exactly
half of the agreement (doc 02, `R-01`).

Put together:

```
14:03  Brokers 5 and 6 drop out of ISR.
       For the 3 misplaced partitions, that leaves ISR = {broker 2} only.

       min.insync.replicas = 1, so broker 2 accepts writes on its own authority and
       acknowledges them. checkout-api receives success. Customers receive order
       confirmations.

       UnderMinIsrPartitionCount stays at ZERO throughout, because the ISR size (1)
       equals the configured minimum (1). The alert that would have paged someone
       could not fire.

14:47  Broker 2 is terminated. Those 3 partitions have no in-sync replica.
       They go offline — correctly, refusing to serve rather than lose data.

14:53  Unclean leader election promotes a replica on broker 5, whose log ends at
       14:03. Everything broker 2 accepted alone is discarded.
```

The loss:

```
affected partitions:         3 of 24                    = 12.5% of the topic
window:                      14:03 to 14:47             = 2,640 seconds
arrival rate (weekday):      640 events/s
events acknowledged and lost: 0.125 × 640 × 2,640       = 211,200 events
                              ÷ 9.6 events per order    ≈ 22,000 orders
```

⚠️ The unclean leader election was **the correct decision with the information available.**
Checkout was failing, three partitions had no path back without the terminated broker, and the
engineer had no way to know that 211,200 acknowledged records existed only on the machine that
had just gone away. The failure was not the decision at 14:53. It was the configuration that made
14:53 a choice between an outage and silent data loss, rather than between an outage and a wait.

### The signal that would have caught it

None of the alerts in place could have. `UnderMinIsrPartitionCount` was zero by construction.
What was needed was a configuration audit, not a runtime metric:

```bash
# Topics whose durability floor is 1 — run this on every cluster, today
for t in $(kafka-topics.sh --bootstrap-server $BS --list | grep -v '^__'); do
  m=$(kafka-configs.sh --bootstrap-server $BS --describe --entity-type topics \
        --entity-name "$t" --all 2>/dev/null \
      | grep -o 'min.insync.replicas=[0-9]*' | head -1 | cut -d= -f2)
  echo -e "$t\t${m:-unset}"
done | awk -F'\t' '$2<2 {print "EXPOSED: " $0}'
```

And a placement audit comparing each partition's replica set against broker racks, which found
the three misplaced partitions in under a minute once someone thought to look.

### Recovery and prevention

The 22,000 orders were reconstructed from `checkout-api`'s access logs over four days, which
worked only because those logs happened to retain 30 days of request bodies. That was luck.

1. **`min.insync.replicas=2` was set on every topic**, and added to the broker default for new
   ones. Applied during a window with full ISR, because applying it while degraded would have
   immediately blocked writes (doc 02, `R-01`).
2. **Replica placement was audited and corrected** for all pre-rack-awareness topics, via
   rack-aware reassignment.
3. **An unclean leader election now requires two people**, and the runbook states explicitly what
   it costs — the phrase "this discards acknowledged records and shortens the log" is in the
   procedure, because at 14:53 nobody said it out loud.
4. **A transactional outbox was added to `checkout-api`** (doc 05, `D-05`), so the authoritative
   record of an order is a database row and Kafka becomes a replayable derivative rather than the
   only copy. This is the change that makes the whole class of incident recoverable instead of
   terminal.
5. **A weekly configuration audit** now checks `min.insync.replicas`, replication factor, and
   rack spread for every topic, and fails a build if any topic is exposed.

**Maps to:** `R-01` (the trap), `R-07` (unclean election), `B-11` (placement not retroactive),
`B-05` (no self-healing), `C-10` (why the gap was silent downstream).

---

## CS-3 · Nine hours investigating a consumer that was working perfectly

**Duration:** 9 hours 20 minutes. **Impact:** settlement reconciliation delayed one business day;
no data loss.

### What happened

At 02:14, `settlement-group`'s lag on `payments.settled` began growing linearly. By 08:00 it was
at 3.1 million records and climbing at exactly the topic's arrival rate, which meant the group
was consuming **nothing at all**.

Everything else was normal. All four consumer pods were running. CPU at 4%. No rebalances. No
errors in the consumer logs — in fact, almost no log lines at all, because the consumer logs per
batch and it was receiving no batches. Brokers healthy, no under-replicated partitions,
`payments.settled` producers writing normally at 180 records/s.

The investigation followed a reasonable path and spent nine hours on it: restart the consumers
(no change), scale from 4 pods to 8 (no change), check network policy (fine), check ACLs (fine),
check for a poison message (none — the consumer was not receiving anything to poison it), roll
back the last consumer deploy from three days earlier (no change).

### The signal that identified it

At 11:30, someone ran a throwaway console consumer to see what the consumer group was seeing:

```bash
kafka-console-consumer.sh --bootstrap-server $BS --topic payments.settled \
  --partition 4 --offset 4201338 --max-messages 5
# Returns five recent records immediately.
```

It worked. The production consumer did not. The difference between them was one setting:

```bash
kafka-console-consumer.sh --bootstrap-server $BS --topic payments.settled \
  --partition 4 --offset 4201338 --max-messages 5 --isolation-level read_committed
# Hangs. Returns nothing.
```

That comparison — `read_uncommitted` sees data, `read_committed` does not — identifies the
problem in thirty seconds and is now step two of Riverbend's lag runbook.

```bash
kafka-transactions.sh --bootstrap-server $BS find-hanging --broker-id 3
# Topic            Partition  ProducerId  ProducerEpoch  StartOffset  ...  Duration(min)
# payments.settled 4          8823        17             4201338           561
```

A transaction had been open for 561 minutes — nine hours and twenty-one minutes, matching the
start of the lag precisely.

### Mechanism

A `read_committed` consumer reads only up to the **last stable offset**, which is the offset of
the first still-open transaction (doc 05, `D-04`). A transaction opened at offset 4,201,338 at
02:14 and was never committed or aborted.

The high watermark climbed past 4.2 million, 4.3 million, 4.5 million as producers continued
normally. The LSO stayed at 4,201,338. `settlement-group` was pinned there and could not advance,
by design, because those records might still be aborted.

Every ordinary diagnostic was green because every ordinary diagnostic was measuring something
that was genuinely fine. The consumer was healthy. The brokers were healthy. The producers were
healthy. The only unhealthy thing was a single piece of transaction state on one partition, and
nothing on any dashboard represented it.

The transaction's producer — a `payments-svc` pod — had been terminated at 02:13 during a node
drain. `transaction.timeout.ms` was 60 seconds and should have caused the coordinator to abort it
by 02:15. Riverbend was running Kafka 3.4 at the time, before KIP-890's fixes to the transaction
protocol's edge cases around producer termination and coordinator state.

### Recovery

```bash
kafka-transactions.sh --bootstrap-server $BS abort \
  --topic payments.settled --partition 4 --start-offset 4201338
```

The LSO advanced immediately. `settlement-group` drained 3.1 million records in 22 minutes.

### Prevention

1. **A new alert: high watermark minus LSO, per partition, on every topic with transactional
   producers.** This is the only metric that represents the failure, and it must be built from
   two separate offset queries because Kafka does not expose the gap directly.
2. **The runbook gained a step:** for any single-group lag with healthy brokers, compare
   `read_committed` and `read_uncommitted` before investigating the consumer at all. Thirty
   seconds, and it eliminates or confirms the most confusing failure in the catalogue.
3. **The cluster was upgraded to Kafka 3.7** for KIP-890.
4. **`transaction.max.timeout.ms` was lowered** from the 15-minute default to 5 minutes, bounding
   how long a legitimately abandoned transaction can stall consumers even when the protocol works
   correctly.

**Maps to:** `D-04` (hanging transaction), `D-03` (what transactions cover), `L-02` (lag that
needs interpretation).

---

## CS-4 · A read-only backfill took checkout down for 23 minutes

**Duration:** 23 minutes of checkout unavailability. **Impact:** an estimated 89,000 order
attempts failed; revenue impact was the largest of the six incidents here.

### What happened

At 19:40 on a Thursday — during the evening peak — a data engineer started a backfill job to
rebuild a clickstream aggregate. It read `clickstream.events` from the beginning: 3,525 GB,
24 hours of retention, using a brand-new consumer group so that no existing group was affected.

By every reasonable standard this was a safe operation. It was read-only. It used its own
consumer group. It touched no production code path, no production database, and no production
service.

At 19:46, produce latency across the **entire cluster** began rising. Not for
`clickstream.events` — for every topic.

At 19:51, `checkout-api` began returning HTTP 503. By 19:54 it was returning 503 for essentially
all order submissions. Checkout was down.

### The signal that identified it

The broker metric that is normally exactly zero:

```promql
sum(rate(node_disk_read_bytes_total{instance=~"kafka-.*"}[5m]))
# Baseline: under 2 MB/s across the whole cluster
# At 19:46:   412 MB/s
```

And the produce-latency breakdown from doc 01, which said precisely which stage:

```promql
kafka_network_requestmetrics_localtimems{request="Produce",quantile="0.99"}
# Baseline: 3 ms     During: 1,840 ms
kafka_network_requestmetrics_remotetimems{request="Produce",quantile="0.99"}
# Baseline: 4 ms     During: 61 ms  — replication was fine
```

`LocalTimeMs` at 1.8 seconds with `RemoteTimeMs` almost unchanged means the leader could not
append to its own log. Local storage, not replication, not network.

### Mechanism

Three stages, and the third is the one that turned a Kafka slowdown into a checkout outage.

**Stage 1 — the page cache was destroyed.** Riverbend's residency window, derived in doc 00, is
about 11 minutes at peak. The backfill read data 24 hours old, none of which was in cache, so
every fetch became a physical disk read. Those reads pulled cold pages *into* the 24 GiB cache,
evicting the recent pages that producers and every healthy consumer depended on:

```
backfill read rate:              412 MB/s across 6 brokers = 69 MB/s per broker
page cache per broker:           25,770 MB
complete cache turnover every:   25,770 ÷ 69 = 373 s ≈ 6 minutes
```

The entire cache was being replaced every six minutes with data nobody else wanted.

**Stage 2 — appends became disk-bound.** With the cache thrashing and the gp3 volumes saturated
by 69 MB/s of random reads on top of 40 MB/s of sequential writes, produce appends went from 3 ms
to 1.84 s.

**Stage 3 — `checkout-api`'s producer buffer filled, and `send()` started blocking.** This is
where doc 03 (`P-02`) becomes an outage:

```
With max.in.flight = 5 and produce latency 1.84 s:
    5 ÷ 1.84 s = 2.7 requests/s per broker connection
    × 6 broker connections × ~100 records per batch = 1,630 records/s of drain

Arrival at evening peak:                              3,400 records/s
Net fill rate:              3,400 − 1,630 = 1,770 records/s × 1.8 KB = 3.19 MB/s
buffer.memory:                                        32 MB
Time to exhaustion:         32 ÷ 3.19                = 10 seconds
```

Ten seconds after produce latency degraded, the buffer was full. `send()` then blocked each
calling thread for up to `max.block.ms` (60 seconds). `checkout-api` runs a bounded request-thread
pool; within another twenty seconds every thread was parked inside `send()`, the pool was
exhausted, and the service returned 503 to everything — including requests that had nothing to do
with Kafka.

**A read-only batch job, in a separate consumer group, touching no production service, took down
checkout.**

### Recovery

At 20:09 the backfill was killed. Disk reads fell to baseline within 90 seconds, the page cache
refilled with recent data, produce latency returned to single-digit milliseconds, and
`checkout-api`'s buffer drained. Checkout recovered at 20:14 without a restart.

### Prevention

1. **Default client quotas for every client id**, so a new consumer is throttled unless it has
   negotiated otherwise (doc 08, `S-11`). This is the change that prevents recurrence:
   ```bash
   kafka-configs.sh --bootstrap-server $BS --alter \
     --add-config 'consumer_byte_rate=52428800,request_percentage=200' \
     --entity-type clients --entity-default
   ```
2. **An alert on broker disk read throughput** (doc 10, alert 10). Near-zero when healthy, so it
   needs no threshold tuning, and it would have fired at 19:47 — four minutes before checkout
   began failing.
3. **`checkout-api` no longer blocks on Kafka.** `max.block.ms` was set to 250 ms, and a full
   buffer now writes the order to the outbox table added after CS-2 and returns success. Kafka
   being slow degrades the pipeline's freshness; it no longer degrades checkout.
4. **Replay-heavy workloads moved to a separate consumer role** with a documented quota and a
   requirement to run outside peak hours. The cultural half of the fix: "read-only" is not the
   same as "harmless," and that had to be said explicitly.

**Maps to:** `L-09` (the page-cache cliff), `P-02` (buffer exhaustion), `B-06` (latency
breakdown), `S-11` (quotas).

---

## CS-5 · The log cleaner that died quietly and filled a disk eleven days later

**Duration:** 11 days of undetected growth, then a 4-hour partial outage. **Impact:** one-sixth
of partitions offline for 100 minutes.

### What happened

On 3 March at 04:41, broker 4's log cleaner thread encountered an error while compacting
`inventory.adjustments-31` and terminated. One line in `server.log`. No alert, no metric, no
symptom.

For eleven days, broker 4 stopped compacting any of its compacted partitions. Nothing else
changed. Producers succeeded. Consumers consumed. Latency was normal.

On 14 March at 21:50, broker 4's disk reached 100%. Kafka marked its log directory offline,
taking every partition on that broker out of service. Partitions whose other replicas were
healthy were re-led elsewhere; the cluster degraded rather than failed, which was the replication
working correctly.

The immediate impact was contained. Finding the cause took two days.

### The signal that identified it — eventually

Nobody had cleaner metrics, so there was nothing to look at. The investigation eventually
compared per-partition sizes across brokers, which made it obvious:

```bash
kafka-log-dirs.sh --bootstrap-server $BS --describe --broker-list 1,2,3,4,5,6 \
  | tail -1 | jq -r '.brokers[] | .broker as $b | .logDirs[].partitions[]
      | select(.partition | startswith("inventory.adjustments"))
      | [$b, .partition, .size] | @tsv' | sort -k3 -rn | head
```

Broker 4's replicas of `inventory.adjustments` were **17 times larger** than the same partitions'
replicas on other brokers. Same data, same retention, same configuration — one broker compacting
and five not, or rather five compacting and one not.

The root cause was then found by searching broker 4's logs for the point at which its partitions
started diverging, which led to the single line from 3 March.

### Mechanism

The log cleaner runs on dedicated threads (`log.cleaner.threads`, default 1). An unrecoverable
error kills the thread and **it is not restarted** (doc 07, `T-09`). Compaction stops for every
compacted partition hosted on that broker, and nothing on the produce or consume path is affected,
so nothing reports it.

Broker 4's growth rate:

```
inventory.adjustments:  1,200/s × 400 B = 480 KB/s cluster-wide
                        × RF 3 ÷ 6 brokers = 240 KB/s on broker 4
catalog.changes:        40/s × 12 KB = 480 KB/s cluster-wide
                        × RF 3 ÷ 6 brokers = 240 KB/s on broker 4
__consumer_offsets:     ~24 records/s × 200 B × RF 3 ÷ 6 ≈ 2.4 KB/s

total uncompacted growth on broker 4:  ~482 KB/s = 41.6 GB/day
free space on broker 4 at the time:    ~450 GB
time to exhaustion:                    450 ÷ 41.6 = 10.8 days
```

Eleven days, which matches. ⚠️ Note `__consumer_offsets` in that list. At Riverbend's year-1
scale it contributed 0.2 GB/day and was irrelevant. At year-3 scale (doc 08, `S-05`) the same
failure would add **6.7 GB/day per broker** from that topic alone, and would hit every broker
rather than one — because a cleaner failure caused by a systemic condition, rather than one
corrupt record, kills the thread everywhere at once.

### Recovery

Restarting broker 4 restarted its cleaner threads, which began working through the backlog. It
took 4 hours to reclaim the space, during which the broker was under heavy I/O load and was kept
out of the leader rotation.

The corrupt record in `inventory.adjustments-31` was identified with `kafka-dump-log.sh` and the
partition was rebuilt from its healthy replicas by deleting the local copy and letting it
re-replicate.

### Prevention

1. **Two metrics added**, which is the whole fix and which almost nobody has until this happens:
   ```promql
   kafka_log_logcleanermanager_time_since_last_run_ms > 600000
   kafka_log_logcleanermanager_uncleanable_partitions_count > 0
   ```
2. **A cross-broker size-skew check**, run daily: any partition whose size differs by more than
   3× across its replicas is flagged. This catches cleaner failures, and it also catches several
   other slow divergences.
3. **Disk alerting moved from a static threshold to `predict_linear`**, which would have fired on
   5 March — nine days before the outage and with ample time to act.

**Maps to:** `T-09` (the cleaner dying), `B-02` (disk full), `C-12` (`__consumer_offsets` as a
failure source), `S-05` (why this gets worse with scale).

---

## CS-6 · The partition expansion that shipped 31 cancelled orders

**Duration:** 90 minutes of exposure; discovered the following day. **Impact:** 31 cancelled
orders were picked, packed, and shipped; 380 orders had lifecycle events applied out of order.

### What happened

`order-processor-group` had been lagging intermittently for two weeks. A capacity review
concluded that 24 partitions limited the group to 24 consumers and that doubling to 48 would
double the available parallelism.

On a Tuesday at 11:14, someone ran:

```bash
kafka-topics.sh --bootstrap-server $BS --alter --topic orders.created --partitions 48
```

The command returned immediately with no output. Lag improved. The change was considered a
success and closed.

At 09:20 the next morning, customer support escalated three complaints about orders that had been
cancelled and shipped anyway.

### Mechanism

Two things went wrong, and the second one is the incident.

**The diagnosis was wrong.** The lag was not a partition-count problem. Per-partition lag showed
one partition consistently at 40× the others — key skew from a small number of very high-volume
merchant accounts (doc 03, `P-10`). Adding partitions does not help skew, because
`murmur2(key) % 48` puts a hot key on exactly one partition just as `% 24` did. The lag improved
after the change for an unrelated reason: the rebalance redistributed the *other* partitions more
evenly across members.

**The partition change broke per-key ordering.** The default partitioner maps a key by
`murmur2(key) % numPartitions`. Changing the modulus re-maps roughly half of all keys:

```
murmur2("ord_8f2a91") = 1,847,203,551
    before:  1,847,203,551 % 24 = 15   → partition 15
    after:   1,847,203,551 % 48 = 39   → partition 39
```

A key keeps its partition only when `hash % 48 < 24`, which is true for about half of all keys.
For the other half, **events written before 11:14 are on one partition and events written after
are on another.**

Riverbend's order lifecycle spans roughly 20 minutes from creation to final state, and
`orders.created` carries every lifecycle event. So for 90 minutes after the change, orders in
flight had their `created` event on the old partition and their `cancelled` or `adjusted` event
on the new one — read by two different consumer instances, concurrently, with no ordering
relationship whatsoever.

```
orders in flight at 11:14 or created shortly after, with a later lifecycle event:
    67 orders/s × 1,200 s (a 20-minute lifecycle window)         ≈ 80,400 orders
× fraction whose key re-mapped                                    ≈ 50%
                                                                  = 40,200 orders at risk
× fraction with a subsequent lifecycle event in the window         ≈ 3%
                                                                  ≈ 1,206 orders with split events
of which, events applied in the wrong order                        =   380 orders
of which, a cancellation applied before its creation               =    47 orders
of those 47, shipped before the error was found                    =    31 orders
```

The 47 orders each received a `cancelled` event on the new partition — processed first, against
an order that did not exist yet, so `order-processor` treated it as a late event for an unknown
order and logged a warning. Then the `created` event arrived on the old partition and created the
order in an **active** state. The cancellation had already been discarded. Thirty-one of them
reached the warehouse before anyone connected the warnings to the partition change.

⚠️ `kafka-topics.sh --alter --partitions` printed nothing, warned about nothing, and completed in
under a second. Nothing in Kafka's interface indicates that this is a data migration (doc 05,
`D-07`).

### Recovery

The 31 shipped orders were handled commercially — recalled where possible, refunded otherwise.
The 380 out-of-order orders were reconciled against `checkout-api`'s logs and corrected in
`orders-db` over two days.

No Kafka-side fix was possible or needed: the records were all present and correct in the log.
The log was fine. The processing of it was not.

### Prevention

1. **Partition changes on keyed topics are now a reviewed migration**, with a written procedure:
   create a new topic at the target count, migrate consumers, migrate producers, retire the old
   one (doc 05, `D-07`). The one-line `--alter` is blocked by ACL on production topics.
2. **`order-processor` now applies events conditionally on a version** carried in each record
   (doc 05, `D-06`, option 3). A late-arriving older event is rejected by the version check
   rather than overwriting a newer state. This is the change that makes the failure class
   impossible rather than merely unlikely — it converts an ordering requirement into an
   idempotence requirement.
3. **Per-partition lag is now the primary capacity signal**, not the group sum (doc 06, `L-01`),
   so key skew is distinguishable from insufficient parallelism before anyone acts on it.
4. **"Unknown order" warnings became errors with an alert.** The system had been telling someone
   for 90 minutes; nobody was listening, because it was a warning among many.

**Maps to:** `D-07` (partition remap), `P-10` (the skew that caused the wrong diagnosis), `L-01`
(sum hiding the distribution), `D-06` (version-conditional writes), `C-11` (the parallelism
ceiling that motivated it).

---

## Patterns across the six

Reading them together is more useful than any one of them.

**Three of six had a completely healthy cluster.** CS-1, CS-3, and CS-6 involved no broker
problem of any kind. Broker dashboards would have shown nothing in all three, and in CS-3 nine
hours were spent looking at a consumer that was working correctly. This is the argument for
instrumenting the pipeline rather than the cluster (doc 10).

**Four of six were caused by a default.** `max.poll.records=500`, `min.insync.replicas=1`,
`num.partitions` reasoning, and the absence of client quotas. None of these were decisions. They
were values nobody had reason to look at, which is what makes a default dangerous — it is
invisible to review precisely because nobody typed it.

**Two of six were latent for months before the trigger.** CS-2's misconfiguration was created in
year 1 and fired in year 2. CS-1's arithmetic was wrong from deployment and only broke when a
third party changed. The trigger gets the attention in the postmortem; the latent condition is
what you can actually find in advance, with an audit.

**The most valuable single change across all six** was not a fix for any one of them. It was the
transactional outbox added after CS-2, which made CS-4's blocking behaviour survivable and would
have made CS-2 itself a recovery exercise rather than a permanent loss. Making the source of
truth something other than Kafka is what turns several of these from data-loss incidents into
delay incidents.

**The cheapest single change** was the `read_committed` versus `read_uncommitted` comparison from
CS-3. Thirty seconds, one command, and it eliminates the most confusing failure in the
collection.

---

## What to take away

1. **A healthy cluster is not a healthy pipeline,** and half of these incidents had one without
   the other.
2. **Compute the `max.poll.records` arithmetic for every consumer that calls anything external.**
   CS-1's configuration was broken from the day it shipped and survived only on a favourable
   latency distribution.
3. **`min.insync.replicas=1` produces no alert when it fails,** because the ISR size equals the
   configured minimum. Only a configuration audit finds it.
4. **An unclean leader election under pressure is a reasonable decision made with incomplete
   information.** Fix the configuration that makes it necessary, not the engineer who made it.
5. **A hanging transaction is invisible to every ordinary metric.** Compare `read_committed`
   against `read_uncommitted` before investigating a lagging consumer.
6. **"Read-only" does not mean "harmless."** A backfill with its own consumer group took down
   checkout by evicting the page cache.
7. **Silent slow failures need a metric or they run for weeks.** The log cleaner died on 3 March
   and was found on 16 March, only because someone compared partition sizes across brokers.
8. **`kafka-topics.sh --alter --partitions` is a data migration that looks like a
   configuration change,** and Kafka will not warn you.
9. **Version-conditional writes are the general escape from ordering requirements.** They convert
   a guarantee that is hard to maintain in a distributed system into one that is straightforward.
10. **Audit for latent conditions rather than waiting for triggers.** Two of these six were
    findable months in advance by a script that takes a minute to run.

Next: [12-staff-interview-questions.md](12-staff-interview-questions.md).
