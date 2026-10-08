# Dependencies, Subcharts, and the Umbrella Decision

This doc is about composition: how charts include other charts, and the one architectural fork that
shapes everything else about how you run Helm — **one big release, or many small ones.**

That fork is worth stating as the thesis, because the rest of the doc is evidence for it:

> **A Helm release is a failure domain.** Everything in one release succeeds together, fails
> together, waits together, rolls back together, and is stored in one Secret together. So the
> question "should these things be one release?" is the question "should a failure in one of them
> affect the others?" — and for most collections of software, the answer is no.

And a corollary that is less obvious and matters as much:

> **Prefer a library chart to a subchart.** A subchart is a chart that renders itself, inside your
> release, with its own scoped values. A library chart is a set of templates your chart renders, in
> your context, with your values. The second has no value-scoping problem, no `global:` namespace to
> collide in, and no separate failure semantics — and for sharing templates across a fleet, it is
> what you actually want.

## Declaring a dependency

In `Chart.yaml`:

```yaml
apiVersion: v2
name: checkout-api
version: 4.11.3
dependencies:
  - name: riverbend-common
    version: "4.2.0"
    repository: "oci://registry.riverbend.internal/charts"

  - name: redis
    version: "~20.3.0"
    repository: "https://charts.bitnami.com/bitnami"
    condition: redis.enabled
    alias: sessionCache

  - name: postgresql
    version: "16.4.2"
    repository: "https://charts.bitnami.com/bitnami"
    tags:
      - database
    import-values:
      - child: service
        parent: db.service
```

Field by field, because four of these are routinely misused:

**`version`** takes a SemVer constraint, not just an exact version. `"4.2.0"` is exact.
`"~20.3.0"` allows patch updates (20.3.x). `"^20.3.0"` allows minor updates (20.x.x).
`">=20.0.0"` allows anything newer, including major versions. ⚠️ **Use exact versions.** The
argument for ranges is that you get fixes automatically; the reality is that you get an unreviewed
chart change at deploy time, and `D-03` is what that looks like.

**`repository`** can be an `https://` chart repository, an `oci://` registry, or
`"file://../riverbend-common"` for a chart in the same repository. The `file://` form is useful in a
monorepo and it ⚠️ does not work once the parent chart is packaged and published, because the
relative path does not survive.

**`condition`** is a *values path* which, if truthy, includes the subchart. If the path does not
exist, the subchart is **included** — so a typo in the condition path enables the subchart rather
than disabling it. `D-04`.

**`tags`** are named groups. `--set tags.database=false` disables every dependency tagged
`database`. Conditions beat tags where both apply.

**`alias`** renames the subchart for this release, which is how you get two copies of the same chart
in one release. With `alias: sessionCache`, the subchart's values live under `sessionCache:` rather
than `redis:`, and its object names derive from the alias.

**`import-values`** copies values *up* from the child to the parent, which is the only mechanism for
a parent to read a subchart's computed values. Obscure and occasionally exactly what you need.

## `Chart.lock` and `helm dependency update`

```bash
helm dependency update ./charts/checkout-api
```

This resolves every constraint against the repositories, downloads the resolved charts into
`charts/`, and writes `Chart.lock`:

```yaml
dependencies:
  - name: riverbend-common
    repository: oci://registry.riverbend.internal/charts
    version: 4.2.0
digest: sha256:6f2a3c91e0d8b47a1f5c8e2d9b0a7f34c1e8d5b2a9f6c3e0d7b4a1f8c5e2d9b0
generated: "2026-09-24T11:02:41.883421Z"
```

```bash
helm dependency build ./charts/checkout-api    # install exactly what Chart.lock says
```

**`update` resolves and can change versions. `build` installs the locked versions.** Same
distinction as `npm install` versus `npm ci`, and the same discipline applies:

- **Commit `Chart.lock`.**
- **Use `helm dependency build` in CI**, never `update`. `update` in CI means the chart you deploy
  is resolved at deploy time from whatever the repository currently offers, which is `D-03`.
- **Run `helm dependency update` deliberately**, as a change, in a pull request where the lock-file
  diff is reviewable.

⚠️ `helm dependency build` fails if `Chart.lock` is missing or does not satisfy `Chart.yaml`, which
is exactly the behaviour you want — it refuses rather than silently resolving.

### To vendor or not to vendor

Two choices for the `charts/` directory:

**Commit the resolved `.tgz` files.** Builds are hermetic — no network at build time, and the exact
bytes you reviewed are the bytes you deploy. Cost: your repository grows, diffs include binary
blobs, and doc 04's arithmetic says every vendored byte is paid for on every release revision. At
Riverbend the vendored `riverbend-common-4.2.0.tgz` is 39 KB, which is 73% of every service
release's state.

**Commit only `Chart.lock` and run `helm dependency build` in CI.** Smaller repository, and the
digest in the lock file still pins the exact content. Cost: a network dependency at build time, and
a repository outage stops deploys.

**Riverbend does not vendor, and mirrors its upstreams.** The reasoning: the lock file's digest
gives the same integrity guarantee as vendoring, an internal OCI registry mirror removes the
upstream-outage risk, and not vendoring removes 73% of release-state bytes. Doc 15, `S-04`, has the
measurement; doc 09 has the mirroring.

## Library charts — the mechanism, in full

A library chart has `type: library` and produces no output of its own:

```yaml
# charts/riverbend-common/Chart.yaml
apiVersion: v2
name: riverbend-common
version: 4.2.0
type: library
description: Shared templates for Riverbend services. Renders nothing on its own.
```

Its templates are all named-template definitions in `_*.tpl` files:

```yaml
{{/* charts/riverbend-common/templates/_deployment.tpl */}}
{{- define "riverbend-common.deployment" -}}
{{- $tier := include "riverbend-common.tierDefaults" . | fromYaml -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "riverbend-common.fullname" . }}
  labels:
    {{- include "riverbend-common.labels" . | nindent 4 }}
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
  revisionHistoryLimit: 3
  progressDeadlineSeconds: 420
  selector:
    matchLabels:
      {{- include "riverbend-common.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "riverbend-common.labels" . | nindent 8 }}
      annotations:
        checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
    spec:
      serviceAccountName: {{ include "riverbend-common.fullname" . }}
      terminationGracePeriodSeconds: {{ $tier.terminationGracePeriodSeconds }}
      priorityClassName: {{ $tier.priorityClassName }}
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        fsGroup: 65532
        seccompProfile: { type: RuntimeDefault }
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: {{ $tier.topologySpreadWhenUnsatisfiable }}
          labelSelector:
            matchLabels:
              {{- include "riverbend-common.selectorLabels" . | nindent 14 }}
      containers:
        - name: {{ .Chart.Name }}
          image: "{{ required "image.repository is required" .Values.image.repository }}:{{ required "image.tag is required" .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy | default "IfNotPresent" }}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          ports:
            - name: http
              containerPort: {{ .Values.service.port }}
          {{- with .Values.probes.readiness }}
          readinessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          env:
            {{- range $k, $v := .Values.extraEnv }}
            - name: {{ $k }}
              value: {{ $v | quote }}
            {{- end }}
          lifecycle:
            preStop:
              exec:
                command: ["/bin/sh", "-c", "sleep {{ $tier.preStopSleepSeconds }}"]
{{- end -}}
```

And the consuming chart's `templates/deployment.yaml` is one line:

```yaml
{{- include "riverbend-common.deployment" . }}
```

**The key property, and the reason to prefer this to a subchart:** `include` executes the template
with the context you pass it — `.` — which is the *parent's* context. So `.Values` inside
`riverbend-common.deployment` is the **service chart's** values, at the top level. No
`riverbend-common:` section, no scoping, no `global:`.

Compare against the subchart approach, where you would wrap the Deployment in a subchart. Then the
values live under `riverbend-common:`, the service's own `image.tag` is invisible to the subchart,
and you need `global.image.tag` or `import-values` to bridge — a whole layer of mechanism that the
library approach simply does not have.

⚠️ One consequence: a library chart is declared as a `dependency`, so it lands in `charts/` and is
subject to `Chart.lock`. It is a dependency in the packaging sense and not in the rendering sense.

### The `fromYaml` trick in that template

`{{- $tier := include "riverbend-common.tierDefaults" . | fromYaml -}}` is worth noticing. A named
template can only return a string, so to return *structured* data you emit YAML and parse it back.
That is how one value (`tier`) becomes seven derived settings (doc 03). It is the standard technique
for a helper that returns more than one thing, and it is not obvious from the documentation.

## The umbrella decision, with arithmetic

Riverbend faced this directly: 31 platform components. One release or 31?

### Option A — one umbrella release

```yaml
# charts/riverbend-platform/Chart.yaml
dependencies:
  - { name: cert-manager,     version: "1.16.2",  repository: "https://charts.jetstack.io" }
  - { name: ingress-nginx,    version: "4.11.3",  repository: "https://kubernetes.github.io/ingress-nginx" }
  - { name: external-dns,     version: "8.7.1",   repository: "https://kubernetes-sigs.github.io/external-dns" }
  - { name: external-secrets, version: "0.10.4",  repository: "https://charts.external-secrets.io" }
  - { name: kube-prometheus-stack, version: "66.2.1", repository: "https://prometheus-community.github.io/helm-charts" }
  # ... 26 more
```

What this produced, measured:

| Property | Value | Consequence |
|---|---|---|
| Vendored chart bytes | 9.8 MB | Release Secret ~1.4 MB — **over the 1 MiB limit, install failed** (`R-05`) |
| Rendered objects | 1,340 | `helm template` takes 41 seconds |
| `global:` keys in use across subcharts | 17 | Six subcharts reading `global.imagePullSecrets`, three reading `global.storageClass`, with different meanings (`V-08`) |
| `--wait` duration | bounded by the slowest component | `kube-prometheus-stack` takes 4m10s, so every platform change waits at least that long |
| Failure domain | all 31 | A bad `external-dns` value blocks a cert-manager upgrade |
| `--atomic` blast radius | all 31 | One failure rolls back the 30 that worked |

The size problem was the one that stopped it working, and it is the least interesting. The failure
domain was the one that mattered.

### Option B — a release per component, orchestrated

```yaml
# helmfile.yaml
repositories:
  - name: jetstack
    url: https://charts.jetstack.io
  - name: prometheus-community
    url: https://prometheus-community.github.io/helm-charts

releases:
  - name: cert-manager
    namespace: cert-manager
    chart: jetstack/cert-manager
    version: 1.16.2
    createNamespace: true
    values: [envs/{{ .Environment.Name }}/cert-manager.yaml]
    atomic: true
    timeout: 300

  - name: external-dns
    namespace: external-dns
    chart: external-dns/external-dns
    version: 8.7.1
    needs: [cert-manager/cert-manager]      # explicit ordering
    values: [envs/{{ .Environment.Name }}/external-dns.yaml]
    atomic: true

  - name: kube-prometheus-stack
    namespace: observability
    chart: prometheus-community/kube-prometheus-stack
    version: 66.2.1
    skipDeps: false
    values: [envs/{{ .Environment.Name }}/kps.yaml]
    atomic: true
    timeout: 600
```

```bash
helmfile -e prod-use1 apply
```

| Property | Value |
|---|---|
| Largest release Secret | 180 KB (`kube-prometheus-stack` with `--skip-crds`) |
| Failure domain | one component |
| Ordering | explicit via `needs:`, which the umbrella could not express at all |
| Per-component timeout | yes — 5 minutes for cert-manager, 10 for the monitoring stack |
| `global:` namespace | gone. Each release has its own values |
| "One command for a new cluster" | still one command |

**Riverbend chose B.** The decisive argument was `needs:`: the umbrella could not express "install
cert-manager before external-dns", because everything in one release is ordered only by the kind
sort (`T-13`). The thing an umbrella is supposed to give you — orchestration — is the one thing it
cannot do.

### When an umbrella *is* right

The test from doc 01, restated: **if component A fails to upgrade, should component B be rolled
back?** Yes for a genuinely atomic unit:

- A database StatefulSet, its backup CronJob, its Service, and its monitoring rules. These are one
  thing; a half-deployed one is not useful.
- An application and its sidecar-configuration ConfigMap.
- A set of objects that must change together to remain consistent — a webhook and the CA bundle
  that validates it.

No for things that merely happen to be installed at the same time, which is most of a platform.

## The `D-` failure catalogue

### D-01 · `Chart.lock` is out of date

**What you see.**

```
Error: the lock file (Chart.lock) is out of sync with the dependencies file (Chart.yaml).
Please update the dependencies
```

**Mechanism.** `helm dependency build` verified the digest of `Chart.yaml`'s dependency list
against the lock and they differ. Somebody edited `Chart.yaml` without re-running
`helm dependency update`.

**Recover.**

```bash
helm dependency update ./charts/checkout-api
git add charts/checkout-api/Chart.lock
```

**Prevent.** A CI check that the lock is current, which catches it in the pull request rather than
at deploy time:

```bash
cp charts/checkout-api/Chart.lock /tmp/lock.before
helm dependency update ./charts/checkout-api >/dev/null
diff -q /tmp/lock.before charts/checkout-api/Chart.lock \
  || { echo "Chart.lock is stale — run 'helm dependency update' and commit it"; exit 1; }
```

### D-02 · `found in Chart.yaml, but missing in charts/ directory`

**What you see.** That message, at render time.

**Mechanism.** `Chart.yaml` declares a dependency and `charts/` does not contain it. Either you did
not vendor and did not run `helm dependency build`, or the vendored `.tgz` is at a different version
than `Chart.yaml` asks for.

**Recover.** `helm dependency build ./charts/checkout-api`.

**Prevent.** ⚠️ Put `helm dependency build` in the deploy script rather than expecting people to
remember it. Riverbend's wrapper:

```bash
#!/usr/bin/env bash
set -euo pipefail
CHART=$1; REL=$2; NS=$3; ENV=$4

helm dependency build "$CHART"       # never `update` — the lock file is the truth
helm upgrade "$REL" "$CHART" --install -n "$NS" \
  -f "envs/${ENV}.yaml" --atomic --timeout 8m --history-max 5 --wait-for-jobs \
  --description "${CI_PIPELINE_URL:-manual-$(whoami)}"
```

### D-03 · A version range pulled in an unreviewed change

**What you see.** A deploy that changed something nobody changed. Or a render that produces
different output than it did yesterday from the same commit.

**Mechanism.** `version: "^20.3.0"` plus `helm dependency update` in CI. The upstream published
20.9.0 overnight; your deploy resolved to it; the chart now sets a different default.

At Riverbend this happened with a `^` on a Redis chart. The new minor version changed the default
`persistence.enabled` from false to true, which created a PVC, which held the pod pending on a
storage class that did not exist in that cluster, which timed out the deploy.

**Confirm it.** Compare the lock against what is in `charts/`:

```bash
yq -r '.dependencies[] | "\(.name)\t\(.version)"' charts/checkout-api/Chart.lock
ls -1 charts/checkout-api/charts/
```

and see what changed upstream:

```bash
helm search repo bitnami/redis --versions | head -20
```

**Recover.** Pin to the version you were on, and re-resolve deliberately.

**Prevent.** Two rules:

1. **Exact versions in `Chart.yaml`.** No `^`, no `~`, no `>=`. A dependency bump is a commit, with
   a diff, reviewed by a human.
2. **`helm dependency build` in CI, never `update`.** Even with exact versions, `update` re-resolves
   and re-downloads; `build` installs what the lock says.

A CI check for the first rule:

```bash
yq -r '.dependencies[]? | .version' charts/*/Chart.yaml \
  | grep -E '[\^~><*]|^$' && { echo "dependency version ranges are not allowed — see D-03"; exit 1; }
```

### D-04 · A `condition` typo enabled the subchart

**What you see.** A subchart you thought you had disabled is installed.

**Mechanism.** `condition: redis.enabled` checks a values path. **If the path does not exist, the
dependency is included.** That default is the opposite of what people expect, and it means
`condition: redis.enabld` — a typo in the condition itself — always includes the subchart.

Same for the values side: if the chart's `values.yaml` has no `redis:` section at all, the condition
path does not resolve and the subchart is on.

**Confirm it.** Check what a render actually contains:

```bash
helm template checkout-api ./charts/checkout-api -f envs/prod-use1.yaml \
  | yq -r 'select(.kind!=null) | .metadata.labels["app.kubernetes.io/name"] // "-"' \
  | sort | uniq -c
```

Any subchart's name appearing is that subchart being rendered.

**Prevent.** Always define the key in `values.yaml`, explicitly false, so the path exists:

```yaml
redis:
  enabled: false
```

And add it to the schema so a typo in the *values* is caught. The typo in the *condition string* is
caught only by the render check above, which is a good reason to have a golden-file test per chart
asserting which subcharts are present.

### D-05 · Two copies of the same chart

Not a failure — the mechanism, because it is not obvious.

`checkout-api` needs two Redis instances: one for sessions, one for rate limiting. Declare the
dependency twice with different aliases:

```yaml
dependencies:
  - name: redis
    version: "20.3.0"
    repository: "https://charts.bitnami.com/bitnami"
    alias: sessionCache
    condition: sessionCache.enabled
  - name: redis
    version: "20.3.0"
    repository: "https://charts.bitnami.com/bitnami"
    alias: rateLimitCache
    condition: rateLimitCache.enabled
```

```yaml
sessionCache:
  enabled: true
  architecture: replication
  replica: { replicaCount: 2 }
rateLimitCache:
  enabled: true
  architecture: standalone
  master: { persistence: { enabled: false } }
```

Objects are named from the alias, so `checkout-api-sessioncache-master` and
`checkout-api-ratelimitcache-master`. ⚠️ Verify the naming actually differentiates — a subchart that
hard-codes `.Chart.Name` rather than using the release-derived fullname will produce the same names
twice, and the second silently overwrites the first. Check with the duplicate detector from `T-06`.

### D-06 · `global:` collisions

Covered in doc 03, `V-08`. The one-line summary: `global` is a flat namespace shared by every chart
in the release including third-party subcharts, so an umbrella with 31 dependencies has 31 charts
reading and writing one map. **The most effective mitigation is not having an umbrella.**

### D-07 · Subchart value scoping

Covered in doc 03, `V-07`. The one-line summary: prefer library charts, whose templates execute in
the parent's context and have no scoping problem.

### D-08 · `import-values`, when you need a value to flow upward

**The problem.** The `postgresql` subchart computes its Service name from its own release-derived
fullname. Your parent chart needs to put that name in an environment variable. The parent cannot
call the subchart's templates, and the parent's values do not contain the computed result.

**The mechanism.**

```yaml
dependencies:
  - name: postgresql
    version: "16.4.2"
    repository: "https://charts.bitnami.com/bitnami"
    import-values:
      - child: service          # path in the CHILD's values
        parent: db.service      # where to put it in the PARENT's values
```

Now `.Values.db.service.ports.postgresql` is available in the parent's templates.

Two forms: the explicit `child`/`parent` mapping above, and the shorthand where the child chart
declares an `exports:` block and the parent lists the export name. The explicit form is clearer and
does not require cooperation from the child chart.

⚠️ `import-values` copies **values**, not computed template output. It cannot give you the
subchart's fullname, because that is produced by a template, not held in values. For a name, the
reliable approach is to compute it yourself in the parent using the same convention, or to set it
explicitly via the subchart's `fullnameOverride` so both sides know it:

```yaml
postgresql:
  fullnameOverride: checkout-api-db     # now the parent knows the name because it chose it
```

That last pattern is the one to reach for. It converts a value-flow problem into a naming decision,
which is easier to reason about and survives a subchart version bump.

### D-09 · One bad subchart blocked the whole release

**What you see.** A cert-manager upgrade that cannot proceed because an unrelated subchart's values
are invalid.

**Mechanism.** Rendering is all-or-nothing. A `required` failure, a schema violation, or a template
error in *any* subchart fails the whole render, so nothing in the release can be upgraded.

**Confirm it.** The error names the chart:

```
Error: execution error at (riverbend-platform/charts/external-dns/templates/deployment.yaml:34:20):
provider is required
```

**Recover.** Fix the value. There is no partial-render option.

**Prevent.** One release per component. This is the failure that made Riverbend split the umbrella,
and it is not fixable within the umbrella model.

### D-10 · `--wait` on an umbrella is bounded by the slowest component

**What you see.** Every platform change takes at least four minutes, even a one-line ConfigMap
change to a component that deploys in five seconds.

**Mechanism.** `--wait` waits for all resources in the release. So the timeout is the slowest
component's rollout, for every change, regardless of what changed.

**Prevent.** One release per component, with per-component timeouts.

### D-11 · A library-chart bump broke the fleet

**What you see.** Services deploying after a library release fail; services that have not deployed
are fine. So the damage accumulates across the day as teams deploy.

**Mechanism.** 240 charts depend on `riverbend-common`. A change to `_deployment.tpl` reaches a
service the next time that service deploys. Riverbend does 45 production upgrades a weekday, so a
bad library release has a blast radius of "everything that deploys today" and a detection time of
"whenever the first affected team notices".

This is the cost side of the library-chart pattern, and it is real.

**Confirm it.** Know which library version each release is on. This is the query that makes the
blast radius visible, and it needs the chart version to be recorded somewhere queryable:

```bash
helm list -A -o json | jq -r '.[] | "\(.namespace)\t\(.name)\t\(.chart)"' \
  | while IFS=$'\t' read -r ns name chart; do
      lib=$(helm get metadata "$name" -n "$ns" -o json 2>/dev/null | jq -r '.dependencies[]?
              | select(.name=="riverbend-common") | .version' 2>/dev/null)
      printf '%-20s %-30s %-24s common=%s\n' "$ns" "$name" "$chart" "${lib:-?}"
    done | sort -k4
```

⚠ **version**: `helm get metadata` arrived in Helm 3.11. On older versions, read the chart's
`Chart.lock` out of the release payload with doc 04's `helm-release-raw` function.

**Recover.** Publish a fixed library version and force a redeploy of everything already on the bad
one. The list from the query above is your work list.

**Prevent.** Treat the library chart as the highest-risk artefact you own, because it is:

1. **A canary tier.** Release the new library version and deploy it to three low-tier services
   first, on a schedule — a day of soak before anything tier-1 picks it up. Because services pick
   up the library version through their own `Chart.yaml` bump, this is naturally staged: bump the
   three canaries, wait, then bump the rest with a scripted pass.
2. **The most rigorous test suite of any chart.** Golden-file renders for a representative chart at
   every tier, across all four environments, compared against committed expected output. A library
   change that alters the render of any service is then visible as a diff in the pull request. Doc
   14, `Q-06`.
3. **An upgrade test**, not just a render test: install a service chart on library v4.2.0, then
   upgrade it to v4.3.0, in a throwaway cluster. That is what catches the immutable-field class
   (`U-07`), which is the way a library change does the most damage.
4. **Never a floating version.** Each service chart pins an exact library version, so a library
   release affects nobody until somebody bumps it. ⚠️ If service charts used `^4.2.0`, a library
   release would reach the entire fleet on the next deploy of each service with no commit anywhere
   — which is `D-03` with a fleet-sized blast radius.

### D-12 · The escape hatch problem

**What you see.** A service needs a Deployment shape the library does not support. The request
arrives as "can you add a value for X".

**Mechanism.** Every accepted request adds a value; values accumulate; the library becomes doc 03's
187-value chart with no contract.

**The choice**, and it is a design decision rather than a bug:

- **Add the value.** Correct when the need is general — more than one service will want it, or it
  represents a real platform-level choice. The test is whether you can imagine a second consumer.
- **Let the service opt out of that template.** Correct when the need is genuinely specific. The
  service stops calling `riverbend-common.deployment` and writes its own `deployment.yaml`, using
  the library's *smaller* helpers — `riverbend-common.labels`, `riverbend-common.selectorLabels`,
  `riverbend-common.tierDefaults` — so it keeps the shared conventions and owns its own shape.

The second option is the one people resist, because it feels like a failure of the abstraction. It
is not. **A library that can express every shape is a values-driven Deployment generator, which is
the thing you built the library to avoid.** Making divergence explicit and visible is better than
making it a configuration option.

This is why the library should be **several small templates plus a few large ones**, not one
monolithic template:

| Template | Granularity | Used by |
|---|---|---|
| `riverbend-common.labels`, `.selectorLabels`, `.fullname`, `.name` | Tiny. Pure conventions | Everything, including the opt-outs |
| `riverbend-common.tierDefaults` | Small. Returns a map | Everything |
| `riverbend-common.podSpec` | Medium. The container and pod-level settings | Services that need a custom workload kind |
| `riverbend-common.deployment`, `.service`, `.hpa`, `.pdb` | Large. Whole objects | The 90% that fit the standard shape |

A service that needs a `StatefulSet` instead of a `Deployment` writes its own `statefulset.yaml`
using `riverbend-common.podSpec` and the label helpers. It gets every platform convention and none
of the wrong shape, and the library did not need a new value. At Riverbend, 14 of the 240 services
opt out of at least one large template this way, and that number being non-zero is a sign the
design is working.

## What to take away

1. **A Helm release is a failure domain.** "Should these be one release?" is "should a failure in
   one affect the others?" — and for a collection of platform components, the answer is no.
2. **Prefer a library chart to a subchart for shared templates.** A library template executes in the
   parent's context with the parent's values: no scoping, no `global:` namespace, no separate
   failure semantics.
3. **Exact dependency versions, `Chart.lock` committed, `helm dependency build` in CI.** A version
   range means an unreviewed chart change arrives at deploy time.
4. **`condition:` includes the subchart when the values path does not exist**, so a typo enables
   rather than disables. Always define the key explicitly as false.
5. **An umbrella chart cannot express ordering.** Everything in one release is ordered only by the
   kind sort, so the one thing an umbrella is supposed to give you is the thing it cannot do. Use
   Helmfile `needs:`, Flux `dependsOn`, or Argo sync waves.
6. **An umbrella's costs compound**: one failure domain, one timeout bounded by the slowest
   component, one shared `global:` namespace, one release Secret that may not fit, and
   `--atomic` rolling back the components that worked.
7. **`import-values` copies values, not template output.** For a subchart's object name, set
   `fullnameOverride` so you chose the name rather than trying to read it.
8. **The library chart is the highest-risk artefact you own.** Pin exact versions so a library
   release reaches nobody until somebody bumps it, canary it through low-tier services, and test the
   *upgrade* path and not just the render.
9. **Let services opt out of library templates.** A library that can express every shape has become
   a values-driven manifest generator, which is what you built it to avoid. Explicit divergence
   beats another configuration option, and a non-zero opt-out count is a sign the design is
   healthy.
10. **Build the library as small helpers plus large objects**, so a service with an unusual shape can
    keep the conventions and write its own workload.
