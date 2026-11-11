# Observability for Migrations

Most of doc 15 exists because somebody found out too late. This document is about finding out
early, and it starts with the single highest-return practice in the whole collection, which
costs about an hour to implement.

---

## Annotate every dashboard with change events

Put a vertical line on every graph at the moment of every deploy, every migration, every
backfill start and stop, and every feature-flag change.

That is it. That is the practice.

**Why it is worth more than any metric you could add.** During an incident, the question a
responder is trying to answer is "what is different?" A dashboard without change markers makes
them reconstruct the answer from four other systems, under time pressure, usually by asking in
a channel. A dashboard with markers answers it in the first glance at the first graph:

```
checkout p99 latency
   200ms |                                    ╭──────
         |                                   ╱
   100ms |                              ╭───╯
         |──────────────────────────────╯
     0   └────────────┬─────────────────┬──────────────
                   02:00              02:11
              │ deploy v8841 │   │ V214 backfill start │
```

The responder does not need to know what V214 is. They know what to pause.

**How to implement it.** Grafana annotations, Datadog events, or whatever your system has, fed
by one webhook from the migration pipeline:

```json
{ "time": "2026-10-09T02:11:04Z",
  "tags": ["migration", "orders-db", "backfill"],
  "title": "V214 backfill started",
  "text": "orders.amount/currency, 521M rows, rate 2300/s, est 63h.
           Pause: UPDATE backfill_progress SET state='paused' WHERE job_name='orders_currency_2026_10';
           Owner: @vk. Runbook: wiki/RVB-4471" }
```

**Put the pause command in the annotation text.** The responder reads it on hover and does not
need to find the runbook. This is a small thing and it reliably saves several minutes at the
exact moment minutes are expensive.

---

## What to watch, in three phases

### Before: the pre-flight gate

These are checks, not dashboards — the migration tool should refuse to start if any fails:

| Check | Threshold | Why |
|---|---|---|
| Oldest running transaction | < 60 s | A long transaction will defeat `lock_timeout` retries (`L-02`) and block CIC (`L-05`) |
| Idle-in-transaction sessions | 0 older than 5 minutes | Same |
| Replica lag, all replicas | < 5 s | Starting with lag means less headroom than you planned |
| Replication slots | all `active`, `wal_status = 'reserved'` | A stopped subscriber plus a backfill fills the disk (`R-06`) |
| Free disk | > 2× the table for a rewrite; > 20% otherwise | `L-04` |
| Dead tuple ratio on the target table | < 20% | Starting bloated makes it worse |
| Active incidents above sev-3 | none | `P-04` |
| Other structural migrations on this table | none | `P-03` |

Automating these as a refusal rather than a warning is the difference between a gate and a
checklist. A warning gets acknowledged.

### During: the seven signals

```
1. Lock waits        count of sessions with wait_event_type='Lock', and the oldest
2. Oldest transaction  age of the oldest open transaction, any session
3. Replica lag       replay_lag per replica, max across all
4. WAL rate          bytes/s generated; and slot retention bytes
5. Dead tuples       n_dead_tup on the target table, and autovacuum activity
6. Error rate by SQLSTATE   especially 42703, 0A000, 55P03, 40001, 23505
7. The job's own progress   rows done, rate, current backoff, ETA
```

Signal 7 is the one teams forget to export, and it is the one that tells you at hour four that
this is a 63-hour job rather than an 8-hour one — while you can still change the plan.

**Signal 6 deserves its own treatment** because the codes are diagnostic:

| SQLSTATE | Meaning | What it tells you |
|---|---|---|
| `42703` | undefined column | A compatibility break. Direction tells you which side (`R6`) |
| `42P01` | undefined table | Same, for a table |
| `0A000` | feature not supported | Almost always "cached plan must not change result type" (`C-02`) |
| `55P03` | lock not available | Your `lock_timeout` fired. **This is good** — it means the protection worked |
| `40001` | serialization failure | Contention, or a replica recovery conflict (`R-02`) |
| `57014` | query canceled | Someone cancelled, or `statement_timeout` |
| `23505` | unique violation | A constraint you added found real duplicates, or a sequence collision after a cutover (`R-09`) |
| `23502` | not null violation | A `NOT NULL` added before the dual write covered every path |
| `53100` | disk full | `L-04` or `R-06` |
| `25006` | read-only transaction | The database has stopped accepting writes. Usually disk, sometimes wraparound |

MySQL equivalents worth the same treatment: `1054` unknown column, `1146` unknown table,
`1205` lock wait timeout, `1213` deadlock, `1799` online alter log overflow, `1062` duplicate
entry.

**Alert on `42703` and `0A000` at *any* non-zero rate during a migration window.** They are
never normal, they are always a compatibility problem, and a threshold-based alert on them
wastes the signal.

### After: the watch period

"Done" is one full business cycle later (doc 15's `P-07`). What to watch, and for how long:

| Signal | Duration | Why this long |
|---|---|---|
| Error rate by SQLSTATE | 24 h | Catches the hourly and daily jobs |
| p99 latency on affected endpoints | 24 h | Plan changes show up under real load, not at 03:00 |
| Table and index size | 7 days | Bloat from the backfill resolves, or does not |
| Divergence rate (production comparison) | Until contract | Doc 07 |
| Autovacuum activity on the table | 7 days | Confirms it caught up |
| The quarterly and monthly consumers | 92 days | Doc 05's `C-14`. This is why the soak is 92 days |

---

## Abort criteria, written as numbers before you start

The person watching at 03:00 should not have to exercise judgement. Write the criteria into
the plan, and where possible into the job:

```
ABORT CRITERIA — V214 backfill

  Hard abort (the job stops itself, pages, and does not resume):
    - any replica replay_lag > 120 s
    - CDC consumer lag on orders.created > 300 s
    - primary CPU > 85% for 5 consecutive minutes
    - checkout p99 > 400 ms for 3 consecutive minutes
    - any non-zero rate of SQLSTATE 42703 or 0A000
    - free disk on the data volume < 15%

  Soft abort (the job pauses, pages, and can be resumed by on-call):
    - replica replay_lag > 30 s
    - CDC consumer lag > 60 s
    - primary CPU > 70% for 10 minutes
    - dead tuples on orders > 80,000,000

  Advisory (log and continue):
    - chunk duration > 400 ms
    - backoff at maximum for > 15 minutes
```

**Three bands, not two.** A single threshold forces every anomaly into "stop everything" or
"ignore", and the result is thresholds set so high they never fire. The soft band is where most
real events land, and it is the one that makes the hard band credible.

**The numbers must come from somewhere.** Each of the above is either a measured baseline ×
a multiple (CPU, latency), a capacity limit you derived (CDC lag against topic retention), or
a correctness boundary (any 42703). A threshold with no derivation behind it gets argued with
at 03:00, which is the worst time to have that argument.

---

## The single-glance dashboard

One screen, and the ordering matters — the top row answers "is this hurting anyone", the
second answers "why", the third answers "how much longer".

```
┌─ IS IT HURTING ANYONE ────────────────────────────────────────────────┐
│  checkout p99      142 ms  (baseline 96 ms, +48%)        ▁▂▃▅▅▅▅      │
│  error rate        0.02%   (baseline 0.01%)              ▁▁▁▁▁▂▁      │
│  SQLSTATE 42703    0       SQLSTATE 0A000    0                        │
└───────────────────────────────────────────────────────────────────────┘
┌─ WHY ─────────────────────────────────────────────────────────────────┐
│  lock waits        0       oldest txn       1.2 s                     │
│  replica lag       max 1.8 s  (soft abort at 30 s)       ▂▃▂▃▄▃▂      │
│  CDC lag           41 s       (soft abort at 60 s)       ▃▄▅▄▅▅▆  ←   │
│  WAL rate          3.1 MB/s   slot retention   2.1 GB                 │
│  dead tuples       14.2 M     last autovacuum  4 min ago              │
│  primary CPU       58%        connections  210 / 600                  │
└───────────────────────────────────────────────────────────────────────┘
┌─ HOW MUCH LONGER ─────────────────────────────────────────────────────┐
│  V214 backfill   rows 198,400,000 / 521,000,000   (38.1%)             │
│  rate 2,280/s    backoff 120 ms    chunk p99 88 ms                    │
│  started 2026-10-09 02:11Z     ETA 2026-10-11 17:40Z  (+2 h vs plan)  │
│  state: running     pause: UPDATE backfill_progress SET state='paused'│
└───────────────────────────────────────────────────────────────────────┘
```

Four design choices in there are deliberate:

- **Every number carries its baseline or its threshold.** "58% CPU" means nothing; "58%,
  baseline 35%, soft abort at 70%" means something. This is the difference between a dashboard
  and a wall of numbers.
- **The arrow marks the signal closest to its threshold.** During an incident, the responder's
  eye should be directed, not left to scan.
- **The ETA is shown against the plan.** "+2 h vs plan" is the number that triggers a
  conversation; "ETA Saturday" does not.
- **The pause command is on the dashboard.** Same argument as the annotation.

---

## Which metric misleads during which failure

The reason to know this is that each failure has a metric that looks *reassuring* while it is
happening, and responders anchor on reassuring metrics.

| Failure | The metric that misleads | Why | What to look at instead |
|---|---|---|---|
| **Lock storm** (`L-01`) | **CPU is low.** Nothing is running | Blocked queries consume no CPU, so the database looks idle while completely unavailable | Count of sessions with `wait_event_type = 'Lock'` |
| **Aurora backfill** | **Replica lag is ~0** | Shared storage; lag is not the mechanism here | CDC lag, reader cache hit ratio, primary CPU |
| **CDC flood** (`B-04`) | **Database metrics all normal** | The database is fine; the damage is in Kafka | Consumer group lag, topic retention headroom |
| **Stopped subscriber** (`R-06`) | **Data size is flat** | WAL growth is not table growth | `pg_ls_waldir()` size, slot retention bytes |
| **Compatibility break** (`C-01`) | **Aggregate error rate is low** | 4% of pods failing is 4% of requests, which may be inside the alert threshold | Error rate **grouped by pod**, and by SQLSTATE |
| **Silent corruption** | **Everything** | There is no signal | Doc 12's verification is the only detector |
| **Partial fleet rollout** (`F-01`) | **"99.7% complete"** | The aggregate hides the tail | Count of targets not in the desired state |
| **Invalid index** (`L-05`) | **Query latency unchanged** | Reads are fine; writes pay for an index nothing uses | Write latency, and `pg_index WHERE NOT indisvalid` |
| **Bloat** (`B-05`) | **Row count is correct** | Dead tuples are not rows | `n_dead_tup`, table size versus expected |

The recurring pattern: **the misleading metric is usually the one measuring the subsystem that
is working.** A lock storm's CPU graph is accurately reporting that the CPU is fine. Building
the habit of asking "which component would this metric be silent about?" is most of the skill.

---

## Metrics for the programme, not the migration

Beyond any single migration, four numbers tell you whether your migration practice is healthy.
They are worth collecting because they are the input to doc 18's argument about process.

**1. Schema change lead time.** Days from "migration merged" to "applied in production", at
the median and p90. Riverbend's median is 1.2 days; Ironvale's is 63. This is the number that
makes the case for changing a process, and it is a DORA-adjacent metric that leadership
already understands.

**2. Migration-caused incident rate.** Incidents attributed to a schema or data migration, as
a fraction of all incidents and as a rate per hundred migrations. The second denominator
matters: a team doing more migrations will cause more incidents in absolute terms, and the rate
is what tells you whether the practice is improving.

**3. Rollback-and-abort rate.** How often a migration is aborted or rolled back. **A rate of
zero is a problem**, not a success — it means either abort criteria that never fire or a
culture where stopping is treated as failure. A healthy rate is a few per cent.

**4. Drift count.** The number of databases whose schema differs from the expected one, over
time. This should trend to zero and stay there. A drift count that spikes after every incident
tells you the emergency path bypasses the migration pipeline, which is doc 18's central
problem at Ironvale.

A useful fifth, if you can get it: **time from a migration defect being introduced to being
detected.** For `C-01`-style compatibility breaks it is minutes. For silent corruption it is
weeks, and measuring it is the strongest available argument for funding verification.

---

## What to take away

1. **Annotate every dashboard with deploys, migrations, backfills and flag flips.** It is an
   hour of work and it is the highest-return practice in this collection.
2. **Put the pause command in the annotation text**, so the responder does not have to find the
   runbook.
3. **Pre-flight checks should refuse, not warn.** A warning gets acknowledged.
4. **Export the job's own progress and ETA.** It is what tells you at hour four that the plan
   was wrong, while you can still change it.
5. **Alert on SQLSTATE `42703` and `0A000` at any non-zero rate during a migration.** They are
   never normal, so a threshold wastes the signal.
6. **`55P03 lock_not_available` is good news** — your `lock_timeout` worked. Count it, do not
   page on it.
7. **Write abort criteria as numbers in three bands** — hard, soft, advisory — before the
   migration starts. Two bands produce thresholds so high they never fire.
8. **Every threshold needs a derivation**, or it gets argued with at 03:00.
9. **Every number on the dashboard carries its baseline or its threshold.** Without one, it is
   not information.
10. **Each failure has a metric that looks reassuring**, and it is usually the one measuring
    the subsystem that is working. A lock storm shows low CPU.
11. **Group the error rate by pod.** A compatibility break affecting 4% of pods hides inside an
    aggregate.
12. **Silent corruption has no metric.** Verification is the only detector, which is why doc 12
    is not optional.
13. **"Done" is one full business cycle later**, and 92 days for anything a quarterly consumer
    touches.
14. **Track lead time, incident rate per hundred migrations, abort rate, and drift count.** An
    abort rate of zero is a problem, not a success.

Next: [17-testing-migrations.md](17-testing-migrations.md), which is about finding these
problems before production does.
