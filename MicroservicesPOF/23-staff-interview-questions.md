# Staff-Level Interview Questions: Points of Failure in Microservices

Twenty-four questions with full model answers. They are written to be **spoken** — what a strong
candidate would actually say out loud, including the clarifying question they would ask first
and the trade-off they would name without being prompted.

None of these is a trivia question. Every one has a defensible wrong answer, and what separates
a staff-level response is rarely more knowledge. It is a different move: naming the trade-off,
asking what the requirement actually is, doing the arithmetic on the whiteboard, or noticing
that the premise of the question is incomplete.

Useful in both directions — to prepare for an interview, or to run one. A section at the end
contrasts senior and staff answers explicitly.

---

## Part 1 — Foundations

### Q1. What are the single points of failure in this architecture? *(Pointing at a diagram with redundant everything.)*

I would push back on the question, politely, because the framing produces a list that misses the
outages that actually happen.

A point of failure is not a component; it is **a place where one thing going wrong changes what
a user sees**. Some of those are components, and those are the ones already handled — they are
why there are three of everything on that diagram. The ones that cause multi-hour incidents are
the other four kinds: the arrows between the boxes, the resources shared underneath them, the
control systems that direct them, and the assumptions the design encoded.

So concretely, looking at this diagram I would ask four things:

**What is shared that is not drawn?** The deployment pipeline — every one of those redundant
instances runs the same build, so a bad deploy has 100% blast radius through three availability
zones. The configuration source. The service registry. The NAT gateway. The node pool.

**What is the retry and timeout behaviour on each arrow?** A slow dependency is far more
dangerous than a dead one, and none of that is on the diagram.

**For each redundant component, name the failure that takes out all of them at once.** There
always is one. Two replicas that fail together 80% of the time give you 99.2%, not 99.99% — the
correlation term dominates the independent term by a factor of 400 in that example. If nobody
can name the correlated failure, the redundancy is a guess.

**How deep is the synchronous dependency chain?** If a request touches ten services in series at
99.9% each, the ceiling is `0.999^10 = 99.0%` — seven hours a month — regardless of how good
each service is. Depth is itself a point of failure, and the fix is reclassifying dependencies,
not hardening them.

**Follow-up I would expect:** *"How would you reduce that 99.0%?"* — By making dependencies
optional, not by making them better. If 6 of the 10 become soft — they degrade rather than fail —
the arithmetic becomes `0.999^4 = 99.6%`, and that is a 4× reduction in downtime with nobody's
service improving. The caveat I would add unprompted: a dependency is only soft if it is soft in
the **thread pool**, not just in the exception handler. If it hangs and my throughput changes, it
is hard whatever the comment says.

---

### Q2. A dependency is down versus a dependency is slow. Which is worse, and why?

Slow, and it is not close.

**Down is cheap.** The connection is refused in about a millisecond. My handler catches it,
applies the fallback, and returns. My latency actually *improves*, because a call that used to
take 30 ms now takes 1. Throughput is unchanged. I have lost a feature and I am still serving
every request.

**Slow is an outage.** Take a service at 640 requests/s with 200 worker threads and a p99 of 310
ms. Little's law, `L = λ × W`: it currently occupies `640 × 0.310 = 198` threads, which is
already tight. Now the dependency goes from 30 ms to 5 seconds, and my timeout is 6 seconds
because someone picked a round number in 2021. Each request now holds a thread for 5 seconds, so
I need `640 × 5 = 3,200` threads and I have 200. My effective throughput is `200 / 5 = 40
requests per second`.

**From 640 to 40 — a 94% drop — and it affects every request, including the ones that never
touch that dependency**, because threads are the shared resource.

The reason this matters more than it sounds: **every failure-handling mechanism I have is
triggered by errors, and a slow dependency produces successes.** The circuit breaker is closed
because the error rate is zero. Health checks pass. Retries are not firing. Nothing automatic
helps me.

So the two things that do help are the two I would check for in any design review. **A timeout
derived from the dependency's measured p99** — two to three times it, not a round number — which
is the mechanism that converts "slow" into "down" so the rest of the machinery can act. And **a
bulkhead**, so that even at the timeout value, that dependency cannot consume more than its
allotted share of my concurrency.

**Follow-up I would expect:** *"How do you pick the bulkhead size?"* — The same Little's law
calculation, per dependency, at p99. For a soft dependency I would deliberately size it small —
if it is optional, rejecting it costs a feature, and the bulkhead's job is to be small enough
that the failure cannot hurt. Acquire timeout of zero: there is no point queueing for permission
to do something optional.

---

### Q3. Your service's error rate is 0.00% and users are reporting failures. Where do you look?

Three structural reasons this happens, and I would check them in this order because each one
eliminates a class.

**First — is traffic normal?** My error rate counts requests that reached me and failed. A
request that died in DNS, at the CDN, at the load balancer, or in the accept queue is not in
that number. **So the signal is a traffic drop, not an error spike.** If request rate is down 30%
against the same hour last week with a flat error rate, requests are dying before they reach me
and everything in my dashboards is irrelevant. An external synthetic probe from a different
network settles it in thirty seconds.

**Second — is any single member of any dimension at 100%?** My aggregate error rate is a mean.
With 24 instances and one black-holing every request, my fleet rate is 4% and one twenty-fourth
of users see total failure. Same for shards, zones, cells, tenants, and client versions. I want
`min by (instance)` on success rate, not `sum`.

That instance case has a specific mechanism worth naming: an instance whose cache client failed
to initialise returns errors in 2 ms instead of 40. If the load balancer uses least-outstanding-
requests — which is normally the right choice — **it actively routes more traffic toward the
broken host because it is fast.** So "suspiciously fast" is a symptom, and almost no dashboard
shows it.

**Third — is the failure producing successes?** A stale replica returns correct-looking data. A
cache serves an old value. A consumer that stopped has no errors because it has no requests. A
derived dataset was pushed with 60% of its rows. For all of these, the signal is not failure, it
is **age**: replication lag, cache age, consumer lag, artefact age.

**Follow-up I would expect:** *"What single metric would you add?"* — Queue time: the gap between
the proxy receiving the request and my handler starting. It is invisible to every framework's
own timing, and it is where a whole class of failure hides. A service can honestly report a 40 ms
p99 while users see 840 ms, because the 800 ms was spent in the accept queue before my timer
started.

---

## Part 2 — Cascades and overload

### Q4. You rolled back the bad deploy twenty minutes ago and the service is still down. What is happening?

You are in a metastable failure, which means the thing keeping you down is no longer the thing
that put you down.

The distinction I would draw: **a cascade is a chain and a metastable failure is a loop.** In a
cascade, A breaks B breaks C, and fixing A unwinds it. In a loop, the system's response to being
overloaded generates more load — so once it is running it does not need the trigger, and removing
the trigger changes nothing.

The test is exactly what you have just described: the trigger is gone and it is still failing.
That is diagnostic.

So I would stop looking for causes and identify which loop. There are about seven and each has a
distinct indicator:

- Attempts divided by logical requests above 2 → **retry loop**.
- Queue age exceeding the client timeout → **queue loop**, where every request I complete is for
  a client that has left.
- Healthy instance count declining in a staircase → **redistribution loop**.
- Cache hit rate collapsed → **cache loop**.
- New connections per second approaching requests per second → **connection loop**.
- GC time or CFS throttle fraction high → **resource loop**.

Then break it, and the way you break it is to **reduce offered load, hard**. Not 20% — aim for
90%, or to zero. And this is the part people get wrong: because of hysteresis, **the load level
you have to drop to is far below the level at which the incident started.** A system that
collapsed at 6,000 requests per second might only recover below 1,500. Restoring traffic to
"normal" does not work, which is why an incident can sit at a flat bottom for hours while people
try increasingly large versions of the same ineffective action.

Concretely, in order of how fast I can apply them: turn retries off; put a concurrency cap on the
gateway; disable the largest non-critical caller; or take the service out of the load balancer
for sixty seconds and put it back.

**Follow-up I would expect:** *"Why not just add capacity?"* — Because the loop scales with me.
If retries are generating 18,000 requests per second against a capacity of 5,000, doubling
capacity to 10,000 still leaves me saturated, and I have spent ten minutes and a scaling event
to learn nothing. Also, new instances are cold — a JVM service might serve 8% of warm capacity
for its first minute — so with round-robin balancing I have added a target for the spiral rather
than capacity.

---

### Q5. Four services in a chain, each configured with three retry attempts. What is your traffic multiplier, and what do you do about it?

Eighty-one times. `3^4`. And nobody configured an 81 — each team configured a 3, and the
exponent is a property of the architecture that no individual team can see.

The reason it matters more than the number suggests: **the multiplier is a function of the
failure rate.** At a 1% failure rate the real amplification is about 1.02×. At 100% it is the
full 81×. So the system's response to being in trouble is to push harder, which is a positive
feedback loop by construction.

Three fixes, and I would want all three.

**Retry at exactly one layer** — usually the outermost one that can make a meaningful choice,
because only it can try a different region or a different endpoint. Everything in between passes
failures through. In a large organisation you often cannot achieve that, in which case budget the
exponent explicitly: two attempts at the edge and none anywhere else gives 2×, which is
affordable.

**Use a retry budget, not a retry count.** A count says each request may be tried three times; a
budget says retries may not exceed 10% of request volume. They are identical when healthy. At
100% failure, a count turns 640 requests per second into 1,920 and a budget turns it into 704.
That is the whole difference between a dependency that recovers and one that does not.

**Only retry what can succeed.** The row people get wrong is HTTP 500: a great many client
libraries retry all 5xx, so during an application bug — exactly when the service is least able to
absorb load — traffic triples and reproduces a deterministic failure. Connection-refused is
always safe to retry because nothing was processed. A timeout is safe only if the operation is
idempotent, because you do not know whether it happened.

The measurement I would add regardless: `attempts / requests` per dependency, on a dashboard.
Most teams have never measured it, and during an incident it is the number that tells you whether
to reduce load or look for a cause.

**Follow-up I would expect:** *"Your mesh team wants to enable retries fleet-wide. Thoughts?"* —
I would object. A platform default is applied to systems the platform team does not understand,
so it must be the conservative choice. Fleet-wide retries hit every non-idempotent `POST`, and
the resulting duplicate charges produce no error metric at all — both sides succeeded. It would
run for weeks and be found by finance. Retries should be opt-in per route, with the service
owner's sign-off recorded in the route config.

---

### Q6. Ten instances at 70% CPU. Is that healthy?

It depends on the answer to a question that is not in what you told me: **how many instances do
you intend to be able to lose?**

70% of what, is the first issue. Latency scales as `1/(1 − ρ)`, so at 70% utilisation requests
are already taking 3.3× their service time. The knee of that curve is around 70–80%, and above it
the curve is very steep — at 90% it is 10×, at 95% it is 20×. So 70% is roughly the right place
to be, and it is not "30% of headroom left" in any meaningful sense.

The real issue is the failure case. Ten instances at 70%:

- Lose one: `7,000 / 9 = 78%`. Fine.
- Lose three — one availability zone, if they are spread 3/3/4: `7,000 / 7 = 100%`. Saturated.
- Lose three during a deploy, with two more unavailable: `7,000 / 5 = 140%`. Collapse.

So if this service is supposed to survive losing a zone — which is presumably why it is in three
zones — then 70% is not healthy, it is an outage waiting for a zone event. The requirement works
out to:

```
utilisation after losing one of three zones ≤ 70%
→ steady-state utilisation ≤ 0.70 × 2/3 = 47%
```

**You have to run under 47% to survive a zone loss and stay on the flat part of the curve.** That
is the real cost of zonal redundancy, and it is much higher than most capacity plans assume.

The alternatives if 47% is too expensive are all legitimate and all need to be decided
explicitly: accept elevated latency during a zone loss and run at 60%; use four or more zones so
losing one costs 25%; or shed load during the failure so the surviving capacity serves the most
valuable traffic. What is not legitimate is discovering it during the zone failure.

**Follow-up I would expect:** *"Can't the autoscaler handle it?"* — No, and this is the most
common conflation in capacity planning. Measure it: metric window plus evaluation period plus
scheduling plus image pull plus container start plus warm-up is typically three to five minutes
to *useful* capacity, and more if a new node is needed. Almost every failure in a distributed
system is faster than that. **Autoscaling is for demand growth; headroom is for failures.** And
during a regional event the cloud may not have capacity in the surviving zones, because everyone
else is asking too.

---

## Part 3 — Data and correctness

### Q7. Write an order to the database and publish an event to Kafka. How do you make that atomic?

You cannot, and I would start there, because the useful part of this question is what you do
instead.

There is no ordering of those two writes that is correct. Database first and you crash before the
publish: the order exists and nobody is told, so it is never fulfilled. Kafka first and the
database write fails: fulfilment ships a product for an order that does not exist. Either order
with a timeout on the second: you do not know whether it happened, so you can neither safely
retry it nor safely skip it.

And it is not rare. At 67 orders per second with a 15-millisecond window, and forty pod restarts
a day from deploys and evictions, that is roughly forty inconsistent orders a day — about 14,600
a year — from code that looks completely normal in review.

Two-phase commit is the formally correct answer and it is wrong here: it blocks holding locks if
the coordinator dies, it multiplies availability down with no optional participants, and Kafka
is not a useful XA participant anyway.

**The answer is the outbox.** You cannot atomically write to two systems, but you can atomically
write to two tables in one database. So the event goes into an `outbox` table **in the same
transaction** as the order, and a separate relay reads unpublished rows and publishes them.

The detail that makes or breaks it: **the same transaction.** The most common failure of the
outbox pattern is an ORM that opens a new session for the outbox insert, which silently makes it
a separate transaction and reintroduces the exact problem. I would want a test that injects a
crash between the two writes and asserts that neither row exists.

For the relay I would use polling with `FOR UPDATE SKIP LOCKED` — simple, no extra
infrastructure, multiple relay instances can run concurrently. Change data capture is the
alternative and is better at low latency; if I used it I would run CDC **on the outbox table**
rather than on business tables, because CDC of a business table publishes your schema as your
event contract, and then an internal column rename breaks every consumer.

**And the consequence that has to be stated**: the outbox gives at-least-once delivery, not
exactly-once. **Every consumer must be idempotent.** The outbox solves atomicity; it does not
solve duplication.

**Follow-up I would expect:** *"Is this exactly-once?"* — No. Exactly-once *delivery* over a
network is impossible: the sender cannot know whether the message arrived, so it either risks
losing it or risks duplicating it. Exactly-once *processing* is achievable and means something
different — at-least-once delivery plus an idempotent consumer, with the deduplication in the
consumer's own store, in the same transaction as the effect. That is the only place the guarantee
actually holds. I would avoid the phrase "exactly-once" in a design document, because it tells
the reader they need not implement the part that does the work.

---

### Q8. Design the inventory reservation for a flash sale. 500 units, 1,200 concurrent buyers.

I would ask one thing first: **is a small oversell acceptable, or is it absolutely not?** That
single answer changes the design, and it is a business question, not a technical one.

Assume a small oversell is very expensive but not fatal — a normal retail situation.

**What I would not do** is read-then-write. `SELECT available` then `UPDATE available = available
− 1` at `READ COMMITTED` does not prevent lost updates — two transactions both read 1 and both
decrement. With 1,200 concurrent attempts in a few milliseconds, that is not an edge case, it is
the normal case, and it is how you sell 847 of 500 units.

**What I also would not do** is `SELECT ... FOR UPDATE`. It is correct and it serialises.
Throughput through a lock is `1 / hold_time`, and a hold time of 45 milliseconds — read, check,
update, commit, plus the application round trip — gives 22 reservations per second. Correct and
unusable, and the queueing blows out the calling service's worker pool as a bonus.

**What I would do**, three things together:

*A single atomic conditional update with no read:*

```sql
UPDATE inventory SET available = available - $qty
WHERE sku = $sku AND available >= $qty
RETURNING available;
```

Zero rows means insufficient stock. One row means reserved. No race, and the hold time drops to
about 3 ms, so per-SKU throughput goes to ~330 per second.

*Row-level stock splitting for the hot SKU.* Split 500 units across 20 rows of 25 and pick a
random shard. Contention on any one row drops 20×, so aggregate throughput goes to several
thousand per second. If the chosen shard is empty, try another, then fall through to a scan.

*A reservation with an expiry, not a decrement.* Checkout reserves; payment confirms; an
unconfirmed reservation expires. **And the expiry must be checked on read**, so an expired hold
is invisible immediately without any sweeper running — correctness must not depend on a
background job. The sweeper exists to tidy up and emit metrics, not to be load-bearing.

That third one is the part most designs miss and it is the one that produces the worst outcome.
Without it, you lose inventory at your abandonment rate, permanently. A ticketing example: 200,000
users holding seats for six minutes with 60% abandonment gives 120,000 abandoned holds against a
100,000-seat venue. The event sells out to nobody.

**Follow-up I would expect:** *"Your sharded design can oversell by one or two at shard
boundaries."* — Yes, and that is the trade I named at the start. A 0.02% oversell rate producing
an apologetic email is cheaper than a 22-per-second ceiling producing a failed sale. **The
important thing is that it is written down as a decision.** The read-then-write version had the
same risk and nobody had decided it.

---

### Q9. Users report seeing stale data immediately after they update something. Walk me through it.

This is replication lag, and I would frame it as a **correctness bug rather than a performance
issue**, because that reframing is what gets it fixed properly.

The mechanism: the write goes to the primary and is acknowledged. The immediate read goes to a
replica that does not have it yet. Both operations were correct against the data they saw.

The reason it is a correctness bug: the application's *logic* is wrong during the window. A user
places an order, the confirmation page reads a replica, finds nothing, and shows "we could not
find your order" — so the user, reasonably, buys again. Replication lag produced a duplicate
order. The service-to-service version is worse: a consumer reads the order row from a replica to
enrich an event, does not find it, and dead-letters it as orphaned. Now you have a DLQ full of
valid orders, discovered whenever someone next reads the DLQ, which may be never.

Five mechanisms, and I would pick per query rather than globally:

**Read your own writes from the primary** — after a write, route that user's reads to the primary
for a few seconds. Cheap, covers the common case, and it is what I would do first.

**Monotonic reads via log position** — the write returns its LSN, subsequent reads require a
replica at or past it, else fall back. Correct rather than heuristic; costs plumbing the position
through the API.

**Synchronous replication** for the specific tables that cannot lose writes, set per transaction
rather than globally, so the payment write is synchronous and the page-view log is not.

**Quorum reads and writes** in a Dynamo-style store — `LOCAL_QUORUM` is the standard answer.

**Do not read from replicas for that data at all** — correct for small, correctness-critical
tables.

The discipline that matters more than any of them: **decide staleness tolerance per query and
make it explicit in the code.** A `readPreference` set once at the connection level guarantees
you will get it wrong for some queries. I like a repository layer that exposes `findOrder(id)`
and `findOrderEventuallyConsistent(id)` — being forced to type the longer name is the design
working.

**Follow-up I would expect:** *"How do you stop a badly-lagged replica serving reads at all?"* —
Make lag a routing input, not just an alert. Each replica reports lag; the proxy removes any
replica above a per-workload threshold from the read pool; if all exceed it, fall back to the
primary and alert, because you are now taking read load you did not plan for. And measure lag
with a heartbeat table rather than `Seconds_Behind_Source`, which reads zero when replication has
stopped entirely.

---

### Q10. Is a distributed lock in Redis safe for correctness?

No, and there are four independent reasons, which is worth saying because fixing one or two does
not help.

**One: the TTL can be shorter than the work.** A five-minute lock on a job that grew to seven
minutes means a second worker starts while the first is running.

**Two: `DEL` deletes whoever's lock is there**, not necessarily yours. The first worker finishes
and deletes the lock the second one now holds. Release has to be a compare-and-delete, which
means a Lua script, because `DEL` is not conditional.

**Three — and this is the one no TTL fixes — a process can pause for an arbitrary duration
without knowing it happened.** A JVM full GC of forty seconds, a VM live migration, memory
pressure and swap, CFS throttling, a slow disk. From inside the process it looks like one line of
code took a long time. The lock expires, someone else takes it, and the paused process wakes up
and continues writing, completely unaware. **There is no upper bound on pause length, so there is
no TTL that closes this.**

**Four: Redis replication is asynchronous**, so a failover can promote a replica that never
received the lock.

The fix is not a better lock. **It is to accept that the lock cannot prevent a stale holder from
acting, and to make the resource reject it.** Give each acquisition a monotonically increasing
fencing token; the holder passes it with every write; the resource remembers the highest token it
has seen and refuses anything lower. Now the pause does not matter — the write is rejected when
it eventually arrives.

Usually you already have the mechanism. A version column with `UPDATE ... WHERE version = $n` is
a fencing check. So is a conditional write in DynamoDB, an `If-Match` in S3, etcd's
`ModRevision`, or a leader epoch.

So the rule I would state: **if you cannot fence, your lock is an optimisation and not a
guarantee** — which is often fine, and should be written in the code so that nobody later depends
on it for correctness.

**Follow-up I would expect:** *"What about Redlock?"* — It attempts to address the replication
problem by acquiring on a majority of independent instances. Whether it provides the guarantee it
claims is genuinely contested, and the disagreement is precisely about reason three, the timing
assumption. My practical position: if the lock is for efficiency, a single Redis lock is fine and
Redlock is over-engineering. If it is for correctness, I need fencing — and once I have fencing,
the lock service's exact guarantees barely matter.

The better question I would raise unprompted: **what would actually break if two ran?** Very
often the answer is "nothing, it would just be wasteful", in which case I want a cheap
optimisation and should say so. And if the answer is "duplicates", the right fix is usually
idempotency, which removes the requirement rather than satisfying it.

---

## Part 4 — Async and derived data

### Q11. A queue consumer has been stopped for six hours and nobody noticed. What monitoring was missing?

Everything, because the standard monitoring set cannot detect this.

A stopped consumer has **zero errors**, because it has no requests. Its latency is undefined. Its
CPU is idle, which looks healthy. Its pods are `Running` and its probes pass. Every dashboard is
green.

The one signal that detects it is **the age of the oldest unprocessed message**. Not queue depth
— age. And the distinction matters: ten thousand messages of lag is 0.3 seconds behind on a
34,000-per-second topic and four minutes behind on a 40-per-second one. The same number means
three different things, so a threshold in messages is either too sensitive for one topic or too
insensitive for another. Age has a consistent meaning and maps directly onto a business
requirement.

Beyond that I would want four more:

**Lag derivative**, because it distinguishes a burst that will drain from a deficit that will
not. If lag is two million and falling at 300 per second, I am 1.9 hours from recovery and I can
say so. If it is two million and flat during a quiet period, no amount of waiting fixes it.

**A successful-process rate**, alerting when it is zero for longer than the expected quiet
period. This catches the case where the queue is legitimately empty versus the consumer being
dead.

**End-to-end canary messages** — inject a synthetic message every minute and measure how long it
takes to come out. This tests the whole path including the consumer's downstream, and it works
when the queue is otherwise empty, which is exactly when lag metrics tell you nothing.

**DLQ depth, alerting above zero.** Not above a threshold — above zero. Every dead-lettered order
is a customer who did not get their goods.

**Follow-up I would expect:** *"You fixed the consumer and now the database is falling over."* —
Yes, and that is the most predictable second outage there is. During six hours a backlog
accumulated, and when I restore the consumer it drains at its maximum rate — three times normal
load, sustained — into a downstream sized for normal. So I rate-limit the drain deliberately, to
maybe 1.5 or 2× steady state. And I compute the drain time *before* the incident, because
somebody will ask "how long?" and "as fast as possible" is the wrong answer. Seventy-five safe
minutes beats twenty-five that re-breaks the database.

---

### Q12. A nightly pipeline has been failing for nine days and the serving tier looks perfectly healthy. Explain and fix.

This is the failure class that defeats everything else in an operations toolkit, and the reason
is that **the system's fault tolerance is what hides the failure.**

A versioned derived store serves the last successfully pushed version. If the next push never
arrives, it keeps serving the previous one — correctly, quickly, with a 100% success rate,
indefinitely. That is a good design; it means a failed push never produces a partial or corrupt
dataset. And it means a dead pipeline is invisible from the serving side.

The chain of non-detection is usually: the job failed with a real error; the alert went to a team
list alongside forty other daily batch alerts and nobody triaged it; **no push means no push
error**, because the push system correctly reported that it had not been asked to do anything;
and the quality decline was gradual enough that no single day crossed a threshold.

The fix is **freshness as an enforced SLO measured by the consumer, not the producer.** A
pipeline that has died cannot report that it died, so the serving tier reports the age of the
version it is serving:

```
max by (store) (time() - derived_store_active_version_created_timestamp) > max_age
```

Each store declares a maximum age with a tier — ranking model 36 hours and paging, "people you
may know" 7 days and a ticket — and **past the limit the service degrades explicitly**, falling
back to a simpler known-good model and emitting a metric, so the failure becomes visible in
product behaviour rather than hidden in it.

The organisational point I would make: with several thousand batch jobs, per-job alerting does
not scale and will always be ignored. **Alert on the output, not the process.** There are far
fewer artefacts than jobs, and each artefact has a consumer who cares about it.

**Follow-up I would expect:** *"What if the push succeeds with only 60% of the rows?"* — Then
freshness passes and you have a different problem. You need validation gates before the version
flip: row count within 5% of the previous version and 20% of the same day last week; a key-space
coverage check against 10,000 known keys; value-distribution checks; and — cheapest and most
effective — the job refuses to start unless all expected input partitions have landed. The
default behaviour of most data tooling is to read whatever it finds, and that default is wrong
for anything that will be served.

---

## Part 5 — Change and platform

### Q13. Your team wants to enable mTLS in STRICT mode across a namespace. What is your process?

The process is more important than the configuration, because the configuration is one line and
it can produce a total outage in forty-five seconds.

**First, the sequencing.** Never go straight to `STRICT`. Enable `PERMISSIVE`, which accepts both
mTLS and plaintext. Then **measure** — the fraction of traffic to each destination arriving
without mTLS must be zero, observed over a full window that includes a weekly cycle, because
some callers only run weekly. That measurement step is the one that gets skipped and it is the
only one that matters.

**Second, the inventory problem.** The mesh's view of a namespace is not the organisation's. The
failure I would expect is a workload without a sidecar — a legacy VM-based service, a job, a
third-party agent — that is logically part of the namespace and physically outside the mesh. It
works under `PERMISSIVE` and is instantly unreachable under `STRICT`. So I want an inventory of
callers derived from **observed traffic**, not from the orchestrator's object list.

**Third, staged rollout with a gate.** Apply to 1% of destination workloads, then one service,
then the namespace, with an automated abort on error-rate regression at each step. A
namespace-wide security policy change has the blast radius of a deploy and, in most
organisations, none of the process.

**Fourth, error attribution.** An mTLS rejection surfaces as a connection reset, which looks
exactly like a network problem and costs twenty minutes of diagnosis. I would add a distinct
policy-rejection counter and a distinguishable response code before the rollout, not after.

**Follow-up I would expect:** *"How is this different from a code deploy?"* — It is not, and
that is the point. Configuration reaches every instance in under a minute with no build, no
review gate, and no rollback automation, while having the same blast radius. The general rule:
**config is code and must go through the code process** — version control, CI validation,
progressive rollout, automated rollback, audit. In most organisations the list of things that can
be changed in production without that process is much longer than the list of things that
cannot, and nobody has audited it.

---

### Q14. A canary passed and the full rollout broke production. What went wrong?

One of four blind spots, and I would want to know which because the fixes differ.

**Not enough traffic to be statistically significant.** This is the most common by a wide margin.
A 1% canary on a service doing 100 requests per second sees 1 per second. To distinguish a 0.1%
error rate from a 1% error rate with confidence you need hundreds of errors, which at that rate is
hundreds of seconds minimum. The canary passed because it had not seen enough requests to fail.
The fix is to **compute** the required traffic and duration from the rate and the effect size you
need to detect, rather than picking "5% for 10 minutes" because it sounds reasonable.

**Not enough time.** A memory leak takes an hour. A cache-related failure takes a full TTL. A
daily batch interaction takes a day. A ten-minute canary cannot see any of them.

**Not the right traffic.** Random sampling under-represents the tail, and bugs live in the tail —
one locale, one client version, one customer segment, one device type. I would deliberately route
a representative slice, including internal users and a sample from each major segment.

**The wrong baseline.** Comparing the canary against last week's metrics conflates the version
change with time of day, traffic mix, and downstream state. The correct comparison is a
**concurrent control group** of old-version instances receiving the same traffic at the same
moment, compared with a test that accounts for variance rather than a threshold on one number.

And I would state the limit honestly rather than trying to fix it: **a canary catches fast,
common, traffic-visible regressions.** It does not catch slow leaks, rare paths, or correctness
bugs that produce valid-looking responses. For those you need shadow traffic with output
comparison, and reconciliation.

**Follow-up I would expect:** *"How long should a rollback take?"* — I would ask whether anyone
has measured it, because that is usually the more interesting answer. From "decide to roll back"
to "old version serving 100% of traffic". A Helm rollback that re-pulls images and waits for
readiness across forty pods can be eight minutes, which is eight minutes of full outage. It
should be a number on the runbook and every on-call engineer should know it.

---

### Q15. Rename a column on a table with 500 million rows, in a service that deploys continuously.

Four deploys, not one, and the reason is a property people forget: **during any rolling deploy,
old and new code run simultaneously against the same database.** That is guaranteed, not an edge
case, so the schema must be compatible with both versions at once — which a single rename never
is.

The expand–contract sequence:

```
Deploy 1 — EXPAND
  Migration: ADD COLUMN total_amount, nullable, no default → instant on modern Postgres
  Code:      writes BOTH columns, reads the old one
  Compatible with the previous version, which ignores the new column ✓

Deploy 2 — BACKFILL
  A batched job copies old → new. Not a migration; a job you can stop.
  Code unchanged.

Deploy 3 — SWITCH READS
  Code: writes both, reads the new column
  Revertible: deploy 2's code still works ✓

Deploy 4 — CONTRACT
  Code: new column only
  Migration: DROP the old column, as a separate later change after a soak period
  ⚠️ Not revertible past deploy 3
```

Two operational details that are where the outages actually come from.

**Set `lock_timeout` on the DDL.** PostgreSQL's lock queue is ordered: if a long analytics query
holds a shared lock and my `ALTER TABLE` requests an exclusive one, the `ALTER` waits — and
**every subsequent query queues behind it**, including short reads. A 50-millisecond schema change
blocked behind a ten-minute query stops all traffic to that table for ten minutes. A two-second
`lock_timeout` with a retry turns that into a failed migration attempt instead of an outage.

**Throttle the backfill against replication lag.** `UPDATE ... WHERE` on 500 million rows in one
transaction holds locks on everything it touches, generates hundreds of gigabytes of WAL that
replicas must apply, and creates 500 million dead tuples for vacuum. Batch it into chunks of a
few thousand with a commit between, and pause when replication lag exceeds a threshold. That
throttle is about ten lines and it is the difference between a background process and a database
incident.

**Follow-up I would expect:** *"That is four deploys over days for a rename. Worth it?"* — Yes,
and the reason is not the rename. It is that every intermediate state is valid and every step
except the last is independently revertible. The single-deploy version has a window during which
something is broken and a rollback that does not work. The general principle applies to APIs,
event schemas, and credentials equally: **destructive operations are never in the same change as
the thing that makes them possible.**

---

### Q16. When should a company adopt a service mesh, and when should it not?

I would answer the second half first, because the failure mode of adopting too early is worse
than adopting too late.

**Do not adopt a mesh if** you have fewer than about twenty services — the control plane is more
complexity than it removes. If you are predominantly event-driven, because **a mesh covers
synchronous traffic and does nothing for messaging**, which at that point is the wrong half of
your problem. If your latency budget cannot absorb 0.6 to 1.0 milliseconds per hop multiplied by
your call depth — on a twelve-hop chain that is ten milliseconds at p50 and potentially sixty at
p99. And, most importantly, **if you do not yet have progressive deployment, load shedding, and
basic observability**, because a mesh amplifies whatever discipline you have, including its
absence.

The last one is the real test. An unowned mesh is a global single point of failure operated by
nobody, which is strictly worse than no mesh.

**Adopt one when** you have more than roughly fifty services or more than two or three languages,
and — this is the actual criterion — **when you cannot get consistent resilience into
applications any other way.** If you have one language and a shared library that teams actually
use, you already have most of the benefit.

The value, stated properly, is not the features. It is the **improvement rate**: a tail-latency
fix that took quarters to reach 2,500 services through a library release now reaches them in days
through a sidecar image bump, and a configuration change in under a minute. That compounds.

The cost, which should be computed rather than assumed: at 100,000 sidecars, roughly 12.5
terabytes of memory and 20,000 cores — typically 5–15% of infrastructure cost. Note that sidecar
memory scales with **pod count, not traffic**, so many-small-pods deployments are
disproportionately expensive. Plus about a year of *worse* diagnosis time while the organisation
learns to read Envoy configuration.

And the trade at the centre of it: **a mesh does not remove failure points, it relocates them.**
Severity moves out of the per-service classes — RPC, patterns, cascades — and into the platform
classes: the control plane and configuration change. Many small failure points become a few large
ones. At 2,500 services that is clearly the right trade; at 20 it is clearly not.

**Follow-up I would expect:** *"What are the middle options?"* — A shared client library, which
has no latency tax and no control plane. Proxyless xDS, which gives you the control plane's
benefits with no sidecar hop. A per-node proxy instead of per-pod, which cuts the resource tax at
the cost of a coarser failure domain. Or a mesh for mTLS and L4 only, with resilience in the
application. These are under-considered, and for most organisations asking the question one of
them is the right answer.

---

## Part 6 — Design and judgement

### Q17. Design admission control for a ticket sale: 5,000 QPS baseline, 500,000 QPS at sale open, 100,000 seats.

The core observation is that the backend can safely handle about 10,000 QPS of booking work and
500,000 is arriving, so **the design question is how you reject 98% of the traffic in a way that
is fair, does not generate retries, and keeps users informed.**

The naive options all fail instructively. Serving everyone produces congestive collapse in
seconds. Rate-limiting and returning 503 is random, which advantages bots and produces a retry
storm from 490,000 rejected clients. Scaling the web tier just delivers 500,000 QPS to a database
that does 1,800 writes per second — you have moved the collapse one layer down, where there is no
admission control at all.

**The answer is a virtual waiting room**, which is load shedding with a user experience attached,
and the user experience is what makes shedding socially acceptable.

- Every arrival gets a static page **served entirely from the CDN**. That absorbs roughly 82% of
  peak traffic with zero origin cost.
- A token is issued on first arrival with one atomic operation — a sorted-set insert keyed by
  arrival time. That is the only backend call in the entire waiting experience.
- Position polling is CDN-cached per bucket with a three-second TTL, so ten million pollers
  produce a couple of thousand origin requests per second rather than three million.
- An admission controller releases tokens at a rate the booking system can absorb.

Three properties make it fair, and all three matter: **position is assigned on arrival and never
changes**, so refreshing and retrying do not help; the token is bound to the session and cannot
be parallelised; and **the wait is visible**, which is the operationally important part — a user
who can see "you are 184,203 of 512,880, about 26 minutes" does not retry.

The part I would emphasise hardest: **the admission rate must be computed from measured backend
health, not set by a human, and it must fail closed.** Conflict rate is superlinear in
concurrency — raising admission 2.5× can raise backend work 4×, because more concurrent users
selecting seats means more of them collide. And if the health signal goes stale, the controller
must back off, because a controller that cannot measure what it is protecting has to assume the
worst. Any manual control is a multiplier in [0, 1] — an operator can slow admission, never speed
it past what the controller computed.

**Follow-up I would expect:** *"What about everything being cold at t=0?"* — That is the second
half of the problem and it is why baseline being 1% of peak matters. At sale open the caches are
empty, the connection pools are unestablished, the JIT has not run, the TLS session cache is
empty — 83,000 full handshakes per second is about 125 cores of pure handshake work — and the CDN
POPs do not have the page. So there is a pre-warm protocol on a timeline: scale at T−60, warm
caches and pools at T−45, **synthetic load at 20% of expected peak at T−35**, a go/no-go gate at
T−20, freeze at T−15, open the waiting room at T−10. The synthetic load step is the most valuable
one, because it converts "we believe we have capacity" into "we have observed this capacity
serving requests", and it routinely catches things like pods pending on IP exhaustion or a
parameter group that was not applied.

And I would disable autoscaling for the duration. It adds cold instances to a maximally loaded
system and removes warm ones during lulls. Capacity is a decision made an hour before, not a
control loop running during the event.

---

### Q18. Design the location pipeline for a ride-hailing system: 750,000 GPS writes per second.

The first thing I would establish is the property that makes this tractable: **these writes are
allowed to be lost.** A GPS fix that is four seconds stale is replaced by a fresher one in four
seconds, and nobody can tell. If I do not establish that, I will design something that costs a
hundred times more than it needs to.

Given that, the geo-index is **an in-memory hash map keyed by S2 cell**, not a database. The
arithmetic:

- Writes: compute the cell from lat/lon, about 200 nanoseconds, then update two hash-map entries.
  `750,000 × 200 ns = 150 ms of CPU per second`, which is 15% of one core.
- Reads: compute the covering cells for the query radius, union their driver sets. p50 0.3 ms,
  p99 2.1 ms.
- Memory: three million drivers at roughly 200 bytes each is 600 megabytes.

Compare a spatial database: the best case on a tuned distributed store is around 50,000 writes
per second per node, so fifteen-plus nodes doing nothing but index maintenance, with radius
queries at 10–30 ms p99 against a 2 ms requirement. **Fifteen percent of one core versus fifteen
nodes**, and the nodes are paying for durability on data that is worthless in four seconds.

The property that makes losing it acceptable is the one I would highlight: **if an index instance
restarts, its map refills within four seconds**, because every driver reports every four seconds.
There is nothing to restore, replicate, or back up. **State that reconstructs itself from natural
traffic in bounded time gives a stateful service a stateless operational profile**, and it is
worth looking for that property in other designs.

The durable copy exists separately: the same event stream feeds a wide-column store with a
thirty-day TTL for trip reconstruction and disputes. **One stream, two consumers, two completely
different service levels** — that is the pattern the whole system is built on.

**Follow-up I would expect:** *"What is the failure mode of the in-memory index?"* — It goes
stale silently, and this is the serious one. If the consumer feeding it stalls, the index keeps
answering queries with drivers who *were* somewhere forty minutes ago. No errors, and the p99 is
actually *better* than usual because the working set is smaller. Matching quality collapses and
nothing technical shows it.

So the index must **measure and enforce its own freshness**: every entry carries the timestamp of
the fix that produced it, the instance reports the age of its newest entry, and it **fails its
readiness check above thirty seconds** — which is a legitimate use of a readiness probe because
the staleness is specific to this instance, not a shared dependency. Plus queries filter out
drivers whose last fix is over sixty seconds old. And the only signal that catches the whole
class is a business metric: median pickup distance per city, as a paging alert.

---

### Q19. A single bad request crashes every instance of a service within seconds. Explain and prevent.

This is the fastest total outage there is, and it is a correlated failure with a restart loop.

The mechanism: one request contains input that triggers a crash — an unhandled panic, deep
recursion, an allocation beyond the memory limit, a catastrophically backtracking regex. The
request is retried, either by the client or by the load balancer, to another instance. That one
crashes. It works its way through the entire fleet, killing each instance, and then the
orchestrator restarts them while the request is still being retried.

Redundancy provides no protection, because the failure is perfectly correlated: **every instance
runs the same code and hits the same bug on the same input.**

The prevention, in order of value:

**Never let an unhandled error in request-scoped code terminate the process.** A `recover()` in
the HTTP middleware, or a catch-all handler. This single change turns a fleet-killer into one
failed request, and it is usually a few lines.

**Bound everything derived from input**: body size, JSON nesting depth, array lengths, recursion
depth, regex complexity on user-supplied patterns.

**Do not retry 500s.** The retry is what spreads it. A 500 means the application ran and failed;
retrying reproduces a deterministic failure and converts one dead instance into a dead fleet.

**Crash-loop backoff is a defence, not a nuisance.** Kubernetes' exponential `CrashLoopBackOff`
limits how fast the loop can run, and "fixing" it with an aggressive restart policy removes a
safety mechanism.

**Follow-up I would expect:** *"What is the queue version of this?"* — A poison message. The
consumer fails and does not commit the offset, so the broker redelivers the same message forever.
It is worse in one specific way: **head-of-line blocking** means one bad message blocks its whole
partition, so if messages are keyed by customer, a specific set of customers stops entirely while
everyone else is fine — which looks like a data problem rather than a pipeline one. The fix is
bounded retries then a dead-letter queue, with permanent failures like a deserialisation error or
a 4xx going to the DLQ on the *first* attempt, because they will never succeed.

---

### Q20. Two teams disagree: one wants cells, one wants a second region. You have budget for one.

I would ask what failures they are each trying to survive, because the two answers address
different ones and the teams are probably not disagreeing about the same thing.

Then I would argue for cells, in most cases, and the reasoning is about *which failures actually
happen.*

**A second region protects against a regional failure**, which is rare, plus latency for distant
users and data-residency requirements, which are real and are different arguments. If the
requirement is residency or latency, the answer is a second region and there is nothing to
discuss.

**But for availability**, most outages are not regional. They are bad deploys, bad configuration
pushes, poison requests, cache collapses, and overload. **A second region does not help with any
of those and can make some worse** — a bad deploy reaches both regions, and now you have
corrupted data in two places.

Cells bound the blast radius of *every* failure including the ones nobody anticipated, which is
the property that matters, because your worst outage will be caused by something not on anyone's
list. Sixteen cells means any single-cell failure — whatever its cause — affects 6.25% of users.
And critically, **cells bound the deployment failure domain**, which is the one that spans every
physical boundary you already paid for.

There is also an operational argument. A second region is roughly 1.6× cost for a warm standby,
and **a standby that does not serve real production traffic does not work** — it accumulates
configuration drift, expired credentials, pipelines that silently stopped deploying to it, and
scaled-down capacity that cannot come back fast enough. The settling question is: when did you
last serve real users from it? If the answer is "never" or "during the test six months ago", you
have a plan, not a capability.

**Follow-up I would expect:** *"How many cells?"* — Eight to sixteen for most systems. The
blast-radius return diminishes sharply — going from 4 to 8 cells improves it by 12.5 points;
going from 32 to 64 improves it by 1.5 points for double the overhead. The binding constraints
are usually different anyway: a cell must be large enough to be efficient and small enough that
you can load-test one to destruction. And the rule that preserves the value: **when growth
exceeds a cell's capacity, add a cell, never grow the cell** — otherwise you are running an
untested configuration.

And I would add the caveat unprompted: **below a certain scale cells are pure overhead.** If the
whole system fits in twelve instances, sixteen cells means sixteen sets of minimums and you have
multiplied the operational surface faster than the team's capacity to manage it.

---

### Q21. Your organisation wants to improve reliability and has six months. What do you do first?

I would order it by reliability per unit of effort, and the first four items are mostly
configuration and discipline rather than projects — which is why they are first.

**One: progressive deployment with automated rollback.** Change is the most common trigger of
outages, and this bounds it. `maxUnavailable: 0`, `minReadySeconds`, and an automated gate that
aborts on error-rate regression. Plus **measure the rollback time**, because that number
determines the length of every deploy-triggered incident and almost nobody knows it.

**Two: timeouts derived from measured p99, retry budgets, and bulkheads.** These address the
most common amplifier. A CI check that fails the build when a configured timeout exceeds 5× the
dependency's measured p99 catches the whole class cheaply.

**Three: the async monitoring set** — age of oldest unprocessed message on every queue, DLQ depth
above zero, and consumer lag derivative. This addresses the longest outages, which are the ones
with no error signal.

**Four: reconciliation on every pair of systems that must agree.** It is the only detector for
correctness failures, where both sides succeeded and they disagree. Running it for the first time
is how most organisations discover they have been losing money for months.

**Five: `N − k` headroom and load shedding.** Compute utilisation after losing the failure domain
you claim to survive; it is a spreadsheet exercise that finds real problems in most fleets.

**Six: freshness SLOs on every derived artefact.**

**Then** cells, and then a second region, which are projects.

The observation I would make about this ordering: **most organisations attempt it in roughly
reverse order**, because a second region is a project you can name, staff, and put on a roadmap,
while timeout hygiene is a habit you have to build across every team. The second is worth far
more.

**Follow-up I would expect:** *"What is the single cheapest high-value thing?"* — Overlaying
deploy, configuration-change, and feature-flag events on every dashboard. It costs a webhook and
it answers the first question of every incident — what changed in the last sixty minutes — without
anybody having to go and look. After that, the ability to **turn retries off fleet-wide without a
deploy**, which is the highest-value emergency lever there is and which most teams do not have
wired up.

---

## Part 7 — Shorter questions with sharp answers

### Q22. Why is `SELECT ... FOR UPDATE` across a network call dangerous?

Because throughput through a lock is `1 / hold_time`, and a network call puts an unbounded,
externally-controlled quantity inside the hold time. A 45-millisecond hold gives 22 operations
per second. If the remote call is a payment provider that occasionally takes 8 seconds, your
throughput on that row occasionally goes to 0.125 per second, and every waiter blocks — which
consumes the caller's worker pool and turns a slow third party into your outage. The correct
structure is a row with a state and an expiry, not a held lock: reserve, release the transaction,
make the remote call, then confirm with a conditional update that revalidates the reservation.

### Q23. Your load balancer's health check queries the database. What is wrong with that?

It turns a shared dependency failure into a total outage. When the database fails over for twenty
seconds, every instance fails its check simultaneously, the balancer removes all of them, and
there are now zero healthy targets — so instead of twenty seconds of elevated errors you get
ninety seconds of total unavailability including the re-registration time. The rule: **a readiness
check may test anything unique to this instance and must never test anything all instances
share.** If every instance would fail it at the same moment, it belongs in an alert, not a health
check. And configure fail-open at the all-targets-unhealthy boundary, because a possibly-broken
backend beats no backend.

### Q24. Everything is behind a 96% CDN hit rate. What keeps you awake?

That a five-point hit-rate drop is a 2× origin load change, and at 96% going to 50% the origin
sees roughly 12× its normal traffic — which it was never sized for, because the cache absorbed
years of growth and nobody re-derived origin capacity. The specific triggers are cheap and
common: a new tracking parameter entering the cache key, a `Vary: User-Agent` added to fix a
rendering bug, a personalisation header on a previously anonymous path. The defences are an
**allowlist** cache key rather than a denylist — because the next tracking parameter has not been
invented yet — a per-path hit-rate alert with paging sensitivity, and an honest answer to the
question "can origin serve 100% of traffic?" If it cannot, the cache is a tier of the system and
needs a database's engineering attention, including a tested cold-start procedure.

---

## Senior versus staff: the difference in the answers

The same question, answered two ways, to make the distinction concrete.

> **"A dependency is timing out. What do you do?"**

**A senior answer:** Add a circuit breaker so we stop calling it while it is down, add retries
with exponential backoff, and set a sensible timeout. Alert on the error rate.

Everything there is correct. Nothing is wrong with it.

**A staff answer:** First I want to know whether it is timing out or slow, because they need
opposite responses — a timeout means my protection is working and a slow success means none of my
mechanisms will fire. Then I want the dependency's measured p99, because the timeout should be
two to three times that and I suspect the current one is a round number that is 20× too high. And
before adding retries I want to know how many other layers retry, because four layers at three
attempts is an 81× multiplier that arrives exactly when capacity is lowest — so I would want a
budget rather than a count.

But the more important question is whether this dependency should be hard at all. If it is soft,
the fix is a bulkhead and a fallback, and then its being down costs a feature rather than the
service. If it is hard, my availability ceiling includes it and I should say so in the design
document.

And I would want to know what happens to the fallback path, because it has almost certainly never
executed.

**The differences, named:**

| | Senior | Staff |
|---|---|---|
| Starts with | The mechanism | The **requirement**, and a clarifying question |
| Numbers | Qualitative — "sensible timeout" | **Derived** — "3× the measured p99, currently 20× too high" |
| Scope | This service | The **system** — who else retries, what the ceiling is |
| Trade-offs | Implicit | **Named unprompted** |
| Failure of the fix | Not considered | **Considered** — "the fallback has never run" |
| The question behind the question | Answered as asked | **Re-framed** — "should this be a hard dependency at all?" |

The pattern across all six rows: **a staff answer treats the question as a symptom and asks what
the system is actually required to do.** It is not more knowledge; it is a different first move.

---

Next: [24-playbook-checklists-and-golden-defaults.md](24-playbook-checklists-and-golden-defaults.md),
which is the operational companion to all of this — triage order, the annotated configuration, and
the checklists.
