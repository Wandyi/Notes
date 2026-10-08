# The Buffering Tier

Stage 4. Everything upstream has been trying to reach this point, and everything downstream
depends on it being here. This doc argues for why the buffer exists, derives its sizing,
works through the topic and partitioning decisions that are specific to logs, and compares the
technologies that can play the role. Scenario IDs are `B-nn`.

This doc deliberately does **not** re-derive Kafka's internals. Broker storage, replication and
ISR, producer and consumer mechanics, delivery semantics, and rebalance behaviour are covered in
depth in [`../Kafka`](../Kafka/README.md), and that collection uses the same Riverbend system, so
the two agree. What is here is the part that is specific to *logs*: why log workloads stress a
broker differently from event workloads, and the decisions that follow from that.

---

## What the buffer is actually for

The buffer is the most commonly questioned component in a log pipeline and the one whose absence
causes the most damage. The objection is reasonable: it is an extra tier, it costs money, it adds
latency, and the data is already going to durable storage a moment later. Why not send from the
gateway to storage directly?

Because the buffer provides four things that are each individually hard and together are the
difference between a pipeline and a liability.

### 1. It decouples the rate of production from the rate of consumption

Production rate is set by your applications. Consumption rate is set by your indexer, your
archiver, and whatever else reads. **These two numbers have no reason to be equal at any given
instant, and both fluctuate independently.**

Without a buffer, every consumer-side hiccup becomes a producer-side problem within seconds. Your
indexer does a rolling restart — a 90-second operation — and with no buffer that is 90 seconds
where the gateway has nowhere to put 250 GB:

```
90 s × 2.78 GB/s = 250 GB with nowhere to go
gateway memory across 90 pods at 8 GB = 720 GB total, most of it in use
→ the gateway must either drop or push back to 12,000 agents
```

Pushing back is the better of the two, and it means the agents' 15-second memory buffers (doc 02)
fill, and then their disk buffers, and now a routine restart of one downstream component has
propagated to every machine in the fleet. **The buffer converts "an entire tier is unavailable"
into "a consumer's lag increased".**

### 2. It makes the same data available to several independent consumers

Three flows read everything, plus a handful of specialised consumers:

```
indexer        → hot searchable tier
archiver       → Parquet in object storage
tail router    → live tail
metrics deriver→ counters extracted from logs (doc 07)
security       → a SIEM copy, owned by another team
```

Without a buffer, the gateway must fan out to five destinations, hold state for each, and
implement per-destination retry and buffering. Now the gateway *is* the buffer, badly, and — the
part that actually bites — **the SIEM being slow affects your indexer**, because they share the
gateway's memory and worker threads.

With a buffer, each is a consumer group with its own offset, own pace, and own failures. The
security team's consumer can be down for three hours and nothing else notices.

### 3. It allows replay, which is what makes the pipeline correctable

This is the property that changes how you operate. A bad parsing rule, a storage bug, a schema
mistake, a mis-configured sampling rule — with a buffer holding six hours of raw records, all of
these are fixed by resetting an offset and reprocessing. Without it they are permanent holes.

Docs 03 and 04 both concluded with the same rule: **keep irreversible operations downstream of
the durable buffer.** That rule is only available to you if the buffer exists.

### 4. It absorbs the difference between peak and provisioned capacity

Downstream tiers are sized for something. Traffic occasionally exceeds it. The buffer converts
"we are over capacity" into "we are behind", which is a fundamentally better failure: it
degrades freshness rather than losing data, it is visible as a number that trends, and it
recovers on its own once the spike passes.

Doc 01's incident-spike figure — 31× on one service — is unprovisionable. The buffer is how you
survive it without provisioning for it.

### When you genuinely do not need one

If you have one consumer, one destination, no replay requirement, and the destination is itself
durable and elastic — sending directly to a managed service, say — then the buffer is a tier of
complexity buying you very little. Under a terabyte a day with a single SaaS destination, skip
it. The agents' disk buffers are your buffer, and that is a reasonable design.

---

## What the buffer must provide

Before choosing a technology, name the requirements. Log workloads want:

| Requirement | Why | How much |
|---|---|---|
| Durable, replicated writes | A lost batch here is lost forever | Survive one AZ failing |
| High sequential write throughput | 0.77 GB/s of unique data after gateway compression | With headroom for 1.7× peak |
| Cheap fan-out reads | 4–6 consumer groups read everything | Reads must not cost what writes cost |
| Replay from an arbitrary offset | Reprocessing is the correction mechanism | Back to the retention horizon |
| Time-bounded retention | Transit, not storage | 6 hours |
| Ordering within a stream | For multiline and sequence reconstruction | Per partition is enough |
| Backpressure that is visible | Lag is the pipeline's primary health signal | Measurable per consumer group |

Two things logs notably do **not** need, and pretending otherwise costs real money:

**Exactly-once semantics.** Logs are idempotent-ish and duplicates are tolerable if they are
detectable. Kafka transactions cost throughput and add a whole class of failure (hanging
transactions blocking `read_committed` consumers — see
[`../Kafka/05-delivery-semantics-and-ordering.md`](../Kafka/05-delivery-semantics-and-ordering.md)).
Use at-least-once with a deterministic record identity and deduplicate where it matters.

**Strict global ordering.** Nobody needs records from two different services in emission order
across the whole platform. They need a single container's output in order, which is per-partition
ordering with the right key. Demanding more constrains partitioning for no benefit.

---

## The topic decision

This is the design choice with the widest consequences, and the answer that seems obvious is
wrong at scale.

### Option A: one topic per service

The reference note this collection extends
([`../../system-design-notes/logProcessingAndAggregation.md`](../../../system-design-notes/logProcessingAndAggregation.md))
proposes this, and the reasoning given is good: a live-tail consumer interested in one service
reads only that service's topic, rather than reading 2.78 GB/s to filter for 44 MB/s.

At 200 services it works well. At Lantern's scale it does not, and the reason is metadata rather
than throughput.

```
40 business units × ~200 services   = 8,000 topics
partitions per topic: 4 minimum for parallelism and ordering
                                    = 32,000 partitions
× replication factor 3              = 96,000 partition replicas
÷ 24 brokers                        = 4,000 partition replicas per broker
```

Four thousand partition replicas per broker is well past comfortable. The consequences, each
covered in [`../Kafka/08-how-kafka-breaks-at-scale.md`](../Kafka/08-how-kafka-breaks-at-scale.md):

- **Every partition is an open file handle and a memory-mapped index.** At 4,000 per broker the
  page cache is competing with index pages for the same memory, which directly attacks the
  16-minute residency figure that doc 01 established as the governing number.
- **Controller failover time scales with total partitions.** Metadata for 96,000 replicas must be
  propagated; even with KRaft this is minutes, and during it leadership is unstable.
- **Writes become random rather than sequential.** Kafka's throughput comes from appending to few
  files. 4,000 active segments per broker on NVMe is survivable and on anything else is not.
- **Partition count per topic cannot be reduced.** You can add partitions; you cannot remove them.
  A per-service topic scheme accumulates partitions for every service that ever existed.

And there is an operational cost that is worse than any of these: **topic creation becomes part
of your deployment path.** A new service produces no logs until its topic exists. Auto-creation
solves it and introduces a way for a typo to create a topic with default settings that nobody
notices for a year.

### Option B: one topic, partitioned by something

```
1 topic × 2,400 partitions × RF 3 = 7,200 replicas ÷ 24 brokers = 300 per broker
```

Comfortable. But now every consumer reads everything. The live-tail consumer for `payment-api`
reads 2.78 GB/s to find 44 MB/s — a 63× read amplification, and doc 12's latency budget cannot
absorb it.

### Option C: a small number of topics by *class*, keyed by service

This is the answer, and the insight behind it is that **the useful grouping is not "which
service" but "which consumer cares, and with what urgency".**

```
logs.app          — application logs, 21% of bytes  →  580 MB/s
logs.access       — edge, mesh, CDN,  45% of bytes  →  1.25 GB/s
logs.client       — mobile and web SDK, 18%         →  500 MB/s
logs.infra        — kubelet, host, network, 5%      →  139 MB/s
logs.batch        — Spark and ETL, 11%              →  306 MB/s
logs.audit        — security-relevant, <0.1%        →  2 MB/s
logs.dlq          — unparseable, for reprocessing   →  variable
```

Seven topics. Partition counts sized to each topic's throughput, targeting 20–30 MB/s per
partition (comfortable for a single consumer thread with parsing work to do):

```
logs.app     580 MB/s ÷ 25 = 24 →  48 partitions  (2× headroom for peak and growth)
logs.access  1,250    ÷ 25 = 50 → 120 partitions
logs.client  500      ÷ 25 = 20 →  48 partitions
logs.infra   139      ÷ 25 =  6 →  24 partitions
logs.batch   306      ÷ 25 = 12 →  36 partitions
logs.audit   2        ÷ 25 <  1 →  12 partitions  (for availability, not throughput)
logs.dlq                        →  12 partitions
                                  ---------------
                                   300 partitions
× RF 3                          =  900 replicas
÷ 24 brokers                    =   38 per broker      ← very comfortable
```

**Why this grouping and not another.** Each topic has a different consumer set, a different
retention need, a different quality-of-service level, and a different cost profile:

- `logs.audit` must never be dropped and needs its own retention and access control. Separating
  it means a volume incident in `logs.access` cannot touch it — the strongest isolation argument
  in the design.
- `logs.access` is 45% of bytes, is read by the archiver and almost nothing else, and is the
  first thing to shed under pressure. Isolating it means shedding it does not require
  discriminating inside a shared topic.
- `logs.app` is what live tail and interactive search actually read. Keeping it separate means
  the tail router consumes 580 MB/s rather than 2.78 GB/s — a **4.8× reduction** that recovers
  most of the per-service-topic benefit without any of its cost.
- `logs.batch` is bursty and nobody tails it, so it can have a lower QoS.

The live-tail amplification is now 580 MB/s to find 44 MB/s — 13×, not 63×. Doc 12 shows that is
affordable and shows the further trick (a filtered secondary topic for actively tailed services)
that closes the remaining gap when needed.

### Partitioning key

Within a topic, which key?

**Not random or round-robin**, despite it giving perfect balance — it destroys per-container
ordering, so multiline records that were split across batches can be reassembled in the wrong
order, and sequence-gap detection (doc 15) becomes impossible.

**Not `tenant.id`** — 40 tenants cannot spread across 120 partitions, and the largest tenant
would get one partition carrying a disproportionate share. This is exactly the skew problem
described in [`../DataPlatform.md`](../DataPlatform.md), where one financial tenant at 18% of
traffic lands on a single partition.

**Not `service.name`** — better, but services are also skewed. The edge access-log service is one
`service.name` producing 31% of all bytes. One partition would receive 388 MB/s while its
neighbours receive 3 MB/s. `B-02`.

**The key is `hash(k8s.pod.uid)`**, or more generally the stream identity: the finest-grained
unit that must stay ordered.

```
140,000 pods hashed across 120 partitions = ~1,167 pods per partition
```

With a thousand-odd independent producers per partition, the law of large numbers does the work
and the skew is small. The busiest pod at 40 MB/s still lands entirely on one partition, so
check that a single partition can carry it:

```
one partition at 25 MB/s target, one pod at 40 MB/s → that pod alone exceeds the target
```

**So the extreme case still breaks it**, and you need one more rule: a **per-pod rate ceiling**
above which the stream is split with a synthetic suffix (`pod.uid#1`, `pod.uid#2`), sacrificing
ordering for that pod deliberately and visibly. Any pod emitting 40 MB/s has already given up on
ordered reading of its own output; making that explicit is better than letting it degrade a
partition everyone else shares.

### Retention

Six hours. The derivation:

```
what retention has to cover:
  a full weekend outage of a consumer          48 h   ← rejected, see below
  a long incident plus recovery                 4 h
  time to notice and start a reprocess          1 h
  margin                                        1 h
                                              ------
                                                6 h
```

Why not 48 hours, which would be genuinely comfortable?

```
0.77 GB/s × 172,800 s × 3 replicas = 399 TB
÷ 12 TB usable per broker          = 34 brokers  (vs 24)
```

Ten extra `i3en.6xlarge` at roughly $1,600/month each is **$16,000 a month** to hold data
that is already in object storage within two minutes. **Kafka is transit, not storage**, and the
reason is that its cost per stored TB is roughly 40× S3's. Doc 09's archiver is what makes six
hours safe: by the time you need the eighth hour, the data is in S3 and reprocessing reads from
there instead.

The exception is `logs.audit`, which is tiny (2 MB/s) and which gets 7 days because the marginal
cost is $0 and the value of never losing it is high.

---

## Sizing, and what actually binds

Doc 01 produced the answer; here is the reasoning in full, because the binding constraint is not
the one people expect.

### The four candidate constraints

```
Input: 1.26 GB/s on the wire (LZ4 from agents), recompressed zstd-1 at the gateway → 0.77 GB/s stored
```

Which is it? This matters, so be explicit. The gateway recompresses with zstd-1 (3.6:1) before
producing, so Kafka stores **0.77 GB/s of unique data**. The 1.26 GB/s figure is the *wire* rate
from agents to gateways, where the cheaper LZ4 is used because that CPU is spent on 12,000 hosts
rather than 90 pods. Two different rates for the same records at two different hops — using the
wrong one here is the most common error in this arithmetic. With the stored figure:

```
1. Disk write:
     0.77 GB/s × RF 3 = 2.31 GB/s across the cluster
     ÷ 550 MB/s per broker (50% of sustained NVMe) = 4.2 brokers

2. Network:
     writes in       0.77 GB/s
     replication out 1.54 GB/s   (each broker forwards to 2 followers)
     consumer reads  3.85 GB/s   (5 consumer groups)
     replication in  1.54 GB/s
     total          7.70 GB/s = 61.6 Gbit/s
     ÷ 15 Gbps usable per broker = 4.1 brokers

3. Retention:
     0.77 GB/s × 21,600 s = 16.6 TB unique × 3 = 49.9 TB
     ÷ 12 TB usable per broker = 4.2 brokers

4. Page cache residency — the one that binds:
     required: consumers must be able to fall 30 minutes behind without
               hitting disk, because 30 minutes is how long a consumer
               deploy plus a bad rollback takes.
     per broker write rate = 0.77 GB/s × 3 ÷ N brokers
     page cache available  = 150 GB per broker
     residency = 150 GB ÷ (2.31 GB/s ÷ N) = 64.9 × N seconds
     for 1,800 s (30 min):  N = 1,800 ÷ 64.9 = 27.7 brokers
```

**Constraints 1 through 3 all say four brokers. Constraint 4 says twenty-eight.** That is the
whole lesson of sizing a log buffer:

> **You size a log Kafka cluster on memory, not on disk, network or CPU** — because the workload
> is many consumers repeatedly reading recent data, and the difference between serving that from
> page cache and serving it from disk is roughly two orders of magnitude in cost.

Lantern runs 24 brokers, giving:

```
residency = 64.9 × 24 = 1,558 s = 26 minutes
```

Twenty-six minutes, which is the figure doc 01's reference table carries and which every later
doc cites.

It is worth seeing how sensitive this is. Had the gateway *not* recompressed — had it simply
forwarded the agents' LZ4 at 1.26 GB/s — the same 24 brokers would give:

```
per broker: 1.26 × 3 ÷ 24 = 158 MB/s
residency:  150 GB ÷ 158 MB/s = 949 s ≈ 16 minutes
```

**Sixteen minutes instead of twenty-six, from a compression-level setting in a completely
different tier.** Nothing about the broker changed. This is the argument for recomputing
residency whenever anything upstream of it changes, and for treating it as a monitored quantity
rather than a design-time constant — doc 19 exports it as a live gauge for exactly this reason.

### Final configuration

```yaml
cluster: lantern-logs
  brokers: 24 × i3en.6xlarge    # 24 vCPU, 192 GB, 2×7.5 TB NVMe, 25 Gbps
  zones: 3, 8 brokers each
  mode: KRaft, 3 dedicated controllers
  replication.factor: 3
  min.insync.replicas: 2
  unclean.leader.election.enable: false
  acks: all                      # set at the gateway's producer
  compression.type: producer     # do NOT recompress at the broker — see B-09
  num.replica.fetchers: 8
  log.retention.hours: 6         # 168 for logs.audit
  log.segment.bytes: 1073741824  # 1 GB
  message.max.bytes: 4194304     # 4 MB — a batch, not a record
```

`min.insync.replicas: 2` with RF 3 is the standard choice and the reasoning is in
[`../Kafka/02-replication-isr-and-durability.md`](../Kafka/02-replication-isr-and-durability.md):
acknowledged data survives one failure, and writes continue through one failure. For a log
pipeline there is a specific extra argument — if the cluster loses enough replicas that writes
must stop, you want them to stop **loudly at the gateway**, where the agents' disk buffers absorb
it, rather than continuing into a cluster that can lose them.

---

## Alternatives to Kafka

Kafka is the default and the right default. Here is when it is not.

### Redpanda

Kafka-protocol-compatible, written in C++, no JVM, thread-per-core with direct I/O. For a log
buffer specifically its advantages are real: no garbage collection pauses under sustained high
throughput, and better performance per node — often 2–3× on write-heavy workloads, which is
exactly this workload.

Against: a smaller operational community, and it manages its own memory rather than relying on
the page cache, which means the residency arithmetic above works differently — you configure the
cache explicitly rather than getting it from the OS. That is arguably better (it is predictable)
but it is not the same, and everything you know about tuning Kafka's memory does not transfer.

**Consider it if** you are starting fresh, your workload is write-dominated, and node count
matters to you more than ecosystem depth.

### Apache Pulsar

Separates brokers from storage (BookKeeper). The relevant advantages for logs: brokers are
stateless so scaling is instant with no partition reassignment, tiered storage to object storage
is native (so "retention" can be days without the Kafka cost derived above), and multi-tenancy is
a first-class concept with per-namespace isolation rather than something you build.

That last point is genuinely attractive for a 40-tenant log platform — see
[`../../system-design-notes/eventPlatformsAtScale.md`](../../../system-design-notes/eventPlatformsAtScale.md)
for the fuller comparison.

Against: two systems to operate instead of one (brokers and bookies, plus ZooKeeper or etcd), a
smaller talent pool, and higher end-to-end latency at the same throughput.

**Consider it if** tenant isolation at the buffer tier is a hard requirement, or if you want
long retention in the buffer without paying for it in broker disk.

### AWS Kinesis Data Streams

Managed, no brokers to run. For a team without Kafka operators this is a serious argument.

Against, and the numbers are decisive at this scale:

```
Kinesis on-demand: $0.040 per GB ingested
0.77 GB/s × 86,400 × 30 = 1,996 TB/month
1,996,000 GB × $0.040   = $79,840/month for ingest alone
plus retrieval charges per consumer beyond the first two
```

Against roughly $38,000/month for 24 `i3en.6xlarge` instances. Kinesis also caps a shard at
1 MB/s write and 2 MB/s read, so 0.77 GB/s needs 770 shards and the read fan-out needs enhanced
fan-out, which is billed separately.

**Consider it if** your volume is well under 100 MB/s and you have no platform team.

### NATS JetStream

Lightweight, simple, excellent latency. Strong for a fast fan-out tier.

Against: not designed for terabyte-scale retention or the replay patterns a log pipeline needs.

**Consider it as a complement**, not a replacement — for instance as the tail-router's fan-out
layer downstream of Kafka, where its latency advantage is worth something and its retention
limitations do not matter.

### Object storage as the buffer

Gateways write small objects to S3 every few seconds; consumers poll a manifest. No brokers at
all.

This is genuinely viable now, and several modern systems do it. It is dramatically cheaper for
storage and infinitely elastic. The costs are latency (S3 PUT plus list plus GET is hundreds of
milliseconds, not single-digit) and request volume (doc 01's object-count arithmetic applies
with full force).

**Consider it if** you have no live-tail requirement. With a tail requirement it cannot meet the
latency budget, so the usual shape is object storage for the archive path and a broker for the
low-latency path — which is what Lantern does anyway, just with the split drawn after the buffer
rather than instead of it.

---

## The consumer groups

Five, each with its own pace and failure mode:

| Group | Reads | Lag tolerance | What happens if it stops |
|---|---|---|---|
| `indexer` | all topics | 2 min | Search goes stale. Most visible. |
| `archiver` | all topics | 10 min | Nothing visible — until retention expires, then permanent loss |
| `tail-router` | `logs.app` only | **5 s** | Live tail breaks immediately |
| `metrics-deriver` | `logs.app`, `logs.access` | 1 min | Derived metrics gap |
| `siem-export` | `logs.audit`, `logs.app` | 30 min | Another team's problem |

Three observations that shape operations.

**The archiver is the most dangerous one and the least visible.** Nobody notices it is stopped —
search still works, tail still works, dashboards are fine. The damage begins at the six-hour
retention horizon, and it is permanent. **Alert on the archiver's lag more aggressively than on
anything else**, and specifically alert on `retention_remaining = 6h − consumer_lag_time` falling
below two hours. That inverted framing — "how long until this becomes unrecoverable" — is far
more actionable than a lag number.

**The tail router's 5-second tolerance is what pins it to the page cache.** At 26 minutes of
residency it has enormous margin, which is exactly why doc 12 can promise a latency budget at
all.

**Every group multiplies the read load.** Adding a sixth consumer group that reads everything
adds 0.77 GB/s of reads. Before adding one, ask whether it can read from the archive instead —
most analytical consumers can, and doc 07 covers the routing.

---

## The buffer failure catalogue

| Class | Scenarios |
|---|---|
| **A. Capacity and retention** | `B-01`, `B-05`, `B-10` |
| **B. Distribution and scale** | `B-02`, `B-03` |
| **C. Consumers** | `B-04`, `B-06`, `B-08` |
| **D. Durability** | `B-07`, `B-09` |

### B-01 · Retention expiry turns an outage into data loss

**What you see.** After a long consumer outage, the consumer restarts and its offset is out of
range. Depending on `auto.offset.reset` it either jumps to the latest offset (skipping
everything, silently) or to the earliest (reprocessing hours, loudly).

**Mechanism.** Six-hour retention. A consumer down for seven hours cannot recover the first hour.

**Confirm it.** Consumer lag *in time* — not records — against the retention setting. The
inverted metric above is the one to alert on.

**Recover.** If the archiver kept up, reprocess from object storage rather than Kafka. If the
archiver was the thing that was down, the data is gone.

**Prevent.** Alert on time-to-unrecoverable, not on lag. Give `logs.audit` longer retention
because it is cheap. And make the archiver the most reliable consumer in the system: fewest
dependencies, simplest code, its own priority class. It is the one whose failure is irreversible.

### B-02 · Partition hot spot

**What you see.** One partition's lag climbing while its siblings sit at zero. One broker's disk
and CPU well above the others.

**Mechanism.** A key with skew. Derived above: `service.name` gives one partition 31% of bytes;
even `pod.uid` fails for a single pod emitting 40 MB/s.

**Confirm it.** Bytes-in per partition, sorted. A healthy topic is within ±15% across partitions;
a hot spot is 10× or more.

**Recover.** Rate-limit the offending source (doc 16). Repartitioning is not a real-time option —
increasing partition count changes the hash mapping for *all* keys and breaks ordering during the
transition.

**Prevent.** Key on the finest-grained stream identity, plus the per-pod ceiling with stream
splitting described above. Monitor per-partition balance as a first-class signal, because it
degrades gradually and nothing fails until it does.

### B-03 · Too many partitions

**What you see.** Slow controller failover, slow metadata propagation, high broker memory, and
producer latency that got worse without any throughput change.

**Mechanism.** The per-service-topic arithmetic above. 4,000 partition replicas per broker.

**Confirm it.** Total partition count, per-broker replica count, controller failover duration in
a test.

**Recover.** ⚠️ You cannot reduce a topic's partition count. Recovery means creating new topics
with fewer partitions and migrating producers and consumers — a project, not an action.

**Prevent.** Topics by class, not by service. Budget partitions explicitly: under 4,000 per
broker is workable, under 1,000 is comfortable, and Lantern's 38 leaves enormous room. See
[`../Kafka/08-how-kafka-breaks-at-scale.md`](../Kafka/08-how-kafka-breaks-at-scale.md).

### B-04 · One lagging consumer degrades the whole cluster

**What you see.** Producer latency rising and *all* consumers slowing, with no change in traffic.
Broker disk read throughput — normally near zero — is suddenly high.

**Mechanism.** The most important cluster-wide failure in a log pipeline. A healthy Kafka cluster
serves nearly all reads from page cache; broker disk reads sit near zero. One consumer falls
past the 26-minute residency window and starts reading from disk. Those reads pull old pages into
the cache, **evicting the recent pages every other consumer needs**. Now everyone is reading from
disk, disk reads compete with the ingest writes, and the whole cluster degrades because of one
consumer's problem.

**Confirm it.** Broker disk read bytes per second. This is the single cleanest alert in the
entire buffer tier, because the healthy value is approximately zero and any sustained non-zero
value means exactly one thing.

**Recover.** Stop or throttle the lagging consumer. Counter-intuitively, ⚠️ **do not scale it up
to catch up faster** — more parallel readers of cold data evict more aggressively and make the
cluster-wide problem worse. Let it catch up slowly, or have it skip forward and backfill from
object storage instead.

**Prevent.** Alert on any consumer approaching the residency window, using the time figure
derived above, not a record count. Configure `replica.fetch` and consumer `fetch.max.bytes` so a
catch-up read is rate-limited. Use Kafka's client quotas to cap a consumer group's read
bandwidth, which turns "degrades the cluster" into "catches up slower". Full treatment in
[`../Kafka/06-lag-backpressure-and-poison-messages.md`](../Kafka/06-lag-backpressure-and-poison-messages.md).

### B-05 · Broker disk fills during a downstream outage

**What you see.** Brokers at high disk usage; if it reaches 100%, the broker stops accepting
writes and its partitions go under-replicated.

**Mechanism.** Retention is time-based, so a volume spike fills disk faster than expected. At 3×
normal volume, six hours of retention is 150 TB rather than 50.

**Confirm it.** Disk usage per broker, trended, with the projection to full.

**Recover.** Reduce retention temporarily — `log.retention.hours` is a dynamic config and takes
effect on the next segment roll. ⚠️ Doing so may push a lagging consumer past its horizon
(`B-01`); check consumer lag before reducing, and prefer dropping `logs.access` retention rather
than `logs.app`.

**Prevent.** Size disk for the volume spike, not the volume plan: Lantern's 24 brokers hold 50 TB
of 288 TB capacity precisely so a 3× spike has somewhere to go. Set both time-based and
size-based retention, so size acts as the backstop.

### B-06 · Rebalance storms

**What you see.** The indexer group repeatedly rebalancing, consumption stopping for seconds at a
time, lag sawtoothing.

**Mechanism.** A consumer exceeding `max.poll.interval.ms` is considered dead and triggers a
rebalance; the rebalance stops everyone; the resulting lag makes the next poll slower; repeat.
Classic, and covered fully in
[`../Kafka/04-consumer-groups-and-rebalance-failures.md`](../Kafka/04-consumer-groups-and-rebalance-failures.md).

**The log-specific trigger:** record size variance. An indexer sized for 494-byte records
receives a batch of 32 KB records (a service logging request bodies) and takes 40× longer to
process a poll. Size limits must be in **bytes**, not records — `max.partition.fetch.bytes` and a
records-per-poll limit derived from it.

**Prevent.** Cooperative sticky assignment, static group membership (`group.instance.id`) so a
rolling restart does not rebalance, and byte-based fetch limits.

### B-07 · Under-replicated partitions during a zone event

**What you see.** `UnderReplicatedPartitions` above zero. If `min.insync.replicas` is not met,
producers receive `NOT_ENOUGH_REPLICAS` and the gateway starts rejecting.

**Mechanism.** An AZ becomes unavailable; 8 of 24 brokers go with it. With rack-aware placement
each partition has exactly one replica per zone, so every partition drops to 2 of 3 — still
meeting `min.insync.replicas: 2`, so **writes continue**. That is the design working.

Without rack awareness, some partitions would have had two replicas in the failed zone and would
drop to 1, below the minimum, and those partitions stop accepting writes.

**Confirm it.** `kafka-topics --describe --under-replicated-partitions`, and check
`broker.rack` is set on every broker.

**Recover.** Restore the zone. ⚠️ Do **not** enable unclean leader election to restore
availability — it trades silent data loss for availability, and for a log pipeline the agents'
disk buffers already give you 20+ minutes of tolerance, so there is no need to make that trade.

**Prevent.** `broker.rack` set correctly on every broker, verified by a test that actually fails
a zone. Rack awareness is a configuration that is either right or catastrophically wrong, and the
only way to know which is to test it.

### B-08 · A poison batch stalls a partition

**What you see.** One partition's lag climbing while others are fine. The consumer logs the same
error repeatedly.

**Mechanism.** A record the consumer cannot process — a size it cannot allocate, a compression
codec it does not support, malformed content. It retries forever, and because ordering must be
preserved within a partition, everything behind it waits. Head-of-line blocking.

**Confirm it.** Per-partition lag plus the consumer's error log; the offset will be constant.

**Recover.** Skip the offset manually and record which offset was skipped so it can be recovered
from the archive later.

**Prevent.** A bounded retry policy with a terminal path: after N attempts, write the record to
`logs.dlq` with its error and advance. Every consumer needs this and most do not have it by
default. Alert on the DLQ rate as a ratio, per doc 04's `S-05`.

### B-09 · Broker recompression

**What you see.** Broker CPU much higher than expected, with no throughput change. Producer
latency elevated.

**Mechanism.** `compression.type` on the topic set to a specific codec rather than `producer`.
When the producer's codec differs, the broker **decompresses and recompresses every batch**,
which destroys the zero-copy path and burns enormous CPU.

```
0.77 GB/s decompress + recompress at zstd-3 ≈ 0.77 GB/s ÷ 180 MB/s per core × 2
  ≈ 8.6 cores of pure waste, plus loss of sendfile() on the read path
```

**Confirm it.** Broker CPU profile, and compare the topic's `compression.type` against the
producer's.

**Prevent.** Set `compression.type: producer` on every log topic, always. There is no reason to
recompress at the broker in this pipeline.

### B-10 · Offset reset

**What you see.** Either a consumer suddenly reprocessing hours of data, or a consumer silently
skipping a gap. Both follow a consumer-group change or an out-of-range offset.

**Mechanism.** `auto.offset.reset` is `latest` (skip, silent) or `earliest` (replay, loud). It
fires when a committed offset is no longer valid — after retention expiry, after a group ID
change, after `__consumer_offsets` loss.

**Confirm it.** The consumer's committed offset history, and the group's state transitions.

**Prevent.** Use `auto.offset.reset: none` for consumers where either behaviour is unacceptable —
the consumer then fails to start and a human decides, which is the right outcome for the
archiver. Never change a consumer group's ID as a way to "reset" something; create the new group
explicitly with a chosen starting offset.

---

## What to take away

1. **The buffer's job is decoupling, fan-out, replay and absorption** — not throughput. It
   converts "a tier is down" into "a consumer is behind", which is a fundamentally better
   failure.
2. **Replay is what makes the pipeline correctable.** Every earlier doc's rule — keep
   irreversible operations downstream of the buffer — is only available because the buffer holds
   raw records.
3. **Do not create one topic per service.** 8,000 topics is 96,000 replicas and 4,000 per broker,
   which attacks the page cache, slows controller failover, and turns sequential writes into
   random ones. You also cannot undo it.
4. **Topic by consumer class, keyed by stream identity.** Seven topics separate the data with
   different consumers, retention and shedding priority, and cut the live-tail read amplification
   from 63× to 13×.
5. **Key on the finest unit that must stay ordered** — the pod, not the service and not the
   tenant — and add a per-stream ceiling above which you split the stream deliberately.
6. **Size the buffer on page-cache residency, not on disk, network or CPU.** Every other
   constraint said 4 brokers; residency said 28. It is the only constraint that reflects how
   consumers actually read.
7. **Recompute residency whenever compression changes.** Moving compression from the agent to the
   gateway changed it from 16 to 26 minutes — an apparently unrelated decision moving the number
   that governs the tier.
8. **Kafka is transit, not storage.** Forty-eight hours of retention costs $16,000 a month more
   than six, to hold data that reached object storage two minutes after arriving.
9. **Broker disk read throughput is the cleanest alert in the tier**, because its healthy value
   is approximately zero and any sustained non-zero value means a consumer has fallen out of the
   page cache and is degrading everyone.
10. **The archiver is the dangerous consumer.** Its failure is invisible until retention expires,
    and then it is permanent. Alert on time-to-unrecoverable, not on lag.
11. **Do not scale up a badly lagging consumer to catch up faster** — more parallel cold reads
    evict more page cache and worsen the cluster-wide problem.
12. **Set `compression.type: producer`.** Anything else makes the broker decompress and
    recompress every batch, burning cores and destroying the zero-copy read path.

Next: [07-the-processing-tier.md](07-the-processing-tier.md), which is what those consumer groups
actually do with the records.
