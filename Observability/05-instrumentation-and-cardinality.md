# Instrumenting RED and USE Without Bankrupting Your Metrics Backend

Docs 01 and 02 showed you the PromQL you want to run. This doc is about the decisions you make
*before* that query is possible — which metric type to emit, what buckets to put a histogram in,
and which labels to attach — because getting these wrong does not show up as a bug, it shows up
three months later as a Prometheus TSDB that falls over, or a five-figure line item on a hosted
metrics vendor's invoice. Both failures trace back to the same instinct: attaching a label because
it might be useful, without asking what it costs.

## Histograms vs. summaries, and why the choice matters at fleet scale

Both are exposed by every Prometheus client library as ways to measure a distribution — request
duration, in this case — but they compute fundamentally different things.

A **summary** computes quantiles (p50, p99, ...) *inside the process*, over a sliding window, and
exports those already-computed quantile values directly. This sounds convenient, and for a single
instance it is. The problem is fleet-wide aggregation: `checkout-api` runs 24 replicas at steady
state and up to 60 at peak. Each replica's summary computes its own p99 from only the requests it
personally handled. There is no mathematically valid way to average 24 independently-computed p99
values into "the fleet's p99" — a value that is the 99th percentile *of one replica's traffic* is
not the 99th percentile of the combined traffic, and averaging them produces a number that is not
any real percentile of anything.

A **histogram** instead exports raw counts per bucket — "how many requests took ≤5ms, how many
took ≤10ms, how many took ≤25ms," and so on, as separate counters. Because these are counts, not
pre-computed quantiles, they aggregate correctly: sum the ≤50ms counter across all 24 replicas,
sum the total-count counter across all 24 replicas, and you get a real, valid bucket count for the
whole fleet, from which `histogram_quantile()` computes a real fleet-wide percentile. The cost is
that the quantile you get is an approximation bounded by your bucket boundaries (more on this
below), rather than an exact value — a trade you accept gladly once you understand the
alternative silently produces a wrong number with no error to warn you.

For any component with more than one replica — which is every component in this collection —
**use a histogram, not a summary**, unless you have a specific reason to want a single process's
exact quantile (rare; mostly relevant to library-internal profiling, not service SLOs).

## Choosing bucket boundaries so the SLO threshold is actually measurable

A histogram's buckets are cumulative: a request of 40ms increments the ≤50ms bucket, the ≤100ms
bucket, and every bucket above it, but not the ≤25ms bucket below it. `histogram_quantile()`
interpolates *within* whichever bucket the target percentile falls into, assuming a roughly
uniform distribution of observations across that bucket's width. That assumption gets worse the
wider the bucket is, and it gets catastrophically bad at exactly the boundary your SLO cares
about if that boundary sits in the middle of a wide bucket rather than on an edge.

Doc 04 set `checkout-api`'s SLO threshold at 500ms. The running example's known latency shape is
p50 = 38ms, p99 = 310ms, p99.9 = 900ms. Pick buckets that are dense where the distribution's mass
actually is (near p50, so p50 itself is trustworthy) and, critically, place a bucket boundary
*exactly at 500ms*, so the SLI computation in doc 04 — "the fraction of requests in the ≤500ms
bucket" — reads a real counter value rather than an interpolated guess:

```
buckets (seconds): 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5
```

Walk why each region is shaped the way it is. Below 100ms, buckets are close together (5ms, 10ms,
25ms, 50ms, 100ms) because that is where the bulk of requests land (p50 = 38ms), and a coarse
bucket there would make even the median untrustworthy. Between 100ms and 1s, there is a bucket
boundary at exactly 250ms and exactly 500ms — 500ms is the SLO threshold from doc 04, so "good
events" (doc 04's numerator) is a direct counter read, not an interpolation. Above 1s, buckets
widen quickly (1s, 2.5s, 5s) because almost nothing lands there and you only need enough
resolution to distinguish "a bit slow" from "hung."

⚠️ If you had instead chosen widely-spaced round buckets — 0.1s, 0.5s, 1s, 5s — you would still
have a boundary at 500ms by coincidence in this specific case, but the region between 100ms and
500ms, where `checkout-api`'s actual p99 of 310ms lives, would have only one bucket covering a
400ms span. `histogram_quantile()` would linearly interpolate p99 somewhere inside a 100ms-to-500ms
bucket using an assumption of uniform density that is nowhere close to true near a distribution's
tail, and the p99 doc 04 reports could be off by tens of milliseconds in either direction. The
general rule: **a bucket boundary must exist at every value you intend to threshold or alert on**
(here, 500ms), and bucket density should roughly track where your own percentiles already are.

## Cardinality: the cost that does not show up until it is expensive

Every distinct combination of label values on a metric is a separate time series, stored and
indexed independently. This is easy to forget because adding "just one more label" feels free at
write time — the cost is multiplicative, and it lands on your TSDB's memory and your vendor's
per-series bill, not on the line of code that added the label.

Work the arithmetic for two real choices on `checkout-api`'s request counter.

**Choice A: label by `status_code` and `route`.** `checkout-api` exposes roughly 40 distinct
routes, and status codes collapse sensibly into 6 classes for dashboarding purposes (200, 201,
400, 404, 429, 500 — the specific small set your dashboards actually distinguish, even if more
exist on the wire). Total series for this one counter:

```
40 routes × 6 status classes = 240 series
```

240 series, for one metric, is nothing — a modern single-node Prometheus handles millions of
active series without strain, and even a metered SaaS vendor's bill for 240 series is
indistinguishable from zero.

**Choice B: also label by `user_id`, to make "which customers are affected" a free dashboard
query.** Riverbend has 2.3 million distinct registered users. Even if only a fraction are active
in checkout on any given day, the label's *cardinality* is defined by every distinct value that
ever appears, not by concurrent volume, because each new value creates a new series that persists
in the TSDB for its retention window. Multiplying onto Choice A's 240 series:

```
240 × 2,300,000 = 552,000,000 series
```

Five hundred and fifty-two million series, from one counter, on one service. That is enough to
exhaust a single Prometheus instance's memory outright (each active series costs roughly 1-3KB of
resident memory for its metadata and chunk buffer, so 552 million series is on the order of a
terabyte of RAM before you have stored a single additional metric), and on a per-series-billed
SaaS vendor it turns a metric that should cost nothing into a bill with six figures in it.

**The rule of thumb**, derived from this arithmetic rather than asserted: a label's value set must
be small and known in advance — status codes, route names, region names, environment names,
Kafka partition IDs. Anything whose value set grows with your user base, your request volume, or
your data (user IDs, request IDs, order IDs, raw error message strings, email addresses) does not
belong on a metric label, ever, regardless of how useful "filter by user" sounds at design time.

## Where the high-cardinality data actually belongs

The need this satisfies — "show me exactly what happened for this one user's failed checkout" —
is real, and metrics are the wrong tool for it, not because the need is illegitimate but because a
metric label is the wrong storage shape for a value with millions of possibilities. That need is
what logs and traces exist for: emit a structured log line per request with `user_id`, `order_id`,
and the full error detail, and let your log store's indexing (built for exactly this access
pattern) handle "find me the requests for user 4471902." The metric tells you *that* 0.4% of
checkout requests failed in the last five minutes; the log tells you *which* 0.4%, once the metric
has told you to go look.

**Exemplars** are the bridge between the two, and worth knowing about even though not every
backend supports them yet. An exemplar is a small piece of extra data — typically a trace ID —
attached to one specific observation inside a histogram bucket, sampled rather than recorded for
every request. When a bucket's count looks anomalous, an exemplar lets you jump directly from that
bucket to one real trace that landed in it, without ever having put a high-cardinality trace ID on
the metric series itself — the trace ID rides along with the *observation*, not the *series
identity*, so it costs nothing in cardinality terms. Prometheus has supported exemplar storage
since 2.26 (behind a feature flag, GA later), and OpenTelemetry's metrics SDK supports attaching
them natively; check your specific backend's support before designing a workflow around them, since
support is newer and less universal than histograms themselves.

## What to take away

1. Use histograms, not summaries, for any component with more than one replica — summaries cannot
   be aggregated correctly across instances, and every service in this collection runs more than
   one replica.
2. Bucket boundaries must include the exact value you intend to threshold or alert on (500ms for
   `checkout-api`'s SLO), or `histogram_quantile()` interpolates across that boundary and the
   number you report at exactly your SLO threshold is not trustworthy.
3. Cardinality cost is multiplicative across labels, not additive — adding a 2.3-million-value
   label to an otherwise-240-series metric produces 552 million series, not 2,300,240.
4. A label's value set must be small and known in advance. If the set of possible values grows
   with your users, requests, or data, it belongs in a log or a trace, not a metric label.
5. Exemplars let a histogram bucket carry a sampled trace ID without paying the cardinality cost of
   putting that ID on the series itself — check whether your backend and client library support
   them before assuming the workflow is available.
