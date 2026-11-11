# Migrations in NoSQL, Search, and Analytics Stores

"Schemaless" was marketed as the end of migrations. What it actually did was move the schema
from a place where the engine enforces it, reports on it and helps you change it, to a place
where none of those are true.

The schema did not disappear. **It moved into the readers** — into every piece of code that
interprets a document, and into every query that assumes a field exists. You still have to
migrate; you just do it without constraints, without a catalogue, and without a tool that can
tell you which rows have the old shape. Scenario IDs are `N-nn`.

---

## The one pattern that covers most of this document

Where a relational migration has six phases over one table, a document store has two
strategies and you pick one per change.

**Bulk rewrite.** Read every document, transform it, write it back. The relational backfill,
with all of doc 06's hazards and usually worse throughput, because document stores rewrite the
whole document rather than one column.

**Versioned documents with lazy migration.** Stamp every document with a `schemaVersion`.
Readers handle every version they might encounter, upgrading in memory. Writers always write
the latest. Documents migrate when something happens to touch them.

```json
{ "_id": "ord_8841", "schemaVersion": 3, "amount": 4299, "currency": "USD" }
```

```python
def read_order(doc):
    v = doc.get("schemaVersion", 1)
    if v < 2:  doc = add_currency_from_storefront(doc); doc["schemaVersion"] = 2
    if v < 3:  doc = split_amount_into_minor_units(doc); doc["schemaVersion"] = 3
    return doc
```

**The trade, stated honestly.** Lazy migration costs nothing at migration time and costs you
**forever** afterwards, because the upgrade chain is code you can never delete until every
document has been touched — and in most systems, some documents are never touched again. A
2019 order read once a year keeps version 1 alive in your codebase indefinitely.

**The resolution most mature systems reach:** lazy migration for the immediate relief, *plus* a
background sweeper that rewrites stale documents at a low rate, *plus* a metric showing the
population by version. When the count at version 1 reaches zero, you delete the branch. Without
the metric and the sweeper, "lazy migration" means "never migrated", and five years later you
have a function with eleven branches that nobody dares touch.

**`schemaVersion` is the single most valuable field in a document store**, and it costs four
bytes. Add it on day one. Doc 05's `C-13` makes the same argument for `jsonb` columns in a
relational database.

---

## DynamoDB: the key schema is the schema, and it is immutable

### N-01 · You cannot change a partition key

**What you see.** Waypoint's `trips` table is keyed on `trip_id`. A new access pattern needs
queries by `driver_id` within a time range. There is no `ALTER TABLE`.

**Mechanism.** In DynamoDB, the partition key determines physical placement. Changing it means
relocating every item, which is not an operation the service offers. The same applies to the
sort key and to local secondary indexes, which can only be defined at table creation.

**Prevent — or rather, what to do.** The only route is doc 09's store-to-store migration, with
DynamoDB on both ends:

1. Create the new table with the new key schema.
2. Enable **DynamoDB Streams** on the old table (this is your CDC).
3. Start a Lambda or Kinesis consumer applying stream records to the new table.
4. Scan the old table in parallel segments and write to the new one, with conditional writes so
   a scan result never overwrites a newer streamed item:
   `ConditionExpression: attribute_not_exists(pk) OR version < :v`
5. Verify, cut over, keep the reverse stream.

Everything in doc 09 applies, including the ordering rule that **capture starts before the
scan**.

**The design lesson.** Because the key schema is immutable, DynamoDB rewards thinking about
access patterns before the first write and punishes discovering them later more harshly than
any relational engine. A generic `PK`/`SK` naming with overloaded values — the single-table
design pattern — is partly a response to exactly this: it buys flexibility by making the key
meaningless to the engine, at the cost of making it meaningless to humans too.

### N-02 · The GSI backfill that throttles the base table

**What you see.** Adding a global secondary index to a 2.1-billion-item table. Hours later,
writes to the **base** table start failing with `ProvisionedThroughputExceededException`.

**Mechanism.** A new GSI is backfilled by the service, and every base-table write during the
backfill also writes to the GSI. In provisioned mode, if the GSI's write capacity is
insufficient, **the GSI's throttling propagates to the base table** — DynamoDB will not accept
a base write it cannot reflect in the index.

This is the most surprising coupling in DynamoDB operations: an index you added for reads takes
down your writes.

**Confirm it.** `ConsumedWriteCapacityUnits` and `WriteThrottleEvents` on the *index*, not the
table. The index metrics are a separate dimension and are easy to miss.

**Prevent.**

- Provision the GSI's write capacity at **2–3× the base table's** for the duration of the
  backfill, then scale it down. The backfill competes with live writes for the same budget.
- Or use on-demand mode for the migration, which removes the ceiling and costs more.
- **Project only the attributes you need** (`KEYS_ONLY` or a short `INCLUDE` list). A GSI with
  `ALL` projection duplicates the entire table — storage, write cost, and backfill duration all
  scale with projection size.
- Add **one GSI at a time**. The limit is 20 per table, and concurrent backfills compound the
  throttling.

### N-03 · The item that grew past 400 KB

**What you see.** A migration that adds a field to every item fails on 0.02% of them with
`ValidationException: Item size has exceeded the maximum allowed size`.

**Mechanism.** DynamoDB's hard 400 KB item limit. Items that were at 399 KB before your field
are now over. The distribution's tail is where this lives, and the tail is exactly what test
data lacks.

**Prevent.** Before any migration that grows items, measure the distribution:
`max(item_size)` across a sample, and plan for the items within your added field's size of the
limit. The usual remedy — moving a large attribute to S3 and storing a pointer — is itself a
migration, so discover this before you start, not at 99.98%.

---

## Cassandra: cheap `ALTER`, immutable primary key, and a type change that was removed

### N-04 · Schema disagreement across the cluster

**What you see.** An `ALTER TABLE ADD` is applied. Some queries see the new column and some
return `Undefined column name`.

**Mechanism.** Cassandra propagates schema changes through gossip, and the cluster converges
over seconds — usually. A node that was down, partitioned, or overloaded during the change can
be left on an older schema version. Northlight, at 2,500 services against a large Cassandra
cluster, sees this on roughly one deployment in forty.

**Confirm it.** This is the diagnostic and it should be a standing check:

```
nodetool describecluster
# Schema versions:
#   86afa796-d883-3932-aa73-6b017cef0d19: [10.0.1.4, 10.0.1.5, 10.0.1.9, ...]
#   7ba1f4de-11a2-3b7c-9f31-20c1ab8e4411: [10.0.2.7]        <-- disagreement
```

**Recover.** Restart the disagreeing node, which forces a schema pull. If several disagree,
stop making schema changes until they converge — concurrent schema changes during a
disagreement can produce divergent schemas that require manual resolution.

**Prevent.** Apply schema changes from **one coordinator node**, one statement at a time, with
a convergence check between each:

```
cqlsh -e "ALTER TABLE ks.t ADD c text;"
# then poll until describecluster reports exactly one schema version
```

Never run schema changes concurrently from multiple clients. Cassandra's schema is
eventually-consistent state like everything else, and concurrent writers to
eventually-consistent state is the general case of a bad idea.

### N-05 · The column type you cannot change, and the one you must not re-add

**Mechanism.** Two related immutabilities:

- **`ALTER TABLE ... ALTER column TYPE` was removed in Cassandra 4.0.** It existed before and
  was unsafe: SSTables on disk hold values serialised in the old type's format, and reading
  them as the new type reinterprets bytes. Removing the feature was the right call and it means
  **a type change is a new table plus a migration**.
- **Primary key and clustering columns cannot be altered at all**, in any version. They
  determine partitioning and on-disk ordering.

Dropping and re-adding a column is permitted with the *same* type — Cassandra records a drop
timestamp and filters out pre-drop data — but it is a pattern worth avoiding, because the
correctness depends on timestamp handling that is easy to disturb with writes carrying
client-supplied timestamps.

**What to do.** A new table, a dual write, a backfill, and a read switch — doc 01's six phases
with Cassandra's own tools (`COPY` for small tables, Spark for large ones). There is no
shortcut, and the absence of one is why Cassandra data models are worth more design time than
relational ones.

---

## Elasticsearch and OpenSearch: mappings are immutable, so you reindex

### N-06 · The field type you cannot change

**What you see.** `order_id` was mapped as `text` and needs to be `keyword` so it can be
aggregated and sorted. `PUT /orders/_mapping` returns
`mapper [order_id] cannot be changed from type [text] to [keyword]`.

**Mechanism.** The mapping determines how values were analysed and written into the inverted
index at **index time**. Changing it cannot retroactively change what is on disk. You can add
new fields; you cannot change existing ones.

**What to do — the alias swap, which is the standard pattern and worth knowing exactly:**

```
# 1. Applications ALWAYS read and write through an alias, never the index name.
#    If they do not, start here - this change is the prerequisite for everything else.
POST /_aliases
{ "actions": [{ "add": { "index": "orders-v1", "alias": "orders" }}]}

# 2. Create the new index with the corrected mapping.
PUT /orders-v2  { "mappings": { "properties": { "order_id": { "type": "keyword" }}}}

# 3. Dual-write: the application writes to both aliases during the transition,
#    OR re-drive from the source of truth (preferred - see below).

# 4. Reindex, in parallel slices.
POST /_reindex?wait_for_completion=false&slices=auto
{ "source": { "index": "orders-v1" }, "dest": { "index": "orders-v2" }}

# 5. Catch up the delta written since the reindex started.
POST /_reindex
{ "source": { "index": "orders-v1",
              "query": { "range": { "updated_at": { "gte": "<reindex start>" }}}},
  "dest": { "index": "orders-v2", "op_type": "index" }}

# 6. Atomic swap. Both actions apply in one cluster-state update.
POST /_aliases
{ "actions": [ { "remove": { "index": "orders-v1", "alias": "orders" }},
               { "add":    { "index": "orders-v2", "alias": "orders" }} ]}
```

**The two things to get right:**

- **Always use an alias.** An application that hardcodes `orders-v1` makes every future
  mapping change an application deploy. This is the cheapest piece of foresight available in
  Elasticsearch operations and it is routinely skipped.
- **Prefer re-driving from the source of truth over `_reindex`.** Riverbend's search index is
  built from the `orders.created` Kafka topic. Rebuilding from the topic rather than
  reindex-from-old-index means the new index is built by the *current* indexing code with the
  *current* enrichment, rather than inheriting whatever bugs were in `orders-v1`. Reindexing
  copies your mistakes faithfully.

### N-07 · The reindex that doubled cluster load for eleven hours

**Mechanism.** `_reindex` is a bulk read plus a bulk write on the same cluster, and with
`slices=auto` it will happily consume everything available. During it, search latency degrades
and the cluster may hit its write queue limits, rejecting live indexing.

**Prevent.** `requests_per_second` throttling on the reindex task, sized from the headroom you
measured, and `slices` set deliberately rather than `auto`. Monitor the task:

```
GET /_tasks?actions=*reindex&detailed
POST /_reindex/<task_id>/_rethrottle?requests_per_second=2000
```

Being able to re-throttle a *running* reindex is a genuinely good piece of design and it is the
equivalent of doc 06's pause switch.

---

## ClickHouse: cheap columns, expensive mutations, immutable sort order

### N-08 · `ALTER ... UPDATE` is a mutation, not a statement

**What you see.** `ALTER TABLE events UPDATE currency = 'USD' WHERE currency = ''` returns
immediately. Nothing appears to happen. Disk usage climbs for the next six hours.

**Mechanism.** ClickHouse is append-optimised and has no in-place row update. `ALTER ... UPDATE`
creates a **mutation**: an asynchronous background job that **rewrites every data part**
containing matching rows. Returning immediately means the job was accepted, not that it ran.

```sql
SELECT database, table, mutation_id, command, parts_to_do, is_done, latest_fail_reason
FROM system.mutations WHERE is_done = 0;
```

Mutations are not transactional, cannot be rolled back, and `KILL MUTATION` stops future work
but leaves already-rewritten parts rewritten. A mutation that is half done has half-updated
data and there is no way back except another mutation.

**Prevent.** Treat mutations as a last resort. The ClickHouse-idiomatic answers:

- **Add a column with a `DEFAULT` expression** — metadata-only, and the value is computed at
  read time until a merge materialises it. No rewrite.
- **Use a `ReplacingMergeTree` or `CollapsingMergeTree`** and insert a new version of the row.
- **Reprocess into a new table and swap** with `EXCHANGE TABLES`, which is atomic.

### N-09 · The sorting key you cannot change, and the dedup it silently breaks

**Mechanism.** Two related constraints, and the second has caused real data loss:

- **`MODIFY ORDER BY` can only append columns** to the sorting key, and only when the primary
  key is a prefix of it. You cannot reorder or remove. The sorting key determines physical
  layout and the sparse primary index; changing it means a new table.
- **`ReplacingMergeTree` deduplicates by the `ORDER BY` key, not by the primary key you
  intended.** If any column in `ORDER BY` is mutable, two versions of the same logical row sort
  to different keys and **are never deduplicated**. The table silently accumulates duplicates,
  and because `FINAL` and background merges both key on `ORDER BY`, nothing reports it.

That second one is worth stating as a rule: **in a `ReplacingMergeTree`, every column in
`ORDER BY` must be immutable for the lifetime of the row.** The common mistake is including a
`status` or `updated_at` column for query performance. The fix is to move the mutable column
out of `ORDER BY` and accept the query cost, or add a `PROJECTION`.

Migrating an existing table to fix this is a full rebuild: new table, correct `ORDER BY`,
`INSERT INTO new SELECT ... FROM old FINAL`, verify, `EXCHANGE TABLES`.

### N-10 · Materialised views that do not backfill

**Mechanism.** A ClickHouse materialised view is a **trigger on insert**, not a maintained
query. Creating one over an existing table populates nothing; it only sees rows inserted from
that moment.

**Prevent.** Create the view, then backfill explicitly into its target table for the historical
range, being careful about the boundary so rows are neither missed nor doubled:

```sql
CREATE MATERIALIZED VIEW orders_daily_mv TO orders_daily AS SELECT ...;

-- Then backfill, excluding anything the view has already captured.
INSERT INTO orders_daily
SELECT ... FROM orders WHERE created_at < '2026-10-09 14:00:00';
```

The same applies to `PROJECTION`s, which need an explicit `MATERIALIZE PROJECTION` to cover
existing parts, and that is itself a mutation (`N-08`).

---

## Redis: the key format is the contract

### N-11 · Changing a key naming scheme

**Mechanism.** Redis has no schema, so the schema is the key naming convention plus the value
encoding, and both live entirely in application code. Changing `order:8841` to
`riverbend:order:v2:8841` is a migration with no tooling at all.

**What to do.** The TTL makes this easier than it looks, when there is one:

1. **Write both** key formats, read the new one with a fallback to the old.
2. **On a fallback hit, write the new key** — lazy migration, exactly the document-version
   pattern.
3. **Wait out the longest TTL.** With a 24-hour TTL, after 24 hours every live key is in the
   new format and the old ones have expired on their own.
4. Remove the fallback and the dual write.

**If there is no TTL**, you need `SCAN` (never `KEYS`, which blocks the single-threaded server
for the duration) with a cursor, migrating in batches. And it is worth noticing that a cache
without a TTL is a storage system you did not plan for — the migration is a good moment to add
one.

---

## Event schemas: the registry mode that is not as safe as it sounds

### N-12 · `BACKWARD` compatibility does not mean "safe for everyone"

**What you see.** A producer removes a field from an Avro schema. The schema registry accepts
it — the compatibility mode is `BACKWARD`, which is the default, and removing a field is
backward compatible. Three consumer services break.

**Mechanism.** The compatibility modes answer different questions, and the names do not make
that obvious:

| Mode | Guarantees | Permits | Upgrade order |
|---|---|---|---|
| `BACKWARD` | New schema can read **old** data | **Delete fields**, add optional fields | **Consumers first** |
| `FORWARD` | Old schema can read **new** data | **Add fields**, delete optional fields | **Producers first** |
| `FULL` | Both | Add or delete **optional** fields only | Either |
| `*_TRANSITIVE` | The same, against **all** prior versions, not just the last | | |
| `NONE` | Nothing | Anything | Hope |

`BACKWARD` is about whether a *consumer running the new schema* can read *old messages*. It
says nothing about whether a consumer running the **old** schema can read **new** messages —
that is `FORWARD`. So under `BACKWARD`, deleting a field is allowed, and every consumer that
has not upgraded and still reads that field breaks.

**Prevent.** For a topic with multiple independent consumer teams — which is every interesting
topic — use **`FULL_TRANSITIVE`**. It permits only adding and removing optional fields with
defaults, which is restrictive and is exactly the restriction doc 01's expand/contract
discipline imposes anyway. The non-transitive variants only check against the immediately
previous version, so a sequence of individually-compatible changes can be collectively
incompatible with a consumer two versions behind.

### N-13 · The Protobuf field number that was reused

**What you see.** A consumer deserialises `currency` and gets a timestamp.

**Mechanism.** In Protobuf, **the field number is the wire contract**; names exist only in the
`.proto` file. Field 7 was `legacy_region` (a string), removed in 2024. In 2026 someone added
`settled_at` (an int64) and the code generator assigned it 7. Old messages still in a topic, a
database column, or a replay buffer carry a string in field 7, and the new reader interprets
those bytes as a varint.

**Prevent.** `reserved`, always, on every removal:

```protobuf
message Order {
  reserved 7, 11 to 13;
  reserved "legacy_region", "old_tax_code";
  int64 order_id  = 1;
  int64 amount_minor = 2;
  string currency = 3;
}
```

`reserved` makes reuse a **compile error**, which is the correct place for this to fail. It
costs one line and it is the difference between a build failure and silent corruption of
historical data.

The related rule: changing a field's type is unsafe except for a small set of wire-compatible
pairs. `int32`, `int64`, `uint32`, `uint64` and `bool` are mutually varint-compatible with
sign-extension caveats; `sint32`/`sint64` use zigzag encoding and are **not** compatible with
`int32`/`int64`. Treat every type change as a new field number.

---

## Lakehouse formats: where schema evolution finally works properly

### N-14 · Iceberg and Delta make rename safe, and the history still has the old shape

Iceberg tracks columns by **field ID**, not by name or position. So:

- **Rename is safe** — the ID is unchanged, and old data files are read correctly.
- **Reorder is safe.**
- **Drop and re-add is safe** — the re-added column gets a new ID, so old files' data is not
  resurrected.
- **Widening is safe** (`int` → `long`, `float` → `double`, `decimal` precision increase).
- **Partition evolution** is supported: old data keeps its old partitioning, new data uses the
  new scheme, and the query planner handles both.

This is a genuine advance and it is worth knowing because it changes what you should put in a
lakehouse. Delta Lake reaches the same place with column mapping (by ID) from Delta 2.0.

**What it does not solve**, and this is the part that affects Riverbend directly: **the
semantic history**. Riverbend's archive holds six years of orders. If `amount_cents` becomes
`amount` with different units, Iceberg will happily rename the column — and every file written
before the change still contains **cents** under a column now called `amount`, which the reader
interprets as units. Field IDs preserve the *identity* of a column across a rename. They
guarantee nothing about its *meaning*.

**What to do about history.** Three options, and the choice should be deliberate:

| Option | Cost | When |
|---|---|---|
| **Rewrite the history** | Reprocess six years of Parquet: at Riverbend, 4.2 billion rows, days of compute | When the data is queried across the boundary routinely and correctness matters |
| **Handle it at read time** | A view with `CASE WHEN partition_date < '2026-11-12' THEN amount/100 ELSE amount END` | Usually right. Cheap, explicit, and the logic lives in one place |
| **Leave a boundary** | Document that queries must not span the date | Only acceptable for data nobody queries historically, which is rarely true of a lakehouse |

**The read-time option is usually correct**, and the discipline that makes it work is that the
view is the only supported access path — if people query the underlying table directly, the
`CASE` is a suggestion rather than a correction.

---

## What to take away

1. **Schemaless moved the schema into the readers.** You still migrate; you do it without
   constraints, a catalogue, or a way to find the rows with the old shape.
2. **`schemaVersion` on every document costs four bytes and is the single most valuable field
   in a document store.** Add it on day one.
3. **Lazy migration costs nothing now and costs you forever**, unless you pair it with a
   background sweeper and a population-by-version metric so you can eventually delete the
   upgrade branches.
4. **DynamoDB's partition key, sort key and LSIs are immutable.** Changing one is a full
   store-to-store migration via Streams plus a parallel scan.
5. **A throttled GSI throttles writes to the base table.** Provision the index at 2–3× during
   backfill, project only what you need, and add one at a time.
6. **Cassandra's schema is eventually consistent**; check `nodetool describecluster` for
   disagreement, and never apply schema changes concurrently from multiple clients.
7. **Cassandra removed `ALTER TYPE` in 4.0 because it corrupted data**, and primary keys were
   never alterable. A type change is a new table and six phases.
8. **Elasticsearch field types are immutable; always read and write through an alias** so the
   reindex-and-swap is a cluster operation rather than an application deploy.
9. **Prefer rebuilding a search index from the source of truth over `_reindex`.** Reindexing
   copies your old bugs faithfully.
10. **ClickHouse `ALTER ... UPDATE` is an asynchronous, non-transactional, unrollbackable
    rewrite of every matching part.** Prefer a `DEFAULT` expression, a replacing engine, or a
    rebuild with `EXCHANGE TABLES`.
11. **In a `ReplacingMergeTree`, every column in `ORDER BY` must be immutable**, or deduplication
    silently stops working and nothing reports it.
12. **ClickHouse materialised views are insert triggers, not maintained queries.** Creating one
    backfills nothing.
13. **In Redis, the key format is the contract**; dual-write, read-with-fallback, and let the
    TTL complete the migration. No TTL means the cache is a storage system you did not plan.
14. **`BACKWARD` compatibility permits deleting a field and breaks consumers who need it.** For
    a topic with independent consumer teams, use `FULL_TRANSITIVE`.
15. **Protobuf field numbers are the wire contract.** `reserved` every removal, so reuse is a
    compile error rather than silent reinterpretation of historical data.
16. **Iceberg and Delta make rename, reorder and widening genuinely safe** through field IDs —
    and they guarantee nothing about meaning. Six years of files called `amount` still contain
    cents.

Next: [15-the-failure-catalogue-and-operations-playbook.md](15-the-failure-catalogue-and-operations-playbook.md),
which is what to do at 02:40 when one of these is happening.
