# Failure Modes, and What You Do in the First Ten Minutes

An interviewer who is going to hire you for a bank will ask what breaks. Often as a direct
question — "what keeps you up at night about this design?" — and sometimes as a scenario: "it is
03:00 and the fraud dashboard shows zero transactions. Walk me through it."

This document is the catalogue. Each failure has a symptom, a cause, how it is detected, what you
do immediately, and what prevents it. The ordering is by how often they actually happen, not by
how dramatic they are, because the dramatic ones are rare and well-rehearsed while the common ones
are the ones that erode trust in the data.

Before the catalogue, one framing that is worth more than any individual entry.

---

## The two categories, and which one to fear

**Loud failures** stop something. A job crashes, a cluster is unreachable, a file does not arrive.
Someone is paged within minutes, the cause is usually in a log, and the data is recoverable
because the log retained it.

**Silent failures** produce wrong answers while everything reports healthy. A transform drops a
category of record. A deduplication window is too short and a re-run double-counts a day. A
compactor stops and nothing is wrong until queries time out six weeks later.

Monarch's five-year incident history, classified:

```
loud failures          194 incidents    mean time to detect   4 minutes
                                        mean time to resolve  38 minutes
                                        mean records affected recoverable, ~0 permanent

silent failures         11 incidents    mean time to detect   31 days
                                        mean time to resolve  9 days (after detection)
                                        mean records affected 1.4 million, often permanent
                                                              in a downstream copy
```

**Eleven silent failures caused more damage than one hundred and ninety-four loud ones.** Nearly
every control in document 08 — the reconciliation, the distribution checks, the nightly balance
re-derivation — exists to convert a silent failure into a loud one. That conversion is the single
most valuable thing the operations design does, and it is the sentence to lead with when asked
what worries you.

---

## F1. A Kafka broker fails

**Frequency:** several times a year, and during an AZ event, three at once.

**Symptom:** producer errors spike briefly; consumer lag rises on a third of partitions; nothing
is lost.

**What is happening:** the broker was the leader for roughly 1/18th of 4,680 partition replicas.
The controller elects new leaders from the in-sync replicas. Producers refresh metadata and
continue.

**Detection:** under-replicated partition count goes non-zero. This is the single most important
Kafka metric and the one to name if asked.

**Immediate response:** usually none. Verify under-replicated partitions returns to zero as the
replacement broker catches up, and verify that `min.insync.replicas` was satisfied throughout — if
it was not, some produces were rejected, and the ring buffers absorbed them.

**Prevention, which is configuration rather than heroics:**

```
replication.factor            3, spread across 3 availability zones
min.insync.replicas           2
acks                          all  (on money topics)
unclean.leader.election       false
```

The last one is the setting that genuinely gives something up.
`unclean.leader.election.enable=false` means that if all
in-sync replicas for a partition are unavailable, **that partition stays offline rather than
electing a replica that is behind.** It trades availability for the guarantee that a committed
record is never lost. On `auth.events` and `ledger.postings` that trade is obviously correct.
Monarch sets it `true` on `app.clickstream`, where losing some events is preferable to a stalled
partition — and being able to explain why the same cluster has both settings is a good answer to
"how do you think about availability versus durability."

---

## F2. The feature job dies

**Frequency:** monthly, counting deployments and task manager failures.

**Symptom:** feature freshness lag climbs. Fraud model scores continue, computed from features
that stopped updating.

**What is happening:** this is the designed degradation from document 04. Authorizations continue.
Features are stale. Model accuracy declines gradually — the velocity features are the ones that go
stale fastest and matter most, so the model becomes progressively blind to in-progress attacks.

**Detection:** feature freshness measured as `now() - max(feature_updated_at)` sampled from Redis,
not from Flink's own metrics. Flink reporting itself healthy while writing nothing is a real
failure mode, so the check must be on the output.

**Immediate response, as a ladder:**

| Freshness | Action |
|---|---|
| Under 5 s | Normal |
| 5–60 s | Alert. Job is probably recovering from a checkpoint; wait |
| 60 s – 10 min | Page. Restart the job from the last checkpoint |
| Over 10 min | Notify the fraud operations team so they can **raise rule thresholds manually** — compensating for a blind model with blunter static rules |
| Over 60 min | Treat as a fraud exposure event; consider lowering approval limits on high-risk segments |

The fourth row is the one that distinguishes a data engineer from a data engineer at a bank. **The
response to a degraded model is a business action by another team, not only an engineering
action**, and the runbook has to name who makes it.

**Prevention:** checkpoint every 30 seconds so recovery replays little; run with task manager
redundancy so a single node loss is a 40-second blip rather than a full restart; and keep
`maxParallelism` set so a rescale never requires a state rebuild.

---

## F3. A Redis shard is lost

**Frequency:** once or twice a year.

**Symptom:** feature reads for one slot range fail or return empty. Roughly 1/N of authorizations
get a default feature vector.

**What is happening:** a node failed and the replica is promoting, or data was lost between the
last replication and the failure.

**Immediate response:** the decision service must **treat an empty feature read as a known state,
not as zero.** A missing velocity counter read as `0` tells the model "this card has had no
activity," which is the single most fraud-friendly value possible. Returning a sentinel that the
model interprets as "unknown" and routing those transactions to a conservative static rule set is
correct.

This is a genuinely easy mistake to make, because the natural implementation of "get the counter,
default to zero if absent" is exactly wrong here. **The absence of a feature must be
distinguishable from a feature whose value is zero.**

**Recovery:** repopulate the shard from DynamoDB — under two minutes. If both are lost, rebuild
from Kafka — 21 minutes.

**Prevention:** replica per shard, and a synthetic check that reads a known card's features every
10 seconds and alerts on an empty result rather than on an error, because an empty result is the
dangerous case.

---

## F4. A hot partition appears

**Frequency:** twice in five years, both after an upstream change.

**Symptom:** one partition's lag grows while 239 are fine. Group-average lag looks normal.

**What happened the first time:** a co-brand partner began submitting its stored-credential
recurring charges under a single technical account rather than per-customer accounts. Keyed by
`account_id`, that account became 4% of all authorizations on one partition — a 9.6× skew that
the partition's consumer could not keep up with.

**Detection:** maximum per-partition lag, as document 03 insisted. On the group average this
incident is invisible.

**Immediate response:** there is no quick fix, which is the uncomfortable truth worth stating.
You cannot parallelise within a partition. The options are all bad in the moment: increase the
single consumer's resources (helps a little), accept the lag, or stop the upstream behaviour.
Monarch did the third — the partner was asked to revert — and then built the prevention.

**Prevention:** a daily job computes the distribution of records per key and alerts when any single
key exceeds 0.5% of a topic's daily volume. That turns the next occurrence into a warning before
it is an incident. It is a cheap job and it would have caught this one three days early.

---

## F5. A poison record stops a consumer

**Frequency:** a few times a year.

**Symptom:** one consumer, or one partition of one consumer, stops advancing. The same record is
processed, fails, and is retried forever.

**What is happening:** a record that passes schema validation but violates an assumption in the
transform — a negative amount where only positives were expected, a null in a field the code
dereferences, a timestamp in 1970 or in 2098.

**Detection:** the "no progress" alarm from document 03, not the lag threshold. Lag grows slowly
on one partition and may not cross a threshold for hours, while "committed offset has not advanced
in 5 minutes" fires immediately.

**Immediate response:** do not delete the record and do not skip past it blindly. The sequence is:
capture the record, advance past it into a quarantine topic, and **count it**. A skipped record
that is not counted is a silent loss, which is the expensive category.

**Prevention:** every transform has a per-record try/catch that routes failures to quarantine
rather than failing the task, with a cap — if quarantine exceeds 0.01% of throughput, fail the job
instead, because at that point something systemic is wrong and continuing to quarantine is
discarding data at scale.

The balance is the point: fail-fast on individual bad records discards nothing but stops
everything; quarantine-always keeps running but can silently discard a day. The cap gets both.

---

## F6. The allowlist silently drops a new field

**Frequency:** twice in five years. Both took over a month to detect.

**Symptom:** none. Everything is healthy. A field that should be in the lake is not, or a category
of transaction is under-counted.

**What is happening:** document 02's field allowlist did its job — an unclassified field was
dropped. But nobody was told, because the drop counter was a metric on a dashboard rather than an
alert.

**Detection:** this is what the distribution checks in document 08 are for. A new entry mode, a
new response code, or a new currency appearing in the clearing file and not in the lake shows up
as a four-standard-deviation move in a category's share.

**Immediate response:** classify the field, add it to the allowlist, and **reprocess from `raw`**
— which works only if `raw` retained the full original payload rather than the allowlisted subset.
Monarch stores the complete original message in `raw.auth_events` as an opaque binary column
alongside the parsed fields, precisely so this recovery exists. That column costs about 340 GB a
year compressed — roughly $16 a year in Glacier Instant — and has twice been the difference
between a reprocess and permanent data loss.

**Prevention:** alert on *any* unclassified field appearing, at a rate above zero. The whole point
of an allowlist is to make the unknown visible, and a metric that nobody looks at does not make
anything visible.

---

## F7. The compactor stops

**Frequency:** three times in five years, usually a permissions change.

**Symptom:** nothing, for about two weeks. Then dashboard queries get slower. Then a reporting job
that used to take 40 minutes takes four hours. Then it times out.

**Detection:** average file size per partition, alerting below 64 MB, as document 05 specified. Not
"did the compaction job succeed" — that check misses the case where the job succeeds while
compacting nothing because its filter no longer matches anything.

**Immediate response:** run compaction on the affected partitions, oldest first. Expect it to take
hours; a month of un-compacted clickstream is 2.6 million files.

**Prevention:** monitor the *outcome* rather than the job. This generalises: for every maintenance
job in the system — compaction, snapshot expiry, retention, balance re-derivation — alert on the
state of the world it is supposed to maintain, not on its exit code. A job can exit zero and do
nothing.

---

## F8. The clearing file is late, short, or wrong

**Frequency:** several times a year.

**Symptom:** the reconciliation does not run, or runs and reports enormous breaks.

**Three distinct cases, three different responses:**

**Late.** The file has not arrived by 03:00. The reconciliation waits until 05:30 and then runs
against what is present, flagging the missing file explicitly. **It does not silently reconcile a
partial day**, because a reconciliation that reports a 40% break when 40% of the file is missing
trains everyone to ignore breaks.

**Short.** The trailer count does not match the detail records. Document 02's rule applies:
publish nothing, request retransmission. Do not ingest 94% of a day.

**Corrected.** A file arrives with the same logical identifier and a different checksum. Stop, do
not auto-apply. Applying a correction changes ledger postings, which changes customer statements.
The response is a human decision with a recorded approval, every time.

**Prevention:** a file-arrival SLO per sender with its own alert, separate from the reconciliation,
so "the file did not arrive" is a distinct incident from "the reconciliation failed."

---

## F9. A backfill starves the live pipeline

**Frequency:** the first two or three times anyone runs a large backfill.

**Symptom:** live consumer lag rises across the board during a historical reprocess.

**What is happening:** the backfill job reads Kafka at maximum speed, saturating broker network
and page cache, and competing with live consumers for the same partitions' data. On the sink side
it writes into the same Iceberg tables, causing commit conflicts and retries.

**Immediate response:** throttle or stop the backfill. Live traffic wins, always.

**Prevention, three mechanisms:**

- **Backfill reads from the lake, not from Kafka**, wherever possible. The lake has no live
  consumers to starve and no shared page cache. This is one of the strongest arguments for the
  lake existing at all.
- **Backfill writes to a branch, not the main table** (document 05's reprocessing procedure), so
  there are no commit conflicts with the live writer.
- **Separate Kafka quotas** by client ID, so that even an unavoidable Kafka-reading backfill is
  capped at a fraction of broker capacity.

---

## F10. Checkpoints start failing under load

**Frequency:** a few times a year, usually after state grows.

**Symptom:** checkpoint duration climbs toward the checkpoint interval. Eventually checkpoints
time out, and the job cannot be safely restarted because the last successful checkpoint is hours
old.

**What is happening:** 640 GB of state being written to S3 while the job continues processing.
When the write takes longer than the interval, checkpoints queue and the situation compounds.

**Detection:** checkpoint duration as a percentage of checkpoint interval, alerting at 50%. By the
time checkpoints are failing it is late.

**Immediate response:** increase the checkpoint interval to stop the compounding, then address the
cause — usually state growth, sometimes a state TTL that was never set.

**Prevention:** incremental checkpoints (only changed RocksDB files are uploaded), state TTL on
every keyed state so nothing grows unbounded, and an explicit review of state size against a
budget whenever a new feature is added. **Unbounded keyed state is the most common way a streaming
job becomes unoperable**, and it is always introduced by a feature that seemed small.

---

## F11. Someone resets offsets to the wrong place

**Frequency:** rare, and severe when it happens.

**Symptom:** either a gap (reset forward — records never processed) or mass duplication (reset
back — records reprocessed).

**Why it happens:** a consumer is stuck, the fast fix is to reset offsets, and under time pressure
the reset is to `earliest` or `latest` rather than to a specific offset.

**The asymmetry that should drive the runbook:** a reset *backward* is recoverable, because the
deduplication in document 08 absorbs the duplicates for anything within 48 hours. A reset
*forward* is a permanent gap unless the offset continuity check catches it. **So the runbook says:
when in doubt, reset backward.**

**Detection:** the Kafka offset continuity check from document 08 detects a forward reset the next
time the lake writer commits.

**Prevention:** offset resets on money topics require two people, implemented as a tool rather than
a policy — the CLI refuses without a second approval. Policies about who may run a command are not
enforcement.

---

## F12. A dashboard query costs $40,000

**Frequency:** once, which was enough.

**Symptom:** the warehouse bill for a week is four times normal.

**What happened:** a BI tool's new dashboard had a filter that generated a query without the
partition predicate, scanning the full `fact_authorization` history on every refresh, refreshing
every 60 seconds, for eleven days before anyone looked at the bill.

**Detection:** per-query bytes scanned, with an alert on any single query above a threshold, and a
daily spend anomaly check. Billing is a monthly artefact; engineering needs a daily one.

**Immediate response:** kill the query, add a resource monitor.

**Prevention, in order of effectiveness:**

- **Hidden partitioning** (document 05) so a filter on `event_time` prunes without the author
  knowing the partition scheme. This eliminates the most common version of the bug entirely.
- **A per-role query cap** on bytes scanned, so a BI service account cannot scan seven years.
- **Spend alerts at a daily granularity**, with a named owner per warehouse.

The first of those is the one to emphasise: **this class of incident is mostly a design failure,
not a user error.** Requiring every analyst to know the physical partition scheme guarantees that
some of them will not.

---

## F13. The ledger does not balance

**Frequency:** four times in five years, all from code bugs.

**Symptom:** the nightly balance re-derivation (document 08) finds an account whose closing balance
does not equal the sum of its postings, or the daily control report's B-versus-C comparison is not
zero.

**This is the most serious failure in the catalogue** and it is treated differently from all the
others: it is a potential financial misstatement, and the response involves the controller's
office, not only engineering.

**Immediate response:**

1. **Stop the general ledger feed.** Do not publish a number you cannot stand behind.
2. **Quantify the scope** — how many accounts, what total amount, what date range. The scope
   determination is the first thing anyone will ask for and it has to be exact.
3. **Determine whether customers were affected** — did anyone see a wrong available balance, was
   anyone declined because of it.
4. **Do not correct the postings.** A wrong posting is corrected by a **reversing entry plus a
   correct entry**, both dated today with an effective date of the original. The original stays.
   Overwriting it destroys the audit trail, which turns a correctable error into a control failure.

Point 4 is the accounting discipline that engineers most often get wrong, because the engineering
instinct is to fix the data. **In a ledger you never fix a record; you add records that correct
its effect.**

**Prevention:** the balanced-batch constraint, the unique `posting_id`, and the nightly
re-derivation — all three, because each catches what the others miss.

---

## The first ten minutes, as a procedure

When something is wrong and you do not yet know what:

**Minute 0–2: classify, do not diagnose.** Is this loud or silent? Is money affected? Is the
authorization path affected? The third question has a fast answer — authorization availability is
monitored independently of the pipeline, and if it is green, nothing you do in the next ten
minutes is customer-facing. That knowledge changes how fast you move.

**Minute 2–4: establish whether data is at risk or only late.** Lag is not loss. If records are
in Kafka and a consumer is behind, nothing is lost and the clock is only on the freshness SLO. If
records are being dropped — ring buffer full, quarantine climbing, offsets skipped — the clock is
on permanent loss and that is a different urgency.

**Minute 4–6: stop the bleeding before diagnosing it.** Pause the backfill. Raise the checkpoint
interval. Throttle the heavy consumer. Stop the downstream feed that would publish wrong numbers.
These are reversible and they buy time.

**Minute 6–10: identify the blast radius and tell someone.** Which consumers are affected, which
SLOs are breached, which business teams need to compensate (the fraud operations threshold
change from F2 is the standard example). **Declare the incident to the people who can take
compensating action before you have a root cause**, because their actions take time to have
effect.

The ordering is deliberate: classification before diagnosis, containment before understanding,
communication before resolution. The instinct runs the other way — find the cause first — and that
instinct costs the most during the failures that matter.

---

## What to take away

1. **Eleven silent failures caused more damage than one hundred and ninety-four loud ones** —
   31 days to detect versus 4 minutes. Nearly every control in this design exists to convert
   silent failures into loud ones, and that conversion is the most valuable thing the operations
   design does.
2. **`unclean.leader.election.enable=false` on money topics and `true` on clickstream.** Being able
   to explain why one cluster has both is a good answer to the availability-versus-durability
   question.
3. **Monitor the output, not the job.** Feature freshness from Redis, not Flink's metrics; average
   file size, not the compactor's exit code. A job can report success and do nothing.
4. **The response to a degraded fraud model is a business action by another team.** The runbook
   names who raises rule thresholds, and that row is what makes it a bank's runbook.
5. **An absent feature must not read as zero.** A missing velocity counter defaulting to zero tells
   the model the card has had no activity, which is the most fraud-friendly value available.
6. **Alert on maximum per-partition lag and on absence of progress**, not on group-average lag — a
   hot partition and a dead consumer are both invisible to the average.
7. **Quarantine individual bad records, but cap it.** Fail-fast stops everything for one bad
   record; quarantine-always silently discards a day. A 0.01% cap gets both behaviours.
8. **Keep the complete original message in `raw`** alongside the parsed fields — 340 GB a year —
   because it is the only thing that makes recovery from a dropped-field bug possible.
9. **Backfill from the lake, write to a branch, and quota Kafka clients.** Live traffic wins
   always, and the lake existing is largely what makes that easy.
10. **Unbounded keyed state is the most common way a streaming job becomes unoperable**, and it is
    always introduced by a feature that seemed small. Set a TTL on every keyed state.
11. **When resetting offsets under pressure, reset backward.** Duplicates are absorbed by
    deduplication; a forward reset is a permanent gap.
12. **A runaway query is a design failure, not a user error.** Hidden partitioning removes the
    class; per-role scan caps and daily spend alerts catch the remainder.
13. **In a ledger you never fix a record — you add a reversing entry and a correct one**, both
    dated today with the original effective date. Overwriting turns a correctable error into a
    control failure.
14. **First ten minutes: classify before diagnosing, contain before understanding, communicate
    before resolving.** The instinct runs the other way and it costs the most when it matters.
