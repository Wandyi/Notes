# Failure Modes and the Operations Playbook

Scenarios `O-01` … `O-12`.

This is the document to open at 3 a.m. It covers the failures that belong to the platform rather
than to any one job, the order to check things in, four runbooks written out step by step, and
the gameday programme that finds these before they find you.

## 1. The four platform-level failure shapes

Individual job failures are covered by the letters in the other documents. What is left are
failures of the platform *as a whole*, and they come in four shapes.

**Correlated restart.** Many jobs restart at once. The trigger might be a Kubernetes node pool
rotation, a Flink operator upgrade, a zone event, or an expired certificate. The problem is not
the restart; it is that 180 jobs restoring simultaneously request 31 TB from S3 at the same
moment, which nothing is sized for.

**Metastable degradation.** The trigger passed and the system cannot recover
([11](11-backpressure-and-flow-control.md) `B-02`). The signature is a symptom that persists
after its cause is gone.

**Control-plane failure.** Jobs keep running — that is the good news — but nothing can be
deployed, scaled, restarted or inspected. This is survivable for hours and catastrophic if it
coincides with a data-plane incident, which is exactly when the shared dependency is most
loaded.

**Silent degradation.** Everything is green and the output is wrong. Late records being dropped
(`E-08`), a watermark poisoned (`E-04`), state that did not restore (`C-07`), deduplication
that stopped working (the `ORDER BY` gotcha in
[13](13-serving-the-results.md) Section 3). This is the most dangerous shape because there is no
page, and it is the reason half of this collection is about measurement.

## 2. The triage order

Work down this list. It is ordered by how quickly each step eliminates a large class, not by how
likely each cause is.

**1. Is anything actually broken, or is a threshold wrong?** Check the user-visible symptom —
serving-store freshness (`Q-01`), not a pipeline metric. If the dashboards are fresh and
correct, the page is about a leading indicator and you have time.

**2. Is it one job or many?** Many jobs means platform; one job means job. This single question
halves the search space and it is answered by one panel.

**3. If many: what do they share?** Node pool, Kafka cluster, S3 prefix, a common upstream
stream, a recently-deployed platform component, an availability zone. The answer is almost
always in that list.

**4. If one: is it lagging, stalled, or wrong?**
   - **Lagging** (consuming, behind, watermark advancing) → backpressure. Go to
     [11](11-backpressure-and-flow-control.md) Section 3.
   - **Stalled** (not consuming, or watermark frozen) → `E-01`, `E-03`, `C-05`, or a source
     problem.
   - **Wrong** (keeping up, output incorrect) → `E-04`, `C-07`, `C-12`, `D-01`, or a code change.

**5. What changed?** In order of likelihood: a job deploy, a schema registration, a producer
deploy, a config change, a platform component upgrade, an infrastructure change. Meridian's
incident dashboard overlays all six as annotations on every graph, which converts step 5 from an
investigation into a glance.

**6. Can you stop the bleeding without understanding it?** Roll back the most recent change,
shed load ([11](11-backpressure-and-flow-control.md) Section 6), or stop a consumer — which on a
log-based platform is recoverable. **Prefer reversible mitigation to diagnosis.** Understanding
can wait; the lag cannot.

## 3. Scenarios

### O-01 — The restore stampede

**What you see.** A zone event terminates 56 TaskManagers. 94 jobs restart simultaneously. S3
returns `SlowDown` to all of them. Restores that normally take 4 minutes take 40. Some jobs
exhaust their restart budget and stop.

**Mechanism.** Aggregate restore demand:

```
31 TB of state, most of it restoring at once
target: 90 MB/s per subtask × ~3,000 subtasks = 270 GB/s
S3 per-prefix limit: 5,500 GET/s
```

The bandwidth is available; the *request rate* per prefix is not, and every job's checkpoints
live under a path prefixed by the bucket and job ID.

**Confirm it.** `503 SlowDown` in TaskManager logs across many jobs, plus restore times far
above their measured norms.

**Recover.**

1. ⚠️ **Do not restart everything again.** The instinct when restores are slow is to bounce
   them; that resets the progress each one has made and re-queues the requests.
2. **Stagger.** Suspend the lower-priority jobs (Tier 3), let Tier 1 restore, then release the
   rest in batches. This requires a way to suspend a job quickly, which is worth having.
3. **Raise restart backoff** so the ones that fail do not immediately retry into the congestion.

**Prevent.**

- **Entropy in the checkpoint path** (`C-03`), which spreads across S3 partitions and largely
  removes the limit.
- **Task-local recovery** ([08](08-checkpointing-savepoints-and-recovery.md) Section 4) for
  large-state jobs, so a node failure does not mean a download.
- **Tiered restore priority**, implemented as a Kubernetes priority class plus an operator-level
  concurrency limit on job restores.
- **A quota on checkpoint storage per tenant** (`M-04`), because the tenant with 9 TB determines
  everyone's recovery time.

### O-02 — A platform component upgrade restarts every job

**What you see.** A Flink Kubernetes Operator upgrade rolls every job.

**Mechanism.** The operator's reconciliation loop detects a spec change (the image tag it
injects) and rolls the deployments.

**Prevent.** This is a known property of the operator pattern and the mitigations are
operational rather than technical:

- **Upgrade the operator in a window**, with the same care as a job deploy.
- **Verify that the operator upgrade does not change the pod spec** before applying — a dry-run
  diff.
- **Roll jobs deliberately in tiers** rather than letting the operator do it all at once. The
  operator supports a concurrency limit on reconciliations; set it.

### O-03 — Certificate expiry

**What you see.** At 00:00 UTC, every producer and every job fails to connect to Kafka.

**Mechanism.** The mTLS certificate expired. Everything that uses it fails at once, which is the
defining property of this failure: **no gradual degradation, no partial failure, total and
simultaneous.**

**Prevent.**

- **Alert at 30, 14 and 3 days** before expiry, on every certificate, from an inventory that is
  generated rather than maintained by hand.
- **Automate rotation** (cert-manager or equivalent) and then **test the rotation**, because an
  automated rotation that has never rotated is an untested code path.
- **Stagger expiry dates** so that a failure of the automation does not take everything at once.

This failure is boring and it is in the top three causes of total platform outages across the
industry. It is worth the effort precisely because it is boring enough to be neglected.

### O-04 — The control plane is down and the data plane is fine

**What you see.** The Flink operator, the JobManagers' HA store, or the Kubernetes API is
unavailable. Jobs keep processing. Nothing can be deployed, scaled or restarted.

**Mechanism.** Control and data planes are separate, which is the design working correctly. The
danger is the combination: if a data-plane incident happens now, you cannot respond.

**Recover.** Restore the control plane. Meanwhile, know what you can still do:

| Action | Available without the control plane? |
|---|---|
| Jobs keep running | Yes |
| Checkpoints keep being taken | Yes (JobManager-driven) |
| A failed task is rescheduled | Only if the JobManager and Kubernetes API are alive |
| Deploy, scale, restart a job | No |
| Change a Kafka quota | Yes — that is Kafka's own control path |
| Shed load | **Yes, if shedding is driven by a control topic** |

That last row is why Meridian's load-shedding switch
([11](11-backpressure-and-flow-control.md) Section 6) is driven by a **Kafka topic** rather than
by a configuration change: it keeps working when the deployment control plane does not. **Put
your emergency controls on the data plane.**

**Prevent.** Run the control plane with a higher availability target than the data plane, which
inverts most teams' instincts. And periodically verify that the emergency controls work with the
control plane deliberately disabled.

### O-05 — A bad deploy that cannot be rolled back

**What you see.** A job was deployed with a state-incompatible change. It is running and
producing wrong output. Rolling back fails because the new version's savepoint cannot be read by
the old version.

**Mechanism.** State schema evolution is a one-way door for incompatible changes (`K-08`). The
new job wrote a savepoint in a format the old job cannot read.

**Recover.** Restart the old version from the **savepoint taken before the deploy**, and accept
losing everything processed since. This only works if that savepoint was taken and retained,
which is why:

**Prevent.** **A retained savepoint before every deploy, kept for 7 days**, in a bucket with a
different lifecycle policy from checkpoints (`C-09`). This is the single most valuable
operational practice for stateful streaming and it costs storage and thirty seconds per deploy.

And: a CI check that loads the *previous* release's savepoint with the *new* release's
serializers, and vice versa where possible.

### O-06 — A schema change that broke eleven consumers

**What you see.** Eleven jobs fail within four minutes of a schema registration.

**Mechanism.** Covered in [03](03-the-event-schema-and-contracts.md). The operational question
is how to respond.

**Recover.**

1. **Identify the schema version and roll the producer back.** The registry's subject history
   gives you the timestamp; correlate with the failures.
2. **Do not roll the consumers forward one at a time** during the incident. Fix the source.
3. **Records already written with the new schema remain**, so consumers must be able to skip or
   dead-letter them (`S-12` in [03](03-the-event-schema-and-contracts.md) Section 7). If they
   cannot, that is the real bug.

**Prevent.** FULL_TRANSITIVE, a schema-change notification to every registered consumer, and a
canary consumer that reads every stream with the previous schema version and alerts on failure
— which catches the break before the real consumers do.

### O-07 — Kafka cluster degraded, jobs unaware

**What you see.** Three brokers are slow. Jobs report normal throughput and growing lag.

**Mechanism.** Flink's Kafka source will happily consume slowly. Its throughput metric reports
what it processed, which equals what it fetched, which is low. Nothing in the job says "I am
being starved."

**Confirm it.** The distinction from a processing bottleneck: the source is **busy** (`B-04`)
rather than backpressured, and Kafka's own metrics show high fetch latency.

**Recover.** Standard Kafka procedures — see [`../Kafka`](../Kafka/README.md). Move leadership
off the affected brokers.

**Prevent.** Alert on **fetch latency from the job's side**, not only on broker-side metrics. A
broker can look healthy to itself and slow to a client in a different zone.

### O-08 — The dead-letter topic that nobody read

**What you see.** A dead-letter topic has 180 GB and 14 months of records. Nobody has looked at
it. Some of those records were never reprocessed, so the datasets derived from that stream have
been quietly incomplete for over a year.

**Mechanism.** Dead-lettering converts a loud failure into a quiet one, which is the point — and
then nobody closes the loop.

**Prevent.** From [03](03-the-event-schema-and-contracts.md) Section 7, restated as an
operational rule:

- **Alert on the dead-letter rate**, not the depth.
- **A weekly review** that categorises what is in there and either fixes or explicitly accepts
  each category.
- **A replay tool** that exists and is tested, because a dead-letter topic without one is
  sentiment.
- **A retention on the dead-letter topic.** If a record has been there for 30 days unreprocessed,
  it is not going to be. Deleting it is honest; keeping it is a pretence that the loop is open.

### O-09 — Everything restarted because of an OOM in one operator

**What you see.** One operator has a memory leak. Its TaskManager is killed by the kernel. The
whole job restarts. Repeatedly.

**Mechanism.** A TaskManager hosts 16 slots from possibly several jobs. An OOM kills the JVM,
taking all 16 with it, which fails every job that had a subtask there.

**Confirm it.** `kubectl describe pod` shows `OOMKilled`. The JVM may have written a heap dump if
configured.

**Prevent.**

- **Configure the heap dump on OOM**, to a persistent volume. Without it, diagnosing the leak
  requires reproducing it.
- **Fewer slots per TaskManager for critical jobs**, which reduces the blast radius of one OOM
  from 16 subtasks to 4.
- **Memory limits that leave headroom for off-heap.** Flink's memory model has many components
  (heap, managed, network, metaspace, overhead) and the common cause of container OOM kills is
  off-heap growth — RocksDB, native libraries, direct buffers — within a container limit sized
  only for the heap.

### O-10 — Nobody knows what a job does

**What you see.** A job named `flink-job-prod-7` has been running for two years. Its repository
was archived. Two consumers depend on its output.

**Prevent.** The **job manifest**, required at submission, machine-readable, and the input to
half the platform checks mentioned in this collection:

```yaml
job: corridor-ad-attribution
owner: corridor-ads-platform
tier: 1
oncall: "#corridor-ads-oncall"
inputs:
  - stream: corridor.tracking.events
    guarantee: at-least-once
    handled_event_types: [AdImpression, AdClick]
outputs:
  - stream: corridor.attributions
    guarantee: at-least-once
    grain: "one row per (click_id)"
  - sink: clickhouse://corridor/attributions
    guarantee: effectively-once via ReplacingMergeTree(click_id)
state:
  estimated_bytes: 4.2e12
  ttl: 24h event time
sla:
  freshness_p99: 12s
  completeness: ">= 99.9% of clicks attributed within 24h"
runbook: https://wiki/.../corridor-ad-attribution
```

This document is what makes automatic lineage (`J-11`), guarantee-chain validation (`D-08`),
consumer notification (`S-10`) and ownership sweeps (`M-09`) possible. **It is the highest-value
piece of platform metadata and it costs each team twenty minutes once.**

### O-11 — The incident that was a query

**What you see.** The platform is paged for high broker CPU. The cause is an analyst running a
`kafka-console-consumer` from the beginning of `corridor.tracking.events` on their laptop.

**Mechanism.** A `--from-beginning` read of a 24-hour, 32-terabyte topic, from outside the
region, uncompressed on the wire.

**Prevent.**

- **A small default quota for unregistered principals** (`T-12` Section 7). This is the single
  configuration that prevents it.
- **A self-service tool** that does what the analyst wanted — sample the stream, tail it, search
  it — without a full read. If the only tool is `kafka-console-consumer`, people will use it.
- **Deny direct broker access from outside the platform's networks**, and provide the tool
  instead.

### O-12 — The pipeline that fixed itself and nobody knew why

**What you see.** Lag grew for 40 minutes and then resolved. No change was made.

**Mechanism.** Something transient: a broker leader election completed, a compaction finished, a
noisy neighbour's job ended, a network path recovered.

**Prevent.** Nothing to prevent — but **record it**. A self-resolving incident with no
explanation is a pattern that will repeat, and the second occurrence is much easier to diagnose
if the first was written down with its timestamps. Meridian keeps a log of unexplained events
with their time windows; three of them turned out to be the same cause, found by correlating
across months.

## 4. Four runbooks

### Runbook 1 — Consumer lag rising on a Tier 1 job

```
1. Check estimated_time_to_drain (T-01).
     Negative or infinite → the job has no surplus. Continue.
     Finite and < 15 min  → it is recovering. Note it and stop.

2. Check the job's watermark lag.
     Frozen  → E-01/E-03 (watermark stall), not a throughput problem. Go to runbook 3.
     Growing with lag → throughput problem. Continue.

3. Find the bottleneck (doc 11 §3).
     All subtasks busy   → capacity. Step 4.
     One subtask busy    → skew. K-03. Step 5.
     Nothing busy        → sink or external. B-03/B-01. Step 6.

4. Capacity:
     a. Is the rescale worth it? Compute (doc 04, T-01 arithmetic).
        Lag < ~94M records on the big jobs → wait, do not rescale.
     b. If yes: native savepoint, rescale, monitor.

5. Skew:
     a. Confirm the hot key from the key-frequency job.
     b. There is no fast fix. Shed (doc 11 §6) at level 3 if the metric
        tolerates sampling, else accept the lag and schedule the two-phase
        aggregation change.

6. Sink:
     a. Check the sink store's own health and ingest metrics.
     b. Check batch size and flush configuration.
     c. If the sink is down: decide drop / buffer / stop, per the job's
        contract. Do not invent the answer now.

7. If lag threatens retention (T-03): escalate immediately. Data loss is
   imminent and it is not recoverable. Extend retention on the topic NOW —
   it takes seconds and buys hours.
```

Step 7 is the one that must not be reached by accident. **Retention is the deadline.** If lag is
20 hours on a 24-hour topic, you have 4 hours before data is lost permanently, and extending
retention is a one-command change that should happen before any diagnosis.

### Runbook 2 — Checkpoints failing

```
1. Which phase? (doc 08, C-01)
     alignment high  → backpressure. Switch to unaligned NOW, then runbook 1.
     sync high       → large heap state, or a GC pause. Check jvm_gc.
     async high      → storage. Check for S3 SlowDown (C-03).

2. If unaligned is already on and alignment is still high:
     the alignment timeout may not be firing. Check
     execution.checkpointing.aligned-checkpoint-timeout.

3. Raise the checkpoint timeout so a slow checkpoint completes rather than
   failing and retrying from scratch. This is nearly always right.

4. DO NOT reduce the interval. DO NOT restart the job.

5. If checkpoints have been failing for longer than the source retention,
   escalate: a restart would now be unrecoverable.
```

### Runbook 3 — Watermark stalled

```
1. Read the current watermark value.
     Far in the future → E-04. Restart from a savepoint before the poison
                         record. Find it by searching for future timestamps
                         in the source around the stall time.
     Frozen at a past value → continue.

2. Per-subtask watermark, using min() not avg().
     One subtask behind  → E-02. Find its partitions; check broker health
                           and subtask placement.
     All subtasks frozen → continue.

3. Per-partition record rate.
     Some partitions at zero → E-03, idleness. Is withIdleness configured?
                               If not, that is the fix, and it requires a
                               deploy. In the meantime, consider whether a
                               synthetic heartbeat can be produced.
     All partitions flowing  → continue.

4. Check the timestamp assigner is attached and to the right operator.
   Check event_time is populated on recent records (a producer change can
   start emitting nulls, which become Long.MIN_VALUE).

5. For a job with multiple sources: which source has the minimum watermark?
   The join's slow side (E-09) is a common answer.
```

### Runbook 4 — Output is wrong, pipeline is healthy

This is the hardest one because there is no failing metric to follow.

```
1. Establish the direction and magnitude. Too high or too low? By how much?
   Constant offset or growing?

2. Too low, small percentage:
     a. numLateRecordsDropped (E-08). Most likely cause.
     b. Filter logic changed.
     c. A source partition not being consumed.

3. Too high:
     a. Duplicates (D-04). Check distinct event_id against total.
     b. Multiple window firings into an append-only sink (W-02).
     c. Join grain (J-04).
     d. A ReplacingMergeTree whose ORDER BY contains a mutable column.

4. Wrong for a bounded period then correct:
     → state did not restore (C-07). Check the JobManager startup log for
       "Skipped checkpoint state for operator".

5. Wrong only on replay / backfill:
     → a processing-time dependency (E-10), or a processing-time lookup
       where a temporal join was needed (doc 10 §3).

6. Wrong since a specific timestamp:
     → correlate with the change annotations. A schema registration and a
       producer deploy are the two most common.

7. If none of the above: run the reconciliation against the lake for the
   affected window and compare row by row for a single key. This is slow
   and it always works.
```

## 5. The gameday programme

Run one a month, in production, in a scheduled window, with the on-call present.

**The exercises, in the order Meridian introduced them:**

1. **Kill one TaskManager.** Measures single-task recovery.
2. **Kill a JobManager.** Verifies HA actually works.
3. **Make one broker slow** (with `tc netem`, not by killing it — slow is worse than dead and far
   less tested).
4. **Expire a producer's credentials.**
5. **Register an incompatible schema** on a staging stream and observe the blast radius.
6. **Inject a record with a timestamp in the year 2287** and see whether the clamp holds
   (`E-04`).
7. **Terminate an availability zone's worth of TaskManagers**, which is the `O-01` stampede.
8. **Disable the control plane** and verify the emergency controls still work (`O-04`).
9. **Fill a sink** — make ClickHouse reject writes — and see what each job does.
10. **Replay a day of history at 5×** into a live cluster and measure the impact on live jobs
    (`R-03`).

**What the first run reliably finds**, in Meridian's experience and in general:

- Recovery is 2–4× slower than anyone estimated, because nobody counted the catch-up.
- At least one Tier 1 job has no `uid`s (`C-07`).
- The alert that should have fired did not, because its threshold was set from a healthy period
  and the failure mode produces a *different* metric shape.
- At least one job's "what to do if the sink is down" is undefined, and the answer chosen under
  pressure is the wrong one.
- The runbook references a dashboard that has been renamed.
- Somebody's emergency control requires the control plane.

That last list is the argument for the programme. None of those are found by reading the design.

## What to take away

1. **Four platform-level failure shapes:** correlated restart, metastable degradation,
   control-plane failure, and silent degradation. The last is the most dangerous because it does
   not page.
2. **Triage by "one job or many", then "what do they share".** Two questions eliminate most of
   the search space.
3. **Prefer reversible mitigation to diagnosis.** Roll back, shed, or stop a consumer — on a
   log-based platform, stopping a consumer defers data rather than losing it.
4. **Retention is the deadline.** When lag approaches retention, extend retention first, before
   any diagnosis. It takes one command and buys hours.
5. **The restore stampede is the failure nobody sizes for.** Entropy in the checkpoint path,
   task-local recovery, tiered restore priority, and a per-tenant checkpoint-storage quota.
6. **Put emergency controls on the data plane**, driven by a control topic, so they survive a
   control-plane outage.
7. **Retain a savepoint before every deploy for 7 days.** It is the only rollback path for a
   state-incompatible change, and it costs thirty seconds.
8. **A dead-letter topic without a tested replay tool and a weekly review is sentiment**, and
   the datasets derived from that stream are quietly incomplete for as long as it is ignored.
9. **The job manifest is the highest-value platform metadata**: it makes lineage,
   guarantee-chain validation, consumer notification and ownership sweeps possible, and costs
   twenty minutes per team once.
10. **Record self-resolving incidents.** The third occurrence is diagnosed by correlating the
    first two, and only if they were written down.
11. **Run gamedays monthly.** The first one finds that recovery is 2–4× slower than estimated,
    that some job has no `uid`s, and that somebody's emergency control needs the control plane.
