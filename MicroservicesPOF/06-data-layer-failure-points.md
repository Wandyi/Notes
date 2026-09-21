# The Data Layer — Where Failure Becomes Permanent

Everything before this doc could be fixed by restarting something. From here it cannot.

That is the property that makes the data layer different, and it is worth stating precisely
before the catalogue. A stateless service that fails is replaced by an identical one and the
system continues. A stateful service that fails takes something with it that no replacement can
reconstruct. So the failure points in this layer have two extra dimensions the previous docs did
not have:

- **Durability**: did we lose something that was acknowledged?
- **Correctness**: are two parties now disagreeing about what is true?

And one structural consequence: **you cannot scale a stateful system by adding instances.** Doc
00's availability arithmetic assumed replicas were interchangeable. In the data layer they are
not — a replica has to *have* the data, which means the data has to be somewhere specific, which
means there is a mapping from data to location, which means there is a point of failure in that
mapping. Sharding is that mapping, and most of this doc is downstream of it.

This doc covers failure points in the data layer generally. The reference notes in
[`../../../system-design-notes/statefulSystemsAtMAANGScale.md`](../../../system-design-notes/statefulSystemsAtMAANGScale.md)
and [`../../../system-design-notes/database`](../../../system-design-notes/database) go deeper on
storage-engine internals; [`../Kafka`](../Kafka/README.md) covers the log-structured case in
detail. This doc is about the failure points a *service owner* has to reason about.

## The running numbers

Everything derives from these, from the collections' running systems:

**Riverbend `orders-db`**: Aurora PostgreSQL, `db.r6g.4xlarge` (16 vCPU, 128 GiB),
`max_connections` 600, ~180 in use steady state, one writer plus two readers. ~67 order-writes/s
steady, ~350/s at flash-sale peak, ~2,400 reads/s.

**Lumen likes**: Cassandra, 58,000 writes/s, 24 nodes, `LOCAL_QUORUM`, RF=3.

**Waypoint locations**: Cassandra, 750,000 writes/s, 24 nodes → ~31,000 writes/node, against a
practical per-node ceiling around 50,000. 6.5 TB/day with a 30-day TTL.

**Gateline seat inventory**: PostgreSQL, 100,000 seats for one event, 500,000 QPS of demand
shaped down to ~10,000 QPS of real booking attempts by the waiting room.

## Sharding, and the four ways a shard key fails you

A shard key is a function from a row to a location. Once chosen, it determines which queries are
cheap, which are impossible, and which failures are possible. It is the hardest thing to change
in a running system — harder than a schema, harder than a language — so the failure modes it
creates are long-lived.

Four ways to choose one, with the failure each invites:

| Strategy | Example | What it is good at | Its characteristic failure |
|---|---|---|---|
| **Hash of a high-cardinality key** | `hash(user_id) % N` | Even distribution | Range queries impossible; resharding rewrites everything (`S-04`) |
| **Range** | `order_date` | Range queries and time-based deletion | **All writes land on the newest shard** (`S-02`) |
| **Entity / tenant** | `tenant_id` | Clean isolation, easy per-tenant operations | One large tenant does not fit on one shard (`S-01`) |
| **Composite** | `(tenant_id, hash(entity_id))` | Both isolation and spread | Complexity; cross-partition queries within a tenant |

The rule that resolves most arguments: **shard by whatever your highest-volume access path uses
in its `WHERE` clause.** If 95% of queries are "everything for one user", shard by user. The 5%
that are not become scatter-gather queries and you pay for them explicitly (`S-03`). Sharding for
the 5% and making the 95% scatter is a mistake that is extremely difficult to undo.

### S-01 · The hot shard

**What you see.** One shard at 95% CPU while the others are at 20%. Total cluster utilisation
looks comfortable. Latency for a subset of users is terrible. Adding nodes does not help.

**Mechanism.** The shard key does not distribute the *load*, only the *keys*. Distribution of keys
and distribution of traffic are different things, and it is traffic that matters.

Lumen is the canonical case. Shard the social graph by `user_id` and the keys distribute
perfectly — 2 billion users across N shards, evenly. Now consider an account with 400 million
followers. Every one of that account's posts requires reading that follower list; every one of
that account's profile views hits the same shard. The account's row is a single key, so no
hashing scheme can spread it.

Quantify it with the reference numbers. Lumen serves 1.15M feed reads/s average, and suppose 3%
of all feed reads involve the top 50 accounts:

```
1.15M × 0.03 = 34,500 reads/s  concentrated on 50 keys
Spread across 512 shards by user_id, those 50 keys live on at most 50 shards
Worst case, one popular account alone: ~5,000 reads/s on one key
```

A single Cassandra partition can serve maybe a few thousand reads/s before the coordinator and
the replica set for that partition saturate. And it does not matter that the other 511 shards are
idle: **a hot key is not a capacity problem, it is a routing problem**, and capacity cannot fix
routing.

The versions of this that catch people:

- **Sequential IDs**: `hash(order_id)` where `order_id` is a monotonically increasing integer
  distributes fine; but partitioning by `order_id` *range* puts all new orders on one shard.
- **Low-cardinality keys**: sharding by `country` means the US shard holds 40% of the data.
- **Time-bucketed keys**: `(sensor_id, hour)` is fine; `(hour, sensor_id)` puts an entire hour on
  one partition.
- **The `null` or default key**: rows with a missing shard key all hash to the same place.
  Surprisingly common.

**Confirm it.** Per-shard or per-partition metrics — request rate, CPU, latency — as a
distribution, not an average. The average is the thing that hides this.

```sql
-- PostgreSQL: which tables/indexes are taking the reads on this shard vs others
SELECT relname, seq_scan, idx_scan, n_tup_ins, n_tup_upd
FROM pg_stat_user_tables ORDER BY idx_scan DESC LIMIT 10;
```

```bash
# Cassandra: find partitions that are physically large, a good proxy for hot
nodetool tablehistograms lumen.follows
# and the partitions the compaction log complains about
grep -i 'Writing large partition' /var/log/cassandra/system.log | tail -20
```

**Recover.** Short-term, get the traffic off the key rather than trying to make the shard faster:

1. **Cache the hot key aggressively**, including in the application process (a local LRU in front
   of the shared cache). A key read 5,000 times/s served from 40 application instances' local
   caches with a 1-second TTL reaches the database 40 times/s. That is a 125× reduction from a
   change that takes an hour.
2. **Read from replicas** for that key specifically, accepting staleness.
3. **Rate-limit or shed** requests for that key if it is genuinely abusive rather than popular.

**Prevent.** The structural fixes, in increasing order of cost:

- **Add entropy to the key for write-heavy hot spots.** Lumen's likes counter for a celebrity post
  receiving 2,778 writes/s to one `post_id` partition is solved by writing to
  `(post_id, shard)` where `shard = random(0, 99)`, and summing the 100 rows on read:
  `2,778 / 100 = 28 writes/s per partition`, which is trivial. The cost is a 100-way read, which
  is fine because reads of the aggregate are far rarer than writes and can be cached. This is the
  single most useful hot-key technique and it generalises widely.
- **Separate the hot entities into their own storage tier.** Lumen keeps the top ~10,000 accounts'
  follower lists in a dedicated, heavily replicated cache tier rather than in the general graph
  store. The 2 billion normal accounts use the general path.
- **Use consistent hashing with virtual nodes** so that a rebalance moves a small fraction of
  keys rather than remapping everything — and so that you can move *individual* hot ranges.
- **Design the schema so the hot entity is not a single row.** The celebrity follower list is not
  one row with 400M entries; it is 400M rows keyed by `(followee, follower)` with a partition key
  that includes a bucket.

### S-02 · Right-edge contention: every write to the same page

**What you see.** Write throughput plateaus at a number far below what the hardware should do.
CPU is moderate. Lock or latch wait time dominates. Adding CPU or faster disks changes nothing.

**Mechanism.** This one is specific enough to be worth deriving, because it is invisible unless
you know to look and it caps write throughput on a huge number of systems. (The reference notes
cover it in depth in
[`../../../system-design-notes/database/btreeRightEdgeContention.md`](../../../system-design-notes/database/btreeRightEdgeContention.md).)

A B-tree index on a monotonically increasing column — an auto-increment primary key, a
`created_at` timestamp, a ULID — has a property: **every insert goes to the same leaf page**, the
rightmost one. That page must be latched (a short-lived physical lock) for each insert.

Riverbend's `orders` table has a `BIGSERIAL` primary key and an index on `created_at`. At 67
inserts/s this is invisible. At the flash-sale peak of 350/s it is still fine. Push to Amazon
scale — 5,000 orders/s — and every one of those 5,000 inserts per second contends for the same
index leaf page, and for the same page again on the `created_at` index. Latch acquisition is
microseconds, but it is serialised:

```
If a latch is held for 20 µs, the maximum rate through it is 1 / 20 µs = 50,000/s
But each insert takes several latches (leaf, sometimes parent on split, WAL buffer),
and contention makes each acquisition slower than the uncontended case.
Real-world plateau on a single hot leaf: often 5,000–15,000 inserts/s
```

Page splits make it worse in a specific way: when the rightmost page fills, it splits, and the
split takes a latch on the parent too — briefly serialising *everything*. With monotonic keys the
split is always a 50/50 split that leaves the left half permanently half-empty, so the index is
twice the size it needs to be (most databases special-case monotonic inserts with a 90/10 split
for this reason, but not all indexes get the optimisation).

**Confirm it.** Look for latch/lock waits attributed to index pages rather than rows.

```sql
-- PostgreSQL: wait events, sampled
SELECT wait_event_type, wait_event, count(*)
FROM pg_stat_activity WHERE state = 'active'
GROUP BY 1,2 ORDER BY 3 DESC;
-- 'LWLock' / 'BufferContent' concentrated on one relation is the signature
```

```sql
-- MySQL/InnoDB
SELECT * FROM performance_schema.events_waits_summary_global_by_event_name
WHERE event_name LIKE 'wait/synch/%' ORDER BY sum_timer_wait DESC LIMIT 10;
```

**Prevent.** Break the monotonicity of the *index key*, not necessarily of the value:

- **Use a random or hash-prefixed primary key**: UUIDv4, or a hash of the natural key. Writes
  scatter across the index. The cost is real: random inserts have poor locality, so the buffer
  pool holds more pages and writes cause more I/O. This is the classic trade and there is no free
  side.
- **Use UUIDv7 or ULID with a *partition* on a hash.** These are time-ordered (good locality) but
  you shard by `hash(id)`, so no single node sees the monotonic sequence.
- **Hash-partition the table** so the "rightmost page" exists once per partition. 16 partitions
  turns one hot page into 16 warm ones, which is usually enough.
- **Reverse-key indexing** (Oracle's term; implementable elsewhere as an index on a reversed or
  hashed expression) spreads sequential values across the index while keeping the column
  sequential. Range scans on that column then become impossible, which is the cost.
- **Use an LSM-tree store for genuinely write-heavy sequential workloads.** Cassandra, RocksDB,
  and friends append to a memtable and never do in-place page updates, so this failure mode does
  not exist. That is why Waypoint's 750,000 location writes/s are on Cassandra and not on
  PostgreSQL — not because PostgreSQL is slow, but because this specific contention is
  structural.

### S-03 · Scatter-gather and the tail-latency multiplier

**What you see.** A query's p99 is far worse than any individual shard's p99, and it degrades as
you add shards.

**Mechanism.** A query that must consult all shards completes only when the *slowest* shard
responds. That is a maximum over N samples, and the maximum of N samples from a distribution is
much worse than the distribution's own tail.

Concretely: each shard has a p99 of 10 ms, meaning a 1% chance of exceeding 10 ms. Query 100
shards:

```
P(all 100 respond within 10 ms) = 0.99^100 = 0.366
P(at least one exceeds 10 ms)   = 63.4%
```

**Nearly two thirds of scatter-gather queries hit at least one shard's p99.** Your per-shard p99
has become roughly your aggregate p63. To get an aggregate p99 of 10 ms you would need each shard
at p99.99.

Generalised: for `N` shards, the aggregate p99 is approximately each shard's `p(0.99^(1/N))`
quantile — and as `N` grows, that pushes into the part of the distribution nobody measures.

| Shards | Per-shard quantile needed for aggregate p99 |
|---|---|
| 1 | p99 |
| 10 | p99.9 |
| 100 | p99.99 |
| 1,000 | p99.999 |

**Prevent.**

- **Design the shard key so the common query hits one shard.** This is the whole point of the
  "shard by what the `WHERE` clause uses" rule, and it is the only real fix.
- **Hedge the slow shard** (doc 02, `R-15`): if a shard has not responded by p95, ask its replica.
  This turns the maximum-of-N into something much closer to the median, and it is the standard
  technique in search systems. Budget it.
- **Return partial results with a completeness flag** where the domain allows. Corridor's search
  returns results from the shards that answered within the budget, marked as partial, rather than
  waiting for all of them. A feed missing 3% of candidates is invisible to users; a feed that
  takes 2 seconds is not.
- **Precompute** instead of scattering. This is what derived-data pipelines are for, and it is
  doc 20's whole subject.

### S-04 · Resharding a live system

**What you see.** A multi-week project that touches everything, with a window during which reads
and writes can go to the wrong place.

**Mechanism.** With `hash(key) % N`, changing `N` remaps almost every key. Going from 8 shards to
16 moves 50% of the data; from 8 to 9 moves 89% of it. During the move, for each key, there is a
moment when it exists in two places or neither.

The failure modes during resharding, in the order they usually bite:

1. **Double-write skew.** The standard approach writes to both old and new locations during
   migration. If they are not written transactionally (and they cannot be, across shards — see
   doc 07), a crash between them leaves them disagreeing.
2. **Read-during-move.** A read routed by the new mapping to a shard that has not received the
   data yet returns "not found", which the application interprets as "does not exist" and may act
   on — creating a duplicate, or telling a user their order is gone.
3. **Backfill overwhelming the source.** Copying 4 TB while serving production traffic doubles the
   read load on the source shards, which are already the busy ones (you are resharding because
   they are hot).
4. **Cutover with in-flight writes.** A write that was routed by the old mapping and lands after
   the cutover goes to a shard that is no longer authoritative.

**Prevent.** Two structural choices made *before* you need them:

- **Consistent hashing with virtual nodes.** Adding a node moves `1/N` of the keys instead of
  most of them. A 128-node ring with 256 vnodes each means adding a node moves 0.8% of data.
- **A lookup table instead of a hash function.** Map logical shards (say 4,096 of them) to
  physical nodes via a table you can edit. Rebalancing means moving logical shards, one at a time,
  atomically, with a per-shard "moving" state that routes reads to the source and writes to both.
  Vitess, Slicer, and most large-scale custom sharding layers do this. It costs a lookup (cached)
  and buys you incremental, resumable, per-shard migration — which turns a three-week project
  into a background process.

And regardless: **over-shard from the start.** Create 1,024 logical shards on 8 machines rather
than 8 shards. Logical shards are cheap; changing their count is not. Growing then means moving
logical shards between machines, which is a data-movement problem and not a remapping problem.

### S-05 · Replication lag is a correctness bug, not a performance one

**What you see.** A user updates something, immediately reloads, and sees the old value. Or worse:
a service writes a record and a downstream service, reading a replica, cannot find it and takes
a compensating action.

**Mechanism.** Asynchronous replication means a write acknowledged by the primary is not yet on
the replicas. Lag is normally milliseconds and occasionally seconds — and during a large write
burst, a long transaction, or a replica doing maintenance, it can be minutes.

The reason this is a correctness bug and not a latency issue: the application's logic is wrong
during the lag window. Concretely at Riverbend:

```
t=0      checkout-api writes the order to the primary. Returns 201 to the user.
t=0.1s   The confirmation page loads and reads from a replica. Order not found.
         The page shows "we could not find your order."
t=0.4s   The user, reasonably, clicks "buy" again.
t=0.4s   A second order is created.
```

The replication lag produced a duplicate order. No component failed. Both reads and both writes
were correct against the data they saw.

The nastier version is service-to-service: `order-processor` publishes `order.created`, a
downstream consumer reads the order row from a replica to enrich it, does not find it, and routes
the event to a dead-letter queue as "orphaned." Now you have a DLQ full of perfectly valid orders,
and you discover it when someone reads the DLQ, which per doc 09 may be never.

**Confirm it.**

```sql
-- PostgreSQL, on the replica: how far behind in time
SELECT now() - pg_last_xact_replay_timestamp() AS replica_lag;
-- On the primary: bytes behind, per replica
SELECT client_addr, state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes
FROM pg_stat_replication;
```

```sql
-- MySQL
SHOW REPLICA STATUS\G   -- Seconds_Behind_Source, which lies during a stall; prefer
                        -- the heartbeat-table technique for a real measurement
```

⚠️ `Seconds_Behind_Source` reads 0 when replication is stopped entirely, because it measures the
gap for the event currently being applied and there is none. Use a heartbeat table written by the
primary every second and read on the replica; the difference is the true lag.

**Prevent.** There are five mechanisms and you should know all of them, because they have
different costs:

| Mechanism | How | Cost | When to use |
|---|---|---|---|
| **Read your own writes from the primary** | After a write, route that user's reads to the primary for N seconds (a cookie or a session flag) | Primary read load | The default. Cheap and covers the common case. |
| **Monotonic reads via LSN** | The write returns its log position; subsequent reads require a replica at or past it, else fall back to the primary | Plumbing the LSN through the API | When you need correctness rather than a heuristic |
| **Synchronous replication** | The primary waits for a replica to acknowledge | Write latency += cross-AZ RTT (1–2 ms), and write availability depends on the replica | Money, ledgers, anything where a lost write is unacceptable |
| **Quorum reads and writes** | `R + W > N` in a Dynamo-style store | Higher latency, more nodes contacted | Cassandra/Dynamo systems: `LOCAL_QUORUM` is the standard answer |
| **Do not read from replicas for that data** | Route the class of query to the primary always | Primary capacity | Small, correctness-critical tables |

And the discipline that matters most: **decide, per query, whether it tolerates staleness, and
make that explicit in the code.** A `readPreference` chosen at the connection level, globally,
guarantees you will get it wrong for some queries. Riverbend's convention is that the repository
layer exposes `findOrder(id)` and `findOrderEventuallyConsistent(id)`, and the second one is
allowed on a replica. Being forced to type the longer name is the design working.

### S-06 · Reading from a replica that is catastrophically behind

**What you see.** A subset of reads returning data that is hours old, intermittently, with no
errors.

**Mechanism.** A replica that is 4 hours behind is fully healthy by every check: it accepts
connections, it answers queries quickly, it reports itself as a replica. Nothing about its
responses says "this is old." It is a gray failure (doc 00) in its purest form.

Causes: a long-running query on the replica blocking WAL application (PostgreSQL's
`max_standby_streaming_delay` explicitly trades this off); single-threaded replication apply not
keeping up with a multi-threaded primary (classic MySQL); a replica that was restored from a
backup and is catching up; network saturation on the replication link.

**Prevent.** Make lag a **routing input**, not just an alert:

1. Every replica reports its lag.
2. The connection pool or proxy removes replicas with lag above a threshold from the read pool.
3. The threshold is per-workload: an analytics reader can tolerate 5 minutes; the order
   confirmation page can tolerate 200 ms.
4. If all replicas exceed the threshold, fall back to the primary — and alert, because you are
   now taking read load you did not plan for.

ProxySQL, PgBouncer with a health script, and most managed read-endpoint implementations support
this. Doing it manually in the application is also fine. Not doing it at all means you are
serving arbitrarily old data and calling it a read replica.

### S-07 · Failover and the lost-write window

**What you see.** After a primary failover, a small number of writes that were acknowledged are
gone.

**Mechanism.** With asynchronous replication, the primary acknowledges a write before the replica
has it. If the primary dies in that window, the write exists only on a machine that is gone. The
new primary's log simply ends earlier.

The exposure is computable:

```
Riverbend at flash-sale peak: 350 order-writes/s
Replication lag at peak:      p99 of 40 ms
Writes at risk at any instant: 350 × 0.040 = 14 orders
```

Fourteen orders. Whether that is acceptable is a business decision, not a technical one, and the
important thing is that it is a *known* number rather than a surprise.

The variant that is worse: the old primary comes back and still has those writes, which now
conflict with newer writes on the new primary that reused the same IDs. This is where a failover
turns into `S-08`.

**Prevent.**

- **Synchronous replication to at least one replica** for the tables that cannot lose writes. In
  PostgreSQL, `synchronous_commit = on` with `synchronous_standby_names = 'ANY 1 (r1, r2)'` — the
  `ANY 1` form is important, because naming a single standby means that standby's failure blocks
  all writes (the same `M = N` trap as Kafka's `min.insync.replicas`; see
  [`../Kafka/02-replication-isr-and-durability.md`](../Kafka/02-replication-isr-and-durability.md),
  which derives this exact arithmetic).
- **Per-table or per-transaction durability.** PostgreSQL lets you set `synchronous_commit` per
  transaction, so the payment write is synchronous and the page-view log is not. This is the
  right answer far more often than a global setting.
- **Fence the old primary** so it cannot accept writes after being replaced (`L-06`). Most managed
  services do this; hand-rolled failover frequently does not.

### S-08 · Split brain: two primaries

**What you see.** Both nodes accept writes. Data diverges. After the partition heals, you have two
inconsistent datasets and must decide which one is real.

**Mechanism.** A network partition separates the primary from the failover controller. The
controller cannot reach the primary, concludes it is dead, and promotes a replica. The old
primary is fine and still has clients connected to it. Both accept writes.

This is the most expensive failure in this doc because the *repair* is manual and lossy: someone
has to look at two sets of writes and decide.

**Prevent.** Three mechanisms, and you need at least two of them:

1. **Quorum-based promotion.** A node may only be promoted with a majority's agreement. A minority
   partition cannot elect anything. This requires an odd number of voters in an external system
   (etcd, Consul, ZooKeeper) rather than the database nodes voting among themselves.
2. **Fencing (STONITH).** Before promoting, actively disable the old primary: revoke its
   credentials, detach its storage, block it at the network layer, or power it off. "We could not
   reach it, so it must be dead" is not fencing. The distinction is the difference between
   *believing* and *ensuring*.
3. **Fencing tokens in the data path.** Every write carries a monotonically increasing epoch
   number, and the storage layer rejects writes with a stale epoch. This is the strongest
   mechanism because it does not depend on reaching the old primary at all. Doc 10 (`L-06`) covers
   it fully.

### S-09 · The connection storm after failover

**What you see.** A failover completes in 20 seconds, and the outage lasts 4 minutes.

**Mechanism.** `F-09` in the database layer. 800 application connections all break at once. All
800 reconnect simultaneously to the new primary. Each reconnection costs: TCP handshake, TLS
handshake, authentication, and then the connection-initialisation queries the pool runs (`SET`
statements, prepared-statement setup, an ORM's metadata queries).

PostgreSQL forks a backend process per connection — roughly 5–10 ms and several MB each. 800
simultaneous connection attempts is 4–8 seconds of pure fork work, during which the new primary
is doing nothing else, while the application is timing out and retrying, adding more connection
attempts.

**Prevent.**

- **A connection proxy** (PgBouncer, RDS Proxy, ProxySQL) that holds the server connections and
  multiplexes clients onto them. Failover then re-establishes 40 connections, not 800, and the
  application's connections to the proxy never break. This is the single highest-value piece of
  database infrastructure for a microservice fleet and it also solves `R-09`.
- **Jittered reconnect with a cap on concurrent connection attempts** in the pool.
- **Pool warm-up limits**: `minimumIdle` well below `maximumPoolSize` so the pool does not
  immediately open its full complement.

### S-10 · A long transaction holds everything

**What you see.** Writes to one table stall. Then writes to unrelated tables stall. Then the
connection pool exhausts and the service is down.

**Mechanism.** One transaction holds a lock that everything else needs. The variants:

- A transaction left open by an application bug (a code path that does not commit, or an
  exception before commit with no rollback).
- An `ALTER TABLE` waiting behind a long read, which then blocks *every subsequent query* on that
  table — including short ones — because lock requests queue in order. A 100 ms `ALTER` can
  produce a 10-minute outage this way, and it is the most common cause of "the migration took the
  site down."
- A batch job doing `UPDATE ... WHERE` over a million rows in one transaction, holding row locks
  the whole time.
- `SELECT ... FOR UPDATE` held across a network call. Gateline's seat-reservation path is exactly
  this shape and doc 18 covers what it does at 500,000 QPS.

**Confirm it.**

```sql
-- PostgreSQL: who is blocking whom, right now
SELECT blocked.pid AS blocked_pid, blocked.query AS blocked_query,
       blocking.pid AS blocking_pid, blocking.query AS blocking_query,
       now() - blocking.xact_start AS blocking_txn_age
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking
  ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
WHERE blocked.wait_event_type = 'Lock';

-- The single most useful monitoring query: oldest transaction age
SELECT max(now() - xact_start) FROM pg_stat_activity WHERE state <> 'idle';
```

**Recover.** Terminate the blocking transaction (`SELECT pg_terminate_backend(pid)`). ⚠️ Know what
it was doing first — terminating a schema migration mid-way can leave the schema in a state your
migration tool does not expect.

**Prevent.**

- **`statement_timeout` and `idle_in_transaction_session_timeout` set on every role**, not left
  at unlimited. Values like 30 s and 60 s respectively. This converts an unbounded outage into a
  bounded error.
- **`lock_timeout` set low (1–3 s) for DDL specifically**, with retries. A migration that cannot
  get the lock in 2 seconds should fail and retry rather than queue behind a long read and block
  the world.
- **Never hold a transaction across a network call.** If you must reserve something and then call
  a payment provider, the reservation is a *row with a state and an expiry*, not a held lock. Doc
  07 (`T-08`) and doc 18 both build this out.
- **Batch in chunks with a commit between them**, sized so each chunk is under a second.

### S-11 · MVCC bloat and the vacuum that cannot keep up

**What you see.** Table and index sizes growing while row count is flat. Queries getting slower
over weeks. Eventually, a wraparound emergency that forces a shutdown.

**Mechanism.** PostgreSQL (and any MVCC store) does not overwrite rows; it writes a new version
and marks the old one dead. `VACUUM` reclaims dead rows — **but only those older than the oldest
running transaction**, because that transaction might still need to see them.

So one long-running transaction — a stuck connection, a replica with `hot_standby_feedback` on
and a slow query, an abandoned `BEGIN` — **prevents vacuum from reclaiming anything across the
entire database**, for as long as it lives. The table grows, the indexes grow, the buffer pool
holds progressively more dead tuples, and every sequential scan reads more pages.

The endgame is transaction-ID wraparound: PostgreSQL's 32-bit transaction counter must not lap
itself, so if vacuum cannot freeze old rows, the database will eventually refuse writes entirely
to protect itself. That is a hard outage requiring single-user-mode recovery.

Cassandra has the analogous problem with tombstones: a deleted row leaves a marker that must be
read past until `gc_grace_seconds` passes and compaction removes it. A partition with a hundred
thousand tombstones makes every read of that partition slow, and Cassandra will refuse the query
past `tombstone_failure_threshold`. Waypoint's 30-day TTL on location data generates tombstones
continuously, which is why TTL'd data in Cassandra needs a compaction strategy (`TWCS`) designed
for it rather than the default.

**Confirm it.**

```sql
-- PostgreSQL: dead tuple ratio and last vacuum
SELECT relname,
       n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE n_dead_tup > 10000
ORDER BY n_dead_tup DESC LIMIT 20;

-- Wraparound headroom: alert well before 200 million
SELECT datname, age(datfrozenxid) AS xid_age FROM pg_database ORDER BY 2 DESC;
```

**Prevent.** Alert on `age(datfrozenxid)` and on the oldest transaction age — the second one is
the leading indicator and the first is the consequence. Tune autovacuum to be more aggressive on
high-churn tables (`autovacuum_vacuum_scale_factor` of 0.01 rather than the default 0.2 on a large
table, since 20% of a 500M-row table is 100M dead rows before vacuum even starts). And enforce
`idle_in_transaction_session_timeout`, which eliminates the most common cause.

### S-12 · The query with no bound

**What you see.** A single request consumes gigabytes of memory, or takes 90 seconds, or returns a
response body that kills the client. Often intermittent, because it depends on data volume.

**Mechanism.** A query with no `LIMIT`, on a table whose size grows. It was fine when the customer
had 40 orders; it is not fine when they have 400,000. The same shape appears as an API endpoint
with no pagination, a `GROUP BY` with unbounded cardinality, or an `IN` clause built from an
unbounded list.

The N+1 variant: an ORM loads 500 parents and then lazily loads each one's children, producing
501 queries. At 3 ms each that is 1.5 seconds of serialised database round trips for one request,
and it scales with result size.

**Confirm it.**

```sql
-- PostgreSQL with pg_stat_statements: the expensive shapes
SELECT substring(query, 1, 100) AS q, calls, mean_exec_time, max_exec_time, rows/calls AS avg_rows
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
```

The column to look at is `rows/calls`. Any query shape averaging thousands of rows returned to an
application is suspicious, and one whose average is growing over time is a future incident with a
date on it.

**Prevent.** Mandatory pagination with a maximum page size, enforced in the repository layer and
not left to callers. A `statement_timeout`. A row-count guard in the driver. And a CI check or
lint rule that flags queries without a `LIMIT` on tables above a size threshold — cheap, and it
catches the N+1 pattern too.

### S-13 · The plan flip

**What you see.** A query that has run in 4 ms for a year suddenly takes 8 seconds. Nothing was
deployed. Nothing changed. It stays slow, or it flips back and forth.

**Mechanism.** The query planner chooses a plan based on statistics. When statistics change — after
a bulk load, after autoanalyze runs, after data distribution shifts — it may choose differently.
The classic flip is index scan → sequential scan, or nested loop → hash join, when an estimated
row count crosses a threshold.

The parameter-sniffing variant: a prepared statement's plan is chosen for the first parameter
value seen and reused. If the first execution was for a rare value (index scan is right) and
subsequent ones are for a common value (sequential scan is right), every subsequent execution
uses the wrong plan. PostgreSQL's `plan_cache_mode` and the generic-versus-custom plan heuristic
make this specific and diagnosable.

**Confirm it.** Capture the plan at the time of the problem, not afterward:

```sql
EXPLAIN (ANALYZE, BUFFERS, VERBOSE) SELECT ...;
-- Compare 'rows=' estimates against 'actual rows='. An estimate off by 100× is the diagnosis.
```

`auto_explain` with `log_min_duration` set will capture the plan for slow executions
automatically, which is what you want because the problem is rarely reproducible on demand.

**Prevent.** Keep statistics current (more aggressive autoanalyze on tables with shifting
distributions); increase the statistics target on skewed columns
(`ALTER TABLE ... ALTER COLUMN ... SET STATISTICS 1000`); use extended statistics for correlated
columns; and put a `statement_timeout` in place so a flipped plan is a bounded error rather than
a pile-up. For the worst offenders, pin the plan — but treat pinning as a debt, since a pinned
plan is wrong as soon as the data changes again.

### S-14 · Disk full, and the WAL that will not stop growing

**What you see.** The database refuses writes. Sometimes it also refuses to start.

**Mechanism.** The write-ahead log grows and cannot be recycled. The three causes, all of which
are "something is preventing the log from being released":

- **A replication slot for a replica that is gone.** PostgreSQL keeps WAL for any slot that has
  not consumed it — indefinitely. A decommissioned replica whose slot was not dropped will fill
  the primary's disk. This is a leading cause of PostgreSQL outages and it is entirely
  preventable with `max_slot_wal_keep_size`.
- **Archiving failing.** `archive_command` returns non-zero (S3 credentials expired, bucket
  policy changed) so WAL is retained pending archive. Silent until the disk fills.
- **A long transaction** preventing checkpointing.

**Confirm it.**

```sql
SELECT slot_name, active,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
FROM pg_replication_slots ORDER BY 3 DESC;

SELECT * FROM pg_stat_archiver;  -- last_failed_wal, failed_count
```

**Prevent.** Set `max_slot_wal_keep_size` so a dead slot is invalidated rather than filling the
disk (you lose the replica, which is the correct trade). Alert on archiver failure count, on
retained WAL per slot, and on disk free — with the disk alert at 20% free, not 5%, because you
need time to act.

### S-15 · The backup that does not restore

**What you see.** You need the backup. It does not work.

**Mechanism.** Backups are write-only until the day they are not. The specific ways they fail,
each of which has happened to someone:

- Never tested, and the format or tooling has drifted.
- Restores successfully but takes 14 hours, and your RTO was 1 hour. **A backup you cannot restore
  in time is not a backup for that purpose.**
- Point-in-time recovery requires WAL archives, which stopped 3 weeks ago (`S-14`).
- The backup is in the same region, account, or blast radius as the thing it protects.
- Backed up the database but not the schema-migration state, the encryption keys, or the
  configuration, so the restored data is unusable.
- Ransomware or an accidental `DELETE` propagated to the backup, because the backup is a replica
  rather than a snapshot.

**Prevent.** Restore drills on a schedule, with the **restore time measured and recorded**. The
number you need on a wall is: how long does it take, today, to get a working database from
nothing? Everything else about backups is downstream of that number. Keep backups in a separate
account with write-once retention. And test PITR specifically, not just full-snapshot restore,
because the interesting recovery is almost always "to the moment before the bad thing."

### S-16 · The noisy neighbour inside the database

**What you see.** One tenant's or one service's query pattern degrades everyone sharing the
database.

**Mechanism.** A shared database has no isolation between its clients. A single tenant running an
unindexed report scans a large table, evicting everyone else's pages from the buffer pool. Cache
hit rate for all tenants falls, so all queries do physical I/O, so everything is slow.

The buffer-pool eviction version is the subtle one: the damage outlasts the query by minutes,
because the buffer pool has to be re-warmed by normal traffic.

**Prevent.** Per-tenant or per-service resource limits where the engine supports them (PostgreSQL
has limited support; `pg_stat_statements` plus a kill-switch is the practical approach). Separate
read replicas for analytical workloads so they cannot evict the transactional working set —
this is the highest-value single fix. Connection limits per role. And for genuine multi-tenancy at
scale, shard by tenant so that the blast radius of one tenant's behaviour is one shard, which is
doc 13's shuffle-sharding argument applied to storage.

### S-17 · Silent type and encoding corruption

**What you see.** Data that is subtly wrong: truncated strings, mangled emoji, timestamps off by
hours, money off by fractions of a cent.

**Mechanism.** Failures that produce no error:

- MySQL in non-strict mode **truncates** a string that exceeds the column length and issues a
  warning, not an error. The application never sees it.
- `utf8` in MySQL is three-byte UTF-8 and cannot store four-byte characters (emoji, some CJK
  characters). Inserting one either errors or truncates the string at that point depending on
  mode. The column type you want is `utf8mb4`.
- A `TIMESTAMP` column with a session time zone different from the application's produces times
  shifted by the offset. This is `promotionTimezone.md` in the reference notes and it is a
  recurring e-commerce bug: a promotion that starts at midnight starts at a different absolute
  moment depending on who wrote the row.
- `FLOAT`/`DOUBLE` for money. `0.1 + 0.2 != 0.3`. Errors accumulate across millions of rows and
  surface as a reconciliation gap nobody can explain.
- An `INT` primary key approaching 2,147,483,647. Inserts begin failing, hard, at an unpredictable
  moment.

**Prevent.** Strict mode on (`sql_mode` including `STRICT_ALL_TABLES`); `utf8mb4` everywhere;
`TIMESTAMPTZ` (or store UTC and a separate zone column, never a bare local timestamp);
`NUMERIC`/`DECIMAL` or integer minor units for money; `BIGINT` keys from the start; and a
monitoring check on the headroom of every integer sequence:

```sql
SELECT sequencename, last_value,
       round(100.0 * last_value / 2147483647, 2) AS pct_of_int4_max
FROM pg_sequences ORDER BY 3 DESC LIMIT 10;
```

### S-18 · The migration that locks the table

**What you see.** A schema change takes the service down for the duration of the change, or for
much longer via `S-10`.

**Mechanism.** Covered fully in doc 11 (`G-09`), because it is fundamentally a change-management
failure. The short version: many `ALTER TABLE` operations take an exclusive lock, and an exclusive
lock request queues *ahead of* subsequent shared-lock requests, so a migration blocked behind one
long read blocks every read after it.

**Prevent.** Doc 11's expand–contract discipline, `lock_timeout` on DDL, and online-migration
tooling (`pg_repack`, `gh-ost`, `pt-online-schema-change`) for anything that rewrites a table.

## What to take away

1. **Data-layer failures are the ones you cannot fix by restarting.** They add two dimensions no
   other layer has: did we lose acknowledged data, and do two parties now disagree about what is
   true?
2. **The shard key determines which failures are possible** and is the hardest thing to change.
   Shard by what your highest-volume query's `WHERE` clause uses; everything else becomes
   scatter-gather that you pay for explicitly.
3. **Even key distribution is not even load distribution.** A hot key cannot be fixed by adding
   capacity — it is a routing problem. Add entropy to the key (the 100-shard counter trick turns
   2,778 writes/s on one partition into 28/s on a hundred), or move hot entities to a dedicated
   tier.
4. **Monotonic index keys serialise every insert on one B-tree page.** This caps write throughput
   at a few thousand per second regardless of hardware. Hash-partition, use random or hashed keys,
   or use an LSM store — which is why Waypoint's 750,000 writes/s are on Cassandra.
5. **Scatter-gather turns per-shard p99 into aggregate p63 at 100 shards.** Hedge the slow shard,
   return partial results with a completeness flag, or precompute.
6. **Over-shard logically from day one** (1,024 logical shards on 8 machines) and map logical to
   physical via a table you can edit. That converts resharding from a multi-week remapping project
   into a background process.
7. **Replication lag is a correctness bug.** It produces duplicate orders and dead-lettered valid
   events. Decide staleness tolerance *per query*, make it explicit in the code, and make lag a
   routing input so a badly-lagged replica leaves the read pool automatically.
8. **A replica four hours behind passes every health check.** Only measuring lag finds it, and
   `Seconds_Behind_Source` reads zero when replication is stopped — use a heartbeat table.
9. **Compute your lost-write exposure**: writes/s × replication lag. For Riverbend at peak that is
   14 orders. Then decide, explicitly, whether that is acceptable, and use per-transaction
   synchronous commit where it is not.
10. **"We could not reach it, so it must be dead" is not fencing.** Split brain needs quorum-based
    promotion plus active fencing plus, ideally, epoch numbers enforced at the storage layer.
11. **Put a connection proxy in front of the database.** It fixes pool exhaustion, failover
    connection storms, and the "scale out the app tier, exhaust `max_connections`" trap in one
    piece of infrastructure.
12. **Set `statement_timeout`, `idle_in_transaction_session_timeout`, and `lock_timeout` on every
    role.** Unbounded is not a default; it is a decision nobody made. Never hold a transaction
    across a network call.
13. **One long transaction stops vacuum reclaiming anything, database-wide.** Monitor the oldest
    transaction age as a leading indicator and `age(datfrozenxid)` as the consequence.
14. **A backup you cannot restore within your RTO is not a backup.** The number that matters is how
    long a restore takes today, measured, not estimated — and test point-in-time recovery, not
    just full-snapshot restore.
15. **Strict mode, `utf8mb4`, `TIMESTAMPTZ`, `NUMERIC` for money, `BIGINT` keys.** Every one of
    these prevents a class of silent corruption that produces no error and is discovered by
    reconciliation.

Next: [07-transactions-sagas-and-dual-writes.md](07-transactions-sagas-and-dual-writes.md), which
takes the hardest problem in this layer — making a change in two places when you can only commit
atomically in one — and works through what is actually achievable.
