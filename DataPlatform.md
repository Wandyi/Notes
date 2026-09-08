# Staff Data Platform Engineer — Questions, Answers, and Case Studies

**Stack in scope:** ClickHouse (OLAP), PostgreSQL (OLTP), S3 (durable object store / lake), AWS
primary with a GCP secondary.

---

## How to read this document

This is interview preparation, but it is written as a course rather than a flashcard deck. Every
question below is followed by an actual answer — not a hint, not a bullet list of keywords, but the
reasoning a staff engineer would say out loud, with the numbers derived in front of you.

The document is organised around **one fictional company and one fictional system**, described in
Part 1. Every question in every later part refers back to it. That is deliberate: the single hardest
thing about a staff-level interview is that the questions are open-ended, and if you answer them from
generic first principles you sound like a textbook. If you answer them against a system you can see
in your head — with real row counts, real tenant names, real dollar figures — you sound like someone
who has run one.

Read Parts 0 and 1 first. After that you can jump to whichever competency area you want.

**Structure of each competency part:**

1. *What the interviewer is actually testing* — the hidden rubric.
2. *The mental model* — taught from scratch, with the naive approach shown first and then broken.
3. *Questions, in four tiers* — screening, design, deep-dive/adversarial, and organisational.
4. *An embedded case study* — a scenario with a worked answer.

At the end there are three long-form case studies in full 45-minute interview format, a rapid-fire
bank of ~120 shorter questions, cheat sheets of numbers worth memorising, and a list of the specific
things that separate a "senior" answer from a "staff" answer.

---

## Index

- [Part 0 — What "staff level" means when you answer](#part-0--what-staff-level-means-when-you-answer)
- [Part 1 — The running system: "Skyline"](#part-1--the-running-system-skyline)
- [Part 2 — Designing and delivering the ingestion pipeline](#part-2--designing-and-delivering-the-ingestion-pipeline)
- [Part 3 — Migrating legacy data lifecycle management without breaking consumers](#part-3--migrating-legacy-data-lifecycle-management-without-breaking-consumers)
- [Part 4 — SLIs, SLOs, dashboards, and alerting for a data platform](#part-4--slis-slos-dashboards-and-alerting-for-a-data-platform)
- [Part 5 — A flexible storage layer for transactional, analytic, and ML workloads](#part-5--a-flexible-storage-layer-for-transactional-analytic-and-ml-workloads)
- [Part 6 — Multi-tenant data model: isolation, secure sharing, and compliance](#part-6--multi-tenant-data-model-isolation-secure-sharing-and-compliance)
- [Part 7 — Data governance: quality, lineage, retention, and access control](#part-7--data-governance-quality-lineage-retention-and-access-control)
- [Part 8 — OLAP vs OLTP: internals, and choosing at production scale](#part-8--olap-vs-oltp-internals-and-choosing-at-production-scale)
- [Part 9 — SQL: writing it, reading plans, and making it fast](#part-9--sql-writing-it-reading-plans-and-making-it-fast)
- [Part 10 — Core primitives: building for a roadmap you can't see yet](#part-10--core-primitives-building-for-a-roadmap-you-cant-see-yet)
- [Part 11 — Cloud and multi-cloud operations](#part-11--cloud-and-multi-cloud-operations)
- [Part 12 — Integrating legacy systems with modern architectures](#part-12--integrating-legacy-systems-with-modern-architectures)
- [Part 13 — Long-form case studies](#part-13--long-form-case-studies)
  - [Case study A — "Design our data platform" (the whiteboard round)](#case-study-a--design-our-data-platform-the-whiteboard-round)
  - [Case study B — The billing discrepancy](#case-study-b--the-billing-discrepancy)
  - [Case study C — Six months to FedRAMP](#case-study-c--six-months-to-fedramp)
  - [Case study D — The cost crisis](#case-study-d--the-cost-crisis)
- [Part 14 — Rapid-fire question bank](#part-14--rapid-fire-question-bank)
- [Part 15 — Numbers worth memorising](#part-15--numbers-worth-memorising)
- [Part 16 — What separates a senior answer from a staff answer](#part-16--what-separates-a-senior-answer-from-a-staff-answer)
- [What to take away](#what-to-take-away)

---

## Part 0 — What "staff level" means when you answer

Before any technical content, understand what is being graded. Interviewers for staff data platform
roles are not primarily checking whether you know what a `MergeTree` is. They are checking five
things, and you can lose the loop while being technically correct on every fact.

### 0.1 They are testing whether you ask for the numbers before designing

A senior engineer hears "design an ingestion pipeline" and starts drawing boxes. A staff engineer
hears it and says: *how many events per second, what is the peak-to-average ratio, how big is an
event, how long must it be retained, how fresh must it be, and who reads it?* Those six numbers
determine essentially the entire design. If you skip them you will design something plausible and
wrong.

Concretely: a pipeline for 5,000 events/sec and a pipeline for 500,000 events/sec are not the same
system with a bigger instance. At 5,000 events/sec a single Postgres table with a `BRIN` index will
serve you for years. At 500,000 events/sec you need batching, partitioning, a columnar store, and a
tiering policy, and the cost of getting the sort key wrong is a six-month migration.

**Say this in the interview, verbatim:** "Before I design anything, can I get six numbers from you?
Events per second at peak and average, average event size, required freshness, required retention,
number of tenants, and the top-1 tenant's share of volume." If the interviewer says "you tell me,"
state your assumptions explicitly and *write them down* — then design against them.

### 0.2 They are testing whether you separate the control plane from the data plane

This is the single most reliable marker of staff-level thinking in platform work, and it applies to
data platforms exactly as much as it applies to Kubernetes or service meshes.

- The **data plane** is the part that touches every record: the ingest workers, the ClickHouse
  shards, the S3 objects, the query path. It must be fast, dumb, and horizontally scalable. It
  should have almost no policy logic in it.
- The **control plane** is the part that decides *what the data plane should do*: which tenant maps
  to which shard, what the retention policy is, what the schema is, which columns are PII, who may
  read what, what the current SLO is. It touches metadata only, is low-volume, and must be strongly
  consistent and auditable.

When you keep those separate, changing a retention policy is a row update in Postgres that the data
plane picks up. When you don't, changing a retention policy is a code deploy and a cron-script edit
on 40 machines — which is exactly the legacy system you'll be asked to migrate off in Part 3.

Concretely in our stack: **Postgres is the control plane. ClickHouse and S3 are the data plane.**
Say that out loud early in any design answer; it organises everything that follows.

### 0.3 They are testing whether you can name what you would give up

Every staff-level design question has no correct answer, only a defensible trade. The failure mode is
answering as if there were a correct answer. The fix is a fixed verbal pattern:

> "I'd choose X. The cost of that is Y. I'd accept Y because Z. If Z stopped being true — say,
> if the compliance team required W — I'd switch to the alternative, and the switch would cost
> roughly N weeks because of M."

That pattern demonstrates four separate things: a decision, awareness of its cost, the condition
under which it's right, and a reversibility estimate. Senior engineers usually produce the first;
staff engineers produce all four.

### 0.4 They are testing whether you think about migration and the people affected

Almost nothing at staff level is greenfield. The interview questions in this document are heavily
weighted toward *changing a running system without breaking its consumers*, because that is the
actual job. When you propose an architecture, immediately follow it with: how do we get from what
exists to this, in what order, and what happens to the 40 customers currently reading the old thing?

### 0.5 They are testing operational realism

If your design has no answer for "it's 3am and ingestion lag is 40 minutes and growing, what do you
look at first," you have designed a diagram, not a system. Every part of this document therefore
includes at least one incident-shaped question.

---

## Part 1 — The running system: "Skyline"

Everything in this document is set at a fictional company. Learn this system; it is the substrate for
every answer.

### 1.1 What the company does

Skyline sells a DNS security product. Customers deploy resolvers (physical appliances or virtual
ones in their own cloud) inside their networks. Every DNS query those resolvers answer is logged and
shipped to Skyline. Skyline scores each query against threat intelligence — is `login-paypa1.com` a
phishing domain? — and gives the customer dashboards, alerting, forensic search, and a monthly
compliance report.

That means Skyline is a data platform company whether it wants to be or not: the product *is* the
query path over telemetry.

### 1.2 The numbers, derived

Let's build the numbers from the ground up rather than asserting them, because in an interview you
will need to do exactly this out loud.

**Event rate.** Skyline has 12,000 paying tenants. A typical mid-size tenant has 4,000 employees, and
an enterprise endpoint generates roughly 3 DNS queries per second during working hours once you
count browsers, background sync, telemetry agents, and OS chatter. But not all 4,000 endpoints are
active at once — call it 40% concurrency during business hours:

```
4,000 endpoints × 40% active × 3 queries/sec ≈ 4,800 events/sec for one mid-size tenant
```

Most tenants are far smaller. Aggregating across the whole customer base, Skyline measures:

- **Average ingest rate: 150,000 events/sec**
- **Peak ingest rate: 600,000 events/sec** (a 4× peak-to-average ratio, driven by timezone overlap —
  09:00 US Eastern is the global peak because it overlaps European afternoon)

Per day, at the average rate:

```
150,000 events/sec × 86,400 sec/day = 12,960,000,000 events/day ≈ 13 billion events/day
```

**Event size.** A raw event arrives as JSON with about 22 fields: timestamp, tenant ID, site ID,
resolver ID, client IP, query name, query type, response code, response IPs, latency, policy verdict,
threat category, and so on. Measured average: **400 bytes of raw JSON**.

```
13e9 events/day × 400 bytes = 5.2 TB/day of raw JSON
```

That 5.2 TB/day is the number that kills naive designs. It is also the number that makes columnar
storage non-optional, because:

**Compressed size.** DNS telemetry compresses extraordinarily well. The same query name repeats
millions of times a day; response codes have 6 distinct values; tenant IDs have 12,000 distinct
values in a stream of 13 billion rows. Stored column-by-column in ClickHouse with `LowCardinality`
on the categorical columns and `ZSTD(1)` on the rest, Skyline measures an effective **37 bytes per
row on disk**:

```
13e9 rows/day × 37 bytes = 481 GB/day in ClickHouse
```

That is a compression ratio of 5.2 TB → 481 GB, roughly **10.8:1**. Hold onto that ratio; you will be
asked to justify it, and the justification is "high-cardinality-in-aggregate but low-cardinality-per-
column data, sorted so that similar values are adjacent."

**Hot storage sizing.** Skyline keeps 30 days queryable at full fidelity in ClickHouse:

```
481 GB/day × 30 days = 14.4 TB compressed, before replication
```

With replication factor 2 (each shard has a replica for availability, not for read scaling):

```
14.4 TB × 2 = 28.8 TB of provisioned storage for the hot tier
```

Spread over 8 shards × 2 replicas = 16 nodes, that's **1.8 TB of data per node**. On `m6i.8xlarge`
class instances (32 vCPU, 128 GB RAM) with 4 TB of gp3, that leaves comfortable headroom for merges,
which need free space equal to the largest part being merged.

**Cold storage sizing.** Everything also lands in S3 as Parquet, retained 400 days for compliance.
Parquet with ZSTD gets roughly 8:1 on this data (worse than ClickHouse because Parquet row groups are
smaller units of compression than ClickHouse's per-column-per-part streams):

```
5.2 TB/day ÷ 8 = 650 GB/day in Parquet
650 GB/day × 400 days = 260 TB in S3
```

At S3 Standard's $0.023/GB-month, 260 TB costs:

```
260,000 GB × $0.023 = $5,980/month
```

That number matters: it's the one you cut with lifecycle policies in Part 11, and cutting it is one of
the easiest wins you'll be asked to find.

### 1.3 The tenant skew, which is the source of most operational pain

Skyline's tenants are not uniform. Measured distribution:

| Tenant cohort | Count | Share of total events | Events/sec each (avg) |
| --- | --- | --- | --- |
| Whale (largest single tenant, "Meridian Financial") | 1 | 18% | 27,000 |
| Top 20 (including Meridian) | 20 | 61% | ~4,600 |
| Mid-market | 2,100 | 33% | ~24 |
| Long tail | 9,879 | 6% | ~1 |

Read that table twice. **One tenant is 18% of your traffic, and 9,879 tenants are 6% of it.** Nearly
every multi-tenancy question in Part 6 is downstream of this skew. Any design that treats tenants
uniformly will either over-provision 9,879 times or fall over when Meridian has an incident.

### 1.4 The control plane: what lives in Postgres

Postgres holds everything *about* the data, and nothing that scales with event volume:

- `tenants` — 12,000 rows: name, region, tier, contract dates, data residency requirement.
- `sites`, `resolvers` — 240,000 rows: which appliance belongs to which tenant, its version, its key.
- `policies`, `policy_rules` — 1.4M rows: allow/deny lists, category blocks. Read constantly by the
  enforcement path, written by customer admins.
- `users`, `api_keys`, `roles`, `grants` — access control.
- `saved_queries`, `alert_definitions`, `report_schedules` — 90,000 rows.
- `retention_policies` — **one row per tenant per dataset**, which is how the control plane governs
  the data plane's lifecycle. This table is the whole point of Part 7.
- `schema_registry`, `dataset_catalog`, `lineage_edges` — the metadata that makes governance possible.

Total: about **40 GB**, peak **800 transactions/sec**, of which 95% are reads. This is a small
Postgres. It is also the most important database at the company, because if it's wrong, the data
plane does the wrong thing to 13 billion rows a day.

### 1.5 The legacy system being replaced: "Argus"

Skyline has been running for nine years. The original pipeline, "Argus," looks like this:

```
resolvers → syslog over TCP → 30 Logstash VMs → Elasticsearch (7-day hot)
                                    ↓
                            nightly HDFS dump → Hive tables (partitioned by dt)
                                    ↓
                         cron: `hive -e "ALTER TABLE ... DROP PARTITION"` + `hdfs dfs -rm -r`
```

Everything about Argus's *data lifecycle management* — retention, deletion, archival, tiering — is a
set of 14 cron jobs on 3 bastion hosts, written between 2017 and 2021 by people who have left.
Retention is encoded as constants inside those scripts. Nobody knows, without reading the scripts,
what the actual retention of any dataset is. There is no record of what was deleted or when.

**Argus's data consumers, all of whom must not break:**

1. The customer-facing Reports UI (reads Elasticsearch for 7 days, Hive for older).
2. Six internal teams with direct Hive access and ~400 saved queries between them.
3. The ML feature pipeline that builds training data for the threat-scoring model.
4. A nightly threat-intel batch job that scans 24 hours of queries against new indicators.
5. **40 enterprise customers with direct read access** to a shared Hive metastore and a nightly S3
   export in a customer-owned bucket. These are contractual. Breaking them is a legal event, not an
   engineering one.
6. The billing pipeline, which counts events per tenant per month. If this is wrong, invoices are
   wrong.

Consumer #5 and #6 are the ones that make this hard. Remember them.

### 1.6 The compliance surface

Skyline sells to regulated buyers, which constrains the architecture in ways that are not negotiable:

- **US Federal customers** require FedRAMP Moderate. That means a separate AWS GovCloud deployment,
  FIPS 140-2 validated endpoints, and no data crossing from the commercial region.
- **EU customers** require data residency: EU tenant data must be stored and processed in
  `eu-central-1` and must not transit US infrastructure.
- **GDPR** gives EU data subjects a right to erasure. DNS logs contain client IPs, which are personal
  data under GDPR. This collides head-on with immutable archival storage — see Part 6.
- **Three customers are on contracts requiring 7-year retention** of their query logs with tamper
  evidence (WORM). This collides head-on with the right to erasure. Also Part 6.
- **SOC 2 Type II** requires evidence that access controls work, which means every read of tenant
  data must be attributable to a principal and logged.

### 1.7 The team and the mandate

You are the incoming staff engineer. The mandate you were hired against is exactly the bullet list
this document covers: build the new ingestion platform, migrate Argus's lifecycle management onto it
without breaking consumers, establish SLIs/SLOs, make the storage layer serve transactional +
analytic + ML workloads, deliver a compliant multi-tenant model, and connect the old world to the new
through interfaces that don't leak.

Six engineers report into the effort. There is a 4-quarter runway. Now let's go through the areas.

---

## Part 2 — Designing and delivering the ingestion pipeline

### 2.1 What the interviewer is actually testing

Ingestion questions look like they're about tools ("Kafka or Kinesis?"). They are not. They are about
whether you understand four things:

1. **Where durability begins.** At what exact point in the pipeline can you tell a customer "your
   data is safe"? Everything before that point can lose data on a process crash; everything after it
   can be replayed. Engineers who can't name this point have designs that lose data silently.
2. **How you handle the impedance mismatch between "many small writes" and "columnar stores want few
   large writes."** This is *the* ClickHouse ingestion problem, and it's the difference between a
   cluster that runs for years and one that dies of "too many parts" in week three.
3. **Whether your pipeline is idempotent**, because it will be replayed, and a replay that
   double-counts is worse than no replay.
4. **Backpressure.** What happens when the sink is slower than the source? Systems without a designed
   answer discover their accidental one during an incident.

### 2.2 The mental model, built from the naive design outward

Let's design Skyline's ingestion the wrong way first, so the constraints reveal themselves.

**Attempt 1: resolvers POST directly to ClickHouse.**

Each resolver batches 100 events and POSTs them to a load balancer in front of ClickHouse, which does
an `INSERT`. Simple, no moving parts, low latency.

Now compute what ClickHouse experiences. Skyline has 240,000 resolvers. At 150,000 events/sec total,
each resolver produces about 0.6 events/sec, so it fills a 100-event batch every ~160 seconds. That's
240,000 ÷ 160 = **1,500 INSERT statements per second**, each carrying 100 rows.

Here is why that destroys the cluster. In ClickHouse's `MergeTree` engine, **every INSERT creates a
new immutable directory on disk called a "part."** A part contains the rows of that insert, sorted by
the table's `ORDER BY` key, with one file per column plus index files. A background process merges
small parts into bigger ones over time — that's the "Merge" in MergeTree.

So 1,500 inserts/sec creates 1,500 new directories per second. The merge scheduler cannot keep up:
merging is O(rows) and disk-bandwidth-bound, and each merge produces a new part that must itself be
merged again later. Parts accumulate. ClickHouse has explicit guardrails for this — when a single
partition exceeds `parts_to_delay_insert` (default 150) it starts artificially sleeping your inserts,
and when it exceeds `parts_to_throw_insert` (default 300) it rejects them outright with the famous
error:

```
DB::Exception: Too many parts (326). Merges are processing significantly slower than inserts.
```

You will hit that in **under a minute**. So the first hard constraint appears:

> **ClickHouse wants few, large inserts.** The working target is roughly **one insert per second per
> table, carrying 10,000–100,000 rows or 10–100 MB.** Everything upstream exists to make that true.

**Attempt 2: put a buffer in front.**

If the sink wants 1 insert/sec of 150,000 rows, something must accumulate 150,000 rows. Three places
you could do it, and the choice is instructive:

*(a) In ClickHouse itself, with the `Buffer` table engine.* A `Buffer` table holds rows in RAM and
flushes to a destination table on size/time thresholds. It works, and it's one line of DDL. It also
**loses everything in RAM if the node restarts**, doesn't participate in replication, and behaves
oddly with `FINAL` and mutations. Skyline cannot use it as the durability point. It is fine as a
micro-optimisation *behind* a durable buffer, not instead of one.

*(b) In ClickHouse's async insert mechanism.* Setting `async_insert = 1` makes the server accumulate
incoming small inserts into a shared in-memory buffer per table and flush them together. With
`wait_for_async_insert = 1` the client's INSERT doesn't return until the buffer is actually flushed to
disk, which restores durability at the cost of latency. This is genuinely useful and much better than
`Buffer`, and modern ClickHouse deployments lean on it heavily. But it still puts the coordination
burden on the database, and it gives you no replay: if a downstream transform is wrong, the data is
already in the table.

*(c) In a log-structured broker — Kafka.* Producers append to partitions; the broker fsyncs and
replicates; consumers read at their own pace and track offsets. This is the answer for Skyline, and
the reason is not "Kafka is the standard." The reason is **replay**.

**Why replay is the deciding argument.** Over four years, Skyline will change how it parses events,
change how it enriches them with threat intelligence, discover a bug that mis-categorised 3 days of
traffic, add a new derived column, and migrate the ClickHouse schema twice. Every one of those is a
"reprocess the last N days" event. If your durable copy is inside ClickHouse, reprocessing means
reading out of ClickHouse, transforming, and writing back — while serving queries. If your durable
copy is a log with 7 days of retention plus Parquet in S3 forever, reprocessing means pointing a new
consumer group at an offset. The second one is a routine, low-risk operation you can do on any given
day; the first one is a multi-week undertaking with its own risk of interfering with live queries.

So state the durability point explicitly:

> **Durability begins when Kafka acknowledges the produce with `acks=all`.** Before that, the resolver
> owns the data and must retry from its local spool. After that, Skyline owns it and can always
> rebuild any downstream state.

### 2.3 The actual pipeline

```
                              ┌──── control plane (Postgres) ─────┐
                              │ schema registry, tenant routing,  │
                              │ retention policy, quotas          │
                              └───────────────┬───────────────────┘
                                              │ (config, read at startup + on change)
                                              ▼
resolvers ──HTTPS/gRPC──► collector tier ──► Kafka ──► stream processor ──┬──► ClickHouse (hot, 30d)
  (local spool,           (auth, validate,   (7-day    (parse, enrich,    │
   at-least-once)          shard, batch)      retain,   dedupe, route)    └──► S3 Parquet (cold, 400d)
                                              RF=3)                              via Iceberg tables
```

Walk each stage, because each one exists for a specific reason and an interviewer will ask "why is
that there?" for every box.

**Resolvers (edge).** Each appliance writes events to a local on-disk spool with a size cap (say
2 GB, ~5M events) and ships them over gRPC with backpressure-aware streaming. The spool is what makes
the system survive a Skyline outage: if the collectors are down for 20 minutes, a 4,800 events/sec
tenant buffers 5.7M events, which fits. If Skyline is down for 6 hours, that tenant's spool overflows
and it drops oldest-first — a decision you should make deliberately and document, because the
alternative (drop newest) is worse for security forensics where recent data matters most.

Each event is stamped at the edge with an **`event_id`**: a UUIDv7 (time-ordered UUID), plus the
`resolver_id` and a monotonic per-resolver sequence number. This triple is what makes end-to-end
deduplication possible later. Generating identity at the edge, not in the pipeline, is the key move;
if the pipeline generates IDs, a retry creates a new ID and dedup becomes impossible.

**Collector tier.** Stateless Go services behind an NLB, autoscaled on CPU. Responsibilities, in
order:

1. **Authenticate** the resolver via mTLS client cert; map cert → `resolver_id` → `tenant_id` from a
   cache of the Postgres control plane (refreshed every 30s, with a 10-minute stale-serve grace so a
   Postgres outage doesn't stop ingestion).
2. **Validate** against the registered schema version for that tenant. Reject structurally invalid
   events with a 400 so the resolver doesn't retry forever; route semantically suspect ones to a dead
   letter topic rather than dropping them.
3. **Enforce quota.** Per-tenant token bucket. Meridian Financial at 27,000 events/sec is fine;
   Meridian Financial at 400,000 events/sec because of a DNS amplification event in their network is
   not, and the collector is where you stop it — before it becomes everyone's problem.
4. **Produce to Kafka** with `acks=all`, `enable.idempotence=true`, keyed by `tenant_id`.
5. **Acknowledge to the resolver** only after the Kafka ack. This is the durability handoff.

Note what the collector does *not* do: no enrichment, no threat-intel lookup, no writes to ClickHouse.
Keeping the collector free of dependencies means the only thing that can stop ingestion is Kafka
being down.

**Kafka.** 3 brokers minimum for RF=3, sized on throughput rather than storage. At peak:

```
600,000 events/sec × 400 bytes = 240 MB/sec ingress
× 3 replicas = 720 MB/sec of replication write bandwidth
```

Retention of 7 days at *average* rate:

```
150,000/sec × 400 bytes × 86,400 × 7 = 36.3 TB, × 3 replicas = 109 TB
```

That's a lot of broker disk, which is why you compress on the producer (`compression.type=zstd`,
roughly 5:1 on this payload → ~22 TB replicated) and why 7 days is a deliberate choice: it's long
enough to cover a weekend outage plus a Monday morning fix, and short enough to be affordable. S3 is
where "forever" lives.

**Partitioning is the subtle decision.** Keying by `tenant_id` gives per-tenant ordering, which you
want, but it also means Meridian Financial's 18% of traffic lands on **one partition**. With 64
partitions, uniform hashing would give each 1.6% of traffic; Meridian gives one partition 18%. That
partition's consumer becomes the bottleneck and lags while 63 others idle.

The fix is a **composite key with tenant-aware fan-out**: key = `tenant_id` for normal tenants, and
`tenant_id:{0..15}` (a random suffix into 16 sub-buckets) for tenants above a volume threshold, with
the threshold and the fan-out factor stored in the Postgres control plane. You lose strict per-tenant
ordering for whale tenants; you keep per-`(tenant, sub-bucket)` ordering. Since Skyline's processing
is order-independent (each event is enriched and stored independently), that's a cheap trade — but
say it out loud, because if the processing *were* order-dependent (e.g. session reconstruction) it
would not be.

This is a specific instance of a general technique worth naming in interviews: **shuffle sharding /
key salting for skewed keys**.

**Stream processor.** A consumer group of Go workers (or Flink, discussed below). Per batch:

1. Read up to 100,000 records or 5 seconds, whichever first.
2. Parse and normalise into the columnar row layout.
3. Enrich: threat-intel category from an in-memory RocksDB snapshot refreshed every 5 minutes;
   GeoIP; tenant metadata from the control-plane cache.
4. Deduplicate within the batch on `event_id`.
5. Write to ClickHouse in one INSERT and to an S3 Parquet buffer.
6. Commit Kafka offsets **only after both writes succeed.**

That last ordering gives at-least-once delivery: a crash between write and commit replays the batch.
Which brings us to the question that separates good from great.

### 2.4 The exactly-once question, answered honestly

**Q: You have at-least-once from Kafka. ClickHouse has no transactions. How do you avoid double
counting?**

The honest staff answer is: *you don't get exactly-once delivery; you get at-least-once delivery plus
idempotent effects, and that's indistinguishable from exactly-once at the query layer.* There are
three mechanisms and you should know all three, because interviewers probe which one you reach for.

**Mechanism 1 — ClickHouse's own block-level deduplication.** For `Replicated*MergeTree` tables,
ClickHouse hashes each inserted block and stores the hash in ClickHouse Keeper. If the same block
hash arrives again (default: within the last 100 blocks, configurable via
`replicated_deduplication_window`), the insert is silently ignored. This means **a retried insert of
the byte-identical batch is free**. It's automatic and it's the reason your stream processor must
retry with the *same* batch, not a re-read batch that might have different boundaries.

You can make this explicit and much more reliable with `insert_deduplication_token`: set it to a
deterministic value like `"{topic}-{partition}-{start_offset}"`, and ClickHouse dedupes on that token
instead of the content hash. Now the batch boundaries can shift and dedup still works. This is the
single highest-leverage ingestion setting most teams don't know about, and naming it is a strong
signal.

**Mechanism 2 — `ReplacingMergeTree` on `event_id`.** Store the table as
`ReplacingMergeTree(ingested_at)` with `ORDER BY (tenant_id, toStartOfHour(ts), event_id)`. Duplicates
of the same `event_id` collapse during background merges, keeping the row with the highest
`ingested_at`.

**There is a trap here that is worth an entire memory of its own.** In `ReplacingMergeTree`, `ORDER BY`
is not merely a query-performance hint — **it is the definition of "the same row."** During a merge,
rows are sorted by the `ORDER BY` tuple and only rows whose *entire tuple is equal* are collapsed. So
if you put a column that changes across versions of the same logical row into `ORDER BY`, dedup
silently stops working. Concretely, if you wrote:

```sql
-- BROKEN: policy_verdict is mutable across re-processing
ENGINE = ReplacingMergeTree(ingested_at)
ORDER BY (tenant_id, toStartOfHour(ts), event_id, policy_verdict)
```

then a re-enrichment that changes `policy_verdict` from `'allow'` to `'block'` produces
`(t, h, e, 'allow')` and `(t, h, e, 'block')` — different tuples, never collapsed, both kept forever.
No error, no warning, and `OPTIMIZE TABLE ... FINAL` won't help because the keys genuinely differ.
**Only immutable-per-entity columns belong in `ORDER BY`.** Mutable state columns live outside it, and
the version column decides which state wins.

Second caveat: merges are lazy and background. Two rows with the same key can coexist for hours. So
reads must either use `FINAL` (correct, but slower — it merges on the fly at query time) or use the
`argMax` idiom:

```sql
SELECT tenant_id, event_id, argMax(policy_verdict, ingested_at) AS verdict
FROM dns_events
WHERE tenant_id = 4471 AND ts >= now() - INTERVAL 1 HOUR
GROUP BY tenant_id, event_id
```

**Mechanism 3 — make the query tolerant.** For the *billing* count, which must be exact, don't count
raw rows at all. Maintain a separate `AggregatingMergeTree` table fed by a materialized view that
uses `uniqExactState(event_id)` per `(tenant_id, day)`. Counting distinct event IDs is idempotent by
construction: replaying the same events doesn't change the distinct count. It costs more memory than
`count()`, but billing correctness is worth it, and at 13B events/day across 12,000 tenants the
per-tenant-per-day cardinality is manageable.

**The answer to give:** "I'd use at-least-once with `insert_deduplication_token` derived from the
Kafka offset range as the primary mechanism, `ReplacingMergeTree` on `event_id` as the safety net for
cases where batch boundaries change, and for billing specifically I'd count `uniqExact(event_id)` in
a pre-aggregated table so the count is idempotent regardless. I would not claim exactly-once
delivery, because ClickHouse has no cross-system transaction — I'd claim at-least-once delivery with
idempotent effects."

### 2.5 Tier 1 questions — screening

**Q2.1: Walk me through what happens to a single DNS event from the resolver to a dashboard.**

*Model answer:* Trace it with a real event. At 09:14:22.310 UTC, a laptop at Meridian Financial
resolves `cdn.example.com`. Meridian's resolver answers it and writes an event with
`event_id = 018f2a1c-...` (UUIDv7, so it sorts by time), `resolver_id = r-88213`, `seq = 4419002`, to
its local spool. Within 200ms the shipper picks it up in a batch of ~500 and streams it over mTLS
gRPC to the nearest collector.

The collector authenticates the client cert, maps `r-88213 → tenant 4471`, validates the event against
schema version 7 registered for that tenant, checks the token bucket (Meridian's is 40,000/sec, they
are at 27,000, fine), and produces to Kafka topic `dns.raw` partition 11, keyed
`4471:7` (Meridian is a whale, so the key is salted). Kafka replicates to 3 brokers and acks. The
collector acks the resolver, which deletes the event from its spool. **The event is now durable and
Skyline owns it.** Elapsed: ~350ms.

A stream processor in consumer group `cg-hot` reads it as part of a 100,000-record batch, enriches it
(threat-intel says `cdn.example.com` is category `content-delivery`, risk 0), writes the batch into
ClickHouse with `insert_deduplication_token = 'dns.raw-11-88301442-88401442'`, and writes the same
batch to an in-progress Parquet file. On success it commits offset 88401442.

ClickHouse writes the batch as one part under partition `202609`, sorted by
`(tenant_id, toStartOfHour(ts), query_name, event_id)`. A materialized view fires on that insert and
updates the per-tenant-per-minute rollup table. Elapsed since the query: ~4 seconds.

The dashboard, refreshing every 30 seconds, queries the *rollup* table — not the raw table — for
"top blocked domains, last 24 hours, tenant 4471," which is a 1,440-row scan instead of a 2.3-billion-
row scan. It renders. Elapsed end-to-end: **under 10 seconds at p50, and the SLO is 60 seconds at
p99**, which we'll define properly in Part 4.

Separately, every 5 minutes the Parquet writer closes its current file at ~256 MB, uploads it to
`s3://skyline-lake/dns_events/tenant_bucket=03/dt=2026-09-06/hour=09/part-000119.parquet`, and commits
it to the Iceberg table so analytics engines see it atomically.

**Q2.2: Why Kafka and not Kinesis or SQS?**

*Model answer:* The requirement that decides it is replay, and secondarily throughput economics.

SQS is out immediately: it's a queue, not a log. Messages are consumed and gone; you cannot rewind to
reprocess three days of data with a new enrichment. SQS is right for task distribution, wrong for a
data backbone.

Kinesis Data Streams is a log and does support replay within its retention (up to 365 days now). It's
a genuine option and it's operationally cheaper — no brokers to run. Two things push me to Kafka for
Skyline specifically. First, **cost at this throughput**: Kinesis charges per shard-hour plus per PUT
payload unit (25 KB each). At 600,000 events/sec × 400 bytes, that's 240 MB/sec; a shard takes 1
MB/sec in, so ~240 shards minimum with zero headroom, realistically 400. At roughly $0.015/shard-hour
that's about $4,300/month in shard hours alone before PUT charges, which at 600K records/sec is
another very large number. Self-managed Kafka on ~9 `i3en` instances is a fraction of that. Second,
**multi-cloud**: Skyline runs a GCP secondary. Kafka runs identically in both; Kinesis doesn't exist
in GCP, so I'd be maintaining a Kinesis path and a Pub/Sub path with different semantics.

I'd flip that answer if throughput were 10× smaller or if the team had no Kafka operational
experience — the honest cost of self-managed Kafka is roughly one engineer's ongoing attention, and
below some volume MSK or Kinesis is straightforwardly better. I'd also seriously evaluate MSK or
Confluent Cloud here to get Kafka semantics without the broker toil, and would probably start there
and only self-manage if the bill justified it.

**Q2.3: What's your batching policy into ClickHouse and why those numbers?**

*Model answer:* Flush on whichever comes first: **100,000 rows, 64 MB, or 2 seconds.** Derivation:

The lower bound comes from ClickHouse's part economics. I want at most ~1 insert/sec per table per
shard so the merge scheduler keeps up. With 8 shards and 150,000 events/sec average, each shard
receives ~18,750 events/sec, so a 2-second flush yields ~37,500 rows per part — comfortably in the
10K–100K sweet spot. At peak (600K/sec) the row trigger fires first, at 100,000 rows every ~1.3
seconds. Both regimes stay near one insert per second per shard.

The upper bound comes from memory and blast radius: a 64 MB batch × N concurrent writers must fit in
the processor's heap, and a failed insert re-processes at most 2 seconds of data.

The time trigger exists so that a low-volume tenant's data isn't stuck waiting for a row count that
takes an hour to reach — which matters because our freshness SLO is per-tenant, not global.

**Q2.4: How do you handle a poison message?**

*Model answer:* Distinguish two failure classes, because conflating them is how pipelines stall.

*Structural failures* — malformed JSON, a field with a value that can't be coerced into the column
type, an event whose `tenant_id` doesn't exist. These are deterministic: retrying will fail forever.
Route them to a `dns.dlq` topic with the original bytes plus the exception and the consumer offset,
increment a per-tenant `dlq_events_total` metric, and **continue**. Alert when the DLQ rate for any
tenant exceeds 0.01% of its volume over 15 minutes, because that usually means a customer upgraded
their appliance to a version we haven't registered a schema for.

*Transient failures* — ClickHouse returning `TOO_MANY_PARTS`, an S3 503, a network timeout. These are
not the message's fault. Retry with exponential backoff and jitter, and **do not** advance the offset.
If retries exhaust, the consumer should stop and page rather than skip, because skipping silently
loses data. The distinction is enforced by an explicit error classifier, not by a generic
`catch (Exception e) { dlq.send(e) }` — a generic catch is how you send 40 minutes of good data to the
DLQ during a ClickHouse restart.

Finally, the DLQ must be *drainable*: a tool that re-reads the DLQ, applies a fixed parser, and
replays into the main pipeline. A DLQ nobody can drain is a data loss with extra steps.

### 2.6 Tier 2 questions — design

**Q2.5: Design the ClickHouse table for `dns_events`. Justify every clause.**

*Model answer:*

```sql
CREATE TABLE dns_events ON CLUSTER skyline
(
    tenant_id        UInt32           CODEC(T64, ZSTD(1)),
    ts               DateTime64(3)    CODEC(Delta, ZSTD(1)),
    event_id         UUID             CODEC(ZSTD(1)),
    resolver_id      UInt32           CODEC(T64, ZSTD(1)),
    site_id          UInt32           CODEC(T64, ZSTD(1)),
    client_ip        IPv6             CODEC(ZSTD(1)),
    query_name       LowCardinality(String) CODEC(ZSTD(1)),
    query_name_full  String           CODEC(ZSTD(3)),
    query_type       LowCardinality(String),
    response_code    LowCardinality(String),
    response_ips     Array(IPv6)      CODEC(ZSTD(1)),
    latency_us       UInt32           CODEC(T64, ZSTD(1)),
    policy_verdict   LowCardinality(String),
    threat_category  LowCardinality(String),
    threat_score     UInt8,
    ingested_at      DateTime         CODEC(Delta, ZSTD(1)),

    INDEX idx_threat threat_category TYPE set(64) GRANULARITY 4,
    INDEX idx_client client_ip       TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/dns_events', '{replica}', ingested_at)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (tenant_id, toStartOfHour(ts), query_name, event_id)
TTL ts + INTERVAL 7 DAY  TO VOLUME 'warm',
    ts + INTERVAL 30 DAY TO VOLUME 's3_cold',
    ts + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
```

Now justify, clause by clause — this is where the interview actually happens.

**`ORDER BY (tenant_id, toStartOfHour(ts), query_name, event_id)`.** This is the most consequential
line in the schema, and it's doing three jobs at once.

*Job 1: it's the primary index.* ClickHouse's primary index is **sparse**: it stores one entry per
`index_granularity` rows (8,192 by default), mapping the sort-key value at that row to a mark in the
data files. So the index for a 2.3-billion-row partition has 2.3e9 ÷ 8192 ≈ 280,000 entries — small
enough to hold in memory. A query with `WHERE tenant_id = 4471` binary-searches that index and reads
only the granules that could contain tenant 4471. **Leading with `tenant_id` means every tenant-scoped
query — which is every customer-facing query — skips ~99.99% of the data.** If you led with `ts`
instead, a tenant query over 24 hours would have to scan all 12,000 tenants' rows for those hours.

*Job 2: it determines compression.* Columnar compression works on runs of similar adjacent values.
Sorting by `tenant_id` then hour then `query_name` means `query_name` values are clustered: a million
consecutive rows might all be `cdn.example.com`. `LowCardinality` turns that into a dictionary index,
and ZSTD then compresses long runs of the same index to almost nothing. **The sort key is why we get
10.8:1 instead of 3:1.**

*Job 3 (because this is `ReplacingMergeTree`): it defines row identity for dedup.* Every column in
that tuple must be immutable for a given logical event. `tenant_id`, `toStartOfHour(ts)`,
`query_name`, and `event_id` all are. `policy_verdict` deliberately is **not** in the key, because
re-enrichment can change it — and as shown in §2.4, putting a mutable column in `ORDER BY` silently
breaks deduplication forever.

Why `toStartOfHour(ts)` rather than `ts` itself? Because full-millisecond timestamps are nearly unique,
so including raw `ts` would make the third and fourth key columns almost useless for clustering —
every row would be its own run. Truncating to the hour keeps rows from the same hour adjacent so that
`query_name` can actually cluster within them, while still letting the index prune by time. You keep
the exact `ts` as a regular column for filtering; ClickHouse can still use it as a "monotonic function
of a key column" for pruning.

**`PARTITION BY toYYYYMMDD(ts)`.** Partitions are the unit of TTL, freeze, drop, and `ALTER ... DROP
PARTITION`. Daily partitions × 90-day retention = 90 partitions per shard, which is a healthy number.
The trap in the other direction is real: partitioning by hour would give 2,160 partitions, and because
merges never combine parts across partitions, you get 24× more parts and 24× more merge pressure for
no query benefit (the `ORDER BY` already prunes by time). **Do not partition by tenant_id** — 12,000
partitions × 90 days = 1.08M partitions is a cluster-killing number, and I'll come back to why in
Part 6, because it's the most common wrong answer to "how do you isolate tenants in ClickHouse."

`ttl_only_drop_parts = 1` tells ClickHouse to drop whole parts when every row in them has expired,
instead of rewriting parts to remove individual expired rows. Since our partitions align with the TTL
boundary, expiry becomes a directory delete rather than a rewrite of terabytes.

**Codecs.** `Delta` before `ZSTD` on `ts` and `ingested_at`: consecutive timestamps in a sorted part
differ by microseconds, so storing the differences turns 8-byte values into 1–2 byte deltas that ZSTD
then crushes. `T64` on the integer columns transposes 64-bit integers bitwise so that the high-order
bits (which are almost all zeros for small IDs) form long zero runs. These aren't cosmetic — on
Skyline's data, switching `ts` from plain `ZSTD` to `Delta, ZSTD` cut that column by about 6×.

**`LowCardinality(String)`** on `query_type` (about 15 distinct values), `response_code` (6),
`policy_verdict` (4), `threat_category` (~120). ClickHouse stores a per-part dictionary and the rows
hold small integer indices. The rule of thumb is: use it under roughly 10,000 distinct values, and
*don't* use it above ~100,000, where the dictionary overhead makes things worse.

Note the deliberate split between `query_name` (LowCardinality) and `query_name_full` (plain String).
`query_name` holds the eTLD+1 (`example.com`) — a few million distinct values globally but only a few
thousand per tenant-hour, so LowCardinality's per-part dictionary stays small. `query_name_full` holds
the complete FQDN including subdomains, which is genuinely high-cardinality (DNS tunnelling generates
unique subdomains by design) and must not be LowCardinality.

**Skip indexes.** These are "data skipping" indexes: for each block of `GRANULARITY × index_granularity`
rows they store a summary, and a query that can't match the summary skips those rows entirely without
decompressing them. `set(64)` on `threat_category` stores up to 64 distinct values per block, so
`WHERE threat_category = 'cryptomining'` skips blocks that don't contain it. `bloom_filter(0.01)` on
`client_ip` supports forensic "show me everything this endpoint did" queries with a 1% false-positive
rate. **Skip indexes only help when the data is clustered relative to the filter** — a bloom filter on
a column whose values are uniformly scattered across every block skips nothing and costs you write
throughput. Measure with `EXPLAIN indexes = 1` before adding one.

**TTL with volume moves.** This is the tiering mechanism, and it's how ClickHouse and S3 combine. The
cluster is configured with a storage policy of three volumes: `hot` (local NVMe), `warm` (gp3 EBS),
and `s3_cold` (an S3-backed disk). Data moves down automatically by age. Queries work identically
across all three — the query planner doesn't care which disk a part is on — but S3-backed parts are
100–200× slower per byte read, which is why the query patterns in Part 5 route long-range queries
away from raw data.

**Q2.6: How do you get data into S3 as Parquet, and why not just export from ClickHouse?**

*Model answer:* You could do `INSERT INTO FUNCTION s3(...) SELECT * FROM dns_events WHERE ...`, and
for one-off exports that's the right tool. As a continuous pipeline it's wrong for three reasons.

First, **it makes ClickHouse a dependency of your archive.** If ClickHouse is unavailable or a
migration corrupts a table, the archive stops or inherits the corruption. The archive's job is to be
the thing you rebuild ClickHouse *from*; it must not depend on it.

Second, **it doubles the read load on the query cluster** at exactly the times you don't want it.

Third, **the archive should be written from the same durable source as everything else**, so that both
sinks are reproducible from Kafka offsets and reconcilable against each other. That reconciliation —
"do ClickHouse and S3 contain the same count for tenant 4471 on 2026-09-05?" — is the strongest data
quality check the platform has, and it only works if both are independent derivations of the log.

So the stream processor writes both. Concretely: a second consumer group `cg-lake` reads the same
topic and buffers to local disk in Parquet, closing a file when it reaches 256 MB or 15 minutes.
Object layout:

```
s3://skyline-lake/dns_events/tenant_bucket=03/dt=2026-09-06/hour=09/part-00119-uuid.parquet
```

`tenant_bucket` is `tenant_id % 64`, not `tenant_id` itself. That's an important detail: partitioning
by raw `tenant_id` would create 12,000 × 24 × 400 = 115 million prefixes, and S3 listing plus Iceberg
manifest overhead makes that miserable. Bucketing to 64 keeps per-tenant pruning (a query for tenant
4471 reads only bucket 3) while keeping the partition count sane.

**Register the files in Apache Iceberg** rather than relying on the directory layout. Iceberg keeps a
manifest of which files belong to which snapshot, so: readers never see a half-written commit
(snapshot isolation); you can time-travel to "the table as of yesterday" for reproducible ML training
sets; schema evolution is a metadata change rather than a rewrite; and deleting one tenant's rows for
GDPR becomes a supported operation instead of a find-and-rewrite exercise. That last point is worth
the whole Iceberg dependency by itself, and I'll return to it in Part 6.

**Q2.7: What's your backpressure story?**

*Model answer:* Backpressure has to be designed at each boundary, because each boundary fails
differently. Walk them from the sink backwards.

*ClickHouse slow → stream processor.* The processor sees insert latency rise or `TOO_MANY_PARTS`. It
must **stop committing offsets** and stop reading. Because Kafka consumers pull, simply not polling
applies backpressure for free — this is the main reason to prefer a pull-based broker. Kafka's
retention absorbs it: at 240 MB/sec peak, 7 days of retention gives roughly 5 hours of buffer even if
we're consuming at zero, which is a huge operational cushion.

*Kafka slow or full → collector.* The producer's `buffer.memory` fills and `send()` blocks (or throws
if `max.block.ms` is exceeded). The collector must translate that into an HTTP 429 / gRPC
`RESOURCE_EXHAUSTED` **rather than buffering in its own heap**, because a collector that buffers is a
collector that OOMs and loses data it already acked. Return the error before acking.

*Collector returns 429 → resolver.* The shipper backs off and the local spool grows. This is where
the data actually waits, which is correct: the edge has the most aggregate storage (240,000 × 2 GB =
480 TB of distributed buffer) and the least contention.

*Spool full → drop.* Oldest-first, with a counter the resolver reports so we can see it. Drops are
visible and attributable, not silent.

The governing principle: **push the queue as close to the source as possible, and make every layer
degrade by refusing work rather than by accumulating it.** The anti-pattern is unbounded in-memory
queues at every hop, which converts a slow sink into a cascading OOM.

One more: **isolate the whale**. Meridian at 18% of volume shares a consumer group with everyone else.
If their traffic spikes, everyone's lag grows. So run a separate consumer group and separate topic for
tenants above a threshold. Now a Meridian incident degrades Meridian's freshness, not everyone's.
That's the ingestion-layer version of cell-based isolation from Part 6.

### 2.7 Tier 3 questions — deep dive and adversarial

**Q2.8: Ingestion lag just went from 3 seconds to 40 minutes and is climbing. Walk me through your
first 15 minutes.**

*Model answer:* First, decide the shape of the problem, because that determines everything. Lag is
`produce_rate - consume_rate` integrated over time, so it's either *more produce* or *less consume*.
One dashboard answers it: Kafka bytes-in/sec versus bytes-out/sec, split by topic.

**Case A — bytes-in spiked.** Look at per-tenant produce rate. In practice this is one tenant. The
classic Skyline version: a customer misconfigures a DNS forwarding loop and generates 30× normal
traffic. Mitigation is at the collector: tighten that tenant's token bucket to their contracted rate.
This is a control-plane change — an `UPDATE` on `tenant_quotas` in Postgres that collectors pick up
within 30 seconds — not a deploy. **Being able to say "this is a config change, not a deploy" is the
payoff of the control/data-plane split, and it's worth pointing out explicitly.**

**Case B — bytes-out dropped.** Now find which stage stalled:
- Consumer group lag per partition. If *one* partition lags and the rest are fine, it's key skew or a
  poison batch on that partition.
- If *all* partitions lag evenly, the sink is slow. Check ClickHouse: `system.merges` for merge
  backlog, `system.parts` count per partition, `system.errors`, and insert latency p99. The signature
  failure is parts-per-partition climbing past 150 (inserts being throttled) toward 300 (inserts
  rejected).
- If ClickHouse is fine, check the processors themselves: CPU saturation, GC pauses, or — very common
  — the threat-intel enrichment dependency. If the RocksDB snapshot refresh failed and the code falls
  back to a synchronous remote lookup per event, throughput collapses by 100×. **Enrichment
  dependencies should be designed to fail open with stale data, never to fail into a slow path.**

**The 15-minute mitigation, before root cause:** buy time and protect the customer-visible path.
1. Confirm Kafka retention headroom — we have ~5 hours, so **no data is at risk**. Say this out loud;
   it converts a panic into a schedule.
2. Scale the consumer group horizontally if partitions allow it (you can't have more consumers than
   partitions — a very common gotcha; with 64 partitions you're capped at 64 consumers).
3. If ClickHouse is the constraint, temporarily increase batch size and reduce insert frequency
   (fewer, bigger parts = less merge pressure), and pause any running mutations or `OPTIMIZE`
   operations, which compete for the same disk bandwidth.
4. If it's one tenant, shift them to the isolated topic/consumer group so their lag stops being
   everyone's lag.
5. Post the freshness SLO status: which tenants are outside the 60-second freshness objective and how
   much error budget the month has left. That's Part 4's machinery earning its keep.

**Q2.9: You need to reprocess the last 3 days because the threat-intel enrichment was wrong. The
platform is live. How?**

*Model answer:* The requirement is: fix 3 days of `threat_category` and `threat_score` without
double-counting, without downtime, and without a period where the dashboard shows a mix of old and new
values in a way that confuses customers.

The naive approach — `ALTER TABLE dns_events UPDATE threat_category = ... WHERE ts > now() - 3 DAY` —
is a **mutation**, and mutations in ClickHouse rewrite every affected part in full. Three days is
~1.4 TB compressed across 8 shards; every part containing any matching row is rewritten completely.
It runs asynchronously, you can't easily monitor progress except via `system.mutations`, it competes
with merges for I/O, and if you get the expression wrong there's no undo. For 1.4 TB, don't.

The approach that works uses the property we built the pipeline for:

1. **Deploy the fixed enrichment** to a *new* consumer group `cg-hot-reproc`, reading the same topic
   from the offset corresponding to 3 days ago. Kafka has 7 days, so the data is there.
2. **Write into a shadow table** `dns_events_v2`, identical schema, not read by anyone. Do not write
   into the live table yet — you want to validate before exposing.
3. **Let it catch up.** Reprocessing 3 days at, say, 4× real-time takes about 18 hours. Meanwhile live
   ingestion continues into `dns_events` untouched. Users see no change.
4. **Validate.** Row counts per tenant per hour must match between the two tables exactly. The
   distribution of `threat_category` should differ only in the expected way — spot-check 50 known-bad
   domains and confirm they now categorise correctly. Diff a sample of 10,000 `event_id`s field by
   field and confirm the *only* differences are the two enrichment columns.
5. **Cut over per partition, not all at once.** For each day-partition, in a single atomic operation:
   `ALTER TABLE dns_events REPLACE PARTITION '20260903' FROM dns_events_v2`. `REPLACE PARTITION` is
   atomic and near-instant — it's a metadata swap of part directories, not a data copy. Do the oldest
   day first, verify dashboards, then proceed.
6. **Keep the old partitions** via `ALTER TABLE ... FREEZE PARTITION` before replacing, which
   hard-links the parts into a `shadow/` directory. That's your rollback: cheap in space (hard links),
   instant to restore.
7. **Re-run the S3/Iceberg side too**, which is easier: write new Parquet files and commit a new
   Iceberg snapshot that replaces the old files for those partitions. Iceberg's snapshot semantics
   mean readers see either the old or the new set, never a mix, and the old snapshot remains for time
   travel until expired.

The thing to emphasise: **the whole plan is possible because the durable source of truth is the log,
not the database.** If ClickHouse were the only copy, step 1 would be impossible and you'd be back to
mutations.

**Q2.10: How do you handle a schema change — say, adding a `dns_over_https` boolean — when 240,000
resolvers upgrade over six weeks?**

*Model answer:* The constraint is that for six weeks you have producers on both versions and you
cannot coordinate their upgrade. So the design rule is: **schema changes must be backward and forward
compatible, and the pipeline must never fail on an unknown field.**

Mechanically:

- **Wire format**: use a schema with defined evolution semantics — Protobuf or Avro with a schema
  registry, not bare JSON. Adding an optional field with a default is a compatible change: old
  consumers ignore it, new consumers see the default when old producers omit it. Register the change
  in the registry with `BACKWARD` compatibility enforced, so an incompatible change is rejected at CI
  time rather than discovered at 3am. (If you're stuck with JSON, the equivalent discipline is:
  additive-only, never repurpose a field name, and unknown fields go into a catch-all map rather than
  causing a parse error.)
- **ClickHouse**: `ALTER TABLE dns_events ADD COLUMN dns_over_https UInt8 DEFAULT 0`. This is a
  *metadata-only* operation for a column with a default — ClickHouse does not rewrite existing parts;
  it synthesises the default at read time until a merge naturally materialises it. So it's instant even
  on 14 TB. Contrast with `ADD COLUMN ... MATERIALIZED <expr>`, which also doesn't rewrite but computes
  per-read, and with adding a column to the `ORDER BY`, which is not possible at all without rebuilding
  the table.
- **Iceberg/Parquet**: Iceberg tracks columns by ID, not by position or name, so adding a column is a
  metadata change and old files are read with the new column as null. This is precisely why Iceberg
  rather than raw Parquet directories — with bare Parquet + Hive-style partitioning, a schema change
  means either rewriting history or having readers that break on files with different schemas.
- **Downstream contracts**: the dashboards and the 40 external customers must not break. Anything they
  read must be a *view*, not the base table, so that we control the projected schema. New column
  appears in the view only when we choose to expose it, with a documented version bump.
- **Rollout observability**: emit `schema_version` as a column and chart the mix. Six weeks in, you can
  see exactly which tenants haven't upgraded, and the "0% on v7" moment is when you're allowed to
  start depending on the new field.

The general principle to state: **never make a schema change that requires simultaneous deployment of
producer and consumer.** Expand, migrate, contract — add the new thing, dual-write/dual-read until
everything is on it, then remove the old thing. Three deploys, never one.

**Q2.11: Two ClickHouse shards are healthy, one is down. What do inserts do, and what do queries do?**

*Model answer:* It depends on choices you make, and the honest answer names the choice rather than
asserting a behaviour.

*Inserts.* Writing through a `Distributed` table with `internal_replication = 1` means the Distributed
table sends the batch to one replica of the target shard and ClickHouse's replication carries it to
the others. If the entire shard (both replicas) is down, the Distributed table's behaviour depends on
`insert_distributed_sync`. In async mode (the default) it spools the batch to local disk on the
initiator node and retries in the background — which sounds nice but means an insert can be
"successful" from the client's view and not be queryable for a long time, and can be lost if the
initiator's disk dies. For Skyline I write **directly to shard-local tables from the stream
processor**, choosing the shard myself, and treat a down shard as a routing decision: fail those
batches, don't commit offsets, and let Kafka hold the data until the shard returns. That gives clean
at-least-once semantics with no hidden spool.

*Queries.* A `Distributed` query fans out to all shards. If a shard is unreachable, the default is to
fail the query. `skip_unavailable_shards = 1` makes it return partial results instead. **That setting
is dangerous and you should say so**: a dashboard that silently returns 7/8 of the data during an
incident tells the customer their traffic dropped 12%, which is worse than an error. My rule is:
customer-facing queries never skip shards; internal exploratory queries may, with a clear banner.
Better still is to expose the shortfall — return the error, and have the UI say "one data region is
temporarily unavailable" rather than showing a wrong number confidently.

**Q2.12: Your stream processor uses 40% of its CPU on JSON parsing. What do you do?**

*Model answer:* First verify the claim with a profile rather than accepting it — but assume it's true,
because it usually is; JSON parsing genuinely dominates naive pipelines.

Options, roughly in order of leverage per unit of effort:

1. **Stop parsing what you don't need.** If 22 fields arrive and 16 are used, a streaming parser that
   extracts only the needed keys (simdjson-style, or Go's `jsoniter` with a field whitelist) avoids
   allocating the rest. Typical win: 2–3×.
2. **Change the wire format.** Protobuf decoding is roughly 5–10× cheaper than JSON parsing and the
   payload is ~40% smaller, which also cuts Kafka bandwidth and storage. The cost is that the 240,000
   resolvers must be upgraded, which takes six weeks and is exactly the migration from Q2.10. Do this
   if the pipeline is going to run for years — it pays back.
3. **Push parsing into ClickHouse.** ClickHouse can ingest `JSONEachRow` directly and its parser is
   extremely fast, vectorised C++. For a pipeline whose only job is parse-and-store, sending raw JSON
   straight to ClickHouse and doing enrichment via materialized views can be dramatically more
   efficient than parsing in a JVM/Go layer. The trade-off is that you've moved CPU onto the database,
   which is the resource you can least easily scale.
4. **Don't parse at all on the archival path.** The S3 writer needs the data columnarised, but if you're
   already producing typed rows for ClickHouse, share the parsed representation between both sinks
   instead of parsing twice. Two consumer groups parsing the same bytes is a 2× waste that's easy to
   miss.

The answer I'd give: measure first, then do (1) immediately because it's a day of work, plan (2) as a
quarter-long track because it also halves Kafka cost, and explicitly reject (3) for Skyline because
ClickHouse CPU is our scarcest resource and we're not going to spend it on parsing.

### 2.8 Tier 4 questions — organisational

**Q2.13: You have six engineers and four quarters. What do you build first and why?**

*Model answer:* The sequencing principle is: **build the thing that makes everything else reversible
first.** For a data platform that's the durable log and the archive, because once you have those, every
subsequent mistake is recoverable by reprocessing, and no design decision downstream is permanent.

*Q1 — the spine.* Collectors, Kafka, and the S3/Iceberg archive. Ship it running in **shadow mode**:
resolvers dual-ship to Argus and to the new collectors, nothing customer-facing reads the new path.
Deliverable at end of quarter: "every event is durably captured twice and we can prove the counts
match." No user-visible change, which is politically hard and technically correct — I'd spend real
effort communicating why.

*Q2 — the hot path and the SLO machinery.* ClickHouse cluster, the rollup materialized views, and the
SLI instrumentation from Part 4. Move internal read traffic over first, because internal teams can
tolerate being the canary and will find the bugs. Deliverable: internal dashboards run on ClickHouse
and there's an error-budget report.

*Q3 — consumer migration.* The Reports UI, the ML feature pipeline, the billing pipeline. This is the
quarter with the real risk, and Part 3 is about how to do it without breaking anyone. Deliverable:
Argus's Elasticsearch tier is decommissioned.

*Q4 — lifecycle, governance, and the external contracts.* Retention as policy-driven control-plane
data, the compliance controls, and finally the 40 external customers on the Hive metastore. They go
last because they're contractual and need lead time, and because by then the platform has three
quarters of production evidence behind it.

The thing to say explicitly: **the highest-risk item (external customers) goes last not because it's
least important but because every quarter of production burn-in reduces its risk.** And the lowest-
visibility item (the spine) goes first because everything else depends on it.

**Q2.14: How do you convince a skeptical VP that the six-month rewrite is worth it when Argus "works"?**

*Model answer:* Don't argue architecture; argue in the units the VP is accountable for. Three framings,
and I'd use all three.

*Cost.* Argus runs 30 Logstash VMs, a 40-node Elasticsearch cluster sized for 7 days of hot data, and
a Hadoop cluster. Put the actual monthly bill on a slide next to the modelled cost of the new platform.
For Skyline's volume, Elasticsearch on this data typically runs 3–5× ClickHouse's cost for the same
retention because of the inverted index write amplification and JVM heap sizing. If the delta is
$180K/year, the rewrite pays for two of the six engineers.

*Risk.* Fourteen cron jobs on three bastion hosts control deletion of customer data, written by people
who no longer work here, with no audit log. Frame it as the compliance finding it will become: "we
cannot currently produce evidence of what customer data was deleted or when, which is a SOC 2
exception and a GDPR Article 17 exposure." That converts an engineering preference into a risk register
item with an owner.

*Capability.* Name product things that are impossible today and become possible: sub-minute freshness
instead of nightly Hive; 400-day retention instead of 7-day search; per-tenant data-residency
guarantees that unblock the EU segment; ML features derivable from the same store. Tie each to revenue
or a specific blocked deal if you can — "three deals in the last two quarters were lost on the EU
residency requirement" is a far stronger argument than any diagram.

Then de-risk the ask: **don't ask for six months of invisible work.** Ask for one quarter to build the
spine in shadow mode with a concrete falsifiable deliverable ("we will prove event counts match Argus
to within 0.001%"), and an explicit kill criterion. A VP is much more willing to fund a quarter with a
checkpoint than a year with a promise.

### 2.9 Case study: the Tuesday morning "too many parts" incident

**Scenario given to you in the interview:** "It's 09:20 on a Tuesday. Alerts fire: ClickHouse inserts
are failing with `TOO_MANY_PARTS` on 3 of 8 shards. Ingestion lag is 12 minutes and climbing. This
started at 09:05. What happened and what do you do?"

*How to work it:* The interviewer wants to see a hypothesis-driven investigation, not a checklist.

**Framing.** Parts accumulate when insert rate exceeds merge rate. So either inserts got smaller/more
frequent, or merges got slower, or a partition boundary changed. 09:05 on a Tuesday is suspicious —
close to the daily peak (09:00 ET), and also a plausible deploy window.

**Hypotheses, cheapest to test first.**

*H1: A deploy changed batching.* Check the deploy log. If someone shipped a change that reduced the
flush threshold — say from 100,000 rows to 10,000 to "improve freshness" — insert rate went up 10× and
part creation with it. This is the single most common cause and the easiest to verify and revert.

*H2: A partition-key change.* Check for a schema change. If someone changed `PARTITION BY toYYYYMMDD(ts)`
to include something high-cardinality, each insert now writes to many partitions at once, and a single
100,000-row insert that used to create 1 part now creates 200. Look at `system.parts`: is the part count
high because of many partitions, or many parts within one partition? That one query distinguishes H1
from H2 immediately.

*H3: Merges are blocked, not slow.* Query `system.merges` and `system.mutations`. A long-running
mutation (someone ran an `ALTER ... DELETE` for a GDPR request at 09:00) monopolises the merge thread
pool and disk bandwidth. Also check free disk: **merges require free space roughly equal to the size of
the parts being merged**, so a disk above ~80% can stall merges entirely, which then makes the disk fill
faster — a genuine death spiral.

*H4: Late-arriving data fanning across partitions.* A tenant's resolver was offline for a week and came
back at 09:05, flushing its spool. Those events have timestamps spread over 7 days, so each insert batch
now touches 7 daily partitions instead of 1, multiplying part creation by 7. **This is the interesting
one**, because it's not anyone's fault and it will recur. Verify by checking the spread of `ts` in
recent inserts, or `SELECT partition, count() FROM system.parts WHERE table='dns_events' AND active
GROUP BY partition ORDER BY partition DESC`.

**Mitigation, in order.**
1. Immediately raise `parts_to_throw_insert` from 300 to 600 **as a temporary measure only**, and say
   out loud that this trades a hard failure for a slower degradation — it buys minutes, not a fix.
2. Increase batch size at the processor to reduce part creation rate.
3. Pause mutations: `KILL MUTATION WHERE table = 'dns_events'`.
4. Temporarily raise `background_pool_size` / merge concurrency if CPU has headroom, since merges are
   often thread-limited rather than I/O-limited at this size.
5. If H4, route late data (ts older than ~2 hours) to a **separate backfill table** with its own
   partitioning, and merge it into the main table with `ATTACH PARTITION` later. This is the durable
   fix and worth designing in from the start.

**The staff-level closing move.** Don't stop at the fix. Say: "The underlying problem is that our
ingestion has no guardrail on parts-per-insert-batch. I'd add three things: an SLI on `parts per active
partition` with an alert at 100 — well before the 150 throttle threshold — so we see this 30 minutes
earlier; a hard rule enforced in code that late-arriving data goes to the backfill path; and a
pre-merge check in CI that fails any PR changing batch thresholds without a corresponding load test."
That's converting an incident into a system property, which is the actual job.

---

## Part 3 — Migrating legacy data lifecycle management without breaking consumers

### 3.1 What the interviewer is actually testing

This is the bullet that most candidates under-prepare, and it's the one that most closely resembles
the real job. The hidden rubric:

1. **Do you know what "data lifecycle management" actually means?** Many candidates hear it and talk
   about ETL. It doesn't mean that. It means the governed answer to: when does data arrive, where does
   it live at each age, when does it move, when is it deleted, who authorised that, and what evidence
   exists that it happened. Retention, tiering, archival, expiry, legal hold, and deletion proof.
2. **Do you understand that consumers, not systems, are the hard part?** Migrating storage is a
   solvable engineering problem. Migrating 400 saved queries owned by six teams and 40 contractual
   external readers is an organisational problem wearing an engineering costume.
3. **Do you default to a strangler pattern rather than a big-bang cutover?** And can you name the
   specific seam you'd strangle at?
4. **Can you define "not disrupting" precisely enough to test it?** "Nothing broke" is not a
   verifiable statement. "For 30 days, every query issued against the legacy interface produced
   byte-identical results on both systems, and we have the diff report" is.

### 3.2 The mental model: lifecycle as policy, not as scripts

Look again at what Argus actually is, from §1.5. Retention is implemented as this, fourteen times over,
on three bastion hosts:

```bash
#!/bin/bash
# /opt/argus/cron/purge_dns.sh — last modified 2019-04-11 by someone who left in 2021
CUTOFF=$(date -d '395 days ago' +%Y-%m-%d)
hive -e "ALTER TABLE dns_queries DROP PARTITION (dt < '$CUTOFF')"
hdfs dfs -rm -r -skipTrash /warehouse/dns_queries/dt=${CUTOFF}
```

Take this seriously as an artifact, because articulating exactly what's wrong with it *is* the answer
to "what does good look like."

**Failure 1: the policy is invisible.** The retention period is the string `'395 days ago'` inside a
shell script. To answer "what is our retention for DNS query data?" — a question a customer, an
auditor, and a lawyer will each ask — someone must find and read the script. There are 14 of them and
they disagree.

**Failure 2: the policy is uniform when the business isn't.** Every tenant gets 395 days. But three
customers contracted for 7 years, EU customers may need *shorter* retention for GDPR minimisation, and
free-tier tenants should get 30. A shell constant cannot express per-tenant policy, so the business
worked around it — by *not deleting* whenever there was doubt. Argus is almost certainly holding data
it has no legal basis to hold.

**Failure 3: no audit trail.** `hdfs dfs -rm -r -skipTrash` produces no durable record. When an auditor
asks "prove that tenant 8812's data was deleted within 30 days of contract termination," the honest
answer is "we can't."

**Failure 4: no legal hold.** If litigation requires preserving one tenant's data, there is no
mechanism except commenting out a cron job — and no mechanism to un-hold it, which means the comment
stays forever.

**Failure 5: deletion isn't idempotent or resumable.** If the script dies halfway, some HDFS
directories are gone and the Hive partition still exists, or vice versa. Nobody notices until a query
returns a "file not found."

**Failure 6: it's coupled to the storage engine.** The policy is expressed in Hive DDL and HDFS paths.
Moving to ClickHouse and S3 means rewriting the policy, which is why the policy will get lost in the
migration unless you deliberately extract it first.

So the target state, stated as a design principle:

> **Lifecycle is control-plane data, enforced by data-plane mechanisms, with an audit record of every
> enforcement action.** The policy lives in Postgres as rows. Enforcement is ClickHouse TTL clauses and
> S3 lifecycle rules plus Iceberg deletes. Every action writes an immutable event to an append-only
> audit log. Nothing about retention is ever a constant in code.

### 3.3 The target design

**The policy table** (Postgres, control plane):

```sql
CREATE TABLE retention_policies (
    policy_id        BIGSERIAL PRIMARY KEY,
    tenant_id        INT         REFERENCES tenants(tenant_id),   -- NULL = default for all
    dataset          TEXT        NOT NULL,                        -- 'dns_events', 'threat_hits', ...
    hot_days         INT         NOT NULL,   -- full fidelity, ClickHouse local disk
    warm_days        INT         NOT NULL,   -- ClickHouse on S3-backed disk
    archive_days     INT         NOT NULL,   -- S3 Parquet/Iceberg only
    purge_after_days INT         NOT NULL,   -- hard delete
    legal_hold       BOOLEAN     NOT NULL DEFAULT FALSE,
    hold_reason      TEXT,
    basis            TEXT        NOT NULL,   -- 'contract:MSA-2024-118', 'regulation:GDPR-min', 'default'
    effective_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_to     TIMESTAMPTZ,            -- NULL = current
    created_by       TEXT        NOT NULL,
    approved_by      TEXT,                   -- required when purge_after_days is reduced
    CONSTRAINT ordering CHECK (hot_days <= warm_days
                           AND warm_days <= archive_days
                           AND archive_days <= purge_after_days)
);
CREATE UNIQUE INDEX ON retention_policies (COALESCE(tenant_id, -1), dataset)
    WHERE effective_to IS NULL;
```

Several deliberate choices worth defending in an interview:

- **`basis` is mandatory.** Every retention number must cite why it exists — a contract ID, a
  regulation, or the explicit default. This single column is what turns an auditor conversation from a
  three-week archaeology project into a query. It also stops the drift where someone bumps retention
  "just in case" and nobody can later justify removing it.
- **Temporal validity (`effective_from` / `effective_to`)** rather than in-place updates. You must be
  able to answer "what was the policy on 2025-03-12?" because that's the question you get when
  something was deleted and someone is unhappy. Never `UPDATE` a policy; insert a new row and close the
  old one.
- **`legal_hold` is a first-class field**, not a comment in a cron job, and it's checked by every
  enforcement path. A hold suspends deletion but not tiering.
- **`approved_by` required for shortening retention.** Lengthening retention is safe; shortening it
  destroys data. Enforce the asymmetry with a trigger or in the service layer.
- **The `CHECK` constraint** makes an incoherent policy unrepresentable. You cannot write a policy that
  archives before it warms.

**Enforcement** is where the control plane drives the data plane, and it works differently per store:

*ClickHouse* — the reconciler renders per-table TTL from the policy rows. Because ClickHouse TTL is
table-level, not row-level-by-tenant, and because you cannot have 12,000 different TTLs on one table,
the design that actually works is **tiered tables by retention class**, not by tenant. Bucket tenants
into a small number of retention classes (say 30/90/400/2555 days), and route each tenant's writes to
the table matching its class:

```sql
-- one table per retention class, same schema
ALTER TABLE dns_events_r400 MODIFY TTL
      ts + INTERVAL 7  DAY  TO VOLUME 'warm',
      ts + INTERVAL 30 DAY  TO VOLUME 's3_cold',
      ts + INTERVAL 400 DAY DELETE;
```

A `Merge` table engine or a view unions them so queries don't care. When a tenant changes class — they
upgrade to a 7-year contract — you move their partitions between tables rather than editing a TTL.
This is a genuinely important design insight and a good one to volunteer: **ClickHouse expresses
retention per table, so your retention classes must be a small closed set, and the control plane's job
is mapping 12,000 tenants onto ~4 classes.**

*S3* — two mechanisms, and knowing when to use which matters. Native **S3 Lifecycle rules** handle
transitions (Standard → Standard-IA → Glacier Instant Retrieval → Deep Archive) and simple expiry, and
they're free to run. They operate on prefixes, so your object layout must make retention class visible
in the prefix — which is why the layout includes `tenant_bucket` and, in a refined version, a
`retention_class=r400/` prefix component. For anything tenant-specific or row-level (GDPR erasure),
lifecycle rules can't help and you need **Iceberg deletes** plus compaction, covered in Part 6.

*Audit* — every enforcement action appends to `lifecycle_audit`:

```sql
CREATE TABLE lifecycle_audit (
    audit_id     BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    action       TEXT NOT NULL,          -- 'tier_move' | 'purge' | 'hold_applied' | 'hold_released'
    dataset      TEXT NOT NULL,
    tenant_id    INT,
    partition_key TEXT,                  -- '20250312'
    policy_id    BIGINT REFERENCES retention_policies(policy_id),
    row_count    BIGINT,
    bytes        BIGINT,
    executed_by  TEXT NOT NULL,          -- service identity
    request_id   UUID NOT NULL
);
```

Append-only, enforced by revoking `UPDATE`/`DELETE` from the application role. This table is the
evidence artifact. When an auditor asks about tenant 8812, it's one query.

**The reconciler** is a control loop, not a cron job, and the distinction matters. A cron job performs
actions; a reconciler compares desired state to actual state and closes the gap. Every 15 minutes it:

1. Reads current policies from Postgres.
2. Reads actual state — `system.parts` from ClickHouse (which partitions exist, on which volume), and
   Iceberg manifests from S3.
3. Computes the diff: partitions that should have moved tier, partitions past purge age, tenants whose
   class changed.
4. Applies changes, **rate-limited** (no more than N partition drops per run, so a bad policy write
   can't delete everything before someone notices).
5. Writes audit rows.
6. Exports a metric: `lifecycle_drift_partitions` — how many partitions are not in their desired state.

Making it a reconciler gives you idempotency (re-running is safe), resumability (a crash just means the
next run picks up), and an SLI (`drift` should be near zero; if it climbs, something is stuck). It also
means a human can safely fix state by hand and the loop will accept it.

### 3.4 The migration strategy: strangler fig with a view seam

Now the actual question: how do you get from Argus to that, with six consumer groups still reading?

**The core idea.** Don't migrate consumers to a new system. Migrate the *system behind an interface the
consumers already use*. Find the narrowest seam between consumers and storage, freeze it as a contract,
put a shim there, and swap what's behind it one dataset at a time. That's the strangler fig pattern,
and the art is choosing the seam.

**Skyline's seams, by consumer:**

| Consumer | Current interface | Seam to strangle |
| --- | --- | --- |
| Reports UI | Internal query service → ES + Hive | The query service's API — we own both sides. Easiest. |
| 6 internal teams | Direct HiveQL against the metastore | A Trino/Presto catalog that presents the same table names |
| ML feature pipeline | Spark reading HDFS paths | An Iceberg table with the same schema |
| Threat-intel batch | Hive query, nightly | Same Trino catalog |
| **40 external customers** | Shared Hive metastore + nightly S3 export | The S3 export contract (file layout, schema, timing) |
| Billing | Hive aggregate, monthly | Recomputed independently and reconciled |

Notice the pattern: **for five of six consumers, the seam is a query interface you can reimplement.**
For the external customers, the seam is a *file format contract*, which is actually easier to hold
stable — bytes in a bucket don't care what produced them.

**The five phases.**

**Phase 0 — Discover what's actually consumed (2–3 weeks, and do not skip it).**

You cannot preserve behaviour you haven't catalogued. Turn on Hive/HDFS audit logging and collect 30
days. Then answer, with data:

- Which tables are actually read? (Expect 60–70% of tables to have zero reads in 30 days. Those get
  deleted, not migrated — and that's the cheapest win in the whole project.)
- Which *columns*? Column-level lineage from parsed query logs tells you what the real schema contract
  is, versus the nominal one.
- Which queries run, by whom, how often, and how long do they take? You need the p99 to know whether
  the new system is actually an improvement.
- What's the actual retention *in practice* per table — not what the script says, but what data exists?
  Run it: `SELECT MIN(dt), MAX(dt), COUNT(DISTINCT dt) FROM each table`. You will find tables with
  seven years of data under a 395-day policy because a cron job silently failed in 2022. **Finding that
  is a compliance disclosure, and you should raise it immediately rather than quietly fixing it** — a
  data platform lead who hides a retention violation has a much bigger problem than a migration.

Deliverable: a consumer registry — every dataset, every consumer, every contract, an owner's name, and
a criticality rating. This document is what makes the rest of the migration schedulable, and it's the
artifact a staff engineer is expected to produce that a senior engineer often skips.

**Phase 1 — Dual-write and prove equivalence (one quarter).**

New pipeline runs alongside Argus. Both write. Nothing reads the new one. The deliverable is *evidence*,
and there are three levels of it, in increasing strength:

*Level 1 — volumetric reconciliation.* Every hour, for every tenant, compare event counts:

```sql
-- new platform
SELECT tenant_id, toStartOfHour(ts) AS h, count() AS c
FROM dns_events WHERE ts >= '2026-09-05' GROUP BY tenant_id, h;
-- legacy
SELECT tenant_id, hour, count(*) FROM dns_queries WHERE dt = '2026-09-05' GROUP BY tenant_id, hour;
```

Publish the per-tenant delta as a metric. **Define the acceptance threshold before you start, and
expect it not to be zero.** Realistically the two systems will differ by 0.01–0.1% because of clock
skew at hour boundaries, Argus's own dropped messages (syslog over TCP with a bounded queue drops
under load — you will discover Argus was losing data all along, which is a great finding), and dedup
differences. The right threshold is a business decision: I'd propose "≤ 0.05% per tenant-hour, and any
tenant-hour above 0.5% is investigated individually," and I'd make sure the discrepancies are
*explained*, not just within tolerance.

*Level 2 — row-level sampling.* Take 10,000 random `event_id`s per day and compare every field across
both systems. This catches enrichment differences that counts can't: a threat category that's `null`
in one and `'unknown'` in the other is invisible to a count and very visible to a customer.

*Level 3 — query-level shadow diffing.* This is the strongest evidence and the one that convinces
consumers. Replay real production queries (from Phase 0's catalogue) against both systems and diff the
result sets. Not just row counts — actual values, with float tolerance where appropriate. Publish a
per-consumer dashboard: "team X's 62 saved queries: 60 identical, 2 differing, here's why." That
dashboard is what gets you a "yes" from a nervous team, because it converts "trust me" into "look."

**Phase 2 — Shadow reads (4–6 weeks).**

Consumers still get legacy results, but the query service also issues the query against the new
platform, compares, and logs mismatches. Serve legacy, measure new. This catches the long tail of
queries that Phase 1's catalogue missed, at zero risk. Track two things: mismatch rate and latency
delta. Gate the next phase on "mismatch rate < 0.01% for 14 consecutive days."

**Phase 3 — Progressive read cutover.**

Flip reads per consumer, per dataset, behind a flag in the control plane — never a deploy. Order by
increasing blast radius: internal exploratory first, then internal production, then the Reports UI at
1% → 10% → 50% → 100% of tenants, then ML, then billing, then external customers.

**Keep the legacy write path running the entire time.** The rollback for any step is flipping the flag
back, which must remain a sub-minute operation with no data loss. The moment you turn off legacy
writes, you've lost your rollback, so that comes last and separately.

For the Reports UI specifically, do the percentage rollout **by tenant, not by request**, so a given
customer sees a consistent system rather than alternating between two with slightly different numbers.
Alternating is how you generate support tickets that say "the dashboard changes every time I refresh."

**Phase 4 — Decommission, deliberately.**

Order matters and people rush it:

1. Stop legacy *writes* only after 30 days of 100% new-path reads with no incidents.
2. Keep legacy *data* readable for another 90 days. Storage is cheap; being unable to answer "what did
   the old system say on March 12" is not.
3. **Take a final immutable snapshot** of legacy data to S3 with Object Lock before deleting anything,
   and record it in the audit log. This is your defence if a dispute arises in month 8.
4. Then delete, partition by partition, with the reconciler, with audit rows.
5. Delete the cron jobs and the bastion hosts *last*, and — this is the part everyone forgets —
   **verify nothing else was running on them.** Nine-year-old bastion hosts accumulate scripts.

### 3.5 Tier 1 questions — screening

**Q3.1: What is data lifecycle management, in your words?**

*Model answer:* It's the governed answer to five questions for every dataset: where does it live at
each age, when does it move, when does it die, who decided that, and how do we prove it happened. The
"governed" part is what distinguishes it from just having a TTL — a TTL is a mechanism; lifecycle
management is a policy plus a mechanism plus an audit trail plus an owner.

Concretely for Skyline's `dns_events`: 0–7 days on local NVMe in ClickHouse for interactive forensic
search; 7–30 days on ClickHouse's S3-backed disk, still queryable but slower; 30–400 days in S3 Parquet
via Iceberg, queryable through Trino with minutes-scale latency; deleted at 400 days — except for three
tenants on 7-year contracts and any tenant under legal hold. Each of those numbers is a row in Postgres
with a documented basis, each transition writes an audit record, and a reconciler continuously verifies
that reality matches policy.

**Q3.2: What does "without disrupting existing consumers" mean concretely? How would you measure it?**

*Model answer:* It has to be decomposed into testable properties, because as a phrase it's unfalsifiable.
I'd define five, each with a metric:

1. **Result equivalence** — the same query returns the same answer. Measured by shadow diffing: the
   mismatch rate across replayed production queries, target < 0.01%.
2. **Interface stability** — no consumer changes code. Measured by counting required consumer-side
   changes; target zero for phases 0–3.
3. **Latency non-regression** — p99 query latency does not get worse. Measured per consumer, per query
   class. Note this is a *non-regression* bar, not an improvement bar; improvement is the goal but
   non-regression is the promise.
4. **Availability** — the new path's availability is at least the old path's. Which requires knowing the
   old path's, which nobody measured, so Phase 0 includes instrumenting Argus.
5. **Freshness non-regression** — data is available at least as soon as before.

And then the sixth, which isn't a metric but matters more than any of them: **no consumer is surprised.**
Every affected team knows the schedule, has seen their own diff report, and has a named rollback. A
migration that is technically flawless and organisationally surprising still counts as a disruption.

**Q3.3: Why not just do a big-bang cutover over a weekend?**

*Model answer:* Because the failure mode isn't recoverable in a weekend. With 13 billion events/day and
six consumer groups, the realistic discovery pattern is that things break on Tuesday, not Saturday —
a monthly report that runs on the 1st, a saved query someone runs quarterly, an external customer's
weekly job. A weekend cutover gives you 48 hours of validation for failure modes with a 30-day period.

There's also a rollback asymmetry. Big-bang means the rollback is "restore the old system and reload
three days of data into it," which is itself a multi-day project you've never rehearsed. Progressive
cutover means the rollback is a flag flip you've done a dozen times in staging.

The one case where big-bang is right: when the systems genuinely cannot coexist — a hard data-residency
constraint, or a licence that forbids running both. Even then I'd argue for cutting over one dataset at
a time rather than all at once.

### 3.6 Tier 2 questions — design

**Q3.4: Forty external customers read a shared Hive metastore. You're deleting Hive. Design the
migration for them.**

*Model answer:* This is the hardest consumer because you don't control their code and the relationship
is contractual. Start by reading the actual contracts — the engineering answer depends on what was
promised. Typically it says something like "Skyline will make available a daily export of the
customer's query logs in a mutually agreed format," which is much more flexible than "Hive."

Three options, and I'd offer them as a menu rather than pick one, because different customers have
different capabilities:

**Option A — preserve the file contract, change the producer.** The nightly S3 export continues:
identical bucket, identical prefix layout, identical schema, identical delivery time, produced now by
the new pipeline instead of a Hive job. For customers who only consume the S3 export, **this is a zero-
change migration** and I'd expect most of the 40 to be in this bucket. The engineering work is entirely
on our side: byte-level comparison of old vs new exports for 30 days before switching.

**Option B — for customers who query the metastore directly**, stand up a Trino cluster with a catalog
that exposes the same database and table names over Iceberg. HiveQL and Trino SQL are not identical,
so this is not zero-change — but you can quantify it precisely by replaying their actual queries from
Phase 0's audit logs and reporting exactly which ones need edits. Going to a customer with "we replayed
your 137 queries, 134 work unchanged, here are the 3 that need a one-line edit and here's the edit" is
a completely different conversation from "we're migrating, please adapt."

**Option C — offer something better as an incentive.** A REST/SQL API or a Snowflake/Databricks share
that gives them fresher data than the nightly dump. Some customers will want this; migrating them is
then a feature delivery, not a forced change. Sequence these first — they become references for the
reluctant ones.

**Process, which matters as much as the technology:**

- **Communicate 6 months out**, not 6 weeks. Enterprise customers have change-control boards; a 6-week
  notice for a contractual interface is how you get an escalation to your CEO.
- **Name a per-customer owner** on your side. Forty customers is too many for one person, so distribute
  it across the team with a shared tracker.
- **Run both interfaces in parallel for 90 days minimum**, and instrument the old one so you can see
  who's still using it. Do not rely on customers telling you they've migrated; rely on the access logs
  going to zero.
- **Have a paid extension path.** If three customers genuinely cannot migrate in time, running the old
  export for them for another quarter costs you far less than a contractual dispute. Decide that in
  advance rather than under pressure.
- **Never delete their data as part of this.** Whatever else happens, the archive stays.

**Q3.5: The billing pipeline counts events per tenant per month. How do you migrate it without invoice
errors?**

*Model answer:* Billing gets special treatment because the error is externally visible, financially
material, and generates a support ticket per affected customer. The strategy is **parallel run with
reconciliation to the penny, for a full billing cycle, before switching.**

Specifically:

1. Compute the new number **independently**, not as a port of the old query. If you port the query, you
   port its bugs and you can't detect them. Independent derivation means a difference is informative.
2. Run both for **two complete billing cycles** — two months, not two weeks, because monthly boundary
   handling is exactly where the bugs live (timezone of the month boundary, late-arriving events
   attributed to the previous month, leap seconds if you're unlucky).
3. Reconcile per tenant per month and **investigate every non-zero delta**, even tiny ones. "0.02% off"
   is not acceptable here even though it would be in Phase 1 volumetrics, because you must be able to
   explain the number to a customer's procurement team.
4. Expect to find that **the old number was wrong.** This happens nearly every time. Argus dropped
   events under load, so historical invoices under-counted. Now you have a business decision — not an
   engineering one — about whether to (a) switch to the correct number and absorb customer questions
   about the increase, (b) apply a compatibility factor for a transition period, or (c) grandfather
   existing contracts. **Escalate that to finance and legal with the data; do not decide it yourself.**
   Bringing them a clear "here is the discrepancy, here is why, here are three options with revenue
   impact" is precisely the staff-level move.
5. Make the billing count **idempotent and reproducible**: `uniqExact(event_id)` over an immutable
   snapshot, with the snapshot ID recorded on the invoice. When a customer disputes an invoice in
   month 9, you must be able to reproduce the exact number, which means the underlying data must be
   immutable and versioned. Iceberg's time travel gives you this for free — record the snapshot ID.
6. Cut over at a **billing boundary**, never mid-cycle.

**Q3.6: How do you handle data that exists in the legacy system but not in the new one — nine years of
history?**

*Model answer:* Separate the question into three, because the answers differ.

*Do we have to keep it?* Check the policy. Argus holds nine years under a 395-day nominal policy, which
means most of it should already have been deleted. The first move is a legal/compliance conversation:
for data past its retention with no legal hold, **the correct action is deletion, not migration.**
Migrating data you shouldn't have converts a passive violation into an active one, and it's expensive.
I'd expect this conversation to remove 70%+ of the volume from scope.

*For what we must keep, does it need to be queryable or just retrievable?* Big difference in cost. Data
under a 7-year contract that gets read twice a year does not belong in ClickHouse; it belongs in S3
Glacier Deep Archive at $0.00099/GB-month with a documented 12-hour retrieval path. Data that feeds ML
training needs to be in Parquet/Iceberg. Only the recent window needs ClickHouse. Map every historical
dataset onto one of those three, and the cost falls by an order of magnitude.

*How do we physically move it?* One-time backfill, and it's a genuinely different engineering problem
from streaming ingest — bulk, restartable, throughput-optimised, and it must not disturb the live path:

- Read from HDFS with Spark, transform to the new schema, write Parquet to S3, register in Iceberg.
- **Partition the work by day and track each day's state in Postgres** (`pending / running / done /
  failed` with row counts). That makes it resumable, parallelisable, and observable — you can say "we
  are 61% through, 2,014 of 3,285 days" instead of "it's running."
- **Rate-limit it** so it doesn't saturate the network or S3 request budget that live ingestion needs.
  A backfill that causes a production incident is a self-inflicted wound; cap it at a fraction of
  available capacity and run it during off-peak.
- Reconcile per day: row count and a checksum of a stable column set, recorded in the same table.
- For anything old and rarely read, write it **directly to the target storage class** (Glacier via
  `x-amz-storage-class` on PUT) rather than writing to Standard and transitioning — a lifecycle
  transition costs about $0.05 per 1,000 objects, and at a few million objects that's real money for
  nothing.

*Schema drift over nine years.* The 2017 schema is not the 2026 schema. Write an explicit
version-detecting reader with one mapping function per historical schema version, and be honest in the
catalogue about which columns are null before which date. **Do not silently backfill defaults into
historical data** — an ML pipeline that sees `threat_score = 0` for 2018 data will learn that 2018 was
safe. Null means "we didn't measure this," and that's the truthful value.

### 3.7 Tier 3 questions — deep dive and adversarial

**Q3.7: You're in Phase 3, 50% of tenants cut over. A customer says their dashboard numbers changed.
What now?**

*Model answer:* Treat it as a real defect until proven otherwise, and work it in a fixed order.

**First, contain.** Flip that tenant back to the legacy path immediately. It's one control-plane row.
Do this *before* investigating — the investigation might take a day, and there's no reason for the
customer to be wrong for a day when the rollback is instant. Then tell them you've reverted them and
you're investigating, with a time commitment.

**Second, reproduce precisely.** Get the exact query, the exact time range, and the exact numbers from
both systems. "Numbers changed" is usually one of five things, and they're distinguishable:

1. **The new system is right and the old one was wrong.** Most common outcome, and it's the awkward
   one. Argus dropped events under load, so the new numbers are *higher*. The customer perceives a
   regression because their baseline moved.
2. **Timezone or boundary semantics.** The old system bucketed by the ingestion date; the new one
   buckets by event time. For an event at 23:58 local that arrives at 00:03, those differ. This shows
   up as small deltas at day boundaries and is very easy to confirm — check whether the delta
   concentrates at boundaries.
3. **Dedup differences.** The new system deduplicates on `event_id`; Argus didn't. If the customer's
   resolvers were retrying, the old numbers were inflated and the new ones are lower.
4. **Genuine data loss in the new pipeline.** DLQ, a consumer stuck on a partition, a shard that missed
   writes. Check the reconciliation dashboard for that tenant-hour.
5. **A semantic difference in the query translation.** HiveQL `COUNT(DISTINCT x)` is exact; a
   ClickHouse `uniq(x)` is approximate (HyperLogLog, ~2% error). If someone translated one to the other,
   the numbers legitimately differ. **This is a very common and very embarrassing migration bug** —
   `uniq` for approximate, `uniqExact` for exact, and you must choose consciously.

**Third, communicate honestly.** If the answer is (1), the message is: "Your old numbers were
undercounting by roughly 0.4% because the legacy collector dropped events under load. The new numbers
are correct. Here is the evidence." Do not fudge the new system to match the old one. I have seen teams
do that; it's a lie with a long tail.

**Fourth, systematise.** Whatever this was, add it to the shadow-diff suite so it's caught before the
next customer sees it. If it's category (2) or (5), it almost certainly affects other tenants who
haven't complained yet — go find them proactively rather than waiting.

**Q3.8: Halfway through, a compliance audit demands proof that you deleted a specific customer's data
in 2023. Argus has no audit log. What do you say?**

*Model answer:* Say the true thing, quickly, to the right people. Concretely:

**Immediately:** tell legal and the compliance owner that Argus has no deletion audit trail and we
cannot produce direct evidence for 2023 actions. Do not let this surface first in an auditor's report.
A known gap that engineering disclosed is a finding with a remediation plan; a gap the auditor found is
a finding with a credibility problem attached.

**Then build the strongest available indirect evidence**, and be precise about its limits:
- Query current Argus state to demonstrate the data is *not present now*. That proves the end state
  even if it doesn't prove the timing.
- Produce the cron script, its modification history from the host, and the scheduler's execution log if
  any exists — which at least establishes the mechanism and that it ran.
- Produce HDFS `fsimage` snapshots or namenode audit logs if retained; these sometimes go back further
  than people expect and can establish approximate deletion dates.
- Correlate with backup manifests: if backups from mid-2023 lack the partitions and backups from
  early 2023 contain them, you've bounded the deletion window.

Present it as: "We can demonstrate the data is absent as of today, and we can bound deletion to between
March and June 2023 through backup manifests. We cannot produce a per-object deletion record because
the legacy system did not generate one. That gap is exactly what the platform migration closes, and
here is the audit schema and the date it goes live."

**And make the remediation concrete and dated**, because that's what converts a finding into an
accepted risk: the new platform's `lifecycle_audit` table, append-only, with per-partition records,
live on a specific date, plus a compensating control in the interim (manual deletion certificates,
signed, for any deletion performed during the migration window).

**The meta-point for the interview:** the graded behaviour here is not technical. It's whether you
escalate honestly and fast. A staff engineer who says "let me see if I can quietly reconstruct
something" is failing this question regardless of how good the reconstruction is.

**Q3.9: How do you handle the legacy system's data quality problems — do you fix them in migration or
preserve them?**

*Model answer:* Default to **preserve the data, fix the interpretation, and make the difference
explicit.** The reasoning: migration and correction are two changes, and combining them makes both
unverifiable. If you fix bugs during the move, then when numbers differ you can't tell whether it's the
migration or the fix, and your equivalence evidence is worthless.

So the sequencing is: migrate faithfully (bug-for-bug), prove equivalence, cut over, *then* fix data
quality as a separate, separately-communicated change with its own before/after.

Three exceptions where I'd fix during migration:

1. **Data that shouldn't exist at all** — records past retention, data for terminated tenants, PII in a
   column that shouldn't have it. Don't migrate a violation forward.
2. **Structural corruption that blocks the migration** — records that can't be parsed into the target
   schema. You have to do something; the right something is route them to a quarantine dataset,
   document the count per day, and don't silently drop them.
3. **Anything with a security or privacy impact**, which gets fixed immediately regardless of migration
   phase.

And in all cases, record the known defects in the dataset catalogue as first-class metadata: "before
2021-06-14, `latency_us` is null for 12% of rows due to a resolver bug." Consumers — especially ML
consumers — need to know. A defect that's documented is a caveat; a defect that's silently corrected is
a reproducibility bug in someone's model.

### 3.8 Tier 4 questions — organisational

**Q3.10: An internal team refuses to migrate. They have 60 saved queries and no bandwidth. What do you
do?**

*Model answer:* Assume the refusal is rational and find out what it's protecting. Almost always it's
one of three things: they've been burned by a migration before, they have a deadline the migration
threatens, or they don't believe the new system will be as good. Each has a different response, and
none of them is "escalate."

*If it's cost-of-change:* remove the cost. Don't ask them to rewrite 60 queries — take the queries,
run them against both systems, and hand back a report plus the edits for the ones that need changing.
For most, the answer is zero edits. Six engineers on the platform team doing this for one consuming
team is a few days; that consuming team doing it themselves is weeks of their roadmap. Absorbing
migration cost into the platform team is nearly always the right economic call and it's the thing that
makes you the team people want to work with.

*If it's trust:* give them the shadow-diff dashboard for their own queries and let them watch it for a
month. Evidence beats persuasion.

*If it's a deadline:* agree a date after their deadline and hold both systems until then. Put it in
writing so it doesn't drift.

*If after all that they still refuse:* now it's a prioritisation decision between two teams, which is
a management decision, not yours to force. Make the cost of *not* migrating visible and specific —
"holding Argus for one team costs $14K/month and blocks the GDPR remediation" — and let the owning
directors decide. Bring data, not frustration.

The failure mode to avoid: migrating around them by cutting them off. It works once and costs you
every future migration.

**Q3.11: How do you know when the migration is actually done?**

*Model answer:* "Done" needs a definition written at the start, because otherwise migrations don't end
— they just become permanently 95% complete, and the legacy system runs forever costing money and
blocking changes. I'd define it as five conditions, all measurable:

1. Legacy write path is off, and has been for 30 days.
2. Legacy read access logs show **zero** reads for 90 days — measured, not asserted.
3. Every dataset in the consumer registry from Phase 0 is either migrated or formally decommissioned
   with an owner's sign-off.
4. The final immutable snapshot exists in S3 with Object Lock, its location is documented in the
   runbook, and someone has actually tested restoring from it.
5. Legacy infrastructure is deleted — VMs, clusters, cron hosts, DNS entries, IAM roles, and the
   Terraform that would recreate them.

Point 5 is the one people skip, and it's the one that matters most for the "does it actually end"
question. An undeleted cluster is a cluster someone will start using again. I'd also add a sixth,
softer condition: the on-call runbook no longer mentions the legacy system. If it does, you're not
done.

### 3.9 Case study: the retention policy that was never a policy

**Scenario:** "During migration discovery you find that `dns_queries` in Hive contains data from 2017
onward — nine years — even though the purge script says 395 days. You also find that three of the 14
cron jobs have been failing silently since a 2022 Hadoop upgrade changed a CLI flag. What do you do,
in what order, and who do you tell?"

*This question is testing judgement under a compliance overhang, not technical skill.* Work it in four
phases.

**Phase 1 — Establish the facts precisely, in a few hours, before telling anyone anything imprecise.**

Get exact numbers, because "we might have extra data" and "we have 4.2 PB of data spanning 3,285 days
across 11 datasets affecting 12,000 tenants including 340 EU tenants" produce very different
conversations:

```sql
SELECT dataset, MIN(dt), MAX(dt), COUNT(DISTINCT dt) AS days, SUM(bytes)
FROM (per-table metadata) GROUP BY dataset;
```

Then determine which tenants are affected and, critically, **which regulatory regimes apply** — how
many are EU (GDPR data minimisation and storage limitation), how many are in scope for contracts that
specify maximum retention, how many are terminated customers whose data should have gone at contract
end. Also determine when each cron job last succeeded, from whatever logs exist.

**Phase 2 — Disclose, within a day.**

Tell your manager, the compliance/privacy owner, and legal, in one message, with the facts and without
speculation about consequences. Structure it as: what we found, how much, who's affected, what we've
done so far (nothing destructive), what we recommend, and what decision we need from them.

Two things you must *not* do. **Don't delete anything yet** — if there's litigation or an
investigation, deleting data that should have been deleted earlier can turn a retention violation into
a spoliation problem, which is far more serious. Legal decides. **And don't sit on it** to "fix it
first"; the fix takes weeks and the disclosure clock may already be running.

**Phase 3 — Contain, then remediate, in that order.**

*Contain (days):* Fix the three broken cron jobs so the problem stops growing. Add monitoring so silent
failure is impossible going forward — the specific control being: **every lifecycle job must emit a
heartbeat and a "records affected" count, and absence of a heartbeat pages.** A job that silently does
nothing must be indistinguishable from a job that failed loudly. That's the actual root cause here, and
naming it as such is the important move: the bug wasn't the CLI flag, it was that a lifecycle job could
fail without anyone knowing.

*Remediate (weeks, under legal direction):* Classify the excess data — under hold, contractually
required, or genuinely over-retained. Delete the third category oldest-first, in tracked batches, with
audit records for every batch. Do it through the new reconciler if it's ready, because then the deletion
itself is evidence-producing.

**Phase 4 — Turn it into a system property.**

The lasting fix is that this class of failure becomes structurally impossible, and there are four
specific controls:

1. **Retention as data, not code** — the `retention_policies` table, so the policy is queryable and
   nobody has to read a shell script to know it.
2. **Reconciliation, not execution** — a control loop that continuously compares actual to desired and
   exports `lifecycle_drift_partitions` as a metric. A cron job that fails leaves no trace; a
   reconciler that fails leaves a rising drift metric.
3. **Drift is an SLI with an alert.** Any dataset with data older than policy for more than 48 hours
   pages. This is the control that would have caught the 2022 breakage on day two instead of year four.
4. **A quarterly attestation report** generated from the audit table, showing per-dataset actual oldest
   record versus policy. Sign it. That's your SOC 2 evidence and it forces someone to look at the
   numbers four times a year.

**The closing line worth saying:** "The technical failure was a changed CLI flag. The systemic failure
was that we had no way to detect a lifecycle job doing nothing. I'd fix the second one, because the
first one will happen again in a different form."

---

## Part 4 — SLIs, SLOs, dashboards, and alerting for a data platform

### 4.1 What the interviewer is actually testing

Most engineers can recite the SRE book definitions. What's being tested is whether you understand that
**data platforms need different SLIs than request/response services**, and whether you can define ones
that are actually measurable.

The rubric:

1. Do you know that "availability" and "latency" are insufficient for a data platform? A pipeline can
   be 100% available, respond in 20ms, and be serving data that's six hours stale and missing 4% of
   events. Every dashboard is green and the product is broken.
2. Can you define an SLI precisely enough that two engineers would compute the same number? Most
   proposed SLIs fail this test.
3. Do you distinguish SLI (measurement) from SLO (target) from SLA (contract with consequences)?
4. Do you tie alerting to error budget burn rather than to threshold crossings? Threshold alerting on a
   data platform generates so much noise that people stop reading it, and then a real incident goes
   unnoticed.
5. Can you name what you'd deliberately *not* alert on?

### 4.2 The mental model: what actually goes wrong with data

Start from failure, not from metrics. Here are the things that have actually gone wrong at Skyline, and
notice that only one of them is a conventional availability problem:

- A resolver firmware bug stopped sending the `latency_us` field. Ingestion succeeded, queries
  succeeded, the column was silently null for 3 weeks.
- A consumer group rebalanced badly and one partition lagged 4 hours while 63 others were fine. The
  global average lag looked healthy at 4 minutes.
- The threat-intel enrichment feed didn't update for 5 days. Every event was categorised, and
  categorised wrong.
- A ClickHouse merge backlog meant `ReplacingMergeTree` duplicates hadn't collapsed, so a
  `count()`-based dashboard showed 3% more events than reality.
- An S3 lifecycle rule with a wrong prefix transitioned 40 days of hot Parquet into Glacier. Nothing
  errored; a query that used to take 8 seconds started taking 5 hours.
- A schema change added a column with a wrong default, so 100% of rows had `threat_score = 0`.

Every one of these is invisible to CPU, memory, request rate, error rate, and latency. So the first
principle:

> **A data platform's SLIs must measure properties of the data, not just of the services.** The four
> that matter are freshness, completeness, correctness, and availability — in that order of how often
> they catch real problems.

Second principle, and it's the one that most affects your design:

> **SLIs must be measured from the consumer's perspective, per tenant, not globally.** A global average
> hides exactly the failures that matter, because failures in a multi-tenant system are almost always
> tenant-shaped.

Let me show why with real numbers. Skyline has 12,000 tenants. Suppose 200 of them — all served by one
degraded collector region — have 45-minute ingestion lag while 11,800 are at 3 seconds. The
volume-weighted average lag is:

```
(11,800 × 3s + 200 × 2,700s) / 12,000 = (35,400 + 540,000) / 12,000 ≈ 48 seconds
```

Forty-eight seconds. If your SLO is "p50 freshness under 60 seconds," **you are meeting it while 200
customers are 45 minutes stale.** Now compute it the right way — as the fraction of tenants meeting
the objective:

```
11,800 / 12,000 = 98.33% of tenants within objective
```

Against a 99.5% target, you are clearly in violation. Same data, and only the second formulation tells
you the truth. **State the SLI as "the proportion of tenant-minutes that met the objective," never as an
average of a latency.** This single reframing is one of the strongest signals you can give in an SLO
discussion.

### 4.3 The four data SLIs, defined precisely

For each: what it means, exactly how you compute it, and what it catches.

---

**SLI 1 — Freshness.** *How old is the newest data a consumer can query?*

Define it as end-to-end event-time lag at the query boundary:

```
freshness_lag(tenant, t) = t − max(event_ts) visible to a query issued at wall-clock time t
```

Measure it by **synthetic probes, not by pipeline internals.** Every 30 seconds, a prober injects a
canary event for each of a stratified sample of tenants (all 20 whales, 100 sampled mid-market, 200
sampled long-tail — 320 probes, not 12,000, because probing everyone costs more than it's worth), then
polls ClickHouse until it appears. Record the delta.

Why synthetic rather than reading Kafka consumer lag? Because consumer lag measures one hop.
Freshness must include collector queueing, Kafka, processing, the ClickHouse insert, *and* the part
becoming visible to queries. A canary is the only thing that measures the property the customer
actually experiences. Consumer lag is a great *diagnostic* — it tells you which hop — but it's not the
SLI.

**SLO:** 99.5% of tenant-minutes have freshness lag < 60 seconds; 99.9% < 5 minutes.

Two thresholds deliberately. The tight one catches degradation; the loose one distinguishes "slow" from
"broken," and only the second is worth waking someone up for.

**Catches:** consumer lag, partition skew, ClickHouse insert failures, collector outages, backpressure.

---

**SLI 2 — Completeness.** *Did all the data that was produced actually arrive?*

This is the hardest one to measure honestly, because the naive version is circular: you can't count
what you didn't receive. The trick is **out-of-band accounting** — make the producer tell you what it
sent, through a channel independent of the data path.

Each resolver emits, every 60 seconds, a tiny heartbeat with `(resolver_id, minute, events_emitted,
bytes_emitted, last_sequence_number)`. Heartbeats go to a *separate* low-volume topic. Then:

```
completeness(tenant, minute) = events_received_in_pipeline / events_claimed_in_heartbeats
```

The sequence number is the backstop: gaps in a resolver's monotonic sequence prove loss even if
heartbeats themselves are lost, and the size of the gap quantifies it.

**SLO:** 99.99% of (tenant, hour) buckets have completeness ≥ 99.9%.

Read that carefully — it's a nested objective, and that structure matters. It says: within any hour, a
tenant may lose up to 0.1% of events (real networks drop packets); but the number of tenant-hours where
loss exceeds that must be under 0.01%. It separates "normal lossiness" from "something is broken."

**Catches:** silent drops, DLQ growth, resolver spool overflow, a consumer stuck on a partition, an
entire region's collectors failing.

There's a second, cheaper completeness check that catches different problems: **cross-store
reconciliation.** ClickHouse and S3/Iceberg are independent derivations of the same Kafka log, so their
counts must agree. Run hourly:

```sql
-- ClickHouse
SELECT tenant_id, toStartOfHour(ts) h, count() FROM dns_events
WHERE ts >= now() - INTERVAL 3 HOUR GROUP BY tenant_id, h
-- vs Iceberg via Trino, same grouping
```

Any disagreement means one sink is broken. This catches ClickHouse insert failures that the pipeline
thought succeeded, and it costs almost nothing.

---

**SLI 3 — Correctness.** *Is the data right?*

Decompose into checks you can actually run, because "is it right" isn't computable in general. Six
classes, each cheap:

1. **Schema conformance** — % of events matching the registered schema. Catches producer drift.
2. **Null rate per column, versus its historical baseline.** `latency_us` is normally 0.02% null;
   an alert at >1% would have caught the firmware bug in hours instead of 3 weeks. This is the single
   highest-value data-quality check and it's trivial to implement.
3. **Distribution drift** — the share of each `response_code` value. `NXDOMAIN` runs at 8–12% of
   traffic. If it hits 40%, either something is genuinely wrong in the customer's network (worth an
   alert to *them*) or our parser broke (worth an alert to *us*).
4. **Referential integrity** — every `tenant_id` in the stream exists in Postgres; every `resolver_id`
   maps to a known device. Violations mean routing or provisioning bugs.
5. **Enrichment freshness** — age of the threat-intel snapshot in use. This is the one that catches the
   5-day-stale feed. Treat "the age of every reference dataset the pipeline depends on" as a
   first-class metric; stale reference data is a top-three cause of silent wrongness.
6. **Duplicate rate** — `count() / uniqExact(event_id)` per tenant-hour. Should be ~1.0. Above 1.01
   means dedup isn't working, which (per Part 2) might mean someone put a mutable column in `ORDER BY`.

**SLO:** 99.9% of (dataset, hour) pass all correctness checks.

**Catches:** the silent-wrongness class, which is the class that damages trust most, because customers
find it before you do.

---

**SLI 4 — Query availability and latency.** *Can consumers get answers, fast enough?*

Conventional, but with a data-platform-specific twist: **stratify by query class**, because a single
latency SLO across all queries is meaningless when the workload spans 40ms dashboard hits and 4-minute
forensic scans.

| Class | Example | Availability SLO | Latency SLO |
| --- | --- | --- | --- |
| Dashboard (pre-aggregated) | "blocked domains, 24h" | 99.9% | p99 < 500 ms |
| Interactive search (raw, ≤7d) | "all queries from 10.2.4.19" | 99.5% | p99 < 5 s |
| Forensic (raw, ≤30d) | "this domain across all sites, 30d" | 99.0% | p99 < 60 s |
| Archive (S3/Iceberg, ≤400d) | "quarterly compliance export" | 99.0% | p95 < 15 min |

Availability here counts *successful* responses — an error, a timeout, and a query killed by a memory
limit all count as failures. A ClickHouse `MEMORY_LIMIT_EXCEEDED` is an availability failure even
though the server is up, and if you don't count it that way your availability number is fiction.

**Catches:** the shard-down case, memory-limit exhaustion, a bad query plan after a schema change,
noisy-neighbour contention, and the Glacier-transition case from §4.2 (which shows up as archive
latency going from minutes to hours).

---

### 4.4 From SLO to alert: burn-rate alerting, derived

Here's where most candidates stumble, so let's build it from scratch.

**The naive approach.** "Alert when freshness lag > 60 seconds." Try it: freshness crosses 60 seconds
briefly during every deploy, every ClickHouse merge spike, every Kafka rebalance. You get 30 pages a
week, you add a "for 5 minutes" clause, then a "for 15 minutes" clause, and now you don't find out
about real outages for 15 minutes. The threshold approach forces you to trade false positives against
detection time with one knob, and there's no good setting.

**The insight.** You don't care about a threshold crossing. You care about **whether you're going to
run out of error budget.**

Build it up. Your freshness SLO is 99.5% of tenant-minutes under 60 seconds, over a 30-day window. So
your error budget is 0.5% of tenant-minutes:

```
12,000 tenants × 60 min × 24 h × 30 days = 518,400,000 tenant-minutes in the window
0.5% of that = 2,592,000 tenant-minutes of allowed badness
```

Now define **burn rate** as: how fast are you consuming budget relative to consuming it evenly across
the window? A burn rate of 1 means you'll exactly exhaust the budget at the end of 30 days. A burn rate
of 14.4 means you'll exhaust it in 30/14.4 ≈ 2.08 days — and, usefully, 1 hour at burn rate 14.4
consumes exactly 2% of the budget (because 1 hour is 1/720 of 30 days, and 14.4/720 = 2%).

That gives the standard **multi-window, multi-burn-rate** alert set:

| Burn rate | Short window | Long window | Budget consumed | Action |
| --- | --- | --- | --- | --- |
| 14.4× | 5 min | 1 hour | 2% | **Page.** Budget gone in ~2 days. |
| 6× | 30 min | 6 hours | 5% | **Page.** Budget gone in ~5 days. |
| 3× | 2 hours | 1 day | 10% | Ticket. |
| 1× | 6 hours | 3 days | 10% | Ticket. |

**Why two windows per rule?** The long window decides whether it's real; the short window decides
whether it's *still happening*. Requiring both to be firing means you page on sustained problems and —
crucially — the alert **resolves quickly** when the problem stops, because the short window drains
fast. Long-window-only alerts stay firing for hours after recovery, which trains people to ignore them.

**Why page on the fast burns and ticket on the slow ones?** A 14.4× burn destroys a month's budget in
two days: it needs a human now. A 1× burn means you'll narrowly miss the SLO in three days: it needs
attention this week, not at 3am. This is the mechanism that converts "how bad is it" into "who do we
wake up," and it's the single most useful thing to be able to explain in an SLO interview.

**The multiplication problem, and the honest answer.** Four SLIs × four burn-rate rules × per-tenant
computation = a lot of alert rules and a lot of cardinality. If you naively make each of 12,000 tenants
its own alert series, you have 192,000 rules and a Prometheus that falls over.

What actually works: compute the SLI per tenant, but **alert on the aggregate "proportion of tenants
meeting objective"**, and attach the offending tenant list as a label-free annotation queried at alert
time. So the paging rule is "the fraction of tenants meeting the freshness objective has dropped such
that we're burning budget at 14.4×", and the alert body contains "affected tenants: 200, top 10 by
volume: [...]". One rule, full detail.

Then add a small number of **per-tenant rules for the whales only** — the top 20 tenants get individual
SLOs because a Meridian Financial outage is a business event on its own. That's 20 × 4 = 80 extra
rules, which is fine. **Tiering your alerting by tenant importance is the same insight as tiering your
architecture by tenant importance**, and consistency between the two is a good thing to point out.

### 4.5 Dashboards that are actually used

The failure mode is a wall of 60 graphs that nobody reads during an incident. Design for the questions
people ask, in the order they ask them. Four dashboards, not sixty.

**Dashboard 1 — "Is the platform healthy?" (the one on the wall).** Six tiles, no more:
1. The four SLIs as a single number each, with error budget remaining as a percentage bar.
2. Events/sec in versus out (the two lines that, when they diverge, mean lag).
3. A tenant heatmap: 12,000 cells coloured by SLO compliance. Human eyes find spatial patterns
   instantly — one red block means one region or one shard; scattered red means something else.

**Dashboard 2 — "Where is the problem?" (the incident dashboard).** Ordered along the pipeline so you
walk it left to right: resolver connections → collector rate and 429s → Kafka produce/consume/lag by
partition → processor throughput, DLQ rate, GC → ClickHouse insert latency, parts per partition, merge
backlog, disk free → S3 write rate and error rate. **Follow the data's path, not the org chart.** The
single most useful property of this dashboard is that the first graph that looks wrong tells you which
stage owns the incident.

**Dashboard 3 — "Which tenant?" (the drill-down).** Templated by tenant: their ingest rate versus their
baseline, their freshness, their completeness, their DLQ rate, their query latency, their quota
utilisation. Support uses this more than engineering does, which is the point — it deflects tickets.

**Dashboard 4 — "What is it costing?"** Covered in Part 11, but it belongs in the same family: cost per
tenant, cost per TB ingested, cost per query class, storage by tier. Cost is an operational signal, not
a finance report — a cost graph that jumps 40% overnight is usually an incident.

**Two things to say about dashboards that show seniority:**

*Every alert must link to the dashboard and the runbook that resolves it.* An alert that says
"FreshnessBurnRateHigh" with no link costs the responder five minutes of navigation at the worst
possible time.

*Dashboards should show the SLO line drawn on the graph.* A latency graph without the objective marked
requires the viewer to remember the target. Drawing it converts "is 340ms bad?" into a glance.

### 4.6 Tier 1 questions — screening

**Q4.1: What's the difference between an SLI, an SLO, and an SLA?**

*Model answer:* An **SLI** is a measurement — "the proportion of tenant-minutes in the last 30 days
where the newest queryable event was less than 60 seconds old." It's a number you compute; it has no
opinion.

An **SLO** is a target for that number that you set internally — "≥ 99.5%." It's the level at which you
consider the service healthy, and it's the thing that generates an error budget: 0.5% of tenant-minutes
may be bad. The budget is what lets you make trade-offs rationally — with budget remaining you ship
faster; with budget exhausted you stop feature work and fix reliability.

An **SLA** is a contractual promise to a customer with a financial consequence — "99.0% freshness or
you get a 10% service credit." SLAs should always be **looser than SLOs**, with real margin, because
you want to be alerted and to have fixed the problem long before you owe anyone money. Skyline's
enterprise contract says 99.0%; our internal SLO is 99.5%. That 0.5% gap is the buffer that keeps
engineering decisions out of the legal department.

The practical implication people miss: **you should have far fewer SLAs than SLOs**, and every SLA
should map to an SLO you've been measuring for at least two quarters. Signing an SLA for something you
haven't measured is how you end up owing credits.

**Q4.2: Give me three SLIs for a data ingestion pipeline.**

*Model answer:* Freshness, completeness, and correctness. Briefly: freshness is end-to-end event-time
lag measured by synthetic canaries at the query boundary — it catches everything that makes data late.
Completeness is received-versus-claimed, using out-of-band producer heartbeats and sequence gaps — it
catches silent loss, which no service-level metric will show you. Correctness is a suite of assertions
— null rates against baseline, distribution drift, enrichment-source age, duplicate ratio — it catches
the class where data arrives on time and is wrong, which is the class that destroys customer trust.

I'd add query availability as a fourth if we're counting the serving side, and I'd deliberately *not*
include CPU, memory, or pod restarts as SLIs. Those are diagnostic signals, not indicators of service
level — a pipeline can be at 90% CPU and perfectly healthy, or at 20% CPU and dropping everything.

**Q4.3: Your freshness SLO is 99.5% and you're at 99.2%. What do you do?**

*Model answer:* First, check whether the budget is actually exhausted or just burning — those need
different responses. 99.2% against a 99.5% target over 30 days means I've used 0.8/0.5 = 160% of the
budget, so it's spent and I'm in violation.

Then the sequence:
1. **Confirm the measurement.** More SLO violations are measurement bugs than people expect —
   a canary prober that itself was slow, or a tenant sample that over-weights a broken region.
2. **Find the shape.** Was it one 4-hour incident (an availability problem) or continuous 0.8%
   degradation (a capacity problem)? Completely different fixes. Look at the burn-rate history.
3. **If it's an incident**, the budget spend is already sunk; the work is the postmortem action items.
4. **If it's continuous degradation**, that's capacity, and it usually means growth outran provisioning.
   Find the constrained resource and add capacity.
5. **Invoke the error budget policy**, which should have been agreed in advance: budget exhausted means
   reliability work takes priority over feature work until the trailing window recovers. The value of
   having agreed this in advance is that it's a policy, not an argument.
6. **Consider whether the SLO is right.** If we've missed three months running and customers haven't
   complained, the objective may be tighter than the business needs, and the honest move is to propose
   loosening it deliberately rather than living permanently in violation — a chronically-violated SLO
   teaches everyone to ignore SLOs.

### 4.7 Tier 2 questions — design

**Q4.4: Design end-to-end monitoring for the Skyline pipeline. Be specific about what you emit and
where.**

*Model answer:* Three layers, and the discipline is that each layer answers a different question.

**Layer 1 — SLIs (the "is it healthy" layer).** Four series, computed per tenant, aggregated for
alerting:
- `freshness_lag_seconds{tenant, percentile}` from the canary prober.
- `completeness_ratio{tenant, hour}` from heartbeat reconciliation.
- `correctness_checks_passed{dataset, check}` from the quality runner.
- `query_success_ratio{class}` and `query_latency_seconds{class, quantile}` from the query gateway.

**Layer 2 — pipeline internals (the "where is it broken" layer).** Emitted by each stage:
- Collector: `events_received_total{tenant, result}`, `auth_failures_total`, `quota_rejects_total{tenant}`,
  `produce_latency_seconds`.
- Kafka: per-partition `consumer_lag_records` and `consumer_lag_seconds` (time-based lag is far more
  useful than record-based — 100,000 records behind means nothing without a rate), broker disk, ISR
  shrink events.
- Processor: `batch_size_rows`, `batch_duration_seconds`, `enrichment_source_age_seconds`,
  `dlq_events_total{tenant, reason}`, `offset_commit_lag`.
- ClickHouse: `parts_per_partition` (**the leading indicator**, alert at 100, well below the 150
  throttle), merge backlog and merge duration, insert latency p99, `MEMORY_LIMIT_EXCEEDED` count,
  replication queue depth, disk free percentage.
- S3: PUT/GET rate, 503 SlowDown count, multipart failures, Iceberg commit conflicts.

**Layer 3 — tracing.** A trace ID stamped on each batch at the collector and propagated through Kafka
headers into the processor and into the ClickHouse `query_id`. When one tenant reports slowness you can
follow one batch end to end. Sample at 0.1% normally, 100% for tenants under investigation — a sampling
rate that's controllable from the control plane, so turning it up doesn't require a deploy.

**And the meta-layer:** monitor the monitoring. The canary prober is a single point of failure for the
freshness SLI, and a broken prober looks exactly like perfect health. So: a heartbeat on the prober
itself, and an alert if the number of probe results in the last 5 minutes drops below expected. **Any
metric whose absence looks like success needs an explicit "is this metric flowing" alert.** That
principle applies to almost every data-quality check.

**Q4.5: How do you set the *right* SLO number? Why 99.5% and not 99.9%?**

*Model answer:* Not by picking a number that sounds good. Three inputs, in this order:

**1. What do users actually need?** Talk to them, and get specific. Skyline's security analysts respond
to alerts; if data is 60 seconds stale that's invisible in their workflow, and at 15 minutes they start
missing things during an active incident. So the *threshold* is set by user need: 60 seconds is
comfortably inside "invisible," and 5 minutes is the "degraded but usable" line. That's where the two
thresholds in §4.3 came from — user experience, not round numbers.

**2. What can we actually achieve, measured?** Instrument first, set targets second. If historical
measurement shows we hit 60 seconds 99.3% of the time with the current architecture, then a 99.9% SLO
is a commitment to build something we don't have. That's a legitimate choice, but it should be a funded
project, not a number in a doc. Setting an SLO you're structurally unable to meet just means permanent
violation and ignored alerts.

**3. What does each nine cost?** This is the argument that actually decides it. Going from 99.5% to
99.9% means cutting bad tenant-minutes by 5×. Concretely for Skyline that means multi-region collector
redundancy, a hot-standby ClickHouse cluster, and doubled Kafka capacity for burst absorption — call it
$40K/month plus meaningful ongoing complexity. Then ask the product owner: is 60-second freshness at
99.9% instead of 99.5% worth $480K/year? For a security product where the difference is a handful of
alert-latency events per year, usually not. For the whale tenants specifically, maybe yes — which is
exactly why Skyline gives the top 20 tenants a tighter SLO on dedicated infrastructure. **Differentiated
SLOs by tier is the answer that gets both economics right.**

The framing to say out loud: "an SLO is a statement about how much unreliability we're willing to pay
to avoid. If you can't name what the next nine costs, you're not setting an SLO, you're expressing a
wish."

**Q4.6: You have 12,000 tenants. How do you do per-tenant SLOs without exploding your metrics system?**

*Model answer:* You don't put 12,000 tenants in a Prometheus label. At 12,000 tenants × 4 SLIs × 5
quantiles you're at 240,000 series for the SLIs alone, and once you cross them with any other dimension
you're into the millions, where Prometheus falls over.

Three techniques, used together:

**Tier the tenants.** Top 20 whales get individual, first-class metric series and individual alerts —
40 tenants' worth of cardinality is nothing. The remaining 11,980 are measured individually but
*reported* as a distribution: "number of tenants in each SLO-compliance bucket." You keep detection;
you drop per-tenant series.

**Compute SLIs in ClickHouse, not in Prometheus.** This is the move people miss, and it's a natural one
for a data platform: you already have a columnar database designed for exactly this. Write per-tenant
per-minute SLI observations into a ClickHouse table (12,000 × 1,440 = 17.3M rows/day, which is nothing
at Skyline's scale), and compute compliance with SQL:

```sql
SELECT
    countIf(freshness_p99 < 60) / count() AS pct_tenant_minutes_ok
FROM sli_observations
WHERE minute >= now() - INTERVAL 30 DAY
```

Then export a handful of aggregate numbers to Prometheus for alerting. Prometheus gets 10 series;
ClickHouse holds the detail and answers "which tenants?" on demand. This also gives you 30-day and
90-day SLO windows for free, which Prometheus is bad at.

**Sample the long tail for expensive probes.** The synthetic canary costs a real event and a poll loop
per tenant. Probing all 12,000 every 30 seconds is 24,000 probes/minute for very little marginal
information about tenants doing 1 event/sec. Stratified sampling — all whales, 100 mid-market, 200 long
tail, rotating — gives statistically sound coverage at 3% of the cost. Be explicit that this is a
sample and state the confidence: with 200 of 9,879 long-tail tenants sampled, you detect a problem
affecting 5% of them with >99% probability within a few cycles.

### 4.8 Tier 3 questions — deep dive and adversarial

**Q4.7: Your SLO says 99.9% and the dashboard is green, but a customer says data is missing. Who's
wrong?**

*Model answer:* Assume the customer is right and the SLI is wrong until proven otherwise. Customers
don't usually report problems that aren't there, and green-dashboard-plus-unhappy-customer is a
measurement bug in the overwhelming majority of cases. Five specific ways the SLI can be lying:

1. **Wrong aggregation.** The §4.2 case: a volume-weighted average hiding a tenant-shaped failure. Check
   the metric definition — is it "proportion of tenant-minutes meeting objective" or an average?
2. **Wrong scope.** The SLI measures the pipeline from collector to ClickHouse, but the customer's data
   never reached the collector — their resolver's spool overflowed, or auth was failing. **The SLI's
   measurement boundary doesn't include the failure.** This is extremely common and it's why
   completeness must be anchored on producer-side claims rather than pipeline-side receipts.
3. **Wrong dimension.** Freshness and completeness are both fine in aggregate but one *column* is null
   — the firmware bug from §4.2. Row-level SLIs are blind to column-level failures.
4. **Sampling gap.** The customer is one of the 9,879 long-tail tenants not in the probe sample.
5. **Different definition of "missing."** The customer queried with a filter on a field whose semantics
   changed, or their timezone differs, or they're looking at a rollup that excludes a category. The data
   is present; their view of it isn't.

The process: reproduce their exact query, compare against the raw event stream in Kafka/S3 for that
tenant and window, and identify at which stage the events disappear. Then — this is the part that
matters — **fix the SLI, not just the incident.** Every "green dashboard, unhappy customer" event should
produce a new or corrected SLI, because it's proof that your measurement has a blind spot. Add that to
the postmortem template as a required field.

**Q4.8: How do you alert on something that's absent — data that should have arrived and didn't?**

*Model answer:* Absence is the hardest alerting problem because the natural implementation
(`if metric > threshold`) has no expression for "the metric stopped existing." A series that vanishes
looks identical to a series that's fine, and in Prometheus a query over a missing series returns no
data, which by default doesn't fire.

Three mechanisms:

**1. Expected-arrival scheduling.** Maintain a table of expected data arrivals — "tenant 4471 emits at
least one event every minute," "the threat-intel feed refreshes every 6 hours," "the nightly export
lands by 06:00 UTC." A watchdog compares expectation to observation and alerts on the gap. Derive the
expectations from history rather than hand-maintaining them (a tenant's normal cadence over the last 7
days, with a tolerance band), because hand-maintained expectations rot.

**2. Alert on the absence explicitly.** In Prometheus terms, `absent()` and `absent_over_time()`, or
better, always emit a zero rather than nothing — a counter that goes to 0 is visible; a counter that
disappears is not. Make every pipeline stage emit a heartbeat gauge unconditionally, even when it has
processed nothing.

**3. Dead-man's switches.** The monitoring system itself must be monitored by something outside it. A
job that pings an external service (Dead Man's Snitch, or a Cloudwatch alarm in a different account)
every 5 minutes; if the ping stops, that external service alerts. Otherwise a monitoring outage looks
exactly like perfect health — and the day your Prometheus dies is a good day for something else to die
too.

For Skyline specifically the highest-value absence alerts are: a tenant whose event rate drops below
20% of its trailing-7-day baseline for that hour-of-week (catches resolver outages, which are the
customer's problem but our support ticket); a partition with no consumer progress for 5 minutes;
enrichment-source age exceeding 2× its refresh interval; and any lifecycle reconciler that hasn't
reported in 30 minutes. Note the hour-of-week baseline rather than a flat threshold — traffic at 03:00
Sunday is legitimately 10% of Tuesday noon, and a flat threshold pages every weekend.

**Q4.9: Your on-call is getting 40 pages a week. Fix it.**

*Model answer:* Forty pages a week means the alerting is broken, and I'd treat it as a reliability
project with its own metrics, not as a tuning exercise.

**Measure first.** For four weeks, classify every page: what fired, was it actionable, did the responder
do anything, was there customer impact. You'll typically find the distribution is something like: 50%
from three noisy rules, 25% duplicates of the same underlying event, 15% actionable-but-not-urgent, 10%
genuinely urgent. That distribution tells you exactly what to fix and in what order.

**Then, in order of impact:**

1. **Delete alerts nobody acts on.** If a page fired 30 times and the responder acknowledged and did
   nothing 30 times, it isn't an alert, it's a notification. Move it to a dashboard or a daily digest.
   Deleting alerts feels risky and is almost always right — an alert that's never acted on has negative
   value because it degrades attention for the ones that matter.
2. **Convert threshold alerts to burn-rate alerts.** Most of the noise is transient threshold crossings
   during normal operation. Burn-rate alerting (§4.4) structurally eliminates this class because a
   30-second blip can't consume 2% of a monthly budget.
3. **Deduplicate causally.** One ClickHouse shard going down currently fires: insert failures, query
   errors, freshness lag, completeness gaps, and replication lag — five pages for one event. Group
   alerts by inferred cause and send one notification with the five symptoms as context. Alertmanager
   inhibition rules, or a dependency graph where a parent alert suppresses its children.
4. **Route by urgency, honestly.** Only page for things that need action within minutes *and* have
   customer impact. Everything else is a ticket in the morning. The test to apply per rule: "if this
   fires at 3am and the responder sleeps through it until 8am, what is the incremental damage?" If the
   answer is "none," it's not a page.
5. **Every page must have a runbook** with the specific first three commands to run. If someone can't
   write those three commands, the alert isn't well-defined enough to page on.

**Set a target and track it.** "Fewer than 2 pages per on-call shift, and >80% of pages actionable."
Review it weekly with the same seriousness as an SLO — because on-call health *is* a reliability
property. A team that's exhausted from pages misses the real incident.

### 4.9 Case study: designing the SLO for a brand-new dataset

**Scenario:** "Product wants to launch a new dataset: DNS-over-HTTPS session records, ~40,000
events/sec, needed by a real-time blocking feature. They want an SLO before launch. You have no
production data. Walk me through it."

**Step 1 — Refuse to set a number yet, and say why.** An SLO with no measurement is a guess that becomes
a commitment. What I *will* commit to before launch is (a) the SLI definitions, (b) the instrumentation,
and (c) a date by which we'll propose numbers based on real data. Typically 4–6 weeks after launch. Say
this plainly; product people accept it readily when you give them a date.

**Step 2 — Work backwards from the product requirement, which does give you a hard constraint.** The
feature blocks malicious DoH sessions in real time. Ask: what's the latency budget for a block decision?
If the product needs to block within 2 seconds of the session starting, and network plus decision plus
enforcement consume 1.2 seconds, then **the data pipeline gets 800ms** — and that's not an SLO you
negotiate, it's a functional requirement. It also immediately tells you the architecture: 800ms rules
out the batch path entirely, and probably rules out going through ClickHouse at all for the blocking
decision. The blocking path reads from a streaming state store; ClickHouse gets the same events for
analytics on a relaxed timeline.

**That's the most important move in this question:** the freshness requirement determines the
architecture, so extract it before designing. A candidate who designs first and asks about latency
afterward has the dependency backwards.

**Step 3 — Define SLIs, splitting the two paths** because they now have genuinely different objectives:

*Blocking path:* decision latency p99 (target derived: < 800ms), decision availability (a
failed decision must fail *open* — never block legitimate traffic because our pipeline is slow — so
there's also a "fail-open rate" SLI that product cares about a lot), and decision correctness measured
against a labelled sample.

*Analytics path:* the standard four from §4.3, with freshness relaxed to 60 seconds because nothing
real-time depends on it.

**Step 4 — Instrument before launch, including the SLI computation itself.** Ship the canary prober, the
heartbeat completeness accounting, and the correctness checks *with* the feature, not after. Retrofitting
SLIs onto a live pipeline is 3× the work and you lose the launch-period baseline, which is the most
informative data you'll ever have about the system's natural variance.

**Step 5 — Launch with a "provisional SLO" and a review date.** Set deliberately loose provisional
numbers, alert only on catastrophic burn (14.4×), and gather data. At week 6, compute the achieved
distribution and propose real numbers: "we achieved p99 of 340ms with 99.7% of session-minutes under
800ms; I propose an SLO of 99.5% under 800ms, which gives us headroom for 2× growth before we need to
re-architect."

**Step 6 — Capacity-plan against the SLO, and be explicit about the growth cliff.** 40,000 events/sec
is 27% of current total volume — a significant addition. Model where the SLO breaks: at what event rate
does p99 exceed 800ms? If the answer is 65,000/sec, then you have 60% headroom and you should set a
capacity alert at 55,000/sec so you get a quarter's warning rather than a surprise.

**The closing point:** "The deliverable before launch isn't an SLO, it's the ability to have an
informed conversation about one in six weeks. Committing to a number now would either be so loose it's
meaningless or so tight we'd violate it immediately, and both damage the credibility of every other SLO
we have."

---

## Part 5 — A flexible storage layer for transactional, analytic, and ML workloads

### 5.1 What the interviewer is actually testing

The word "flexible" in this bullet is a trap. The naive reading is "build one store that does
everything," and candidates who go there get taken apart, because that store doesn't exist and the
attempts to build it (HTAP databases) have specific, well-understood limits.

The rubric:

1. Do you understand *why* transactional, analytic, and ML workloads want physically different storage,
   at the level of disk layout and CPU cache behaviour — not just "OLTP is row, OLAP is column"?
2. Do you reach for polyglot persistence and then, crucially, solve the problem polyglot persistence
   creates — consistency between copies?
3. Do you know what ML workloads need that analytics workloads don't? (Point-in-time correctness,
   reproducibility, and low-latency single-entity lookup. Most candidates name none of these.)
4. Can you define "flexible" as a property of the *interfaces and primitives*, not of a single engine?
5. Do you know when to stop? A three-store architecture for a startup with 200 GB is malpractice.

### 5.2 The mental model: why one store can't do all three

Let's derive the conflict from physics rather than asserting it, because the derivation is what makes
the answer convincing.

**Consider one query from each workload, against Skyline's data.**

*Transactional:* "Insert a new policy rule for tenant 4471, and atomically update the policy's version
and the audit log." Touches 3 rows across 3 tables. Must be atomic. Must be visible immediately.
Concurrency: hundreds of these per second from different tenants. Latency budget: 10ms.

*Analytic:* "For tenant 4471, over the last 30 days, count events grouped by threat category." Touches
2.3 billion rows, reads 2 columns out of 16, returns 120 rows. Latency budget: 2 seconds.

*ML training:* "Give me, for each of 40 million (client_ip, hour) pairs over 90 days, the 24 features
we computed — as they were known at that hour, not as we know them now." Touches billions of rows,
reads all columns, returns a 40M-row dataset, and must be **reproducible six months later**.

*ML inference:* "For client_ip 10.2.4.19 right now, give me its 24 features." Touches one entity.
Latency budget: 5ms. Called 40,000 times per second.

Now look at what each implies for physical layout.

**The transactional query wants row storage.** All three tables' rows must be updated atomically, and
each update touches a whole row. If the row's 20 columns are scattered across 20 separate files (the
columnar layout), a single-row insert means 20 writes and a single-row read means 20 seeks. Row storage
puts the whole row contiguously: one 8 KB page read gets you everything. It also wants **in-place
mutability with locking and MVCC**, because concurrent writers must not corrupt each other, and it wants
a write-ahead log so a commit is durable after one sequential fsync.

**The analytic query wants column storage,** and here's the arithmetic that proves it. Reading 2 of 16
columns from 2.3 billion rows:

```
Row store:    2.3e9 rows × 400 bytes = 920 GB read from disk (you read whole rows to get 2 columns)
Column store: 2.3e9 rows × ~5 bytes (2 compressed columns) ≈ 11.5 GB read
```

An **80× difference in bytes read**, before you count the compression advantage that comes from
sorting similar values together. And there's a second, less obvious win: columnar layout puts values of
the same type adjacently in memory, so aggregation runs as a tight SIMD loop over a contiguous array —
processing 8 or 16 values per CPU instruction. A row store's aggregation loop strides through memory
with a 400-byte stride, missing cache on every row.

Then the reverse: the columnar store is terrible at the transactional query. Updating one row means
touching 16 column files. ClickHouse's answer is to not support it — `ALTER TABLE ... UPDATE` is an
asynchronous mutation that rewrites entire parts, taking minutes to hours. There is no row-level lock,
no multi-table transaction, no `SELECT ... FOR UPDATE`. **This isn't a missing feature; it's the
consequence of the layout that makes the analytic query 80× faster.**

**ML training wants immutable, versioned, columnar files** — it reads everything, repeatedly, from many
parallel workers, and it needs the *exact same bytes* six months later to reproduce a model. That's
S3 + Parquet + a table format that snapshots. It doesn't want ClickHouse, because ClickHouse's parts
merge and change underneath you (that's the whole point of MergeTree), so "the table as of March" isn't
a thing ClickHouse can give you.

**ML inference wants a key-value store.** One entity, 5ms, 40,000 QPS. That's a hash lookup. Neither a
row store scanning a B-tree nor a column store assembling 24 column reads is the right shape; you want
Redis or DynamoDB with the feature vector serialised as one value.

So:

> **Four workloads, four physical layouts, and the layouts are mutually exclusive at the storage-engine
> level.** "Flexible" cannot mean one engine. It must mean something else.

**What about HTAP?** Systems like TiDB, SingleStore, and Postgres with a columnar extension (Citus,
Hydra) genuinely do both by maintaining two representations — a row store for writes and a column store
for reads, with replication between them. That's a real option and you should know it. Its honest
limits: the column replica lags the row store (so you don't get transactional consistency in analytics
anyway, which was the main reason to want HTAP); it's a smaller operating envelope than dedicated
systems at either end; and at Skyline's 13 billion rows/day the analytic side would need to be
ClickHouse-class anyway. **HTAP is excellent in the 100 GB–5 TB range where operating two systems is
disproportionate overhead. At 260 TB it isn't the answer.** Saying that — with the crossover point
named — is much stronger than dismissing HTAP outright.

### 5.3 What "flexible" actually means

Redefine it. Flexibility is not one engine serving all workloads; it's a layer where **adding a new
workload doesn't require re-ingesting or re-modelling the data.** Concretely, four properties:

1. **One durable source of truth that every store derives from.** For Skyline: the Kafka log for the
   recent window, S3/Iceberg for all of history. Every other store — ClickHouse, Redis, a future
   vector index — is a *materialisation* of that, rebuildable from it. This is what makes new workloads
   cheap: a new consumer group, not a new pipeline.
2. **An open table format at the base layer.** Iceberg (or Delta/Hudi). This is the load-bearing choice:
   it means Spark, Trino, ClickHouse, Athena, DuckDB, Snowflake, and BigQuery can all read the *same
   files* without copying. Without it, "supporting a new workload" means "exporting a copy," and you
   get the copy-proliferation problem where nobody knows which copy is authoritative.
3. **Schema and semantics defined once, centrally.** A schema registry plus a semantic/metric layer, so
   "monthly active resolvers" means the same thing in the dashboard, the ML feature, and the invoice.
   Three teams independently defining the same metric slightly differently is the most common cause of
   "the numbers don't match" escalations.
4. **A small set of composable primitives rather than bespoke pipelines.** Covered properly in Part 10,
   but it's the same idea: `ingest → land → transform → materialise → serve`, with each step a
   configured instance of a shared component.

### 5.4 The architecture

```
                          ┌──────────── CONTROL PLANE (Postgres) ─────────────┐
                          │ tenants · policies · schema registry · catalog    │
                          │ retention · lineage · access grants · quotas      │
                          │ THE OLTP WORKLOAD LIVES HERE                      │
                          └────────────────────┬──────────────────────────────┘
                                               │ governs
   ┌───────────────────────────────────────────┼─────────────────────────────────────────┐
   │                              DATA PLANE   │                                         │
   │   Kafka (7d log, source of truth for hot) │                                         │
   │        │                                  ▼                                         │
   │        ├──► ClickHouse ──── serves: dashboards, interactive search, forensics       │
   │        │    (hot 30d, tiered to S3-backed disk; rollup MVs for the fast path)       │
   │        │                                                                            │
   │        ├──► S3 + Iceberg ── serves: ML training, ad-hoc analytics via Trino,        │
   │        │    (400d, Parquet)  compliance exports, and REBUILDING CLICKHOUSE          │
   │        │                                                                            │
   │        └──► Redis / DynamoDB ── serves: online feature lookup for real-time scoring │
   │             (current feature vectors, TTL'd)                                        │
   └────────────────────────────────────────────────────────────────────────────────────┘
```

Three data-plane stores, each chosen for a layout, all derived from one log, all describable in one
catalogue. Note what's *not* here: no separate "reporting database," no per-team copies, no
Elasticsearch. Each of those would be a fourth materialisation, and the discipline is that **a new
store must justify itself by a workload the existing three genuinely cannot serve.**

Now walk the three interesting design decisions inside this.

**Decision 1 — the medallion layering in S3.** Raw data is not the same as usable data, and conflating
them is how lakes become swamps. Three zones:

- **Bronze (raw)** — exactly what arrived, unmodified, including malformed records. Partitioned by
  arrival date. Retained per policy. **This is the "we can always rebuild" guarantee**; never transform
  in place, never delete except by policy.
- **Silver (cleaned/conformed)** — parsed, typed, deduplicated, enriched, conformed to the registered
  schema. This is what 90% of consumers should read. Partitioned by event date.
- **Gold (aggregated/curated)** — business-level tables: daily per-tenant rollups, the ML feature
  tables, the billing aggregates. Small, heavily used, well-documented.

The value of the split is that each zone has different guarantees and different consumers, and a bug in
the Silver transform is recoverable from Bronze without re-ingesting from the edge. The failure mode to
warn about: teams reading Bronze directly because Silver was late. Once that happens you have two
definitions of the data. Make Silver's freshness an SLO so nobody has a reason to.

**Decision 2 — ClickHouse's role is a materialised view of the lake, not a separate system of record.**
This is a mindset shift worth stating explicitly, because it changes how you handle every incident:
if ClickHouse loses a shard, you don't restore from a ClickHouse backup; you re-derive from Iceberg.
Practically, keep a `restore_from_lake.sql` runbook that does:

```sql
INSERT INTO dns_events
SELECT * FROM iceberg('s3://skyline-lake/silver/dns_events', ...)
WHERE ts >= '2026-09-01' AND ts < '2026-09-02' AND tenant_id % 8 = 3;  -- one shard, one day
```

Test it quarterly. A restore path you haven't run is a restore path you don't have.

**Decision 3 — the ML path gets a feature store, and here's why it isn't optional.**

### 5.5 What ML workloads need that analytics doesn't

This is where most candidates are thin, so it's a high-leverage area to be strong in. Three requirements
that pure analytics never surfaces.

**Requirement 1 — point-in-time correctness (avoiding label leakage).**

Skyline trains a model to predict whether a DNS query is malicious. A feature is "number of distinct
domains this client IP queried in the previous hour." Training data is built by joining historical
events with those features.

The naive join is:

```sql
-- WRONG: leaks the future
SELECT e.event_id, e.label, f.distinct_domains_1h
FROM events e JOIN features f ON e.client_ip = f.client_ip
```

`features` holds the *current* feature values. So a training row from March gets a feature computed
from September data. The model learns from information that didn't exist at prediction time. It scores
beautifully offline and fails in production — the classic and expensive failure.

The correct join is **as-of**: for each event, take the feature value as of that event's timestamp:

```sql
SELECT e.event_id, e.label, f.distinct_domains_1h
FROM events e
ASOF LEFT JOIN features f
  ON e.client_ip = f.client_ip AND e.ts >= f.computed_at
```

ClickHouse has native `ASOF JOIN`, which is a genuine advantage worth mentioning — it does exactly this
"most recent row with key match and timestamp ≤" semantics efficiently. In Spark you'd do it with a
window function, and in a feature store it's the built-in `get_historical_features` primitive.

**The storage implication:** feature tables must be **append-only with a validity timestamp**, never
updated in place. If you overwrite a feature value, point-in-time correctness becomes impossible
forever, because the history is gone. That single constraint shapes the whole feature table design and
it's the thing to say out loud.

**Requirement 2 — reproducibility.**

Six months after a model ships, someone asks why it flagged a specific customer's traffic. You must
reproduce the training set exactly. That requires: immutable data (Iceberg snapshots), a recorded
snapshot ID on every training run, versioned feature definitions, and versioned transformation code.
The training run's metadata is:

```
model_v4.2 → iceberg snapshot 7738291847362 → feature_defs git sha a4f81c2 → training code sha 91bc3f0
```

All four, or it isn't reproducible. Iceberg's time travel makes the first one a one-liner:

```sql
SELECT * FROM silver.dns_events FOR VERSION AS OF 7738291847362
```

Without a table format that snapshots, "the data as of March" requires you to have copied it in March,
which is how people end up with a `training_data_v4_final_FINAL/` directory on S3.

**Requirement 3 — training/serving skew elimination.**

The feature "distinct domains in the last hour" is computed two ways: in Spark over Iceberg for
training, and in the streaming path for inference. Two implementations, two languages, two subtly
different definitions of "the last hour" (sliding versus tumbling? inclusive of the current event or
not?). The model then sees different distributions at serving time than at training time and degrades
silently.

The fixes, best to worst:
1. **One definition, two execution modes** — define the feature once in a transformation DSL/SQL that
   both the batch and streaming engines execute. Flink SQL, or a feature store's transformation layer.
2. **Log the serving-time features** and use *those* as training data. This is often the most practical
   answer: you're guaranteed no skew, because training data is literally what serving saw. Cost: you
   can't backfill a new feature without waiting for it to accumulate.
3. **Continuous skew monitoring** — compute both and alert when distributions diverge (population
   stability index, or a simple per-feature p50/p99 comparison). Do this regardless of which of the
   above you chose, because skew creeps back in.

**Do you need a feature store product?** Honest answer: not necessarily. A "feature store" is three
capabilities — an offline store (Iceberg tables, which you have), an online store (Redis, which is one
component), and a registry mapping feature names to definitions and to both stores. If you have 20
features and one model, build the registry as a Postgres table and skip the product. If you have 200
features across 8 models and 5 teams, the coordination problem is real and Feast/Tecton/Databricks
Feature Store earns its keep. **Skyline at 24 features and 2 models should build the thin version and
revisit at 100 features.** Naming that threshold shows you're optimising for the actual situation and
not resume-driven.

### 5.6 Tier 1 questions — screening

**Q5.1: Why not just use Postgres for everything?**

*Model answer:* For the control plane, we do — 40 GB, 800 tx/sec, complex relational constraints,
transactions. Postgres is exactly right and I'd resist any pressure to move it.

For the event data, run the numbers. 13 billion rows/day at 400 bytes is 5.2 TB/day. In 30 days that's
156 TB in a row store before indexes, and indexes on a table like this can easily add 50%. A single
Postgres instance tops out well before that — the largest practical single-node Postgres is a few tens
of terabytes, and at that size vacuum, index maintenance, and backup windows become the dominant
operational problem.

But size isn't even the main argument; **layout is**. The characteristic query is "aggregate 2 columns
over 2.3 billion rows." In Postgres that reads whole rows: ~920 GB of I/O. In ClickHouse it reads 2
compressed columns: ~11.5 GB. That's 80× less I/O, and then ClickHouse aggregates it with vectorised
SIMD over contiguous arrays. The measured difference on this workload is typically two orders of
magnitude, and no amount of Postgres tuning closes it because it's a property of the storage layout.

I'd also note where Postgres *could* stretch further than people think: with declarative partitioning,
BRIN indexes on the timestamp, and aggressive pre-aggregation, Postgres handles surprisingly large
append-only time-series workloads — TimescaleDB exists for exactly this and is a legitimate choice up
to maybe 10–20 TB. **The decision point is roughly: below a few TB with modest query concurrency,
Postgres and simplicity win; above that, columnar wins decisively.** Skyline is 50× past that line.

**Q5.2: Why not just use ClickHouse for everything?**

*Model answer:* Because the control plane needs things ClickHouse deliberately doesn't provide.

*No multi-statement transactions.* Creating a policy rule means inserting the rule, bumping the policy
version, and writing an audit row atomically. In ClickHouse there's no way to make those three either
all-happen or all-not-happen. A crash mid-sequence leaves a policy whose version doesn't match its
rules, which is a correctness bug in a security product.

*No enforced constraints.* No foreign keys, no unique constraints. `resolvers.tenant_id` referencing
`tenants.tenant_id` is enforced by the database in Postgres and by hope in ClickHouse. For 1.4 million
policy rules edited by customers through a UI, hope is not adequate.

*Updates and deletes are wrong-shaped.* A customer editing a policy rule is a single-row update.
ClickHouse mutations rewrite parts. `ReplacingMergeTree` can simulate updates, but reads then need
`FINAL` or `argMax`, and you've made every read of your configuration data more expensive and more
subtle. For a table read on every request path, that's a bad trade.

*Concurrency model.* ClickHouse is built for a few heavy queries, not thousands of tiny ones. Default
`max_concurrent_queries` is 100; each query is designed to use many threads. A workload of 800
single-row point queries per second is the opposite of what it's optimised for.

The clean framing: **ClickHouse is optimised for reading a lot of rows a few times; Postgres is
optimised for reading a few rows a lot of times.** Skyline needs both, so it runs both, and the
boundary is control plane versus data plane.

**Q5.3: How do you keep Postgres and ClickHouse in sync?**

*Model answer:* Start by challenging the premise, because "in sync" usually means two different things
and only one of them is a real requirement.

*Config data flowing into the query path.* ClickHouse queries need tenant names, policy metadata, and
resolver-to-site mappings — data that lives in Postgres. The right mechanism is **ClickHouse
dictionaries**: an external dictionary sourced from Postgres, refreshed on an interval, held in memory,
and joined at query time with `dictGet()`. This is dramatically faster than an actual join (it's a hash
lookup, not a distributed join) and it's the idiomatic answer.

```sql
CREATE DICTIONARY tenant_dict (
    tenant_id UInt32, name String, tier String, region String
) PRIMARY KEY tenant_id
SOURCE(POSTGRESQL(host 'pg-ro' db 'skyline' table 'tenants' user '...' password '...'))
LAYOUT(HASHED()) LIFETIME(MIN 300 MAX 600);

-- then in queries:
SELECT dictGet('tenant_dict', 'name', tenant_id) AS tenant, count()
FROM dns_events WHERE ts >= today() GROUP BY tenant_id;
```

The 300–600 second lifetime means config changes propagate within ~10 minutes, which is fine for
display names and dangerous for anything security-relevant — so **access-control decisions must not be
made from a stale dictionary.** Those go through the control plane at query-admission time instead.

*Event data flowing into Postgres.* This should be rare and I'd push back on most requests for it. If
something needs event aggregates in Postgres (say, a dashboard widget wanting a precomputed daily
count), write the aggregate from a scheduled job rather than trying to replicate the event stream.
Small, derived, and clearly marked as derived.

*What I would not do:* dual-write from the application to both stores. You get no atomicity across
them, so a failure between the two writes leaves them permanently divergent and there's no reconciler
to notice. If Postgres data genuinely must reach ClickHouse continuously, use **CDC** — Debezium
reading the Postgres WAL via logical replication into Kafka, then into a `ReplacingMergeTree` keyed on
the primary key with the LSN as the version column. That's a real pattern with a real cost (a
replication slot that can bloat WAL if the consumer stalls — monitor `pg_replication_slots.
restart_lsn` lag religiously), and I'd only take it on if the requirement justified it.

### 5.7 Tier 2 questions — design

**Q5.4: Design the storage layer so that a new workload — say, a vector-similarity search over domain
names — can be added without re-ingesting data.**

*Model answer:* This is the actual test of "flexible," so let me answer it as a general capability
rather than a one-off.

The property I want: **any new workload is a new materialisation, built by a new consumer of an
existing source, with no changes to ingestion and no new edge deployment.** Three things make that
true.

*The source is complete and replayable.* Bronze in Iceberg has every field that ever arrived, including
ones nothing currently uses. If ingestion had dropped "unused" fields to save space, a new workload
needing them would require a resolver-side change and six weeks. **Store the raw payload even when you
don't need it** — at 8:1 compression in Bronze, the marginal cost is small, and it's insurance against
exactly this. (With the caveat from Part 6 that you must still not store data you have no legal basis
for.)

*The transformation layer is a set of jobs over Iceberg, not a monolith.* Adding "compute an embedding
for each distinct domain and write it to a vector index" is a new job reading Silver and writing a new
Gold table plus a new serving store. It doesn't touch the ingestion path at all.

*The catalogue makes it discoverable and governed.* The new dataset registers itself: schema, owner,
lineage (derived from `silver.dns_events`), retention class, PII classification, access grants. Which
means it inherits governance rather than being an ungoverned side-project — the thing that usually goes
wrong when a team spins up "just a quick vector index."

Concretely for the vector case: a Spark job reads distinct `query_name_full` values from Silver
(cheap — it's a distinct over one column), computes embeddings in batch, writes them to a new Gold
Iceberg table `gold.domain_embeddings` (domain, embedding, model_version, computed_at), and loads them
into a serving index. Note `model_version` in the schema — embeddings from different model versions
aren't comparable, and a table without that column becomes unusable the first time you upgrade the
model.

Where would I *not* claim flexibility? If the new workload needs a field we never collected, or needs
sub-second freshness when the pipeline is minutes, or needs a different partitioning of the base data
for performance. Those require real work. Being clear about the boundary is more credible than claiming
the architecture handles everything.

**Q5.5: The ML team wants 90 days of raw events for training. That's 43 TB. They want it in a
dataframe. What do you do?**

*Model answer:* First, push back on the requirement, respectfully and with specifics — because "give me
all the raw data" is almost always a proxy for a real need that has a cheaper shape.

Ask: what's the actual model? If it's a per-query classifier, they need labelled examples, and 43 TB of
mostly-benign queries is enormously redundant — 99.7% of it is one class. **Stratified sampling gives
the same model quality at 1% of the data.** Take all of the positive class and a random sample of
negatives, with the sampling weights recorded so they can correct for it. That turns 43 TB into ~430 GB,
which changes everything downstream.

If they need full data (say, for sequence models over per-client behaviour), then the answer is to
change the access pattern, not the volume:

1. **Don't move it into a dataframe. Move the compute to the data.** Spark or Ray reading Iceberg
   directly, with predicate and projection pushdown so only the needed columns and partitions are read.
   Reading 6 of 16 columns cuts 43 TB to ~16 TB before any filtering.
2. **Materialise a purpose-built training table in Gold** with exactly the columns needed, pre-joined
   with features, pre-filtered. Compute it once, use it many times. This turns a 6-hour job that every
   engineer reruns into a 20-minute job.
3. **Pin the snapshot.** The training table is read at a specific Iceberg snapshot ID, recorded in the
   experiment metadata, so the run is reproducible and re-running it next week doesn't silently pick up
   new data.
4. **Give them a sandbox with a budget**, not raw S3 credentials. A Spark/Trino cluster with a cost cap
   and a quota. Otherwise the first full scan will cost several thousand dollars in S3 GET requests and
   compute, and nobody will notice until the bill arrives.

The cost point deserves a number, because it makes the argument concrete. 43 TB in ~256 MB Parquet
files is about 172,000 objects. At $0.0004 per 1,000 GET requests that's trivially small — but the
*compute* to scan 43 TB at, say, 1 GB/sec/core needs roughly 12,000 core-seconds per pass, and if five
engineers each run it five times a week you're burning real money on redundant scans of identical data.
That's the argument for materialising the training table.

**Q5.6: How do you serve both a 40ms dashboard query and a 4-minute forensic scan from the same
ClickHouse cluster without the second one ruining the first?**

*Model answer:* Four mechanisms, layered, because no single one is sufficient.

**1. Don't run them against the same data.** The dashboard query should never touch raw events. Build
rollup tables with `AggregatingMergeTree` fed by materialized views:

```sql
CREATE TABLE dns_rollup_1m
(
    tenant_id UInt32, minute DateTime, threat_category LowCardinality(String),
    events AggregateFunction(sum, UInt64),
    uniq_clients AggregateFunction(uniq, IPv6)
) ENGINE = ReplicatedAggregatingMergeTree(...)
ORDER BY (tenant_id, minute, threat_category);

CREATE MATERIALIZED VIEW dns_rollup_1m_mv TO dns_rollup_1m AS
SELECT tenant_id, toStartOfMinute(ts) AS minute, threat_category,
       sumState(toUInt64(1)) AS events, uniqState(client_ip) AS uniq_clients
FROM dns_events GROUP BY tenant_id, minute, threat_category;
```

A 24-hour dashboard query now reads 1,440 minutes × ~120 categories = ~173,000 rows for one tenant
instead of 2.3 billion. That's the 40ms. **This single change matters more than all the isolation
mechanisms combined** — the cheapest way to stop a query from interfering is to make it not expensive.

Two things to know about ClickHouse materialized views that interviewers probe: a MV is an **insert
trigger**, not a maintained view — it sees each inserted block and writes derived rows to the target
table. It does **not** see updates or deletes to the source, and it does **not** backfill history unless
you use `POPULATE` (which races with concurrent inserts) or manually backfill with an `INSERT
SELECT` over historical partitions. And `AggregateFunction` columns store intermediate states, so you
read them with the `-Merge` combinator: `sumMerge(events)`, `uniqMerge(uniq_clients)`.

**2. Resource isolation via settings profiles and quotas.** ClickHouse lets you cap per-user resource
use, which is the direct mechanism:

```sql
CREATE SETTINGS PROFILE forensic_profile SETTINGS
    max_memory_usage = 20000000000,          -- 20 GB, not the node's whole RAM
    max_execution_time = 300,
    max_threads = 8,                          -- leave cores for interactive queries
    max_bytes_before_external_group_by = 10000000000,  -- spill to disk instead of OOM
    priority = 10;                            -- lower priority than interactive

CREATE QUOTA forensic_quota FOR INTERVAL 1 HOUR
    MAX queries = 100, MAX read_rows = 500000000000, MAX execution_time = 3600
    TO forensic_role;
```

`max_bytes_before_external_group_by` is the important one to name: without it a large `GROUP BY` either
fits in memory or fails; with it, it spills to disk and completes slowly instead of erroring. For
forensic queries, slow-and-correct beats fast-and-failed.

**3. Workload separation at the cluster level.** For genuinely conflicting workloads, use separate
replicas. ClickHouse replicas are independent read targets, so route interactive queries to replicas 1
and 2 and analytical/export queries to replica 3. They share the same data via replication but not the
same CPU and page cache. This is the strongest isolation short of separate clusters and it costs one
extra replica.

**4. Admission control at the gateway.** Before a query reaches ClickHouse, a proxy inspects it:
estimate the scan size from the partition filter, reject or downgrade queries with no time bound,
enforce per-tenant concurrency limits. The specific rule I'd enforce first: **every query must have a
time predicate**, because a query without one scans all 30 days. That single rule prevents most
self-inflicted outages.

**Q5.7: Where do you put the "current state" of an entity — say, the last-seen time and risk score for
each client IP?**

*Model answer:* This is the classic "mutable state in an immutable pipeline" problem, and the answer
depends on the read pattern and cardinality. Skyline has roughly 40 million active client IPs.

*Option A — ClickHouse `ReplacingMergeTree`.* Table keyed `(tenant_id, client_ip)` with
`last_seen` as the version column, fed by a materialized view off the event stream. Reads use `FINAL`
or `argMax`. Good for analytical access ("show me all clients with risk > 80 for tenant 4471"), which
is a scan over 40M rows — a few hundred milliseconds. Bad for point lookups at 40,000 QPS: ClickHouse
isn't built for that concurrency, and each lookup with `FINAL` does real work.

*Option B — Postgres.* Gives you real updates and transactions. 40 million rows with an update rate
tied to event volume — potentially thousands of updates/sec — means heavy MVCC churn: every update
writes a new tuple version and autovacuum must reclaim the old ones. Doable with tuning
(`fillfactor`, aggressive autovacuum on that table, HOT updates by avoiding indexed-column changes) but
it puts a high-churn workload next to your control plane, which I'd resist on blast-radius grounds
alone.

*Option C — Redis or DynamoDB.* One key per `(tenant, client_ip)`, value is the state blob. Point reads
at 5ms and 40,000 QPS is exactly what these are for. TTL handles expiry of stale entities for free.
Costs: no analytical query capability, and it's a separate store to operate and secure.

*The answer: B is wrong here, and it's A **and** C, deliberately.* Write current state to Redis for the
serving path (single-entity, high QPS, low latency) *and* keep the append-only history in ClickHouse
for the analytical path (scan-oriented, and it preserves the point-in-time history that ML needs per
§5.5). Both are derived from the same stream, so they can't diverge in a way that isn't fixable by
replay.

**Say the general principle:** when a piece of data has two access patterns with incompatible shapes,
maintaining two materialisations from one source is correct — it's not duplication in the bad sense,
because neither is a source of truth. Duplication is only dangerous when copies can independently
*change*. Copies that are both pure functions of the same log are just caches.

### 5.8 Tier 3 questions — deep dive

**Q5.8: A ClickHouse materialized view has been silently wrong for 3 weeks. How did that happen and how
do you fix it?**

*Model answer:* Four realistic causes, and I'd check them in this order because that's roughly their
frequency:

**1. The MV was created after data already existed and was never backfilled.** MVs only see new inserts.
Someone created the rollup on the 10th; it has no data before the 10th; a 30-day dashboard silently
shows 20 days. Diagnosis: compare `min(minute)` in the rollup against `min(ts)` in the source.

**2. The MV's `GROUP BY` doesn't match the target table's `ORDER BY`.** If the MV groups by
`(tenant_id, minute, threat_category)` but the target table's `ORDER BY` is `(tenant_id, minute)`, then
rows with different categories are separate rows in the MV output but collapse to the same sort key in
an `AggregatingMergeTree`. Merges then combine them, and your per-category breakdown becomes a total
silently. **The target table's `ORDER BY` must exactly match the MV's `GROUP BY` keys.** This is a
classic and it produces plausible-looking wrong numbers rather than errors.

**3. The MV reads from the wrong side of a distributed setup.** An MV attached to a `Distributed` table
doesn't fire — inserts to `Distributed` are forwarded, and the MV must be attached to the *local*
`MergeTree` table on each shard. Attach it to the wrong one and it silently produces nothing (or, worse,
partial data).

**4. An insert failed after the source write but before the MV write, or vice versa.** ClickHouse MVs
are not transactional with the source insert: if the MV's target insert throws, behaviour depends on
`materialized_views_ignore_errors`. With it enabled, the source row lands and the rollup silently
misses it. With it disabled, the whole insert fails — which is the safer default for data you care
about, and it's worth knowing that setting exists.

**The fix, in order:**
1. **Quantify the damage** — for each affected time bucket, compare the rollup's numbers against a
   recomputation from raw. Produce a per-tenant, per-day delta report. You need this to know who was
   shown wrong numbers.
2. **Fix the definition** (the `ORDER BY` mismatch, the attachment point, whatever it was).
3. **Rebuild affected partitions**: compute into a shadow table with `INSERT INTO ... SELECT` over the
   raw data, verify, then `REPLACE PARTITION` atomically — same technique as the reprocessing case in
   Q2.9.
4. **Tell the affected consumers**, including customers if they saw wrong numbers. Three weeks of wrong
   dashboards is a disclosure, not just a bug fix.

**And the systemic fix, which is the actual answer:** add a continuous reconciliation check that
recomputes a *sample* of rollup buckets from raw data and compares. Run it hourly over the last 24
hours and a random older sample. That check is cheap and it turns "silently wrong for 3 weeks" into
"alerted in one hour." **Any derived dataset without a reconciliation check against its source will
eventually be silently wrong** — that's the principle worth stating.

**Q5.9: How do you handle late-arriving data in a pre-aggregated world?**

*Model answer:* Late data is the permanent tax on any pre-aggregation, and there's no free answer —
only a choice about which cost to pay.

Establish the facts first: at Skyline, ~99.4% of events arrive within 10 seconds of their event time.
Resolvers that were offline can flush spools that are days old. So the distribution is very tight with
a very long tail, which is the common shape and it's what makes the trade-off sharp.

Four strategies:

**1. Watermark and drop.** Define "we accept data up to 1 hour late; anything later goes to a separate
late table and doesn't update the rollup." Simple, bounded, and rollups become immutable after 1 hour —
which is genuinely valuable, because immutable aggregates can be cached forever and exported with
confidence. The cost is that a resolver returning from a 3-day outage has its data absent from
dashboards. For Skyline that's unacceptable for the security use case: forensic completeness matters
more than aggregate stability.

**2. Incremental update via `AggregatingMergeTree`.** This is ClickHouse's natural answer and it's why
the rollup uses aggregate *states* rather than final values. A late event simply produces another
partial state row for the same key; merges combine them; readers use `-Merge` and get the correct
total. **Late data works automatically with no special handling**, which is a strong argument for this
engine. Cost: rollup rows for old periods keep changing, so you can't treat them as immutable, and any
downstream cache must be invalidated. Also `count()` on the rollup table is not the answer to "how many
rollup rows are there" in any stable sense.

**3. Recompute affected windows.** Track which time buckets received late data (a simple
`(bucket, last_modified)` table), and periodically recompute those buckets from raw. Correct, and the
recomputation is bounded because you only touch dirty buckets. Cost: complexity, plus a period where the
rollup and raw disagree.

**4. Lambda-style: serve rollup for old data, raw for recent.** The query layer unions a pre-aggregated
table for anything older than 1 hour with an on-the-fly aggregation over the last hour of raw data.
Fresh *and* fast, at the cost of two code paths for the same logical query — which is the classic lambda
architecture complaint, and it's real: the two paths drift.

**What I'd do for Skyline:** strategy 2 as the default, because `AggregatingMergeTree` handles it
natively and forensic completeness is a product requirement. Add strategy 3 as a repair mechanism for
data more than 24 hours late (rare enough that recomputation is cheap). Explicitly instrument
**lateness as a metric** — a histogram of `ingested_at - ts` — because you cannot reason about any of
this without knowing the actual distribution, and because a change in that distribution is itself an
incident signal.

### 5.9 Case study: the ML team's silent failure

**Scenario:** "The threat-scoring model's production precision dropped from 0.94 to 0.71 over six weeks.
No deployment happened. Training metrics still look fine when the team retrains. What's your
investigation, and what does it say about the storage layer?"

**Frame it first.** Production degraded, training didn't, no code changed. So the model is the same and
the *data it sees* changed. Three families: the input distribution shifted (the world changed), the
feature computation changed (we changed), or there's skew between how features are computed at training
versus serving time. Note that "training metrics look fine on retrain" is a strong clue — it suggests
the offline path is self-consistent and the online path has diverged from it.

**Investigation, ordered by likelihood:**

**1. Feature skew (most likely).** Compare, feature by feature, the distribution of values seen at
serving time against the distribution in the training set. Skyline logs serving-time feature vectors,
so this is a direct comparison. Look for a feature whose p50 or null rate moved. The classic finding:
`distinct_domains_1h` is computed in the streaming path over a *sliding* 1-hour window but in the
training path over a *tumbling* hour bucket. Those give systematically different values, and the
difference grew when traffic patterns shifted seasonally.

**2. A silently-changed upstream.** `threat_category` comes from the threat-intel feed. If the vendor
re-categorised a large class of domains (or if our snapshot went stale, per §4.3), the feature's meaning
changed without any code change on our side. Check `enrichment_source_age_seconds` history and the
category distribution over time. **This is why "age of every reference dataset" is an SLI.**

**3. Genuine data drift.** The attackers changed behaviour — DNS tunnelling techniques evolve. This is
real and it's the case where the model needs retraining rather than the platform needing fixing. But
you must rule out (1) and (2) first, because retraining on skewed features bakes the skew in.

**4. Label leakage discovered late.** If the original training set was built with the naive join from
§5.5, the model was always weaker than its offline metrics claimed, and production precision was
declining as the leaked signal became less predictive. Diagnosis: rebuild the training set with a
correct `ASOF JOIN` and see whether offline metrics drop to ~0.71. **If they do, the model was never
0.94, and that's the finding.**

**What it says about the storage layer** — this is the part the interviewer is actually after:

- **Feature definitions must be single-sourced.** Two implementations of one feature is a latent bug
  with a long fuse. Either one definition executed by two engines, or train on logged serving features.
- **Feature values must be versioned and append-only** so you can reconstruct what the model saw at any
  past moment. If features were overwritten in place, this whole investigation is impossible.
- **Reference/enrichment data must be versioned too**, with the version recorded on each row. "Which
  threat-intel snapshot scored this event?" must be answerable, which means `threat_intel_version` is a
  column, not an ambient property.
- **Training runs must pin an immutable snapshot.** Otherwise "retrain and it looks fine" is
  uninformative, because the retrain used different data.
- **Skew monitoring is infrastructure, not an ML-team concern.** The platform should compute the
  training-versus-serving distribution comparison for every registered feature and alert on divergence,
  the same way it computes null rates for every column.

**The closing line:** "The proximate cause is a windowing mismatch. The platform cause is that we let
two implementations of one feature exist, and had no monitor that would notice they'd diverged. The
fix I'd fund is a feature registry where a definition is written once and both paths execute it — and
until that lands, a skew monitor on every feature, which is a week of work and would have caught this
in six days instead of six weeks."

---

## Part 6 — Multi-tenant data model: isolation, secure sharing, and compliance

### 6.1 What the interviewer is actually testing

This is the highest-stakes bullet, because the failure mode is a cross-tenant data leak, which is a
company-ending class of bug. The rubric:

1. Do you know the isolation models (silo / pool / bridge) and can you choose per-layer rather than
   picking one globally?
2. **Do you default to defence in depth?** A single `WHERE tenant_id = ?` is not isolation; it's one
   bug away from a breach. Staff-level answers have at least three independent layers.
3. Can you talk about compliance as *architecture* — data residency changing your deployment topology,
   FedRAMP forcing a separate region, GDPR erasure colliding with immutable storage — rather than as a
   checklist someone else owns?
4. Do you understand the specific tension between "the customer wants their data deleted" and "the
   contract requires 7-year immutable retention," and do you know the actual technique (crypto-
   shredding) that resolves it?
5. Can you design *secure sharing* — giving a customer access to their own data, or two customers
   access to shared data — without copying it?

### 6.2 The mental model: three isolation models, chosen per layer

The standard vocabulary, defined properly:

**Silo.** Each tenant gets dedicated infrastructure — their own database, their own cluster, their own
bucket. Strongest isolation; a bug in the query layer cannot leak across tenants because there is
nothing to leak into. Worst economics: 12,000 ClickHouse clusters is absurd, and per-tenant fixed costs
(minimum instance size, minimum storage, backup overhead) dominate for small tenants.

**Pool.** All tenants share infrastructure, with logical separation via a `tenant_id` column and
filtering. Best economics; a long-tail tenant doing 1 event/sec costs almost nothing. Weakest
isolation: correctness depends entirely on every query being filtered correctly, and on no tenant being
able to exhaust shared resources.

**Bridge.** A mix: pooled for most, siloed for some. Usually the right answer, and the interesting
question is *what determines which tenants get siloed*.

**The key insight to lead with:** you don't pick one model for the whole system. **You pick one per
layer**, because the cost/benefit differs sharply by layer. Skyline's actual answer:

| Layer | Model | Why |
| --- | --- | --- |
| Postgres (control plane) | Pool + RLS | 40 GB total; per-tenant DBs would mean 12,000 connection pools |
| Kafka | Pool, with dedicated topics for whales | Partition-level isolation for the top 20 |
| ClickHouse | Pool, sharded by tenant; dedicated cluster for GovCloud + EU | `tenant_id` leads the sort key, so pooling costs nothing at query time |
| S3 | Pool by prefix, silo by bucket for regulated tenants | Prefix isolation via IAM; separate buckets where the *encryption key* must differ |
| Compute (query) | Bridge — shared pool, dedicated replicas for whales | Noisy-neighbour containment |
| Encryption keys | **Silo, always** | This is what makes crypto-shredding and hard isolation possible |

That last row is the one that shows depth. **Even in a fully pooled storage layer, per-tenant
encryption keys give you a hard isolation boundary and a deletion mechanism that pooled storage
otherwise can't provide.** More on that in §6.5.

### 6.3 Defence in depth: five layers, each independently sufficient

Here's the reasoning that makes this necessary. Suppose isolation rests on one thing: the application
adds `WHERE tenant_id = :current_tenant` to every query. Estimate the failure probability. A team of 20
engineers writes maybe 400 queries a year touching tenant data. If each has a 0.2% chance of missing
the filter — an optimistic rate — that's:

```
1 − (0.998)^400 ≈ 55% chance of at least one unfiltered query per year
```

**More likely than not, every year.** And the consequence is a cross-tenant data leak. That arithmetic
is the argument for defence in depth, and it's worth doing out loud in the interview because it turns a
platitude into a quantified risk.

So: five layers, ordered from outermost. The property you want is that **any single layer failing does
not produce a leak.**

**Layer 1 — Identity and authorisation at the edge.** Every request carries a token whose claims
include the tenant. The gateway validates it and resolves `tenant_id` from the token, **never from a
request parameter.** The single most common multi-tenant vulnerability is `GET /api/events?tenant_id=X`
where the server trusts `X`. The rule: **tenant identity comes from the credential, never from the
request body or query string.** Where a user genuinely has access to several tenants (an MSP managing
20 customers), the token carries the *set*, and the requested tenant must be checked for membership in
it.

**Layer 2 — Query construction that cannot omit the filter.** Not "remember to add the filter" —
structurally impossible to omit. Two techniques:
- All tenant data access goes through a data-access layer that takes a `TenantContext` as a required
  constructor argument and injects the predicate. There is no code path that builds a query without
  one, and lint/CI rules forbid raw SQL against tenant tables outside that layer.
- Better where available: push it into the database so the application *can't* be trusted wrongly.
  That's layer 3.

**Layer 3 — Database-enforced row filtering.**

*Postgres — Row-Level Security:*

```sql
ALTER TABLE policy_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_rules FORCE ROW LEVEL SECURITY;   -- applies to the table owner too

CREATE POLICY tenant_isolation ON policy_rules
    USING (tenant_id = current_setting('app.tenant_id')::int);

-- application sets this per transaction, from the validated token:
SET LOCAL app.tenant_id = '4471';
```

Two details that separate a real answer from a recited one. **`FORCE ROW LEVEL SECURITY` matters**:
without it, the table owner bypasses RLS entirely, and applications frequently connect as the owner.
And **`SET LOCAL` rather than `SET`**: with a transaction-pooling PgBouncer, a plain `SET` persists on
the pooled connection and the next transaction — possibly another tenant's — inherits it. That is a
cross-tenant leak caused by connection pooling, and it's a genuinely common production bug.

*ClickHouse — Row Policies:*

```sql
CREATE ROW POLICY tenant_isolation ON skyline.dns_events
    FOR SELECT USING tenant_id = toUInt32(getSetting('SQL_tenant_id'))
    TO tenant_readers;
```

ClickHouse row policies are enforced by the server on every read, so even a hand-written query in a SQL
console can't escape them. Two caveats to mention: policies are `PERMISSIVE` by default and multiple
permissive policies combine with `OR` (so adding a policy can *widen* access — use `AS RESTRICTIVE`
when you mean `AND`), and row policies don't apply to `INSERT`, so write-side isolation is your job.

**Layer 4 — Physical separation where it's affordable.** Sharding by `tenant_id` means tenant 4471's
data lives on shard 3 and nowhere else. A query bug that omits the filter still only sees the shard it
queried. This turns a "leak everything" bug into a "leak 1/8 of tenants" bug — not a fix, but a real
blast-radius reduction. For regulated tenants, escalate to a separate cluster where the blast radius is
zero.

**Layer 5 — Encryption with per-tenant keys.** Even with full physical access to the S3 objects, data
for tenant 4471 is unreadable without key `arn:aws:kms:...:key/tenant-4471`. An IAM misconfiguration
that grants bucket-wide read does not grant decryption. This is the layer that protects you against
your own infrastructure mistakes, which are more common than application bugs.

**And the sixth thing, which isn't a layer but makes the layers real: continuous verification.** Write
an automated test suite that, for a sample of tenants, attempts cross-tenant access through every
interface — API, SQL console, S3, the export path — and asserts failure. Run it in CI and in
production continuously. **An isolation control you don't test is an isolation control you don't have**,
and this test suite is also your SOC 2 evidence.

### 6.4 The tenant sharding design

For ClickHouse, the question "how do I physically separate tenants" has one very tempting wrong answer,
so let's kill it first.

**The wrong answer: `PARTITION BY tenant_id`.** It looks like isolation. What it actually does: 12,000
tenants × 90 days = 1.08 million partitions per table. ClickHouse holds metadata for every part of
every partition in memory; merges never cross partitions, so the 9,879 long-tail tenants each get their
own tiny parts that never consolidate. You'll hit part-count limits, server startup will take hours
(it scans part metadata), and `system.parts` becomes unqueryable. **This is a cluster-killer and it's
the most common wrong answer to this question.**

**The right answer: `ORDER BY (tenant_id, ...)` with sharding by a hash of tenant, and partitioning by
time.** The sort key gives you the query pruning (a tenant query reads only its granules); sharding
gives you the blast-radius reduction; time partitioning gives you the lifecycle mechanism. All three
goals met without exploding metadata.

**Shard assignment is a control-plane decision, not a hash function.** This is the subtle part. The
naive approach, `shard = tenant_id % 8`, is deterministic and requires no state — and it's wrong for
Skyline, because Meridian Financial is 18% of volume and would land on one shard, giving that shard
~2.4× the load of the others. Instead, keep an explicit mapping table:

```sql
CREATE TABLE tenant_shard_assignment (
    tenant_id     INT PRIMARY KEY REFERENCES tenants(tenant_id),
    shard_id      SMALLINT NOT NULL,
    weight        INT NOT NULL,       -- observed events/sec, updated daily
    assigned_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    migrating_to  SMALLINT            -- non-null during a rebalance
);
```

Now you can bin-pack by observed weight, split a whale across shards with a salted key, and rebalance
without a rehash-everything event. The `migrating_to` column supports the dual-write phase of moving a
tenant between shards. **Explicit assignment beats hashing whenever the keys are skewed** — which is a
generalisable point worth naming, since it's the same insight as the Kafka partitioning fix in Part 2.

The cost of explicit assignment is that the mapping must be available to every writer and reader.
Cache it aggressively (it changes daily at most), serve it stale during a control-plane outage, and
version it so a reader can tell whether it's using a stale map.

### 6.5 Compliance as architecture

Four requirements, each of which changes the architecture rather than adding a checkbox.

---

**Requirement 1 — Data residency (EU tenants' data must not leave the EU).**

This cannot be solved with a `WHERE` clause. If EU data is in a US-region ClickHouse cluster, it has
left the EU, regardless of who can query it. Residency is a *deployment topology* requirement.

The architecture: **a full regional stack per residency zone.** `eu-central-1` gets its own collectors,
Kafka, ClickHouse, and S3 buckets. EU tenants' resolvers resolve a DNS name that routes to EU
collectors. Nothing about EU event data crosses regions.

The hard part is the control plane, because that's global by nature: a single `tenants` table, a single
identity system, a single UI. Resolve it by splitting the control plane's data by sensitivity:

- **Global, replicated:** tenant IDs, shard assignments, schema versions, feature flags. Non-personal
  metadata.
- **Regional, never replicated:** anything that is or contains personal data — user records, email
  addresses, IP allow-lists, saved queries (which can embed IP addresses).

So `tenants` replicates globally but `users` does not; the global row for an EU tenant contains a
pointer ("this tenant's user records live in `eu-central-1`") rather than the records. A US admin
console fetches EU user data by calling the EU control plane API, which enforces its own authorisation
and logs the access — **the data is displayed in the US, not stored there**, and whether that's
acceptable is a legal question you should ask rather than assume.

Cross-region queries for internal analytics are the leak everyone forgets. An analyst running "top
domains globally" against a union of regional clusters has just moved EU data to the US. The control is
technical: the query gateway refuses cross-region unions of raw data and permits only pre-aggregated,
k-anonymised results above a threshold (say, aggregates covering ≥ 50 distinct clients, so no
individual is identifiable).

---

**Requirement 2 — FedRAMP Moderate for US Federal customers.**

This is even more separating. FedRAMP requires an authorised boundary: **a separate AWS GovCloud
deployment**, FIPS 140-2/3 validated cryptographic endpoints, US-person-only operational access, and
control implementation evidence across ~325 controls.

The architectural consequences:

- **Complete stack duplication in GovCloud.** No shared services with commercial — not even monitoring.
  A Datadog agent shipping metrics from GovCloud to a commercial SaaS endpoint is a boundary violation,
  which means your observability stack must be self-hosted inside the boundary. This surprises teams
  and it's expensive; call it out early.
- **Deployment pipeline separation.** Artifacts are built commercially and *promoted* into GovCloud
  through a controlled path with signature verification, because the CI system itself is usually not in
  the boundary.
- **Access control on humans.** Only US persons, background-checked, with break-glass procedures and
  session recording. This changes on-call rotations and is an org design constraint, not just a tech
  one.
- **A configuration-drift problem.** Two deployments diverge. The control is that everything is
  Terraform/Helm from one repo with environment overlays, and there's a continuous drift check —
  otherwise a fix applied commercially silently doesn't reach GovCloud, and you find out during an
  incident.

**The staff-level point:** FedRAMP is roughly a 2–4× multiplier on operational cost and a significant
tax on delivery velocity, because every change goes through change control and annual assessment. That
should be a deliberate business decision tied to federal revenue, not something engineering absorbs
quietly. Being able to say "this costs us roughly 30% of platform-team capacity, is that worth the
federal segment?" is exactly the framing expected at this level.

---

**Requirement 3 — GDPR right to erasure, colliding with immutable retention.**

Here's the tension, stated concretely. A German data subject exercises Article 17: delete all personal
data relating to me. Their client IP appears in ~4 million events across 400 days of history, spread
over ClickHouse parts and ~2,000 Parquet files in S3, some of which are under Object Lock in compliance
mode because a *different* customer's contract requires 7-year WORM retention on the same physical
storage.

**Object Lock in compliance mode cannot be removed by anyone, including the AWS account root.** That is
the entire point of it. So you cannot delete the object. And yet you must erase the data.

**The resolution is crypto-shredding**, and knowing this is a strong differentiator:

> Encrypt data with a key scoped to the erasure unit. To erase, destroy the key. The ciphertext remains
> — satisfying WORM — but is permanently unrecoverable, which regulators broadly accept as erasure.

Design it deliberately, because the *scope* of the key determines what you can erase:

- **Per-tenant keys** let you erase an entire tenant on contract termination. Necessary, not sufficient
  for Article 17, since you can't erase one data subject inside a tenant.
- **Per-tenant-per-day keys** (12,000 × 400 = 4.8M keys) get expensive in KMS terms and still don't
  reach one subject.
- **Per-subject keys are impractical** for DNS logs, where the "subject" is a client IP that isn't known
  in advance and changes via DHCP.

So the honest architecture is a **hybrid**, and describing it accurately is the answer:

1. **Crypto-shredding at tenant granularity** — a per-tenant KMS data key (envelope encryption:
   KMS holds the key-encrypting key, and the data keys are stored wrapped) handles termination and
   whole-tenant erasure. Destroy the KEK; every object encrypted under it is dead.
2. **Row-level erasure in the mutable tiers** — ClickHouse `DELETE FROM dns_events WHERE client_ip = ...`
   (lightweight delete, which writes a `_row_exists` mask rather than rewriting parts), and Iceberg
   position/equality deletes in the lake, resolved at read time and applied physically at the next
   compaction.
3. **Pseudonymisation as the real mitigation, applied at ingest.** This is the design move that makes
   the whole problem smaller: don't store the raw client IP in the analytical store at all. Store
   `HMAC(client_ip, tenant_key)` for correlation, and keep the reversible mapping in a small, separate,
   heavily-controlled store with its own short retention. Now:
   - Analytics still work — you can count distinct clients, correlate a client's behaviour, join across
     time — because the HMAC is stable within a tenant.
   - Erasure becomes deleting **one row from the mapping table**, after which the pseudonym in 4 million
     events is no longer linkable to a person, and arguably no longer personal data.
   - The 400 days of Parquet under Object Lock never need to be touched.

**That third point is the answer that solves the problem rather than fighting it.** Say it as: "The best
way to handle erasure at scale is to minimise what's personal in the first place — pseudonymise at
ingest, keep the small reversible mapping separate and erasable, and you've converted a
delete-from-4-million-immutable-records problem into a delete-one-row problem." Then note the caveat
honestly: pseudonymised data is still personal data under GDPR if you hold the key, so the mapping
store's controls and retention are what carry the compliance argument — and you should get that
position reviewed by counsel rather than asserting it yourself.

4. **A deletion register.** Every erasure request produces a durable record: request, scope, methods
   applied, verification, timestamp, and a re-verification job that periodically re-checks that the
   subject's data hasn't reappeared via a backup restore or a replay from Kafka. **The backup and replay
   paths are the ones people forget** — restoring a 6-month-old snapshot resurrects deleted data unless
   the deletion register is replayed after every restore. Make that a mandatory step in the restore
   runbook.

---

**Requirement 4 — Contractual WORM retention (7 years, tamper-evident) for three customers.**

Mechanism: S3 Object Lock in **compliance mode** with a 7-year retain-until date, in a dedicated bucket,
with versioning enabled (Object Lock requires it). Governance mode is the wrong choice here — it can be
overridden by a principal with `s3:BypassGovernanceRetention`, which means it isn't tamper-*proof*,
only tamper-*resistant*.

Additional controls: MFA delete on the bucket, a separate AWS account with a distinct trust boundary
(so a compromise of the main account can't touch it), CloudTrail data events logged to yet another
account, and a periodic integrity check that verifies object checksums against a manifest stored
elsewhere.

**The critical planning point:** Object Lock is *irrevocable*. If you write an object with a 7-year lock
by mistake — say, a bug that applies the lock to all tenants instead of three — you pay for that storage
for 7 years with no way to delete it. So the lock decision must be made by the control plane with an
explicit per-tenant flag, tested thoroughly in a non-locked environment, and the IAM policy that permits
setting long retention periods should be tightly scoped. I'd also cap the maximum retain-until date the
service can set, so a bug can't write a 100-year lock.

### 6.6 Secure data sharing without copying

The requirement: a customer wants to analyse their own data in *their* Snowflake, or a security
researcher needs access to a de-identified subset, or two customers in the same industry consortium
want to share threat indicators.

**The anti-pattern is copying.** Every copy is a new access-control surface, a new retention obligation,
a new thing that goes stale, and — the killer — a copy you cannot revoke. Once you've dropped Parquet
into the customer's bucket, revoking access is a conversation, not a control.

Four mechanisms, best to worst for Skyline:

**1. Cross-account access to Iceberg tables via S3 Access Points (preferred).** Create a dedicated
Access Point per sharing relationship, with a policy that permits only the relevant prefixes, and grant
the customer's AWS account principal access. They query the same physical Parquet files with their own
Athena/Spark/Snowflake-external-table. No copy exists. Revocation is deleting the access point policy,
effective immediately. Their compute costs are theirs, which also solves the "customer runs an
expensive scan" problem.

Details that matter: the customer must be able to decrypt, so the KMS key policy must grant their
principal `kms:Decrypt` — which is also your revocation lever, and a stronger one than the bucket policy
since it can't be bypassed by any S3-level misconfiguration. Also, they'll need read access to the
Iceberg metadata, so scope the access point to include the metadata prefix.

**2. A governed query interface (a "clean room" pattern).** For sharing where the recipient must *not*
see raw rows — threat-intel consortium sharing, or research access — expose a query API that accepts a
restricted query language and enforces aggregation minimums: no result row may represent fewer than N
distinct entities, no output may include raw identifiers, and there's a per-recipient query budget to
prevent differencing attacks (issuing many overlapping aggregate queries to reconstruct individuals).
That last control is the one that shows real understanding — aggregation thresholds alone are
defeatable by an adversary who can issue enough queries.

**3. Managed sharing products** — Snowflake Secure Data Sharing, Databricks Delta Sharing, BigQuery
Analytics Hub. Genuinely good if you and the recipient are on the same platform: no copy, granular
revocation, built-in auditing. Delta Sharing is notable because it's an open protocol, so the recipient
doesn't need Databricks. Cost: platform coupling, and for Skyline it means maintaining a Snowflake
presence purely for sharing.

**4. Scheduled export to a customer-owned bucket.** What Argus does today. Simple, universally
compatible, and the reason the 40 legacy customers work at all. Accept it as a compatibility path,
harden it (customer-managed KMS key so *they* control decryption, checksums, a manifest, and a delivery
receipt), but steer new relationships toward (1).

**For all of them, the non-negotiables:** every access is logged with principal, dataset, rows returned,
and purpose; every sharing relationship has an expiry date and requires renewal (relationships without
expiries become permanent by neglect); and there's a quarterly review where an owner re-attests to each
one. Those three turn sharing from a growing liability into a managed one.

### 6.7 Tier 1 questions — screening

**Q6.1: How do you isolate tenants in a shared ClickHouse cluster?**

*Model answer:* Five layers, and I'd emphasise that no single one is sufficient.

The sort key leads with `tenant_id`, so every tenant query prunes to that tenant's granules — that's a
performance property, not a security one, but it means pooling costs nothing at query time. Physical
sharding by tenant assignment limits blast radius: a bug that omits a filter sees one shard, not the
whole cluster. ClickHouse **row policies** enforce `tenant_id = <session tenant>` server-side on every
`SELECT`, so even a hand-written console query can't escape it. The data-access layer makes an
unfiltered query impossible to write, with CI rules banning raw SQL outside it. And per-tenant
**settings profiles and quotas** cap memory, threads, execution time, and rows read, so isolation covers
resource consumption and not just visibility.

Then the two things people leave out: regulated tenants get a physically separate cluster, because for
GovCloud and EU-residency tenants logical isolation isn't legally sufficient; and there's a continuous
test suite that attempts cross-tenant access through every interface and asserts failure.

**Q6.2: A customer asks "can other customers see my data?" What's your answer?**

*Model answer:* Answer with the mechanisms and their independence, not with a reassurance — customers
who ask this question are technical and a vague answer makes it worse.

"No, and here's why it's structurally hard rather than just carefully avoided. Your data is encrypted
with a key that exists only for your account; no other tenant's credentials can decrypt it. It's stored
on a shard that only holds a subset of tenants and is queried through a server-side row policy that
filters by the tenant in your authenticated session — not by anything in the request. Requests can't
specify a tenant; it's derived from your credential. Every access is logged with the principal and the
rows returned, and we can show you your own access log. We run automated cross-tenant access attempts
continuously as a test, and we have an independent SOC 2 Type II audit of these controls. If you need
physical isolation, we offer a dedicated deployment."

Then be honest about the residual risk, because that's what earns credibility: "The remaining risk is a
bug in the platform itself. We manage it with defence in depth — a single bug in any one layer doesn't
produce exposure — and with a bug bounty and third-party penetration testing. I won't tell you the risk
is zero."

**Q6.3: What's the difference between silo, pool, and bridge, and which would you pick?**

*Model answer:* Silo is dedicated infrastructure per tenant — strongest isolation, worst unit economics,
and it doesn't scale operationally past a few hundred tenants because every operation (upgrade, patch,
migration) multiplies. Pool is shared infrastructure with logical separation — best economics, isolation
depends on software correctness, and it introduces noisy-neighbour risk. Bridge mixes them.

I'd pick bridge, and the interesting content is the *policy for which tenants get siloed*. Three
triggers, in priority order: **regulatory** (GovCloud, EU residency — non-negotiable and it's about
where data lives, not about size), **contractual** (an enterprise that paid for dedicated
infrastructure), and **operational** (a tenant large enough that pooling them creates unmanageable
noisy-neighbour risk — for Skyline, anything above roughly 5% of cluster capacity).

Then the part people miss: **the tenant's tier must be a control-plane attribute that can change**, and
promoting a tenant from pooled to dedicated must be a supported, rehearsed operation rather than a
project. Otherwise you end up with a tenant you should silo and no way to do it. The migration
primitive is: dual-write to both locations, backfill history, verify, flip reads, stop dual-write.
Build it once, use it for every promotion.

### 6.8 Tier 2 questions — design

**Q6.4: Design the multi-tenant data model for Skyline. Show me the schema.**

*Model answer:* Three levels: the tenancy metadata in the control plane, the isolation attributes on
data-plane tables, and the access-control model.

**Control plane:**

```sql
CREATE TABLE tenants (
    tenant_id        SERIAL PRIMARY KEY,
    external_id      UUID NOT NULL UNIQUE,          -- what customers see; never expose serial IDs
    name             TEXT NOT NULL,
    tier             TEXT NOT NULL CHECK (tier IN ('free','standard','enterprise','dedicated')),
    residency_zone   TEXT NOT NULL,                 -- 'us-commercial','eu','us-gov'
    isolation_model  TEXT NOT NULL CHECK (isolation_model IN ('pooled','dedicated')),
    kms_key_arn      TEXT NOT NULL,                 -- per-tenant KEK — the crypto-shred handle
    status           TEXT NOT NULL,                 -- active | suspended | terminating | terminated
    contract_id      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    terminated_at    TIMESTAMPTZ
);

CREATE TABLE tenant_data_locations (       -- where this tenant's data physically is
    tenant_id     INT REFERENCES tenants(tenant_id),
    dataset       TEXT NOT NULL,
    store         TEXT NOT NULL,           -- 'clickhouse' | 's3' | 'redis'
    location      TEXT NOT NULL,           -- cluster name / bucket+prefix
    region        TEXT NOT NULL,
    PRIMARY KEY (tenant_id, dataset, store)
);
```

`external_id` as a UUID is a deliberate small thing worth defending: exposing sequential integers leaks
customer count and invites enumeration attacks against any endpoint with a weak authorisation check.

`tenant_data_locations` is the table that makes compliance answerable. "Where is tenant 4471's data?" is
a query, not an investigation — and that question gets asked by auditors, by customers, and by you
during a deletion request.

**Data plane** — `tenant_id` is the first column of every sort key, always, plus a residency-derived
routing decision made at write time. Nothing about the row schema changes per tenant; the schema is
uniform and the *placement* varies.

**Access control** — a three-level model, because real customers need it:

```sql
CREATE TABLE grants (
    grant_id     BIGSERIAL PRIMARY KEY,
    principal_id UUID NOT NULL,          -- user or service account
    tenant_id    INT  NOT NULL REFERENCES tenants(tenant_id),
    scope        TEXT NOT NULL,          -- 'tenant' | 'site:<id>' | 'dataset:<name>'
    role         TEXT NOT NULL,          -- 'viewer' | 'analyst' | 'admin'
    column_mask  TEXT[],                 -- columns this grant may NOT see, e.g. {client_ip}
    granted_by   UUID NOT NULL,
    expires_at   TIMESTAMPTZ,            -- NULL only for permanent org roles
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`column_mask` supports the real requirement that a junior analyst can see aggregate traffic but not raw
client IPs — column-level access control, enforced by the query layer rewriting `client_ip` to a masked
expression rather than by omitting the column (omitting changes the result shape and breaks their
tooling). And `expires_at` on grants: **time-bounded access by default** is the control that prevents
permission accumulation, which is the thing every access review finds.

**Q6.5: An MSP manages 20 sub-tenants. How does that work?**

*Model answer:* This is hierarchical tenancy, and the mistake is modelling it as a special case. Model
it as a general graph and the MSP falls out of it:

```sql
CREATE TABLE tenant_hierarchy (
    parent_tenant_id INT REFERENCES tenants(tenant_id),
    child_tenant_id  INT REFERENCES tenants(tenant_id),
    relationship     TEXT NOT NULL,   -- 'msp' | 'subsidiary' | 'delegated_admin'
    permissions      TEXT[] NOT NULL, -- what the parent may do: {'read','manage_policy'} — NOT {'read_pii'} by default
    established_at   TIMESTAMPTZ NOT NULL,
    established_by   UUID NOT NULL,
    PRIMARY KEY (parent_tenant_id, child_tenant_id)
);
```

Design points worth stating:

- **The child's data stays the child's.** Events are tagged with the child's `tenant_id`, not the
  parent's. The MSP gets *access*, not ownership. That matters enormously when the relationship ends —
  and it will — because then it's a grant revocation rather than a data extraction project.
- **The permission set is explicit and not inherited by default.** An MSP that can manage policy should
  not automatically see raw client IPs; those are the sub-tenant's employees' personal data and the
  sub-tenant may not have consented to sharing them.
- **Cross-tenant queries are explicit and bounded.** "Show me all my sub-tenants" issues 20 scoped
  queries and unions the results in the application, rather than one query with
  `tenant_id IN (...)`. Slightly slower, and it means the row policy still applies per query, so a bug
  in hierarchy resolution can't produce an unfiltered scan. Depth is capped at 2 — deeper hierarchies
  are a support burden nobody actually needs, and unbounded recursion in an authorisation check is a
  denial-of-service waiting to happen.
- **Audit records both principals.** The log entry says "user U at MSP tenant 88 read tenant 4471's
  data," because the sub-tenant has a legitimate interest in seeing that, and giving them that view is a
  feature.

**Q6.6: How do you handle a tenant's data when they terminate their contract?**

*Model answer:* Terminate is a *state machine*, not an event, because the actual requirement is a
sequence with a grace period.

```
active → suspended → terminating → terminated → purged
```

- **Suspended** (day 0): access revoked, ingestion stopped, data retained. This is reversible —
  non-payment gets resolved, and a customer who pays on day 3 must get their data back intact. Do not
  delete anything here.
- **Terminating** (day 1–30, per contract): a data-export window. The customer can pull their history
  via the export API. Notify them at day 1, 15, 25, and 29. **Record the notifications**, because "we
  never told them" is a common dispute.
- **Terminated** (day 30): access fully revoked, data still physically present, marked for purge.
- **Purged** (day 30 + policy delay, typically 30–90 more days): crypto-shred — schedule the tenant's
  KMS key for deletion (AWS enforces a 7–30 day waiting period, which is a useful safety net, and use
  the maximum), then physically delete ClickHouse partitions, S3 objects, and Redis keys.

The specifics that make this a good answer:

- **Purge is idempotent and verifiable.** After purging, a verification job asserts zero rows in every
  store for that tenant and records the result. The assertion must cover the stores people forget:
  backups, the DLQ topic, log aggregation, the metrics system (tenant labels), support-ticket
  attachments, and any BI tool's cached extracts.
- **Legal hold overrides everything.** Check it at every transition. A held tenant stops at
  `terminated` and never purges until released.
- **Backups are the hard part and you should raise it unprompted.** A 90-day backup retention means the
  tenant's data lives in backups for 90 days after purge. Options: exclude terminated tenants from new
  backups and wait out the old ones (simple, means you can't promise immediate erasure); or rely on
  crypto-shredding so the backup contains only unreadable ciphertext (the good answer, and another
  reason per-tenant keys are the right foundation). With crypto-shredding, "purged" is honest the
  moment the key is destroyed, regardless of what bytes exist where.
- **Produce a deletion certificate**: what was deleted, when, by what method, verified how, signed. Many
  enterprise contracts require it, and it's the artifact that closes the loop.

### 6.9 Tier 3 questions — deep dive and adversarial

**Q6.7: A bug in a shared dashboard query showed tenant A's data to tenant B for 6 hours. Walk me
through the response.**

*Model answer:* This is a security incident, and the ordering matters more than the technical detail.

**Minute 0–15: stop the bleeding.** Disable the affected feature — not a fix, a kill switch. If the bug
is in a specific dashboard, take the dashboard down. Availability loss is strictly better than
continued exposure. Then confirm it's actually stopped, by testing rather than by reading the code.

**Minute 15–60: scope it precisely.** From the audit log — and this is where "log every access with
principal, tenant, and rows returned" pays for itself — determine exactly: which tenants' data was
exposed, to which principals, which columns, how many rows, and over what window. **Do not estimate.**
The disclosure notice will state these numbers and revising them upward later is far worse than taking
an extra hour to be right.

**Hour 1–4: preserve and notify internally.** Snapshot the logs to immutable storage (this becomes
evidence). Notify security, legal, and the executive on call. GDPR Article 33 requires notifying the
supervisory authority within **72 hours** of becoming aware of a personal data breach — that clock
started at minute 0, so legal needs to be engaged in hour 1, not day 2. Many US state laws and customer
contracts have their own clocks, often shorter.

**Hour 4–24: root cause and fix.** Find the actual defect. **And then find why five layers of defence
all failed simultaneously**, because if a single application bug caused exposure, the other layers
weren't real. Typically the finding is that the dashboard query bypassed the data-access layer, ran as a
service account exempt from row policies, and read from a pooled cache keyed without the tenant. That's
three layers that were nominal rather than enforced, and the postmortem's main output should be making
them enforced.

**Day 1–3: external notification**, drafted by legal, factual, specific. What happened, what data, whose,
for how long, what we did, what we're doing. Affected customers get individual contact, not a status
page post.

**Week 1–4: systemic remediation.** The specific controls I'd expect to come out of it: no service
account may be exempt from row policies (use a session-scoped tenant instead); every cache key must
include the tenant, enforced by a typed cache API that takes a `TenantContext`; the cross-tenant access
test suite gains a case for this specific pattern; and a CI check that fails any query against tenant
tables not routed through the data-access layer.

**The thing that shows staff level:** say explicitly that you'd resist the pressure to under-report.
There's always someone arguing the exposure was "technically only metadata" or "probably nobody looked."
The engineer's job in that room is to state the facts accurately and let legal make the disclosure call
on an accurate basis.

**Q6.8: How do you prove tenant isolation to an auditor?**

*Model answer:* Auditors want evidence, and evidence means artifacts produced by systems, not
assertions by engineers. Five things, and the framing is that each is *continuously* generated rather
than assembled for the audit:

1. **Design documentation** — the data-flow diagram showing where tenant identity is established and
   where it's enforced, mapped to specific controls. This is the narrative, and it's necessary but the
   weakest evidence.
2. **Configuration as evidence** — the actual row policies, IAM policies, KMS key policies, and RLS
   definitions, exported from the live systems on a schedule, version-controlled, with change history
   showing who changed what and the approval. "Here is the policy, here is its git history, here is the
   PR approval for the last change."
3. **The continuous test suite** — automated cross-tenant access attempts across every interface, run
   in production on a schedule, with results retained. This is the strongest evidence because it's a
   *test of the running system*, not of a document. "We attempt unauthorised cross-tenant access 4,800
   times a day; here are 18 months of results, all denied."
4. **Access logs** — every read of tenant data with principal, tenant, dataset, timestamp, and rows.
   Immutable, retained per policy. Auditors will sample and ask you to explain specific entries, so they
   must be readable and complete.
5. **Independent validation** — third-party penetration testing focused specifically on multi-tenancy,
   plus the SOC 2 Type II report itself.

**The insight worth stating:** design the platform so evidence is a by-product of operation rather than
an artifact of audit season. If producing evidence is a two-week project each year, you'll produce it
late and it'll be thin. If it's a dashboard, the auditor gets it in an hour, and — much more valuable —
*you* see it continuously, so a control that degrades is caught in days rather than at the next audit.

**Q6.9: Your biggest tenant's queries are slowing everyone down. Fix it, without telling them to stop.**

*Model answer:* Immediate mitigation, then structural fix.

**Immediate (minutes):** apply a settings profile to that tenant capping `max_threads`,
`max_memory_usage`, and `max_execution_time`, plus a quota on `read_rows` per hour. This is a
control-plane change. It degrades *their* experience, not everyone's — which is the correct allocation
of pain, since they're the source. Tell them, and frame it as protecting their SLO too, because a
cluster in trouble hurts them as well.

**Diagnose (hours):** find out what they're actually running.
`SELECT query, count(), sum(read_rows), avg(query_duration_ms) FROM system.query_log WHERE user =
'tenant_4471' AND event_time > now() - INTERVAL 1 DAY GROUP BY query ORDER BY sum(read_rows) DESC` will
almost always show a small number of query shapes doing all the damage. The usual culprits: a dashboard
with auto-refresh set to 5 seconds running a 30-day scan; a query with no time predicate; a `SELECT *`
against raw events; or a `JOIN` where a dictionary would do.

**Structural fixes, in order of leverage:**

1. **Make their expensive query cheap.** Nine times out of ten there's a rollup that would answer it.
   If they're scanning raw events for something a materialized view could serve, building that view
   helps them *and* the cluster. This is the highest-value move and it's collaborative rather than
   restrictive.
2. **Dedicated read replicas.** Route their queries to replicas that no one else uses. Same data,
   separate CPU and page cache. Costs one replica; solves noisy-neighbour completely for reads.
3. **Promote them to a dedicated cluster.** At 18% of volume, Meridian is past the threshold where
   pooling makes sense. This should be a supported migration (§6.7's promotion primitive), and it's
   often something you can *sell* — "dedicated infrastructure" is a premium tier, so the fix becomes
   revenue.
4. **Admission control for everyone**, not just them. The general lesson is that the platform allowed a
   single tenant to consume unbounded resources. Per-tenant concurrency limits and mandatory time
   predicates should be defaults, so the next whale doesn't repeat this.

**The framing to use with the customer:** never "you're using too much." Instead: "we noticed your
forensic dashboard is running a 30-day scan every 5 seconds; we've built a pre-aggregated view that
answers the same question in 200ms instead of 40 seconds, and we'd like to switch you to it." Same
outcome, and you've delivered a performance improvement instead of a restriction.

### 6.10 Case study: the EU expansion

**Scenario:** "Sales just closed a deal contingent on EU data residency, and three more are pending.
Today everything runs in `us-east-1`. You have one quarter. What do you build, what do you tell sales,
and what do you refuse to promise?"

**Step 1 — Establish what "residency" actually requires, in writing.** This is not a technical question
and getting it wrong in either direction is expensive. Ask legal for a written answer to: must data be
*stored* in the EU only, or also *processed* only in the EU? Are backups included (yes, always)? Is
metadata about the data in scope? May EU data be *viewed* from the US by support staff under an
appropriate transfer mechanism (SCCs), or not at all? Does the customer's contract say "EU" or a
specific country?

The answers change the design by an order of magnitude. "Storage in the EU" is a bucket and a cluster.
"No US person may ever access it" changes your support model, your on-call rotation, and your hiring.
**Do not design until you have this in writing** — and say that in the interview, because proceeding on
an assumption here is how teams build the wrong thing for a quarter.

**Step 2 — Design the regional stack.** Assume the common answer: storage and processing in the EU,
support access from the US permitted under SCCs with logging.

Full data plane in `eu-central-1`: collectors, Kafka, ClickHouse, S3 buckets, Redis. Routing at the
edge: EU tenants' resolvers get an EU endpoint from the provisioning system, and — belt and braces —
collectors reject events for tenants whose `residency_zone` doesn't match the collector's region. That
server-side check is what protects you from a provisioning bug, and it's the control an auditor will
ask about.

Control plane split by sensitivity, per §6.5: global replication of non-personal metadata, regional-only
storage of anything personal. The `tenants` row replicates; the `users` rows don't.

**Step 3 — Name the hard parts honestly.** Four, and volunteering them is the point of the question:

*Existing EU tenants already have data in `us-east-1`.* New deals are easy; the 340 existing European
tenants are the actual work. Migrating them means: stand up EU infrastructure, dual-write, backfill
history from S3 to the EU region (cross-region transfer at $0.02/GB — for 340 tenants at, say, 8 TB
total, that's $160, negligible; the time and verification effort is the real cost), verify, cut over
reads, then **delete the US copy and prove it**. That last step is the one that makes it a compliance
activity rather than a data migration.

*Cross-region analytics break.* Internal dashboards that aggregate globally can no longer union raw
data. Rebuild them on pre-aggregated, k-anonymised regional exports. Several internal teams will be
unhappy; get ahead of it.

*The whole platform's operational surface doubles.* Two of everything: two on-call targets, two upgrade
paths, two capacity models, two sets of dashboards. Say the number: this is roughly a 60–80% increase in
platform operational load, permanently. It should be funded, not absorbed.

*Disaster recovery gets harder.* You can't fail EU over to the US. So the EU stack needs its own
multi-AZ resilience and its own backup strategy, entirely within EU regions, which raises its cost
relative to the US stack.

**Step 4 — What to tell sales.** Be specific about what's real when:

- "In one quarter we can serve *new* EU tenants with full EU residency for event data, including
  storage, processing, and backups. I'll commit to that."
- "Migrating our 340 existing EU tenants takes a second quarter, because the migration must be verified
  and the US copies provably deleted."
- "Support staff accessing EU data from the US requires SCCs in the contract; if a customer requires
  EU-only personnel access, that's a hiring decision with a 6+ month lead time and I can't commit to
  it this year."

**Step 5 — What to refuse to promise.** FedRAMP-style formal certification of the EU stack, EU-only
personnel access, and anything requiring the control plane's *global* components to be EU-only. Each of
those is a much larger programme, and agreeing to them under deal pressure is how platform teams end up
with commitments they can't meet. **The staff-level behaviour here is to give sales a crisp, honest
menu with dates, rather than a yes that becomes a problem in six months.**

---

## Part 7 — Data governance: quality, lineage, retention, and access control

### 7.1 What the interviewer is actually testing

Governance is where candidates either sound like a compliance officer (all process, no engineering) or
like they've never met one (all engineering, no accountability). The rubric:

1. Do you treat governance as **engineered properties of the platform** rather than as documents and
   meetings? A "data governance council" that produces a policy nobody can enforce is not governance.
2. Can you define data quality as a set of *executable assertions* with owners and consequences, rather
   than as an aspiration?
3. Do you know what lineage is actually *for*? Most candidates say "understanding dependencies." The
   real uses are impact analysis before a change, root-cause analysis after an incident, and regulatory
   proof of where personal data flows — and column-level lineage is what makes the third one work.
4. Do you understand **data contracts** — the idea that a dataset has a producer who owes consumers a
   specification, with versioning and a deprecation process?
5. Can you say who *owns* data, and what ownership means concretely?

Parts 3 and 6 covered retention and access control mechanically. This part covers the governance layer
that makes them coherent: the catalogue, the contracts, the quality system, the lineage graph, and the
operating model.

### 7.2 The mental model: governance is a control plane over datasets

Here's the framing that makes governance an engineering problem. Every dataset should have answers to
seven questions, and **all seven should be machine-readable and enforced, not written in a wiki**:

1. **What is it?** Schema, semantics, grain (what does one row represent?), units.
2. **Who owns it?** A named team with an on-call rotation, not a person who left.
3. **Where did it come from?** Lineage, to the column level.
4. **How good is it?** Quality assertions and their current pass rate.
5. **How long does it live?** Retention class and basis (Part 3).
6. **How sensitive is it?** Classification, PII flags, residency constraints.
7. **Who may read it?** Grants, and how to request access (Part 6).

If those seven live in a catalogue that the platform *enforces* — the pipeline refuses to create an
unregistered dataset, retention won't run without a policy, access requires a grant — then governance is
a property of the system. If they live in Confluence, governance is a hope.

**The naive approach and why it fails.** Most companies start with "we'll document our datasets in a
wiki." Within a year the wiki describes 60% of datasets, 30% accurately. The reason is structural: the
wiki is a *parallel* artifact that must be manually kept in sync with reality, and there's no
consequence for divergence. The fix is to make the catalogue **the thing that creates the dataset** —
you register a dataset to get a table, so registration can't drift because it's not optional.

Concretely at Skyline:

```sql
CREATE TABLE dataset_catalog (
    dataset_id       TEXT PRIMARY KEY,        -- 'silver.dns_events'
    layer            TEXT NOT NULL,           -- bronze | silver | gold
    owner_team       TEXT NOT NULL,           -- must resolve to a real team in the org directory
    oncall_rotation  TEXT NOT NULL,
    grain            TEXT NOT NULL,           -- 'one row per DNS query event'
    description      TEXT NOT NULL,
    schema_ref       TEXT NOT NULL,           -- pointer into the schema registry, versioned
    classification   TEXT NOT NULL,           -- public | internal | confidential | restricted
    contains_pii     BOOLEAN NOT NULL,
    pii_columns      TEXT[],
    residency_scope  TEXT[],                  -- {'us-commercial','eu'}
    retention_class  TEXT NOT NULL REFERENCES retention_classes(name),
    sla_freshness_s  INT,                     -- NULL if no freshness commitment
    upstream         TEXT[],                  -- dataset_ids
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    deprecated_at    TIMESTAMPTZ,
    replacement      TEXT REFERENCES dataset_catalog(dataset_id)
);
```

Enforcement points that make it real, and these are the answer to "how do you keep a catalogue
accurate":

- **CI fails** if a pipeline writes to a dataset with no catalogue entry.
- **The retention reconciler skips** — and alerts on — datasets with no retention class, so an
  unregistered dataset is loudly non-compliant rather than quietly forgotten.
- **The access-grant system refuses** to grant access to an unclassified dataset.
- **A weekly report** lists datasets whose owner team no longer exists in the org directory. Ownership
  rot is the most common governance failure and it's trivially detectable.

### 7.3 Data quality as executable assertions

"Data quality" is meaningless until decomposed. The standard six dimensions, each made concrete for
Skyline with an actual check:

| Dimension | Question | Skyline check |
| --- | --- | --- |
| Completeness | Is anything missing? | `received/claimed ≥ 0.999` per tenant-hour (Part 4's heartbeat accounting) |
| Accuracy | Does it match reality? | Sampled events re-derived from resolver-side logs match |
| Consistency | Do copies agree? | ClickHouse count = Iceberg count per tenant-hour |
| Timeliness | Is it fresh enough? | p99 freshness < 60s |
| Validity | Does it conform to rules? | `response_code ∈ {NOERROR,NXDOMAIN,SERVFAIL,REFUSED,FORMERR,NOTIMP}` |
| Uniqueness | Are there duplicates? | `count()/uniqExact(event_id) < 1.001` per tenant-hour |

Every check is a SQL assertion with a threshold, a severity, and an owner:

```yaml
- id: dns_events.response_code_valid
  dataset: silver.dns_events
  dimension: validity
  severity: error                # error | warn
  owner: data-platform
  schedule: "*/15 * * * *"
  query: |
    SELECT countIf(response_code NOT IN ('NOERROR','NXDOMAIN','SERVFAIL',
                                          'REFUSED','FORMERR','NOTIMP')) / count() AS bad_ratio
    FROM silver.dns_events
    WHERE ts >= now() - INTERVAL 15 MINUTE
  assert: bad_ratio < 0.0001
  on_failure: [alert, quarantine_batch]
```

Four design decisions in that snippet worth defending:

**Severity determines consequence, and the consequences must be real.** `error` blocks promotion from
Bronze to Silver — the bad batch goes to quarantine and doesn't reach consumers. `warn` alerts and
proceeds. **A quality check with no consequence is a metric, not a control.** Most teams' "data quality
frameworks" are entirely `warn`, which is why they're ignored.

**Thresholds are ratios against a baseline, not absolutes.** `bad_ratio < 0.0001` scales with volume;
`bad_count < 100` doesn't, and it will fire spuriously as you grow and miss real problems when you
shrink. For distribution checks, compare against the trailing 7-day value for the same hour-of-week,
not against a constant — traffic composition at 03:00 Sunday genuinely differs from Tuesday noon.

**Checks run on a schedule against a window, not on every row.** Row-level validation belongs in the
pipeline (and it's there — schema conformance at the collector). These are *statistical* checks over
windows, which is what catches the class of problem where individually-valid rows are collectively
wrong.

**Anomaly detection over the checks themselves.** The highest-value quality check at Skyline isn't any
of the six above — it's "null rate for column X deviates from its 7-day baseline by more than 3σ,"
applied automatically to every column of every dataset. It requires no per-column configuration, it
scales to new columns for free, and it's what would have caught the firmware bug from Part 4 in hours.
**Auto-generated baseline checks over every column beat hand-written checks over a few columns**,
because the hand-written ones are always on the columns you already thought about.

**Where quality checks run matters.** Three placements with different trade-offs:
- *At ingest (the collector)*: cheapest to act on, but you only see one event at a time, so only
  structural checks are possible.
- *At the Bronze→Silver boundary*: the sweet spot. You have a batch, you can compute statistics, and
  you can quarantine before consumers see it.
- *Post-hoc on Silver/Gold*: catches things the earlier stages can't (cross-dataset consistency), but
  by then consumers may have already read bad data — so these must be paired with a consumer
  notification mechanism.

Use all three; be explicit about which failures each can catch.

### 7.4 Lineage, and what it's actually for

**Definition first, since people use the word loosely.** Lineage is the directed graph of data
dependencies: which datasets feed which, and — at the useful granularity — which *columns* feed which
columns, through which transformation.

**Table-level lineage** says `gold.tenant_daily` derives from `silver.dns_events`. Useful for impact
analysis at a coarse level.

**Column-level lineage** says `gold.tenant_daily.blocked_count` derives from `silver.dns_events.
policy_verdict` and `silver.dns_events.tenant_id`, via `countIf(policy_verdict='block')`. This is
dramatically more useful, and it's the difference between "42 downstream tables might be affected" and
"3 downstream columns are affected."

**The three real uses**, in order of how often they save you:

**1. Impact analysis before a change.** You want to change `policy_verdict` from four values to six.
Column-level lineage answers "what breaks?" in seconds: 3 dashboards, 1 ML feature, and the billing
aggregate. Without it, you either don't change it (paralysis) or you change it and find out (incidents).
This is the daily-value use case.

**2. Root-cause analysis after an incident.** The billing number is wrong. Lineage walks upstream:
`invoice.event_count ← gold.tenant_daily.events ← silver.dns_events ← bronze.dns_raw ← kafka topic
dns.raw`. At each hop you check the quality assertions and freshness. This turns a multi-team
investigation into a traversal.

**3. Regulatory proof of personal-data flow.** GDPR Article 30 requires a record of processing
activities: what personal data you hold, where it flows, who accesses it. **Column-level lineage plus
PII classification generates this automatically.** Tag `client_ip` as PII in the catalogue, and lineage
propagation tells you every downstream dataset that contains PII-derived data — including the ones
nobody remembered. That propagation is also how you find the dataset a team built last year that
quietly contains personal data with no retention policy.

**How to capture it, honestly ranked:**

*Best: emit it from the execution engine.* Spark, Trino, dbt, and Flink can all emit **OpenLineage**
events describing the actual job that ran, its inputs, and its outputs. Because it comes from the
engine, it reflects what *actually happened*, including dynamic SQL. This is the only approach that
doesn't drift.

*Good: parse the SQL.* For ClickHouse materialized views and scheduled queries, parse the SQL to extract
column dependencies. Accurate for static SQL, blind to anything constructed at runtime.

*Acceptable as a supplement: declared lineage.* The `upstream` array in the catalogue. Requires
discipline, drifts, but it covers the cases the other two miss (a Python job doing something opaque).

*Worst, and very common: a diagram someone drew.* It was accurate the day it was drawn.

**The honest limitation to volunteer:** lineage tells you *structural* dependency, not *semantic*
impact. It'll tell you `blocked_count` depends on `policy_verdict`; it won't tell you that adding two
new verdict values makes the count mean something different. Semantic impact needs a human who
understands the domain, and lineage's job is to tell that human which three things to look at instead
of forty.

### 7.5 Data contracts

The idea: **a dataset is an API, and it needs the same discipline.** A producer publishes a
specification; consumers depend on it; changes follow a versioning and deprecation process. Without
this, every schema change is a surprise and every consumer defensively copies data so they can't be
broken — which is how you get the copy proliferation problem.

A contract for `silver.dns_events`:

```yaml
dataset: silver.dns_events
version: 3.1.0
owner: data-platform
grain: one row per DNS query event
schema:
  tenant_id:      {type: uint32, nullable: false, description: "Skyline tenant"}
  ts:             {type: timestamp_ms, nullable: false, description: "resolver-observed query time, UTC"}
  event_id:       {type: uuid, nullable: false, unique: true}
  client_ip:      {type: string, nullable: false, pii: true, note: "HMAC pseudonym, stable per tenant"}
  policy_verdict: {type: enum, values: [allow, block, monitor, redirect], nullable: false}
guarantees:
  freshness_p99_seconds: 60
  completeness_min: 0.999
  uniqueness: "event_id unique within (tenant_id, day)"
  availability: 0.995
compatibility: backward     # additive changes only within a major version
deprecation_policy:
  notice_period_days: 90
  channel: "#data-platform-announcements + direct email to registered consumers"
consumers:                  # registered, so they can be notified
  - {team: reports-ui, contact: "#reports", criticality: high}
  - {team: ml-platform, contact: "#ml", criticality: high}
  - {team: billing, contact: "#billing", criticality: critical}
```

Three points that make this an engineering artifact rather than a document:

**The contract is enforced in CI.** A PR changing the schema runs a compatibility check against the
declared `compatibility` level and fails on a breaking change without a major version bump. Same
mechanism as Protobuf/Avro schema registries, applied to tables.

**`guarantees` are the SLOs from Part 4**, so the contract and the monitoring are the same numbers.
Consumers can build against a documented freshness guarantee rather than an observed behaviour they've
come to depend on.

**The consumer registry is what makes deprecation possible.** You cannot give 90 days' notice to
consumers you can't enumerate. Registration should be a prerequisite for access — you get a grant, you
get put on the notification list. This closes the loop with Part 6's grants table.

**Versioning in practice — expand/migrate/contract.** Never break in place:

1. *Expand:* add the new column/table alongside the old. Both work. No consumer changes.
2. *Migrate:* consumers move at their own pace within the notice window. Track adoption by measuring
   reads of the old versus new.
3. *Contract:* remove the old, only after reads hit zero and the notice period has elapsed. **Measured,
   not assumed** — the same discipline as decommissioning Argus in Part 3.

For a genuinely breaking change (changing `policy_verdict`'s meaning), publish `v4` as a separate
dataset, dual-produce both, and let consumers migrate. Expensive; that's the point. The expense is what
makes producers think carefully, and the alternative — breaking consumers — is more expensive but the
cost lands on someone else, which is why it happens without contracts.

### 7.6 Tier 1 questions — screening

**Q7.1: What does data governance mean to you?**

*Model answer:* The engineered guarantee that for every dataset, we know what it is, who owns it, where
it came from, how good it is, how long it lives, how sensitive it is, and who may read it — and that
those answers are enforced by the platform rather than documented in a wiki.

The distinction I'd draw is between governance as *process* and governance as *properties*. Process
governance is review boards and policy documents; it produces artifacts that drift from reality within
months. Property governance means the pipeline refuses to create an unregistered dataset, retention
won't run without a policy, access requires an explicit grant with an expiry, and lineage is emitted by
the execution engine rather than drawn by a human. Then governance is true by construction.

The practical test: if I ask "which datasets contain personal data and what's their retention?" and the
answer takes two weeks of interviews, you have process governance. If it's a SQL query, you have the
other kind.

**Q7.2: How do you measure data quality?**

*Model answer:* As executable assertions across six dimensions — completeness, accuracy, consistency,
timeliness, validity, uniqueness — each with a threshold, an owner, and a real consequence on failure.

The two design points I'd emphasise. First, **consequences must be real**: an `error`-severity check
blocks promotion from raw to curated, so bad data doesn't reach consumers. A framework where every
check only warns is a dashboard, and dashboards get ignored. Second, **automatic baseline checks beat
hand-written ones**: null rate, distinct count, and value distribution per column, compared against a
trailing 7-day baseline for the same hour-of-week, applied automatically to every column of every
dataset. It requires no per-column work, it covers new columns for free, and in practice it catches
more real problems than curated rule sets, because curated rules only cover the failures you already
imagined.

And I'd report quality as an SLI: "percentage of dataset-hours passing all assertions," trended, per
dataset, with an owner. That's what makes it manageable rather than anecdotal.

**Q7.3: What is data lineage and why do you care?**

*Model answer:* It's the dependency graph of data — ideally at column granularity: which columns feed
which, through which transformation.

Three uses that justify the cost. Impact analysis: before changing a column I can enumerate exactly what
breaks, which turns "we can't change that, nobody knows what depends on it" into a five-second query.
Root-cause analysis: when a number is wrong I walk upstream through the graph checking freshness and
quality at each hop, instead of convening four teams. And regulatory: combined with PII classification,
lineage propagation automatically produces the record of where personal data flows, which is a GDPR
Article 30 requirement and also how you discover the dataset someone built last year that quietly
contains personal data.

I'd capture it from the execution engine via OpenLineage rather than by declaration, because
engine-emitted lineage reflects what actually ran and can't drift. And I'd be honest that lineage gives
structural, not semantic, impact — it narrows the human review from forty things to three, it doesn't
eliminate it.

### 7.7 Tier 2 questions — design

**Q7.4: Design a data quality system for Skyline. Where does it run, what does it check, what happens
on failure?**

*Model answer:* Four components.

**1. Inline validation, at the collector.** Per-event structural checks: schema conformance, required
fields, type coercion, `tenant_id` resolves to a real tenant. Runs on every event, so it must be
microseconds. On failure: reject with a 400 for structural problems (the resolver shouldn't retry), or
route to DLQ for ambiguous ones. **This layer's job is to keep garbage out of the log**, not to assess
quality.

**2. Batch assertions, at the Bronze→Silver boundary.** This is the main gate. For each batch (15-minute
window per tenant), compute the statistical checks: null rates, value distributions, duplicate ratio,
referential integrity, volume versus baseline. On `error`-severity failure, **quarantine the batch**: it
lands in `quarantine.dns_events` with the failure reason, doesn't get promoted to Silver, and alerts the
owning team. On `warn`, promote and alert.

Quarantine rather than drop is important. A quarantined batch can be inspected, fixed, and re-promoted;
a dropped batch is gone. And a quarantine that grows is a visible signal, where silent dropping isn't.

**3. Cross-dataset reconciliation, scheduled.** Hourly: ClickHouse counts versus Iceberg counts versus
producer heartbeat claims, per tenant-hour. Daily: Gold aggregates recomputed from Silver on a sample
and compared. These catch the failures that within-batch checks structurally cannot — a batch can be
internally perfect and still have been written to the wrong place.

**4. Continuous profiling, automatic.** For every column of every registered dataset, compute daily:
null rate, distinct count, min/max, and a distribution summary. Store the history. Alert on deviation
beyond 3σ from the trailing 7-day, same-hour-of-week baseline. This is the check that scales without
configuration.

**Reporting:** every dataset has a quality score — the fraction of dataset-hours in the last 7 days
passing all `error` assertions — shown in the catalogue next to its owner. Consumers can see it before
depending on a dataset, which is the incentive that makes producers care.

**And the escape valve:** an owner can acknowledge a known failing check with an expiry date and a
reason, which suppresses the alert but shows in the catalogue as a known defect. Without this, teams
route around the system by deleting checks. With it, known defects stay visible.

**Q7.5: A dataset has no owner — the team was reorganised away. What do you do?**

*Model answer:* Orphaned datasets are the normal steady state of any organisation older than three
years, so I'd want a *process*, not a one-off.

**Immediate triage for this dataset:** determine whether anyone reads it. Access logs answer this in
minutes. Three outcomes:

- *Nobody has read it in 90 days.* Propose deletion. Announce with a 30-day notice, then move it to a
  "pending deletion" state where reads still work but generate a loud alert, wait another 30 days, then
  delete. That pending state catches the quarterly job you didn't know about, which is exactly the
  consumer that access logs miss. **This is the most common outcome and it's a genuine win** — orphaned
  datasets are cost and risk with no value.
- *It's read, and the readers can own it.* Transfer ownership to the largest consumer. They have the
  incentive to keep it correct.
- *It's read, and it's genuinely infrastructural.* The platform team takes it, but only with an explicit
  contract and quality checks — accepting an ungoverned dataset just moves the problem.

**The systemic fix**, which is the real answer: ownership must be continuously validated, not set once.
Concretely, four controls:

1. A weekly job cross-references `dataset_catalog.owner_team` against the org directory and reports
   datasets whose owner no longer exists. Ownership rot becomes visible in days.
2. Ownership transfers are part of the reorg checklist — if a team is dissolved, its datasets must be
   reassigned before the team is removed from the directory.
3. **Quarterly ownership attestation**: each owner confirms their datasets, or they're flagged. It's
   annoying, and it's the only thing that reliably works, because the alternative is discovering
   orphans during incidents.
4. Datasets with no reads for 180 days are auto-proposed for deletion. Make the default decay, not
   accumulation.

**Q7.6: How would you implement column-level lineage for a ClickHouse + Spark + dbt stack?**

*Model answer:* Different capture mechanism per engine, one common model.

**The common model — OpenLineage.** A standard event schema describing a run: job, inputs, outputs,
column-level mappings, and run facets (start, end, status). Everything emits into it; one graph store
(Marquez, DataHub, OpenMetadata, or your own Postgres tables) consumes it. Standardising on the *event
format* rather than the tool means you can swap the graph store later.

**Spark:** the OpenLineage Spark listener attaches to the session and derives lineage from the logical
plan — including column-level mappings, because the plan has them. Configuration only, no code changes.
This is the highest-fidelity source you'll have.

**dbt:** `dbt-ol` wraps dbt runs and emits OpenLineage from the manifest. dbt already knows the DAG via
`ref()`, and column-level comes from parsing the compiled SQL.

**ClickHouse:** no native support, so build it. Two sources:
- *Materialized views and scheduled queries*: parse their SQL at deploy time. You control this code, so
  parse it in CI and emit lineage as part of the deployment — which has the nice property that lineage
  is updated atomically with the change.
- *Ad-hoc and application queries*: read `system.query_log`, which records every query with its text and
  the tables it accessed. Parse the SQL for column references. Accuracy is decent for the query shapes
  that matter; be honest that dynamic SQL and `SELECT *` degrade it.

**Ingestion and Kafka:** the stream processor emits lineage explicitly — it knows its input topic and
output tables. This is declared lineage, and it's fine here because the code is yours and the mapping is
stable.

**Stitching it together:** the graph store keys nodes by fully-qualified dataset name, so
`kafka.dns.raw → bronze.dns_events → silver.dns_events → clickhouse.dns_rollup_1m → dashboard.blocked_domains`
is one connected path across four systems. That cross-system stitching is the whole value; lineage
within one engine is much less useful than lineage across the boundary, because the boundary is where
knowledge is actually lost.

**Practical advice I'd give:** start with table-level across all systems rather than column-level in one.
Coverage beats fidelity early — table-level lineage over the whole platform answers "what breaks?"
adequately, while perfect column-level lineage for Spark only leaves the ClickHouse half dark, which is
where your consumers are.

### 7.8 Tier 3 questions — deep dive

**Q7.7: Someone claims a metric on the executive dashboard is wrong. It's derived through 6 hops. How
do you investigate?**

*Model answer:* Work top-down through the lineage graph with a bisection strategy, and start by
establishing what "wrong" means.

**Step 0 — get the specific claim.** "Wrong" is usually one of: it doesn't match another dashboard, it
doesn't match a hand calculation, or it changed unexpectedly. Each points somewhere different. Get the
exact number, the exact time range, and the number they expected, with its source.

**Step 1 — check for a definitional mismatch before touching data.** In my experience a majority of
"the metric is wrong" reports are two correct numbers computed differently. Does the exec dashboard's
"active tenants" mean "tenants with ≥1 event" while finance's means "tenants with an active contract"?
Compare the definitions from the catalogue first — it's five minutes and it resolves most cases.

**Step 2 — bisect the lineage chain.** With 6 hops, don't walk them in order; check hop 3 first. At each
hop the question is "is the input to this hop right and is the output right?" Compare hop 3's output
against a recomputation from hop 2's input. That halves the search space in one step, so 6 hops takes
about 3 checks rather than 6.

**Step 3 — at the suspect hop, check the usual suspects,** in likelihood order:
- **Freshness**: is the input stale? A hop that ran before its input finished produces a correct
  computation over incomplete data. Check the run timestamps against the input's completion.
- **Filter drift**: someone added `WHERE status = 'active'` three months ago, changing the population.
  Check the transformation's git history against the date the number started diverging.
- **Join fan-out**: a join against a dimension table with duplicate keys multiplies rows. Extremely
  common, and it shows up as a number that's a suspiciously round multiple of the right one.
- **Late data**: the hop ran at 02:00 over data that was still arriving until 02:30.
- **Timezone**: the boundary of "day" differs between hops. Look for a delta that's roughly 1/24th of
  the total, concentrated at boundaries.
- **Approximate vs exact**: a `uniq()` (HyperLogLog, ~2% error) somewhere in the chain where the
  consumer assumes exactness. This is the one that produces a persistent small discrepancy that nobody
  can explain.

**Step 4 — fix, and then fix the class.** Whatever it was, add a reconciliation assertion at that hop
so the same divergence is caught automatically next time. The general control: **every hop should
assert something about its output relative to its input** — row count ratio within an expected band,
sum of a measure preserved, no unexpected nulls. Six hops with six assertions localises the next
problem in one query instead of a day.

**And the meta-fix for executive dashboards specifically:** they should be built on a small number of
certified Gold datasets with contracts and quality checks, not on a chain of six ad-hoc transformations.
If the exec dashboard is 6 hops from raw with no assertions in between, the finding is architectural.

**Q7.8: How do you handle the tension between governance and velocity? Engineers say governance slows
them down.**

*Model answer:* They're usually right, and the response is to fix the governance rather than to defend
it. Governance that slows people down is badly implemented governance — it's almost always because
it's implemented as *approval* rather than as *guardrails*.

The distinction: an approval gate is a human in the path, so its cost scales with volume and it becomes
a queue. A guardrail is automated, runs in CI, gives feedback in seconds, and its cost is near zero at
any volume. Concretely:

*Approval-shaped (bad):* "File a ticket with the data governance council to create a new dataset;
reviewed at the biweekly meeting." Median time to create a dataset: 12 days. Engineers route around it
by writing to an existing dataset or standing up their own S3 bucket, and now you have ungoverned data
*and* an approval process.

*Guardrail-shaped (good):* "Add a YAML file with owner, classification, retention class, and schema; CI
validates it and provisions the dataset." Median time: 20 minutes. Nobody routes around it because it's
faster than not using it.

**The design principle: make the governed path the easiest path.** If registering a dataset is how you
get a table provisioned, monitoring configured, and access grants working, then registration isn't
overhead — it's the tool. Nobody skips it because skipping it means doing more work.

Where I'd keep human review, deliberately and narrowly: creating a dataset classified `restricted`,
granting access to personal data, shortening a retention period, and establishing an external sharing
relationship. Four cases, each genuinely consequential, each rare. Everything else is automated. That's
maybe 2% of changes going through review instead of 100%, and because it's 2%, the review is actually
thorough — which is the second benefit of narrowing it.

**The measurement that keeps you honest:** track time-to-first-query for a new dataset. If it's rising,
governance is becoming a tax. Treat it as a platform SLO.

### 7.9 Case study: the undocumented dataset that runs payroll

**Scenario:** "You discover a ClickHouse table, `analytics.tenant_usage_final_v2`, with 4 TB of data, no
catalogue entry, no retention policy, no owner, created 18 months ago. `system.query_log` shows it's
read 40 times a day. One of the readers is the finance team's revenue reporting job. What do you do?"

**Step 1 — Do not delete it, and resist the instinct to.** It's read 40 times a day by a revenue
process. The correct first action is to *protect* it: take a snapshot to S3 immediately, before anything
else, so that whatever happens next is reversible.

**Step 2 — Identify consumers precisely, from data.**

```sql
SELECT user, initial_query_id, count() AS n, min(event_time), max(event_time),
       any(query) AS sample_query
FROM system.query_log
WHERE has(tables, 'analytics.tenant_usage_final_v2') AND event_time > now() - INTERVAL 30 DAY
GROUP BY user, initial_query_id ORDER BY n DESC;
```

That gives you the principals. Map service accounts to teams. Expect surprises: the finance job, a
dashboard, and probably two things nobody remembers.

**Step 3 — Determine what it is and where it came from.** No lineage exists, so reconstruct it: the
table DDL (`SHOW CREATE TABLE`) tells you the schema and engine; `system.query_log` filtered to
`INSERT` statements against it tells you the producer; if it's fed by a materialized view,
`system.tables WHERE engine = 'MaterializedView'` and its `as_select` gives you the transformation. If
it's populated by an external job, the inserting user identifies it.

**Step 4 — Assess the risk honestly, in three dimensions.**

*Correctness risk:* an undocumented, unmonitored table feeds revenue reporting. Nobody has verified it
in 18 months. **The most likely finding is that it's subtly wrong.** Reconcile it against a
first-principles recomputation from `silver.dns_events` for a few periods. Whatever you find, finance
needs to know.

*Compliance risk:* 4 TB with no retention policy means data that should have been deleted probably
hasn't been. Check the oldest record against what policy would say. If it contains client IPs, it's
personal data outside the governed retention system — which is a finding to disclose, exactly as in the
Part 3 case study.

*Operational risk:* no owner, no monitoring, no alerting. If it breaks, revenue reporting breaks
silently and finance discovers it at month end.

**Step 5 — Remediate in the right order.** Note that governance comes *after* stabilisation:

1. **Stabilise:** assign a temporary owner (the platform team), add basic monitoring — freshness, row
   count versus baseline — so a failure is visible today.
2. **Reconcile:** verify correctness against first principles. Report the result to finance regardless
   of outcome.
3. **Govern:** create the catalogue entry, classify it, assign a retention class, register the
   consumers, write a contract.
4. **Rationalise:** does it duplicate an existing Gold dataset? `tenant_usage_final_v2` strongly
   suggests there's a `v1` and an unfinished migration. If a governed equivalent exists, migrate
   consumers to it and deprecate this one via the normal 90-day process.
5. **Transfer ownership** to the team with the strongest interest — likely whoever owns revenue
   reporting.

**Step 6 — Ask why it existed, because that's the real finding.** Someone needed a dataset 18 months
ago and created it outside the governed path. Why? Almost always because the governed path was slower
than the ungoverned one — which is Q7.8's point exactly. **If creating a governed dataset takes two
weeks and creating an ungoverned one takes an hour, you will keep finding these.** The durable fix is
making registration the fast path, plus a detection control: a weekly job that lists tables in
ClickHouse and objects in S3 with no catalogue entry, so the next one is found in seven days rather than
eighteen months.

**The closing line:** "I'd treat the orphan as a symptom. The controls I'd add are a weekly
unregistered-asset report so detection drops from 18 months to a week, and a one-command dataset
registration so nobody has a reason to bypass it. Deleting this table without fixing those just means
I'll find another one next year."

---

## Part 8 — OLAP vs OLTP: internals, and choosing at production scale

### 8.1 What the interviewer is actually testing

The surface question ("what's the difference between OLAP and OLTP?") has a memorised answer that
everyone gives. What distinguishes a staff answer is depth of *mechanism* and honesty about *boundaries*:

1. Can you explain the difference at the level of pages, buffers, CPU cache, and vectorised execution —
   not just "row vs column"?
2. Do you know each system's failure modes at production scale? Anyone can describe ClickHouse's
   strengths; the useful engineer knows what breaks at 8 shards and 30 TB.
3. Can you place the boundary? At what data size, query pattern, and concurrency does the answer flip?
4. Do you know the warehouse landscape — Snowflake, Databricks, BigQuery, Redshift, ClickHouse — well
   enough to choose between them with reasons that aren't marketing?
5. Have you actually operated one? Interviewers detect this quickly, and the tell is whether you talk
   about merges, vacuum, spill, and skew — the things you only learn from running the thing.

### 8.2 The mechanism, derived properly

Everyone says "OLTP is row-oriented, OLAP is column-oriented." Let's make that mean something.

**How a row store executes a query.** Postgres stores tuples in 8 KB pages. A page holds a page header,
an array of item pointers, and the tuples themselves growing from the end. A tuple contains all the
row's columns contiguously, plus a 23-byte header carrying `xmin`, `xmax` (the transaction IDs that
created and deleted it), and flags.

For our Skyline event row at ~200 bytes stored, a page holds about `(8192 − 24) / (200 + 4) ≈ 40` rows.

Now `SELECT threat_category, count(*) FROM events WHERE ts > X GROUP BY threat_category` over 2.3
billion rows:

```
2.3e9 rows ÷ 40 rows/page = 57,500,000 pages
57.5e6 pages × 8 KB = 460 GB read from disk
```

...to extract one column that represents maybe 1% of the bytes. Every page read brings 200× more data
than needed. Worse, the CPU walks 2.3 billion tuples, each requiring: read the header, check MVCC
visibility against the snapshot, compute the column offset (variable-length columns before it mean
this isn't a constant), and extract. That's branch-heavy pointer-chasing code with a cache miss on
essentially every row, because 200 bytes of stride blows through a 64-byte cache line.

**How a column store executes the same query.** ClickHouse stores each column in its own file, in sorted
order, compressed in blocks. `threat_category` is `LowCardinality(String)`, so on disk it's a
dictionary plus an array of small integer indices, then ZSTD over that.

```
2.3e9 rows × ~1 byte (dictionary index, before compression) ≈ 2.3 GB
after ZSTD over sorted, highly-repetitive data: ~150 MB actually read
```

**460 GB versus 150 MB — a factor of about 3,000** on this particular query. Then the execution: the
column is decompressed into a contiguous array of `UInt8` dictionary indices, and the aggregation is a
loop over that array incrementing counters. No MVCC check (parts are immutable — nothing to check). No
offset computation (fixed width). No pointer chasing. The loop vectorises: with AVX-512, 64 one-byte
values per instruction. And ClickHouse processes in blocks of 65,536 values, so the working set stays in
L2 cache.

**That's the real answer to "why is OLAP faster for analytics."** It's four compounding effects: less
I/O (columnar projection), less I/O again (compression from sorting), fewer instructions (vectorised
execution over fixed-width arrays), and better cache behaviour (contiguous access). Any one gives maybe
5×; together they give 1000×+.

**Now the reverse, which people forget to explain.** `UPDATE policy_rules SET action = 'block' WHERE
rule_id = 88213`:

*Postgres:* look up `rule_id` in a B-tree index (3–4 page reads, all likely cached), read the heap page
containing the tuple (1 read), write a new tuple version, mark the old one dead, write a WAL record,
fsync. Total: ~5 page accesses, one sequential fsync, sub-millisecond. Concurrent updates to other rows
don't block, because locking is per-tuple.

*ClickHouse:* there is no such operation. `ALTER TABLE ... UPDATE` is a mutation: ClickHouse identifies
every part containing a matching row and **rewrites those parts entirely** — all 16 column files, all
rows in them. For a part containing 10 million rows, changing one row rewrites 10 million rows across
16 files. It's asynchronous, tracked in `system.mutations`, and competes with merges for disk bandwidth.

**So the trade is explicit:** the column store gets its 1000× read advantage by giving up in-place
mutability, per-row locking, and multi-statement transactions. Those aren't oversights; they're what
paid for the speed. **Say it that way** — as a purchase rather than a limitation — and the whole
comparison becomes coherent.

### 8.3 The other structural differences that matter

Beyond layout, four more mechanisms differ and each shows up in production decisions.

**MVCC and the vacuum problem (OLTP).** Postgres never updates in place; it writes a new tuple version
and leaves the old one for later cleanup. This gives readers a consistent snapshot without blocking
writers — excellent. The cost is **bloat**: dead tuples occupy space until `VACUUM` reclaims them, and
autovacuum must keep up with the update rate.

At Skyline's control plane (800 tx/sec, mostly reads) this is a non-issue. But consider the hypothetical
from Part 5 of putting 40 million client-IP state rows in Postgres with thousands of updates/sec: each
update writes a new tuple, so the table churns its entire size repeatedly, autovacuum falls behind, the
table bloats to several times its live size, and query performance degrades because scans read dead
tuples. The mitigations you should know: `fillfactor` below 100 to leave room for **HOT updates**
(heap-only tuples — if no indexed column changed and there's room on the page, the new version goes on
the same page and no index update is needed, which is a large win); per-table aggressive autovacuum
settings; and avoiding updates to indexed columns.

Also know **transaction ID wraparound**: `xmin`/`xmax` are 32-bit, so after ~2 billion transactions IDs
wrap, and Postgres must "freeze" old tuples before that happens. If autovacuum can't keep up, Postgres
eventually refuses writes to protect data. It's rare but catastrophic, and monitoring
`age(datfrozenxid)` is basic Postgres operational hygiene.

**Merge and part management (OLAP).** ClickHouse's equivalent background tax. Parts accumulate from
inserts and merge into larger ones in the background — an LSM-tree-like process. The operational
concerns: **write amplification** (a row may be rewritten 5–10 times as it merges up through size
tiers, which is the real cost of the compression you enjoy), merges needing free disk equal to the
merged parts' size (so a disk above ~80% can deadlock merges), and merge threads competing with query
threads for CPU and I/O.

**The symmetry worth stating:** Postgres has vacuum, ClickHouse has merges. Both are background
processes reclaiming the cost of their respective write strategies, both can fall behind under load,
and both produce the same operational signature — degrading performance and growing disk — when they
do. If you understand one, you understand the shape of the other.

**Concurrency models.** Postgres: one backend process per connection, designed for thousands of
concurrent small queries. Each is single-threaded (parallel query exists but is limited). Connection
overhead is real — a few MB per backend — which is why PgBouncer exists.

ClickHouse: designed for a small number of large queries, each using many threads. `max_threads`
defaults to the core count, so **one query can consume the whole machine.** `max_concurrent_queries`
defaults to 100. Throwing 1,000 concurrent small queries at ClickHouse gives worse throughput than 10
concurrent large ones — the opposite of Postgres's profile. This is why Part 5's dashboard queries go
to pre-aggregated tables: it's not just about scan size, it's about keeping concurrency low.

**Durability and consistency.** Postgres: WAL, `synchronous_commit`, real ACID, choice of isolation
levels up to Serializable via SSI. ClickHouse: an insert into a single MergeTree table is atomic at the
part level (the whole block appears or doesn't), replication is eventually consistent by default with
`insert_quorum` available for stronger guarantees, and there are no cross-table transactions. **A
materialized view firing on an insert is not transactional with that insert** — which is exactly the
Q5.8 failure mode.

### 8.4 Where the boundary actually is

The most useful thing you can offer is a decision procedure with numbers. Here's mine, in the order I'd
apply it.

**Question 1 — What does one query touch?** If the typical query touches fewer than ~1,000 rows
identified by a key, that's OLTP, regardless of table size. A billion-row table with point lookups is a
Postgres table with a good index. If the typical query touches millions of rows and aggregates, that's
OLAP even if the table is small.

**Question 2 — Do you need multi-row transactions or enforced constraints?** If yes, OLTP, or you're
building those guarantees in application code, which is a bad trade. This question alone decides the
Skyline control plane.

**Question 3 — What's the write pattern?** Append-only with immutable rows favours OLAP. Frequent
in-place updates of individual rows favours OLTP. A workload that's 80% appends and 20% updates of
recent rows is the awkward middle — that's where `ReplacingMergeTree` or an HTAP system earns its keep.

**Question 4 — What's the concurrency?** Thousands of concurrent queries points to OLTP engines or a
serving cache in front of OLAP. Tens of concurrent queries is fine for OLAP.

**Question 5 — What's the size?** Only now, and it's mostly a tiebreaker:

- **< 100 GB:** Postgres for everything unless the query pattern is overwhelmingly analytical. The
  operational simplicity of one system is worth a lot, and Postgres with good indexes and
  pre-aggregation handles more analytics than people expect.
- **100 GB – 2 TB:** Postgres still viable with partitioning and BRIN indexes; TimescaleDB if it's
  time-series. Introduce OLAP if analytical queries dominate or if they're interfering with the
  transactional workload.
- **2 TB – 20 TB:** the crossover. Analytical queries are painful in Postgres. Introduce a dedicated
  OLAP store, keep Postgres for the transactional workload.
- **> 20 TB analytical:** dedicated OLAP, no question. Skyline is at 260 TB.

**The trap in this question** is the candidate who says "it depends on size" and stops. Size is the
*last* input. A 50 TB append-only log with only point lookups by key is DynamoDB, not ClickHouse. A
200 GB table with complex aggregations run by 50 analysts is ClickHouse, not Postgres. **Access pattern
dominates size.**

### 8.5 The warehouse landscape, compared honestly

You will be asked to compare. Here's a defensible take on each, focused on the properties that actually
drive decisions rather than feature lists.

**ClickHouse.** Fastest per dollar for high-volume, filter-and-aggregate workloads over
mostly-append-only data — often by a large margin. Best when your query shape is known in advance and
you can design the sort key for it. Excellent compression, extremely good ingest throughput.

*Weaknesses to name, because naming them is what shows experience:* joins are historically its weakest
area (the default hash join builds the right-hand table in memory; large-to-large joins need
`grace_hash` or `partial_merge` and are still comparatively slow — the idiomatic fix is dictionaries or
denormalisation). Updates and deletes are painful. Cluster operations are manual in the open-source
version — resharding is a genuine project, not a button. Cross-shard queries need `GLOBAL JOIN`, which
broadcasts. And it will happily let you write a query that consumes all cluster memory unless you've
configured limits.

*Choose it when:* high volume, known query patterns, cost-sensitive, and you have the operational
capability. Skyline is exactly this profile.

**Snowflake.** Best-in-class separation of storage and compute, with genuinely independent virtual
warehouses so workloads don't interfere. Zero-copy cloning and time travel are excellent for
development and reproducibility. Secure data sharing is the best in the market. Near-zero operational
burden.

*Weaknesses:* cost is consumption-based and can escalate quickly without governance — the classic
failure is an analyst's scheduled query on a large warehouse costing thousands a month. Less control
over physical layout (clustering keys exist but you don't manage files directly). Proprietary format,
though Iceberg support has improved that substantially. Latency floor is higher than ClickHouse for
sub-second dashboard queries.

*Choose it when:* you have many independent teams needing isolation, you value operational simplicity
over cost efficiency, or data sharing with partners is a first-class requirement.

**Databricks.** Strongest when the workload spans SQL analytics *and* ML/data science on the same data —
that's its actual differentiator, not SQL performance. Delta Lake gives ACID on object storage; Unity
Catalog gives governance across both worlds. Photon closed much of the SQL performance gap.

*Weaknesses:* more complex operationally than Snowflake. Cluster startup latency makes it awkward for
interactive dashboards without SQL warehouses running continuously. Costs come from two directions (DBU
plus cloud compute), which makes forecasting harder.

*Choose it when:* ML is co-equal with analytics, or you're already invested in Spark.

**Redshift.** Deep AWS integration and predictable pricing on reserved instances. RA3 nodes separated
storage and compute; Serverless removed much of the operational burden.

*Weaknesses:* historically required significant manual tuning (distribution keys, sort keys, vacuum),
though much is now automated. Concurrency has been a persistent pain point (concurrency scaling helps,
at cost). Generally behind Snowflake and ClickHouse on price/performance for the same workload.

*Choose it when:* you're deeply committed to AWS, have a moderate workload, and value the integration
over the last increment of performance.

**BigQuery.** Genuinely serverless — no cluster to size. Excellent for spiky, unpredictable analytical
workloads. Strong ML integration via BigQuery ML.

*Weaknesses:* on-demand pricing is per-byte-scanned, which is dangerous for exploratory work (`SELECT *`
on a large table is an expensive mistake) — mitigate with partitioning, clustering, and capacity-based
pricing. It's GCP-only, which is a real constraint in a multi-cloud posture.

*Choose it when:* you're on GCP, your workload is spiky, and you want zero infrastructure management.

**The framing to offer:** these split along two axes. *Operational burden versus cost efficiency* —
ClickHouse self-hosted is the cheapest and the most work; BigQuery/Snowflake are the most expensive and
the least work. And *specialised versus general* — ClickHouse is exceptional at one shape of query;
Snowflake and Databricks are good at everything. **For Skyline, the query shape is known, the volume is
high, and the team has the capability, so ClickHouse's specialisation pays. If Skyline had a small
platform team and unpredictable analyst workloads, I'd argue for Snowflake despite the cost** — and I'd
want the interviewer to see that I can argue both sides.

### 8.6 Tier 1 questions — screening

**Q8.1: OLAP vs OLTP — explain the difference.**

*Model answer:* They optimise for opposite access patterns, and the whole difference follows from the
physical layout that choice implies.

OLTP handles many small transactions touching few rows, with strict consistency requirements. Row-
oriented storage keeps a row's columns contiguous, so a point lookup is one page read. B-tree indexes
find rows by key in logarithmic time. MVCC lets readers and writers proceed concurrently. A WAL makes a
commit durable with one sequential fsync. Postgres doing a single-row update is ~5 page accesses and
sub-millisecond.

OLAP handles few large queries scanning many rows and touching few columns. Column-oriented storage puts
each column in its own file, so a query reading 2 of 16 columns reads about 1/8 of the bytes — and
because sorted columns compress far better, in practice far less than that. Execution is vectorised over
contiguous fixed-width arrays, so aggregation is a SIMD loop instead of pointer-chasing.

The concrete number: aggregating one column over 2.3 billion Skyline rows reads ~460 GB in Postgres and
~150 MB in ClickHouse, and the ClickHouse loop is cache-friendly and vectorised on top of that.

The purchase price: the column store gives up in-place updates, row-level locking, and multi-statement
transactions. That's not a gap in the product — it's what bought the read performance.

**Q8.2: When would you use Postgres over ClickHouse?**

*Model answer:* Whenever the workload wants transactions, constraints, in-place mutation, or high
concurrency of small queries.

For Skyline that's the entire control plane: tenants, policies, users, grants, retention policies,
schema registry. Creating a policy rule updates three tables atomically — impossible in ClickHouse.
Foreign keys enforce that every resolver belongs to a real tenant — no equivalent in ClickHouse.
Customers edit individual rules constantly — single-row updates, which ClickHouse handles by rewriting
parts. And it's 800 mostly-tiny transactions per second, which is Postgres's ideal profile and
ClickHouse's worst.

I'd also use Postgres over ClickHouse for *analytical* workloads below a few hundred GB where the
operational simplicity of one system outweighs the performance difference. A 50 GB analytics table with
a BRIN index on the timestamp and a few materialized views serves a lot of dashboards perfectly well,
and running one database instead of two is worth real money in team time.

**Q8.3: What's the biggest ClickHouse operational problem you've hit?**

*Model answer:* Part explosion — "too many parts" — and it's worth describing because the fix is
structural rather than a setting.

The mechanism: every insert creates a part; background merges consolidate them; if insert rate exceeds
merge rate, parts accumulate until ClickHouse throttles inserts at 150 parts per partition and rejects
them at 300. Root causes, in order of frequency: too-small batches (someone reduced the flush threshold
to improve freshness), a partition key with too much cardinality (each insert scatters across many
partitions), late-arriving data spreading one batch across many day-partitions, and merges being
starved by a concurrent mutation or by low free disk — merges need free space equal to the parts being
merged, so a disk above 80% can deadlock them, which then fills the disk faster.

The fixes are: batch to 10,000–100,000 rows with at most about one insert per second per table per
shard; keep partitions coarse — daily, not hourly, and never by tenant; route late data to a separate
backfill table; and alert on `parts per active partition` at 100 so you see it 30 minutes before the
throttle rather than at the rejection.

The general lesson is that ClickHouse's write path assumes batching, and every ingestion component
upstream exists to provide it. Treat "one insert per second per table" as an architectural constraint,
not a tuning parameter.

### 8.7 Tier 2 questions — design

**Q8.4: Design the data model for a feature that shows each tenant their top 100 queried domains, with
trend, updated every minute, over any window from 1 hour to 90 days.**

*Model answer:* Work backwards from the query shapes, because "any window from 1 hour to 90 days" spans
three orders of magnitude and one physical design won't serve all of it.

The naive design — scan raw events with `GROUP BY query_name ORDER BY count() DESC LIMIT 100` — costs,
for a 90-day window on Meridian:

```
27,000 events/sec × 86,400 × 90 = 210 billion rows for one tenant
```

Even at ClickHouse's speed that's tens of seconds and enormous I/O, per request, per tenant. Not viable.

**The design: a rollup hierarchy.**

```sql
-- Level 1: per-minute, per-domain counts (feeds everything else)
CREATE TABLE domain_counts_1m (
    tenant_id UInt32, minute DateTime, query_name LowCardinality(String),
    events AggregateFunction(sum, UInt64),
    clients AggregateFunction(uniq, IPv6)
) ENGINE = ReplicatedAggregatingMergeTree(...)
PARTITION BY toYYYYMM(minute)
ORDER BY (tenant_id, minute, query_name)
TTL minute + INTERVAL 8 DAY;

-- Level 2: hourly, from the minute table
CREATE TABLE domain_counts_1h (...) ORDER BY (tenant_id, hour, query_name) TTL hour + INTERVAL 100 DAY;

-- Level 3: daily
CREATE TABLE domain_counts_1d (...) ORDER BY (tenant_id, day, query_name) TTL day + INTERVAL 400 DAY;
```

Materialized views chain them: raw → 1m, 1m → 1h, 1h → 1d. Each level has a shorter TTL than the one
below it, because you only need minute granularity recently.

**The query router picks the coarsest table that satisfies the window:** ≤ 6 hours → minute table;
6 hours – 14 days → hourly; > 14 days → daily. A 90-day query now reads:

```
90 days × ~50,000 distinct domains for a large tenant = 4.5 million rows
```

instead of 210 billion. **A 46,000× reduction**, and it runs in well under a second.

**Now the part that separates a good answer from a complete one — cardinality control.** The minute-level
table has a row per `(tenant, minute, domain)`. For Meridian with 50,000 distinct domains per minute,
that's 50,000 rows/minute = 72 million rows/day for one tenant. Across all tenants the rollup could
approach the raw table's size, which defeats the purpose.

Two fixes:

*Truncate the domain.* Store the eTLD+1 (`example.com`), not the full FQDN. `a1b2c3.tunnel.evil.com`
and `d4e5f6.tunnel.evil.com` both become `evil.com`. Cardinality drops by 10–50× for typical traffic and
by orders of magnitude for tunnelling traffic. Since the feature is "top domains," the eTLD+1 is what
users want anyway.

*Keep only the heavy hitters per bucket.* You need the top 100. Storing all 50,000 domains per minute to
answer a top-100 query is wasteful. Use a `TOPK` sketch — ClickHouse's `topKState(200)` — storing an
approximate top-200 per bucket in a few KB. Merging sketches across buckets gives an approximate top-100
over any window.

**But be careful and say why:** top-K sketches are *not* mergeable without error. An item ranked 150th
in every minute of a day might be top-10 for the day and be missing from every per-minute sketch. So the
honest design keeps exact counts at the minute level with the eTLD+1 truncation (which makes cardinality
manageable), and uses sketches only where approximation is explicitly acceptable. **Volunteering this
limitation is the strongest move in this answer** — plenty of candidates propose sketches without
knowing they don't merge cleanly.

**Trend** ("up 40% vs last week") is a second query against the same rollup for the prior period. Cheap,
because it's the same shape.

**Freshness:** the materialized view fires on insert, so the minute table is current within the insert
batch interval (~2 seconds). The "updated every minute" requirement is satisfied with 30× headroom.

**Q8.5: Your ClickHouse cluster is at 80% disk. What are your options, in order?**

*Model answer:* First, urgency: 80% is when merges start being constrained (they need free space equal
to the parts being merged), so this is a "this week" problem that becomes a "right now" problem at ~90%.
Options in order of speed and reversibility:

**Immediate (minutes to hours), buys time:**
1. **Drop expired partitions the TTL hasn't gotten to.** Check for partitions past retention that TTL
   hasn't processed — `ttl_only_drop_parts` may be off, or TTL merges may be backlogged. An explicit
   `ALTER TABLE ... DROP PARTITION` is instant and frees whole partitions.
2. **Clear `shadow/` directories** from old `FREEZE` operations. These are hard links, so they only
   consume space for parts that have since been merged away — but on a cluster that's been frozen a few
   times, it can be a lot, and it's completely safe to remove old freeze snapshots you've already backed
   up.
3. **Check for orphaned parts and stale detached parts** — `system.detached_parts`. These accumulate
   from failed merges and replica recovery and are frequently gigabytes.

**Short term (days):**
4. **Tighten TTL to move data to the S3-backed volume sooner.** Changing `TO VOLUME 's3_cold'` from 30
   days to 14 days moves ~7.7 TB off local disk. Queries over that range get slower; that's the trade,
   and it's reversible.
5. **Improve compression on the biggest columns.** Check `system.columns` for
   `data_compressed_bytes` per column. Often one column dominates — for Skyline it's
   `query_name_full`. Switching it from `ZSTD(1)` to `ZSTD(6)`, or adding a codec, can save 20–30% on
   that column at some CPU cost. Applies to new parts immediately, old parts as they merge, or force it
   with `OPTIMIZE`.
6. **Drop unused columns or indexes.** A skip index that isn't being used (check `EXPLAIN indexes = 1`
   on real queries) costs space and write throughput for nothing.

**Medium term (weeks):**
7. **Add shards.** The real fix if this is growth rather than a one-off. Note that ClickHouse
   resharding is not automatic: you add shards, then rebalance by copying partitions with
   `clickhouse-copier` or by re-inserting from the lake. **This is a project, not an operation**, which
   is exactly why you want to notice disk pressure at 70%, not 85%.
8. **Reduce replication factor**, if you're at 3 and can justify 2 — that cuts storage by a third, at
   the cost of tolerating one fewer replica loss. Rarely the right call, but it's an option to name.

**The structural point:** this shouldn't be a surprise. Disk growth is predictable from ingest volume,
and you should have a capacity model that projects to 6 months with an alert at the point where
"add shards" still has enough lead time. **Alert on projected days-until-full, not on percentage.** At
Skyline's 481 GB/day across 8 shards, each shard grows 60 GB/day, so on a 4 TB disk at 80% (3.2 TB used,
800 GB free), you have 13 days. "13 days" is an actionable alert; "80%" isn't.

**Q8.6: You need to join event data with a 500-million-row dimension table in ClickHouse. How?**

*Model answer:* First, question the requirement — a 500M-row dimension is unusual and often signals a
modelling problem. But assuming it's real (say, a domain reputation table), the options in order of
preference:

**1. Don't join — denormalise at write time.** Look the reputation up in the stream processor and store
it as a column. Costs storage; eliminates the join entirely. This is usually right for a value that
doesn't change often. The failure mode: when reputation changes, historical rows keep the old value —
which is often *correct* (you want to know what we knew at the time, per Part 5's point-in-time
discussion), but must be a conscious decision.

**2. Dictionary, if it fits in memory.** ClickHouse dictionaries with `LAYOUT(HASHED)` hold the whole
thing in RAM on every node. For 500M rows with a UInt64 key and a UInt8 value, that's roughly
500e6 × (8 + 1 + hash overhead ~16) ≈ 12 GB per node. Feasible on 128 GB nodes but significant.
`LAYOUT(SPARSE_HASHED)` cuts it substantially at some CPU cost. `LAYOUT(COMPLEX_KEY_CACHE)` or
`SSD_CACHE` holds a bounded subset with misses going to the source — good when access is skewed, which
domain reputation lookups are (a small set of domains gets most traffic).

Then it's `dictGet('domain_rep', 'score', domain_id)` — a hash lookup per row, not a join.

**3. An actual JOIN with the right algorithm.** If you must, know the choices:
- `hash` (default): builds the right table in memory. 500M rows will exceed memory — this fails.
- `parallel_hash`: same but multi-threaded build. Faster, same memory problem.
- `grace_hash`: partitions both sides into buckets that fit in memory, spilling to disk. **This is the
  right choice for large-to-large joins** and it's the one to name.
- `full_sorting_merge`: sorts both sides and merges. Good when one side is already sorted on the join
  key.
- `partial_merge`: minimises memory at significant speed cost.

Set with `SETTINGS join_algorithm = 'grace_hash'`. And remember ClickHouse's `JOIN` semantics quirk:
the right-hand table is the one held in memory, so **put the smaller table on the right** — the
optimiser will not always reorder for you.

**4. In a distributed setting**, a plain `JOIN` against a `Distributed` table performs the join on each
shard against that shard's local data, which is wrong unless the dimension is co-located. `GLOBAL JOIN`
gathers the right side onto the initiator and broadcasts it to all shards — correct, but it moves the
whole dimension across the network for every query. For a 500M-row dimension that's untenable, which
pushes you back to (1) or (2).

**The answer for Skyline:** denormalise the reputation score at ingest (option 1), keeping
`reputation_version` as a column so we know which snapshot scored it, and use a `SSD_CACHE` dictionary
for the enrichment lookup in the stream processor. No query-time join at all.

### 8.8 Tier 3 questions — deep dive

**Q8.7: Explain what happens when you run `OPTIMIZE TABLE ... FINAL` on a 10 TB table.**

*Model answer:* You force-merge every part in every partition into one part per partition, and on a
10 TB table this is almost always a mistake.

Mechanically: ClickHouse schedules merges for all parts. Each merge reads the input parts, merges them
in sort-key order (applying the engine's collapse rules — `ReplacingMergeTree` dedup, `AggregatingMergeTree`
state merging), and writes a new part. Reading 10 TB and writing 10 TB, at maybe 200 MB/sec of effective
merge throughput per node, is:

```
10 TB ÷ 200 MB/sec ≈ 50,000 seconds ≈ 14 hours per node
```

During which it saturates disk I/O, competes with ingestion (causing the part accumulation from Q8.3),
and requires free disk space equal to the largest merge. It's also **not resumable** in a useful way —
kill it and the completed merges stand, but the rest is undone work.

**And the outcome is usually undesirable even if it succeeds.** One giant part per partition means any
future merge involving it must rewrite the whole thing, so subsequent merges get *more* expensive. The
tiered structure that MergeTree maintains exists for a reason.

**When it's legitimate:** on a single small partition after a bulk load, to collapse duplicates before
taking a snapshot; on a partition you're about to freeze and archive; or in a maintenance window on a
replica that isn't serving traffic.

**What people actually want when they reach for it** is usually one of:
- *"I want deduplicated reads"* → use `FINAL` in the `SELECT` (merges on the fly, per query), or the
  `argMax` idiom. `SELECT ... FINAL` on a well-sorted table with `do_not_merge_across_partitions_
  select_final = 1` is much cheaper than people assume.
- *"I want to reclaim space from deleted rows"* → `OPTIMIZE ... FINAL` on the specific partitions
  affected, one at a time, with `PARTITION` specified.
- *"Query performance is bad because of too many parts"* → fix the ingestion batching; the merges will
  catch up on their own.

**The answer to give:** "I wouldn't. I'd ask what problem it's solving, because each of the three real
problems has a better answer, and on 10 TB this saturates the cluster for most of a day and leaves the
table in a worse structural state."

**Q8.8: How would you shard a ClickHouse cluster, and what happens when you need to reshard?**

*Model answer:* Sharding key first, then the honest answer about resharding, which is that it's hard and
you should design to avoid it.

**Sharding key: hash of `tenant_id`, via explicit assignment rather than modulo** (per Part 6). The
reasons: every customer-facing query filters by tenant, so tenant-sharding means each query hits exactly
one shard — no scatter-gather, no cross-shard aggregation, and 8× the effective concurrency. Explicit
assignment rather than `tenant_id % N` lets you bin-pack by observed volume and split whales.

The alternative — sharding by a hash of `event_id` for uniform distribution — spreads every tenant across
all shards. Uniform load, but every query fans out to all shards and aggregates results at the
initiator. For Skyline's query pattern that's strictly worse.

**Resharding: the honest answer is that ClickHouse doesn't do it for you.** There's no automatic
rebalance. Your options:

1. **`clickhouse-copier`** (or its successors) — copies partitions between clusters/shards according to a
   config. It works, it's slow, and it needs careful coordination with ongoing writes.
2. **Re-ingest from the lake.** This is why Part 5 insisted ClickHouse is a materialisation of Iceberg
   rather than a system of record. Stand up new shards, replay from S3 into the new topology, dual-write
   during the transition, verify, cut reads over, drop the old. Slower in wall-clock time, dramatically
   safer, and it exercises the restore path you should be testing anyway.
3. **Split at the assignment layer without moving data.** Because shard assignment is a control-plane
   table, you can direct *new* tenants and *new* data to new shards while old data stays put. Combined
   with a `Distributed` table that knows the full topology, queries still work. Over time the old shards
   age out via TTL and you've rebalanced without copying anything. **This is the technique worth
   volunteering** — for time-partitioned data with bounded retention, you can often reshard by waiting.

**The design principle:** over-shard initially. Sixteen logical shards on 8 physical nodes (two shards
per node) means doubling capacity is moving shards to new nodes rather than resplitting data. The cost
is slightly more metadata and more parts; the benefit is that the expensive operation becomes a cheap
one. It's the same reasoning as choosing a Kafka partition count you won't need to change.

**Q8.9: Postgres query that was fast is now slow. No code changed. Walk me through it.**

*Model answer:* "No code changed" means the *plan* changed or the *data* changed. Work it in that order.

**1. Get the plan.** `EXPLAIN (ANALYZE, BUFFERS)` on the actual query. Compare estimated versus actual
rows at each node. A large divergence — estimated 100, actual 2,000,000 — means the planner had bad
statistics and chose accordingly, which is the most common cause.

**2. Check statistics freshness.** `pg_stat_user_tables.last_analyze` and `last_autoanalyze`. If the
table grew substantially since the last `ANALYZE`, estimates are stale. Autovacuum's analyze threshold
is 10% of the table by default, which for a large, steadily-growing table means statistics can be badly
out of date for a long time. `ANALYZE` the table and re-check.

**3. Look for the classic plan flip: nested loop to hash join, or index scan to sequential scan.** The
usual mechanism: the table crossed a size threshold where the planner's cost model decided a seq scan
was cheaper. Sometimes it's right and the query genuinely needs a different index. Sometimes it's wrong
because `random_page_cost` is set for spinning disks (default 4.0) on an SSD, where 1.1 is more
realistic — that single setting causes a lot of unnecessary sequential scans.

**4. Check for bloat.** `pg_stat_user_tables.n_dead_tup` versus `n_live_tup`. If dead tuples are a large
fraction, scans read mostly-dead pages. Cause: autovacuum can't keep up, or a long-running transaction
is holding back the xmin horizon so vacuum *can't* remove tuples. Check
`pg_stat_activity` for old transactions and idle-in-transaction connections — a forgotten `BEGIN` in an
application can block vacuum across the whole database.

**5. Check index health.** A bloated index (from many updates) gets slower. `REINDEX CONCURRENTLY`
rebuilds without blocking. Also check whether the index is still being *used* —
`pg_stat_user_indexes.idx_scan` — since a plan flip may have abandoned it.

**6. Check for lock contention.** `pg_locks` joined to `pg_stat_activity`. A query waiting on a lock is
slow without any plan issue. `log_lock_waits = on` catches this historically.

**7. Check parameter-value skew.** With a prepared statement, Postgres may build a generic plan after
five executions that's good on average and terrible for the specific parameter values now being used.
`plan_cache_mode = force_custom_plan` tests this. Correlated with "it's slow for one customer only."

**8. Check the environment.** Did the instance change? Is the buffer cache cold after a restart or
failover? Is another workload competing for I/O? `pg_stat_statements` gives you the before/after per
query and is the single best tool here — if you don't have it enabled, enable it today, because it's
the thing that makes this whole investigation five minutes instead of an afternoon.

**The framing:** "no code changed" is a strong clue but not a constraint — data changed, statistics
changed, or the environment changed, and the plan comparison tells you which in the first two minutes.

### 8.9 Case study: choosing the warehouse

**Scenario:** "Leadership wants to consolidate on one analytical platform. Today: ClickHouse for product
analytics, Redshift for finance, and three teams with their own Athena setups. Someone proposes moving
everything to Snowflake. Evaluate."

**Step 1 — Establish what problem consolidation solves,** because "consolidate" is a means, not an end.
Ask which of these is actually hurting: cost (paying for four platforms), inconsistency (four answers to
"how many active tenants"), operational burden (four systems to run), or capability gaps. The right
architecture differs sharply depending on which.

If the answer is *inconsistency*, consolidating engines doesn't fix it — a shared semantic layer and
data contracts do, and you can have those across four engines. **That's the most important thing to say,
because it's the most common case and consolidation is the wrong answer to it.** If the answer is
*operational burden*, consolidation genuinely helps.

**Step 2 — Characterise each workload honestly.**

*Product analytics (ClickHouse):* 13B events/day, sub-second dashboards, 30-day hot. This is
ClickHouse's ideal workload and where it's several times cheaper than the alternatives.

*Finance (Redshift):* modest volume, complex SQL, month-end batch, absolute correctness required, heavy
joins across dimension tables. Volume is small; complexity is high. ClickHouse's join weakness makes it
a poor fit; Snowflake is a very good fit.

*Three Athena setups:* ad-hoc exploration over S3. Low, spiky usage. Athena's pay-per-query is well
matched, and the real problem here is probably governance, not the engine.

**Step 3 — Evaluate the Snowflake proposal against each.**

*Finance:* clear win. Better SQL, better concurrency, less tuning, and it's a small workload so cost is
modest.

*Ad-hoc:* win on governance and experience. Cost depends on discipline — needs warehouse auto-suspend,
resource monitors, and per-team budgets from day one, or it escalates.

*Product analytics:* **this is where the proposal fails, and it's the crux.** Model it. Skyline scans
roughly 30 TB/day across dashboard queries. Snowflake pricing is credit-based, so estimate the warehouse
size and hours needed to serve sub-second dashboard queries at Skyline's concurrency. Realistically
you'd need multiple large warehouses running continuously — call it a Large (8 credits/hour) plus a
Medium (4 credits/hour) running 24/7:

```
12 credits/hour × 730 hours × ~$3/credit ≈ $26,000/month
```

versus the ClickHouse cluster's 16 nodes at roughly $1,500/month each all-in ≈ **$24,000/month** — so
comparable in raw cost, but Snowflake's latency floor is higher and the dashboard SLO is p99 < 500ms.
The honest conclusion is that Snowflake likely *cannot meet the latency SLO* for this workload at any
reasonable cost, which makes it a capability question rather than a cost question.

**Step 4 — Recommend, with the trade named.**

"Consolidate finance and the three Athena setups onto Snowflake. Keep ClickHouse for product analytics,
because it's serving a sub-second SLO at a price Snowflake can't match, and the workload is stable
enough that its operational burden is bounded.

What I'd consolidate instead is the *layer that actually causes the inconsistency*: one Iceberg lake as
the shared source of truth, one catalogue, one semantic layer defining metrics once. Snowflake reads
Iceberg tables externally; ClickHouse reads the same S3 data; Athena queries the same catalogue. Then
'how many active tenants' has one definition regardless of engine, which was the actual problem.

So: two engines, one storage layer, one semantic layer. That gets 90% of the consolidation benefit at
none of the latency or cost risk."

**Step 5 — Name what would change your mind, because that's what makes it a recommendation rather than
an opinion.** "If the product analytics workload became less predictable — many teams running arbitrary
queries rather than a fixed dashboard set — ClickHouse's advantage would shrink, because its advantage
comes from designing the sort key for known queries. And if we lost the operational capability to run
ClickHouse — if the two people who know it left — I'd revisit immediately, because a self-managed
cluster nobody understands is more expensive than any SaaS bill."

---

## Part 9 — SQL: writing it, reading plans, and making it fast

### 9.1 What the interviewer is actually testing

SQL rounds at staff level are not "can you write a join." They test:

1. Can you write **correct** SQL for a genuinely tricky requirement — sessionisation, gaps and islands,
   as-of joins, deduplication with tie-breaking?
2. Can you read an execution plan and say *why* it's slow, not just that it is?
3. Do you know the optimisations that matter, and — more importantly — do you know which ones are myths?
4. Can you reason about a query's cost before running it?
5. Do you know the dialect differences that bite? (`uniq` vs `uniqExact`, `ANY JOIN`, `PREWHERE`,
   `LIMIT BY` — ClickHouse has real semantic differences from standard SQL.)

This part is drill-heavy. Work through the queries rather than reading them.

### 9.2 Reading a Postgres plan

Take a real slow query from Skyline's control plane: "find all policy rules created in the last 30 days
for enterprise tenants, with the tenant name."

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT t.name, pr.rule_id, pr.pattern, pr.created_at
FROM policy_rules pr
JOIN tenants t ON t.tenant_id = pr.tenant_id
WHERE pr.created_at >= now() - INTERVAL '30 days'
  AND t.tier = 'enterprise'
ORDER BY pr.created_at DESC
LIMIT 100;
```

```
Limit  (cost=185432.11..185432.36 rows=100 width=84) (actual time=2841.203..2841.219 rows=100 loops=1)
  Buffers: shared hit=1204 read=98341
  ->  Sort  (cost=185432.11..185498.44 rows=26532 width=84) (actual time=2841.201..2841.209 rows=100 loops=1)
        Sort Key: pr.created_at DESC
        Sort Method: top-N heapsort  Memory: 42kB
        ->  Hash Join  (cost=412.00..184418.92 rows=26532 width=84) (actual time=8.114..2803.447 rows=24118 loops=1)
              Hash Cond: (pr.tenant_id = t.tenant_id)
              ->  Seq Scan on policy_rules pr  (cost=0.00..178204.00 rows=1298442 width=72)
                    (actual time=0.021..2551.882 rows=1284119 loops=1)
                    Filter: (created_at >= (now() - '30 days'::interval))
                    Rows Removed by Filter: 118331
                    Buffers: shared hit=1102 read=98180
              ->  Hash  (cost=387.00..387.00 rows=2000 width=20) (actual time=8.052..8.053 rows=1987 loops=1)
                    ->  Seq Scan on tenants t  (cost=0.00..387.00 rows=2000 width=20)
                          Filter: (tier = 'enterprise')
Planning Time: 0.412 ms
Execution Time: 2841.298 ms
```

**How to read this, top-down but analysed bottom-up.**

Plans are trees; the innermost/most-indented nodes run first. So read the *structure* top-down and the
*execution* bottom-up.

**The costs.** `cost=185432.11..185432.36` is (startup cost .. total cost) in arbitrary planner units.
They're only useful for comparing plans, not as time estimates. `actual time=2841.203..2841.219` is
(time to first row .. time to last row) in milliseconds, and — the trap — **it's per loop**. A node with
`loops=1000` and `actual time=..2.5` took 2,500ms total, not 2.5ms. Always multiply.

**The critical line.** `Seq Scan on policy_rules` with `rows=1284119` and `Rows Removed by Filter:
118331`. So Postgres read 1.4 million rows and threw away only 118 thousand — the filter is barely
selective. It took 2,551ms out of the total 2,841ms. **That's the whole problem: 90% of the time is one
sequential scan.**

**`Buffers: shared hit=1102 read=98180`** — 98,180 pages read from disk (or OS cache) versus 1,102 from
Postgres's buffer cache. At 8 KB per page that's about 800 MB. This is the single most useful line in
the plan and it's why you should always use `BUFFERS`: it tells you the actual I/O rather than a cost
estimate.

**The estimate check.** Hash Join estimated 26,532 rows, actual 24,118 — good. Seq Scan estimated
1,298,442, actual 1,284,119 — good. So statistics are accurate and the planner made a *reasonable*
choice given the available access paths. **This is not a stats problem; it's a missing-index problem.**
That distinction matters: `ANALYZE` won't help here.

**Why didn't it use an index on `created_at`?** Either there isn't one, or there is and the planner
judged it not worth it — 1.28M of 1.4M rows match, so an index scan would visit nearly every row and
also do random heap access. **When a filter matches most of the table, a sequential scan is correct.**
The planner is right; the query is wrong.

**The fix.** The real selectivity is in `tier = 'enterprise'` (1,987 of 12,000 tenants), not in the date
filter. Restructure so the selective condition drives:

```sql
CREATE INDEX idx_pr_tenant_created ON policy_rules (tenant_id, created_at DESC);
```

Now the planner can do a nested loop: for each of the 1,987 enterprise tenants, index-scan
`policy_rules` for their recent rules. And because the index is ordered by `created_at DESC` within each
tenant, the sort is cheaper too.

Even better for the `LIMIT 100`, avoid materialising all 24,118 matches:

```sql
SELECT t.name, pr.rule_id, pr.pattern, pr.created_at
FROM tenants t
JOIN LATERAL (
    SELECT rule_id, pattern, created_at
    FROM policy_rules pr
    WHERE pr.tenant_id = t.tenant_id AND pr.created_at >= now() - INTERVAL '30 days'
    ORDER BY created_at DESC
    LIMIT 100
) pr ON true
WHERE t.tier = 'enterprise'
ORDER BY pr.created_at DESC
LIMIT 100;
```

Each tenant contributes at most 100 rows (198,700 max instead of 1.28M), and each inner query is an
index range scan. Measured on this shape, ~2,841ms → ~14ms.

**The general lesson to articulate:** *find where the selectivity is, and make sure the plan can exploit
it.* The date filter looked selective ("last 30 days!") and wasn't, because policy rules are created
continuously and the table only holds ~14 months. The tier filter looked incidental and was the real
discriminator.

### 9.3 Plan node vocabulary you must know

**Scan nodes:**
- *Seq Scan* — read every page. Correct when returning a large fraction of the table.
- *Index Scan* — walk the index, fetch each matching heap tuple. Random I/O per row, so it loses to a
  seq scan above roughly 5–10% selectivity.
- *Index Only Scan* — answer entirely from the index, no heap access. Requires all needed columns in
  the index (use `INCLUDE`) **and** the pages to be marked all-visible in the visibility map — which
  means it depends on vacuum having run. An index-only scan showing `Heap Fetches: 1284119` isn't
  index-only in practice, and the fix is `VACUUM`.
- *Bitmap Heap Scan* — build a bitmap of matching pages from one or more indexes, then read pages in
  physical order. The middle ground: better than random I/O, still uses indexes, and it's how Postgres
  combines multiple indexes on one table.

**Join nodes:**
- *Nested Loop* — for each outer row, probe the inner. Great when the outer is small and the inner has
  an index. Catastrophic when the outer is large: watch for high `loops=`.
- *Hash Join* — build a hash table on the smaller side, probe with the larger. Best for large
  unsorted joins. Watch for `Batches: 8` in the output — more than 1 batch means it spilled to disk
  because `work_mem` was too small.
- *Merge Join* — both sides sorted, merge. Good when inputs are already sorted (e.g. from index scans).

**Red flags to call out when reading a plan:**
- Estimated versus actual off by more than ~10× → stats problem, or a correlation the planner can't see
  (fix with `CREATE STATISTICS` for correlated columns).
- `Rows Removed by Filter` large → reading data to throw it away; wrong index or wrong query shape.
- `Sort Method: external merge  Disk: 82000kB` → sort spilled; raise `work_mem` for this query.
- `loops=` in the thousands on a nested loop → the join order is probably wrong.
- `Heap Fetches` high on an index-only scan → vacuum.

### 9.4 Reading a ClickHouse plan

ClickHouse's plan output is different and the important information is elsewhere. The primary question
is always: **how many granules did we read, and could we have read fewer?**

```sql
EXPLAIN indexes = 1
SELECT threat_category, count() AS c
FROM dns_events
WHERE tenant_id = 4471
  AND ts >= '2026-09-01 00:00:00' AND ts < '2026-09-06 00:00:00'
  AND threat_category = 'phishing'
GROUP BY threat_category;
```

```
Expression ((Projection + Before ORDER BY))
  Aggregating
    Expression (Before GROUP BY)
      ReadFromMergeTree (skyline.dns_events)
      Indexes:
        MinMax
          Keys: ts
          Condition: and((ts in [1756684800, +Inf)), (ts in (-Inf, 1757116800)))
          Parts: 5/90
          Granules: 284213/2841200
        Partition
          Keys: toYYYYMMDD(ts)
          Parts: 5/5
          Granules: 284213/284213
        PrimaryKey
          Keys: tenant_id, toStartOfHour(ts)
          Condition: and((tenant_id in [4471, 4471]), (toStartOfHour(ts) in [1756684800, 1757116800]))
          Parts: 5/5
          Granules: 1842/284213
        Skip
          Name: idx_threat
          Description: set GRANULARITY 4
          Parts: 5/5
          Granules: 218/1842
```

**Read it as a funnel.** Each index stage narrows the granule count, and the last number is what you
actually read:

```
2,841,200 granules total in the table
→   284,213 after partition/minmax pruning by date   (10× reduction)
→     1,842 after primary key pruning by tenant       (154× reduction)
→       218 after the threat_category skip index      (8.4× reduction)
```

At `index_granularity = 8192`, 218 granules is about **1.8 million rows read** out of 23 billion. That's
the whole story of the query's cost, and it's more informative than any cost estimate.

**What to look for:**
- If `PrimaryKey` barely reduces granules, your `WHERE` doesn't align with the sort key — this is the
  single most common ClickHouse performance problem.
- If a `Skip` index doesn't reduce granules, it's dead weight: it costs write throughput and space for
  nothing. Drop it.
- If `Parts: 90/90`, no partition pruning happened — usually a missing or non-sargable time predicate.

**Then check what actually happened**, since `EXPLAIN` is an estimate:

```sql
SELECT query_duration_ms, read_rows, read_bytes,
       formatReadableSize(memory_usage) AS mem, result_rows
FROM system.query_log
WHERE query_id = '...' AND type = 'QueryFinish';
```

`read_rows` versus the table's total rows is the efficiency number that matters. And ClickHouse prints a
progress summary with every query — `Elapsed: 0.184 sec. Processed 1.81 million rows, 14.22 MB` — which
is the fastest feedback loop you'll get. Get in the habit of reading it every time.

**`PREWHERE` — the ClickHouse-specific optimisation worth knowing.** Normally ClickHouse reads all
columns needed by the query for the selected granules, then filters. `PREWHERE` reads *only the filter
columns* first, evaluates the condition, and then reads the remaining columns only for the rows that
survived:

```sql
SELECT query_name, client_ip, latency_us
FROM dns_events
PREWHERE threat_category = 'phishing'      -- read this small column first
WHERE tenant_id = 4471 AND ts >= today()
```

If `threat_category = 'phishing'` matches 0.1% of rows, you avoid reading 99.9% of the wide
`query_name` and `client_ip` columns. ClickHouse applies this automatically when
`optimize_move_to_prewhere = 1` (the default), but its heuristic isn't always right — explicit
`PREWHERE` on a highly selective, narrow column is a common 5–10× win on wide tables. **Knowing
`PREWHERE` exists and when it helps is a strong ClickHouse-specific signal.**

### 9.5 Query drills

Work these. Each teaches a pattern that shows up in interviews.

---

**Drill 1 — Deduplication with a tie-break.**

*Requirement:* From a `ReplacingMergeTree` that may hold duplicates, get exactly one row per `event_id`,
keeping the latest `ingested_at`, for tenant 4471 today.

*ClickHouse, three ways, in increasing sophistication:*

```sql
-- (a) FINAL: correct, simplest, merges on the fly
SELECT * FROM dns_events FINAL
WHERE tenant_id = 4471 AND ts >= today();

-- (b) argMax: explicit, often faster, but you must list every column
SELECT event_id,
       argMax(query_name, ingested_at)     AS query_name,
       argMax(policy_verdict, ingested_at) AS policy_verdict,
       max(ingested_at)                    AS ingested_at
FROM dns_events
WHERE tenant_id = 4471 AND ts >= today()
GROUP BY event_id;

-- (c) LIMIT BY: ClickHouse-specific, concise, keeps whole rows
SELECT * FROM dns_events
WHERE tenant_id = 4471 AND ts >= today()
ORDER BY event_id, ingested_at DESC
LIMIT 1 BY event_id;
```

*What each teaches:* `FINAL` is correct and its cost is often overestimated — with
`do_not_merge_across_partitions_select_final = 1` it only merges within partitions, which is much
cheaper. `argMax` avoids the merge but requires enumerating columns, so it's brittle as schemas evolve.
`LIMIT BY` is the elegant middle: it keeps whole rows and reads naturally, and it's a ClickHouse
extension worth knowing because it has no standard-SQL equivalent.

*Postgres equivalent* (for the control plane), where `DISTINCT ON` is the idiomatic answer:

```sql
SELECT DISTINCT ON (event_id) *
FROM events
WHERE tenant_id = 4471 AND ts >= current_date
ORDER BY event_id, ingested_at DESC;
```

`DISTINCT ON` is Postgres-specific and much cleaner than the portable window-function version
(`ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY ingested_at DESC) = 1`). Know both; use
`DISTINCT ON` on Postgres.

---

**Drill 2 — Sessionisation (gaps and islands).**

*Requirement:* Group each client's DNS queries into "sessions," where a gap of more than 30 minutes
starts a new session. Report session count and duration per client.

This is the classic gaps-and-islands problem and it appears in almost every SQL interview at this level.

```sql
WITH marked AS (
    SELECT
        client_ip,
        ts,
        -- 1 when this row starts a new session, else 0
        if(ts - lagInFrame(ts) OVER w > 1800, 1, 0) AS is_new_session
    FROM dns_events
    WHERE tenant_id = 4471 AND ts >= now() - INTERVAL 1 DAY
    WINDOW w AS (PARTITION BY client_ip ORDER BY ts
                 ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
),
sessions AS (
    SELECT
        client_ip,
        ts,
        -- running sum of the flag = a session id, unique within client
        sum(is_new_session) OVER (PARTITION BY client_ip ORDER BY ts
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS session_id
    FROM marked
)
SELECT client_ip, session_id,
       min(ts) AS started, max(ts) AS ended,
       dateDiff('second', min(ts), max(ts)) AS duration_s,
       count() AS queries
FROM sessions
GROUP BY client_ip, session_id;
```

**The trick, stated explicitly:** flag each row that begins a new group, then take a *running sum* of
that flag. The running sum is constant within a group and increments between groups, so it *is* the
group ID. Once you've seen this, every gaps-and-islands problem is the same three steps: lag, flag,
cumulative-sum.

Two ClickHouse notes: `lagInFrame` rather than `lag` (ClickHouse's window functions require an explicit
frame for lag/lead semantics), and a named `WINDOW` clause to avoid repeating the specification.

*Postgres:* identical structure with `LAG(ts) OVER (PARTITION BY client_ip ORDER BY ts)` and
`EXTRACT(EPOCH FROM ts - lag_ts) > 1800`.

---

**Drill 3 — As-of join for point-in-time features.**

*Requirement:* For each event, attach the client's risk score *as it was known at that moment*. (Part 5
explains why this matters.)

```sql
SELECT
    e.event_id, e.ts, e.query_name,
    r.risk_score, r.computed_at
FROM dns_events e
ASOF LEFT JOIN client_risk_history r
    ON e.client_ip = r.client_ip AND e.ts >= r.computed_at
WHERE e.tenant_id = 4471 AND e.ts >= today();
```

**The semantics:** `ASOF JOIN` requires exactly one inequality condition, which must be the last one,
and it matches the row with the **closest** value satisfying it — here, the most recent risk score at or
before the event. `ASOF LEFT JOIN` keeps events with no prior score (a new client) as NULL, which is
usually what you want; a plain `ASOF JOIN` drops them, which silently biases your training set toward
known clients.

*Portable version, for engines without `ASOF`:*

```sql
SELECT e.event_id, e.ts, r.risk_score
FROM dns_events e
LEFT JOIN LATERAL (
    SELECT risk_score, computed_at
    FROM client_risk_history r
    WHERE r.client_ip = e.client_ip AND r.computed_at <= e.ts
    ORDER BY r.computed_at DESC
    LIMIT 1
) r ON true
WHERE e.tenant_id = 4471;
```

Correct, and dramatically slower at scale because it's a correlated subquery per row. Knowing that
`ASOF JOIN` exists — and that it's specifically a time-series database feature — is worth pointing out.

---

**Drill 4 — Percentiles and approximate aggregates.**

*Requirement:* p50/p95/p99 DNS latency per tenant per hour, over 30 days.

```sql
SELECT
    tenant_id,
    toStartOfHour(ts) AS hour,
    quantile(0.50)(latency_us) AS p50,
    quantile(0.95)(latency_us) AS p95,
    quantile(0.99)(latency_us) AS p99,
    count() AS n
FROM dns_events
WHERE ts >= now() - INTERVAL 30 DAY
GROUP BY tenant_id, hour;
```

**The things to know and to volunteer:**

- `quantile()` uses reservoir sampling and is **approximate**. `quantileExact()` is exact but holds all
  values in memory — at 13 billion rows that's fatal. `quantileTDigest()` is a better accuracy/memory
  trade for high cardinality. **Choose deliberately and say which you chose and why**; silently using an
  approximate function where exactness is expected is the migration bug from Part 3.
- Computing three quantiles separately reads and processes the column three times. Use
  `quantiles(0.5, 0.95, 0.99)(latency_us)` — plural — which computes all three in one pass and returns
  an array. Roughly a 3× win.
- The same discipline applies to distinct counts: `uniq()` is HyperLogLog (~2% error, tiny memory),
  `uniqExact()` is exact (memory proportional to cardinality), `uniqCombined()` is a good middle. For
  billing, `uniqExact`. For a dashboard, `uniq`.
- For a *pre-aggregated* version of this in an `AggregatingMergeTree`, store
  `quantilesTDigestState(0.5, 0.95, 0.99)(latency_us)` — the sketch state merges across buckets
  correctly, which a stored p95 value does not. **You cannot average percentiles.** That's a genuinely
  common error worth naming: the average of hourly p95s is not the daily p95.

---

**Drill 5 — Funnel / sequence analysis.**

*Requirement:* How many clients queried a known-malicious domain, and then within 5 minutes queried a
command-and-control domain?

```sql
SELECT
    count() AS clients_matching_sequence
FROM (
    SELECT
        client_ip,
        windowFunnel(300)(               -- 300-second window
            ts,
            threat_category = 'malware-distribution',
            threat_category = 'c2'
        ) AS level
    FROM dns_events
    WHERE tenant_id = 4471 AND ts >= now() - INTERVAL 1 DAY
      AND threat_category IN ('malware-distribution', 'c2')
    GROUP BY client_ip
)
WHERE level = 2;
```

`windowFunnel(window)(timestamp, cond1, cond2, ...)` returns the maximum number of conditions matched
**in order** within the sliding window. It's purpose-built for this and it's dramatically faster than
the self-join you'd otherwise write. `sequenceMatch` and `sequenceCount` handle more complex patterns
with a regex-like syntax over event sequences.

Note the `AND threat_category IN (...)` predicate: filtering to only relevant events before the
`GROUP BY` cuts the input enormously. **Push filters as early as possible** — and here it's also
semantically safe, because rows of other categories can't affect the funnel.

---

**Drill 6 — Rewriting a slow query.**

*Given:*

```sql
-- 45 seconds
SELECT t.name, count(*) AS events
FROM dns_events e, tenants t
WHERE e.tenant_id = t.tenant_id
  AND toDate(e.ts) = '2026-09-05'
  AND t.tier = 'enterprise'
  AND e.query_name LIKE '%.evil.com'
GROUP BY t.name
ORDER BY events DESC;
```

*Find four problems.*

**Problem 1: `toDate(e.ts) = '2026-09-05'` is not sargable in the way you want.** Wrapping the column in
a function can prevent partition pruning and primary-key range use. Rewrite as a range so the index and
partition pruning apply:

```sql
WHERE e.ts >= '2026-09-05 00:00:00' AND e.ts < '2026-09-06 00:00:00'
```

(ClickHouse is smarter than most engines about monotonic functions of key columns, but the explicit
range is unambiguous, portable, and always correct. Make it a habit.)

**Problem 2: `LIKE '%.evil.com'` has a leading wildcard,** so no index or skip index can help — it's a
full scan of the `query_name` column with a substring match per row. Two fixes: if you're matching a
domain suffix, store a reversed domain column (`moc.live.` prefix-matches, and prefixes *are*
indexable), or use ClickHouse's `endsWith(query_name, '.evil.com')`, which is faster than `LIKE` and
lets a `tokenbf_v1` or `ngrambf_v1` skip index participate.

**Problem 3: joining to `tenants` to filter by tier scans all tenants' events, then discards
non-enterprise ones.** Filter first — get the enterprise tenant IDs, then restrict the scan so the
primary key prunes:

```sql
WHERE e.tenant_id IN (SELECT tenant_id FROM tenants WHERE tier = 'enterprise')
```

Better still, use a dictionary and avoid the join entirely:

```sql
WHERE dictGet('tenant_dict', 'tier', e.tenant_id) = 'enterprise'
```

— though note this evaluates per row, so the `IN` subquery is better here because it can prune by
primary key. **Prefer the form that lets the index prune over the form that's cheapest per row.**

**Problem 4: implicit comma join syntax.** Cosmetic, but it obscures join conditions and invites
accidental cross joins. Use explicit `JOIN`.

*Rewritten:*

```sql
SELECT dictGet('tenant_dict', 'name', tenant_id) AS name, count() AS events
FROM dns_events
PREWHERE endsWith(query_name_full, '.evil.com')
WHERE ts >= '2026-09-05 00:00:00' AND ts < '2026-09-06 00:00:00'
  AND tenant_id IN (SELECT tenant_id FROM tenants WHERE tier = 'enterprise')
GROUP BY tenant_id
ORDER BY events DESC;
```

Now: partition pruning by date, primary-key pruning by tenant set, `PREWHERE` reading only the domain
column for the selective filter before touching anything else, no join, and the tenant name resolved by
a hash lookup. Typical measured result on this shape: 45s → ~0.4s.

### 9.6 Optimisations that matter, and myths

**Real, in rough order of impact:**

1. **Make the query prune.** Align `WHERE` with the sort key / partition key / index. This dominates
   everything else, routinely by 100×.
2. **Read fewer columns.** Never `SELECT *` on a wide columnar table. Each column is separate I/O.
3. **Pre-aggregate.** The fastest scan is the one you don't do. A materialized view that turns a
   billion-row scan into a thousand-row scan beats any tuning.
4. **`PREWHERE`** on a selective, narrow column when the table is wide.
5. **Reduce join size** — filter before joining, put the smaller side on the right in ClickHouse, use
   dictionaries instead of joins for dimensions.
6. **Use the right aggregate.** `uniq` vs `uniqExact`, plural `quantiles`, `sumIf` instead of
   `sum(if(...))` (marginal, but idiomatic and clearer).
7. **In Postgres: covering indexes** with `INCLUDE` to enable index-only scans, and `CREATE STATISTICS`
   for correlated columns the planner otherwise mis-estimates.

**Myths worth being able to debunk, because interviewers sometimes test them:**

- *"`SELECT COUNT(1)` is faster than `SELECT COUNT(*)`."* Identical in Postgres and ClickHouse. The
  planner treats them the same. This one has been folklore for twenty years.
- *"Subqueries are always slower than joins."* Modern planners often transform between them. Frequently
  a subquery is faster because it filters earlier, as in Drill 6.
- *"Add an index to every column you filter on."* Indexes cost write throughput, space, and planner
  time, and an index on a low-selectivity column is never used. In ClickHouse, an unused skip index is
  pure overhead. Measure before adding, and check `pg_stat_user_indexes.idx_scan` / `EXPLAIN indexes=1`
  after.
- *"`UNION` vs `UNION ALL` doesn't matter."* It matters enormously: `UNION` deduplicates, which means a
  sort or hash over the entire result. Use `UNION ALL` unless you specifically need deduplication.
- *"`ORDER BY` is free if you have an index."* Only if the index order matches the requested order
  exactly, including direction and column sequence, and only if the query doesn't need a different
  access path.
- *"Denormalise everything for analytics."* Often right, sometimes not — denormalisation multiplies
  storage and makes updates a rewrite. For a dimension that changes and is small, a dictionary lookup
  beats denormalisation.

### 9.7 Tier 1–2 questions

**Q9.1: Write a query for "the 10 domains with the largest week-over-week increase in blocked queries,
for tenant 4471."**

```sql
WITH
    toStartOfDay(now() - INTERVAL 7 DAY)  AS this_week_start,
    toStartOfDay(now() - INTERVAL 14 DAY) AS last_week_start
SELECT
    query_name,
    countIf(ts >= this_week_start) AS this_week,
    countIf(ts >= last_week_start AND ts < this_week_start) AS last_week,
    this_week - last_week AS delta,
    if(last_week = 0, NULL, round((this_week - last_week) / last_week * 100, 1)) AS pct_change
FROM dns_events
WHERE tenant_id = 4471
  AND ts >= last_week_start
  AND policy_verdict = 'block'
GROUP BY query_name
HAVING last_week >= 100          -- suppress noise from tiny denominators
ORDER BY delta DESC
LIMIT 10;
```

*Points to make while writing it:* `countIf` computes both periods in a single pass instead of two
queries or a self-join. The `HAVING last_week >= 100` guard is the judgement call — without it, a domain
going from 1 to 30 blocks shows a 2,900% increase and dominates the list, which is noise. Ranking by
absolute `delta` rather than percentage is deliberate for the same reason; if the requirement really
wants percentage, keep the floor. And `if(last_week = 0, NULL, ...)` avoids a division-by-zero that
would otherwise be `inf` and sort to the top.

**Q9.2: This query does a full scan. Make it use the index.**

```sql
SELECT * FROM policy_rules WHERE lower(pattern) = 'evil.com';
```

*Model answer:* Wrapping the column in `lower()` makes the predicate non-sargable — the B-tree on
`pattern` is ordered by `pattern`, not by `lower(pattern)`, so it can't be range-searched. Three fixes:

```sql
-- (a) expression index, matching the query exactly
CREATE INDEX idx_pattern_lower ON policy_rules (lower(pattern));

-- (b) store it normalised, if case-insensitivity is a domain rule (it is, for DNS)
--     then the index on `pattern` works directly and every query is simpler
ALTER TABLE policy_rules ADD CONSTRAINT pattern_lowercase CHECK (pattern = lower(pattern));

-- (c) use a case-insensitive collation or citext for the column
```

I'd choose (b) for DNS specifically, because domain names are case-insensitive by definition, so
normalising at write time is semantically correct and makes every future query simpler. (a) is the right
answer when you can't change the write path. **The general rule: a function on the indexed column
disables the index unless there's a matching expression index.** Same applies to implicit casts —
`WHERE tenant_id = '4471'` on an integer column, or comparing a `timestamptz` to a `timestamp`.

**Q9.3: How do you find the slowest queries in production?**

*Postgres:*

```sql
SELECT
    substring(query, 1, 100) AS q,
    calls,
    round(total_exec_time::numeric, 0) AS total_ms,
    round(mean_exec_time::numeric, 2)  AS mean_ms,
    rows,
    round(100.0 * shared_blks_hit / nullif(shared_blks_hit + shared_blks_read, 0), 1) AS cache_hit_pct
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

*ClickHouse:*

```sql
SELECT
    normalized_query_hash,
    any(query) AS sample,
    count() AS calls,
    sum(query_duration_ms) AS total_ms,
    avg(query_duration_ms) AS mean_ms,
    sum(read_rows) AS total_rows_read,
    formatReadableSize(sum(read_bytes)) AS total_read
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 1 DAY
GROUP BY normalized_query_hash
ORDER BY total_ms DESC
LIMIT 20;
```

**Order by *total* time, not mean.** A query taking 50ms called 2 million times consumes far more
capacity than one taking 30 seconds called twice, and it's usually easier to fix. Ranking by mean sends
you after the dramatic query while the actual load comes from the boring one.

Group by the *normalised* form (`normalized_query_hash` in ClickHouse, which `pg_stat_statements` does
automatically) so that queries differing only in literals aggregate together. Otherwise a parameterised
query appears as ten thousand distinct entries and never surfaces.

Also check `read_rows` — a query reading far more rows than it returns is a pruning failure and usually
the easiest big win.

### 9.8 Tier 3 questions

**Q9.4: A query is fast for most tenants and slow for one. Why?**

*Model answer:* Almost always data skew, and there are five distinguishable causes:

1. **Volume skew.** Meridian has 18% of all events. A query that scans "this tenant's last 7 days" reads
   200× more for Meridian than for a median tenant. The query is fine; the data isn't uniform. Fix by
   routing large tenants to pre-aggregated tables, or by capping the scan window for them.
2. **Cardinality skew.** A `GROUP BY client_ip` for a tenant with 40,000 endpoints builds a
   40,000-entry hash table; for one with 4 million (a university or an ISP) it builds a 4-million-entry
   one and may exceed memory. Symptom: `MEMORY_LIMIT_EXCEEDED` for one tenant only. Fix:
   `max_bytes_before_external_group_by` so it spills instead of failing, and consider
   `uniq` sketches instead of exact grouping.
3. **Part/partition distribution.** If this tenant's data is spread across many more parts (they had a
   backfill, or late data), the query reads more parts and merges more streams. Check
   `system.parts` filtered to their partitions.
4. **Plan divergence (Postgres).** With prepared statements, a generic plan chosen from average
   statistics can be terrible for an outlier parameter value. Symptom: slow for one value, fast for
   others, and `plan_cache_mode = force_custom_plan` fixes it.
5. **Shard placement.** If tenant sharding put this tenant on a shard that also hosts two other whales,
   they're contending. Check per-shard load rather than assuming uniformity.

**Diagnostic order:** compare `read_rows` for the fast and slow cases first. If the slow one reads 200×
more, it's cause 1 (data volume) and the fix is architectural. If it reads a similar number of rows and
is still slow, it's cause 2, 3, or 5 — memory, part count, or contention — and `system.query_log`'s
`memory_usage` plus `system.parts` distinguishes them in two queries.

**Q9.5: Write a query to detect DNS tunnelling — clients making an unusual number of unique subdomain
queries to a single parent domain.**

```sql
SELECT
    tenant_id,
    client_ip,
    cutToFirstSignificantSubdomain(query_name_full) AS parent_domain,
    uniqExact(query_name_full)                      AS unique_subdomains,
    count()                                         AS total_queries,
    avg(length(query_name_full))                    AS avg_name_length,
    uniqExact(query_name_full) / count()            AS uniqueness_ratio,
    sum(length(query_name_full))                    AS bytes_exfiltrated_upper_bound
FROM dns_events
WHERE ts >= now() - INTERVAL 1 HOUR
  AND query_type IN ('A', 'AAAA', 'TXT', 'NULL')
GROUP BY tenant_id, client_ip, parent_domain
HAVING unique_subdomains >= 50
   AND uniqueness_ratio > 0.9        -- nearly every query is a distinct name
   AND avg_name_length > 40          -- encoded payloads are long
ORDER BY unique_subdomains DESC
LIMIT 100;
```

**The reasoning behind each condition, which is what's being tested** — this question is as much about
domain thinking as SQL:

- DNS tunnelling encodes data into subdomain labels, so each query is a *unique* name under a common
  parent. Normal browsing reuses names heavily (a client hits `cdn.example.com` hundreds of times), so
  `uniqueness_ratio` near 1.0 is the strongest single signal.
- `cutToFirstSignificantSubdomain` gives the eTLD+1, so `a1b2.tunnel.evil.com` and `c3d4.tunnel.evil.com`
  group under `evil.com`.
- Encoded payloads are long — base32 of a data chunk — hence the length filter. Legitimate names average
  ~20 characters.
- `TXT` and `NULL` record types are over-represented in tunnelling because they carry more data per
  response.
- The `HAVING` thresholds are a starting point, not truth. **Say that**: they need tuning against
  labelled data, and the right way to set them is to measure the false-positive rate against known-good
  traffic. Some CDNs and antivirus products legitimately generate high-cardinality subdomains, so an
  allow-list of known parent domains is necessary in practice.

**And a performance note to volunteer:** this groups by `(tenant, client_ip, parent)` over an hour of
all tenants' data — potentially 540 million rows. In production I'd run it per tenant from the rollup
tables, or maintain a dedicated `AggregatingMergeTree` keyed on `(tenant_id, client_ip, parent_domain,
hour)` with `uniqState(query_name_full)`, so the detection query reads thousands of rows instead of
hundreds of millions. **Detection queries that run continuously should read pre-aggregated state, not
raw events** — that's the same principle as Part 5's dashboard design.

**Q9.6: Explain `GROUP BY` memory behaviour in a distributed ClickHouse query.**

*Model answer:* Two-phase aggregation, and understanding where memory is consumed is what lets you
predict failures.

**Phase 1 — partial aggregation on each shard.** Each shard scans its local data and builds a hash table
keyed by the `GROUP BY` columns, holding partial aggregate states. Memory here is proportional to *that
shard's* distinct key count, not the global count.

**Phase 2 — merge on the initiator.** Each shard sends its partial states to the initiating node, which
merges them into the final result. **Memory on the initiator is proportional to the *global* distinct key
count**, and this is where large `GROUP BY` queries die.

Concretely: `GROUP BY client_ip` across all tenants over 30 days. If there are 40 million distinct client
IPs globally and each hash entry costs ~100 bytes with the aggregate state, that's 4 GB on the
initiator — plus the network transfer of 8 shards' worth of partial states.

**The mitigations, and knowing them is the point of the question:**

- `distributed_aggregation_memory_efficient = 1` — merges bucket-by-bucket in a streaming fashion rather
  than accumulating everything first. Substantially lower peak memory, slightly slower. Should usually
  be on for large aggregations.
- `group_by_two_level_threshold` / `group_by_two_level_threshold_bytes` — switches to a two-level hash
  table when the key count gets large, which enables parallel merging and the bucket-wise streaming
  above.
- `max_bytes_before_external_group_by` — spill to disk instead of failing. Set it to roughly half of
  `max_memory_usage`, because the merge phase needs headroom.
- **Shard by the `GROUP BY` key when you can.** If the query groups by `tenant_id` and the cluster is
  sharded by `tenant_id`, each shard's partial result is already final for its own tenants — no merge
  needed. `optimize_distributed_group_by_sharding_key = 1` lets ClickHouse exploit that. **This is the
  best fix by a wide margin**, and it's why Part 8's sharding choice matters for queries and not just for
  isolation.
- Reduce cardinality: group by a bucketed or truncated key, or use a sketch (`uniq`) rather than exact
  grouping when approximation is acceptable.

---

## Part 10 — Core primitives: building for a roadmap you can't see yet

### 10.1 What the interviewer is actually testing

This bullet — "skill at building complex systems and identifying core primitives and how to apply them
to meet changing business needs and future roadmap requirements" — is the most abstract one, and it's
the one most specific to staff level. It's testing whether you can build a *platform* rather than a
*solution*.

The rubric:

1. Can you identify a primitive? A primitive is a small, composable capability that many features are
   built *from*. Most engineers build features; staff engineers notice that five features share a
   shape and build the shape.
2. Do you know the failure mode on both sides — under-abstraction (ten near-identical pipelines) and
   over-abstraction (a configurable framework nobody can use)?
3. Can you decide *when* to extract a primitive? "Rule of three" is a start; the better answer is about
   which axis of variation you've actually seen.
4. Do you build for *optionality* rather than for predicted requirements? Nobody knows the roadmap. The
   goal isn't to predict it; it's to make changes cheap in whichever direction it goes.

### 10.2 The mental model: find the shape that repeats

Here's how the failure looks concretely, because it's much easier to recognise than to define.

Skyline's platform team gets five requests over eighteen months:

1. "Ingest DNS query events." → Pipeline A: collector, Kafka topic, processor, ClickHouse table, S3
   writer, retention job, dashboard.
2. "Ingest DHCP lease events." → someone copies Pipeline A, changes the schema, and ships Pipeline B.
3. "Ingest firewall logs." → Pipeline C, copied from B, which has drifted from A.
4. "Ingest cloud-provider VPC flow logs." → Pipeline D.
5. "Ingest endpoint telemetry." → Pipeline E.

Eighteen months later there are five pipelines with 80% identical code and five subtly different
behaviours. A bug in retry logic must be fixed five times, and it's fixed in three. Adding a new SLI
means five changes. Adding a new dataset means a two-week project. **The team's velocity is now inversely
proportional to the number of datasets it supports** — which is the definition of not having a platform.

Now the over-correction, which is equally common and harder to unwind. Someone notices the duplication
and builds "the unified ingestion framework": a YAML-driven engine with pluggable parsers, configurable
enrichment DAGs, a rules engine for routing, and an expression language for transformations. Two years
later it has 40 configuration options, three people understand it, the YAML for a simple dataset is 200
lines, and adding a genuinely new behaviour requires modifying the framework, which risks all five
datasets at once. **You've traded five simple things you can reason about for one complex thing you
can't.**

The path between them is *primitives*, and the distinction is precise:

> A **framework** takes control: you fill in the blanks and it runs the show. A **primitive** is a
> capability you call: you keep control and compose.

Frameworks are inverted-control and rigid at the edges. Primitives are libraries and stay flexible.
The test: can a team use *three* of your primitives and write the rest themselves? If yes, they're
primitives. If using any of it means adopting all of it, it's a framework.

### 10.3 Skyline's primitives

What are the actual reusable capabilities under those five pipelines? Not "ingestion" — too big. Break
it down until each piece has a single responsibility and a stable interface:

**1. Typed event intake.** *Given* an authenticated source and a registered schema, *produce* validated
records to a durable log, with per-tenant quota and DLQ routing. Varies by: schema, auth method, quota.
Doesn't vary at all otherwise.

**2. Durable log.** Kafka topic with a standard configuration derived from a volume class. Varies by:
partition count, retention, whether the tenant is isolated.

**3. Stream transform.** *Given* a source topic, a transformation function, and a sink, *apply* the
function with at-least-once semantics, batching, backpressure, DLQ, and metrics. **The transformation
function is ordinary code, not configuration.** That's the critical design decision — it's what keeps
this a primitive rather than a framework. You get retry, batching, offset management, and observability
for free; you write your enrichment logic in Go.

**4. Columnar sink.** *Given* records and a table definition, *write* to ClickHouse with correct
batching, idempotency tokens, and part-count-aware backpressure.

**5. Lake sink.** *Given* records and a table definition, *write* Parquet to S3 and commit to Iceberg
with correct file sizing and partition layout.

**6. Dataset registration.** *Given* a dataset spec (schema, owner, classification, retention class,
SLOs), *provision* the topic, tables, catalogue entry, quality checks, dashboards, and access grants.

**7. Lifecycle reconciler.** *Given* retention policies, *converge* physical storage to match, with
audit. (Part 3.)

**8. SLI computation.** *Given* a dataset with declared SLOs, *emit* freshness, completeness, and
correctness measurements. (Part 4.)

**9. Query gateway.** *Given* an authenticated principal and a query, *enforce* tenant scoping, resource
limits, and audit logging, then execute.

**10. Backfill/replay.** *Given* a source, a time range, and a sink, *reprocess* idempotently with
progress tracking and rate limiting. (Parts 2 and 3.)

**Now the test that proves they're the right primitives:** adding the DHCP lease dataset becomes —
write a schema, write a transformation function, write a dataset spec. Everything else is composition.
That's a two-day task instead of a two-week project, and it doesn't add a sixth thing to maintain.

**And the second test, which matters more:** adding a *new capability* — say, per-record encryption for
a regulated dataset — is a change to one primitive (the sinks) that all datasets inherit. In the
five-copies world it's five changes; in the framework world it's a risky change to a shared engine with
40 options.

### 10.4 When to extract a primitive

"Rule of three" — wait until you've built it three times — is the standard advice, and it's roughly
right for the wrong reason. The real reason to wait isn't the count; it's that **you cannot see the axis
of variation until you've seen variation.**

After one implementation you know nothing about what varies. After two you have a hypothesis, and it's
usually wrong — the second case tends to differ from the first along an axis that turns out to be
incidental. After three you can see which differences are essential (schema, transformation logic) and
which are accidental (someone named a field differently).

Extract along the axes that actually varied. **Do not add configuration for variation you haven't
seen.** Every unexercised configuration option is a maintenance cost and a bug surface with no
demonstrated value, and it's how the 40-option framework happens: each option was added for a
hypothetical.

**The counter-rule:** extract early when the cost of divergence is high even at n=2. Security and
correctness primitives qualify. You don't wait for three implementations of tenant isolation to see the
pattern — the second copy is already a liability, because the failure mode is a data breach rather than
some duplicated code. **Deduplicate anything where inconsistency is dangerous, immediately; deduplicate
anything where inconsistency is merely annoying, at three.**

### 10.5 Designing for optionality

You can't predict the roadmap, so don't try. Optimise instead for the *cost of changing direction*. Six
concrete techniques, each of which has paid off in this document already:

**1. Keep raw data.** Bronze holds everything that ever arrived, including fields nothing uses. That's
what makes "we now need a field we've been discarding" a reprocessing job rather than a six-week
resolver rollout. Cheapest optionality you can buy.

**2. Make everything replayable.** If every derived store is a pure function of an immutable log, then
every mistake is recoverable and every schema is provisional. This is the single highest-leverage
property in the whole architecture — it's what made Q2.9's reprocessing a Tuesday.

**3. Put policy in data, not code.** Retention, quotas, shard assignment, feature flags, tenant tiers.
Anything in a config table changes without a deploy, which means it can change during an incident, and
it can be different per tenant without a code branch.

**4. Use open formats at the base layer.** Iceberg and Parquet mean a future engine can read your data.
If Skyline decides in two years that DuckDB or a new engine serves some workload better, that's a new
reader, not a migration.

**5. Version interfaces from day one.** `v1` in the path, expand/migrate/contract as the discipline.
Retrofitting versioning onto an unversioned interface with 40 external consumers is Part 3's whole
problem.

**6. Prefer boring, reversible decisions; spend your risk budget deliberately.** Some choices are
one-way doors — your table format, your durable log, your tenancy model, your primary cloud. Those
deserve real analysis. Most choices are two-way doors — which library, which dashboard tool, which
naming convention. Decide those quickly and move on. **The skill is telling them apart**, and stating
which category a decision is in is a strong interview signal by itself.

### 10.6 Tier 1–2 questions

**Q10.1: What's a primitive, and give me one from a system you've built.**

*Model answer:* A primitive is a small capability with a stable interface that many features are
composed from — as opposed to a framework, which takes control and asks you to fill in blanks.

The one I'd point to at Skyline is the **stream transform**: given a source topic, a transformation
function, and a sink, apply it with at-least-once semantics, batching, backpressure, DLQ routing, offset
management, and standard metrics. The transformation itself is ordinary Go, not configuration.

The reason that's a good primitive is the interface boundary. Everything on the platform side is
genuinely universal — every pipeline needs correct batching and idempotency. Everything domain-specific
is code, so there's no expression language to learn, no configuration to debug, and no limit on what a
transformation can do. When we added `insert_deduplication_token` to fix a double-counting bug, five
pipelines got the fix from one change.

The test that it was the right boundary: a team could use just the sink primitive with their own
consumer, and that works fine. Primitives compose; frameworks don't.

**Q10.2: How do you avoid over-engineering while still building a platform?**

*Model answer:* Two rules and a measurement.

First: **abstract along axes you've observed varying, never along axes you predict.** Every
configuration option added for a hypothetical requirement is a permanent cost with no demonstrated
value, and it's how you get a framework with 40 options where three are used. If I haven't seen it vary
three times, it's a constant.

Second: **prefer composition over configuration.** A library someone calls stays flexible in ways a
framework someone configures doesn't. If a team needs behaviour we didn't anticipate, with primitives
they write it; with a framework they file a ticket against us and wait.

The exception: correctness and security primitives get extracted at n=2, because a second inconsistent
copy of tenant isolation is a breach waiting to happen, not just duplication.

The measurement that keeps it honest: **time-to-first-query for a new dataset.** If it's going up, the
abstraction is becoming a tax. If a team can onboard a dataset in a day without talking to us, it's
working. I'd track it like an SLO.

**Q10.3: The business wants to add a completely new data type — network flow records at 10× your
current volume. What changes?**

*Model answer:* Walk the primitives and ask which hold and which break at 10×, and be honest that the
answer is "most hold, two don't."

*Holds without change:* dataset registration, the transform primitive's semantics, lifecycle
reconciliation, SLI computation, the query gateway, governance. These are volume-independent by design.

*Needs scaling, not redesign:* Kafka partitions and brokers, processor instances, ClickHouse shards, S3
throughput. Linear scaling with more of the same. Worth checking that nothing has a hidden per-dataset
constant — for instance, if the SLI prober does a fixed poll per tenant, 10× volume is fine but 10×
tenants wouldn't be.

*Genuinely needs rethinking, and this is where the answer earns its keep:*

**Cost.** 10× volume at Skyline's current unit economics is roughly 10× the storage and compute bill.
That's a business conversation before it's an engineering one: at 130 billion records/day, the S3 and
ClickHouse cost alone would be several hundred thousand a month. The right first move is to ask whether
full-fidelity retention is needed, because flow records are usually valuable in aggregate and rarely
queried individually. **Sampling and aggressive pre-aggregation at ingest may cut this by 90% with no
product impact** — sample 1:100 for the raw tier, keep exact aggregates. That's a design question the
volume forces, and skipping it is the most expensive mistake available here.

**Schema shape.** Flow records have different cardinality characteristics — 5-tuples instead of domain
names — so the sort key and compression codec choices need fresh analysis rather than copying the DNS
table. Getting the sort key wrong here is a migration later.

**Isolation.** At 10× the volume of everything else combined, this dataset will dominate shared
infrastructure. It probably needs its own Kafka cluster and its own ClickHouse cluster, or it becomes
everyone's noisy neighbour. That's a topology decision, and the primitives support it because the
control plane already maps datasets to locations.

**The thing to say:** "The primitives hold. What changes is that the volume forces two decisions we
haven't had to make — sampling policy and physical isolation — and both of those are exactly the kind
of decision I'd want in the control plane so they're per-dataset settings rather than a fork of the
platform."

### 10.7 Tier 3 question

**Q10.4: You inherit a platform with 12 bespoke pipelines. You have two quarters. What do you do?**

*Model answer:* The instinct is to build the unified platform and migrate all twelve. That fails
predictably: six months of no visible value, a v2 that doesn't handle three edge cases from the
originals, and pipelines 7 through 12 never migrate.

**Instead, strangle from the shared edges inward, in four steps.**

*Step 1 — Measure before deciding (2 weeks).* For each of the twelve: volume, consumers, incident count
over the last year, on-call load, lines of code, last-modified date, and owner. You'll find a
distribution, not a uniform set: probably three pipelines generate 80% of the operational pain, four are
stable and untouched for a year, and two have no consumers at all. **The two with no consumers get
deleted, which is the cheapest win available.** The four stable ones might never need migrating — a
pipeline that hasn't had an incident or a change in 18 months costs nothing to leave alone, and
migrating it is pure risk.

*Step 2 — Extract the least-controversial primitive first (4 weeks).* Not the whole platform — one
capability every pipeline needs and none of them does well. Usually that's **observability**: a shared
metrics and SLI library that all twelve adopt. It's low-risk (additive, no behaviour change), it's
immediately valuable (you can finally see all twelve on one dashboard), and it builds credibility for
the bigger changes. It also produces the data for step 3.

*Step 3 — Extract the sinks (6 weeks).* The ClickHouse and S3 writers are where the correctness bugs
live — batching, idempotency, part management. Twelve implementations means twelve chances to get
`insert_deduplication_token` wrong. Extract, migrate the three painful pipelines first, and let the
improvement be visible: "pipeline 4's duplicate rate went from 0.3% to zero."

*Step 4 — Migrate opportunistically, not comprehensively.* From here, migrate a pipeline **when you're
already touching it** for another reason — a new requirement, an incident, a schema change. The marginal
cost of migrating while you're in there is small; the cost of a dedicated migration project is large and
the value is speculative.

**Explicitly plan to leave some unmigrated, and say so.** If four pipelines are stable and cheap, the
right number to migrate is zero. Full consistency is an aesthetic goal, not a business one. A staff
engineer who says "I'd migrate seven of twelve and deliberately leave four, here's the criterion" is
demonstrating better judgement than one who promises all twelve.

**What I'd deliver at the end of two quarters:** twelve pipelines visible on one SLI dashboard, two
deleted, three migrated to shared sinks with measurable reliability improvements, a documented primitive
set, and a written criterion for when the remaining ones get migrated. That's a defensible outcome and
it leaves the team faster than it found them.

---

## Part 11 — Cloud and multi-cloud operations

### 11.1 What the interviewer is actually testing

1. Do you know the managed services well enough to choose between them *and* to know when to run
   something yourself?
2. Do you understand cloud **cost structure** — not prices, but where the money actually goes, which is
   almost never where people expect?
3. Can you design for failure domains — AZ, region, account, provider?
4. Do you know what multi-cloud actually costs, and can you distinguish the versions of it that are
   sane from the version that's a fantasy?
5. Have you operated at scale — do you know about S3 request rates, cross-AZ charges, NAT gateway bills,
   and EBS throughput limits?

### 11.2 The mental model: failure domains and blast radius

Cloud architecture is fundamentally about choosing failure domains. Nested, from smallest:

**Instance** — fails constantly. Design assumes it. Stateless services autoscale; stateful ones
replicate.

**Availability Zone** — fails occasionally (a few times a year across a large fleet). Independent power,
cooling, and network within a region, with 1–2ms latency between AZs. **The default unit of redundancy.**

**Region** — fails rarely but spectacularly, and a region-wide control-plane failure can prevent you
from even launching replacement capacity. Multi-region is expensive and complex.

**Account** — a hard security and blast-radius boundary. A compromised credential or a runaway Terraform
apply is contained within an account. **Under-used by most teams**, and it's the cheapest isolation
mechanism available: accounts are free.

**Provider** — fails almost never as a whole, but *services* within a provider have correlated failures,
and commercial factors (pricing changes, acquisition) are real risks.

**Skyline's choices, with the reasoning:**

*Multi-AZ within a region: yes, everywhere.* Kafka brokers across 3 AZs with `min.insync.replicas=2`;
ClickHouse replicas in different AZs; Postgres with a synchronous standby in another AZ; S3 is
multi-AZ by default. Cost: cross-AZ data transfer, which is not free and is discussed below.

*Multi-region: for storage, not for compute.* S3 Cross-Region Replication for the archive gives
durability against regional loss at $0.02/GB transfer plus duplicate storage. But a hot standby
ClickHouse cluster in another region would double the largest cost line to protect against an event
that happens roughly once every few years. **The honest trade: accept several hours of RTO for a
regional failure, with a documented recovery procedure that rebuilds ClickHouse from S3.** That's a
business decision to put in front of leadership explicitly rather than to make silently.

*Multi-account: yes, and aggressively.* Separate accounts for production data plane, production control
plane, the WORM archive, non-production, security tooling, and GovCloud (which is a separate partition
entirely). The WORM archive in its own account is the important one: it means a full compromise of the
production account cannot delete the immutable archive.

*Multi-cloud: see §11.5, where the answer is "yes, but not the way people mean it."*

### 11.3 Cost: where the money actually is

Engineers estimate cloud cost by adding up instance prices and are wrong by a large factor, because the
bill is dominated by things that don't appear in an architecture diagram. Skyline's actual structure:

**Storage.**
- S3 Standard: $0.023/GB-month. 260 TB = **$5,980/month**.
- With lifecycle to Standard-IA at 30 days ($0.0125/GB-mo) and Glacier Instant Retrieval at 90 days
  ($0.004/GB-mo): roughly 20 TB Standard + 45 TB IA + 195 TB GIR
  = `460 + 563 + 780` = **$1,803/month**. A **70% saving** for a lifecycle policy that takes an hour to
  write. This is consistently the single largest easy win in a data platform's bill.
- Watch the minimum durations: Standard-IA charges a 30-day minimum, Glacier Flexible 90 days, Deep
  Archive 180. Transitioning objects that get deleted early costs *more* than leaving them. And each
  transition costs about $0.05 per 1,000 objects — at 172,000 objects that's $8.60, negligible, but at
  50 million small objects it's $2,500 and worth avoiding by writing to the target class directly.
- EBS gp3: $0.08/GB-month plus provisioned IOPS/throughput above the baseline. The ClickHouse cluster's
  16 × 4 TB = 64 TB = **$5,120/month**, which is more than the entire S3 archive. **Local storage is
  ~3.5× the cost of S3 per GB**, which is the arithmetic that justifies aggressive tiering.

**Data transfer — the line item that surprises people.**
- Cross-AZ: **$0.01/GB in each direction**, so $0.02/GB round trip. Kafka is where this bites, and it's
  worth deriving because the number is invisible on every dashboard. At Skyline's *average* 60 MB/sec of
  produce traffic, uncompressed:
  ```
  replication:  60 MB/s × 2 additional copies, all cross-AZ        = 120 MB/s
  consumers:    2 consumer groups × 60 MB/s = 120 MB/s, of which
                ~2/3 lands in a different AZ from the leader       =  80 MB/s
                                                          total    = 200 MB/s cross-AZ

  200 MB/s × 2.6e6 sec/month = 520,000 GB/month
  520,000 GB × $0.01 = $5,200/month in cross-AZ transfer for Kafka alone
  ```
  **That's comparable to the Kafka instance bill**, and it appears nowhere in an architecture diagram.
  Two mitigations, and note they attack different halves. **Producer-side compression**
  (`compression.type=zstd`, roughly 5:1 on this payload) reduces *every* copy proportionally, including
  replication — that's the bigger lever, taking the whole figure to about $1,040. **Rack-aware consumer
  fetching** (`client.rack` on the consumer plus `replica.selector.class=RackAwareReplicaSelector` on
  the broker) lets consumers read from a same-AZ follower, eliminating the 80 MB/s consumer half —
  but it cannot touch replication traffic, which is the unavoidable price of multi-AZ durability.
  Combined, roughly **$4,200/month recovered.**
- Egress to internet: $0.09/GB for the first 10 TB. Customer exports go via S3 with requester-pays or
  into the customer's own bucket where possible, to avoid this.
- **NAT Gateway: $0.045/GB processed** plus $0.045/hour. A pipeline pulling from S3 through a NAT
  gateway pays this on every byte. **Use VPC Gateway Endpoints for S3 and DynamoDB — they're free** and
  they eliminate this entirely. This is the most common six-figure mistake in data platforms, and it's a
  five-minute fix.

**Requests.**
- S3 PUT: $0.005 per 1,000. Writing 256 MB Parquet files: 650 GB/day ÷ 256 MB ≈ 2,540 files/day ≈
  $0.38/month. Negligible. Writing 1 MB files instead: 650,000/day ≈ $97/month, plus vastly worse query
  performance from listing and opening overhead. **Small files are the enemy in a lake**, and the cost
  shows up in query time before it shows up on the bill.
- S3 GET: $0.0004 per 1,000. A Spark job scanning 172,000 objects costs $0.07 in requests. Not the
  problem; the compute is.

**Compute.** ClickHouse's 16 nodes at roughly $1,000/month each on-demand for `m6i.8xlarge` ≈
$16,000/month, or about $6,400 with a 3-year reserved commitment. **Reserved/savings plans on steady-
state data infrastructure is a 40–60% saving and it's the most under-taken action in cost management**,
because it requires a commitment nobody wants to make. For a data platform whose baseline load is
predictable, it's nearly free money.

**The summary to internalise:** for a data platform, cost is roughly *storage tiering* + *cross-AZ
transfer* + *committed-use discounts*, and most teams optimise instance types instead, which is the
smallest of the three.

### 11.4 Managed versus self-managed

A decision framework rather than a list:

**Use managed when** the service is undifferentiated (nobody buys your product because you run Kafka
well), the operational burden is high relative to your team size, the managed version's limitations
don't bind you, and the cost delta is less than the loaded cost of the engineering time.

**Self-manage when** you're at a scale where the managed premium is large in absolute terms, you need
control the managed version doesn't offer, or the service is genuinely core to your differentiation.

Skyline's actual calls:

- **Postgres → RDS/Aurora.** 40 GB, 800 tx/sec. Self-managing this to save a few hundred dollars a
  month would be indefensible. Managed, multi-AZ, automated backups, done.
- **Kafka → MSK, initially; revisit at scale.** At 240 MB/sec peak the MSK premium is meaningful, but
  broker operations (rebalancing, upgrades, disk management) are a real ongoing cost. I'd start managed
  and self-manage only when the delta clearly exceeds an engineer's time. The trap is teams that
  self-manage Kafka from day one for cost reasons and then spend a quarter on a broker incident.
- **ClickHouse → self-managed, or ClickHouse Cloud.** This is the genuinely close call. ClickHouse Cloud
  offers separated storage/compute via SharedMergeTree, which is architecturally better for a spiky
  workload and removes the resharding problem from Part 8. The cost comparison depends heavily on
  utilisation: for Skyline's steady 24/7 load, self-managed on reserved instances is cheaper; for a
  workload with a 10× daily peak, the elasticity wins. **I'd model both against actual utilisation
  rather than list prices**, and I'd weight ClickHouse's operational complexity heavily — it's the
  system most likely to need deep expertise at 3am.
- **Spark → EMR or Databricks, not self-managed.** Running Spark on raw Kubernetes is a real cost with
  little upside for a team this size.
- **Object storage → always managed.** There is no version of "we should run our own object storage" at
  this scale that ends well.

**The framing:** "I'd default to managed and require a written justification to self-manage, with the
justification being a number — the annual saving versus the loaded cost of the engineering attention.
For Skyline, ClickHouse is the only service where that number plausibly favours self-managing, and it's
close enough that I'd re-evaluate annually."

### 11.5 Multi-cloud, honestly

The question "do you have multi-cloud experience" is often really "do you understand what multi-cloud
costs?" There are four distinct things people mean, and they have wildly different economics.

**Version 1 — Portable, running in one cloud at a time.** Everything on Kubernetes, open-source
components, cloud-specific services avoided. You *could* move. Cost: you give up managed services and
run Postgres, Kafka, and object storage yourself, or accept the lowest common denominator. **Usually a
bad trade** — you pay a large ongoing operational tax for optionality you'll likely never exercise.

**Version 2 — Split by workload.** Data platform on AWS, ML training on GCP for TPUs, or a SaaS
dependency elsewhere. Each workload uses its native cloud's strengths, with a defined interface between
them. **This is usually sane** and it's what most real "multi-cloud" companies actually do.

**Version 3 — Active-active across clouds.** The same service runs in both, serving traffic. Extremely
expensive: duplicate everything, cross-cloud data transfer at internet egress rates, two operational
models, and the hardest part — keeping state consistent across providers. **Justified almost only by
regulatory requirements** (some financial and government contracts mandate provider diversity).

**Version 4 — Customer-driven.** Your customers are on GCP and want your product to run there, or their
data can't leave their cloud. **This is a product requirement, not an architecture preference**, and
it's the most common genuine driver.

**Skyline's situation is version 4 with a bit of 2.** Some enterprise customers run their resolvers in
GCP and want telemetry to stay in GCP. So:

*What runs in both:* the collector tier and the durable log. Collectors are stateless Go behind a load
balancer — trivially portable. Kafka runs in both (which, note, is a reason to prefer Kafka over Kinesis
per Q2.2). Object storage differs (S3 versus GCS) but is abstracted behind a storage interface, and both
support the same Parquet/Iceberg layout.

*What stays in one place:* the control plane (single source of truth, replicated read-only where needed)
and the primary ClickHouse cluster. Running two analytical clusters with cross-cloud replication is
where costs explode.

*The interface between them:* GCP-resident data is processed and stored in GCP, with only aggregated,
non-personal results flowing to the primary region for global dashboards. **That flow direction is
deliberate** — small aggregates cross the boundary, never raw events, because cross-cloud egress at
$0.08–0.12/GB would be ruinous on 5.2 TB/day:

```
5.2 TB/day × 1000 × $0.09 = $468/day = ~$14,000/month, just to move data between clouds
```

That number is the whole argument. Say it out loud: **"cross-cloud data movement at our volume costs
more than the compute, so the architecture has to keep data where it lands."**

*What we deliberately don't do:* abstract over cloud primitives. We use S3 APIs on AWS and GCS APIs on
GCP behind a thin internal interface, rather than adopting a lowest-common-denominator abstraction
layer. Abstractions over cloud storage leak — consistency models, multipart semantics, lifecycle rules,
and IAM all differ — and pretending they don't produces subtle bugs. **Two implementations of a small
interface beats one implementation of a leaky abstraction.**

### 11.6 Tier 1–2 questions

**Q11.1: How would you reduce this platform's cloud bill by 30%?**

*Model answer:* Measure first — I'd want cost allocated by service, by dataset, and by tenant before
optimising, because the intuition about where money goes is usually wrong. Assuming Skyline's structure,
in descending order of impact:

**1. S3 lifecycle tiering (~$4,200/month, 70% of the S3 bill).** 260 TB sits in Standard when most of it
is read a few times a year. Transitions to Standard-IA at 30 days and Glacier Instant Retrieval at 90
days cut it to about $1,800. One hour of work. Check the minimum-duration charges and object counts
first.

**2. Reserved instances / savings plans (~$9,600/month, 40–60% of compute).** The ClickHouse cluster and
Kafka brokers run 24/7 at predictable load. A 3-year commitment on the steady-state baseline, with
on-demand for burst, is the largest single saving and it requires only a decision.

**3. Cross-AZ transfer for Kafka (~$4,200/month).** Verify producer compression is on — that's the
bigger lever, because it shrinks replication traffic too — and enable rack-aware consumer fetching so
consumers read from a same-AZ replica. This is invisible on most dashboards, which is why it persists.

**4. VPC Gateway Endpoints for S3.** If any traffic to S3 is going through a NAT gateway at $0.045/GB,
this is both a large saving and free to fix. Always check.

**5. ClickHouse tiering to S3-backed volumes.** Moving the 7–30 day range from EBS to an S3-backed disk
converts ~$3,400/month of EBS into ~$250/month of S3, at the cost of slower queries over that range.
Whether that's acceptable depends on the query mix — measure what fraction of queries touch 7–30 days.

**6. Right-size after measuring, not before.** Check actual CPU and memory utilisation. Data
infrastructure is frequently over-provisioned because it was sized for peak plus a safety margin that
was never revisited.

**The discipline that keeps it saved:** cost per TB ingested and cost per tenant as tracked metrics with
an owner, reviewed monthly. Otherwise it regresses within two quarters — someone provisions something
during an incident and nobody removes it.

**Q11.2: Design for an AZ failure. What breaks?**

*Model answer:* Walk each component and name the actual behaviour, including the ones that fail badly.

*Collectors:* stateless behind an NLB across 3 AZs. One AZ down means capacity drops by a third;
autoscaling replaces it in the remaining AZs. **No data loss** if the resolvers retry, which they do.
Recovery: minutes.

*Kafka:* brokers across 3 AZs, RF=3, `min.insync.replicas=2`. Losing one AZ loses one replica per
partition; 2 remain, so writes continue. **This is why `min.insync.replicas=2` and not 3** — at 3, an AZ
failure stops all writes, which is a strictly worse outcome than accepting reduced redundancy
temporarily. Partition leaders on the failed AZ's brokers re-elect within seconds.

*Stream processors:* stateless consumers. Those in the failed AZ die, the group rebalances, remaining
consumers pick up their partitions. Brief lag spike during rebalance; no data loss because offsets are
committed after writes.

*ClickHouse:* replicas in different AZs. Losing one AZ loses one replica per shard; the other serves
reads and writes. **Capacity halves for affected shards**, so query latency rises and this is where the
user-visible impact is. When the AZ returns, the replica catches up from the replication log.

*Postgres:* Multi-AZ RDS fails over to the standby. **60–120 seconds of unavailability**, and every
application needs connection retry logic that survives it — this is the thing that actually breaks in
practice, because a service that opens a connection at startup and never reconnects will stay broken
after the failover completes.

*S3:* multi-AZ by design. No impact.

**What actually breaks, and this is the honest part:** capacity, not availability. Everything survives,
but at reduced headroom during a period when load hasn't decreased. If the cluster is normally at 70%
utilisation, losing a third of capacity puts it above 100% and you start shedding load. **So designing
for AZ failure means running at a utilisation that leaves room for it — for 3 AZs, below ~66% steady
state.** That's a real cost and it should be an explicit capacity-planning input, not an accident.

**What I'd test:** run a game day. Terminate an AZ's instances deliberately, in production, on a
schedule. The failures you find are always in the places nobody modelled — a hardcoded endpoint, a
connection pool that doesn't reconnect, a cross-AZ dependency someone added. An untested failover plan
is a hypothesis.

**Q11.3: A customer requires their data to stay in GCP. What do you build?**

*Model answer:* Establish scope first — the same discipline as the EU case in Part 6. Does "stay in GCP"
mean storage, or storage plus processing? Does it include backups? Is metadata in scope? The answer
changes the cost by a factor of several.

Assuming storage and processing in GCP:

*Deploy the portable layer in GCP:* collectors (stateless Go on GKE), Kafka (self-managed or Confluent
Cloud on GCP), stream processors, and a ClickHouse cluster sized for that customer's volume — probably
much smaller than the main one, since it's one customer.

*Storage:* GCS with the same Parquet/Iceberg layout. The storage interface has two implementations
behind it; the object layout and table format are identical, which means tooling and queries port
without change.

*Control plane:* stays primary (AWS), with a read replica or cached projection in GCP so the data plane
can operate during a cross-cloud partition. Critically, **the GCP data plane must keep ingesting even if
it can't reach the AWS control plane** — serve the last-known config with a staleness bound, per Part
2's collector design.

*The flow between them:* only aggregated, non-personal metrics cross to the primary region for the
unified dashboard. Raw events never cross, because at $0.09/GB egress the volume makes it economically
impossible before it's even a compliance question.

*What I'd tell the customer, precisely:* their raw telemetry is stored and processed entirely in GCP,
in the region they choose; aggregate operational metrics (event counts, error rates — no personal data)
flow to our primary region for platform monitoring; and the management console runs in AWS but only
displays data fetched from GCP on demand. Then let them confirm that's acceptable, because sometimes it
isn't and it's much better to find out now.

*The cost I'd flag internally:* this roughly doubles the operational surface for one customer. It should
be priced accordingly, and there should be a threshold — below N customers or below $X ARR, it's not
worth it. **A staff engineer should surface that economics question rather than just building what
sales sold.**

### 11.7 Tier 3 question

**Q11.4: Your primary region has been degraded for 3 hours. Ingestion is failing. What do you do?**

*Model answer:* Triage in a fixed order: protect the data, restore the customer-visible path, then
recover.

**First — establish what's actually degraded.** "Region degraded" is usually one service, not
everything. Is it EC2 launches (can't scale), EBS (storage I/O), the network, or a control-plane API? The
answer determines what's still usable. Check the provider's status page *and* your own signals, because
status pages lag.

**Second — protect the data, and this is the most important step.** Where is data accumulating and how
long until something overflows?
- If Kafka is up but consumers are down, data is safe for ~5 hours of retention headroom. Say the
  number; it converts panic into a deadline.
- If Kafka is down, resolvers are spooling locally. At 2 GB spools and average rates, most tenants have
  hours; whale tenants have less. **Compute the time-to-overflow for the largest tenants specifically**
  — that's the real clock.
- If collectors are up but Kafka is down, collectors must return 429 rather than buffering in heap
  (Part 2's backpressure design). Verify that's what's happening, because a collector silently dropping
  data is the worst outcome available.

**Third — decide on failover, with a clear-eyed view of what we actually have.** Skyline's design
accepts hours of RTO for a regional failure. So the options are:
- *Wait it out*, if the provider's ETA is credible and spool headroom exceeds it. Often the right call
  — failover has its own risks, and a half-executed failover during a recovering region is worse than
  waiting.
- *Partial failover:* stand up collectors and Kafka in the secondary region and redirect resolver
  traffic via DNS. This preserves ingestion (the thing you can't recover later) even if queries stay
  degraded. **Ingestion and query availability are separable, and prioritising ingestion is correct**
  because lost data is unrecoverable while a slow dashboard is temporary.
- *Full failover:* also rebuild ClickHouse in the secondary from S3. Hours of work; only worth starting
  if the outage looks like it will exceed the time to complete it.

**Fourth — communicate.** Status page updated within 15 minutes and every 30 minutes after, with what's
affected and what isn't. "Ingestion is buffered and no data will be lost; dashboards are unavailable" is
a much better message than silence, and it's usually true.

**Fifth — recover deliberately.** When the region returns, the backlog drains. **Do not let it drain at
full speed** — 3 hours of backlog at 4× replay rate is a thundering herd that will re-break ClickHouse
via part explosion. Rate-limit the catch-up, monitor part counts, and accept a longer recovery for a
stable one.

**Afterwards, the postmortem question that matters:** was our RTO decision right? We accepted hours of
recovery to avoid a hot standby's cost. If this happens twice a year rather than once every three years,
the economics have changed and the decision should be revisited with real data. **Bringing that back as
a quantified reconsideration, rather than an emotional "we need multi-region," is the staff move.**

---

## Part 12 — Integrating legacy systems with modern architectures

### 12.1 What the interviewer is actually testing

Part 3 covered *migrating off* a legacy system. This part covers the harder case: legacy systems that
**aren't going away** and must interoperate indefinitely. That's most of the real world.

The rubric:

1. Can you design an **interface** that decouples two systems with different models, rather than
   letting the legacy model leak into the new one?
2. Do you know the integration patterns — anti-corruption layer, CDC, strangler fig, event-carried
   state transfer, outbox — and when each applies?
3. Do you handle the fact that legacy systems are often *slow, fragile, and rate-limited*, and that
   you don't get to change them?
4. Can you reason about consistency across a boundary where you control only one side?

### 12.2 The mental model: the anti-corruption layer

The core risk in integrating with a legacy system is not technical failure. It's **conceptual
contamination**: the legacy system's data model, naming, and assumptions spread into the new system,
and five years later the new system is as hard to change as the old one.

Skyline's concrete version. The legacy provisioning system, "Mercury," is a 12-year-old Java app with an
Oracle database. It's the system of record for customers, contracts, and entitlements, and it is not
being replaced this decade. Its model:

- A "customer" is a row in `CUST_MASTER` with a 9-character `CUST_CD` like `MERFIN001`.
- Entitlements are a bitfield in `ENT_FLAGS`, where bit 7 means "DNS logging enabled" and bit 12 means
  "extended retention," except bit 12 also means "legacy pricing" for accounts created before 2019.
- Contract dates are stored as `VARCHAR2(8)` in `YYYYMMDD`, and `'99991231'` means "no end date."
- Customer hierarchy is a self-referencing `PARENT_CUST_CD` with no constraint, so cycles exist.

The tempting integration: have the new platform read `CUST_MASTER` directly and use `CUST_CD` as the
tenant identifier.

**Why that's a trap.** Within a year the new platform has `cust_cd` columns, code that special-cases
`'99991231'`, and a bitfield parser with the bit-12 exception. When Mercury is eventually replaced, or
when a customer needs a tenant that doesn't exist in Mercury (a trial, an internal test tenant), the new
platform can't represent it. **You've inherited a 12-year-old model's constraints into a system built
this year.**

**The anti-corruption layer (ACL)** is a translation boundary. It's the only component that knows about
Mercury. It exposes the *new* system's model and translates:

```
Mercury (Oracle) ──► ACL ──► Skyline control plane
  CUST_CD 'MERFIN001'         tenant_id 4471, external_id <uuid>
  ENT_FLAGS bit 7             feature 'dns_logging' = enabled
  ENT_FLAGS bit 12 + created  retention_class = 'r2555' (with the pre-2019 pricing rule
    date < 2019                applied here and nowhere else)
  END_DT '99991231'           contract_end = NULL
  PARENT_CUST_CD (cyclic)     tenant_hierarchy rows, cycles detected and rejected
```

Three properties make it work, and all three are worth naming:

**1. Translation is one-directional and total.** The ACL knows Mercury; nothing downstream does. Search
the new platform's codebase for "CUST_CD" and you should find hits only in the ACL. That's a testable
invariant — I'd add a CI check for it, because it's the property that decays first.

**2. The new model is designed for the new system's needs, not derived from the old one.** `tenant_id`
is a surrogate key that exists whether or not Mercury knows about it. That means Skyline can create a
trial tenant, an internal test tenant, or a tenant during a Mercury outage. **The mapping table has a
nullable `cust_cd`**, and that nullability is the whole point.

**3. Weirdness is quarantined and documented.** The bit-12 exception lives in one function with a
comment explaining it and a test asserting it. It doesn't propagate.

```sql
CREATE TABLE tenant_external_mapping (
    tenant_id       INT PRIMARY KEY REFERENCES tenants(tenant_id),
    source_system   TEXT NOT NULL,           -- 'mercury'
    external_key    TEXT,                    -- CUST_CD; NULL for Skyline-native tenants
    last_synced_at  TIMESTAMPTZ,
    sync_status     TEXT NOT NULL,           -- 'ok' | 'stale' | 'conflict' | 'orphan'
    raw_snapshot    JSONB,                   -- last raw record, for debugging translation bugs
    UNIQUE (source_system, external_key)
);
```

`raw_snapshot` is a small detail that pays for itself constantly: when a translation bug appears, you
need the input that produced the wrong output, and going back to Mercury gives you *today's* value, not
the one that caused the bug.

### 12.3 The integration patterns, and when each applies

**Pattern 1 — Change Data Capture (CDC).** Read the legacy database's transaction log and stream
changes. Debezium reads Oracle redo logs (via LogMiner or XStream), MySQL binlogs, or Postgres WAL.

*Use when:* you need near-real-time sync, the legacy system can't be modified, and you have DBA
cooperation for log access.

*Watch for:* the legacy DBA will resist — log mining has real overhead on Oracle. Schema changes on the
source can break the connector, so you need alerting on connector health. And **initial snapshot plus
streaming has a consistency seam**: Debezium's snapshot-then-stream handles it, but you must understand
that the snapshot may take hours on a large table and that changes during it are handled by the log
position. Also: for Postgres sources, a replication slot whose consumer stalls will grow WAL until the
disk fills, taking down the source database. Monitor slot lag as a first-class alert.

**Pattern 2 — Outbox.** The legacy system writes events to an `OUTBOX` table in the same transaction as
its business change; a poller reads and publishes them.

*Use when:* you can make a small change to the legacy system, and you need the events to be
transactionally consistent with the data change.

*Why it's better than CDC when available:* the legacy team controls the event schema, so it's a
*designed* contract rather than a leak of internal table structure. CDC couples you to their physical
schema — when they rename a column, you break. The outbox is an interface they own and version.

*Watch for:* the poller must handle ordering and at-least-once delivery; consumers must be idempotent.

**Pattern 3 — Scheduled extract.** A nightly job queries the legacy system and produces a file.

*Use when:* real-time isn't needed, the legacy system is fragile, or the team won't allow anything else.

*Underrated.* It's simple, it's easy to reason about, it puts bounded load on the source at a
predictable time, and failures are obvious. **For reference data that changes slowly — a contract table
updated a few times a day — a nightly extract is often the right answer, and reaching for CDC is
over-engineering.** Say that; interviewers notice when a candidate chooses the simple thing for the
right reason.

**Pattern 4 — API façade.** Call the legacy system's API synchronously.

*Use when:* you need current data and the legacy system has a usable API.

*Watch for:* you've now coupled your availability to theirs. If Mercury is down, is Skyline down? The
answer must be no, which means caching with a documented staleness bound, circuit breaking, and a
defined degraded behaviour. **Never make a high-volume path synchronously dependent on a legacy
system** — that's the Part 2 rule that collectors don't depend on Postgres, applied here.

**Pattern 5 — Event-carried state transfer.** The legacy system publishes complete state snapshots
rather than deltas ("here is customer MERFIN001's full record as of now"). Consumers keep their own
copy.

*Use when:* consumers need the data locally for performance, and eventual consistency is acceptable.

*Advantage over deltas:* self-healing. A missed event is corrected by the next snapshot for that entity,
whereas a missed delta is wrong forever. For low-change-rate reference data, this is a significantly
more robust choice, and it's worth knowing because most people default to deltas.

**Skyline's actual choices:**
- *Mercury → Skyline (customer/entitlement data):* outbox where Mercury's team will build it (they
  agreed for entitlements, which change rarely and matter a lot), nightly extract as the reconciliation
  backstop for everything. Not CDC — the Oracle DBA vetoed log mining, which is a normal and legitimate
  outcome you should be able to work with rather than argue against.
- *Skyline → Mercury (usage for billing):* a daily aggregate file in the format Mercury's billing job
  already consumes. **Match their existing input format rather than asking them to change** — that's
  the difference between a two-week integration and a two-quarter one.
- *Legacy Reports UI → new platform:* an API façade that speaks the old query interface and translates
  to the new one, which is Part 3's strangler seam.

### 12.4 Handling the fact that legacy systems are fragile

You don't get to change them, so design around their properties.

**They're slow.** Mercury's customer API does about 20 requests/sec before it degrades. So: cache
aggressively (customer data changes a few times a day — a 15-minute TTL is fine), batch where the API
allows it, and **never call it on a per-event path.** Skyline resolves 150,000 events/sec; even one
Mercury call per 10,000 events would exceed its capacity.

**They're fragile.** A retry storm can take Mercury down, which becomes your incident *and* your
reputation problem with their team. So: circuit breaker, bounded concurrency (a semaphore capping
in-flight requests to well under their limit), exponential backoff with jitter, and a **hard rate limit
on your side** that's below their stated capacity. Be the well-behaved client; it buys you goodwill
you'll need later.

**They have no SLA.** Mercury has a nightly maintenance window and occasional multi-hour outages. So the
integration must degrade gracefully: serve cached data with a staleness indicator, queue outbound writes
for later delivery, and **alert on staleness rather than on errors.** An integration that's been serving
6-hour-old data without erroring is the failure mode that goes unnoticed — the same "absence" problem as
Part 4's alerting.

**Their data is dirty.** Cycles in the hierarchy, duplicate customer codes, contract end dates before
start dates, entitlements referencing products that no longer exist. **The ACL must validate and
quarantine**, not propagate. A record that fails validation goes to a `sync_conflicts` table with the
reason, `sync_status` is set to `conflict`, and a human is alerted. **Do not guess** — a "helpful"
inference in the ACL (assuming a null end date means active) becomes an invisible business rule that
someone will contradict later.

**Their schema changes without notice.** Someone adds a column, changes a length, repurposes a flag. So:
the ACL validates the source schema on every run and fails loudly on unexpected changes rather than
silently mis-parsing. And you build a relationship with their team so you hear about changes — which is
an organisational control, not a technical one, and it's usually the most effective.

### 12.5 Tier 1–2 questions

**Q12.1: How do you integrate with a legacy system you can't modify?**

*Model answer:* Two decisions: how data crosses the boundary, and how you stop their model from
infecting yours.

For the crossing, pick the least invasive mechanism that meets the freshness requirement. If minutes are
fine, a scheduled extract is the most robust and the easiest to operate. If seconds are needed and the
DBA allows it, CDC off the transaction log. If they'll accept a small change, an outbox table is better
than CDC because it's a contract they own rather than a leak of their physical schema. A synchronous API
call is the last resort, because it couples your availability to theirs.

For the model, an anti-corruption layer: one component that knows the legacy model and translates to
yours. Nothing downstream sees their identifiers, their encodings, or their exceptions. The testable
invariant is that grepping your codebase for their column names finds hits only in the ACL — I'd enforce
that in CI, because it's the property that erodes silently.

Then design for their fragility: cache with a documented staleness bound, rate-limit yourself below
their capacity, circuit-break, quarantine records that fail validation rather than guessing, and alert
on staleness rather than on errors — because the dangerous failure is the one where data stops updating
without anything erroring.

**Q12.2: The legacy system is the source of truth for customer data, but it's often wrong. What do
you do?**

*Model answer:* Separate "source of truth" (an authority question) from "correct" (a data question).
They're not the same, and conflating them is how you end up either propagating errors or silently
diverging.

Concretely: keep Mercury as the authority, but add a validation and reconciliation layer that makes
errors *visible and attributable* rather than silently absorbed.

1. **Validate on ingest.** Every record from Mercury goes through assertions: contract end after start,
   `CUST_CD` matches the expected format, entitlement bits map to known products, hierarchy has no
   cycles. Failures are quarantined with `sync_status = 'conflict'` and reported to Mercury's owning
   team — not fixed by us.
2. **Never silently correct.** If we "fix" a bad record in translation, we've created a second version
   of the truth and nobody can reconcile the two. The moment we start guessing, every downstream
   discrepancy becomes unexplainable.
3. **Have an explicit override path.** Sometimes you must operate despite bad upstream data — a customer
   is being billed wrong and it's blocking them. So provide a `tenant_overrides` table where an
   authorised human sets a value with a reason, an expiry, and a link to the ticket filed against
   Mercury. **The override is visible, temporary, and traceable**, which is the opposite of a silent
   correction.
4. **Publish a data quality report back to them.** "Last month, 340 records failed validation; here's the
   breakdown." This shifts the conversation from us complaining to them having a metric, and in my
   experience it's what actually drives fixes.
5. **Reconcile continuously.** A daily comparison of our view against Mercury's, with drift as a metric.
   Divergence should be zero except for known overrides.

**Q12.3: You need to send data *back* to a legacy system. How?**

*Model answer:* Harder than reading, because now you can break their system and you'll own that incident.

Principles:

**Match their existing input mechanism.** If Mercury's billing job reads a fixed-width file from an SFTP
drop every night, produce that file. Do not propose a REST API. The integration cost is entirely on your
side and the risk to them is near zero, which is what makes it get approved. **Meeting a legacy system
where it is, is a feature, not a compromise.**

**Make writes idempotent and identifiable.** Every record carries a batch ID and a natural key so a
re-delivery can be detected. Legacy systems rarely handle duplicates well, and you *will* re-deliver.

**Validate before sending, against their rules.** If Mercury rejects a malformed record by aborting the
whole batch — which 12-year-old batch jobs frequently do — one bad record blocks everything. So validate
against their constraints on our side and quarantine failures before they leave.

**Make it resumable and observable.** Track per-batch state (`pending / sent / acknowledged / failed`)
in our database. When someone asks "did yesterday's usage file get processed?", that's a query.

**Get an acknowledgement, and alert on its absence.** Fire-and-forget to a legacy system is how you
discover in month three that nothing has been processed since month one. If they can't produce an ack,
derive one — read back a status table, check for a response file, or reconcile against their reported
totals. **The absence of an ack must alert**, per Part 4's absence-detection principle.

**Rate-limit and schedule considerately.** Send during their low-usage window. Don't compete with their
own batch jobs. Their operations team's goodwill is a real resource.

### 12.6 Tier 3 question

**Q12.4: Design bi-directional sync between the new platform and a legacy system, where both can modify
the same entity.**

*Model answer:* First, try very hard not to. Bi-directional sync with concurrent modification is
genuinely one of the hardest problems in distributed systems, and most requirements for it dissolve
under examination.

**The decomposition that usually resolves it: split ownership by field, not by entity.** Look at what
actually gets modified where:

- Mercury owns: customer name, contract dates, entitlements, billing address. Modified by sales and
  finance in Mercury's UI.
- Skyline owns: retention overrides, alert configuration, saved queries, API keys. Modified by customers
  in Skyline's UI.
- Genuinely contested: almost nothing, once you list them.

So the design is **single-writer-per-field**: each field has exactly one owning system, sync is
one-directional per field, and the other side's copy is read-only and clearly marked as such in the UI
("Contract dates are managed in Mercury"). No conflicts are possible because there's no concurrent
write. **This resolves 90% of "we need bi-directional sync" requirements**, and proposing it is the
strongest move available.

**If there's genuinely a contested field**, then you need conflict resolution and you must pick a
strategy consciously:

*Last-write-wins by timestamp.* Simple, and it silently loses data. Requires synchronised clocks, which
across a 12-year-old Oracle box and a modern platform you do not have. Acceptable only for fields where
losing an update is harmless.

*Version vectors / explicit versioning.* Each side tracks a version; a write carrying a stale version is
rejected and the client must re-read and retry. Correct, and it requires the legacy system to
participate — which it usually can't. This is often the blocker that forces you back to single-writer.

*Conflict detection with human resolution.* Detect divergence, quarantine both versions, surface them to
an operator. Correct and honest. Appropriate when conflicts are rare and consequential — which, for
customer contract data, they are. **This is what I'd build for the genuinely contested case**: a
`sync_conflicts` table, an alert, and a small resolution UI. If conflicts turn out to be frequent, that's
evidence the ownership model is wrong and should be revisited.

*CRDTs.* Mathematically elegant, and inapplicable here — they require both sides to implement the merge
semantics, and Mercury will not be growing CRDT support.

**The mechanics regardless of strategy:**
- **Sync must be idempotent.** Every sync operation carries an identifier and applying it twice is
  a no-op.
- **Loop prevention.** A change synced from Mercury to Skyline must not be detected as a Skyline change
  and synced back. Tag each write with its origin and skip records whose origin is the destination.
  Without this you get an infinite ping-pong, and it's the classic bi-directional sync bug.
- **Reconciliation is mandatory, not optional.** A scheduled full comparison, because incremental sync
  *will* drift — a missed event, a failed apply, a schema change. Report drift as a metric with a target
  of zero.
- **Bound the divergence window.** Define and monitor "how stale can either side be," and alert when
  exceeded.

**The closing point:** "I'd spend the first week trying to eliminate the requirement by mapping field
ownership, because single-writer-per-field is dramatically simpler and in my experience it covers almost
everything. If a genuinely contested field survives that analysis, I'd use conflict detection with human
resolution rather than automatic merge, because for contract and entitlement data, a wrong automatic
merge is worse than a delayed correct one."

### 12.7 Case study: the API that can't change

**Scenario:** "Forty enterprise customers integrate with a REST API you built in 2016. It returns a
nested JSON structure with fields that no longer make sense, it's not versioned, and one endpoint takes
40 seconds because it does a full scan. You need to move the backend from Argus to the new platform.
Customers cannot change their code this year. Go."

**Step 1 — Accept the constraint completely, and say so.** The API contract is frozen. Every byte of
every response must remain identical. This is not negotiable and pretending otherwise wastes the first
month. The work is entirely behind the interface.

**Step 2 — Characterise the contract precisely, from traffic rather than from documentation.** The 2016
docs are wrong; the code is the spec, and even the code doesn't tell you which behaviours customers
actually depend on. So: capture 30 days of production traffic — requests and responses. Now you know
which endpoints are used, by whom, with which parameters, and — critically — **what the responses
actually look like**, including the accidental behaviours (field ordering, null versus omitted, the
timestamp format with no timezone, the field that's a string in one case and a number in another).
Customers depend on accidents; you can only preserve what you've observed.

Build a **golden corpus**: 10,000 recorded request/response pairs covering every endpoint and every
customer's usage patterns. This is the acceptance test for everything that follows.

**Step 3 — Build a translation layer, not a rewrite.** A new service that speaks the 2016 contract on
the front and the new platform's interfaces on the back:

```
customer → [2016 API façade] → translation → new query gateway → ClickHouse / Iceberg
                    │
                    └─ response shaping: rebuild the exact nested JSON,
                       including the fields that no longer mean anything
```

Some fields no longer have a source — say `cache_hit_ratio`, which Argus computed from a component that
no longer exists. Options: compute an equivalent from new data if one exists; return a constant that
matches what it always effectively was; or return null if it was frequently null anyway. **Decide per
field, from the captured traffic, and document each decision.** The traffic capture tells you whether
anyone could plausibly be depending on it — a field that's been `0.0` for three years is safe to hardcode.

**Step 4 — Validate with shadow traffic, byte-for-byte.** Run both backends for every request. Compare
responses byte-for-byte (after normalising things that are legitimately non-deterministic, like a
request ID). Any difference is a bug until proven otherwise. Target: 100% match on the golden corpus,
and > 99.99% on live shadow traffic, with every mismatch explained.

Expect to find that the old system is inconsistent — the same request returns slightly different results
depending on load, because of Elasticsearch's approximate aggregations. That's the interesting case, and
the answer is: **match the distribution, not the exact value**, and document that this endpoint was
always approximate. Some customers may have built on the assumption it was exact; that's worth flagging
to them proactively.

**Step 5 — Fix the 40-second endpoint, quietly.** This is the easy win. That endpoint does a full scan
because Argus had no pre-aggregation. On the new platform it reads a rollup and returns in 200ms. **The
contract says nothing about latency**, so making it 200× faster is a pure improvement requiring no
customer change. Lead with this when you announce the migration — customers experience the change as a
performance improvement rather than a risk.

One caution worth mentioning: a response that was 40 seconds and becomes 200ms can break a client with a
race condition that the slow response was accidentally hiding. It's rare, but roll out gradually per
customer and watch their error rates, rather than flipping everyone at once.

**Step 6 — Cut over per customer, with instant rollback.** Route by customer ID behind a control-plane
flag. Start with the two customers with the lowest volume and the best relationships. Monitor their
error rates and latency. Expand weekly. Any anomaly, flip back — sub-minute, no data loss.

**Step 7 — Now start the conversation about v2**, from a position of credibility. With the old API
running unchanged on the new backend, you've bought unlimited time. Design a clean v2, publish it, and
offer customers a reason to migrate — better data, more fields, higher rate limits, real freshness —
rather than a deadline. Run v1 indefinitely; it's a thin translation layer over the same backend, so its
marginal cost is now small. **The mistake would be to force v2 as part of this migration**; that
converts a zero-risk backend change into a forty-way negotiation.

**The framing to close with:** "The migration and the API redesign are two separate projects that people
constantly conflate. Coupling them means the backend migration is blocked on forty customers' roadmaps.
Decoupling them means the backend migration ships this quarter with zero customer involvement, and the
API redesign happens later on its own merits — with the old contract preserved as a thin adapter
forever, which costs us almost nothing."

---

## Part 13 — Long-form case studies

These are full 45-minute interview simulations. Each spans several competency areas at once, which is
how real interviews work. For each: the prompt, what the interviewer is watching for at each stage, and
a worked answer with the timing you should aim for.

---

### Case study A — "Design our data platform" (the whiteboard round)

**Prompt:** *"We're a DNS security company. Customers' resolvers send us query logs. We need to store
them, let customers search and build dashboards, feed an ML threat-scoring model, and produce compliance
reports. Design it."*

**What the interviewer is grading, by phase:**

| Minutes | Phase | What they're watching |
| --- | --- | --- |
| 0–5 | Requirements | Do you ask for numbers before designing? |
| 5–10 | High-level | Can you draw a clean architecture and justify each box? |
| 10–25 | Deep dive | Do you have real depth in at least two areas they probe? |
| 25–35 | Failure modes | Do you know what breaks and how you'd know? |
| 35–45 | Trade-offs | Can you name what you gave up and when you'd choose differently? |

---

**Minutes 0–5: Establish the numbers.** Ask, and if they defer, state assumptions and write them down.

"Six things: peak and average events per second, average event size, required query freshness, required
retention, number of tenants, and the largest tenant's share of volume. Also: is there an existing system
I'm replacing, and are there regulatory constraints?"

Given Skyline's answers (150K/sec average, 600K peak, 400 bytes, 60-second freshness, 30 days hot / 400
days archive, 12,000 tenants, top tenant 18%, replacing a legacy stack, FedRAMP and GDPR in scope),
derive on the board:

```
13 billion events/day · 5.2 TB/day raw · ~480 GB/day compressed columnar · 260 TB archive over 400 days
```

**Then state the shaping constraints out loud before drawing anything**, because this is what shows you
understood the numbers rather than just collected them:

- 5.2 TB/day rules out row storage for events.
- 60-second freshness rules out batch-only; this is a streaming pipeline.
- A 4× peak-to-average ratio means the ingest path must absorb bursts, not just handle the average.
- One tenant at 18% means uniform hashing anywhere will produce hot spots.
- FedRAMP means a separate deployment, not a feature flag.

---

**Minutes 5–10: The architecture.**

```
  resolvers ──► collectors ──► Kafka ──► stream processors ──┬──► ClickHouse (hot 30d)
  (local spool)  (authN, quota,  (7d,        (parse, enrich,  │    ↳ rollup MVs → dashboards
                  validate,       RF=3)       dedupe)         │
                  batch)                                      └──► S3 + Iceberg (400d)
                                                                   ↳ Trino → ad-hoc + compliance
                       ┌─────────────────────────┐                 ↳ Spark → ML training
                       │ CONTROL PLANE: Postgres │                 ↳ Redis ← features → inference
                       │ tenants, policies,      │
                       │ schema, retention,      │───► governs routing, quotas, retention,
                       │ grants, catalog         │     shard assignment, access
                       └─────────────────────────┘
```

Say the organising principle: **"Postgres is the control plane; ClickHouse, S3, and Redis are the data
plane, and each is a materialisation of the Kafka log. That means every derived store is rebuildable,
which is what makes every later decision reversible."**

Then justify each box in one sentence — collectors for authentication and batching so nothing downstream
does per-tenant policy; Kafka for durability and replay; ClickHouse for sub-second tenant-scoped
aggregation; S3/Iceberg for cheap long retention, ML reproducibility, and as the rebuild source; Redis
for 5ms single-entity feature lookup.

---

**Minutes 10–25: Deep dive.** They'll pick two. Be ready for all of these:

*"Show me the ClickHouse schema."* → Part 2 §2.6. Lead with the sort key and explain it does three jobs
(index, compression, dedup identity). Volunteer the `ReplacingMergeTree` `ORDER BY` trap.

*"How do you avoid double counting?"* → Part 2 §2.4. At-least-once plus idempotent effects;
`insert_deduplication_token` from the Kafka offset range; `uniqExact(event_id)` for billing.

*"How do you isolate tenants?"* → Part 6 §6.3. Five layers, and the arithmetic showing why one isn't
enough.

*"How do you know it's working?"* → Part 4. Freshness, completeness, correctness, availability; measured
per tenant; burn-rate alerting.

*"What about the one tenant at 18%?"* → Salted Kafka keys, explicit shard assignment rather than modulo,
a dedicated consumer group, dedicated read replicas, and a documented promotion path to a dedicated
cluster.

---

**Minutes 25–35: Failure modes.** Volunteer these before being asked; it's a strong signal.

"The four things that will actually break: **part explosion** in ClickHouse from bad batching — I'd alert
on parts-per-partition at 100, well below the 150 throttle. **Consumer lag from partition skew** — the
whale tenant on one partition — which is why keys are salted. **Silent data quality failure**, like a
column going null after a firmware change, which conventional monitoring can't see and which
per-column baseline checks can. And **disk pressure on the hot tier**, where I'd alert on projected
days-until-full rather than percentage, because adding shards is a project and needs lead time."

---

**Minutes 35–45: Trade-offs.** The closing move.

"Three things I gave up deliberately.

**I gave up exactly-once semantics** and built at-least-once with idempotent effects instead, because
ClickHouse has no cross-system transaction. The cost is that billing needs a distinct-count rather than
a row-count. I'd accept that.

**I gave up cross-region hot failover.** A regional outage means hours of recovery, rebuilding
ClickHouse from S3. The alternative doubles the largest cost line to protect against something that
happens every few years. That's a business decision I'd take to leadership explicitly rather than make
silently.

**I gave up a single unified engine.** Three data stores means three things to operate. I'd accept that
because the workloads are genuinely incompatible at this volume — but if we were at 200 GB instead of
260 TB, I'd put all of it in Postgres and the operational simplicity would be worth more than the
performance."

Then: **"The decision I'd most want to revisit in a year is self-managing ClickHouse. If our peak-to-
average ratio grows, or if the two people who know it deeply leave, the separated storage/compute model
in ClickHouse Cloud becomes the better trade and I'd want to have kept that door open — which is
another reason the lake, not ClickHouse, is the system of record."**

---

### Case study B — The billing discrepancy

**Prompt:** *"It's the 3rd of the month. Finance says last month's invoices total $340,000 but the usage
data says it should be $391,000 — a 13% shortfall. Invoices went out yesterday. Forty-one customers are
affected. Find the problem and tell me what you'd do."*

**What's being tested:** incident leadership under commercial pressure, investigation discipline,
knowing when to escalate versus when to dig, and — the real test — whether you optimise for finding the
truth or for finding a fix.

---

**Phase 1 — Stabilise the situation (first 30 minutes).**

Three actions in parallel, and the first one is not technical:

1. **Stop the bleeding.** Are more invoices going out? If billing runs in batches, pause the remainder.
   You can always resume; you can't unsend.
2. **Escalate immediately.** Finance, the account team, and your manager. **Do not investigate for four
   hours and then report.** Forty-one customers have wrong invoices; the business needs to know now to
   decide whether to proactively contact them. The message is: "We've confirmed a 13% discrepancy
   affecting 41 customers, investigation started, first update in 2 hours."
3. **Snapshot everything.** Freeze the exact data used to generate those invoices, before anything
   changes. If it came from a live table that's still being written, capture it now. **In a
   reconciliation dispute the inputs are the evidence**, and I'd take an Iceberg snapshot or a table
   copy immediately.

---

**Phase 2 — Determine the shape of the error (next 2 hours).**

The discipline: **characterise before hypothesising.** A 13% aggregate shortfall could be many small
errors or a few large ones, and those have completely different causes.

```sql
SELECT tenant_id, invoiced_events, computed_events,
       computed_events - invoiced_events AS delta,
       round(100.0*(computed_events-invoiced_events)/nullif(computed_events,0),2) AS pct
FROM billing_reconciliation
WHERE billing_month = '2026-08'
ORDER BY abs(delta) DESC;
```

Then ask four questions of that output:

- **Is it concentrated or spread?** If 3 tenants account for the whole $51,000, it's a per-tenant issue —
  routing, a shard, an account state. If all 41 are down ~13%, it's systematic.
- **Is 13% a suspicious number?** Look for structure. 1/8 = 12.5% — **one of eight shards missing**.
  4.2% ≈ 1/24 — one hour of the day. 3.2% ≈ 1/31 — one day of the month. **Round fractions are the
  strongest clue available** and they immediately localise the problem.
- **When did it start?** Compare against the previous three months. If July was fine and August isn't,
  what changed on August 1st? Deploy log, config changes, infrastructure changes.
- **Which direction?** Under-count means data missing from the billing input. Over-count would mean
  duplicates. Here it's under, so something was excluded.

Suppose the answer is: spread evenly across all 41 tenants, at 12.4%, starting August 8th. **12.5% is
1/8. There are 8 shards. A shard is missing from the billing query.**

---

**Phase 3 — Confirm the root cause (next 2 hours).**

Verify rather than assume — a plausible hypothesis that's wrong costs you a day.

- Query the billing input for August, grouped by shard. If shard 6 has zero or partial rows, confirmed.
- Why? Walk the lineage backwards from the billing aggregate to raw. Candidates: the billing job's
  cluster config lists 7 of 8 shards (someone added shard 8 in a resharding on August 8th and updated
  the ingest config but not the billing job's); shard 6 was down during the aggregation window and the
  query ran with `skip_unavailable_shards = 1`, returning partial results silently (**this is the
  danger of that setting from Part 2 §2.7, made concrete**); or a materialized view on that shard was
  never created.

Suppose it's the first: a shard added on August 8th, invisible to the billing job's hardcoded shard
list. That's the technical cause. **The systemic cause is that topology is duplicated in config instead
of read from the control plane**, and that's the finding that matters.

---

**Phase 4 — Remediate.**

*Immediate:* recompute August's usage correctly, from the immutable snapshot, and produce a corrected
per-tenant delta report. **Give finance numbers, not conclusions** — whether to reissue invoices, credit
forward, or absorb is a business decision with revenue and customer-relationship implications, and it's
not yours.

*Verify the fix:* recompute July and June with the corrected job. If they were also affected — the shard
was added August 8th, so probably not, but *check* rather than assume — the scope widens and finance
needs to know immediately.

*Then the systemic fixes, and this is what the interviewer is waiting for:*

1. **Cluster topology comes from the control plane**, never from a static list in a job config. One
   source of truth for "which shards exist."
2. **`skip_unavailable_shards = 0` for any query producing a number of record.** Billing must fail
   loudly rather than return partial results confidently. I'd audit every scheduled job for this
   setting today.
3. **Reconciliation before invoicing, as a hard gate.** Compare the billing aggregate against an
   independent count from the lake, per tenant. Any tenant deviating by more than 0.1% blocks the run
   and alerts. **This control alone would have caught it before a single invoice went out**, and it's
   the highest-value item on the list.
4. **Assert on shard coverage.** The billing job asserts it read from exactly N shards where N comes
   from the control plane, and that every expected tenant appears.
5. **Idempotent, reproducible billing.** Pin the Iceberg snapshot ID on every invoice so any invoice can
   be reproduced exactly, months later, during a dispute.

---

**The closing statement:** "The proximate cause is a hardcoded shard list. The real finding is that we
generated and sent invoices with no reconciliation gate — the system had no way to notice it was wrong.
I'd ship the reconciliation gate this week, before the root-cause fix, because it protects against the
next different cause too."

---

### Case study C — Six months to FedRAMP

**Prompt:** *"We've signed a federal customer contingent on FedRAMP Moderate authorisation in six
months. Today we're a single commercial AWS deployment. You own the data platform side. What do you do,
and what do you tell the exec team on day one?"*

**What's being tested:** whether you can scope a compliance programme realistically, whether you'll push
back on an impossible timeline with evidence rather than complaint, and whether you understand that
compliance is architecture plus evidence plus process.

---

**Day one message to the exec team.** Lead with the honest assessment, because a staff engineer who
nods along to an impossible date and fails in month five has done more damage than one who reset
expectations in week one.

"FedRAMP Moderate for a system that has never been in scope typically takes 12–18 months end to end:
building the environment, implementing ~325 controls, producing the System Security Plan, a 3PAO
assessment, and the agency authorisation process. Six months to *authorised* is not achievable. Six
months to *assessment-ready with a sponsoring agency engaged* is achievable if we start now and if the
agency relationship is already in motion. I'd want to reset the contract milestone to 'assessment-ready'
and confirm with the customer that they can accept an agency ATO on a defined path."

Then give them what *is* deliverable, because credibility comes from the alternative, not the pushback.

---

**The data-platform scope, in four workstreams.**

**Workstream 1 — The boundary (months 1–2).** A complete GovCloud deployment of the data platform.
Everything inside the authorisation boundary: collectors, Kafka, ClickHouse, S3, Postgres, and — the one
teams forget — **the entire observability stack**, because shipping metrics to a commercial SaaS
endpoint crosses the boundary. That means self-hosted Prometheus, Grafana, and log aggregation inside
GovCloud, which is a genuine chunk of work nobody budgets for.

Also inside: the CI/CD promotion path. Artifacts are built commercially and promoted with signature
verification; the build system itself stays outside. Get that design reviewed early because it's a
common assessment finding.

**Workstream 2 — Controls implementation (months 1–5).** The data-platform-relevant control families,
with what each actually requires:

- *Access control (AC):* least privilege, separation of duties, session termination, and **US-persons-
  only operational access**, which is a hiring and rotation constraint, not a technical one. Raise it in
  week one because it has the longest lead time.
- *Audit and accountability (AU):* every access to federal data logged with principal, time, and object;
  logs protected from modification; retained per policy. Part 6's access logging, hardened — logs go to
  a separate account with Object Lock.
- *Identification and authentication (IA):* MFA everywhere, FIPS 140-2/3 validated cryptographic modules.
  Practically: FIPS endpoints for every AWS service call, and verifying that every library doing crypto
  in our code uses a validated module. **This is more invasive than it sounds** — a Go binary needs
  `boringcrypto`, and any dependency doing its own TLS needs checking.
- *System and communications protection (SC):* encryption in transit and at rest, boundary protection,
  cryptographic key management. Per-tenant KMS keys already exist; the work is documenting and proving
  the key lifecycle.
- *Configuration management (CM):* baseline configurations, change control, **and continuous drift
  detection between commercial and GovCloud**, because divergence is guaranteed otherwise and it's both
  an operational risk and an audit finding.
- *Contingency planning (CP):* backup, recovery, and a *tested* restore. The restore-from-Iceberg
  runbook from Part 5 becomes a control with evidence — you must show test results, not a procedure.

**Workstream 3 — Evidence generation (months 2–6).** This is the workstream people underestimate by the
largest margin. FedRAMP requires *evidence* that controls operate, not just that they exist.

The strategic move — and it's the same one from Part 7 — is to **make evidence a by-product of
operation**. Concretely: the continuous cross-tenant isolation test suite produces access-control
evidence; the lifecycle audit table produces retention evidence; the drift detector produces
configuration-management evidence; the SLI dashboard produces availability evidence. Each one is a
dashboard you already want, that an assessor can be shown live. **If evidence generation is a separate
manual project, it will consume the team for two months and be stale by assessment.**

**Workstream 4 — Documentation (months 3–6).** The System Security Plan is a large document mapping
every control to its implementation. Engineering writes the technical narratives; a compliance
specialist writes and assembles the rest. **Budget for hiring or contracting that specialist in month 1**
— engineers writing an SSP is slow, poor-quality, and demoralising, and it's the most common reason these
programmes slip.

---

**What I'd flag as risks on day one, with mitigations:**

- **Agency sponsorship.** FedRAMP requires an agency to sponsor and authorise. If the customer isn't
  already engaged as a sponsor, the timeline is not ours to control. **This is the single largest
  schedule risk and it's not an engineering one** — raise it explicitly and make sure someone owns it.
- **3PAO availability.** Assessors are booked months ahead. Engage one in month 1, not month 5.
- **Operational cost.** GovCloud plus a duplicated stack plus continuous compliance is roughly a 2–4×
  multiplier on the platform's operating cost and an ongoing ~30% tax on team capacity for change
  control and evidence. That needs to be in the business case for the federal segment, not absorbed.
- **Feature velocity.** Every change to the authorised system goes through change control. The team
  should expect meaningfully slower delivery in GovCloud, and product needs to plan for feature
  divergence between the two environments — which is itself a decision to make deliberately rather than
  discover.

---

**The closing:** "In six months I can deliver a fully deployed, controls-implemented, evidence-producing
GovCloud data platform that's ready for a 3PAO assessment. What I can't deliver is the authorisation
itself, because that depends on assessor scheduling and agency process. I'd rather tell you that now
and be right in month six than agree today and be wrong."

---

### Case study D — The cost crisis

**Prompt:** *"Our cloud bill went from $180K to $310K a month over two quarters. Revenue grew 20%.
Finance wants it back under $220K in one quarter. Where do you start?"*

**What's being tested:** whether you measure before cutting, whether you understand cloud cost structure,
and whether you can distinguish "we're wasting money" from "we grew" — and handle the political
dimension of the answer.

---

**Step 1 — Decompose the increase before proposing anything (week 1).**

$130K of growth against 20% revenue growth. If cost scaled linearly with revenue you'd expect $216K, so
about **$94K is unexplained**. That's the number to chase, and framing it that way immediately makes the
conversation productive rather than defensive.

Get the bill broken down three ways:
- **By service** (compute / storage / transfer / requests). This is available from Cost Explorer and
  takes an hour.
- **By tag** (which team, which dataset, which environment). If tagging is incomplete — it always is —
  fixing tagging is the first task, because you cannot manage what you cannot attribute.
- **Over time**, daily. **A step change points to a specific event; a ramp points to growth.** That
  single distinction determines the entire investigation, and it's the first graph I'd draw.

---

**Step 2 — Classify each increase (week 1–2).**

Three buckets, with very different responses:

*Growth-driven (expected).* Ingest volume up 20% → storage and compute up proportionally. This isn't
waste; it's the cost of the business. The lever here is **unit economics**: cost per TB ingested, cost
per tenant. If those are flat, you're scaling efficiently. If they're rising, something is
super-linear and that's the real problem.

*Event-driven (a step change).* Someone launched something. Typical finds: a new environment nobody
turned off, a backfill job still running three months later, a dashboard with a 5-second refresh doing
30-day scans, a debug logging level left on in production shipping 10× the log volume, a development
ClickHouse cluster running 24/7.

*Drift (a slow ramp with no corresponding growth).* Data accumulating without a retention policy;
snapshots never expiring; a lifecycle rule that silently stopped working; over-provisioned instances
from a load test that were never scaled back.

---

**Step 3 — Find the specific items.** In my experience the distribution is heavily skewed: 3–5 items
account for 80% of the unexplained increase. The usual suspects at a data platform, in order of how
often they're the answer:

1. **S3 with no lifecycle policy.** Storage grows monotonically. 260 TB in Standard costs $5,980; the
   same data properly tiered costs $1,803. If a new dataset launched without lifecycle rules, this is
   pure, immediate savings.
2. **Untiered ClickHouse data.** EBS at $0.08/GB-month versus S3 at $0.023. If TTL rules weren't applied
   to a new table, hot storage is holding 400 days of data at 3.5× the price.
3. **NAT Gateway data processing.** $0.045/GB. A job pulling from S3 without a VPC Gateway Endpoint can
   generate five figures a month invisibly. **Always check this** — it's the most common large mistake
   and the fix is free.
4. **Cross-AZ transfer.** $0.01/GB each way. New Kafka consumers without rack awareness, or a service
   moved to a different AZ from its dependency.
5. **Orphaned resources.** Unattached EBS volumes, old snapshots, idle load balancers, unreleased
   Elastic IPs, a dev cluster from a project that ended.
6. **Query cost explosion.** If anything is pay-per-scan (Athena, BigQuery), one badly-written scheduled
   query can be enormous. Check per-query bytes scanned.

---

**Step 4 — Build the plan, sequenced by effort and risk.**

Present it as a table with savings, effort, and risk, because that's what makes it a decision rather
than a wish list:

| Action | Monthly saving | Effort | Risk |
| --- | --- | --- | --- |
| S3 lifecycle policies | $4,200 | 1 day | None — reversible |
| Delete orphaned resources | $8,000 | 2 days | Low — verify unused first |
| VPC endpoints for S3 | $6,000 | 1 day | None |
| Kafka compression + rack-aware consumers | $4,200 | 3 days | Low |
| ClickHouse TTL to S3 volumes | $3,400 | 1 week | Medium — slower queries 7–30d |
| Reserved instances / savings plans | $9,600 | 1 week (approval) | Low — commitment risk |
| Right-size after utilisation review | $12,000 | 2 weeks | Medium — needs load testing |
| Shut down idle non-prod | $5,000 | 1 week | Low — with auto-shutdown schedules |

That totals about $52K. Finance asked to go from $310K to under $220K, so the target reduction is $90K —
say plainly that you're short rather than padding the table. The remaining gap requires either accepting a higher baseline (because the business grew) or
making a product decision: **reduce retention, sample high-volume datasets, or change the SLO.** Those
are trades for leadership, and presenting them as options with impact is the right move:

- "Cutting hot retention from 30 to 14 days saves $7K/month and means forensic queries over 14–30 days
  go from 3 seconds to 90 seconds. That's a product decision."
- "Sampling the highest-volume dataset 1:10 for the raw tier while keeping exact aggregates saves
  $18K/month and means individual-record lookups older than 7 days become best-effort."

---

**Step 5 — Make it stick, which is the part that distinguishes a staff answer.**

Cost regresses within two quarters unless there's a mechanism. Four:

1. **Cost as an SLI.** Cost per TB ingested and cost per tenant, on a dashboard, reviewed monthly with
   an owner. Trends, not absolutes.
2. **Tagging enforced at provisioning.** Untagged resources are automatically flagged and, after a grace
   period, stopped in non-production. Attribution has to be automatic or it decays.
3. **Anomaly alerting on spend.** A daily-spend alert at 20% above trailing average, routed to
   engineering, not just finance. **Treat a cost spike as an incident signal** — it usually is one.
4. **Lifecycle policies required at dataset creation.** Part 7's dataset registration already demands a
   retention class, so a new dataset physically cannot be created without a tiering policy. **That's the
   control that prevents item 1 from recurring**, and it's the one worth building even if the others
   slip.

**The closing:** "About $52K of this is straightforward waste and I'll have most of it back within six
weeks with no product impact. The remaining gap is a real trade between cost and capability, and I'd
rather bring you three specific options with their consequences than quietly degrade something and have
you find out from a customer."

---

## Part 14 — Rapid-fire question bank

Shorter questions with tight answers. These are the ones that come up as follow-ups, in phone screens,
or as sanity checks between deeper questions. Cover them all; a wrong answer here undermines a good
answer elsewhere.

### 14.1 ClickHouse

**What does `ORDER BY` do in a MergeTree table?** Three things: it's the sparse primary index used for
granule pruning, it determines physical row order (which drives compression), and in
`Replacing`/`Collapsing`/`Summing` engines it *defines row identity for merges*. Only put
immutable-per-entity columns in it.

**What's `index_granularity`?** Rows per index entry, default 8,192. The primary index stores one mark
per granule, so it's small enough to stay in memory. Lower it for very selective point-lookup workloads
(more index, finer pruning); raise it for wide scans.

**Difference between `PARTITION BY` and `ORDER BY`?** Partitions are physical directories and the unit
of DROP, TTL, FREEZE, and ATTACH; merges never cross them. `ORDER BY` is the sort within a part. Keep
partitions coarse (daily) and use `ORDER BY` for query pruning.

**What happens on `INSERT`?** A new immutable part is written: rows sorted by `ORDER BY`, one file per
column, plus index and checksum files. Background merges consolidate parts over time.

**Why "too many parts"?** Insert rate exceeds merge rate. Throttling at 150 parts per partition
(`parts_to_delay_insert`), rejection at 300 (`parts_to_throw_insert`). Fix by batching to 10K–100K rows
and roughly one insert/sec per table per shard.

**`ReplacingMergeTree` vs `CollapsingMergeTree`?** `Replacing` keeps the row with the highest version
per sort key — good for "latest state." `Collapsing` uses a `sign` column (+1/-1) so a matched pair
cancels — good for incremental deltas where you emit a cancel row. `VersionedCollapsing` adds a version
so out-of-order arrival still collapses correctly.

**When is a merge guaranteed to have happened?** Never, without `OPTIMIZE ... FINAL`. Merges are
background and best-effort. Reads needing guaranteed dedup use `FINAL` or `argMax`.

**What's a materialized view in ClickHouse?** An insert trigger on the source table that writes derived
rows to a target table. It does *not* see updates or deletes, does not backfill history without
`POPULATE` or a manual `INSERT SELECT`, and must be attached to the local `MergeTree` table (not the
`Distributed` table) in a sharded cluster.

**What's `AggregatingMergeTree` for?** Storing partial aggregate *states* (`sumState`, `uniqState`) that
merge correctly across rows. Read with `-Merge` combinators. Handles late-arriving data automatically,
because a late event just adds another partial state.

**`uniq` vs `uniqExact` vs `uniqCombined`?** `uniq` is HyperLogLog, ~2% error, tiny memory. `uniqExact`
is exact, memory proportional to cardinality. `uniqCombined` is a hybrid — exact below a threshold, HLL
above. Use `uniqExact` for billing, `uniq` for dashboards.

**What is `PREWHERE`?** Reads only the filter columns first, evaluates the condition, then reads the
remaining columns only for surviving rows. Large win on wide tables with a selective filter on a narrow
column. Automatic via `optimize_move_to_prewhere`, but explicit is often better.

**How do skip indexes work?** They store a summary (`minmax`, `set`, `bloom_filter`, `tokenbf_v1`,
`ngrambf_v1`) per block of `GRANULARITY × index_granularity` rows. A query that can't match the summary
skips the block without decompressing. Only useful when data is clustered relative to the filter.

**What's `LowCardinality`?** Dictionary encoding per part. Use under ~10,000 distinct values; harmful
above ~100,000 because dictionary overhead dominates.

**How does ClickHouse replication work?** `ReplicatedMergeTree` coordinates through ClickHouse Keeper (or
ZooKeeper): replicas share a replication log of part operations and fetch parts from each other.
Eventually consistent by default; `insert_quorum` and `select_sequential_consistency` tighten it.

**What's the `Distributed` engine?** A view over shard-local tables that fans queries out and merges
results. Writing through it is asynchronous by default (spools locally) — for controlled semantics,
write directly to shard-local tables.

**How do you delete data?** `ALTER TABLE ... DELETE` is a heavyweight mutation that rewrites parts.
Lightweight `DELETE FROM` (23.x+) writes a `_row_exists` mask, applied at read time and materialised at
the next merge. Best of all: `DROP PARTITION` if the data aligns with a partition.

**Dictionaries — why and which layout?** Fast key-value lookups joined at query time with `dictGet`,
avoiding real joins. `HASHED` for in-memory, `SPARSE_HASHED` for lower memory, `CACHE`/`SSD_CACHE` for
bounded memory with source lookups on miss, `DIRECT` for no caching.

**Why is ClickHouse bad at joins?** The default hash join builds the right-hand table in memory, so a
large right side fails. Use `grace_hash` for large-to-large, put the smaller table on the right (the
optimiser won't always reorder), and prefer dictionaries or denormalisation.

**What's `GLOBAL JOIN`?** In a distributed query, gathers the right-hand side on the initiator and
broadcasts it to all shards. Correct but expensive; avoid for large dimensions.

**How do you move data between storage tiers?** Storage policies with named volumes, and `TTL ... TO
VOLUME 'name'` clauses. Queries work identically across tiers; S3-backed parts are just slower.

### 14.2 PostgreSQL

**What is MVCC?** Each row version carries `xmin`/`xmax` (creating and deleting transaction IDs).
Readers see versions visible to their snapshot; writers create new versions rather than overwriting.
Readers never block writers.

**What is bloat and what causes it?** Dead tuples not yet reclaimed by vacuum. Caused by updates and
deletes outrunning autovacuum, or by a long-running transaction holding back the xmin horizon so vacuum
*can't* reclaim.

**What is a HOT update?** Heap-Only Tuple: if no indexed column changed and the page has free space, the
new version goes on the same page with no index update. Enabled by setting `fillfactor` below 100.
Significantly reduces index bloat on update-heavy tables.

**What is transaction ID wraparound?** `xid` is 32-bit; after ~2 billion transactions it wraps, so old
tuples must be frozen first. If vacuum falls too far behind, Postgres stops accepting writes to protect
data. Monitor `age(datfrozenxid)`.

**When does the planner choose a seq scan over an index scan?** When the filter is not selective enough
that random index+heap I/O beats sequential reads — typically above 5–10% of the table. Also when
statistics are stale, or when `random_page_cost` (default 4.0) is set for spinning disks on SSD
hardware, where ~1.1 is realistic.

**What is a bitmap heap scan?** Builds a bitmap of matching pages from one or more indexes, then reads
pages in physical order. Middle ground between index and sequential scans, and how Postgres combines
multiple indexes on one table.

**When does an index-only scan actually avoid heap access?** Only when all needed columns are in the
index *and* the pages are marked all-visible in the visibility map — which requires recent vacuum. Check
`Heap Fetches` in the plan.

**What's `INCLUDE` on an index for?** Adds non-key columns to the leaf pages so a query can be answered
index-only without them participating in the ordering or uniqueness. Smaller than adding them as key
columns.

**What is a BRIN index?** Block Range INdex: stores min/max per range of pages. Tiny, and effective when
the column correlates with physical order — a timestamp on an append-only table. Useless on unordered
data.

**When do you need `CREATE STATISTICS`?** When columns are correlated and the planner's independence
assumption produces bad estimates — e.g. `city` and `postcode`. Extended statistics teach it the
dependency.

**Read Committed vs Repeatable Read vs Serializable?** RC: each statement sees a fresh snapshot. RR: the
whole transaction sees one snapshot (prevents non-repeatable reads; Postgres's RR also prevents phantom
reads). Serializable: adds SSI, detecting dangerous read/write dependencies and aborting one transaction
— prevents write skew.

**What's write skew?** Two transactions each read a state, each make a decision valid alone, and the
combination violates an invariant (both doctors go off-call because each sees the other on-call). Only
Serializable prevents it.

**What is logical replication, and what's the risk?** Decodes WAL into row-level changes published to
subscribers via a replication slot. The risk: **a slot whose consumer stalls prevents WAL cleanup, and
the disk fills, taking down the primary.** Monitor slot lag as a first-class alert.

**PgBouncer transaction pooling — what breaks?** Anything with session state: `SET` (use `SET LOCAL`),
prepared statements, advisory locks, `LISTEN/NOTIFY`, temp tables. The dangerous one in multi-tenancy is
a `SET app.tenant_id` persisting onto the next transaction, which is another tenant's.

**How do you add a column safely to a large table?** `ADD COLUMN ... DEFAULT` is metadata-only since
PG11 — instant. Adding a `NOT NULL` without a default requires a rewrite. Adding an index needs `CREATE
INDEX CONCURRENTLY`. Always set `lock_timeout` before DDL so a blocked `ALTER` doesn't queue every
subsequent query behind it.

**What is the outbox pattern?** Write an event row to an `outbox` table in the same transaction as the
business change; a separate process publishes it. Gives you atomicity between state change and event
publication without a distributed transaction.

**What is `pg_stat_statements` for?** Aggregated per-normalised-query statistics: calls, total and mean
time, rows, buffer hits/reads. The single most valuable extension for query performance work. Order by
*total* time, not mean.

### 14.3 S3, Parquet, and table formats

**What is S3's consistency model?** Strong read-after-write for PUTs, overwrites, deletes, and LIST
(since December 2020). The old eventual-consistency caveats no longer apply.

**What are S3's request rate limits?** 3,500 PUT/COPY/POST/DELETE and 5,500 GET/HEAD per second *per
partitioned prefix*. It scales automatically but you can get 503 SlowDown during partition splits;
spread load across prefixes and retry with backoff.

**What's the ideal object size for analytics?** 128 MB–1 GB. Small files kill query performance
(per-object open overhead, listing cost, more metadata) and increase PUT charges. Compaction of small
files is a routine maintenance job in any lake.

**How does Parquet make queries fast?** Columnar layout with row groups; per-column-chunk min/max
statistics and optional bloom filters enable predicate pushdown so readers skip row groups entirely;
dictionary and run-length encoding compress well; readers fetch only needed column chunks via ranged
GETs.

**Why Iceberg over Hive-style directories?** Atomic commits via snapshots (no partial reads), time
travel and reproducible reads, schema evolution by column ID (rename and reorder safely), hidden
partitioning (queries don't need to know the partition scheme), row-level deletes, and no expensive
LIST operations to plan a scan.

**Iceberg copy-on-write vs merge-on-read?** CoW rewrites affected data files on delete/update — slower
writes, fastest reads. MoR writes delete files applied at read time — fast writes, slower reads until
compaction. Choose by write frequency; compact regularly either way.

**What is S3 Object Lock?** WORM retention. *Governance* mode can be overridden with a specific IAM
permission; *compliance* mode cannot be overridden by anyone, including root. Requires versioning.
Compliance mode is irrevocable — a bug that applies a 7-year lock is 7 years of unavoidable cost.

**SSE-S3 vs SSE-KMS vs SSE-C?** SSE-S3: S3-managed keys, no per-request cost. SSE-KMS: your KMS keys,
auditable in CloudTrail, per-request KMS charges — use **S3 Bucket Keys** to cut those by up to 99%.
SSE-C: you supply the key per request; S3 never stores it.

**How do you cut S3 storage cost?** Lifecycle transitions: Standard → Standard-IA (30d) → Glacier
Instant Retrieval (90d) → Deep Archive. Mind the minimum-duration charges (30/90/180 days) and the
~$0.05 per 1,000 objects transition cost; for cold-from-birth data, write directly to the target class.

**What's an S3 Access Point?** A named endpoint with its own policy, scoped to a bucket and prefix.
Ideal for per-consumer or per-tenant access without one giant bucket policy, and revocation is deleting
one policy.

**Multipart upload limits?** Minimum 5 MB per part (except the last), maximum 10,000 parts, maximum
object size 5 TB. Abort incomplete uploads with a lifecycle rule or you pay for orphaned parts forever.

### 14.4 Kafka and streaming

**What does `acks=all` mean?** The leader waits for all in-sync replicas to acknowledge. Combined with
`min.insync.replicas=2` and RF=3, it tolerates one broker loss without data loss and without blocking
writes.

**Why `min.insync.replicas=2` and not 3?** With 3, losing one broker (or one AZ) stops all writes.
With 2, you continue writing with reduced redundancy — strictly better than an outage.

**What is producer idempotence?** `enable.idempotence=true` gives each producer a PID and sequence
numbers per partition, so broker-side retries don't duplicate. Free; always on in modern clients.

**Can you have more consumers than partitions?** No — the extras idle. Partition count is the ceiling on
consumer parallelism, so choose it generously up front; increasing it later breaks key-to-partition
stability.

**What breaks when you increase partitions?** Keyed messages hash to different partitions after the
change, so per-key ordering across the boundary is lost and any stateful consumer keyed by partition is
disrupted.

**How do you handle a hot partition?** Salt the key for high-volume keys (`tenant:0..15`), trading
strict per-key ordering for balance. Or route the hot key to a dedicated topic and consumer group.

**Consumer lag: records or seconds?** Seconds. Records-behind is meaningless without a rate — 100,000
records is 10 seconds at one rate and an hour at another. Emit both; alert on time-based lag.

**What's a rebalance storm?** Repeated consumer group rebalances (from long processing pauses exceeding
`max.poll.interval.ms`, or flapping members), during which no progress is made. Fix with smaller poll
batches, longer intervals, and cooperative sticky assignment.

### 14.5 SLOs, reliability, and operations

**What's an error budget?** `1 − SLO`, expressed as allowed bad events or time in the window. It converts
reliability into a currency you can spend on velocity, and gives an agreed policy for when to stop
feature work.

**What's multi-window multi-burn-rate alerting?** Alert when the error-budget burn rate is high over both
a long and a short window. Long confirms it's real; short confirms it's ongoing and makes the alert
resolve quickly. Page at 14.4× (2% of budget in an hour) and 6× (5% in six hours); ticket below that.

**Why not just alert on a threshold?** Thresholds trade false positives against detection time with one
knob and there's no good setting. Burn rate makes severity proportional to actual budget impact, so a
30-second blip can't page.

**What's the difference between an SLO and an SLA?** SLO is your internal target; SLA is a contractual
promise with financial consequences. SLAs should be meaningfully looser than SLOs so you fix problems
long before you owe credits.

**How do you alert on missing data?** Expected-arrival schedules derived from history, `absent()`-style
checks, always emitting zero rather than nothing, and dead-man's switches monitored *outside* your
monitoring system. Anything whose absence looks like success needs an explicit liveness alert.

**What's the first metric you'd add to a new pipeline?** End-to-end freshness measured by synthetic
canaries at the query boundary — because it's the only single metric that catches failures at every
stage, and it measures what the user experiences rather than what a component reports.

### 14.6 Governance, security, and compliance

**What's crypto-shredding?** Encrypt with a key scoped to the erasure unit; to erase, destroy the key.
The ciphertext can remain (satisfying WORM retention) but is permanently unrecoverable. It's what
resolves GDPR erasure against immutable archives.

**What's the difference between pseudonymisation and anonymisation?** Pseudonymised data can be
re-linked with additional information you hold (an HMAC with a key you keep) and is still personal data
under GDPR. Anonymised data cannot be re-linked by anyone and falls outside GDPR. Most "anonymisation"
is actually pseudonymisation.

**What's row-level security and where does it live?** Server-side row filtering: Postgres `CREATE
POLICY`, ClickHouse `CREATE ROW POLICY`. It's the layer that holds even when the application's query is
wrong. Remember `FORCE ROW LEVEL SECURITY` in Postgres (owners bypass RLS otherwise) and that ClickHouse
policies are permissive-OR by default.

**What's a data contract?** A producer's versioned specification of a dataset — schema, semantics,
guarantees (freshness, completeness), compatibility policy, deprecation notice period, and a registered
consumer list. Enforced in CI, so a breaking change fails the build.

**What is column-level lineage for?** Impact analysis before a change, root-cause traversal after an
incident, and automatic generation of the personal-data flow record for GDPR Article 30. Capture from
the execution engine (OpenLineage) rather than by declaration, so it can't drift.

**How do you prove a control works to an auditor?** Continuous, automated evidence: a test suite that
attempts cross-tenant access and records the denials, an append-only audit table, configuration exported
from live systems with git history. Design so evidence is a by-product of operation, not an audit-season
project.

**What's the difference between silo, pool, and bridge tenancy?** Dedicated infrastructure per tenant,
shared infrastructure with logical separation, and a mix. Choose per *layer*, not globally — and always
silo the encryption keys, because that's what makes crypto-shredding and hard isolation possible even in
a pooled store.

### 14.7 Migration and integration

**What's the strangler fig pattern?** Put an interface in front of the legacy system, redirect
functionality to the new system piece by piece behind that interface, and remove the legacy system when
nothing routes to it. The art is choosing the seam.

**What's an anti-corruption layer?** A translation boundary that converts a legacy system's model into
yours, so their identifiers, encodings, and exceptions don't spread. Testable invariant: their column
names appear only inside the ACL.

**What's expand/migrate/contract?** Add the new thing alongside the old, move consumers at their own
pace, then remove the old — only after measured zero usage. Three deploys, never one, and no
simultaneous producer/consumer deployment.

**How do you prove a migration didn't break anything?** Shadow reads with byte-level (or
tolerance-bounded) result diffing against replayed production queries, per consumer, with a published
mismatch rate and a gate like "< 0.01% for 14 consecutive days."

**When do you turn off the legacy system?** When access logs show zero reads for 90 days, every dataset
is migrated or formally decommissioned, a final immutable snapshot exists and has been test-restored,
and the infrastructure — including the Terraform that would recreate it — is deleted.

**CDC vs outbox vs scheduled extract?** CDC when you need real-time and can't modify the source, at the
cost of coupling to their physical schema. Outbox when they'll accept a small change — better, because
the event schema is a contract they own. Scheduled extract when freshness allows — simplest, most
robust, and underrated for slowly-changing reference data.

---

## Part 15 — Numbers worth memorising

You will do arithmetic on a whiteboard. Having these in your head means you can size a system in
thirty seconds instead of hand-waving. All figures are order-of-magnitude anchors, not quotes — prices
change and vary by region, and you should say so when you use them.

### 15.1 Time and capacity conversions

```
1 day        = 86,400 seconds        ≈ 10^5      (the single most useful approximation)
1 month      = 2.6 million seconds   ≈ 2.6 × 10^6
1 year       = 31.5 million seconds  ≈ 3 × 10^7

1,000 events/sec  = 86.4 million/day   ≈ 2.6 billion/month
10,000 events/sec = 864 million/day    ≈ 26 billion/month
100,000 /sec      = 8.64 billion/day
1 million /sec    = 86.4 billion/day
```

Bytes, given a 400-byte event:

```
1,000 events/sec   × 400 B = 400 KB/sec  = 34.5 GB/day  = ~1 TB/month
150,000 events/sec × 400 B = 60 MB/sec   = 5.2 TB/day   = ~156 TB/month raw
```

**The one-line trick:** *events/sec × bytes × 10^5 ≈ bytes/day.* Then divide by your compression ratio.

### 15.2 Compression ratios (measured on telemetry-shaped data)

| Format / setting | Typical ratio | Note |
| --- | --- | --- |
| JSON → gzip | 5–8:1 | Row-wise; no column benefit |
| JSON → Parquet + Snappy | 6–10:1 | Columnar, fast |
| JSON → Parquet + ZSTD | 8–12:1 | Better ratio, ~2× slower to write |
| JSON → ClickHouse, unsorted | 4–6:1 | Sorting is most of the win |
| JSON → ClickHouse, well-sorted + LowCardinality + codecs | **10–15:1** | What Skyline achieves |
| Delta + ZSTD on a timestamp column | 6–10× vs plain ZSTD | Sorted timestamps are near-constant deltas |

**The insight to carry:** most of the compression comes from *sorting*, not from the codec. A well-chosen
`ORDER BY` beats a better codec every time.

### 15.3 Storage prices (AWS us-east-1, approximate)

```
S3 Standard                     $0.023 /GB-month
S3 Standard-IA                  $0.0125            (30-day minimum charge)
S3 One Zone-IA                  $0.010             (single AZ — accept the durability trade knowingly)
S3 Glacier Instant Retrieval    $0.004             (90-day minimum, millisecond access)
S3 Glacier Flexible Retrieval   $0.0036            (90-day minimum, minutes–hours)
S3 Glacier Deep Archive         $0.00099           (180-day minimum, ~12 hours)

EBS gp3                         $0.080 /GB-month   (3,000 IOPS + 125 MB/s baseline included)
EBS io2                         $0.125 /GB-month   + per-provisioned-IOPS

S3 PUT/POST/LIST                $0.005  per 1,000
S3 GET/SELECT                   $0.0004 per 1,000
Lifecycle transition            ~$0.05  per 1,000 objects
```

**Ratios to remember:** EBS is ~3.5× S3 Standard. S3 Standard is ~23× Deep Archive. Standard → Glacier
Instant Retrieval alone is a ~5.75× reduction, and it keeps millisecond access.

### 15.4 Data transfer prices — where the surprises live

```
Same AZ                          $0
Cross-AZ, same region            $0.01 /GB EACH DIRECTION  ($0.02 round trip)
Cross-region                     $0.02 /GB
Internet egress                  $0.09 /GB (first 10 TB/month)
NAT Gateway processing           $0.045 /GB  + $0.045/hour
VPC Gateway Endpoint (S3/DDB)    $0        ← always use these
VPC Interface Endpoint           ~$0.01/hour/AZ + $0.01/GB
```

**The two mistakes that cost six figures:** S3 traffic routed through a NAT Gateway instead of a free
Gateway Endpoint, and Kafka consumers reading cross-AZ instead of from a rack-aware same-AZ replica.

### 15.5 Latency anchors

```
L1 cache                        ~1 ns
Main memory                     ~100 ns
NVMe SSD random read            ~100 µs      (0.1 ms)
Network, same AZ                ~0.5 ms
Network, cross-AZ               ~1–2 ms
S3 GET, first byte              ~20–100 ms
Network, US east ↔ west         ~60–70 ms
Network, US ↔ Europe            ~80–100 ms
```

**The implication people miss:** a single S3 GET costs roughly 1,000× an NVMe read. That's why small
files destroy lake query performance — the per-object latency dominates, and 10,000 small files means
10,000 round trips.

### 15.6 Throughput anchors

```
gp3 baseline                     125 MB/s (up to 1,000 MB/s provisioned)
NVMe instance store              2–7 GB/s sequential
Instance network                 10–25 Gbps typical (1.25–3 GB/s)
Kafka broker                     ~100 MB/s/broker conservative planning figure; far more achievable
Postgres simple txn              10,000–50,000/sec on good hardware
ClickHouse scan+aggregate        ~1–10 GB/s per node on compressed data, cluster-scaling
Parquet scan (Spark/Trino)       ~50–200 MB/s per core on compressed data
```

### 15.7 ClickHouse operational thresholds

```
index_granularity                8,192 rows (default)
parts_to_delay_insert            150 per partition   ← inserts start sleeping
parts_to_throw_insert            300 per partition   ← inserts rejected
Alert threshold you should set   100 per partition   ← ~30 min of warning
Insert batch target              10,000–100,000 rows / 10–100 MB
Insert frequency target          ~1 per second per table per shard
max_concurrent_queries           100 (default)
LowCardinality useful below      ~10,000 distinct values
Partitions per table             hundreds, not millions — daily is usually right
Free disk needed for merges      ≈ size of the parts being merged (keep below 80% used)
```

### 15.8 Reliability arithmetic

```
Availability   Downtime/month      Downtime/year
99%            7.2 hours           3.65 days
99.5%          3.6 hours           1.83 days
99.9%          43.2 minutes        8.76 hours
99.95%         21.6 minutes        4.38 hours
99.99%         4.3 minutes         52.6 minutes
99.999%        26 seconds          5.26 minutes
```

Burn rates against a 30-day window:

```
Burn rate  Budget consumed in 1 hour   Budget exhausted after
1×         0.14%                       30 days
6×         0.83%                       5 days
14.4×      2%                          2.08 days
36×        5%                          20 hours
```

---

## Part 16 — What separates a senior answer from a staff answer

Same question, two answers. The difference is rarely technical knowledge; it's scope, honesty, and
whether the answer ends at the system or at the organisation.

**On design.** *Senior:* "I'd use Kafka, ClickHouse, and S3, here's the diagram." *Staff:* "Before I
design, I need six numbers, because a 5,000/sec pipeline and a 500,000/sec pipeline aren't the same
system with bigger instances. Given these numbers, here's the design, here's the one decision that's
hard to reverse, and here's what I'd change if the volume were 10× lower."

**On trade-offs.** *Senior:* "ClickHouse is faster for analytics." *Staff:* "ClickHouse buys a ~1000×
read advantage on this query shape by giving up in-place updates, row locking, and transactions. That's
the right purchase here because our events are immutable. It would be the wrong purchase for the control
plane, which is why we run both."

**On failure.** *Senior:* "We'd have monitoring and alerts." *Staff:* "The four things that will actually
break are part explosion, partition skew, silent column-level quality failures, and hot-tier disk
pressure. Conventional monitoring catches none of them, so here are the four specific SLIs that do, and
here's the leading indicator for each so we get 30 minutes of warning rather than an outage."

**On migration.** *Senior:* "We'd migrate consumers to the new system." *Staff:* "Forty of the consumers
are contractual external customers, so they go last, after three quarters of production burn-in. The
seam I'd strangle is the S3 export file contract, because bytes in a bucket don't care what produced
them — which makes it a zero-change migration for most of them. And I'd absorb the migration cost onto
my team rather than asking theirs to spend roadmap on it."

**On incidents.** *Senior:* "I'd find the root cause and fix it." *Staff:* "First I'd establish whether
data is at risk, because we have five hours of Kafka retention and saying that out loud converts a panic
into a schedule. Then mitigate, then root cause. And the postmortem's main output isn't the fix — it's
the control that makes this class of failure detectable in minutes instead of weeks."

**On disagreement.** *Senior:* "The VP wants X but X is wrong." *Staff:* "The VP is optimising for
something real. I'd reframe in their units — this is a SOC 2 exception and a GDPR exposure, not an
architecture preference — and I'd de-risk the ask by proposing one quarter with a falsifiable deliverable
and an explicit kill criterion rather than six months on faith."

**On uncertainty.** *Senior:* answers everything. *Staff:* "I haven't operated Databricks at this scale,
so I'd want to benchmark rather than assert. What I do know is the shape of the comparison and the two
numbers that would decide it." **Admitting the boundary of your experience, and then showing you know how
to close the gap, reads as more senior, not less.**

### Red flags interviewers watch for

- Designing before asking for numbers.
- Proposing an architecture with no named downside.
- "We'd use Kubernetes/Kafka/Spark" as an answer rather than as a consequence of a requirement.
- No answer for "how would you know it's broken?"
- Treating compliance as someone else's checklist.
- Big-bang migration with no rollback.
- `PARTITION BY tenant_id` in ClickHouse (or any per-tenant physical partitioning at 10,000+ tenants).
- Claiming exactly-once semantics without explaining the mechanism.
- Not knowing what their proposal costs, to within an order of magnitude.
- Blaming a previous team for a legacy system rather than describing how they worked with it.
- Optimising a system while ignoring that one tenant is 18% of it.

### Phrases worth having ready

- *"Before I design, can I get six numbers?"*
- *"I'd choose X; the cost is Y; I'd accept Y because Z; if Z stopped being true I'd switch, and
  switching would cost roughly N weeks."*
- *"That's a control-plane change, not a deploy."*
- *"We have five hours of retention headroom, so no data is at risk — this is a schedule, not an
  emergency."*
- *"I'd migrate seven of the twelve and deliberately leave four. Here's the criterion."*
- *"The proximate cause was X. The systemic cause was that we had no way to detect X, and that's what
  I'd fund."*
- *"I'd rather tell you that now and be right in month six than agree today and be wrong."*

---

## What to take away

1. **Ask for the numbers before you design.** Events per second, event size, freshness, retention, tenant
   count, and top-tenant share determine nearly the entire architecture. Skipping them produces designs
   that are plausible and wrong.

2. **Separate the control plane from the data plane, and say so early.** Postgres governs; ClickHouse
   and S3 execute. The payoff is that policy changes — retention, quotas, routing, tenant tiers — become
   row updates rather than deploys, which matters most during an incident.

3. **Make the durable log the source of truth, and everything else a materialisation.** This one decision
   is what makes reprocessing a Tuesday instead of a project, makes schema changes provisional, makes
   ClickHouse restorable, and makes resharding survivable. It is the highest-leverage property in the
   whole architecture.

4. **The sort key is the most consequential line in a ClickHouse schema.** It's the sparse index, it
   drives compression, and in `ReplacingMergeTree` it silently defines row identity for deduplication.
   Only immutable-per-entity columns belong in it.

5. **ClickHouse wants few, large inserts.** Roughly one per second per table per shard, 10,000–100,000
   rows each. Everything upstream — collectors, Kafka, batching — exists to make that true. Alert on
   parts-per-partition at 100, not at the 150 throttle.

6. **Measure data properties, not just service properties.** Freshness, completeness, correctness. A
   pipeline can be 100% available, respond in 20ms, and serve data that's six hours stale and missing 4%
   of events, with every conventional dashboard green.

7. **Compute SLIs per tenant and report them as "proportion of tenant-minutes meeting objective."** A
   volume-weighted average hides exactly the failures that matter, because failures in a multi-tenant
   system are tenant-shaped.

8. **Alert on error-budget burn rate, not on thresholds.** Multi-window, multi-burn-rate: page at 14.4×
   and 6×, ticket below. It's the only formulation that gives fast detection without alert fatigue.

9. **Isolation needs five independent layers.** Credential-derived tenant identity, a data-access layer
   that can't omit the filter, database-enforced row policies, physical sharding, and per-tenant
   encryption keys. Do the arithmetic on single-layer failure probability; it's more likely than not
   over a year.

10. **Per-tenant encryption keys are what make deletion possible.** Crypto-shredding is how you satisfy
    GDPR erasure against immutable WORM archives and backups. Better still: pseudonymise at ingest so
    erasure becomes deleting one row from a mapping table rather than rewriting four million immutable
    records.

11. **Retention is control-plane data with a documented basis, enforced by a reconciler that exports
    drift as a metric.** A cron job that fails silently leaves no trace; a reconciler that fails leaves a
    rising number. That difference is the whole lesson of the Argus case study.

12. **Governance must be guardrails, not approvals.** Make the governed path the fastest path — dataset
    registration is how you *get* a table, monitoring, and access — and nobody routes around it. If
    creating a governed dataset takes two weeks and an ungoverned one takes an hour, you will keep
    finding orphaned 4 TB tables that run payroll.

13. **Every derived dataset needs a reconciliation check against its source.** Without one, it will
    eventually be silently wrong, and you'll find out from a customer three weeks later.

14. **Access pattern dominates size when choosing a store.** A 50 TB append-only log with point lookups is
    DynamoDB. A 200 GB table with heavy aggregation is ClickHouse. Ask what one query touches before you
    ask how big the table is.

15. **Migrate faithfully first, fix bugs second.** Combining migration with correction makes both
    unverifiable. And define "not disrupting" as five testable properties with metrics, plus a sixth that
    isn't a metric: no consumer is surprised.

16. **Extract primitives along axes you've seen vary, never along axes you predict** — except for
    correctness and security, which you deduplicate at the second copy because the failure mode is a
    breach rather than duplicated code. Prefer composition over configuration: a primitive keeps control
    with the caller; a framework takes it.

17. **Cloud cost is storage tiering, cross-AZ transfer, and committed-use discounts.** Most teams optimise
    instance types, which is the smallest of the three. Check for S3 traffic through a NAT gateway first;
    it's the most common six-figure mistake and the fix is free.

18. **Multi-cloud at data-platform volume means keeping data where it lands.** At 5.2 TB/day, moving data
    between clouds costs more than the compute. Portable collectors and a portable log, region-local
    storage and analytics, and only small aggregates crossing the boundary.

19. **Use an anti-corruption layer with every legacy system, and enforce it.** Their identifiers and
    encodings appear only inside the ACL — a testable invariant worth a CI check, because it's the
    property that erodes silently.

20. **Decouple the backend migration from the interface redesign.** Preserve the old contract byte-for-
    byte behind an adapter and ship the backend change with zero customer involvement. Coupling them
    blocks your migration on forty customers' roadmaps.

21. **Name what you gave up.** Every staff-level design question has a defensible trade, not a correct
    answer. State the decision, its cost, the condition that makes it right, and roughly what reversing
    it would cost. That's four separate signals in one sentence.

22. **Escalate honestly and early.** A retention violation you disclose is a finding with a remediation
    plan; one an auditor finds is a finding with a credibility problem attached. The graded behaviour in
    every compliance scenario is speed and accuracy of disclosure, not the cleverness of the fix.

