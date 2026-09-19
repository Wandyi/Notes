# Staff-Level Interview Questions: Kafka

Sixteen questions with full model answers. They are written to be *spoken* — what a strong
candidate would actually say, including the clarifying question they would ask first and the
trade-off they would name unprompted.

These are not trivia questions. Every one of them has a defensible wrong answer, and what
distinguishes a staff-level response is usually not more knowledge but a different move: naming
the trade-off, asking what the requirement actually is, or noticing that the premise of the
question is incomplete. A section at the end contrasts senior and staff answers explicitly.

Useful either way round — to prepare for an interview, or to run one.

---

### Q1. A team tells you their pipeline is durable because they use replication factor 3 and `acks=all`. What do you check next?

Two things, and the second one is the one that catches people.

**First, `min.insync.replicas` on the topic.** Replication factor tells me how many replicas the
partition *has*; it says nothing about how many a record was written to before the producer was
told it succeeded. That is `min.insync.replicas`. If it is 1 — and it is 1 unless someone set it,
because that is the broker default — then when the ISR shrinks to a single replica, the leader
acknowledges writes on its own authority. Lose that leader and those acknowledged records are
gone.

The arithmetic I would write on the whiteboard: with replication factor `N` and minimum in-sync
`M`, an acknowledged record survives `M − 1` failures, and the partition keeps accepting writes
through `N − M` failures. With `M = 1` that is zero and two. Zero is the number that matters.

**Second, whether `acks=all` is true of every producer, not just the one they showed me.**
`min.insync.replicas` is only consulted for `acks=all` writes. A topic set to
`min.insync.replicas=3` gives an `acks=1` producer exactly the durability of one broker. The two
settings are two halves of one agreement, and they live in different places owned by different
teams, which is why auditing one of them is the usual failure.

The thing I would specifically warn them about is that `min.insync.replicas=1` is
**indistinguishable from a correct configuration during normal operation**. With a full ISR,
`acks=all` waits for all three replicas regardless. Same latency, same throughput, every test
passes. It diverges only when the ISR has shrunk — which is precisely the failure you bought
replication to survive. That is what makes it survive code review: everyone checks the producer,
sees `acks=all`, and ticks the box.

**Follow-up I would expect:** *"What should it be set to?"* — `replication.factor − 1`. Not equal
to the replication factor, which is the trap careful people fall into: with `M = N`, any single
broker restart blocks writes, so rolling restarts become outages. If they genuinely need to
survive two failures, that is RF=5 with `M=3`, and it costs 67% more storage.

---

### Q2. Consumer lag on one group is growing. Every broker metric is green. Walk me through triage.

I would want to eliminate the confusing case first, because it is cheap and it is the one that
costs people a day.

**Step 1 — is this a last-stable-offset problem?** If the topic has transactional producers, run
a console consumer against it twice, once with `--isolation-level read_uncommitted` and once with
`read_committed`. If the first sees recent records and the second does not, there is a hanging
transaction pinning the last stable offset, and the consumer is fine. Thirty seconds, and it
either eliminates or confirms the failure that is invisible to every other diagnostic.

**Step 2 — is the group stable?** `kafka-consumer-groups.sh --describe --state`, three times in a
row. If it reports `PreparingRebalance` or `CompletingRebalance` repeatedly, it is a rebalance
storm and I go to Q6. Confirm with rebalance rate — anything above a few per hour is abnormal.

**Step 3 — what is the shape of the lag across partitions?** This distinguishes three different
problems in one graph:

- **Uniform across all partitions** — a capacity problem. The group cannot keep up. Compute drain
  time and decide whether to scale, and check first that scaling is even possible.
- **One partition much higher, growing linearly** — a stuck partition. Poison message, or a
  record being retried forever. Find the offset and read it.
- **One partition much higher, growing proportionally** — key skew. One key or a small set of
  keys dominates that partition, and adding consumers will not help because one partition is one
  consumer.

**Step 4 — is the consumer actually working?** Consumer CPU, and the downstream's latency. Very
often the consumer is idle and the bottleneck is a database or an API, in which case the answer
is not a Kafka change at all.

The thing I would emphasise is that **"scale it up" is the wrong first move** and actively makes
two of these worse — more members deepens a rebalance storm, and more members adds concurrent
load to a downstream that is already the constraint.

---

### Q3. Design the topic configuration for a payments event stream. Justify every value.

I would ask two questions first, because they change the answer completely: *what is the
consequence of losing one record*, and *is there a source of truth outside Kafka*? For payments I
will assume losing a record is a financial and audit problem, and that there is a ledger
database.

```
replication.factor          = 3
min.insync.replicas         = 2
acks                        = all              (producer side, must match)
enable.idempotence          = true             (default on 3.0+; do not override acks)
unclean.leader.election     = false            (the default; leave it)
retention.ms                = 604800000        (7 days)
cleanup.policy              = delete
partitions                  = 12
max.message.bytes           = default
compression.type            = producer
message.timestamp.type      = CreateTime
```

**Replication factor 3, minimum in-sync 2.** Survives one broker loss on both axes — writes
continue, and acknowledged data survives. Not 3-and-3, which would make every broker restart a
write outage.

**Unclean leader election off.** For payments, a partition being unavailable is strictly better
than a partition silently shortening its log. I would rather the write path stop than discard an
acknowledged settlement.

**Seven days of retention.** Derived from the maximum tolerable consumer outage, not from a
storage budget. A settlement consumer broken on a Friday before a holiday weekend needs to be
able to come back on Tuesday and still find its data. I would then check what seven days costs
and negotiate if it is unaffordable — but starting from the recovery requirement and pricing it
is the right order, and starting from the storage budget produces a number with no relationship
to anything.

**Twelve partitions**, sized from consumer parallelism with headroom, not from throughput. A
payments stream is low-volume; the constraint is that partition count is a permanent ceiling on
consumer parallelism and increasing it later breaks per-key ordering. Twelve has useful divisors,
so the group can run at 2, 3, 4, 6, or 12 members with even assignment.

**Keyed by the entity whose ordering matters** — the account or the settlement batch, not the
event id. This has to be a documented contract, because no configuration can enforce it and every
producer must honour it.

**`CreateTime`, with timestamp bounds** (`message.timestamp.before.max.ms` /
`after.max.ms`). Event time matters for reconciliation, so I keep it, and I bound the skew so a
producer with a broken clock is rejected loudly rather than writing records that either expire
immediately or never expire.

**What I would add beyond topic configuration**, because the topic settings are the smaller half:

- **Consumers store their offsets in the ledger database, in the same transaction as the write.**
  That is the only way to get genuine exactly-once against an external store; Kafka transactions
  do not cover Postgres.
- **`auto.offset.reset=none`.** If the offset is ever invalid I want the consumer to refuse to
  start and page somebody, not to silently skip or silently reprocess.
- **A transactional outbox on the producer side**, so the ledger is the source of truth and Kafka
  is a replayable derivative. That single decision converts most catastrophic Kafka failures into
  recoverable ones.

---

### Q4. When is exactly-once achievable, and when is it not?

There is one test, and it is not about Kafka features.

A consumer does two things: it produces a side effect, and it commits an offset. If those two
operations can be made **atomic** — either both happen or neither does — then exactly-once is
achievable. If they cannot, it is not, and the honest answer is at-least-once plus idempotent
processing.

That gives three cases:

**Kafka to Kafka, one cluster.** Achievable, with transactions. The output records and the offset
commit go into one transaction via `sendOffsetsToTransaction`, so there is no window where one
exists without the other. Kafka Streams with `processing.guarantee=exactly_once_v2` does this for
you and is the better choice than hand-rolling the protocol.

**Kafka to a transactional database.** Achievable, but not with Kafka transactions — they cover
no state outside Kafka. You get it by storing the consumer's offset **in the destination
database**, in the same transaction as the business write, and seeking to that offset on startup.
Kafka's own committed offsets become advisory, useful for lag monitoring.

**Kafka to anything else** — a third-party API, an email, a payment capture. Not achievable, at
all, by any configuration. The correct goal is **effectively once**: at-least-once delivery plus
idempotent processing, so duplicates are delivered and have no observable effect.

Two things I would add that separate a good answer from a complete one.

**Design for duplicates even when you have transactions.** An operator replays a topic during an
incident, and no protocol protects you from that. A pipeline that only works because duplicates
never occur has no recovery options.

**Transactions have a specific and nasty failure mode**: a hanging transaction pins the last
stable offset, so every `read_committed` consumer stops permanently while every metric stays
green. If I turn on transactions, I add an alert on high watermark minus last stable offset at
the same time, because nothing else represents it.

---

### Q5. A team wants to add partitions to a keyed topic to improve throughput. What do you tell them?

First I would check whether it will help, because usually it will not. The two most common
reasons a consumer group is slow are key skew and a slow downstream, and adding partitions fixes
neither. I would ask to see per-partition lag: if one partition dominates, that is skew, and
`murmur2(key) % 48` puts a hot key on exactly one partition just as `% 24` did.

If more parallelism genuinely is the constraint, then the thing they need to know is that
**adding partitions to a keyed topic is a data migration, not a configuration change.**

The default partitioner is `murmur2(key) % numPartitions`. Change the modulus and about half of
all keys move to a different partition. Records for one entity written before the change are on
one partition and records written after are on another — read by two different consumer
instances, concurrently, with no ordering relationship. That persists for the whole retention
period, not just during the change, and for the entire backlog if a consumer is lagging.

⚠️ And `kafka-topics.sh --alter --partitions` completes in under a second, prints nothing, and
warns about nothing.

The options I would offer, in order:

1. **Do not.** Size partition count for the topic's whole life. Over-provisioning is much cheaper
   than this migration.
2. **New topic at the target count**, migrate consumers, then producers, retire the old one.
   More work, and correct.
3. **Drain first** — stop producers, let consumers reach the end of every partition, resize,
   resume. Correct, and it costs a write outage.
4. **Make ordering unnecessary**: version every event and apply conditionally, so a late older
   event is rejected by the version check. This is the answer I would actually push for, because
   it converts an ordering requirement into an idempotence requirement, and idempotence is much
   easier to guarantee in a distributed system than ordering is.

---

### Q6. A consumer group rebalances constantly. Diagnose it.

The first thing I would establish is which timeout is firing, because there are two and they
detect different things.

`session.timeout.ms` detects a **dead process** — no heartbeat. Heartbeats come from a background
thread, so they keep flowing even while your application thread is stuck.
`max.poll.interval.ms` detects a **live process that stopped consuming** — no `poll()` call. A
slow consumer trips the second and never the first, which is why raising `session.timeout.ms`,
the usual first attempt, does nothing.

For a slow consumer the arithmetic is the diagnosis:

```
max.poll.records × worst-case time per record  versus  max.poll.interval.ms
```

With the defaults that is `500 × your per-record time` against 300 seconds. So any consumer whose
per-record time exceeds 600 milliseconds is at risk with default configuration, and one calling
an API with a p99 of 1.4 seconds is guaranteed to be evicted under load — `500 × 1.4 = 700`
seconds against a 300-second limit.

Then I would explain why it does not recover on its own. When a member is evicted, its partitions
are redistributed to the survivors, who now have more partitions, fuller poll batches, and longer
processing — so the next eviction is *more* likely. Meanwhile the evicted member finishes its
batch, fails to commit, rejoins, and triggers another rebalance. It amplifies.

**The immediate fix** is `max.poll.records`, sized so that the product is under half of the
interval. Half, not just under, because the worst case you measured is not the worst case that
exists.

**The structural fix** is to decouple processing from polling: hand records to a bounded internal
worker pool, keep calling `poll()`, and pause partitions when the pool is full. The subtlety is
that offsets may then only be committed over the contiguous *completed* prefix — committing the
highest completed offset when an earlier one is still in flight silently turns you into
at-most-once.

**What I would also check:** whether the group is on an eager assignor. The default
`partition.assignment.strategy` is `[RangeAssignor, CooperativeStickyAssignor]`, which looks
cooperative and is not — the group negotiates the first strategy all members support, which is
the eager one. Many teams believe they are running incremental rebalances and are not.

---

### Q7. How do you choose a retention period?

Retention is a recovery decision that people make as a storage decision, and that is the whole
answer.

The question to ask is: **what is the longest a consumer of this topic could plausibly be broken,
and still need the data it missed?** Not "how much disk do we have."

Concretely, the inputs are how long a bad deploy can go unnoticed, how long a holiday weekend is,
how long your on-call rotation takes to respond to a non-paging alert, and whether there is any
other copy of the data. Then multiply by a safety factor, then compute the storage cost, then
negotiate if it is unaffordable. Starting from the storage budget produces a number with no
relationship to your recovery requirements, and you discover that during the incident.

Three specifics that catch people:

**Retention deletes whole segments, not records.** Nothing expires until its segment closes, at
`segment.bytes` (1 GB) or `segment.ms` (7 days). On a low-traffic topic that means a one-hour
retention actually retains a week, which matters if the short retention was chosen for a reason —
a deletion commitment, for instance.

**`retention.bytes` is per partition.** Someone capping a 200-partition topic at "500 GB" has
actually authorised 500 GB × 200 partitions × the replication factor. And when both time and byte
limits are set, whichever fires first wins, so an accidentally tight byte cap silently shortens
your documented retention.

**Retention uses producer-supplied timestamps by default.** A backfill publishing historical
records with their original timestamps can be deleted within five minutes of a successful
publish. Every `send()` succeeded and the data is gone.

The operational half: alert on **time to expiry** per consumer group — how long until this
group's position ages out of retention — not just on lag. That alert fires while there is still
time to act.

---

### Q8. What breaks first when a Kafka cluster grows tenfold?

Not the bytes. That is the thing to say first, because it is the surprise.

In the growth I have seen, traffic grows roughly linearly while **partition and topic count grow
superlinearly** — Kafka succeeds, becomes the default integration substrate, and forty teams
create topics with whatever the default partition count is. The bytes stay concentrated in one or
two high-volume topics; the operational load ends up in hundreds of near-empty ones.

So the things that break, roughly in order:

**Rebalances, at around fifty members in one group.** This is the first wall and it is
non-linear. With an eager assignor the coordinator waits for every member, so rebalance time is
the maximum over members, not the average. As the group grows, the chance that at least one
member is mid-batch approaches certainty — at 200 members with a 2% per-member chance, it is 98%,
so essentially every rebalance takes the worst-case path. Cooperative rebalancing and static
membership stop being optimisations and become requirements.

**Rolling restarts, at around a dozen brokers.** Controlled shutdown time scales with partitions
led per broker, and you have to wait for the ISR to recover between brokers. An 18-minute
procedure becomes two hours, which means a maintenance window and an approval.

**Cross-zone network cost**, once consumer egress reaches a few hundred megabytes per second.
Consumers connect to leaders, leaders are spread across zones, so about two-thirds of read
traffic is billed. The fix is two configuration lines — `client.rack` plus
`RackAwareReplicaSelector` — and for a cluster at 650 MB/s of egress it is a five-figure monthly
saving.

**Multi-tenancy, at around ten teams.** "Ask before you run a backfill" works on a single-team
cluster and does not work on a platform. Client quotas have to become the default that teams
negotiate away from, rather than something added after an incident.

**`__consumer_offsets`, at several hundred consumer groups.** It is compacted, so its health
depends entirely on the log cleaner, and the write rate scales with groups times partitions. A
cluster can be writing tens of gigabytes a day to maintain a couple of megabytes of state. Also,
`offsets.topic.num.partitions` cannot be changed after the topic exists, so it is a decision you
get to make exactly once.

**What I would tell them *not* to worry about:** ZooKeeper's old 200,000-partition ceiling if
they are on KRaft, and page-cache pressure if they are also scaling instance sizes — in the case
I have measured, the residency window actually improved with scale, because instances grew faster
than traffic did. What got worse was the blast radius of crossing the cliff, not the cliff
itself.

---

### Q9. Design disaster recovery for a Kafka-backed order pipeline. What is your RPO and how do you know?

The core difficulty is one sentence: **offsets do not mean the same thing on two clusters.**
Cluster B's copy of a topic is a new log starting at zero, with no arithmetic relationship to
cluster A's offsets. Failing over producers is a bootstrap-server change. Failing over consumers
requires machinery.

**The design.** Active/passive with MirrorMaker 2, because active/active without
region-partitioned keys is a consensus problem Kafka does not solve — no cross-cluster ordering,
no cross-cluster deduplication, and offset sync does not work for groups that are active on both
sides.

- `MirrorSourceConnector` for data, `MirrorCheckpointConnector` for offset translation,
  `MirrorHeartbeatConnector` for lag measurement.
- `IdentityReplicationPolicy` so topic names match and consumers need no reconfiguration —
  acceptable because this is strictly one-directional in normal operation.
- Mirroring configured in **both** directions from day one, even though the reverse carries
  nothing, because failback needs it and you will not be configuring it during an outage.

**The RPO, and this is the part I care about most:** it is not a setting, it is a measurement.
MirrorMaker is asynchronous, so at any moment some records exist on the source and not the
target. My RPO equals mirror lag, and mirror lag varies with load.

So I would state it as a distribution rather than a number: measured at p50 and p99, with an
alert above the committed threshold. And I would state the peak figure, not the steady-state one,
because mirror lag is worst during exactly the conditions that cause disasters. If steady-state
lag is 3 seconds at 640 records/s, that is about 1,900 records at risk; if lag reaches 30 seconds
during a load spike at 3,400 records/s, it is 102,000. The second number is the one to plan with.

The heartbeat topic is what makes this measurable, and without that alert the RPO in the document
is unverified.

**Two things I would insist on.**

**Consumers must be idempotent.** Offset translation deliberately maps to an offset at or *before*
the true position, so failover produces duplicates and never gaps. An organisation whose consumers
cannot tolerate duplicates does not have a disaster-recovery plan.

**Exercise failover and failback, not just failover.** Failback is harder — the standby has
accumulated data the primary lacks, offsets must be translated in the reverse direction, and
consumers face a different duplicate set. Teams practise the first half and discover the second
half during a real event.

⚠️ And I would push back if anyone proposed a cross-region stretch cluster instead. It gives RPO
zero at the cost of a cross-region round trip on every `acks=all` write, and it couples the two
regions' availability rather than decoupling them. A stretch cluster across *zones* is ordinary
good practice; across regions it is a specialised choice. It is also high availability, not
disaster recovery — it replicates an accidental topic deletion faithfully and instantly.

---

### Q10. Why might a read-only consumer degrade the entire cluster?

Because Kafka's performance model is the operating system's page cache, and a page cache is
shared.

In normal operation a Kafka cluster does almost no physical disk I/O. Producers append into page
cache, consumers read data written seconds ago from page cache, and the kernel flushes in the
background. How far back that works is a simple division: page cache size divided by write rate.
For a broker with 24 GiB of cache taking 40 MB/s of writes, that is about eleven minutes.

A consumer reading data older than that misses the cache. The damage is not the disk read itself
— it is that the read pulls cold pages *into* the cache, **evicting the recent pages everyone
else depends on**. Groups that were comfortably inside the window start missing too, so they also
read from disk, which evicts more. It is a feedback loop with a cliff, and the cliff moves closer
as traffic grows.

The consequence people find counter-intuitive: a batch job replaying a topic from the beginning,
using its own consumer group, touching no production service, can degrade every producer on the
cluster. I have seen exactly that take down a checkout service — produce latency went from 8 ms
to 1.8 seconds, the producer's 32 MB buffer filled in about ten seconds, `send()` started blocking
for up to `max.block.ms`, and the web tier's request threads were all parked inside it within a
minute.

**What to do about it:**

- **Client quotas as a default**, not as a remediation. `consumer_byte_rate` and also
  `request_percentage`, which catches clients that are expensive in requests rather than bytes.
- **Alert on broker disk read throughput.** It is near zero when healthy, which makes it one of
  the few Kafka alerts that needs no threshold tuning.
- **Never let a producer block a request thread.** `max.block.ms` should be small and there should
  be a fallback path — an outbox table, or shedding — so Kafka being slow degrades the pipeline
  rather than the front door.
- For heavy replay, a separate cluster or a tiered-storage tier, so historical reads do not share
  a page cache with live traffic.

---

### Q11. A stakeholder asks for an SLO on the streaming pipeline. What do you propose?

Two SLIs, because one is not enough, and a threshold derived from a downstream deadline rather
than chosen as a round number.

**SLI 1, freshness:** the proportion of events written to the destination within N seconds of
being produced. I would build this as a histogram in the consumer — `now − record.timestamp()`
observed after each record — because Kafka does not export it and consumer lag is a poor
substitute. Lag in records is not comparable across times of day, means nothing when traffic is
zero, and cannot be stated as a user-facing objective.

**SLI 2, completeness:** the proportion of events produced that are eventually processed. This
exists because freshness structurally cannot see a dropped record — a record that is never
processed never produces a freshness observation, so a pipeline silently dropping 2% can show
perfect freshness. Completeness has to be measured by comparing counts across a window long
enough to absorb normal lag.

**Where the threshold comes from.** If the downstream is an hourly rollup that runs at ten past
the hour, an event delayed by more than a few minutes misses its window. I would set the freshness
threshold about two orders of magnitude tighter than that deadline — 30 seconds against a
several-minute constraint — so that the SLO detects problems well before they cause harm.

**Then I would check the budget against known events**, which is the step that makes this useful
rather than decorative. A 99.5% monthly target is 216 minutes of budget. If a single flash sale
produces a 49-minute recovery tail during which the pipeline is more than 30 seconds behind, then
two flash sales a month consume half the budget. That is not a reason to loosen the target — it
is the conversation the SLO exists to force, about whether the consumer needs more headroom.

A budget that is never threatened is set too loosely to influence any decision, and a budget
that is always exhausted will be ignored. The right target is one the system meets when working
as designed and misses when the things you care about go wrong.

---

### Q12. One large Kafka cluster or several smaller ones?

Start with one. Split for **blast radius and governance**, never for throughput — a single
cluster scales further than almost anyone needs.

The arguments that eventually make splitting correct, in the order they usually bite:

**One tenant can hurt everyone.** The page-cache problem from Q10 is the clearest case: one
team's backfill degrades every topic on the cluster. Quotas mitigate and do not eliminate it.

**The maintenance window is shared.** A two-hour rolling restart is two hours of elevated risk
for every team, coordinated with all of them.

**Configuration is cluster-wide.** `auto.create.topics.enable`, `num.partitions`,
`offsets.topic.num.partitions`, the broker default `min.insync.replicas` — one value for
everybody. A cluster carrying payments and clickstream must pick settings that suit both, which
means it suits neither.

**Upgrades are all-or-nothing.** One team needing a new feature moves the whole platform.

When I do split, I split **by criticality rather than by team**, because team-based splits
multiply indefinitely and criticality has about three levels: a small, conservatively-configured
critical cluster; a throughput-tuned bulk cluster for telemetry; and a platform cluster with
strict quotas and partition budgets for everyone else.

And I would state the cost, because it is real: three clusters is three upgrade cycles and three
monitoring surfaces, and — the significant one — **cross-cluster joins become impossible**. A
consumer cannot read two clusters in one transaction, and mirroring between them brings its own
failure modes. Splitting a cluster is much easier than merging two, so I would not do it until one
of those arguments is actually hurting rather than theoretically applicable.

---

### Q13. A producer throws `OutOfOrderSequenceException`. What does it mean and what do you do?

I would treat it as a **data-loss alarm, not a client error**, and that framing is the whole
answer.

With idempotence enabled — the default since Kafka 3.0 — the producer stamps each batch with a
producer id and a per-partition sequence number, and the broker tracks the last sequence it
accepted. This exception means the broker received a sequence that skips ahead of what it
expected, which means **a batch the producer believes it sent successfully is not in the broker's
log.**

The realistic causes, in order:

1. **An unclean leader election.** The new leader's log is shorter than the old one's, so its
   producer state is behind what the producer sent. This is the common case, and the exception is
   often the first symptom anyone notices.
2. **Producer state expiry** — `producer.id.expiration.ms`, one day by default — for a producer
   that went quiet and then resumed.
3. **Genuine loss** from `min.insync.replicas=1` plus a leader failure.

The instinct is to catch it and re-create the producer, which obtains a fresh producer id and
restores throughput. That works and it hides the fact that records vanished. If I do that, I log
it at error severity, emit a metric, and reconcile.

**The diagnostic:** correlate the exception timestamp against
`UncleanLeaderElectionsPerSec` and the ISR history for that partition. If an unclean election
happened, the scope of the loss is the difference in log end offsets across the election.

**Prevention** is the durability configuration from Q1, plus leaving unclean leader election off.

---

### Q14. How do you safely roll-restart an 18-broker cluster?

One broker at a time, with a gate, and the gate is the entire answer.

```bash
for i in $(seq 1 120); do
  urp=$(kafka-topics.sh --bootstrap-server $BS --describe \
        --under-replicated-partitions | grep -c "Partition:")
  [ "$urp" -eq 0 ] && exit 0
  sleep 15
done
exit 1   # abort the rollout — do not proceed on timeout
```

The reasoning: a topic with RF=3 and `min.insync.replicas=2` tolerates one missing replica. A
restarted broker becomes *reachable* within seconds but is not back **in the ISR** until it has
fetched everything it missed — which for a broker down three minutes at 40 MB/s is several
gigabytes. Automation that advances on a readiness probe restarts the next broker while the
previous one is still catching up, two replicas are then out of sync, the partition falls below
the minimum, and every `acks=all` producer is blocked. That is the most common self-inflicted
Kafka outage, and it happens during routine maintenance.

⚠️ A readiness probe that returns healthy before the broker is in the ISR is worse than no probe,
because it gives automation permission to proceed. The probe has to check ISR membership, not
port reachability.

**What it costs at 18 brokers:** controlled shutdown of roughly 580 leaderships is about 90
seconds, restart and log load about a minute, ISR catch-up a few minutes, plus the verification
gate. Call it six to seven minutes each, so around two hours — which is a maintenance window and
an approval rather than an afternoon.

**Can you go faster?** Only with a specific, verified argument. With RF=3 rack-aware across three
zones and `min.insync.replicas=2`, taking down all brokers in one zone leaves two replicas of
every partition, so writes continue — turning 18 sequential restarts into 3. But that is safe
only if replica placement is *genuinely* rack-aware, which must be checked rather than assumed,
because rack awareness is not retroactive and your oldest topics probably predate it. It also
leaves zero tolerance for an unrelated failure during the window. For most teams the honest answer
is: accept that the window grew, and plan it into the release process.

---

### Q15. Is Kafka a good system of record?

It has the properties people notice — durable, replicated, ordered, replayable — and it lacks
several that "system of record" implies, so my answer is usually no, with specific reasons.

**What it genuinely gives you:** an immutable, ordered, replicated log with a replay capability
that is very hard to get from a database. As an *integration* substrate and an audit trail, it is
excellent.

**What it does not give you:**

- **Queries.** There is no way to ask "what is the current state of order X" without replaying a
  partition or maintaining a materialised view elsewhere. Compaction helps — a compacted topic is
  a table pretending to be a log — but you still cannot query it by key without building an index.
- **Enforced retention semantics.** Data expires on a timer. "System of record" and "deleted after
  72 hours" are hard to say in the same sentence, and an infinite-retention topic is a growing
  cost with no compaction unless it is keyed.
- **Transactional reads across entities.** No multi-key consistent snapshot.
- **Corrections.** An immutable log means a mistake is fixed by appending a correction, and every
  consumer must implement the correction semantics. There is no `UPDATE`.
- **Schema enforcement.** Kafka stores bytes. Structure is enforced by a registry you have to run,
  and the default compatibility mode decides who deploys first in a way that is easy to get
  backwards.

**Where I land:** the pattern that works is a database as the system of record with Kafka as the
**replayable derivative** — the transactional outbox. The business state lives somewhere
queryable, transactional, and correctable; the event stream is generated from it atomically, so
the two cannot disagree. That arrangement also makes several otherwise-unrecoverable Kafka
failures merely inconvenient, because you can always regenerate the stream.

Event sourcing with Kafka as the durable log is a legitimate architecture, and the teams that
succeed with it treat the log as infinite-retention and compacted, and build and maintain the
materialised views deliberately. That is a much larger commitment than "we already have Kafka, so
let us keep the data there," which is how most of these conversations start.

---

### Q16. You have inherited an undocumented Kafka cluster. What do you do in the first week?

I would spend the first two days finding out what is *exposed*, not what is *broken*, because the
broken things announce themselves and the exposed things do not.

**Day 1 — the durability audit.** For every topic: replication factor, `min.insync.replicas`, and
whether replicas are spread across failure domains. Anything with RF=1 or `min.insync.replicas=1`
goes on a list immediately, because those are the topics where a single broker failure loses
data. I would also check `unclean.leader.election.enable` at both broker and topic level, and
whether `auto.create.topics.enable` is on — if it is, I would expect to find typo-topics quietly
absorbing writes with RF=1.

**Day 1 — the current state.** Under-replicated partitions, under-min-ISR partitions, offline
partitions, active controller count, offline log directories, and disk headroom per broker. A
cluster running one replica short is a stable state that generates no urgency, so it can persist
for months and nobody will have mentioned it.

**Day 2 — who uses it.** Every consumer group, its lag, its state, and when it last committed.
Groups that have been empty for a long time are either dead services or offset-expiry incidents
waiting to happen. Topics with no consumer group at all are either abandoned or typos. On any
cluster that has been running for years there are always dozens of both.

**Day 3 — the recovery story.** What is the retention on each topic, and what is the longest
consumer outage it survives? Is there anything outside Kafka that could reconstruct the data? Is
there a standby cluster, and if so, when was it last exercised? I would expect "there is a
standby" and "nobody has failed over to it" to both be true.

**Day 4 — the observability gap.** Which of the twelve alerts that matter exist? In my
experience the ones reliably missing are log-cleaner health, broker disk read throughput,
rebalance rate, and anything measuring the pipeline end to end rather than the cluster.

**Day 5 — write it down and pick three things.** A one-page document per topic: owner,
durability configuration, retention rationale, consumers, ordering contract. Then the three
highest-value changes, which in my experience are almost always: set `min.insync.replicas=2`
where it is 1, turn off topic auto-creation, and add the missing alerts.

**What I would deliberately not do in week one:** change partition counts, upgrade anything, or
reorganise topics. Those need the understanding that weeks two and three produce, and the cluster
has survived without them so far.

---

## Senior versus staff, on the same question

The difference is rarely knowledge. It is usually one of these four moves.

| Question | A senior answer | What makes it a staff answer |
|---|---|---|
| "How do you make Kafka durable?" | `acks=all`, RF=3, `min.insync.replicas=2` | Deriving *why* those numbers: `M − 1` failures survived for data, `N − M` for availability — and then noting that `M = N` is a trap, and that the producer and topic settings are owned by different teams so auditing one is the usual failure |
| "Consumer lag is growing." | Scale the consumer group | Establishing that scaling makes two of the five possible causes actively worse, and diagnosing from the *shape* of the lag distribution before touching anything |
| "Should we use exactly-once?" | Explaining Kafka transactions | Asking what the side effect is, and pointing out that transactions cover nothing outside Kafka — then proposing the outbox, and noting that operators replay topics regardless of any protocol |
| "How many partitions?" | Enough for the throughput | Deriving from consumer parallelism with headroom for growth and skew, choosing a number with many divisors, and flagging that increasing it later is a data migration |
| "What's your RPO?" | Quoting the number in the document | Stating that RPO is a *measurement* of mirror lag, that it is worst during the conditions that cause disasters, and that it is unverified without a heartbeat alert |
| "How do you monitor Kafka?" | Listing broker JMX metrics | Observing that every broker metric can be green during the three worst incidents, and building end-to-end freshness first |

The four moves, stated plainly:

1. **Derive rather than recall.** Numbers you can reconstruct are numbers you can adapt when the
   situation differs from the one you memorised.
2. **Name the trade-off before being asked.** Every Kafka setting trades durability against
   availability or latency against throughput, and saying which is the point.
3. **Question the premise.** "Add partitions to fix lag" and "we need exactly-once" are both
   usually the wrong framing, and noticing that is worth more than answering well.
4. **Distinguish loud failures from silent ones,** and deliberately engineer toward loud. A wedged
   partition pages someone; a dropped record is found at reconciliation three weeks later.

Next: [13-operating-playbook-and-golden-config.md](13-operating-playbook-and-golden-config.md).
