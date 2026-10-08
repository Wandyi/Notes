# Staff-Level Interview Questions

Eighteen questions with model answers and the follow-ups a strong answer invites. They cite the
scenario IDs from the rest of the collection rather than re-deriving, so a thin answer here is a
pointer to the doc that fills it in.

These work in both directions. As an interviewer, the follow-ups are where the signal is — the
opening question is usually answerable from reading. As a candidate, the pattern worth noticing
is that **nearly every strong answer starts by making the question precise**, and several of the
questions are unanswerable as asked.

---

## Part 1 — The design question

### Q1. Design a log aggregation system that handles 10 TB/hr from thousands of services, supporting real-time viewing, regex search, and a daily bulk export for analytics.

The brief this whole collection is built on. A staff-level answer has five moves, and the order
matters more than the content.

**Move 1 — make the requirement precise before designing anything.** Five ambiguities, from
doc 01: is 10 TB average or peak; raw or compressed; what does "real time" mean numerically;
what scope may the regex cover; and what does "the last 48 hours" mean when records arrive
late. **Declaring the assumptions is the single strongest signal in this question**, because a
candidate who starts drawing boxes has not noticed that the answer changes by an order of
magnitude depending on the first two.

**Move 2 — the numbers, derived.**

```
10 TB/hr = 2.78 GB/s = 5.56M records/s at ~500 B
peak 1.7× diurnal = 4.73 GB/s
12,000 hosts, p50 18 KB/s, p99 4.1 MB/s
compression: LZ4 2.2:1 on the wire, Parquet + zstd 11:1 at rest
storage: 253 MB/s = 21.8 TB/day
```

**Move 3 — the seven stages, and the fork into three flows.** Emit, collect, transport, buffer,
process, store, serve — with the buffer as the point where three consumer paths diverge, because
live tail, search and export want incompatible things (doc 00's table).

**Move 4 — the two hard parts, named as hard.**

*The regex requirement is unbuildable as stated.* Brute force over 48 hours is 480 TB, at
250 MB/s per core, which is 192,000 cores for a ten-second answer. Say this. Then give the
buildable version: bound the scope, extract literals from the pattern, prune with n-gram Bloom
filters, verify with RE2, estimate cost before running, and report partial results honestly.

*The tar file is the wrong interface.* 694 GB per day per tenant, not reproducible, no schema
contract, duplicating data that already exists in object storage. Offer a shared Iceberg table
first, and build the tar properly if it is genuinely required.

**Move 5 — volume control, and where it sits in priority.** 65% of the volume is
machine-generated data nobody reads individually. Aggregating successful access logs alone
removes 44% of ingest. **A candidate who gets to this before being prompted is thinking about
the actual problem**, because it is worth more than every other optimisation combined
(doc 08).

**Follow-ups worth asking:**
- *Why a buffer between the gateway and storage?* → doc 06's four reasons: decoupling, fan-out,
  replay, absorption. A candidate who says "durability" has only the weakest one.
- *What happens when an agent cannot keep up?* → the four options, and the fact that none is
  both lossless and non-blocking.
- *How do you know you are not losing data?* → doc 15. If the answer is "we'd see errors",
  press: there is no acknowledgement path back to the application.

---

## Part 2 — Collection and transport

### Q2. An agent is restarted. Does it duplicate or lose data, and which do you want?

**You want duplicates.** The agent advances its checkpoint only after downstream acknowledgement
(`A-02`), so a restart replays the window between the last checkpoint and the crash — at a 5
second interval and 4.1 MB/s, about 20 MB per host.

The alternative, checkpointing on send, gives `A-03`: silent loss, unrecoverable and
undetectable.

Bound the duplicates with a deterministic identity — `xxhash64(host_id, inode, byte_offset)` —
and deduplicate downstream (doc 07).

**Follow-up:** *How do you test which one you have?* → `kill -9` under load and reconcile
counts. Gaps mean premature acknowledgement. Most teams have never run this.

### Q3. Your log agent is OOMKilled during a traffic spike. Walk me through it.

`A-07`. Volume spikes, the memory buffer fills because downstream is also under pressure, the
container exceeds its limit, the kernel kills it, the buffer is discarded, it restarts into a
still-full downstream, and it is killed again.

**The key insight is that the failure occurs precisely when the agent is most needed.** So:

- An internal buffer cap that triggers disk spill well below the container limit, so the agent
  controls its own degradation rather than the kernel doing it abruptly.
- The Kubernetes limit at ~2× the internal cap, as a backstop that should never fire.
- Load-test at 30× normal input — doc 01's worst observed spike — because this failure only
  appears under conditions nobody tests.
- Level-aware shedding so the 5% you lose is `DEBUG`, not your only `ERROR`.

**Follow-up:** *Why not just raise the memory limit?* → It moves the ceiling without changing
the shape. The spike is 31×; no limit accommodates that. The fix is degradation you control.

### Q4. You have 12,000 agents and 90 gateway pods behind a load balancer. Some gateway pods are at 90% CPU and others at 5%. Why?

`T-01`. gRPC multiplexes over one long-lived HTTP/2 connection; an L4 balancer distributes
connections, and each agent has one. Connections established at different times accumulate on
whichever pods were available, and nothing ever moves an established connection — so after a few
rolling restarts the distribution is badly skewed.

Fixes: an L7 proxy that balances streams rather than connections, plus `MAX_CONNECTION_AGE` with
**jitter**.

**Follow-up, and this is the real question:** *Why does the jitter matter?* → Without it every
connection established during a deploy expires in the same second, so you have scheduled a
reconnect storm (`T-02`). Worse than the handshake cost is that it synchronises 1,330 agents'
batch timers, converting smooth traffic into periodic spikes that persist for hours.

### Q5. Your gateway was down for 30 seconds. It comes back and immediately falls over. Explain, and fix it.

`T-03`, and it is a metastable failure — removing the trigger does not end it.

```
30 s × 2.78 GB/s = 83 GB buffered across 12,000 agents
flushed at maximum agent speed against a tier sized for 4.73 GB/s
= 17.5 s of pure backlog before any new data is accepted
→ agents time out, retry, and add load
```

**Two fixes, and the second is the one people miss.**

*Shed explicitly.* Reject with `RESOURCE_EXHAUSTED` and a `retry-after` that agents honour.
Counter-intuitively, rejecting more makes recovery faster, because a rejected client backs off
and a timing-out client retries.

*Rate-limit recovery, not just steady state.* An agent flushing a backlog must not exceed about
1.5× its normal send rate. One setting, and it converts a 17.5-second stampede into a
60-second catch-up. **Almost no agent does this by default**, and it is the highest-value
configuration change in the transport tier.

---

## Part 3 — Buffering and storage

### Q6. How many Kafka brokers do you need for 10 TB/hr, and what is the binding constraint?

The interesting part is the last clause. Four candidate constraints, with 0.77 GB/s stored after
gateway compression:

```
disk write:  2.31 GB/s ÷ 550 MB/s per broker              = 4.2 brokers
network:     7.70 GB/s total ÷ 15 Gbps usable             = 4.1 brokers
retention:   49.9 TB ÷ 12 TB usable                       = 4.2 brokers
page cache:  150 GB ÷ (2.31 GB/s ÷ N) ≥ 1,800 s           = 27.7 brokers
```

**The first three all say four. Page-cache residency says twenty-eight.** You size a log Kafka
cluster on memory, because the workload is several consumers repeatedly reading recent data, and
the difference between serving that from page cache and from disk is two orders of magnitude in
cost.

**Follow-up:** *What happens when a consumer falls outside that window?* → `B-04`. Its disk
reads evict the recent pages every *other* consumer needs, so one consumer's problem degrades
the whole cluster. Broker disk read throughput is the cleanest alert in the tier, because its
healthy value is approximately zero.

**Second follow-up:** *So do you scale that consumer up to catch up faster?* → No. More parallel
cold readers evict more aggressively. Throttle it, or have it skip forward and backfill from
object storage.

### Q7. Should you create one Kafka topic per service?

No, and the reasoning is not throughput.

```
8,000 services × 4 partitions × RF 3 = 96,000 replicas ÷ 24 brokers = 4,000 per broker
```

Four thousand partition replicas per broker means memory-mapped index pages competing with the
page cache — attacking the exact number that governs the tier — plus slow controller failover,
random rather than sequential writes, and a partition count you can never reduce.

**Topic by consumer class instead** — application, access, client, infrastructure, batch, audit,
dead-letter. Seven topics, 300 partitions, 38 replicas per broker. It separates data with
different consumers, different retention and different shedding priority, and it cuts live
tail's read amplification from 63× to 13× because the tail router reads only `logs.app`.

**Follow-up:** *What is the partition key?* → The finest unit that must stay ordered — the pod
UID, not the service (one access-log service is 31% of bytes, `B-02`) and not the tenant. Plus a
per-pod ceiling above which you split the stream with a suffix, deliberately giving up ordering
for a pod that has already given up on it.

### Q8. Why is sorting the data inside your Parquet files the highest-return storage optimisation?

Because it is what makes the free index useful.

Parquet writes min/max statistics per column per row group whether you sort or not. The
statistics cost 0.02% and the pruning they enable differs completely:

```
sorted by service:    one service occupies contiguous row groups
                      → a single-service query skips 87.5% of the file
unsorted:             every row group's range spans all services
                      → skips 0%
```

**Same index, same cost, and nothing reports that it is useless.** This is `W-02`, and the
diagnostic is `bytes_scanned ÷ bytes_returned` per query — in the thousands when pruning works,
in the tens when it does not.

**Follow-up:** *Why not partition by service instead?* → 24 hours × 40 tenants × 5 classes ×
12,400 services = 60 million directories a day. Partition on what *every* query filters; sort
on what *most* queries filter.

---

## Part 4 — Indexing and query

### Q9. Justify building — or not building — an inverted index over log message bodies.

Frame it as a bet: you pay on 100% of data at write time to save reading some fraction at read
time. Logs are written about twelve times for every record read, so most index bets lose.

The inverted index specifically:

```
storage:      ~13.6× the columnar approach for the same window
indexing CPU: 430 cores at peak vs 56 for a columnar writer
```

What it buys is genuine full-text search — relevance, phrases, stemming — which nothing else
provides.

**The answer is: almost certainly not, and here is the alternative.** N-gram Bloom filters on
the body column at 6.8% overhead give substring search, and the query language rejects patterns
they cannot serve rather than attempting them.

**Follow-up:** *Why is a 10% false-positive rate acceptable on an n-gram filter?* → Because a
query conjoins many n-grams. An 11-trigram search term passes spuriously with probability
`0.08^11 ≈ 10^-12`. **The looseness per trigram is what makes the filter cheap, and the
conjunction is what makes it selective.**

**Third:** *So what breaks?* → Short search terms. A 3-character term is one trigram and gets
only the raw rate of pruning. Reject searches under n+1 characters.

### Q10. A user submits `/^[a-f0-9]{32}$/` over the last 7 days, all services. What happens?

**Nothing should happen until you have told them what it would cost.**

The planner walks the regex parse tree extracting mandatory literals (doc 11). A character
class with `{32}` produces none — the query is unindexable, and the planner knows that before
executing anything.

```
7 days, all services: ~150 TB after partition pruning alone
at 250 MB/s per core: 600,000 core-seconds
```

So: reject at admission with the estimate, the reason, and concrete alternatives —
"add `service:` → 5.1 TB; narrow to 4 hours → 3.4 TB; search for a literal instead of a
character class".

**That screen is worth more to platform health than any optimisation**, because it converts an
accidental cluster-wide event into a decision and teaches the user how the system works.

**Follow-up:** *What if they say "run it anyway"?* → Allow it, on the export queue with its own
resource pool and a cost charged to their team. The point is informed choice, not prohibition.

**Second follow-up:** *What if they use `(a+)+b` instead?* → `Q-04`. A backtracking engine takes
exponential time and one user pins every worker in the cluster. Use RE2, which guarantees linear
time; the lost features (backreferences, lookaround) are almost never needed for logs.

### Q11. Your search returns no results. Name five reasons and how the interface should distinguish them.

Doc 13's list: it did not happen; it happened outside the time range; the field name is wrong;
it was sampled away; the pipeline lost it.

**The default assumption — the first one — is wrong more than half the time**, and an empty
screen sends the engineer to debug the wrong thing.

Four cheap hints that eliminate four of the five: a field-name suggestion computed from fields
actually present for that service; a count for the same query with the failing filter dropped;
the effective sampling rate for that service and level; and the measured pipeline health for the
window.

**Follow-up:** *Which of those is hardest?* → Pipeline health for a specific window, because it
requires doc 15's canary and sequence-gap data to be queryable by time and tenant. Worth
building; it is the one that stops "the platform lost it" being an unanswerable accusation.

---

## Part 5 — Correctness

### Q12. How do you measure log loss?

Start by making the question precise: **loss means "emitted and not retrievable"**, which
includes stored-but-unfindable. Any measurement that stops at the storage write is measuring
something easier and less useful.

Three techniques, none sufficient alone:

- **Stage counters** that must reconcile. Cheap, continuous, localises to a stage. Blind to
  everything before the agent and to the query path.
- **Per-stream sequence numbers.** Exact, localises to a source, detects duplicates and
  reordering for free. Costs 4.9% of raw bytes and under 0.3% stored, because both columns
  compress almost perfectly.
- **Canary records** queried back through the normal search path. The only one that measures
  the whole chain including the query layer. 0.21% of capacity.

**Their disagreement is diagnostic.** Canary worse than sequence means the query path; sequence
worse than counters means concentrated rather than uniform loss.

**Follow-up:** *Your canaries report 99.999% and users say logs are missing.* → `D-05`. The
canary is not representative — single-line, small, default pipeline, one tenant. Run variants:
multiline, oversized, high-field-count, and one per tenant through that tenant's actual rules.

### Q13. Someone asks for a 99.999% delivery guarantee. Respond.

Push back with the specific reason, not a general one.

```
99.99%  achievable, and where the design sits
99.999% requires eliminating agent-restart loss (synchronous checkpointing: −30% agent
        throughput), dedup false positives (exact structures: 5× memory), and
        application-side appender drops
```

**The last item disqualifies it.** Eliminating appender drops means blocking the application
when logging is slow — trading a logging problem for a service outage. That is the wrong trade
at any loss rate.

The constructive answer: **route the data that genuinely needs a stronger guarantee onto a
different path.** Audit and financial records are 0.07% of volume; give them a separate topic,
no sampling, no shedding, longer retention, and their own consumer. A different guarantee for
0.07% of the volume is cheap; a better number on 100% of it is not.

### Q14. Your archiver stopped four hours ago. Kafka retention is six hours. What do you do?

**Buy time first, diagnose second.** The distinguishing feature of this question is that
diagnosis is the wrong first move.

```
1. time_until_unrecoverable = 6h − 4h = 2 hours. That sets everything.
2. Extend Kafka retention — dynamic config, effective on the next segment roll.
   Check broker disk headroom first (B-05).
3. If disk cannot absorb it: start the raw-dump consumer — a minimal job writing raw
   Kafka batches to object storage with no processing. Ugly, deployable in minutes,
   fully recoverable later.
4. Now diagnose the archiver.
5. Catch up slowly. ⚠️ Do not scale it aggressively — cold reads evict the page cache
   and degrade the whole cluster.
```

**Follow-up:** *Why didn't anyone notice for four hours?* → Because nothing user-visible broke.
Search was fine, tail was fine, dashboards were green. **The archiver is the consumer whose
failure is silent and irreversible**, which is why you alert on `time_until_unrecoverable`
rather than on lag.

**Second follow-up:** *Is the raw-dump consumer something you'd write during the incident?* →
No. It is the highest-value emergency tool in the platform and it must exist beforehand.

---

## Part 6 — Cost, tenancy, and judgement

### Q15. Your log platform costs $300,000 a month. Where does it go, and what would you cut?

The distribution is the surprising part:

```
cross-zone network      $106,272   35%   ← the line nobody puts in their design
hot tier + query         $64,000   21%
storage (warm + cold)    $48,217   16%
Kafka                    $38,400   13%
processing + gateway     $29,380   10%
agents + compaction      $15,011    5%
```

**Cut volume, not cost.** At about $29,400 per TB/hr per month, an 82.5% reduction saves
$242,000 a month — more than every engineering optimisation combined. The hierarchy: do not
emit, do not collect, aggregate, sample, tier.

The largest single item is aggregating successful access logs: 44% of ingest, for the loss of
individually retrievable successful requests. Keep a 0.5% sample and the trade becomes easy.

**Follow-ups:**
- *What is the quickest win on the network line?* → Confirm follower fetching (KIP-392) is
  enabled and that clients set `client.rack`; without it, cross-zone consumer reads add roughly
  $200,000 a month and it fails silently. Then compress harder at the gateway: zstd-3 instead of
  zstd-1 saves $23,000 for twelve cores.
- *Why is that a good example of something?* → A local optimisation (zstd-1 for gateway latency)
  was the global wrong answer because the network cost was not on the same page as the latency
  budget.

### Q16. How do you stop one team's incident from degrading the platform for everyone?

Graded quotas, and the grading is the whole answer.

A naive quota drops everything above a threshold, which during an incident discards exactly the
records that explain it — and that is why operators disable quota systems. Instead:

```
100–150%  accept, notify, bill the overage
150–300%  drop TRACE and DEBUG, sample INFO, keep WARN and above whole
>300%     drop below WARN, page the tenant
absolute ceiling at 5× committed:
          sample even ERROR, but guarantee one record per distinct
          (service, error.code) per second
```

**That guarantee is the key idea.** During a 31× spike the tenant is not producing more
information, just more copies of the same few errors. One of each per second preserves
essentially all the diagnostic value at 0.4% of the volume.

Plus bulkheads, not just rate limits — cap in-flight gateway requests and concurrent query
workers per tenant, because a few enormous operations starve everyone without exceeding any
rate.

**Follow-up:** *What if every tenant hits their limit at once?* → The platform cannot absorb it,
and that should be written down rather than discovered. Sum of hard limits is 15 TB/hr against
17 TB/hr capacity, which is safe against any *one* tenant's emergency and not against all of
them. The mitigation for the correlated case is platform-level shedding, dropping `logs.access`
globally first.

### Q17. A team logs the full request body on error, and one of their endpoints handles payments. How do you handle it?

Three layers, and the third is the one that determines whether the fix sticks.

**Immediate:** credit-card patterns with Luhn validation at the gateway, before durability, with
alerting — a detected card number is an incident, not a redaction. **The Luhn check is what
makes the rule usable**: the bare digit pattern matches 1.4% of all records (order IDs, trace
IDs, nanosecond timestamps), and with Luhn it matches 0.003%. A 467× false-positive reduction,
and without it the rule damages data and gets disabled.

**Structural:** a platform rule that request and response bodies are never logged in production
for regulated endpoints, enforced in the shared HTTP middleware rather than by policy.

**The one that matters:** give them a sanctioned alternative. A short-lived, access-controlled,
separately stored body capture they can enable for ten minutes. **Most policy violations in
logging are someone meeting a real need the sanctioned path did not serve**, and providing the
path is more effective than forbidding the violation.

**Follow-up:** *The data is already in Kafka and in storage. Now what?* → Rotate before deleting
— deleting the log does not un-expose anything, and the time spent is time the exposure
continues. Then purge across Kafka (6 h), hot (48 h), warm (30 d) and cold (13 mo). **Write that
purge tooling before you need it.**

### Q18. You are six months into running this platform. Engineers still use `kubectl logs`. Why, and what do you do?

The question is about judgement rather than mechanism, and the strong answer resists the urge to
blame the users.

Five likely causes, each with a distinct fix:

1. **Latency.** Live tail is slower than `kubectl logs`. Check the budget: the multiline flush
   timeout is usually the largest term (doc 12), and it can be reduced per source.
2. **No CLI.** If tail only exists in a browser, it does not compose with `grep`, `jq` or
   `tee`, and people go back to the terminal. Doc 12 argues the CLI matters more than the UI.
3. **Search is awkward.** If "errors from one service, recently" takes more than about thirty
   characters, casual use never becomes habit, and the habit is what makes the platform reached
   for during an incident.
4. **It is not trusted.** Someone searched during an incident, got nothing, and concluded the
   platform loses data. Often they were right (`Q-11`, `V-02`) and nothing told them. Publish
   the completeness SLI and show sampling rates in results.
5. **Sampling surprised them.** They looked for a specific request and it was not there.

**What to do first: instrument it.** You have query logs (doc 13). Compare the set of services
people search against the set they run, and interview the gaps. **The failure is diagnosable
from data you already have, and the most common answer is number 4** — a single bad experience
during an incident costs more trust than months of good ones rebuild.

---

## What a strong answer looks like across all eighteen

Five patterns, and they are the transferable part.

1. **Make the question precise first.** Q1's five ambiguities, Q12's definition of loss, Q13's
   reframing. Several of these questions are unanswerable as asked, and noticing that is the
   signal.
2. **Derive the number rather than asserting it.** 192,000 cores, 28 brokers, 467× — each comes
   from two or three visible steps, and the steps are what make the conclusion checkable.
3. **Name the binding constraint.** Q6's page cache, Q8's sort order, Q15's network line. In
   each case the obvious constraint was not the real one.
4. **Say what the design costs.** Duplicates instead of loss, 0.5% sampled access logs instead
   of all of them, no full-text search. An answer with no stated cost has not finished.
5. **Reach for the boundary over the convention.** Separate paths rather than priority fields,
   path-scoping rather than filters, price signals rather than policies. This recurs in nearly
   every answer, and it is the collection's strongest single idea.

Next: [24-golden-defaults-and-checklists.md](24-golden-defaults-and-checklists.md).
