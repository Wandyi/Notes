# Observability and the Release Inventory

**Helm emits no metrics.** It is a command-line program that exits. There is no `/metrics`, no
controller, nothing to scrape. Doc 15 ended by arguing that the binding constraint at fleet scale
is nobody being able to answer basic questions about releases; this doc builds the thing that
answers them.

The thesis:

> **The state is already in the cluster — you just have to expose it.** Every release Secret's
> labels carry the release name, revision, status and modification time, and its payload carries the
> chart version, the dependency versions, and the rendered manifest. An exporter that reads those
> and emits Prometheus metrics is about a hundred lines, and it converts five questions that
> currently require a script into five PromQL queries and four alerts.

And the one that decides where to put your effort:

> **Alert on the states that do not self-resolve.** A `failed` release is loud — somebody's deploy
> went red. A `pending-upgrade` release is silent, permanent, and blocks the next deploy. So does a
> `SecretSynced: false` ExternalSecret, an `Application` with auto-sync disabled, and a release
> whose rollback image has been garbage-collected. Those four are the alerts worth having; most
> others are noise.

## The questions to be able to answer

From doc 15's `S-12`, plus three more that come up during incidents:

| # | Question | Why | Answered by |
|---|---|---|---|
| 1 | Which releases are in `failed` or `pending-*`? | Does not self-resolve; blocks the next deploy | `I-03` |
| 2 | Which releases are on library chart v4.1 or older? | Coverage of a fleet-wide fix | `I-02` |
| 3 | Which production releases have not been upgraded in 90 days? | Stale image, untested rollback | `I-07` |
| 4 | Which releases cannot be rolled back? | Doc 05's `U-12` | `I-06` |
| 5 | Which releases differ from Git? | Doc 04's `R-12` | `I-04` |
| 6 | Which objects are orphans? | Doc 05's `U-08`, `U-14` | `I-05` |
| 7 | Who deployed this, when, and from which commit? | Every incident, within the first two minutes | `I-08` |
| 8 | How much `etcd` is release history? | Doc 15's `S-01` | `I-01` |

## The exporter

A CronJob that reads release Secrets and writes Prometheus metrics to a Pushgateway — or, if you
prefer a pull model, a small HTTP server. The CronJob version is simpler and the staleness (five
minutes) is acceptable for everything above.

```bash
#!/usr/bin/env bash
# helm-inventory.sh — emit Prometheus metrics for every Helm release in the cluster.
# Runs as a CronJob every 5 minutes. Needs `get`/`list` on secrets cluster-wide.
set -euo pipefail

OUT=$(mktemp); trap 'rm -f "$OUT"' EXIT
NOW=$(date -u +%s)

cat >> "$OUT" <<'HEADER'
# HELP helm_release_info Metadata for a Helm release revision.
# TYPE helm_release_info gauge
# HELP helm_release_status One per release, value 1, labelled with the current status.
# TYPE helm_release_status gauge
# HELP helm_release_last_deployed_timestamp_seconds Unix time of the last modification.
# TYPE helm_release_last_deployed_timestamp_seconds gauge
# HELP helm_release_revision_count Retained revisions for this release.
# TYPE helm_release_revision_count gauge
# HELP helm_release_state_bytes Bytes this release's retained revisions occupy in etcd.
# TYPE helm_release_state_bytes gauge
HEADER

# Pass 1: the cheap labels-only sweep. This is doc 15's S-02 query — no payload decoding,
# so it completes in about a second even at 7,240 revisions.
kubectl get secrets -A --field-selector type=helm.sh/release.v1 -o json \
  | jq -r '.items[] | [
      .metadata.namespace,
      (.metadata.labels.name      // "?"),
      (.metadata.labels.version    // "0"),
      (.metadata.labels.status     // "unknown"),
      (.metadata.labels.modifiedAt // "0"),
      (((.data.release // "") | length) * 3 / 4 | floor)
    ] | @tsv' > /tmp/revisions.tsv

# Aggregate per release: highest revision wins for status; sum bytes; count revisions.
awk -F'\t' -v now="$NOW" '
  { key = $1 "\t" $2
    count[key]++
    bytes[key] += $6
    if ($3 + 0 > maxrev[key] + 0) { maxrev[key] = $3; status[key] = $4; modified[key] = $5 }
  }
  END {
    for (k in count) {
      split(k, p, "\t"); ns = p[1]; rel = p[2]
      printf "helm_release_status{namespace=\"%s\",release=\"%s\",status=\"%s\"} 1\n",
             ns, rel, status[k]
      printf "helm_release_info{namespace=\"%s\",release=\"%s\",revision=\"%s\"} 1\n",
             ns, rel, maxrev[k]
      printf "helm_release_last_deployed_timestamp_seconds{namespace=\"%s\",release=\"%s\"} %s\n",
             ns, rel, modified[k]
      printf "helm_release_revision_count{namespace=\"%s\",release=\"%s\"} %d\n",
             ns, rel, count[k]
      printf "helm_release_state_bytes{namespace=\"%s\",release=\"%s\"} %d\n",
             ns, rel, bytes[k]
    }
  }' /tmp/revisions.tsv >> "$OUT"

# Pass 2: chart and dependency versions. This DOES decode payloads, so it is the expensive
# part — one decode per release (not per revision), current revision only.
cat >> "$OUT" <<'HEADER2'
# HELP helm_release_chart_info Chart and app version of the current revision.
# TYPE helm_release_chart_info gauge
# HELP helm_release_dependency_info A resolved dependency of the current revision.
# TYPE helm_release_dependency_info gauge
HEADER2

awk -F'\t' '{ key = $1 "\t" $2; if ($3+0 > m[key]+0) { m[key] = $3 } }
            END { for (k in m) { split(k, p, "\t"); print p[1] "\t" p[2] "\t" m[k] } }' \
  /tmp/revisions.tsv \
| while IFS=$'\t' read -r ns rel rev; do
    payload=$(kubectl get secret -n "$ns" "sh.helm.release.v1.${rel}.v${rev}" \
                -o jsonpath='{.data.release}' 2>/dev/null \
              | base64 -d 2>/dev/null | base64 -d 2>/dev/null | gzip -d 2>/dev/null) || continue
    [ -n "$payload" ] || continue
    jq -r --arg ns "$ns" --arg rel "$rel" '
      "helm_release_chart_info{namespace=\"\($ns)\",release=\"\($rel)\"," +
        "chart=\"\(.chart.metadata.name // "?")\"," +
        "chart_version=\"\(.chart.metadata.version // "?")\"," +
        "app_version=\"\(.chart.metadata.appVersion // "?")\"} 1",
      ( (.chart.metadata.dependencies // [])[]
        | "helm_release_dependency_info{namespace=\"\($ns)\",release=\"\($rel)\"," +
          "dependency=\"\(.name)\",dep_version=\"\(.version)\"} 1" )
    ' <<<"$payload" 2>/dev/null
  done >> "$OUT"

curl -sf --data-binary @"$OUT" \
  "http://pushgateway.observability:9091/metrics/job/helm-inventory/cluster/${CLUSTER_NAME}"
echo "pushed $(grep -c '^helm_' "$OUT") samples"
```

Three design decisions in there that are worth copying:

**Two passes with different costs.** Pass 1 reads labels only and is cheap enough to run every five
minutes at any scale. Pass 2 decodes one payload per *release* (940) rather than per *revision*
(7,240), which is an eightfold reduction and takes about 5 seconds. Everything the alerts need is in
pass 1; pass 2 exists for the inventory questions.

**`* 3 / 4` on the data length** converts the base64 length `jq` sees into the byte length `etcd`
stores. Doc 15 depends on this being right.

**The `cluster` label in the Pushgateway path** makes the metrics fleet-wide, so a single PromQL
query covers all four clusters. That is what makes doc 10's environment questions answerable.

### Deploying it

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: helm-inventory
  namespace: observability
spec:
  schedule: "*/5 * * * *"
  concurrencyPolicy: Forbid                    # K8s/cronJobs doc 02
  startingDeadlineSeconds: 120
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 300
      ttlSecondsAfterFinished: 3600
      template:
        spec:
          serviceAccountName: helm-inventory
          restartPolicy: Never
          containers:
            - name: inventory
              image: registry.riverbend.internal/mirror/toolbox:1.9   # kubectl + jq + curl
              command: ["/scripts/helm-inventory.sh"]
              env:
                - name: CLUSTER_NAME
                  value: rb-prod-use1
              resources:
                requests: { cpu: 100m, memory: 256Mi }
                limits:   { memory: 512Mi }
              volumeMounts:
                - { name: scripts, mountPath: /scripts }
          volumes:
            - name: scripts
              configMap: { name: helm-inventory-scripts, defaultMode: 0755 }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: helm-inventory
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "list"]
```

⚠️ **That ClusterRole reads every Secret in the cluster**, which is a significant grant and is
unavoidable for this job (doc 13: release Secrets have unpredictable names, so `resourceNames`
cannot narrow it). Two consequences to accept deliberately: the exporter's ServiceAccount is a
high-value target, and **the metrics must not contain secret values** — which is why pass 2 extracts
only `chart.metadata` and never touches `.config`.

## The alerts worth having

Four, and the shared property is that each catches a state that **does not resolve itself**.

```yaml
groups:
  - name: helm-releases
    rules:
      # 1. The most valuable alert in this doc. pending-* is permanent and blocks deploys.
      - alert: HelmReleaseStuckPending
        expr: |
          helm_release_status{status=~"pending-.*"} == 1
        for: 15m
        labels: { severity: warning }
        annotations:
          summary: "Release {{ $labels.namespace }}/{{ $labels.release }} stuck in {{ $labels.status }}"
          description: >-
            This state never clears on its own and the next helm upgrade will refuse with
            "another operation in progress". See Helm/17, O-01, for the recovery — and check
            whether a deploy is genuinely running before intervening.

      # 2. A failed release is usually already known, but a failed release that stays failed
      #    means nobody followed up.
      - alert: HelmReleaseFailedAndAbandoned
        expr: |
          helm_release_status{status="failed"} == 1
        for: 2h
        labels: { severity: warning }
        annotations:
          summary: "Release {{ $labels.namespace }}/{{ $labels.release }} failed 2h ago and is still failed"

      # 3. Release history disappearing means something is deleting Secrets. Doc 04, R-11.
      - alert: HelmReleaseHistoryShrinking
        expr: |
          (
            sum by (cluster) (helm_release_revision_count)
            - sum by (cluster) (helm_release_revision_count offset 1h)
          )
          < -0.10 * sum by (cluster) (helm_release_revision_count offset 1h)
        for: 10m
        labels: { severity: critical }
        annotations:
          summary: "{{ $labels.cluster }}: Helm revision count dropped >10% in an hour"
          description: >-
            Something is deleting release Secrets. Check for Secret-cleanup automation that does
            not exclude type=helm.sh/release.v1. Rollback targets are being destroyed.

      # 4. Release state growth. Doc 15, S-01.
      - alert: HelmReleaseStateGrowth
        expr: |
          sum by (cluster) (helm_release_state_bytes) > 5e8
        for: 1h
        labels: { severity: info }
        annotations:
          summary: "{{ $labels.cluster }}: Helm release state is {{ $value | humanize1024 }}B"
          description: >-
            See Helm/15, S-01 and S-04. The three levers are --history-max 5, shrinking the
            library chart, and --skip-crds on platform charts.
```

⚠️ **The alerts deliberately not included, and why:**

- **"A release was upgraded"** — that is a deploy, not an event. Your CI already knows.
- **"A release has more than N revisions"** — `--history-max` handles this; an alert would fire
  constantly and change nothing.
- **"A release's chart version is behind the latest"** — this is a *report*, not an alert. It is
  true of most releases most of the time, and an alert that is always firing is not an alert.

That last distinction is the useful one: **`I-02`, `I-06` and `I-07` below are reports, and the
four above are alerts.** Reports go to a dashboard or a weekly message and drive planned work.
Alerts interrupt somebody. Confusing the two is how you end up with an alerting channel nobody
reads.

## The `I-` catalogue

### I-01 · Helm emits no metrics

**Mechanism.** Helm is a CLI. There is nothing to scrape.

**Recover.** The exporter above. ⚠️ Note that **under Flux's `helm-controller` you get some of this
for free** — the controller exposes `gotk_reconcile_condition` with `kind="HelmRelease"`, which
covers the status question:

```promql
gotk_reconcile_condition{kind="HelmRelease", type="Ready", status="False"} == 1
```

So if you run model 2 from doc 11, check what the controller already gives you before building the
exporter. It will not give you chart versions, dependency versions, or state bytes, so you will
still want pass 2.

### I-02 · "Which releases are on the old library chart?"

**The report:**

```promql
helm_release_dependency_info{dependency="riverbend-common", dep_version!~"4\\.[3-9]\\..*"}
```

As a table, grouped by owning namespace, on a dashboard. And as a one-off from the command line
without the exporter:

```bash
kubectl get secrets -A --field-selector type=helm.sh/release.v1 -o json \
  | jq -r '.items[] | [.metadata.namespace, .metadata.labels.name, (.metadata.labels.version|tonumber)] | @tsv' \
  | sort -k1,1 -k2,2 -k3,3nr | awk -F'\t' '!s[$1"/"$2]++' \
  | while IFS=$'\t' read -r ns rel rev; do
      v=$(kubectl get secret -n "$ns" "sh.helm.release.v1.${rel}.v${rev}" \
            -o jsonpath='{.data.release}' 2>/dev/null \
          | base64 -d | base64 -d | gzip -d 2>/dev/null \
          | jq -r '(.chart.metadata.dependencies // [])[] | select(.name=="riverbend-common") | .version' 2>/dev/null)
      [ -n "$v" ] && printf '%-18s %-30s %s\n' "$ns" "$rel" "$v"
    done | sort -k3
```

**Why this matters concretely.** When a library-chart fix goes out, this report is the *coverage
measurement*. Without it, "we rolled out the fix" means "we ran the script and it did not obviously
fail", and the eleven services that were mid-incident and got skipped are invisible. Doc 15's
`S-06` tiered rollout writes failures to a file; this report tells you whether the fleet actually
converged a day later.

### I-03 · Stuck `pending-*` releases

Alert 1 above. **Mechanism:** doc 05, `U-09` — Helm writes `pending-upgrade` before doing the work
and has no lease or expiry, so a killed process leaves it forever.

⚠️ **The `for: 15m` in the alert is doing real work.** A `pending-upgrade` that is 30 seconds old is
a deploy in progress, and alerting on it would page somebody for every deploy. Fifteen minutes is
comfortably longer than Riverbend's 8-minute timeout plus its rollback, so anything that old is
genuinely dead. **Set this from your own longest `--timeout`, not from the example.**

### I-04 · Drift from Git

**What you see.** Nothing, which is the problem.

**The detection:** a scheduled `helm diff` per release, reporting a boolean.

```bash
#!/usr/bin/env bash
# drift-check.sh — nightly. Reports which releases differ from what Git would produce.
set -uo pipefail
OUT=$(mktemp); trap 'rm -f "$OUT"' EXIT
echo '# TYPE helm_release_drift_lines gauge' >> "$OUT"

for cluster in rb-dev rb-stg rb-prod-euw1 rb-prod-use1; do
  env=${cluster#rb-}
  tier=$([[ $env == prod-* ]] && echo prod || echo nonprod)
  helm list -A --kube-context "$cluster" -o json \
    | jq -r '.[] | "\(.namespace)\t\(.name)"' \
    | while IFS=$'\t' read -r ns rel; do
        [ -d "charts/$rel" ] || continue           # skip third-party releases
        n=$(helm diff upgrade "$rel" "./charts/$rel" \
              -n "$ns" --kube-context "$cluster" \
              -f envs/00-base.yaml -f "envs/10-${tier}.yaml" -f "envs/20-${env}.yaml" \
              --no-color --context 0 2>/dev/null | grep -cE '^[+-]' || echo 0)
        echo "helm_release_drift_lines{cluster=\"$cluster\",namespace=\"$ns\",release=\"$rel\"} $n" >> "$OUT"
      done
done
curl -sf --data-binary @"$OUT" "http://pushgateway.observability:9091/metrics/job/helm-drift"
```

⚠️ **Two honest limitations.** The image tag differs by design — CI sets it per deploy and Git does
not know it — so every release shows a non-zero diff unless you normalise it out. Add
`--set-string image.tag=$(current_deployed_tag)` or filter the diff. And under Argo CD's render-only
model this whole check is unnecessary, because the controller *is* a continuous drift detector and
reports `OutOfSync` itself — which is one of model 1's real advantages (doc 11).

**The useful signal is a change in the number**, not the number itself. A release whose drift was 2
lines for a month and is suddenly 40 is worth looking at.

### I-05 · Orphan audit

Doc 05's `U-08` and `U-14`. The audit, fleet-wide:

```bash
#!/usr/bin/env bash
# orphans.sh — objects annotated as belonging to a release, but absent from its manifest.
set -uo pipefail
KINDS="deploy,sts,ds,svc,cm,secret,sa,hpa,pdb,ingress,job,cronjob"

helm list -A -o json | jq -r '.[] | "\(.namespace)\t\(.name)"' \
| while IFS=$'\t' read -r ns rel; do
    helm get manifest "$rel" -n "$ns" 2>/dev/null \
      | yq -r 'select(.kind!=null) | "\(.kind|ascii_downcase)/\(.metadata.name)"' \
      | sort -u > /tmp/owned.txt
    kubectl get $KINDS -n "$ns" -o json 2>/dev/null | jq -r --arg r "$rel" '.items[]
        | select((.metadata.annotations // {})["meta.helm.sh/release-name"] == $r)
        | "\(.kind|ascii_downcase)/\(.metadata.name)"' | sort -u > /tmp/actual.txt
    comm -13 /tmp/owned.txt /tmp/actual.txt | sed "s|^|ORPHAN $ns/$rel |"
  done
# Plus: objects with NO owner at all that carry Helm's managed-by label
kubectl get $KINDS -A -o json | jq -r '.items[]
  | select((.metadata.labels // {})["app.kubernetes.io/managed-by"] == "Helm")
  | select((.metadata.annotations // {}) | has("meta.helm.sh/release-name") | not)
  | "UNOWNED \(.metadata.namespace)/\(.kind|ascii_downcase)/\(.metadata.name)"'
```

The second half catches a different thing and is worth having: objects that carry Helm's
`managed-by` label with no release annotation. That combination means either a `helm template`
output applied with `kubectl` (doc 11's `G-01`), or a release whose Secrets were deleted (doc 04's
`R-11`). Both are worth knowing about.

⚠️ This is a **report**, run weekly. The orphans are not urgent; they are a slow accumulation of
cost and confusion. Riverbend's first run found 84 orphans including 11 PVCs totalling 2.3 TB.

### I-06 · Which releases cannot be rolled back

Doc 05's `U-12`. The pre-flight check, run as a report so you know *before* an incident:

```bash
#!/usr/bin/env bash
# rollbackable.sh — can each release actually roll back one revision?
set -uo pipefail
helm list -A -o json | jq -r '.[] | "\(.namespace)\t\(.name)\t\(.revision)"' \
| while IFS=$'\t' read -r ns rel rev; do
    prev=$((rev - 1))
    [ "$prev" -ge 1 ] || { echo "NO-PREV   $ns/$rel (only revision 1)"; continue; }
    if ! helm get manifest "$rel" -n "$ns" --revision "$prev" >/dev/null 2>&1; then
      echo "NO-HISTORY $ns/$rel (revision $prev pruned — see R-04)"; continue
    fi
    missing=0
    while read -r img; do
      crane manifest "$img" >/dev/null 2>&1 || { echo "IMG-GONE  $ns/$rel -> $img"; missing=1; }
    done < <(helm get manifest "$rel" -n "$ns" --revision "$prev" \
               | yq -r '.. | select(has("image")) | .image' 2>/dev/null \
               | grep -v '^null$' | sort -u)
    [ "$missing" -eq 0 ] && echo "OK        $ns/$rel (rev $prev)"
  done
```

**Why this is a report and not an alert:** it is true of a handful of releases most of the time and
the fix is a registry retention policy, not an interrupt. But it is exactly the thing you want to
have already known when somebody asks "can we roll back?" during an incident — and finding out then
is the worst possible time.

⚠️ And note it also catches `U-13`: a release whose previous manifest uses an API version the
cluster has since removed. Extend the check with a `kubectl apply --dry-run=server` on the previous
manifest if that matters to you, which it does immediately after a cluster upgrade.

### I-07 · Stale releases

```promql
# Production releases not upgraded in 90 days
(time() - helm_release_last_deployed_timestamp_seconds{cluster=~"rb-prod-.*"}) > 90 * 86400
```

**Why this matters, and it is not what people assume.** A release that has not deployed in 90 days
is not necessarily wrong — some services are genuinely stable. What it *is*, reliably:

- **An untested deploy path.** Nobody has exercised this release's upgrade in three months. The
  first person to do so will discover whatever broke in the meantime — a chart change, an API
  removal, a values file that moved.
- **An image that may be gone.** `I-06`'s check, at higher risk.
- **A base image with unpatched CVEs**, because nothing has rebuilt it.

So the action is not "deploy it because it is old"; it is **exercise the deploy path deliberately**.
Riverbend redeploys every production release at least quarterly, unchanged, specifically so that
"can we deploy this?" is never an open question. The same pass is the one doc 05's `U-13`
recommends after a cluster upgrade.

### I-08 · The deploy ledger

**What you see.** "Who deployed this?" — and `helm history` says
`https://ci.riverbend.internal/p/8977`, which tells you the pipeline and not the person, the
commit, or the reason.

**Mechanism.** Helm records what it was told (`--description`) and the cluster's audit log records
the *identity* that made the API calls, which for a CI deploy is a ServiceAccount. Neither knows
which human pressed the button.

**The ledger** is a small append-only record written by CI, joining what Helm knows to what CI
knows:

```bash
# In the deploy pipeline, after a successful helm upgrade
cat <<EOF | curl -sf -X POST --data-binary @- http://deploy-ledger.observability/v1/deploys
{
  "cluster":        "${CLUSTER}",
  "namespace":      "${NS}",
  "release":        "${REL}",
  "revision":       $(helm get metadata "$REL" -n "$NS" -o json | jq '.revision'),
  "chart_version":  "${CHART_VER}",
  "image_digest":   "${IMG_DIGEST}",
  "git_sha":        "${GIT_SHA}",
  "git_ref":        "${GIT_REF}",
  "pr":             "${PR_URL}",
  "triggered_by":   "${GITHUB_ACTOR}",
  "approved_by":    "${APPROVER}",
  "pipeline":       "${CI_PIPELINE_URL}",
  "started_at":     "${DEPLOY_START}",
  "finished_at":    "$(date -u +%FT%TZ)",
  "outcome":        "success"
}
EOF
```

**What it buys, concretely.** During an incident the first question is "what changed", and the
ledger answers it across the fleet in one query rather than 940 `helm history` calls:

```bash
# Everything deployed to production in the last two hours, newest first
curl -sf 'http://deploy-ledger.observability/v1/deploys?cluster=rb-prod-use1&since=2h' \
  | jq -r '.[] | "\(.finished_at)  \(.release)  \(.chart_version)  \(.git_sha[0:8])  \(.triggered_by)"'
```

⚠️ **Two properties make this worth building rather than relying on `helm history`.** It survives
`--history-max` pruning, so the record of this morning's deploy is still there after four failed
`--atomic` attempts consumed the revisions (doc 04, `R-04`). And it covers releases Helm does not
know about — the model-1 Argo CD applications from doc 11, where there is no release Secret at all.

**If you do not want to build a service**, the cheap version is genuinely useful: append a line to
an object-storage file, or write a Kubernetes `Event`, or — simplest — put the full context in the
`--description`:

```bash
--description "${GITHUB_ACTOR}@${GIT_SHA:0:8} chart=${CHART_VER} pr=${PR_NUMBER} ${CI_PIPELINE_URL}"
```

That single flag change gets you 70% of the value for zero infrastructure, and it is the thing to do
today if you do nothing else in this doc.

## The dashboard

What to put on it, in reading order — the questions people actually arrive with:

| Panel | Query | Why it is on the dashboard |
|---|---|---|
| Releases by status | `sum by (status) (helm_release_status)` | The one-glance health of the fleet |
| Stuck and failed, as a table | `helm_release_status{status=~"pending-.*\|failed"}` | The actionable list |
| Release state bytes per cluster | `sum by (cluster) (helm_release_state_bytes)` | Doc 15's `S-01`, trended |
| Library-chart version spread | `count by (dep_version) (helm_release_dependency_info{dependency="riverbend-common"})` | Fleet-fix coverage |
| Releases not deployed in 90 days | `I-07`'s query | Untested deploy paths |
| Drift, top 20 by lines | `topk(20, helm_release_drift_lines)` | Where Git and the cluster disagree |
| Deploys per day, by cluster | From the ledger | Change rate, and the denominator for a change-failure rate |

⚠️ That last panel is the one that turns this into something a manager can use: **deploys per day is
the denominator of your change-failure rate**, and a change-failure rate is the number that makes an
argument for the testing in doc 14. Without the ledger you do not have the denominator.

## What to take away

1. **Helm emits no metrics** — it is a CLI that exits. The state is already in the cluster; an
   exporter of about a hundred lines exposes it.
2. **Read labels for the inventory, payloads only when you need the chart version.** Labels are one
   pass and a second; payloads are eight times fewer if you read one per release instead of one per
   revision.
3. **Alert on the states that do not self-resolve.** `pending-*` is the most valuable alert in this
   doc, because it is permanent, silent, and blocks the next deploy. Set its `for:` from your own
   longest timeout, or you will page on every deploy.
4. **Distinguish alerts from reports.** Stale releases, old library versions, orphans and
   rollback-ability are reports that drive planned work. Treating them as alerts produces a channel
   nobody reads.
5. **The exporter needs cluster-wide Secret read**, which is unavoidable and makes its
   ServiceAccount a high-value target. Never let the metrics touch `.config` — extract only chart
   metadata.
6. **Know before the incident whether each release can roll back.** An image garbage-collected from
   the registry, a pruned revision, or a removed API version each make rollback impossible, and
   finding out mid-incident is the worst time.
7. **A release not deployed in 90 days is an untested deploy path**, not necessarily a stale one.
   The action is to exercise it deliberately — Riverbend redeploys every production release
   quarterly, unchanged, so "can we deploy this?" is never open.
8. **Build the deploy ledger, or at minimum put the actor, commit and pull request in
   `--description`.** The flag change costs nothing and answers "what changed" in the first two
   minutes of an incident.
9. **The ledger survives what `helm history` does not**: `--history-max` pruning, and releases with
   no release Secret at all under Argo CD's render-only model.
10. **Deploys per day is the denominator of your change-failure rate**, which is the number that
    justifies everything in doc 14. Without a ledger you do not have it.
