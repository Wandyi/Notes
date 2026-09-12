# Worked Examples

Four complete CronJobs and two jobs that should not be CronJobs at all. Each example states the
requirement, derives the configuration from it, and then shows what would go wrong with the
obvious alternative — because the value is in the reasoning, not in the YAML.

Doc 10 already carries `payout-settlement` as the annotated golden template, so it is not repeated
here.

---

## 1. `invoice-rollup` — hourly, window-based, must not double-count

**The requirement.** Every hour, aggregate the previous hour's orders into invoice lines. Finance's
daily close reads these. A double-count double-bills a customer; a missing hour makes the close
unbalanced.

**The measurements** that drive every number below: 7 minutes median runtime, 34 minutes at p99,
up to 240,000 orders in a peak hour, peak memory 1.6 GB.

### The derivations

**Schedule: `10 * * * *`, not `0 * * * *`.** Two reasons. Orders written near the top of the hour
are still committing, so starting at :10 lets them land — and it keeps the job out of the fleet's
:00 herd (doc 01).

**`concurrencyPolicy: Forbid`.** Two concurrent runs reading the same order window append the same
invoice lines. `Allow` is the default and would have been a billing incident (doc 02).

**`activeDeadlineSeconds: 3300`.** Derived in doc 03: the Job must finish inside its own hourly
interval or `Forbid` will skip the next firing, so the ceiling is 3600 and 3300 leaves margin.

**`backoffLimit: 1`.** One p99 attempt is 2040s; a second would need 4090s, which does not fit in
3300s. So one retry — which helps on a normal day and is truncated on a bad one. That is
acceptable *only because* the watermark makes a missed hour self-healing.

**Watermark, not clock.** The job does not compute "the previous hour" from `now()`. It reads its
high-water mark and processes forward to `now() - 5 minutes` (doc 05). This is what turns a skipped
firing into a longer next run instead of a permanent data gap.

**Memory 2560Mi, no CPU limit.** 1.6 GB p99 × 1.5 headroom, rounded (doc 06). No CPU limit so the
job uses idle cores and stays at 7 minutes rather than being throttled to 24.

### The manifest

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: invoice-rollup
  namespace: billing
  labels:
    app.kubernetes.io/name: invoice-rollup
    riverbend.io/team: billing-platform
    riverbend.io/tier: "2"
  annotations:
    riverbend.io/runbook: "https://wiki.riverbend.io/runbooks/invoice-rollup"
    riverbend.io/max-success-age-seconds: "10800"    # 2 intervals + p99 + margin (doc 08)
spec:
  schedule: "10 * * * *"
  timeZone: "Etc/UTC"
  startingDeadlineSeconds: 1800     # an hour's rollup is still useful 30 min late
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5
  jobTemplate:
    metadata:
      labels: { cronjob: invoice-rollup }
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 3300
      ttlSecondsAfterFinished: 86400
      podFailurePolicy:
        rules:
          - action: Ignore
            onPodConditions: [{ type: DisruptionTarget }]
          - action: FailJob
            onExitCodes: { containerName: rollup, operator: In, values: [78, 79] }
      template:
        metadata:
          labels: { app: invoice-rollup, cronjob: invoice-rollup }
        spec:
          restartPolicy: Never
          serviceAccountName: invoice-rollup
          automountServiceAccountToken: false
          priorityClassName: batch-low
          terminationGracePeriodSeconds: 45     # time to finish the open batch transaction
          securityContext:
            runAsNonRoot: true
            runAsUser: 10001
            seccompProfile: { type: RuntimeDefault }
          containers:
            - name: rollup
              image: registry.riverbend.internal/billing/invoice-rollup@sha256:5c1b9e4a7d2f...
              args:
                - --batch-size=500          # bounds memory: doc 06
                - --late-arrival-buffer=5m  # the watermark ceiling: doc 05
              env:
                - name: RUN_KEY
                  valueFrom:
                    fieldRef: { fieldPath: "metadata.labels['batch.kubernetes.io/job-name']" }
                - name: POD_NAME
                  valueFrom:
                    fieldRef: { fieldPath: metadata.name }
                - name: DATABASE_URL
                  valueFrom:
                    secretKeyRef: { name: billing-db, key: url }
              resources:
                requests: { cpu: "1", memory: "2560Mi" }
                limits:   { memory: "2560Mi" }
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities: { drop: ["ALL"] }
```

### The code contract

The manifest is only half of it. The job must:

```python
run_key = os.environ["RUN_KEY"]              # stable across attempts — the idempotency key

# 1. Claim the run. Zero rows means someone else owns it: exit cleanly.
if not claim_run("invoice-rollup", run_key, os.environ["POD_NAME"]):
    log.info("run already claimed, exiting", run_key=run_key); sys.exit(0)

# 2. Window from the watermark, never from the clock.
start = last_successful_watermark("invoice-rollup") or DEFAULT_EPOCH
end   = now() - timedelta(minutes=5)

# 3. Process in bounded batches; every write idempotent on (order_id, run_key).
rows = 0
for batch in fetch_orders(start, end, size=500):
    with transaction():
        rows += upsert_invoice_lines(batch, run_key)
        advance_watermark("invoice-rollup", batch[-1].created_at)   # same transaction

# 4. Record the outcome, including the number that proves it did something.
complete_run("invoice-rollup", run_key, rows_written=rows)
```

### What breaks without each piece

| Remove this | What happens |
|---|---|
| `Forbid` | A 70-minute run overlaps the next firing and invoice lines double (F-15) |
| `activeDeadlineSeconds` | A database lock hangs the job; every subsequent hour is silently skipped (F-08) |
| the watermark | Any skipped firing is a permanently missing hour of billing data (F-16) |
| `run_key` on the upsert | A retry after a mid-run OOM duplicates whatever the first attempt wrote |
| `rows_written` | A run that processes nothing looks identical to a healthy run (F-13) |
| `startingDeadlineSeconds` | One long suspension puts the job behind the missed-schedule wall forever (F-01) |

---

## 2. `partner-sftp-export` — an unreliable third party

**The requirement.** Every weekday at 06:00, push a CSV of the previous day's shipments to a
partner's SFTP server. The partner's ingestion window closes at 07:00. The partner rejects
duplicate files for the same day. Their server is unavailable for roughly one run in twelve.

### The derivations

**`backoffLimit: 3`.** The failures are genuinely transient, which is the one case where a large
retry budget earns its keep. Four attempts against a 1-in-12 per-attempt failure rate reduces the
expected failure rate by orders of magnitude (doc 03).

**`activeDeadlineSeconds: 1500`.** 4 attempts × 240s + backoff waits (10 + 20 + 40 = 70s) = 1030s,
plus margin for a slow partner and a cold start.

**`startingDeadlineSeconds: 2700`.** Derived from the *partner's* window, not from ours: after
06:45 the upload will be rejected at the far end, so starting is wasted work. This is the clearest
example in the collection of a config value that is a business fact (doc 01).

**Exit codes.** A rejected credential must not burn 38 minutes of retries, so the job exits 79 for
an auth failure and 75 for a connection failure, and `podFailurePolicy` treats them differently.

**Idempotency at the partner.** The filename carries the business date, and the job checks for an
existing file before uploading. The partner's duplicate rejection is a backstop, not the design.

### The manifest and its supporting objects

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: partner-sftp-export
  namespace: integrations
  labels: { riverbend.io/team: integrations, riverbend.io/tier: "2" }
  annotations:
    riverbend.io/runbook: "https://wiki.riverbend.io/runbooks/partner-sftp-export"
    riverbend.io/max-success-age-seconds: "270000"   # 75h: covers a weekend gap (below)
spec:
  schedule: "0 6 * * 1-5"
  timeZone: "Etc/UTC"
  startingDeadlineSeconds: 2700
  concurrencyPolicy: Forbid
  failedJobsHistoryLimit: 5
  jobTemplate:
    metadata:
      labels: { cronjob: partner-sftp-export }
    spec:
      backoffLimit: 3
      activeDeadlineSeconds: 1500
      ttlSecondsAfterFinished: 172800
      podFailurePolicy:
        rules:
          - action: Ignore
            onPodConditions: [{ type: DisruptionTarget }]
          - action: FailJob            # auth failure: retrying cannot help, page now
            onExitCodes: { containerName: export, operator: In, values: [78, 79] }
      template:
        metadata:
          labels: { app: partner-sftp-export, cronjob: partner-sftp-export }
        spec:
          restartPolicy: Never
          serviceAccountName: partner-sftp-export
          automountServiceAccountToken: false
          priorityClassName: batch-low
          containers:
            - name: export
              image: registry.riverbend.internal/integrations/sftp-export@sha256:2d8f1c...
              args: ["--partner=northwind", "--for-date=yesterday", "--skip-if-present"]
              volumeMounts:
                - { name: partner-key, mountPath: /etc/partner, readOnly: true }
              resources:
                requests: { cpu: "200m", memory: "256Mi" }
                limits:   { memory: "512Mi" }
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities: { drop: ["ALL"] }
          volumes:
            - name: partner-key
              secret:
                secretName: partner-sftp-key    # a mounted volume, never an env var (doc 07)
                defaultMode: 0400
```

⚠️ Note the staleness threshold: **75 hours**, not 3. A weekday-only schedule has a legitimate
64-hour gap from Friday 06:00 to Monday 06:00, so a tighter threshold would page every Sunday.
This is a general rule for weekday and weekly schedules — derive the staleness threshold from the
**longest legitimate gap**, then add a run's worth of margin.

The NetworkPolicy from doc 07 belongs with this job: DNS, the database, and the partner's IP on
port 22 — nothing else. And because the partner will eventually change that IP, the follow-up work
is to route it through an egress gateway with a stable allow-list.

---

## 3. `catalog-reindex` — long, interruptible, only the latest matters

**The requirement.** Rebuild the product search index nightly. It took 1h50m single-threaded and
over 3 hours on catalogue-import days. A partial index is worthless. The newest index is always
strictly better than an older one.

### The derivations

**`concurrencyPolicy: Replace`.** The one job in the fleet where `Replace` is right (doc 02): a
rebuild started 90 minutes ago is less useful than one starting now, and an interrupted rebuild
leaves nothing of value. This is safe *because* of the staging-and-swap design below.

**Indexed sharding, 8 ways.** 110 minutes single-threaded is 41% of a daily interval on a good day
and over 100% of the overlap budget on an import day. Eight shards bring it to about 15 minutes
(doc 03), which removes the overlap question entirely rather than managing it.

**Staging index plus alias swap.** Each shard writes into `products_v<n>`; when all eight succeed,
a final step repoints the `products` alias. Partial failure is structurally invisible to readers
(doc 05, pattern 6) — which is also what makes `Replace` safe.

**Spot capacity.** The work is interruptible and re-runnable, so it belongs on cheap capacity
(doc 06) — with `podFailurePolicy` ignoring `DisruptionTarget` so reclamations do not consume the
retry budget.

**A per-shard offset in the schedule.** `37 3 * * *` rather than `0 3 * * *`, computed from the
job name (doc 01).

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: catalog-reindex
  namespace: search
  labels: { riverbend.io/team: search, riverbend.io/tier: "2" }
  annotations:
    riverbend.io/max-success-age-seconds: "172800"   # 48h: one missed night is tolerable
spec:
  schedule: "37 3 * * *"
  timeZone: "Etc/UTC"
  startingDeadlineSeconds: 3600
  concurrencyPolicy: Replace
  failedJobsHistoryLimit: 3
  jobTemplate:
    metadata:
      labels: { cronjob: catalog-reindex }
    spec:
      completionMode: Indexed
      completions: 8
      parallelism: 8
      backoffLimitPerIndex: 2      # each shard retries independently (verify availability
      maxFailedIndexes: 0          # on your cluster version — doc 03)
      activeDeadlineSeconds: 5400
      ttlSecondsAfterFinished: 86400
      podFailurePolicy:
        rules:
          - action: Ignore
            onPodConditions: [{ type: DisruptionTarget }]
      template:
        metadata:
          labels: { app: catalog-reindex, cronjob: catalog-reindex }
        spec:
          restartPolicy: Never
          serviceAccountName: catalog-reindex
          automountServiceAccountToken: false
          priorityClassName: batch-low
          terminationGracePeriodSeconds: 30
          nodeSelector: { workload: batch-spot }
          tolerations:
            - { key: workload, operator: Equal, value: batch-spot, effect: NoSchedule }
          containers:
            - name: reindex
              image: registry.riverbend.internal/search/reindex@sha256:1a4f7b2e...
              args: ["--shard-count=8"]       # the shard itself comes from JOB_COMPLETION_INDEX
              resources:
                requests: { cpu: "2", memory: "4Gi" }
                limits:   { memory: "4Gi" }
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities: { drop: ["ALL"] }
```

⚠️ `maxFailedIndexes: 0` is deliberate: a search index missing one eighth of the catalogue is
worse than yesterday's complete index, because it looks fine and quietly returns no results for a
range of products. Contrast doc 03's note that tolerating a failed shard can be acceptable — the
right value depends entirely on whether partial output is usable, and here it is not.

⚠️ The alias swap must be a separate step that runs only after all eight shards succeed. An
indexed Job gives you no "finally" step, so either the swap is a second CronJob gated on the
first's success (fragile, clock-coupled) or — better, and the honest recommendation — this job is
the one in the fleet that has outgrown CronJobs and belongs in a workflow with a DAG (doc 11).

---

## 4. `db-vacuum` — two hours, heavy, and in everyone's way

**The requirement.** Weekly Postgres maintenance. Two hours of heavy I/O. Must not run during
business hours. Must not be interrupted halfway, because a partial vacuum wastes the I/O without
completing the work.

### The derivations

**`0 4 * * 0` — Sunday 04:00 UTC**, inside the quiet window where no other scheduled job runs
(doc 04's F-20 discussion). The schedule is chosen against the *fleet's* timetable, not in
isolation.

**`activeDeadlineSeconds: 14400`** (4 hours) — generous, at roughly 2× the p99 runtime, because
being killed at 1h55m of a 2h job is pure waste. The deadline exists to catch a genuine hang, not
to enforce a performance target.

**`safe-to-evict: "false"`** so the cluster autoscaler cannot consolidate the node mid-vacuum
(doc 06) — paired, as always, with the deadline so a hang cannot pin the node forever.

**On-demand capacity, not spot.** The work is *not* cheaply resumable, which inverts
`catalog-reindex`'s reasoning: a reclaimed spot node wastes 90 minutes of I/O.

**Checkpointing per table.** The job vacuums table by table and records which tables are done, so a
restart resumes rather than starting over (doc 05, pattern 5).

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: db-vacuum
  namespace: platform
  labels: { riverbend.io/team: platform, riverbend.io/tier: "2" }
  annotations:
    riverbend.io/max-success-age-seconds: "950400"   # 11 days: a weekly job plus one missed run
spec:
  schedule: "0 4 * * 0"
  timeZone: "Etc/UTC"
  startingDeadlineSeconds: 7200     # up to 2h late is fine; after 06:00 wait for next Sunday
  concurrencyPolicy: Forbid
  failedJobsHistoryLimit: 4         # 4 weeks of failure history, given it runs weekly
  jobTemplate:
    metadata:
      labels: { cronjob: db-vacuum }
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 14400
      ttlSecondsAfterFinished: 1209600   # 14 days: must outlive the weekly cadence
      podFailurePolicy:
        rules:
          - action: Ignore
            onPodConditions: [{ type: DisruptionTarget }]
          - action: FailJob
            onExitCodes: { containerName: vacuum, operator: In, values: [78, 79] }
      template:
        metadata:
          labels: { app: db-vacuum, cronjob: db-vacuum }
          annotations:
            cluster-autoscaler.kubernetes.io/safe-to-evict: "false"
        spec:
          restartPolicy: Never
          serviceAccountName: db-vacuum
          automountServiceAccountToken: false
          priorityClassName: batch-low
          terminationGracePeriodSeconds: 120    # let the current table finish and checkpoint
          nodeSelector: { workload: batch }
          tolerations:
            - { key: workload, operator: Equal, value: batch, effect: NoSchedule }
          containers:
            - name: vacuum
              image: registry.riverbend.internal/platform/db-vacuum@sha256:6e0a3d...
              args: ["--parallel=2", "--checkpoint-per-table", "--skip-if-bloat-below=15%"]
              resources:
                requests: { cpu: "2", memory: "2Gi" }
                limits:   { memory: "2Gi" }
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities: { drop: ["ALL"] }
```

⚠️ Two operational notes that matter more than any field here. First, this job's failure is
**invisible for a week** — the staleness alert is the only thing that will tell you, and its
threshold (11 days) means the news arrives late. For weekly jobs, prefer doc 08's
"expected-but-absent" alert built on `kube_cronjob_next_schedule_time`, which fires within an hour.
Second, this is the job most likely to be killed by a cluster upgrade (F-20); its schedule belongs
in whatever calendar drives node-pool rotations.

---

## 5. A job that should not be a CronJob: the bucket poller

**What exists.** A CronJob running `*/1 * * * *` that lists an S3 prefix and processes any new
files.

**Why it is wrong.** It fires 1,440 times a day. On a typical day fewer than 30 of those firings
find anything, so **over 97% of the runs are pure overhead** — 1,410 Job objects, 1,410 pods, and
roughly 20,000 API writes a day to discover that nothing has changed. And it still adds up to 60
seconds of latency to work that could have started immediately.

**What to do instead.** Have the bucket notify a queue on object creation, and consume the queue.
If you want to stay Job-shaped, KEDA's `ScaledJob` creates Jobs from queue depth:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledJob
metadata:
  name: partner-file-ingest
  namespace: integrations
spec:
  jobTargetRef:
    template:
      spec:
        restartPolicy: Never
        containers:
          - name: ingest
            image: registry.riverbend.internal/integrations/file-ingest@sha256:8c2b...
  pollingInterval: 10
  maxReplicaCount: 10
  triggers:
    - type: aws-sqs-queue
      metadata:
        queueURL: https://sqs.us-east-1.amazonaws.com/…/partner-files
        queueLength: "5"
```

Latency drops from up to 60 seconds to a few seconds, the 1,410 empty runs disappear, and the work
scales with arrivals instead of with the clock.

⚠️ Keep a **low-frequency reconciliation CronJob** — hourly, not every minute — that scans for
files the notifications missed. Event delivery is at-least-once but not guaranteed-once-forever,
and the sweep is your safety net. This is the correct use of a CronJob here: not the primary
trigger, but the backstop that makes the event path trustworthy.

---

## 6. A job that should not be 240 CronJobs: per-tenant reports

**What exists.** `tenant-report-acme`, `tenant-report-globex`, … 240 CronJobs generated by a Helm
loop, each `0 5 * * *`.

**Why it is wrong.** Four separate problems, all of which get worse as you sell more:

1. **240 simultaneous firings at 05:00** — the herd, concentrated (F-18).
2. **240 × 24 = 5,760 firings a day** if any of them are hourly, and 240 sets of Prometheus series
   that rotate every run (doc 11).
3. **Onboarding a tenant requires a deploy.** Schedules are data pretending to be manifests.
4. **240 alerts.** Nobody can tell whether three failing tenants is normal.

**What to do instead.** One CronJob that iterates, with per-tenant isolation and an explicit
run-level verdict:

```yaml
spec:
  schedule: "0 5 * * *"
  timeZone: "Etc/UTC"
  startingDeadlineSeconds: 3600
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      # 240 tenants x ~25s each, run 8-way parallel: about 13 minutes. Deadline at 3x that.
      activeDeadlineSeconds: 2400
      backoffLimit: 1
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: reports
              image: registry.riverbend.internal/reporting/tenant-reports@sha256:4f7a...
              args:
                - --concurrency=8
                - --fail-run-above-error-rate=0.05   # explicit verdict, not exit 0 (doc 11)
```

Then per-tenant outcomes become metric labels rather than CronJob objects:

```promql
# One alert covers all tenants, with the failing ones named in the alert body.
riverbend_tenant_report_failed{tenant=~".+"} > 0
```

If tenants genuinely need *different* schedules — because customers pick their own delivery time —
then the schedules are user data and belong in a table, with a frequent dispatcher job that reads
what is due (doc 11). Two hundred and forty CronJobs is the wrong answer to both versions of the
requirement.

---

## What to take away

1. Every number in a good CronJob manifest is derived from a measurement or a business fact. If you
   cannot say where a value came from, it is a guess that will not survive the job's growth.
2. The same field takes opposite values for good reasons: `catalog-reindex` runs on spot with
   `Replace`, `db-vacuum` runs on-demand with `Forbid`, and the difference is entirely whether
   interrupted work is cheap to redo.
3. Derive staleness thresholds from the **longest legitimate gap** — 75 hours for a weekday job,
   11 days for a weekly one — and use the expected-but-absent alert for anything sparse, or the
   news arrives a week late.
4. `maxFailedIndexes` and similar "tolerate partial success" settings depend on whether partial
   output is usable. For a search index it is not, because it fails silently and looks fine.
5. Two shapes recur in every large fleet and are always worth fixing: the polling CronJob (97%
   empty runs, and it still adds latency) and the job-per-tenant fleet (schedules masquerading as
   manifests). Keep a low-frequency CronJob as the reconciliation backstop in the first case.
