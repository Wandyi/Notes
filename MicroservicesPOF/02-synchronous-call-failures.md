# The Synchronous Call — Why "Slow" Is Worse Than "Down"

One service calls another over the network and waits for a response. That is the smallest unit of
a microservice architecture, and it contains more failure modes than any other single thing in
this collection. This doc takes that one hop apart.

The claim it argues, which is the most important idea in the collection after partial failure:
**a dependency that is down is an inconvenience; a dependency that is slow is an outage.** Not
your dependency's outage — *yours*. And not just for the callers of that dependency — for every
user of your service, including the ones whose requests never touch it.

That is counter-intuitive enough that the derivation comes first and the failure catalogue comes
after.

## Deriving why slow is worse than down

Start with Riverbend's `checkout-api`. From the running example: 640 requests/s steady, 200
worker threads, p50 38 ms, p99 310 ms. It calls `promotions-service`, among nine others.

### Case A: `promotions-service` is down

Down means the TCP connection is refused, or the request returns 503 immediately. Response time
for that call: roughly 1 ms — it takes about as long as a connection refusal takes to travel
back.

What happens to `checkout-api`? Its own handler catches the error, skips the promotion, and
returns an order with no discount applied. Request duration drops slightly, because one
downstream call now takes 1 ms instead of 30 ms. Throughput is unchanged. Nothing queues.

**Riverbend loses the ability to apply promotions and keeps selling.** That is a business
problem worth an incident, and it is not an outage.

### Case B: `promotions-service` is slow

Slow means requests still succeed, eventually — they now take 5 seconds instead of 30
milliseconds.

`checkout-api` has a 6-second client timeout on that call (a round number someone chose in 2021).
So every checkout request now holds a worker thread for 5 seconds instead of 300 milliseconds.

Apply **Little's law**, which is the one formula to memorise in this collection:

> **L = λ × W**
>
> The average number of items in a system (`L`) equals the arrival rate (`λ`) times the average
> time each item spends in the system (`W`).

Here, `L` is the number of worker threads occupied, `λ` is the request rate, and `W` is the
request duration.

Before:

```
L = 640 req/s × 0.310 s = 198 threads occupied
```

Against a pool of 200, that is already tight (which is its own finding), but it works.

After:

```
L = 640 req/s × 5.0 s = 3,200 threads needed
```

`checkout-api` has 200. So 200 requests are in flight and the other 3,000 are queued — in the
accept queue (`E-14`), in a framework queue, or refused. The service's effective throughput
collapses to:

```
200 threads / 5.0 s = 40 requests/s
```

**`checkout-api` went from 640 req/s to 40 req/s — a 94% drop — because a dependency got slow.**
And crucially: it dropped for *all* requests, including requests from customers who have no
eligible promotion and whose result would not have differed. The threads are the shared
resource, and a slow dependency consumes the shared resource whether or not its answer mattered.

### The comparison, stated plainly

| | Dependency **down** | Dependency **slow** |
|---|---|---|
| Time per affected call | ~1 ms | 5,000 ms |
| Threads held | ~0 | all of them |
| Effect on unrelated requests | None | Total |
| Your error rate | Unchanged (you handled it) | 100% timeout or queue-reject |
| Detected by a circuit breaker? | Yes, instantly — errors | **Often not** — these are successes |
| Detected by a health check? | Yes | No — the instance is healthy, just busy |

Every mechanism you have for handling failure is triggered by *errors*. A slow dependency
produces successes. That is why it gets through all of them.

Two design conclusions, and they organise everything below:

1. **A timeout is what converts "slow" into "down".** It is not an error-handling detail; it is
   the mechanism by which you cap the damage a slow dependency can do. A call with no timeout, or
   with a timeout far above the dependency's normal latency, has no cap.
2. **Isolating the shared resource is what stops the damage spreading** to unrelated requests.
   That is bulkheading, and it is doc 03.

## Choosing a timeout, properly

Most timeouts are round numbers chosen by whoever wrote the client first: 30 s, 10 s, 5 s, 1 s.
None of those numbers came from data. Here is how to derive one.

**Step 1: get the dependency's latency distribution**, not its average. From Riverbend's
`promotions-service`, measured over a week at steady state:

```
p50  = 24 ms
p90  = 41 ms
p99  = 88 ms
p999 = 210 ms
max  = 1,900 ms   (a handful of requests per day)
```

**Step 2: decide what fraction of legitimate requests you are willing to fail.** A timeout at p99
fails 1% of *healthy* requests. At p999 it fails 0.1%. If the call is retryable and idempotent,
failing 1% costs you a retry; if it is not, failing 1% is an error budget you may not have.

**Step 3: set the timeout at a multiple of p99 — typically 2× to 3×.** For `promotions-service`:

```
timeout = 3 × p99 = 3 × 88 ms ≈ 250 ms
```

Compare that to the 6 seconds actually configured — a factor of **24× too high**. Re-run the
Little's law calculation with 250 ms:

```
L = 640 req/s × (0.310 s baseline + 0.250 s worst-case promotions) ≈ 358 threads
```

Still above 200, so `checkout-api` would degrade — but to roughly 360/200 = 1.8× queueing, not
16× collapse. And with promotions declared a soft dependency and given its own bulkhead, the
degradation is contained entirely.

**Step 4: sanity-check the timeout against the caller's budget**, which is the next section, and
is where most timeout configurations fall apart.

### The timeout is not a budget

Here is the configuration Riverbend actually had, and it is completely typical:

```
Client (mobile app)      timeout: 30 s
  → CDN                  timeout: 30 s
    → gateway            timeout: 30 s
      → checkout-api     timeout: 10 s
        → promotions     timeout: 6 s
        → pricing        timeout: 6 s
        → inventory      timeout: 6 s
        → payment        timeout: 20 s
```

Every number was chosen locally and defensibly. Together they are incoherent:

- `checkout-api` has a 10-second limit from the gateway, but calls `payment` with a 20-second
  timeout. If payment takes 15 seconds, the gateway has already given up at 10 s and returned an
  error to the user — **while `checkout-api` is still waiting and payment is still charging the
  card.** The user sees a failure; the charge succeeds. That is a correctness failure produced
  entirely by timeout misconfiguration.
- The four downstream timeouts sum to 38 seconds against a 10-second allowance, so in a bad-but-
  not-catastrophic scenario `checkout-api` spends its entire budget on the first two calls and
  the gateway times out before it reaches payment at all.
- Nobody can answer "how long can this request take?" from the configuration, because the answer
  depends on which combination of dependencies is slow.

The fix is a **deadline**, not a timeout. A deadline is an absolute point in time, set once at the
edge, propagated on every hop, and decremented by elapsed time rather than reset.

```
Edge sets:  deadline = now + 3,000 ms
  gateway receives 2,985 ms remaining → passes it down
    checkout-api receives 2,960 ms remaining
      calls promotions with min(250 ms, remaining)     → 250 ms
      258 ms elapsed; 2,702 ms remaining
      calls pricing with min(300 ms, remaining)        → 300 ms
      ...
      calls payment with min(1,500 ms, remaining)      → whatever is left, capped at 1,500
```

Three properties fall out of this, and each is worth having on its own:

1. **Total request time is bounded by one number set in one place.** You can answer the question.
2. **A call is never started with insufficient time to complete.** If 40 ms remain and payment's
   p50 is 80 ms, do not make the call — fail immediately. This is one of the highest-value and
   least-implemented optimisations in distributed systems: it eliminates work that is guaranteed
   to be wasted, and during an incident that wasted work is what is keeping you saturated.
3. **The downstream can cancel.** gRPC propagates deadlines natively and a server can check
   `ctx.Done()`; a database driver can cancel the query. Work stops instead of continuing on
   behalf of a caller who left.

gRPC gives you deadline propagation for free (`context.WithTimeout` plus the `grpc-timeout`
header). HTTP does not have a standard for it — you have to carry it yourself in a header, and
every service has to honour it. Pick a header name (`X-Request-Deadline` as a Unix millisecond
timestamp, or `X-Timeout-Ms` as remaining milliseconds) and make honouring it a platform
requirement. A mesh can inject and enforce it, which is one of the better arguments for a mesh
(doc 21).

## Deriving the retry multiplier

Retries are the second half of the story, and the arithmetic is the part people skip.

A retry policy of "3 attempts" means: in the worst case, one logical request becomes three
network requests. That seems modest. Now stack the layers, each configured by a different team,
each reasonably:

```
Mobile app:   3 attempts
Gateway:      3 attempts
checkout-api: 3 attempts calling pricing
pricing:      3 attempts calling the pricing cache
```

One user action, worst case:

```
3 × 3 × 3 × 3 = 81 requests to the pricing cache
```

**An 81× amplifier**, and nobody configured an 81. Each team configured a 3.

This is not a theoretical worst case; it is the *normal* case during an incident, because the
condition that triggers retries — the dependency being slow or erroring — triggers them at every
layer simultaneously. Your traffic multiplies by 81× at exactly the moment your capacity is
lowest. That is `F-01` in doc 04, and it is the single most common mechanism by which a small
problem becomes a total outage.

### The generalised formula

With `n` layers each making `a` attempts:

```
amplification = a^n
```

Some numbers, so the shape is visible:

| Layers `n` | 2 attempts each | 3 attempts each | 4 attempts each |
|---|---|---|---|
| 1 | 2× | 3× | 4× |
| 2 | 4× | 9× | 16× |
| 3 | 8× | 27× | 64× |
| 4 | 16× | **81×** | 256× |
| 5 | 32× | 243× | 1,024× |

The lesson is not "do not retry". It is that **retries must be configured for the system, not for
the call**, because the exponent is a property of the architecture and no individual team can see
it.

### The three rules that bound it

**Rule 1: retry at exactly one layer.** Usually the outermost one that can make a meaningful
choice — typically the client or the edge, because only it can try a different region. Everything
in between passes failures through. If you retry in the middle, you must not retry at the edge,
and this has to be a written platform decision because the exponent is invisible locally.

If you cannot achieve single-layer retry (and in a large organisation you often cannot), then
budget the exponent explicitly: at most 2 attempts at the edge and 1 (no retry) everywhere else
gives 2×, which is affordable.

**Rule 2: use a retry budget, not a retry count.** A count says "each request may be tried 3
times." A budget says "**retries may not exceed 10% of total request volume.**" These behave
identically when things are healthy — few requests fail, so few are retried. They behave
completely differently during an incident:

- Count-based, at 100% failure: 640 req/s becomes 1,920 req/s. The struggling dependency gets
  3× load.
- Budget-based, at 100% failure: 640 req/s becomes 704 req/s. The struggling dependency gets
  1.1× load.

That is the entire difference between a dependency that recovers and one that does not. gRPC
supports this natively (`retryThrottling` with `maxTokens` and `tokenRatio`); Envoy has
`retry_budget`; Finagle pioneered it. If your client library does not have it, implement it as a
token bucket: each request adds one token, each retry consumes ten, retries are allowed only when
tokens are available.

**Rule 3: only retry what can succeed.** Classify every error:

| Error | Retry? | Why |
|---|---|---|
| Connection refused | **Yes** | Nothing was processed; another instance may be up |
| Connection reset before response | **Yes, if idempotent** | Unknown whether it was processed |
| Timeout | **Only if idempotent** | The request may have succeeded — this is partial failure |
| HTTP 502 / 503 | **Yes** | Infrastructure-level; the request likely did not reach the app |
| HTTP 504 | **Only if idempotent** | Same uncertainty as a timeout |
| HTTP 429 | **Yes, after `Retry-After`** | Explicitly tells you when |
| HTTP 500 | **No** | The app ran and failed. Retrying reproduces the failure and adds load |
| HTTP 400 / 422 | **No** | The request is wrong. It will be wrong next time |
| HTTP 401 / 403 | **No** (retry once after refreshing the token, then no) | Retrying an unauthorised request is pure waste |
| HTTP 404 | **No** | |
| gRPC `UNAVAILABLE` | **Yes** | Defined as retryable |
| gRPC `RESOURCE_EXHAUSTED` | **Yes, with backoff** | Backpressure signal; respect it |
| gRPC `DEADLINE_EXCEEDED` | **Only if idempotent, and only if budget remains** | |
| gRPC `INTERNAL` / `UNKNOWN` | **No** | |

The row that gets violated most is HTTP 500. A great many client libraries retry all 5xx, which
means that during an application-level bug — the exact time the service is least able to absorb
load — traffic triples.

### Jitter is not optional

Exponential backoff without randomisation does not spread load; it **synchronises** it. If 5,000
clients all fail at t=0 and all back off 1 s, 2 s, 4 s, then at t=1 exactly 5,000 requests
arrive simultaneously, then at t=3, then at t=7. You have converted continuous overload into
periodic spikes, which is worse, because the spikes are higher than the original load.

The correct form is **full jitter**:

```
delay = random_between(0, min(cap, base × 2^attempt))
```

Not `base × 2^attempt × random(0.5, 1.5)` ("equal jitter"), which still clusters. Full jitter
spreads the retries of a synchronised cohort uniformly across the whole window, which is what you
want. With `base = 100 ms`, `cap = 20 s`:

| Attempt | Window | Expected delay |
|---|---|---|
| 1 | 0–100 ms | 50 ms |
| 2 | 0–200 ms | 100 ms |
| 3 | 0–400 ms | 200 ms |
| 4 | 0–800 ms | 400 ms |
| 8 | 0–12.8 s | 6.4 s |
| 10+ | 0–20 s (capped) | 10 s |

## The failure catalogue

### R-01 · A call with no timeout

**What you see.** Threads or goroutines accumulate without bound. Memory grows. The service stops
responding while CPU is near zero. A thread dump shows hundreds of threads parked in a socket
read.

**Mechanism.** Many defaults are infinite, and this surprises people because it feels like it
should not be. Some real defaults: Java's `HttpURLConnection` has no connect or read timeout by
default; Go's `http.Client{}` zero value has no timeout at all (only `http.DefaultTransport`'s
dial timeout applies, and that does not bound the response); Python's `requests` has no timeout
unless you pass one; most JDBC drivers have no query timeout by default; most Redis clients have
a connect timeout but not a command timeout.

A call with no timeout does not fail when the dependency fails. It fails when the *operating
system* gives up, which for a TCP connection with no traffic is `tcp_keepalive_time` + probes ≈
**2 hours 11 minutes** on Linux defaults. In practice your service dies long before that.

**Confirm it.** Take a thread dump or goroutine dump and count threads blocked in socket reads
grouped by stack:

```bash
# Go: fetch the goroutine profile and group by the top frames
curl -s localhost:6060/debug/pprof/goroutine?debug=2 \
  | awk '/^goroutine /{n=$0} /net\/http.*RoundTrip|internal\/poll.*Read/{print n; }' \
  | wc -l

# Java
jstack <pid> | grep -A3 'java.net.SocketInputStream.socketRead' | grep 'at ' | sort | uniq -c | sort -rn
```

**Recover.** Restart the affected instances to release the threads, and — more importantly — cut
traffic to the hanging dependency at the mesh or client level so the restarted instances do not
immediately re-fill.

**Prevent.** Audit every outbound client for both timeouts, because there are two and setting one
is a common half-fix:

- **Connect timeout**: how long to establish a TCP connection. Should be short — 100–500 ms.
  Connecting is fast or it is not going to happen.
- **Request/read timeout**: how long to wait for the response. Derived from the dependency's p99
  as above.

A platform-level test is worth building: a synthetic dependency that accepts connections and
never responds, and a CI check that every client, pointed at it, fails within its configured
budget. This catches the "we set a timeout on the HTTP client but the connection pool's borrow
has its own unbounded wait" class of bug, which no code review catches.

### R-02 · Nested timeouts that exceed the caller's

**What you see.** The user gets an error, and the operation completes anyway. Double charges,
duplicate orders, "I got an error but it worked."

**Mechanism.** Covered in "the timeout is not a budget" above. The caller gives up before the
callee does. The callee, unaware, completes the work. Any side effect it produces happens after
the user was told it did not.

**Confirm it.** Compare, for a sample of requests over a day, the caller's recorded outcome
against the callee's. A query worth running on any payment path:

```sql
-- Orders the API reported as failed, where a charge exists anyway
SELECT o.order_id, o.api_status, p.charge_id, p.created_at
FROM order_attempts o
JOIN payment_charges p ON p.idempotency_key = o.idempotency_key
WHERE o.api_status IN ('TIMEOUT','ERROR')
  AND p.status = 'SUCCEEDED'
  AND o.created_at > now() - interval '7 days';
```

Any rows here are silent correctness failures your monitoring did not report. Most teams running
this query for the first time are surprised.

**Recover.** For the immediate incident: reconcile. For the pattern: you need the idempotency
key that makes the query above possible in the first place — see `T-05`.

**Prevent.** Deadline propagation with the invariant **every downstream timeout ≤ remaining
budget**, enforced in the client library rather than in configuration. Plus: for any operation
with an external side effect, the caller must be able to *ask* about the outcome later rather
than inferring it — a status endpoint keyed by idempotency key.

### R-03 · A timeout derived from a round number

**What you see.** Either far too many spurious failures (timeout below the real p99) or no
protection at all (timeout 20× above p99).

**Mechanism.** No mechanism — it is the absence of one. The number came from a template.

The under-set version is its own outage: a timeout at p95 means 5% of healthy requests fail, and
if those are retried you have added 5% load for nothing; if the dependency's latency then rises
slightly, the failure rate jumps non-linearly because you are on the steep part of the
distribution. Timeouts set too tight cause incidents during ordinary traffic growth.

**Confirm it.** For each client, compute `configured_timeout / observed_p99` for the dependency.
Anything below 1.5 is fragile; anything above 10 provides no protection.

```
# Per-dependency timeout sanity, as a PromQL-shaped expression
configured_timeout_seconds
  / histogram_quantile(0.99, sum by (le) (rate(rpc_duration_seconds_bucket[7d])))
```

**Prevent.** Derive timeouts from measured distributions, store them next to the code, and
re-derive them quarterly or when the dependency's p99 moves by more than 50%. A dependency whose
latency doubles has silently halved your safety margin, and nobody gets an alert for that.

### R-04 · Retrying a non-idempotent operation

**What you see.** Duplicates. Double charges, duplicate orders, doubled inventory decrements, two
emails.

**Mechanism.** The first attempt timed out. It may have succeeded. You retried. Now it has
happened twice. This is partial failure (doc 00) meeting a retry policy, and it is guaranteed to
occur eventually — the only question is at what rate.

What makes it insidious is that the retry is usually configured somewhere other than the code
that would have thought about it: a mesh-wide retry policy, a load balancer's `retry_on`
setting, an SDK default. A platform team enables retries fleet-wide to improve availability, and
a payments team starts double-charging.

**Confirm it.** As in `R-02`, join the caller's attempts to the callee's effects by idempotency
key — and if there is no idempotency key, that is the finding.

**Prevent.**

- **Retries must be opt-in per route, never fleet-wide by default.** If the platform enables
  retries globally, mutating endpoints must be explicitly excluded, and the exclusion must be
  the default for `POST` and `PATCH` unless the route declares itself idempotent.
- **Make the operation idempotent** so the question stops mattering: a client-generated
  idempotency key, stored with the result, returning the stored result on replay. `T-05` covers
  doing this correctly, including the parts people get wrong (key scope, concurrent replay, and
  what to do when the first attempt is still in flight).
- **Use `PUT`/`DELETE` semantics where the domain allows.** "Set the quantity to 3" is safe to
  retry; "add one" is not.

### R-05 · Retry amplification across layers

**What you see.** During an incident, the offered load on the failing dependency is 10×–80× its
normal level, and it stays there. Reducing user traffic by half barely helps.

**Mechanism.** The `a^n` arithmetic above.

**Confirm it.** This is measurable and most teams have never measured it. Instrument an attempt
counter on the client and compare *logical* requests to *physical* attempts:

```
# Amplification factor, per dependency, right now
sum(rate(rpc_attempts_total{target="pricing"}[1m]))
  / sum(rate(rpc_requests_total{target="pricing"}[1m]))
```

A value of 1.0 means no retries are happening. During an incident, watch this number; if it goes
above 2, retries are a material part of your load, and above 5 they are your load.

**Recover.** Turn retries off. This is counter-intuitive during an incident — the instinct is to
retry harder — and it is frequently the single action that ends the outage. Most meshes and
clients allow retry policy to be changed without a deploy; make sure yours does, because this is
an emergency lever you want available.

**Prevent.** Single-layer retry, retry budgets, and — the structural fix — **make amplification
visible in design review** by drawing the call graph with attempt counts on the edges and
multiplying them out. Teams that see "81×" written on a diagram fix it; teams that see "3
attempts" in four different config files do not.

### R-06 · Retries without jitter

**What you see.** Load arriving in sharp periodic spikes during an incident — a sawtooth on the
graph with a period matching your backoff schedule. The dependency recovers between spikes and
is knocked over by each one.

**Mechanism.** Synchronised cohorts, as derived above.

**Confirm it.** Look at the request-rate graph at a 1-second resolution during the incident. If
you can read the backoff schedule off the graph, there is no jitter. (At 10-second or 1-minute
resolution this is invisible, which is why it is often missed — most dashboards average it away.)

**Prevent.** Full jitter, and check the library's actual behaviour rather than trusting the
option name. Several popular retry libraries call a ±10% randomisation "jitter", which does not
decorrelate anything.

### R-07 · Retrying errors that cannot succeed

**What you see.** A bug causes 100% of requests to a route to return 500. Traffic to the service
triples. The service, already broken, is now also overloaded, which breaks the routes that were
working.

**Mechanism.** Retry-on-all-5xx. The error is deterministic; the retry reproduces it exactly.

**Prevent.** The classification table above, implemented in one shared client library rather than
in each service. Also make the server's signal honest: if a request cannot succeed, return a 4xx
so that a correct client will not retry it. Servers that return 500 for validation failures cause
their callers to retry validation failures forever.

### R-08 · No retry budget

**What you see.** A dependency at 50% error rate receives 2× its normal traffic, which pushes it
to 80% error rate, which — with count-based retries — receives 2.8× its normal traffic. It never
gets out.

**Mechanism.** Derived above: count-based retries scale retry load *with the failure rate*, which
is exactly backwards. The worse it gets, the harder you push.

**Prevent.** A token-bucket retry budget capped at 10% of request volume, per dependency, shared
across all callers in a process. When the budget is exhausted, failures are returned immediately.
Alert on budget exhaustion — it is a precise signal that a dependency is in trouble and that you
have stopped making it worse, which is exactly what you want to know.

### R-09 · Connection pool exhaustion

**What you see.** Requests fail or block with "unable to obtain connection from pool" /
"connection pool timeout", while the target database or service is barely loaded. Adding
application instances makes it worse.

**Mechanism.** Little's law again, applied to connections instead of threads. The pool is a queue
with `P` servers; its capacity in requests per second is:

```
capacity = P / W      where W is the time a connection is held
```

Riverbend's `order-processor` holds a pool of 20 connections to `orders-db`, and each unit of
work holds a connection for 15 ms:

```
capacity = 20 / 0.015 = 1,333 requests/s
```

Fine at its normal 67 orders/s. Now the database's p99 rises from 15 ms to 200 ms because an
index is missing after a migration:

```
capacity = 20 / 0.200 = 100 requests/s
```

Capacity fell 13×. Anything above 100/s queues on the pool, and the pool's borrow timeout — often
30 seconds and often not set at all — determines whether that queue is bounded.

The trap that catches teams: **adding instances makes it worse.** 12 pods × 20 connections = 240
connections. Scale to 40 pods to "handle the load" and you have 800 connections against
`orders-db`'s `max_connections` of 600. The database starts refusing connections, which looks
like a database failure and is a client-side arithmetic failure.

**Confirm it.** Three numbers, and you need all three:

```
# Pool utilisation: in-use / total
hikaricp_connections_active / hikaricp_connections_max

# Time spent waiting to borrow — the signal that matters
histogram_quantile(0.99, rate(hikaricp_connections_acquire_seconds_bucket[5m]))

# Total connections at the server, versus its limit
SELECT count(*), setting::int AS max
FROM pg_stat_activity, pg_settings WHERE name='max_connections' GROUP BY setting;
```

Acquire time rising while query time is flat means the pool is the bottleneck. Both rising means
the database is, and the pool is transmitting it.

**Recover.** Reduce hold time, not pool size — a larger pool against a slow database just moves
the queue into the database, where it is worse because the database has no admission control.
Kill long-running queries; if there is one slow query shape causing it, block that route.

**Prevent.** Size the pool from the arithmetic, not from a template:

```
P = required_throughput × hold_time × safety_factor
  = 67 req/s × 0.015 s × 3
  = 3.0  →  round up to 5, not 20
```

Small pools are usually correct and this surprises people. A pool larger than the database can
usefully serve concurrently does not add throughput; it adds queueing inside the database, where
you cannot see it and cannot shed it. As a bound: total connections across all clients should be
well under the database's `max_connections`, with the practical concurrency limit being roughly
`2 × cores + effective_spindle_count` for a traditional relational database — for `orders-db`'s
16 vCPUs, useful concurrency is on the order of 30–40, not 600.

Above that, put a **connection proxy** in front (PgBouncer in transaction mode, ProxySQL, RDS
Proxy) so that 800 client connections multiplex onto 40 server connections. This is essentially
mandatory past a few dozen application instances.

And set a **borrow timeout** — 1–2 seconds, not 30 — so that pool exhaustion fails fast instead of
becoming `R-01` by another route.

### R-10 · One pool shared across all dependencies

**What you see.** A slow, unimportant dependency makes calls to a fast, critical dependency fail.

**Mechanism.** If `checkout-api` uses one HTTP client with one connection pool of 200 for all
nine downstream services, then when `promotions` gets slow it occupies the pool, and calls to
`payment` cannot get a connection. The pool is the shared resource, and it has no notion of
importance.

**Prevent.** One pool per dependency, sized independently. This is bulkheading at the connection
layer and it is the cheapest bulkhead available — see doc 03 (`P-05`) for the full treatment,
including the version at the thread level, which matters more.

### R-11 · HTTP/2 and gRPC defeat your load balancer

**What you see.** Traffic is wildly imbalanced across backend instances — some at 90% CPU, some at
5%. Adding instances does not help, because new instances receive no traffic. Scaling events make
the imbalance worse, not better.

**Mechanism.** This one catches almost everybody who moves from HTTP/1.1 to gRPC.

HTTP/1.1 opens a connection per concurrent request. An L4 load balancer distributing
*connections* therefore distributes *requests* — balancing works by accident.

HTTP/2 and gRPC multiplex many requests over **one long-lived connection**. An L4 balancer sees
one connection and pins it to one backend forever. If a client opens one connection to a service
with 30 instances, 100% of that client's traffic goes to one instance. The balancer is working
exactly as designed and is balancing the wrong unit.

It gets worse on scale-out. Existing connections stay where they are, so a new instance gets
traffic only from *new* connections — of which there are approximately none, because everything
is long-lived. You scale from 30 to 60 instances and the original 30 stay at 90% CPU.

**Confirm it.** Requests-per-second per backend instance, as a distribution. A healthy L7-balanced
service has a tight distribution; a connection-pinned one has an extremely wide one. Also check
connection count per instance — if it is roughly `clients / instances` and not changing, you are
pinned.

**Prevent.** Pick one:

1. **Balance at L7.** An Envoy/nginx/HAProxy in HTTP/2 mode distributes individual *streams*, not
   connections. This is what a service mesh gives you by default and is the main reason gRPC
   shops adopt meshes.
2. **Client-side load balancing.** The gRPC client resolves all backend addresses and maintains a
   subchannel to each, round-robining requests across them. Requires the client to discover
   endpoints (`D-01`) and to handle churn.
3. **Force connection turnover.** Set `MAX_CONNECTION_AGE` on the server (gRPC's
   `grpc.KeepaliveParams{MaxConnectionAge: 30 * time.Minute, MaxConnectionAgeGrace: 5 * time.Minute}`)
   so connections are periodically retired and rebalanced. Add jitter — gRPC-Go adds ±10%
   automatically, which is enough here because the population is small. This is the minimum
   viable fix and it should be on **every** gRPC server regardless of which other option you
   choose, because it is also what lets new instances ever receive traffic.

### R-12 · Head-of-line blocking

**What you see.** p99 latency far above p50 with no corresponding resource saturation, and the
slow requests are not the slow *endpoints* — ordinary fast calls are occasionally very slow.

**Mechanism.** Two layers of it.

*At the HTTP/2 layer*: streams on one connection share a flow-control window and a maximum
concurrent stream limit (`SETTINGS_MAX_CONCURRENT_STREAMS`, commonly 100–250). If 100 slow
requests occupy all the streams on a connection, the 101st waits — even though the server has
idle capacity, and even though the 101st request would take 2 ms.

*At the TCP layer*: HTTP/2's multiplexing is over a single TCP connection, so a lost packet stalls
**every** stream on that connection until it is retransmitted. HTTP/1.1 with six connections
loses one sixth of its throughput to the same packet loss; HTTP/2 loses all of it. On a lossy
mobile network this makes HTTP/2 measurably worse than HTTP/1.1, which is the problem QUIC/HTTP/3
exists to solve (it multiplexes over UDP with per-stream loss recovery).

**Confirm it.** For the stream-limit version: track concurrent streams per connection against the
limit. For the TCP version: correlate p99 with `netstat -s | grep -i retrans` on the path.

**Prevent.** Maintain a small pool of HTTP/2 connections (4–8) rather than one, so a stalled
connection does not stall everything; raise `MAX_CONCURRENT_STREAMS` if the server can genuinely
handle it (but note that raising it also removes a backpressure signal — see `P-09`); separate
long-running or streaming calls onto their own connections; and use HTTP/3 for mobile clients
where loss is common.

### R-13 · Thread exhaustion, and the async trap

**What you see.** Throughput collapses while CPU is 10%. Or, after "fixing it with async",
memory grows until the process is OOMKilled.

**Mechanism.** The thread-per-request model binds concurrency to threads, and threads are
expensive (about 1 MB of stack each, plus scheduler cost), so pools are small — hundreds, not
tens of thousands. Little's law then caps throughput at `threads / latency`, as derived at the
top of this doc.

The apparent fix is asynchronous I/O: goroutines, async/await, reactive streams. Concurrency is
no longer bound by threads, so a slow dependency no longer collapses throughput. **And that is
the trap.** The queue did not go away; it moved from a bounded resource (the thread pool) to an
unbounded one (the heap). Instead of 200 threads blocked and requests rejected, you now have
50,000 in-flight requests each holding a context object, a buffer, and a partially-parsed body.

The synchronous version fails fast and stays up. The asynchronous version accepts everything and
dies. **Unbounded concurrency is not higher capacity; it is the absence of backpressure.**

**Confirm it.** For the sync version: threads busy / threads total, plus queue depth. For the
async version: in-flight request count, heap growth rate, and the age of the oldest in-flight
request. If in-flight count has no ceiling in your code, you have this problem and have not hit
it yet.

**Prevent.** Async is the right model — but it must have an **explicit concurrency limit**, chosen
the same way a thread pool size would have been:

```
max_in_flight = target_throughput × acceptable_latency
              = 640 req/s × 0.5 s
              = 320
```

Beyond 320 in flight, reject with 503 immediately (`P-08`). You have kept async's efficiency and
restored the thread pool's most valuable property, which was that it said no.

Better still, make the limit adaptive — Netflix's concurrency-limits approach uses a TCP-Vegas-
style algorithm to infer the right limit from observed latency, so it tracks the dependency's
real capacity instead of a number set last year.

### R-14 · The slow dependency that never errors

**What you see.** Nothing in your error-handling machinery activates. The circuit breaker is
closed. Retries are not firing. Health checks pass. And throughput is 6% of normal.

**Mechanism.** The dependency returns HTTP 200 in 5 seconds. Every mechanism you built is
triggered by errors, and there are none. This is the case the whole doc opened with, and it is
worth its own catalogue entry because the *response* is different: nothing automatic will help
you, so the design has to have anticipated it.

**Confirm it.** Latency, per dependency, as a distribution, tracked on the *client* side. Client-
side is important: the server reports its handler duration, which excludes queueing (`E-14`), so
the server can honestly report 40 ms while the client sees 5 s.

**Prevent.** Three mechanisms, all needed:

1. **Timeouts derived from p99** — so slow becomes an error, and the rest of your machinery can
   act on it.
2. **A latency-triggered circuit breaker**, not only an error-triggered one. Most breaker
   libraries count failures; configure them so that a timeout counts as a failure (many do not by
   default) and, if available, so that a p99 threshold trips the breaker directly.
3. **A bulkhead**, so that even at the timeout value, the slow dependency cannot consume more
   than its allotted share of your concurrency (doc 03, `P-05`).

### R-15 · Hedging that doubles load at the worst moment

**What you see.** A tail-latency optimisation works beautifully for months, then turns a minor
latency regression into a full outage.

**Mechanism.** Hedging (also called a backup request) sends a second request if the first has not
responded by some threshold — typically p95 or p99. In steady state that adds 1–5% extra load and
cuts p99 substantially, which is a very good trade: Northlight uses it exactly this way.

But the threshold is relative to a *distribution that moves*. If the dependency's latency rises
so that 100% of requests exceed the p99 threshold, then 100% of requests get hedged, and offered
load **doubles** — at the moment the dependency is already struggling. Hedging is an amplifier
whose gain is the fraction of requests over the threshold, and that fraction goes to 1 exactly
when you cannot afford it.

**Prevent.** Hedge under a budget, exactly like retries: hedged requests may not exceed 5% of
traffic. When the budget is exhausted, stop hedging. gRPC's hedging policy supports this
(`hedgingPolicy` combined with `retryThrottling`). Also cancel the loser as soon as one response
arrives, and make sure the operation is idempotent — a hedged request is a retry that overlaps
in time, so everything in `R-04` applies and the concurrency makes it harder.

### R-16 · Circular dependencies and distributed deadlock

**What you see.** Two services are each waiting on the other. Both have full thread pools. Neither
can make progress. Restarting one fixes it for a while.

**Mechanism.** `service-a` calls `service-b` for enrichment; `service-b` calls `service-a` for
authorisation. Under normal load, plenty of threads, no problem. Under load, `service-a` uses all
200 of its threads calling `service-b`; `service-b` uses all 200 of its threads calling
`service-a`; both are waiting on a peer that has no threads free to answer. The system is in
deadlock and no individual service is at fault.

The same shape appears with a shared dependency: `A → B → C` and `A → C`, where `C`'s pool is
consumed by the `A → B → C` path so the direct `A → C` path starves.

**Confirm it.** Build the call graph from trace data (not from documentation, which will not show
the cycle) and look for cycles:

```
# From a trace store, find spans whose service appears twice in the same trace's ancestry
SELECT trace_id, string_agg(service_name, ' → ' ORDER BY start_time) AS chain
FROM spans
WHERE trace_id IN (
  SELECT trace_id FROM spans GROUP BY trace_id, service_name HAVING count(*) > 1
)
GROUP BY trace_id LIMIT 50;
```

**Recover.** Break the cycle by shedding on one side. Restarting one service frees its threads and
lets the other drain — which is why "restart it and it comes back" is the common experience, and
why it recurs.

**Prevent.**

- **Detect cycles in CI** from the service dependency graph, and fail the build on a new one.
  This is cheap and almost nobody does it.
- **Break the cycle architecturally**: cache the authorisation decision in `service-a` so the
  back-call is not needed, or move the shared data into a store both read, or invert one
  direction to an event.
- **Deadlines everywhere** make deadlock self-limiting: it becomes a latency spike that clears
  rather than a permanent wedge.
- **Separate pools per direction**, so inbound request handling never competes with outbound
  calls for the same threads.

## What to take away

1. **A dependency that is slow is far more dangerous than one that is down.** Down costs you one
   feature; slow costs you all of your concurrency and therefore all of your requests, including
   those that never touch the dependency. Riverbend's worked example: 640 req/s to 40 req/s.
2. **Little's law, `L = λ × W`, is the formula to memorise.** It sizes thread pools, connection
   pools, and concurrency limits, and it explains why a latency change is a capacity change.
3. **A timeout is the mechanism that converts "slow" into "down"** so the rest of your failure
   handling can act. Derive it as 2–3× the dependency's measured p99, not from a round number,
   and re-derive it when that p99 moves.
4. **Timeouts do not compose; deadlines do.** Set one absolute deadline at the edge, propagate the
   remaining budget on every hop, never start a call that cannot finish in the time left, and
   never let a downstream timeout exceed the caller's remaining budget — that is how a user gets
   an error for a charge that succeeded.
5. **Retry amplification is `a^n`.** Four layers at three attempts is 81×, and it arrives exactly
   when capacity is lowest. Measure `attempts / requests` per dependency; during an incident,
   watch it.
6. **Retry at one layer, under a budget (≤10% of traffic), with full jitter, only on errors that
   can succeed.** A budget keeps retry load flat as failure rate rises; a count multiplies it.
7. **Turning retries off is a legitimate and frequently correct incident action.** Make sure it
   is a lever you can pull without a deploy.
8. **Retries must be opt-in per route.** A fleet-wide retry default turns every non-idempotent
   endpoint into a duplicate generator.
9. **Size connection pools from `throughput × hold_time`, not from a template.** Small is usually
   right. A pool bigger than the server's useful concurrency moves the queue somewhere you cannot
   see it, and scaling out multiplies your connection count into the server's limit.
10. **HTTP/2 and gRPC break L4 load balancing** because one connection carries everything. Use L7
    balancing or client-side balancing, and set `MAX_CONNECTION_AGE` on every gRPC server
    regardless.
11. **Async does not remove the queue; it moves it from a bounded resource to the heap.** Always
    set an explicit in-flight limit — ideally adaptive — or you have removed backpressure rather
    than added capacity.
12. **Hedging is an amplifier whose gain goes to 2× exactly when you cannot afford it.** Budget it
    like a retry.
13. **Cycles in the call graph become distributed deadlock under load.** Detect them in CI from
    trace data; documentation will not show them.

Next: [03-resilience-patterns-and-their-own-failures.md](03-resilience-patterns-and-their-own-failures.md),
which takes the mechanisms this doc has been recommending — breakers, bulkheads, shedding — and
shows how each one becomes the point of failure it was installed to prevent.
