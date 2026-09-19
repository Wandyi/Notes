# Replication, the ISR, and What Durability You Actually Bought

This is the most consequential doc in the collection. Nearly every real Kafka data-loss incident
reduces to a team believing they had configured durability when they had configured something
that *looks* identical in steady state and diverges only during the exact failure they bought
replication to survive.

So rather than listing settings, this doc derives the contract. By the end you should be able to
answer, for any topic you own, two questions without looking anything up: **how many broker
failures can this topic survive without losing an acknowledged write, and how many can it survive
while still accepting writes?** Those are different numbers, they are set by different
configuration, and trading one against the other is the entire design space.

## The naive version, and where it breaks

Here is what almost every team does, and it is not unreasonable:

> "We set `replication.factor=3`. Every record exists on three brokers. We can lose two brokers
> and still have the data."

Every sentence is true and the conclusion is false. Replication factor describes how many
replicas the partition *has*. It says nothing about how many replicas a given record was written
to before the producer was told the write succeeded. Those are different quantities, and the gap
between them is where data is lost.

Work through what happens on `orders.created` — RF=3, replicas on brokers 1, 2, 3 — when broker 3
becomes slow because its EBS volume is degraded:

1. Broker 3 stops keeping up. After `replica.lag.time.max.ms` (30 s) the leader removes it from
  the in-sync replica set. **ISR is now {1, 2}. The replication factor is still 3.**
2. Broker 2 then hits the same degraded storage — same availability zone, same underlying
  failure. It too drops out. **ISR is now {1}. The replication factor is still 3.**
3. A producer writes order `ord_8f2a91`. The leader, broker 1, appends it and acknowledges.
4. Broker 1 fails.

Order `ord_8f2a91` was acknowledged to `checkout-api`, which returned HTTP 201 to a customer, and
it exists nowhere. Brokers 2 and 3 never received it. When one of them becomes leader, the
partition's log simply ends before that record. Nothing is corrupt; nothing is inconsistent;
there is no error anywhere. The record was never replicated and the customer was told it was
safe.

Whether step 3 is allowed to happen is decided by `min.insync.replicas`, and if you have
never set it, it is **1**.

## Deriving the contract

Two settings control the write path, and they are in different places, which is the root of the
confusion:

- `acks` is a *producer* setting. It says how many acknowledgements the producer waits for.
- `min.insync.replicas` is a *topic* setting. It says how small the ISR may get before the
broker refuses `acks=all` writes.

Here is what the broker does with them, exactly:

> When a produce request arrives with `acks=all`, the leader first checks whether the current
> ISR size is at least `min.insync.replicas`. If not, it rejects the request with
> `NotEnoughReplicasException` *without writing anything*. If the check passes, it appends the
> record and waits for **every replica currently in the ISR** to fetch it before acknowledging.
>
> When a produce request arrives with `acks=1`, the leader appends and acknowledges
> immediately. `min.insync.replicas` **is not consulted at all.**
>
> When a produce request arrives with `acks=0`, the producer does not wait for a response and
> the leader's success or failure is never communicated.

Two conclusions follow, and they are the load-bearing statements of this doc.

**First: an acknowledged** `acks=all` **write exists on at least** `min.insync.replicas` **replicas.** Not
on `replication.factor` replicas — on `min.insync.replicas` of them. That number, and not the
replication factor, is your durability floor.

**Second:** `min.insync.replicas` **does nothing unless the producer uses** `acks=all`**.** A topic with
`min.insync.replicas=2` and a producer with `acks=1` has the durability of `acks=1`, which is the
durability of one broker. The topic setting is not a guard rail; it is one half of an agreement
that the producer must also honour.

From the first conclusion, name the replication factor `N` and the minimum in-sync count `M`:


| Question                                                                     | Answer    | Why                                                                 |
| ---------------------------------------------------------------------------- | --------- | ------------------------------------------------------------------- |
| How many replica losses can an **acknowledged record** survive?              | **M − 1** | It exists on at least M replicas; losing M of them loses all copies |
| How many replica losses can the partition **keep accepting writes** through? | **N − M** | Writes stop when the ISR drops below M                              |


Applied to the three configurations anyone actually chooses, with `N = 3`:


| Configuration | Acknowledged data survives | Writes continue through | Character                                                             |
| ------------- | -------------------------- | ----------------------- | --------------------------------------------------------------------- |
| `M = 1`       | **0 failures**             | 2 failures              | Availability at any cost. Replication is decorative during a failure. |
| `M = 2`       | 1 failure                  | 1 failure               | Balanced. The right default for almost everything.                    |
| `M = 3`       | 2 failures                 | **0 failures**          | Durability at any cost. Any single broker restart blocks writes.      |


⚠️ Read the `M = 1` row again. It tolerates *zero* failures for acknowledged data while
tolerating *two* for availability. That is not a bad trade if you genuinely prefer availability —
it is the right choice for `clickstream.events` — but it is a catastrophic default for anything
you would reconcile against a bank statement. And it is the default.

⚠️ And read the `M = 3` row, because it is the trap that catches careful people. A team that
reasons "we want maximum durability, so set `min.insync.replicas` equal to the replication
factor" has built a topic where **one broker restart stops all writes**. Rolling restarts become
outages. `M = N` is almost never correct; `M = N − 1` gets you one fewer tolerated failure on
durability and buys back the ability to operate the cluster.

### The single sentence to remember

**Set** `min.insync.replicas = replication.factor − 1`**, and require** `acks=all`**.** With RF=3 that is
`M=2`: one failure is survivable in both directions, which is the shape of almost every real
failure. Deviate from it only with a written reason.

Riverbend's topics, checked against the derivation:


| Topic                   | N   | M   | Acked data survives | Writes continue through | Correct?                                                         |
| ----------------------- | --- | --- | ------------------- | ----------------------- | ---------------------------------------------------------------- |
| `orders.created`        | 3   | 2   | 1 failure           | 1 failure               | Yes                                                              |
| `payments.settled`      | 3   | 2   | 1 failure           | 1 failure               | Yes — and transactions add more, see doc 05                      |
| `inventory.adjustments` | 3   | 2   | 1 failure           | 1 failure               | Yes                                                              |
| `catalog.changes`       | 3   | 2   | 1 failure           | 1 failure               | Yes                                                              |
| `clickstream.events`    | 2   | 1   | **0 failures**      | 1 failure               | Deliberately, and it should be written down                      |
| `orders.created.dlq`    | 3   | 2   | 1 failure           | 1 failure               | Yes — the DLQ needs the same durability as the topic it protects |


The `clickstream.events` row is the interesting one. RF=2 with M=1 means an acknowledged
clickstream event can be lost to a single broker failure. For behavioural analytics sampled at
34,000 events per second, losing a few seconds of data during a broker failure is genuinely
acceptable, and the alternative — RF=3 — would add 3.5 TB of disk and 40 MB/s of replication
traffic for no business benefit. That is a good decision. It is only a good decision if it was
made on purpose, and the test of whether it was made on purpose is whether anyone can state the
reason without being prompted.

---



## Failure catalogue


| Class                                          | The question it answers               | Scenarios       |
| ---------------------------------------------- | ------------------------------------- | --------------- |
| **A. The contract was never what you thought** | Did we actually configure durability? | `R-01` … `R-04` |
| **B. The contract working as designed**        | Writes are blocked. Is that correct?  | `R-05` … `R-06` |
| **C. The contract deliberately broken**        | Someone traded data for uptime        | `R-07`          |
| **D. Replication itself failing**              | Replicas exist but are not keeping up | `R-08` … `R-10` |


---



## Class A — the contract was never what you thought



### R-01 · `acks=all` with `min.insync.replicas=1`

**What you see.** Nothing, for years. Then a reconciliation gap after an incident: orders that
`checkout-api` logged as created, with no corresponding record in Kafka or in `orders-db`.

**Mechanism.** The sequence in "the naive version" above. The ISR shrank to one replica, the
leader acknowledged writes on its own authority, and the leader then failed.

What makes this the most dangerous configuration in Kafka is that **it is indistinguishable from a
correct configuration during normal operation.** With a healthy ISR of three, `acks=all` waits for
all three regardless of `min.insync.replicas`. Your latency is the same. Your throughput is the
same. Every test passes. The configuration diverges from a correct one only when the ISR has
shrunk — which is to say, only during the event you configured replication to survive.

This is also why it survives review. Someone looks at the producer config, sees `acks=all`, and
ticks the box. The topic config is somewhere else, was created by a script two years ago, and
inherits the broker default.

**Confirm it.** Check both halves, and check them per topic rather than trusting a cluster
default:

```bash
# Topic side: what is actually set, including inherited values
kafka-configs.sh --bootstrap-server $BS --describe --entity-type topics \
  --entity-name orders.created --all | grep min.insync.replicas
```

A line reading `min.insync.replicas=1 sensitive=false synonyms={DEFAULT_CONFIG:min.insync.replicas=1}`
means nobody ever set it and you are exposed. Audit every topic at once:

```bash
for t in $(kafka-topics.sh --bootstrap-server $BS --list | grep -v '^__'); do
  m=$(kafka-configs.sh --bootstrap-server $BS --describe --entity-type topics \
        --entity-name "$t" --all 2>/dev/null \
      | grep -o 'min.insync.replicas=[0-9]*' | head -1 | cut -d= -f2)
  echo -e "$t\t${m:-unset}"
done | awk -F'\t' '$2<2 {print "EXPOSED: " $0}'
```

Then check the producer side, which you cannot read from the broker — it has to come from your
application configuration or from a client-side audit.

**Recover.** Set it. The change takes effect immediately on the next produce request; no restart
is required and no data moves:

```bash
kafka-configs.sh --bootstrap-server $BS --alter --entity-type topics \
  --entity-name orders.created --add-config min.insync.replicas=2
```

⚠️ Before you do this on a cluster that currently has under-replicated partitions, understand what
you are turning on: any partition whose ISR is already below two will **immediately start
rejecting writes**. That is the correct behaviour and it is also a surprise. Check `B-05` first,
restore replication, then tighten the setting.

**Prevent.** Two mechanisms, because one is not enough:

1. Set `min.insync.replicas=2` in the broker configuration so that *newly created* topics inherit
  it. This does nothing for existing topics.
2. Disable topic auto-creation (`auto.create.topics.enable=false`) and create topics through a
  reviewed, declarative path where replication factor and minimum ISR are explicit and required
   fields. See `R-10` for what auto-creation does otherwise.



### R-02 · `acks=1` — acknowledged by the leader alone

**What you see.** A small, bounded amount of data missing after every leader election, whether or
not that election was caused by a failure. Rolling restarts lose records.

**Mechanism.** With `acks=1`, the leader responds as soon as it has appended to its own log.
Followers fetch afterwards. The window between the acknowledgement and the followers catching up
is small — typically single-digit milliseconds — but it is a window in which the record exists on
exactly one broker. Any leader change in that window loses it.

How much data is at risk is straightforward to estimate, and worth doing because it turns an
abstract risk into a number someone can make a decision about. `orders.created` at peak:

```
3,400 records/s × 5 ms replication lag = 17 records at risk at any instant
```

Seventeen orders per leader election. Riverbend does a rolling restart of six brokers roughly
monthly, with 24 partitions whose leadership moves — so `acks=1` on `orders.created` would cost
on the order of a few hundred orders a month, silently, as a consequence of routine maintenance.
For clickstream events at 85,000/s the same arithmetic gives about 425 records per election, and
nobody cares, which is why `clickstream.events` uses `acks=1` on purpose.

⚠️ `min.insync.replicas` **is not consulted for** `acks=1` **writes.** A topic set to
`min.insync.replicas=3` gives an `acks=1` producer exactly the same durability as
`min.insync.replicas=1`. The topic setting cannot protect you from the producer's choice. This is
the most common misconception in this doc's subject area.

**Confirm it.** You cannot see `acks` from the broker. It has to come from client configuration.
Two useful approaches: audit application configuration in source control, and — more reliably —
check whether `RemoteTimeMs` on the produce path is near zero for a topic's leaders, since
`acks=all` writes must wait for followers and therefore *always* show non-trivial remote time.

**Recover and prevent.** On Kafka **3.0 and later,** `enable.idempotence` **defaults to** `true`**, and
idempotence requires** `acks=all` — so a producer with default configuration is already safe. The
exposure is producers that explicitly set `acks=1`, usually copied from a tutorial written before
2021, or set deliberately during a latency investigation and never reverted. Grep for it.

### R-03 · `acks=0` — the producer that cannot fail

**What you see.** Produce error rate is exactly zero, permanently, which should itself be
suspicious. Records are missing during broker restarts, network blips, and load spikes, in
quantities nobody can bound.

**Mechanism.** `acks=0` means the producer writes to its socket and considers the send complete.
It does not wait for a response, so it cannot learn that the broker rejected the record, that the
broker was not the leader, that the record was too large, or that the connection dropped
mid-write. Retries are impossible because there is nothing to retry on. The producer's own error
metrics are meaningless.

**When it is defensible:** essentially never, and the reason is not durability but *diagnosis*.
Even for genuinely disposable data, `acks=1` costs almost nothing extra — the leader responds
without waiting for followers — and it gives you an error signal. `acks=0` buys you one network
round trip of latency, in exchange for being unable to tell whether your pipeline is working. If
someone proposes it for throughput, the correct response is to raise `linger.ms` and `batch.size`
instead, which improves throughput far more (doc 03, `P-09`) and keeps the error signal.

**Confirm it.** Compare producer-side `record-send-total` against broker-side
`MessagesInPerSec` for the topic. With `acks=0` the two diverge silently during any disruption,
and that divergence is the only evidence you will get.

### R-04 · No `fsync`: correlated power loss can beat replication

**What you see.** After a whole-rack or whole-zone power event, a small number of acknowledged
records are missing from every replica.

**Mechanism.** This is the durability boundary that replication alone does not cover, and it is
worth understanding because it is the one point where the answer is "accept the risk" rather than
"fix the configuration."

Kafka does not `fsync` each record. It appends to the operating system's page cache and lets the
kernel flush asynchronously; `log.flush.interval.messages` and `log.flush.interval.ms` are
effectively unbounded by default. So an acknowledged `acks=all` record is in the page cache of
`min.insync.replicas` machines — in volatile memory — not necessarily on any durable medium.

Replication makes this safe *as long as failures are independent*. Two machines do not lose power
in the same instant by coincidence. They do lose power in the same instant if they share a rack,
a power distribution unit, or an availability zone. That is a **correlated** failure, and it can
defeat any number of replicas that share the correlated component.

The available responses, in the order you should consider them:

1. **Spread replicas across failure domains** so that no single power event covers
  `min.insync.replicas` replicas. This is why `broker.rack` matters (doc 01, `B-11`), and it is
   the correct answer for essentially everyone.
2. **Force fsync per write** by setting `log.flush.interval.messages=1`. This works and it is
  very expensive — you have replaced sequential buffered appends with a synchronous device
   round trip per batch, and throughput typically falls by an order of magnitude. Choose this
   only if you have a regulatory requirement that names it.
3. **Accept it**, having written down the exposure. For Riverbend, a simultaneous power loss
  across two availability zones is a scenario in which the recovery concern is the whole
   platform and not a handful of Kafka records.

**Prevent.** Rack-aware placement, verified on a schedule rather than assumed. Treat the fsync
question as a documented risk acceptance with a named owner, which is a much better outcome than
either ignoring it or paying for option 2 reflexively.

---



## Class B — the contract working as designed



### R-05 · Producers blocked with `NotEnoughReplicasException`

**What you see.** `checkout-api` produce calls failing. Broker logs show
`NotEnoughReplicasException` or `NotEnoughReplicasAfterAppendException`. `UnderMinIsrPartitionCount`
is above zero. No broker is obviously broken.

**Mechanism.** This is `min.insync.replicas` doing exactly its job. The ISR fell below the
minimum, so rather than accept a write it cannot make durable, the leader refuses it. The write
path is *unavailable* and no data has been lost.

This is the failure mode you engineered for in preference to `R-01`, and the correct emotional
response to seeing it is relief rather than alarm. It is still an incident — customers cannot
place orders — but it is a loud, bounded, recoverable incident rather than a silent unbounded one.

⚠️ There are two distinct exceptions and the difference tells you something useful:

- `NotEnoughReplicasException` — the pre-append check failed. Nothing was written. The
producer can retry safely; there is no possibility of a duplicate.
- `NotEnoughReplicasAfterAppendException` — the record *was* appended to the leader's log,
and then the ISR shrank before enough followers acknowledged it. The record is in the leader's
log below the high watermark's reach, so it is invisible to consumers, and it may or may not
survive a subsequent election. A retry can therefore produce a duplicate. This is precisely the
situation the idempotent producer exists to handle (doc 03, `P-01`), and it is a good concrete
reason to leave idempotence enabled.

**Confirm it.**

```bash
kafka-topics.sh --bootstrap-server $BS --describe --under-min-isr-partitions
```

```promql
kafka_server_replicamanager_underminisrpartitioncount > 0
```

**Recover.** Restore the ISR, which means fixing whatever made a follower fall behind — almost
always `B-01` (a broker down), `B-06` (slow storage), or `B-12` (a rolling restart that moved too
fast). Writes resume automatically the moment the ISR is back to `min.insync.replicas`; there is
nothing to reset.

⚠️ Under pressure, someone will suggest lowering `min.insync.replicas` to 1 to restore writes.
It works, and it converts a recoverable availability incident into a potential data-loss incident
at the worst possible moment — because the ISR is already degraded, which is exactly the
condition under which `M=1` loses data. If you do it deliberately as a business decision, set a
reminder to put it back, and expect to reconcile afterwards. It should be a named decision with
an owner, not a reflex.

**Prevent.** Everything in doc 01: gate rolling restarts on ISR, alert on under-replication
before it becomes under-min-ISR, and keep replication headroom.

### R-06 · `min.insync.replicas` equal to the replication factor

**What you see.** Every routine broker restart blocks writes on some topics. Someone concludes
Kafka is fragile.

**Mechanism.** From the derivation: writes continue through `N − M` failures. With `M = N` that is
zero. Taking any single broker down — for a patch, a resize, a node drain — removes a replica and
immediately puts every affected partition below the minimum.

The team that configures this is usually the team that cares most about durability, which is why
it is worth calling out specifically: the instinct is right and the arithmetic is wrong. `M = N`
buys one additional tolerated failure for acknowledged data and pays for it by making the cluster
non-operable.

**Recover and prevent.** `M = N − 1`. If two tolerated failures for acknowledged data is a real
requirement, the answer is `N = 5, M = 3` — which survives two losses on both axes — not `N = 3, M = 3`. That costs 67% more disk and replication bandwidth, and for a topic where the requirement
is genuine it is the honest price.

---



## Class C — the contract deliberately broken



### R-07 · Unclean leader election

**What you see.** A partition that was unavailable becomes available again, and its log has
*shrunk*. Consumers that had read to offset 8,412,900 find the partition's end at 8,410,150.
`UncleanLeaderElectionsPerSec` recorded a non-zero value. Consumers may log
`OffsetOutOfRangeException`.

**Mechanism.** When every in-sync replica for a partition is unavailable, Kafka has two choices,
and `unclean.leader.election.enable` picks between them:

- `false` **(default since Kafka 0.11).** Keep the partition offline until an in-sync replica
returns. No acknowledged record is ever lost. The partition is unavailable for reads and writes,
potentially for a long time.
- `true`**.** Elect an out-of-sync replica as leader. The partition becomes available immediately.
Every record the previous leader had that this replica lacks is **permanently discarded**, and
the new leader's log end offset is *lower* than the old one's.

The second option does not merely lose data — it loses data that consumers may have already read
and acted upon. `order-processor` can have written an order to `orders-db` and then have the
source record cease to exist, which puts your database ahead of your log and makes the log no
longer a valid source of truth for a rebuild.

⚠️ Consumers whose committed offset is beyond the new leader's log end offset will throw
`OffsetOutOfRangeException` and then apply `auto.offset.reset`. With the default of `latest`, they
silently skip forward. With `earliest`, they reprocess the entire retained topic. Neither is what
you want, and doc 07 (`T-01`) covers why `auto.offset.reset=none` is often the right answer for
important consumers.

**Confirm it.**

```promql
increase(kafka_controller_controllerstats_uncleanleaderelectionspersec_total[1h]) > 0
```

```bash
# Is it enabled anywhere it should not be? Check broker default and per-topic overrides.
kafka-configs.sh --bootstrap-server $BS --describe --entity-type brokers --entity-name 1 \
  | grep unclean
kafka-configs.sh --bootstrap-server $BS --describe --entity-type topics \
  --entity-name clickstream.events --all | grep unclean
```

**Recover.** You cannot recover the discarded records from Kafka. Recovery means reconciling from
an upstream source — for `orders.created`, replaying from `orders-db` or from `checkout-api`'s own
write-ahead record. This is a strong argument for the transactional outbox pattern (doc 05,
`D-05`): if the producer's database holds the authoritative record, an unclean election is
recoverable rather than terminal.

**Prevent.** Leave it `false`, which is the default, and be deliberate about the exception. It is
genuinely the right choice for `clickstream.events`: if all replicas of a clickstream partition
are down, you would rather resume collecting events and lose the gap than stop collecting. Enable
it **per topic**, never cluster-wide:

```bash
kafka-configs.sh --bootstrap-server $BS --alter --entity-type topics \
  --entity-name clickstream.events --add-config unclean.leader.election.enable=true
```

If you need to trigger one manually during an outage — a decision that should involve whoever owns
the data — it is an explicit command, which is the right level of friction:

```bash
kafka-leader-election.sh --bootstrap-server $BS --election-type UNCLEAN \
  --topic orders.created --partition 7
```

---



## Class D — replication itself failing



### R-08 · ISR shrink caused by a slow follower, not a failed one

**What you see.** `UnderReplicatedPartitions` oscillating. All brokers up. No obvious fault.

**Mechanism.** Covered mechanically in doc 01 (`B-06`); here is the part that belongs to the
durability discussion. A follower is in the ISR if it caught up within
`replica.lag.time.max.ms` — **30 seconds**, measured as time since it last matched the leader's
log end offset. This is a deliberately generous, time-based definition, and people frequently
propose tightening it. Do not, and here is why.

An older Kafka version used a *record-count* threshold (`replica.lag.max.messages`). It was
removed because it is unusable: the right number depends on the write rate, which varies. A
threshold that keeps followers in the ISR at 640 records/s ejects all of them at 3,400 records/s
during a flash sale — so the topic drops below min ISR and stops accepting writes at precisely the
moment the business most needs it to work. The time-based definition is scale-invariant, which is
why it is the one that survived.

⚠️ Lowering `replica.lag.time.max.ms` to "detect problems faster" makes the ISR more brittle and
makes `R-05` more likely, without improving durability at all. The 30-second default is correct
for almost everyone.

**Confirm it.** `kafka_server_replicafetchermanager_maxlag{clientId="Replica"}` gives the
furthest-behind follower in records. Correlate with the produce latency breakdown from `B-06`; if
`RemoteTimeMs` is high while `LocalTimeMs` is not, the bottleneck is on the follower side.

**Prevent.** Address the storage or network cause. If followers are broadly unable to keep up
rather than one being faulty, see `R-09`.

### R-09 · A follower that can never catch up

**What you see.** A restarted or newly-added broker's replicas sit under-replicated for hours and
the lag is not closing, or closes only during off-peak hours.

**Mechanism.** A follower must fetch both the backlog it missed *and* the live write rate. If the
sum exceeds its available replication throughput, it never converges. The knob that most often
causes this is `num.replica.fetchers`**, which defaults to 1** — one fetcher thread per
source broker. One thread fetching from one leader is a single TCP stream, and a single stream
does not saturate a modern network interface.

For a Riverbend broker rejoining after five minutes down:

```
backlog:     40.5 MB/s × 300 s            = 12,150 MB to fetch
live rate:   40.5 MB/s must be kept up with concurrently
```

If the fetcher achieves 100 MB/s, the surplus over the live rate is `100 − 40.5 = 59.5 MB/s`, so
the backlog closes in `12,150 ÷ 59.5 ≈ 204 s` — about three and a half minutes, which is fine.
If the fetcher achieves only 50 MB/s, the surplus is `9.5 MB/s` and the backlog takes
`12,150 ÷ 9.5 ≈ 1,279 s` — over 21 minutes, during which the topic tolerates no further failures.
And if the achieved rate is below 40.5 MB/s, it never catches up at all, which is the state where
people start restarting things.

⚠️ The trap: a reassignment throttle set during an earlier incident and never removed. A
`--throttle 50000000` left in place caps replication at 50 MB/s forever, and it is invisible
unless you look for it specifically. Check it before diagnosing anything else:

```bash
kafka-configs.sh --bootstrap-server $BS --describe --entity-type brokers --entity-name 1 \
  | grep -E "replication.throttled.rate"
```

**Recover.** Remove stale throttles. Raise `num.replica.fetchers` to 4–8 (it is a broker setting
and requires a restart, so plan it). Temporarily reduce the live write rate if you can — pausing a
non-critical producer to let replication converge is a legitimate move.

**Prevent.** `num.replica.fetchers=4` as a baseline on any cluster with more than a handful of
brokers, and always run `--verify` after a reassignment, which is what removes the throttle.
Add a scheduled check for lingering throttle configuration.

### R-10 · A topic silently created with replication factor 1

**What you see.** A topic nobody remembers creating, with RF=1, discovered when a broker failure
takes it completely offline. Frequently something like `orders.created.DLQ` — the same name with
different capitalisation from a typo in a client configuration.

**Mechanism.** `auto.create.topics.enable` defaults to `true`. Any client that produces to or
subscribes to a name that does not exist causes the broker to create it, using the broker defaults
`num.partitions` (1) and `default.replication.factor` (1). So a typo in a topic name produces a
single-partition, single-replica topic that accepts writes happily and has no redundancy at all.

The mechanism is worse than it first appears for two reasons. It produces topics with **RF=1**,
which have no durability whatsoever and go fully offline on one broker failure. And it produces
them *silently* — the produce succeeds, so no error surfaces anywhere, and the data goes into a
topic no consumer is reading.

**Confirm it.** Audit for anything under-replicated by design:

```bash
kafka-topics.sh --bootstrap-server $BS --describe \
  | awk '/ReplicationFactor: 1/ {print "RF=1: " $2}'
```

Also list topics with no consumer group, which finds typo-topics that are silently absorbing
writes:

```bash
kafka-topics.sh --bootstrap-server $BS --list | grep -v '^__' | sort > /tmp/all_topics
kafka-consumer-groups.sh --bootstrap-server $BS --list | while read g; do
  kafka-consumer-groups.sh --bootstrap-server $BS --describe --group "$g" 2>/dev/null \
    | awk 'NR>1 && $2!="" {print $2}'
done | sort -u > /tmp/consumed_topics
comm -23 /tmp/all_topics /tmp/consumed_topics
```

**Recover.** Increase the replication factor with a reassignment — it cannot be changed with
`kafka-configs.sh`, because adding a replica means copying data:

```bash
cat > increase-rf.json <<'EOF'
{"version":1,"partitions":[
  {"topic":"orders.created.DLQ","partition":0,"replicas":[1,3,5]}
]}
EOF
kafka-reassign-partitions.sh --bootstrap-server $BS \
  --reassignment-json-file increase-rf.json --execute
```

**Prevent.** `auto.create.topics.enable=false` on every production cluster, and topic creation
through a declarative, reviewed path. The objection is always that auto-creation is convenient in
development; the answer is that development clusters can keep it and production cannot. This is
one of the highest-value single settings on this list, because it eliminates an entire category of
invisible problem.

---



## What to take away

1. **Replication factor is not durability.** `min.insync.replicas` is. An acknowledged `acks=all`
  record exists on `min.insync.replicas` replicas, not on `replication.factor` of them.
2. **Two numbers, both derivable.** With RF `N` and minimum ISR `M`: acknowledged data survives
  **M − 1** losses; writes continue through **N − M** losses. Every durability decision is a
   point on that trade.
3. **Set** `min.insync.replicas = replication.factor − 1` **and require** `acks=all`**.** For RF=3 that
  is 2. Deviate only with a written reason.
4. `min.insync.replicas` **does nothing without** `acks=all`**.** The topic setting and the producer
  setting are two halves of one agreement, and auditing only one of them is the most common way
   this goes wrong.
5. **`min.insync.replicas=1` is indistinguishable from a correct configuration until the moment
  it matters.** Same latency, same throughput, all tests pass — and no durability during the
   exact failure you replicated for.
6. `M = N` **is a trap for careful people.** It makes every routine restart a write outage. If you
  need to survive two losses, go to RF=5 with M=3.
7. `NotEnoughReplicasException` **is good news.** It is the loud, bounded, no-data-lost failure
  you chose over the silent one. Lowering the minimum to clear it trades a recoverable incident
   for an unrecoverable one.
8. **Unclean leader election discards acknowledged records and shortens the log.** Leave it off,
  enable it per topic where availability genuinely outranks the data, and never cluster-wide.
9. **Replication is not free of the physical world.** Kafka does not fsync per write, so
  correlated power loss can beat any replication factor confined to one failure domain. Spread
   replicas across zones; that is the real answer.
10. **Turn off** `auto.create.topics.enable`**.** It converts typos into RF=1 topics that accept
  writes silently and lose them completely.

Next: [03-producer-failure-modes.md](03-producer-failure-modes.md), which moves from "was it
replicated" to "was it written once, in order, at all."