# The USE Method: Utilization, Saturation, Errors

Doc 01 covered RED, for things that answer requests. This doc covers **USE**, for things that do
not answer requests but do have a hard ceiling on how much work they can absorb: `orders-db`'s CPU,
`session-cache`'s memory, `order-processor`'s ability to keep up with `order-events`. The method
was named by Brendan Gregg for exactly this class of problem, and its central claim is one most
teams get half right: they track **Utilization** because it is the easy number to get, and skip
**Saturation**, which is usually the number that would have paged them ten minutes earlier.

## Defining the three signals, precisely

**Utilization** is the fraction of a resource's capacity that is currently in use, over a time
window — CPU busy time as a percentage, memory bytes in use over total, connections open over the
maximum allowed. It answers "how busy is this resource," and it is almost always the first metric
any monitoring setup captures, because every infrastructure exporter reports it by default.

**Saturation** is the amount of work waiting for that resource because it is not currently
available — a queue depth, a number of threads blocked, a count of connections waiting to be
granted, a lag between produced and consumed work. It answers "is work backing up here," and it is
the signal most teams do not instrument, because it usually is not exposed by default and has to be
derived or explicitly enabled.

**Errors** is the count of resource-level errors specific to that resource — disk I/O errors,
out-of-memory kills, connection refusals, checksum failures — as distinct from the *application*
errors RED tracks. A `checkout-api` request failing because `orders-db` refused a new connection is
an Error in USE terms for `orders-db`, and simultaneously an Error in RED terms for `checkout-api`;
the same underlying event shows up in both methods because the two methods are describing the same
incident from two different components' point of view.

## The distinction that matters most: utilization is not saturation

Here is the trap in concrete form, using `orders-db` — the Aurora PostgreSQL primary behind
`order-processor`, `invoice-rollup`, and `payout-settlement`, provisioned as a db.r6g.4xlarge (16
vCPU).

**Scenario A.** At 14:00 on a Tuesday, `orders-db`'s CPU utilization sits at a comfortable 60%. If
that were the only number on the dashboard, you would conclude there is 40% of headroom left and
move on. But `pg_stat_activity` — Postgres's live view of what every connection is doing — shows
something the CPU number cannot: the count of sessions with a non-null `wait_event` (meaning the
query is not running, it is blocked on a lock, a buffer pin, or another session) has climbed from 2
to 40 over the last ten minutes. Those 40 sessions are holding a connection each while doing no
useful work, out of a pool of 600. Upstream, `checkout-api`'s own connection pool to `orders-db` is
now waiting on connections that `orders-db` is not releasing, and request duration on `checkout-api`
is climbing even though nothing about `checkout-api` itself changed. **60% CPU utilization was the
wrong number to be reassured by; the queue of blocked sessions was the one already telling you
something was wrong.**

**Scenario B.** At 03:00, `catalog-reindex` and the hourly `invoice-rollup` overlap during a
catalogue-import day (the case doc 04 of `K8s/cronJobs` describes `catalog-reindex` running past
its usual 1h50m, sometimes over three hours). `orders-db`'s CPU utilization spikes to 95% running
the aggregation queries `invoice-rollup` issues. `pg_stat_activity`'s wait-event count stays at
zero the entire time — every session actively using CPU is doing useful work, and no query is stuck
waiting on anything. **95% utilization, and it is fine**, because nothing is queued. The database is
running hot, not backing up.

The general principle those two scenarios establish: **utilization tells you how busy a resource
is; saturation tells you whether work is piling up faster than the resource can drain it.** A
resource can be moderately utilized and badly saturated (Scenario A — lock contention, not raw CPU
demand, was the bottleneck) or heavily utilized and not saturated at all (Scenario B — genuinely
busy, genuinely keeping up). Alerting on utilization alone catches neither case correctly: it would
have stayed silent through Scenario A's real incident, and it would have paged unnecessarily during
Scenario B's harmless spike.

## Applying USE to orders-db

Three resources inside `orders-db` are worth tracking independently, because each can saturate
without the others being anywhere near their ceiling.

**CPU.** Utilization from the managed database's CloudWatch-derived exporter:

```promql
aws_rds_cpuutilization_average{dbinstance_identifier="orders-db"}
```

Saturation has no single built-in metric on a managed database the way it does on a box you run
yourself (no `node_cpu` to read run-queue length from) — you derive it from `pg_stat_activity`
instead, exported by `postgres_exporter`:

```promql
pg_stat_activity_count{datname="orders", wait_event_type!=""}
```

That is Scenario A's signal: a non-zero, and especially a *climbing*, count of sessions with a
wait event is queued work, regardless of what CPU utilization shows at the same moment.

**Connections.** Utilization is in-use connections over the ceiling you configured:

```promql
pg_stat_activity_count{datname="orders", state="active"} / pg_settings_max_connections
```

With `max_connections` set to 600 and a steady ~180 in use, that ratio sits around 30% under normal
load. Saturation for this specific resource is not a separate query — a connection pool is binary
in a way CPU is not: once `pg_stat_activity_count` reaches 600, the *next* request for a connection
is the saturation event, visible upstream as `checkout-api`'s own pool queue depth (its client-side
metric, not a database metric — the two must be read together to tell whether an application is
slow because the database is slow, or blocked because the database's connections are simply full).

**Disk I/O.** Aurora's CloudWatch exporter reports a genuine saturation metric here, not a derived
one — `DiskQueueDepth`, the number of I/O requests waiting to be serviced by storage:

```promql
aws_rds_disk_queue_depth_average{dbinstance_identifier="orders-db"}
```

against utilization, expressed as IOPS relative to the instance's provisioned throughput ceiling:

```promql
aws_rds_read_iops_average{dbinstance_identifier="orders-db"}
  + aws_rds_write_iops_average{dbinstance_identifier="orders-db"}
```

A rising `DiskQueueDepth` with IOPS flat at the provisioned ceiling is a resource genuinely out of
capacity — the disk equivalent of Scenario A. A rising IOPS number with `DiskQueueDepth` near zero
is the disk equivalent of Scenario B: busier, not backed up.

## Applying USE to session-cache (Redis)

`session-cache` backs the login sessions that `session-reaper` deletes every five minutes once they
expire — the CronJob and this resource are two views of the same data. Three Redis-specific
signals, on the 3 × cache.r6g.xlarge cluster (roughly 9.4 GiB usable memory per node):

**Memory utilization** is the most direct of the three:

```promql
redis_memory_used_bytes{service="session-cache"} / redis_memory_max_bytes{service="session-cache"}
```

**Evictions** are Redis's saturation signal, and they work differently from a queue: when a Redis
instance configured with an eviction policy (`allkeys-lru` or similar) runs out of memory, it does
not queue new writes — it deletes existing keys to make room. `redis_evicted_keys_total` climbing
means the cache is at its ceiling and is actively discarding data, which for `session-cache`
translates directly into a RED-visible symptom on `checkout-api`: an evicted session forces a
logged-in user to re-authenticate mid-checkout. **A rising eviction rate is the saturation signal
here, in the same conceptual role queue depth plays for CPU or disk** — it is "work" (in this case,
"data that should still be resident") that the resource can no longer hold.

```promql
rate(redis_evicted_keys_total{service="session-cache"}[5m])
```

**Connected clients**, tracked mostly as a sanity check against connection-limit-driven errors:

```promql
redis_connected_clients{service="session-cache"}
```

⚠️ A `session-reaper` firing that runs *slower* than usual (doc 04 of `K8s/cronJobs` covers a hung
firing under `Forbid`) means expired keys accumulate for longer between cleanups, pushing memory
utilization up and eviction rate with it — so an eviction-rate alert on `session-cache` is also,
indirectly, a second, independent detector for `session-reaper` itself misbehaving.

## Applying USE to order-processor's consumption of order-events

`order-processor` does not have a CPU-or-memory-shaped ceiling as its primary constraint — it has a
**queue** ceiling: `order-events`, the 24-partition Kafka topic it reads from. The saturation signal
here is consumer lag: the difference between the latest offset produced to a partition and the
offset the consumer group has committed, summed across all 24 partitions.

```promql
sum(kafka_consumergroup_lag{consumergroup="order-processor-group", topic="orders.created"})
```

This is queued work in the most literal sense USE describes: every unit of lag is one order that
has been accepted by `checkout-api` (and therefore already counted as a RED success on that
service) but not yet written to `orders-db` by `order-processor`. Utilization, for a consumer, is a
weaker signal — CPU or network usage on the consumer pods — because a consumer can sit at low CPU
utilization while still falling behind, if the bottleneck is downstream (for instance, waiting on
`orders-db` writes, looping back to the CPU/connections signals above) rather than in the consumer's
own compute.

⚠️ **A second, container-specific saturation trap sits inside `order-processor`'s own pods.**
Kubernetes enforces a CPU *limit* using the kernel's CFS bandwidth controller, which throttles a
container that exceeds its limit within a scheduling period, even if node-wide CPU utilization looks
low. The utilization metric —

```promql
rate(container_cpu_usage_seconds_total{pod=~"order-processor-.*"}[5m])
```

— can show a pod comfortably under its CPU limit on average while still being throttled in bursts,
because averaging over five minutes hides periods where the container hit its limit and was paused
by the kernel. The actual saturation signal is:

```promql
rate(container_cpu_cfs_throttled_seconds_total{pod=~"order-processor-.*"}[5m])
```

A non-zero, rising value here, even alongside a CPU utilization graph that looks fine, means the
container is being paused mid-execution — directly slowing down how fast it can drain
`order-events`, and therefore a second, independent contributor to the consumer-lag number above.
This is Scenario A's exact shape, restated inside Kubernetes: utilization looked acceptable,
saturation was already happening.

## The common mistake: instrumenting utilization because it is easy, and stopping there

Every managed service, every container runtime, and every OS-level exporter reports utilization by
default — `aws_rds_cpuutilization_average`, `container_cpu_usage_seconds_total`,
`redis_memory_used_bytes` all exist without you writing a line of code. Saturation almost never
does: `DiskQueueDepth` on Aurora is a rare exception where the platform hands you a real saturation
metric directly; `pg_stat_activity`'s wait-event count, Redis's eviction rate, Kafka's consumer
lag, and CFS throttling all had to be specifically identified as *the* saturation signal for that
resource, because none of them is labeled "saturation" in the exporter's documentation.

The failure mode this produces is not "no monitoring" — it is a dashboard that looks complete, full
of correctly-graphed utilization panels, that stays green through Scenario A every time it recurs.
The fix is not a new tool; it is a deliberate second pass over every resource already on a
dashboard, asking specifically: **if this resource were the bottleneck right now, what would be
queued, and where does that queue's length show up as a number?**
For every resource in the running example, that number existed — `wait_event` counts, eviction
rate, consumer lag, CFS throttled seconds — it simply required asking the saturation question
explicitly, rather than assuming the utilization panel already answered it.

## What to take away

1. Utilization is how busy a resource is; Saturation is how much work is queued waiting for it.
   They can move independently — `orders-db` showed 60% CPU utilization with a real, climbing
   saturation problem, and 95% utilization with none.
2. Saturation metrics are rarely exported by default and have to be identified per resource type:
   `pg_stat_activity` wait events for database CPU/locks, `DiskQueueDepth` for storage, eviction
   rate for a memory-bounded cache, consumer lag for a queue, CFS throttled seconds for a
   CPU-limited container.
3. The same underlying incident produces an Error in USE terms on the resource (`orders-db`
   refusing a connection) and an Error in RED terms on the service that depended on it
   (`checkout-api` returning a 5xx) — the two methods describe one event from two vantage points.
4. A resource can look fine on every default dashboard panel and still be the bottleneck, because
   the default panels are almost always utilization panels. Treat "what would saturation look like
   here, and where do I query it" as a mandatory second question for every resource, not an
   optional refinement.
5. Instrumenting only utilization is the single most common USE implementation gap. If a resource
   is on a dashboard with a utilization panel and no saturation panel, treat that as unfinished,
   not as "good enough."
