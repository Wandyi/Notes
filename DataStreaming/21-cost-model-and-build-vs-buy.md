# The Cost Model, and Build versus Buy

This document does two things. First, it derives Meridian's bill line by line, so that the
$361,170 quoted throughout the collection is checkable rather than asserted. Second, it compares
the realistic alternatives — managed services, other engines, other architectures — with numbers
rather than adjectives.

The headline, before the detail: **compute for a four-million-event-per-second platform costs
about $77,000 a month. Moving the bytes between racks in the same building costs $240,813.**
Everything interesting about streaming economics follows from that ratio.

## 1. The full bill

All figures are monthly, `us-east-1`, 730 hours, using 1-year reserved pricing where the
capacity is steady and a spot/reserved blend where it is not. Prices are approximate list rates
and will differ for you; the *structure* is what transfers.

### Transport tier

```
Kafka brokers
  48 × i3en.2xlarge (8 vCPU, 64 GiB, 5 TB NVMe)
  on-demand $0.904/h, 1-year standard RI $0.57/h
  48 × $0.57 × 730                                          = $ 19,973

KRaft controllers
  3 × m6i.xlarge, 1-year RI $0.122/h
  3 × $0.122 × 730                                          = $    267
                                                              ─────────
                                                              $ 20,240
```

### Processing tier

```
TaskManagers
  96 nodes steady, 168 at peak (doc 01 §7)
  time-weighted average 120 nodes
  r6id.4xlarge (16 vCPU, 128 GiB, 950 GB NVMe)
  on-demand $1.2096/h
  blend: 60% 1-year RI at $0.762, 40% spot at $0.42
    → effective $0.625/h
  120 × $0.625 × 730                                        = $ 54,750

JobManagers
  180 jobs in 16 session clusters, HA (2 per cluster)
  32 × m6i.large, 1-year RI $0.061/h
  32 × $0.061 × 730                                         = $  1,426
                                                              ─────────
                                                              $ 56,176
```

The spot fraction deserves a note. **TaskManagers are a reasonable spot workload** because a
terminated TaskManager is a job restart, which the platform handles anyway — it is the same
event as a machine failure. The constraint is that a spot interruption on a large-state job
costs 9.8 minutes of recovery
([01](01-requirements-and-the-capacity-model.md) Section 8), so Meridian runs the 21 large-state
jobs entirely on reserved capacity and uses spot for the other 159. The 40% blend is the result
of that split, not a uniform policy.

### Storage

```
Checkpoints and savepoints
  31 TB of state × 2.2 (3 retained checkpoints share incremental
    files, plus retained savepoints)
  = 68 TB × $0.023/GB                                       = $  1,564

Tiered Kafka storage
  full retention beyond 6 h local: 33.6 TB × $0.023          = $    773

S3 request charges
  checkpoint PUTs: 180 jobs × ~60 files/min × 43,200 min/mo
    = 466M PUT × $0.005/1,000                                = $  2,330
  tiered-storage GETs (replay and lagging consumers)         = $    570
                                                              ─────────
                                                              $  5,237
```

### Control plane and supporting services

```
Schema registry (3 × m6i.large, RI)                          = $    134
Metadata store (RDS db.m6g.large, multi-AZ)                  = $    310
Job registry, lineage service, control API (4 × m6i.large)   = $    178
Flink Kubernetes Operator (2 × m6i.large)                    = $     89
Certificate and secret management                            = $    406
                                                              ─────────
                                                              $  1,117
```

### Observability

```
Metrics: ~2.1M active series after the cardinality policy
  (doc 16 §5), self-hosted Prometheus + Thanos
  storage + compute                                          = $  7,420
Logs from the platform itself                                = $  2,180
Tracing (1 in 10,000 sampling)                               = $  1,587
                                                              ─────────
                                                              $ 11,187
```

### The shared serving tier

```
ClickHouse for platform-owned derived datasets
  18 × m6id.4xlarge, RI blend                                = $ 24,200
  S3 for cold tier                                           = $  2,200
                                                              ─────────
                                                              $ 26,400
```

(Tenant-owned serving stores are billed to tenants separately and are not in this total.)

### Network — the big one

From [01](01-requirements-and-the-capacity-model.md) Section 9:

```
Kafka cross-zone
  produce      607.2 MB/s × 2/3                =   405 MB/s
  replication  607.2 MB/s × 2                  = 1,214 MB/s
  consume    2,064.0 MB/s × 2/3                = 1,376 MB/s
                                                 ──────────
                                                 2,995 MB/s

Flink shuffle cross-zone
  13.1M/s × 0.55 shuffled × 330 B = 2,376 MB/s
  × 2/3                                        = 1,584 MB/s
                                                 ──────────
total cross-zone                                 4,579 MB/s

4,579 MB/s × 2,629,800 s = 12,040,650 GB
× $0.02/GB (charged $0.01 each direction)                    = $240,813
```

### Total

```
Transport tier                     $ 20,240    5.6%
Processing tier                    $ 56,176   15.6%
Storage                            $  5,237    1.4%
Control plane                      $  1,117    0.3%
Observability                      $ 11,187    3.1%
Shared serving                     $ 26,400    7.3%
Cross-zone network                 $240,813   66.7%
                                   ────────  ──────
                                   $361,170  100.0%
```

### The derived units

```
events/month:  345.6e9 × 30.42 = 10,513 billion
  $361,170 ÷ 10,513                 = $34.35 per billion events

GB ingested/month: 138.3 TB × 30.42 = 4,207,100 GB
  $361,170 ÷ 4,207,100              = $0.0859 per GB

per job: $361,170 ÷ 180             = $2,007 per job per month
```

The last one is worth keeping in mind for a different reason: it is the **average**, and the
distribution is extremely skewed. The attribution job alone costs about $38,000; the median job
costs about $310. Anyone proposing a new job is proposing something between those, and the
question "which end?" is answerable from its state size and its input rate before it is built.

## 2. What the bill tells you to do

Four conclusions fall straight out of the table, in order of value.

### 1. Attack the network first

Two thirds of the bill, and $114,028 of it is recoverable with configuration changes
([01](01-requirements-and-the-capacity-model.md) Section 9):

```
rack-aware consumer fetch (client.rack)      saves $ 72,372
zone-local placement for tolerant jobs       saves $ 41,656
                                                   ────────
                                                   $114,028   (31.6%)
```

The costs, stated honestly: consumers read from a follower that may be a few tens of
milliseconds behind, and zone-pinned jobs restart rather than degrade when their zone fails.
Meridian accepts both for everything except `riverbend.payments.settled`.

A third lever, not yet taken: **reduce the shuffle**. 55% of operator input is re-partitioned.
Co-partitioning (`J-09`), operator chaining, and pre-aggregating before the shuffle each reduce
it. A 20% reduction in shuffled bytes is $26,760 a month.

And a fourth, which is a bigger decision: **run in fewer availability zones.** Two zones instead
of three cuts cross-zone traffic by roughly a third, saves about $80,000 a month, and reduces
your tolerance from "any one zone" to "any one zone, with less margin." For a platform whose
data is replayable from a log, that trade is more defensible than it sounds. Meridian has not
taken it; the discussion is on record.

### 2. Volume control is worth more than tuning

The bill scales almost linearly with bytes. Every technique that reduces bytes multiplies
through the whole table:

| Lever | Reduction | Monthly saving |
|---|---|---|
| Avro instead of JSON (already done) | 2.6× on uncompressed | would have been ~$430,000 |
| Removing the 14 unused fields (`Z-`, doc 20 §2) | 681 MB/s of 1,600 = 42% on one stream | $31,400 |
| Reducing precision on the analytical copy | ~18% on Waypoint | $2,900 |
| zstd-3 instead of lz4 | 33% on the wire | $79,500 |
| Pre-aggregating before the shuffle | 20% of shuffle | $26,760 |

**The Avro line is the one to notice.** Had Meridian been built on JSON, the same platform would
cost roughly $790,000 a month instead of $361,170 — not because JSON compresses worse on the
wire (it compresses better) but because the uncompressed form flows through page cache, shuffle
buffers, RocksDB and every serialization step
([01](01-requirements-and-the-capacity-model.md) Section 4). **The encoding decision, made once
at the start, is worth more than every optimisation in this collection combined.**

### 3. The long tail of jobs is a real cost

180 jobs, median cost $310, and roughly 60 of them process under 1,000 events a second. Each
carries:

```
minimum 2 subtasks for redundancy         ~$110/month
a share of a JobManager                    ~$45
checkpoint storage and requests            ~$25
metrics series                             ~$30
                                           ──────
                                          ~$210/month floor
```

Sixty jobs × $210 = $12,600 a month of pure floor. Consolidating related small jobs — several
tenants' low-rate streams into one multi-stream job — recovered $6,400 of that on Meridian, at
the cost of a larger blast radius per job and a deploy that affects several teams.

**Whether that trade is worth it depends on how much you value independent deployability**,
which for small jobs owned by different teams is quite a lot. Meridian consolidated within teams
and not across them.

### 4. Reserved capacity and spot are worth more than they look

```
all on-demand:                                   $ 92,600 compute
Meridian's blend (RI + spot):                    $ 76,416 compute
all 3-year RI (no flexibility):                  $ 61,200 compute
```

The middle option is 17.5% better than on-demand and 25% worse than full commitment. The
flexibility that costs 25% is the ability to change instance types, which over three years of a
platform's life is worth having — Meridian changed its TaskManager instance type twice in three
years, once for the NVMe and once for the memory.

## 3. The managed alternatives

### Managed Kafka

| | Self-managed | MSK | Confluent Cloud |
|---|---|---|---|
| Broker cost | $19,973 | $31,400 | — |
| Throughput charge | — | — | $0.11/GB in, $0.09/GB out |
| Storage | included | $0.10/GB-month | $0.00013/GB-hour |
| At Meridian's volume | **$20,240** | **~$41,000** | **~$318,000** |
| Ops effort | 1.2 engineers | 0.4 | 0.1 |
| Control | full | partial | limited |

Confluent Cloud's number is dominated by the throughput charge:

```
in:  607.2 MB/s × 2,629,800 s ÷ 1,000 = 1,596,735 GB × $0.11 = $175,641
out: 2,064 MB/s × 2,629,800 s ÷ 1,000 = 5,427,907 GB × $0.09 = $488,512
```

which comes to $664,000 before any discount — so the $318,000 assumes a substantial enterprise
discount, which is what a platform at this volume would actually negotiate. Even discounted it
is 15× the self-managed broker cost.

**The rule that generalises:** managed streaming services price per byte, and per-byte pricing is
excellent at low volume and terrible at high. The crossover for Kafka is somewhere around 40–80
MB/s, below which you should not be running your own brokers and above which the economics
invert fast.

The 1.2 engineers of operational effort is $220,000 a year fully loaded, which is the honest
other side of the comparison. At Meridian's volume self-managing saves $3.5M a year against
Confluent and costs $150,000 a year of extra effort against MSK — so the *interesting*
comparison is self-managed against MSK, not against Confluent.

### Managed Flink

| | Self-managed on K8s | Amazon Managed Flink | Confluent Flink | Ververica |
|---|---|---|---|---|
| Unit | EC2 | KPU ($0.11/h, 1 vCPU + 4 GB) | CFU | KPU-equivalent |
| At 1,536 vCPU steady | $54,750 | 1,536 × $0.11 × 730 = **$123,341** | ~$140,000 | ~$95,000 |
| Large state support | Local NVMe | 50 GB/KPU attached storage | limited | good |
| Ops effort | 1.5 engineers | 0.5 | 0.3 | 0.6 |

Managed Flink is 2.25× the raw compute cost, which is a smaller multiple than managed Kafka
because compute is harder to mark up than bandwidth.

**The constraint that matters more than the price is state.** Amazon Managed Flink's 50 GB of
storage per KPU means the 4.2 TB attribution job needs 84 KPUs for storage alone regardless of
its CPU need. At $0.11 per KPU-hour that is $6,745 a month for that job's storage, against
$1,800 of NVMe on self-managed nodes.

**Managed stream processing is priced for jobs with modest state.** If your platform is mostly
stateless and small-state jobs, it is a good deal and removes a lot of operational work. If it
is dominated by multi-terabyte state, the pricing model works against you.

### The honest recommendation

```
< 20 MB/s, < 10 jobs        → fully managed everything. Do not build this.
20–100 MB/s, 10–40 jobs     → managed Kafka + managed Flink. The ops savings
                              exceed the price premium.
100 MB/s – 1 GB/s           → the genuinely hard zone. Model it with your
                              own numbers; both answers are defensible.
> 1 GB/s                    → self-managed, almost certainly. The per-byte
                              premium exceeds the cost of a platform team.
```

Meridian is at 607 MB/s onto the log, in the upper part of the hard zone, and self-manages. The
decision was made on the Kafka side (where the multiple is 15×) and the Flink side followed for
operational coherence rather than because the Flink economics demanded it.

## 4. Choosing the processing engine

| | Flink | Spark Structured Streaming | Kafka Streams | ksqlDB / Flink SQL | Materialize |
|---|---|---|---|---|---|
| Model | True per-event | Micro-batch | Per-event, library | SQL over either | Incremental view maintenance |
| Latency | ms | 100 ms – seconds | ms | depends | ms |
| Max practical state | TBs (RocksDB) | limited | GBs (RocksDB per instance) | as underlying | GBs |
| Event time + watermarks | **Best in class** | Good | Basic | Good | Good |
| Exactly-once to sinks | 2PC, broad | Idempotent sinks, narrower | Kafka-to-Kafka only | as underlying | Yes |
| Deployment | Cluster | Cluster | **Just a library in your app** | Cluster/service | Service |
| Batch on the same code | Yes | **Yes, natively** | No | Partially | No |
| Ops burden | High | Medium-high | **Low** | Medium | Low |
| Best at | Heavy state, strict event time | Mixed batch + streaming | Kafka-to-Kafka within a service | Analyst-authored pipelines | Low-latency materialised views |

**Kafka Streams' row deserves more attention than it usually gets.** It is a *library*: you add
a dependency, your service becomes a stream processor, and you deploy it like any other service
— no cluster, no JobManager, no slots. For a team with fewer than about fifteen Kafka-to-Kafka
jobs and state under a few hundred gigabytes, that removes most of this collection's operational
material.

What you give up: Flink's event-time sophistication, the ability to read from anything that is
not Kafka, TB-scale state, and batch execution of the same code. Those are real and they are why
Meridian uses Flink — but a team that does not need them is paying a large operational premium
for capabilities they will not use.

**Spark Structured Streaming** is the right answer when the same team already runs large Spark
batch workloads and the streaming requirement is minutes rather than milliseconds. The
micro-batch model makes sub-second latency hard and makes "the same code in batch and streaming"
genuinely natural, which is the kappa argument's best implementation.

**SQL-first (Flink SQL, ksqlDB)** changes who can build pipelines, which is often worth more
than any technical property. Meridian expresses 74 of its 180 jobs in Flink SQL, and the ones
written by analysts rather than platform engineers are in that set. The limit is that debugging
a SQL job's state and watermarks requires understanding everything in this collection anyway —
SQL lowers the authoring bar and not the operating bar.

## 5. The seven-step decision procedure

For someone deciding whether to build a streaming platform at all, and what shape.

**1. Establish the latency requirement, honestly.** Not "real time" — a number, with a reason.
If it is over five minutes, incremental batch is the answer and you can stop
([00](00-what-a-streaming-platform-is-and-why-it-is-hard.md) Section 10).

**2. Count the jobs you will have in two years.** Under fifteen: write consumers, or use Kafka
Streams. The platform's fixed cost is not justified below that.

**3. Compute the volume in bytes, not events**, and place yourself in Section 3's zones. This
decides managed against self-managed more than anything else.

**4. Estimate the state.** Total, and the largest single job. This decides your engine, your
node shape, your recovery times, and whether managed processing is viable.

**5. Decide the guarantee per stream**, using
[09](09-delivery-guarantees-and-exactly-once.md)'s decision tree. If the answer is "everything
exactly-once", go back and check — it is usually eleven jobs out of 180, not all of them.

**6. Price the network.** Multiply your expected cross-zone bytes by $0.02/GB and see whether it
dominates. If it does — it will — decide on rack-aware fetch and zone-local placement *before*
building, because retrofitting them means changing partition assignment and job placement.

**7. Write down the thresholds** at which each decision expires
([23-scaling-to-10x-and-100x.md](23-scaling-to-10x-and-100x.md)). A platform that does not know
where its design breaks will find out by breaking.

## What to take away

1. **Cross-zone network is 66.7% of the bill** — $240,813 against $77,000 of compute. Moving
   bytes between racks in the same building costs three times as much as processing them.
2. **$114,028 a month, 31.6%, is recoverable with two configuration changes**: rack-aware
   consumer fetch and zone-local job placement. Neither is a rewrite, and both should be
   decided before building because retrofitting them moves partition assignment.
3. **The encoding decision is worth more than every optimisation in this collection combined.**
   The same platform on JSON would cost roughly $790,000 a month instead of $361,170, because
   the uncompressed form flows through page cache, shuffle, state and serialization.
4. **The average job costs $2,007 and the median costs $310.** Sixty small jobs carry $12,600 a
   month of pure fixed floor, and consolidating them trades money against independent
   deployability.
5. **TaskManagers are a reasonable spot workload** — a spot interruption is the same event as a
   machine failure — but not for large-state jobs, where it costs 9.8 minutes of recovery.
6. **Managed streaming services price per byte**, which is excellent below roughly 40–80 MB/s
   and inverts fast above it. Confluent Cloud at Meridian's volume is 15× the self-managed
   broker cost.
7. **Managed stream processing is priced for modest state.** 50 GB per KPU means the 4.2 TB job
   needs 84 KPUs for storage alone.
8. **The interesting managed comparison is self-managed against MSK, not against Confluent** —
   $20,240 against $41,000, less $150,000 a year of engineer time, which is genuinely close.
9. **Kafka Streams removes most of this collection's operational material** for a team with
   under fifteen Kafka-to-Kafka jobs and sub-terabyte state. Using Flink instead is paying an
   operational premium for capabilities you may not use.
10. **SQL lowers the authoring bar and not the operating bar.** A Flink SQL job still has
    watermarks, state and checkpoints, and debugging it requires all of this collection.
11. **Seven steps: latency, job count, bytes, state, guarantees, network, thresholds.** Do them
    in that order, and stop at step 1 if the answer is five minutes.
