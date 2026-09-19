# A Primer on Observability, RED, and USE

This doc builds the mental model everything else in this collection depends on. If you take one
idea away, take this one: **you cannot monitor everything, so the whole discipline is about
picking the small number of signals that would actually change what you do next.** RED and USE are
not two competing philosophies to choose between once — they are two different answers to "what
should I measure" for two different *kinds* of component, and almost every production system is
made of both kinds.

## Monitoring vs. observability, made concrete

Riverbend's `checkout-api` has a dashboard with 34 panels: CPU, memory, disk I/O, network, JVM heap
(it is a Java service), garbage collection pause time, thread pool size, open file descriptors,
and so on. Every panel is a real, correctly-graphed number. And yet, on the night `checkout-api`
had a 40-minute outage that cost Riverbend an estimated $310,000 in abandoned checkouts, on-call
did not find the cause on that dashboard. They found it in the access logs, by noticing that
`POST /checkout` had gone from 38ms to 4,100ms about six minutes before the first customer
complaint arrived.

That gap is the entire subject of this doc. **Monitoring** is watching known signals for known
failure modes: "alert if CPU exceeds 90%." It answers questions you thought to ask in advance.
**Observability** is having enough data, and enough ways to slice it, that you can answer a
question you *did not* think to ask in advance — "why did requests from mobile clients in the
`eu-west-1` region start failing at 14:32, and only for requests larger than 2KB?" You cannot get
there from a CPU graph. You get there from being able to break down request rate, error rate, and
duration by arbitrary dimensions after the fact.

This matters because it inverts a very common instinct. Most engineers, asked to instrument a new
service, start with the machine: CPU, memory, disk. Those numbers describe whether the *box* is
healthy. They say almost nothing about whether the *service* is healthy, because a healthy-looking
box can serve every request wrong, slow, or not at all — and a box under real pressure can still
be serving every request correctly, just later than you'd like. The fix is not to stop measuring
the machine. It is to measure the *service* first, because that is what your customer and your SLO
actually care about, and use machine-level metrics to explain *why* the service-level ones moved.

## Three pillars, one paragraph each, because later docs assume you know the words

- **Metrics** are numbers aggregated over time — a counter, a gauge, or a histogram, sampled or
  exported on an interval. They are cheap to store and query at high cardinality-per-series but
  expensive to store at high cardinality-of-labels (doc 05). They answer "how much" and "how many."
- **Logs** are discrete, timestamped events, usually with structured fields. They are expensive to
  store at volume but let you reconstruct exactly what one specific request did. They answer "what
  happened, for this one thing."
- **Traces** are a causally-linked set of spans showing how one request moved through multiple
  services, with timing for each hop. They answer "where did the time go, across service
  boundaries, for this one request."

This collection is about metrics — specifically, the two families of metric that give you the
best chance of noticing a problem before your customers, without drowning you in noise. Logs and
traces exist to answer the question a metric raises; they are not a substitute for having the
right metric raise it in the first place.

## Two different kinds of component need two different questions

Ask "is this thing healthy" about `checkout-api` and about `orders-db`, and you are really asking
two different questions, because the two components fail in different ways.

`checkout-api` exists to answer requests. Its job is fully described by three questions: how many
requests is it getting, how many of them fail, and how long do the successful ones take? That
triplet — **Rate, Errors, Duration** — is the **RED method**, and it applies to anything that
serves discrete units of work on demand: HTTP APIs, gRPC services, queue consumers processing one
message at a time, even a CronJob if you count firings as "requests." Doc 01 covers it in full.

`orders-db` does not "serve requests" in that sense from the outside — a client asks `checkout-api`
for something, and `checkout-api` asks the database, but the database itself is better described
as a **finite resource** with a capacity that can run out: connection slots, disk I/O bandwidth,
buffer cache, CPU. For a resource, the three questions that matter are: how much of its capacity is
in use, how much work is queued waiting for capacity that is not yet available, and is it throwing
errors? That triplet — **Utilization, Saturation, Errors** — is the **USE method**, and it applies
to anything with a hard ceiling: CPUs, memory, disks, network links, thread pools, connection
pools, and message queues. Doc 02 covers it in full.

The reason you need both, rather than picking one, is that a healthy answer to the RED questions
can hide a resource about to run out, and a healthy answer to the USE questions can hide a service
that is technically "up" but returning wrong answers. `checkout-api` can have a perfect RED
picture — low error rate, fine p99 — for the twenty minutes before `orders-db`'s connection pool
fills up and every fifth request starts queuing for a connection. And `orders-db` can show
comfortable CPU and disk utilization while `order-processor`'s consumer lag on `order-events`
climbs into the hundreds of thousands, because the bottleneck is consumer-side deserialization, not
the database at all. You need the request-side view and the resource-side view of the same
incident, from two different components, to see either problem coming. Doc 03 works through
exactly this scenario as a worked example.

## The naive alternative, and why it does not survive contact with a real incident

Before RED and USE were named as such, the default instinct — and still the default at a lot of
teams — was "alert on things going down, and put CPU and memory on a dashboard." Here is why that
degrades:

**Alerting on "down" misses the slow, partial, and wrong.** A service returning HTTP 200 with an
empty body, or 200 forty seconds late, is not down. Riverbend's checkout outage above never
tripped a liveness probe — every pod was `Running` and `Ready` the entire time. The database
connections were just all checked out, so requests queued inside the application until the load
balancer's own timeout fired. "Is it up" was never the right question; "how long is it taking, and
how much of that is queueing" was.

**CPU and memory dashboards answer "is the box busy," not "is the box about to fall over."** A CPU
graph sitting at 60% tells you nothing about whether the next 10% of load pushes you to 70% or to
100%-and-queueing, because the relationship between load and utilization is often nonlinear near
capacity. What you actually need is *saturation* — the queue of work waiting for that resource —
which utilization does not capture at all. Doc 02 derives this distinction in detail, because it is
the single most common gap in a USE implementation done half-right.

**A dashboard with 34 correct panels is not more informative than one with 6 — it is less, because
nobody can hold 34 panels' worth of "what does normal look like" in their head at 2 a.m.** Every
extra panel is either redundant with a better one, or so rarely load-bearing that it is
noise on the night it matters. Doc 06 is about cutting a metric surface down to the handful that
earn their place.

## What "good" looks like, stated as a target

By the end of this collection, for any component in your system you should be able to say:

1. Which of RED or USE (or both) applies to it, and why (doc 03).
2. The specific PromQL expression that computes each signal, and what it would show during a
   plausible failure (docs 01, 02).
3. What SLO, if any, is defined on top of those signals, and where the target number came from —
   not "we picked 99.9% because everyone does" (doc 04).
4. What alert exists for each signal that is actually worth paging on, and why the ones that
   aren't don't have one (doc 06).
5. Whether instrumenting it correctly is cheap or expensive, and specifically why — usually a
   cardinality question (doc 05).

That is a checklist, not a certification. Doc 09 turns it into one you can actually run against a
service before it ships.

## What to take away

1. Observability is about answering questions you did not anticipate; monitoring is about known
   failure modes. You need both, but observability is what saves you the night something new
   breaks.
2. Request-driven components (services, APIs, queue consumers) are described by **RED**: Rate,
   Errors, Duration. Finite resources (CPUs, disks, connection pools, queues) are described by
   **USE**: Utilization, Saturation, Errors.
3. A system is usually a mix of both kinds, and a full picture of an incident almost always needs
   the RED view of the service *and* the USE view of what it depends on — one rarely explains
   itself without the other.
4. Utilization is not saturation. A resource can look comfortably utilized while a queue behind it
   grows without bound; doc 02 is largely about that gap.
5. More panels is not more observability. The goal is the smallest set of signals that would
   change what you do next, and everything after this doc is about finding that set.
