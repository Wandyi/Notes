# Staff-Level Interview Questions

Twenty questions, each with a model answer, the follow-ups a strong answer invites, and what
distinguishes a strong answer from a merely complete one.

They are written for both sides of the table. If you are being interviewed, the model answers
show the shape; if you are interviewing, the follow-ups are where the signal is.

A note on how to use them: **the model answers are longer than you should speak.** Say the first
two sentences, then stop and let the interviewer pick a direction. A candidate who delivers four
minutes of uninterrupted answer is demonstrating preparation rather than judgement.

---

## Q1 — Design a platform that ingests 4 million events a second and serves real-time dashboards, a feature store, and an analytics lake.

**The shape of a strong answer.** Do not start drawing boxes. Start by resolving the ambiguities
from [01](01-requirements-and-the-capacity-model.md) Section 1:

- What is an event, and is 4 million the average or the peak?
- What latency does each of the three consumers need? (They differ by three orders of
  magnitude, and that difference is the design.)
- How far back must this be replayable?
- What guarantee, per stream?

Then the capacity model, out loud: 4M/s × 400 B = 1.6 GB/s, ÷ 3.2 compression = 500 MB/s onto
the log, plus derived streams. Then the tiers.

Then — and this is the part that separates answers — **name the binding constraint**. For a
platform at this scale it is page-cache residency on the brokers and cross-zone network on the
bill, and saying so early shows you have built one rather than read about one.

**Follow-ups a strong answer invites.** "Why 48 brokers when the bytes need 12?" "What happens
when the analytics consumer starts a full replay?" "Which of the three sinks determines your
delivery guarantee?"

**What distinguishes it.** A complete answer has all the boxes. A strong one has a number on
every arrow and can say which number it is least confident about.

---

## Q2 — What is a watermark, and how do you choose its delay?

**Model answer.** A watermark is an assertion carried in the stream: "no record with event time
earlier than *T* will arrive after this point." It is a belief, not a fact — records earlier
than *T* do arrive, and they are called late.

The delay is chosen by measuring `ingest_time − event_time` for the actual stream and reading
the percentiles. On one platform, four streams' p99.9 lateness spans 240 milliseconds to 41
seconds — a factor of 171 — so a single platform-wide delay is either useless or crippling.

Set the delay near **p99.5**, and build an explicit path for everything beyond it. The reason to
stop there: the completeness curve is almost flat after a few seconds. Going from 2 seconds to
60 buys 0.12% more data, because what is still missing is missing for a structural reason —
client buffering, a reconnection — that waiting does not fix.

**Follow-ups.** "What if the stream has two populations?" (Split the stream; separate watermarks;
union where both are needed, at the speed of the slower one.) "What happens when a partition
goes idle?" (`E-03`: the watermark is a minimum, so it freezes — configure `withIdleness`, and
understand it trades a guaranteed stall for possible lateness.) "How does clock skew interact?"
(`E-05`: the delay must exceed your producer clock skew, which nobody enumerates.)

**What distinguishes it.** Saying "five seconds" is the tutorial answer. Saying "I would measure
the distribution and set it near p99.5, and here is why the curve is flat past that" is the
staff answer.

---

## Q3 — Your job's checkpoints are taking longer and longer. Walk me through it.

**Model answer.** This is the death spiral (`C-01`), and the first move is to determine which
phase is growing:

- **Alignment high** → backpressure. Barriers travel with records, so anything that slows
  records slows barriers. The cause is upstream.
- **Sync high** → the snapshot itself. Usually large heap state or a GC pause.
- **Async high** → upload. S3 throttling, or a genuinely large delta.

For alignment, the emergency action is to switch to unaligned checkpointing, which breaks the
loop. Then find the actual bottleneck.

The two things **not** to do: reduce the checkpoint interval (the instinctive response, and it
tightens the spiral) and restart the job (which replays from an old checkpoint, which is more
work, which is the condition you are escaping).

**Follow-ups.** "Why does unaligned help?" (It stops the operator waiting for the slowest input
and includes the in-flight buffers in the snapshot instead.) "What does it cost?" (Bigger
checkpoints, slower recovery, incompatible with some sinks.) "When would you not use it?"
(Steady state — run aligned with a 30-second alignment timeout so you get the cheap path
normally and the robust one under pressure.)

---

## Q4 — A job restarts. How long until the output is correct again?

**Model answer.** Four terms, and people quote only the first three:

```
detection + reschedule     45 s
state download             state ÷ (parallelism × per-subtask MB/s)
RocksDB open               ~60 s
catch-up                   backlog ÷ surplus rate
```

For a 4.2 TB job at parallelism 360: 45 + 130 + 60 = 235 seconds to be *running*, then 235 × the
input rate of backlog divided by the surplus, which at 60% target busyness is 352 seconds.
**Nine point eight minutes.**

The interesting part is the last term's sensitivity. The surplus is `input × (1/busyness − 1)`,
so at 80% busyness recovery is 19.6 minutes and at 90% it is 39.2. **Provisioned headroom is
recovery time**, hyperbolically.

**Follow-ups.** "How would you halve it?" (Task-local recovery: restore from the local disk copy
at 1.4 GB/s instead of S3 at 90 MB/s — 16× on the download term — at the cost of another copy of
the state on local disk, which on a 16-slot node does not fit.) "What if 94 jobs restart at
once?" (`O-01`, the restore stampede — S3 per-prefix limits, and the fix is entropy in the
checkpoint path.)

---

## Q5 — Do you need exactly-once?

**Model answer.** Almost certainly not, for most streams.

The guarantee is about *effects*, not deliveries — duplicate delivery is guaranteed after any
restart. So the question is whether the second processing changes anything, and there are four
ways to make sure it does not, in cost order:

1. **The sink is naturally idempotent** — emit absolute values, not deltas, into an upsertable
   store. Cost: zero.
2. **Dedupe on a business key at the sink.** Cost: index storage.
3. **Two-phase commit.** Cost: visible latency equals the checkpoint interval — p50 31.8 seconds
   against 36 milliseconds. A factor of 870.
4. **Accept at-least-once and guard at the receiver.**

On one 180-job platform, 11 jobs use 2PC. The other 169 are covered by the first two.

And 2PC has a cost you did not sign up for: **one dangling transaction blocks every
`read_committed` consumer of that partition**, so a transactional producer imposes latency and
stall risk on consumers who never asked for the guarantee.

**Follow-ups.** "How do you get an idempotent sink?" (Restructure the computation to emit
absolute values keyed by something stable — usually possible.) "Kafka to Kafka exactly-once?"
(2PC works; check `transaction.timeout.ms` against interval + duration + worst-case restart.)
"What breaks it?" (`D-02`: a `uid` change or a rescale orphans pre-committed transactions.)

---

## Q6 — One subtask is at 100% and 359 are at 30%. What now?

**Model answer.** Key skew. A keyed operator processes one key at a time within a subtask, so a
single hot key cannot be parallelised — parallelism 3,600 would not help.

Confirm it: subtask busyness distribution, then the key frequencies. Then decide whether this is
a *hot key* (one key too big) or a *low-cardinality key* (not enough keys to fill the
parallelism). They look identical and have the same fix.

The fix is **two-phase aggregation with a salted key**: key by `(key, random(0..63))` for a
partial aggregation over a short window, then by `key` for the combine. The hot key now spreads
across 64 subtasks; the combine sees 64 records per window.

Three caveats: it works only for decomposable aggregations, the salt must be random per record
rather than derived from the record, and it adds the phase-1 window to your latency.

**Follow-ups.** "What if the aggregation is not decomposable?" (Sketches, if the output is a
count or a quantile; otherwise the hot key is a hard limit and you shed or you change the
product requirement.) "How do you find the hot key without deploying a change?" (A separate
lightweight job counting key frequencies on the source stream — worth running continuously for
Tier 1 streams.)

---

## Q7 — Design the stream schema for a high-volume event.

**Model answer.** Three layers: transport (key, partition, headers), a **uniform envelope** on
every stream, and the body.

The envelope is uniform and non-negotiable, because every piece of platform tooling — the
auditor, the lineage tracker, the PII scanner, the replay tool — works on any stream precisely
because of that uniformity. Ten fields: `event_id` (UUIDv7, deterministic per logical event),
`event_type`, `event_time`, `ingest_time`, `producer`, `schema_version`, `tenant`, `trace_id`,
`parent_event_id`, `partition_key`.

Avro, for the schema-resolution rules rather than for the byte size — it is the only common
format designed around a reader whose schema differs from the writer's, which is the permanent
condition of a streaming platform.

FULL_TRANSITIVE compatibility, with the transitive window set to your maximum replay depth.
Every field optional with a default. Never delete a field — deprecate it.

And the design rules: units in field names, new names for new meanings, bound every unbounded
field, one open map with an enforced 60-day promotion rule.

**Follow-ups.** "Why is the envelope 118 bytes acceptable on a 640-byte record but not on a
96-byte one?" (It is not — Waypoint's location stream carries a reduced envelope, and that is
the one exception the platform allows.) "How do you handle a field whose meaning changed but
whose type did not?" (Compatibility checking is structural; the control is units in names plus a
semantic-change question in the review plus distribution monitoring.) "What does the schema cost?"
(On a 1.85M/s stream, a 14-byte field is $8,400 a month. Show that in the review.)

---

## Q8 — How do you know you are not losing data?

**Model answer.** You cannot detect loss by looking at the platform, because the platform counts
what arrived. Loss is the gap between what was sent and what arrived, and only the producer
knows the first number.

Three techniques, catching different classes:

1. **Sequence numbers per producer per partition**, checked by an auditing consumer. Catches
   anything serialized but never logged. Misses whole producers dying.
2. **Periodic count reconciliation** — each producer emits its attempted count per minute;
   compare against arrival. Catches everything including dead producers. Misses loss before the
   count.
3. **Canary records** on every partition. Catches whole-path failure, including single-partition
   failure that averaging would hide. Misses partial loss.

Then write the honest sentence, which names its own exclusions: "fewer than 4 records per
billion for internally-produced streams, excluding records lost inside producing applications
before `send()`, which we cannot see. For mobile-produced streams we make no loss claim, because
the client buffer can be discarded by the OS."

**Follow-ups.** "What is the biggest source of loss?" (`send()` returning before durability, with
no failure handler — invisible on both sides.) "How would you find it?" (`record-error-rate > 0`
alerted on every producer; turning that on typically finds several services losing data.)

---

## Q9 — A backfill needs to reprocess 7 days. What do you tell the person who asked?

**Model answer.** Two numbers: how long and how much.

For a 1.85M/s stream at 5× replay: 33.6 hours, 215 nodes, against a steady-state cluster of 96.
**A replay is a second cluster.** And the node-hours are constant across replay rates — 1× for
seven days costs the same as 20× for eight hours — so choose the rate by what the surrounding
system tolerates, not by cost.

Then the risks: a replay reading old data evicts recent data from the broker page cache, so its
cost is paid by every live consumer. Replay from the lake, or on a separate cluster.

And the questions back: does the output replace a range or create a new dataset? Is the job
replay-deterministic — no wall-clock dependency, no processing-time lookups, no side effects? If
not, the replay will produce different numbers from the live run and you will not know which is
right.

**Follow-ups.** "The replay disagrees with the live run. Which is right?" (Run the replay twice.
If the two replays agree with each other and disagree with live, it is lateness — and the replay
is *more* correct. If the replays disagree with each other, it is non-determinism.) "Lambda or
kappa?" (Q16.)

---

## Q10 — Consumer lag is 40 million and the job is processing at exactly the input rate.

**Model answer.** Lag is a stock and throughput is a flow. A healthy flow with a large stock
means the stock is permanent — the job has no surplus to drain it. Alert on
`estimated_time_to_drain`, not on lag.

Then the interesting decision: rescale or wait? Rescaling costs downtime, which *adds* lag:

```
wait:     40M ÷ 121,600/s surplus = 329 s
rescale:  savepoint 95 s + restore + open = 252 s downtime
          + 46M more lag
          at the new surplus                = 386 s
          total                             = 638 s
```

**Rescaling is twice as slow.** The crossover is around 94 million records. Having that number
written down is what makes the decision fast at 3 a.m.

**Follow-ups.** "What would make the rescale cheaper?" (Native savepoints: 6 seconds instead of
95.) "What if lag is approaching retention?" (Extend retention immediately, before any
diagnosis. It is one command and it converts a deadline into a problem.)

---

## Q11 — Where does the money go?

**Model answer.** On a 4-million-event-per-second platform, roughly:

```
cross-zone network       66.7%
processing compute       15.6%
shared serving            7.3%
transport compute         5.6%
observability             3.1%
storage + control plane   1.7%
```

**Moving bytes between racks in the same building costs three times as much as processing
them.** Which means the first optimisation is not tuning; it is rack-aware consumer fetch and
zone-local job placement, worth 31.6% of the bill as configuration changes.

And the largest single lever was made before any of that: **the encoding.** The same platform on
JSON instead of Avro would cost roughly 2.2× as much — not because JSON compresses worse on the
wire (it compresses better) but because the uncompressed form flows through page cache, shuffle
buffers, RocksDB and every serialization step.

**Follow-ups.** "Would you run in two zones?" (Cuts cross-zone by a third, ~$80,000 a month, and
reduces zone tolerance. For a platform whose data is replayable it is more defensible than it
sounds — but it is a decision to make explicitly, not by accident.) "Managed or self?" (Q17.)

---

## Q12 — A dashboard is stale. Every pipeline metric is green.

**Model answer.** The break is downstream of the pipeline, and the diagnostic is a query against
the serving store rather than a pipeline metric:

```sql
SELECT max(window_start) FROM the_table
```

That single query is the most valuable piece of monitoring in a streaming platform, because it
measures what users experience rather than what the pipeline reports.

Causes: a failing sink connector retrying silently, merges behind in the OLAP store, a
materialised view whose refresh broke, a caching layer, or a query with a date predicate that is
now yesterday.

And the design commitment behind it: **the platform's failure mode should be stale, not wrong.**
A consumer can detect staleness; they cannot detect wrongness.

**Follow-ups.** "How do you measure end-to-end latency honestly?" (A canary whose producer and
consumer are the same process, so both timestamps come from one clock — summing per-stage
latencies across machines is off by more than the latency itself.) "What about freshness of a
windowed result?" (`window + watermark delay + sink flush + store visibility`, and the number to
publish is for the *oldest* event in the window, not the newest.)

---

## Q13 — Design a stream-stream join.

**Model answer.** First, establish which of four joins is meant — windowed stream-stream,
temporal (against a versioned table), processing-time lookup, or interval. Their state costs
differ by three orders of magnitude, and "join" is one word.

For a genuine stream-stream join, the state is
`(rate_left × window × bytes_left) + (rate_right × window × bytes_right) × 1.23`. For a 24-hour
ad-attribution join at 180,000 impressions/s and 220 bytes: **4.2 TB**, almost entirely on the
impressions side.

Then the reductions, in order: **filter, then project, then key, then join.** Projecting a
640-byte record to the 220 bytes the join needs is a 2.9× saving on state, shuffle,
serialization and checkpoints simultaneously, and it is one line.

And the product conversation: the window is linear in the state, so halving it saves 2.1 TB.
That is worth putting in front of whoever owns the attribution rule.

**Follow-ups.** "What if unmatched records matter?" (An inner join makes non-events invisible.
Use an outer join or a timer that fires on the window boundary and emits the unmatched side —
that branch is the entire value of the pattern.) "How would you reduce it further?" (If the
output is a count rather than an enumeration, a Bloom filter over impression IDs is 2.8 GB
against 3.42 TB, at a stated 0.1% false-positive rate.)

---

## Q14 — When would you not use stream processing?

**Model answer.** Four cases, and stating them is the answer:

1. **Latency budget over five minutes.** Incremental batch gives you event-time correctness for
   free (the data is all there), recovery for free (re-run), schema evolution for free (it is a
   table), and people who already understand it.
2. **Fewer than about fifteen jobs.** The platform's fixed cost — state management, rescaling,
   schema evolution, lineage — is not justified. Write a consumer, or use Kafka Streams, which
   is a library rather than a cluster.
3. **Unbounded state with no designed bound.** "Count distinct users, ever" is a streaming
   computation plus an unstated promise to buy unbounded memory.
4. **The computation is genuinely batch.** A global median over 400 days, model training,
   all-pairs similarity. Dressing these as streams helps nobody.

**Follow-ups.** "Where is the boundary between incremental batch and streaming?" (Around one
minute it stops being comfortable; around fifteen seconds it stops being possible.) "When is
Kafka Streams right?" (Under ~15 Kafka-to-Kafka jobs with sub-terabyte state. Using Flink
instead is paying a large operational premium for capabilities you may not use.)

---

## Q15 — How do you delete one person's data from a streaming platform?

**Model answer.** The obvious answers fail. Compaction tombstones only work for compacted
topics and the tombstones themselves expire. Waiting for retention does not satisfy "now" and
does not touch the 400-day lake. Rewriting the lake touches essentially every file, because
rows are sorted by time and a person appears throughout — roughly $180,000 per request at
petabyte scale, against thousands of requests a month.

**Crypto-shredding.** Encrypt each subject's data with a per-subject key; deleting the key makes
every copy — on the log, in the lake, in backups, in downstream stores — permanently unreadable.
Deletion is O(1) and it covers copies you have lost track of, which no rewrite-based approach
does.

The costs are real: a key store doing 1.85 million lookups a second, every read needing a key,
losing the key store meaning losing everything, and the inability to aggregate an encrypted
field.

The design that makes it affordable: **shred the link, not the behaviour.** Encrypt the
identifier, not every field. The behavioural data survives as unattributable rows; the person is
no longer identifiable. This also resolves the common conflict where a financial regulation
requires seven-year retention and privacy law requires deletion — the transaction survives, the
identification does not.

**Follow-ups.** "Is pseudonymous data in scope?" (Yes. A member ID that links to a person is
personal data, which puts ~94% of a typical platform's volume in scope.) "How do you verify
deletion propagated?" (A deletion *stream* that every derived store subscribes to, a registry of
subject-linked stores generated from job manifests, and a monthly sampled verification.)

---

## Q16 — Lambda or kappa?

**Model answer.** Kappa for the semantics, lambda for the execution, and be specific about
where the line falls.

The kappa argument is right about the important thing: two implementations of the same logic
diverge, and every discrepancy between a streaming and a batch number — window boundaries, time
zones, deduplication, lateness — exists because two people wrote the same thing twice.

Where kappa genuinely breaks:

- **Replay state is unchanged by replay speed.** A 24-hour window holds 24 hours of data whether
  you replay at 1× or 20×, and without watermark alignment it holds *more*, because the source
  spread is wider.
- **Batch engines are better at batch** — a sort-merge join over bounded data beats a hash join
  maintaining state, often by 3–5×.
- **Some computations are not streamable.**

So: one definition of the logic, run as a stream live and in the engine's batch mode for
backfills under ~30 days, with a second implementation only where a measurement demands it. On
one 180-job platform that is 2 jobs. **Kappa wins for 99% of the work and loses for the 1% where
it should**; claiming 100% is what makes people distrust it.

---

## Q17 — Managed or self-managed?

**Model answer.** It depends on volume, and the crossover is sharper than people expect, because
**managed streaming services price per byte** — which is excellent at low volume and inverts
fast.

For the log: self-managed brokers at 607 MB/s cost about $20,000 a month; the same volume on a
fully-managed service with per-byte pricing is $224,000–$664,000 before discount. The crossover
is around 40–80 MB/s.

For processing: managed Flink is about 2.25× the raw compute, a smaller multiple because compute
is harder to mark up. The constraint that matters more than price is **state** — 50 GB per unit
of compute means a 4.2 TB job needs 84 units for storage alone.

And the other side, honestly: 1.2 engineers of Kafka operations is $220,000 a year. So the
interesting comparison is self-managed against the *middle* option (a managed service that still
prices per instance), not against the fully-managed one.

```
< 20 MB/s, < 10 jobs   → fully managed. Do not build this.
20–100 MB/s            → managed. The ops savings exceed the premium.
100 MB/s – 1 GB/s      → genuinely hard. Model it.
> 1 GB/s               → self-managed, almost certainly.
```

---

## Q18 — Walk me through a schema change that broke production.

**Model answer.** The instructive one is the change that **passed every check**. A field changed
from seconds to milliseconds. Type unchanged (`long`), name unchanged, FULL_TRANSITIVE
compatibility passed. Every downstream consumer was 1,000× wrong for four days.

Compatibility checking is structural and knows nothing about meaning.

The controls that would have caught it:

- **Units in field names.** `view_duration_ms`. This one convention eliminates the class.
- **A semantic-change question in the review**, forcing the author to state whether any existing
  field's meaning changed.
- **Distribution monitoring on numeric fields** for Tier 1 streams: alert when a median moves
  more than 3× across a deploy boundary.
- **Sanity bounds in the consumer**, failing loudly rather than clamping.

And the part that makes it a staff answer: the daily reconciliation did not catch it, because it
compared the stream against the lake and both had the same wrong values. **A reconciliation
against a downstream copy of the same data proves only that your pipeline is deterministic.**

---

## Q19 — Your pipeline is exactly-once. Prove it.

**Model answer.** I would not claim it without a test, because the guarantee degrades silently —
someone adds a second sink, changes a connector, turns off `read_committed` to fix a lag problem,
or rescales and orphans transactions. None of those produce an error.

So: make it a **tested property**. A nightly job that (1) runs the pipeline against a fixed
input fixture, (2) kills a TaskManager mid-run, (3) asserts the output matches a clean run
byte for byte — and asserts the *documented* guarantee rather than the strongest one, so an
at-least-once pipeline is tested for at-least-once.

Then the measurement in production: distinct `event_id` against total records at the sink, on a
sample. And the honest sentence, which separates pipeline-level duplication from observable
duplication and names loss separately:

> "Produced at-least-once with an idempotent upsert keyed by (window_start, surface). Measured
> over 30 days: duplicate emissions 0.0071%, all absorbed by the upsert, so observable
> duplication is zero. Observable *loss* is the late-record drop rate, 0.031%, published per
> window."

---

## Q20 — What did you get wrong last time, and what did you change?

**Model answer.** This is the question with the most signal and the least preparation, so the
shape matters more than the content. A strong answer:

- Names a specific decision, not a category.
- Says what the reasoning was *at the time*, sympathetically — a mistake that was obviously
  stupid is not interesting.
- Names the cost, with a number.
- Distinguishes the fix that prevented recurrence from the fix that prevented the class.
- Admits what is still unresolved.

An example with the right shape: "We set the watermark delay at p99 rather than p99.5 because we
were optimising a dashboard's freshness. That dropped 0.61% of records, silently, for fourteen
months, and it fed a regulatory report. The technical fix was a side-output reconciliation path
and a `late_record_ratio` SLI. The organisational fix — which mattered more — was that every
derived dataset now publishes a completeness class, so the trade-off the pipeline makes is
visible to whoever uses the number. What is still unresolved is that we have no good way to
detect when a *consumer* starts treating a provisional number as final."

**What distinguishes it.** The organisational/technical distinction, and the unresolved part.
Anyone can describe a fix; describing what remains broken after the fix is what shows you kept
looking.

---

## The five patterns that distinguish a strong answer

Across all twenty, the same five things separate a complete answer from a strong one.

**1. Numbers on every arrow, and knowing which one is least reliable.** "About 500 MB/s onto the
log, and the number I am least sure of is the compression ratio, which I would measure before
committing."

**2. Naming the binding constraint.** Most systems are limited by one thing. Saying which, and
why the other candidates are not it, demonstrates having built one.

**3. Stating the trade rather than the choice.** "Exactly-once here costs a factor of 870 in
latency" is a better answer than "we would use exactly-once."

**4. Saying what you would measure before deciding.** A design that names its own uncertainties
is more credible than one that does not, and it is what actually happens on a real project.

**5. Knowing when not to build it.** Every question above has a version where the right answer
is "this does not need a streaming platform." Volunteering that is a strong signal, not a weak
one.
