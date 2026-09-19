# Choosing Between RED and USE, and Reconciling Both With the Four Golden Signals

Docs 01 and 02 gave you two complete methods. This doc answers the question that matters once you
actually have a component in front of you: which one does *this* thing need? The short answer is
that most interesting components need both, and the reason incident diagnosis is often slow is not
missing metrics — it is having only one of the two views and trying to explain an incident that
only makes sense with both.

## A test you can run against any component

Ask two yes/no questions about the component in front of you.

**Does it receive discrete units of work from a caller who is waiting on a pass/fail outcome?** If
yes, it needs **RED**. `checkout-api` receiving an HTTP request, `catalog-search-api` receiving a
query, `order-processor` receiving one Kafka message to process — all of these have a caller
(human, upstream service, or the topic itself) waiting for that specific unit of work to resolve.

**Does it have a hard ceiling on some resource that, once reached, causes work to queue or fail
regardless of how correct the code is?** If yes, it needs **USE**. `orders-db`'s 600 connections,
`session-cache`'s memory, `order-events`'s partition throughput, and `checkout-api`'s own CPU limit
and thread pool are all ceilings — capacities that exist independent of whether the code handling
each unit of work is bug-free.

The reason most real components answer yes to both is that "serves requests" and "has a resource
ceiling" are not mutually exclusive properties — they are two properties of the same object.
`checkout-api` answers yes to the first question because it is, from its callers' point of view, an
HTTP service. It also answers yes to the second, because internally it runs a bounded thread pool
and a bounded connection pool to `orders-db`, either of which can saturate independent of the
request logic being correct. **Needing RED does not exempt a component from also needing USE for
its own internals** — it only tells you that RED is not the whole picture. `checkout-api` should be
instrumented with RED for the requests it receives, and with USE for the internal resources
(thread pool, its own connection pool, memory) that determine whether it can keep serving those
requests under load.

The only components that answer yes to just one question are the pure cases: `order-events`
itself (the Kafka topic) is a pure resource with no logic of its own to fail — USE only, no RED,
since a topic does not "handle" a request. A stateless pass-through proxy with no internal buffering
and no meaningful resource ceiling of its own would be closer to RED only — rare in practice, but
conceptually clean.

## Reconciling with Google's four golden signals

You will also encounter Google's SRE book's **four golden signals**: Latency, Traffic, Errors, and
Saturation. It is tempting to treat this as a third, separate framework you now have to reconcile
with RED and USE — it is not. Lay the four terms next to RED and USE's six and the overlap is
almost total:

| Golden signal | Equivalent | Source method |
|---|---|---|
| Latency | Duration | RED |
| Traffic | Rate | RED |
| Errors | Errors | RED (and, separately, USE's own Errors for the resource) |
| Saturation | Saturation | USE |

**The four golden signals are best understood as "RED, plus Saturation borrowed from USE" — not a
third method.** The reason Google's list includes Saturation at all, despite being framed around
services rather than resources, is exactly the point doc 02 makes: a service's own Latency and
Errors can look fine while a resource behind it — a thread pool, a connection pool, a downstream
database — is saturating and about to make Latency and Errors look very not-fine a few minutes
later. Google's list is a shorthand for "watch the service, and watch the one resource-level signal
most likely to predict the service's next incident." It does not replace USE's Utilization or
Errors-for-the-resource; it just picks Saturation as the single highest-value USE signal to keep on
a service-level dashboard, because it is usually the leading indicator, while Utilization and
resource-level Errors are more useful once you already know which resource to dig into.

Practically: if someone hands you a "four golden signals" dashboard requirement, you now know
exactly how to satisfy it without inventing a fourth framework — instrument RED per doc 01, add the
one or two Saturation signals from doc 02 that correspond to the resources this component depends
on most directly, and you have satisfied both frameworks with one dashboard.

## Worked example: the outage from doc 00, seen through both views

Doc 00 mentioned a 40-minute `checkout-api` outage, costing an estimated $310,000 in abandoned
checkouts, where `POST /checkout` latency went from 38ms to 4,100ms. Here is that incident in full,
with a cause, and with both signal sets laid side by side to show what each one contributes.

**The trigger.** At 14:26, a flash-sale push notification goes out to Riverbend's mobile app.
`checkout-api` traffic begins climbing from its steady 640 req/s toward the collection's stated peak
of 3,400 req/s. The autoscaler adds `checkout-api` pods in response — correctly, this part of the
system is working as designed — but every new pod opens its own allocation against `orders-db`'s
shared pool of 600 connections.

**The RED view, from `checkout-api`:**

| Time | Rate (req/s) | p99 duration | Error rate |
|---|---|---|---|
| 14:26 | 680 | 320ms | 0.05% |
| 14:29 | 1,450 | 340ms | 0.06% |
| 14:32 | 2,600 | 1,900ms | 3.1% |
| 14:35 | 3,100 | 4,100ms | 12.4% |
| 14:38 | 2,950 | 4,050ms | 14.9% |

Reading this table alone: Rate climbed, which you would expect during a flash sale and is not
itself alarming. Duration and Errors both broke sharply at 14:32, three minutes after traffic
started climbing and about six minutes before the numbers above would have generated a customer
complaint (matching doc 00's account). **This view tells you precisely when the service broke
and by how much, but not why** — a 34x latency increase and a jump to double-digit error rates is
consistent with several different root causes: a code regression, a downstream network partition, a
garbage-collection pause, or a resource exhausted somewhere in the request path.

**The USE view, from `orders-db`, over the same window:**

| Time | Connections in use / 600 | Sessions with `wait_event` set |
|---|---|---|
| 14:26 | 184 | 1 |
| 14:29 | 410 | 3 |
| 14:32 | 600 | 38 |
| 14:35 | 600 | 61 |
| 14:38 | 600 | 57 |

Connections hit the ceiling of 600 at the same 14:32 timestamp where `checkout-api`'s p99 broke, and
stayed pinned there for the rest of the incident. The wait-event count — Scenario A's saturation
signal from doc 02 — climbed in lockstep. **This view tells you exactly which resource ran out, at
exactly the moment it ran out**, but on its own it does not tell you which upstream service was
affected, or by how much, without separately checking every consumer of `orders-db`
(`checkout-api`, `order-processor`, and indirectly `invoice-rollup`/`payout-settlement`).

**Put together, at 14:32, the two views make root cause identifiable in well under two minutes:**
`checkout-api`'s Duration and Errors broke at the same timestamp `orders-db`'s connection count hit
its ceiling, and there is a direct mechanism connecting the two — every `checkout-api` request needs
an `orders-db` connection, and there were none left, so requests queued inside the application until
either a connection freed up or the client gave up. An on-call engineer who has both dashboards open
side by side, with time axes aligned, reads this correlation directly off the screen. An on-call
engineer with only the RED dashboard has to *hypothesize* a resource exhaustion and go looking for
confirmation, one candidate resource at a time — which is a defensible description of why the real
version of this incident ran 40 minutes rather than 2: the connection-pool metrics existed, but were
on a separate dashboard nobody thought to open until well after code-level causes had been ruled out
first.

The fix that ends this specific incident — a connection pool sized correctly for the autoscaled pod
count, or a `pgbouncer`-style external pooler in front of `orders-db` so replica count and
connection count are decoupled — is an infrastructure change, not a monitoring one. But no one
proposes that fix without first seeing the correlation above, and the correlation is only visible if
both signal sets are instrumented and displayed together, which is the practical argument for
treating "does this component need RED, USE, or both" as a question you answer explicitly for every
component, rather than by whichever framework the team happened to reach for first.

## What to take away

1. Ask two questions per component: does it serve discrete requests with a caller waiting on the
   outcome (RED), and does it have a hard capacity ceiling (USE). Most non-trivial components
   answer yes to both, including the request-driven ones — `checkout-api` needs RED for its
   requests and USE for its own thread pool and connection pool.
2. Google's four golden signals are not a third framework — Latency, Traffic, and Errors map
   directly onto RED's Duration, Rate, and Errors, and Saturation is USE's single highest-value
   leading indicator, chosen because it tends to predict a RED-visible break before that break
   happens.
3. In the worked incident, `checkout-api`'s RED view showed exactly when and how badly the service
   broke; `orders-db`'s USE view showed exactly which resource ran out and when. Neither view
   alone identifies root cause as fast as the two together, correlated on a shared timestamp.
4. A dashboard that only ever shows one method for a given component is incomplete by construction
   for any component that answers yes to both of the questions above — which is most of them.
5. When you are handed a "four golden signals" requirement, you satisfy it by instrumenting RED
   (doc 01) plus the one or two Saturation signals from USE (doc 02) for the resources that
   component depends on most — you do not need a separate implementation for it.
