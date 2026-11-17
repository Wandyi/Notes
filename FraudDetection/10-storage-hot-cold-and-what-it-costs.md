# Storage: 281 Gigabytes You Must Read in Three Milliseconds, and 29 Terabytes a Year You Must Keep for Seven

The usual presentation of fraud-detection storage is a two-line summary: hot data in Redis for
real-time scoring, cold data in a warehouse for training and reporting. That is directionally
right and it hides every decision that matters — which store, keyed how, replicated how, costing
what, and what happens when it restarts.

This document works through the four storage tiers a fraud system actually has, sizes each from the
figures in doc [01](01-the-running-systems-and-their-numbers.md), and puts a number on the bill, so
that "the cost of maintaining hot storage for millions of users" is a quantity rather than a
concern.

Scenarios are `S-01` through `S-10`.

---

## There are four tiers, not two

| Tier | Holds | Size | Read latency | Written by | Read by |
|---|---|---|---|---|---|
| **Online features** | 200.9M entity keys × 1.4 KB of packed aggregates | 281 GB | p99 under 3 ms | Stream processor, 37,638 writes/s | Scoring path, 2,916 reads/s |
| **Operational** | Open cases, block lists, allow-lists, active rules, model artefacts, thresholds | 40 GB | p99 under 10 ms | Analysts, control plane | Scoring path, case UI |
| **Decision log** | Every decision with all 412 feature values, model and rule versions | 80 GB/day, 29 TB/year | seconds (analytical) | Scoring path, asynchronously | Training, monitoring, disputes, audit |
| **Archive** | Raw events and decisions for the regulatory retention period | 7 years, 206 TB | minutes | Lifecycle policy | Audit, legal, rare backfills |

The two-tier framing collapses the middle two, which are the ones with the interesting properties.
The operational tier is small, mutable, and *correctness-critical in a way the feature store is not*
— a stale velocity count costs you some accuracy, while a stale block list is a compliance failure.
The decision-log tier is enormous, append-only, and the thing everything downstream depends on.

---

## The online feature store: choosing it, and the cost

### What the workload actually is

From doc [05](05-the-streaming-pipeline.md):

```
Reads:   2,916 key reads/s average, 23,328 at the seasonal peak
Writes:  37,638 bucket writes/s average, 301,104 at the seasonal peak
Ratio:   writes are 12.9× reads
State:   281 GB logical, 379 GB with overhead, 1.14 TB at 3 replicas
Latency: p99 under 3 ms for a multi-get of 6 keys
Durability: loss of state costs 2 hours of rebuild (doc 05, P-01), not data loss
```

**The write-heaviness is the defining characteristic and it eliminates several popular answers.**
A store optimised for read-mostly caching behaves badly at 300,000 writes per second. So does
anything that persists every write synchronously to a replicated log.

### The candidates, assessed honestly

**Redis.** In-memory, single-threaded per shard, sub-millisecond, extremely well understood.
Works, and the considerations are: memory cost (281 GB logical becomes roughly 420 GB resident
after allocator overhead for this access pattern), the need for cluster mode and therefore
resharding operations, and that persistence options are either slow (AOF with frequent fsync) or
lossy (RDB snapshots). For a workload where losing state costs a two-hour rebuild rather than
losing money, **lossy persistence is the correct choice**, which makes Redis a better fit here than
it is for the operational tier.

**Aerospike or a similar hybrid-memory store.** Keeps the index in memory and the data on NVMe.
This is the purpose-built answer for exactly this shape — hundreds of gigabytes, millions of keys,
write-heavy, single-digit-millisecond — and it is what a large fraction of real fraud platforms
run. The cost advantage is substantial because SSD is roughly a tenth the price of memory, and the
latency penalty at p99 is a fraction of a millisecond.

**DynamoDB or a managed key-value service.** Operationally simplest, and the pricing model is the
problem: at 301,104 writes per second of a small item, on-demand pricing is enormous, and
provisioned capacity for the seasonal peak means paying for 8× what you use for 364 days. Viable
where the write rate is a tenth of this; expensive here.

**Cassandra or ScyllaDB.** Write-optimised by design — a log-structured merge tree is the right
shape for this — and horizontally scalable. The caution is read latency at p99 under compaction,
which is exactly when you least want it, and the operational weight of running it well.

**A relational database.** Not viable at 300,000 writes per second of 200 million keys, and the
reason is worth stating because it is instructive: this workload has no joins, no transactions
across keys, no range scans, and no secondary indexes. Everything a relational engine provides is
unused, and everything it costs is paid.

**Ironvale runs Aerospike for the online features and Redis for the operational tier**, which is a
common and defensible split: hybrid memory where the data is large and loss-tolerant, in-memory
where the data is small and latency matters most.

### S-01 · The cost, derived

The question "what does hot storage for 70 million users cost?" deserves a number. Two
configurations, both sized for the 8× seasonal peak:

**All in memory (Redis cluster):**

```
Resident memory needed:     420 GB × 3 replicas = 1.26 TB
Instance type:              memory-optimised, 128 GiB usable per node
Nodes:                      1.26 TB / 128 GiB = 10 nodes, round to 12 for headroom
Cost per node:              roughly $1,150/month on-demand, $690 with a 1-year commitment
Monthly:                    12 × $690 = $8,280
Annual:                     $99,400
```

**Hybrid memory (Aerospike, index in RAM, data on NVMe):**

```
Index:                      200.9M keys × 64 bytes = 12.9 GB, × 3 replicas = 38.6 GB
Data on NVMe:               379 GB × 3 = 1.14 TB
Instance type:              NVMe-backed, 64 GiB RAM, 900 GB NVMe per node
Nodes:                      4 (bounded by NVMe, with headroom)
Cost per node:              roughly $610/month with a 1-year commitment
Monthly:                    4 × $610 = $2,440, plus licence
Annual:                     $29,300 plus licence
```

**So the hot tier is somewhere between $30,000 and $100,000 a year**, against a $26 million fraud
loss and $739 billion of volume. That is 0.11% to 0.38% of the loss it exists to reduce.

**This is the point to make when somebody raises hot-storage cost as a trade-off.** At issuer scale
it is not a meaningful constraint — it is a rounding error against the loss. Hot storage becomes a
real constraint in two specific situations: when the entity count is far larger than the customer
count (behavioural analytics keyed on sessions, or a system keying on every IP address it has ever
seen), and at small scale, where the fixed cost of a three-node cluster is large relative to a
fraud loss of a few thousand dollars a month. **The honest answer to "is hot storage expensive?" is
"it is 0.2% of the loss at Ironvale's scale and 400% of the loss at a small merchant's scale",
which is a much better answer than either "yes" or "no".**

### S-02 · The key design that tripled the bill

**What you see.** The feature store is three times the size the capacity model predicted.

**Mechanism.** Keys were designed as one key per `(entity, feature, window)` rather than one key per
entity holding a packed vector. For a card, that is 89 buckets × 6 counted quantities = 534 keys
instead of 1. Each key carries the store's own per-record overhead — index entry, key string,
metadata — which for a small value dominates the value itself.

```
Packed:    1 key, 1.4 KB value, ~64 bytes overhead       = 1.46 KB per card
Unpacked:  534 keys × (8 bytes value + 64 bytes overhead
             + 38 bytes key string) = 58.7 KB per card   = 40× worse
```

**Prevent.** One key per entity, one packed binary value, deserialised in the client. The scoring
path needs all of an entity's features at once anyway, so there is no access-pattern argument for
splitting them. Ironvale's value is a fixed-layout struct — no field names, positions defined by
the feature-schema version, with the version as the first two bytes — which is both the smallest
and the fastest to parse. The cost of this choice is that adding a feature requires a schema
version bump and a migration, which is a real cost and is the right one to pay.

### S-03 · Expiry, and the keys nobody ever reads again

Of 200.9 million entity keys, how many are read in a given day?

```
Distinct cards transacting per day: roughly 25,200,000 (70M × 0.36 of cards active daily)
Distinct devices:                   roughly 19,000,000
Distinct merchants:                 roughly 1,100,000
Distinct IP prefixes, emails, accounts: roughly 31,000,000
Total distinct keys read per day:   roughly 76,300,000 of 200,900,000 = 38%
```

**Sixty-two percent of the keys are not read on any given day**, and a long tail is never read
again — a device used once, an IP prefix seen for a week, an email from an abandoned signup.

**Practice:** set a TTL per entity type, tuned to the longest window that entity's features cover
plus a margin. Ironvale's: card 95 days, account 95 days, device 95 days, merchant 400 days (needed
for seasonality), IP prefix 35 days, email 95 days. Measured saving: **31% of total state**, from
a configuration change.

**The subtlety that bites:** a TTL on the packed value means a key that expires loses *all* of the
entity's windows, including the 30-day one, when only the 1-minute ones are stale. If you want
per-window expiry you need either per-window keys (which `S-02` says not to do) or a sweep that
rewrites the packed value dropping old buckets. Ironvale does the latter as part of the normal
write path — each write drops buckets that have fallen out of all windows — so expiry is
incremental and free rather than a separate job.

### S-04 · The restart that took forty minutes to become useful

**What you see.** A node is replaced. Its shard's reads are served, but the aggregate values are
wrong for forty minutes.

**Mechanism.** The replacement node came up empty and began accepting reads before its state was
restored, so it returned "no data" for keys that have data. And, per doc
[02](02-the-decision-path-and-the-latency-budget.md)'s `D-05`, "no data" was imputed as zero.

**Prevent.** Two things. **The store must not serve reads for a partition until that partition's
state is restored** — this is a configuration setting in every store listed above and it is
routinely left at the permissive default because the permissive default makes a node "available"
faster. And **the client must distinguish "key absent" from "partition unavailable"**, which most
client libraries conflate into a nil return. Ironvale patched its client to surface the
distinction, which required reading the protocol-level response rather than the convenience API,
and it is the single change that converted the most dangerous failure mode in doc
[02](02-the-decision-path-and-the-latency-budget.md) from invisible to detectable.

---

## The decision log: 29 terabytes a year and why it is cheap at the price

Sizing, from doc [04](04-features-and-the-feature-store.md):

```
412 features, mostly numeric, highly compressible in a columnar format
Per record, compressed:  1.9 KB
Per day:                 42,000,000 × 1.9 KB = 80 GB
Per year:                29 TB
Object storage at roughly $0.022/GB/month for infrequent-access:
  29 TB × 1,024 × $0.022 = $653/month in year one
Cumulative over 7 years, with the lifecycle transitions in S-06: about $71,000
```

**$71,000 over seven years for the data that makes training, monitoring, dispute defence and
regulatory explanation possible.** Against a $26 million annual loss this does not require a
business case. The decision to skip it is never made on cost; it is made by not realising it is
needed until it is too late to create retrospectively.

### S-05 · The format, and why it matters more than the size

Store the decision log as **partitioned columnar files** — Parquet or ORC, partitioned by date and
subpartitioned by a hash of the card token. Three reasons that are specific to this workload:

- **Training reads a subset of columns over a long time range.** A columnar format reads the 412
  feature columns without touching the dozens of metadata columns, which for a year-long training
  query is the difference between 29 TB and perhaps 4 TB of I/O.
- **Dispute defence reads all columns for one transaction.** This is the opposite access pattern and
  it is served by the hash subpartition — the file holding that card's decisions is locatable
  without scanning.
- **Schema evolution happens constantly**, because the feature set changes. Both formats handle
  added and removed columns; neither handles a column whose *meaning* changed, which is why the
  feature-schema version must be a column in every record.

**The one thing to get right at the start, because it is unfixable later:** write the schema version
and the model, rule-estate and policy versions into every record. Doc
[06](06-models-labels-and-training.md) needs them to reproduce an evaluation; doc
[13](13-explainability-compliance-and-fairness.md) needs them to explain a decision; and nothing
can reconstruct them from the data.

### S-06 · Retention, which is a legal question with a storage answer

Fraud and payments data retention is set by regulation, not by engineering preference, and the
periods are long: commonly **five to seven years** for transaction records, with longer holds where
an investigation or dispute is open. The practical consequences:

- **A legal hold must be able to pin specific records indefinitely**, exempting them from the
  lifecycle policy. This requires that the lifecycle policy be driven by a per-object tag rather
  than purely by age, which is a design decision made at the beginning or not at all.
- **The right to erasure does not override a statutory retention obligation**, but it does require
  that you be able to find and act on everything about one person — which means the archive needs
  to be queryable by subject, not only by date. Doc
  [13](13-explainability-compliance-and-fairness.md) covers the interaction.
- **Lifecycle transitions are where the cost is controlled**: hot for 90 days (queried constantly by
  training and monitoring), infrequent-access for 15 months, archival for the remainder. The
  archival tiers are roughly a tenth the price and have retrieval delays measured in hours, which is
  acceptable for an audit request and not for anything else.

### S-07 · The analytical store that became the operational one

**What you see.** The case-management UI is slow, and it is querying the warehouse.

**Mechanism.** The decision log has everything, so somebody pointed the operational UI at it. A
columnar analytical store answers "how many declines by segment last month" in seconds and "show me
this one transaction" in about the same time, because the fixed cost of a query dominates.

**Prevent.** The operational tier is a separate, small, row-oriented store with the recent window —
Ironvale keeps 30 days of decisions in PostgreSQL, indexed by card, account and case, which is
roughly 2.4 TB and serves the UI in single-digit milliseconds. **Two copies of the same data with
different access patterns is correct here**, and the instinct to avoid duplication is the wrong
instinct.

---

## S-08 through S-10, briefly

### S-08 · The model artefact in the critical path

A 1,870-tree model is perhaps 40 MB serialised. Loading it from object storage at startup adds
seconds to a deploy, which is fine; loading it *lazily on first request* adds seconds to a request,
which is not, and is a mistake made often enough to name. **Practice:** load and validate the model
at startup, before the health check passes, and fail to start rather than starting degraded. Keep
the previous version resident so that a rollback is a pointer swap rather than a fetch, which is
what makes the control-plane/data-plane separation in doc
[06](06-models-labels-and-training.md) actually fast.

### S-09 · The block list that is eventually consistent

The block list lives in the operational tier and is read on every transaction. If it is replicated
asynchronously across regions, a card blocked in one region is still approved in another for the
replication interval. For an accuracy feature that is tolerable; for a compliance control it is
not. **Practice:** block-list writes are synchronous to a quorum before acknowledging, and the
scoring path treats a stale or unavailable block list as a hard failure rather than an absent
feature — the one place in the entire system where fail-closed is correct, per doc
[02](02-the-decision-path-and-the-latency-budget.md)'s degradation ladder.

### S-10 · The multi-region question

Ironvale is single-country; Northwind runs in three regions. The design question is whether
features are global or regional, and it has a clean answer: **the aggregates that matter for fraud
are global, because a fraudster using a card in three regions is exactly the pattern you want to
see.** But a globally-synchronous feature store cannot meet a 3 ms read from three continents.

The resolution is asymmetric: **read locally, write globally.** Each region holds a full replica
for reads; writes propagate asynchronously to all regions. The consequence is that cross-region
velocity is stale by the propagation delay — Northwind measures 340 ms p99 — and so the
cross-region pattern is detectable within a second rather than instantly. Since the fraud pattern it
catches unfolds over minutes, this is an acceptable and well-understood weakness, and it is far
better than the alternative of regional silos where it is undetectable entirely. **The
configuration to avoid is a regional feature store with no cross-region view at all**, which is the
default outcome of building region by region and is how card-testing campaigns that hop regions go
unnoticed.

---

## What to take away

1. **There are four tiers, not two.** The two the usual summary omits — operational (small, mutable,
   compliance-critical) and the decision log (enormous, append-only, the foundation of everything
   downstream) — are the ones with the interesting properties.
2. **The feature store's workload is write-heavy, 12.9 writes per read**, which eliminates
   read-optimised caches, managed key-value services at on-demand pricing, and relational engines
   whose every feature is unused and every cost is paid.
3. **The hot tier costs $30,000 to $100,000 a year at Ironvale's scale** — 0.11% to 0.38% of the
   fraud loss. "Is hot storage expensive?" is answered properly as "0.2% of the loss at issuer
   scale, 400% of it at a small merchant's", not as yes or no.
4. **One key per entity holding one packed value.** Splitting into one key per feature-window
   multiplies the state by 40×, because per-record overhead dominates a small value. Pay the schema
   versioning cost instead.
5. **62% of keys are not read on any day.** Per-entity TTLs saved Ironvale 31% of state from a
   configuration change — and drop expired buckets inside the normal write path so expiry is
   incremental rather than a job.
6. **A restored node must not serve reads before its partition is restored**, and the client must
   distinguish "key absent" from "partition unavailable". The permissive defaults for both are what
   turns the most dangerous failure in this collection from detectable into invisible.
7. **The decision log costs about $71,000 over seven years** and makes training, monitoring,
   disputes and regulatory explanation possible. The decision to skip it is never made on cost; it
   is made by not realising it is needed.
8. **Write the schema, model, rule-estate and policy versions into every decision record.** Nothing
   can reconstruct them afterwards, and three separate downstream capabilities depend on them.
9. **Keep two copies with different shapes** — columnar for analytics, row-oriented for the 30-day
   operational window. The instinct to avoid duplication is wrong here.
10. **Read locally, write globally** for multi-region features. Cross-region velocity stale by 340 ms
    is a well-understood weakness; regional silos with no cross-region view at all is how
    region-hopping card-testing campaigns go undetected.
