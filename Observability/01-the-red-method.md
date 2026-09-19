# The RED Method: Rate, Errors, Duration

Doc 00 introduced RED as the answer to "is this request-driven component healthy." This doc makes
that precise enough to instrument: what exactly each of the three signals is, how to query it, and
the two traps — averaging and denominator choice — that make a RED dashboard *look* complete while
quietly hiding the failure you most need to see.

## Defining the three signals, precisely

**Rate** is the number of requests a component receives per unit time, broken down by outcome-
relevant dimensions — not a single number. "`checkout-api` is doing 640 requests per second" is a
fact, but it is not yet a *signal*, because it cannot distinguish "640 req/s, all healthy" from
"640 req/s, but the 40 req/s hitting `/checkout/apply-coupon` are all failing." The unit of measure
is always "count of requests started or completed in a window, divided by the window length," and
the dimensions worth slicing by are the ones that correspond to a different code path: HTTP route,
HTTP method, response status class, and — where it exists — a caller identity like API key or
client platform.

**Errors** is the subset of Rate whose outcome was a failure the component is responsible for.
"Responsible for" is doing real work in that sentence: a `400 Bad Request` because a client sent a
malformed payload is not `checkout-api` failing — the component did its job correctly by rejecting
bad input. A `502` because `orders-db` was unreachable, or a `500` because of an unhandled
exception, is `checkout-api` failing. Getting this boundary wrong in either direction breaks the
signal: count client errors as failures and your error rate is dominated by noise you cannot fix;
exclude a category of server-caused failure and your error rate under-reports real breakage. The
convention used in this collection, and a reasonable default anywhere: **5xx and any 2xx/3xx that
your own application logic marks as a logical failure count as Errors; 4xx does not**, unless a
specific 4xx (like a `429` your own rate limiter issued) represents your system doing something you
want visibility into.

**Duration** is how long a request took, from the component's point of view, expressed as a
*distribution*, not a single number — and the next section is entirely about why "not a single
number" is the load-bearing part of that sentence.

## Why "average latency" actively misleads you

Take a one-minute window of `checkout-api` traffic at its steady rate: at 640 req/s, that is
38,400 requests. Recall the running example's numbers: p50 38ms, p99 310ms, p99.9 900ms. Reconstruct
what those percentiles imply about the shape of that traffic, one segment at a time.

- 99% of requests — 38,016 of them — fall at or below the p99 boundary. Most of those cluster near
  the p50 of 38ms; call their average 35ms, since the bulk of the distribution sits below the
  median-to-p99 range and only a thin slice approaches 310ms.
- The next 0.9% — 346 requests — fall between the p99 and p99.9 boundaries. Call their average
  310ms, the boundary value itself, since that is the region defined by that percentile band.
- The remaining 0.1% — 38 requests — exceed the p99.9 boundary of 900ms. Call their average 900ms.

Compute the mean across all 38,400 requests:

```
(38,016 × 35ms) + (346 × 310ms) + (38 × 900ms)
= 1,330,560ms + 107,260ms + 34,200ms
= 1,472,020ms

1,472,020ms ÷ 38,400 requests ≈ 38.3ms average
```

The average is 38.3ms — indistinguishable from the p50 of 38ms. **A dashboard showing only average
latency would report this minute as "38ms, normal," while 38 real customers experienced 900ms or
worse.** At steady state, that 0.1% is not a rounding error: 38 requests per minute is 2,280 per
hour. If even a fraction of those are checkout attempts that time out client-side and get
abandoned, that is a measurable, daily revenue leak that an average-latency dashboard will never
surface, because averaging is a lossy compression that specifically destroys the information in the
tail. This is why doc 00's convention is a hard rule rather than a style preference: **a latency
number with no percentile attached should be treated as a bug in whatever produced it.**

The right representation is a histogram — a distribution of observed durations across buckets —
queried for specific percentiles, so you can ask "what is p99" and "what is p50" as two different
questions with two different answers, instead of collapsing them into one number that answers
neither.

## Querying Duration: percentiles from a histogram

Instrument `checkout-api` with a histogram metric, one observation per completed request:

```
http_request_duration_seconds_bucket{service="checkout-api", route="/checkout", method="POST", status="200", le="0.05"}
http_request_duration_seconds_bucket{service="checkout-api", route="/checkout", method="POST", status="200", le="0.1"}
http_request_duration_seconds_bucket{service="checkout-api", route="/checkout", method="POST", status="200", le="0.5"}
http_request_duration_seconds_bucket{service="checkout-api", route="/checkout", method="POST", status="200", le="1"}
http_request_duration_seconds_bucket{service="checkout-api", route="/checkout", method="POST", status="200", le="+Inf"}
```

Each `le` (less-than-or-equal) bucket is a cumulative count of requests at or below that duration.
Prometheus's `histogram_quantile` reconstructs a percentile by interpolating across those buckets:

```promql
histogram_quantile(
  0.99,
  sum(rate(http_request_duration_seconds_bucket{service="checkout-api", route="/checkout"}[5m])) by (le)
)
```

Read this from the inside out, because the order of operations here is where people get it wrong:

1. `rate(...[5m])` converts each bucket's raw cumulative counter into a per-second rate over the
   trailing 5 minutes — required because the underlying counters only ever increase, and you want
   "how many requests per second landed in this bucket recently," not "how many ever."
2. `sum(...) by (le)` aggregates across all `checkout-api` pod replicas, keeping the bucket
   boundaries (`le`) as the grouping key. ⚠️ If you aggregate without `by (le)`, you collapse the
   buckets into one number and `histogram_quantile` has nothing to interpolate across — this is the
   single most common broken-dashboard bug with histogram metrics.
3. `histogram_quantile(0.99, ...)` walks the resulting cumulative distribution and interpolates the
   value at which 99% of the mass has been accounted for.

Getting p50, p99, and p99.9 is the same expression three times with a different first argument —
which is exactly why a histogram, not three separately-tracked numbers, is the right instrument:
you decide which percentiles matter for the dashboard *after* the data is collected, not before.

⚠️ Bucket boundaries are chosen at instrumentation time and cannot be changed retroactively without
losing historical comparability — doc 05 covers choosing them well. If your buckets are `[0.1, 0.5,
1, 5]` and your real p99 is 310ms, every request between 100ms and 500ms lands in one bucket, and
`histogram_quantile` can only interpolate linearly within it — accurate enough for alerting, not
precise enough to distinguish a p99 of 200ms from a p99 of 450ms.

## Querying Rate and Errors, and the denominator trap

Rate, broken down by route and status class:

```promql
sum(rate(http_requests_total{service="checkout-api"}[5m])) by (route, status)
```

The naive error-rate query looks like this:

```promql
sum(rate(http_requests_total{service="checkout-api", status=~"5.."}[5m]))
  /
sum(rate(http_requests_total{service="checkout-api"}[5m]))
```

That is correct *if* `http_requests_total` is only incremented once a request has fully completed
with a final status. It quietly breaks in two common situations:

**Requests still in flight.** If your counter increments when a request *starts* rather than when
it finishes (some frameworks default to this), the denominator includes requests that have not yet
had a chance to fail, understating the error rate during a spike in duration — exactly the moment
you most need an accurate number. Instrument the completion counter, not the start counter, and if
you need in-flight visibility, track it as a separate gauge (`http_requests_in_flight`), not by
reusing the request-total counter.

**Client-side retries.** Suppose `checkout-api`'s own client library retries a request twice after
a `503` before giving up. If `http_requests_total` on the *server* side counts each attempt
separately — which it should, since each attempt really did hit the server — then three attempts
that resolve as "2 failures, 1 success" is a 66% error rate for that logical operation, but your
dashboard, counting at the request-attempt level, is answering a different, still-valid question:
"what fraction of attempts the server saw failed." Neither number is wrong; they answer different
questions, and the failure mode is presenting one while your reader assumes the other. State
explicitly, next to the panel, which one you are showing — "error rate per request attempt" versus
"error rate per logical operation, after retries" — because a reader who assumes the latter while
looking at the former will underestimate how often your system needs a retry to succeed at all,
which is itself a signal (retries are not free — doc 02 covers what they cost the resources behind
the retried call).

## A second worked example: catalog-search-api

`catalog-search-api` is a good second example specifically because it looks similar to
`checkout-api` on the surface — another HTTP service, another p50/p99 pair — but its Rate dimension
needs a different breakdown to be useful. At a steady 210 req/s with p50 22ms / p99 140ms, the
number that matters is not "total search QPS" but the split between query types, because they hit
different code paths with very different cost:

```promql
sum(rate(http_requests_total{service="catalog-search-api"}[5m])) by (query_type)
```

An exact SKU lookup (`query_type="exact"`) is an indexed point lookup; a free-text query
(`query_type="fuzzy"`) does tokenization and scoring across the index `catalog-reindex` rebuilt at
03:00. If `catalog-search-api`'s overall p99 climbs, "which `query_type` moved" is the first
question, and it is only answerable if Rate and Duration were both instrumented with `query_type`
as a label from the start — retrofitting a label onto a metric after an incident means you cannot
answer the same question about the incident you are currently having.

## Where RED breaks down

RED assumes a request: something arrives, a component acts on it, and the request resolves as a
success or a failure within a duration you can measure end to end. Two shapes of work in the
running example do not fit that assumption, and forcing RED onto them produces a signal that looks
complete but answers the wrong question.

**`order-processor` consuming `order-events`.** There is no client waiting for a response, so
"Duration" as request-response latency does not exist. You can still measure per-message processing
time (how long it takes to consume, deserialize, and write one record to `orders-db`), and that
number is useful — but it does not tell you whether the consumer is keeping up with the topic,
because a consumer can process each message quickly while still falling behind if messages arrive
faster than it processes them. The signal that actually answers "is `order-processor` keeping up" is
consumer lag: the gap between the latest offset produced to `order-events` and the offset
`order-processor` has committed. That is a queued-work signal, not a per-item duration signal — it
belongs to USE's Saturation, not to RED's Duration. Doc 02 covers it.

**Scheduled, non-continuous work — the CronJobs this collection's numbers feed into.**
`invoice-rollup` and `payout-settlement` do not have a Rate at all in any meaningful sense; they
fire once an hour or once a day. Doc 08 of `../K8s/cronJobs/` makes the analogous argument for this
shape of work directly: **the primary signal for something that runs occasionally is the staleness
of its last success, not a failure count**, because most of the ways scheduled work breaks — a
missed firing, a silent hang, a skipped window — produce no failure event for RED-style monitoring
to catch at all. If you instrument `invoice-rollup` with a naive "error rate" panel modeled on
`checkout-api`'s, you will get a flat 0% error rate through an incident where the job simply never
ran, because "never ran" and "ran and succeeded" are indistinguishable to a metric that only counts
outcomes of runs that happened.

The general rule this leaves you with: RED is the right method exactly when a client is waiting on
a bounded, per-item outcome. The moment work is asynchronous, queued, or scheduled rather than
requested, at least one of Rate, Errors, or Duration stops meaning what it means for a synchronous
service, and you need the resource-oriented view from doc 02, or the staleness-oriented view from
`../K8s/cronJobs/08-observability-and-alerting.md`, instead.

## What to take away

1. Rate, Errors, and Duration are each dimensioned signals, not single numbers — Rate needs a
   breakdown by route/status/caller, Duration needs to be a distribution, and Errors needs an
   explicit, stated boundary for what counts as this component's fault.
2. An average latency number hides the tail by construction. In the `checkout-api` example, a
   minute where 38 requests took 900ms averaged out to 38.3ms — indistinguishable from a
   completely healthy minute unless you query percentiles directly.
3. `histogram_quantile` requires `sum(...) by (le)` to preserve the bucket boundaries before
   interpolating a percentile — collapsing the buckets first is the most common way this query
   silently breaks.
4. Error rate has an implicit denominator choice — per-attempt versus per-logical-operation — and
   retries make the two genuinely different numbers. State which one a dashboard is showing.
5. RED assumes a bounded request-response with a client waiting on the outcome. It does not fit
   queue consumers (which need a saturation signal like consumer lag) or scheduled work (which
   needs a staleness signal, since a skipped or hung run produces no failure event at all).
