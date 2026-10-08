# Case Studies

Ten incidents, `CS-1` … `CS-10`, across Meridian's six tenants. Each combines scenarios from the
earlier documents, because real incidents never come one class at a time. Each is written as:
**what happened**, **the timeline**, **why the obvious diagnosis was wrong**, **what actually
fixed it**, and **what changed afterwards**.

They are ordered roughly by how instructive they are rather than by severity.

---

## CS-1 — Corridor: the watermark that went to the year 2287

**System:** `corridor-sessionize`, a session window over `corridor.tracking.events`, 30-minute
gap, ~14 million active members, 4.8 GB of state.

**What happened.** At 14:02 the job emitted 11 million session records in four seconds — every
open session, closed simultaneously. Then it emitted nothing for six hours while continuing to
consume 1.85 million records a second.

### Timeline

```
14:02:11  A mobile client running app version 6.4.1-beta emits an event with
          event_time = 9,999,999,999,999,999 microseconds (year 318,857).
          The gateway's clamp is configured for the production app and this
          beta build bypassed it via a different ingest path.

14:02:11  The watermark generator takes max(event_time) − 8 s.
          Watermark jumps to the year 318,857.

14:02:11  Every session window's gap has "expired" relative to the watermark.
          11,043,882 sessions fire.

14:02:15  Downstream ClickHouse receives 11M rows with session_end in the
          year 318,857. The dashboard, which filters to the last 24 hours,
          shows zero sessions.

14:06     Page fires: "corridor session count dropped to zero."

14:11     On-call checks the job. State: RUNNING. Throughput: normal.
          Lag: near zero. Checkpoints: succeeding.
          Every standard metric is green.

14:24     On-call restarts the job, reasoning that a restart fixes most
          things. The job restores from the latest checkpoint — which
          contains the poisoned watermark. Nothing changes.

14:51     Someone checks numLateRecordsDropped. It equals the input rate
          exactly: every record is being dropped as late.

15:04     Watermark value is read directly. The year is 318,857.

15:22     The bad record is located by querying ClickHouse for session_end
          beyond 2100 and tracing back to the member and the app version.

15:40     Job restarted from a savepoint taken at 13:00, before the poison.
          Two hours of sessions must be replayed.

17:55     Caught up.
```

### Why the obvious diagnosis was wrong

The symptom — "no output" — pointed at a stalled job. Every instinct said the job was stuck, so
the response was to restart it. **The actual state was the opposite: the job had run too far
forward, permanently.**

The restart made it worse in a specific way: it consumed 22 minutes and it restored the poison
from the checkpoint, which ruled out the correct diagnosis for anyone reasoning "we restarted,
so state is clean."

### What actually fixed it

Restoring from a savepoint predating the poisoned record. There was no other option: a
watermark is monotonic within a job's state, and the poison was in every subsequent checkpoint.

### What changed

1. **The clamp moved into the timestamp assigner** (`E-04`), not just the gateway, so no ingest
   path can bypass it. Four lines, in a shared library, applied to all 180 jobs.
2. **`currentInputWatermark` became a first-class alert**, with a check for "is this timestamp
   in the future." Nobody had thought to alert on a watermark being *too far ahead*.
3. **Beta app builds got their own ingest path and their own topic**, so a beta client cannot
   affect production streams at all.
4. **The runbook's step 1 for "no output" became "read the watermark value"**, before any
   restart. That one reordering would have saved 80 minutes.

**The lesson:** `RUNNING` with green metrics and zero output is a watermark problem until proven
otherwise, and **restarting is not a neutral action** — it costs time and it can restore the
cause.

---

## CS-2 — Gateline: the on-sale that moved

**System:** `gateline-ticket-allocation`, a keyed state machine over
`gateline.ticket.requests`, 240 partitions, normally 5,000 events/s.

**What happened.** A major on-sale was moved forward by 40 minutes for a marketing reason. The
platform team was not told. The pre-scaling procedure (`P-07`) runs at T−60 minutes, so at the
new T−0 the jobs were at baseline parallelism.

### Timeline

```
09:20  Marketing moves the on-sale from 11:00 to 10:20.
       Announced on social media. Not communicated to the platform team.

10:20  500,000 requests/s arrive within 10 seconds.
       The job is at parallelism 24, sized for 5,000/s.

10:20  Producers' record accumulators fill in 1.4 seconds.
       send() begins blocking (I-01). The ticketing API's request
       threads are held. The API's p99 goes from 80 ms to 60 s.

10:21  Kafka absorbs what reaches it. Two of 240 partitions receive
       everything, because the stream is keyed by event_id and there
       is one event on sale (I-05).

10:22  The autoscaler observes high busyness and triggers a scale-up.
       Savepoint + restore + reschedule = 4 minutes of downtime,
       during the spike (P-06).

10:26  The job resumes at parallelism 96. Two subtasks are saturated;
       94 are idle. Parallelism did not help, because the bottleneck
       is one key (P-01).

10:31  On-call throttles the gateway to 40,000 requests/s and enables
       the waiting room.

10:52  Backlog drained. 180,000 requests had been rejected with 429;
       roughly 11,000 users saw an error rather than a queue position.
```

### Why the obvious diagnosis was wrong

The first hypothesis was "we did not scale in time", which is true and incomplete. **Scaling
would not have helped**, because the bottleneck was a single partition fed by a single key. The
job could have been at parallelism 720 and still processed 2,083 events/s on the hot partition.

The second hypothesis was "Kafka could not take the load", also wrong: Kafka absorbed 500,000
events/s without difficulty. The producers blocked because their *local buffers* filled while
waiting for two partition leaders.

### What actually fixed it

Shedding at the gateway. Nothing else was available in the time frame.

### What changed

1. **The key changed from `event_id` to `event_id + section_id`**, giving 40–200 keys instead of
   one (`I-05`). The ordering requirement was per-section all along; nobody had asked.
2. **A calendar integration**: the on-sale schedule is a data source the platform reads, and a
   change to it triggers the pre-scaling procedure automatically. The organisational fix, which
   is the one that actually prevents recurrence.
3. **Autoscaling disabled for Gateline jobs during announced events** (`P-06`), because a
   reactive scale during a 10-second spike is strictly harmful.
4. **The ticketing API stopped calling `send()` on request threads** (`I-01`), putting a bounded
   local queue in front with an explicit drop policy. The API now degrades instead of stopping.

**The lesson:** a 100× scheduled spike is a capacity-reservation problem, not an autoscaling
problem — and **the fix for a hot key is a better key, not more machines.**

---

## CS-3 — Waypoint: 0.6% of trips missing, for fourteen months

**System:** `waypoint-trip-aggregation`, per-city per-hour trip counts from
`waypoint.trip.events`, feeding a regulatory report.

**What happened.** A quarterly audit compared the streaming trip counts against the operational
database and found the stream consistently 0.6% low, for as far back as anyone could check.

### The investigation

```
Hypothesis 1: events lost in transit.
  Checked with the sequence-gap auditor (doc 02 §9). Loss: 3 per billion.
  Not this.

Hypothesis 2: duplicates in the database inflating its count.
  Checked. The database's count is correct.
  Not this.

Hypothesis 3: window boundary semantics.
  The stream uses [start, end); the report generator used [start, end].
  This accounted for 0.004%. Real, and not the cause.

Hypothesis 4: late records dropped.
  numLateRecordsDropped had never been graphed. When graphed:
  0.61% of records, steady, for the whole retention of the metric.
  This is it.
```

### The mechanism

Waypoint's watermark delay is 10 seconds, chosen from the p99.5 of lateness
([05](05-event-time-watermarks-and-lateness.md) Section 3). The lateness distribution has a
second population — mobile clients that buffered and reconnected (`I-09`, `E-06`) — accounting
for roughly 0.6% of records arriving minutes to hours late.

**The design was working exactly as specified.** The specification was never communicated to the
people who used the output.

### Why it took fourteen months

Because nothing failed. The pipeline had no errors, no lag, no alerts. The output looked
plausible — 0.6% is within the range that a month-over-month comparison would not flag. And the
only way to detect it was a comparison against an independent source, which happened quarterly
and which had previously been done at a granularity too coarse to see it.

### What changed

1. **`late_record_ratio` became a platform-wide SLI** with a warn at 0.05% and a page at 0.5%
   (`E-08`). Meridian had 31 jobs above 0.05% at the time this was turned on. Nine of them
   mattered.
2. **The side-output path was built** (`E-06`): late records go to
   `waypoint.trip.events.late`, and a nightly job reconciles them into the regulatory table.
   The regulatory number is now final at T+1 day and exact.
3. **Every derived dataset now publishes `completeness_class`** and the streaming number is
   labelled provisional until reconciliation.
4. **The daily reconciliation against the lake** (`W-07`) was built for all Tier 1 datasets,
   which found two more discrepancies in its first month.

**The lesson:** this is the silent-degradation class
([15](15-failure-modes-and-the-operations-playbook.md) Section 1) in its purest form. **The
design was correct, the implementation was correct, and the output was wrong** — because the
trade-off the design made was never surfaced to the consumer. Publishing completeness is what
converts a hidden approximation into an honest one.

---

## CS-4 — Northlight: the mesh config push

**System:** Northlight's service mesh emits `northlight.playback.heartbeats` at 1.1 million
events/s from 100,000 sidecars via node-level aggregators
([02](02-producers-and-the-ingestion-edge.md) Section 7).

**What happened.** A mesh configuration change enabled a new telemetry field on every sidecar.
The change rolled out over 90 seconds.

### The arithmetic

```
new field: request_headers, a map, average 340 B
heartbeat record: 220 B → 560 B
                  a 2.55× increase in bytes

byte rate:   242 MB/s → 616 MB/s
compressed:   75.6 MB/s → 192.5 MB/s
platform total onto the log: 607.2 → 724.1 MB/s   (+19%)
```

### Timeline

```
13:40  Config push begins. Sidecars pick it up over 90 seconds.

13:41  Platform ingest rate rises 19%. Nothing alerts — this is within
       the diurnal range.

13:42  Per-broker write rate rises from 38.0 to 45.3 MB/s.
       Page-cache residency falls from 24.5 to 20.5 minutes (T-09).
       No alert: the residency alert threshold is 15 minutes.

14:15  A routine deploy restarts corridor-ad-attribution. Its restore
       takes 4 minutes, as expected, and its catch-up read now falls
       partly outside the page cache.

14:19  The disk read storm from that catch-up slows every consumer on
       the bulk cluster. Fetch p99 goes from 18 ms to 340 ms.

14:23  Six jobs backpressure. Two checkpoint timeouts.

14:26  On-call pages. The visible symptom is "the Kafka cluster is slow",
       with no obvious cause — the deploy at 14:15 was routine and had
       been done dozens of times.

15:10  Someone correlates the ingest rate increase at 13:41 with the
       mesh config push.

15:24  The field is disabled. Ingest returns to normal.
       Residency recovers over the following 25 minutes.
```

### Why the obvious diagnosis was wrong

The trigger was the deploy at 14:15, so the investigation focused there. But that deploy was
identical to dozens of previous ones; **what changed was the environment it ran in**, 34 minutes
earlier, in a different system, owned by a different team.

The causal chain crossed four boundaries — a mesh config change, a record size, a broker's page
cache, and another tenant's job restart — and no single team could see all four.

### What changed

1. **Record size per stream became a monitored metric** with an alert on a change greater than
   20%. This is not a metric anyone had thought to collect.
2. **Page-cache residency became a graphed, alerted metric** (`T-09`) with the alert at 20
   minutes rather than 15, so there is warning before the binding constraint binds.
3. **Mesh config changes affecting telemetry volume go through the platform's change review**,
   which is an organisational control and the only one that catches a cross-boundary chain.
4. **The change-annotation panel** ([16](16-observability-of-a-streaming-pipeline.md) Section 8)
   was extended to include infrastructure and mesh changes, not just job deploys. The 15:10
   correlation would have been a glance.

**The lesson:** the binding constraint on a platform can be moved by a change in a system that
has nothing to do with it, and the symptom appears later, attached to an unrelated trigger.
**Monitor the binding constraint directly**, not the things you think determine it.

---

## CS-5 — Riverbend: the dual-write that was found by reconciliation

**System:** `riverbend.payments.settled`, produced by `payment-service` alongside a database
write.

**What happened.** A reconciliation — built after CS-3 — found that 0.03% of settled payments in
the database had no corresponding event on the stream. 0.03% of 67 events/s is about 52 events
a day, each a real payment.

### The mechanism

`I-12`, exactly:

```java
@Transactional
public void settle(Payment p) {
    paymentRepo.updateStatus(p.getId(), SETTLED);
    kafkaTemplate.send("riverbend.payments.settled", event);   // async
}
```

The `send()` is asynchronous and outside the transaction. Two failure orders:

- Process dies after `send()` and before commit → event exists, payment does not. This is the
  *worse* direction and it was happening at about 4 a day.
- `send()` fails after commit, failure handler logs at `WARN` → payment exists, event does not.
  About 48 a day.

Both had been happening since the service was written, three years earlier.

### Why it was not found sooner

The failure handler existed — this was not `I-02`'s fire-and-forget — and it logged. The log
line said `WARN payment event publish failed, will be retried` and there was no retry. Nobody
alerted on that log line, and `record-error-rate` was not on any dashboard.

**A `WARN` log with no alert is a way of not knowing something while appearing to handle it.**

### What changed

1. **The transactional outbox** (`I-12`, `X-07`). The event is written to an `outbox` table in
   the same transaction; Debezium publishes it. Atomicity is now local.
2. **`record-error-rate > 0` alerts on every producer, platform-wide.** Turning this on found
   errors on 14 other services, three of which were losing data.
3. **The reconciliation became continuous**, not quarterly, comparing database state transitions
   against stream events hourly.
4. **The 52 events a day were re-emitted** from the database for the period the database retained
   — 18 months — and the reconciliation confirmed convergence.

**The lesson:** the dual-write problem is the most common correctness bug in event-driven
systems, it produces losses in both directions, and **it is only ever found by a reconciliation
that someone decided to run.**

---

## CS-6 — Lumen: the schema change that passed every check

**System:** `lumen.engagement.events`, 240,000 events/s, feeding a creator-payout calculation.

**What happened.** A producer change altered `view_duration` from seconds to milliseconds. The
schema was unchanged — `long` before and after. FULL_TRANSITIVE compatibility passed. The
registry accepted it.

For four days, creator payout calculations used durations 1,000× too large, which inflated the
"qualified view" count (views over 30 seconds) from 12% of views to 99.8%.

### Why nothing caught it

- **Schema compatibility is structural** (`S-01`). The type did not change.
- **No test compared distributions** before and after a deploy.
- **The payout calculation had no sanity bounds.** A creator qualifying 99.8% of views is
  implausible and nothing said so.
- **The daily reconciliation compared the stream against the lake**, and both had the same wrong
  values, because the lake is fed from the same stream. **A reconciliation against a
  downstream copy of the same data validates nothing.**

That last point is the most instructive: Meridian's reconciliation framework, built after CS-3,
did not catch this, and the reason is that it compared two computations over the same input
rather than comparing against an independent measurement.

### What changed

1. **Units in field names** (`S-01`): `view_duration_ms`. A one-time migration across 31
   streams, done over a quarter.
2. **Distribution monitoring on numeric fields** for Tier 1 streams: alert when a field's median
   moves more than 3× across a deploy boundary. This caught two genuine bugs in its first
   quarter.
3. **Sanity bounds in the consuming calculation**, with a loud failure rather than a clamp. The
   payout job now refuses to run when the qualified-view ratio is outside 5–40%.
4. **The reconciliation framework gained a category**: "against an independent source"
   (the product analytics system, which counts views separately) rather than only "against the
   lake."

**The lesson:** structural compatibility checking knows nothing about meaning, and **a
reconciliation against a downstream copy of the same data proves only that your pipeline is
deterministic.**

---

## CS-7 — Corridor: the restore stampede

**System:** the whole platform, during an availability-zone impairment.

**What happened.** One AZ became unreachable for 22 minutes. 56 TaskManagers were lost. 94 jobs
restarted. Recovery took 71 minutes, against a documented expectation of about 12.

### The mechanism

`O-01`. Aggregate restore demand hit S3's per-prefix request limit:

```
94 jobs, ~24 TB of state to restore
at 90 MB/s per subtask × ~2,400 affected subtasks = 216 GB/s desired
actual achieved: 31 GB/s, throttled

S3 503 SlowDown across every job
```

And compounding it:

```
each job's exponential backoff was configured as fixed-delay 10 s
→ 94 jobs retrying every 10 seconds into a throttled endpoint
→ the retries themselves consumed the request budget
```

### Why the obvious response made it worse

At minute 18, with restores clearly stuck, the on-call restarted the jobs that had not yet
recovered — reasoning that they were hung. Each restart discarded the partial download progress
and re-queued the requests. **Restore time went up, not down.**

### What changed

1. **Entropy injection in the checkpoint path** (`C-03`). `s3://bucket/_entropy_/<job>/chk-<n>/`
   spread the load across S3 partitions. The next drill restored 94 jobs in 14 minutes.
2. **Exponential backoff with a cap** replaced fixed-delay on all 180 jobs (`C-05`).
3. **Tiered restore priority**: the Flink operator now limits concurrent job restores and
   releases them by tier. Tier 1 jobs recover first; Tier 3 jobs wait, which is correct and
   which nobody had configured.
4. **The runbook gained an explicit "⚠️ do not restart jobs that are restoring" step**, near
   the top.
5. **The zone-loss gameday** became quarterly rather than never.

**The lesson:** individual recovery times do not compose. **A platform's recovery time is not
the maximum of its jobs' recovery times; it is set by whatever resource they all contend for**,
and that resource is usually the one nobody sized because each job's use of it is small.

---

## CS-8 — Waypoint: the enrichment lookup that took down four jobs

**System:** four jobs enriching `waypoint.location.updates` and `waypoint.trip.events` with
driver attributes from DynamoDB via async I/O.

**What happened.** A DynamoDB partition experienced elevated latency — p99 from 8 ms to 4.2
seconds — for eleven minutes. All four jobs failed and entered restart loops.

### The mechanism

`J-05`, compounded:

```
1. Async I/O timeout was 10 s, and the default timeout action is to
   fail the job. At 4.2 s p99, ~3% of lookups exceeded 10 s.

2. Capacity (200 in flight) filled, because each request occupied a
   slot for 4.2 s instead of 8 ms:
     sustainable rate = 200 ÷ 4.2 = 48 requests/s
     actual rate      = 750,000/s ÷ 96 subtasks = 7,812/s
   → 163× over capacity. Immediate, total backpressure.

3. Backpressure lengthened checkpoint alignment. Checkpoints timed out.

4. The timeout action failed the jobs. They restarted, restored state,
   and immediately hit the same lookup latency.

5. Four jobs restarting in a loop, each restoring state, saturated the
   S3 path — a small-scale CS-7.
```

### Why the obvious diagnosis was wrong

The pages said "Flink jobs restarting." The Flink cluster was healthy. DynamoDB's own dashboards
showed the affected partition but the *table's* aggregate metrics looked normal, because one hot
partition out of many does not move the average.

Twenty-six minutes were spent looking at Flink before anyone looked at the dependency, which is
the normal outcome when the failing component is not the one paging.

### What changed

1. **The timeout action degrades instead of failing** (`J-05`): emit the record unenriched with
   `enrichment_status: "timeout"`, increment a counter. Four lines.
2. **A circuit breaker** around the lookup: after 50 consecutive failures, stop calling for 30
   seconds and emit unenriched. The job stays up and the output is marked.
3. **A local cache with a stale-serving fallback**: when the store is unavailable, serve from
   cache past its TTL and count it. Hit rate is 68%, so 68% of records keep full enrichment
   through an outage.
4. **Downstream consumers gained a policy for `enrichment_status`**, decided in advance.
5. **The dependency's health became part of the job's dashboard**, so the next investigation
   starts in the right place.

**The lesson:** **an async lookup makes the remote store's availability part of your job's
availability**, and the default configuration converts a dependency's degradation into your
outage. The four lines that change the timeout action are the highest-value four lines in this
collection.

---

## CS-9 — Riverbend: exactly-once that stalled every consumer

**System:** `riverbend-payment-enrichment`, exactly-once with a transactional Kafka sink,
60-second checkpoints.

**What happened.** The job entered a restart loop because of an unrelated bug. Over 90 minutes
it restarted 61 times. Each restart left a dangling transaction. Every `read_committed` consumer
of `riverbend.payments.enriched` — seven of them, including the settlement reconciliation —
stopped advancing.

### The mechanism

`C-10`. A `read_committed` consumer cannot read past the **Last Stable Offset**, which is the
offset of the oldest open transaction. One dangling transaction from 90 minutes ago blocked
everything after it, even though 61 later transactions had committed successfully.

```
transaction.timeout.ms was set to 900,000 (15 min) — raised six months
earlier to stop ProducerFencedException errors (C-11) without computing
the right value.

61 restarts × up to 15 min of dangling transaction each
→ a continuously-moving blockage
```

The consumers showed **growing lag with zero throughput and no errors**, which is a signature
nobody recognised.

### What actually fixed it

```bash
kafka-transactions.sh --bootstrap-server broker:9092 list \
  | grep riverbend-payment-enrichment
# then, after confirming the producers were dead:
kafka-transactions.sh --bootstrap-server broker:9092 \
  abort --topic riverbend.payments.enriched --partition N --start-offset M
```

for each dangling transaction. Eleven of them.

### What changed

1. **The restart loop was fixed** — a poison record with a deserialization failure, now
   dead-lettered (`C-05`).
2. **`transaction.timeout.ms` computed properly** (`C-11`): interval 60 s + p99 duration 2.6 s +
   worst-case restart 240 s = 303 s, set to 600,000 (10 min) rather than 15.
3. **A failure-rate limit**: a job that fails 10 times in an hour stops and pages, rather than
   restarting forever.
4. **An alert on open transaction count** per topic, which did not exist.
5. **A review of which jobs actually need 2PC** ([09](09-delivery-guarantees-and-exactly-once.md)
   Section 3). Two of the eleven were changed to idempotent sinks, because their outputs were
   absolute values into an upsertable store and the transactional path was buying nothing.

**The lesson:** **a transactional producer imposes a cost on every `read_committed` consumer of
that topic**, and the failure mode — growing lag, zero throughput, no errors — does not look
like anything else. Choose exactly-once per stream, deliberately, knowing that.

---

## CS-10 — Northlight: the job that could not be rolled back

**System:** `northlight-concurrency`, a 5-minute sliding window over playback heartbeats,
computing concurrent viewers.

**What happened.** A change replaced a `ValueState<HashMap<String, Long>>` with a `MapState`
(`K-07`'s optimisation — a good change, correctly motivated by a profiling result). The deploy
succeeded. Two hours later, the concurrency numbers were found to be wrong: roughly 40% low.

The rollback failed. The old version could not read the new version's savepoint, because the
state serializer had changed incompatibly.

### The mechanism

Two independent problems that combined:

**The correctness bug.** The `MapState` migration also changed the key from
`session_id` to `member_id`, because the developer noticed that the map was keyed by session and
"member is more natural." That was a semantic change disguised as a refactor: a member with
three concurrent sessions now counted as one.

**The rollback failure.** `K-08`. `ValueState<HashMap>` used Kryo (because `HashMap<String, Long>`
is not a Flink POJO); `MapState` used Flink's native map serializer. There is no migration path
between them in either direction.

### What happened next

```
16:10  Wrong numbers noticed.
16:14  Rollback attempted. Fails: StateMigrationException.
16:20  Decision: restore the old version from the pre-deploy savepoint.
       Meridian retains one (O-05). It is 2h10m old.
16:24  Old version restored from the pre-deploy savepoint.
       It must replay 2h10m of heartbeats: 1.1M/s × 7,800 s = 8.6 billion
       events. At the job's surplus rate, 4.4 hours.
16:26  Decision: accept a gap instead. Start the old version from the
       current offsets with empty state, and accept 5 minutes of
       warm-up (the window length) with no output.
16:33  Correct numbers resume. A 23-minute gap in the metric.
```

### What changed

1. **A CI check that loads the previous release's savepoint with the new release's serializers
   and vice versa**, failing the build on incompatibility (`K-08`). This is two hours of work
   and it prevents the entire class.
2. **Avro-generated types for all state**, removing Kryo everywhere (`K-07`), which also made
   future schema evolution possible.
3. **`disableGenericTypes()` in every job's test suite**, so a Kryo fallback fails a test rather
   than shipping.
4. **A rule that a refactor may not change semantics in the same commit.** The key change from
   `session_id` to `member_id` was a product decision wearing a refactor's clothes, and
   separating them would have forced a review of what "concurrent" means.
5. **The pre-deploy savepoint retention went from 24 hours to 7 days**, because the one that
   saved this incident was nearly expired.

**The lesson:** **a state-incompatible change is a one-way door**, and the only thing standing
between you and it is a savepoint you took before walking through. Meridian had one because of
a policy set after an earlier incident; without it, the only recovery was a 4.4-hour replay.

---

## What these ten have in common

Reading them together, five patterns recur, and they are worth more than any individual
incident.

**1. The obvious diagnosis was wrong in eight of ten.** The symptom pointed at the wrong
component — a restart for a watermark poison (CS-1), Flink for a DynamoDB problem (CS-8), a
routine deploy for a mesh config change (CS-4). **The component that pages is frequently not the
component that failed.**

**2. Restarting made it worse in three.** CS-1 restored the poison, CS-7 discarded download
progress, CS-9's loop created the dangling transactions. Restarting is an action with a cost and
a failure mode, not a neutral reset.

**3. Four were found by reconciliation, not by monitoring.** CS-3, CS-5, CS-6 and part of CS-10
produced no alert of any kind. **The silent class is found only by comparing against something
independent**, and CS-6 shows that "independent" has to mean genuinely independent — not a
downstream copy of the same computation.

**4. Six had a four-line fix that was known in advance.** The timestamp clamp (CS-1), the
timeout action (CS-8), the checkpoint entropy (CS-7), `source.ts_ms`, the error-rate alert
(CS-5), units in field names (CS-6). None of these are hard. They are the kind of thing that
does not get done because nothing is currently broken.

**5. Half were ultimately organisational.** Gateline's on-sale calendar, Northlight's mesh
change review, Riverbend's reconciliation cadence, Lumen's refactor rule. **The technical fix
prevents the specific recurrence; the organisational fix prevents the class**, and the
organisational fix is the one that gets dropped from the postmortem action list.
