# Install, Upgrade and Rollback — What the Lifecycle Engine Actually Does

This is Helm's third tool: the part that talks to the API server. It is where the expensive
failures live, because its failures happen in production, under time pressure, with a change half
applied.

Two ideas run through this doc.

> **`--wait` only waits for the kinds it has code for.** Everything else it applies and declares
> success on. A release that installs only custom resources completes instantly and tells you
> nothing about whether the thing works.

> **`helm rollback` restores Kubernetes objects and nothing else.** Not your database, not your
> CRDs, not the side effects of your hooks, not the image that has been garbage-collected from the
> registry. Every incident where rollback made things worse is a case of expecting it to restore
> something outside that boundary.

## `install` and `upgrade` are different code paths

They look like variations of one command and they are not, and three behaviours differ in ways
that matter.

| | `helm install` | `helm upgrade` |
|---|---|---|
| Existing objects | **Refuses** if an object exists without matching ownership metadata (`R-07`) | Adopts and patches objects the current release owns |
| Change computation | Creates everything | Three-way merge against the previous revision's manifest |
| Deletions | Nothing to delete | **Deletes** objects present in the old manifest and absent from the new one |
| Failure state | Release left in `failed` at revision 1, with objects possibly created | Release left `failed`, previous revision still `superseded` |
| Hooks run | `pre-install`, `post-install` | `pre-upgrade`, `post-upgrade` |

The practical consequence is that **`helm upgrade --install` is what you should always run**, in CI
and by hand. It installs if absent and upgrades if present, which makes your deploy command
idempotent and identical in every environment. There is essentially no reason to run bare
`helm install` in automation.

⚠️ One exception to know about: `helm upgrade --install` fails with
`"<name>" has no deployed releases` when the release exists but has never reached `deployed` —
typically because revision 1 failed. The release is not absent (so `--install` does not trigger a
fresh install) and there is nothing to upgrade from. Doc 17, `O-02`, has the recovery; the
one-liner is `helm uninstall --keep-history` is *not* what you want, `helm uninstall` followed by a
fresh install is.

## What `--wait` actually waits for

This is the highest-value paragraph in the doc. `--wait` polls resources until they report ready,
and **it has readiness logic only for a specific list of kinds**:

| Kind | Ready means |
|---|---|
| `Pod` | Phase is `Running` and all containers ready — or phase is `Succeeded` |
| `Deployment` | `status.updatedReplicas` and `status.readyReplicas` meet the spec, accounting for the rollout strategy |
| `StatefulSet` | Updated and ready replicas meet the spec, respecting `partition` in a partitioned update |
| `DaemonSet` | `numberReady` equals `desiredNumberScheduled` and `updatedNumberScheduled` matches |
| `ReplicaSet`, `ReplicationController` | Ready replicas meet the spec |
| `PersistentVolumeClaim` | Phase is `Bound` |
| `Service` | A `LoadBalancer` has an ingress address; other types are immediately ready |
| `Job` | **Only with `--wait-for-jobs`.** Otherwise a Job is ready as soon as it is created |
| `CustomResourceDefinition` | The `Established` condition is true |
| **Anything else, including every custom resource** | **Ready the instant the API server accepts it** |

Read that last row again. `Prometheus`, `Certificate`, `ExternalSecret`, `Kafka`, `Rollout`,
`Cluster`, `IngressRoute`, `VirtualService` — every CR your platform is built on — is "ready" to
Helm as soon as it is stored. Helm has no idea whether the controller behind it did anything.

⚠️ And `Ingress` is not in the list, so `--wait` does not wait for an address to be assigned or for
the controller to program a route. A release that finishes successfully may have an Ingress nothing
has configured yet.

### What follows from this

**A chart that installs only custom resources cannot be waited on.** At Riverbend, the
`checkout-api-certs` release creates two cert-manager `Certificate` objects. `helm upgrade --wait`
returns in under a second, and the certificates are issued forty seconds later — or not at all, if
the issuer is misconfigured. The release said `deployed`. Nothing was wrong with Helm; the question
you asked it was not the question you meant.

**The workarounds, in order of robustness:**

1. **Let a controller that understands readiness do the waiting.** Flux's `HelmRelease` uses
   `kstatus` conventions and supports `spec.healthChecks`, so it can genuinely wait for a CR with
   a `Ready` condition. Argo CD has per-resource health assessments including Lua scripts for
   custom kinds. If you want CR readiness, **this is a reason to run Helm under a GitOps
   controller rather than from CI** — and one of the better reasons. Doc 11.
2. **Add a `post-install`/`post-upgrade` hook Job that blocks on the real condition.** A Job
   running `kubectl wait --for=condition=Ready certificate/checkout-api-tls --timeout=120s`.
   Hooks *are* waited for, so this converts an unwaitable thing into a waitable one:
   ```yaml
   apiVersion: batch/v1
   kind: Job
   metadata:
     name: {{ include "checkout-api.fullname" . }}-await-certs
     annotations:
       "helm.sh/hook": post-install,post-upgrade
       "helm.sh/hook-weight": "5"
       "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
   spec:
     backoffLimit: 0
     template:
       spec:
         serviceAccountName: {{ include "checkout-api.fullname" . }}-await
         restartPolicy: Never
         containers:
           - name: wait
             image: registry.riverbend.internal/kubectl:1.29
             command:
               - kubectl
               - wait
               - --for=condition=Ready
               - certificate/{{ include "checkout-api.fullname" . }}-tls
               - --timeout=120s
   ```
   ⚠️ This needs its own ServiceAccount with `get`/`list`/`watch` on that kind, and it makes your
   deploy depend on a container image and RBAC. Worth it for a release whose whole purpose is the
   CR; not worth it as a general pattern.
3. **Verify after deploying, outside Helm.** A CI step after `helm upgrade` that asserts the
   post-conditions. Least elegant, most transparent, and easiest to debug — which is often the
   right trade in a deploy pipeline.

**Do not add `--wait` and assume it means "verified".** `--wait` means "the kinds Helm understands
reached the state Helm checks for". That is a useful guarantee and a narrow one.

## `--atomic`, `--timeout`, `--force`, `--cleanup-on-fail`

Four flags that interact, and the interactions are where people get hurt.

**`--timeout` (default `5m0s`) is a *total* budget for the whole operation**, not per resource. It
covers hooks, apply, and the `--wait`. A release with a two-minute migration hook and a slow
rolling update can exceed five minutes without anything being wrong. Riverbend uses `8m` for
service releases and `20m` for anything with a data migration.

**`--wait` makes Helm block until readiness.** On its own, a timeout leaves the release `failed`
with the new objects applied — a *half-applied, unrolled-back* state. That is the honest default:
Helm told you it failed, and the cluster is in whatever state the partial rollout reached.

**`--atomic` implies `--wait` and adds "roll back on any failure".** So the failure states become
clean: either the upgrade succeeded, or it was rolled back. That is genuinely valuable and it is
the right default for most releases.

⚠️ **But `--atomic` has three costs that are not obvious.**

*First, the rollback can also time out.* If the upgrade timed out because the cluster cannot
schedule the new pods, the rollback needs to schedule the *old* pods, which may also fail — and now
you are in `pending-rollback`, which is worse than where you started. This is `CS-4`, and it
happened to Gateline ninety seconds before a ticket sale opened.

*Second, it converts a successful upgrade into a rollback if a `post-upgrade` hook fails.* The
application deployed fine; a smoke test was flaky; Helm rolled back a working deploy. `H-07`.

*Third, rollback restores objects and not side effects.* If the `pre-upgrade` migration ran and
then the rollout failed, `--atomic` gives you old code against a new schema. Whether that is safe
is a property of your migration discipline, not of Helm. This is the single most important thing to
reason about before enabling `--atomic` on a release with migrations, and doc 06 is about making it
safe.

**`--cleanup-on-fail`** deletes resources that this upgrade *created* if the upgrade fails. Without
it, a failed upgrade that created three new objects leaves them behind, and the next attempt hits
`R-07` on them. With `--atomic` you mostly do not need it, because the rollback handles it, but on
a non-atomic upgrade it is worth having.

**`--force`** does not mean "try harder". It means **replace instead of patch**: Helm issues a
`PUT` of the whole object rather than a merge patch, and for some kinds it deletes and recreates.
That gets you past an immutable-field error by destroying the object — which for a Service means a
new `clusterIP` and broken connections, and for a Deployment means every pod terminated at once
with no rolling update. ⚠️ `--force` is a tool for a very specific situation (`U-07`) and is
actively dangerous as a habit. If a runbook says "if it fails, add `--force`", delete that line
from the runbook.

### The flag set Riverbend actually uses

```bash
helm upgrade "$REL" "$CHART" \
  --install \
  --namespace "$NS" \
  --values "envs/${ENV}.yaml" \
  --set-string image.tag="$IMAGE_TAG" \
  --atomic \
  --timeout 8m \
  --history-max 5 \
  --wait-for-jobs \
  --description "$CI_PIPELINE_URL"
```

Two of those are underused and worth calling out.

**`--wait-for-jobs`** makes `--wait` actually wait for Jobs in the manifest to complete. Without
it, a Job in your templates (not a hook — an ordinary Job) is "ready" when created, so a release
whose purpose is to run a job reports success before the job starts.

**`--description`** sets the text that appears in `helm history`. Putting the CI pipeline URL there
means that six weeks later, when you are looking at `helm history` during an incident, every
revision links to the pipeline that created it. This costs one flag and repays itself the first
time you use it:

```
REVISION  UPDATED                   STATUS      CHART               DESCRIPTION
45        Mon Sep 22 09:14:02 2026  superseded  checkout-api-4.11.1 https://ci.riverbend.internal/p/8841
46        Wed Sep 24 11:02:55 2026  superseded  checkout-api-4.11.2 https://ci.riverbend.internal/p/8903
47        Mon Sep 29 16:44:01 2026  deployed    checkout-api-4.11.3 https://ci.riverbend.internal/p/8977
```

## Rollback's five blind spots

`helm rollback checkout-api 46` re-applies revision 46's manifest as a new revision 48. What that
does and does not restore:

**Restores:** every Kubernetes object in the manifest, back to the spec revision 46 rendered.
Objects created since are deleted; objects deleted since are recreated.

**Does not restore, and this is the whole list:**

1. **Your database.** A `pre-upgrade` migration that added a column, dropped a column, or
   backfilled a table is not undone. The old code now runs against the new schema. Doc 06.
2. **CRDs.** Anything in `crds/` is untouched by rollback, as it is by upgrade. If revision 47
   somehow updated a CRD (only possible if the CRD is in `templates/`), rollback reverts it — and
   reverting a CRD schema while resources exist that use the new fields is its own incident. Doc
   07, `C-06`.
3. **Hook side effects.** A `post-upgrade` hook that sent a webhook, invalidated a cache,
   registered a service, or published a message has already done it. Rollback runs `pre-rollback`
   and `post-rollback` hooks — which are a *different set of hooks*, and if you have not written
   them, nothing compensates.
4. **Anything with `helm.sh/resource-policy: keep`.** Deliberately excluded from Helm's
   management of deletions, so an object added in revision 47 with that annotation survives the
   rollback.
5. **The image.** Revision 46's manifest references `:2026.09.14`. If your registry's retention
   policy garbage-collected it — or if somebody rebuilt and overwrote a mutable tag — the rollback
   produces pods in `ImagePullBackOff`. ⚠️ This is the most common way a rollback fails outright,
   and it is entirely preventable: **immutable tags plus a retention policy longer than your
   rollback window.** Riverbend keeps images for 90 days and `--history-max 5`, so the window is
   comfortably covered.

**Also worth knowing:** rollback uses the *chart stored in revision 46's Secret*, not the chart on
disk. This is a good thing — it is why rollback works when the chart has been deleted from the
repository — and it means a rollback does not pick up a chart fix you just made. If you want the
new chart with the old values, that is an upgrade, not a rollback.

## The `U-` failure catalogue

### U-01 · The upgrade hangs

**What you see.** `helm upgrade` produces no output for minutes.

**Mechanism.** Helm is in step 8 — waiting. It prints nothing while waiting, which makes a normal
wait indistinguishable from a stuck one. What it is waiting on is visible only in the cluster.

**Confirm it.** In another terminal, go to the workload, not to Helm:

```bash
kubectl -n checkout rollout status deploy/checkout-api --timeout=10s
kubectl -n checkout get pods -l app.kubernetes.io/instance=checkout-api \
  -o wide --sort-by=.metadata.creationTimestamp | tail -20
kubectl -n checkout get events --sort-by=.lastTimestamp | tail -30
```

The events are usually decisive, and the five things they usually say:

| Event | Meaning |
|---|---|
| `FailedScheduling: 0/380 nodes are available: 24 Insufficient cpu` | No capacity. The cluster autoscaler may fix it; `--timeout` may expire first |
| `Failed to pull image ... not found` | Bad tag. Will never succeed |
| `Readiness probe failed: HTTP probe failed with statuscode: 503` | The app started and is not healthy |
| `CrashLoopBackOff` with no events | Look at `kubectl logs --previous` |
| Nothing at all, pods `Running` and ready | Helm is waiting on something *other* than this workload — a PVC, a LoadBalancer Service, or a hook |

For that last case, find what is not ready across the whole release:

```bash
helm get manifest checkout-api -n checkout \
  | yq -r 'select(.kind!=null) | "\(.kind)/\(.metadata.name)"' \
  | while read -r kn; do
      printf '%-50s %s\n' "$kn" "$(kubectl get "$kn" -n checkout -o jsonpath='{.status.conditions[*].type}{" "}{.status.phase}' 2>&1)"
    done
```

**Recover.** Decide whether it will succeed. If it will not, let the timeout fire rather than
`Ctrl-C` — see `U-09` for why interrupting is the thing that creates a stuck release.

**Prevent.** ⚠️ A `readinessProbe` whose `initialDelaySeconds` plus `failureThreshold` ×
`periodSeconds` exceeds your `--timeout` guarantees a timeout on a healthy deploy. Do the
arithmetic once per service: Riverbend's `checkout-api` has `initialDelaySeconds: 20`,
`periodSeconds: 5`, `failureThreshold: 6`, so worst case a pod takes 50 seconds to be declared
ready; with `maxSurge: 25%` over 24 replicas that is 4 batches ≈ 200 seconds, comfortably inside
8 minutes. A service with a 3-minute JVM warm-up and 40 replicas does not fit in 8 minutes, and
its timeout needs to be set from that arithmetic rather than from the default.

### U-02 · `--timeout` expired, and what state you are in

**What you see.**

```
Error: UPGRADE FAILED: timed out waiting for the condition
```

**Mechanism.** The budget expired. The new manifest **has been applied**. Pods are mid-rollout.

**Confirm it.** `helm history` shows the new revision as `failed`:

```
47        Mon Sep 29 16:44:01 2026  failed      checkout-api-4.11.3  Upgrade "checkout-api" failed: timed out waiting for the condition
```

Note that the release is `failed`, not `pending-upgrade` — Helm finished and recorded the failure.
You **can** upgrade again from here. That distinction is the difference between `U-02` (annoying)
and `U-09` (stuck).

**Recover.** Three options and the choice depends on whether the rollout is progressing:

```bash
# Is it still making progress, just slowly?
kubectl -n checkout get deploy checkout-api -o jsonpath=\
'{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
```

- `Progressing=True ReplicaSetUpdated` — it is working. Wait, then `helm upgrade` again with a
  longer `--timeout` to record success.
- `Progressing=False ProgressDeadlineExceeded` — it will not finish. Roll back:
  `helm rollback checkout-api -n checkout --wait --timeout 8m`.
- Under `--atomic`, Helm already attempted the rollback. Check whether it succeeded.

**Prevent.** Set `--timeout` from the probe-and-surge arithmetic above, and set the Deployment's
`progressDeadlineSeconds` to something *less* than your Helm timeout so that Kubernetes declares
the rollout failed before Helm gives up — which gives you a clear `ProgressDeadlineExceeded`
reason instead of an ambiguous "timed out".

### U-03 · `--atomic` rolled back a working deploy

**What you see.** The deploy is reported as failed and rolled back; the application was fine.

**Mechanism.** Any failure after the apply triggers rollback, including a flaky `post-upgrade`
smoke test and a `--wait` timeout on a resource unrelated to the change.

**Recover.** Nothing to recover; you are on the old version. Investigate what failed:

```bash
helm history checkout-api -n checkout | tail -4
kubectl -n checkout get jobs -l 'helm.sh/hook' -o wide     # hook Jobs, if the delete policy kept them
kubectl -n checkout logs job/checkout-api-smoke-test
```

**Prevent.** Decide, per release, whether `--atomic` is right — it usually is, and the two cases
where it is not are worth naming:

- **A release with non-reversible hooks** (a forward-only migration). Here `--atomic` creates
  old-code-against-new-schema, and you want `--wait` without `--atomic` so a human decides. Doc 06.
- **A release under a hard deadline** where a failed rollback is worse than a half-applied
  upgrade — Gateline's pre-sale window, `CS-4`.

And make `post-upgrade` hooks not flaky: `backoffLimit: 2` on a smoke-test Job so a single
transient failure retries rather than triggering a fleet rollback.

### U-04 · `--wait` reported success on something that never became healthy

Covered in full above. The scenario shape:

**What you see.** `helm upgrade --wait --atomic` succeeded in two seconds. The thing does not work.

**Confirm it.** Compare what Helm applied against what has a healthy status:

```bash
helm get manifest checkout-api-certs -n checkout | yq -r 'select(.kind!=null)|.kind' | sort | uniq -c
```

If the kinds are all custom, `--wait` did nothing.

**Prevent.** One of the three workarounds above. The general principle worth taking away: **when a
tool's guarantee does not cover your case, arrange for the gap to be loud.** A hook that blocks on
`kubectl wait` is louder than a post-deploy check nobody reads, which is louder than trusting
`--wait`.

### U-05 · A Job in the manifest was not waited for

**What you see.** A release whose job has not run yet reports success; a downstream step fails.

**Mechanism.** An ordinary `Job` (no hook annotation) is "ready" when created, unless
`--wait-for-jobs`.

**Prevent.** `--wait-for-jobs` in the standard flag set. ⚠️ And know that this also makes Helm wait
for `Job`s you did not intend to block on — a long-running backfill Job in the chart now holds the
deploy. If you have both kinds, the long one belongs in a separate release or as a CronJob.

### U-06 · `"<name>" has no deployed releases`

**What you see.** `helm upgrade --install` fails with that message on a release that exists.

**Mechanism.** Every revision is `failed` (or all are `pending-*`), so there is no `deployed`
revision to upgrade from. Most commonly: the very first install failed.

**Confirm it.**

```bash
helm history checkout-api -n checkout --max 20 -o json | jq -r '.[] | "\(.revision)\t\(.status)"'
```

**Recover.** If revision 1 failed and nothing ever succeeded, the release has no good state and the
cleanest path is to remove it and install fresh:

```bash
# See what it thinks it owns, so you know what the uninstall will remove
helm get manifest checkout-api -n checkout

helm uninstall checkout-api -n checkout          # ⚠️ deletes the objects it owns
helm upgrade checkout-api ./charts/checkout-api --install -n checkout \
  -f envs/prod-use1.yaml --atomic --timeout 8m --history-max 5
```

If there *is* an older `deployed` revision, roll back to it instead — that restores a good state
and gives you something to upgrade from:

```bash
helm rollback checkout-api 44 -n checkout --wait --timeout 8m
```

**Prevent.** ⚠️ `--atomic` on the *first* install does not help, because there is no previous
revision to roll back to; what it does instead is uninstall the release entirely, which is usually
what you want for a failed first install. The genuine prevention is that a first install into a new
namespace should be tested somewhere disposable first, which is `use case 6`'s ephemeral
environments earning their keep.

### U-07 · `field is immutable`

**What you see.**

```
Error: UPGRADE FAILED: cannot patch "checkout-api" with kind Deployment:
Deployment.apps "checkout-api" is invalid: spec.selector: Invalid value: ...
field is immutable
```

**Mechanism.** Some fields cannot be changed after creation. The ones that catch people:

| Kind | Immutable field | Typical cause |
|---|---|---|
| `Deployment`, `ReplicaSet`, `DaemonSet` | `spec.selector` | A label helper changed; a chart version bumped `helm.sh/chart` *into* the selector (`T-08`) |
| `StatefulSet` | Everything except `replicas`, `template`, `updateStrategy`, `persistentVolumeClaimRetentionPolicy`, `ordinals`, `minReadySeconds` | Adding a volume claim template; changing `serviceName` |
| `Service` | `spec.clusterIP`; `spec.type` transitions in some directions | Switching `ClusterIP` to `LoadBalancer` or back |
| `Job` | `spec.template`, `spec.selector`, `spec.completions` | Re-running a Job with a changed image — the reason hook Jobs need `before-hook-creation` |
| `PersistentVolumeClaim` | Everything except `resources.requests.storage`, and that only **upward** with an expandable StorageClass | Trying to shrink a volume |
| `CustomResourceDefinition` | `spec.names.kind`, `spec.group`, `scope` | Renaming a CRD |

**Confirm it.** See exactly what differs:

```bash
diff <(kubectl get deploy -n checkout checkout-api -o json | jq -S '.spec.selector') \
     <(helm template checkout-api ./charts/checkout-api -f envs/prod-use1.yaml \
        | yq -o=json 'select(.kind=="Deployment").spec.selector' | jq -S '.')
```

**Recover.** Three ways, in increasing order of disruption. Pick the least one that works.

**(a) Make the new manifest match.** If the selector change was accidental — a chart refactor that
pulled a version label into `spec.selector` — fixing the template is the right answer and costs
nothing. Check this first; it is the case more often than people assume.

**(b) Delete the object, keeping its children, then upgrade.** For a Deployment:

```bash
# --cascade=orphan leaves the ReplicaSet and pods running, so there is NO downtime
kubectl delete deploy -n checkout checkout-api --cascade=orphan

helm upgrade checkout-api ./charts/checkout-api -n checkout -f envs/prod-use1.yaml \
  --atomic --timeout 8m
```

⚠️ `--cascade=orphan` is the part that makes this safe and is the part people omit. Without it,
deleting the Deployment terminates every pod immediately. With it, the old pods keep serving while
Helm creates the new Deployment, which adopts or replaces them through a normal rolling update. You
are left with an orphaned old ReplicaSet to clean up:

```bash
kubectl get rs -n checkout -l app.kubernetes.io/instance=checkout-api \
  -o json | jq -r '.items[] | select(.metadata.ownerReferences == null) | .metadata.name'
```

For a `Service`, there is no orphan option and deleting it changes the `clusterIP` — so for a
ClusterIP service in front of live traffic, plan a maintenance window or create a new Service and
migrate.

**(c) `--force`.** Replaces rather than patches, and for a Deployment that means a delete and
recreate with no `--cascade=orphan`, so every pod goes at once. ⚠️ **This is a full outage of the
service for the duration of the new rollout.** It is the right answer approximately never for a
production Deployment, and it is the answer people reach for first because it is one flag.

**Prevent.** The selector discipline from `T-08`: `spec.selector` uses only
`app.kubernetes.io/name` and `app.kubernetes.io/instance`, and nothing that changes with a version.
Plus a CI check that the upgrade path works, which is the point of doc 14's upgrade test — install
the previous chart version, then upgrade to the new one, in a throwaway cluster. That single test
catches every scenario in this section before production sees it, and almost nobody runs it.

### U-08 · Renamed objects leave orphans

**What you see.** Two Deployments, one with no pods; twice the expected Services; an old ConfigMap
nothing mounts.

**Mechanism.** Helm's deletion logic compares manifests by *kind plus name plus namespace*. Rename
an object and Helm sees "one object gone, one new object" and does the right thing — it deletes the
old one. **Unless the name change came from a change in the fullname helper *and* the old object no
longer matches what Helm recorded**, or unless the rename happened at the same time as an
interrupted upgrade.

The common real cause is different: the object was created by revision 46 under one name, revision
47 renamed it, and revision 47's *apply* failed partway — so the new object exists, the old one
was not yet deleted, and the release is `failed`. Now you have both, and the next upgrade only
knows about the names in the last *successful* manifest.

**Confirm it.** Compare what Helm owns against what carries its ownership annotation:

```bash
# What Helm believes it owns
helm get manifest checkout-api -n checkout \
  | yq -r 'select(.kind!=null) | "\(.kind|ascii_downcase)/\(.metadata.name)"' | sort > /tmp/owned.txt

# What actually carries this release's ownership annotation
kubectl get deploy,svc,cm,secret,sa,hpa,pdb -n checkout -o json \
  | jq -r --arg r checkout-api '.items[]
      | select(.metadata.annotations["meta.helm.sh/release-name"]==$r)
      | "\(.kind|ascii_downcase)/\(.metadata.name)"' | sort > /tmp/actual.txt

comm -13 /tmp/owned.txt /tmp/actual.txt   # orphans: annotated as ours, not in the manifest
```

**Recover.** Delete the orphans by hand, after confirming nothing references them. The `comm`
output is your list.

**Prevent.** Run that comparison as a scheduled audit — doc 16, `I-05`. Orphans accumulate silently
and are found either by an audit or by a cost review.

### U-09 · `another operation (install/upgrade/rollback) is in progress`

**What you see.**

```
Error: UPGRADE FAILED: another operation (install/upgrade/rollback) is in progress
```

and `helm history` shows the latest revision as `pending-upgrade`.

**Mechanism.** Helm writes a `pending-*` release record *before* doing the work and updates it to
`deployed` or `failed` after. If the Helm process dies in between — `Ctrl-C`, a CI job timeout, a
killed runner pod, a laptop closing — the record stays `pending-upgrade` forever. There is no lease
and no timeout; nothing ever cleans it up.

⚠️ **This means Helm's "lock" is not a lock.** It is a flag with no owner and no expiry, and it
cannot distinguish "another process is genuinely working right now" from "a process died two weeks
ago". That asymmetry is the whole problem: Helm has to refuse, because proceeding might mean two
concurrent upgrades, and it cannot tell.

**Confirm it.** Establish whether anything is actually running, because that determines whether it
is safe to intervene:

```bash
helm history checkout-api -n checkout --max 5
kubectl get secret -n checkout -l 'owner=helm,name=checkout-api' \
  -o custom-columns='NAME:.metadata.name,STATUS:.metadata.labels.status,MODIFIED:.metadata.labels.modifiedAt'
```

Convert `modifiedAt` (a Unix timestamp) to an age. **If it is older than your longest `--timeout`
plus a margin, no process is working and it is safe to proceed.** If it is two minutes old, wait —
you may be about to collide with a live deploy.

Then check your CI: is a pipeline for this release running right now? This is the check people skip
and it is the one that prevents making things worse.

**Recover.** In order of preference:

**(a) `helm rollback` to the last `deployed` revision.** This is the supported path, it works in
most cases, and it leaves clean state:

```bash
LAST_GOOD=$(helm history checkout-api -n checkout -o json \
  | jq -r 'map(select(.status=="deployed")) | max_by(.revision) | .revision')
echo "rolling back to $LAST_GOOD"
helm rollback checkout-api "$LAST_GOOD" -n checkout --wait --timeout 8m
```

⚠️ `helm rollback` with no revision argument goes to the previous revision, which may itself be the
`pending` one. Always pass the revision explicitly, computed as above.

**(b) Delete the pending revision's Secret.** If there is no `deployed` revision to roll back to
(the `pending` one is revision 1), the pending record is the only thing in the way:

```bash
# ⚠️ DESTRUCTIVE to release history. Confirm nothing is running first (above).
# Look at it before deleting it.
kubectl get secret -n checkout sh.helm.release.v1.checkout-api.v47 \
  -o jsonpath='{.data.release}' | base64 -d | base64 -d | gzip -d | jq '.info'

kubectl delete secret -n checkout sh.helm.release.v1.checkout-api.v47
```

Helm now sees revision 46 as the latest and you can upgrade. The objects revision 47 partially
applied are still in the cluster and Helm does not know about them — so the next upgrade's
three-way merge uses revision 46 as "old", which is usually what you want and occasionally leaves
an orphan (`U-08`).

**(c) Edit the status in the payload.** The one you find on the internet, usually described as
"just patch the label". ⚠️ **Patching the label is not enough** — the authoritative status is inside
the gzipped payload (doc 04), so a label-only fix produces a release that looks fine in `helm list`
and still refuses to upgrade. The full, correct surgery is in doc 17, `O-01`, and it should be your
third choice, not your first.

**Prevent.** Four things, and the first two matter most:

1. **Never `Ctrl-C` a `helm upgrade`.** Let it time out. A timeout writes `failed`, which is
   recoverable; an interrupt writes nothing, which is not.
2. **Set the CI job timeout to be longer than `--timeout` plus a margin.** If the job is killed at
   10 minutes and Helm's timeout is 8 minutes plus a 3-minute rollback, the job kills Helm mid-
   rollback. At Riverbend the rule is `job_timeout >= helm_timeout × 2 + 2m`.
3. **Serialise deploys per release.** A CI concurrency group keyed on `release+namespace` prevents
   two pipelines from racing, which is the other way you reach this state (`U-15`).
4. **Alert on `pending-*` older than 15 minutes** — doc 16, `I-03`. This state does not resolve
   itself, so the only question is whether you find it or your next deploy does.

### U-10 · `--force` caused an outage

**What you see.** Every pod of a service terminated at once during a deploy.

**Mechanism.** `--force` replaces objects. For a Deployment, replacement means delete and create,
with the default cascade — so the ReplicaSet and every pod go immediately, and the service is down
until the new rollout completes.

**Prevent.** Remove `--force` from every script and runbook you own. When you genuinely need to get
past an immutable field, `U-07`'s option (b) with `--cascade=orphan` does the same job without the
outage. Grep for it:

```bash
grep -rn 'helm upgrade.*--force' . --include='*.sh' --include='*.yaml' --include='*.yml'
```

### U-11 · Objects left behind by a failed upgrade

**What you see.** The next upgrade fails with `R-07` on an object the *failed* upgrade created.

**Mechanism.** A failed non-atomic upgrade leaves what it managed to apply. Newly created objects
are owned by a release whose revision is `failed`, and the ownership check can then disagree.

**Prevent.** `--atomic`, or `--cleanup-on-fail` if you have a reason not to use `--atomic`.

### U-12 · Rollback produced `ImagePullBackOff`

**What you see.** A rollback whose pods cannot start.

**Mechanism.** Blind spot 5. The old image is gone from the registry, or a mutable tag now points
at different content.

**Confirm it.**

```bash
OLD_TAG=$(helm get manifest checkout-api -n checkout --revision 46 \
  | yq -r 'select(.kind=="Deployment").spec.template.spec.containers[0].image')
echo "$OLD_TAG"
crane manifest "$OLD_TAG" >/dev/null && echo "present" || echo "GONE — rollback to 46 will not start"
```

**Prevent.** Immutable tags, and a registry retention policy longer than your rollback window. Then
add the check above as a pre-flight step in your rollback runbook, so you find out in one second
rather than after the rollback half-completes.

### U-13 · Rollback to a revision whose chart is incompatible with the cluster

**What you see.** A rollback failing with `no matches for kind "PodDisruptionBudget" in version
"policy/v1beta1"`.

**Mechanism.** Rollback replays the manifest stored in the old revision — including its API
versions. If the cluster has been upgraded since and removed that API version, the stored manifest
is no longer applicable. So **a Kubernetes upgrade silently invalidates your older rollback
targets.**

**Prevent.** After every cluster upgrade, treat pre-upgrade revisions as non-rollbackable and say
so. Practically: run a forward deploy of every release shortly after a control-plane upgrade so
that each release's recent history uses current API versions. At Riverbend this is a scripted pass
over all 240 releases on the day after a cluster upgrade, and it exists specifically so that
"roll back" remains a valid answer for the following week.

### U-14 · Uninstall left things behind

**What you see.** After `helm uninstall`, storage still costing money; a Secret still present; a CRD
and all its resources still there; a namespace still existing.

**Mechanism.** Five categories `helm uninstall` deliberately does not delete:

1. **Anything annotated `helm.sh/resource-policy: keep`.** By design — it is how a chart protects a
   PVC or a generated Secret. The object becomes an orphan with no owner. ⚠️ It also then blocks a
   reinstall with `R-07`.
2. **PVCs created from a StatefulSet's `volumeClaimTemplates`.** These are not in the manifest —
   the StatefulSet controller creates them — so Helm never knew about them. Kubernetes 1.27+ has
   `persistentVolumeClaimRetentionPolicy` on the StatefulSet, which is the actual fix.
3. **Everything in `crds/`.** By design (doc 07).
4. **Hook resources whose delete policy did not cover the case.** Doc 06, `H-05`.
5. **The namespace.** Helm never deletes a namespace, even one it created with
   `--create-namespace`.

**Confirm it.** Before uninstalling, know what will survive:

```bash
REL=checkout-api; NS=checkout
echo "== kept by resource-policy =="
helm get manifest "$REL" -n "$NS" \
  | yq -r 'select(.metadata.annotations["helm.sh/resource-policy"]=="keep") | "\(.kind)/\(.metadata.name)"'
echo "== PVCs from volumeClaimTemplates =="
kubectl get pvc -n "$NS" -o json | jq -r '.items[]
  | select((.metadata.annotations // {}) | has("meta.helm.sh/release-name") | not)
  | "\(.metadata.name)\t\(.spec.resources.requests.storage)"'
echo "== hook resources =="
kubectl get jobs,pods -n "$NS" -l 'helm.sh/hook' 2>/dev/null
```

**Recover.** Delete the leftovers explicitly. For an ephemeral environment, deleting the namespace
handles categories 1, 2 and 4 in one command, which is why the ephemeral teardown in doc 01
includes it.

**Prevent.** For ephemeral environments, always delete the namespace. For real ones, an orphan audit
(doc 16, `I-05`). And be deliberate about `resource-policy: keep` — it is correct for a database
PVC and wrong for a generated ConfigMap, and the default of not using it is right more often than
people assume.

### U-15 · Two upgrades of the same release at once

**What you see.** A release in `pending-upgrade`; or objects flapping between two versions; or a
revision whose manifest is a mixture.

**Mechanism.** Helm has no locking beyond the `pending-*` flag, and that flag is written at the
*start* of the operation — so two processes starting within the same moment can both read
`deployed`, both write `pending-upgrade`, and both proceed. The second one to finish wins, and the
intermediate state is undefined.

How it happens in practice: a merge to `main` triggering a deploy while a manual `helm upgrade` is
in flight; two CI pipelines for two commits; a GitOps controller reconciling while an engineer
deploys by hand.

**Confirm it.**

```bash
helm history checkout-api -n checkout --max 10 \
  -o json | jq -r '.[] | "\(.revision)\t\(.updated)\t\(.status)\t\(.description)"'
```

Two revisions seconds apart, from two different pipeline URLs (which you have because of
`--description`), is the signature.

**Prevent.** Serialise, at whichever layer you have:

- **CI concurrency groups**, keyed on release and namespace, with "cancel in progress" *off* —
  cancelling a running deploy is how you get `U-09`:
  ```yaml
  # GitHub Actions
  concurrency:
    group: deploy-${{ env.RELEASE }}-${{ env.CLUSTER }}-${{ env.NAMESPACE }}
    cancel-in-progress: false
  ```
- **One deployer per release.** If a GitOps controller owns a release, humans do not run
  `helm upgrade` against it. Doc 11 argues this should be a hard rule with RBAC behind it, not a
  convention.

## What to take away

1. **Always `helm upgrade --install`.** It is idempotent, identical across environments, and makes
   `helm install` unnecessary in automation.
2. **`--wait` waits only for the kinds it has code for.** Custom resources are "ready" the instant
   they are stored, so a CR-only release succeeds instantly and verifies nothing. Use a GitOps
   controller's health checks, a blocking hook, or a post-deploy assertion — but do not read
   `--wait` as "verified".
3. **`--wait-for-jobs` is needed for ordinary Jobs in your manifest**, not just hooks. Without it,
   a release whose purpose is to run a job reports success before the job starts.
4. **`--atomic` is the right default and has three costs**: the rollback can also fail, a flaky
   `post-upgrade` hook rolls back a working deploy, and it cannot undo side effects — so with
   forward-only migrations it produces old code against a new schema.
5. **`--timeout` is a total budget.** Derive it from probe delays times rollout batches, not from
   the 5-minute default, and set the CI job timeout to at least twice it plus a margin.
6. **`--force` means replace, which for a Deployment means an outage.** Remove it from your
   runbooks; `kubectl delete --cascade=orphan` followed by a normal upgrade does the same job with
   no downtime.
7. **Rollback restores Kubernetes objects and five things it does not**: your database, CRDs, hook
   side effects, `resource-policy: keep` objects, and images that have been garbage-collected.
   Immutable tags plus a registry retention window longer than `--history-max` removes the last one
   entirely.
8. **`pending-upgrade` is a flag with no owner and no expiry**, so Helm cannot tell a live
   operation from a dead one and has to refuse. Never `Ctrl-C` an upgrade; let it time out, because
   a timeout is recoverable and an interrupt is not.
9. **Immutable-field failures are prevented by selector discipline and caught by an upgrade test.**
   Install the old chart version, then upgrade to the new one, in a throwaway cluster — the single
   highest-value test in doc 14, and almost nobody runs it.
10. **A Kubernetes upgrade invalidates older rollback targets**, because stored manifests reference
    API versions the cluster may have removed. Deploy every release forward shortly after a
    control-plane upgrade so rollback stays a valid answer.
11. **Serialise deploys per release.** Helm's pending flag is not a lock; a CI concurrency group is,
    and "cancel in progress" must be off.
