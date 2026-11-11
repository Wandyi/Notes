# Golden Defaults and Checklists

The reference page. Triage order first, because that is what you need at 02:40; then the
configuration worth adopting; then the templates; then what to build at your scale, and the
numbers worth carrying in your head.

---

## Triage order

```
1. Did anything change in the last 24 hours?
     SELECT version, description, installed_on, success FROM flyway_schema_history
      WHERE installed_on > now() - interval '24 hours' ORDER BY installed_on DESC;
     SELECT job_name, state, rows_done, updated_at FROM backfill_progress
      WHERE state IN ('running','paused');

2. If a backfill is running: PAUSE IT. Cheap, reversible, removes a variable.
     UPDATE backfill_progress SET state = 'paused' WHERE state = 'running';

3. Identify the shape:
     one table, all queries timing out, onset in seconds   -> LOCK STORM, R1
     unrelated tables degrading over minutes               -> RESOURCE, R2
     a subset of pods erroring on 42703 / 0A000            -> COMPATIBILITY, R6
     pipeline blocked, tool says dirty or locked           -> STUCK, R4
     no symptom, a human noticed a wrong number            -> CORRUPTION, doc 12

4. Lock storm:   cancel the DDL, NOT the blocker. pg_cancel_backend.
   Resource:     find which one - lag, WAL, slots, dead tuples, CDC, connections.
   Compatibility: OLD pods failing -> ROLL FORWARD.  NEW pods failing -> revert, migrate, redeploy.
   Stuck:        record the actual schema state BEFORE touching anything.

5. Never:
     clear a dirty flag before reconciling the schema
     revert the deploy when old pods are failing
     kill a long UPDATE on MySQL or Oracle (undo rollback takes longer)
     restore production to recover a dropped column (restore a clone)
     delete files from pg_wal/
```

---

## Golden configuration

### Every PostgreSQL migration session

```sql
SET application_name = 'migrate:V214';   -- so it is identifiable in pg_stat_activity
SET lock_timeout = '2s';                 -- never queue; fail and retry
SET statement_timeout = '0';             -- the statement itself may legitimately run long
SET idle_in_transaction_session_timeout = '60s';
```

Wrapped in a jittered retry loop (doc 02's `L-01`). If your tool does not do this, wrap the
tool.

### PostgreSQL cluster settings that matter for migrations

```ini
# Fail a stuck session rather than letting it block DDL forever.
idle_in_transaction_session_timeout = 60s     # set on application roles, not globally

# Lose a subscriber rather than a primary. The default is the other way round.
max_slot_wal_keep_size = 200GB

# Make lock waits visible instead of mysterious.
log_lock_waits = on
deadlock_timeout = 1s
log_min_duration_statement = 1000

# Read-only roles cannot hold the vacuum horizon open indefinitely.
ALTER ROLE app_ro SET statement_timeout = '120s';
ALTER ROLE app_ro SET idle_in_transaction_session_timeout = '60s';
```

### PostgreSQL table settings during a backfill

```sql
ALTER TABLE orders SET (
  autovacuum_vacuum_scale_factor = 0.01,   -- 5.2M dead tuples, not the default 104M
  autovacuum_vacuum_cost_delay   = 0,      -- no throttling on this table
  autovacuum_vacuum_cost_limit   = 2000
);
-- afterwards:
ALTER TABLE orders RESET (autovacuum_vacuum_scale_factor,
                          autovacuum_vacuum_cost_delay,
                          autovacuum_vacuum_cost_limit);
```

And, where you can afford the rewrite beforehand: `fillfactor = 85`, so whole-table updates
can be HOT — removing 16% of the WAL and all of the index bloat.

### Three roles, every database

```sql
CREATE ROLE app_owner LOGIN;    -- pipeline only; owns objects
CREATE ROLE app_rw    LOGIN;    -- the application; no CREATE
CREATE ROLE app_ro    LOGIN;    -- analysts, BI

GRANT USAGE ON SCHEMA public TO app_rw, app_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_ro;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO app_rw;

-- The lines everyone forgets. Without them, the migration succeeds and the
-- application cannot see the new table.
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT USAGE ON SEQUENCES TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO app_ro;
```

### MySQL

```sql
SET SESSION lock_wait_timeout = 5;           -- the default is ONE YEAR
ALTER TABLE orders ADD COLUMN c char(3), ALGORITHM=INSTANT;   -- state it, so it fails loudly
```

```ini
innodb_online_alter_log_max_size = 1G        # raise before a long INPLACE
binlog_expire_logs_seconds       = <3x your longest expected gh-ost copy>
binlog_transaction_dependency_tracking = WRITESET   # on the SOURCE - this is what
                                                    # makes parallel replica apply
                                                    # help a chunked backfill
sql_mode = STRICT_TRANS_TABLES,...           # or NOT NULL does not mean NOT NULL
```

### gh-ost

```
--max-lag-millis=1500
--throttle-control-replicas=<the replicas your APPLICATION reads, not the one gh-ost reads>
--max-load=Threads_running=40
--critical-load=Threads_running=100
--critical-load-interval-millis=1000
--postpone-cut-over-flag-file=/tmp/gh-ost.postpone   # separate the long part from the risky part
--throttle-query='SELECT checkout_p99_over_200ms()'  # throttle on YOUR SLI
```

### Backfill job defaults

```
chunk sizing          target 100 ms duration, cap 10 MB of WAL per chunk
                      (adapt at runtime; rows are not uniform)
predicate             ... AND new_col IS NULL        <- load-bearing
cursor                updated in the SAME transaction as the chunk
pause                 a `state` column, pausable with one UPDATE
backoff               min 50 ms, max 60 s, x2 on pressure, /2 on recovery
abort                 three bands: hard (stops, pages), soft (pauses, pages), advisory (logs)
logging               one line per 1,000 chunks; EXPLAIN ANALYZE every 1,000
exposes               rows done, rate, backoff, ETA, and the throttle signal's value
```

---

## The migration file header

```sql
-- V214__orders_add_currency.sql
--
-- direction:   expand            (pipeline runs this BEFORE the deploy)
-- phase:       1 of 6            (plan: RVB-4471)
-- class:       1                 (metadata-only, additive, reversible)
-- tables:      orders (521M rows, 336 GB, as of 2026-10-01)
-- rewrite:     no                (metadata-only on PG 11+; verified on clone 2026-10-02)
-- duration:    ~10 ms            (measured on clone orders-db-clone-1002)
-- rollback:    V214_down.sql     (drops the columns; safe until phase 6a)
-- consumers:   checked against the registry 2026-10-01; debezium + warehouse
--              need the matching subscriber DDL FIRST (V214a)
-- reviewed-by: @vk, @dba-oncall

SET lock_timeout = '2s';
SET statement_timeout = '30s';

ALTER TABLE orders ADD COLUMN amount   numeric(14,2);
ALTER TABLE orders ADD COLUMN currency char(3);

-- Deliberately omitted, each for a reason:
--   NOT NULL      - needs the backfill first (phase 3)
--   DEFAULT 'USD' - would make every pre-existing row silently claim to be USD
--   an index      - CREATE INDEX CONCURRENTLY cannot run in a transaction; it is V215
```

---

## The migration plan template

```
MIGRATION PLAN — <ticket>

1. WHAT AND WHY
   One paragraph. What changes, and what breaks if it does not happen.

2. CURRENT STATE
   Table, row count, size, write rate, read rate. Measured, with a date.

3. CONSUMERS                                       (doc 05's C-14, doc 18)
   Every reader and writer, with an owner and a sign-off.
   The compatibility window = the MAXIMUM over all of them.

4. PHASES
   Phase | What | Deploy? | Duration | Measured on | Date
   Each phase's direction (expand / contract) and therefore its pipeline ordering.

5. BACKFILL                                        (doc 06)
   Rows, chunk size, throttle signal and why that one, expected rate and duration.
   What observes writes to this table, and which is the slowest.

6. VERIFICATION                                    (doc 12)
   Method | Coverage | Confidence | When it runs
   Known exceptions. Residual risk. Both stated explicitly.

7. ABORT CRITERIA                                  (doc 16)
   Hard / soft / advisory, as numbers, each with its derivation.

8. ROLLBACK                                        (doc 13)
   Per phase: mechanism, duration (measured), data loss, tested-on, owner.
   THE POINT OF NO RETURN: which step, what reversal costs, who decides.

9. CALENDAR                                        (doc 18)
   Every phase's date checked against freezes. Including phase 6b, 92 days out.

10. RUNBOOK
   The pause statement, copy-pasteable.
   The three most likely failures and their responses.
   Who to call, and the one thing that needs them: crossing the point of no return.
```

---

## The pre-flight gate

The tool should **refuse**, not warn:

```
[ ] oldest running transaction < 60 s
[ ] no idle-in-transaction session older than 5 minutes
[ ] replica lag < 5 s on every replica
[ ] every replication slot active, wal_status = 'reserved'
[ ] free disk > 20%, and > 2x the table if this rewrites
[ ] dead tuple ratio on the target table < 20%
[ ] no active incident above sev-3
[ ] no other structural migration registered against this table
[ ] the migration's measured duration + 100% margin fits the available window
```

---

## What to build, by scale

### Tier 1 — under ~10 GB, one repository, no uncontrolled clients

Four things, and then stop. The rest of this collection is describing problems you do not have.

```
[ ] lock_timeout + retry loop in the migration runner
[ ] a linter in CI
[ ] expand-before-deploy, contract-after-deploy, as separate pipeline steps
[ ] three database roles, with ALTER DEFAULT PRIVILEGES
```

### Tier 2 — ~10 GB to ~1 TB, several services, replicas, probably CDC

Add:

```
[ ] the compatibility CI job: previous release's tests against the new schema
[ ] nightly schema diff against production -> alert on any difference
[ ] migration + deploy annotations on every dashboard
[ ] a migration registry with a partial unique index (one structural change per table)
[ ] the pre-flight gate above
[ ] a backfill harness: chunked, resumable, throttled, pausable from SQL
[ ] verification: invariants + chunked checksums, as a standard step
[ ] a consumer registry, built from two weeks of sampled activity plus a human pass
[ ] max_slot_wal_keep_size, and an alert on slot retention
```

### Tier 3 — multi-terabyte, sharded, multi-region, or regulated

Add:

```
[ ] a fleet reconciler with per-target state, waves, and bidirectional drive
[ ] standing schema-drift detection, forever, independent of any rollout
[ ] risk classification computed automatically from statements + table size
[ ] production-sized clone rehearsals with timings, for every class 3 change
[ ] reverse replication for every store cutover, with a stated window
[ ] min/max fleet schema version as a metric, gating deploys
[ ] gamedays in the week before a large migration
[ ] the rehearsal clone in the compliance inventory, with automatic deletion
[ ] the erasure process updated as a step in every plan that adds personal data
```

---

## Numbers worth remembering

Derived in this collection; carry them as reference points, not as your own figures.

| Number | What it is |
|---|---|
| **~1.2 KB** | WAL per updated row on a 460-byte table with five indexes — **2.6× the row itself** |
| **625 GB** | WAL for a 521-million-row backfill |
| **240 GB** | Dead tuples from the same backfill; the table doubles and stays doubled |
| **17** | Rows per 8 KB page at 460 bytes |
| **1.4 s** | Time to exhaust a 480-connection pool at 350 req/s once queries stop completing |
| **8 ms vs 50 min** | `ADD COLUMN ... DEFAULT` on PostgreSQL 11+ versus a volatile default |
| **80–120 MB/s** | Table rewrite throughput on a `db.r6g.4xlarge` |
| **2×** | Free space a rewrite needs, plus WAL |
| **5,000** | SQL Server's lock escalation threshold — chunk below it |
| **1 year** | MySQL's default `lock_wait_timeout` |
| **64** | MySQL instant-ALTER row versions before a rebuild is forced |
| **89%** | Keys that move when hash-modulo sharding goes from 8 shards to 9 |
| **50%** | Keys that move when it doubles — which is why such systems only double |
| **(G − A) / A** | Seconds of replica lag gained per second, at generation rate G and apply rate A |
| **79×** | Riverbend's CDC event rate during an unthrottled backfill |
| **63 hours** | The same backfill throttled to 10× normal CDC rate |
| **460,500** | Rows to sample for 99% confidence that under 1 in 100,000 differ — **independent of table size** |
| **5,210** | Wrong rows that still permits, on 521 million |
| **< 5 min** | Full chunked checksum of a 240 GB table with eight workers |
| **482,400** | Orders lost by restoring to a two-hour-old point at 67 orders/s |
| **92 days** | Riverbend's real compatibility window, set by a quarterly export |
| **7.7 min** | Ironvale's change-window capacity per change — the impossible number |
| **114 min** | The same windows, used only for the 7% of changes that need them |
| **45 days** | A per-shard backfill across 4,096 shards, 64 in parallel |
| **7.5 million** | `pg_attribute` rows at 4,100 tenant schemas × 40 tables |
| **~20:1** | Calendar time to machine time on a typical column migration |

---

## The ten rules, if you remember nothing else

1. **Two versions of your code run against one schema.** Every schema state must work with
   both.
2. **Never change the meaning of an existing thing.** Add, move, remove — with a deploy
   boundary between each.
3. **`SET lock_timeout` plus a jittered retry.** One line, and it converts the most common
   outage into a clean failure.
4. **Expand before deploy; contract after deploy.** No tool that couples them gets both right.
5. **The backfill's rate is set by the slowest thing that observes writes**, which is usually
   not the database.
6. **`AND new_col IS NULL`** — idempotence, and it stops the backfill destroying correct live
   writes.
7. **Verification must be able to fail.** Not the backfill's expression, not the backfill's
   cursor, and with the known exceptions written down.
8. **Name the point of no return, with a measured reversal cost and a person.** It is "stop
   writing the old column", weeks before "drop it".
9. **"Complete" is a count of zero, never a percentage** — and drift detection runs forever,
   because a restore silently undoes every migration applied after the backup was taken.
10. **During the incident, the intuitive action is often the wrong one.** Cancel the DDL not
    the blocker; roll forward not back; reconcile the schema before clearing the flag.

---

Back to [README.md](README.md).
