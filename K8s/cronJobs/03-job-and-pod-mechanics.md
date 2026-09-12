# Job and Pod Mechanics: Retries, Deadlines, and Cleanup

Everything in this doc lives *below* the CronJob, on the Job and the Pod. That is where retries,
timeouts, parallelism, and cleanup are configured, and it is the layer people most often leave
entirely at defaults — which is how you end up with a job that retries six times over eleven
minutes when you wanted it to fail fast, or one that hangs forever because nothing ever told it
to stop.

## Retries: `backoffLimit` and how the backoff actually times out

A Job's contract is "keep trying until the work succeeds or the retry budget is spent."
`.spec.backoffLimit` is that budget, and it counts **failures, not attempts**. The default is 6,
so a default Job makes up to **seven attempts**: the first one plus six retries.

Between attempts the Job controller waits, doubling each time, starting at 10 seconds and capped
at 6 minutes. For a default `backoffLimit: 6` the waits are:

| Retry | Wait before it | Cumulative wait |
|---|---|---|
| 1st | 10s | 10s |
| 2nd | 20s | 30s |
| 3rd | 40s | 1m 10s |
| 4th | 80s | 2m 30s |
| 5th | 160s | 5m 10s |
| 6th | 320s | 10m 30s |

So a job whose every attempt fails instantly still takes **10 minutes 30 seconds of pure waiting**
before the Job is marked failed — plus however long each attempt runs. For
`partner-sftp-export`, where each attempt spends 4 minutes uploading before the partner's server
rejects it, the total is 7 × 240s of work + 630s of waiting = **2310 seconds, about 38 minutes**,
to discover that the partner is down.

Two immediate consequences. First, **the default retry budget is far too long for anything
scheduled frequently** — a `*/5` job with `backoffLimit: 6` can still be retrying when its next
two firings arrive. Second, this is why `activeDeadlineSeconds` matters: the retry budget alone
does not bound wall-clock time in any way you would naturally predict.

### Sizing the retry budget by what the failures actually are

Retries only help for **transient** failures. Reason about your failure classes before choosing
a number:

- `partner-sftp-export` fails about 1 run in 12, essentially always because the partner's SFTP
  server is briefly unavailable. That is the ideal retry case: the next attempt usually works.
  `backoffLimit: 3` (four attempts spanning about 17 minutes of real time) converts a 1-in-12
  failure rate into roughly 1 in 20,000, assuming independence — which is optimistic, but the
  direction is right and it is the difference between a weekly page and a yearly one.
- `invoice-rollup` fails either because the database is briefly unavailable (retry helps) or
  because a malformed order row makes the aggregation throw (retry cannot possibly help, and
  will fail identically three more times). A large budget buys nothing against the second class.
- `cert-expiry-audit` is a read-only report. One retry is plenty.

The heuristic: **`backoffLimit` should be 2 or 3 for work with genuine transient failure modes,
and 0 or 1 for work whose failures are deterministic.** Six is a bad default for almost
everything; it is generous for the transient case and pure delay for the deterministic case. Use
`podFailurePolicy` (below) when you have both classes in one job and can tell them apart by exit
code.

### `restartPolicy`: `Never` or `OnFailure`, and why `Never` is better

A Job's pod template must set `restartPolicy` to `Never` or `OnFailure`. `Always` is rejected,
because a pod that always restarts can never complete. The choice between the two changes what
"retry" physically means:

- **`OnFailure`** — the kubelet restarts the failed *container in place*, in the same pod. The
  pod's restart count goes up, and those restarts count against `backoffLimit`.
- **`Never`** — the kubelet does nothing. The pod is marked failed and the **Job controller
  creates a brand-new pod** for the next attempt.

`Never` costs slightly more (a new pod object and a new image start per attempt) and is worth it,
for one decisive reason: **each attempt leaves behind its own pod object, so you can read each
attempt's logs separately.**

```bash
# With restartPolicy: Never — one pod per attempt, all inspectable.
kubectl -n integrations get pods -l batch.kubernetes.io/job-name=partner-sftp-export-29818940
# NAME                                  STATUS    RESTARTS   AGE
# partner-sftp-export-29818940-4qj7x    Error     0          18m
# partner-sftp-export-29818940-9fzkd    Error     0          14m
# partner-sftp-export-29818940-tm2vn    Completed 0          9m
kubectl -n integrations logs partner-sftp-export-29818940-4qj7x   # attempt 1, still there
```

With `OnFailure` there is one pod, and earlier attempts are reachable only through `--previous`,
which gets you the immediately preceding attempt and nothing before it. When you are diagnosing "it
failed three times then worked", losing attempts 1 and 2 is exactly the wrong trade.

⚠️ There have also been version-dependent oddities in how `OnFailure` restarts are counted
against `backoffLimit`, which can result in more attempts than you configured. `Never` avoids the
question entirely. Use `Never` for scheduled work unless you have a specific reason not to.

⚠️ Note the label in that command: `batch.kubernetes.io/job-name`. The older `job-name` label
still exists on many clusters for compatibility, but the prefixed form is the current one. If a
selector mysteriously matches nothing, check which form your cluster version sets.

## `activeDeadlineSeconds`: the only real timeout

`.spec.activeDeadlineSeconds` on the Job is a **wall-clock budget measured from the Job's start**.
When it expires, the Job is terminated: its pods are deleted, no further retries happen, and the
Job gets a `Failed` condition with reason `DeadlineExceeded`.

Three properties make it the most important field in this doc:

1. **It covers everything** — every attempt, every backoff wait, every second the pod spent
   Pending waiting for a node. It is the total.
2. **It takes precedence over `backoffLimit`.** If the deadline expires mid-retry, the retry
   budget is irrelevant.
3. **It is the only thing that bounds a hang.** A process blocked on a socket read with no
   timeout, or waiting on a database lock, will sit there indefinitely. No probe, no
   `backoffLimit`, and no CronJob setting will stop it. Only this field will.

Set it on **every** Job created by a CronJob. If you only take one action after reading this
collection, make it this one — and doc 02's silent-stall scenario (`Forbid` plus a hung job
equals a permanently stopped CronJob) is why.

### ⚠️ Do not confuse it with `startingDeadlineSeconds`

These two fields sound alike, live on different objects, and mean unrelated things. This confusion
is common enough to be worth a table:

| | `startingDeadlineSeconds` | `activeDeadlineSeconds` |
|---|---|---|
| Lives on | CronJob `.spec` | Job `.spec` (i.e. `jobTemplate.spec`) |
| Measured from | the *scheduled* time | the time the Job actually started |
| Answers | "how late is too late to bother starting?" | "how long may this run before we give up?" |
| On expiry | the firing is skipped; nothing is created | the running Job is killed and marked `DeadlineExceeded` |
| Typical value | 200s–2700s | one to several times the p99 run duration |

### Sizing the deadline and the retry budget together

They interact, and you have to solve them jointly. Work through `invoice-rollup`:

- Measured single-attempt duration: 7 minutes median, **34 minutes at p99** (2040 seconds).
- Schedule interval: 60 minutes. `concurrencyPolicy: Forbid`, so any Job still active at the
  next firing causes a skipped hour.

Start from the constraint that matters most: **the whole Job, including retries, should finish
inside one interval**, or `Forbid` will eat the next firing. That caps the deadline at under
3600s; leave margin and call it **3300s (55 minutes)**.

Now check what retry budget fits. One p99 attempt is 2040s. A 10-second backoff plus a second
p99 attempt would need 2040 + 10 + 2040 = 4090s, which does not fit in 3300s. So the honest
configuration is:

```yaml
jobTemplate:
  spec:
    backoffLimit: 1              # allow one retry — it fits on a normal day, not a p99 day
    activeDeadlineSeconds: 3300  # 55 min: finish inside the hour so Forbid never skips
```

with the explicit, written-down acceptance that **on a p99-slow day the retry will not get to
run**, because the deadline will cut it off. That is the correct trade for this job: a missed
hour is recoverable, since the next run's watermark logic (doc 05) picks up the orders it
skipped. What is *not* recoverable is a Job that runs 90 minutes and silently cancels the
following four firings.

Compare `partner-sftp-export`, where the calculation comes out differently because the interval
is a day, not an hour:

- Attempt duration ~4 minutes (240s), `backoffLimit: 3` → 4 attempts.
- Backoff waits for 3 retries: 10 + 20 + 40 = 70s.
- Worst case: 4 × 240 + 70 = **1030 seconds**.
- Add margin for a slow partner and pod startup: `activeDeadlineSeconds: 1500`.

⚠️ The failure mode to check for is **a deadline smaller than the retry budget needs**, because
it silently truncates your retries. If you had set 600s here, the Job would die during attempt 3
and you would never understand why `backoffLimit: 3` produced two and a half attempts. When you
set a deadline, do the arithmetic and put it in a comment next to the value.

### Termination is not instant

When the deadline expires, or `Replace` deletes a Job, the pod gets SIGTERM and then SIGKILL
after `terminationGracePeriodSeconds` (default 30). Two rules follow:

- **Handle SIGTERM.** Stop accepting new work, finish or roll back the current unit, release
  locks, write "aborted" to your run ledger, exit. A process that ignores SIGTERM contributes
  nothing but latency and leaves cleanup to the next run.
- **Size the grace period to the real cleanup time.** A job holding a 10-second database
  transaction needs more than a couple of seconds; a job that writes a 200 MB file needs time to
  either finish or delete the partial. But remember the grace period extends the overlap window
  under `Replace`, so do not make it enormous without reason.

## `podFailurePolicy`: not all failures deserve a retry

By default every pod failure looks the same to the Job controller: it decrements the budget.
That conflates two very different things:

- **Your code failed** — bad input, a bug, a rejected request. Retrying may or may not help.
- **The infrastructure took your pod away** — node preemption, a spot instance reclaimed, an
  eviction under memory pressure, a node drain during a cluster upgrade. Your code never got a
  fair chance.

The second class burning retry budget is a real problem for long jobs on spot capacity.
`db-vacuum` runs two hours; if its node is reclaimed 90 minutes in, that is one failure charged
against a budget sized for genuine errors. Three reclaims and the Job fails without your code
ever having misbehaved.

`.spec.podFailurePolicy` lets you classify. It requires `restartPolicy: Never`. It reached GA in
1.31 and was beta from 1.26, so on older clusters check the feature gate before relying on it.

```yaml
jobTemplate:
  spec:
    backoffLimit: 3
    activeDeadlineSeconds: 9000
    template:
      spec:
        restartPolicy: Never      # required by podFailurePolicy
        # ...
    podFailurePolicy:
      rules:
        # 1. Infrastructure disruption: do not charge it to the retry budget at all.
        - action: Ignore
          onPodConditions:
            - type: DisruptionTarget

        # 2. Configuration error: retrying is pointless, fail immediately and page.
        #    Our images exit 78 for "bad config" and 79 for "missing credential".
        - action: FailJob
          onExitCodes:
            containerName: vacuum
            operator: In
            values: [78, 79]
```

`action: Ignore` means the failure does not increment the counter — the Job creates a replacement
pod and carries on with its full budget intact. `action: FailJob` short-circuits: the Job is
marked failed at once, which turns a 38-minute pointless retry sequence into an immediate,
actionable alert.

For this to work your job has to **use distinct exit codes**, which is a small code change with a
large payoff. A convention that works well:

| Exit code | Meaning | Desired behaviour |
|---|---|---|
| 0 | success | — |
| 1 | unexpected/unclassified error | retry (maybe it is transient) |
| 75 | transient dependency failure (`EX_TEMPFAIL` by convention) | retry |
| 78 | configuration error (`EX_CONFIG` by convention) | fail immediately |
| 79 | missing or invalid credential | fail immediately |

`Ignore` for `DisruptionTarget` is worth adding to essentially every long-running scheduled job,
regardless of whether you use exit codes. It is pure upside.

## Parallelism: `completions`, `parallelism`, and indexed Jobs

Most CronJobs are "non-parallel": `completions: 1`, `parallelism: 1`, one pod, done. Two other
shapes exist and both are useful for scheduled work.

**Fixed completion count.** Set `completions: 20` and `parallelism: 5`: the Job runs until 20
pods have succeeded, keeping 5 in flight. Useful when work items come from a queue that each pod
pulls from — but note every pod gets an identical spec, so the pods must coordinate through
something external to avoid doing the same work.

**Indexed completion.** Set `completionMode: Indexed` and each pod gets a distinct index in the
`JOB_COMPLETION_INDEX` environment variable (and in the
`batch.kubernetes.io/job-completion-index` annotation). This is static sharding, and it is the
clean way to make a long scheduled job faster without inventing a work queue.

`catalog-reindex` takes 1h50m single-threaded. Sharded eight ways by index:

```yaml
jobTemplate:
  spec:
    completionMode: Indexed
    completions: 8             # 8 shards, indices 0..7
    parallelism: 8             # all at once
    activeDeadlineSeconds: 3600
    backoffLimit: 8            # roughly one retry per shard on average
    template:
      spec:
        restartPolicy: Never
        containers:
          - name: reindex
            image: registry.riverbend.internal/search/reindex@sha256:1a4f...
            # The container reads JOB_COMPLETION_INDEX and processes
            # products WHERE id % 8 = $JOB_COMPLETION_INDEX
            args: ["--shard-count=8"]
```

Runtime drops from about 110 minutes to roughly 15 (eight-way split plus some non-parallel
overhead), which also moves the overlap risk from "real" to "irrelevant". Two things to hold in
mind: the Job is not complete until **every** index succeeds, so one bad shard fails the whole
run; and you now need 8× the resources simultaneously, which interacts with autoscaling and
quota (doc 06).

For the "one bad shard" problem, newer clusters offer `backoffLimitPerIndex` and
`maxFailedIndexes`, which give each index its own retry budget and let you tolerate a few failed
shards rather than losing the run:

```yaml
    backoffLimitPerIndex: 2   # each shard gets its own 2 retries
    maxFailedIndexes: 1       # tolerate one permanently failed shard out of 8
```

These graduated through beta in the 1.28–1.33 range; check availability on your cluster before
depending on them, and be careful about `maxFailedIndexes` semantics — "7 of 8 shards reindexed"
is acceptable for a search index and completely unacceptable for a settlement run.

## Cleanup: history limits and TTL

Every firing leaves objects behind. Left unmanaged, a `*/5` CronJob produces 288 Jobs and at
least 288 pods per day, all of them in etcd. Two independent mechanisms clean up, and their
interaction catches people out.

**History limits (on the CronJob).** The CronJob controller keeps the most recent N finished Jobs
and deletes older ones:

```yaml
spec:
  successfulJobsHistoryLimit: 3    # default 3
  failedJobsHistoryLimit: 3        # default 1  <-- raise this
```

⚠️ **The default `failedJobsHistoryLimit` of 1 is the wrong value for debugging.** Failures are
precisely what you need history for, and one is not enough: if a job fails at 02:00 and fails
again at 03:00, the 02:00 evidence — including its pods and their logs — is gone before anyone
looks. Raise it to at least 3.

Setting either limit to `0` deletes finished Jobs immediately. That keeps etcd tidy and destroys
your ability to debug anything after the fact. Only do it if logs are shipped off-cluster *and*
you have run-outcome metrics, and even then, keeping one is nearly free.

**TTL after finish (on the Job).** The separate TTL controller deletes a finished Job once
`.spec.ttlSecondsAfterFinished` has elapsed since it completed:

```yaml
jobTemplate:
  spec:
    ttlSecondsAfterFinished: 86400   # delete 24h after finishing
```

The two mechanisms are independent and **whichever triggers first wins.** With
`ttlSecondsAfterFinished: 3600` and `successfulJobsHistoryLimit: 10`, you will rarely see 10 —
you will see however many finished in the last hour. That is usually fine, but it means the
history limit is not a guarantee of what you will find.

⚠️ **Deleting a Job deletes its pods, and deleting the pods destroys the logs.** `kubectl logs`
reads from the node's log files via the kubelet; there is no archive. So the honest ordering is:
ship logs off-cluster first, then set aggressive TTLs. If you delete Jobs after an hour and do
not ship logs, then any failure investigated more than an hour later starts with "we have no
idea what it printed."

A sensible default pair for the fleet:

| Job class | `ttlSecondsAfterFinished` | `failedJobsHistoryLimit` |
|---|---|---|
| High-frequency, low-stakes (`session-reaper`) | 3600 (1h) | 3 |
| Hourly business logic (`invoice-rollup`) | 86400 (24h) | 5 |
| Daily critical (`payout-settlement`) | 604800 (7d) | 10 |

The principle: **retain for as long as it takes someone to notice and investigate.** For a job
whose failure is noticed on Monday morning, an hour of retention is worthless.

## Two pod-level details that bite scheduled work

**Probes on Job pods.** Readiness probes are meaningless for a Job — nothing routes traffic to
it — and a readiness probe on a Job pod does nothing useful. A **liveness** probe, however, can
act as a hang detector: if the probe fails, the kubelet kills the container, and with
`restartPolicy: Never` that fails the pod, which the Job counts as an attempt. Use this only when
you have a genuinely cheap "am I making progress" check, and prefer `activeDeadlineSeconds` for
the general case: a deadline is simpler, needs no endpoint, and cannot itself become the bug.

**The job-tracking finalizer.** Pods created by a Job carry a finalizer
(`batch.kubernetes.io/job-tracking`) so the Job controller can count terminal pods reliably. It
is normally invisible, but if the Job controller is unhealthy or the Job object is force-deleted,
pods can sit in `Terminating` indefinitely waiting for a finalizer nobody will remove. If you see
job pods stuck `Terminating` with no obvious cause, check
`.metadata.finalizers` — and see doc 04 for the safe way out.

## What to take away

1. `backoffLimit` counts failures, not attempts, and defaults to 6 — up to seven attempts with
   10m30s of pure backoff waiting. That default is too generous for most scheduled work; use 0–3
   based on whether your failures are actually transient.
2. Prefer `restartPolicy: Never`. One pod per attempt means one readable log per attempt, which
   is what you need when debugging "failed twice then worked".
3. `activeDeadlineSeconds` is the only field that bounds a hang. Set it on every CronJob's Job
   template. Nothing else will save you.
4. Size the deadline and the retry budget **together**, and write the arithmetic in a comment. A
   deadline shorter than the retry sequence silently truncates your retries.
5. `startingDeadlineSeconds` (how late may we start) and `activeDeadlineSeconds` (how long may we
   run) are different fields on different objects. Do not mix them up.
6. Use `podFailurePolicy` to `Ignore` `DisruptionTarget` on any long job, so preemption stops
   consuming your retry budget — and to `FailJob` on config-error exit codes so unretryable
   failures alert immediately.
7. Indexed Jobs are the clean way to shard a slow scheduled job; eight-way sharding took
   `catalog-reindex` from 110 minutes to about 15.
8. Raise `failedJobsHistoryLimit` above its default of 1, and ship logs off-cluster before you
   set aggressive TTLs — deleting a Job deletes its pods, and that deletes the only copy of the
   logs.
