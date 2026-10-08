# Case Studies

Nine incidents, each walked the same way: **what happened**, **mechanism**, **the signal that would
have caught it**, **recovery**, **what changed afterwards**. They span all six running systems from
the README.

Two of them — `CS-3` and `CS-8` — are cases where Helm behaved exactly as documented and the
documentation was the problem. One — `CS-4` — is an outage caused by the rollback rather than the
deploy. Those three are the most instructive, because they are the ones you cannot fix by being
more careful.

| | System | Duration | Root class | Doc |
|---|---|---|---|---|
| `CS-1` | Riverbend | 41 min | `T-11` / `G-03` — `lookup` under a render-only controller | 02, 11 |
| `CS-2` | Riverbend | 2 h 10 min | `T-06` — name truncation collision | 02 |
| `CS-3` | Lantern | 6 h 40 min | `R-05` + `C-01` — 1 MiB Secret, CRDs already replaced | 04, 07 |
| `CS-4` | Gateline | 11 min of a sale window | `U-03` — `--atomic` rollback under a deadline | 05 |
| `CS-5` | Northlight | 4 h 50 min, staged | `D-11` — library chart blast radius | 08 |
| `CS-6` | Corridor | 9 days (silent) | `G-04` — hook not mapped under render-only | 11 |
| `CS-7` | Riverbend | no outage; $14,880/mo | `U-14` — `resource-policy: keep` orphans | 05 |
| `CS-8` | Waypoint | 4 h of data re-derivation | `H-08` — `pre-rollback` down-migration | 06 |
| `CS-9` | Lantern | 3 weeks degraded | `S-01` + `R-11` — release-state growth, then a cleanup sweep | 15, 04 |

---

## CS-1 · The password that rotated itself

**System:** Riverbend. **Duration:** 41 minutes of `checkout-api` returning 500s at up to 3,400
requests per second.

### What happened

Riverbend migrated `checkout-api` from CI-driven `helm upgrade` to Argo CD. The migration was
tested in `rb-dev`, looked clean, and was promoted. Nineteen minutes after the production
`Application` was created with `automated: {selfHeal: true}`, `checkout-api` began failing every
request with a database authentication error.

The timeline:

| Time | Event |
|---|---|
| 14:02 | `Application` created in `rb-prod-use1`, first sync succeeds |
| 14:05 | Second reconcile. Secret `checkout-api-db` rewritten with a new random password |
| 14:05 | Existing pods still hold the old password in memory — **no errors yet** |
| 14:19 | A routine HPA scale-up starts three new pods. They read the new password. The database rejects it |
| 14:21 | Error rate crosses 8%; on-call paged |
| 14:26 | Argo CD reconciles again. Secret rewritten *again*, with a third password |
| 14:33 | Root cause identified from Argo's diff showing a permanently-changing Secret |
| 14:35 | `argocd app set --sync-policy none`. The rewriting stops |
| 14:41 | Password reset in PostgreSQL to match the current Secret; pods restarted |
| 14:43 | Recovered |

### Mechanism

The chart used the standard generate-a-password-once pattern:

```yaml
{{- $existing := lookup "v1" "Secret" .Release.Namespace "checkout-api-db" }}
data:
  {{- if $existing }}
  password: {{ index $existing.data "password" }}
  {{- else }}
  password: {{ randAlphaNum 32 | b64enc | quote }}
  {{- end }}
```

Under `helm upgrade` this works: `lookup` reads the live Secret and reuses its value.

Under Argo CD's render-only model (doc 11, model 1), the render is `helm template`, which has **no
cluster connection**. `lookup` returns an empty map every time, the `else` branch runs, and
`randAlphaNum` produces a new password. Argo CD then applies it as a change, because it genuinely
differs from what is live.

⚠️ **The reconcile interval turned a deploy-time bug into a continuous one.** At a 3-minute
interval, the Secret was being rewritten twenty times an hour. The 14-minute gap between the first
rewrite and the first error is the other half of the story: existing pods held their connection
pools, so nothing broke until a scale-up created a pod that had to authenticate fresh. A bug that
had been active for fourteen minutes presented as a sudden failure with no obvious trigger.

### The signal that would have caught it

The determinism check, which takes two seconds:

```bash
a=$(helm template checkout-api ./charts/checkout-api | grep -A3 'name: checkout-api-db' | sha256sum)
b=$(helm template checkout-api ./charts/checkout-api | grep -A3 'name: checkout-api-db' | sha256sum)
[ "$a" = "$b" ] || echo "NON-DETERMINISTIC — rewrites on every render"
```

And `argocd app diff` before enabling auto-sync would have shown a Secret that differed on every
invocation — which is a signature, not a normal diff.

**Why the dev test did not catch it.** `rb-dev` had been created two weeks earlier by the same Argo
CD migration, so its Secret had *never* been created by a real `helm upgrade`. The password was
being rewritten there too, constantly, and nothing noticed because dev's single pod was never
restarted during the test window.

### Recovery

Disable auto-sync, read the current Secret, reset the database password to match it, restart the
pods. ⚠️ Note the direction: they changed the *database* to match the *Secret*, not the reverse,
because the Secret was being rewritten and any value they wrote into it would be replaced within
three minutes.

### What changed

1. **The chart moved to the External Secrets Operator** (doc 12). The Secret is now materialised by
   a controller from AWS Secrets Manager; the chart contains a path.
2. **A CI check banning non-deterministic template functions**, fleet-wide:
   ```bash
   grep -rnE 'randAlpha|randNumeric|randAscii|genCA|genSelfSignedCert|genSignedCert|uuidv4' \
     charts/*/templates/ && exit 1
   ```
   It found the same pattern in four other charts, none of which had been migrated to Argo CD yet.
3. **The migration checklist gained a step:** render the chart twice and compare, before creating
   any `Application`.
4. **Dev environments are created by the same mechanism as production.** A dev environment that
   differs in *how it deploys* cannot test a deployment change — which is doc 10's `E-04` in a form
   nobody had considered.

---

## CS-2 · Two Deployments, one selector

**System:** Riverbend. **Duration:** 2 hours 10 minutes of a per-PR environment behaving
impossibly, and a near-miss in production.

### What happened

A developer opened two pull requests against `checkout-api` on the same day, testing a
blue/green configuration. The ephemeral-environment system created two releases in one namespace:

```
pr-4471-riverbend-checkout-api-integration-canary-green
pr-4471-riverbend-checkout-api-integration-canary-blue
```

Both environments came up. Then the pods started behaving as though two different images were
serving one endpoint, scaling events happened that nobody triggered, and `kubectl get pods` showed
a set that did not match either Deployment's replica count.

### Mechanism

The chart's fullname helper was the standard one:

```yaml
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
```

Both release names, plus `-checkout-api`, exceed 63 characters, and **they share their first 63
characters** — the distinguishing `green`/`blue` is past position 63. So both truncated to:

```
pr-4471-riverbend-checkout-api-integration-canary-checkout-api
```

The second `helm install` hit `R-07` on some objects and — because the chart's `selectorLabels`
included the fullname — produced a Deployment whose `spec.selector` matched the *other*
Deployment's pods. Two Deployments, each adopting the other's ReplicaSets, each scaling in response
to a pod count the other controlled.

⚠️ **The near-miss.** The same helper was in `riverbend-common`, used by all 240 services. Nothing
in production had names long enough to collide — yet. A service named
`inventory-reconciliation-adjustment-processor` was 12 characters from it.

### The signal that would have caught it

A duplicate check on the render, which is three lines:

```bash
helm template "$REL" ./charts/checkout-api \
  | grep -E '^kind:|^  name:' | paste - - | sort | uniq -d
```

Any output is a collision. And a length check on release names at creation time.

### Recovery

Uninstall both releases, delete the namespace, and recreate the environments with shortened names.
Two hours went into working out what was happening, because the symptom — pods that belong to
neither Deployment — does not map onto any familiar failure.

### What changed

1. **The fullname helper hashes rather than truncates** when it would exceed 63 characters:
   ```yaml
   {{- define "riverbend-common.fullname" -}}
   {{- $n := printf "%s-%s" .Release.Name .Chart.Name -}}
   {{- if gt (len $n) 63 -}}
   {{- printf "%s-%s" (trunc 54 $n | trimSuffix "-") (sha256sum $n | trunc 8) -}}
   {{- else -}}{{- $n -}}{{- end -}}
   {{- end -}}
   ```
2. **Selector labels no longer include the fullname.** Only `app.kubernetes.io/name` and
   `app.kubernetes.io/instance`, per doc 02's `T-08` — so even a name collision cannot produce
   cross-adoption.
3. **Release names capped at 40 characters in CI**, not 53. The 13 characters of headroom absorb
   every suffix the templates add.
4. **The duplicate-name check runs on every chart in CI.**

⚠️ Change 2 was the expensive one: altering `spec.selector` on 240 live Deployments is
`U-07`, requiring `kubectl delete --cascade=orphan` on each. It was done over three weeks, tier 3
first, at 20 services a night.

---

## CS-3 · The release Secret that would not fit, halfway through

**System:** Lantern (10 TB/hour log platform). **Duration:** 6 hours 40 minutes with the monitoring
stack degraded — during which Lantern's own pipeline had an unrelated incident that took longer to
diagnose because the monitoring was down.

### What happened

Lantern upgraded `kube-prometheus-stack` from 61.x to 66.2.1 to pick up a fix. The upgrade was run
as:

```bash
helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --install -n observability --version 66.2.1 -f values.yaml --atomic --timeout 15m
```

It failed:

```
Error: UPGRADE FAILED: create: failed to create: Secret
"sh.helm.release.v1.kube-prometheus-stack.v18" is invalid: data: Too long:
must have at most 1048576 bytes
```

The team read that as "the upgrade did not happen" and retried twice. Then they discovered the
Prometheus Operator was crash-looping and every `ServiceMonitor` had stopped being reconciled.

### Mechanism

Two failures compounding, and the order is the whole story.

**First,** `R-05`. The chart's vendored CRDs had grown between versions. The release payload — chart
files base64'd, plus manifest, plus values, gzipped, base64'd again — came to roughly 1.3 MB, over
the 1 MiB Secret limit. The install of the *release record* failed.

**Second, and this is the part that caused the outage:** Helm had already applied the contents of
`crds/`. Doc 07 is explicit that `crds/` is applied **before** the templates and outside the release
manifest — so by the time Helm tried to write the release Secret, the new CRD schemas were in the
cluster and the old controller was still running.

⚠️ So the cluster was in a state Helm has no concept of: **new CRDs, old controller, no release
record of either.** `helm history` showed revision 17 as `deployed`, because revision 18 never got
written. Every signal said the upgrade had not happened. The cluster disagreed.

The operator crash-looped because the new CRD schemas had a required field its version did not set.

### The signal that would have caught it

A chart-size check before the upgrade, which is two commands:

```bash
helm pull prometheus-community/kube-prometheus-stack --version 66.2.1 --untar -d /tmp/kps
du -sh /tmp/kps/kube-prometheus-stack
```

Anything over about 2 MB of chart is worth checking; over 4 MB will fail. And in CI:

```bash
sz=$(helm package ./chart -d /tmp | grep -o '/tmp/.*tgz' | xargs stat -f%z)
[ "$sz" -lt 1500000 ] || { echo "chart exceeds 1.5MB — see R-05"; exit 1; }
```

### Recovery

1. Reapply the **old** CRDs to unblock the operator:
   `kubectl apply --server-side --force-conflicts -f <v0.76 CRD bundle>`
2. Confirm the operator recovered and `ServiceMonitor` reconciliation resumed.
3. Redo the upgrade properly: apply the new CRDs deliberately, then
   `helm upgrade ... --skip-crds`.

⚠️ Step 1 is a CRD **downgrade** with resources present. It worked because the new fields were
additive and nothing had used them yet — a 40-minute window. Had anything written a new field, that
data would have been pruned (doc 07, `C-01`). The team did not know this at the time.

### What changed

1. **CRDs left Helm entirely** — doc 07's strategy 4. A separate pipeline applies them with
   `kubectl apply --server-side`, with its own review. The argument that settled it was not
   technical: a CRD schema change affects every team in the cluster, so it should not arrive as a
   side effect of one team's chart bump.
2. **`--skip-crds` on every third-party chart**, with the CRD apply in the same commit — because
   `--skip-crds` alone is half a change (`C-04`).
3. **A chart-size gate in CI.**
4. **The runbook gained a line:** *after any failed Helm operation on a chart with CRDs, check the
   installed CRD versions.* `helm history` cannot tell you, because `crds/` is not in the release.
5. **Monitoring for the monitoring.** Lantern now has a minimal external check — one Prometheus
   scraping a handful of critical targets, deployed separately — precisely so that a monitoring-stack
   incident is not invisible.

---

## CS-4 · The rollback that missed the doors opening

**System:** Gateline (ticketing; 5,000 QPS baseline → 500,000 QPS in 10 seconds, on a published
schedule). **Duration:** 11 minutes of an 18-minute sale window with degraded capacity.

### What happened

Gateline's routine is to pre-scale 40 minutes before a sale and freeze deploys 30 minutes before.
For a high-demand event, a configuration change was needed inside the freeze window — a rate-limit
value — and was approved as an exception.

| Time before doors | Event |
|---|---|
| T−40 min | Pre-scale: `queue-api` from 12 to 340 replicas. Cluster autoscaler adds 94 nodes |
| T−26 min | Approved exception: `helm upgrade --atomic --timeout 5m` with the new rate limit |
| T−26 min | Helm applies. Rolling update of 340 replicas begins, `maxSurge: 25%` = 85 surge pods |
| T−24 min | 61 surge pods `Pending` — `Insufficient cpu`. Autoscaler adding nodes, slowly |
| T−21 min | `--timeout 5m` expires. `--atomic` triggers rollback |
| T−21 min | **Rollback needs to schedule the OLD pod spec** — same resource requests, same shortage |
| T−16 min | Rollback times out. Release status `pending-rollback` |
| T−16 min | 208 of 340 replicas ready, mixed between two versions |
| T−11 min | On-call disables the HPA and scales the known-good ReplicaSet directly |
| T−4 min | 340 replicas ready, single version |
| T+0 | Doors open. Sale completes with an estimated 3.4% of requests shed in the first 90 seconds |

### Mechanism

Doc 05, `U-03`. Three things compounded:

**`--atomic` rolls back on any failure**, including a `--wait` timeout caused by capacity rather than
by anything wrong with the change.

**The rollback has the same problem as the upgrade.** It needed to schedule pods with identical
resource requests into a cluster that could not schedule pods. `--atomic` assumes the rollback is
more likely to succeed than the upgrade, which is true when the upgrade is bad and false when the
*cluster* is the problem.

**The timeout was inherited, not derived.** 5 minutes was the value on a 12-replica service. At 340
replicas with `maxSurge: 25%`, the rollout is 4 batches of 85 pods; each batch needs nodes that take
90–180 seconds to join. The arithmetic says 8–12 minutes minimum. Nobody redid it when the pre-scale
went from 12 to 340.

⚠️ **And the deeper problem: the pre-scale had already consumed the surge headroom.** Doc 15's `S-06`
calculation — spare capacity ÷ (replicas × `maxSurge`) — was negative. The cluster had exactly
enough capacity for 340 replicas and none for 85 more.

### The signal that would have caught it

The timeout arithmetic, done once:

```
batches       = ceil(1 / maxSurge) = 4
per batch     = node provisioning (150 s) + pod ready (40 s) = 190 s
minimum       = 4 × 190 = 760 s = 12 m 40 s
--timeout 5m  → guaranteed failure
```

And a pre-flight check on surge headroom:

```bash
REPL=$(kubectl -n gateline get deploy queue-api -o jsonpath='{.spec.replicas}')
SURGE=$(kubectl -n gateline get deploy queue-api \
  -o jsonpath='{.spec.strategy.rollingUpdate.maxSurge}')
echo "this deploy needs room for $((REPL * ${SURGE%\%} / 100)) surge pods"
kubectl get nodes -o json | jq -r '[.items[] | .status.allocatable.cpu] | length as $n
  | "nodes=\($n)"'
kubectl describe nodes | grep -A3 'Allocated resources' | grep cpu | head -5
```

### Recovery

Disable the HPA, scale the known-good ReplicaSet directly, let the autoscaler catch up. This takes
the workload out of the Deployment's control and is the right call under a deadline — and it was
written down, because Helm's next upgrade reconciles it away.

### What changed

1. **`--atomic` removed from deploys inside a pre-sale window.** The reasoning: within the window, a
   half-applied upgrade that a human is watching is better than an automatic rollback that can fail
   in the same way. Outside the window, `--atomic` stays on.
2. **`--timeout` is computed, not configured.** The deploy wrapper derives it:
   ```bash
   REPL=$(kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.spec.replicas}')
   SURGE_PCT=${SURGE:-25}
   BATCHES=$(( (100 + SURGE_PCT - 1) / SURGE_PCT ))
   TIMEOUT=$(( BATCHES * 190 + 120 ))
   helm upgrade ... --timeout "${TIMEOUT}s"
   ```
3. **`maxSurge: 0` with `maxUnavailable: 10%` during pre-scale windows.** A rollout that needs no
   extra capacity cannot be blocked by the absence of capacity — at the cost of briefly running
   below the target replica count, which is the correct trade when headroom is zero.
4. **The freeze is now enforced**, not approved-around. An admission policy rejects writes to
   `gateline` namespaces during a window, and the exception process requires the two changes above.

---

## CS-5 · The library chart bump

**System:** Northlight (service mesh platform; 2,500 services, 100,000 sidecars).
**Duration:** 4 hours 50 minutes, affecting services progressively as they deployed.

### What happened

Northlight's library chart `northlight-workload` v7.4.0 added a `topologySpreadConstraints` block
with `whenUnsatisfiable: DoNotSchedule` for tier-1 and tier-2 services. It was tested on three
charts, rendered correctly, and released.

Over the following five hours, 61 services deployed and picked it up. 14 of them ended up with pods
stuck `Pending`.

### Mechanism

Doc 08, `D-11`. The library change was correct for the shape it was tested against and wrong for a
shape nobody tested.

The new constraint spread pods across `topology.kubernetes.io/zone` with `maxSkew: 1` and
`DoNotSchedule`. For a service with 24 replicas across 3 zones, that is 8 per zone and satisfiable.
For a service with **2 replicas** — which 14 tier-2 services had, being low-traffic but
high-criticality — `maxSkew: 1` across 3 zones with `DoNotSchedule` means the scheduler will not
place the second pod in a zone that already has one, and Northlight had a zone with no capacity for
that service's node selector. Pod 2 stayed `Pending` indefinitely.

⚠️ **The blast radius was time-distributed, which made it hard to recognise.** Each affected team
saw one service with one `Pending` pod, concluded it was a capacity problem in their own service,
and filed separate tickets. It was 3 hours 20 minutes before somebody noticed 14 tickets sharing a
`northlight-workload` version.

### The signal that would have caught it

Golden-file renders across tiers *and replica counts*. Northlight's tests rendered three charts, all
of which had 12 or more replicas. A fixture with 2 replicas would have produced a visibly different
constraint block and prompted the question.

And the fleet query from doc 16's `I-02`, which would have surfaced the shared version immediately:

```promql
count by (dep_version) (helm_release_dependency_info{dependency="northlight-workload"})
```

### Recovery

Release v7.4.1 changing `whenUnsatisfiable` to `ScheduleAnyway` for any service with fewer than 6
replicas, computed in the library:

```yaml
whenUnsatisfiable: {{ if lt (int .Values.replicaCount) 6 }}ScheduleAnyway{{ else }}{{ $tier.topologySpreadWhenUnsatisfiable }}{{ end }}
```

Then redeploy the 61 services that had picked up v7.4.0.

### What changed

1. **Golden-file fixtures cover the *distribution* of consumers, not representative charts.** The
   fixture set is now: tier 1 at 24 replicas, tier 1 at 3, tier 2 at 12, tier 2 at 2, tier 3 at 1.
   The two low-replica fixtures exist because of this incident.
2. **A library release is canaried through three tier-3 services with a 24-hour soak** before any
   service chart bumps to it.
3. **The library version is a metric.** Doc 16's exporter, with a dashboard panel showing the
   version spread — so "14 tickets share a version" is a glance rather than a three-hour deduction.
4. **Library releases are announced with the rendered diff attached**, for one fixture per tier, so
   consuming teams see the effect rather than the changelog.

---

## CS-6 · The migration that never ran

**System:** Corridor (professional network; 1B members, versioned derived stores).
**Duration:** 9 days, silent.

### What happened

Corridor's `profile-index` service had a `pre-upgrade` hook running a schema migration against its
derived store. The service was migrated from Flux `HelmRelease` to Argo CD as part of a platform
consolidation.

Nine days later, a query started returning results missing a field that had been added in a release
eight days earlier. Investigation found the derived store's schema had never been migrated: the
column existed in the code's expectations and not in the store.

Six releases had shipped in those nine days. None of their migrations had run.

### Mechanism

Doc 11, `G-04`. Under Flux's `HelmRelease` (model 2), Helm's full lifecycle runs and `pre-upgrade`
hooks execute.

Under Argo CD's render-only model, Helm hooks are *mapped* to Argo CD resource hooks. The mapping
covers `pre-install`/`pre-upgrade` → `PreSync`, which should have worked — and did not, because the
chart's hook annotation was:

```yaml
    "helm.sh/hook": pre-upgrade
    "helm.sh/hook-delete-policy": hook-succeeded
```

with **no `before-hook-creation`**. Argo CD translated the delete policy to `HookSucceeded`, deleted
the Job after the first successful run, and on the next sync created it again — but the Job name was
fixed and the *previous* Job had been deleted, so creation succeeded. That part worked.

What did not work: the migration container's image tag came from `.Values.image.tag`, and the
values were supplied through a multi-source `Application` where the image tag was set by a CI-written
ConfigMap that the migration to Argo CD had not wired up. So the Job ran the **old** image, whose
`migrate up` was a no-op because that image's migration set was already applied.

⚠️ **The hook ran, succeeded, and did nothing.** Every signal was green: the Job completed, Argo CD
reported `Synced` and `Healthy`, the sync history showed six successful syncs. The failure was that
the hook did the wrong work successfully.

### The signal that would have caught it

**A migration that asserts its own postcondition.** Corridor's migration tool printed
`migrations applied: 0` and exited 0, which is correct behaviour for an up-to-date schema and
indistinguishable from this failure.

The check that would have caught it — and which Corridor added — is a version assertion:

```bash
# In the migration container, after `migrate up`
EXPECTED=$(cat /app/EXPECTED_SCHEMA_VERSION)     # baked into the image at build time
ACTUAL=$(migrate version)
[ "$ACTUAL" = "$EXPECTED" ] || {
  echo "FAIL: schema is at $ACTUAL, this build requires $EXPECTED"; exit 1; }
```

⚠️ That five-line check is the generalisable lesson: **a migration step that can succeed by doing
nothing must assert that it did the right thing.** Same principle as doc 10's `E-12` and doc 05's
`U-04` — when a mechanism can succeed vacuously, assert the outcome, not the exit code.

### Recovery

Run the migration manually with the correct image, then verify the derived store. The nine days of
missing field values had to be backfilled, which took 31 hours of batch processing.

### What changed

1. **The migration asserts its expected schema version**, baked into the image at build time.
2. **The Argo CD migration checklist** includes running the hook-mapping script (doc 11) for every
   chart and verifying that every value the hooks depend on is wired through.
3. **`before-hook-creation` added to every hook**, with a lint rule.
4. **A schema-version metric**, exported by the service, alerted when it does not match the running
   image's expectation. This is the check that makes the class of failure — code and schema
   disagreeing — visible regardless of *why* they disagree.

---

## CS-7 · Fourteen thousand eight hundred and eighty dollars a month

**System:** Riverbend. **Duration:** no outage. Discovered in a cost review, 14 months after it
started.

### What happened

A finance review flagged that `rb-dev`'s EBS spend had grown 6× in a year while its node count grew
1.4×. The cluster had **1,240 PersistentVolumes with no pod attached**, totalling 62 TB of gp3, at
$0.08/GB/month = **$4,960/month**. Across dev, staging and the two production clusters the total was
**$14,880/month**.

### Mechanism

Doc 05, `U-14`. Two categories, roughly equal:

**`helm.sh/resource-policy: keep` on PVCs.** A chart used by 41 stateful services annotated its PVC
template so that an accidental `helm uninstall` would not destroy data. Reasonable. The consequence:
every ephemeral environment that installed that chart and was then uninstalled left its PVC behind,
with no owner, forever. At roughly 90 ephemeral environments a week and a 50 GB default volume, that
accumulated fast.

**StatefulSet `volumeClaimTemplates`.** PVCs created by the StatefulSet controller are not in Helm's
manifest — Helm never knew about them — so `helm uninstall` could not delete them even if it wanted
to.

⚠️ **In both cases, the ephemeral-environment teardown deleted the release and not the namespace.**
Deleting the namespace would have collected everything. The teardown script had been written before
the stateful charts existed and had never been revisited.

### The signal that would have caught it

The orphan audit from doc 16's `I-05`, or simply:

```bash
kubectl get pvc -A -o json | jq -r '.items[]
  | select(.status.phase=="Bound")
  | "\(.metadata.namespace)\t\(.metadata.name)\t\(.spec.resources.requests.storage)"' \
  > /tmp/pvcs
kubectl get pods -A -o json | jq -r '.items[] | .metadata.namespace as $ns
  | (.spec.volumes // [])[] | select(.persistentVolumeClaim) | "\($ns)\t\(.persistentVolumeClaim.claimName)"' \
  | sort -u > /tmp/used
awk -F'\t' 'NR==FNR{u[$1"\t"$2]=1; next} !($1"\t"$2 in u) {print "UNATTACHED: " $0}' /tmp/used /tmp/pvcs
```

Eleven lines, and it is the check nobody runs because nothing pages for it.

### Recovery

Delete them, in stages, with a hold. The team snapshotted everything first (⚠️ snapshots are cheaper
than volumes but not free — the snapshot bill was $1,100 for one month), kept the snapshots 30 days,
then deleted them.

### What changed

1. **Ephemeral teardown deletes the namespace**, always, as the final step — which handles
   `resource-policy: keep`, `volumeClaimTemplates`, and hook resources together.
2. **`resource-policy: keep` requires a justification comment** naming what data it protects, and is
   not used in non-production values. A lint rule enforces the comment.
3. **`persistentVolumeClaimRetentionPolicy` on every StatefulSet**, which is the actual Kubernetes
   fix for the second category:
   ```yaml
   spec:
     persistentVolumeClaimRetentionPolicy:
       whenDeleted: Delete      # in dev; Retain in production
       whenScaled: Retain
   ```
4. **A weekly unattached-PVC report**, with age and cost, to the owning team. Not an alert — a
   report, because the appropriate response is a decision rather than an interrupt.

---

## CS-8 · The rollback that dropped a column

**System:** Waypoint (ride-hailing; 750,000 GPS writes/s alongside a money ledger).
**Duration:** no user-visible outage. 4 hours of trip-record re-derivation, and a data-integrity
review.

### What happened

Waypoint deployed `trip-ledger` revision 61, adding a `settlement_currency` column with a backfill.
The chart had symmetrical migration hooks:

```yaml
# pre-upgrade hook
command: ["/app/trip-ledger", "migrate", "up"]
---
# pre-rollback hook
command: ["/app/trip-ledger", "migrate", "down", "1"]
```

| Time | Event |
|---|---|
| 02:14 | `helm upgrade --atomic --timeout 20m`. `pre-upgrade` migration adds the column, backfills 4.1M rows in 6 m 40 s |
| 02:21 | New pods start. Readiness probes fail — a config key was absent in one region's values |
| 02:28 | `--wait` times out. `--atomic` triggers rollback |
| 02:28 | **`pre-rollback` hook runs `migrate down 1`, dropping `settlement_currency`** |
| 02:31 | Rollback completes. Old code runs fine. Release status `deployed` |
| 02:31 | Everything green. Nobody paged |
| 09:40 | A settlement reconciliation job reports 4.1M trips with no currency |

### Mechanism

Doc 06, `H-08`. The design was symmetrical and looked correct: up on upgrade, down on rollback.

The flaw is that **`--atomic` triggers a rollback automatically, from a failure, with no human
deciding.** So a destructive operation — dropping a column — was attached to an event that fires
when something goes wrong at two in the morning. The rollback did exactly what it was told.

⚠️ **And it succeeded**, which is why nobody was paged. `helm history` showed a clean rollback. The
release was `deployed`. The outage was in the data, seven hours later, discovered by a batch job.

The 4.1M rows were recoverable — the source events were in Waypoint's event stream — but
re-deriving them took 4 hours and required a data-integrity review before the settlement run could
proceed.

### The signal that would have caught it

A grep, before the chart ever shipped:

```bash
grep -rln 'pre-rollback' charts/*/templates/ | while read -r f; do
  grep -nE 'migrate|down|revert|drop' "$f" && echo "  ^^ destructive pre-rollback in $f"
done
```

### Recovery

Re-run `migrate up`, then re-derive the column from the event stream. The config-key bug that caused
the original failure was a one-line values fix.

### What changed

1. **`pre-rollback` and `post-rollback` hooks that modify data are banned**, with the CI grep above.
   `post-rollback` for a notification is allowed and is the only rollback hook Waypoint now uses.
2. **All migrations are expand/contract** (doc 06). Because the old code runs against the new
   schema, rolling back the code is sufficient and there is no reason for a down-migration to exist
   in the deploy path at all.
3. **Down-migrations are a separate, human-invoked tool**, not a hook — run deliberately, after
   somebody has thought about it.
4. **A data-integrity check after every rollback of a release with migrations**, added to the
   runbook. The general lesson: **a rollback that Helm reports as successful can still have broken
   something Helm does not know about**, and the check has to be explicit.

---

## CS-9 · Fourteen thousand release Secrets, then a cleanup job

**System:** Lantern. **Duration:** 3 weeks of degraded tooling, then 90 minutes of no rollback
capability across the platform.

### What happened

Two connected problems, three weeks apart.

**Weeks 1–3.** Lantern's platform team noticed `helm list -A` taking over a minute, then failing
with a client timeout. `etcd` had grown to 2.1 GB. The cause: Lantern runs 40 tenants, each with its
own set of releases — 1,410 releases total — and the agent DaemonSet chart alone had 340 revisions
because a per-tenant configuration change redeployed it. `--history-max` was at its default of 10.

```
14,100 revisions × 28 KB = 395 MB of release Secrets
```

⚠️ But `etcd` was 2.1 GB, not 395 MB, and the gap is instructive: `etcd`'s on-disk size includes
MVCC history not yet compacted plus boltdb fragmentation. A `defrag` after compaction took it to
780 MB. The *logical* data was 395 MB of Helm plus ~380 MB of everything else.

**Week 4.** An engineer wrote a cleanup job to reduce the Secret count, targeting Secrets not
referenced by any pod:

```bash
# The job that caused the second incident
kubectl get secrets -A -o json | jq -r '.items[] | select(.metadata.ownerReferences == null)
  | "\(.metadata.namespace) \(.metadata.name)"' | while read -r ns n; do
    kubectl delete secret -n "$ns" "$n"     # ⚠️ release Secrets have no ownerReferences
  done
```

Release Secrets have no `ownerReferences` and are referenced by no pod. The job deleted **11,200 of
them** before someone stopped it. Every release in the platform lost its history; 340 of them lost
their current revision record entirely and became invisible to Helm.

### Mechanism

Doc 15's `S-01` and `S-02` for the first part; doc 04's `R-11` for the second.

The second is the interesting one, and the mechanism is a category error: **the engineer reasoned
about Secrets as "credentials consumed by pods", and release Secrets are neither.** Every heuristic
for "is this Secret in use" — mounted by a pod, referenced by a ServiceAccount, owned by a
controller — returns false for a release Secret that is essential.

### The signal that would have caught it

For the growth: doc 16's `HelmReleaseStateGrowth` alert, or simply looking.

For the deletion: doc 16's alert 3, which exists because of this incident:

```promql
(
  sum by (cluster) (helm_release_revision_count)
  - sum by (cluster) (helm_release_revision_count offset 1h)
) < -0.10 * sum by (cluster) (helm_release_revision_count offset 1h)
```

At a 5-minute scrape interval that fires roughly 5 minutes into the deletion, not 90.

### Recovery

**For the 340 releases whose current record was deleted:** restore the Secrets from the previous
night's Velero backup, which had them because Velero includes Secrets by default. 22 releases were
created after the backup and had to be re-adopted with doc 17's `O-05` procedure — about 25 minutes
each, done in parallel by four engineers.

**For the rest:** history was lost and nothing was broken. Rollback depth went to zero until the
next deploy.

### What changed

1. **`--history-max 5`** platform-wide, and a CI check that every `helm upgrade` invocation includes
   it — because the flag is not persisted (doc 04, `R-04`), so one invocation without it undoes the
   pruning for that release.
2. **The agent DaemonSet's per-tenant configuration moved out of the release.** It is now a
   ConfigMap reconciled by a small controller, so a tenant configuration change is not a Helm
   upgrade. That took the DaemonSet's revision count from 340 to 4 and was the single largest
   reduction.
3. **A documented exclusion in every cleanup sweep:**
   ```bash
   kubectl get secrets -A --field-selector 'type!=helm.sh/release.v1' ...
   ```
   with a comment pointing at this incident.
4. **The revision-count alert.**
5. **Release Secrets are an explicitly named item in the backup policy**, rather than incidentally
   included. The distinction matters: "we back up Secrets" is a property that a future backup-scope
   optimisation can silently remove.
6. **A platform rule:** any script that deletes Kubernetes objects in bulk must be reviewed by two
   people and must run with `--dry-run=client` first, with the output pasted into the change record.
   ⚠️ The dry run would have shown 11,200 Secrets named `sh.helm.release.v1.*`, which is
   unmistakable.

---

## What to take away

1. **`lookup` plus a reconciling controller is the most damaging combination in this collection.**
   `CS-1` was three passwords in twenty minutes, and the fourteen-minute delay before any error
   made a continuously-active bug look like a sudden failure.
2. **A dev environment that differs in *how it deploys* cannot test a deployment change.** That is
   the generalisation of `CS-1`'s dev test passing.
3. **Truncation that loses the distinguishing part is silent; a hash is not.** And selector labels
   must never contain a name that can change, because the recovery is deleting every workload
   (`CS-2`).
4. **A failed Helm operation does not mean nothing happened.** `crds/` is applied before the release
   record is written, so `CS-3` left new CRDs, an old controller, and no record of either — with
   `helm history` reporting the previous revision as current.
5. **`--atomic` assumes the rollback is more likely to succeed than the upgrade**, which is false
   when the cluster is the problem. `CS-4`'s rollback failed the same way the upgrade did, and the
   inherited 5-minute timeout was arithmetically impossible at 340 replicas.
6. **Test the distribution of your consumers, not a representative sample.** `CS-5`'s library change
   was correct at 12 replicas and wrong at 2, and the blast radius was time-distributed so it
   presented as 14 unrelated tickets.
7. **A step that can succeed by doing nothing must assert that it did the right thing.** `CS-6`'s
   migration ran, exited 0, and applied nothing, for nine days, with every signal green. A
   five-line schema-version assertion would have failed loudly on the first sync.
8. **Never attach a destructive operation to `pre-rollback`.** A rollback fires automatically from a
   failure with no human deciding, and `CS-8`'s down-migration dropped a freshly-backfilled column
   at 02:28 while reporting success.
9. **Release Secrets defeat every heuristic for "is this Secret in use".** No owner references, no
   pod mounts, no ServiceAccount reference — and essential. `CS-9`'s cleanup job was reasoning about
   the wrong kind of object.
10. **Two of these nine had no outage and were found by a cost review and a batch job.** The
    expensive failures are not always the loud ones, and an audit nobody runs is how they stay
    invisible for fourteen months.
