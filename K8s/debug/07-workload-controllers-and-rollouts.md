# Workload Controllers & Rollouts

Controllers reconcile *desired* (your spec) toward *actual* (running pods). When a rollout is
stuck or a workload won't reach desired count, the question is always: **what is the controller
waiting for, and why can't it proceed?**

## Fast triage

| Symptom | Jump to |
|---|---|
| `kubectl rollout status` hangs | [Stuck Deployment rollout](#stuck-deployment-rollout) |
| Replicas < desired, pods not appearing | [Desired vs actual](#desired-vs-actual) |
| StatefulSet stuck on one ordinal | [StatefulSet](#statefulset-rollouts) |
| DaemonSet not on all nodes | [DaemonSet](#daemonset-rollouts) |
| Job/CronJob not completing / not firing | [Jobs](#jobs--cronjobs) |
| Rollout "succeeded" but old pods linger | [Revisions](#revisions--rollback) |

```bash
kubectl -n $NS rollout status deploy/$DEPLOY --timeout=30s
kubectl -n $NS get deploy,rs,pods -l app=$APP -o wide
kubectl -n $NS describe deploy $DEPLOY    # conditions + events
```

---

## Desired vs actual

Trace the ownership chain: **Deployment → ReplicaSet → Pods**. Find where the number drops.

```bash
kubectl -n $NS get deploy $DEPLOY -o jsonpath='{.spec.replicas} desired / {.status.readyReplicas} ready / {.status.updatedReplicas} updated{"\n"}'
kubectl -n $NS get rs -l app=$APP        # which RS is scaling, DESIRED vs CURRENT vs READY
```

- **Deployment desired N, RS current < N** → the RS controller can't create pods: hitting a
  **ResourceQuota**, a **failing admission webhook** (doc 08), or the controller-manager is
  down (doc 05). Check RS events: `kubectl describe rs <rs>`.
- **RS current N, ready < N** → pods are created but not becoming Ready: schedule/start/probe
  problems — drop to doc 01. The controller is doing its job; the pods aren't healthy.
- **`Deployment` conditions** tell you directly:
  - `Progressing=False, reason=ProgressDeadlineExceeded` → rollout gave up (default 600s). The
    new pods never went Ready. Root cause is in the new pods (doc 01), not the Deployment.
  - `ReplicaFailure=True` → RS can't create pods (quota/webhook/scheduling). See its message.

---

## Stuck Deployment rollout

`RollingUpdate` proceeds only as fast as new pods become Ready, bounded by `maxSurge` /
`maxUnavailable`. It stalls when **new pods never reach Ready**.

```bash
kubectl -n $NS rollout status deploy/$DEPLOY
kubectl -n $NS get pods -l app=$APP -o wide     # find the new (not-Ready) pods
kubectl -n $NS describe deploy $DEPLOY | sed -n '/Conditions/,/Events/p'
```

Ranked causes:

1. **New pods crashlooping / not Ready** — bad image tag, bad config/secret, failing probe,
   OOM. The rollout *correctly* halts to protect availability; go debug the new pod (doc 01).
   This is the whole point of a rolling update — it won't tear down healthy old pods for
   broken new ones (unless your `maxUnavailable` is too permissive).
2. **`maxUnavailable: 0` + no room to surge** — can't add a surge pod (no capacity) and can't
   remove an old one → deadlock. Give capacity or allow `maxUnavailable > 0`.
3. **New pods Pending** — no capacity for the surge pod (doc 01/06).
4. **ProgressDeadlineExceeded** — rollout marked failed; it won't auto-rollback (you must).
5. **Paused rollout** — someone ran `kubectl rollout pause`. `kubectl rollout resume`.

Mitigation while you fix root cause:
```bash
kubectl -n $NS rollout undo deploy/$DEPLOY            # back to previous revision
kubectl -n $NS rollout undo deploy/$DEPLOY --to-revision=<n>
```

⚠️ A rolling update with a bad readiness probe that *passes* too early will happily roll a
broken version to 100% — the probe is your rollout's safety interlock. A too-strict probe
stalls good rollouts; a too-loose probe ships bad ones.

---

## StatefulSet rollouts

StatefulSets update pods **one at a time, in reverse ordinal order**, and *wait for each to be
Ready before proceeding*. One unhealthy ordinal blocks the entire rollout.

```bash
kubectl -n $NS get statefulset $STS -o wide
kubectl -n $NS get pods -l app=$APP       # which ordinal is stuck (e.g. -2 not Ready)
kubectl -n $NS describe statefulset $STS
```

- **Stuck on ordinal K** → pod-K isn't Ready (crashloop, probe, volume). Fix pod-K; the
  rollout resumes. It will *not* skip ahead — by design (ordered, safe for stateful systems).
- **`updateStrategy: OnDelete`** → the STS controller won't update pods automatically at all;
  you delete pods manually to trigger updates. "Rollout does nothing" may just be OnDelete.
- **Partitioned rollout** (`rollingUpdate.partition: K`) → only ordinals >= K update; a
  canary mechanism. If updates seem to "not apply" to low ordinals, check the partition.
- **Volume/identity issues** on reschedule → doc 03 (multi-attach, node loss, force-delete).
- ⚠️ Scaling a StatefulSet down does **not** delete PVCs by default; scaling back up reuses
  them (data persists) — usually what you want, occasionally a surprise. See `persistentVolume
  ClaimRetentionPolicy` (doc 03).

---

## DaemonSet rollouts

A DaemonSet runs one pod per (matching) node. "Not on all nodes" is usually scheduling.

```bash
kubectl -n $NS get ds $DS -o wide         # DESIRED vs CURRENT vs READY vs UP-TO-DATE
kubectl -n $NS describe ds $DS
```

- **DESIRED < node count** → nodeSelector/affinity excludes nodes, or **node taints without
  matching tolerations** (DS pods need tolerations for tainted nodes; system DaemonSets tolerate
  most taints). A tainted node with no toleration simply gets no DS pod.
- **CURRENT < DESIRED** → pods can't be created/scheduled on some nodes: no room (DS pods use
  requests too), image pull failing on specific nodes, or CNI/IPAM on that node.
- **`updateStrategy: RollingUpdate` with `maxUnavailable`** governs DS rollout speed; a stuck
  new pod on one node blocks progress there. `OnDelete` = manual, like STS.
- DS pods are often critical infra (CNI, kube-proxy, logging) — a DS rollout that bricks nodes
  is high-blast-radius; canary via `maxUnavailable: 1` and node labels.

---

## Jobs & CronJobs

```bash
kubectl -n $NS get job $JOB -o wide
kubectl -n $NS describe job $JOB          # completions, failures, backoff
kubectl -n $NS get cronjob $CJ -o wide
```

- **Job not completing** — pod exits non-zero, retries up to `backoffLimit`, then Job is
  `Failed`. Read the pod logs (doc 01). ⚠️ Non-native **sidecars** (mesh/SQL proxy) keep the
  pod running so the Job never completes — the app finished but the pod isn't Complete. Use
  native sidecars (`restartPolicy: Always` init container, 1.29+) or make the sidecar exit.
- **`activeDeadlineSeconds`** exceeded → Job terminated regardless of progress.
- **Parallelism/completions** confusion — Indexed jobs, `completions` vs `parallelism` shape
  how many pods and in what pattern.
- **CronJob not firing** — check `.spec.suspend: true`, a bad cron schedule, and
  `startingDeadlineSeconds`. If the controller was down past the deadline, runs are skipped.
  `concurrencyPolicy: Forbid` skips a run if the previous one is still going — a long-running
  job silently starves the schedule.
- **CronJob history** — `successfulJobsHistoryLimit`/`failedJobsHistoryLimit` govern how many
  old Jobs/pods linger; too high floods etcd, too low erases evidence before you can debug.

---

## Revisions & rollback

```bash
kubectl -n $NS rollout history deploy/$DEPLOY
kubectl -n $NS rollout history deploy/$DEPLOY --revision=3
kubectl -n $NS get rs -l app=$APP --sort-by=.metadata.creationTimestamp
```

- Old ReplicaSets are kept per `revisionHistoryLimit` (default 10) scaled to 0 — normal, not a
  leak. They're what `rollout undo` uses.
- ⚠️ **What a Deployment rollback does and doesn't cover**: `rollout undo` reverts the pod
  template (image, env, resources). It does **not** revert ConfigMaps/Secrets/CRs the app reads
  at runtime, nor database migrations. A "rollback" that only rolls back the Deployment while a
  migration already changed the schema can make things worse. Know your data/coupling.
- **Immutable-ish fields** — some spec fields (e.g. `selector`, certain volume settings) can't
  be changed in place; the API rejects the update. You must recreate the object.

---

## Prevention checklist

- Readiness probes that actually reflect "serving correctly" — they're your rollout interlock.
- `maxUnavailable`/`maxSurge` that leave room to make progress (not `maxUnavailable: 0` on a
  full cluster).
- Native sidecars for anything with a Job or a sidecar that must outlive/precede the app.
- Decouple config and migrations from Deployment rollbacks; have a real rollback plan for data.
- Canary DaemonSet/StatefulSet changes with `maxUnavailable: 1` / partitions — they're ordered
  and high-blast-radius.
