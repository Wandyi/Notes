# The Lake: Why There Is Storage Under the Warehouse, and the Small-File Problem That Ruins It

The outline in the question says "Load: store in data warehouses like Redshift, BigQuery, or
Snowflake." This document is about why, at Monarch's volume, that is half of a design — and why
the missing half is a layer of files in object storage that the warehouse reads from rather than
replaces.

Two forces create the lake:

1. **Price per terabyte.** Three hundred terabytes of history inside a warehouse costs between

   five and twenty times what it costs in S3, for data that is queried a handful of times a year.
2. **The things only raw retention can do.** Rebuilding a materialisation after a transform bug,

   reprocessing two years of history with a new definition, and answering "what did the record say
    on 3 March 2024" all require the original records, in their original form, addressable by date.
    A warehouse holding modelled tables cannot do any of the three.

---

## The naive version: load straight into the warehouse

Start with the design the outline implies. Kafka consumers write directly into Snowflake tables;
there is no S3 layer; the warehouse holds everything for seven years.

Work out what it costs. Snowflake storage is billed at roughly $23 to $40 per compressed TB per
month depending on contract and region. Using the lower end and Monarch's 300 TB:

```
300 TB × $23/TB-month = $6,900/month = $82,800/year
```

Against S3, derived in document 01:

```
hot 90 days in Standard + remainder in Glacier Instant = $1,825/month = $21,900/year
```

A difference of about $61,000 a year. Real money, but by itself not an architecture argument —
$61,000 is one engineer-month, and if the warehouse-only design were simpler it might be worth
it.

The architecture arguments are the other three:

**You cannot reprocess.** In February, someone discovers that the merchant-category
normalisation has been mapping a particular code wrong since launch. In a warehouse-only design
the normalised value is all you have; the original code was transformed on the way in and
discarded. The fix is not "re-run the transform" — there is nothing to re-run it over.

**You cannot serve a second engine.** Monarch wants an analytical store for sub-second dashboards
(document 06) and a Spark job for model training. Both now have to read through the warehouse,
paying warehouse compute prices for a full-table scan and competing with the reporting jobs for
the same resources.

**You cannot cheaply keep what you might need.** When all retention costs warehouse prices, the
rational response is to keep less. Teams drop raw columns, shorten retention, and pre-aggregate
aggressively — and then, three years later, cannot answer a regulator's question because the
column was dropped in a cost review.

So: **raw records land in object storage, and the warehouse is a modelled layer built on top of
it.** The warehouse keeps what warehouse queries need; the lake keeps everything.

---

## Why a table format, and not just Parquet files in folders

The first instinct is to write Parquet files into date-named prefixes:

```
s3://monarch-lake/auth_events/dt=2026-11-12/part-00001.parquet
```

and point a query engine at it. This is the Hive-style layout, it works, and it was the industry
default for a decade. At Monarch's scale it breaks in five specific ways, and each one is worth
knowing because they are the reasons table formats exist.

**There is no atomic commit.** A Spark job writing 540 files for a day's clickstream fails after
400. A query running concurrently sees 400 files — a partial day — and returns a wrong answer
with no error. There is no way to make a multi-file write appear all at once.

**Schema changes are positional and silent.** Parquet files carry their own schemas. Add a column
in the middle in March, and files from February and March have different layouts. Depending on
the engine and how it resolves columns, you get nulls, an error, or — the bad case — values read
from the wrong column because resolution was by position rather than name.

**The partition scheme leaks into every query.** If the table is partitioned by `dt`, a query
filtering on `event_time` does not prune partitions unless it *also* filters on `dt`. Every
analyst must know the physical layout, and the one who writes `WHERE event_time > '2026-11-01'`
without the `dt` predicate scans seven years. At Monarch's size that single mistake is a
four-figure query.

**Changing the partition scheme means rewriting the table.** You partitioned by day; at 278 GB a
day of clickstream you now want day plus hour. In Hive layout that is a full rewrite of 110 TB and
a coordinated change to every query.

**Listing is slow and sometimes wrong.** Finding the files for a query means listing S3 prefixes.
S3 listing is paginated, eventually consistent for some operations historically, and at hundreds
of thousands of objects it takes long enough to dominate query planning.

**Apache Iceberg** addresses all five by keeping a manifest — an explicit, versioned list of which
files constitute the table — rather than inferring the table from a directory listing.

| Problem | What Iceberg does |
|---|---|
| No atomic commit | A commit is a single atomic swap of the metadata pointer. Readers see the old snapshot or the new one, never a partial write |
| Silent schema drift | Columns have stable IDs; renames and reorders are metadata operations and cannot misalign data |
| Partition scheme leaks into queries | Hidden partitioning: the table records that `dt` is derived from `event_time`, so a filter on `event_time` prunes automatically |
| Repartitioning requires a rewrite | Partition evolution: change the scheme going forward; old data keeps its old layout and both are queryable |
| Slow, unreliable listing | Manifests list files explicitly, with per-file column statistics for pruning before any file is opened |

The one worth dwelling on for this interview is **snapshots**. Every commit creates a snapshot,
and a snapshot is queryable:

```sql
SELECT * FROM auth_events FOR TIMESTAMP AS OF '2026-03-03 14:00:00';
```

That is the compliance requirement from document 00 — "reproduce what the system knew on a past
date" — satisfied by the storage layer rather than by a convention someone has to maintain.
Document 09 builds on it.

The alternative, Delta Lake, solves the same problems with a transaction log instead of
manifests, and for this design the choice barely matters. Hudi is stronger for
high-frequency upserts, which matters for the CDC tables and not for the event tables. If asked,
say that and move on; a long comparison of table formats is not what is being tested.

---

## The small-file problem, which is where this goes wrong in practice

This is the single most common operational failure in a streaming-to-lake pipeline, and it does
not appear in any architecture diagram.

### How it happens

The lake writer is a Flink job consuming `app.clickstream.v1`. To keep the lake reasonably fresh
it commits every two minutes. It runs with 120 parallel writers, matching the partition
assignment.

Each writer, at each commit, writes at least one file per partition it is writing into. So:

```
commits per day      = 1,440 minutes / 2 = 720
parallel writers     = 120
files per day        = 720 × 120 = 86,400
average file size    = 277,900 MB / 86,400 = 3.2 MB
```

**Eighty-six thousand files a day, averaging 3.2 MB.**

Now the consequences, which compound:

```
files in a 30-day query  = 2,592,000
files in a 13-month scan = 34,214,400
```

A query over one month must open 2.6 million objects. Each `GET` has roughly 20–60 ms of latency
before a byte of data arrives, and although engines parallelise, the planning cost alone becomes
minutes. Parquet's advantages evaporate: a 3.2 MB file cannot hold a useful row group, so
column-level statistics are nearly worthless for pruning and compression ratios fall because
dictionaries are built over too few rows.

And the cost is not only time:

```
S3 GET requests at $0.0004 per 1,000:
  2,592,000 requests × $0.0004/1000 = $1.04 per full-month scan
```

That is small per query. But Iceberg metadata operations, manifest reads, and repeated planning
multiply it, and a dashboard that re-plans this query every five minutes for a working day runs
it ninety-six times. The real damage is in query planning time, not request fees — but it shows up
in both.

### The fix, and why it has to be a separate job

Two mechanisms, both needed.

**Commit less often where you can.** The clickstream's freshness SLO is 60 minutes (document 01),
not 2 minutes. Committing every 15 minutes instead:

```
commits per day   = 96
files per day     = 96 × 120 = 11,520
average file size = 277,900 MB / 11,520 = 24.1 MB
```

A 7.5× improvement for a configuration change, bought with 13 extra minutes of staleness that
nobody wanted anyway. **Check the SLO before tuning anything** — this is free and routinely
missed. Note that money streams keep tighter commits because their lake freshness SLO is 15
minutes; the clickstream was the one paying for freshness it did not need.

**Compact on a schedule.** A background job rewrites small files into large ones, as an Iceberg
commit that replaces the file set atomically. Readers are unaffected — they see either the old
snapshot or the new one.

Target file size is a real decision. Monarch uses **512 MB**:

```
clickstream, after compaction = 277,900 MB / 512 MB = 543 files per day
auth.events, after compaction =  20,500 MB / 512 MB =  40 files per day
```

Why 512 MB rather than 128 MB or 2 GB:

- Too small and you are back to planning overhead and weak statistics.
- Too large and a query touching one row must read an entire file's row group, and parallelism
suffers because a file is the unit of read assignment — 40 files a day cannot be read by 200
workers usefully.
- 512 MB holds roughly 8 to 16 row groups of 32–64 MB, which gives statistics fine-grained enough
to prune within the file while keeping the file count manageable.

Compaction cost, derived:

```
daily data to compact        365 GB
EMR/Spark throughput         ≈ 110 GB per node-hour for read-rewrite of Parquet
node-hours required          365 / 110 = 3.3
on r6g.4xlarge spot (~$0.31/hr)
daily cost                   3.3 × $0.31 ≈ $1.03
```

**About a dollar a day to keep the lake queryable.** The job that nobody puts in the architecture
diagram is also the cheapest thing in it, and the one whose absence degrades every query in the
system.

Two operational notes that matter more than the cost:

**Compaction must be monitored as a pipeline, not a chore.** When it silently stops — a common
failure, usually a permissions change — nothing breaks immediately. Query times degrade over
weeks, and by the time someone investigates there are 40 million files. Monarch alerts on
*average file size per partition* falling below 64 MB, which catches a stopped compactor within a
day.

**Old snapshots must be expired.** Compaction does not delete the small files; it writes new ones
and commits a new snapshot. The old files remain, because the old snapshot still references them
and time travel still works. Without a snapshot-expiry job, storage grows at roughly twice the
rate of ingestion forever. Monarch expires snapshots older than 30 days for clickstream and 7
years for money streams — which is the same retention tiering from document 01, applied one layer
down.

---

## Table layout: three zones, and what earns a zone

The common vocabulary is bronze, silver and gold, which communicates nothing to someone who has
not heard it. Monarch names its zones after what they guarantee.

`raw` **— exactly what arrived, nothing applied.**

One table per topic, one row per event, every field as received. The only additions are
`ingest_time` and the Kafka coordinates (topic, partition, offset) so any row is traceable to its
origin. No type coercion beyond what Avro already enforced, no normalisation, no filtering,
no deduplication.

This zone exists to be the thing you reprocess from, so its one rule is **append-only, never
corrected.** If a producer sent a wrong value, the wrong value stays in `raw` forever. Correcting
it would destroy the only record of what was actually received, and that record is precisely what
a dispute or an examination turns on.

`conformed` **— correct, deduplicated, typed, j]**

**oined to reference data.**

Here the work happens: duplicates removed by `transaction_id`, amounts converted to integer minor
units, merchant IDs joined to the merchant reference table, timestamps normalised to UTC with the
original offset retained, late-arriving events merged into their correct event-date partitions.

One table per business entity rather than per topic: `fact_authorization` joins `auth.events` and
`risk.decisions`, because by this point they are one thing analytically.

This zone is **rebuildable from** `raw` **by design**, and Monarch tests that claim quarterly by
rebuilding a month of it and comparing. A rebuild path that has never been exercised is not a
rebuild path.

`curated` **— shaped for a specific consumer, aggregated, and disposable.**

Daily merchant summaries, account-month statements, the regulatory extract tables, the feature
tables used for model training. These are the things the warehouse and the dashboard store read.
Each is a query over `conformed`, materialised because running it live would be too slow or too
frequent.

Nothing in `curated` is a source of truth. All of it can be dropped and rebuilt.

The reason to name zones by guarantee rather than by colour is that it makes the rules
enforceable. "Is it acceptable to update this row?" has an answer: never in `raw`, only by
rebuild in `conformed`, freely in `curated`.

---

## Partitioning: what to partition by, and the trap

Partition columns determine what can be pruned. The rule is to partition by **what queries filter
on and what retention operates on** — and those are usually the same thing, which is convenient.

| Table | Partitioning | Reasoning |
|---|---|---|
| `raw.auth_events` | `days(event_time)` | 20.5 GB/day compressed → 40 files/day at 512 MB. Day granularity is right |
| `raw.clickstream` | `days(event_time)`, `hours(event_time)` | 278 GB/day is too much for one partition; hourly gives 11.6 GB and 23 files per hour |
| `conformed.fact_authorization` | `days(event_time)`, `bucket(16, account_id)` | Dispute and account-history queries filter by account; bucketing lets them read 1/16th of a day |
| `curated.merchant_daily` | `days(business_date)` | Small; queried by date range |
| `raw.core_cdc_accounts` | `days(ingest_time)` | CDC is queried by *when the change was captured*, not when the account was created |

Three things in that table are decisions rather than defaults.

**Clickstream gets hourly partitions and authorizations do not.** The guide is partition size: aim
for 1–10 GB per partition. At 20.5 GB/day, authorizations are already near the top of that range
with daily partitions; at 278 GB/day, clickstream needs hourly to get to 11.6 GB. **Partition
granularity follows volume, so two tables in the same lake legitimately have different
granularity** — a uniform policy is the wrong answer here.

`fact_authorization` **is bucketed by** `account_id` **as a second partition dimension.** The most
frequent non-reporting query against this table is "every authorization for account X in a date
range," which backs dispute investigation and customer service. Without bucketing, that query
reads every file in every day of the range. With 16 buckets it reads a sixteenth. Why 16 and not
256: each bucket within each day must still produce files of a useful size.

```
20.5 GB/day ÷ 16 buckets = 1.28 GB per bucket-day → ~3 files of 512 MB
20.5 GB/day ÷ 256 buckets = 80 MB per bucket-day → 1 small file, back to the small-file problem
```

**Bucket count is bounded below by file size, not chosen for selectivity alone.** This is the trap
that catches people: more buckets look strictly better for pruning, and past a point they
reintroduce the exact problem compaction exists to solve.

**CDC is partitioned by** `ingest_time`**, everything else by** `event_time`**.** An account row's
`created_at` might be from 2014; the *change* to it was captured today. Queries ask "what changed
yesterday," so `ingest_time` is what prunes. Getting this backwards produces a table where every
query scans twelve years of partitions to find yesterday's changes.

---

## Reprocessing, which is the capability the lake exists for

The scenario from the start of this document: in February, the merchant-category normalisation is
found to have been wrong since launch. 

With `raw` intact, the procedure is mechanical:

1. **Fix the transform, and version it.** The new code is `conform-authorization v7`. Version

   numbers are recorded in the output, so a row can always be attributed to the code that made it.
2. **Rebuild into a new branch, not over the live table.** Iceberg branching lets

   `conformed.fact_authorization` have a `reprocess-v7` branch written in parallel while the main
    branch keeps serving queries.
3. **Compare before promoting.** Row counts per day must match exactly. The columns that should

   not have changed must be identical. The MCC column should differ on exactly the rows with the
    affected codes, and that count should match the independently-computed expectation.
4. **Promote by fast-forwarding the branch**, which is one atomic metadata commit. Queries

   in flight finish against the old snapshot; new queries see the new one.
5. **Keep the old snapshot for the full retention window** — because if the old values were used

   in a report sent to a partner or a regulator, you must still be able to reproduce that report
    exactly as sent.

Step 5 is the one that distinguishes a bank from a product company. Elsewhere the fixed data
simply replaces the wrong data. Here, **the wrong data was the basis of statements that went to
customers and filings that went to regulators, and those statements are not retroactively
reissued.** The record of what you said must survive the correction of what was true. Document 09
is about the implications.

Cost of a full 13-month clickstream reprocess, so the number is in hand:

```
data to read and rewrite    110 TB
Spark throughput             ≈ 110 GB per node-hour
node-hours                   110,000 / 110 = 1,000
r6g.4xlarge spot at $0.31/hr
total                        1,000 × $0.31 = $310
wall clock at 200 nodes      5 hours
```

**Three hundred and ten dollars and five hours to reprocess thirteen months of history.** Knowing
that number changes how you make decisions — reprocessing stops being a last resort and becomes an
ordinary tool, which is exactly the posture you want. The design that makes it cheap is the one
where `raw` exists and is never corrected.

---

## What to take away

1. **The lake exists for three reasons beyond price**: reprocessing after a transform bug, serving

   engines other than the warehouse, and being able to afford to keep columns you might need in
    seven years. Price alone ($61,000/year here) would not justify it.
2. **Use a table format, not Parquet in date folders.** Iceberg gives atomic multi-file commits,

   ID-based schema evolution, hidden partitioning so queries prune without knowing the layout,
    partition evolution without a rewrite, and manifest-based planning instead of S3 listing.
3. **Snapshots are the compliance feature.** `FOR TIMESTAMP AS OF` turns "reproduce what the

   record said on 3 March 2024" into a query rather than a project.
4. **The small-file problem is the most common operational failure in this architecture.** A

   2-minute commit interval across 120 writers produces 86,400 files a day at 3.2 MB each and 2.6
    million files in a one-month query.
5. **Check the freshness SLO before tuning anything.** Moving the clickstream from 2-minute to

   15-minute commits is a 7.5× file-count improvement that costs staleness nobody wanted.
6. **Compact to 512 MB files on a schedule, and expire old snapshots.** Compaction costs about a

   dollar a day; without snapshot expiry, storage grows at twice the ingestion rate forever.
7. **Alert on average file size per partition falling below 64 MB.** A stopped compactor breaks

   nothing immediately and degrades everything over weeks.
8. **Three zones named for what they guarantee**: `raw` is append-only and never corrected,

   `conformed` is rebuildable from `raw` and that rebuild is tested quarterly, `curated` is
    disposable.
9. **Partition granularity follows volume** — daily for 20 GB/day authorizations, hourly for 278

   GB/day clickstream — and **bucket counts are bounded below by file size**, not chosen purely
    for selectivity.
10. **Partition CDC by capture time, not by the business timestamp of the row**, or every query

    scans the entire history to find yesterday's changes.
11. **A full 13-month reprocess costs $310 and five hours.** When reprocessing is that cheap it

    becomes an ordinary tool, and the only reason it is that cheap is that `raw` exists and is
      never corrected in place.
