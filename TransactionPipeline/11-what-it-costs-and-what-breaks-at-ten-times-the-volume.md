# What It Costs, Where the Money Actually Goes, and What Breaks First at Ten Times the Volume

Two questions that come late in an interview and that most candidates have not prepared for:
*what does this cost to run*, and *what breaks first if volume grows tenfold*. Both are checking
the same thing — whether you have operated a system of this size or only drawn one.

This document derives both from the numbers in document 01.

---

## The monthly bill, component by component

List prices, US East, with reserved or spot pricing where a workload can genuinely use it.

| Component | Configuration | Monthly |
|---|---|---:|
| **Kafka brokers** | 18 × `kafka.im4gn.2xlarge`, 31.3 TB provisioned | $11,300 |
| Kafka cross-AZ transfer | Producer and consumer traffic across zones | $1,400 |
| **Flink — feature job** | 20 × `r6g.2xlarge` on-demand, 80 subtasks | $5,886 |
| Flink — ledger job | 6 × `r6g.xlarge` on-demand, exactly-once | $883 |
| Flink — ClickHouse feed | 4 × `r6g.xlarge` | $589 |
| **Lake writers** | 3 × `r6g.2xlarge` spot, 15-minute micro-batch | $339 |
| Lake maintenance | Compaction, snapshot expiry, balance re-derivation | $150 |
| **S3 storage** | 300 TB tiered: 90 days Standard, rest Glacier Instant | $1,825 |
| S3 requests | GET, PUT, LIST across all workloads | $400 |
| **Redis feature store** | 2 shards × 2 nodes, `cache.r6g.4xlarge`, 210 GB | $4,709 |
| DynamoDB durable copy | 190 GB, 2,300 WCU provisioned with autoscaling | $1,400 |
| **ClickHouse** | 6 × `r6g.4xlarge` reserved, 14 days of facts | $2,672 |
| **Snowflake** | 10.4 TB storage + ~1,312 credits/month | $4,175 |
| **Trino / Athena** | 200 regulatory queries/year, ~8 TB each | $667 |
| **Batch compute** | EMR spot: conform, curate, reconcile | $186 |
| **Observability** | Metrics, logs, traces, the control report | $3,200 |
| **Gateway and tokenization** | 40 ECS tasks across two regions | $1,800 |
| **Total** | | **$41,581** |

```
annual run rate = $41,581 × 12 = $498,972   ≈ $500,000
```

**Half a million dollars a year** to run the data platform of a card issuer with 95 million cards
and 3.06 billion events a day.

### Cost per transaction, which is the number to have ready

```
authorizations per month = 99,360,000 × 30.4 = 3,020,544,000
cost per authorization   = $41,581 / 3,020,544,000 = $0.0000138
```

**About 1.4 thousandths of a cent per authorization.** Or, in a unit that is easier to hold:

```
cost per 1,000 authorizations = $0.0138
```

Have this ready, because interviewers ask "is that a lot?" and the only way to answer is to
compare it to something. Two comparisons:

**Against interchange.** Monarch earns roughly 1.8% plus $0.10 on an average $94 transaction, or
about $1.79 in interchange revenue per transaction. The data platform costs 0.0008% of that.

**Against what the real-time tier saves.** Document 07 derived $3,247,860 a month in avoided fraud
loss from 2-second features versus 15-minute features.

```
platform cost / fraud savings = $41,581 / $3,247,860 = 1.3%
```

**The entire data platform costs 1.3% of what one of its consumers saves.** That is the sentence
that ends the cost conversation, and it is worth deriving rather than asserting.

---

## Where the money actually goes, which is not where people guess

Three observations, each of which contradicts a common assumption.

### Storage is 4.4% of the bill

```
S3 storage + requests = $2,225 / $41,581 = 5.4%
S3 storage alone      = $1,825 / $41,581 = 4.4%
```

Three hundred terabytes, seven years of every transaction the bank has processed, costs less than
the observability stack. **Nobody should ever shorten a retention period to save money on object
storage.** The retention tiering in document 01 mattered for a different reason — it reduced
*scan* volume and therefore query cost — but the storage bill itself was never the problem.

This is worth stating because retention-shortening reviews happen regularly at banks and are
almost always arguing about the wrong line item.

### The real-time tier is 43% of the cost and serves 3.2% of the volume

```
Kafka money topics (≈40% of broker cost)   $5,080
Flink feature job                          $5,886
Flink ledger job                             $883
Redis                                      $4,709
DynamoDB                                   $1,400
                                          -------
real-time tier                            $17,958  = 43% of total
```

Forty-three percent of the budget, for the 99 million authorizations that are 3.2% of the events.
Per event, the real-time tier is roughly **23 times more expensive** than the batch tier.

That is not a problem to fix. It is the correct allocation, and the justification is the $3.2
million a month. But being able to state the ratio shows you know what you are buying, and it
immediately answers "could we save money by streaming less?" — yes, about $18,000 a month, at a
cost of $3.2 million.

### The clickstream is 69% of events, 76% of bytes, and 18% of cost

Because every component it touches is the cheap one: a single day of Kafka retention, spot-priced
micro-batch writers, Glacier storage after 90 days, and no real-time path at all.

**Volume and cost correlate weakly.** The expensive thing is not the number of records; it is the
latency requirement and the correctness bar attached to them. Which is the same conclusion
document 07 reached from the other direction, and it is the most portable lesson in this whole
collection.

---

## Three cost mistakes worth naming

**Running streaming jobs on on-demand instances when they could be smaller.** Monarch's feature job
runs 20 nodes because 80 subtasks at the measured 820 events/second per subtask covers the 6,400/s
peak. At average load it needs 2. The 10× over-provision exists for the peak, and the honest
accounting is that it is paying $5,300 a month to be ready for a four-hour Black Friday. Flink's
reactive mode and Kubernetes autoscaling can reduce this, at the cost of rescale events — which
cost 25 minutes of freshness each (document 04). Monarch judged the complexity not worth $5,300
and that judgment is defensible either way, which is the kind of thing to say out loud.

**Keeping the warehouse running.** Snowflake's auto-suspend is the single highest-leverage setting
in the bill. Monarch's reporting warehouse suspends after 60 seconds of inactivity, which is
aggressive and correct for a workload of 90 scheduled queries a day. A 10-minute auto-suspend on
the same workload costs roughly $1,900 a month more for nothing.

**Not measuring per-consumer cost.** Monarch attributes every dollar to a consumer through tagging
and query attribution, and publishes it monthly. The effect is not that teams economise — it is
that **a consumer with no owner becomes visible.** Three materialised aggregates were dropped in
the first such review because nobody claimed them, saving $340 a month and, more valuably,
removing three things that could silently stop refreshing.

---

## What breaks first at ten times the volume

Ten times means 993.6 million authorizations a day — 11,500 per second average, 64,000 at peak —
and 30.6 billion events. Working through what fails, in the order it fails, which is a better
answer than "we would scale horizontally."

### First wall: the partition count

This is the one that hurts, and it is a consequence of a decision made on day one.

```
feature job subtasks needed = 64,000 / 820 = 78 → 80 at today's peak
at 10×                      = 640,000 / 820 = 780 subtasks
```

Seven hundred and eighty subtasks requires at least 780 partitions. `auth.events` has 240.

Increasing Kafka partitions is possible, and it **breaks key-to-partition mapping**: a key that
hashed to partition 37 of 240 hashes somewhere else in 800. Every key-ordered guarantee is
violated across the change, and every stateful consumer's keyed state becomes misaligned with its
new partition assignment.

The migration is genuinely awkward:

1. Create `auth.events.v2` with 1,200 partitions.
2. Dual-publish from the gateway to both topics.
3. Start a new feature job on v2, building state from a 7-day replay, while the v1 job keeps

   serving.
4. When the v2 job's features match the v1 job's within tolerance, switch the feature-store writer

   to the v2 job.
5. Migrate remaining consumers, then retire v1.

Roughly three weeks of careful work. **This is the argument for over-provisioning partitions on day
one** — 1,200 partitions instead of 240 would have cost nothing measurable in 2026 and saved three
weeks in 2029. If there is one thing to change about Monarch's design with hindsight, it is this.

### Second wall: `maxParallelism`

Document 04 set it to 1,024. At 10× the job needs 780 subtasks, which fits — just. At 13× it does
not, and raising `maxParallelism` requires rebuilding state from scratch, which means a 7-day
replay with no feature updates during it.

The fix is the same lesson: set it to 32,768 on day one. It costs a slightly larger key-group
metadata structure and nothing else.

### Third wall: feature-store size

```
active cards at 10×                     520,000,000
state per card                          12.3 KB
keyed state                             6.4 TB
serving footprint in Redis              1.9 TB
shards needed (105 GB usable each)      19 → 20 shards × 2 nodes
cost                                    $47,100/month
```

Ten times the data costs ten times as much, which is linear and therefore not a wall in the
architectural sense — but $47,100 a month for a feature store is enough to make someone revisit
whether every card needs every feature. The likely answer at that scale is tiering: full features
for the 18% of cards that transact weekly, reduced features for the long tail, computed on demand.

### Fourth wall: Kafka broker storage

```
raw bytes/day at 10×       24.8 TB
broker storage at 10×      232 TB  (same retention policy)
brokers needed             232 / 1.74 = 134 → 140
```

A 140-broker cluster is operable but it is past the point where one cluster is comfortable.
Partition replica count would be about 46,800, approaching the range where controller operations
and recovery times start to dominate. The answer is **multiple clusters split by stream** —
clickstream on its own cluster, money streams on another — which is a better separation anyway and
should arguably have been the design at 1×.

### What scales without any thought at all

Worth listing, because it shows where the design is already right:

- **S3**: 3 PB instead of 300 TB, at ten times the price. No architectural change.
- **Batch compute**: ten times the node-hours, same jobs, same code. $1,860 instead of $186.
- **Iceberg**: ten times the files, same manifests, same planning model. Compaction target rises
from 512 MB to perhaps 1 GB.
- **Trino/Athena regulatory queries**: ten times the scan, still 200 queries a year, $6,670
instead of $667.
- **The reconciliation**: ten times the rows in the same joins.

**The batch tier scales by spending more money and the streaming tier scales by requiring a
migration.** That asymmetry is the practical reason to keep the streaming tier as small as the
freshness SLOs allow, and it is a better argument than cost.

---

## What 100× would actually look like, honestly

Ten billion authorizations a day is more than the entire global card industry processes. No issuer
reaches it. So the honest answer to "how would you scale to 100×" at a card issuer is to decline
the premise, and then answer the question that is actually behind it:

> Authorization volume is not where growth comes from. It is bounded by how many cards exist and
> how often humans buy things, and both grow at single-digit percentages a year. The growth I
> would actually plan for is **events per authorization** — more models scoring each transaction,
> richer feature sets, more granular rule telemetry — and that is a different scaling problem with
> a different answer.

Concretely: if Monarch goes from one fraud model to twelve models scoring every transaction (a
realistic five-year trajectory — separate models for card-present, card-not-present, account
takeover, application fraud, merchant risk, and so on), then:

```
decision records per auth          1 → 12
risk.decisions volume              99.4 M → 1.19 B records/day
feature reads per auth             1 batched → still 1, if features are shared
feature definitions                47 → ~300, with heavy overlap
```

The interesting consequence is that **the feature store barely grows while the decision log grows
twelvefold**, because models share features. So the scaling pressure lands on the 7-year retention
of decision records — 33.5 TB becomes 402 TB — and on the governance process, not on the real-time
path.

That is a more useful answer than "shard more," and it demonstrates the thing the question is
actually probing: whether you know which dimension of your system is going to grow.

---

## What to take away

1. **About $41,600 a month, $500,000 a year**, for a platform handling 3.06 billion events and

   99.4 million authorizations a day.
2. **$0.0138 per thousand authorizations**, or 0.0008% of the interchange revenue on an average

   transaction. Have the comparison ready, because "is that a lot" is unanswerable without one.
3. **The platform costs 1.3% of what its fraud consumer saves.** Derive it rather than assert it.
4. **Storage is 4.4% of the bill.** Never shorten retention to save object-storage money — the

   retention tiering mattered for scan cost, not storage cost.
5. **The real-time tier is 43% of the cost for 3.2% of the events** — 23× more expensive per

   event. That is the correct allocation, and knowing the ratio answers "could we stream less?"
    immediately.
6. **The clickstream is 69% of events, 76% of bytes, and 18% of cost**, because every component it

   touches is the cheap one. Volume and cost correlate weakly; latency requirements and correctness
    bars drive cost.
7. **Auto-suspend on the warehouse is the highest-leverage single setting**, worth about $1,900 a

   month between a 60-second and a 10-minute timeout.
8. **Attribute cost per consumer and publish it** — not so teams economise, but so a consumer with

   no owner becomes visible.
9. **The partition count is the first wall at 10×.** 240 partitions cannot support 780 subtasks,

   and increasing them breaks key-to-partition mapping, so the migration is a three-week dual-write
    exercise. Over-provision partitions on day one; 1,200 would have cost nothing.
10. **Set** `maxParallelism` **to 32,768 on day one.** Raising it later requires a full state rebuild.
11. **The batch tier scales by spending more; the streaming tier scales by requiring a migration.**

    That asymmetry is a better argument for a small streaming tier than cost is.
12. **At a card issuer, 100× authorization volume is not a real scenario — 12× decision records

    per authorization is.** The growth axis is models and features per transaction, which lands on
      retention and governance rather than on the real-time path.
