# Observability and Alerting for Scheduled Work

Doc 00 ended on an uncomfortable fact: nothing in the CronJob object will ever tell you a CronJob
is unhealthy. `kubectl get cronjob` shows a job that has failed 400 consecutive times exactly the
same way it shows a job that has never failed. This doc is how you close that gap.

The central argument is that **monitoring scheduled work is a freshness problem, not an
availability problem**, and that almost everyone's first instinct — alert when a job fails — is
the wrong primitive.

## What "healthy" even means for a job that is not running

A service is healthy if it is responding now. A CronJob is not running 99% of the time, so "is it
up" is meaningless. The property you actually care about is:

> **A successful run has completed recently enough that its output is still trustworthy.**

That phrasing gives you a measurable SLO. For `invoice-rollup`, "every hour of orders is rolled up
within three hours of that hour ending." For `payout-settlement`, "every day's payouts complete
before 06:00 UTC." For `cert-expiry-audit`, "the certificate inventory is never more than 48 hours
old." Each of those is checkable from one number — the age of the last success — and each maps
directly onto a business commitment rather than onto a Kubernetes concept.

Monitoring then has to answer four questions, and they line up with the failure classes from doc
04 because both derive from the same object chain:

| Question | Failure class it catches | Signal |
|---|---|---|
| Did it **fire**? | A: F-01, F-02, F-06 | `lastScheduleTime` advancing |
| Did it **finish**? | B: F-08, F-09, F-10 | `status.active` staying non-zero; Job duration |
| Did it **succeed**? | C: F-14, F-15 | Job `Complete` vs `Failed` condition |
| Did it do the **right thing**? | C: F-13, F-16 | your own outcome metric — nothing Kubernetes has |

### Why alerting on failures alone leaves you blind

Take the catalogue from doc 04 and ask, for each entry, whether a "job failed" alert would fire:

| Failure | Does a failure alert fire? | Why |
|---|---|---|
| F-01 missed-schedule wall | **No** | Nothing was created, so nothing failed |
| F-02 suspended and forgotten | **No** | Suspension is not a failure |
| F-06 `FailedCreate` (quota, webhook) | **No** | The *Job* never existed; the CronJob object records an event |
| F-08 hung job plus `Forbid` | **No** | The job is `active`, not failed — indefinitely |
| F-13 exit 0 on failure | **No** | Kubernetes was told it succeeded |
| F-16 skipped window | **No** | A skip is not a failure |
| F-14 OOMKilled with retries exhausted | Yes | The Job gets a `Failed` condition |
| F-10 preemption exhausting retries | Yes | Same |

**Six of the eight most damaging failures produce no failure event at all.** That is the case for
inverting the default: make the primary alert *staleness*, and treat failure alerts as a
secondary, faster signal that happens to arrive earlier when it applies.

## The metrics that exist

Most of what you need comes from **kube-state-metrics**, which translates API objects into
metrics. The CronJob-relevant series:

| Metric | What it gives you |
|---|---|
| `kube_cronjob_status_last_schedule_time` | Unix time of the last firing — "did it fire" |
| `kube_cronjob_status_last_successful_time` | Unix time of the last successful run — **the most important series in this doc** |
| `kube_cronjob_next_schedule_time` | Unix time of the next expected firing |
| `kube_cronjob_status_active` | Count of active Jobs — overlap and hang detection |
| `kube_cronjob_spec_suspend` | 1 if suspended — F-02 |
| `kube_cronjob_info` | Labels carrying `schedule` and `timezone`, useful for annotating alerts |
| `kube_job_status_start_time` / `_completion_time` | Job duration, by subtraction |
| `kube_job_failed` / `kube_job_complete` | Terminal Job conditions (set only once the Job gives up or finishes) |
| `kube_job_status_failed` | Count of failed pods — attempts, not Jobs |
| `kube_job_owner` | The join key from a Job to its owning CronJob |

Three practical notes before you write queries against these:

⚠️ **`kube_job_owner` is how you attribute a Job to a CronJob.** As doc 04 noted, there is no
built-in label linking them, so every per-CronJob aggregation over Job metrics needs this join.
The shape is always the same:

```promql
some_job_metric
  * on(job_name, namespace) group_left(owner_name)
    kube_job_owner{owner_kind="CronJob"}
```

⚠️ **Check that `kube_cronjob_status_last_successful_time` is actually exported.** The underlying
field (`.status.lastSuccessfulTime`) has existed since 1.21, but some kube-state-metrics
deployments run with a metric allowlist that omits it, and older versions did not export it at
all. Verify before designing your alerting around it:

```bash
kubectl -n monitoring exec deploy/kube-state-metrics -- \
  wget -qO- localhost:8080/metrics | grep -c kube_cronjob_status_last_successful_time
```

If it is missing, the fallback is to derive last success from Job completion times joined through
`kube_job_owner` — workable, but it stops working the moment TTL deletes the Job, which is
exactly why the purpose-built series is better.

⚠️ **Job and pod metrics disappear when the objects do.** With `ttlSecondsAfterFinished: 3600`,
any query with a lookback longer than an hour sees gaps that are cleanup, not failure. This is
the main argument for the run ledger in doc 05: it is the only record whose lifetime you control.

## The alerts worth having

Six rules cover the fleet. Every threshold below is derived, not chosen, because an unexplained
threshold is a threshold nobody will maintain.

### 1. Staleness — the one that matters most

```yaml
- alert: CronJobNoRecentSuccess
  expr: |
    time() - kube_cronjob_status_last_successful_time > 10800
  for: 5m
  labels:
    severity: page
  annotations:
    summary: "{{ $labels.namespace }}/{{ $labels.cronjob }} has not succeeded in over 3h"
    runbook: "https://wiki.riverbend.io/runbooks/{{ $labels.cronjob }}"
```

Deriving the threshold for `invoice-rollup`: allow for one entirely missed firing (so a single
transient failure does not page), plus a slow run at p99, plus a margin.

```
  2 × schedule interval        = 2 × 3600s = 7200s
+ p99 run duration             =            2040s
+ margin                       =             ~500s
                               = 9740s  ->  round to 10800s (3h)
```

This one alert catches F-01, F-02, F-06, F-08, F-16, and most of F-13 — every failure whose
signature is "no successful run happened" regardless of the reason. It is the closest thing to a
universal CronJob health check.

The threshold has to be **per job**, since 3 hours is right for an hourly rollup and absurd for a
weekly vacuum. Carry it on the object so the alert rule stays generic:

```yaml
metadata:
  annotations:
    riverbend.io/max-success-age-seconds: "10800"
```

Then either template the rule per tier, or export that annotation as a metric with a small
exporter and compare against it. The tier label from doc 07 (`riverbend.io/tier`) is usually
enough granularity in practice — three tiers, three thresholds, three rules.

⚠️ A staleness alert on a *weekly* job is nearly useless as a page, because it fires up to a week
after the problem started. For anything weekly or rarer, alert on the **absence of a run when one
was expected** instead, using `kube_cronjob_next_schedule_time`:

```promql
# A firing was expected more than 30 minutes ago and no Job has appeared since.
(time() - kube_cronjob_next_schedule_time > 1800)
  and on(namespace, cronjob) (kube_cronjob_status_active == 0)
```

### 2. Job failed outright

```yaml
- alert: CronJobRunFailed
  expr: |
    kube_job_failed{condition="true"} == 1
      * on(job_name, namespace) group_left(owner_name)
        kube_job_owner{owner_kind="CronJob"}
  for: 1m
  labels:
    severity: ticket        # deliberately not a page
```

Note two deliberate choices. `kube_job_failed` reflects the Job's terminal condition, so it fires
only after the retry budget is spent — a job that fails twice and then succeeds does not alert,
which is correct, because that is retries working as designed. And the severity is a ticket, not a
page: a single failed run of a retry-tolerant job is tomorrow's work. The *page* comes from the
staleness rule when failures persist. Getting this split right is most of what separates useful
CronJob alerting from noise people mute.

### 3. Running too long

```yaml
- alert: CronJobRunningTooLong
  expr: |
    (time() - kube_job_status_start_time > 5400)
      * on(job_name, namespace) group_left(owner_name)
        kube_job_owner{owner_kind="CronJob"}
      and on(job_name, namespace) kube_job_status_active > 0
  for: 5m
```

This catches a hang (F-08) *before* the staleness threshold does, which matters when `Forbid` is
in play, because every minute of hang is another skipped firing. Set the threshold above
`activeDeadlineSeconds` and it will never fire — the deadline kills the job first. Set it at
roughly 1.5 × p99 duration and it warns you that this run is abnormal while there is still time
to look.

### 4. Suspended too long

```yaml
- alert: CronJobSuspendedTooLong
  expr: kube_cronjob_spec_suspend == 1
  for: 24h
  labels:
    severity: ticket
  annotations:
    summary: "{{ $labels.cronjob }} has been suspended for over 24h — intentional?"
```

`for: 24h` does the work here: a suspension during an incident is normal and should not alert, and
one that outlives the incident should. This is F-02, and it is a two-line rule for a failure mode
that regularly lasts months.

### 5. Overlap in progress

```yaml
- alert: CronJobOverlappingRuns
  expr: kube_cronjob_status_active > 1
  for: 5m
```

Under `Forbid` this should be impossible, so if it fires you have learned something important
about your assumptions — a manual run, a second controller, or a stale `.status.active`. Under
`Allow` it is informational, and the number to watch is doc 02's duration-to-interval ratio
instead.

### 6. Fleet-wide breakage

```yaml
- alert: ManyCronJobsStale
  expr: |
    count(time() - kube_cronjob_status_last_successful_time > 86400) > 10
  for: 15m
  labels:
    severity: page
```

Ten unrelated CronJobs going stale together is not ten coincidences. It is one cause: an admission
webhook failing closed, a namespace-wide quota exhaustion, a control-plane problem, or a node pool
that no longer matches everyone's `nodeSelector`. This alert exists because the per-job alerts,
firing 40 at once, communicate "40 problems" when the truth is one — and the response to one
shared cause is completely different.

### 7. The series that vanished

```yaml
- alert: CronJobMissing
  expr: absent(kube_cronjob_info{namespace="billing", cronjob="payout-settlement"}) == 1
  for: 15m
  labels:
    severity: page
```

For tier-1 jobs only, and it covers the case no other rule can: the CronJob was **deleted**. A
deleted CronJob has no metrics at all, so every threshold-based rule about it silently stops
evaluating. This is the difference between "our alerting says nothing is wrong" and "our alerting
has nothing to say."

## Outcome metrics: the part Kubernetes cannot give you

The fourth question — did it do the *right* thing — has no Kubernetes signal. Exit code 0 is the
only thing the platform knows, and F-13 showed how easily that lies.

So the job has to report on itself. The obstacle is that **Prometheus pulls, and your job is
gone before the next scrape.** `session-reaper` runs for 20 seconds against a 30-second scrape
interval; most runs would never be scraped at all, and the ones that were would be sampled at a
random point mid-run. Pull-based scraping is structurally wrong for short-lived work.

Three workable approaches, in the order I would reach for them:

**1. Derive metrics from the run ledger (preferred).** Doc 05's `job_runs` table already records
status, duration, and `rows_written` per run, durably. A small exporter turns the table into
metrics:

```promql
riverbend_job_last_success_age_seconds{job_name="invoice-rollup"}
riverbend_job_rows_written{job_name="invoice-rollup"}
```

This has a property nothing else has: **it survives object cleanup and cluster rebuilds**, because
it lives in your database. It is also queryable in SQL for the gap analysis of F-16. If you are
going to build one mechanism, build this one.

**2. Push to a Pushgateway.** The conventional answer for batch jobs:

```python
# The grouping key must identify the JOB, not the run — otherwise each run creates a
# new series and the gateway accumulates them forever.
push_to_gateway(
    "pushgateway.monitoring:9091",
    job="invoice-rollup",
    grouping_key={"namespace": "billing"},
    registry=registry,
)
```

⚠️ The Pushgateway **never forgets**. Metrics persist until explicitly deleted, so a job that is
decommissioned leaves a series frozen at its last value forever — and a staleness alert built on
that series will never fire again, because the series is present and unchanging. It is a monitor
that quietly stops monitoring. If you use the Pushgateway, delete the group when a job is retired,
and prefer "age of last success computed from a pushed timestamp" over "a boolean success gauge",
so a frozen series looks stale rather than healthy.

**3. OTLP push to a collector.** If you already run an OpenTelemetry collector, jobs can push
metrics and traces over their lifetime and exit. This avoids the Pushgateway's memory problem and
gives you traces for free (below). It is the best answer when the collector already exists and
poor value if it would exist only for this.

Whichever you choose, the metrics worth emitting are the same, and they are about *effect*:

| Metric | Why |
|---|---|
| `rows_processed` / `rows_written` | The F-13 detector. Zero when it should be thousands is the alert. |
| `duration_seconds` by phase | Tells you *which* phase got slower when a 7-minute job becomes 34 |
| `watermark_lag_seconds` | For watermark jobs (doc 05), how far behind real time the data is — the most business-meaningful number available |
| `items_failed` | Partial failure inside a "successful" run — the run succeeded, 40 records did not |

`watermark_lag_seconds` deserves highlighting, because it collapses the whole of this doc into one
number for pipeline-shaped jobs. It does not care whether the cause was a skipped firing, a hang,
a slow run, or a broken schedule. If invoice data is 90 minutes behind, that is the alert,
regardless of mechanism.

An outcome alert then looks like:

```yaml
- alert: InvoiceRollupWroteNothing
  expr: riverbend_job_rows_written{job_name="invoice-rollup"} == 0
  for: 10m
  annotations:
    summary: "Rollup reported success but wrote zero invoice lines"
```

## Logs

Two things make job logs useful rather than merely present.

**Ship them off-cluster before you set aggressive TTLs.** Deleting a Job deletes its pods, and
deleting a pod deletes the only copy of its logs (doc 03). The ordering is non-negotiable: log
shipping first, then TTL.

**Put the run key in every line.** Structured logs keyed by the idempotency key from doc 05 make
the two questions you always ask answerable directly:

```json
{"ts":"2026-09-11T15:12:04Z","level":"info","job":"invoice-rollup",
 "run_key":"2026-09-11T15:10:00Z","attempt":"invoice-rollup-29818940-4qj7x",
 "phase":"aggregate","rows":38000,"msg":"batch committed"}
```

With `run_key` and `attempt` present, "show me everything that happened on the 15:10 run,
including both attempts" is one query — and without them, correlating attempt 1's failure with
attempt 2's success across expired pods is guesswork.

## Traces

One trace per run, with a span per phase, answers the question metrics cannot: *where did the time
go?* When `invoice-rollup` moves from 7 minutes to 34, the useful finding is not the total but
that the `fetch-orders` span went from 40 seconds to 26 minutes because a query lost its index.

Two conventions make this work for batch:

- **Use the run key as a baggage item or attribute**, so traces, logs, metrics, and ledger rows
  all join on the same identifier.
- **Sample batch traces at 100%.** A job that runs 288 times a day produces 288 traces, which is
  nothing, and the one you need is always the anomalous run you did not sample.

## Dashboards worth building

Two, not twelve.

**A fleet table**, one row per CronJob, sorted by staleness in units of its own interval:

| CronJob | Tier | Schedule | Last success | Intervals behind | Last duration | p95 duration / interval |
|---|---|---|---|---|---|---|

"Intervals behind" is the key column: it normalises a weekly job and a five-minute job onto the
same scale, so one sorted table surfaces the worst problem in a 412-job fleet at a glance. The
last column is doc 02's overlap-risk ratio, and anything above 50% is a capacity conversation.

**A per-job panel set** for when you are looking at one job: duration p50/p95 against the schedule
interval, run outcomes over time, the outcome metric (`rows_written`), memory working set against
the limit, and the events stream. That set answers "is this normal for this job?" in about ten
seconds, which is the question you have at 03:00.

## Two things that expire behind your back

**Events.** `FailedCreate` and `TooManyMissedTimes` are the highest-signal diagnostics in the whole
collection, and they are deleted after about an hour by default. A CronJob stuck for three days
shows no event explaining why (F-01). Run an event exporter that writes events to your logging
backend, and you convert the most valuable signals from "only visible if you happen to look within
the hour" into history. For a fleet of scheduled jobs this is one of the highest-value pieces of
observability plumbing available, and it is usually a single Deployment.

**Your monitoring itself.** Every alert in this doc depends on kube-state-metrics running, being
scraped, and rules being evaluated. If that chain breaks, every rule goes quiet — which is
indistinguishable from everything being fine. The standard remedy is a heartbeat: a trivial
CronJob whose only purpose is to prove the pipeline works end to end.

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: monitoring-heartbeat
  namespace: platform
spec:
  schedule: "*/5 * * * *"
  timeZone: "Etc/UTC"
  startingDeadlineSeconds: 120
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      backoffLimit: 0
      activeDeadlineSeconds: 60
      ttlSecondsAfterFinished: 600
      template:
        spec:
          restartPolicy: Never
          automountServiceAccountToken: false
          containers:
            - name: heartbeat
              image: registry.riverbend.internal/platform/heartbeat@sha256:7b3e...
              args: ["--emit-run-record"]
              resources:
                requests: { cpu: "10m", memory: "32Mi" }
                limits:   { memory: "32Mi" }
```

Then alert on the heartbeat going stale with a tight threshold, and route it as a monitoring
problem rather than an application one. It tests the whole chain at once: the CronJob controller is
firing, the scheduler is placing pods, images are pulling, kube-state-metrics is exporting, and
your rules are evaluating. When the heartbeat is quiet and nothing else is complaining, believe the
heartbeat.

## What to take away

1. Health for scheduled work is **freshness**, not availability: "a successful run completed
   recently enough that its output is trustworthy." That sentence is your SLO.
2. Alerting only on job failures misses six of the eight most damaging failure modes, because
   nothing fails when a job is never created, never finishes, or lies about succeeding.
3. Make `time() - kube_cronjob_status_last_successful_time` your primary alert. Derive the
   threshold as 2 × interval + p99 duration + margin, and carry it as an annotation on the object.
4. Split severities: staleness pages, a single failed run tickets. Getting this wrong is how
   CronJob alerts end up muted.
5. Add the fleet-wide "many jobs stale at once" rule and, for tier-1 jobs, an `absent()` rule — a
   deleted CronJob has no metrics and therefore no alerts.
6. Prometheus pull cannot observe a 20-second pod. Emit outcome metrics from the run ledger, and
   remember the Pushgateway never forgets a decommissioned job.
7. Measure *effect*, not exit codes: rows written, items failed, and — best of all —
   `watermark_lag_seconds`, which expresses every failure mode as one business-meaningful number.
8. Export Kubernetes events. `FailedCreate` and `TooManyMissedTimes` expire in an hour, and they
   are the two events that explain the failures that last for days.
9. Run a heartbeat CronJob. It is the only thing that tells you your CronJob monitoring is alive.
