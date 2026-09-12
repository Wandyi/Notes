# Failure Scenario Catalogue

Twenty failure modes, each one seen in real clusters, grouped by what the failure *looks* like
rather than by which field causes it — because when you are investigating you start from the
symptom. Each entry is referenceable by its ID (`F-07`) from the other docs.

The grouping itself is diagnostic:

| Class | The question it answers | Scenarios |
|---|---|---|
| **A. Never fired** | Did the controller create a Job at all? | F-01 … F-07 |
| **B. Fired but never finished** | A Job exists; why is it still active? | F-08 … F-12 |
| **C. Finished, but wrongly** | It says success. Was it? | F-13 … F-17 |
| **D. Fleet-level and operational** | It is not one job; it is all of them | F-18 … F-20 |

The first branch in any CronJob investigation is that class boundary, and one command decides it:

```bash
kubectl -n $NS get cronjob $CJ
# LAST SCHEDULE empty or stale  -> class A (nothing was created)
# LAST SCHEDULE recent, ACTIVE non-zero and staying non-zero -> class B
# LAST SCHEDULE recent, ACTIVE 0 -> class C (Jobs are completing; question their outcome)
```

---

## Class A — the CronJob never fired

### F-01 · Past the 100-missed-schedules wall: stopped permanently

**What you see.** `LAST SCHEDULE` frozen hours or days ago. No Jobs. No failures. The CronJob
looks completely normal — correct schedule, `SUSPEND: False`.

**Mechanism.** Covered in detail in doc 01. The controller enumerates missed firings since
`lastScheduleTime`; past 100 it refuses to continue, emits `TooManyMissedTimes`, and stops. It
cannot recover on its own, because escaping requires a firing and the firing is what is blocked.
Most commonly triggered by suspending a frequent CronJob over a weekend — a `*/5` schedule
reaches 100 missed firings in **8 hours 20 minutes**.

**Confirm it.**
```bash
kubectl -n $NS describe cronjob $CJ | tail -20        # look for TooManyMissedTimes
kubectl -n $NS get cronjob $CJ -o jsonpath='{.status.lastScheduleTime}{"\n"}'
```
⚠️ Events expire after about an hour, so a CronJob stuck for days will show **no event at all**.
Absence of the event does not rule this out. The reliable signal is a `lastScheduleTime` many
intervals old combined with an unset `startingDeadlineSeconds`.

**Recover.** Setting the deadline bounds the search window, which immediately unblocks it:
```bash
kubectl -n $NS patch cronjob $CJ --type=merge \
  -p '{"spec":{"startingDeadlineSeconds":200}}'
# Then watch for a firing within one interval:
kubectl -n $NS get cronjob $CJ -w
```
If it still does not fire, delete and recreate the CronJob — that discards `status` and starts the
window from the new `creationTimestamp`. Do that only after confirming no Job is active.

**Prevent.** Set `startingDeadlineSeconds` on every CronJob in the fleet. Make it a policy check
(doc 10) so a manifest without it cannot merge.

### F-02 · Suspended and forgotten

**What you see.** Nothing has run since a date that correlates with an incident or a maintenance
window. `SUSPEND: True`.

**Mechanism.** Someone suspended it — correctly! — during an incident, and never resumed it.
`suspend: true` is the right tool for pausing a misbehaving job, and it has no expiry, no
reminder, and no alert. Worse: if the suspension was applied with `kubectl patch` while the
manifest in git still says `suspend: false`, a GitOps reconcile will silently resume it at an
unpredictable moment, which is its own hazard.

**Confirm it.**
```bash
kubectl get cronjobs -A -o custom-columns=\
'NS:.metadata.namespace,NAME:.metadata.name,SUSPEND:.spec.suspend,LAST:.status.lastScheduleTime' \
  | awk '$3=="true"'
```

**Recover.** `kubectl -n $NS patch cronjob $CJ --type=merge -p '{"spec":{"suspend":false}}'` —
then immediately check F-01, because a long suspension of a frequent job usually leaves you
behind the wall as well.

**Prevent.** Alert on any CronJob suspended for more than 24 hours (doc 08 has the query). Record
the reason in an annotation when you suspend, so the next person knows whether it is safe to
resume:
```bash
kubectl -n $NS annotate cronjob $CJ \
  ops.riverbend.io/suspended-reason="INC-4471, corrupt partner feed, resume after fix" \
  ops.riverbend.io/suspended-by="vaibhav" --overwrite
```

### F-03 · Ran at the wrong hour after a control-plane change

**What you see.** Everything ran, one hour early or late, cluster-wide or after an upgrade.

**Mechanism.** `timeZone` was unset, so schedules were interpreted in the
kube-controller-manager's local zone (doc 01). That zone is an implementation detail and can
change with a control-plane image. Or `timeZone` was set to a zone whose DST rules changed and
the control plane's `tzdata` is stale.

**Confirm it.** Compare the intended time with the `creationTimestamp` of recent Jobs — those are
in UTC and are the ground truth for when firing happened:
```bash
kubectl -n $NS get jobs -o json \
  | jq -r --arg cj "$CJ" '.items[]
      | select(.metadata.ownerReferences[]? | select(.kind=="CronJob" and .name==$cj))
      | [.metadata.name, .metadata.creationTimestamp,
         (.metadata.annotations["batch.kubernetes.io/cronjob-scheduled-timestamp"] // "-")]
      | @tsv'
```
⚠️ There is **no built-in label linking a Job to its CronJob** — the relationship is carried only
by the owner reference, which is why the filter above needs `jq`. The
`batch.kubernetes.io/cronjob-scheduled-timestamp` annotation (1.28+) records the *intended*
firing time, which is what makes it possible to tell a wrong schedule from a late start. Because
the owner-reference filter is awkward to type mid-incident, add your own label in
`jobTemplate.metadata.labels` (for example `cronjob: invoice-rollup`) on every CronJob you own;
doc 10's template does this, and then `kubectl get jobs -l cronjob=$CJ` just works.

**Recover and prevent.** Set `timeZone` explicitly on every CronJob, even when the value is
`"Etc/UTC"`.

### F-04 · One firing a year goes missing (or happens twice)

**What you see.** A daily job has 364 runs in a year, and the gap is the second Sunday in March.

**Mechanism.** The DST trap from doc 01. A schedule of `0 2 * * *` in a US zone has no matching
local time on spring-forward day, so nothing fires. Schedules in the repeated hour on fall-back
day are ambiguous.

**Confirm it.** Count runs per day over a year from your run ledger or from
`kube_cronjob_status_last_successful_time` history, and look for the transition dates.

**Fix.** Move the schedule to UTC, or out of the 00:00–03:00 local window.

### F-05 · A "monthly" job runs five times a month

**What you see.** A schedule intended to be rare fires weekly.

**Mechanism.** The day-of-month / day-of-week OR rule (doc 01). `0 3 1 * 1` means "the 1st **or**
any Monday", not "the 1st if it is a Monday".

**Confirm it.** Check whether both the 3rd and 5th fields are non-`*`. If they are, you have this
bug — there is no configuration in which the OR is what you wanted, because if you wanted OR you
would have written two CronJobs.

**Fix.** Restrict at most one of the two fields and put the extra condition in the job's code.

### F-06 · `FailedCreate`: the controller cannot create the Job

**What you see.** `LAST SCHEDULE` is not advancing, and `describe cronjob` shows repeated
`FailedCreate` events. The schedule is working perfectly; creation is being refused.

**Mechanism.** Four common refusals, distinguishable from the event message:

| Message contains | Cause | Fix |
|---|---|---|
| `exceeded quota` | A `ResourceQuota` in the namespace is full — often by *pods from previous failed jobs that were never cleaned up*, or by a `count/jobs.batch` quota | Clean up finished Jobs, set TTLs, raise the quota |
| `admission webhook ... denied the request` | A mutating/validating webhook rejects the pod template — a policy engine requiring labels, a sidecar injector that is down | Fix the manifest, or the webhook |
| `violates PodSecurity "restricted"` | Pod Security Admission enforcement was raised on the namespace and the job template runs as root or lacks a `securityContext` | Doc 07 |
| `serviceaccount "x" not found` | The SA was deleted or never created in that namespace | Create it |

⚠️ The webhook case is the nastiest, because a webhook whose backing service is down fails
*closed* by default, so **every CronJob in the cluster stops firing at once** with no other
symptom. Doc 08's fleet-wide staleness alert is designed to catch exactly this.

**Confirm it.**
```bash
kubectl -n $NS describe cronjob $CJ | sed -n '/Events/,$p'
kubectl get events -A --field-selector reason=FailedCreate --sort-by=.lastTimestamp | tail -30
```

### F-07 · The CronJob name is too long

**What you see.** The CronJob applies fine but no Job ever appears, or creation fails with a
complaint about an invalid generated name.

**Mechanism.** The Job name is the CronJob name plus a hyphen plus a timestamp (doc 00), and must
fit in 63 characters. So the **CronJob name must be ≤ 52 characters**. A Helm release prefix plus
a descriptive name crosses that line easily:
`riverbend-platform-billing-invoice-rollup-reconciliation` is 55.

**Confirm it.** `echo -n "$CJ" | wc -c`.

**Fix.** Shorten the name. Add a lint rule; it is a one-line check and it prevents a confusing
half-hour.

---

## Class B — a Job exists but never finishes

### F-08 · Hung job plus `Forbid`: every future firing silently skipped

**What you see.** One Job active for far longer than the job has ever taken. No failures. No
alerts. Subsequent firings simply do not happen.

**Mechanism.** Doc 02 in full. The pod is blocked — a database lock, an unbounded socket read, a
dependency that accepted the connection and went quiet — so the Job stays active, so `Forbid`
skips every firing. Riverbend lost 31 consecutive hourly rollups this way.

**Confirm it.**
```bash
kubectl -n $NS get jobs -l cronjob=$CJ \
  -o custom-columns='NAME:.metadata.name,ACTIVE:.status.active,START:.status.startTime'
kubectl -n $NS get events --field-selector reason=JobAlreadyActive | tail
# What is the pod actually waiting on?
kubectl -n $NS logs job/$JOB --tail=50
kubectl -n $NS exec -it $POD -- cat /proc/1/status 2>/dev/null   # if the image has a shell
```

**Recover.** Delete the hung Job (`kubectl -n $NS delete job $JOB`), which frees the next firing.
Then find the dependency that hung.

**Prevent.** `activeDeadlineSeconds` on every Job template — this scenario is the single strongest
argument for it — plus the `lastSuccessfulTime` staleness alert from doc 08.

### F-09 · The Job is active but no pod is running

**What you see.** `ACTIVE: 1` for twenty minutes and `kubectl get pods` shows the pod `Pending`.

**Mechanism.** This is a scheduling problem wearing a CronJob costume, and the debug collection
already covers the causes ([`../debug/01-pod-lifecycle-and-startup.md`](../debug/01-pod-lifecycle-and-startup.md)).
Three are disproportionately common for scheduled work:

- **No capacity, and the autoscaler is still booting a node.** Batch jobs often request more than
  a service pod does, and they arrive in bursts. Two to four minutes Pending is normal on a
  scale-up; doc 06 covers making that predictable.
- **A `nodeSelector` or toleration that matches a node pool which no longer exists.** Jobs are
  edited rarely, so they keep pointing at last year's pool name long after services were updated.
- **`ResourceQuota` allows the Job object but not the pod.** The Job is created, the pod is
  refused, and the Job sits at zero pods. The event is on the Job, not the pod that does not
  exist — so look there:
  ```bash
  kubectl -n $NS describe job $JOB | sed -n '/Events/,$p'
  ```

⚠️ Time spent Pending counts against `activeDeadlineSeconds`. A job with a 300-second deadline
that waits 240 seconds for a node has 60 seconds to do its work, and will "time out" while
looking, in the logs, like it barely started.

### F-10 · Repeated eviction or preemption exhausts the retry budget

**What you see.** A long job fails with several pods in `Failed`, reasons mentioning eviction,
preemption, or node shutdown. Your code appears in none of the logs.

**Mechanism.** Spot reclamation, node drain during a cluster upgrade, node memory pressure
evicting a `Burstable` pod, or preemption by a higher-priority workload. Each of those kills the
pod, the Job counts it as a failure, and enough of them exhaust `backoffLimit` (doc 03).

**Confirm it.**
```bash
kubectl -n $NS get pods -l batch.kubernetes.io/job-name=$JOB \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,REASON:.status.reason'
kubectl -n $NS get events --field-selector reason=Preempted,reason=Evicted | tail
```

**Fix.** Three layers, all worth having for long jobs:
1. `podFailurePolicy` with `action: Ignore` on `DisruptionTarget`, so disruptions do not consume
   retries (doc 03).
2. Protect the pod from *voluntary* disruption while it works:
   ```yaml
   template:
     metadata:
       annotations:
         cluster-autoscaler.kubernetes.io/safe-to-evict: "false"
   ```
   This stops the cluster autoscaler from removing a node to consolidate while a two-hour vacuum
   is running. ⚠️ It does not stop a node drain performed by a cluster upgrade, and if it is left
   on a job that hangs it can pin an expensive node indefinitely — always pair it with
   `activeDeadlineSeconds`.
3. Make the work **checkpointable** so a restart resumes rather than starting over (doc 05). For a
   two-hour job on spot capacity this is the only real answer.

### F-11 · Pods stuck `Terminating` after the Job is gone

**What you see.** Pods in `Terminating` for a long time; the Job may already have been deleted.

**Mechanism.** Two candidates. Either the container ignores SIGTERM and is waiting out
`terminationGracePeriodSeconds`, or the pod still carries the job-tracking finalizer and nothing
is removing it — typically after a force-delete of the Job, or while the Job controller is
unhealthy.

**Confirm it.**
```bash
kubectl -n $NS get pod $POD -o jsonpath='{.metadata.deletionTimestamp} {.metadata.finalizers}{"\n"}'
```

**Fix.** If it is a grace period, wait — or fix the app to handle SIGTERM. If it is an orphaned
finalizer with no owning Job, removing it is legitimate, but understand you are telling the API
server "nothing needs to account for this pod anymore":
```bash
kubectl -n $NS patch pod $POD --type=merge -p '{"metadata":{"finalizers":null}}'
```
⚠️ Do this only after confirming the owning Job no longer exists. Removing the finalizer on a pod
whose Job is still tracking it can make the Job's success/failure counts wrong.

### F-12 · Deleting the CronJob killed the run that was in progress

**What you see.** Someone removed a CronJob (a cleanup PR, a Helm release change) and an
in-flight 40-minute settlement run died with it.

**Mechanism.** Jobs carry an owner reference to their CronJob, so deleting the CronJob cascades:
Jobs are deleted, pods are deleted, work is interrupted mid-flight. The same applies to a Helm
uninstall or an Argo CD prune.

**Prevent.** Suspend first, confirm nothing is active, then delete:
```bash
kubectl -n $NS patch cronjob $CJ --type=merge -p '{"spec":{"suspend":true}}'
kubectl -n $NS get cronjob $CJ -o jsonpath='{.status.active}{"\n"}'   # wait for []
kubectl -n $NS delete cronjob $CJ
```
If you must remove the CronJob while letting the current Job finish, orphan instead of cascading:
```bash
kubectl -n $NS delete cronjob $CJ --cascade=orphan
```
The running Job survives, loses its owner, and will not be cleaned up by history limits — so
clean it up yourself afterwards.

---

## Class C — it completed, but the outcome was wrong

This class is the most dangerous, because every Kubernetes-level signal says success.

### F-13 · The job "succeeded" while doing nothing

**What you see.** Green runs for weeks. Then someone notices the output has been missing since a
date nobody can explain.

**Mechanism.** The container exited 0 despite failing. Kubernetes' entire notion of success is
the exit code of PID 1, so anything that swallows a failure turns into a false green. The usual
culprits:

```sh
# A shell pipeline reports only the LAST command's status. curl fails, jq succeeds, exit 0.
curl -s https://partner.example.com/feed | jq '.orders' > /tmp/out.json

# Without -e, the script continues past the failure and exits 0 from the final echo.
#!/bin/sh
./do-the-work                 # fails
echo "done"                   # exit 0

# The classic: someone silenced a flaky step during an incident and never removed it.
./upload-to-partner || true
```

**Confirm it.** Do not trust the exit code — check the job's *effect*. Does the output file
exist? Did the row count change? Is `lastSuccessfulTime` advancing while the downstream table is
stale? That gap is the signature.

**Fix.** In any shell entrypoint:
```sh
#!/bin/bash
set -Eeuo pipefail        # -e exit on error, -u undefined vars, -o pipefail catches curl above
```
And assert the outcome before exiting: "wrote N rows, N > 0" as an explicit check that exits
non-zero. `set -euo pipefail` is the cheapest single reliability improvement available to
scheduled work — and the deeper fix is the outcome metric in doc 08, which measures the *effect*
rather than the exit code.

### F-14 · `OOMKilled` mid-run, leaving partial writes

**What you see.** Exit code 137, `reason: OOMKilled` in `lastState`, and a downstream table with
some of the expected rows.

**Mechanism.** The mechanics are the same as any pod ([`../debug/01-pod-lifecycle-and-startup.md`](../debug/01-pod-lifecycle-and-startup.md)),
but batch work has a specific shape: memory scales with the *input size*, which grows with your
business. A job sized for 40,000 orders an hour OOMs the first time you hit 240,000 — so this
failure arrives precisely on your busiest day, and it arrives for all of a job class at once.

The compounding problem is partial work. If the job writes incrementally and dies halfway, the
retry re-reads the input and re-writes rows that already exist.

**Confirm it.**
```bash
kubectl -n $NS get pod $POD \
  -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}{"\n"}'
```

**Fix.** Raise the limit from measured working-set data, and — more importantly — **stream instead
of accumulating**: process in batches of 500 with a bounded buffer, so memory is a function of
batch size rather than input size. Then the job that handled 40,000 also handles 4,000,000. And
make the writes idempotent (doc 05) so the retry after an OOM is safe.

### F-15 · The same work executed twice

**What you see.** Double-billed invoices, a partner file uploaded twice, duplicate rows.

**Mechanism.** Doc 02 enumerates the paths. The ones that actually happen, in order of frequency:
a human ran `kubectl create job --from=cronjob/...` during an incident while the schedule also
fired; a DR or second cluster had the same CronJobs unsuspended; `concurrencyPolicy` was left at
`Allow`; a `Replace` deletion overlapped with its replacement during the grace period.

**Confirm it.** Look for two Jobs in the same window, including ones with no owner reference:
```bash
kubectl -n $NS get jobs --sort-by=.metadata.creationTimestamp \
  -o custom-columns='NAME:.metadata.name,OWNER:.metadata.ownerReferences[0].name,CREATED:.metadata.creationTimestamp'
```
The Jobs with an empty `OWNER` are the manual ones.

**Fix.** `Forbid` is table stakes. For anything where a duplicate is a money or correctness
problem, the exclusion must live at the data — doc 05.

### F-16 · A window of data was never processed

**What you see.** Data exists for 13:00 and 15:00 but not 14:00.

**Mechanism.** A firing was skipped — by `Forbid` while the previous run was slow, by
`startingDeadlineSeconds` expiring, or by a control-plane gap — and **nothing backfills** (doc 01).
For a job that processes "the previous hour", a skipped firing is permanently missing data unless
the job itself catches up.

**Confirm it.** Compare the set of expected firings against the runs in your ledger. This is
exactly why doc 05 recommends a ledger table: in SQL, "which hours have no completed run?" is one
query, and without one it is an archaeology project across expired events.

**Fix.** Make the job compute its own window from a **watermark** — "process everything since the
last successful high-water mark" — rather than from the clock. Then a skipped firing is
self-healing: the next run does two hours of work. Doc 05 builds this.

### F-17 · The job worked on deploy day and broke three weeks later

**What you see.** Nothing changed, and the job started failing.

**Mechanism.** Something *did* change; the job just does not run often enough to notice
immediately. Candidates, in rough order of likelihood:

- **A Secret or ConfigMap was rotated.** Unlike a Deployment, a CronJob has no rollout to fail —
  the next firing simply picks up the new value and dies. Credential rotation is the number one
  cause of "it broke on its own".
- **A mutable image tag moved.** `:latest` or `:stable` means the next firing runs code nobody
  tested. Digest-pin scheduled work (doc 07); the slow feedback loop of a daily job makes mutable
  tags far more dangerous here than for a service.
- **An RBAC change removed a permission** the job needs. Nothing fails until it next runs.
- **A NetworkPolicy was tightened** and the job's egress to a dependency is now denied. Very
  common with the "default deny plus allow list" pattern, where the list was built from
  always-running services and the weekly job was invisible during the audit.
- **The namespace's Pod Security Admission level was raised** and the job runs as root (F-06).

**The general shape:** a CronJob is a consumer of cluster configuration that only exercises that
configuration occasionally, so **the time between a breaking change and its symptom is the
schedule interval, not minutes.** A weekly job can be broken for six days before anyone can find
out. That latency is the argument for two practices: run scheduled jobs in staging on the same
schedule, and alert on staleness rather than on failure.

---

## Class D — fleet-level failures

### F-18 · Midnight thundering herd

**What you see.** Every night at 00:00, pods Pending for minutes, image pull timeouts, a node
scale-up spike, and occasionally jobs skipped en masse.

**Mechanism.** Doc 01's herd: 96 of Riverbend's CronJobs fired at exactly 00:00 UTC. The
resulting burst exceeded cluster capacity, the autoscaler took minutes, the registry throttled
concurrent pulls, and jobs with tight `startingDeadlineSeconds` skipped together.

**Fix.** Spread schedules deterministically from the job name (doc 01). Add a lint rule that
rejects `@daily`, `@hourly`, and minute-0 schedules in favour of a computed offset.

### F-19 · Etcd and apiserver load from job churn

**What you see.** Elevated apiserver write rate and etcd database size, correlated with nothing
anybody deployed.

**Mechanism.** Every firing writes a Job, at least one Pod, and several Events, then writes again
to update status, then writes deletions during cleanup. Multiply by the fleet: doc 11 works the
arithmetic out and finds Riverbend's 412 CronJobs generating on the order of 30,000 object
writes a day before any of them does any work.

**Fix.** Doc 11: cut frequency where it is unjustified, set TTLs so objects do not accumulate,
and consolidate many tiny jobs into fewer jobs that loop internally.

### F-20 · A cluster upgrade killed a fleet of in-flight jobs

**What you see.** After a node-pool rotation, dozens of Jobs failed with disruption-related
reasons, and several long jobs never completed.

**Mechanism.** A rolling node upgrade drains nodes. Draining evicts pods, including job pods
mid-run. PodDisruptionBudgets do not meaningfully protect a single-pod Job — a PDB with
`minAvailable: 1` over one pod blocks the drain rather than protecting the work, which just
stalls the upgrade until someone overrides it.

**Fix.** Treat scheduled work as a first-class input to maintenance planning:
1. Know your quiet window. Riverbend's is 09:00–13:00 UTC, when no scheduled job runs; node
   rotations are scheduled there.
2. For upgrades that cannot wait, suspend the long jobs first (F-02's annotation habit keeps that
   auditable), then upgrade, then resume — and re-check F-01 afterwards.
3. Make long jobs checkpointable (doc 05) so a mid-run kill costs minutes rather than the run.

---

## The one-page cheat sheet

| Symptom | Most likely | Doc |
|---|---|---|
| `LAST SCHEDULE` frozen, nothing running | F-01 missed-schedule wall, or F-02 suspended | 01 |
| `LAST SCHEDULE` frozen, `FailedCreate` events | F-06 quota / webhook / PSA / missing SA | 07 |
| Ran at the wrong time | F-03 time zone, F-04 DST | 01 |
| Runs far more often than intended | F-05 day-of-month/day-of-week OR | 01 |
| One Job active forever, later firings missing | F-08 hang plus `Forbid` | 02, 03 |
| Job active, pod `Pending` | F-09 capacity, selector, or quota | 06 |
| Pods failed without your logs | F-10 preemption or eviction | 03, 06 |
| Exit 137 | F-14 OOMKilled | 06 |
| Green for weeks but no output | F-13 exit 0 on failure | 08 |
| Duplicate side effects | F-15 double execution | 02, 05 |
| A gap in the data | F-16 skipped window, no backfill | 05 |
| Broke with no deployment | F-17 secret, image tag, RBAC, or policy drift | 07 |
| Nightly cluster-wide burst | F-18 thundering herd | 01, 06 |

## What to take away

1. Start every investigation by deciding the class: did it fire, did it finish, did it do the
   right thing? `kubectl get cronjob` answers that in one line.
2. The class-A failures (F-01, F-02, F-06) are the ones that persist for days, because a CronJob
   that is not firing produces no signal at all. They are also the ones a single field —
   `startingDeadlineSeconds` — and a single alert — staleness — would have caught.
3. The class-C failures are the expensive ones. Kubernetes reports success based solely on exit
   code, so `set -euo pipefail` and an outcome metric are not polish; they are the difference
   between knowing and not knowing.
4. Scheduled work fails on a delay. A weekly job can be broken by today's RBAC change and only
   tell you next Sunday. Assume drift, and exercise the same schedule in staging.
5. Nearly every scenario here is prevented by the same short list: `startingDeadlineSeconds`,
   `activeDeadlineSeconds`, `concurrencyPolicy: Forbid`, digest-pinned images, idempotent work,
   and a staleness alert. Doc 10 makes that list a template.
