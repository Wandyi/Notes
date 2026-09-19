# Broker, Storage, and Cluster Failures

Twelve failure modes that originate below Kafka's data model: a machine, a disk, a JVM, or the
coordination layer. They are grouped by what you can observe, because during an incident you
start from the symptom and not from the cause.


| Class                                        | The question it answers                                     | Scenarios       |
| -------------------------------------------- | ----------------------------------------------------------- | --------------- |
| **A. The broker or its storage is gone**     | Is a replica missing entirely?                              | `B-01` … `B-05` |
| **B. The broker is running but not healthy** | It is up. Why is it hurting?                                | `B-06` … `B-09` |
| **C. Cluster coordination has failed**       | It is not one broker; it is the cluster's ability to decide | `B-10` … `B-12` |


Three commands decide the class, and they are the first three things to run on any Kafka
incident. Learn them in this order:

```bash
# 1. Are any partitions completely unavailable? (no leader at all — hard outage)
kafka-topics.sh --bootstrap-server $BS --describe --unavailable-partitions

# 2. Are any partitions below their min.insync.replicas? (producers with acks=all are blocked)
kafka-topics.sh --bootstrap-server $BS --describe --under-min-isr-partitions

# 3. Are any partitions merely under-replicated? (still serving, durability reduced)
kafka-topics.sh --bootstrap-server $BS --describe --under-replicated-partitions
```

The three are nested: every unavailable partition is also under-min-ISR, and every under-min-ISR
partition is also under-replicated. Read them from the top and stop at the first one that returns
rows, because that is the severity you are dealing with. An empty first command and a non-empty
third means you have a degraded cluster and time to think. A non-empty first command means
someone's writes are failing right now.

---



## Class A — the broker or its storage is gone



### B-01 · A broker terminated without controlled shutdown

**What you see.** A burst of produce and fetch errors lasting a few seconds, then recovery.
Client logs show `NOT_LEADER_OR_FOLLOWER` and metadata refreshes. `UnderReplicatedPartitions`
jumps to roughly the number of partitions that broker hosted and stays there.

**Mechanism.** There are two completely different shutdown paths and the difference is worth
understanding because it is the difference between a non-event and a visible blip.

With **controlled shutdown** (`controlled.shutdown.enable=true`, the default), a broker asked to
stop politely tells the controller first. The controller moves leadership for every partition
that broker leads to an in-sync follower, waits for the moves to be acknowledged, and only then
lets the broker exit. Clients discover the new leaders through the ordinary metadata-refresh path.
Done properly, a controlled shutdown of one Riverbend broker is invisible in client error rates.

With an **abrupt termination** — `kill -9`, an OOM kill, a spot-instance reclaim, a hypervisor
failure, or a Kubernetes `SIGKILL` after the grace period expired — there is no warning. The
partitions that broker led have no leader until the cluster notices it is gone. In KRaft that
detection is driven by broker heartbeats: brokers heartbeat to the controller every
`broker.heartbeat.interval.ms` (2,000 ms) and are fenced after
`broker.session.timeout.ms` (**9,000 ms**) without one. So the worst case is about nine seconds
of unavailability for the partitions that broker led, plus the time for clients to refresh
metadata and retry.

Nine seconds of failed produces is survivable *if and only if* your producers are configured to
ride it out. A producer with `delivery.timeout.ms=120000` retries transparently and you never
notice. A producer with a two-second timeout — which `checkout-api` has, because it is serving a
mobile client — returns errors to users. The broker failure was nine seconds; the customer
impact is a configuration choice made in the producer. Doc 03 (`P-04`) covers that setting.

**Confirm it.**

```bash
# Which brokers does the cluster currently believe are alive?
kafka-broker-api-versions.sh --bootstrap-server $BS 2>/dev/null | grep -c "id:"

# In KRaft, ask the controller quorum directly — this also shows fenced brokers
kafka-metadata-quorum.sh --bootstrap-server $BS describe --status
```

**Recover.** Usually nothing: bring the broker back and it re-joins, catches up from the
leaders, and re-enters the ISR. Watch `UnderReplicatedPartitions` return to zero — that is your
signal that recovery is complete, and it is the gate you must respect before touching the next
broker (`B-12`).

If the broker is not coming back, leadership has already moved and the partitions are running
with two replicas instead of three. That is a *durability* problem, not an availability one, and
you have as long as your risk tolerance allows to fix it — see `B-05`, because the fix is not
automatic.

**Prevent.** Three things, in order of how often they are missed:

1. On Kubernetes, `terminationGracePeriodSeconds` must be longer than a controlled shutdown
  takes. Controlled shutdown time scales with the number of partitions the broker leads; for
   Riverbend's 140 replicas per broker it is around 20 seconds, so a grace period of 30 seconds
   is too tight and 120 is comfortable. ⚠️ If the grace period is shorter than the shutdown, the
   kubelet sends `SIGKILL` and you have converted every routine restart into `B-04`, which costs
   you an hour instead of a minute.
2. Set `controlled.shutdown.max.retries` (default 3) and `controlled.shutdown.retry.backoff.ms`
  (default 5,000) so a transient controller hiccup does not abandon the polite path.
3. On spot or preemptible instances, do not run brokers. If you must, consume the termination
  notice and trigger a controlled shutdown from it.



### B-02 · A disk fills and log directories go offline

**What you see.** Produce requests to a subset of partitions fail with `KafkaStorageException`.
`OfflineLogDirectoryCount` goes above zero on one broker. If every log directory on the broker
is offline, the broker shuts itself down, and you are now also in `B-01`.

**Mechanism.** Kafka does not gracefully degrade when it runs out of disk. When a write to a log
directory fails, the broker marks that entire directory offline, takes every partition stored on
it out of service, and tells the controller — which elects new leaders for those partitions
elsewhere. Partitions whose *other* replicas are healthy keep working with reduced replication.
Partitions that lose their last in-sync replica become unavailable.

Recall Riverbend's steady state from doc 00: **1.55 TB used of 2 TB per broker, 78% full.** That
number is the whole story here, because four ordinary events each consume the remaining 450 GB:

- **Raising retention.** Someone changes `clickstream.events` from 24 h to 36 h to support a
backfill. That is `40.8 MB/s × 12 h × RF 2 ÷ 6 brokers` = **294 GB per broker**, arriving
gradually over the following 24 hours, which is exactly long enough for the person who made
the change to have moved on to something else.
- **A lagging consumer on a compacted topic.** Segments cannot be deleted while they are needed,
and compaction of `inventory.adjustments` cannot reclaim space if the log cleaner is stuck
(`T-08`).
- **Reassigning a failed broker's partitions.** Covered in `B-05`: absorbing one broker's 1.55 TB
across the five survivors adds **310 GB each**, taking them to 93% full.
- **A stuck or slow segment deletion.** Retention is enforced by a background thread every
`log.retention.check.interval.ms` (5 minutes). If it is blocked, data accumulates at the full
ingest rate.

⚠️ Kafka has **no disk-based admission control**. There is no setting that says "stop accepting
writes at 90% full." It writes until the write fails. Whatever protection you have must come from
monitoring and from retention arithmetic done in advance.

**Confirm it.**

```bash
# Per-broker, per-log-dir, per-partition size — the authoritative view
kafka-log-dirs.sh --bootstrap-server $BS --describe --broker-list 1,2,3,4,5,6 \
  | tail -1 | jq -r '.brokers[] | .broker as $b | .logDirs[]
      | {broker: $b, dir: .logDir,
         totalBytes: ([.partitions[].size] | add)}'
```

Then find the biggest partitions, which is almost always more useful than the biggest topics,
because disk problems are usually skew problems:

```bash
kafka-log-dirs.sh --bootstrap-server $BS --describe --broker-list 1 \
  | tail -1 | jq -r '.brokers[].logDirs[].partitions[]
      | [.partition, .size] | @tsv' | sort -k2 -rn | head -20
```

**Recover.** In descending order of preference:

1. **Reduce retention on the largest topic, temporarily.** This is the fastest lever and the only
  one that does not move data around a cluster that is already under stress:
   Space is reclaimed on the next retention check, within five minutes. Remember to put it back,
   and write down who needs to know that 12 hours of clickstream history no longer exists.
2. **Move partitions off the full broker** with a reassignment (see doc 13). Slower, and it
  consumes replication bandwidth you may not have.
3. **Grow the volume.** On gp3 this is online and takes minutes, and it is the right answer if
  the growth was legitimate rather than a mistake.

⚠️ Do **not** delete segment files by hand. The broker's in-memory index and the
`recovery-point-offset-checkpoint` file do not know you did it, and you will turn a disk-space
incident into a corrupted-log incident.

**Prevent.** Alert on projected exhaustion rather than on a static threshold, because a static
threshold gives you no warning on a fast-filling disk and wakes you up needlessly on a slow one:

```promql
predict_linear(kafka_log_size_bytes[6h], 48 * 3600) > kafka_log_dir_capacity_bytes * 0.85
```

And do the retention arithmetic from doc 00 as part of any retention change review. A topic
retention change is a capacity change, and it should be reviewed like one.

### B-03 · One log directory fails while the broker survives

**What you see.** `OfflineLogDirectoryCount = 1` on a broker that is otherwise healthy and
serving traffic normally for most of its partitions.

**Mechanism.** If you configure `log.dirs` with several directories on separate volumes — the
"just a bunch of disks" layout — Kafka isolates failures to a single directory rather than
killing the broker. This behaviour was added in Kafka 1.0. Partitions on the failed directory go
offline and are re-led elsewhere; partitions on the other directories are untouched.

⚠️ **Version trap for anyone on KRaft:** multiple log directories were *not supported* in KRaft
mode until Kafka **3.7** (KIP-858). If you migrated to KRaft on 3.3 through 3.6 with a
multi-directory configuration, you were running unsupported. Riverbend is on 3.7, so it is fine,
but check this before assuming JBOD works on any cluster you inherit.

**Confirm it.**

```bash
# Offline dirs, per broker, from JMX
# kafka.log:type=LogManager,name=OfflineLogDirectoryCount
# Prometheus: kafka_log_logmanager_offlinelogdirectorycount
kafka-log-dirs.sh --bootstrap-server $BS --describe --broker-list 4 \
  | tail -1 | jq -r '.brokers[].logDirs[] | [.logDir, .error] | @tsv'
```

A non-null `error` field naming the directory is the confirmation.

**Recover.** Replace or repair the volume, then restart the broker. The broker re-creates the
directory's partitions from the leaders. Until then you are running with reduced replication on
those partitions, so treat it as `B-05`.

**Prevent.** The honest answer for most teams is: **do not use JBOD, use one volume per broker
and make it a network volume with its own redundancy.** JBOD's benefit is cost and throughput on
bare metal with local NVMe. Its cost is a much more complicated failure model — per-directory
reassignment, uneven fill across directories, and an operational path most teams practise once.
On EBS or its equivalents, a single large volume is simpler and the underlying storage is already
replicated.

### B-04 · Unclean shutdown, then a very long log recovery

**What you see.** A broker restarts and does not come back for tens of minutes. Its logs show
`Recovering unflushed segment` repeated thousands of times, and `Loading logs` with no progress
indication. Meanwhile the cluster is running one replica short and nobody can tell you when that
ends.

**Mechanism.** Kafka does not `fsync` on every write. It appends into page cache and lets the
operating system flush in the background, which is the main reason it is fast
(`log.flush.interval.messages` defaults to effectively unbounded). The consequence is that after
an abrupt termination, the broker cannot trust anything written since the last recorded
**recovery point**, so on startup it re-reads every segment past that point, validates record
batches, and rebuilds the offset and timestamp indexes.

The cost scales with bytes to be validated and is bounded by disk read throughput and by
`num.recovery.threads.per.data.dir`, which **defaults to 1**. For a Riverbend broker holding
1.55 TB, the worst case is reading all of it single-threaded:

```
1.55 TB = 1,587,200 MB
gp3 at default 125 MB/s throughput  → 1,587,200 ÷ 125 = 12,698 s ≈ 3.5 hours
gp3 provisioned to 500 MB/s         → 1,587,200 ÷ 500 =  3,174 s ≈ 53 minutes
```

In practice recovery reads far less than the whole disk, because the recovery point advances as
the OS flushes — but "far less" is not a number you can plan with, and observed recovery times of
20 to 60 minutes on multi-terabyte brokers are completely ordinary. Plan for the pessimistic
figure and be pleased when it is faster.

**Confirm it.** The broker's own log is the only real source:

```bash
grep -E "Recovering|Loading logs|Logs loading complete" /var/log/kafka/server.log | tail -20
# "Logs loading complete in NNNNN ms" is the finish line
```

JMX exposes `kafka.server:type=KafkaServer,name=BrokerState` — in KRaft, `3` means RUNNING. A
broker stuck at `2` (STARTING) is recovering.

**Recover.** Wait. There is no safe way to shorten a recovery in progress. Resist the urge to
restart it again, which discards the progress made and starts over.

**Prevent.**

- `num.recovery.threads.per.data.dir=8` or so. It costs nothing at steady state and divides
recovery time until you hit the volume's throughput ceiling.
- Make controlled shutdown actually happen — see `B-01`'s grace-period point, which is the most
common cause of accidental unclean shutdowns in containerised deployments.
- Provision volume throughput with recovery in mind, not just steady-state writes. Riverbend
writes 40 MB/s at peak, so 125 MB/s looks generous; it is the recovery case that argues for
500 MB/s, and it is the case nobody sizes for.



### B-05 · A broker died and Kafka did not heal itself

**What you see.** `UnderReplicatedPartitions` has been sitting at 140 for two days. Everything
works. Nobody is paged. The cluster is one failure away from data loss and it looks completely
stable.

**Mechanism.** This is the most important thing to understand about Kafka's failure model, and it
contradicts the intuition people bring from other distributed data stores.

**Kafka does not re-replicate to restore replication factor when a broker is lost.** There is no
background repair. A partition configured with RF=3 that loses a replica stays at two replicas
indefinitely, permanently, until a human or an external tool moves it. Cassandra, Elasticsearch,
and Ceph all self-heal; Kafka does not. The reasoning is defensible — automatically copying
terabytes around a cluster that just lost a node is itself a way to cause an outage — but the
consequence is that a partially-failed Kafka cluster is a stable state, and stable states do not
generate urgency.

For `orders.created` with RF=3 and `min.insync.replicas=2`, running on two replicas means the
next single broker failure takes the partition below min ISR and **stops producer writes**
(doc 02, `R-02`). You have gone from tolerating two failures to tolerating zero, and the only
outward sign is a gauge nobody looks at.

**Confirm it.**

```bash
kafka-topics.sh --bootstrap-server $BS --describe --under-replicated-partitions
# For each row, compare Replicas: with Isr: — the difference is what is missing
```

**Recover.** If the broker is coming back, bring it back and let it catch up; that is cheapest
by a wide margin. If it is not coming back, replace it with a broker **carrying the same**
`node.id` and let it re-replicate, which avoids any reassignment at all.

If you must reassign to the surviving brokers, generate and apply a plan — and throttle it, or
the replication traffic will saturate the same brokers that are already carrying the extra load:

```bash
kafka-reassign-partitions.sh --bootstrap-server $BS \
  --topics-to-move-json-file topics.json --broker-list "1,2,3,5,6" --generate > plan.json
kafka-reassign-partitions.sh --bootstrap-server $BS \
  --reassignment-json-file plan.json --execute --throttle 50000000    # 50 MB/s
kafka-reassign-partitions.sh --bootstrap-server $BS \
  --reassignment-json-file plan.json --verify                          # removes the throttle
```

⚠️ Before doing this on Riverbend, check the disk arithmetic from `B-02`: absorbing 1.55 TB
across five brokers puts them at **93% full**, which is likely to turn a durability problem into
an availability problem. Reducing `clickstream.events` retention first is usually the correct
first step.

**Prevent.** Alert on `UnderReplicatedPartitions > 0` sustained for 15 minutes, and treat it as a
ticket rather than a page — but a ticket with a deadline, because its cost is invisible until it
is catastrophic. Teams operating more than a handful of brokers should run **Cruise Control**,
which does self-healing and rebalancing as an explicit, rate-limited, observable process. The
value is not that it moves partitions; it is that it converts "a human remembers to do this" into
a system property.

**What Cruise Control actually does.** It is a separate service, originally built at LinkedIn,
that sits beside the cluster and closes the loop this section describes manually:

1. **Monitors.** A metrics reporter runs inside every broker and publishes CPU, disk, and
  network load, plus per-partition size and leader/replica counts, to a dedicated internal
   topic. Cruise Control consumes that topic and builds a cluster-wide load model.
2. **Detects.** An anomaly detector watches the model for broker failure, goal violations (for
  example, disk usage skewed past a threshold), and slow brokers — the `B-06` pattern, found
   automatically instead of by a human noticing ISR churn.
3. **Proposes.** Given a prioritized list of **goals** — rack awareness, disk-usage balance,
  network-capacity balance, leader-replica balance — it computes a plan of partition moves and
   leadership changes that satisfies the hard goals and does its best on the soft ones. This is
   the same kind of plan `kafka-reassign-partitions.sh --generate` produces in `B-05`'s recovery
   steps, but continuously and goal-driven rather than hand-run once.
4. **Executes, throttled.** It applies the plan incrementally under a configured bandwidth cap,
  the same throttle used by hand in `B-05`'s example, so rebalancing does not itself become an
   incident on a cluster that is already short a broker.

**Where it changes this section's playbook.** With Cruise Control running in self-healing mode,
the scenario this section opens with — `UnderReplicatedPartitions` stuck at 140 for two days,
nobody paged — does not reach that stable, silent state. The anomaly detector sees the broker
loss and either raises an alert with a ready-to-apply plan, or executes the rebalance itself
depending on configuration. It does not remove the need to reason about the disk arithmetic in
`B-02` before absorbing a dead broker's partitions — the tool executes the move, it does not
change how much room the survivors have.

---



## Class B — the broker is running but not healthy



### B-06 · A slow disk shows up as ISR churn, not as a disk alert

**What you see.** `UnderReplicatedPartitions` oscillating between zero and a few dozen, every few
minutes, on a cluster where no broker has restarted. Produce p99 latency has doubled. Disk
utilisation looks unremarkable.

**Mechanism.** Followers fall behind because the *leader* cannot serve their fetches promptly, or
because the follower cannot append what it fetched. Either way, a follower that has not caught up
within `replica.lag.time.max.ms` (30 s) is removed from the ISR, then re-added once it catches up,
then removed again. The oscillation is the symptom; the slow storage is the cause.

The diagnostic that separates the possibilities is the **breakdown of produce request latency**,
which Kafka exposes as separate JMX attributes on
`kafka.network:type=RequestMetrics,request=Produce`:


| Attribute             | What it measures                                       | High value means                                  |
| --------------------- | ------------------------------------------------------ | ------------------------------------------------- |
| `RequestQueueTimeMs`  | Waiting for a request-handler thread                   | Not enough `num.io.threads`, or handlers blocked  |
| `LocalTimeMs`         | Leader appending to its own log                        | **Local disk or page-cache pressure**             |
| `RemoteTimeMs`        | Waiting for followers to acknowledge (`acks=all` only) | **Replication is the bottleneck, not local disk** |
| `ResponseQueueTimeMs` | Waiting for a network thread to send the response      | Not enough `num.network.threads`                  |
| `ResponseSendTimeMs`  | Writing the response to the socket                     | Network saturation or a slow client               |


This table is the highest-value diagnostic in Kafka operations, because "produce is slow" has
five distinguishable causes and this tells you which one in about ten seconds. ⚠️ Note that
`RemoteTimeMs` is only meaningful for `acks=all`; with `acks=1` the leader responds immediately
and replication slowness shows up as ISR churn with no latency signal at all.

**Confirm it.**

```promql
# Which stage of the produce path is slow?
kafka_network_requestmetrics_localtimems{request="Produce",quantile="0.99"}
kafka_network_requestmetrics_remotetimems{request="Produce",quantile="0.99"}

# How far behind is the furthest-behind follower, in records?
kafka_server_replicafetchermanager_maxlag{clientId="Replica"}
```

On AWS, also check whether you are on gp2 with exhausted burst credits, or gp3 whose provisioned
throughput is below what peak actually needs — Riverbend peaks at 40.5 MB/s of writes per broker
plus consumer reads, and a 125 MB/s volume has less headroom than it appears once page-cache
misses start adding reads.

**Recover.** Raise volume throughput (online on gp3), or move the hottest partitions off the
affected broker. If the cause is a single hot partition, doc 05 (`D-06`) covers key skew, which is
the usual reason one broker is working harder than its peers.

**Prevent.** Alert on `LocalTimeMs` p99, not on disk utilisation. Utilisation is a poor proxy
here: a volume can be at 30% utilisation and still be at its throughput limit, and doc 10 explains
why saturation and utilisation diverge for storage in particular.

### B-07 · A garbage-collection pause long enough to be declared dead

**What you see.** A broker is marked as failed and fenced, leader elections happen, and then the
broker reappears seconds later insisting it is fine. Repeatedly. Nothing in the broker's log
explains the gap, because the broker was not running during it.

**Mechanism.** A stop-the-world garbage collection pause — meaning a pause during which every
application thread in the JVM is halted — stops the broker's heartbeats. If the pause exceeds
`broker.session.timeout.ms` (9,000 ms), the controller fences the broker exactly as if it had
died, and the churn that follows is real even though the broker was never actually broken.

The usual cause is a heap that is too *large*, which surprises people. Kafka deliberately keeps
very little data on the JVM heap; its working set lives in the operating system's page cache,
outside the JVM entirely. A 6 GiB heap is generous for a Riverbend broker. Configuring 24 GiB
does not make Kafka faster — it makes each garbage collection cover four times as much memory,
and it steals 18 GiB from the page cache, which is where the actual performance comes from. You
pay twice.

**Confirm it.**

```promql
# Pause time as a fraction of wall clock — anything approaching 0.01 is worth attention
rate(jvm_gc_pause_seconds_sum{gc="G1 Young Generation"}[5m])
max_over_time(jvm_gc_pause_seconds_max[5m])
```

Correlate the pause timestamps against the fencing events in the controller log. If they line up,
you have it.

**Recover and prevent.** Heap of 6 GiB with G1 (the default collector for Kafka's supported JVMs),
`-XX:MaxGCPauseMillis=20`, and leave the rest of memory to the page cache. If a broker genuinely
needs more heap, the usual real cause is an extreme partition count on that broker — each
partition carries in-memory index and metadata structures — which is a `S-01` problem to be solved
by reducing partitions rather than by growing the heap.

### B-08 · File descriptor exhaustion

**What you see.** `Too many open files` in the broker log, followed by log directories going
offline or the broker dying. Often shortly after a topic was created or partitions were added.

**Mechanism.** A broker holds open file descriptors for every segment of every partition it
hosts — the `.log`, `.index`, and `.timeindex` files, so three per segment — plus one per client
connection, plus inter-broker connections. For a Riverbend broker:

```
1.55 TB ÷ 140 partition-replicas   ≈ 11 GB per partition
11 GB ÷ 1 GB segment size          ≈ 11 segments per partition
140 partitions × 11 segments × 3   ≈ 4,620 descriptors for segment files
+ producer, consumer, and inter-broker connections
```

That is comfortably under a properly configured limit and hopelessly over the Linux default of
1,024. It also scales with partition count, which means it is a problem that arrives when
someone else creates a topic on your cluster.

**Confirm it.**

```bash
KPID=$(pgrep -f kafka.Kafka)
ls /proc/$KPID/fd | wc -l                         # current
cat /proc/$KPID/limits | grep "open files"         # ceiling
```

**Prevent.** Set the broker's `nofile` limit to 100,000 or more and alert at 80% of it. Include
the descriptor cost in any partition-count review — doc 08 (`S-01`) treats partition count as a
resource with several independent limits, and this is one of them.

### B-09 · The partitioned broker that cannot do damage, and why that is by design

**What you see.** A broker loses connectivity to the controller quorum but is still reachable
from some clients. You expect a split brain — two brokers both believing they lead the same
partition, both accepting writes, with divergent logs to reconcile afterwards.

**Mechanism.** It does not happen, and it is worth knowing precisely why, because "can Kafka
split-brain?" is a standard design-review question and the answer is a specific mechanism rather
than a reassurance.

Two fences operate independently:

1. **Broker fencing.** A broker that cannot heartbeat to the controller quorum within
  `broker.session.timeout.ms` is fenced by the controller *and knows it is fenced*, because
   fencing is a state in the metadata log it is failing to receive. A fenced broker stops serving
   produce and fetch requests for partitions it no longer leads.
2. **Leader epoch validation.** Every produce and fetch request carries the leader epoch the
  client believes is current. A broker whose epoch is stale rejects the request with
   `FENCED_LEADER_EPOCH` or `NOT_LEADER_OR_FOLLOWER`, and the client refreshes metadata. So even a
   broker that has not yet noticed its own fencing cannot accept a write, because the *epoch* in
   the request will not match what a newly elected leader has established.

The result is that a network partition produces *unavailability* on the isolated side, never
divergence. This is Kafka choosing consistency over availability for the partition, which is the
correct trade for a log that other systems treat as a source of truth.

⚠️ The one configuration that breaks this guarantee is `unclean.leader.election.enable=true`,
which explicitly permits an out-of-sync replica to become leader and therefore explicitly permits
acknowledged records to disappear. It is covered in doc 02 (`R-06`) because it belongs to the
durability discussion, not the network one.

**Confirm it.** Count leaders per partition across brokers; it is always exactly one. If you want
to see the fencing in action, `kafka-metadata-quorum.sh --bootstrap-server $BS describe --status`
reports which brokers are fenced.

---



## Class C — cluster coordination has failed



### B-10 · Controller quorum loss on KRaft

**What you see.** Existing traffic continues to flow normally. Topic creation hangs. A broker
that fails is never replaced as leader, so its partitions go offline and *stay* offline.
`ActiveControllerCount` summed across the cluster is 0 instead of 1.

**Mechanism.** KRaft controllers form a Raft quorum — three at Riverbend, tolerating one failure.
Lose two and there is no majority, so no new metadata records can be committed. Existing leaders
keep serving from metadata they already have, which is why the data plane looks healthy.

What freezes is every *decision*: leader elections, ISR shrink and expand, topic and partition
creation, broker registration, and offset-topic partition creation. The cluster is in a state
where it works until it needs to change, and then it does not.

⚠️ This makes controller quorum loss a uniquely deceptive failure. Your dashboards are green.
Your throughput is normal. And your cluster has zero fault tolerance, because the mechanism that
responds to faults is the thing that is broken. The next broker failure becomes a partition
outage with no recovery path.

**Confirm it.**

```bash
kafka-metadata-quorum.sh --bootstrap-server $BS describe --status
# LeaderId -1, or CurrentVoters showing fewer than a majority reachable
kafka-metadata-quorum.sh --bootstrap-server $BS describe --replication
# Per-voter LogEndOffset and Lag — a voter far behind is not contributing to quorum
```

```promql
sum(kafka_controller_kafkacontroller_activecontrollercount) != 1
```

**Recover.** Restore controller nodes until a majority is back. Controllers hold only the metadata
log, which is small — Riverbend's `__cluster_metadata` is a few hundred megabytes — so they start
quickly. Do not attempt to reconfigure the voter set to "work around" the outage while a majority
is down; unsafe quorum changes are how a recoverable outage becomes a rebuild from backup.

**Prevent.** Run controllers on **dedicated nodes** (Riverbend does), spread across three
availability zones, with `ActiveControllerCount != 1` alerting at page severity. Use five
controllers rather than three when the cluster is large enough that a rebuild would be a
multi-day event — five tolerates two simultaneous failures, which is what you want if controller
nodes share a failure domain with anything else.

### B-11 · Replicas of the same partition in the same availability zone

**What you see.** An availability-zone impairment takes far more partitions below min ISR than
your capacity model predicted, or takes some offline entirely.

**Mechanism.** Kafka spreads replicas across `broker.rack` values when the rack is set *at the
time the partition is created*. Three failure paths lead to replicas sharing a zone:

1. `broker.rack` was never set, so Kafka had no zone information and distributed replicas
  round-robin by broker id. Two of three replicas landing in one zone is then a matter of luck.
2. `broker.rack` was set later. Existing partitions keep the placement they were created with —
  **rack awareness is not retroactive.** New topics are safe, old topics are not, and the old
   ones are the important ones.
3. A manual reassignment specified brokers explicitly and did not respect zones. This is the
  most common cause, because reassignment plans are written under time pressure.

For Riverbend, `clickstream.events` deserves separate thought: at RF=2 across three zones, losing
one zone leaves *some* partitions with a single replica. With `min.insync.replicas=1` writes
continue, so availability is preserved and durability is momentarily zero for those partitions.
That is a defensible choice for clickstream data and an indefensible one for orders, which is
exactly why they are configured differently.

**Confirm it.** Compare each partition's replica list against the brokers' racks:

```bash
# Broker → rack
kafka-configs.sh --bootstrap-server $BS --describe --entity-type brokers --entity-default \
  2>/dev/null
kafka-metadata-quorum.sh --bootstrap-server $BS describe --status >/dev/null  # connectivity check

# Then, for a topic, list replicas and check for duplicate racks per partition
kafka-topics.sh --bootstrap-server $BS --describe --topic orders.created \
  | awk '/Partition:/ {print $2, $6, $8}'
```

Map broker ids to zones from your inventory and look for any partition whose replica set contains
the same zone twice. Worth doing as a scheduled audit rather than during an incident.

**Recover.** Reassign the affected partitions with a rack-aware plan. `kafka-reassign-partitions.sh --generate` respects `broker.rack`, which makes it a better starting point than a hand-written plan.

**Prevent.** Set `broker.rack` on every broker from the beginning, audit placement on a schedule,
and require that any hand-written reassignment plan be checked for zone spread before it is
applied. Doc 13 includes that check as a step.

### B-12 · A rolling restart faster than replication can keep up

**What you see.** Midway through a routine rolling restart, producers to `orders.created` start
failing with `NotEnoughReplicasException`. No broker is down other than the one you intended.

**Mechanism.** This is the most common self-inflicted Kafka outage, and the arithmetic is simple
enough to be worth internalising.

`orders.created` has RF=3 and `min.insync.replicas=2`, so it tolerates one missing replica and
not two. You restart broker 1; its partitions drop to two in-sync replicas, which is still fine.
Broker 1 comes back and is *reachable* within seconds, but it is not back **in the ISR** until it
has fetched everything it missed. For a broker that was down two minutes at Riverbend's peak
write rate, that is `40.5 MB/s × 120 s ≈ 4.9 GB` to fetch before it is caught up, plus whatever
new traffic arrives while it catches up.

If your automation moves on when the broker's readiness probe passes — a TCP check, or a
successful API version request — it restarts broker 2 while broker 1 is still catching up. Now two
replicas are out of sync, the ISR is down to one, the partition is below `min.insync.replicas=2`,
and every `acks=all` producer is blocked. You have caused a write outage on your most important
topic with a routine operation, and the cluster did exactly what you told it to.

**Confirm it.** During the restart, the signal is unambiguous:

```bash
kafka-topics.sh --bootstrap-server $BS --describe --under-min-isr-partitions
```

**Recover.** Stop the rollout. Do not restart the next broker. Wait for
`UnderReplicatedPartitions` to reach zero; writes resume as soon as the ISR is back to two.

**Prevent.** The rule is one sentence and it should be encoded in automation rather than in a
runbook: **wait for** `UnderReplicatedPartitions == 0` **between brokers, with no timeout that
proceeds anyway.**

```bash
# The gate, as a loop. Fail the rollout rather than continuing past it.
for i in $(seq 1 120); do
  urp=$(kafka-topics.sh --bootstrap-server $BS --describe --under-replicated-partitions \
        | grep -c "Partition:")
  [ "$urp" -eq 0 ] && echo "ISR restored, proceeding" && exit 0
  sleep 15
done
echo "ISR not restored after 30 minutes — aborting rollout" && exit 1
```

For Riverbend's six brokers at roughly three minutes each including catch-up, a full rolling
restart is about **18 minutes**. Doc 08 (`S-05`) works out what the same procedure costs at 18
brokers, and the answer is the reason large clusters end up with a change-management process.

⚠️ A readiness probe that returns healthy before the broker is in the ISR is worse than no probe,
because it gives automation permission to proceed. ++***If you run Kafka on Kubernetes, the readiness
probe must check ISR membership, not port reachability***++. The Strimzi operator does this correctly;
hand-rolled StatefulSets frequently do not.

---



## What to take away

1. **Three commands, in order: unavailable, under-min-ISR, under-replicated.** They are nested,
  and the first one that returns rows is your severity.
2. **Controlled shutdown is a non-event; abrupt termination costs about nine seconds** of
  unavailability for the partitions that broker led — and an hour of log recovery afterwards.
   On Kubernetes, the grace period is what decides which one you get.
3. **Kafka does not self-heal replication.** A cluster running one replica short is a stable
  state that generates no urgency and tolerates zero further failures. This is the single
   biggest difference from other distributed stores.
4. **Kafka has no disk admission control.** It writes until the write fails. Riverbend at 78%
  full has room for exactly one of: a retention increase, a broker replacement, or a compaction
   backlog.
5. **Break produce latency down by stage.** `LocalTimeMs` means local storage, `RemoteTimeMs`
  means replication, `RequestQueueTimeMs` means thread starvation. One JMX bean distinguishes
   five different incidents.
6. **A large heap makes Kafka slower, not faster.** Six GiB and leave the rest to page cache; a
  long garbage-collection pause gets the broker fenced as if it had died.
7. **Kafka cannot split-brain** while `unclean.leader.election.enable=false`, and the mechanism is
  broker fencing plus leader-epoch validation — not luck and not timing.
8. **Controller quorum loss is the deceptive one.** Traffic flows, dashboards are green, and the
  cluster has lost the ability to respond to any subsequent failure.
9. **Rack awareness is not retroactive.** Setting `broker.rack` protects topics created
  afterwards and does nothing for the ones you already have.
10. **Gate every rolling restart on** `UnderReplicatedPartitions == 0`**,** and make the gate abort
  rather than time out and proceed.

Next: [02-replication-isr-and-durability.md](02-replication-isr-and-durability.md), which takes
the storage-and-broker failures above and asks the question this doc deferred — under which
configurations does any of this actually lose data?