# Best Practices and the Golden Instrumentation Template

Everything in docs 00-08 has been reasoning: why RED and USE exist, how to derive an SLO, what
cardinality actually costs. This doc is the output — a checklist you can run against a service
before it ships, split by the kind of component it is, plus the anti-patterns from every case study
in doc 07 collected into one list so a reviewer can recognize them without having read the whole
collection first.

The framing matters here the same way it does in `K8s/cronJobs` doc 10: **a best practice that
depends on every engineer remembering it is not a practice, it is a hope.** The last section turns
this list into something a pull request template or a linter can actually check.

## The golden template for a new request-driven service

This is what a service shaped like `checkout-api` should export before it is allowed to take
production traffic. Every metric carries the reason it exists and a pointer to the doc that derives
it.

```
# ---------- Rate and Errors: one counter, split by outcome (doc 01) ----------
# A request counter labeled by endpoint, method, and status code. Cardinality stays bounded
# because these three label sets repeat on every request — see doc 05's cardinality budget.
http_requests_total{endpoint, method, status_code}                          counter

# ---------- Duration (doc 01) ----------
# A histogram, not a summary (doc 05, Q5 in doc 08) — bucket boundaries chosen to bracket the
# SLO threshold tightly. checkout-api's SLO is p99 < 500ms, so buckets cluster there:
http_request_duration_seconds{endpoint, method}                             histogram
# buckets: [0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1, 2, 5]
# Five buckets fall between 100ms and 500ms specifically, because that is the range the p99
# alert needs to resolve accurately — a bucket set that only has [0.1, 1, 10] cannot tell a
# histogram_quantile() call whether p99 is 200ms or 480ms, both of which round into the same
# bucket boundary.

# ---------- USE, for whichever resources this service itself depends on (doc 02, 03) ----------
# checkout-api holds its own connection pool to orders-db and calls a synchronous fraud-check
# dependency — both are resources with a ceiling this service can exhaust:
db_pool_connections_in_use{pool="orders_db"}          gauge   # utilization numerator
db_pool_connections_max{pool="orders_db"}             gauge   # utilization denominator
db_pool_wait_seconds{pool="orders_db"}                histogram  # saturation: time queued for a slot
downstream_call_duration_seconds{dependency}          histogram  # both a RED signal for the call
                                                                  # and an early-warning saturation
                                                                  # proxy for the dependency (CS-1)
```

A service that ships without the duration histogram cannot have p99/p99.9 alerting at all (doc 01);
without the connection-pool gauges, an incident shaped exactly like CS-1 in doc 07 is invisible from
this service's own dashboard until the shared database's own metrics happen to be checked, which CS-1
showed cost fifteen extra minutes.

## The golden template for a new resource-backed component

This is what a service shaped like `order-processor` — something consuming from a queue and writing
to a resource with a ceiling, rather than serving synchronous requests — should export.

```
# ---------- Saturation: the earliest and most important signal for a consumer (doc 02, 03) ----------
kafka_consumergroup_lag{topic, partition}                gauge

# ---------- Rate and Errors, per record processed (doc 01's logic, applied per-record) ----------
records_processed_total{result}                          counter    # result: ok | retried | dropped
record_process_duration_seconds                          histogram

# ---------- Utilization, expressed as a counter so bursts between scrapes aren't averaged away ----
worker_busy_seconds_total                                 counter
worker_pool_size                                          gauge

# ---------- Staleness: is the oldest unacknowledged record actually moving? (doc 08, Q3) ----------
oldest_unacked_record_age_seconds                          gauge
```

`oldest_unacked_record_age_seconds` is the one item on this list most teams skip, and it is the one
that distinguishes "the consumer is slow" from "the consumer is stuck on a poison-pill record" —
lag alone cannot tell those apart, because both look like a growing number.

## Anti-patterns, collected

Every one of these showed up somewhere in docs 01-08, and each is repeated often enough across real
systems that a reviewer should recognize it on sight.

- **Alerting on, or dashboarding, latency as an average with no percentile.** Hides exactly the tail
  event an incident usually looks like — CS-2 in doc 07 is the concrete demonstration. (doc 01)
- **Treating utilization as if it were saturation.** A resource can sit at a comfortable-looking
  utilization number while a queue behind it grows without bound — CS-1 and CS-3 are both this
  mistake, from two different resources. (doc 02)
- **An unbounded or near-unique label on a metric.** Email, user ID, raw IP, request ID, or free text
  as a label value turns a bounded metric into an unbounded one — CS-4's 32-million-series incident.
  (doc 05)
- **A fixed-percentage alert threshold with no awareness of sample size.** Fires on noise during low
  traffic and can just as easily miss a real problem during high traffic — CS-5. (doc 04)
- **Alerting on every resource a service touches instead of on the customer-visible symptom.** Turns
  one incident into five uncorrelated pages instead of one page with good diagnostics attached — Q4
  in doc 08. (doc 06)
- **A blanket CPU or memory threshold applied identically to every service regardless of shape.**
  Pages for non-events on services that scale horizontally and stay silent for I/O-bound ones that
  are actually falling behind — Q2 in doc 08. (doc 02, 06)
- **A summary instead of a histogram for a latency metric on a horizontally-scaled service.**
  Per-instance quantiles cannot be validly aggregated across instances — Q5 in doc 08. (doc 05)
- **A batch job's success measured only by process exit code.** Exit 0 can mean "did nothing,"
  exactly as `K8s/cronJobs` F-13 describes for CronJobs; the same gap applies to any batch or
  queue-drain process that reports success without checking its own effect. (doc 08, Q12)

## The checklist

Run this against any new service, or any existing one whose instrumentation has not been reviewed
since it shipped.

**For anything that serves requests:**

- [ ] A request counter exists, labeled by endpoint, method, and status code — no label with more
      than a few dozen realistic values (doc 05).
- [ ] A duration histogram exists, with bucket boundaries chosen around the SLO threshold, not the
      client library's defaults (doc 01, 05).
- [ ] At least one percentile above p99 (p99.9 or higher) is computed and visible somewhere, not
      only the average (doc 01).
- [ ] An SLO exists, with a target derived from an actual business consequence and a comparison
      against historical incident data — not copied from another service's template (doc 04, Q1/Q6
      in doc 08).
- [ ] The paging alert is burn-rate-based against that SLO, evaluated across at least two windows
      of different length (doc 04, Q9 in doc 08).
- [ ] Every resource this service depends on with a hard ceiling (a connection pool, a thread pool,
      a rate-limited downstream call) has its own utilization and saturation signal, visible on the
      same dashboard the paging alert links to (doc 02, 03).

**For anything that consumes from a queue or processes a resource-bound backlog:**

- [ ] A lag or backlog-depth signal exists and has its own alert, sized to how long the backlog
      would take to drain at normal throughput — not an arbitrary round number (doc 02, 06).
- [ ] The age of the oldest unprocessed item is tracked separately from the backlog count, so a
      stuck item is distinguishable from a merely slow one (Q3 in doc 08).
- [ ] Worker utilization is exported as a counter of busy-time, not a sampled percentage gauge, so
      short bursts of full saturation are not averaged away (doc 02).
- [ ] CPU and memory dashboards exist as diagnostics but are not the primary alert for this
      component — the primary alert is the backlog/lag signal (doc 02, Q2 in doc 08).

**For every metric, on every kind of component, before it merges:**

- [ ] No label on any metric takes its value from user input, an email, a raw ID, an IP address, or
      free text (doc 05, CS-4 in doc 07).
- [ ] A rough cardinality estimate — label count multiplied across every label on the metric — has
      actually been computed, not assumed to be fine (doc 05, Q11 in doc 08).
- [ ] Every alert that pages has a runbook link, and the runbook says what to check first, not just
      what the alert means.

## What to take away

1. The template is longer for request-driven services than for resource-backed ones only because
   request-driven services more often have both their own RED surface *and* a USE surface for what
   they depend on — the checklist reflects that a service is rarely purely one kind of component.
2. Every anti-pattern in this doc was a real incident in doc 07, not a hypothetical warning — that is
   deliberate, because a checklist item with no story behind it is the first one a reviewer skips
   under time pressure.
3. The two most commonly skipped items are the connection/thread-pool USE signals on a
   request-driven service, and the oldest-unacked-record-age gauge on a consumer — both are cheap to
   add and both were the actual missing piece in this collection's most expensive case studies.
4. A checklist that lives only in a wiki page is advice. Encode the non-negotiable subset (a
   duration histogram exists, no label matches a denylist pattern, a paging alert is burn-rate-based)
   as a CI check or a linter rule, the same way `K8s/cronJobs` doc 10 turns its own defaults table
   into an admission policy — a rule enforced at merge time is the only version of this list that
   survives contact with a deadline.
