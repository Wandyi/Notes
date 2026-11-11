# Backfills: The Long Phase, and the One Most Likely to Cause an Unrelated Incident

A backfill is phase 3 of doc 01: bringing rows that already exist into conformance with a
schema that just changed. It is the longest phase by a wide margin, and it has a property none
of the others share — **it can take down parts of your system that have nothing to do with the
table you are migrating.**

That is the thing to carry through this document. The failures below are mostly not "the
backfill was wrong." They are "the backfill was correct and it exhausted a resource that
something else needed." Scenario IDs are `B-nn`.

---

## B-01 · The single-statement backfill, costed

Riverbend needs `amount` and `currency` populated for 521 million existing orders. The obvious
statement:

```sql
UPDATE orders
   SET amount = amount_cents / 100.0,
       currency = 'USD';
```

Before reading on, it is worth predicting what this does. Most people predict "it will be
slow." It is considerably worse than slow, in four independent ways, and each of them is
arithmetic you can do in advance.

### Cost 1: write-ahead log volume

PostgreSQL does not modify a row in place. An `UPDATE` writes a **complete new version** of the
row and marks the old one dead. So the WAL cost per row is not "the bytes that changed":

| Component | Bytes | Why |
|---|---|---|
| The new tuple | 484 | 460-byte row plus a 24-byte tuple header — the whole row, not the delta |
| WAL record overhead | 55 | Record header, relation and block identifiers |
| Index entries | 200 | Five indexes on `orders`; the page is full (default `fillfactor` 100) so this cannot be a HOT update, and every index gets a new entry at ~40 bytes |
| Full-page image | 482 | The first write to a page after a checkpoint logs the whole 8 KB page. At 17 rows per page, that is 8,192 ÷ 17 amortised per row |
| **Total** | **~1,221 B** | **About 1.2 KB of WAL per updated row** |

521 million rows × 1.2 KB = **625 GB of write-ahead log** for a statement that changes an
8-byte value and adds a 3-character string.

That 2.6× amplification over the row itself is the number to remember. It explains why
backfills are bounded by things that consume WAL — replication, archiving, CDC — far more often
than by CPU.

### Cost 2: dead tuples, and a table that doubles

Every updated row leaves its old version behind as a dead tuple until vacuum reclaims it. Mid
statement, the table holds 521 million live rows and up to 521 million dead ones:

521M × 460 B = **240 GB of dead space**, on a 240 GB table. The heap grows to **480 GB**.

And here is the part that surprises people: `VACUUM` makes that space *reusable*, not
*returned*. The file stays 480 GB. Getting back to 240 GB requires `VACUUM FULL` or
`pg_repack`, which is a second multi-hour operation (doc 02's `L-04`, doc 03's `O-07`). So the
cost of a careless backfill is permanent until you schedule a second migration to undo it.

### Cost 3: one transaction, one snapshot, held for hours

At a sustained 90 MB/s of WAL throughput, 625 GB takes 625,000 ÷ 90 = **6,944 seconds, about
1.9 hours**. For all of that time:

- **Every row it has touched is locked.** Concurrent updates to those orders block. At 764
  row-writes/second, that is roughly 5.3 million blocked writes — in practice, a complete
  outage of order updates for two hours.
- **It holds one snapshot**, which pins the oldest transaction ID the database may consider
  dead. Vacuum cannot clean up *any* table in the database past that point. This is `B-07` and
  it is how a backfill on `orders` causes bloat on `customers`.

### Cost 4: the rollback, which differs enormously by engine

Suppose it fails at 95% — a disk alert, an operator cancelling it, a failover.

- **PostgreSQL**: the rollback is nearly instantaneous. MVCC means aborting marks the
  transaction dead and the new row versions simply become the dead ones. You lose the work and
  keep 240 GB of bloat and 625 GB of already-written WAL.
- **MySQL/InnoDB**: the rollback **replays the undo log**, row by row, and it typically takes
  **longer than the original operation**. A 1.9-hour update that fails at 95% can take 2–3
  hours to roll back, during which the table is degraded and you cannot cancel the rollback.
  This is the single most important operational difference between the two engines for
  backfills, and it is why MySQL shops chunk more aggressively than PostgreSQL shops.
- **Oracle**: similar to MySQL — undo-based rollback, and you may also exhaust the undo
  tablespace mid-statement and get `ORA-30036`, which fails the statement and then rolls back.

**The conclusion is not "chunk it."** It is: every one of these four costs is a function of
transaction size, and chunking is the one lever that reduces all four at once.

---

## The chunked backfill, built up piece by piece

Start with the simplest thing that is not wrong, then add each protection and say what it buys.

### Version 1: key-range chunking

```sql
UPDATE orders
   SET amount = amount_cents / 100.0, currency = 'USD'
 WHERE order_id >= $1 AND order_id < $2;
```

driven by a loop advancing `$1` and `$2` by 5,000 at a time.

Each chunk now writes 5,000 × 1.2 KB = **6 MB of WAL**, holds locks for a few milliseconds, and
leaves 2.3 MB of dead tuples that autovacuum can reclaim immediately.

#### B-02 · Chunking with `OFFSET`, which is quadratic

It is worth being explicit about why key ranges and not `LIMIT`/`OFFSET`, because `OFFSET` is
the intuitive choice:

```sql
-- NEVER do this:
UPDATE orders SET ... WHERE order_id IN (
  SELECT order_id FROM orders ORDER BY order_id LIMIT 5000 OFFSET $n
);
```

`OFFSET n` makes the database find and discard `n` rows before returning any. Chunk 1 discards
0 rows; chunk 100,000 discards 500 million. The total work is the sum of an arithmetic series:

> 5,000 × (1 + 2 + … + 104,200) ≈ 5,000 × 104,200² / 2 = **2.7 × 10¹³ row reads**

against 521 million for the key-range version. That is **52,000 times more work**, and it gets
slower as it progresses, so the first thousand chunks look fine and the job never finishes.

### Version 2: a predicate that makes re-running safe

```sql
UPDATE orders
   SET amount = amount_cents / 100.0, currency = 'USD'
 WHERE order_id >= $1 AND order_id < $2
   AND amount IS NULL;                      -- <-- added
```

This one clause does two jobs:

- **Idempotency.** Re-running a chunk is a no-op, so a crash between "chunk applied" and
  "cursor advanced" costs nothing.
- **It stops the backfill fighting the dual write.** This is `B-10` and it is the most
  consequential line in the whole job. During the backfill, live writes are setting `amount`
  and `currency` correctly for new and updated orders — including, in Riverbend's case, the
  first Canadian orders, where `currency = 'CAD'`. Without `AND amount IS NULL`, a chunk that
  reaches one of those rows overwrites `'CAD'` with `'USD'` derived from `amount_cents`. The
  backfill silently destroys correct data, and it does so for exactly the rows that matter most.

### Version 3: a cursor, so it can resume

Sixty-three hours of work will be interrupted. Persist progress in the database, in the same
transaction as the chunk, so the two cannot disagree:

```sql
CREATE TABLE backfill_progress (
  job_name     text PRIMARY KEY,
  next_key     bigint NOT NULL,
  max_key      bigint NOT NULL,
  rows_done    bigint NOT NULL DEFAULT 0,
  started_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  state        text NOT NULL DEFAULT 'running'   -- running | paused | done | aborted
);
```

```sql
BEGIN;
  UPDATE orders SET amount = amount_cents / 100.0, currency = 'USD'
   WHERE order_id >= $1 AND order_id < $2 AND amount IS NULL;

  UPDATE backfill_progress
     SET next_key = $2, rows_done = rows_done + <affected>, updated_at = now()
   WHERE job_name = 'orders_currency_2026_10';
COMMIT;
```

Putting the cursor update in the same transaction is the point. A cursor in a file, in
application memory, or in Redis can disagree with the database; a cursor in the same commit
cannot.

The `state` column is not decoration. It is the **pause switch**, and it is the first thing an
incident responder reaches for:

```sql
UPDATE backfill_progress SET state = 'paused' WHERE job_name = 'orders_currency_2026_10';
```

A backfill you cannot pause from SQL is a backfill you will have to kill, and killing is worse.

### Version 4: rate control with feedback

A fixed `pg_sleep(0.1)` between chunks is the common approach and it is wrong, for a simple
reason: **load is not constant**. The rate that is safe at 03:00 is dangerous at 14:00, and the
rate that is safe on an ordinary Tuesday is dangerous during a flash sale.

The backfill must read a health signal and throttle itself. The loop:

```
loop:
  signal = measure()
  if signal > critical:   state = 'aborted';  exit
  if signal > high:       sleep(backoff); backoff = min(backoff * 2, 60s); continue
  if signal < low:        backoff = max(backoff / 2, 50ms)
  run one chunk
  sleep(backoff)
```

Additive-decrease, multiplicative-increase on the backoff: back off fast when the system is
unhappy, recover slowly. The asymmetry matters — a backfill that speeds up as fast as it slows
down will oscillate.

**Which signal?** This is where most teams copy a control loop written for a different storage
architecture, and it is the most important judgement call in the document.

---

## Choosing the throttle signal, which depends on your architecture

| Signal | Right when | Wrong when |
|---|---|---|
| **Replica lag** | Self-managed PostgreSQL or MySQL with streaming replicas, where WAL/binlog apply is the bottleneck | **Aurora** — replicas share storage and lag is milliseconds regardless of load, so the signal never fires |
| **Primary CPU / IOPS** | Almost always a useful secondary | Alone, it misses lag and downstream effects entirely |
| **Dead tuple count / vacuum backlog** | Long PostgreSQL backfills | Short ones |
| **CDC / logical replication slot lag** | Any table with a change-data-capture consumer | Tables nobody streams |
| **Your own SLI** (checkout p99) | Always worth adding as the outer limit | As the only signal — it fires after users are already affected |

### Riverbend's binding constraint is not the database

Work through it, because the answer is not the obvious one.

**Is it WAL?** At a chunk rate of 5,000 rows every 2 seconds, WAL is 2,500 rows/s × 1.2 KB =
3 MB/s. The instance sustains 90 MB/s. **Not the constraint.**

**Is it replica lag?** `orders-db` is Aurora. Readers share the storage volume; there is no WAL
to apply. `AuroraReplicaLag` stays in the tens of milliseconds. **Not the constraint**, and a
team that copied a replica-lag throttle from a PostgreSQL blog post would run unthrottled.

**Is it CPU?** 2,500 row-updates/second against a baseline of 764 is a 3.3× increase in write
work on a 16-vCPU instance running at 35%. It pushes to about 60%. Uncomfortable, not fatal.
**Not the constraint.**

**Is it the change-data-capture stream?** Here is the answer.

Riverbend runs a Debezium connector on `orders` with `REPLICA IDENTITY FULL` (the fraud
pipeline needs before-images). Normal rate: 67 orders/s × 3.4 row-changes over an order's
lifetime = **228 change events/second**, at roughly 1.1 KB per event with both images.

An unthrottled backfill finishing in 8 hours emits:

> 521,000,000 ÷ 28,800 s = **18,090 events/second** — **79× the normal rate**

and in bytes: 521M × 1.1 KB = **573 GB** into a topic whose 72-hour retention was sized for
228 events/s × 1.1 KB × 259,200 s = **65 GB**.

Three things break, in this order:

1. **Consumers fall behind.** The search indexer and the lakehouse writer are provisioned for
   228 events/s with headroom to about 1,200. At 18,090 they lag immediately.
2. **Retention expires under the lagging consumers.** The topic rolls segments to stay inside
   its size bound, and a consumer that is 9 hours behind has its offset deleted. That is
   **permanent data loss in the downstream systems**, not a delay.
3. **The fraud pipeline sees 521 million "order changed" events** and scores them, because it
   cannot tell a backfill from a real change.

So the constraint is a Kafka topic in a different system, owned by a different team, and it is
79× away from tolerating the obvious plan.

**Setting the rate from it.** The pipeline already handles a 5.2× diurnal peak, so 10× normal
is a defensible ceiling: 2,280 events/second.

> 521,000,000 ÷ 2,300 rows/s = 226,500 seconds = **62.9 hours**, about **2.6 days**

That is where doc 01's 63 hours comes from. The database could do it in two hours; the
*ecosystem* needs 63.

### The alternative: route the backfill around CDC

63 hours is not a law of nature. If you can make the backfill invisible to the change stream,
you can run at 18,000 rows/s and finish in 8 hours. Four ways, with costs:

| Approach | How | Cost |
|---|---|---|
| **Pause the connector, re-snapshot after** | Stop Debezium, backfill at full speed, then run an incremental snapshot | Downstream is stale for the duration; the re-snapshot is itself 521M events, just at a time you choose |
| **Filter by transaction annotation** | `pg_logical_emit_message` a marker, or set `application_name`, and drop those events in a Kafka Streams filter | Needs a filter nobody forgets to remove; the marker must be inside the same transaction |
| **Signal table** | Debezium's signalling mechanism, or a `backfill_marker` column the consumer checks | An extra column, and consumers must cooperate |
| **Throttle to 10×** | Do nothing clever | 63 hours instead of 8 |

For a one-off migration, 63 hours of a background job is usually cheaper than the coordination
that the other three require. For a migration you will run on 4,096 shards (doc 11), 63 hours
each is not survivable and you must solve it properly.

**The general lesson, which generalises past CDC:** before setting a backfill rate, enumerate
everything that observes writes to the table. Triggers, CDC, materialised views, audit tables,
search indexers, cache invalidators, `updated_at`-based incremental syncs. **The backfill will
exercise all of them at once, and the slowest one sets your rate.**

### The other two systems, for contrast

**Ironvale's `txn_log`** — self-managed PostgreSQL 15, 2.4 billion rows, with streaming
replicas. Here replica lag *is* the signal:

- Measured single-process WAL apply ceiling on the standby: **45 MB/s**.
- Normal WAL generation: **12 MB/s**. Headroom: 33 MB/s.
- At 1.2 KB per row: 33 MB/s ÷ 1.2 KB = **27,500 rows/second**.
- 2.4 billion ÷ 27,500 = 87,273 s = **24.2 hours**.

Which is six times longer than the four-hour change window, and Ironvale's change process has
no category for a job that runs for a day. That collision is doc 18's subject.

**Lumen's sharded MySQL** — 1.34 billion rows per shard, 4,096 shards. Per-shard copy rate
measured at 22,000 rows/s (doc 03), giving 16.9 hours per shard. Running 64 shards in parallel:

> 4,096 ÷ 64 = 64 waves × 16.9 hours = **1,082 hours ≈ 45 days**

A migration whose execution time is measured in months changes what kind of plan it needs. It
is no longer an operation; it is a programme with a progress dashboard, an owner, and a status
report — which is doc 11.

---

## The failure catalogue

### B-03 · The chunk size that is wrong in either direction

**Too large.** 500,000-row chunks produce 600 MB WAL bursts, multi-second row locks, and
replica lag that sawtooths. On SQL Server, anything over ~5,000 rows escalates to a table lock
(doc 02's `L-12`), which silently defeats chunking entirely.

**Too small.** 50-row chunks mean 10.4 million transactions. Each commit costs a WAL flush and
an fsync — on this hardware about 0.8 ms — so the commits alone are 10.4M × 0.8 ms = **2.3
hours of pure overhead**, and the per-statement planning cost adds more.

**The sweet spot** is where chunk duration is 50–200 ms. Measure it rather than guessing:
start at 1,000, time ten chunks, scale toward a 100 ms target, and cap by WAL per chunk at
roughly 10 MB. For `orders` that lands at 5,000–8,000 rows.

**Adapt it at runtime.** Rows are not uniform; a chunk covering a range with wide `delivery_note`
values costs more than one without. Target a *duration*, not a row count.

### B-04 · The backfill that fires every trigger you forgot about

**What you see.** 521 million rows in an audit table. A webhook endpoint owned by another team
rate-limits you and then pages their on-call. Every row's `updated_at` is set to the backfill
window, and the nightly warehouse sync — which syncs rows where `updated_at > last_run` — tries
to move the entire table.

**Mechanism.** A backfill is an `UPDATE`, and the database cannot distinguish it from a real
one. Everything attached to writes fires:

- `AFTER UPDATE` triggers writing audit rows.
- An `updated_at` trigger, which is the one with the longest reach — **every downstream
  incremental sync keyed on `updated_at` now thinks the whole table changed.**
- Materialised view refresh queues.
- Cache invalidation messages.
- Outbox rows, which at Riverbend means the backfill writes into the same outbox the
  application uses, and the outbox publisher becomes the bottleneck.

**Confirm it.** Before starting, list them:

```sql
SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger
WHERE tgrelid = 'orders'::regclass AND NOT tgisinternal;
```

**Prevent.** Decide per trigger, deliberately:

- **Audit triggers**: usually disable for the backfill, with an explicit record that you did.
  `ALTER TABLE orders DISABLE TRIGGER audit_orders;` takes `ACCESS EXCLUSIVE` briefly, so it
  needs the `lock_timeout` treatment, and ⚠️ **it disables the trigger for live writes too**,
  not just yours. If live writes must stay audited, use a session-scoped guard inside the
  trigger instead: `IF current_setting('app.backfill', true) = 'on' THEN RETURN NEW; END IF;`
- **`updated_at`**: do **not** bump it. Set it explicitly to its existing value in the
  `UPDATE`, or guard the trigger as above. Otherwise you have scheduled a full re-sync of every
  downstream system for the same night.
- **Outbox**: the backfill should not write outbox rows at all. Its changes are not business
  events.

### B-05 · Autovacuum cannot keep up, and then it is behind on everything

**What you see.** Mid-backfill, `orders` grows past its expected size, index scans get slower,
and `pg_stat_user_tables` shows `n_dead_tup` climbing monotonically rather than sawtoothing.

**Mechanism.** Autovacuum triggers when dead tuples exceed
`autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor × n_live_tup`. With the default
scale factor of 0.2 on a 521-million-row table, that is **104 million dead tuples** before
autovacuum even starts. By then you have 48 GB of bloat and a vacuum that must scan 480 GB.

Then its throughput is capped by the cost delay. On PostgreSQL 15 defaults
(`autovacuum_vacuum_cost_delay = 2ms`, `autovacuum_vacuum_cost_limit = 200`), the budget is
200 credits per 2 ms = 100,000 credits/second. At roughly 6 credits per page processed
(a mix of hits, misses and dirties), that is ~16,600 pages/s × 8 KB = **133 MB/s** — which is
actually fine. On PostgreSQL 11 and earlier, where the default delay was 20 ms, the same
arithmetic gives **13 MB/s**, and that version of the folk wisdom ("autovacuum can never keep
up") is still widely repeated. Check your version before believing either.

The genuinely binding problem is usually not throughput but **`autovacuum_max_workers`** (three
by default) and the fact that one of them is now occupied by your 480 GB table for an hour,
leaving two for the rest of the database.

**Prevent.** Tune the table for the duration of the backfill, and set it back:

```sql
ALTER TABLE orders SET (
  autovacuum_vacuum_scale_factor = 0.01,   -- start at 5.2M dead tuples, not 104M
  autovacuum_vacuum_cost_delay = 0,        -- no throttling on THIS table
  autovacuum_vacuum_cost_limit = 2000
);
-- ... backfill ...
ALTER TABLE orders RESET (autovacuum_vacuum_scale_factor,
                          autovacuum_vacuum_cost_delay,
                          autovacuum_vacuum_cost_limit);
```

And **set `fillfactor` below 100 before the backfill** if you can afford the rewrite, or on the
new table if you are creating one. With `fillfactor = 85`, updates that do not touch an indexed
column can be HOT — the new row version goes on the same page and **no index entries are
written at all**. That removes 200 of the 1,221 bytes of WAL per row (16%) and removes index
bloat entirely. For a table you are about to update in its entirety, this is the single
highest-value preparation step.

### B-06 · The hour-long transaction that stops vacuum everywhere

**What you see.** Bloat and slow queries on tables the backfill never touches. Autovacuum logs
show `removable cutoff: ... , which was 2847283 XIDs old`.

**Mechanism.** Vacuum can only remove row versions that are invisible to **every** running
transaction. One long-running transaction pins the horizon for the whole database. A backfill
that holds a single transaction open — or a verification query doing a full-table checksum, or
a `pg_dump` — blocks cleanup everywhere.

At the extreme, this becomes transaction ID wraparound. PostgreSQL's XID counter is 32-bit;
when the oldest unfrozen transaction gets within roughly 2 billion of the current one,
PostgreSQL forces aggressive anti-wraparound vacuums, and **with fewer than one million XIDs
remaining it stops accepting write transactions entirely.** Recovering from that requires
single-user mode and is a multi-hour full outage.

**Confirm it.**

```sql
-- Oldest transaction, and what it is doing
SELECT pid, now() - xact_start AS age, state, left(query, 120)
FROM pg_stat_activity WHERE xact_start IS NOT NULL ORDER BY xact_start LIMIT 5;

-- How close to wraparound
SELECT datname, age(datfrozenxid),
       2147483648 - age(datfrozenxid) AS xids_remaining
FROM pg_database ORDER BY age(datfrozenxid) DESC;
```

**Prevent.** Keep every backfill transaction short — which chunking already does — and add
`idle_in_transaction_session_timeout`. The non-obvious case is **the verification query**: a
`SELECT md5(...) FROM orders` over 521 million rows holds a snapshot for its whole duration, so
verification has the same hazard as the backfill and needs the same chunking (doc 12).

**MySQL's equivalent** is the history list. Long transactions prevent the purge thread from
clearing old row versions from the undo log:

```sql
SHOW ENGINE INNODB STATUS\G   -- look for "History list length"
SELECT * FROM information_schema.INNODB_METRICS WHERE NAME = 'trx_rseg_history_len';
```

A history list length above a few hundred thousand means purge is behind. Lumen has seen the
undo tablespace grow to 400 GB during a long backfill, and InnoDB's undo tablespace does not
shrink on its own before MySQL 8.0's truncation, so that disk stays consumed.

### B-07 · The missing index, and the partial index that is its own migration

**What you see.** Each chunk takes 40 seconds instead of 80 milliseconds. `EXPLAIN` shows a
sequential scan.

**Mechanism.** The chunking predicate must be index-supported. `WHERE order_id >= $1 AND
order_id < $2` uses the primary key and is fine. But a backfill keyed on something else —
`WHERE created_at >= $1 AND created_at < $2 AND amount IS NULL`, or
`WHERE tenant_id = $1 AND amount IS NULL` — may have no usable index and scan 240 GB per chunk.

**The partial index that looks like the answer:**

```sql
CREATE INDEX CONCURRENTLY orders_backfill_todo ON orders (order_id) WHERE amount IS NULL;
```

It is genuinely useful — it shrinks as the backfill progresses, so finding remaining work gets
cheaper, and at the end it is empty, which makes "are we done?" an index-only lookup rather
than a full scan. But understand what it costs:

- Building it when **every** row matches means building a full-size index: 521M entries at ~32
  bytes = **17 GB**, taking 41 minutes under `CREATE INDEX CONCURRENTLY`.
- Maintaining it during the backfill means every chunk also deletes 5,000 index entries,
  adding WAL and index bloat.
- It must be dropped afterwards, which is another step to forget.

**When it is worth it:** when the predicate cannot otherwise be indexed, or when the backfill
is sparse (only 3% of rows need work, so the index is small from the start). **When it is
not:** when you are backfilling every row and can chunk on the primary key, which is the common
case. Then the predicate `AND amount IS NULL` is just a cheap filter applied to rows you were
reading anyway.

### B-08 · The plan that changes halfway through

**What you see.** The backfill runs at a steady 2,300 rows/s for nine hours, then abruptly
drops to 400 rows/s with no change in load.

**Mechanism.** Autoanalyze ran. At the start, `amount IS NULL` matched 100% of rows and the
planner chose a plan accordingly. After analyze samples a table where `amount` is now 60%
populated, the selectivity estimate changes and the planner may switch the chunk query from an
index range scan to something else, or start using the partial index when it was better not to.

**Confirm it.** Log `EXPLAIN (ANALYZE, BUFFERS)` for one chunk every thousand, and diff the
plans. A backfill that does not log its plan periodically cannot tell you this happened.

**Prevent.** Keep the chunk query's shape stable and simple enough that only one plan is
sensible — a primary-key range scan with a filter. Resist the temptation to make the query
clever; a backfill query should be boring on purpose.

### B-09 · The backfill that cannot finish because the table grows faster

**What you see.** Progress stalls at 88%, where "progress" is `rows_done / total_rows` and
`total_rows` keeps increasing.

**Mechanism.** You are backfilling 521 million rows at 2,300/s while the table gains 5.79
million rows/day. Over 63 hours it gains 1.5 million. That is fine — 2,300/s is 199 million/day
against 5.8 million/day of growth, a 34:1 ratio.

It is not fine on a table where the ratio is closer. If your throttled rate is 400 rows/s
(34.5 million/day) on a table growing 20 million rows/day, you converge at 14.5 million/day net
and a 521-million-row backfill takes **36 days** — assuming growth does not increase.

**Prevent — and this is the structural fix.** Make new rows arrive *already correct*, so the
backfill has a fixed target. That is exactly what phase 2 (dual write) is for, and it is the
reason doc 01 puts dual-write **before** backfill rather than after. With the dual write
deployed, `total_rows` where `amount IS NULL` is a monotonically decreasing set fixed at the
moment of the phase-2 deploy, and the backfill has a real finish line.

If you find yourself backfilling without a dual write in place, stop and deploy the dual write
first. You are otherwise chasing a moving target and the arithmetic may never close.

### B-10 · Sparse keys, and ten thousand empty chunks

**What you see.** The job reports 104,200 chunks. 38,000 of them affect zero rows, and the run
takes 40% longer than estimated.

**Mechanism.** `order_id` is not dense. Riverbend archives orders older than 90 days, so the
live key space has large gaps, and a `BETWEEN` chunk over an archived range does nothing but
pay for a round trip and a transaction.

**Prevent.** Chunk by *rows*, not by *key arithmetic* — advance the cursor to the next real
key:

```sql
-- Find the end of the next chunk of 5,000 ACTUAL rows
SELECT order_id FROM orders
 WHERE order_id >= $1 AND amount IS NULL
 ORDER BY order_id OFFSET 5000 LIMIT 1;
```

The `OFFSET 5000` here is bounded and index-supported, so it does not reintroduce `B-02`'s
problem — it reads 5,000 index entries, not 500 million rows. If it returns nothing, you are in
the last chunk.

---

## Running it: the operational shape

A backfill is a long-lived job, so it needs the properties of one.

**Where it runs.** A Kubernetes `Job` or a dedicated worker, not a `kubectl exec` on somebody's
laptop and not a `psql` session in a terminal that will close. It must survive node drains,
which means it must be restartable, which the cursor already makes true.

**What it exposes.** At minimum: rows done, rows remaining, current rate, current backoff,
estimated completion, and the value of the throttle signal it is reacting to. The estimate
matters more than it sounds — it is what tells you at hour four that this is a 63-hour job and
not an 8-hour one, while you can still change the plan.

**How it is stopped.** The `state` column, not a `kill`. And the runbook entry should say
"pause the backfill" as a *first* action during any unrelated incident on that database, so the
responder does not have to reason about whether it is relevant.

**What it logs.** One line per thousand chunks with the numbers above, plus a periodic
`EXPLAIN (ANALYZE)` from `B-08`. Not one line per chunk: 104,200 log lines about a background
job is a cost to the log platform for no benefit.

---

## What to take away

1. **A PostgreSQL `UPDATE` writes about 1.2 KB of WAL per row** on `orders` — 2.6× the row
   itself — because it writes a whole new tuple, index entries, and amortised full-page images.
   521 million rows is **625 GB of WAL**.
2. **A single-statement backfill leaves 240 GB of dead tuples and the table stays at double
   size**, because vacuum makes space reusable, not returned.
3. **PostgreSQL rolls back instantly; MySQL and Oracle replay undo and take longer than the
   original.** This is why MySQL shops chunk harder.
4. **Never use `OFFSET` to chunk.** On this table it is 52,000× more work and gets slower as it
   goes, so the first thousand chunks look fine.
5. **`AND new_col IS NULL` in the backfill predicate is load-bearing**: it makes chunks
   idempotent and it stops the backfill overwriting correct live writes with stale derivations.
6. **Put the cursor update in the same transaction as the chunk.** A cursor anywhere else can
   disagree with the data.
7. **Give the job a `state` column so it can be paused with one `UPDATE`.** A backfill you can
   only kill is a backfill that will be killed badly.
8. **Throttle on a measured signal with asymmetric backoff**, and pick the signal from your
   architecture — replica lag is meaningless on Aurora and essential on streaming replicas.
9. **Riverbend's binding constraint is a Kafka topic in another team's system**, 79× away from
   tolerating the obvious plan. The database could finish in two hours; the ecosystem needs 63.
10. **Enumerate everything that observes writes before setting a rate** — triggers, CDC,
    `updated_at`-based syncs, outbox, cache invalidation. The slowest one sets the rate.
11. **Do not bump `updated_at`**, or you have scheduled a full re-sync of every downstream
    system keyed on it.
12. **Set `fillfactor` below 100 before a whole-table backfill** so updates can be HOT. It
    removes the index-entry WAL (16%) and all index bloat.
13. **Tune `autovacuum_vacuum_scale_factor` down to 0.01 for the duration**, because the default
    0.2 means autovacuum does not start until 104 million dead tuples.
14. **A long transaction blocks vacuum on every table in the database**, and the verification
    query has the same hazard as the backfill.
15. **Target a chunk *duration* of 50–200 ms, not a row count**, and cap WAL per chunk at
    about 10 MB.
16. **Deploy the dual write before the backfill**, or the target set keeps growing and the
    arithmetic may never close.
17. **At Lumen's 4,096 shards the same job is 45 days.** Execution time measured in months is
    not an operation; it is a programme, and it needs doc 11's machinery.

Next: [07-dual-writes-shadow-reads-and-the-transition.md](07-dual-writes-shadow-reads-and-the-transition.md),
which covers the phase that makes the backfill's finish line hold still.
