# Change Data Capture and Streaming from Databases

Scenarios `X-01` … `X-10`.

A large fraction of the events on any streaming platform did not start life as events. They
started as rows in an operational database, and something turned them into a stream. That
something is change data capture, and it is the most common source of subtly-wrong streaming
data because it makes a promise — "this stream reflects the database" — that is much harder to
keep than it sounds.

## 1. Four ways to get changes out of a database, and only one is good

### Polling a timestamp column

```sql
SELECT * FROM orders WHERE updated_at > :last_seen ORDER BY updated_at
```

Simple, works with any database, requires no privileges. And it is wrong in four ways that each
lose data silently:

**Clock skew and transaction ordering.** `updated_at` is set when the statement runs;
transactions commit later, in a different order. A transaction that sets `updated_at = 10:00:05`
and commits at 10:00:09 is invisible to a poll at 10:00:07 that advances the cursor to 10:00:08.
That row is lost permanently.

**Deletes are invisible.** A deleted row has no timestamp to select. Unless you implement soft
deletes throughout the schema — which is a large change to an operational system — deletes never
reach the stream.

**Intermediate states are lost.** A row updated three times between polls produces one event.
For a state machine or an audit trail, that is missing data, not compression.

**Load on the primary.** A poll every second over a large table is a scan, and it competes with
the application.

Polling is what people build first. It is acceptable only for small, append-only,
non-time-critical tables, and even then the clock-skew hole is real.

### Triggers writing to an audit table

Better: captures deletes, captures every intermediate state, and is transactionally consistent
because the trigger runs inside the transaction.

The cost is on the write path. A trigger doubles the write cost of every statement, and on a
table doing 12,000 writes a second that is a material addition to the primary's load. Triggers
also make schema changes harder and are often forbidden by DBA policy.

Acceptable for low-volume tables where you control the schema. Not for a hot table.

### Log-based CDC

Read the database's own replication log — MySQL's binlog, Postgres's WAL, Oracle's redo,
MongoDB's oplog — and turn each committed change into an event.

This is the right answer, and the reasons are structural:

- **It is the transaction log**, so it has every change, in commit order, including deletes, and
  including every intermediate state.
- **It is already being written** for replication and crash recovery, so reading it adds almost
  no write-path cost.
- **It reflects committed state only**, so a rolled-back transaction never appears.
- **It gives you a position** (LSN, GTID, binlog coordinates) that is a resumable cursor.

The costs: it needs elevated database privileges, it couples you to the database's log format
and retention, and — the one that causes incidents — **a CDC connector that falls behind can
prevent the database from reclaiming log space**, which fills the disk on the primary.

Tools: Debezium (the most widely used, works with everything), Flink CDC (Debezium embedded in
Flink, no separate Kafka Connect cluster), and the cloud-managed equivalents (DMS, Datastream).

### The transactional outbox

The application writes the event it wants to publish into an `outbox` table **in the same
transaction** as the business change:

```sql
BEGIN;
  UPDATE orders SET status = 'PAID' WHERE id = 'ord-7781204';
  INSERT INTO outbox (event_id, aggregate_id, type, payload)
       VALUES ('evt-...', 'ord-7781204', 'OrderPaid', '{...}');
COMMIT;
```

A separate process — usually log-based CDC on the `outbox` table — publishes it.

This solves the dual-write problem (`I-12`) exactly: the state change and the event are one
atomic operation, and the publish is at-least-once with a deterministic ID.

**The crucial advantage over raw CDC** is that the outbox carries the event **the application
meant to publish**, not a row diff. That distinction is Section 3's subject and it is the most
important design decision here.

## 2. The CDC record, and what it actually contains

A Debezium change event:

```json
{
  "op": "u",
  "before": {"id": "ord-7781204", "status": "PENDING", "total_cents": 4299, "updated_at": "..."},
  "after":  {"id": "ord-7781204", "status": "PAID",    "total_cents": 4299, "updated_at": "..."},
  "source": {
    "db": "riverbend", "table": "orders",
    "ts_ms": 1790000000000,
    "lsn": 892714400221,
    "txId": 4471028,
    "snapshot": "false"
  },
  "ts_ms": 1790000000412
}
```

Five fields matter for stream design:

**`op`** — `c` create, `u` update, `d` delete, `r` read (from the initial snapshot). A consumer
must handle all four, and the `r` case is the one people forget: snapshot records look like
inserts but are not, and treating them as inserts double-counts.

**`before` / `after`** — the whole row, both versions, if the database is configured to provide
it. Postgres needs `REPLICA IDENTITY FULL` for `before` on updates; without it you get only the
primary key, and "what changed" is unanswerable. MySQL's row-based binlog gives both by default.

**`source.ts_ms`** — when the change was committed in the database. **This is your event time.**
The outer `ts_ms` is when the connector processed it, which is ingest time.

**`source.lsn` / `txId`** — position and transaction. The transaction ID matters because a single
transaction may change many rows across many tables, and preserving that grouping is the
subject of `X-05`.

**`source.snapshot`** — whether this came from the initial consistent snapshot or from the log.

### Sizing

CDC records are large because they carry the whole row twice:

```
riverbend.orders row: 38 columns, ~420 B
CDC event:  before (420) + after (420) + source metadata (180) + envelope
            ≈ 1,100 B, against a 420 B row

at 12,000 writes/s across Riverbend's tables:
  12,000 × 1,100 B = 13.2 MB/s
```

**A 2.6× amplification over the row size**, which is worth knowing before you enable CDC on a
high-write table. Dropping `before` where you do not need it (Postgres's default
`REPLICA IDENTITY DEFAULT`) halves it, at the cost of losing the ability to compute what
changed.

## 3. The deepest problem: a row diff is not a business event

This is the section that matters most and it is usually skipped.

CDC gives you `orders.status` changed from `PENDING` to `PAID`. What the business cares about is
`OrderPaid`, with the payment method, the amount, the promotion applied, and the reason. Some of
that is in other tables, changed in the same transaction; some of it is in the application's
memory and was never written anywhere.

Three consequences.

**Your stream's schema is your database's schema.** A column rename in an operational database
— a refactor with no business meaning — becomes a breaking change to every downstream consumer
([03](03-the-event-schema-and-contracts.md)). The database team did not sign up for that, and
they will find out by breaking you.

**Business events must be reassembled from row changes**, which means joining several CDC
streams by transaction ID and inferring intent from diffs. `status: PENDING → PAID` means
"paid"; `PENDING → CANCELLED` means "cancelled"; `PAID → PAID` with a changed `total_cents`
means... something, and only the application knows.

**Intent is not recoverable.** Why did the status change? A customer action, a timeout, an admin
override, a batch job? The row does not say. If a downstream consumer needs to distinguish
those, CDC cannot provide it and no amount of cleverness will.

### The decision

**Use raw CDC when** the consumer wants the *data*: replicating a table to the lake, maintaining
a search index, feeding a cache, building a dimension table for a temporal join
([10](10-joins-and-enrichment.md)). Here "the current state of this row" is exactly the
question, and CDC answers it perfectly.

**Use the outbox when** the consumer wants the *event*: an order was paid, a user signed up, a
shipment was dispatched. The application knows the intent; make it say so.

Meridian's split:

| Stream | Source | Why |
|---|---|---|
| `riverbend.orders.lifecycle` | **Outbox** | Downstream needs intent, not diffs |
| `riverbend.payments.settled` | **Outbox** | Same, and it is financial |
| `riverbend.catalog.changes` | **Raw CDC** | It is a dimension table; the row is the point |
| `riverbend.inventory.adjustments` | **Raw CDC** | Current stock level per SKU; compacted |
| `corridor.member.profile` | **Raw CDC** | Enrichment source for temporal joins |

The rule of thumb: **if the consumer would be happy with a nightly table dump, raw CDC is right.
If the consumer needs to know why something happened, you need the outbox.**

## 4. The snapshot problem

A CDC connector starting fresh has to bridge two things: the current contents of the table, and
the ongoing stream of changes. Getting the boundary right is subtle.

### Why the naive approach fails

```
1. SELECT * FROM orders        (takes 40 minutes on a 400M-row table)
2. start reading the log from now
```

Changes during those 40 minutes are in neither: the snapshot read them before they changed (or
after), and the log reading started later. Rows are silently stale or missing.

### The classic approach: lock and snapshot

```
1. FLUSH TABLES WITH READ LOCK   (blocks all writes)
2. record the log position
3. SELECT * FROM orders
4. release the lock
5. read the log from the recorded position
```

Correct, and it locks the table for the duration of the snapshot, which on a 400-million-row
table is a 40-minute outage. Unacceptable on a production primary.

### What is actually done: incremental snapshotting

Debezium's DDD-3 algorithm (based on Netflix's DBLog) snapshots in **chunks**, interleaved with
log reading, with no locks:

```
1. start reading the log immediately (so nothing is missed)
2. for each chunk of the table, by primary-key range:
     a. write a low watermark signal to a signalling table
     b. SELECT the chunk
     c. write a high watermark signal
     d. any log events for keys in this chunk that appeared between the
        watermarks take precedence over the chunk's values
3. continue until the table is covered
```

The watermark trick is what makes it correct: **for any key, the log event always wins over the
snapshot value**, and the watermarks identify which log events fall in the ambiguous window.

The practical consequences:

- **No locks, no outage.** The snapshot can take hours.
- **It can be paused and resumed**, which matters for a multi-day snapshot of a large table.
- **It requires a signalling table** the connector can write to, which needs a schema change and
  a privilege.
- **Snapshot records have `op: "r"`**, and they arrive interleaved with real changes, so a
  downstream consumer sees a mixture and must handle both.

### The consumer-side implication

During a snapshot, a downstream aggregation sees 400 million "reads" that are not creates. A
job counting orders must ignore `op: "r"` or it will count the entire table as new orders.

**Meridian's convention:** the CDC connector routes snapshot records to a `.snapshot` topic and
live changes to the main topic. Consumers that need the initial state read both; consumers that
only care about ongoing changes read one. Another instance of **prefer a separate path to a
discriminating field**.

## 5. Scenarios

### X-01 — The CDC connector fell behind and filled the database's disk

**What you see.** The Postgres primary's disk is 94% full. `pg_wal` is 800 GB.

**Mechanism.** A logical replication slot holds the WAL until the consumer confirms it has been
read. A stopped or slow connector means the slot's `confirmed_flush_lsn` stops advancing, and
Postgres cannot recycle WAL segments. The database fills and then stops accepting writes.

**This is a CDC connector taking down a production database**, and it is the single most
dangerous property of log-based CDC.

**Confirm it.**

```sql
SELECT slot_name, active,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) AS retained
FROM pg_replication_slots;
```

**Recover.** Restart the connector if it is stopped. If it cannot catch up in time, **drop the
replication slot** — which loses the position and requires a new snapshot, but saves the
database. That is the right trade and it must be pre-authorised, because during the incident
nobody wants to be the person who dropped the slot.

**Prevent.**

- **`max_slot_wal_keep_size`** (Postgres 13+) bounds the retained WAL; the slot is invalidated
  rather than the disk filling. **Set it.** The database survives, the connector needs a new
  snapshot, and that is the correct priority.
- **Alert on slot lag in bytes**, at 20% of the WAL volume, on every slot, owned by the database
  team rather than the streaming team.
- **Never point CDC at the primary if a replica will do.** Postgres logical replication from a
  standby works from version 16; before that, MySQL's binlog from a replica has always worked.

### X-02 — Schema change in the database broke the stream

**What you see.** A column was dropped in a migration. The CDC stream's schema changed, and
eleven consumers failed.

**Mechanism.** Section 3's first consequence. The database team ran a routine migration.

**Prevent.**

- **The outbox for anything with real consumers**, so the published schema is decoupled from the
  table schema.
- **Where raw CDC is used, register its schema in the registry and apply compatibility
  checks**, so an incompatible database migration fails the CDC connector loudly rather than
  propagating. Debezium can be configured to do this.
- **Put the CDC-sourced tables in the database team's change process.** A migration on a
  CDC-sourced table requires a check against the stream contract. This is an organisational
  control and it is the only one that actually works.

### X-03 — Deletes handled as updates

**What you see.** Deleted rows persist in a downstream materialised view forever.

**Mechanism.** A CDC delete arrives as `op: "d"` with `after: null`. A consumer that upserts
`after` into a key-value store writes a null — or skips the record — and never removes the key.

For Kafka, Debezium can emit a **tombstone** (null value) after a delete, which drives log
compaction to remove the key. This is controlled by `tombstones.on.delete` and it must be on for
compacted topics.

**Prevent.** Handle all four `op` values explicitly, with a `default` branch that counts
(`S-10`'s pattern). And for compacted topics, verify tombstones are being emitted — the symptom
of their absence takes weeks to appear.

### X-04 — CDC event time is the connector's clock

**What you see.** Event-time windows over a CDC stream produce results that shift when the
connector restarts.

**Mechanism.** The timestamp assigner used the outer `ts_ms` (when the connector processed it)
instead of `source.ts_ms` (when the database committed it). On a catch-up after a connector
outage, thousands of changes committed over an hour all get the same processing timestamp.

**Prevent.** `source.ts_ms` is the event time. Always. This is a one-line fix and a very common
bug.

### X-05 — Transaction boundaries lost

**What you see.** A transfer moves $200 from account A to B in one transaction. A downstream
job briefly sees A debited and B not yet credited, and alerts on an imbalance.

**Mechanism.** CDC emits one event per changed row. Two rows changed in one transaction become
two events, possibly on different topics, definitely on different partitions, with no ordering
between them.

**Prevent.** Three options:

- **Debezium's transaction metadata topic** emits `BEGIN` and `END` markers with the event count
  per transaction. A consumer can buffer until it has all of them. This works and it is real
  machinery — the consumer needs a buffer, a timeout, and a policy for an incomplete
  transaction.
- **Route all tables in a transaction boundary to one topic, keyed by the transaction's
  aggregate.** Works when there is a natural aggregate (an account, an order); fails for
  cross-aggregate transactions.
- **The outbox**, where the application writes *one* event for the whole business operation. The
  transaction boundary problem disappears because there is one row.

The third is why the outbox keeps being the answer.

### X-06 — The same row updated twice arrives out of order

**What you see.** A row's final state in the sink is an earlier version.

**Mechanism.** Two changes to the same primary key, in different partitions because the
partitioner used something other than the primary key, processed concurrently.

**Prevent.** **Key CDC topics by the primary key**, always. And carry the LSN as a version so
the sink can reject out-of-order writes:

```sql
-- ClickHouse
ENGINE = ReplacingMergeTree(lsn) ORDER BY (id)
```

⚠️ And note the gotcha from [13](13-serving-the-results.md): `id` in the `ORDER BY` is correct
because it is immutable; putting `status` there would break deduplication silently.

### X-07 — The outbox table became the bottleneck

**What you see.** The `outbox` table has 400 million rows and inserts are slowing.

**Mechanism.** Rows are written and never deleted, or deleted by a periodic job that cannot keep
up, or deleted in a way that causes index bloat.

**Prevent.**

- **Delete rows after publication**, in batches, by primary-key range. With log-based CDC on the
  outbox, the delete generates a CDC event too — route it to a null sink, or use a partitioned
  outbox table and drop whole partitions, which generates no row-level events at all.
- **Partition the outbox by time** and drop old partitions. This is the cleanest approach: the
  drop is a metadata operation, there is no index bloat, and there are no delete events.
- **Do not index the outbox beyond its primary key.** It is written sequentially and read
  sequentially by the log reader; extra indexes cost write throughput for no benefit.

### X-08 — Two CDC streams joined and one is always behind

**What you see.** A temporal join between `orders` CDC and `customers` CDC produces nulls for
recent orders.

**Mechanism.** `E-09` and `J-06`. The `customers` connector has higher lag because its table is
larger, or it is snapshotting, or it shares a connector cluster with something busy.

**Prevent.** Monitor per-connector lag, and where a join depends on two CDC streams, either give
them dedicated connectors or accept the join's watermark being the slower one and size the state
for it.

### X-09 — The snapshot restarted and duplicated everything

**What you see.** A connector restarted mid-snapshot and began the snapshot again from the
start. Downstream received 280 million duplicate `op: "r"` records.

**Mechanism.** Older Debezium snapshot modes were not resumable. If the connector restarted, the
snapshot restarted.

**Prevent.** Incremental snapshotting (Section 4), which is resumable by design. And idempotent
sinks keyed by primary key, which make a duplicated snapshot harmless — another instance of
`D-05`'s "emit absolute values."

### X-10 — CDC from a database that is itself a replica of something

**What you see.** Event times are minutes behind the actual business events.

**Mechanism.** CDC is running against a read replica that is itself lagging. `source.ts_ms` is
the commit time on the *replica*, which for physical replication equals the primary's commit
time (good) but for logical replication chains may not.

**Prevent.** Know your topology. Read `source.ts_ms` semantics from the specific connector's
documentation for the specific replication mode, and verify by comparing against a known event.
And monitor replica lag as an input to your pipeline's freshness, because it is one.

## 6. The CDC checklist

1. **Log-based, not polling.** Polling loses data at transaction boundaries and cannot see
   deletes.
2. **Outbox for business events, raw CDC for data.** If the consumer needs to know *why*, CDC
   cannot tell them.
3. **`source.ts_ms` is the event time.**
4. **Key by primary key**, and carry the LSN as a version.
5. **Incremental snapshotting**, with snapshot records on a separate topic.
6. **Handle all four `op` values**, with a counted default branch, and emit tombstones for
   deletes on compacted topics.
7. **`max_slot_wal_keep_size` (or equivalent) set**, and slot lag alerted, owned by the database
   team.
8. **CDC from a replica, not the primary**, where the database supports it.
9. **The CDC stream's schema is in the registry** with compatibility checks, and CDC-sourced
   tables are in the database team's change process.
10. **The outbox is partitioned by time and dropped**, not deleted row by row.

## What to take away

1. **Polling a timestamp column loses rows at transaction boundaries and cannot see deletes**,
   and both losses are silent. It is acceptable only for small append-only tables.
2. **Log-based CDC is the right answer** because the transaction log is already being written,
   has every change in commit order, and reflects only committed state.
3. **A CDC connector can take down a production database** by preventing WAL reclamation. Set
   `max_slot_wal_keep_size`, alert on slot lag, and pre-authorise dropping the slot.
4. **A row diff is not a business event.** CDC tells you `status` changed; it cannot tell you
   why. Use the outbox where intent matters.
5. **Raw CDC makes your database schema your public API.** A routine column rename becomes a
   breaking change to eleven consumers who the database team has never met.
6. **CDC records are ~2.6× the row size** because they carry before and after.
7. **Incremental snapshotting with watermarks** lets you bootstrap a 400-million-row table with
   no locks, resumably — and it interleaves `op: "r"` records that consumers must not count as
   creates.
8. **`source.ts_ms`, not the outer `ts_ms`.** One line, very common bug, and it makes every
   event-time window shift when the connector restarts.
9. **Transaction boundaries are lost by row-level CDC.** The transaction metadata topic works
   and is real machinery; the outbox makes the problem disappear.
10. **Partition the outbox by time and drop partitions.** Row-by-row deletes generate their own
    CDC events and bloat the index.
