# Alerting and Dashboards: Cutting the Signal Down to What Earns a Page

You now have RED signals (doc 01), USE signals (doc 02), an SLO and error budget (doc 04), and a
histogram instrumented so its buckets actually mean something (doc 05). This doc is about the last
step: deciding which of those signals go on a dashboard, which of them get an alert, and — the
harder question — which of them get neither, because most of them should not.

## The 34-panel dashboard, revisited

Doc 00 described `checkout-api`'s original dashboard: 34 panels, every one correctly graphed, none
of them the thing on-call actually needed at 2 a.m. The failure was not inaccuracy, it was
selection. Derive a replacement instead of asserting one.

Start from what an on-call engineer needs to answer, in order, during an incident: is the service
actually hurting customers right now (RED, at the service's own boundary), and if so, is it the
service's own code or something it depends on (USE, on what it depends on). That ordering — RED
first, USE second — is the dashboard's structure, not just its content:

1. **Request rate** — `sum(rate(http_requests_total{job="checkout-api"}[5m]))`. Confirms traffic
   is arriving at all; a rate collapsing to near zero, on a service that normally does 640 req/s,
   is itself an incident (an upstream load balancer problem, most likely), and this panel is the
   one that reveals it.
2. **Error rate** — `sum(rate(http_requests_total{job="checkout-api", code=~"5.."}[5m])) /
   sum(rate(http_requests_total{job="checkout-api"}[5m]))`. The numerator of doc 04's SLI, isolated.
3. **Latency: p50, p99, p99.9 on one panel** — three `histogram_quantile()` lines, so the shape of
   degradation is visible: if only p99.9 moves, a small slice of requests is hitting something
   specific (a cold cache key, a retry); if p50 moves too, the whole fleet is affected.
4. **`orders-db` connections in use, against `max_connections`** —
   `pg_stat_activity_count{datname="orders"} / 600`. The single USE signal most likely to explain a
   RED-panel problem for this specific service, because doc 03's worked incident showed exactly
   this dependency saturating first.
5. **`order-processor` consumer lag on `order-events`** —
   `kafka_consumergroup_lag{consumergroup="order-processor-group", topic="orders.created"}`. The
   second most likely explanation: if `checkout-api`'s own RED signals are fine but confirmed
   orders are appearing late downstream, this is where that shows up.
6. **`session-cache` memory utilization** — `redis_memory_used_bytes / redis_memory_max_bytes`.
   Every request touches session state; a Redis cluster approaching its memory ceiling degrades
   `checkout-api` even though nothing in `checkout-api`'s own code changed.

Six panels, not 34. Each one is on the dashboard because a specific, previously-observed incident
(doc 03's connection-pool exhaustion is one; a Redis memory ceiling and a stalled consumer are the
other two live in this collection) showed up there first or explained what a RED panel already
flagged. A panel that has never been the first place an incident showed up, and cannot be tied to
a specific dependency the service actually has, does not belong on this dashboard — it belongs, at
most, on a deeper drill-down dashboard one click away, which nobody needs to stare at during a page.

## Every alert must map to an action, or it should not exist

Contrast two alert definitions on the same underlying fact — CPU pressure on `checkout-api`'s
nodes:

```yaml
# Alert A
- alert: HighCPU
  expr: node_cpu_utilization > 0.90
  for: 5m
```

```yaml
# Alert B
- alert: CheckoutLatencySLOBurn
  expr: |
    (
      sum(rate(http_request_duration_seconds_bucket{job="checkout-api", le="0.5"}[1h]))
      /
      sum(rate(http_request_duration_seconds_count{job="checkout-api"}[1h]))
    ) < (1 - 14.4 * 0.001)
  for: 2m
```

Alert A fires on a fact that may or may not matter: a CPU-bound batch job sharing the node, a
momentary GC pause, or genuine customer-facing degradation all look identical to this rule. The
engineer who gets paged has to go find out which one it is before they know whether to do
anything — the alert has outsourced its own diagnosis to whoever answers it. Alert B is doc 04's
fast burn-rate alert: it fires only when the SLO — the thing actually tied to abandoned carts — is
being consumed 14.4x faster than sustainable. Whoever answers it already knows the consequence
("we're burning through this month's error budget right now") without doing any translation work
first. **Every alert should let the page itself answer "why does this matter," not just "what
crossed a line."**

This does not mean CPU is never worth looking at — it means CPU is not worth *paging on directly*.
It belongs on the drill-down dashboard, one layer below the RED-based page, as the fast diagnostic
step once you already know customers are affected.

## Page on symptoms, use resources to diagnose

Generalize the CPU example into a rule: **alert on RED signals that breach the SLO (symptoms);
keep USE signals on dashboards as the drill-down layer, not as independent pages (causes).** The
reasoning is the same as above at the level of an entire alerting strategy rather than one rule.

A resource being under pressure is not, by itself, evidence that a customer is affected — `orders-db`
running at 550 of 600 connections for ten minutes during a batch job's overlap window (doc 02) may
never show up as a single failed checkout, if `checkout-api`'s own connection pool and retry logic
absorb it. Paging on the resource number directly means paging on every such absorbed blip, most
of which resolve before anyone could have acted on the page anyway. Paging on the RED-based SLO
burn means you only wake someone when the resource pressure has actually translated into customer
impact — and when it has, the USE dashboard from the previous section is exactly where you look
next, already correlated in time with the page that woke you.

The exception worth naming: a resource with a hard, unrecoverable failure mode and no absorbing
layer above it deserves its own page regardless of current customer impact — `session-cache`
running out of memory entirely evicts session keys outright rather than degrading gracefully, so a
saturation alert on Redis memory approaching 100% is reasonable as a second, independent page,
because by the time it shows up as a RED-signal burn on `checkout-api`, active user sessions have
already been destroyed. The rule is "prefer symptom-based paging," not "never page on a cause" —
apply it component by component, based on whether something above the resource actually absorbs
its saturation.

## Why alert fatigue is arithmetic, not a mood

Suppose Riverbend instruments each of its roughly 40 request-driven services the naive way: ten
independent threshold alerts per service — CPU, memory, disk, error count, p50, p99, connection
pool, queue depth, restart count, and one more for good measure.

```
40 services × 10 independent threshold alerts = 400 alerts
```

Four hundred alerts, most of them thresholds like Alert A above, each capable of firing on a
transient condition that resolves on its own. Even a modest 2% chance per alert per week of firing
on something that turns out not to matter produces roughly 8 spurious pages a week across the
fleet — enough that on-call engineers rationally start responding slower, or muting categories of
alert wholesale, which is exactly how a real incident's page gets missed inside the noise.

Replace those ten per-service threshold alerts with the doc 04 approach — one SLO, one pair of
burn-rate alerts (fast and slow) per service — and the count drops to:

```
40 services × 2 burn-rate alerts = 80 alerts
```

Eighty alerts, each one tied directly to a business-meaningful budget rather than an arbitrary
threshold, is a fleet a human can actually hold a mental model of. The reduction is not from
alerting on less — it is from alerting on the thing that already aggregates the ten separate
symptoms of "this service is unhealthy" into the one number (the SLI) that was always the point.

## A worked incident, from page to root cause

Reuse doc 03's connection-pool exhaustion scenario and walk the on-call path against the
six-panel dashboard above, with rough elapsed time per step:

**T+0:00** — `CheckoutLatencySLOBurn` (the fast pair from doc 04) pages. The page itself states the
burn rate and the SLO it threatens, not just "latency high."

**T+0:30** — On-call opens the dashboard. Panel 3 (latency) shows p99 climbing from 310ms toward
2.1 seconds over the last four minutes; panel 2 (error rate) is only slightly elevated, so this
reads as a queuing problem, not a code-path throwing errors outright.

**T+1:15** — Panel 4 (`orders-db` connections in use) shows the line pinned flat at 600 of 600 —
not climbing, *pinned*, which is the specific shape of a resource that has hit its ceiling and
stayed there, as opposed to one still climbing toward it. On-call now has a specific hypothesis
instead of a general one.

**T+2:00** — On-call checks `pg_stat_activity` directly (one query, informed by exactly which
resource panel 4 flagged) and finds 600 connections held open, a large fraction idle-in-transaction
— consistent with doc 03's cause: a slow query holding a transaction open longer than usual under
load, starving the pool for everyone else.

**T+3:30** — Mitigation applied (kill the longest-held idle-in-transaction sessions, buying
headroom while the slow query is fixed properly); panel 4 begins dropping from 600; panel 3's p99
follows it down within about ninety seconds, confirming the causal link rather than assuming it.

Just under four minutes from page to a confirmed, specific root cause, because the page pointed at
customer impact immediately and the dashboard's second layer pointed at the one dependency actually
worth checking — rather than at a 34-panel dashboard where the pinned-at-600 line was one signal
among dozens with no particular claim on the engineer's attention first.

## What to take away

1. A dashboard for a service should have a handful of panels, ordered RED-first (customer impact)
   then USE (the dependencies most likely to explain it) — not a comprehensive inventory of every
   metric the service happens to export.
2. Every alert should let the page state why it matters, not just what threshold it crossed. An
   SLO burn-rate alert does this by construction; a raw resource threshold usually does not.
3. Prefer paging on symptoms (RED signals breaching the SLO) and using USE signals as the
   drill-down layer during triage — except for resources with a hard, unrecoverable failure mode
   nothing above them absorbs, which deserve their own page.
4. Alert fatigue is a predictable consequence of alert count and per-alert noise rate, not a
   discipline problem — ten independent threshold alerts per service across 40 services produces
   400 alerts; two SLO burn-rate alerts per service produces 80.
5. A well-designed dashboard turns an incident into a short, directed sequence of checks — the
   worked example above goes from page to confirmed root cause in under four minutes because each
   panel answered a specific question the previous one raised.
