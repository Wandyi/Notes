# How Helm Breaks at Scale

Helm does not fail at scale. It gets slow, and it consumes resources nobody attributed to it, and
the human ability to reason about the fleet degrades faster than any of the technical limits. That
is the characteristic shape of the `S-` class: **everything still works and everything is worse.**

This doc compares Riverbend at two points — 40 releases, when Helm was introduced, and 940 releases
today — and derives each breakpoint rather than asserting it. Every number here descends from one
number, the 28 KB per release revision derived in doc 04, and every derivation names its
assumptions so you can substitute your own.

The thesis:

> **The binding constraint at Helm scale is not `etcd`, latency, or CPU. It is that nobody can
> answer a basic question about the fleet.** "Which releases are on library chart v4.1?" "Which
> production releases have not been deployed in 90 days?" "Which releases would fail to roll back
> because their image is gone?" At 40 releases you read the list. At 940 you need an inventory, and
> if you do not build one, the answer to every one of those questions becomes "nobody knows", which
> is how a fleet becomes unmaintainable while every individual release is healthy.

## Riverbend, then and now

| | Year 1 | Year 4 | Factor |
|---|---|---|---|
| Clusters | 1 (`rb-prod-use1`, 40 nodes) | 4 (60 / 80 / 380 / 140 nodes) | 4× |
| Services | 22 | 240 | 11× |
| Namespaces | 6 | 38 | 6× |
| Service releases | 22 | 816 | 37× |
| Platform releases | 18 | 124 | 7× |
| **Total releases** | **40** | **940** | **23×** |
| Release revisions retained | 310 (`--history-max` 10) | 7,240 | 23× |
| Upgrades per weekday | 14 | 355 (45 prod, 310 dev) | 25× |
| Chart count | 22 application + 0 library | 240 application + 1 library + 31 third-party | — |
| Rendered objects per cluster | ~260 | ~3,900 | 15× |

The architecture did not change. Every number in this doc is about which of them stopped being
comfortable.

## S-01 · Release history dominates `etcd`

**What you see.** `etcd` database size growing faster than your workload count. A growth rate
nobody can explain from the number of pods.

**Mechanism and derivation.** From doc 04: one release revision is **28 KB** in `etcd`. So:

```
7,240 revisions × 28 KB = 202,720 KB = 203 MB
```

Now put that in context by enumerating what *else* is in `etcd` at Riverbend's production cluster.
This is the part worth doing, because the ratio is the surprise:

| Object class | Count | Bytes each | Total |
|---|---|---|---|
| Pods (with status) | 3,100 | ~6 KB | 18.6 MB |
| ReplicaSets (`revisionHistoryLimit: 3`) | ~1,000 | ~3 KB | 3.0 MB |
| **Helm release Secrets** | **7,240** | **28 KB** | **203 MB** |
| Other Secrets — TLS from cert-manager | 340 | ~8 KB | 2.7 MB |
| Other Secrets — materialised by ESO | 1,100 | ~2 KB | 2.2 MB |
| ConfigMaps | 890 | ~4 KB | 3.6 MB |
| Services, Endpoints, EndpointSlices | ~1,800 | ~2 KB | 3.6 MB |
| Nodes, CRs, RBAC, everything else | — | — | ~12 MB |
| **Total** | | | **~249 MB** |

**Helm release history is 82% of the logical contents of `etcd`, and 96% of all Secret bytes.**

⚠️ Two caveats that keep this honest. First, **`etcd`'s on-disk size is larger than its logical
contents**, because MVCC keeps superseded revisions until compaction (every 5 minutes by default)
and the boltdb file does not shrink without a defragmentation — so a 249 MB logical dataset commonly
occupies 600–900 MB on disk. Check both:

```bash
# On a self-managed control plane
etcdctl endpoint status -w table
# Or from metrics, which works on managed control planes that expose them
# etcd_mvcc_db_total_size_in_bytes        — allocated on disk
# etcd_mvcc_db_total_size_in_use_in_bytes — logical, after compaction
```

Second, 249 MB against `etcd`'s practical 8 GB ceiling is 3%. **So this is not, at Riverbend, an
availability problem.** It matters for three other reasons:

1. **The growth rate without pruning.** 355 upgrades a weekday × 28 KB = **9.9 MB/day**, or
   **2.6 GB/year** with `--history-max 0`. That reaches the 8 GB ceiling in about three years, and
   long before that it makes `etcd` snapshots and restores slow. A single `helm upgrade` somewhere
   in your fleet that omits `--history-max` is enough to start this, because the flag is not
   persisted (doc 04, `R-04`).
2. **The watch cache.** `S-03`.
3. **`helm list -A`.** `S-02`.

**Confirm it.** Measure your own ratio rather than trusting Riverbend's:

```bash
# Total bytes of Helm release Secrets, by namespace
kubectl get secrets -A --field-selector type=helm.sh/release.v1 -o json \
  | jq -r '[.items[] | {ns: .metadata.namespace,
                        b: ((.data.release // "") | length)}]
           | group_by(.ns) | map({ns: .[0].ns, n: length, mb: ((map(.b) | add) / 1048576 * 0.75)})
           | sort_by(-.mb)[] | "\(.ns)\t\(.n) revisions\t\(.mb | floor) MB"' \
  | column -t
```

⚠️ The `* 0.75` converts the base64-encoded length that `jq` sees back to the byte length `etcd`
stores. Without it you overstate by a third.

**Recover / Prevent.** Three levers, in order of effect per unit of effort. `S-04` works the first
one through.

| Lever | Effect at Riverbend | Cost |
|---|---|---|
| `--history-max 5` instead of 10 | 203 MB → 109 MB | Rollback depth 10 → 5. Doc 04 argues 5 is enough |
| Stop vendoring the library chart | 109 MB → 33 MB | A network dependency at build time |
| `--skip-crds` on the platform charts | Removes ~40 MB of CRD schemas from platform release history | You need doc 07's strategy 4 anyway |

Combined: **203 MB → 33 MB, an 84% reduction**, and none of the three is a trade-off you would not
make for other reasons.

## S-02 · `helm list -A` takes 38 seconds

**What you see.** A command that was instant is now slow enough that people stop using it, which
means the fleet becomes invisible.

**Mechanism and derivation.** `helm list -A` does not read labels. It lists every Secret matching
`owner=helm` across all namespaces and then, for each one, **decodes the full payload** —
base64, gunzip, JSON-parse — to construct a release object. So the work is:

```
Per revision:  base64-decode 28 KB   ≈ 0.15 ms
               gunzip 21 KB → 86 KB  ≈ 0.6 ms
               JSON-parse 86 KB      ≈ 1.5 ms
               transfer + allocation ≈ 2.7 ms
                                     ─────────
                                     ≈ 5 ms

7,240 revisions × 5 ms = 36 seconds
```

Plus the API server's own work: 203 MB read from the watch cache, serialised, and sent. Measured at
Riverbend: **38 seconds**, and 1.2 GB of transient allocation in the `helm` process.

⚠️ And note the shape of the cost: it scales with **revisions**, not releases. Dropping
`--history-max` from 10 to 5 halves this, which is a second reason to do it.

**Confirm it.**

```bash
time helm list -A >/dev/null
time helm list -n checkout >/dev/null     # one namespace, ~30 releases
```

**Recover.** Two things, and the second is the one to build.

**Scope it.** `helm list -n checkout` reads one namespace's Secrets and takes under a second. Most
of the time you know the namespace.

**Read labels instead of payloads.** Everything `helm list` displays except the chart version is in
the Secret's labels, which are free:

```bash
kubectl get secrets -A --field-selector type=helm.sh/release.v1 \
  -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.labels.name}{"\t"}{.metadata.labels.version}{"\t"}{.metadata.labels.status}{"\t"}{.metadata.labels.modifiedAt}{"\n"}{end}' \
  | sort -k1,1 -k2,2 -k3,3nr | awk '!seen[$1"/"$2]++' | column -t
```

That takes **1.4 seconds** for the same 7,240 Secrets, because it never decodes a payload. The
`awk` keeps only the highest revision per release. ⚠️ The `status` label is a copy and can be stale
(doc 04), so for a *decision* read the payload for the specific release you care about — but for an
*inventory*, the labels are the right source, and this is the basis of doc 16's exporter.

**Prevent.** Build the inventory (doc 16) so that nobody needs `helm list -A` for a routine
question.

## S-03 · Watch pressure on Secrets

**What you see.** API server memory higher than the object count suggests. A controller that lists
Secrets consuming surprising bandwidth on startup.

**Mechanism.** The API server keeps a watch cache per resource type, holding deserialised objects.
203 MB of release Secrets is 203 MB the API server holds resident, plus per-object overhead —
call it **250–300 MB of API server memory** for Helm release history alone, per API server replica.

Worse, **every client that watches or lists Secrets pays too.** At Riverbend the clients are:

| Client | Why it watches Secrets | Cost of Helm's share |
|---|---|---|
| `kube-controller-manager` | ServiceAccount and certificate controllers | Full list on startup: 203 MB |
| The Argo CD application controller | Cluster credentials and repository secrets | ⚠️ Watches all Secrets by default |
| The ESO controller | Its own `SecretStore` credentials | Full list on startup |
| A cost-attribution agent | Object inventory | Full list every scrape |
| Any operator using a default informer | Nothing — it just does | 203 MB each |

⚠️ **The one to look for is a controller with an unfiltered Secret informer.** Each such controller
holds its own copy. Four of them is 800 MB of pod memory across the cluster, spent caching release
history that none of them care about.

**Confirm it.** Find who is listing Secrets, from the API server's own metrics:

```promql
# Requests to the Secret resource, by client
sum by (client, verb) (
  rate(apiserver_request_total{resource="secrets", verb=~"LIST|WATCH"}[10m])
)

# Bytes the API server is sending for Secret responses
sum by (resource) (rate(apiserver_response_sizes_sum{resource="secrets"}[10m]))
```

And the watch-cache size:

```promql
apiserver_cache_list_total{resource_prefix=~".*secrets.*"}
apiserver_watch_cache_events_received_total{resource="secrets"}
```

**Prevent.** Two things:

1. **Reduce the bytes.** `S-01`'s levers. This is the only fix that helps every client at once.
2. **Fix the unfiltered informers you control.** A controller that only needs its own Secrets should
   scope its informer by namespace or by label selector. For third-party operators, check whether
   they expose a `--namespace` or `--watch-labels` flag; several do and few people set them.

## S-04 · Chart bloat, and the reduction worked through

**What you see.** Release-state size that does not correlate with the number or complexity of your
services.

**Mechanism.** From doc 04: the `chart` field is **73%** of a release payload, and the vendored
library chart is 39 KB of the 47.6 KB of chart bytes — so **the library chart is 60% of every
service release's state.**

**The change.** Stop vendoring. Keep `Chart.lock` (whose digest gives the same integrity guarantee),
run `helm dependency build` in CI, and let the dependency come from the internal OCI registry at
build time.

⚠️ **Important correction to an intuition:** not vendoring does *not* remove the library chart from
the release Secret. Helm embeds whatever is in `charts/` at render time, and after
`helm dependency build` that is the same `.tgz`. Vendoring versus not vendoring is about your **Git
repository** and your build hermeticity — it does not change the release payload at all.

So what *does* reduce it? Three things, and being precise about this matters:

**(a) Make the library chart smaller.** It grew from 12 KB to 39 KB over eighteen months. An audit
found 8 KB of it was templates nothing included any more (the same dead-code problem as doc 03's
`V-11`), and 6 KB was a vendored sub-dependency the library did not need:

```bash
# Named templates defined in the library but included by nothing
comm -23 \
  <(grep -rhoE '\{\{-? *define +"[^"]+"' charts/riverbend-common/templates/ \
      | grep -oE '"[^"]+"' | tr -d '"' | sort -u) \
  <(grep -rhoE '\{\{-? *include +"[^"]+"' charts/ \
      | grep -oE '"[^"]+"' | tr -d '"' | sort -u)
```

Result: 39 KB → 25 KB.

**(b) `.helmignore` the library chart properly.** Its `README.md`, `tests/`, `ci/` and `.github/`
were being packaged: 4 KB.

```
# charts/riverbend-common/.helmignore
.git/
.github/
ci/
tests/
README.md
*.md
.helmignore
```

Result: 25 KB → 21 KB.

**(c) `--history-max 5`.** Halves the multiplier.

**The arithmetic of the combined change:**

```
Before:  chart bytes 47,570 → ×4/3 base64 = 63,427
         + manifest 16,200 + config 3,050 + info 1,240 + structure 2,100 = 86,017 JSON
         ÷ 4.1 gzip = 20,980  → ×4/3 base64 = 27,973  + 900 overhead = 28,873 B
         × 7,240 revisions = 203 MB

After:   chart bytes 29,570 (library 39,000 → 21,000) → ×4/3 = 39,427
         + 16,200 + 3,050 + 1,240 + 1,800 = 61,717 JSON
         ÷ 4.1 = 15,053  → ×4/3 = 20,071  + 900 = 20,971 B   (27% smaller per revision)
         × 3,620 revisions (history-max 5) = 76 MB
```

**203 MB → 76 MB**, a 63% reduction, from deleting dead templates, fixing a `.helmignore`, and
changing one flag. Adding `--skip-crds` on the platform charts took it to **33 MB**.

⚠️ Re-derive this for your own charts. The 4.1:1 compression ratio is the assumption most likely to
be wrong for you — a chart whose bulk is an already-compressed `.tgz` compresses much worse than one
whose bulk is plain-text templates.

## S-05 · Render time

**What you see.** CI stages that take minutes. An Argo CD repo-server at high CPU.

**Mechanism and measurements** at Riverbend:

| Render | Objects | Time |
|---|---|---|
| One service chart via the library | 11 | 0.9 s |
| The 31-subchart umbrella (before the split) | 1,340 | 41 s |
| `kube-prometheus-stack` alone | 620 | 11 s |
| All 240 service charts, sequentially | 2,640 | 3 m 36 s |
| All 240, `xargs -P 8` | 2,640 | 41 s |

The umbrella's 41 seconds is the interesting number, because it was paid on **every** platform
change including a one-line ConfigMap edit (doc 08, `D-10`). Splitting into 31 releases made the
common case 1–11 seconds.

**Prevent.** Parallelise in CI (`xargs -P`), and render only what changed:

```bash
# Charts whose own files changed, plus everything if the library changed
if git diff --name-only origin/main...HEAD | grep -q '^charts/riverbend-common/'; then
  ls -d charts/*/ | grep -v riverbend-common
else
  git diff --name-only origin/main...HEAD | grep '^charts/' | cut -d/ -f1-2 | sort -u
fi | xargs -P 8 -I{} sh -c 'helm template "$(basename {})" {} -f envs/20-prod-use1.yaml >/dev/null || echo "FAILED: {}"'
```

## S-06 · The fleet deploy window

**What you see.** A library-chart rollout that takes a working day.

**Mechanism and derivation.** Rolling `riverbend-common` v4.3.0 to 240 services means 240
`helm upgrade` operations, each with `--atomic --wait`. Per-release time is dominated by the rolling
update, not by Helm:

```
Per release:  render + apply            ≈ 3 s
              rolling update, tier 1    ≈ 2 m 40 s  (24 replicas, maxSurge 25% → 4 batches × 40 s)
              rolling update, tier 3    ≈ 45 s      (3 replicas, 1 batch)
Weighted average across the fleet (30 tier-1, 74 tier-2, 136 tier-3)
              = (30×160 + 74×95 + 136×45) / 240 ≈ 76 s

Sequential:   240 × 76 s   = 5 h 04 m
Parallel ×8:  240 × 76 / 8 = 38 m
Parallel ×24: 240 × 76 / 24 = 13 m
```

⚠️ **But parallelism is bounded by something other than time.** Each concurrent upgrade adds surge
pods. Across 240 services at 25% surge, if all deployed at once, the cluster needs 25% more pod
capacity — 775 extra pods at Riverbend's 3,100. At 24 concurrent that is about 78 extra pods, which
is fine. At 240 concurrent it is a capacity event that the cluster autoscaler will try to solve by
adding nodes, slowly, while every deploy's `--wait` counts down.

**So the concurrency limit is set by surge headroom, not by Helm.** The calculation:

```
spare pod capacity ÷ (average replicas × maxSurge)
= 300 spare pods ÷ (13 avg replicas × 0.25)
= 300 ÷ 3.25 ≈ 92 concurrent upgrades
```

Riverbend uses 24, well inside that, because it also wants the rollout to be interruptible and
observable. 13 minutes for a fleet-wide library rollout is acceptable; the constraint that actually
matters is being able to stop it:

```bash
# Fleet rollout, in tier order, with a stop file and a failure budget
FAILED=0
for tier in 3 2 1; do
  echo "=== tier $tier ==="
  charts_in_tier "$tier" | xargs -P 24 -I{} sh -c '
    [ -f /tmp/STOP-ROLLOUT ] && exit 0
    ./ci/deploy.sh {} prod-use1 || echo {} >> /tmp/rollout-failures
  '
  n=$(wc -l < /tmp/rollout-failures 2>/dev/null || echo 0)
  echo "tier $tier complete; cumulative failures: $n"
  [ "$n" -le 2 ] || { echo "failure budget exceeded — stopping before tier $((tier-1))"; exit 1; }
done
```

⚠️ **Tier 3 first.** The 136 least critical services are the canary for the library change, and by
the time you reach the 30 tier-1 services you have 136 successful upgrades of evidence. This is doc
10's blast-radius ladder applied within a cluster rather than across clusters.

## S-07 · Chart-registry fan-in

**What you see.** Registry rate limits or 5xx during a fleet rollout.

**Mechanism.** 240 concurrent `helm dependency build` operations each pull `riverbend-common` from
the internal registry. At 24 concurrency that is 24 pulls of a 21 KB artefact — trivial. At 240 it
is still trivial in bytes and can still exhaust connection limits on a small registry.

The real fan-in problem is **image pulls**, not charts: 240 services × 24 replicas rolling means up
to 5,760 pod starts, each pulling an image, though node-level image caching absorbs most of it.

**Prevent.** Cache the dependency once per CI run rather than per chart:

```bash
helm pull oci://registry.riverbend.internal/charts/riverbend-common --version 4.3.0 -d /tmp/dep
for c in charts/*/; do
  [ -f "$c/Chart.yaml" ] || continue
  mkdir -p "$c/charts" && cp /tmp/dep/riverbend-common-4.3.0.tgz "$c/charts/"
done
# now no chart needs the network
```

## S-08 · CI minutes

**What you see.** A CI bill, and pull requests that take 20 minutes to check.

**Mechanism and derivation** for doc 14's full pipeline:

| Stage | Per chart | 240 charts, ×8 parallel |
|---|---|---|
| `helm lint` + schema test | 4 s | 2 m |
| Render + `kubeconform`, 4 envs × 2 k8s versions | 8 s | 4 m |
| Conftest | 5 s | 2 m 30 s |
| Golden diff, 2 envs | 10 s | 5 m |
| `--dry-run=server` | 20 s | 10 m |
| `helm diff`, 4 clusters | 30 s | 15 m |
| **Upgrade test in `kind`** | **5 m** | **2 h 30 m** |

**The upgrade test is the whole cost**, which is why doc 14 scopes it to changed charts and, for a
library change, to three representative charts. With that scoping:

- A normal service-chart PR: 1 chart × everything = **6 minutes**.
- A library-chart PR: 240 charts × stages 1–6 plus 3 upgrade tests = **40 minutes**.

⚠️ That second number is the one to design around. A 40-minute check on the highest-risk change you
make is correct, and it needs to be *predictable* — a library PR that sometimes takes 40 minutes and
sometimes 3 hours because the scoping logic misfired is how people start bypassing it.

## S-09 · The Argo CD repo-server thundering herd

**What you see.** After an Argo CD restart, or after a commit to a shared values file, the
repo-server saturates and applications sit `Unknown` for minutes.

**Mechanism and derivation.** Argo CD caches rendered manifests keyed on the repository revision and
the resolved values. So steady state is cheap: a render happens only when something changed.

The problem is what invalidates the cache. **A commit to `envs/00-base.yaml` changes the revision
for every application that references it** — all 816. So:

```
816 applications × 0.9 s render = 734 CPU-seconds of rendering
repo-server parallelism (default `--parallelismlimit`, say 10) → 73 s of queue
```

73 seconds is survivable. But two things make it much worse:

- **A repo-server restart** empties the cache, producing the same 816-render herd with no commit
  involved.
- ⚠️ **`helm dependency build` inside the render.** If the chart's `charts/` is not committed, Argo
  CD runs the dependency resolution per render, adding a registry round trip: 0.9 s becomes 2.4 s,
  and 73 s becomes 195 s.

**Confirm it.**

```promql
# Render duration and queue depth
histogram_quantile(0.95, sum by (le) (rate(argocd_repo_pending_request_total[5m])))
rate(argocd_git_request_duration_seconds_sum[5m]) / rate(argocd_git_request_duration_seconds_count[5m])
```

**Prevent.** Four things, in order:

1. **Commit `charts/`** for charts Argo CD renders, or use a chart from OCI (which Argo caches),
   so the render does no network I/O. This is the biggest single win and it is the opposite of
   `S-04`'s advice — ⚠️ **under Argo CD, vendor; under CI-driven Helm, do not.** The trade-off
   inverts because the renderer is different.
2. **Raise `--parallelismlimit`** and scale repo-server replicas. It is CPU-bound and stateless.
3. **Reduce shared-file churn.** A commit to `00-base.yaml` invalidating 816 applications is
   correct behaviour; the mitigation is not to touch that file casually, and to batch changes to it.
4. **`--repo-server-timeout-seconds`** high enough that a queued render does not time out and retry,
   which turns a herd into a loop.

## S-10 · Accumulated hook Jobs and orphans

**What you see.** 1,847 completed Jobs across 38 namespaces, the oldest 14 months old (doc 06,
`H-01`). Plus orphaned objects from renamed manifests (`U-08`) and `resource-policy: keep` leftovers
(`U-14`).

**Mechanism.** Each is individually harmless. 1,847 Jobs plus their pods is roughly 3,700 objects,
~11 MB — negligible in `etcd` and extremely annoying in every `kubectl get pods -A`, which is the
real cost: **it makes the cluster harder to read.**

**Confirm and clean.**

```bash
kubectl delete jobs -A -l 'helm.sh/hook' --field-selector status.successful=1
```

**Prevent.** `before-hook-creation,hook-succeeded` plus `ttlSecondsAfterFinished` on every hook Job
(doc 06), enforced by the library chart so no service chart can get it wrong.

## S-11 · Object count against cluster limits

**What you see.** Nothing yet. This is the headroom check.

**Derivation** against the published Kubernetes limits, which
[`../../../system-design-notes/Kubernetes/18-performance-and-scale-limits.md`](../../../system-design-notes/Kubernetes/18-performance-and-scale-limits.md)
covers in depth:

| Limit | Published ceiling | Riverbend `use1` | Headroom |
|---|---|---|---|
| Nodes | 5,000 | 380 | 13× |
| Pods | 150,000 | 3,100 | 48× |
| Pods per node | 110 | 8.2 avg | comfortable |
| Services | 10,000 (iptables `kube-proxy`) | 1,240 | 8× |
| `etcd` database | 8 GB practical | 249 MB logical | 32× |
| Secrets (no hard limit) | — | 8,780 | — |

**So Helm is not near a cluster limit and will not be.** The useful conclusion is the opposite of
alarming: at 23× growth, Helm's resource consumption remained a rounding error against Kubernetes'
own ceilings, and every problem in this doc is about **latency and legibility**, not capacity.

⚠️ The one exception worth watching: `etcd` growth with `--history-max 0` reaches the 8 GB ceiling
in about three years at Riverbend's deploy rate, and that failure is a cluster-wide write outage
rather than a slow command. So the flag matters for a reason quite different from the rest of this
doc.

## S-12 · The human limit, which arrives first

**What you see.** Questions that nobody can answer.

Here are five real ones, and at 40 releases each is a glance at a list. At 940 each requires a
script — which means, in practice, that nobody asks.

| Question | Why it matters | At 940 releases |
|---|---|---|
| Which releases are on `riverbend-common` v4.1 or older? | A fleet-wide security fix's coverage | A script with a payload decode per release |
| Which production releases have not been upgraded in 90 days? | Stale charts, stale images, untested rollback paths | Not in `helm list`'s output usefully |
| Which releases would fail to roll back because their previous image is gone? | Doc 05's `U-12` | Cross-reference release manifests against the registry |
| Which releases are in `failed` or `pending-*` right now? | Doc 17's `O-01`, which does not self-resolve | `helm list -A` at 38 seconds, so nobody looks |
| Which releases have values that differ from Git? | Doc 04's `R-12` drift | A `helm diff` per release, per cluster |

**This is the binding constraint.** Not one of those questions is hard; all of them are
*inconvenient*, and inconvenient questions in a large fleet are questions that go unasked until an
incident forces them.

**Recover / Prevent.** Build the inventory. That is doc 16, and the argument for it is exactly this
scenario: **the cost of not having one is not a slow command, it is a fleet where nobody knows the
answer to a question they should be able to answer in one second.**

The minimum viable version is the label-reading query from `S-02`, run every five minutes into a
metrics endpoint. Doc 16 builds it out.

## S-13 · The breakpoint table

Consolidating, so you can find where you are:

| At... | What starts to hurt | The fix |
|---|---|---|
| ~20 releases | Nothing. Read the list | — |
| ~50 releases | Chart duplication. Every chart is somebody's dialect | A library chart (doc 08) |
| ~100 releases | `helm list -A` noticeably slow; no answer to "which version of what" | `--history-max 5`; start the inventory (doc 16) |
| ~200 releases | Fleet rollouts need concurrency and a failure budget | `S-06`'s tiered rollout |
| ~400 releases | Release history is the largest object class in `etcd` | `S-01`'s three levers |
| ~800 releases | `helm list -A` over 30 s, so people stop using it. Argo repo-server herds | The inventory is now mandatory; `S-09` |
| ~2,000 releases | Shared-values-file churn invalidates thousands of renders | Split the values repository per domain; consider more Argo instances |
| ~5,000 releases | One Argo CD or one `etcd` per fleet is the wrong shape | Shard by cluster group; this is Northlight's problem, not Riverbend's |

⚠️ Note what is *not* in that table: nodes, pods, and CPU. Helm's scale problems are about the
number of *releases and revisions*, which is uncorrelated with the size of your cluster. A 40-node
cluster with 900 releases has every problem in this doc; a 5,000-node cluster with 30 releases has
none of them.

## What to take away

1. **Helm does not fail at scale; it gets slow and consumes resources nobody attributed to it.**
   Everything still works and everything is worse.
2. **Release history was 82% of Riverbend's `etcd` contents and 96% of its Secret bytes.** Measure
   your own ratio; it is almost always larger than people expect, because nobody counts it.
3. **249 MB against an 8 GB ceiling is 3%, so this is not an availability problem** — it is a
   latency, memory and legibility problem. The one exception is `--history-max 0`, which reaches the
   ceiling in about three years and fails as a cluster-wide write outage.
4. **`helm list -A` decodes every revision's payload**, which is why it takes 38 seconds at 7,240
   revisions. Reading the Secret labels instead takes 1.4 seconds and is the basis of an inventory.
5. **Three levers took release state from 203 MB to 33 MB**: `--history-max 5`, deleting dead
   library templates and fixing `.helmignore`, and `--skip-crds`. None is a trade-off you would not
   make anyway.
6. **Vendoring does not change the release payload** — Helm embeds `charts/` either way. It is about
   your Git repository and build hermeticity. ⚠️ And the advice inverts by renderer: under Argo CD,
   vendor (to avoid a network round trip per render); under CI-driven Helm, do not.
7. **Fleet rollout concurrency is bounded by surge headroom, not by Helm.** Spare pod capacity
   divided by (average replicas × `maxSurge`) gives the real limit — 92 at Riverbend, of which they
   use 24 so the rollout stays interruptible.
8. **Roll out to tier 3 first, within the cluster.** 136 low-criticality upgrades is evidence before
   the 30 that matter, and a failure budget between tiers stops a bad library version early.
9. **A commit to a shared values file invalidates every Argo CD application that reads it** — 816
   renders from one commit. Commit `charts/` so renders do no network I/O, raise the parallelism
   limit, and do not touch shared files casually.
10. **The human limit arrives before every technical one.** Five basic questions about the fleet go
    from a glance to a script, and inconvenient questions go unasked until an incident forces them.
    That is the real reason to build the inventory.
11. **Helm's scale is measured in releases and revisions, not nodes and pods.** A small cluster with
    many releases has every problem here; a huge cluster with few releases has none.
