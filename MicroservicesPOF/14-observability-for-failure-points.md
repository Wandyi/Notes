# Observability for Failure Points — Finding Out Which One You Are In

This doc assumes you know RED, USE, golden signals, SLOs, and error budgets. If you do not, read
[`../Observability`](../Observability/README.md) first — it derives all of them properly and
this doc will not repeat that material.

What this doc does instead is narrower and, in an incident, more useful: **for each of the
thirteen POF classes, which signal detects it, which signal misleads you, and what to look at
first.**

The gap it closes is a specific one. Standard observability practice instruments *components* —
this service's error rate, that database's CPU. But most of the failures in this collection are
not component failures. They are failures of the *relationships* between components: a retry
policy, a connection pool, a cache hit rate, a queue's age, a config propagation. Those do not
appear on a per-service dashboard, and that is why the most common sentence in a serious incident
is:

> "Everything is green and the site is down."

## Why everything is green

Three structural reasons, and each one has a fix.

**1. Components report on themselves.** A service's error rate measures requests that reached it
and failed. A request that never arrived (DNS, `E-02`), or that was rejected by the load balancer
(`E-09`), or that timed out in the accept queue before being dequeued (`E-14`) is not in that
number. **The service is honestly reporting 0% errors while serving 40% of its traffic.**

*Fix: measure from the client's position, not the server's.* Real-user monitoring and synthetic
probes from outside your network are the only signals that see hops 1–6 of doc 01.

**2. Averages hide distributions.** A service with 24 instances where one is a black hole
(`D-04`) has a fleet error rate of 4%, which may be inside the error budget. For the users
hitting that instance it is 100%. Same for shards, cells, zones, tenants, and API versions:
**any metric aggregated across a dimension that can fail independently will hide a total failure
of one member.**

*Fix: alert on the worst member, not the aggregate.* `max by (instance)` rather than `sum`.

**3. The dangerous failures produce successes.** A slow dependency returns HTTP 200 (`R-14`). A
stale replica returns correct-looking data (`S-06`). A cache serves an old value (`C-09`). A
consumer that stopped has no errors because it has no requests (`Q-01`). **Every mechanism you
have is triggered by errors, and these produce none.**

*Fix: measure the things that are wrong when the answer is wrong* — staleness, age, lag,
reconciliation mismatches — not just failure.

## The detection matrix

For each POF class: the signal that detects it, the signal that lies during it, and the first
query to run.

| Class | Detects it | **Misleads during it** | First question |
|---|---|---|---|
| **E** Edge / path | Synthetic probes from outside; RUM; *traffic volume anomaly* | Server error rate (0% — the requests never arrived) | Is traffic *down* rather than failing? From where? |
| **R** Sync RPC | Client-side per-dependency latency and attempt count | Server-reported handler duration (excludes queueing) | Which dependency's latency moved first? |
| **P** Patterns | Breaker state, bulkhead rejections, shed count, all as explicit metrics | Error rate (the pattern's rejections may not be counted as errors) | Is a protective mechanism currently active? |
| **F** Feedback | `attempts/requests`; queue age vs client timeout; healthy-instance count | Everything looks maximally bad, which tells you nothing about the loop | If I remove load, does it recover? |
| **D** Discovery | Endpoint-set size; config staleness per node; xDS NACK rate | Registry health (it is fine; the *propagation* is not) | Does the endpoint set in use match reality? |
| **S** Storage | Per-shard distribution; replication lag; pool acquire time; oldest transaction | Database CPU (a hot shard is invisible in cluster average) | Is it all shards or one? All queries or one shape? |
| **T** Transactions | **Reconciliation mismatch counts** | Every request-path metric (both sides succeeded) | Do the two systems that should agree, agree? |
| **C** Cache | Hit rate *per key pattern*; origin request rate; eviction rate | Cache CPU and memory (memory is 100% by design) | What is the hit rate and when did it change? |
| **Q** Async | **Age of oldest unprocessed message**; DLQ depth | Error rate, CPU, pod count — all normal for a stopped consumer | Is lag growing, flat, or shrinking? |
| **L** Locks / time | Duplicate-effect counts; lease renewal failures; clock offset; pause duration | Everything (a duplicate is two successes) | How many processes believe they hold this? |
| **G** Change | Deploy/config/flag change events on every dashboard | The metrics of whatever broke, which point downstream of the cause | **What changed in the last 60 minutes?** |
| **N** Capacity | Saturation (queue depth, in-flight, throttled periods) | CPU (wrong for I/O-bound; hides CFS throttling entirely) | What is saturated, and how long does more take? |
| **I** Isolation | Per-cell / per-zone / per-tenant success rate | Any global aggregate | What is the affected set, and does it match a boundary? |

Two rows are worth pulling out because they are the ones most often missing entirely.

**Row T** is the only class where *no request-path metric can ever detect the failure*, because
both writes succeeded and the failure is that they disagree. Reconciliation (doc 07, `T-13`) is
not a nice-to-have for correctness-critical systems; it is the only detector.

**Row G** is the one that saves the most time. Every dashboard in your organisation should have
deploy, config-change, and flag-flip annotations overlaid on the time axis. It costs a webhook
and it is the highest-value observability change most teams can make, because it answers the
first question of every incident without anybody having to go and look.

## The signals most teams do not have

Ranked by value-per-effort. If you implement the top five you will detect most of this
collection.

**1. Queue time (request age at start of handling).** The gap between the proxy receiving a
request and your handler starting. Invisible to every application framework's own timing, and it
is where `E-14`, `N-01`, `F-04`, and `P-08` all show up. Implementation: the proxy sets
`X-Request-Start`; the handler computes the delta.

```
histogram_quantile(0.99, sum by (le, route) (rate(http_queue_time_seconds_bucket[5m])))
```

**2. Age of oldest unprocessed message, per queue.** The only detector for the entire `Q` class
(doc 09, `Q-01`).

**3. Attempts per logical request, per dependency.** Makes retry amplification visible, which is
the sustaining effect in most long outages (`F-01`).

```
sum by (target) (rate(rpc_attempts_total[1m]))
  / sum by (target) (rate(rpc_requests_total[1m]))
```

**4. Cache hit rate per key pattern**, not in aggregate. An aggregate hit rate of 94% can hide
one pattern at 5% that is producing all of your origin load (`E-06`, `C-11`).

**5. Reconciliation mismatch count**, per pair of systems that must agree. The only detector for
class `T`.

**6. Client-observed latency minus server-reported handler time.** The sum of everything in
between. When an incident is invisible in your service metrics, this is where it shows up.

**7. Per-instance success rate as a distribution, not a mean.** Catches the black hole (`D-04`)
and the one bad shard.

```
# The worst instance, which is what users on it experience
min by (service) (
  sum by (service, instance) (rate(requests_total{code=~"2.."}[5m]))
  / sum by (service, instance) (rate(requests_total[5m]))
)
```

**8. CFS throttled fraction.** `N-06` is invisible in CPU utilisation and is a common cause of
otherwise-unexplained p99.

**9. Connection pool acquire time**, distinct from query time. Distinguishes "the pool is too
small" from "the database is slow" (`R-09`), which need opposite fixes.

**10. Config and discovery staleness per node.** How long since this instance last successfully
refreshed its configuration and its endpoint set. Catches `D-07`'s silent NACK, where a sidecar
keeps working on frozen config for weeks.

**11. Feature availability, per feature.** "Percentage of feed loads that included the
recommendations module." This is the only thing that detects `P-14` — a degradation that has been
running for three weeks with zero errors.

**12. Clock offset across the fleet.** `node_timex_offset_seconds`. Cheap, and `L-08` invalidates
timing assumptions system-wide.

## Alert on symptoms, and make causes queryable

The standard advice — alert on symptoms, not causes — is correct and often misapplied, so it is
worth being precise about what it means here.

**Alert on**: things a user experiences. Error rate, latency, and — the ones people forget —
**traffic volume anomalies** (a drop means requests are dying before they reach you), **staleness**
(how old is the data users are getting), and **correctness** (reconciliation mismatches).

**Do not alert on**: CPU, memory, disk, cache hit rate, replication lag, queue depth, pod
restarts — *by themselves*. These are diagnostic. They tell you why, not whether.

But there are two important exceptions, and missing them is how teams end up with symptom-only
alerting that pages too late:

**Exception 1: leading indicators with a long lead time.** Certificate expiry (`E-07`), disk
filling, transaction-ID wraparound (`S-11`), quota headroom (`N-11`), integer sequence exhaustion
(`S-17`). These are causes, they are guaranteed to become outages, and the lead time is the whole
point. Alert on them, well in advance, as tickets rather than pages.

**Exception 2: signals for failures with no symptom.** A stopped consumer (`Q-01`) has no
user-facing symptom until hours later. A silent degradation (`P-14`) never has one. A
reconciliation mismatch (`T-13`) has one only at quarter-end. For these, the "cause" metric *is*
the symptom metric — there is nothing else.

### Dependency-aware alerting

A single failure at the bottom of a dependency graph produces alerts from everything above it.
`orders-db` has a problem, and you get forty pages: `order-processor`, `checkout-api`, the
gateway, six consumers, four dashboards' SLO burn alerts. The signal is buried in the noise, and
the people who could fix it are reading pages about services they do not own.

Two mechanisms, and you want both:

**Inhibition.** Suppress alerts whose cause is already alerting. Alertmanager expresses this
directly:

```yaml
inhibit_rules:
  - source_matchers: [severity="critical", component="database"]
    target_matchers: [severity=~"warning|critical", depends_on="database"]
    equal: [cluster, environment]
```

**Dependency-aware grouping.** Rather than suppressing, group all the alerts into one incident
annotated with the dependency graph, so a responder sees "42 services affected, common
dependency: `orders-db`" instead of 42 pages. This is better than pure inhibition because the
breadth is itself information — it tells you the blast radius.

The prerequisite for both is a **live dependency graph derived from traces**, not from a wiki. A
documented dependency graph is wrong within a month; a trace-derived one is correct by
construction and is also what you need for `R-16`'s cycle detection.

## SLOs for the four paths

Doc 01 established that read, write, async, and streaming are different paths. They need
different SLIs, and applying a request-path SLO to an async path is a common way to have an SLO
that cannot detect the failure.

| Path | SLI | Riverbend example |
|---|---|---|
| **Read** | Availability (fraction of requests that succeed) and latency | 99.9% of catalogue reads succeed within 300 ms |
| **Write** | Availability, latency, **and correctness** | 99.95% of checkouts succeed within 2 s; **zero unreconciled orders after 5 minutes** |
| **Async** | **Freshness**: fraction of items processed within a time budget | 99% of `order.created` events processed within 60 s; 99.9% within 5 min |
| **Streaming** | Connection availability and **message age** | 99.9% of connected clients receive an update within 5 s of the event |

The write-path correctness SLI is the one that is almost never written down and is the one that
matters most for a commerce or financial system. "99.95% of checkouts succeed" says nothing about
whether the successful ones produced consistent state.

And a note on error budgets for the async path: the budget is spent in *time behind*, not in
failed requests. A consumer that is 10 minutes behind for an hour has consumed a specific amount
of budget, and that arithmetic works the same way — it just uses a different unit.

## Tracing across the boundaries that break it

Doc 01 covered starting traces at the CDN. Three more boundaries break tracing, and each has a
specific fix:

**Across a queue** (`Q-10`): inject `traceparent` into message headers; model the consumer's span
as a *link* to the producer's span rather than a child, because the relationship is causal but
not synchronously nested.

**Across a batch**: one consumer poll processes 500 messages from 500 different traces. Do not
put them all in one trace (it becomes unreadable and the sampling is wrong). Create a span per
message, each linked to its originating trace, plus one batch span linking to all of them.

**Across a retry**: each attempt should be its own span, children of one logical-request span,
with an `attempt` attribute. Without this, a request that took 6 seconds across three attempts
looks like one 6-second span and you cannot see the amplification.

And the sampling decision, which matters more than the instrumentation: **head-based sampling at
1% misses almost every failure.** Use tail-based sampling — buffer the spans, decide after the
trace completes — and keep 100% of traces that contain an error, exceed a latency threshold, or
touch a rare code path, plus ~1% of the rest. This costs more in collector capacity and it is the
difference between having traces for incidents and having traces for the boring case.

## The green-dashboard checklist

When the dashboard is green and something is wrong, work through these in order. Each one
corresponds to a structural blind spot described above.

1. **Is traffic normal?** A 30% drop in request rate with a normal error rate means requests are
   dying before they reach you (`E` class). This is the single most missed signal.
2. **What does an external probe say?** From a different network, a different region, a different
   ISP. If synthetic probes fail and your metrics are green, everything between the user and you
   is suspect.
3. **Is any single member of any dimension at 100% failure?** Instance, shard, zone, cell, tenant,
   API version, client version. Check `min by (...)` on success rate for each dimension.
4. **Is queue time high while handler time is normal?** (`E-14`.)
5. **Is anything stale?** Replication lag, cache age, config age, consumer lag, derived-data age.
   Staleness produces successful responses containing wrong data.
6. **Does reconciliation agree?** If two systems should match and do not, you have a `T`-class
   failure that no request metric will ever show.
7. **Is a protective mechanism active?** A breaker open, a bulkhead rejecting, a shedder
   shedding, a rate limiter limiting. These are "working as designed" and are also an outage for
   whoever is being rejected.
8. **Is a feature silently disabled?** Check per-feature availability, not just request success
   (`P-14`).
9. **What changed?** Deploys, config, flags, infrastructure, *and* upstream dependencies' changes.
10. **Is there a gray failure?** One node slightly slow, one network path lossy, one replica
    behind. Look at distributions across instances rather than aggregates.

## Triage: the first five queries

Under pressure, in this order. The point of the ordering is that each query either identifies the
class or eliminates several.

```
# 1 — What changed? (class G; eliminates most things if empty)
#     Deploys, config pushes, and flag flips in the last 90 minutes, all sources.

# 2 — Is it everyone, or a subset? (class I, E, D)
sum by (region, zone, cell, client_version) (rate(requests_total{code=~"5.."}[5m]))
  / sum by (region, zone, cell, client_version) (rate(requests_total[5m]))

# 3 — Is traffic arriving at all? (class E)
sum(rate(requests_total[5m]))            # compare to the same time last week

# 4 — Are we generating our own load? (class F — decides whether to shed or to fix)
sum(rate(rpc_attempts_total[1m])) / sum(rate(rpc_requests_total[1m]))

# 5 — What is saturated, and is it us or downstream? (classes N, R, S)
#     Compare queue time against handler time, and handler time against
#     dependency time. Whichever grew first is where to look.
histogram_quantile(0.99, sum by (le) (rate(http_queue_time_seconds_bucket[1m])))
histogram_quantile(0.99, sum by (le, target) (rate(rpc_duration_seconds_bucket[1m])))
```

Query 4 is the one that changes your strategy rather than your diagnosis: if amplification is
above 2, you are in a feedback loop, and doc 04's procedure applies — **reduce load, do not fix
the cause first.**

## What to take away

1. **Standard observability instruments components; most failures here are failures of the
   relationships between components.** That gap is why "everything is green and the site is down"
   is the most common sentence in a serious incident.
2. **Three structural blind spots**: components report only on requests that reached them;
   aggregates hide the total failure of one member; and the dangerous failures produce successes.
   Each has a specific fix — measure from outside, alert on the worst member, measure staleness
   rather than only failure.
3. **A traffic *drop* with a normal error rate is a failure signal**, and it is the one most
   teams do not alert on. It means requests are dying before they reach you.
4. **Alert on `min by (instance/shard/cell/tenant)`, not on the aggregate.** A 4% fleet error rate
   can be one instance at 100%.
5. **Overlay deploy, config, and flag-change events on every dashboard.** It costs a webhook and
   it answers the first question of every incident.
6. **The five highest-value missing metrics**: queue time, age of oldest unprocessed message,
   attempts per request, per-pattern cache hit rate, and reconciliation mismatch count. Those five
   detect most of this collection.
7. **Reconciliation is the only detector for the transaction class**, because both sides
   succeeded and no request-path metric can ever see the disagreement.
8. **"Alert on symptoms" has two exceptions**: leading indicators with long lead times (certs,
   disk, wraparound, quota) and failures with no user-facing symptom (stopped consumers, silent
   degradations). For the second group, the cause metric *is* the symptom metric.
9. **Use dependency-aware inhibition or grouping**, driven by a trace-derived dependency graph —
   a documented one is wrong within a month.
10. **The four paths need four different SLIs.** Async is measured in freshness, not availability;
    the write path needs a correctness SLI, which is the one nobody writes down.
11. **Propagate trace context across queues as links, span-per-message in batches, and
    span-per-attempt on retries** — otherwise amplification and async latency are invisible.
12. **Head-based sampling at 1% misses almost every failure.** Tail-based sampling that keeps all
    errors and all slow traces is the difference between having traces when it matters and not.
13. **In triage, run the amplification query early.** It does not tell you the cause; it tells you
    whether to reduce load before looking for one, which is a different and more urgent decision.

Next: [15-testing-for-failure-chaos-and-gamedays.md](15-testing-for-failure-chaos-and-gamedays.md),
which is about proving that any of this works before you need it to.
