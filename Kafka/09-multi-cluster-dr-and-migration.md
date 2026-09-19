# Multi-Cluster, Disaster Recovery, and Migration

Doc 08 ended with the reasons a single cluster stops being enough. This doc is about what happens
next. It covers considerably less ground than the earlier docs, deliberately: multi-cluster Kafka
has fewer mechanisms and each one has a small number of sharp edges, and the sharp edges are
almost all versions of a single problem.

**That problem: offsets do not mean the same thing on two clusters.** Everything difficult about
Kafka disaster recovery follows from it.

## Why offsets do not transfer

An offset is a position in one partition of one log on one cluster. When MirrorMaker copies
`orders.created` partition 7 from cluster A to cluster B, cluster B's partition 7 is a *new log*.
It starts at offset 0 regardless of where A's started, and every subsequent offset is whatever B
assigned on arrival.

So A's offset 8,412,901 might be B's offset 43,118. There is no arithmetic relationship between
them — the difference is not a constant, because A's log may have had segments deleted by
retention before mirroring began, because mirroring may have been paused and resumed, and because
A's log contains transaction markers (doc 05) that are not reproduced identically.

This means a consumer group's committed offsets are **meaningless on the other cluster**. You
cannot copy `__consumer_offsets` across and expect anything sensible. Failing over a producer is
easy — point it at a different bootstrap server. Failing over a *consumer* requires answering
"where in cluster B's log is the record I had reached in cluster A's log", and that question
needs machinery.

## MirrorMaker 2, briefly

MirrorMaker 2 (KIP-382, Kafka 2.4+) runs as a set of Kafka Connect connectors:

| Connector | What it does |
|---|---|
| `MirrorSourceConnector` | Copies records, and replicates topic configuration and ACLs |
| `MirrorCheckpointConnector` | Emits **checkpoints** mapping source offsets to target offsets per consumer group — the machinery that solves the problem above |
| `MirrorHeartbeatConnector` | Writes periodic heartbeats, so you can measure end-to-end lag and detect a stalled mirror |

By default, topics arrive on the target with a prefix: `orders.created` from cluster `us-east`
becomes `us-east.orders.created`. The prefix is not cosmetic — it is how MirrorMaker avoids
infinite replication cycles in a bidirectional setup, because a topic that already carries a
source prefix is not mirrored back.

⚠️ `IdentityReplicationPolicy` keeps the original names, which is what most disaster-recovery
setups want so that consumers do not need reconfiguring on failover. It also removes the cycle
protection, so it is **only safe for one-directional mirroring**. Choosing it for an
active/passive setup and later adding the reverse direction is how people create replication
loops.

---

## Failure catalogue

| Class | Scenarios |
|---|---|
| **A. Failover does not work the way you assumed** | `M-01` … `M-04` |
| **B. Topologies and their costs** | `M-05` … `M-06` |
| **C. Migration** | `M-07` … `M-08` |

---

## Class A — failover does not work the way you assumed

### M-01 · Consumer offsets that were never translated

**What you see.** During a disaster-recovery exercise, consumers pointed at the standby cluster
start from the beginning of every topic, or from the end. Either way, not from where they were.

**Mechanism.** The problem at the top of this doc, encountered for the first time during the
exercise. Producers failed over cleanly, which created the impression that failover works;
consumers did not, because nobody had configured `MirrorCheckpointConnector` or knew it existed.

**Recover and prevent.** Offset translation has two forms and you should know which you are
using.

**Automatic**, since Kafka 2.7: MirrorMaker writes translated offsets directly into the target
cluster's `__consumer_offsets`:
```properties
sync.group.offsets.enabled = true
sync.group.offsets.interval.seconds = 60
emit.checkpoints.enabled = true
emit.checkpoints.interval.seconds = 60
```
⚠️ It only writes offsets for groups that are **not currently active on the target cluster** —
otherwise it would fight a live consumer. So in active/passive this works, and in active/active
it does not, which is one of several reasons active/active is harder (`M-05`).

**Manual**, using the checkpoint topic directly, which is what you need if you want to inspect
or control the cutover:
```java
Map<TopicPartition, OffsetAndMetadata> translated =
    RemoteClusterUtils.translateOffsets(mm2Properties, "us-east", "order-processor-group",
                                        Duration.ofSeconds(30));
consumer.commitSync(translated);   // on the target cluster, before the group starts
```

**The guarantee you get.** Translation maps to an offset **at or before** the true equivalent
position. That is deliberate: it guarantees you never skip records, at the cost of reprocessing
some. So **failover produces duplicates, never gaps** — which is the right trade, and which means
your consumers must be idempotent (doc 05, `D-06`) for disaster recovery to be safe at all.

An organisation that has not made its consumers idempotent does not have a disaster-recovery
plan; it has a disaster-recovery aspiration.

### M-02 · An RPO nobody can state

**What you see.** The disaster-recovery document says "RPO: 5 minutes." Nobody can say where the
number came from or whether it is being met.

**Mechanism.** MirrorMaker is **asynchronous**. It consumes from the source and produces to the
target with no coordination with the source producer, so at any moment some records exist on the
source and not on the target. If the source is lost at that moment, those records are lost.

Your recovery point objective is therefore not a configuration value — it is **a measurement of
mirror lag**, and it varies continuously with load, network conditions, and MirrorMaker's own
health.

Compute the exposure for `orders.created`:

```
mirror lag, measured at steady state:            3 seconds
orders.created average rate:                   640 records/s
records at risk at a typical moment:  640 × 3 = 1,920 orders

during a flash sale, if mirror lag rises to 30 s:
                                    3,400 × 30 = 102,000 orders
```

⚠️ Mirror lag is worst exactly when a disaster is most likely — during a load spike, a network
event, or a partial regional impairment. So the RPO you measure at steady state is the
optimistic one, and the number to plan with is the one observed at peak.

**Confirm and monitor it.** The heartbeat connector exists for this, and it is the reason to
enable it:
```promql
# End-to-end mirror lag from the heartbeat topic — the authoritative RPO signal
time() - kafka_mirror_heartbeat_timestamp_seconds{source="us-east", target="us-west"}
```
Alert when it exceeds your stated RPO. Without this alert your RPO statement is unverified, and
an unverified RPO is a number in a document rather than a property of a system.

**Prevent.** State RPO as a measured distribution — "3 seconds at p50, 30 seconds at p99, alerted
above 60" — rather than a single number. If the business requires an RPO near zero, asynchronous
mirroring cannot provide it and you need a synchronous topology (`M-06`), with the latency cost
that implies.

### M-03 · Failback is harder than failover, and nobody practises it

**What you see.** The failover exercise succeeds. Three days later, returning to the primary
cluster is a multi-day project nobody planned.

**Mechanism.** Failover moves traffic from A to B. Failback must move it back — and by then B has
data that A does not, because B has been accepting writes. The steps are asymmetric:

1. Records written to B during the outage must be mirrored to A. That is the reverse direction,
   which may not have been configured, and which needs a replication policy that does not create
   a cycle with the forward direction.
2. Consumer offsets must be translated **back**, which needs checkpoints in the reverse
   direction too.
3. A's topics now contain the pre-failover records plus the mirrored post-failover records —
   with different offsets than B has for the same records, so every consumer's position must be
   translated again rather than restored from memory.
4. Any consumer that was idempotent on the way out must still be idempotent on the way back, over
   a different duplicate set.

**Prevent.** Configure mirroring in **both directions from the start**, even for an
active/passive topology where the reverse direction normally carries nothing. It costs almost
nothing while idle and it is the difference between a planned failback and an improvised one.
Then exercise the round trip — failover *and* failback — rather than only the first half, which
is the half that works.

### M-04 · The standby cluster nobody monitors

**What you see.** A disaster-recovery exercise reveals the standby has been 40 hours behind for a
month. Or that mirroring of three topics stopped when they were recreated with different
configuration and nobody noticed.

**Mechanism.** A passive cluster produces no user-visible symptoms when it breaks. No customer
complains, no latency graph moves, and the team's attention is entirely on the active cluster.
MirrorMaker connectors fail in ordinary Connect ways — a task dies, a configuration change is
rejected, a topic is added to the source and never picked up because the topic filter did not
match it.

**Prevent.** Treat the standby as production, which means four specific things:

1. **Alert on mirror lag** per topic (`M-02`), not only in aggregate. One stalled topic in
   twenty does not move an aggregate.
2. **Alert on Connect task state.** A `FAILED` task is silent otherwise.
3. **Reconcile the topic list.** A scheduled job comparing the source's topics against the
   target's catches both the filter-mismatch case and the recreated-topic case:
   ```bash
   diff <(kafka-topics.sh --bootstrap-server $SRC --list | grep -v '^__' | sort) \
        <(kafka-topics.sh --bootstrap-server $DST --list | grep -v '^__' \
          | sed 's/^us-east\.//' | sort)
   ```
4. **Exercise it on a schedule.** A disaster-recovery plan that has not been executed in six
   months is a hypothesis. The exercise finds `M-01`, `M-03`, and `M-04` while they are cheap.

---

## Class B — topologies and their costs

### M-05 · Active/active, and the conflict nobody resolves

**The appeal.** Two regions both accepting writes, mirrored bidirectionally. No failover step, no
idle standby, and users are served locally.

**What it actually requires**, and each of these is a reason most teams should not do it:

1. **No cross-cluster ordering.** A record written in `us-east` and a record written in `us-west`
   have no defined order relative to each other, even for the same key. Two updates to
   `sku-88431` in the same second, one per region, arrive at both clusters in opposite orders.
   Every consumer must therefore resolve conflicts itself — last-write-wins by timestamp, a
   version vector, or a business rule — and "by timestamp" requires clock synchronisation you do
   not have.
2. **No cross-cluster deduplication.** Idempotence is per cluster. A record produced in both
   regions — by a client that retried against a different endpoint, for instance — appears twice
   and nothing detects it.
3. **Offset translation does not apply to active groups** (`M-01`), so a group consuming in both
   regions cannot have its offsets synchronised.
4. **Cycle prevention constrains your naming.** You need the source prefix, which means consumers
   must subscribe to both `orders.created` and `us-west.orders.created` and handle both — or you
   need a carefully-maintained topic filter.

**When it is genuinely right:** when writes are **partitioned by region** so that conflicts cannot
occur — each region owns a disjoint set of keys, and the mirror exists only so each region can
*read* the other's data. That is a well-defined system. Active/active where both regions can write
the same key is a distributed-consensus problem that Kafka does not solve for you, and choosing it
means choosing to solve it in application code.

### M-06 · The stretch cluster and its latency

**The idea.** Rather than two clusters and asynchronous mirroring, run *one* cluster whose
brokers span regions, with replicas in each. Synchronous replication then gives RPO of zero.

**The cost, derived.** `acks=all` waits for every in-sync replica. If replicas are in regions
60 ms apart:

```
minimum produce latency with acks=all = one cross-region round trip ≈ 60 ms
checkout-api's budget for the entire request                        = 2,000 ms
p99 produce latency today                                           ≈ 8 ms
```

Sixty milliseconds per produce, up from eight. That may be acceptable for `orders.created` and it
is not acceptable for `clickstream.events` at 85,000 records/s, where it changes the batching
economics entirely.

There is also a subtler problem: `replica.lag.time.max.ms` (30 s) is generous enough that a
cross-region replica stays in the ISR through ordinary network variation, but a network event
between regions causes ISR shrink on *every* partition simultaneously, which with
`min.insync.replicas=2` means cluster-wide write rejection. You have coupled two regions'
availability rather than decoupling them.

**Where stretch clusters do work:** across **availability zones within one region**, where round
trips are one to two milliseconds. Riverbend already does this, and it is why a single AZ failure
is survivable without any of the machinery in this doc. Stretching across zones is ordinary good
practice; stretching across regions is a specialised choice that needs the latency budget to
support it.

⚠️ A three-zone stretch within a region protects against zone failure and **not** against region
failure, human error, or a bad configuration change — all three of which propagate instantly to
every replica. A stretch cluster is high availability, not disaster recovery, and conflating the
two is common. If your recovery requirement includes "someone deleted a topic," you need a second
cluster or a backup, because replication faithfully replicates the deletion.

---

## Class C — migration

### M-07 · Migrating to a new cluster without downtime

**The situation.** A new cluster — a new region, a managed service, a version too far to upgrade
in place, or the criticality split from doc 08 (`S-12`).

**The approach that works**, in order, with the reasoning for each step:

1. **Mirror the old cluster to the new one** with `IdentityReplicationPolicy` so topic names
   match, and with checkpoints enabled from the start.
2. **Let it catch up**, and verify. Compare record counts per partition and confirm heartbeat lag
   is small and stable.
3. **Move consumers first, not producers.** This is the step people get backwards. Consumers on
   the new cluster read mirrored data, so they can be validated against real traffic while the
   old cluster is still authoritative, and rolled back trivially if something is wrong. Moving
   producers first means data exists only on the new cluster and rollback loses it.
4. **Move producers, one at a time, per topic.** During the transition a topic has producers on
   both clusters and the mirror is still running, so consumers on the new cluster see both the
   locally-produced records and the mirrored ones. That is fine if consumers are idempotent — and
   it is another reason `D-06` is the foundation for everything in this doc.
5. **Stop the mirror** once no producer writes to the old cluster and consumers have drained it.
6. **Keep the old cluster running, read-only, for at least one full retention period.** It is
   your rollback, and decommissioning it early converts a reversible migration into a
   one-way one.

⚠️ The step that most often goes wrong is 4, because "producers, one at a time" requires knowing
every producer. On a platform cluster with 40 teams, the honest first task of a migration is
discovering who actually writes to each topic, and the answer is reliably different from the
documentation.

### M-08 · Dual-write instead of mirroring

**The alternative.** Rather than mirroring, have producers write to both clusters during the
transition.

**Why it looks attractive:** no MirrorMaker to operate, no offset translation, and the cutover is
a configuration change per producer.

**Why it usually is not:** a dual-write is two independent operations with no atomicity. When one
succeeds and the other fails — which happens whenever either cluster has a bad minute — the two
clusters diverge, and nothing detects or repairs it. You have taken the problem MirrorMaker
solves and moved it into every producer, individually, with no reconciliation.

**When it is reasonable:** short, supervised migrations of a small number of producers where the
source of truth is elsewhere — an outbox table (doc 05, `D-05`) that can be replayed to whichever
cluster fell behind. If you can reconcile, dual-write is fine. If you cannot, use the mirror.

---

## What to take away

1. **Offsets do not transfer between clusters.** Every hard part of multi-cluster Kafka is a
   consequence of this, and producers failing over cleanly creates a false impression that
   consumers will too.
2. **Offset translation maps to an offset at or before the true position,** so failover produces
   duplicates and never gaps. Idempotent consumers are therefore a prerequisite for disaster
   recovery, not an enhancement.
3. **RPO is a measurement, not a setting.** It equals mirror lag, it is worst during exactly the
   conditions that cause disasters, and it is unverified unless you alert on the heartbeat topic.
4. **Configure both mirroring directions from the start,** because failback needs the reverse
   direction and the reverse direction is not configured during an outage.
5. **A standby cluster produces no symptoms when it breaks.** Alert on per-topic mirror lag,
   Connect task state, and a scheduled topic-list reconciliation.
6. **Active/active without region-partitioned keys is a consensus problem Kafka does not solve.**
   No cross-cluster ordering, no cross-cluster deduplication, no offset sync for active groups.
7. **A stretch cluster across zones is good practice; across regions it buys RPO zero for a
   cross-region round trip on every write** — and couples two regions' availability.
8. **A stretch cluster is high availability, not disaster recovery.** It replicates deletions and
   bad configuration faithfully.
9. **Migrate consumers before producers,** so the old cluster stays authoritative and rollback
   stays free.
10. **Keep the old cluster read-only for a full retention period** after cutover. It is the
    rollback plan.

Next: [10-observability-and-slos-for-kafka.md](10-observability-and-slos-for-kafka.md), which
collects every metric these nine docs have referenced into one alerting surface.
