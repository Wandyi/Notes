# The Release as State — What Is Actually Stored, and What Depends On It

This is the doc that explains the most Helm behaviour per page. If you read only one more after
doc 00, read this one.

Helm's second tool is a state store, and almost nobody looks at it. The state lives in a Secret
whose name you have probably seen in a `kubectl get secrets` listing and scrolled past. Everything
confusing about Helm — why it refuses to adopt an object, why it reverts a field you edited by
hand, why a release "does not exist" while its pods are serving traffic, why `helm rollback` has a
limit, why your `etcd` is growing — is a consequence of what is in that Secret and how the upgrade
path uses it.

The thesis:

> **Helm has exactly two sources of truth: the release Secret and a live read of the API server.**
> It has no other memory. Everything else people attribute to Helm — knowing what it created,
> knowing what changed, knowing what to delete — is derived from those two by comparison. Once you
> can see that comparison, Helm stops being surprising.

## Opening the box

Install `checkout-api` and look at what appeared:

```bash
kubectl get secrets -n checkout -l owner=helm
```

```
NAME                                     TYPE                 DATA   AGE
sh.helm.release.v1.checkout-api.v45      helm.sh/release.v1   1      6d
sh.helm.release.v1.checkout-api.v46      helm.sh/release.v1   1      2d
sh.helm.release.v1.checkout-api.v47      helm.sh/release.v1   1      4h
```

Three revisions. The name decomposes as:

```
sh.helm.release.v1  .  checkout-api  .  v47
└── fixed prefix       └── release      └── revision
    (the ".v1" here        name             number
     is the STORAGE
     format version,
     not the revision)
```

⚠️ The `v1` in the prefix is the version of Helm's *storage format*, and the `v47` at the end is the
revision. Two different things that look the same, and people misread the prefix as revision 1.

The labels are how Helm finds these without listing every Secret in the namespace:

```bash
kubectl get secret -n checkout sh.helm.release.v1.checkout-api.v47 -o jsonpath='{.metadata.labels}' | jq
```

```json
{
  "modifiedAt": "1759241041",
  "name": "checkout-api",
  "owner": "helm",
  "status": "deployed",
  "version": "47"
}
```

⚠️ **The `status` label is a *copy*.** The authoritative status is inside the payload, at
`.info.status`. `helm list` reads the label because it is cheap; `helm upgrade` reads the payload.
When they disagree — which happens if somebody edits the label to "fix" a stuck release — you get a
release that looks healthy in `helm list` and still refuses to upgrade. This is the trap in `O-01`'s
most-cited internet fix, and doc 17 covers doing it correctly.

### Decoding the payload

The single `data` key is `release`, and getting at it requires three layers of unwrapping:

```bash
kubectl get secret -n checkout sh.helm.release.v1.checkout-api.v47 \
  -o jsonpath='{.data.release}' \
  | base64 -d \
  | base64 -d \
  | gzip -d \
  | jq 'keys'
```

```json
["chart", "config", "info", "manifest", "name", "namespace", "version"]
```

Why three layers? Working outwards:

1. **`jsonpath` gives you the Secret's `data` value, which Kubernetes base64-encodes** because
   Secret data is `[]byte` and JSON has no byte type. That is the first `base64 -d`, and every
   Secret in Kubernetes works this way.
2. **Helm base64-encodes its payload before putting it in the Secret.** That is the second
   `base64 -d`. Historically this was for compatibility with the `configmap` driver, where the
   value had to be a printable string — a ConfigMap's `data` is `map[string]string`, not bytes.
   Helm kept the encoding when it moved to Secrets so that both drivers store an identical payload.
   It costs 33% of the payload size, and doc 15 counts that cost across 7,240 revisions.
3. **The payload is gzip-compressed JSON.** That is the `gzip -d`, and it is what makes the whole
   thing fit at all.

Useful enough to make a function. Put this in your shell profile; you will use it during incidents:

```bash
helm-release-raw() {  # usage: helm-release-raw <namespace> <release> <revision>
  kubectl get secret -n "$1" "sh.helm.release.v1.$2.v$3" \
    -o jsonpath='{.data.release}' | base64 -d | base64 -d | gzip -d
}
```

### What each field is

| Field | Contents | Size at Riverbend | Why it matters |
|---|---|---|---|
| `name`, `namespace`, `version` | Strings | tiny | Identity |
| `info` | `status`, `first_deployed`, `last_deployed`, `description`, `notes` | ~1.2 KB | `status` is the authoritative one. `notes` is the rendered `NOTES.txt` |
| `config` | The **user-supplied** values only | ~3 KB | What `helm get values` prints without `--all` |
| `manifest` | The **rendered YAML**, post-sort, hooks excluded | ~16 KB | What `helm get manifest` prints. The "old manifest" side of the three-way merge |
| `chart` | **The entire chart**: `Chart.yaml`, `values.yaml`, every template file, every file in `charts/` and `crds/` | ~65 KB | The expensive one, and the surprising one |

**The `chart` field is the whole chart, embedded, in every revision.** This is the fact that
generates the most downstream consequences and the one nobody expects. Helm stores the chart so
that `helm rollback` works without needing the chart source — which is a genuinely valuable
property, because it means you can roll back a release whose chart has been deleted from the
repository. The cost is that every chart file you add is paid for on every revision of every
release, forever.

And the files inside `chart` are stored as base64-encoded byte arrays *within* the JSON, because
that is how Go serialises `[]byte`. So chart bytes are inflated 33% before the gzip, on top of the
33% inflation of the whole payload. Two rounds of base64.

You can see the breakdown for any release:

```bash
helm-release-raw checkout release checkout-api 47 \
  | jq '{
      manifest_bytes: (.manifest | length),
      config_bytes:   (.config   | tojson | length),
      notes_bytes:    (.info.notes // "" | length),
      chart_files:    (.chart.templates | length),
      chart_bytes:    ([.chart.templates[].data | length] | add),
      chart_values:   (.chart.values | tojson | length)
    }'
```

## Deriving the 28 KB, step by step

Every scale claim in doc 15 descends from one number: the bytes one release revision occupies in
`etcd`. Here it is, for a typical Riverbend service release, with each assumption named.

**Step 1 — the chart's files.** `checkout-api`'s chart, packaged:

| Part | Bytes |
|---|---|
| `Chart.yaml` | 420 |
| `Chart.lock` | 210 |
| `values.yaml` (with its comments) | 2,100 |
| `values.schema.json` | 3,400 |
| 9 files in `templates/` (thin — they just `include`) | 1,800 |
| `templates/NOTES.txt` | 640 |
| `charts/riverbend-common-4.2.0.tgz` (18 library templates, already gzipped) | 39,000 |
| **Total chart bytes** | **47,570** |

**Step 2 — base64 inflation of chart files inside the JSON.** Each file's content is a `[]byte`
field, base64-encoded when serialised: 47,570 × 4/3 = **63,427 bytes**.

**Step 3 — the rest of the payload.**

| Part | Bytes |
|---|---|
| Chart files, base64'd (step 2) | 63,427 |
| `manifest` — 11 rendered objects | 16,200 |
| `config` — user-supplied values as JSON | 3,050 |
| `info` including rendered notes | 1,240 |
| JSON structure, keys, escaping | ~2,100 |
| **Total JSON** | **86,017 bytes** |

**Step 4 — gzip.** The payload is highly repetitive: YAML keys recur, the label block appears in
every object, and the base64 of already-gzipped `.tgz` content is the only part that resists.
Measured compression on this shape is about **4.1:1**. Call it 86,017 ÷ 4.1 = **20,980 bytes**.

⚠️ This is the step where an assumption could be wrong for your charts, and it is the one to
re-measure rather than inherit. A chart whose bulk is an already-compressed `.tgz` dependency
compresses much worse than one whose bulk is plain-text templates. Measure it:

```bash
helm-release-raw checkout checkout-api 47 | wc -c          # uncompressed JSON
kubectl get secret -n checkout sh.helm.release.v1.checkout-api.v47 \
  -o jsonpath='{.data.release}' | base64 -d | wc -c        # what etcd stores
```

**Step 5 — base64 again, for the Secret.** 20,980 × 4/3 = **27,973 bytes**.

**Step 6 — Secret object overhead.** Metadata, labels, managed fields, type: roughly **900 bytes**.

**Total: 28,873 bytes ≈ 28 KB per revision.**

Two consequences, and they are why this arithmetic was worth doing:

**The chart is 73% of it.** 63,427 of 86,017 pre-compression bytes are chart files, and 39,000 of
those are the vendored library chart. So the single most effective way to shrink release state is
to shrink the chart — and specifically, to stop vendoring. Doc 15, `S-04`, turns this into a
concrete change and measures it.

**The manifest is 19%.** The rendered output of the chart — the part you would have guessed
dominates — is less than a fifth.

**Across Riverbend:** 7,240 revisions × 28 KB = **203 MB**. Against `etcd`'s practical 8 GB limit
that is 2.5%, which sounds fine. Against the cluster's *total* Secret bytes of 495 MB it is 41%,
which is the number that matters, because several things in a Kubernetes cluster list or watch all
Secrets. Doc 15, `S-01`.

## The three-way strategic merge, worked through

This is the mechanism behind "why did Helm change that" and "why did Helm *not* change that", and
it is worth doing once on a real object rather than reading a description of it.

On `helm upgrade`, for each object in the new manifest, Helm has three versions:

| Name | Where it comes from |
|---|---|
| **old** | The `manifest` field of the current release revision's Secret |
| **new** | What the chart just rendered |
| **live** | A fresh `GET` from the API server |

It computes a patch that moves **live** towards **new**, using **old** to decide what it is allowed
to touch. The rule, stated precisely:

> A field's value comes from **new** if new specifies it. A field present in **old** and absent
> from **new** is **removed**. A field present in **live** and in neither old nor new is **left
> alone**.

That last clause is the important one. It is what makes Helm coexist with other controllers.

### Example: three fields, three outcomes

`checkout-api`'s Deployment. Revision 46 (old) rendered:

```yaml
spec:
  replicas: 24
  template:
    spec:
      containers:
        - name: checkout-api
          image: registry.riverbend.internal/checkout-api:2026.09.14
          env:
            - name: LOG_LEVEL
              value: warn
```

Three things then happened before the upgrade to revision 47:

1. Someone ran `kubectl set env deploy/checkout-api LOG_LEVEL=debug` during an investigation.
2. The HorizontalPodAutoscaler scaled `spec.replicas` to 31.
3. A mutating webhook added an Istio sidecar container and an annotation.

So **live** is:

```yaml
metadata:
  annotations:
    sidecar.istio.io/status: '{"version":"...","containers":["istio-proxy"]}'
spec:
  replicas: 31                          # changed by the HPA
  template:
    spec:
      containers:
        - name: checkout-api
          image: registry.riverbend.internal/checkout-api:2026.09.14
          env:
            - name: LOG_LEVEL
              value: debug              # changed by a human
        - name: istio-proxy             # added by a webhook
          image: docker.io/istio/proxyv2:1.24.1
```

Revision 47's chart bumps only the image tag, so **new** is identical to old except
`image: ...:2026.09.17`.

Apply the rule field by field:

| Field | old | new | live | Outcome | Why |
|---|---|---|---|---|---|
| `image` | `:2026.09.14` | `:2026.09.17` | `:2026.09.14` | **`:2026.09.17`** | new specifies it |
| `env[LOG_LEVEL]` | `warn` | `warn` | `debug` | **`warn`** — the hand-edit is reverted | new specifies it, so new wins over live |
| `replicas` | `24` | `24` | `31` | **`24`** — you just scaled down mid-upgrade | new specifies it. ⚠️ See below |
| `istio-proxy` container | absent | absent | present | **kept** | in neither old nor new, so untouched |
| `sidecar.istio.io/status` | absent | absent | present | **kept** | same |

Four lessons, and the third one has caused real incidents.

**Hand-edits to fields the chart manages are reverted on the next upgrade.** This is correct and
desirable — it is how Helm keeps the cluster matching the chart — and it means `kubectl edit` is a
*temporary* intervention with an expiry date of "next deploy". If you edit something in an
incident, write it down, because it is coming back.

**Additions by other controllers survive**, because they are in neither manifest. This is what
makes sidecar injection, `kubectl annotate`, and `cluster-autoscaler`'s bookkeeping compatible with
Helm.

**⚠️ `replicas` is the dangerous case, and it has a specific fix.** If your chart sets
`spec.replicas` *and* you run an HPA, every `helm upgrade` resets the replica count to the chart's
value, then the HPA scales back up over the next few minutes. At Riverbend's peak this meant a
routine deploy dropped `checkout-api` from 31 replicas to 24 and took four minutes to recover,
during which latency rose. The fix is to **not render `replicas` at all when autoscaling is
enabled**:

```yaml
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
```

Now `replicas` is in neither old nor new, falls into the "leave alone" case, and the HPA owns it
uncontested. Every well-built chart does this, and it is worth grepping your fleet for the ones
that do not:

```bash
for c in charts/*/; do
  grep -q 'autoscaling.enabled' "$c/values.yaml" 2>/dev/null || continue
  grep -rq 'if not .Values.autoscaling.enabled' "$c/templates/" \
    || echo "FIGHTS WITH HPA: $c"
done
```

**It is a *strategic* merge, so lists with merge keys behave better than you would expect.**
Kubernetes types carry `patchMergeKey` metadata — `containers` merges on `name`, `env` merges on
`name`, `ports` merges on `containerPort`. So the Istio sidecar is not clobbered by a
container-list replacement; the lists are merged by key. ⚠️ This applies only to built-in types
whose Go structs carry the annotations. **CustomResources have no merge metadata, so Helm falls
back to a JSON merge patch, where a list is replaced wholesale.** That is why editing one element
of a list inside a CR and then upgrading loses your edit and possibly the whole list — and it is a
reason to be careful about hand-editing custom resources that a chart owns.

## The `R-` failure catalogue

### R-01 · "Error: release: not found" for something that is running

**What you see.**

```
$ helm upgrade checkout-api ./charts/checkout-api -n checkout
Error: UPGRADE FAILED: "checkout-api" has no deployed releases
```

or

```
$ helm history checkout-api -n checkout
Error: release: not found
```

...while `kubectl get deploy -n checkout` shows a happily running `checkout-api`.

**Mechanism.** Helm found no release Secret matching the name. Five causes, in descending
frequency:

1. **Wrong namespace.** `helm` without `-n` uses your kubeconfig context's namespace, which is
   `default` on a fresh kubeconfig. The release is in `checkout`.
2. **Wrong storage driver.** `$HELM_DRIVER` set to `configmap` or `memory` in the shell or in a CI
   image, so Helm is looking for ConfigMaps. `memory` is the worst, because it always finds nothing
   and silently succeeds at installing.
3. **The Secret was deleted** — by a cleanup job, an overzealous `kubectl delete secrets --all`, a
   namespace restore from backup that restored workloads but not Secrets, or a "remove unused
   Secrets" compliance sweep. `R-11`.
4. **All revisions are `failed`**, and the specific message `has no deployed releases` means Helm
   found the release but no revision with status `deployed` — so there is nothing to upgrade *from*.
   This is a different problem wearing the same shirt.
5. **The release was installed by something else** — Argo CD's render-only model creates no release
   Secret at all, so Helm is correct that there is no release. Doc 11, `G-01`. This one is not a
   failure; it is two tools disagreeing about who owns the deployment.

**Confirm it.** Work through the causes in order; this takes about thirty seconds:

```bash
echo "driver: ${HELM_DRIVER:-secret}"
kubectl get secrets -A -l owner=helm -o custom-columns=\
'NS:.metadata.namespace,NAME:.metadata.labels.name,REV:.metadata.labels.version,STATUS:.metadata.labels.status' \
  | grep -i checkout
kubectl get configmaps -A -l owner=helm 2>/dev/null | head    # cause 2
kubectl get deploy -n checkout checkout-api -o jsonpath='{.metadata.labels}' | jq   # cause 5
```

That last command is the decisive one for cause 5: if the Deployment has
`app.kubernetes.io/managed-by: Helm` and the annotations `meta.helm.sh/release-name` and
`meta.helm.sh/release-namespace`, then *something* installed it with Helm and the Secret is missing
(causes 1–3). If it has `argocd.argoproj.io/instance` instead, Argo CD owns it and you should not
be running `helm upgrade` at all.

**Recover.** Depends on the cause:

- Causes 1 and 2: pass `-n` and `--kube-context`, unset `HELM_DRIVER`. Nothing is broken.
- Cause 3, and the release Secret is genuinely gone: you must **re-adopt** the running objects.
  There is no `helm import`, so the procedure is to install with `--dry-run` to confirm the render
  matches, then stamp ownership metadata on every existing object, then install for real so Helm
  adopts rather than conflicts. Full procedure in doc 17, `O-06`; the short version is that Helm
  will adopt an object it finds already present **if and only if** the ownership metadata matches
  the release it is installing.
- Cause 4: `helm rollback` to the last revision that *was* deployed, or if there never was one,
  `helm uninstall --keep-history` and reinstall. Doc 17, `O-02`.

**Prevent.** Three things:

1. **Always pass `-n` explicitly**, in every script and every runbook. Never rely on context.
2. **Exclude `type=helm.sh/release.v1` from any Secret cleanup automation**, and say so in a
   comment in that automation. This is the single change that prevents cause 3.
3. **Back up release Secrets** with the same tooling you use for the rest of the cluster. Velero
   includes them by default; a hand-rolled "back up the important Secrets" script probably does
   not. A release Secret is 28 KB and it is the difference between `helm rollback` and a manual
   re-adoption of 11 objects under time pressure.

### R-02 · Helm reverted a field somebody edited by hand

**What you see.** A configuration change made with `kubectl edit` or `kubectl set env` disappeared
after the next deploy. Or: `replicas` dropped mid-afternoon with no scaling event.

**Mechanism.** The three-way merge, above. If the chart specifies the field, new beats live.

**Confirm it.** Ask what Helm thinks it owns:

```bash
helm get manifest checkout-api -n checkout \
  | yq 'select(.kind=="Deployment")' \
  | diff - <(kubectl get deploy -n checkout checkout-api -o yaml \
              | yq 'del(.metadata.managedFields, .metadata.resourceVersion, .metadata.uid,
                        .metadata.generation, .metadata.creationTimestamp, .status)')
```

Every difference is either a field a controller owns (fine), or a field you edited that will be
reverted (not fine), or a field Helm will try to change (also not fine). Reading this diff before a
deploy is the single most useful pre-deploy check that costs nothing.

**Recover.** Put the change in the chart, or accept it is temporary.

**Prevent.** Do not render fields another controller owns: `replicas` under an HPA (the guard
above), and `resources` if you run VPA in auto mode. Both are the same pattern — **omit the field
so it falls into the "leave alone" case** — and it is the only mechanism Helm offers for shared
ownership of an object.

### R-03 · The wrong storage driver

**What you see.** A release visible from your laptop and invisible from CI, or the reverse.

**Mechanism.** `$HELM_DRIVER` selects where release state lives: `secret` (default), `configmap`,
`memory`, or `sql` (PostgreSQL). Each looks in a different place and none of them migrates.

Reasons the non-default drivers exist, so you can judge whether you want one:

- **`configmap`** — the Helm 2 default. Only reason to use it now is compatibility with an old
  tool, and it is strictly worse: ConfigMaps are not encrypted at rest under an
  `EncryptionConfiguration` that covers only Secrets, and your release `config` may contain
  passwords (`Y-08`).
- **`memory`** — state lives only in the process. ⚠️ Every operation succeeds and nothing is
  recorded, so it is genuinely dangerous to set by accident. Its legitimate use is `helm template`
  -like experimentation and Helm's own tests.
- **`sql`** — release state in PostgreSQL instead of the cluster. Solves the 1 MiB limit (`R-05`)
  and the `etcd` growth problem (`S-01`) and introduces an external dependency on your deploy path:
  if the database is down, you cannot deploy or roll back. ⚠️ It is also markedly less used than
  the Secret driver, so you are on a less-travelled path. Worth considering only at a scale where
  `S-01` is genuinely hurting, and doc 15 argues that shrinking charts gets you further for less
  risk.

**Confirm it.**

```bash
env | grep -i helm
kubectl get cm -A -l owner=helm --no-headers 2>/dev/null | wc -l   # non-zero = configmap driver in use
```

**Prevent.** Pin it explicitly in CI — `HELM_DRIVER=secret` — rather than relying on the default,
so that a base-image change cannot alter it silently.

### R-04 · `--history-max` deleted the revision you wanted to roll back to

**What you see.**

```
$ helm rollback checkout-api 39 -n checkout
Error: release: not found
```

**Mechanism.** `helm upgrade --history-max N` keeps N revisions and deletes older ones. The default
is **10**. Riverbend does 45 production upgrades a weekday across 240 services, but a single busy
service can burn through ten revisions in a day — and a flapping release under `--atomic` burns
*two* per failed attempt (the failed upgrade, and the rollback).

⚠️ **A rollback creates a new revision, it does not reuse the old one.** So rolling back from 47 to
46 creates revision 48. This means an `--atomic` upgrade that fails three times in a row consumes
six revisions and, at `--history-max 5`, has destroyed every record of the version you were
actually running this morning.

**Confirm it.**

```bash
helm history checkout-api -n checkout --max 50
```

Look at the `REVISION` column: if the lowest number is not 1, revisions have been pruned. The
`DESCRIPTION` column tells you which were rollbacks.

**Recover.** The revision is gone and Helm cannot recreate it. What you can do:

- **Deploy the old version forward.** You know what it was from Git and from your image registry;
  `helm upgrade --set image.tag=<old>` with the old values file achieves the same end state. This
  is why doc 16 argues for a deploy ledger — an external record of "what was deployed when" that is
  not subject to Helm's pruning.
- If you have a `etcd` or Velero backup from before the pruning, the Secret is in it.

**Prevent.** The trade-off is real and worth stating rather than just picking a number.

| `--history-max` | Cost at Riverbend | Buys you |
|---|---|---|
| `0` (unlimited) | Unbounded `etcd` growth. Genuinely do not do this | Nothing you need |
| `10` (default) | 7,240 Secrets, 203 MB, 41% of Secret bytes | ~10 upgrades of rollback depth |
| `5` | ~3,900 Secrets, 109 MB | ~5 upgrades — enough, given that you almost never roll back more than one |
| `3` | ~2,400 Secrets, 67 MB | Enough for one bad deploy plus its retry. Tight under `--atomic` flapping |

**Riverbend settled on `5`**, with the reasoning that (a) the realistic rollback target is always
the immediately previous revision, (b) anything further back is better done as a forward deploy of
a known-good tag, and (c) 5 survives two `--atomic` failures without losing the last good revision.
⚠️ Set it on every `helm upgrade` invocation, because it is not persisted in the release — a single
upgrade without the flag reverts to keeping 10 and your pruning is undone.

### R-05 · The release Secret exceeds 1 MiB

**What you see.**

```
Error: create: failed to create: Secret "sh.helm.release.v1.riverbend-platform.v1"
is invalid: data: Too long: must have at most 1048576 bytes
```

Frequently reported against `kube-prometheus-stack`, and it is the real-world instance of this
problem.

**Mechanism.** A Kubernetes Secret's total data is limited to 1 MiB. The release payload is
compressed and base64'd, so 1 MiB of Secret holds roughly 3 MB of pre-compression JSON, which holds
roughly 2.2 MB of raw chart bytes at a 4:1 compression ratio — call it **a few megabytes of chart**
before you hit the wall. Which sounds like a lot until you look at what is in a big chart.

Riverbend's `riverbend-platform` umbrella was 31 vendored dependencies:

| Contributor | Chart bytes |
|---|---|
| `kube-prometheus-stack` including its `crds/` | 6,900,000 |
| 30 other charts | 2,400,000 |
| Umbrella's own templates and values | 500,000 |
| **Total** | **9,800,000** |

At the 4:1 ratio that is roughly 1.4 MB of Secret. Over the limit, so the install never completed.

⚠️ **CRDs dominate.** The Prometheus Operator's CRDs are enormous — they embed the full OpenAPI
schema of every Prometheus configuration option, which is tens of thousands of lines. And they are
in `crds/`, which Helm does not template but *does* store in the `chart` field of every revision.
So a chart you are storing purely for rollback purposes is 70% CRD schemas you can never roll back
anyway (doc 07: Helm does not touch `crds/` on rollback). You are paying, repeatedly, for something
with no value.

**Confirm it.** Before installing anything large, measure the chart:

```bash
helm pull prometheus-community/kube-prometheus-stack --version 66.2.1 --untar -d /tmp/kps
du -sh /tmp/kps/kube-prometheus-stack
du -sh /tmp/kps/kube-prometheus-stack/charts/*/crds /tmp/kps/kube-prometheus-stack/crds 2>/dev/null
```

Anything over about 2 MB of chart is worth checking; anything over 4 MB will probably fail.

**Recover, in order of preference.**

1. **`--skip-crds`, and apply the CRDs separately.** This is the answer, it is what the chart's own
   documentation recommends at scale, and it is what you should be doing anyway for the reasons in
   doc 07:
   ```bash
   # CRDs as their own, deliberately-versioned artefact
   kubectl apply --server-side -f https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/v0.78.2/example/prometheus-operator-crd/
   helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
     --install -n observability --version 66.2.1 --skip-crds -f values.yaml
   ```
   ⚠️ `--server-side` matters here: the CRDs exceed `kubectl apply`'s client-side annotation limit
   (the last-applied-configuration annotation itself hits the 262 KB annotation cap), which is the
   *other* famous error from these CRDs:
   `metadata.annotations: Too long: must have at most 262144 bytes`.
2. **Split the release.** One release per component instead of one umbrella. Riverbend's actual
   resolution, and doc 08 argues you wanted this regardless.
3. **Stop vendoring**, so the chart references its dependencies rather than embedding them. This
   trades the size problem for a network dependency at install time; doc 09 has the trade-off.
4. **Switch to the `sql` driver**, which has no 1 MiB limit. Solves this specific problem and adds
   an external dependency to your deploy path.

**Prevent.** A CI check on chart size, with the threshold set well below the wall:

```bash
helm dependency update ./charts/"$CHART" >/dev/null
sz=$(helm package ./charts/"$CHART" -d /tmp --version 0.0.0-ci | grep -o '/tmp/.*tgz' | xargs stat -f%z)
[ "$sz" -lt 1500000 ] || { echo "chart package ${sz}B exceeds 1.5MB; see R-05"; exit 1; }
```

### R-06 · `etcd` rejects the request as too large

**What you see.**

```
Error: UPGRADE FAILED: etcdserver: request is too large
```

**Mechanism.** Different limit, same shape. `etcd`'s default `--max-request-bytes` is **1.5 MiB**,
and the API server's own `--max-request-bytes` defaults to 3 MB but it cannot write more than
`etcd` accepts. So the 1 MiB Secret limit usually bites first, but a *manifest* with thousands of
objects can also produce an individual object too large to store, or an update whose request
exceeds the limit.

Also worth knowing: this is the same limit behind
`Request entity too large: limit is 3145728` when applying a giant manifest with `kubectl`.

**Recover / Prevent.** Same as `R-05`: fewer objects per release. Raising `etcd`'s
`--max-request-bytes` is possible on a self-managed control plane, is not on a managed one, and is
the wrong fix in both cases — a release too big for `etcd` is too big for a human to reason about.

### R-07 · "cannot be imported into the current release: invalid ownership metadata"

**What you see.**

```
Error: INSTALLATION FAILED: Unable to continue with install: ConfigMap "checkout-api-config"
in namespace "checkout" exists and cannot be imported into the current release: invalid
ownership metadata; label validation error: missing key "app.kubernetes.io/managed-by": must
be set to "Helm"; annotation validation error: missing key "meta.helm.sh/release-name": must
be set to "checkout-api"
```

**Mechanism.** Helm found an object it wants to create, already present, and not marked as
belonging to this release. Rather than overwrite it, it refuses. The check is:

```
labels["app.kubernetes.io/managed-by"]          == "Helm"
annotations["meta.helm.sh/release-name"]        == <this release's name>
annotations["meta.helm.sh/release-namespace"]   == <this release's namespace>
```

All three must match. Common causes:

- The object was created by `kubectl apply` before Helm existed in this namespace.
- It belongs to a *different* release — two charts both creating `default-ServiceAccount` or a
  shared `ConfigMap`, which is a chart design problem.
- A previous release was uninstalled with `--keep-history`, or uninstalled while an object carried
  `helm.sh/resource-policy: keep`, so the object outlived its release.
- The release name changed (`pr-4471` became `pr-4471-b`) while the objects kept the old owner.

**Confirm it.** Read the metadata on the object named in the error:

```bash
kubectl get configmap -n checkout checkout-api-config \
  -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}{"\n"}{.metadata.annotations.meta\.helm\.sh/release-name}{"\n"}{.metadata.annotations.meta\.helm\.sh/release-namespace}{"\n"}'
```

**Recover.** Two paths, and the choice is about whether you want Helm to own the object.

**Path A — adopt it** (you want Helm to manage this object going forward). Stamp the metadata, then
upgrade. Helm will then adopt and reconcile it:

```bash
kubectl label   configmap -n checkout checkout-api-config 'app.kubernetes.io/managed-by=Helm' --overwrite
kubectl annotate configmap -n checkout checkout-api-config \
  'meta.helm.sh/release-name=checkout-api' \
  'meta.helm.sh/release-namespace=checkout' --overwrite
helm upgrade checkout-api ./charts/checkout-api -n checkout -f envs/prod-use1.yaml
```

⚠️ **Look at the object's contents first.** Adoption means the next upgrade overwrites its data
with whatever the chart renders. If somebody hand-edited that ConfigMap with a value the chart does
not know about, adoption destroys it. `kubectl get -o yaml` and compare against
`helm template | yq` before you stamp anything.

There is also a flag: ⚠ **version** Helm 3.17 added `--take-ownership` to `install` and `upgrade`,
which performs the adoption for you. Convenient, and it makes the overwrite implicit rather than
explicit — which is exactly the step you want to be explicit about the first few times. Use the
manual path when the object might contain something you care about, and the flag when you are
certain it does not.

**Path B — exclude it** (you want the object to stay outside Helm). Remove it from the chart, or
guard it with a value:

```yaml
{{- if .Values.config.create }}
apiVersion: v1
kind: ConfigMap
...
{{- end }}
```

**Prevent.** One rule: **exactly one thing creates any given Kubernetes object.** When a chart and
a human both want to create a ConfigMap, that is an ownership question to settle in design, not an
error to work around at deploy time. A chart should also never create objects with names it does
not derive from the release name, because that is how two releases end up fighting over one object.

### R-08 · Adopting resources deliberately, at scale

Not a failure — the procedure, because it comes up whenever you migrate something into Helm.

You have 14 objects created by `kubectl apply` and you want a chart to own them. The safe sequence:

```bash
REL=checkout-api; NS=checkout; CHART=./charts/checkout-api; VALS=envs/prod-use1.yaml

# 1. Does the chart render what is already there? This is the gate. Fix the chart until it does.
helm template "$REL" "$CHART" -f "$VALS" -n "$NS" > /tmp/rendered.yaml
yq -r 'select(.kind!=null) | "\(.kind)/\(.metadata.name)"' /tmp/rendered.yaml | sort > /tmp/want.txt
cat /tmp/want.txt

# 2. For each object the chart wants, compare against what exists.
while read -r kn; do
  kubectl get "$kn" -n "$NS" >/dev/null 2>&1 || { echo "ABSENT (will be created): $kn"; continue; }
  diff <(yq "select(.kind==\"${kn%%/*}\" and .metadata.name==\"${kn##*/}\")" /tmp/rendered.yaml) \
       <(kubectl get "$kn" -n "$NS" -o yaml | yq 'del(.metadata.managedFields,.metadata.resourceVersion,
           .metadata.uid,.metadata.generation,.metadata.creationTimestamp,.status,
           .metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"])') \
    > "/tmp/diff-$(echo "$kn" | tr / -).txt" && echo "IDENTICAL: $kn" || echo "DIFFERS:   $kn"
done < /tmp/want.txt

# 3. READ every DIFFERS diff. Each one is a change the adoption will make to a live object.
# 4. Only then, stamp ownership on all of them.
while read -r kn; do
  kubectl label "$kn" -n "$NS" 'app.kubernetes.io/managed-by=Helm' --overwrite
  kubectl annotate "$kn" -n "$NS" "meta.helm.sh/release-name=$REL" \
    "meta.helm.sh/release-namespace=$NS" --overwrite
done < /tmp/want.txt

# 5. Install. Helm adopts rather than conflicts, and revision 1 records the current state.
helm upgrade "$REL" "$CHART" --install -n "$NS" -f "$VALS" --history-max 5
```

Step 3 is the whole procedure. Steps 4 and 5 are mechanical; step 3 is where you find out that the
chart sets `revisionHistoryLimit: 3` and the live Deployment has 10, and decide whether you care.

### R-09 · Two releases with the same name in different namespaces

**What you see.** `helm upgrade redis` upgraded the wrong one.

**Mechanism.** Helm 3 release names are namespace-scoped, deliberately. `redis` in `checkout` and
`redis` in `search` are unrelated releases. Without `-n`, Helm uses your kubeconfig's current
namespace.

**Prevent.** `-n` always. And ⚠️ note that `helm list` **without `-A` shows only the current
namespace**, which is the reason people believe a release does not exist. `helm list -A` is the
command you actually want, always.

### R-10 · `helm list` does not show a release that exists

**What you see.** `helm list -A` omits a release whose Secret you can see.

**Mechanism.** `helm list` filters by status. By default it shows `deployed` and `failed` and hides
`uninstalled`, `superseded`, and — the one that catches people — releases in `pending-*` states are
shown but a release whose *only* revisions are `superseded` is not.

**Confirm it.** Ask for everything:

```bash
helm list -A --all --all-namespaces -o json | jq -r '.[] | "\(.namespace)/\(.name)\t\(.status)"'
```

and cross-check against the ground truth, which is always the Secrets:

```bash
kubectl get secrets -A --field-selector type=helm.sh/release.v1 \
  -o custom-columns='NS:.metadata.namespace,REL:.metadata.labels.name,REV:.metadata.labels.version,STATUS:.metadata.labels.status' \
  | sort -u -k1,2
```

⚠️ **The `--field-selector type=helm.sh/release.v1` form is worth remembering**; it is much cheaper
than a label selector across a large cluster, and it is the canonical way to enumerate every Helm
release in a cluster regardless of what `helm list` thinks.

### R-11 · A cleanup job deleted release history

**What you see.** Releases disappearing from `helm list` over time. Rollback targets missing.

**Mechanism.** Something deletes Secrets. Real examples: a compliance job removing Secrets not
referenced by any pod (release Secrets are referenced by nothing); a cost-optimisation script
deleting objects untouched for 90 days; a Velero restore that excluded Secrets; a `kubectl delete
secret --all -n <ns>` during a cleanup.

**Prevent.** Exclude `type=helm.sh/release.v1` from every sweep, and write an alert that notices:

```promql
# Release-Secret count dropped by more than 10% in an hour without a corresponding uninstall
(
  sum(kube_secret_info{type="helm.sh/release.v1"})
  -
  sum(kube_secret_info{type="helm.sh/release.v1"} offset 1h)
) < -0.1 * sum(kube_secret_info{type="helm.sh/release.v1"} offset 1h)
```

⚠️ `kube-state-metrics` does not expose Secret `type` as a label by default; doc 16 covers the
release-inventory exporter that makes this and several other useful alerts possible.

### R-12 · Drift — the four ways state diverges

Not a single failure but the pattern behind several. There are four pairs that can disagree, and
each has a different detection method. Worth having the full map:

| Divergence | Cause | Detect with |
|---|---|---|
| **Git ≠ release Secret** | Someone ran `helm upgrade` from a laptop; a merged PR was never deployed | `helm diff upgrade` against the Git chart, as a scheduled job |
| **Release Secret ≠ live objects** | `kubectl edit`; an object deleted by hand; a controller changing a managed field | `helm get manifest` vs `kubectl get -o yaml`, per `R-02` |
| **Release Secret ≠ release Secret across environments** | Environment files drifted; a deploy skipped a region | Compare `helm get values --all` across clusters, per doc 10, `E-09` |
| **Helm ≠ the other deployer** | Argo CD and Helm both managing one release | Check for both `meta.helm.sh/*` and `argocd.argoproj.io/instance` on one object |

The first two are what `helm diff` and GitOps drift detection handle. The third is an audit nobody
runs, and doc 10 makes the case for running it. The fourth is doc 11's entire subject, and it is the
one that produces the most baffling behaviour, because two reconcilers with different opinions
produce an object that changes back and forth on a timer.

**One command that catches the fourth**, and it is worth running once on any cluster you inherited:

```bash
kubectl get deploy,sts,ds,svc,cm -A -o json | jq -r '
  .items[]
  | select((.metadata.annotations // {}) | has("meta.helm.sh/release-name"))
  | select((.metadata.annotations // {}) | has("argocd.argoproj.io/tracking-id") or
           ((.metadata.labels // {}) | has("argocd.argoproj.io/instance")))
  | "CONTESTED: \(.metadata.namespace)/\(.kind)/\(.metadata.name)"'
```

Any output is an object two systems believe they own.

### R-13 · Chart bloat is paid per revision

**What you see.** `etcd` growth that does not match your deploy volume.

**Mechanism.** The `chart` field is 73% of the payload (derived above), so a chart that grows by
100 KB costs 100 KB × (4/3) ÷ 4.1 × (4/3) ≈ 43 KB per revision, across every revision of every
release using it. Riverbend's `riverbend-common` grew from 12 KB to 39 KB over eighteen months as
templates accumulated; multiplied by 816 service releases at 5 revisions each, that one chart's
growth accounts for roughly 4,080 × 9 KB = **37 MB** of `etcd`.

**Prevent.** `.helmignore` aggressively (`T-12`), stop vendoring where you can (doc 09), and keep
CRDs out of charts (doc 07). Doc 15, `S-04`, works through the full reduction Riverbend achieved.

## What to take away

1. **Helm has two sources of truth: the release Secret and a live API read.** Everything else is
   derived by comparing them. Most confusing Helm behaviour is that comparison doing exactly what
   it is specified to do.
2. **`sh.helm.release.v1.<name>.v<n>` holds gzipped JSON, double-base64'd** — once by Helm for
   driver compatibility, once by Kubernetes for Secret data. The `status` label is a copy; the
   authoritative status is inside the payload, which is why editing the label does not unstick a
   release.
3. **The release stores the entire chart, in every revision.** That is what makes rollback work
   without the chart source, and it is 73% of the bytes. Shrinking the chart is the most effective
   lever on release-state size.
4. **28 KB per revision at Riverbend, 7,240 revisions, 203 MB — 41% of all Secret bytes in the
   cluster.** Re-derive it for your charts rather than inheriting the number; the compression ratio
   depends heavily on whether your bulk is plain text or an embedded `.tgz`.
5. **The three-way merge rule: new wins over live where new specifies a field; a field in neither
   manifest is left alone.** Hand-edits to chart-managed fields are reverted on the next upgrade;
   additions by webhooks and other controllers survive.
6. **Omit `replicas` when an HPA is enabled.** Otherwise every deploy resets the replica count and
   the HPA spends minutes recovering, which is a self-inflicted latency event on every deploy.
   The same pattern — omit the field — is the only way to share an object with another controller.
7. **Strategic merge works on built-in kinds and not on CustomResources**, where a list is replaced
   wholesale. Be careful hand-editing lists inside CRs that a chart owns.
8. **`--history-max 5` on every upgrade.** A rollback creates a new revision rather than reusing
   one, so `--atomic` flapping burns two revisions per attempt and can destroy the record of
   this morning's known-good state.
9. **The 1 MiB Secret limit is real and CRDs are what hits it.** `--skip-crds` plus separately
   applied CRDs with `kubectl apply --server-side` is the answer, and you wanted it anyway.
10. **Ownership metadata is three fields and all three must match.** Stamping them is how you adopt
    an existing object into a chart — and you should read the diff before you stamp, because
    adoption means the next upgrade overwrites the object's contents.
11. **Exclude `type=helm.sh/release.v1` from every Secret cleanup sweep**, and back release Secrets
    up. A 28 KB object is the difference between `helm rollback` and manually re-adopting eleven
    objects during an incident.
