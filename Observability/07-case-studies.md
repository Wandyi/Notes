# Case Studies: Five Incidents at Riverbend

Doc 04's failure catalogue for CronJobs works because each entry is a real shape, not a hypothetical
one. This doc does the same thing for observability: five incidents, each one a case where the
*mechanism* was ordinary but the *signal* that should have caught it either did not exist or was
looking at the wrong thing. Each entry follows the same structure — what you saw, the mechanism
underneath it, how you would confirm it, the fix, and — the part unique to this doc — what signal
would have caught it sooner, and why nobody had it yet.

Read these after docs 01–06. Every incident here is the "what happens when you skip this" version
of an argument made abstractly earlier in the collection.

---

## CS-1 · The connection pool that emptied in under three minutes

**What you see.** At 14:02, the on-call engineer for `checkout` is paged: `checkout-api p99 latency
above 500ms for 5 minutes`. `/checkout` requests that normally return in 310ms at p99 are taking
several seconds, and a growing fraction are timing out outright.

**The investigation, minute by minute — this is the part worth reading closely, because the delay
is the incident.**

- **14:02** — Page fires. On-call opens the `checkout-api` pod dashboard: CPU sits at 22% across all
  24 replicas, memory at 340Mi of a 512Mi limit. Nothing here explains a latency spike, so on-call
  moves on.
- **14:05** — On-call checks `checkout-api`'s own logs for the slow requests. The stack traces show
  time spent inside the database client's connection-acquisition call, not inside application logic.
  That points at `orders-db`, so on-call switches dashboards.
- **14:11** — The `orders-db` infrastructure dashboard shows CPU at 41%, disk I/O comfortably below
  its provisioned IOPS, and no replication lag. Every panel on this dashboard is a **utilization**
  number, and every one of them looks fine — which is exactly doc 02's trap. Nothing here shows that
  `orders-db`'s **connection slots**, a resource with its own hard ceiling (`max_connections = 600`),
  are the thing actually exhausted.
- **14:19** — Seventeen minutes after the page, on-call thinks to run
  `SELECT count(*) FROM pg_stat_activity`, and gets back 600. Every connection slot is in use. That
  is the root cause: `checkout-api`'s connection pool has grown to fill the database's entire
  capacity, so every request that needs a connection queues behind one that already has it.

**Mechanism.** `orders-db` normally runs with about 180 of its 600 connection slots in use — the
number carried in this collection's running-example table. At 13:36, Riverbend's third-party
fraud-check dependency (called synchronously from the checkout write path) began degrading, and its
timeout rate rose from a baseline 0.3% of calls to roughly 6%. A retry path added three weeks
earlier opens a fresh database connection before confirming the previous one closed on the timeout
branch specifically — a leak that only manifests on that one error path. Here is why the exhaustion
happened in minutes, not hours:

```
Steady request rate:                 640 req/s
Elevated dependency-timeout rate:     6% of requests
Timeouts per second:                  640 × 0.06 = 38.4 timeouts/sec
Leak rate (1 leaked connection
  per 15 timeouts on the buggy path): 38.4 / 15 ≈ 2.56 leaked connections/sec
Spare capacity before exhaustion:     600 - 180 = 420 connections
Time to exhaust spare capacity:       420 / 2.56 ≈ 164 seconds ≈ 2 minutes 44 seconds
```

The leak started at 13:36. The connection pool was fully exhausted by roughly 13:39 — a full 23
minutes before the p99 alert even fired, because `checkout-api`'s p99 only crossed the 500ms
alerting threshold once enough requests were actually queueing behind the exhausted pool to move
the percentile that far. The alert was correct; it just could not have fired any earlier given what
it was watching.

**Confirm it.**
```promql
# orders-db connections in use as a fraction of the hard ceiling — the USE utilization signal
pg_stat_activity_count / pg_settings_max_connections
```
```sql
-- What's actually holding a connection open, and for how long
SELECT pid, state, now() - query_start AS held_for, query
FROM pg_stat_activity
WHERE state != 'idle'
ORDER BY held_for DESC
LIMIT 20;
```

**The fix.** Immediate: restart the fraud-check client with a hard connection-acquisition timeout
(2 seconds) so a stuck request fails fast instead of holding its slot indefinitely, which drains the
backlog within one connection lifetime. Root cause: the retry path is rewritten to close the
existing connection before opening a replacement one, and a unit test asserts connection count stays
flat across 1,000 simulated timeouts. Structural: `checkout-api`'s connection pool is capped well
below `orders-db`'s ceiling (450, leaving headroom for `invoice-rollup` and `payout-settlement`'s own
connections, per doc 03's reconciliation of shared dependencies), so a leak in the application can
never single-handedly starve the database for every consumer.

**What signal would have caught it sooner.** A single dashboard panel showing `checkout-api`'s RED
picture (p99 latency, error rate) directly above `orders-db`'s USE picture — specifically
**connections in use as a percentage of `max_connections`**, not CPU — would have shown both moving
together from 13:39 onward. Doc 03 works through this exact pairing as its worked example, and
argues that on-call would have found root cause in under two minutes instead of seventeen, because
the correlation is visible without switching dashboards or guessing which resource to check. The
missing piece was never data — `pg_stat_activity` was queryable the whole time — it was that nobody
had put the *saturation* signal for connections next to the *utilization* signal for CPU, so the
dashboard that existed answered "is the box busy" instead of "is the box out of the one thing that
matters."

---

## CS-2 · The outage the average never saw

**What you see.** Customer support tickets mention "checkout hanging" for about twenty minutes on a
Tuesday afternoon. Nobody on the platform team was paged. When someone finally checks the
`checkout-api` latency dashboard afterward, average latency for the period reads 44ms — barely above
the usual 38ms — and nothing on the standard dashboard suggests a problem happened at all.

**Mechanism.** A specific version of Riverbend's mobile app, released two days earlier, retries a
failed checkout submission in a tight loop with no backoff and no cap on attempts, and does so with
the full original payload each time. During the incident window, roughly 0.05% of requests to
`/checkout` — customers on that app version who hit one transient failure — became stuck in this
loop, and their individual requests each took up to 12,000ms to eventually resolve (the request
itself was not failing outright, just queueing behind the same growing backlog its own retries were
contributing to).

Here is why the average absorbed this without moving much, and why doc 01's argument about averages
applies exactly here:

```
Baseline:  99.95% of requests at ~38ms, 0.05% of requests at ~12,000ms

Average = 0.9995 × 38ms + 0.0005 × 12,000ms
        = 37.98ms + 6.00ms
        = 43.98ms  ≈  44ms
```

A 6ms shift on a metric that already has normal day-to-day noise of ±5-10ms is invisible against the
baseline — nobody would set an alert threshold tight enough to catch it without also alerting
constantly on ordinary variance. Compare what the same 0.05% did to the tail:

```
p99.9 is, by definition, the value below which 99.9% of requests fall — i.e. it describes the
worst 0.1% of traffic. Half of the affected 0.05% (itself sitting right at the boundary of that
worst 0.1%) is enough to drag the 99.9th percentile from its usual 900ms up past 6,000ms, because
the affected population's actual latency (12,000ms) now occupies exactly the slots the percentile
is measuring.
```

The percentile did not miss anything. It was built to describe exactly this population, and it
moved by more than 6×. The average was built to describe the typical request, and the typical
request was completely unaffected — both numbers were "correct," and only one of them was useful
for noticing an incident.

**Confirm it.**
```promql
# The percentile the incident actually shows up in
histogram_quantile(0.999,
  sum by (le) (rate(http_request_duration_seconds_bucket{service="checkout-api", endpoint="/checkout"}[5m]))
)

# Compare against the average, computed from the same histogram, to see the gap directly
rate(http_request_duration_seconds_sum{service="checkout-api", endpoint="/checkout"}[5m])
/
rate(http_request_duration_seconds_count{service="checkout-api", endpoint="/checkout"}[5m])
```

**The fix.** Immediate: identify the offending app version from the `client_version` label on
request logs (not a metric label — see doc 05 on why a per-version label on a high-cardinality-prone
dimension like this belongs in logs, not in the metric itself) and force an app-store rollback.
Structural: the mobile client gets exponential backoff with a retry cap, and the checkout endpoint
gets a per-customer rate limit so one client's misbehavior cannot compound against a shared resource.

**What signal would have caught it sooner.** p99.9 latency, alerted on with its own threshold
separate from p99 — Riverbend added exactly this after this incident, at a 3,000ms threshold, which
is comfortably above the normal 900ms but would have fired within the first two minutes of this
one. The broader lesson doc 01 makes explicit: never dashboard or alert on a latency average alone,
because the average is mathematically designed to be dominated by the bulk of traffic, which is
precisely the part of traffic that is not the problem during a tail-latency incident.

---

## CS-3 · The lag that grew for six hours before anyone noticed

**What you see.** A data analyst asks, at 15:20 on a Thursday, why order confirmation emails have
been arriving up to forty minutes after checkout instead of the usual few seconds. Nobody was paged.

**Mechanism.** `kafka_consumergroup_lag` for `order-processor-group` began climbing at 09:14 and did
not stop until an engineer manually intervened at 15:20 — just over six hours. The cause was routine
and easy to miss: `orders-db`'s autovacuum on its largest table (`orders`, tens of millions of rows)
had never been tuned to prefer Riverbend's overnight quiet window, so it triggered mid-morning and
competed with `order-processor`'s writes for Aurora's I/O capacity. Average write-commit latency for
`order-processor`'s inserts rose from its normal 8ms to around 19ms — not a dramatic-looking number
on its own, but enough to push the consumer group's aggregate write throughput just below the
incoming rate of events on `order-events`.

The accumulation is simple arithmetic once you have the rate:

```
Observed average lag growth: ~14 messages/second (read directly off the metric's slope once
                              someone finally looked)
Duration before intervention: 6 hours = 21,600 seconds
Total accumulated lag:        14 × 21,600 = 302,400 messages
```

Three hundred thousand order confirmations were queued and delayed by the time anyone noticed —
consistent with what doc 00 described this exact scenario as producing: lag "in the hundreds of
thousands."

**Why nobody noticed sooner.** `order-processor`'s pod dashboard showed CPU at roughly 18% and
memory flat the entire six hours, because the pods were spending their time *waiting* on slow
database writes, not consuming CPU. A utilization-only view of the resource `order-processor`
depends on (`orders-db`'s write path) looked completely uneventful, exactly as doc 02 predicts:
**utilization tells you how busy something is, not how much work is backed up waiting for it.** The
one signal that was moving the entire time — consumer lag, which is the saturation signal for a
Kafka consumer — had no alert defined on it at all.

The shape of this failure is the same one `K8s/cronJobs` doc 08 describes for a CronJob that
silently stops firing: the thing that would tell you something is wrong is *absence of progress*,
not a failure event, and if you only alert on failures or on "is the box busy," a silently growing
backlog produces no signal until a human notices the downstream symptom.

**Confirm it.**
```promql
# Lag by partition, so you can see whether it's uniform (write-path bottleneck) or localized
# (one hot partition, a different problem entirely)
kafka_consumergroup_lag{group="order-processor-group", topic="orders.created"}

# Cross-check against the dependency doing the actual work
rate(pg_stat_statements_total_time{query=~".*INSERT INTO orders.*"}[5m])
/ rate(pg_stat_statements_calls{query=~".*INSERT INTO orders.*"}[5m])
```

**The fix.** Immediate: pause the autovacuum manually, let the consumer group catch up (at full
speed, 6 pods' worth of freed capacity clears 302,400 messages in a little over eight minutes).
Structural: tune `autovacuum_vacuum_cost_delay` and scheduling so maintenance work on `orders` prefers
Riverbend's 09:00–13:00 UTC quiet window — the same window `K8s/cronJobs` doc 09 already uses for
node maintenance, reused here for database maintenance for the same reason.

**What signal would have caught it sooner.** An alert on `kafka_consumergroup_lag` exceeding a
threshold derived from the topic's normal processing time — not an arbitrary round number, but
"lag that would take longer than 10 minutes to drain at the consumer group's typical throughput." At
`order-processor`'s normal capacity, a lag alert set at 10,000 messages would have fired around
09:26, twelve minutes into the incident, instead of six hours later.

---

## CS-4 · One label turned one metric into thirty-two million

**What you see.** Grafana dashboards across the entire cluster — not just `checkout-api`'s — become
sluggish to load, some panels timing out. The on-call Prometheus operator sees the server's memory
climbing steadily toward its limit with no corresponding traffic increase anywhere.

**Mechanism.** During a debugging session two days earlier, an engineer investigating a
customer-specific checkout failure added a `customer_email` label to `checkout-api`'s existing
`http_requests_total` counter, intending to filter by that one customer while diagnosing the issue,
and the change merged along with an unrelated fix without anyone noticing the label was still
attached.

Before the change, `http_requests_total`'s cardinality was bounded by the dimensions that actually
repeat: 14 checkout endpoints × 2 HTTP methods × 6 status-code buckets × roughly 40 pod instances at
a time ≈ 6,720 active series — a number that stays roughly flat no matter how much traffic arrives,
because those label *values* repeat across every request.

`customer_email` does not repeat. It is close to unique per request. So instead of a bounded set of
label combinations, the metric started minting a new time series for nearly every request:

```
Steady request rate:        640 requests/sec
New series per second:      ≈ 640 (one per request, since customer_email is ~unique)
Time before anyone noticed: 14 hours = 50,400 seconds
New series created:         640 × 50,400 ≈ 32,256,000
```

Over thirty-two million new, mostly single-use time series, each consuming roughly 2-4KB of head
block memory in Prometheus, added somewhere between 60GB and 120GB of unplanned memory pressure to a
server sized for a small fraction of that — and because one Prometheus server scrapes the whole
cluster, every other team's dashboards and alerts degraded along with `checkout-api`'s, not just the
service that caused it.

**Confirm it.**
```promql
# Which metric name is responsible for the spike in total active series
topk(10, count by (__name__) ({__name__=~".+"}))

# Confirm it's a label-cardinality problem specifically, not a legitimate metric-count increase
count(count by (__name__, customer_email) (http_requests_total)) by (__name__)
```
```bash
curl -s localhost:9090/api/v1/status/tsdb | jq '.data.headStats.numSeries'
```

**The fix.** Immediate: remove the label and redeploy; use the TSDB admin API's delete-series
endpoint to drop the orphaned series rather than waiting for their natural retention to expire, since
each one holds memory until then. Structural: doc 05's cardinality budget — a per-metric label
allowlist enforced in code review and, ideally, by a linter that flags any label name matching a
denylist of known-unbounded fields (email, user ID, IP address, request ID, free-text). The
debugging need that prompted the original change is real and worth keeping, but it belongs in a
structured log line scoped to the one investigation, not a label on a metric that ships to
production forever.

**What signal would have caught it sooner.** An alert on Prometheus's own `prometheus_tsdb_head_series`
growth rate — a meta-alert on the observability system observing itself — would have fired within
the first hour, long before cluster-wide dashboard degradation made the problem visible to everyone
at once. Riverbend added this alert after the incident: page if total active series grows by more
than 5% in any 15-minute window outside of a planned deploy of a new service.

---

## CS-5 · The alert that paged on five errors, and the one that would not have

**What you see.** At 03:14 on a weeknight, on-call is paged: `checkout-api error rate > 1% for 5
minutes`. Investigation finds five failed requests out of eighty attempted during that window — a
handful of legitimate, unremarkable client-side validation failures — and nothing wrong with the
service at all. The page was correct by the letter of its own rule and useless in every way that
matters.

**Mechanism.** The alert's rule was a fixed error-rate threshold with no awareness of traffic volume:

```yaml
- alert: CheckoutErrorRateHigh
  expr: |
    sum(rate(http_requests_total{service="checkout-api", status_code=~"5.."}[5m]))
    /
    sum(rate(http_requests_total{service="checkout-api"}[5m]))
    > 0.01
  for: 5m
```

During the overnight low-traffic window, `checkout-api` saw roughly 80 requests across that 5-minute
evaluation window — about 0.27 requests per second, far below the steady daytime 640 req/s in this
collection's running-example table. Five of those eighty happened to fail:

```
Naive error rate = 5 / 80 = 6.25%
Threshold = 1%
6.25% > 1%  ->  alert fires
```

Five errors is a rounding error at daytime volume — at 640 req/s, five errors in five minutes is a
0.0026% error rate, nowhere near any reasonable threshold. The alert is not wrong about the *ratio*;
it is wrong to treat the ratio the same way regardless of how many requests it was computed from.

**Confirm it.** Compare what a burn-rate alert, built from the SLO in doc 04, would have done with
the same five errors. `checkout-api`'s SLO is 99.9% success monthly, so its error budget is 0.1% of
requests. A multi-window burn-rate alert requires the *rate of budget consumption* to exceed a
threshold in **both** a short window and a longer window before paging — using the standard
fast-burn convention (a burn rate high enough to exhaust 2% of the 30-day budget within one hour):

```
Burn rate = observed error rate / budget error rate

5-minute window:  6.25% / 0.1%  = 62.5×   (exceeds the 14.4× fast-burn threshold)
1-hour window:    at the same ~0.27 req/s overnight rate, 1 hour ≈ 972 requests.
                  If the five errors were an isolated blip (not sustained), the 1-hour
                  window sees the same 5 errors diluted across 972 requests:
                  5 / 972 ≈ 0.51%
                  0.51% / 0.1% = 5.1×     (below the 14.4× threshold)

Page condition: BOTH windows must exceed 14.4×.
5-minute window: yes (62.5×).  1-hour window: no (5.1×).  -> does not page.
```

The short window alone looks alarming, which is exactly why the naive alert fired. The long window
shows the same five errors are not a sustained trend, just noise from a small sample — and a
burn-rate alert that requires both windows to agree does not page on noise it cannot yet distinguish
from a real problem. If the errors had kept happening at the same rate for the full hour (indicating
something actually wrong), the 1-hour window's burn rate would have climbed past 14.4× too, and the
page would have been justified.

**The fix.** Replace the fixed-threshold error-rate alert with the multi-window burn-rate alert from
doc 04, sized against `checkout-api`'s actual SLO rather than an arbitrary 1%.

**What signal would have caught it sooner** is the wrong question for this incident — the existing
signal caught it *too eagerly*. The lesson is the mirror image of the other four case studies:
observability failures are not only "we had no signal," they are also "we had a signal that could
not tell noise from a trend," and a single-window, traffic-blind threshold is that failure mode's
most common cause.

---

## One-page cheat sheet

| Symptom | Root cause class | Doc |
|---|---|---|
| Latency climbs, resource dashboards all look "fine" | Saturation signal missing next to a utilization one (CS-1) | 02, 03 |
| An incident that never moves the average | Average hides a small, severe tail (CS-2) | 01 |
| A backlog grows for hours with no alert | No saturation-based alert on a queue or consumer group (CS-3) | 02, 06 |
| Cluster-wide dashboard/metrics slowness with no traffic change | Unbounded-cardinality label (CS-4) | 05 |
| An alert fires on a tiny sample at low traffic | Fixed-threshold alert with no volume or burn-rate awareness (CS-5) | 04, 06 |

## What to take away

1. The most expensive incidents in this doc were not missing data — `pg_stat_activity`,
   `kafka_consumergroup_lag`, and the raw request logs were all queryable the whole time. They were
   missing the one signal, put in the one place, that would have made the correlation obvious in
   under a minute.
2. Utilization dashboards are the most common false reassurance in this list. Three of the five case
   studies (CS-1, CS-3, and implicitly CS-4's TSDB memory) involved a component that looked
   comfortably utilized while the thing actually running out — connection slots, consumer capacity
   relative to arrival rate, memory headroom — was never graphed.
3. An average is a real number describing a real thing — the typical request — and that is precisely
   why it cannot describe a tail event affecting a small fraction of requests. Always pair it with a
   high percentile before trusting it to represent an SLO.
4. A label that is unique or near-unique per request (email, user ID, request ID, raw IP) does not
   belong on a metric, ever, regardless of how useful it seems mid-investigation. It belongs in a log
   line.
5. Not every incident here was a missing alert. CS-5 shows the opposite failure — an alert that
   exists and fires, but cannot distinguish a real trend from noise in a small sample. Both failure
   directions cost real time and trust in the alerting system.
6. Every one of these incidents was found, eventually, by a human reasoning from first principles
   under pressure. The point of docs 04 and 06 is to make that reasoning happen once, at design time,
   instead of every time, at 3 a.m.
