# How Kafka Breaks at Scale

Every failure in docs 01–07 exists at every cluster size. What changes with scale is **which ones
dominate, how fast they arrive, and whether the standard fix still works**. A rebalance that took
three seconds takes three minutes. A rolling restart that took a coffee break takes a working day
and a change-management ticket. A configuration that was a reasonable default becomes the
constraint on the whole platform.

This doc compares Riverbend at two points in its life and derives each breakpoint, so that you can
do the same arithmetic for your own cluster and find out which wall you hit first.

## The two Riverbends

| | **Year 1** | **Year 3** | Factor |
|---|---|---|---|
| Brokers | 6 × `m5.2xlarge` (8 vCPU, 32 GiB) | 18 × `m5.4xlarge` (16 vCPU, 64 GiB) | 3× count, 2× size |
| Disk per broker | 2 TB gp3 | 6 TB gp3 | 3× |
| Topics | 6 | 340 | 57× |
| Partitions | 346 | 11,800 | 34× |
| Partition-replicas | 838 | 31,400 | 37× |
| **Partition-replicas per broker** | **140** | **1,744** | **12×** |
| Consumer groups | 6 | 180 | 30× |
| Largest consumer group | 40 members | 200 members | 5× |
| Client instances | ~130 | ~3,000 | 23× |
| Ingress, average | 43 MB/s | 217 MB/s | 5× |
| Ingress, peak | 115 MB/s | 575 MB/s | 5× |

Notice the shape of the growth, because it is typical and it is the reason scale problems
surprise people. **Traffic grew 5×. Partitions grew 34×. Topics grew 57×.** The bytes are not
what got hard. What got hard is the *metadata* — the number of things the cluster has to keep
track of, coordinate, and move — and almost nobody capacity-plans for that.

This happens because Kafka succeeds. Riverbend went from one team's order pipeline to the default
integration substrate for 40 teams, each creating topics with a default partition count nobody
questioned. The bytes are still dominated by `clickstream.events`; the operational load is
dominated by 339 topics that carry almost no data.

---

## Breakpoint catalogue

| Class | Scenarios |
|---|---|
| **A. Metadata volume** — partitions, topics, and what they cost | `S-01` … `S-03` |
| **B. Coordination** — groups, rebalances, and the offsets topic | `S-04` … `S-06` |
| **C. Operations** — restarts, recovery, and change windows | `S-07` … `S-08` |
| **D. Physical limits** — network, memory, connections, money | `S-09` … `S-11` |
| **E. The organisational breakpoint** | `S-12` |

---

## Class A — metadata volume

### S-01 · Partitions per broker: 140 becomes 1,744

This is the primary scale axis in Kafka, and it is worth separating the things people conflate.
A partition-replica on a broker costs you in **five independent ways**, and they hit at different
thresholds:

**1. File descriptors.** Three per segment plus connections (doc 01, `B-08`):

```
Year 1:  140 replicas × 11 segments × 3 =  4,620 descriptors
Year 3:  2.53 TB ÷ 1,744 replicas       =  1.45 GB per replica
         1.45 GB ÷ 1 GB segments        ≈  2 segments per replica
         1,744 × 2 × 3                  = 10,464 descriptors
```
Both fit comfortably in a 100,000 limit and neither fits in the default 1,024. This one is
solved by configuration and stays solved.

**2. Memory for indexes.** Each segment has a memory-mapped offset index and time index, sized by
`log.index.size.max.bytes` (10 MB each by default, sparsely populated). The mapped regions are
virtual, so the real cost is page-cache pressure and address space — noticeable at tens of
thousands of segments per broker, not at ten thousand.

**3. Replication fetch requests.** Each broker fetches from each other broker for the partitions
it follows, and those are batched per broker pair, not per partition. So this scales with **broker
count**, not partition count — `num.replica.fetchers` (default 1) threads per source broker. At
18 brokers that is 17 peer relationships, and with the default of one fetcher thread each, a
single thread carries all partitions from one peer. Doc 02 (`R-09`) shows why that becomes the
catch-up bottleneck.

**4. Controlled shutdown time.** The broker must move leadership for every partition it leads:

```
Year 1:  140 replicas ÷ 3 (RF) ≈   47 leaderships →  ~20 seconds
Year 3:  1,744 ÷ 3             ≈  581 leaderships →  ~90 seconds
```
This is the number that turns a rolling restart from minutes into hours (`S-07`).

**5. Unclean recovery time.** Bounded by bytes on disk and read throughput (doc 01, `B-04`), so
it scales with disk size rather than partition count:

```
Year 3: 2.53 TB ÷ 500 MB/s provisioned = 5,060 s ≈ 84 minutes, single-threaded
        with num.recovery.threads.per.data.dir=8, bounded by volume throughput
```

**Where the real ceiling is.** The old guidance of "no more than about 4,000 partitions per
broker and 200,000 per cluster" was mostly a ZooKeeper-mode **controller** constraint — a new
controller had to load all metadata from ZooKeeper before it could act (doc 00). KRaft removes
that: metadata is a replicated log every controller already holds, so controller failover is
size-independent.

⚠️ KRaft raising the cluster ceiling does **not** raise the per-broker ceiling. The five costs
above are all per broker and all still apply. Riverbend's 1,744 replicas per broker is
comfortable; 10,000 would not be, and the symptom would be controlled shutdown taking long enough
to make rolling restarts impractical rather than anything dramatic.

### S-02 · Choosing partition count, once, for the topic's whole life

Doc 05 (`D-07`) established that adding partitions to a keyed topic is a data migration. That
makes the initial choice unusually consequential, so here is the method.

**Start from consumer parallelism, not from throughput.** Throughput per partition is rarely the
binding constraint — a single partition sustains tens of MB/s. The binding constraint is almost
always that one partition is consumed by one member (doc 04, `C-11`), so partition count is your
maximum consumer parallelism forever.

```
1. Peak arrival rate                       orders.created: 3,400 records/s
2. Per-consumer-instance throughput        measured: 120 records/s
3. Required members at peak                3,400 ÷ 120 = 29 members
4. Headroom for growth, ×2                 58
5. Headroom for skew, ×1.5 (doc 03, P-10)  87
6. Round to a number with useful factors   96
```

Step 6 matters more than it looks. A partition count with many divisors lets you run the group at
many sizes with even assignment: 96 divides by 2, 3, 4, 6, 8, 12, 16, 24, 32, 48. A count of 97
does not, so every group size leaves some members with one more partition than others — which,
under `RangeAssignor`, means predictable imbalance.

⚠️ Riverbend chose 24 for `orders.created` in year 1, sized for the traffic it had. By year 3 it
needs 96 and cannot get there without the migration in doc 05. **Over-provisioning partitions is
much cheaper than adding them later** — up to the per-broker limits in `S-01`, and provided you
do not over-provision every topic, which is `S-03`.

### S-03 · 340 topics, most of them almost empty

**What you see.** Cluster metadata is large, topic listing is slow, and 94% of the bytes are in
one topic while 94% of the partitions are in the other 339.

**Mechanism.** When Kafka becomes a platform, teams create topics with the default partition
count. Riverbend's default is 12. Three hundred and thirty-nine topics at 12 partitions with
RF=3 is:

```
339 × 12 × 3 = 12,204 partition-replicas
```

which is 39% of the cluster's total replicas, carrying a negligible share of the bytes. Every one
of them costs leadership to move on restart, a fetch relationship to maintain, and index memory.

⚠️ The `num.partitions` broker default is one of the highest-leverage settings on a platform
cluster, and it is almost always left at whatever it was. Setting it to **1** forces teams to
choose deliberately; anyone who needs more will ask, and that conversation is where `S-02`
happens.

**Prevent.** Topic creation as a reviewed, declarative process (doc 02, `R-10` already argued for
disabling auto-creation). Add a partition-count budget per team, and a periodic audit that finds
topics with no traffic and no consumer group — on a platform cluster there are always dozens, and
deleting them is the cheapest capacity work available.

---

## Class B — coordination

### S-04 · Rebalance time: why 200 members is not 5× worse than 40

**What you see.** `clickstream-rollup-group` at 200 members: rebalances that took 3 seconds now
take minutes, and they happen more often.

**Mechanism.** Two effects compound, and only one of them is linear.

**The linear part.** More members means more JoinGroup and SyncGroup requests, a larger assignment
payload for the leader to compute and distribute, and more coordinator work. This is real and
manageable.

**The non-linear part** is the barrier (doc 04). The coordinator waits for **every** member, so
the rebalance takes as long as the slowest one. As the group grows, the chance that *at least
one* member is in a slow state approaches certainty:

```
Suppose any given member has a 2% chance of being mid-long-batch when a rebalance starts.

 40 members:  1 − 0.98^40  = 55%  chance that at least one member is slow
200 members:  1 − 0.98^200 = 98%  chance that at least one member is slow
```

At 40 members, roughly half of rebalances are fast. At 200 members, **essentially every rebalance
takes the slow path**, because with that many members somebody is always busy. The average
rebalance time does not grow by 5× — it converges on the worst case.

Combine that with `S-05`'s rebalance count and the arithmetic becomes untenable:

```
Rolling restart of 200 pods, eager assignor:
    400 rebalances × 30 s (the slow path, now near-certain) = 12,000 s = 3h 20m of stopped consumption
    clickstream.events arrives at 170,000 records/s throughout
    → 170,000 × 12,000 = 2.04 billion records of lag to drain afterwards
```

**This is why cooperative rebalancing and static membership stop being optimisations and become
requirements.** At 40 members they save you time. At 200 they are the difference between a
deployment and an outage. The mechanics are in doc 04 (`C-02`), and the thing scale changes is
whether you can defer them.

⚠️ This is also the strongest practical argument for adopting KIP-848's new consumer group
protocol once you are on Kafka 4.0: it removes the barrier entirely, which removes the
non-linear term rather than mitigating it.

### S-05 · 180 consumer groups and the `__consumer_offsets` load

**What you see.** Nothing, until the log cleaner has a bad day (doc 07, `T-09`) and then every
broker's disk fills at once.

**Mechanism.** Every group commits offsets for every partition it owns, on a timer. Work out the
write rate:

```
Year 1:    6 groups × ~20 partitions each =    120 partition-offsets
           ÷ 5 s commit interval          =     24 offset records/s

Year 3:  180 groups × ~65 partitions each = 11,700 partition-offsets
           ÷ 5 s commit interval          =  2,340 offset records/s
           × ~200 bytes per record        =    468 KB/s
           × 86,400 s                     =     40 GB/day written
           × RF 3                         =    121 GB/day of cluster writes
```

Now the part that makes this interesting. The **steady-state size** of that data is:

```
11,700 partition-offsets × ~200 bytes ≈ 2.3 MB
```

**The cluster writes 40 GB a day to maintain 2.3 megabytes of state,** and the only thing keeping
the topic from growing without bound is the log cleaner. Doc 07 (`T-09`) described a dead cleaner
as a slow failure; at this scale it is 121 GB/day of cluster disk, which on a less generously
provisioned cluster is days rather than weeks to a full disk — across every broker simultaneously,
because `__consumer_offsets` partitions are spread over all of them.

⚠️ A second effect at this scale: `offsets.topic.num.partitions` defaults to **50** and
**cannot be changed after the topic is created** without re-mapping every group (doc 04). At 180
groups that is 3.6 groups per partition and fine. At 2,000 groups it is 40 groups per partition,
and the broker leading a given partition is the coordinator for all 40 of them — so a broker
restart triggers 40 groups to find a new coordinator simultaneously. If you expect to reach four
figures of consumer groups, set `offsets.topic.num.partitions` higher **at cluster creation**,
because that is the only opportunity you get.

**Manage it.** Raise `auto.commit.interval.ms` for groups that do not need five-second commit
granularity — going to 30 seconds cuts the write rate by 6×, at the cost of replaying up to 30
seconds of records after a crash, which for an idempotent consumer is free. Alert on cleaner
health. Audit for abandoned groups, which keep committing forever if their pods are still running.

### S-06 · The controller, and why KRaft changed the ceiling

**Year 1, either mode:** fine. 346 partitions is nothing.

**Year 3 in ZooKeeper mode:** 11,800 partitions is still within the old guidance, but the
failure characteristic is what matters. A controller failover requires the new controller to load
all partition metadata from ZooKeeper before it can process anything — and during that window,
**no leader election happens anywhere in the cluster**. A broker failure during a controller
failover is therefore an extended partition outage rather than a nine-second blip.

**Year 3 in KRaft:** metadata is a replicated log that every controller already holds in memory,
so failover is a Raft election — sub-second, independent of cluster size. The `S-01` per-broker
costs are unchanged.

What this means practically: if you are still on ZooKeeper and growing, the migration is not
optional maintenance. ZooKeeper support was removed in Kafka 4.0, so the timeline is set for you,
and the migration path (KIP-866, bridge mode) requires an intermediate version — which means you
cannot defer it until you are on 4.0 and then jump.

⚠️ On KRaft, the controller quorum becomes a distinct availability domain with the deceptive
failure mode from doc 01 (`B-10`): traffic keeps flowing while the cluster silently loses the
ability to respond to any subsequent failure. At Riverbend's year-3 scale, with a rebuild being a
multi-day event, five controllers rather than three is the right call.

---

## Class C — operations

### S-07 · The rolling restart that became a change-management process

**Year 1.** Six brokers, ~140 replicas each:

```
per broker: controlled shutdown 20 s + restart 60 s + ISR catch-up ~100 s ≈ 3 minutes
6 brokers × 3 min = 18 minutes
```

One person, one afternoon, no ceremony.

**Year 3.** Eighteen brokers, ~1,744 replicas each:

```
controlled shutdown (581 leaderships)                          ≈  90 s
process restart and log load                                   ≈  60 s
ISR catch-up: 3.5 min down × 67.6 MB/s peak write = 14.2 GB
    at 150 MB/s fetch, surplus over live rate = 82 MB/s
    14.2 GB ÷ 82 MB/s                                          ≈ 173 s
verification gate (UnderReplicatedPartitions == 0), with margin ≈  60 s
                                                        per broker ≈ 6.4 minutes
18 brokers × 6.4 min                                              ≈ 115 minutes
```

Nearly two hours in the best case, and realistically three once you include a pause to check
dashboards between brokers. You cannot parallelise it safely without the analysis below, and it
now needs a maintenance window, an approval, and someone watching.

**Can you restart more than one at a time?** Only with a specific argument. With RF=3 spread
across three availability zones and `min.insync.replicas=2`, taking down **all brokers in one
zone** leaves two replicas of every partition — still at the minimum, so writes continue. That
turns 18 sequential restarts into 3 sequential zone restarts, roughly 20 minutes.

⚠️ It is also an argument that must be verified, not assumed, and it fails for exactly the
reasons in doc 01 (`B-11`): any partition with two replicas in the same zone drops to one and
goes below the minimum. So this is safe only if replica placement is genuinely rack-aware, which
you must check rather than believe. And it leaves zero tolerance for an unrelated broker failure
during the window.

The honest conclusion for most teams is: **restart one broker at a time, gate on ISR, and accept
that the window grew.** Plan it into the release process rather than discovering it during a
security patch with a deadline.

### S-08 · Recovery times that outgrow your patience

Several recovery operations scale with per-broker data, which grew 1.6× (1.55 TB → 2.53 TB) even
though the cluster grew much more:

| Operation | Year 1 | Year 3 | Scales with |
|---|---|---|---|
| Unclean log recovery (doc 01, `B-04`) | ~53 min at 500 MB/s | ~84 min | Bytes on the broker |
| Replacing a dead broker (re-replicating 1.55 TB → 2.53 TB) | ~5 h at 100 MB/s | ~7 h | Bytes × available replication bandwidth |
| Reassigning one topic off a broker | Minutes | Minutes to hours | Topic size |
| Full cluster rebuild from backup | Days | Days | Do not plan on this |

⚠️ The row that changes behaviour is broker replacement. **Seven hours** of running one replica
short (doc 01, `B-05`) is long enough that a second failure during the window is a realistic
event rather than a theoretical one. That is the argument for either keeping a warm spare broker,
or running Cruise Control so re-replication starts automatically rather than when a human
notices.

---

## Class D — physical limits

### S-09 · Cross-availability-zone network cost

**What you see.** A cloud bill line item that grew faster than the cluster.

**Mechanism.** Consumers connect to partition **leaders**, which are spread across three zones
without regard to where the consumer is. So roughly two-thirds of all consumer traffic crosses a
zone boundary and is billed.

```
Year 3 consumer egress:
    clickstream.events 204 MB/s × 3 consumer groups = 612 MB/s
    all other topics                                ≈  50 MB/s
                                            total   ≈ 662 MB/s

Fraction crossing a zone boundary (consumers spread over 3 zones):  2/3
    662 × 2/3                                       = 441 MB/s
    441 MB/s × 86,400 s                             = 38,102,400 MB/day
                                                    ≈ 38,100 GB/day
At $0.02/GB for cross-zone transfer:
    38,100 × $0.02                                  = $762/day
                                                    ≈ $22,900/month
```

Nearly twenty-three thousand dollars a month, for data that did not need to leave the zone.
(Managed offerings differ on what they bill — several do not charge for inter-broker replication
within a cluster — so check your own provider's terms. Consumer traffic is billed essentially
everywhere.)

**The fix is a client setting.** Since Kafka 2.4 (KIP-392), consumers can fetch from the
**closest replica** rather than the leader:

```
# On every broker
replica.selector.class=org.apache.kafka.common.replica.RackAwareReplicaSelector

# On every consumer
client.rack=us-east-1b
```

A consumer in zone B then reads from a replica in zone B whenever one exists. With RF=3 across
three zones, one always does, so cross-zone consumer traffic goes to approximately zero and the
$22,900 largely disappears.

⚠️ Two caveats worth stating. Follower fetching serves reads only up to the **high watermark**,
which a follower learns slightly later than the leader — so it adds a small amount of latency,
typically single-digit milliseconds. And it does nothing for producers, which must always write
to the leader. For Riverbend that trade is obviously correct; for a latency-critical consumer it
is a measurement rather than an assumption.

### S-10 · Connections, network threads, and 3,000 clients

**What you see.** Request queue time rising on brokers (doc 01, `B-06`) with no disk or
replication problem. Occasional connection timeouts from clients that were previously fine.

**Mechanism.** A consumer opens a connection to **every broker** leading a partition it is
assigned, and at year-3 scale that is most of them:

```
~3,000 client instances × up to 18 brokers = up to 54,000 connections
÷ 18 brokers                               ≈ 3,000 connections per broker
```

Kafka handles connections with `num.network.threads` (default **3**) doing non-blocking I/O, and
`num.io.threads` (default **8**) doing the actual request work. Three network threads for 3,000
connections is workable at low request rates and becomes the bottleneck as request rate climbs —
and request rate climbs with client count, not with bytes, because each client polls on its own
schedule.

The signal that distinguishes this from every other slowness is `RequestQueueTimeMs` from doc 01's
table: requests arriving and waiting for a handler, while `LocalTimeMs` and `RemoteTimeMs` stay
low.

**Manage it.** Raise `num.network.threads` to 8–16 and `num.io.threads` to roughly the vCPU count
at this scale. Set `connections.max.idle.ms` so abandoned connections are reaped. And reduce
client count where it is gratuitous — the `P-11` anti-pattern of a producer per request is a
connection problem as much as a partitioning one.

### S-11 · Page cache, and the finding that goes the other way

Recompute the residency window from doc 00 for year 3:

```
Year 3 broker: 64 GiB RAM − 8 GiB heap − 2 GiB OS ≈ 54 GiB ≈ 57,982 MB page cache
bytes written per broker at peak                  = 67.6 MB/s
residency: 57,982 ÷ 67.6                          = 858 s ≈ 14 minutes

(Year 1 was 11 minutes.)
```

**The window got better, not worse.** Buying larger instances bought back cache headroom faster
than traffic consumed it. It is worth reporting honestly, because it contradicts the expectation
that everything degrades with scale, and because it tells you where *not* to spend attention.

What did get worse is the **blast radius** of crossing the cliff. At year 1, one replay-heavy
consumer degraded 6 brokers serving 6 topics and 6 groups. At year 3 it degrades 18 brokers
serving 340 topics and 180 consumer groups — most of them belonging to teams who have no idea
the other team started a backfill. The mechanism is identical (doc 06, `L-09`); the number of
people affected is 30× higher.

⚠️ **This is the point at which client quotas stop being optional.** On a single-team cluster,
"ask before you backfill" works. On a platform cluster with 40 teams, it does not, and the
default must be a quota that a team asks to have raised:

```bash
# A default ceiling for every client that has not negotiated otherwise
kafka-configs.sh --bootstrap-server $BS --alter \
  --add-config 'consumer_byte_rate=52428800,producer_byte_rate=52428800,request_percentage=200' \
  --entity-type clients --entity-default
```

`request_percentage` is the less-known and often more useful one: it limits the share of broker
request-handler time a client may consume, which catches clients that are expensive in requests
rather than in bytes — a consumer polling every millisecond with `fetch.min.bytes=1`, for example.

---

## Class E — the organisational breakpoint

### S-12 · When to stop growing the cluster and split it

Every scenario above has a technical fix. The reason large organisations end up with several
Kafka clusters is not that a single one cannot be made to work — it is **blast radius and
governance**, and those do not have technical fixes.

The arguments for splitting, in the order they usually become compelling:

1. **One tenant can hurt everyone.** `S-11` is the clearest case: a backfill by one team degrades
   340 topics. Quotas mitigate it and do not eliminate it, because a quota cannot distinguish a
   legitimate surge from an accidental one.
2. **The maintenance window is shared.** A two-hour rolling restart (`S-07`) is a two-hour
   elevated-risk window for every team on the cluster, coordinated with all of them. Two clusters
   of nine brokers restart in an hour each and can be scheduled independently.
3. **Configuration is cluster-wide.** `auto.create.topics.enable`, `num.partitions`,
   `offsets.topic.num.partitions`, `unclean.leader.election.enable`, the broker's default
   `min.insync.replicas` — all of these are one value for everyone. A cluster carrying both
   payment events and clickstream events must pick settings that suit both, which means it suits
   neither.
4. **Upgrades are all-or-nothing.** One team needing a Kafka 4.0 feature moves the whole platform.
5. **Failure domains follow ownership, badly.** An incident on a platform cluster involves
   whoever owns the cluster, not whoever owns the data, and those are different people with
   different context.

The split that usually works is **by criticality rather than by team** — because team-based
splits multiply indefinitely while criticality has about three levels:

| Cluster | Topics | Configuration character |
|---|---|---|
| **Critical** | `orders.created`, `payments.settled`, `inventory.adjustments` | RF=3, `min.insync.replicas=2`, unclean election off, conservative quotas, smaller and restartable quickly |
| **Bulk** | `clickstream.events` and other high-volume telemetry | RF=2, `min.insync.replicas=1`, unclean election on, tuned for throughput and cost |
| **Platform** | The 339 small topics from 40 teams | Strict quotas, strict partition budgets, auto-creation off, generous defaults for durability |

⚠️ The cost is real and should be stated alongside the benefits: three clusters means three
upgrade cycles, three sets of monitoring, and — the significant one — **cross-cluster joins
become impossible**. A consumer cannot read two clusters in one transaction, and mirroring between
them brings its own failure modes (doc 09). Do not split until one of the five arguments above is
actually hurting; splitting a cluster is much easier than merging two.

---

## What breaks first, in order

If you want one thing to take away from this doc, it is this table. These are the thresholds at
which each concern typically stops being theoretical, for a cluster growing the way Riverbend
did.

| Breakpoint | Typically bites around | First symptom | Section |
|---|---|---|---|
| Eager rebalance becomes untenable | ~50 members in one group | Deployments cause visible lag spikes | `S-04` |
| Rolling restart needs a window | ~12 brokers or ~1,000 replicas/broker | Restarts take more than an hour | `S-07` |
| Cross-zone cost becomes a line item | ~200 MB/s of consumer egress | Finance asks about the network bill | `S-09` |
| Client quotas become mandatory | ~10 teams on one cluster | One team's backfill degrades others | `S-11` |
| `__consumer_offsets` load matters | ~500 consumer groups | Log cleaner failures become expensive fast | `S-05` |
| Network threads saturate | ~2,000 client connections per broker | `RequestQueueTimeMs` rises with healthy disks | `S-10` |
| Partition count per broker | ~4,000 replicas/broker | Controlled shutdown exceeds several minutes | `S-01` |
| Splitting the cluster | ~20 teams, or one critical plus one bulk workload | Configuration arguments between teams | `S-12` |

---

## What to take away

1. **Traffic grows linearly; metadata grows superlinearly.** Riverbend's bytes grew 5× while
   partitions grew 34× and topics 57×. Capacity-plan for the metadata, because that is what gets
   hard.
2. **Partitions per broker cost you five separate things** — descriptors, index memory, fetch
   relationships, controlled-shutdown time, and recovery time — and they hit at different
   thresholds. KRaft raises the cluster ceiling and changes none of them.
3. **Size partition count for the topic's whole life,** from consumer parallelism with headroom
   for growth and skew, and round to a number with many divisors.
4. **Set `num.partitions=1` as the broker default** on a platform cluster, so that every topic's
   partition count is a decision somebody made.
5. **Rebalance cost converges on the worst case as a group grows.** At 200 members, 98% of
   rebalances hit the slow path, so cooperative rebalancing and static membership become
   requirements rather than optimisations.
6. **Riverbend writes 40 GB a day to `__consumer_offsets` to maintain 2.3 MB of state.** That
   makes log-cleaner health a first-order concern, and `offsets.topic.num.partitions` a decision
   you get to make exactly once.
7. **An 18-minute rolling restart became two hours.** Zone-at-a-time restarts can recover most of
   that, and only if rack-aware placement is verified rather than assumed.
8. **Replacing a broker takes seven hours at year-3 size,** which is long enough that a second
   failure during the window is a realistic planning assumption.
9. **`client.rack` plus `RackAwareReplicaSelector` saves Riverbend roughly $22,900 a month** in
   cross-zone transfer, for two configuration lines and a few milliseconds of latency.
10. **Page-cache residency improved with scale** because instances grew faster than traffic — but
    the blast radius of crossing the cliff grew 30×, which is what makes client quotas mandatory
    on a shared cluster.
11. **Split clusters for blast radius and governance, not for throughput,** and split by
    criticality rather than by team.

Next: [09-multi-cluster-dr-and-migration.md](09-multi-cluster-dr-and-migration.md), which covers
what happens once you have more than one cluster — by choice or by necessity.
