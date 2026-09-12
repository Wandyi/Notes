# Operating CronJobs Day to Day

The previous docs were about designing scheduled work. This one is about living with it: the
commands you run, the procedures that keep an intervention from becoming an incident, how to test
a job whose whole point is that it runs unattended, and how CronJobs fit into a GitOps world that
assumes everything is declarative.

Pod-level debugging is not repeated here. If the pod is `Pending`, `CrashLoopBackOff`,
`ImagePullBackOff`, or `OOMKilled`, the mechanism and the commands are in
[`../debug/01-pod-lifecycle-and-startup.md`](../debug/01-pod-lifecycle-and-startup.md) and they are
no different for a Job's pod than for a Deployment's. What is different is everything *above* the
pod, and that is what follows.

## Orienting yourself in three commands

When someone says "the nightly job is broken", these three answer "broken how?" — which decides
everything else. They map onto the failure classes in doc 04.

```bash
# 1. Is the schedule working at all?
kubectl -n $NS get cronjob $CJ
#    LAST SCHEDULE stale or empty -> class A, nothing is being created (F-01, F-02, F-06)
#    SUSPEND True                 -> F-02, and you are done looking

# 2. What did the controller try to do, and what did it say about it?
kubectl -n $NS describe cronjob $CJ | sed -n '/Events/,$p'
#    FailedCreate        -> quota, webhook, PSA, missing ServiceAccount (F-06)
#    JobAlreadyActive    -> Forbid is skipping firings; something is still running (F-08)
#    TooManyMissedTimes  -> past the wall, it has stopped for good (F-01)

# 3. What happened on the recent runs?
kubectl -n $NS get jobs -l cronjob=$CJ \
  --sort-by=.metadata.creationTimestamp \
  -o custom-columns='NAME:.metadata.name,ACTIVE:.status.active,SUCCEEDED:.status.succeeded,FAILED:.status.failed,START:.status.startTime,END:.status.completionTime'
```

⚠️ Step 3 relies on the `cronjob:` label convention from doc 04 — Kubernetes does not add one, so
if the job predates that convention, filter on the owner reference instead:

```bash
kubectl -n $NS get jobs -o json | jq -r --arg cj "$CJ" '.items[]
  | select(.metadata.ownerReferences[]? | select(.kind=="CronJob" and .name==$cj))
  | [.metadata.name, (.status.startTime // "-"), (.status.completionTime // "running")] | @tsv'
```

Two more that earn their place in muscle memory:

```bash
# Every attempt of one run, with its outcome. This is where logs live.
kubectl -n $NS get pods -l batch.kubernetes.io/job-name=$JOB \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,REASON:.status.reason,NODE:.spec.nodeName'

# Logs from every attempt at once — note this reads pods, so it only works before TTL cleanup.
for p in $(kubectl -n $NS get pods -l batch.kubernetes.io/job-name=$JOB -o name); do
  echo "=== $p"; kubectl -n $NS logs $p --timestamps | tail -40
done
```

And the fleet-wide sweep, which is worth running on a schedule rather than only in anger:

```bash
# Every CronJob, sorted by how long since its last success. The top of this list is your backlog.
kubectl get cronjobs -A -o json | jq -r '.items[]
  | [ .metadata.namespace, .metadata.name,
      (.spec.schedule),
      (.spec.suspend | tostring),
      (.status.lastScheduleTime // "never"),
      (.status.lastSuccessfulTime // "never") ] | @tsv' \
  | column -t -s $'\t'
```

## Triggering a run by hand

The everyday need: a job failed, you fixed the cause, and you want it to run now rather than
waiting for 02:00.

```bash
kubectl -n billing create job invoice-rollup-manual-20260912 \
  --from=cronjob/invoice-rollup
```

This copies the `jobTemplate` exactly and creates a standalone Job. Four things about it that
matter:

⚠️ **The Job has no owner reference to the CronJob.** Consequences, all of which have surprised
someone: `concurrencyPolicy: Forbid` cannot see it, so it can run alongside a scheduled run
(F-15); history limits will never clean it up, so it sits in the namespace consuming
`count/jobs.batch` quota until you delete it; and it does not appear in `.status.active` or in any
per-CronJob metric join.

⚠️ **Name it so that a stranger can tell what it is.** `invoice-rollup-manual-20260912` beats
`test`, `fix`, or `job1`. Six weeks later someone is auditing stray Jobs and the name is the only
context available. Include the date and the word `manual`.

⚠️ **It runs the current template** — including the current image digest and the current secrets.
That is usually what you want, and it is the mechanism behind the canary procedure below.

⚠️ **`--from` cannot change arguments.** For a backfill you need different arguments, which means
rendering and patching the manifest rather than using `--from` alone; doc 05 has the command and
the trap about reusing run keys.

Clean up when you are done:

```bash
kubectl -n billing delete job invoice-rollup-manual-20260912
```

Better, set a TTL at creation so cleanup is not a thing you have to remember:

```bash
kubectl -n billing create job invoice-rollup-manual-20260912 \
  --from=cronjob/invoice-rollup --dry-run=client -o yaml \
  | yq '.spec.ttlSecondsAfterFinished = 86400' \
  | kubectl apply -f -
```

## Suspending and resuming

`suspend` is the right tool for "stop this now" and it is safe, because it affects only *future*
firings:

```bash
kubectl -n billing patch cronjob invoice-rollup --type=merge -p '{"spec":{"suspend":true}}'
```

It does **not** stop a run already in progress. If you need that too, delete the active Job
afterwards — and know that you are killing work mid-flight, which is only safe if the job is
idempotent or checkpointed (doc 05):

```bash
kubectl -n billing get cronjob invoice-rollup -o jsonpath='{.status.active[*].name}{"\n"}'
kubectl -n billing delete job invoice-rollup-29818940
```

Three habits turn suspension from a liability into a controlled operation:

**1. Record why, on the object.** F-02 is a suspension nobody remembered. The annotation makes the
next person's decision possible:

```bash
kubectl -n billing annotate cronjob invoice-rollup --overwrite \
  ops.riverbend.io/suspended-reason="INC-4471: duplicate lines from bad partner feed" \
  ops.riverbend.io/suspended-by="vaibhav" \
  ops.riverbend.io/suspended-at="2026-09-12T09:14:00Z"
```

**2. On resume, check the missed-schedule wall.** This is the step people skip. A frequent job
suspended for more than a few hours may be past the 100-missed limit from doc 01, in which case
unsuspending it does nothing at all:

```bash
kubectl -n billing patch cronjob invoice-rollup --type=merge -p '{"spec":{"suspend":false}}'
# Now confirm it actually fires within one interval:
kubectl -n billing get cronjob invoice-rollup -w
# If LAST SCHEDULE does not advance, apply the F-01 fix:
kubectl -n billing patch cronjob invoice-rollup --type=merge \
  -p '{"spec":{"startingDeadlineSeconds":1800}}'
```

**3. Decide what happens to the skipped windows.** For a watermark-driven job (doc 05), nothing —
the next run catches up by design. For a clock-driven job, the suspension created a data gap and
you now owe a backfill. Work out which of the two you have *before* resuming, because the answer
determines whether you are finished.

### A fleet-wide kill switch

Occasionally you need everything scheduled to stop — a database failover, a migration, a security
incident. Have the command ready rather than inventing it under pressure:

```bash
# Suspend every CronJob in a namespace, recording the reason.
for cj in $(kubectl -n billing get cronjob -o name); do
  kubectl -n billing patch $cj --type=merge -p '{"spec":{"suspend":true}}'
  kubectl -n billing annotate $cj --overwrite \
    ops.riverbend.io/suspended-reason="INC-4471 fleet freeze"
done

# Capture what was ALREADY suspended first, so resume does not un-suspend the wrong ones.
kubectl -n billing get cronjob -o json \
  | jq -r '.items[] | select(.spec.suspend==true) | .metadata.name' \
  > /tmp/already-suspended.txt
```

Run the capture **before** the freeze, not after. Resuming everything blindly is how a job that
was deliberately off for three months comes back to life and processes three months of stale work.

## Deleting a CronJob without killing a run

Deleting a CronJob cascades to its Jobs and their pods (F-12), so a cleanup PR merged at 02:20 can
kill a settlement run that started at 02:00. The safe sequence:

```bash
kubectl -n billing patch cronjob payout-settlement --type=merge -p '{"spec":{"suspend":true}}'
kubectl -n billing get cronjob payout-settlement -o jsonpath='{.status.active}{"\n"}'   # wait for []
kubectl -n billing delete cronjob payout-settlement
```

If you must remove it immediately while letting the in-flight Job finish, orphan the children:

```bash
kubectl -n billing delete cronjob payout-settlement --cascade=orphan
```

The running Job survives with no owner, finishes normally, and will never be garbage-collected by
history limits — so note it down and delete it yourself.

## Changing a schedule

A schedule change takes effect immediately, has no rollout, no revision history, and no rollback.
Three consequences:

- **There is no `kubectl rollout undo` for a CronJob.** Your git history is the only record of
  what the schedule used to be. This is a good reason for schedules to live in git even in an
  organisation that is otherwise relaxed about it.
- **Making a schedule more frequent is riskier than it looks.** Going from hourly to `*/5`
  multiplies the object churn by 12 (doc 11), shrinks the overlap budget by 12 (doc 02), and can
  bring the job's duration-to-interval ratio above 1 in a single edit. Re-derive the overlap ratio
  before merging.
- **Making it less frequent can trip the missed-schedule wall** in the opposite direction than you
  would guess. The controller evaluates the *new* schedule against the old `lastScheduleTime`; a
  long gap plus a frequent schedule is the F-01 recipe, so if you are changing a schedule on a job
  that has not run in a while, set `startingDeadlineSeconds` in the same change.

Always verify the first firing after a schedule change instead of assuming:

```bash
kubectl -n $NS get cronjob $CJ -o jsonpath='{.spec.schedule}{"  next="}{.status.lastScheduleTime}{"\n"}'
```

## Rolling out a new image, and canarying it

A Deployment gives you a progressive rollout and an automatic halt when pods fail. A CronJob gives
you neither: you merge a new image digest, and the next firing runs it. If it is broken, the run
fails — and for a daily job you have burned a day, for a weekly job a week.

The canary procedure that fills the gap:

```bash
# 1. Merge the new digest into the CronJob. Nothing runs yet.
# 2. Immediately trigger one manual run from the updated template.
kubectl -n billing create job invoice-rollup-canary-20260912 \
  --from=cronjob/invoice-rollup
# 3. Watch it end to end.
kubectl -n billing wait --for=condition=complete --timeout=30m \
  job/invoice-rollup-canary-20260912
kubectl -n billing logs job/invoice-rollup-canary-20260912 --tail=100
# 4. Check the OUTCOME, not just the exit code (doc 08): rows written, watermark advanced.
# 5. If it failed, revert the digest in git before the scheduled firing arrives.
```

Step 2 is the whole point: it moves the feedback from "whenever the schedule next fires" to "two
minutes after merge", which is the single biggest reliability difference between a well-run
CronJob fleet and a poorly run one. For anything critical, wire this into the deployment pipeline
so it happens without anyone choosing to do it.

⚠️ For idempotent, watermark-driven jobs the canary is nearly free — it just does the work early.
For a job that is *not* idempotent, a canary run is a real execution with real side effects, so
either gate it behind a `--dry-run` flag the job itself implements, or point the canary at a
staging target. Which is another argument for the job having a dry-run mode at all: it makes the
job testable in production without being dangerous in production.

## Testing scheduled work

Scheduled work is under-tested because it is awkward to test, and the awkwardness is worth
attacking directly. Five layers, cheapest first:

**1. Run the container locally.** The work is a process with arguments and environment; it does not
need Kubernetes to exercise its logic. If your job can only be run inside the cluster, that is a
design problem worth fixing — it makes every subsequent layer harder.

```bash
docker run --rm \
  -e SCHEDULED_FOR=2026-09-11T15:10:00Z \
  -e DATABASE_URL=postgres://localhost/riverbend_test \
  registry.riverbend.internal/billing/invoice-rollup@sha256:5c1b9e... \
  --from=2026-09-11T15:00:00Z --to=2026-09-11T16:00:00Z
```

**2. Validate the manifest against the API server.** Catches an invalid schedule, a bad
`restartPolicy`, and admission-policy violations before merge — but *not* a valid schedule that
means the wrong thing:

```bash
kubectl apply --dry-run=server -f cronjob.yaml
```

**3. Assert the schedule's meaning in CI.** The day-of-month/day-of-week trap (F-05) and step-value
surprises are invisible to any syntax check. Compute the next few firings and assert them:

```python
# With croniter, or any cron library. The assertion is on INTENT, not syntax.
from croniter import croniter
from datetime import datetime, timezone
it = croniter("0 6 * * 1-5", datetime(2026, 9, 12, tzinfo=timezone.utc))
got = [it.get_next(datetime).isoformat() for _ in range(3)]
assert got == ["2026-09-14T06:00:00+00:00",   # Monday — note Saturday and Sunday are skipped
               "2026-09-15T06:00:00+00:00",
               "2026-09-16T06:00:00+00:00"], got
```

⚠️ Use a library whose semantics match Kubernetes' parser, and be aware they differ at the edges
(seconds fields, `?`, `L`). The test is still worth having: it catches the intent bugs, which are
the common ones, even if it cannot perfectly model the controller.

**4. The two-run test and the kill test** from doc 05. These are the tests that verify idempotency
and checkpointing, and they are the ones that pay off during a real incident.

**5. Staging on the real schedule.** Run the same CronJobs in staging with the same schedules, so
that a rotated credential, a tightened NetworkPolicy, or a raised Pod Security level breaks
staging first (F-17). For jobs whose schedule is too sparse for this to be useful — a weekly
vacuum — run staging on an accelerated schedule (hourly) against a smaller dataset. You lose
fidelity on volume and keep fidelity on *configuration*, which is what actually drifts.

A reasonable CI gate for any CronJob change:

- [ ] `kubectl apply --dry-run=server` passes
- [ ] next-N-firings assertion passes
- [ ] CronJob name ≤ 52 characters (F-07)
- [ ] `startingDeadlineSeconds`, `activeDeadlineSeconds`, `concurrencyPolicy` all explicitly set
- [ ] image is digest-pinned
- [ ] duration-to-interval ratio below 50% at p95 (doc 02) — from the last 30 days of metrics
- [ ] two-run test passes for any job with non-idempotent writes

## CronJobs in a GitOps world

Declarative tooling and CronJobs interact awkwardly in exactly one place, and it is worth knowing
about before it bites: **`suspend` is both an emergency control and a declared field.**

Suspend a CronJob with `kubectl patch` while git says `suspend: false`, and your GitOps controller
sees drift and reverts it — resuming a job you deliberately stopped, at a time nobody chose.
Riverbend had a job resume itself 40 minutes into an incident because Argo CD synced on a
schedule.

Pick one of two policies and write it down:

- **Suspension lives in git.** Emergency suspension is `kubectl patch` *immediately followed by* a
  PR that sets the same field, and the incident is not over until the PR is merged. This keeps a
  single source of truth, at the cost of needing a merge during an incident.
- **Suspension is operational state.** Configure the controller to ignore that field, so
  `kubectl patch` is authoritative:
  ```yaml
  # Argo CD Application
  spec:
    ignoreDifferences:
      - group: batch
        kind: CronJob
        jsonPointers:
          - /spec/suspend
  ```
  Simpler in an incident, but now git does not describe reality, so the doc-08 "suspended too
  long" alert becomes load-bearing rather than merely useful.

Either is defensible. Having neither means the behaviour is decided by whoever last touched it.

Two more GitOps notes specific to scheduled work:

- **Pruning deletes runs.** An Argo CD prune or `helm uninstall` that removes a CronJob cascades to
  its in-flight Job (F-12). For critical jobs, consider a resource policy that prevents automated
  deletion (`argocd.argoproj.io/sync-options: Delete=false`, or Helm's
  `helm.sh/resource-policy: keep`) so removal is always a deliberate human act.
- **Deploy the job's dependencies with it.** A CronJob that reads a new ConfigMap key fails at its
  next firing if the ConfigMap lands in a later sync wave. Unlike a Deployment, nothing fails
  loudly at deploy time, so ordering bugs surface hours later. Put the CronJob in a later sync
  wave than its ConfigMaps, Secrets, and RBAC.

## The per-job runbook

At 03:00 the person paged is usually not the person who wrote the job. Doc 07's `runbook`
annotation points at a document, and that document should answer six questions and nothing else:

1. **What does this job do, and who consumes its output?** One paragraph. "Aggregates the previous
   hour of orders into invoice lines. Finance's daily close reads these; a missing hour shows up as
   an unbalanced close."
2. **What is the impact of one missed run?** The question that decides whether to act now or at
   09:00. Be specific: "One missed hour is recoverable — the next run catches up via the watermark.
   Four consecutive misses delays the daily close."
3. **Is it safe to re-run?** The single most valuable line in the document. "Yes, idempotent on
   `(order_id, run_key)`. Manual re-runs are safe at any time."
4. **How do I re-run it, exactly?** The literal command, copy-pasteable, including the backfill
   form with its `--run-key` caveat.
5. **What are the known failure modes?** The three or four that have actually happened, with the
   distinguishing signal for each. This is where last quarter's incident should have added a line.
6. **Who owns it, and when does it become someone's emergency?** Team, channel, and the escalation
   threshold.

Keep it to one page. A runbook nobody can read in 90 seconds does not get read at 03:00.

## What to take away

1. Three commands orient you: `get cronjob` (is it firing), `describe cronjob` events (what did the
   controller try), and the Job list (what happened on recent runs). Everything below the pod is
   ordinary pod debugging — use [`../debug/`](../debug/README.md).
2. A manual Job from `--from=cronjob/...` has no owner reference, so `Forbid` cannot see it and
   nothing cleans it up. Name it descriptively and give it a TTL.
3. `suspend` stops future firings, not the current run. Annotate why, and after resuming, *verify*
   it fires — a long suspension may have left you behind the missed-schedule wall.
4. Capture what was already suspended before a fleet freeze, or resuming will start something that
   was off on purpose.
5. Suspend, confirm nothing is active, then delete — or use `--cascade=orphan` to let the current
   run finish.
6. A CronJob has no rollout and no rollback. Canary every image change with an immediate manual run
   and check the *outcome*, not the exit code; otherwise your feedback loop is the schedule
   interval.
7. Test in five layers: locally, `--dry-run=server`, a next-firings assertion, the two-run and kill
   tests, and staging on the real (or accelerated) schedule.
8. Decide explicitly whether `suspend` lives in git or is operational state. Leaving it undecided
   means a GitOps sync can resume a job mid-incident.
9. Write the six-question runbook. "Is it safe to re-run?" is the line that saves the most time at
   03:00.
