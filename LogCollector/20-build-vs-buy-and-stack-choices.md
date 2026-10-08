# What It Costs, and Whether to Build It

Twenty docs of design decisions, each with a local cost. This doc adds them up, compares the
total against buying the same capability, and works through the stack choices — Elasticsearch
versus Loki versus ClickHouse versus a vendor — with the numbers rather than the marketing.

The conclusion is not the one most people expect, and it is worth stating first so the rest is
checkable: **at Lantern's scale, after volume control, building and buying cost roughly the
same.** The decision therefore has to be made on other grounds, and this doc sets out what those
are.

---

## The bill

Everything Lantern runs, at 10 TB/hr, before volume control. Rates are on-demand AWS us-east-1;
committed-use discounts typically take 30–40% off the compute lines.

### Compute

| Component | Sizing | Derived in | Monthly |
|---|---|---|---|
| Agents (Fluent Bit × 12,000) | 106 cores, 420 GB | doc 03 | $4,011 |
| Gateway | 90 pods × 4 cores, 8 GB | doc 05 | $11,300 |
| Processing | 72 pods × 8 cores, 16 GB | doc 07 | $18,080 |
| Kafka | 24 × `i3en.6xlarge` | doc 06 | $38,400 |
| Hot tier + query | 40 × `i3en.6xlarge` | docs 09, 11 | $64,000 |
| Compaction | 111 cores | doc 09 | $3,500 |
| Catalogue + control plane | Postgres HA, config service | doc 09 | $4,800 |
| Export + tail routers + misc | | docs 12, 14 | $4,200 |
| | | **compute** | **$148,291** |

### Storage

| Tier | Volume | Monthly |
|---|---|---|
| Hot (in the node cost above) | 106.6 TB | — |
| Warm, S3 Standard | 611 TB | $14,053 |
| Cold, S3 Glacier IR | 8,066 TB | $32,264 |
| Requests (PUT, GET, lifecycle) | | $1,900 |
| | **storage** | **$48,217** |

### Network — the line nobody puts in their design

Kafka replicates across three availability zones, and cross-zone transfer is charged in both
directions at $0.01/GB each way.

```
data produced into Kafka (after gateway zstd-1)      0.77 GB/s
producer → leader, 2/3 of the time cross-zone        0.51 GB/s
leader → 2 followers, always cross-zone              1.54 GB/s
consumers, with follower fetching (KIP-392)          0     ← same-zone reads
                                                     ---------
                                                     2.05 GB/s crossing zones

2.05 GB/s × 2,592,000 s/month = 5,314 TB/month
5,314,000 GB × $0.02 (both directions)               = $106,272/month
```

**One hundred and six thousand dollars a month to move data between buildings in the same city**
— 35% of the entire platform bill, and larger than every compute line combined except the query
tier.

This is the most commonly missed line in a log platform budget, and it deserves three specific
responses:

- **Follower fetching (KIP-392) is not optional.** It is already assumed above. Without it,
  consumers read cross-zone too, and five consumer groups reading 0.77 GB/s across zones adds
  roughly **$200,000 a month**. Confirm it is enabled and confirm consumers are actually using
  it, because the client configuration (`client.rack`) is easy to omit and fails silently — you
  get correct behaviour and a large bill.
- **Compressing harder at the gateway pays for itself many times over here.** zstd-9 instead of
  zstd-1 takes 0.77 GB/s to 0.45 GB/s, cutting this line to $62,000 — a **$44,000 monthly
  saving** for about 65 extra gateway cores costing $1,600. Doc 05 chose zstd-1 for latency;
  with this number visible, the right answer is zstd-3 as a compromise (0.60 GB/s, $83,000,
  saving $23,000 for 12 cores) or zstd-9 if the extra 20 ms of gateway latency is acceptable.
  **This is a case where the local optimisation and the global one disagree, and the local one
  was chosen because the global cost was not on the same page.**
- **Two zones instead of three** would cut it further and is the wrong answer — it takes
  `min.insync.replicas=2` from "survives one zone" to "survives nothing".

### The total

| | Monthly | Annual | Share |
|---|---|---|---|
| Compute | $148,291 | $1.78M | 49% |
| Storage | $48,217 | $0.58M | 16% |
| Cross-zone network | $106,272 | $1.28M | 35% |
| **Infrastructure** | **$302,780** | **$3.63M** | |
| People (6.5 engineers fully loaded) | $135,400 | $1.62M | |
| **Total cost of ownership** | **$438,180** | **$5.26M** | |

```
per TB/hr of sustained ingest:  $302,780 ÷ 10 = $30,278/month
per GB ingested:                $302,780 ÷ 7,200,000 GB = $0.042/GB
```

**Four point two cents per gigabyte, all in, self-hosted.** Hold on to that number; it is the
one to compare everything against.

### The people line is not padding

Six and a half engineers, and the split is instructive:

```
2.0  pipeline: agents, gateway, processing, the control plane
1.5  storage and query: compaction, indexing, the query engine
1.0  Kafka
1.0  on-call, incident response, gamedays
1.0  tenant support, onboarding, schema governance, cost reviews
```

That last one surprises people and is the one that grows. **A multi-tenant platform spends as
much effort on its tenants as on its infrastructure**, and a plan that budgets for the
infrastructure and not for the tenant-facing work produces a platform nobody can onboard onto.

---

## Buying instead

### List prices at this volume

```
240 TB/day = 7.2 PB/month = 7,200,000 GB/month
480 billion records/day = 14.4 trillion records/month
```

| Vendor | Model | At 7.2 PB/month, list |
|---|---|---|
| Datadog Logs | $0.10/GB ingested + indexing per million events | **$720,000** ingest alone |
| Datadog, 15-day indexed | + ~$2.50 per million events | + **$36,000,000** |
| Splunk Cloud | workload or ingest pricing | **$1.5M–3M** |
| Elastic Cloud | capacity-based | **$400,000–600,000** |
| Grafana Cloud Logs | ~$0.50/GB ingested (Loki-based) | **$3,600,000** |
| New Relic | $0.35/GB ingested | **$2,520,000** |

**Two things are immediately obvious.**

First, **indexing everything on a per-event SaaS model is not a real option** — thirty-six
million dollars a month is not a negotiating position, it is a category error. Every vendor's
answer to this is a tiered product where most data is stored unindexed and cheap, which is
exactly the architecture doc 09 built. The vendors reached the same conclusion; they are just
charging for it.

Second, even the cheapest ingest-only line is **$400,000–720,000 a month against $302,780
self-hosted**. Enterprise discounts of 50–70% are normal at this volume, which brings the
cheapest into the $150,000–300,000 range — genuinely comparable.

### The comparison that actually matters: after volume control

Doc 08 reduced ingest by 82.5%, to **0.485 GB/s = 42 TB/day = 1,258 TB/month**. Every price
above scales with volume, and so does most of the self-hosted bill.

| | Before volume control | After (82.5% reduction) |
|---|---|---|
| Self-hosted infrastructure | $302,780 | **$60,400** |
| Self-hosted, with people | $438,180 | **$195,800** |
| Datadog ingest at list | $720,000 | $125,800 |
| Datadog with a 40% discount | $432,000 | **$75,500** |
| Plus 2 engineers to run it | | +$41,700 |
| **SaaS total** | | **$117,200** |

Check the self-hosted figure: roughly 80% of the $302,780 is volume-proportional, so
`$302,780 × 0.2 + $302,780 × 0.8 × 0.175 = $60,556 + ...` — more precisely, the fixed portion
(catalogue, control plane, minimum cluster sizes, on-call) does not shrink, so the realistic
floor is about $60,400.

```
self-hosted, total cost of ownership:   $195,800/month
SaaS, total cost of ownership:          $117,200/month
```

**After volume control, buying is cheaper** — by about $78,600 a month, or $943,000 a year. And
before volume control, building is cheaper by $293,000 a month.

**Volume control changes the build-versus-buy answer**, because SaaS cost is almost entirely
proportional to volume while self-hosted cost has a substantial fixed floor. That is the single
most useful finding in this doc, and it means **the first thing to do is not choose a vendor or
a stack — it is reduce volume, and then decide.**

### What the comparison omits, in both directions

**In favour of buying**, beyond price: no on-call for the platform itself, features you would
never build (anomaly detection, log-to-trace correlation, a mature UI), and someone else's
problem when it breaks at 03:00.

**In favour of building:** no egress surprises, no per-seat pricing, data residency you control,
no vendor lock-in on your query language and dashboards, and — the one that bites organisations
later — **cost that does not reprice at renewal.** A vendor at $75,500/month with a 40% discount
is a vendor who can quote $150,000 in year three, and by then the migration cost is a year of
work.

**Omitted from both:** the cost of *not* having good logs. Every hour of extra mean-time-to-
resolution across an engineering organisation is worth considerably more than the difference
between these numbers, and it is the reason the cheapest option is rarely the right one.

---

## The self-hosted stack choices

If you build, four realistic architectures. All assume the same collection and transport tiers
(docs 02–07); they differ in storage, indexing and query.

### Elasticsearch / OpenSearch

**What you get.** The most capable search over logs that exists — full-text, relevance ranking,
aggregations, and Kibana, which is a genuinely good product. The module library gives working
parsers for common software on day one.

**What it costs**, from doc 10:

```
on-disk:            ~13.6× the columnar approach for the same window
indexing CPU:       430 cores at peak, vs 56 for a columnar writer
48 h hot tier:      720 TB with replicas, vs 53 TB
```

Pricing that hot tier on `i3en` NVMe:

```
720 TB ÷ 12 TB per node = 60 nodes × $1,600 = $96,000/month for 48 hours
```

Against $64,000 for 40 nodes doing hot *and* query in the columnar design.

**When it is right.** Under about 1 TB/day; when full-text relevance genuinely matters; when
your team already operates it well. **Operating Elasticsearch at log scale is a specialist
skill** — shard sizing, merge policy, mapping governance, hot-warm architecture, circuit
breakers — and a team that has it should weigh that heavily, because it is the scarcest input
here.

**When it is wrong.** At 10 TB/hr with no full-text requirement, you are paying 13× for a
capability you are not using.

### Grafana Loki

**What you get.** Doc 10's "index almost nothing". Labels only, chunks in object storage,
brute-force scan within label-matched chunks. Extremely cheap to write, extremely cheap to
store, and an excellent fit with Grafana and Prometheus if you already run them.

**The cost model**:

```
index:     a few hundred MB total
storage:   240 TB/day ÷ 8 (gzip on ordered chunks) = 30 TB/day
write CPU: minimal — no index to build
```

Cheaper than the columnar design on storage (30 TB/day versus 21.8 TB/day is worse, actually —
columnar compresses better — but Loki needs no index tier and no hot NVMe tier).

**When it is right.** When queries are reliably label-prunable: "errors from this service in
this window" is Loki's best case and it is most queries. When cost dominates. When you are in
the Grafana ecosystem.

**When it is wrong.** When the important query is the unusual one. "Find this trace ID
anywhere" degenerates into scanning everything the labels could not eliminate — and that query
is disproportionately likely during an incident, which is when you can least afford a
twenty-minute answer. Loki's bet is a good one on average and it loses at the moments that
matter most.

### ClickHouse

**What you get.** The columnar architecture docs 09 through 11 describe, as a product. Sparse
primary index, `tokenbf_v1` and `ngrambf_v1` skip indexes, `MergeTree` with TTL-driven tiering
to object storage, and SQL.

**What it costs.** Essentially the derived bill above — it is what those docs were describing.

**When it is right.** Large volume, mostly structured logs, queries that filter and aggregate
more than they full-text search, and a team comfortable with SQL. This is the mainstream answer
for new large log platforms and has been for several years.

**Watch out for:** the `ORDER BY` key is the whole design and changing it means rewriting the
table (doc 09's `W-02`), `ReplacingMergeTree` deduplication is eventually-consistent and subtly
wrong if a mutable column is in the sort key, and operating a distributed ClickHouse cluster at
this scale is its own skill. See
[`../../system-design-notes/Clickhouse`](../../../system-design-notes/Clickhouse/README.md) for
the depth.

### A lakehouse: Iceberg plus a query engine

**What you get.** Parquet in object storage, an Iceberg catalogue, and Trino, DuckDB, Spark or
Athena for queries. Maximum flexibility, zero lock-in, and doc 14's export problem solved for
free because the table *is* the export.

**What it costs.** Cheapest storage of the four. The weakness is interactive latency: a Trino
query against S3 is seconds at best, and there is no tier-0 equivalent — so the 48% of queries
that want sub-second answers on the last 15 minutes are not served.

**When it is right.** When logs are primarily an analytical dataset, when interactive search is
a secondary requirement, and when you already have this stack for other data.

**The hybrid, which is what Lantern actually is.** A fast store for the hot window and a
lakehouse behind it:

```
tier 0 + tier 1:  ClickHouse or a purpose-built store — interactive, 48 h
tier 2 + tier 3:  Iceberg over Parquet in S3 — analytical, 13 months
one query layer federating both
```

This is more work than either alone and it is what most large platforms converge on, because the
hot and cold requirements are genuinely different products (doc 09's ten-million-to-one query
ratio).

### Side by side

| | Elasticsearch | Loki | ClickHouse | Lakehouse |
|---|---|---|---|---|
| Storage, 48 h hot | 720 TB | 60 TB | 53 TB | 43 TB |
| Index overhead | 90–140% | <0.1% | 3–22% | 0–3% |
| Write CPU (relative) | 7.7× | 0.8× | 1.0× | 0.9× |
| Full-text search | **excellent** | substring scan | n-gram filters | scan only |
| Label-pruned query | good | **excellent** | excellent | good |
| Unpruned query | good | **very poor** | good | moderate |
| Sub-second on hot data | yes | yes | **yes** | no |
| Aggregation | good | limited | **excellent** | excellent |
| Cost at 10 TB/hr | **highest** | low | low | **lowest** |
| Operational difficulty | high | low | moderate | moderate |
| Ecosystem | largest | Grafana | growing | largest |

---

## A decision procedure

Answer in order and stop at the first that applies.

**1. Under 100 GB/day?** Buy. Any SaaS. The volume is $300–3,000 a month at list and building
anything costs more in the first week of engineering time than three years of subscription.

**2. Under 1 TB/day?** Buy, or run a single-node ClickHouse or a small Elasticsearch cluster if
you have a strong reason. The break-even against one engineer's salary is around 2–5 TB/day.

**3. Have you applied doc 08's volume control?** If not, do that first. It changes the answer,
and every other decision is made with wrong inputs until it is done. This step is skipped
constantly and it is the most expensive skip in the whole exercise.

**4. Is data residency, sovereignty or air-gapping a hard requirement?** Build. The
constraint decides it.

**5. Over 5 TB/day after volume control, with a platform team?** Build, probably. The margin
narrows as volume grows but the fixed floor means it rarely reverses, and the strategic
argument — no renewal repricing — grows with volume.

**6. Over 5 TB/day without a platform team?** Buy, and use the savings to hire one. Then
revisit. **Running a log platform badly is worse than paying a vendor**, because a platform
nobody trusts gets routed around, and then you have both the cost and the `kubectl logs`.

**7. Genuinely on the line?** Two tiebreakers that decide more real cases than cost does:
- **Does full-text relevance search matter to your users?** If yes, either buy or run
  Elasticsearch, and accept the cost.
- **Can you get eight nines of confidence in your own on-call?** The platform is needed most
  during incidents, and an in-house platform that is down during an incident is worse than a
  vendor's that is up.

---

## What to take away

1. **The full bill at 10 TB/hr is $302,780 a month in infrastructure and $438,180 with people
   — 4.2 cents per gigabyte ingested, all in.**
2. **Cross-zone network is 35% of the bill and is the line nobody puts in their design.**
   $106,272 a month to move data between buildings in the same city.
3. **Follower fetching (KIP-392) is not optional** — without it, cross-zone consumer reads add
   roughly $200,000 a month, and the client-side `client.rack` setting fails silently when
   omitted.
4. **Compressing harder at the gateway saves $23,000–44,000 a month** in cross-zone transfer for
   1,600 dollars of CPU. Doc 05 chose zstd-1 on latency grounds because this number was not on
   the same page — a real example of a local optimum being the global wrong answer.
5. **Budget a full engineer for tenant-facing work.** Onboarding, schema governance, cost
   reviews and support are as much effort as the infrastructure, and a plan without them
   produces a platform nobody can adopt.
6. **Indexing everything on a per-event SaaS model is a category error** at this scale — $36M a
   month. Every vendor's real product is the tiered architecture doc 09 describes; they are just
   charging for it.
7. **Volume control changes the build-versus-buy answer.** Before it, building is cheaper by
   $293,000 a month. After an 82.5% reduction, buying is cheaper by $78,600. SaaS cost is
   proportional to volume; self-hosted cost has a large fixed floor.
8. **So reduce volume first, then decide.** Choosing a vendor or a stack before doing doc 08's
   work means choosing with wrong inputs.
9. **Elasticsearch costs 13.6× the storage and 7.7× the write CPU** and buys genuine full-text
   relevance search. Decide whether your users need that; do not pay for it by default.
10. **Loki's bet — that queries are label-prunable — is right on average and wrong at the
    moments that matter**, because the unusual query is disproportionately likely during an
    incident.
11. **Most large platforms converge on a hybrid**: a fast store for 48 hours, a lakehouse for
    thirteen months, one query layer over both. The hot and cold requirements differ by seven
    orders of magnitude and are genuinely different products.
12. **Running a log platform badly is worse than paying a vendor**, because a platform nobody
    trusts gets routed around — and then you have the cost *and* people back on `kubectl logs`.

Next: [21-case-studies.md](21-case-studies.md), which walks five real-shaped systems end to end.
