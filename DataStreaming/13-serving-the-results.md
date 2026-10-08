# Serving the Results

Scenarios `Q-01` … `Q-10`.

A streaming pipeline that nobody can query is a very expensive way to heat a data centre. This
document is about the last hop: where results land, what that store adds to your latency, and
the half-dozen ways a healthy pipeline produces a stale or wrong dashboard.

The framing that makes the decisions tractable: **the sink is not a detail at the end of the
pipeline; it determines your delivery guarantee ([09](09-delivery-guarantees-and-exactly-once.md)),
a large fraction of your latency, and whether the results can be corrected.** Choose it early.

## 1. Five kinds of serving, and what each is for

### 1. Another stream

The result goes back onto Kafka. Not really "serving" — it is composition — but it is the most
common sink on Meridian, and the right default when the consumer is another job.

Latency added: ~10 ms. Guarantee: 2PC available. Queryable: no.

### 2. A key-value store — "what is the current value for this key?"

Redis, DynamoDB, Cassandra. Point lookups by key, sub-millisecond, very high QPS.

Use for: feature stores, real-time personalisation, current-state lookups, rate limiters, the
live driver position. Anything where the query is "give me the row for key K."

Latency added: 1–3 ms. Guarantee: idempotent upsert (Method A). Queryable: by key only.

### 3. A real-time OLAP store — "aggregate over a filtered slice"

ClickHouse, Druid, Pinot. Columnar, high ingest rate, sub-second scans over billions of rows,
arbitrary `GROUP BY`.

Use for: dashboards, ad-hoc analysis, anomaly detection, anything with a `WHERE` and a
`GROUP BY` over recent data.

Latency added: 0.2–10 s depending on batching. Guarantee: dedupe key (Method B). Queryable:
richly.

### 4. A lakehouse table — "everything, forever, for batch"

Iceberg, Delta Lake, Hudi on object storage. Cheap, large, transactional metadata, read by Spark
/ Trino / anything.

Use for: the system of record, reprocessing source, ML training data, long-retention compliance.

Latency added: minutes. Guarantee: 2PC via atomic metadata commit (Method C, and cheap here).
Queryable: by batch engines, not interactively at low latency.

### 5. A search index — "find the documents matching this"

Elasticsearch, OpenSearch. Full-text, faceted, relevance-ranked.

Use for: log search, entity search, anything with a text query.

Latency added: refresh interval, typically 1–30 s. Guarantee: idempotent by document ID.

### The mistake to avoid

**Using one store for all five.** Every team eventually tries to make ClickHouse do key-value
lookups, or Elasticsearch do analytics, or Redis hold a year of history. Each of those works up
to a point and then fails expensively.

Meridian's rule is that a derived dataset declares its **access pattern** in its contract, and
the access pattern picks the store:

| Access pattern | Store |
|---|---|
| Point lookup by key, > 10,000 QPS | Key-value |
| Filter + group-by over < 90 days | Real-time OLAP |
| Full scan, joins, > 90 days | Lakehouse |
| Text or fuzzy match | Search index |
| Another job consumes it | Kafka |

A dataset with two access patterns goes to two stores, fed by two sinks from the same job. That
is not duplication to be eliminated; it is the correct design, and trying to avoid it is how you
end up with a store doing something it is bad at.

## 2. Freshness: the number that gets quoted wrong

From [01](01-requirements-and-the-capacity-model.md) Section 10, the formula for a windowed
result:

```
worst-case freshness = window length
                     + watermark delay
                     + sink flush interval
                     + serving-store visibility
```

Meridian's four serving paths, each fully costed:

| Path | Window | Watermark | Sink flush | Store visibility | **Worst-case** |
|---|---|---|---|---|---|
| Live driver map → Redis | none | 10 s | 0.2 s | 0.001 s | **10.2 s** |
| Corridor 1-min metrics → ClickHouse | 60 s | 8 s | 1 s | 0.2 s | **69.2 s** |
| Riverbend order state → DynamoDB | none | 0.25 s | 0.05 s | 0.005 s | **0.31 s** |
| Corridor events → Iceberg | none | 8 s | 300 s | 30 s (metadata refresh) | **338 s** |

The Iceberg row is the one that surprises people. The pipeline latency is 36 milliseconds; the
observable freshness is five and a half minutes, and 300 of those seconds are a commit interval
chosen to avoid the small-file problem (Section 4).

**Publish the worst-case number, not the pipeline's number.** The pipeline's 36 ms is true and
useless; the 338 s is what a user experiences.

### The freshness contract

Every derived dataset on Meridian carries one, and it has four parts because "how fresh is it"
has four different answers:

```
Dataset:    corridor_metrics_1m (ClickHouse)
Grain:      one row per (window_start, surface, country)

Freshness
  p50 lag from event to queryable:        9.4 s
  p99:                                   71 s
  worst case (oldest event in a window):  69.2 s + 8 s tail = 77 s

Completeness
  provisional rows appear at window close + 8 s
  rows become final after allowed lateness (60 s)
  the `completeness_class` column says which

Correctness
  at-least-once pipeline, deduplicated by ReplacingMergeTree on
    (window_start, surface, country)
  queries MUST use the deduplicating view, not the raw table

Availability
  99.9% of minutes have a row within 90 s
  during a pipeline incident, rows stop appearing; they do not become wrong
```

That last line is the important one and it is a design commitment, not a description. **The
platform's failure mode is stale, not wrong.** A consumer can detect staleness (the newest
window is old); it cannot detect wrongness. Every choice in this document leans toward making
failures show up as missing data rather than as plausible data.

## 3. Real-time OLAP: ClickHouse, Druid, Pinot

All three do "aggregate over a filtered slice of recent data, fast." They differ in ways that
matter for a streaming sink.

| | ClickHouse | Druid | Pinot |
|---|---|---|---|
| Streaming ingest | Via `async_insert` or Kafka engine | Native Kafka indexing service, exactly-once | Native Kafka, upsert support |
| Exactly-once from Kafka | No — dedupe via `ReplacingMergeTree` | **Yes**, built into the indexing service | Yes, with upsert |
| Ingest latency | ~1 s | ~5–30 s (segment handoff) | ~1–5 s |
| Upserts | `ReplacingMergeTree`, merged lazily | Poor — append-oriented | **Native upsert**, first-class |
| Query flexibility | Full SQL, joins | Limited joins, lookups only | Limited joins |
| Late data | Natural — just insert | Requires reindexing the segment | Natural |
| Ops burden | Moderate | High (many node types) | Moderate-high |
| Best at | Flexible SQL, high cardinality | Time-series rollups at huge scale | Upserts + low-latency user-facing |

Meridian uses **ClickHouse** for the derived-metrics tier, for two reasons: it accepts late data
by simply inserting it, and its SQL is flexible enough that most ad-hoc questions do not require
a new pipeline. Druid's exactly-once Kafka ingestion is genuinely nicer, and its handling of
late data — requiring the affected segment to be reindexed — is genuinely worse for a platform
where lateness is routine.

The deep material on both is in
[`../../system-design-notes/Clickhouse`](../../../system-design-notes/Clickhouse/README.md) and
[`../../system-design-notes/Druid`](../../../system-design-notes/Druid/README.md).

### The ClickHouse gotchas a streaming sink hits

**Too many parts.** Each insert creates a part; merges consolidate them. Above roughly 100
inserts/s per table, merges fall behind and ClickHouse starts rejecting writes with "Too many
parts." The fix is batching (`B-03`): 100,000 rows per insert, or `async_insert` with
`wait_for_async_insert=0` and a 1-second buffer.

⚠️ **`ReplacingMergeTree` deduplicates only within a part, and only after a merge.** A query
without `FINAL` (or without a deduplicating view) sees duplicates for as long as merges take,
which under load is minutes. The standard pattern is a view:

```sql
CREATE VIEW corridor_metrics_1m AS
SELECT window_start, surface, country,
       argMax(value, version) AS value,
       argMax(completeness_class, version) AS completeness_class
FROM corridor_metrics_1m_raw
GROUP BY window_start, surface, country;
```

⚠️ **Never put a mutable column in the `ORDER BY` of a `ReplacingMergeTree`.** The `ORDER BY` is
the deduplication key; if a column in it changes between two versions of the same logical row,
they are two different rows and deduplication silently stops working. This is a known, quiet,
data-corrupting mistake and it is recorded in the ClickHouse notes referenced above.

## 4. The lakehouse sink and the small-file problem

Writing a stream to Iceberg looks simple and has one dominant failure mode.

A streaming writer commits on every checkpoint. With 360 subtasks and a 60-second checkpoint
interval:

```
360 files per commit × 1,440 commits/day = 518,400 files/day
each file: 607 MB/s ÷ 360 subtasks × 60 s = 101 MB   ← actually fine
```

That arithmetic works because Corridor's volume is large. Now a smaller stream:

```
riverbend.orders.lifecycle: 640/s × 1,400 B = 0.9 MB/s
360 subtasks × 60 s → each file is 0.9 MB × 60 ÷ 360 = 150 KB
518,400 files/day of 150 KB
```

Half a million 150-kilobyte files a day. Consequences:

- **Query planning becomes the query.** Trino listing 500,000 files to read 75 GB spends more
  time in planning than in scanning.
- **Metadata grows without bound.** Iceberg's manifest files track every data file; the manifest
  list itself becomes large.
- **S3 request costs.** 518,400 PUTs/day is $2.60/day for that one table, plus the GETs on every
  read.

**Three fixes, used together:**

**Reduce writer parallelism.** The sink does not need the job's parallelism. A `rebalance()` to
24 writers before the sink gives files 15× larger, at the cost of a shuffle.

**Lengthen the commit interval.** A 5-minute checkpoint for a lake-sink job (they can have their
own interval) gives files 5× larger again. This is where the 300-second flush in Section 2's
table comes from, and why the Iceberg path's freshness is five minutes.

**Run compaction.** A scheduled job that rewrites small files into 128–512 MB ones and expires
old snapshots. This is not optional; it is part of operating a lakehouse table, and a table
without it degrades steadily. Meridian runs compaction hourly per table.

Target: **128 MB files**, which is large enough for efficient scans and small enough for
parallel reads.

## 5. Scenarios

### Q-01 — The pipeline is healthy and the dashboard is stale

**What you see.** Every Flink metric is green. The dashboard's newest data point is 40 minutes
old.

**Mechanism.** The break is downstream of the pipeline:

- The sink connector is failing and retrying silently.
- ClickHouse merges are behind and the deduplicating view is timing out.
- A materialised view in the serving store is refreshing on a schedule that broke.
- The dashboard's own caching layer.
- The query has a `WHERE date = today()` and it is now tomorrow in UTC.

**Confirm it.** A **freshness probe on the serving store, not on the pipeline**:

```sql
SELECT max(window_start) FROM corridor_metrics_1m
```

compared against `now()`. This single query is the most valuable monitoring in this document,
because it measures what users experience rather than what the pipeline reports.

**Prevent.** Alert on serving-store freshness for every Tier 1 dataset, independent of pipeline
metrics. Meridian runs one probe per dataset per minute and alerts at 3× the contracted p99.

### Q-02 — The dashboard is fresh and wrong

**What you see.** A count that is 4× too high.

**Mechanism.** Usually one of `W-02` (multiple firings into an append-only sink), the missing
`FINAL` on a `ReplacingMergeTree`, or a join grain error (`J-04`).

**Prevent.** The deduplicating view pattern (Section 3), and a daily reconciliation against a
batch recomputation (`W-07`).

### Q-03 — Provisional results treated as final

**What you see.** An automated process reads the newest window, acts on it, and the window is
later corrected by late data.

**Mechanism.** `E-06`'s `completeness_class` exists and nobody filtered on it.

**Prevent.** Two tables or two views: `..._provisional` and `..._final`, with the final one
excluding windows still inside their allowed-lateness period. Make the safe thing the default
name:

```sql
CREATE VIEW corridor_metrics_1m AS          -- the plain name is the SAFE one
  SELECT * FROM corridor_metrics_1m_all WHERE completeness_class = 'final';

CREATE VIEW corridor_metrics_1m_realtime AS -- the risky one has a qualifier
  SELECT * FROM corridor_metrics_1m_all;
```

**Naming is a control.** If the unqualified name is the fast-and-approximate one, people will use
it by accident; if it is the correct one, they have to ask for the approximation deliberately.

### Q-04 — Serving store ingest is the pipeline bottleneck

Covered as `B-03`. The addition here: **measure the sink's capacity independently before
designing the pipeline around it.** A 15-minute load test against ClickHouse with realistic
batch sizes tells you the parallelism and batching your sink needs, and it is the cheapest
de-risking available.

### Q-05 — Query load affects ingest

**What you see.** Dashboard queries slow down during ingest spikes, and ingest slows down during
heavy queries.

**Mechanism.** Most OLAP stores share resources between ingest and query. ClickHouse merges
compete with queries for I/O and CPU; Druid's historicals serve queries while the middle
managers ingest, which separates them better.

**Prevent.**

- **Separate the tiers where the store allows it.** ClickHouse Cloud's separate compute for
  ingest and query, or a self-managed split with a replicated table where one replica takes
  writes and others serve reads.
- **Resource groups / query quotas** so one expensive ad-hoc query cannot starve ingest.
- **Push the heavy aggregation into the pipeline.** A query scanning 4 billion rows to compute
  a daily total is better served by a pipeline that maintains the daily total. This trades
  flexibility for cost and it is usually the right trade for the dashboards people look at every
  day.

### Q-06 — The same question answered two ways gives two answers

**What you see.** The real-time dashboard and the daily report disagree by 0.4%.

**Mechanism.** `W-07`'s five causes. But there is a structural version too: the real-time path
and the batch path are **different code**, and different code computes different things.

**Prevent.** The single most effective fix is to **make the batch path a replay of the streaming
path** rather than a separate implementation
([17](17-reprocessing-backfill-and-the-lambda-question.md)). When they are the same code, the
only remaining differences are lateness and duplicates, both of which are measurable.

Where that is not possible, **publish both numbers with their definitions** and reconcile daily.
A known, explained, monitored 0.4% is a fact; an unknown one is an outage of trust.

### Q-07 — Serving a query that spans streaming and historical data

**What you see.** "Show me this metric for the last 90 days." The streaming store has 30 days;
the lake has 400.

**Mechanism.** Two stores, one question.

**Prevent.** Three patterns, in increasing order of effort:

**Union at query time.** A view that reads the OLAP store for recent data and the lake for older,
with a cutover date. Works; the join point needs care so rows are neither duplicated nor missed:

```sql
CREATE VIEW metrics_all AS
  SELECT * FROM clickhouse.metrics WHERE window_start >= '2026-09-01'
  UNION ALL
  SELECT * FROM iceberg.metrics  WHERE window_start <  '2026-09-01';
```

The cutover date must be **before** the OLAP store's retention boundary and **after** the lake's
completeness watermark, or you get a gap or an overlap.

**A federating query engine.** Trino can query both and hide the union. More infrastructure, less
per-dataset work.

**Keep everything in one store.** ClickHouse with tiered storage (hot on SSD, cold on S3) can
hold 400 days in one table. Simplest for the consumer, and the cold-tier query latency is much
worse — 10–60 s instead of 0.3 s — which is usually acceptable for a 90-day query.

Meridian uses the third for datasets under 50 TB and the first above.

### Q-08 — A key-value sink with no TTL

**What you see.** A Redis instance holding the "current position" of every driver ever seen,
including 40 million who have not driven in two years.

**Mechanism.** Upserts, no expiry.

**Prevent.** TTL on every key in a serving store fed by a stream, sized to the access pattern.
Waypoint's driver positions expire in 10 minutes; a driver who has not reported in 10 minutes is
not "at their last position", they are *offline*, and the absence of a key is a more honest
representation than a stale value.

That reframing is general: **an expired key is information.** A stale one is a lie with a
timestamp.

### Q-09 — Serving-store schema drift

**What you see.** A new field was added to the stream. The ClickHouse table does not have it, so
the sink silently drops it — or fails, depending on configuration.

**Mechanism.** The stream's schema evolves ([03](03-the-event-schema-and-contracts.md)); the
serving store's does not, because it is a separate DDL change.

**Prevent.**

- **Generate the serving-store DDL from the stream's schema**, so a schema change produces a
  migration automatically.
- **Or use a schemaless landing column**: a `Map(String, String)` for anything not yet
  materialised, with the same 60-day promotion rule as `S-06`'s open map.
- **Fail loudly on unknown fields** rather than dropping them, at least in staging. A sink that
  silently drops fields will drop an important one eventually.

### Q-10 — The serving store is the system of record

**What you see.** A serving store's data cannot be rebuilt because the pipeline that produced it
has changed six times and the source retention is 7 days.

**Mechanism.** Someone treated a derived, disposable store as authoritative.

**Prevent.** **Be explicit about which store is the system of record**, and make sure it is one
that can be rebuilt or that is itself authoritative. On Meridian:

```
Kafka (7 d)        → recoverable, short
Iceberg lake (400 d) → the system of record for derived data
ClickHouse (30 d)  → disposable; rebuilt from the lake in ~4 h
Redis (10 min TTL) → disposable; rebuilt from the stream in seconds
```

The property to verify — by actually doing it, not by reasoning about it — is that **every
disposable store can be rebuilt within its stated time**. Meridian rebuilds one ClickHouse table
from the lake quarterly as a drill, which is how they know it is four hours and not the two they
assumed.

## 6. Where the aggregation should happen

A recurring design argument: should the pipeline compute the aggregate, or should the serving
store?

**Pipeline-side aggregation.** The job computes per-minute counts and writes them. Storage is
small, queries are trivial, cost is low. But the aggregation is fixed — a new dimension means a
new pipeline and a backfill.

**Store-side aggregation.** The job writes raw events; the store aggregates at query time.
Completely flexible, and expensive: storing 345.6 billion raw events a day and scanning them
repeatedly.

**The middle.** Write a **pre-aggregated but still granular** dataset — the finest grain anyone
will query, aggregated no further. For Corridor that is
`(minute, surface, country, device_class)`, which reduces 1.85 million events a second to about
96,000 rows a second:

```
raw:      1,850,000 rows/s × 640 B = 1,184 MB/s
rollup:      96,000 rows/s × 180 B =   17.3 MB/s    ← 68× reduction
```

and any query grouping by a subset of those dimensions is a simple re-aggregation of the rollup.

**The rule:** aggregate in the pipeline down to the finest grain anyone queries, and let the
store do the rest. The 68× reduction pays for itself immediately, and the flexibility loss is
bounded by a dimension list you can extend with a schema change rather than a rewrite.

The thing that breaks this: **non-decomposable aggregations**. A count of distinct members
cannot be re-aggregated from per-minute distinct counts. Emit a HyperLogLog sketch instead of a
number, and the store merges sketches — `uniqCombined` state in ClickHouse, `HLLSketch` in
Druid. 12 KB per row instead of 8 bytes, and the query works.

## What to take away

1. **The sink determines your guarantee, much of your latency, and whether results can be
   corrected.** Choose it early, not at the end.
2. **Five serving shapes — stream, key-value, real-time OLAP, lakehouse, search — and a dataset
   with two access patterns needs two stores.** That is correct design, not duplication.
3. **Publish worst-case freshness, not pipeline latency.** Meridian's Iceberg path has 36 ms of
   pipeline and 338 s of observable freshness, and only the second is what a user experiences.
4. **The freshness contract has four parts**: freshness, completeness, correctness, and
   availability — with the commitment that the failure mode is stale, not wrong.
5. **`ReplacingMergeTree` deduplicates only within a part after a merge**, so queries need the
   deduplicating view — and never put a mutable column in its `ORDER BY`, which silently breaks
   deduplication.
6. **The small-file problem is the lakehouse sink's dominant failure.** Reduce writer
   parallelism, lengthen the commit interval, and run compaction — all three, targeting 128 MB
   files.
7. **Probe freshness on the serving store, not on the pipeline.** `SELECT max(window_start)` is
   the single most valuable query in this document.
8. **Name the safe view without a qualifier.** If the unqualified name returns provisional data,
   people will use it by accident.
9. **An expired key is information; a stale one is a lie with a timestamp.** Put a TTL on every
   key in a stream-fed serving store.
10. **Be explicit about which store is the system of record**, and verify by drill that every
    disposable store can be rebuilt within its stated time.
11. **Aggregate in the pipeline to the finest grain anyone queries** — a 68× reduction for
    Corridor — and emit sketches rather than numbers for anything that cannot be re-aggregated.
