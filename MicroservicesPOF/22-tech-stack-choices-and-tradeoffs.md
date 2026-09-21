# Technology Choices and the Failure Points They Buy and Create

Every technology choice is a trade of one failure profile for another. There is no option that
removes failure points; there are options that replace a set you find intolerable with a set you
can live with.

That framing matters because most technology comparisons are written the other way round — as
lists of capabilities — and capabilities are not what determines whether your system stays up at
3 a.m. The questions that determine that are:

- **What does this choice make impossible?** (The failure it eliminates.)
- **What new thing can now break?** (The failure it introduces.)
- **What does it cost to operate on its worst day?**
- **What is the escape route if it is wrong?**

This doc goes concern by concern with those four questions, then maps the answers onto the six
running systems, then covers the choices that look good and are not.

## The decision procedure

Before the tables, the method. Applied to any technology choice, in this order:

**1. State the failure you are trying to eliminate, specifically.** Not "we need better
scalability" but "at 5,000 writes/s our single primary's right-edge index contention caps us at
3,000 and we are at 2,400." A choice made without a named failure is a preference.

**2. Establish the null option.** What happens if you do nothing? Frequently the answer is "we
have 18 months", which changes the decision entirely. The most common error in architecture is
solving a problem you will not have for two years with a technology whose failure modes you will
have next week.

**3. Enumerate what the new thing can break.** Use the thirteen POF classes as a checklist. For
each: does this choice make this class better, worse, or unchanged? A choice that improves `S`
and worsens `D` and `G` is a real trade, and writing it that way makes it discussable.

**4. Cost its worst day, not its normal day.** Every technology is fine when healthy. The
question is what happens during its characteristic failure, who can fix it, and how long it
takes. A datastore your team cannot debug is a datastore whose incidents are open-ended.

**5. Establish the escape route.** How do you get off this if it is wrong? A choice with a
migration path is a much smaller commitment than one without. This question alone eliminates a
lot of otherwise attractive options.

**6. Count the operational surface you are adding.** Each new stateful system is a thing to
patch, back up, monitor, capacity-plan, upgrade, and be paged for. Doc 13's warning applies
generally: **if you cannot operate one well, you cannot operate six.**

## Service granularity — the choice that determines all the others

This belongs first because it dominates. Most of the failure classes in this collection exist
*because* there is a network between two pieces of code, and every one of them is avoided by
there not being one.

| | Monolith | Modular monolith | Services (coarse) | Services (fine) |
|---|---|---|---|---|
| Typical count | 1 | 1 deployable, N modules | 5–30 | 100+ |
| **Eliminates** | All of `R`, `T`, `D`, most of `F` | Same, plus enforced boundaries | Team independence, independent scaling | Maximum independence |
| **Creates** | Deploy coupling; one blast radius; scaling is all-or-nothing | Same as monolith, plus discipline cost | `R`, `T`, `D`, `F`, `I` | All of the above, multiplied; `Q-11` fan-out |
| Transaction across boundaries | **A local transaction** | A local transaction | A saga (doc 07) | A saga, often several |
| Debugging a request | A stack trace | A stack trace | Distributed tracing | Distributed tracing, and often not enough |
| Availability arithmetic | One term | One term | `0.999^5 = 99.5%` | `0.999^30 = 97.0%` |
| Right when | Small team, one domain, early | Medium team, clear domains, deploy coupling is acceptable | Multiple teams with genuinely independent lifecycles | Very large organisations with strong platform support |

**The honest guidance, which is unpopular:** the majority of distributed-systems problems in this
collection are self-inflicted by a service boundary drawn in the wrong place. If two services
must always change together, deploy together, and be transactionally consistent with each other,
**they are one service wearing two costumes**, and merging them eliminates an entire column of
failure modes for free.

The test for whether a boundary is real:

- Can the two sides deploy independently, and do they?
- Can one be down while the other works?
- Do they have different scaling characteristics?
- Are they owned by different teams with different priorities?
- Is there a stable contract between them that does not change every sprint?

**Fewer than three "yes" answers means the boundary is costing you more than it provides.**
Merging services is a legitimate, under-used architectural move, and the fact that it looks like
going backwards is a cultural problem, not a technical one.

## Protocol between services

| | REST / JSON | gRPC / Protobuf | GraphQL | Async messaging |
|---|---|---|---|---|
| **Eliminates** | Nothing; the baseline | Schema drift; manual serialisation; missing deadlines | Over-fetching; client-driven aggregation round trips | Availability coupling (the big one) |
| **Creates** | No deadline standard (`R-02`); loose contracts (`G-10`); verbose | `R-11` (HTTP/2 defeats L4 LB); `R-12` HOL blocking; tooling burden | A single endpoint whose cost is unbounded; N+1 resolvers; caching is hard | `Q` class entirely; ordering and duplication (doc 09) |
| Deadline propagation | **Manual** — you must carry a header and honour it | **Native** (`grpc-timeout`) | Manual | N/A |
| Schema enforcement | Optional (OpenAPI, often stale) | **Compile-time** | Typed, runtime | Registry-dependent |
| Debuggability | Excellent — `curl` works | Poor without tooling | Moderate | Poor — no request to inspect |
| Right when | External APIs, low volume, heterogeneous clients | High-volume internal service-to-service | A client aggregating many backends (BFF) | The caller does not need the result |

The two most consequential rows: **gRPC gives you deadline propagation for free**, which is doc
02's single most valuable mechanism and which REST shops almost never implement; and **gRPC
breaks L4 load balancing**, which is the surprise that catches every team migrating from
HTTP/1.1.

On GraphQL specifically: it moves query composition to the client, which means **the cost of a
request is determined by the caller**. A malicious or careless query can fan out to thousands of
resolver calls. Query depth limits, complexity budgets, and persisted queries are not optional
hardening; they are the thing that makes it safe, and a GraphQL endpoint without them is an
unbounded-cost endpoint (`S-12` with the caller holding the pen).

## Datastore

The largest and least reversible choice. The escape route question matters most here.

| | Relational (PostgreSQL / MySQL) | Wide-column (Cassandra / Scylla) | Document (MongoDB) | KV (DynamoDB) | Search (OpenSearch) | OLAP (ClickHouse / Pinot / Druid) |
|---|---|---|---|---|---|---|
| **Eliminates** | `T` within one database — real transactions; ad-hoc queries; constraints | Write ceilings; `S-02` right-edge contention; single-node limits | Schema migration friction | Capacity planning; operational load | Text and faceted query complexity | Aggregation cost over huge datasets |
| **Creates** | Single-writer ceiling; `S-02`; `S-11` vacuum; connection limits (`R-09`) | **No transactions, no joins**; `S-01` hot partitions; tombstones; repair | Weak multi-document transactions; unbounded document growth | Partition-key rigidity; no joins; hot partitions; cost surprises | Not a source of truth; index rebuild time; split-brain history | Not for point lookups; eventual; ingestion lag (`CD-3`) |
| Write ceiling (single cluster) | ~10–50k/s | **Very high, linear** | Moderate | Very high | Moderate | Very high (append) |
| Read pattern it punishes | Scatter-gather across shards | Anything not on the partition key | Cross-collection joins | Anything not on the key | Point lookups by ID | Point lookups |
| Worst day | Failover with lost writes (`S-07`); wraparound (`S-11`) | Repair storm; a hot partition you cannot reshard | An unindexed query; a document that grew to 16 MB | A hot partition throttling; a bill | A shard that will not recover; an index rebuild | Ingestion stalled; a segment merge storm |
| Escape route | Good — SQL is portable in principle | **Poor** — the data model is the query pattern | Moderate | **Poor** — model and API are proprietary | Good — it is a derived index | Good — it is derived |

**The rule that resolves most of these arguments:** *choose by access pattern, then verify by
write ceiling, and treat anything derived as replaceable.*

- If your dominant access is "everything for one key", a KV or wide-column store is right and a
  relational database will work until it does not.
- If your dominant access is "arbitrary queries over related data", relational is right and
  denormalising into a KV store will cost you more than the write ceiling would have.
- **A search index or an OLAP store is never a source of truth.** It is an artefact rebuildable
  from something else, and if it is not, you have a Corridor `CD-2` waiting to happen.

The Waypoint contrast (doc 19) is the clearest illustration: 750,000 location writes/s on
Cassandra *and* 290 trip writes/s on a relational store, in the same system, because they have
different access patterns and different correctness requirements. **Choosing one datastore for a
system with two workloads is how you get a bad fit for both.**

## Cache

| | In-process (local) | Shared (Redis / Memcached) | CDN | Read replica |
|---|---|---|---|---|
| **Eliminates** | Network latency; shared-cache hot keys (`C-03`) | Per-instance memory duplication; cold start after restart | Origin load for static and semi-static content | Read load on the primary |
| **Creates** | Multi-tier incoherence (`C-13`); memory per instance; cold on every restart | A network hop; a shared failure domain; `C-04` load-bearing risk | Cache-key discipline (`E-06`); invalidation you do not control | Replication lag as a correctness bug (`S-05`) |
| Invalidation | Hard (fan-out to N instances) | Easy (one delete) | Hard and slow (purge APIs) | Automatic |
| Right for | Very hot, small, read-mostly, staleness-tolerant | Shared state, sessions, moderate-size values | Anything cacheable at the edge | Read scaling of queryable data |
| Typical TTL | 1–5 s | 60 s – 1 h | Minutes to days | N/A |

The combination that works, and it is a layering rather than a choice: **local cache with a
1-second TTL in front of a shared cache in front of the origin.** Lumen's `LM-2` derives the
numbers — a 42× reduction from the local tier alone — and the 1-second TTL bounds the
incoherence to something nobody can perceive.

The choice worth being careful about is **Redis as a durable store** (`C-14`). It is a fine
durable store with AOF and replication, and it is a completely different operational posture
from a cache. Run two clusters with opposite configurations rather than one that is ambiguous.

## Messaging

| | Kafka | RabbitMQ | SQS / SNS | Pulsar | NATS JetStream |
|---|---|---|---|---|---|
| **Eliminates** | Replay impossibility; fan-out coupling; ordering loss (per key) | Complex routing logic in applications | **Operational burden entirely** | Kafka's partition/consumer coupling | Latency and operational weight |
| **Creates** | Partition-count ceiling on consumers (`Q-03`); operational weight; rebalances | No replay; queue-depth memory pressure; clustering complexity | No ordering (standard); no replay; visibility-timeout semantics; per-message cost | Two systems to operate (brokers + BookKeeper) | Less mature ecosystem; storage semantics |
| Ordering | Per partition | Per queue, single consumer | FIFO queues only, throughput-limited | Per key, with more flexibility | Per subject |
| Replay | **Yes** — a core feature | No | No | Yes | Yes, bounded |
| Consumer scaling | Capped by partitions | Competing consumers, uncapped | Uncapped | Decoupled from partitions | Uncapped |
| Operational cost | **High** | Moderate | **None** | High | Low |
| Right when | High volume, replay needed, multiple independent consumers | Complex routing, moderate volume, work queues | You want to not operate a broker | Kafka's semantics with better consumer scaling | Low latency, simple needs, edge |

**The under-considered option is SQS/SNS**, and the reason is doc 12's operational-surface
argument. Kafka is the right answer for Lumen and Corridor, where replay and fan-out at millions
of events per second are the product. For Riverbend at 640 events/s across 11 consumers, a
managed queue removes an entire stateful system from the estate at the cost of replay — and
Riverbend's outbox (doc 07) means the events are reconstructible from the database anyway, which
is the replay you actually need.

**Ask: do you need replay, or do you need "we can rebuild this from the source of truth"?** They
are not the same requirement, and the second one is usually cheaper.

## Coordination

| | etcd / ZooKeeper / Consul | A database row | Redis lock | None (partition or idempotency) |
|---|---|---|---|---|
| **Eliminates** | Split brain (with fencing); leader ambiguity | Same, with no extra system | Coarse contention, cheaply | **The problem itself** |
| **Creates** | A consensus system to operate (`D-13`); quorum loss; disk-latency sensitivity | Load on a database; lock hold time in a transaction | **No correctness guarantee** (`L-01`) | Design constraints |
| Correctness | Strong, with fencing | Strong, and free | **Best-effort only** | Strong by construction |
| Operational cost | High | None | Low | None |
| Right when | Leader election for stateful systems; genuine consensus needs | A singleton job, an exclusive assignment | An optimisation where duplicates are tolerable | **Almost always — try this first** |

Doc 10's decision tree in one line: **idempotency beats partitioning beats a database
conditional update beats a lock service**, and every step down that list adds a dependency. The
conditional-update-on-a-row pattern —

```sql
UPDATE jobs SET last_run = now() WHERE name = $1 AND last_run < now() - interval '1 hour';
-- one caller gets a row count of 1; everyone else gets 0
```

— replaces a lock service for most singleton-work cases, is correct, and costs nothing.

## Workflow and saga orchestration

| | Hand-rolled state machine | A workflow engine (Temporal / Cadence) | Step Functions / managed | Choreography (events only) |
|---|---|---|---|---|
| **Eliminates** | Nothing beyond what you build | Durable execution, retries, timeouts, compensation, visibility — all of `T-09` | Same, managed | Central coordination |
| **Creates** | A six-month project that looks like a two-week one | A platform to operate; a programming model to learn | Vendor lock-in; execution limits; cost per transition | **No visibility** (`T-08`); compensation has no owner |
| Process state queryable | Only what you build | **Yes, natively** | Yes | **No** |
| Right when | The flow is 2–3 steps and genuinely simple | Multi-step flows with compensations and deadlines, more than a handful of them | The same, on a managed platform, at moderate volume | Pure notification fan-out |

The honest assessment: **hand-rolling durable saga execution is one of the most commonly
underestimated pieces of work in this collection.** The visible part — a state column and a
sweeper — is two weeks. The rest — resumability, exactly-once step execution, timeout handling,
compensation ordering, versioning of in-flight workflows, and a UI to see what is stuck — is
where the six months goes. If you have more than two or three such flows, buy it.

## Compute and orchestration

| | Kubernetes | Managed containers (ECS / Cloud Run) | Serverless (Lambda) | VMs |
|---|---|---|---|---|
| **Eliminates** | Manual placement; bespoke deploy tooling | Cluster operation | Capacity planning; scaling; patching | Orchestrator complexity |
| **Creates** | `N-06` CFS throttling; `N-07` OOM; `N-09` node limits; `D-03` propagation; a large operational surface | Less control over placement and networking | Cold starts (`N-05`); execution limits; **connection-pool explosion** (`R-09` — one pool per concurrent invocation); vendor coupling | Slow scaling; manual everything |
| Failure domain | Node, zone, cluster | Task, zone | Invocation | Instance |
| Right when | Many services, a platform team, need for control | Fewer services, want most of the benefit without the cluster | Spiky, stateless, short-lived work | Legacy, specialised hardware, or genuinely simple |

The serverless row that catches people: **each concurrent invocation is a separate process with
its own connection pool.** 1,000 concurrent Lambdas × 5 connections each = 5,000 database
connections against a `max_connections` of 600. This is `R-09` in its most surprising form, and
the fix — a connection proxy, mandatory — is the same one.

## Deployment tooling

Covered in doc 11; the summary of what to buy:

| Capability | Why it is non-negotiable | Minimum viable |
|---|---|---|
| Progressive rollout with health gating | Bounds the blast radius of the most common failure cause | `maxUnavailable: 0` + `minReadySeconds` + an abort on error rate |
| Automated rollback | The window between "bad" and "reverted" should be measured by a machine | Argo Rollouts, Flagger, or a CI job watching a metric |
| Config as code with staged rollout | Config has deploy blast radius and less friction (`G-03`) | Git + CI validation + scoped application |
| Feature flags, fail-static | Separates deploy from release; instant undo | Any flag service, with local caching and compiled-in defaults |
| A measured rollback time | It is the number that determines incident length | A stopwatch and a runbook entry |

## Observability

| | Metrics (Prometheus-shaped) | Logs | Traces | Profiles |
|---|---|---|---|---|
| Answers | "Is it broken, and how much?" | "What exactly happened to this one request?" | "Where did the time go across services?" | "What is this process actually doing?" |
| Cost driver | **Cardinality** — series count | Volume | Sampling rate | Sampling rate |
| Fails when | High-cardinality labels explode the series count | Volume exceeds budget and sampling drops the errors | Head-based sampling at 1% misses every failure | Rarely used, so rarely working |
| Non-negotiable | Yes | Yes | **Yes** for more than ~10 services | No, but transformative for `N` and `F` |

The choice that matters most is **tail-based sampling for traces** (doc 14). It costs more
collector capacity and it is the difference between having traces for incidents and having traces
for the boring case.

The trap is metric cardinality: a label with per-user or per-request-ID values multiplies your
series count by millions and takes down the metrics system, which then blinds you across every
service at once. See [`../Observability/05-instrumentation-and-cardinality.md`](../Observability/05-instrumentation-and-cardinality.md).

## Mapping the stack to the system shape

Pulling the six case studies together. **The same concern has different right answers depending
on what the system does**, which is the core claim of this doc.

| Concern | Riverbend (correctness) | Lumen (read fan-out) | Gateline (step load) | Waypoint (dual philosophy) | Corridor (derived data) | Northlight (platform) |
|---|---|---|---|---|---|---|
| **Primary store** | Relational, sharded | Wide-column | Relational (seats are financial) | **Both** — in-memory + wide-column *and* relational | Relational OLTP + versioned derived stores | N/A |
| **Cache** | Shared + local; price contract, not TTL | Local (1 s) + shared + CDN; never expire hot keys | Shared, 2 s TTL; **pre-warmed** | The in-memory index *is* the cache | Heavy; derived stores are the cache | N/A |
| **Messaging** | Managed queue would do; Kafka for the outbox | Kafka — replay and fan-out are the product | Modest; post-purchase only | Kafka — one stream, two service levels | Kafka at 3.5M/s — the backbone | N/A |
| **Coordination** | Conditional updates; no lock service | Almost none | **Per-seat queues** — genuine contention | Conditional update on the driver row | Almost none | Sharded control plane |
| **Consistency** | Strong on money and stock | Eventual everywhere | **Strong on seats**, eventual elsewhere | **Eventual on location, strong on trips** | Eventual, with freshness SLOs | N/A |
| **Isolation** | Cells at 10× | Cells by user | **Cells per event** | **Cells per region/city** — free from the domain | Regional online; global offline | Control-plane shards |
| **Scaling** | HPA on concurrency | HPA on concurrency; pre-scale for campaigns | **Pre-provisioned; autoscaling disabled** | HPA; headroom for zone loss at rush hour | Offline capacity scheduling | Control plane for the failure case |
| **Degradation** | Two states on checkout; rich on browse | **Nine-level ladder** | Very short ladder; shed at the front door | Location degrades; trips do not | Fall back to a simpler model | Fail-static |
| **The defining defence** | Idempotency + reconciliation | Local caches + fan-out thresholds | **The waiting room** | Separating the two philosophies | **Freshness SLOs on artefacts** | Control-plane sharding |

## Choices that look good and create failure points

A catalogue of attractive decisions with delayed costs.

**"We'll use the same database for everything."** One PostgreSQL for orders, sessions, events,
search, and analytics. Simple, and it makes every workload a noisy neighbour to every other
(`S-16`), couples their availability, and means the analytics query that scans a large table
evicts the transactional working set.

**"Microservices from day one."** A four-person team with fourteen services has fourteen
deployment pipelines, fourteen on-call surfaces, distributed transactions between things that
should be function calls, and `0.999^14 = 98.6%` availability. Start with a modular monolith and
extract when a boundary proves itself.

**"We'll make it event-driven so it's decoupled."** Events decouple *availability*, not
*understanding*. A choreographed flow across eight services has no owner, no queryable state, and
no compensation path (`T-08`). Use events for notification; orchestrate anything with an outcome.

**"Retries everywhere, for resilience."** Doc 02's `a^n`, and duplicates on every non-idempotent
endpoint. Retries without budgets and idempotency make systems less reliable, not more.

**"Just add a cache."** You have added a correctness surface (`C-08`, `C-09`), a load-bearing
dependency (`C-04`), and a cold-start cliff (`C-05`). Caches are right, and they are a tier of
the system, not a decoration.

**"We'll scale to zero to save money."** Cold starts on the user-facing path, connection storms
on wake, and a system whose first request after quiet always fails. Fine for internal tools.

**"Let's use the newest datastore, it benchmarks well."** Benchmarks measure the good day. The
question is the bad day and who on your team can debug it at 3 a.m. A boring datastore your team
knows beats a better one they do not.

**"We'll add a service mesh to fix our reliability."** A mesh amplifies the discipline you have
(doc 21). Without progressive deployment, shedding, and observability first, it adds a global
control plane to a system that could not operate the simple version.

**"Multi-region for availability."** Most outages are bad deploys and bad configs, which
multi-region does not help with and can make worse (a bad deploy reaching both regions). Cells
within one region usually buy more availability per dollar (doc 13).

**"We'll write our own X."** Occasionally correct. The reliable signal: if the thing you are
writing has a name — a service mesh, a workflow engine, a message broker, a feature-flag service
— the existing implementations encode failure modes you have not thought of yet, and you will
discover them in production.

## The order to do things in

If you are improving an existing system, this ordering gives the most reliability per unit of
effort. It is the same list as doc 13's and it is worth repeating here because the technology
choices above should be made *against* it, not instead of it.

1. **Progressive deployment with automated rollback** (doc 11). The most common cause, bounded.
2. **Timeouts derived from p99, retry budgets, and bulkheads** (docs 02, 03). The most common
   amplifier, bounded.
3. **The async monitoring set — age of oldest, DLQ depth, drain rate** (doc 09). The longest
   outages, made visible.
4. **Reconciliation on every pair of systems that must agree** (doc 07). The only detector for
   correctness failures.
5. **`N − k` headroom and load shedding** (docs 03, 12). Overload, bounded.
6. **Freshness SLOs on every derived artefact** (doc 20). Silent staleness, made visible.
7. **Cells** (doc 13). Everything else, bounded.
8. **A second region.** Latency, residency, and genuine disaster recovery — a different argument.

Items 1–4 are mostly configuration and discipline and cost very little. Items 7–8 are projects.
**Most organisations attempt them in roughly the reverse order.**

## What to take away

1. **Every technology choice trades one failure profile for another.** Ask what it makes
   impossible, what can now break, what its worst day costs, and what the escape route is.
2. **Establish the null option first.** Solving a problem you will not have for two years with a
   technology whose failure modes you will have next week is the most common architecture error.
3. **Service granularity dominates every other choice**, because most of this collection's
   failure classes exist only because there is a network between two pieces of code. Fewer than
   three "yes" answers to the boundary test means merging is the right move.
4. **gRPC gives you deadline propagation for free and breaks L4 load balancing.** Both are
   large; the second surprises everyone.
5. **A GraphQL endpoint without depth limits, complexity budgets, and persisted queries is an
   unbounded-cost endpoint with the caller holding the pen.**
6. **Choose a datastore by access pattern, verify by write ceiling, and treat anything derived as
   replaceable.** A search index or an OLAP store is never a source of truth. Two workloads with
   different access patterns need two stores — one store for both fits neither.
7. **Layer caches rather than choosing one**: local at 1 s, shared, CDN. And keep "cache Redis"
   and "data Redis" in separate clusters with opposite configurations.
8. **Ask whether you need replay or just "rebuildable from the source of truth."** They are
   different requirements and the second is usually cheaper — which makes a managed queue a
   better fit than Kafka for a lot of systems that chose Kafka.
9. **Idempotency beats partitioning beats a conditional database update beats a lock service.**
   Every step down that list adds a dependency; the conditional update replaces a lock service
   for most singleton work and costs nothing.
10. **Hand-rolling durable saga execution is a six-month project that looks like a two-week one.**
    Past two or three such flows, buy it.
11. **Each concurrent serverless invocation has its own connection pool.** A connection proxy is
    mandatory, not optional.
12. **Tail-based trace sampling is the difference between having traces when it matters and
    having them for the boring case.** And metric cardinality is the thing that takes down the
    observability stack and blinds you everywhere at once.
13. **The same concern has different right answers for different system shapes.** Gateline
    disables autoscaling; Lumen depends on it. Waypoint runs two consistency models in one
    request. There is no stack, only a stack for a workload.
14. **A boring technology your team can debug at 3 a.m. beats a better one they cannot.**
    Benchmarks measure the good day.
15. **Do the cheap things first**: progressive deployment, timeouts and budgets, async
    monitoring, reconciliation. They cost configuration and discipline. Cells and second regions
    are projects, and most organisations attempt them in the wrong order.

Next: [23-staff-interview-questions.md](23-staff-interview-questions.md), a question bank that
uses everything in this collection, with model answers and the follow-ups a strong answer
invites.
