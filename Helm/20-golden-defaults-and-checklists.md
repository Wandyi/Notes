# Golden Defaults and Checklists

Everything in the collection, condensed into things you can copy. The annotated golden chart, the
flag set with every flag justified, a three-tier model so a low-stakes release stays low-stakes, and
five checklists.

⚠️ **One warning about using this doc on its own.** Every default here is a conclusion from an
earlier doc, and a default you adopt without knowing what it prevents is a default somebody will
remove in six months because it looked unnecessary. Each entry cites its doc; when you disagree with
one, read that doc before changing it.

## The golden flag set

```bash
helm upgrade "$REL" "$CHART" \
  --install \
  --namespace "$NS" \
  --values envs/00-base.yaml \
  --values "envs/10-${TIER}.yaml" \
  --values "envs/20-${ENV}.yaml" \
  --set-string image.digest="$IMG_DIGEST" \
  --atomic \
  --timeout "${TIMEOUT}" \
  --history-max 5 \
  --wait-for-jobs \
  --description "${ACTOR}@${GIT_SHA:0:8} chart=${CHART_VER} pr=${PR} ${CI_URL}"
```

| Flag | Why | Doc |
|---|---|---|
| `--install` | Idempotent; identical in every environment. There is no reason to run bare `helm install` in automation | 05 |
| `--namespace` explicitly | Never rely on the kubeconfig context. Half of "release: not found" is this | 04 · `R-01` |
| Three `--values`, enumerated | Layered configuration; **never globbed**, because a glob sorts lexically and can invert precedence | 03 · `V-05`, 10 |
| `--set-string image.digest` | A digest, not a tag, so the bytes are pinned. `--set-string` because `--set` turns `1.20` into `1.2` | 03 · `V-04`, 09 |
| `--atomic` | Failures are clean: succeeded, or rolled back. ⚠️ Three exceptions below | 05 · `U-03` |
| `--timeout` **computed** | The 5-minute default is arithmetically impossible at high replica counts. Derive it | 05 · `U-01`, 18 · `CS-4` |
| `--history-max 5` | Enough for one bad deploy plus a retry; not persisted, so it must be on every invocation | 04 · `R-04` |
| `--wait-for-jobs` | Without it an ordinary `Job` is "ready" when created, so a release whose purpose is a job reports success before it starts | 05 · `U-05` |
| `--description` with the actor, commit and PR | `helm history` becomes useful six weeks later. One flag, 70% of a deploy ledger's value | 05, 16 · `I-08` |

**Flags deliberately absent, and why:**

| Flag | Why not |
|---|---|
| `--force` | Replaces rather than patches; for a Deployment that is every pod at once — a full outage. `kubectl delete --cascade=orphan` does the same job with no downtime (05 · `U-07`, `U-10`) |
| `--reuse-values` | Silently ignores new chart defaults, so you upgrade the templates without the defaults. If you want it, you have unwritten values files (03 · `V-10`) |
| `--skip-crds` *without a CRD apply step* | Half a change. Fine **with** the apply step in the same commit (07 · `C-04`) |
| `--no-hooks` | A deploy that skips migrations. If you need it, something is wrong with the hook, not the deploy |

**The computed timeout:**

```bash
# Derive from the rollout shape, not from a default. See 05 · U-01 and 18 · CS-4.
REPL=$(kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 1)
SURGE_PCT=$(kubectl -n "$NS" get deploy "$DEP" \
  -o jsonpath='{.spec.strategy.rollingUpdate.maxSurge}' 2>/dev/null | tr -d '%' || echo 25)
: "${SURGE_PCT:=25}"
BATCHES=$(( (100 + SURGE_PCT - 1) / SURGE_PCT ))
PER_BATCH=190          # node provisioning (150s) + pod ready (40s); measure YOUR cluster
MIGRATION=60           # the pre-upgrade hook's budget
TIMEOUT="$(( BATCHES * PER_BATCH + MIGRATION + 120 ))s"
```

⚠️ **`PER_BATCH` is the number to measure rather than inherit.** It is dominated by how long your
cluster autoscaler takes to add a node, which varies by an order of magnitude between clusters.

**The three cases where `--atomic` is wrong:**

1. **A release with forward-only migrations.** `--atomic` gives you old code against a new schema.
   With expand/contract (doc 06) this stops being a concern, which is the better fix.
2. **A deploy inside a hard deadline window.** The rollback can fail the same way the upgrade did
   (`CS-4`). A half-applied upgrade a human is watching beats an automatic rollback that also times
   out.
3. **The very first install.** There is no previous revision, so `--atomic` uninstalls the release
   entirely — which is usually what you want, and worth knowing rather than discovering.

## The golden chart

### `Chart.yaml`

```yaml
apiVersion: v2
name: checkout-api
version: 4.11.3                     # the CHART version. Bump on any template or values change.
appVersion: "2026.09.17"            # ⚠️ QUOTED. Unquoted 1.20 becomes the float 1.2 (02 · T-05)
type: application
description: Riverbend checkout API
kubeVersion: ">=1.29.0-0"           # ⚠️ the -0 is required to match 1.29.0-eks-a1b2c3 (02)
dependencies:
  - name: riverbend-common
    version: "4.2.0"                # ⚠️ EXACT. No ^, ~, or >= (08 · D-03)
    repository: "oci://registry.riverbend.internal/charts"
annotations:
  riverbend.io/owner: checkout-team
  riverbend.io/runbook: https://wiki.riverbend.internal/runbooks/checkout-api
```

### `values.yaml`

```yaml
# ---------------------------------------------------------------------------
# checkout-api — 18 values. Everything else is a PLATFORM decision and lives
# in riverbend-common. If you want a knob that is not here, open a PR against
# the library chart rather than adding one (03 · V-11).
# ---------------------------------------------------------------------------

# Service criticality. ONE value from which the library derives the PDB percentage,
# priority class, topology-spread strictness, grace period, preStop sleep and alert
# route. Prefer one semantic value to seven mechanical ones (03).
tier: 1

image:
  repository: registry.riverbend.internal/checkout-api
  # ⚠️ Set by CI as a DIGEST, not a tag. A tag is mutable and the kubelet re-resolves
  # it on every node replacement (09).
  digest: ""
  pullPolicy: IfNotPresent

# Ignored when autoscaling.enabled — the library omits spec.replicas in that case, so
# the HPA owns it uncontested. Rendering it would reset the count on every deploy (04 · R-02).
replicaCount: 2

autoscaling:
  enabled: true
  minReplicas: 6                    # survives an AZ loss with the library's spread constraints
  maxReplicas: 60
  targetCPUUtilizationPercentage: 65

# Requests are what the scheduler uses. Deliberately NO cpu limit — CPU limits throttle
# this service's p99 (see ../MicroservicesPOF, N-04). Memory limit == request.
resources:
  requests: { cpu: "200m", memory: "512Mi" }
  limits:   { memory: "512Mi" }

service:
  port: 8080

ingress:
  enabled: true
  subdomain: checkout                # host = subdomain + global.baseDomain, via printf (13 · Z-06)

serviceAccount:
  # The IRSA role ARN. Per-cluster, so it belongs in envs/20-*.yaml, never in a shared layer (10 · E-07).
  annotations: {}

# A MAP, not a list, so an override MERGES instead of replacing (03 · V-02).
extraEnv:
  OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel-collector.observability:4317"

secrets:
  # Paths into the secret store, NOT secret values. The chart contains no secret
  # material at all (12).
  storeName: ""
  pathPrefix: ""

probes:
  readiness:
    httpGet: { path: /readyz, port: 8080 }
    initialDelaySeconds: 20
    periodSeconds: 5
    failureThreshold: 6              # worst case 50s to ready; feeds the --timeout arithmetic
  liveness:
    httpGet: { path: /livez, port: 8080 }
    initialDelaySeconds: 30
    periodSeconds: 10
    failureThreshold: 3
```

### `values.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["tier", "image"],
  "properties": {
    "tier":         { "type": "integer", "enum": [1, 2, 3] },
    "replicaCount": { "type": "integer", "minimum": 0, "maximum": 200 },
    "image": {
      "type": "object",
      "additionalProperties": false,
      "required": ["repository"],
      "properties": {
        "repository": { "type": "string", "pattern": "^registry\\.riverbend\\.internal/" },
        "digest":     { "type": "string", "pattern": "^(sha256:[0-9a-f]{64})?$" },
        "tag":        { "type": "string", "not": { "const": "latest" } },
        "pullPolicy": { "enum": ["Always", "IfNotPresent", "Never"] }
      }
    },
    "resources": {
      "type": "object", "additionalProperties": false,
      "properties": {
        "requests": { "$ref": "#/$defs/rmap" },
        "limits":   { "$ref": "#/$defs/rmap" }
      }
    },
    "autoscaling": {
      "type": "object", "additionalProperties": false,
      "properties": {
        "enabled":     { "type": "boolean" },
        "minReplicas": { "type": "integer", "minimum": 1 },
        "maxReplicas": { "type": "integer", "minimum": 1 },
        "targetCPUUtilizationPercentage": { "type": "integer", "minimum": 1, "maximum": 100 }
      }
    },
    "service":  { "type": "object", "additionalProperties": false,
                  "properties": { "port": { "type": "integer", "minimum": 1, "maximum": 65535 } } },
    "ingress":  { "type": "object", "additionalProperties": false,
                  "properties": { "enabled": { "type": "boolean" },
                                  "subdomain": { "type": "string", "pattern": "^[a-z0-9-]{1,40}$" } } },
    "serviceAccount": { "type": "object", "additionalProperties": false,
                        "properties": { "annotations": { "type": "object" } } },
    "extraEnv": { "type": "object", "additionalProperties": { "type": "string" } },
    "secrets":  { "type": "object", "additionalProperties": false,
                  "properties": { "storeName": { "type": "string" },
                                  "pathPrefix": { "type": "string" } } },
    "probes":   { "type": "object" },
    "global":   { "type": "object" }
  },
  "$defs": {
    "rmap": {
      "type": "object", "additionalProperties": false,
      "properties": {
        "cpu":    { "type": "string", "pattern": "^[0-9]+m?$" },
        "memory": { "type": "string", "pattern": "^[0-9]+(Mi|Gi)$" }
      }
    }
  }
}
```

**The four lines doing the real work:**

- **`"additionalProperties": false`** — the highest-value line in any chart. Turns a
  silently-ignored typo into a build failure (03 · `V-01`).
- **`image.repository` pattern** — enforces the mirror, so an unmirrored upstream image cannot be
  configured (09 · `K-06`).
- **`image.tag` `"not": {"const": "latest"}`** — policy in a place that cannot be skipped.
- **`ingress.subdomain` pattern** — makes template syntax unrepresentable, which closes the `tpl`
  injection path (13 · `Z-06`).

⚠️ Note `"global": { "type": "object" }` with no `additionalProperties: false`. The parent cannot
enumerate what subcharts put under `global`, so constraining it breaks them. This is the one place
the strictness has to be relaxed.

### `templates/`

```yaml
# templates/deployment.yaml
{{- include "riverbend-common.deployment" . }}
```

```yaml
# templates/migrate-rbac.yaml  — hooks cannot use what the manifest creates (06)
{{- include "riverbend-common.migrateRBAC" . }}
```

```yaml
# templates/migrate-job.yaml
{{- if .Values.migrations.enabled }}
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "riverbend-common.fullname" . }}-migrate
  labels: {{- include "riverbend-common.labels" . | nindent 4 }}
  annotations:
    "helm.sh/hook": pre-upgrade
    "helm.sh/hook-weight": "-5"
    # before-hook-creation: re-runnable (a Job's template is immutable).
    # hook-succeeded: no accumulation.
    # ⚠️ NO hook-failed — a deleted failed Job takes its logs with it (06 · H-05).
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 2                   # >0 is a CLAIM that the migration is idempotent (06 · H-06)
  activeDeadlineSeconds: 600
  ttlSecondsAfterFinished: 86400    # backstop against H-01's orphan accumulation
  template:
    metadata:
      labels: {{- include "riverbend-common.selectorLabels" . | nindent 8 }}
    spec:
      restartPolicy: Never
      # No serviceAccountName with API permissions: the kubelet resolves secretKeyRef,
      # not the pod, so this hook needs no RBAC at all (13 · Z-05).
      containers:
        - name: migrate
          image: "{{ .Values.image.repository }}@{{ required "image.digest is required" .Values.image.digest }}"
          command: ["/app/checkout-api", "migrate", "up"]
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: {{ include "riverbend-common.fullname" . }}-db
                  key: url
{{- end }}
```

⚠️ That Job should also assert its own postcondition — `CS-6`'s lesson. A migration that exits 0
having applied nothing is indistinguishable from success:

```bash
# inside the migrate container, after `migrate up`
EXPECTED=$(cat /app/EXPECTED_SCHEMA_VERSION)   # baked in at image build time
ACTUAL=$(/app/checkout-api migrate version)
[ "$ACTUAL" = "$EXPECTED" ] || { echo "FAIL: schema $ACTUAL, build requires $EXPECTED"; exit 1; }
```

### `.helmignore`

```
.git/
.gitignore
.github/
ci/
tests/
testdata/
.idea/
*.swp
*.tgz
*.md
```

Every byte here is paid on every revision of every release (04, 15 · `S-04`).

## The three-tier model

The point of tiers is that **a low-stakes release should stay low-stakes.** Applying doc 14's full
pipeline to a chart that prints a report once a day is overhead with no return.

| | **Tier 3 — low stakes** | **Tier 2 — normal** | **Tier 1 — critical** |
|---|---|---|---|
| Example | `cert-expiry-audit` | `catalog-search` | `checkout-api`, `payments` |
| Chart | library templates, default values | library templates | library templates |
| `values.schema.json` | optional | **required** | **required** |
| `--atomic` | yes | yes | yes, except in freeze windows |
| `--timeout` | default 5m | computed | computed |
| `--history-max` | 3 | 5 | 5 |
| `helm diff` in PR | no | **yes** | **yes**, with the deletion gate |
| `kubeconform` | yes | yes | yes, all cluster versions |
| `--dry-run=server` | no | yes | yes |
| Upgrade test | no | on chart change | on chart change **and** on library change |
| Golden files | no | one environment | all tiers × 2 environments |
| Runbook | no | link only | **full** (doc 17 · `O-12`) |
| Promotion | dev → prod | dev → stg → prod | dev → stg → euw1 → use1, gated |
| Deploy ledger | yes | yes | yes |
| Inventory alerts | fleet-wide only | fleet-wide only | per-release |

⚠️ The tier is a value in the chart, not the environment (doc 10), so a tier-1 service is tier 1 in
dev too — which is what makes dev a useful test of a tier-1 deploy.

## Checklist — a new first-party chart

- [ ] `helm create` output **deleted**, not edited. Start from the golden chart above.
- [ ] `Chart.yaml`: `apiVersion: v2`, quoted `appVersion`, exact dependency versions, `kubeVersion`
      with the `-0` suffix, owner and runbook annotations.
- [ ] Templates are `include` of library templates. Any hand-written template is a deliberate
      opt-out with a comment saying why (08 · `D-12`).
- [ ] `values.yaml` under 25 leaf values. Each has a comment saying what breaks if you change it.
- [ ] `values.schema.json` with `additionalProperties: false`, and a CI test that it rejects a typo.
- [ ] `.helmignore` excludes `.git`, `.github`, `ci`, `tests`, `*.md`.
- [ ] No `randAlphaNum`, `genCA`, `genSignedCert`, `uuidv4`, or `lookup`-then-generate anywhere
      (02 · `T-11`, 11 · `G-03`).
- [ ] `spec.selector` uses only `app.kubernetes.io/name` and `/instance` (02 · `T-08`).
- [ ] `spec.replicas` omitted when `autoscaling.enabled` (04 · `R-02`).
- [ ] Release name under 40 characters.
- [ ] `helm template` output passes `kubeconform -strict` at every cluster version you run.
- [ ] `kubectl apply --dry-run=server` passes in a namespace with your real PSA label and quota.
- [ ] Duplicate-name check clean:
      `helm template … | grep -E '^kind:|^  name:' | paste - - | sort | uniq -d`
- [ ] Upgrade test passes: previous version installed, upgraded, object set unchanged, rollback
      works.
- [ ] Deploy identity's Role covers exactly the kinds this chart renders (13).
- [ ] For tier 1 and 2: `helm diff` posted on the PR; runbook created.

## Checklist — adopting a third-party chart

- [ ] Version pinned exactly. `Chart.lock` committed.
- [ ] **Run the audit** (09 · `K-09`) and paste the output into the pull request:
  - [ ] Kinds and counts
  - [ ] Cluster-scoped objects — `ClusterRole`, `ClusterRoleBinding`, CRDs, webhooks, `APIService`
  - [ ] Wildcard or `secrets`-granting RBAC
  - [ ] `hostPath`, `hostNetwork`, `hostPID`, `privileged`
  - [ ] Webhook `failurePolicy` — ⚠️ a `Fail` policy on `*/*` can break every write in the cluster
- [ ] Chart package under 1.5 MB, or `--skip-crds` **with** the CRD apply step in the same commit
      (04 · `R-05`, 07 · `C-04`).
- [ ] CRD strategy decided and written down: out of Helm if more than one release uses the kind.
- [ ] Every image the render references is mirrored, and the values pin **digests** (09 · `K-06`).
- [ ] Images the render does *not* expose — operator-constructed references — checked by reading the
      chart, and backed by an admission policy restricting registries.
- [ ] Hooks enumerated, and under a render-only GitOps model, the mapping checked (11 · `G-04`).
- [ ] Values file committed, with the non-default settings commented.
- [ ] Uninstall pre-flight understood: what survives, what cascades (17 · `O-06`).
- [ ] Signature verification enforced where the mechanism exists (09 · `K-07`, 11's Flux `verify:`).

## Checklist — a new environment or cluster

- [ ] `envs/20-<env>.yaml` created. **Only** cluster-specific facts: account ID, region, zones, role
      ARNs, storage class, hostnames, endpoints.
- [ ] No chart was edited. ⚠️ If a chart needed editing, it contains environment logic — fix that
      first (10 · `E-01`).
- [ ] The shared-layer grep is clean:
      `grep -nE 'arn:aws|[a-z]{2}-[a-z]+-[0-9][a-z]?|\.internal|[0-9]{12}' envs/00-base.yaml envs/10-*.yaml`
- [ ] Namespaces created with PSA labels, quota sized for **rollout** surge not steady state
      (13 · `Z-09`), and `riverbend.io/deployer` label.
- [ ] CRDs applied — `kubectl apply --server-side` — before any chart.
- [ ] Secret store configured; `ExternalSecret` paths resolve. Check every `ExternalSecret`'s status,
      not just the deploy's exit code (12 · `Y-05`).
- [ ] Deploy identity created with the derived Role, bound per namespace with a `RoleBinding`
      (13 · `Z-01`).
- [ ] Position in the promotion ladder decided and labelled. Smaller production clusters go **first**
      (10).
- [ ] The full bootstrap runs from empty in CI — which is the only thing that keeps this checklist
      honest (10 · `E-11`).
- [ ] Drift audit's exclusion list updated with this environment's intended differences
      (10 · `E-09`).
- [ ] `envs/DIFFERENCES.md` updated: every deliberate difference, with a reason and an owner.

## Checklist — upgrade day for a third-party chart

- [ ] Release notes read, specifically for CRD changes and removed values.
- [ ] `helm diff upgrade` reviewed, on every cluster.
- [ ] New chart's CRDs diffed against installed:
      `kubectl get crd <name> -o json | jq '.spec.versions[].name'`
- [ ] CRDs applied first, separately, with `--server-side`.
- [ ] New images mirrored; digests updated in the values.
- [ ] Chart package size checked (04 · `R-05`).
- [ ] Rollback pre-flight: previous images still present, previous manifest still applicable
      (16 · `I-06`):
  ```bash
  helm get manifest "$REL" -n "$NS" --revision "$((REV-1))" \
    | yq -r '.. | select(has("image")) | .image' 2>/dev/null | grep -v '^null$' | sort -u \
    | while read -r i; do crane manifest "$i" >/dev/null 2>&1 || echo "GONE: $i"; done
  helm get manifest "$REL" -n "$NS" --revision "$((REV-1))" | kubectl apply --dry-run=server -f -
  ```
- [ ] Non-production first, with a soak.
- [ ] ⚠️ **After the upgrade, verify the installed CRD versions** — `helm history` cannot tell you,
      because `crds/` is not in the release (18 · `CS-3`).

## Checklist — mid-incident

**Before touching anything:**

- [ ] Which of the three Helms is broken — templater, state store, lifecycle? (doc 00)
- [ ] **Who deploys this release?** Helm, Flux, or Argo CD render-only? If Argo, `helm history` is
      empty and `helm upgrade` is the wrong tool (11 · `G-01`).
- [ ] Is anything running right now? Release record age versus your longest timeout, plus a check of
      your CI (17, rule two).

**Orientation, in this order:**

```bash
NS=…; REL=…

# 1. What is serving traffic? Endpoints, not pods, is authoritative.
kubectl -n "$NS" get endpointslices -l "kubernetes.io/service-name=$REL" \
  -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}{" ready="}{.conditions.ready}{"\n"}{end}'

# 2. What changed? The ledger, or --description.
helm history "$REL" -n "$NS" --max 5

# 3. What state is the release in?
kubectl get secret -n "$NS" -l "owner=helm,name=$REL" \
  -o custom-columns='REV:.metadata.labels.version,STATUS:.metadata.labels.status,AT:.metadata.labels.modifiedAt'

# 4. What is the cluster saying?
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -30
kubectl -n "$NS" describe rs -l "app.kubernetes.io/instance=$REL" | sed -n '/Events/,$p'
```

⚠️ **Step 4's second command matters more than people expect.** Pod Security Admission and
ResourceQuota reject the *pod*, not the Deployment — so `helm upgrade` succeeds, the rollout silently
never progresses, and the real error is only in the ReplicaSet's events (13 · `Z-04`, `Z-09`).

**Then:** doc 17's triage tree, keyed on the error string.

**Restore service before fixing Helm's bookkeeping.** Scaling a known-good ReplicaSet directly is
legitimate during an outage, and must be written down because the next upgrade reconciles it away
(17 · `O-10`).

## The commands, in the order you need them

**Orientation**

```bash
helm list -A                                      # ⚠️ slow at scale; prefer the label query (15 · S-02)
helm list -n "$NS"
helm history "$REL" -n "$NS" --max 10
helm status "$REL" -n "$NS"
helm get values "$REL" -n "$NS" --all             # ⚠️ --all, or you see only the overrides
helm get manifest "$REL" -n "$NS"
helm get manifest "$REL" -n "$NS" --revision 46
helm get hooks "$REL" -n "$NS"
helm get metadata "$REL" -n "$NS" -o json         # ⚠ version 3.11+
```

**Fleet inventory, without the payload decode**

```bash
kubectl get secrets -A --field-selector type=helm.sh/release.v1 \
  -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.labels.name}{"\t"}{.metadata.labels.version}{"\t"}{.metadata.labels.status}{"\n"}{end}' \
  | sort -k1,1 -k2,2 -k3,3nr | awk '!seen[$1"/"$2]++' | column -t
```

**Read the raw release state**

```bash
helm-release-raw() {   # namespace release revision
  kubectl get secret -n "$1" "sh.helm.release.v1.$2.v$3" \
    -o jsonpath='{.data.release}' | base64 -d | base64 -d | gzip -d
}
helm-release-raw checkout checkout-api 47 | jq '.info'
```

**Before changing anything**

```bash
helm diff upgrade "$REL" "$CHART" -n "$NS" -f envs/00-base.yaml -f "envs/20-${ENV}.yaml"
helm template "$REL" "$CHART" -f "envs/20-${ENV}.yaml" --debug   # ⚠️ --debug prints output on failure
helm template "$REL" "$CHART" -f "envs/20-${ENV}.yaml" | kubectl apply --dry-run=server -f - -n "$NS"
```

**Recovery**

```bash
# pending-*: roll back to the last DEPLOYED revision, explicitly
LAST_GOOD=$(helm history "$REL" -n "$NS" -o json \
  | jq -r 'map(select(.status=="deployed")) | max_by(.revision) | .revision')
helm rollback "$REL" "$LAST_GOOD" -n "$NS" --wait --timeout 8m

# immutable field, no downtime
kubectl delete deploy -n "$NS" "$REL" --cascade=orphan && helm upgrade … --atomic

# adopt an existing object (read the diff FIRST — 17 · O-04)
kubectl label   "$KIND" -n "$NS" "$NAME" 'app.kubernetes.io/managed-by=Helm' --overwrite
kubectl annotate "$KIND" -n "$NS" "$NAME" \
  "meta.helm.sh/release-name=$REL" "meta.helm.sh/release-namespace=$NS" --overwrite
```

**Audits worth having as aliases**

```bash
# Contested objects — two deployers (04 · R-12, 11 · G-02)
kubectl get deploy,sts,ds,svc,cm -A -o json | jq -r '.items[]
  | select((.metadata.annotations // {}) | has("meta.helm.sh/release-name"))
  | select((.metadata.labels // {}) | has("argocd.argoproj.io/instance"))
  | "CONTESTED: \(.metadata.namespace)/\(.kind)/\(.metadata.name)"'

# Release state bytes (15 · S-01)
kubectl get secrets -A --field-selector type=helm.sh/release.v1 -o json \
  | jq '[.items[] | ((.data.release // "") | length) * 0.75] | add / 1048576 | floor'

# Orphaned hook Jobs (06 · H-01)
kubectl get jobs -A -l 'helm.sh/hook' \
  -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,AGE:.metadata.creationTimestamp' | sort -k3

# Unattached PVCs (18 · CS-7)
kubectl get pvc -A -o json | jq -r '.items[] | select(.status.phase=="Bound")
  | "\(.metadata.namespace)\t\(.metadata.name)\t\(.spec.resources.requests.storage)"' > /tmp/p
kubectl get pods -A -o json | jq -r '.items[] | .metadata.namespace as $n
  | (.spec.volumes // [])[] | select(.persistentVolumeClaim) | "\($n)\t\(.persistentVolumeClaim.claimName)"' \
  | sort -u > /tmp/u
awk -F'\t' 'NR==FNR{u[$1"\t"$2]=1;next} !($1"\t"$2 in u){print "UNATTACHED: " $0}' /tmp/u /tmp/p

# Charts that fight the HPA (04 · R-02)
for c in charts/*/; do
  grep -q 'autoscaling' "$c/values.yaml" 2>/dev/null || continue
  grep -rq 'if not .Values.autoscaling.enabled' "$c/templates/" \
    || echo "FIGHTS WITH HPA: $c"
done

# Non-deterministic templates — break under any reconciling controller (11 · G-03)
grep -rnE 'randAlpha|randNumeric|randAscii|genCA|genSelfSignedCert|genSignedCert|uuidv4' \
  charts/*/templates/

# Environment logic in charts (10 · E-01)
grep -rnE '(eq|ne)[[:space:]]+\.Values\.(global\.)?(environment|env|region|cluster|stage)' charts/
```

## The one-page summary

If you remember ten things from the collection:

1. **Helm is three tools** — a pure templater, an in-cluster state store, a lifecycle engine. Decide
   which one is broken before debugging.
2. **The release Secret is the state**, it contains the whole chart, and the authoritative status is
   inside the gzipped payload, not the label.
3. **`--wait` covers only the kinds Helm has code for.** Every custom resource is "ready" the instant
   it is stored.
4. **Rollback restores Kubernetes objects and five things it does not**: your database, CRDs, hook
   side effects, kept resources, and images that are gone.
5. **Every migration must be backward compatible with the running code.** Expand/contract makes the
   whole ordering question disappear.
6. **`crds/` is installed once and never touched.** You own the CRD lifecycle; take it out of Helm if
   more than one release uses the kind.
7. **`"additionalProperties": false` and `helm diff` in the pull request** are the two
   highest-value-per-effort things in the collection.
8. **A release is a failure domain.** Prefer many small releases to one umbrella, and use a real
   orchestrator for ordering.
9. **Never generate secrets in templates.** `lookup` returns empty on every render, so the pattern
   rotates credentials rather than preserving them.
10. **At fleet scale the binding constraint is legibility, not capacity.** Nobody can answer a basic
    question about 940 releases without an inventory, so build one.
