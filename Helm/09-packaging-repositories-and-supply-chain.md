# Packaging, Repositories, and the Supply Chain

A chart is a program that creates arbitrary Kubernetes objects using your credentials. Most
organisations install between ten and forty charts they have never read, from repositories they do
not control, resolved at deploy time.

That sentence is the whole doc. Everything below is either a mechanism you need in order to say
something narrower than that, or a failure that happens when you cannot.

The thesis:

> **"Pinned" is not one property, it is four**, and most teams have one or two of them. You need
> the *chart version* pinned, the *chart content* verified against a digest, the *images the chart
> references* pinned and mirrored, and the *repository* under your control. Pinning the version
> alone — which is what almost everyone does and calls it done — gives you none of the other three.

## Two kinds of repository, and why OCI won

### Classic HTTP repositories

A chart repository is a web server with an `index.yaml` and some `.tgz` files. That is genuinely
all it is:

```yaml
# index.yaml, abridged
apiVersion: v1
entries:
  cert-manager:
    - apiVersion: v2
      name: cert-manager
      version: 1.16.2
      appVersion: v1.16.2
      created: "2026-11-14T09:22:03.118Z"
      digest: 8e1f3c9a7b2d4e6f0a5c8b1d3e7f9a2c4b6d8e0f1a3c5b7d9e1f3a5c7b9d1e3f
      urls:
        - https://charts.jetstack.io/charts/cert-manager-v1.16.2.tgz
```

`helm repo add` fetches `index.yaml` and caches it locally. `helm repo update` re-fetches. `helm
search repo` searches the local cache — which is why your search results are stale until you
update, and why `helm install` of a version you know exists can fail with `chart "x" version "y"
not found` (`K-01`).

⚠️ **The `digest` field is in the index, and Helm verifies it on download.** So if you install by
exact version from an index you fetched, you do get content integrity — against the index. What you
do not get is any guarantee that the index entry itself was not changed, because the index is just
a file on a web server.

### OCI registries

Since Helm 3.8, charts are first-class OCI artefacts and live in a container registry:

```bash
helm push checkout-api-4.11.3.tgz oci://registry.riverbend.internal/charts
helm install checkout-api oci://registry.riverbend.internal/charts/checkout-api --version 4.11.3
helm pull oci://registry.riverbend.internal/charts/checkout-api --version 4.11.3
```

**Why this is better, concretely:**

- **No index.** The registry is queried directly, so there is no staleness and no
  `helm repo update`.
- **Content-addressed.** You can install by digest:
  `oci://registry.riverbend.internal/charts/checkout-api@sha256:...`. That is a genuine
  immutability guarantee, not a convention.
- **One artefact store.** The same registry, the same authentication, the same replication, the
  same retention policy, and the same vulnerability scanning as your images.
- **Signable with the tooling you already have.** `cosign sign` works on a chart artefact exactly
  as it does on an image.

**What it costs:**

- ⚠️ **`helm search repo` does not work.** There is no index to search, so discovery is whatever
  your registry's UI or API offers. For an internal registry this is fine; for browsing a public
  catalogue it is a real loss.
- Some older tooling assumes HTTP repositories. `helm repo add oci://...` is not a thing — OCI URLs
  are used directly, not added as repositories, which trips people up the first time.
- Registry authentication is per-registry (`helm registry login`), and getting that into CI is one
  more credential.

**Recommendation: OCI for everything you publish, and mirror everything you consume into OCI.** The
mirroring is the part that matters, and it is `K-11`.

## The four things to pin

### 1 — The chart version

```yaml
dependencies:
  - name: cert-manager
    version: "1.16.2"        # exact. Not ^1.16.2
```

Covered in doc 08, `D-03`. Necessary, insufficient.

### 2 — The chart content

Chart versions are **not immutable by protocol**. Nothing in the Helm repository specification
prevents a publisher from replacing `cert-manager-1.16.2.tgz` with different bytes and updating the
index digest to match. Most publishers do not. Some do, by accident, when a release pipeline is
re-run.

Two mechanisms to detect it:

**`Chart.lock`'s digest** covers the resolved dependency set of your own chart, and
`helm dependency build` verifies it. This is the cheapest protection and you get it for free by
committing the lock file.

**Provenance files.** `helm package --sign` produces a `.tgz.prov` alongside the `.tgz`, containing
the chart's `Chart.yaml`, a hash of the archive, and a PGP signature:

```bash
helm package ./charts/checkout-api --sign \
  --key 'Riverbend Platform <platform@riverbend.internal>' \
  --keyring ~/.gnupg/secring.gpg

helm install checkout-api ./checkout-api-4.11.3.tgz --verify --keyring ./riverbend-pub.gpg
```

⚠️ `--verify` is opt-in, and if you do not pass it, the `.prov` file is downloaded and ignored. A
signature nobody verifies is documentation. If you are going to sign, the verification has to be
enforced somewhere that cannot be skipped — which means a policy controller at admission, not a
flag in a script.

**In practice, cosign on OCI artefacts has largely displaced `.prov`**, because it uses keyless
signing with an OIDC identity (so there is no long-lived private key to manage), it integrates with
policy controllers that can enforce verification at admission time, and it is the same tooling as
your images:

```bash
# Sign, in CI, with the workflow's OIDC identity — no key material anywhere
cosign sign oci://registry.riverbend.internal/charts/checkout-api:4.11.3

# Verify, and mean it
cosign verify oci://registry.riverbend.internal/charts/checkout-api:4.11.3 \
  --certificate-identity-regexp 'https://github\.com/riverbend/charts/\.github/workflows/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

### 3 — The images the chart references

**This is the one everybody misses.** You pinned `cert-manager` to 1.16.2. Its `values.yaml`
contains:

```yaml
image:
  repository: quay.io/jetstack/cert-manager-controller
  tag: ""          # defaults to .Chart.AppVersion
```

So the image is `quay.io/jetstack/cert-manager-controller:v1.16.2` — a *tag*, from an external
registry, resolved at pod-start time by the kubelet. Pinning the chart pinned the chart. The thing
that actually runs is still a mutable reference to somebody else's registry.

Consequences, all of which happen:

- **The tag is mutable.** If the publisher re-pushes it, your pods run different code the next time
  they restart — which they do, on every node replacement.
- **The registry is a runtime dependency.** Your ability to scale up or recover from a node loss
  depends on `quay.io` being reachable. During an incident, when you most need to add capacity.
- **It bypasses your scanning.** Images pulled from external registries were never scanned by your
  pipeline.
- **Rate limits.** Public registries throttle, and a 380-node cluster rolling a DaemonSet is a lot
  of pulls from one egress IP.

**The fix is to mirror and rewrite**, and every serious chart supports it:

```yaml
# envs/prod-use1/cert-manager.yaml
image:
  repository: registry.riverbend.internal/mirror/jetstack/cert-manager-controller
  digest: sha256:4a1f8c2e9b7d5a3f0c6e8b1d4a7f2c9e5b8d1a4f7c0e3b6d9a2f5c8e1b4d7a0f
webhook:
  image:
    repository: registry.riverbend.internal/mirror/jetstack/cert-manager-webhook
    digest: sha256:...
cainjector:
  image:
    repository: registry.riverbend.internal/mirror/jetstack/cert-manager-cainjector
    digest: sha256:...
```

⚠️ Note `digest:` rather than `tag:`. Most well-maintained charts support a `digest` value, and
using it is the difference between "this version" and "these exact bytes". Charts that do not
support it, you can still handle with a digest-pinned repository reference
(`repo@sha256:...` in the `repository` field works on some charts and not others), or by rewriting
at admission with a mutating policy.

**Find every image a chart will pull**, before you install it:

```bash
helm template cert-manager jetstack/cert-manager --version 1.16.2 \
  -f envs/prod-use1/cert-manager.yaml \
  | yq -r '.. | select(has("image")) | .image' 2>/dev/null \
  | grep -v '^null$' | sort -u
```

⚠️ This finds images in `image:` fields. It does **not** find images in an operator's
configuration — the Prometheus Operator's `--prometheus-default-base-image` flag, an operator that
constructs an image reference at runtime, or an image referenced inside a ConfigMap. Those need
reading the chart. This is the honest limit of the automated check, and the reason the chart-review
checklist in doc 20 has a manual line item for it.

### 4 — The repository

The chart came from `https://charts.jetstack.io`. That is a domain you do not control, serving
files you do not control, to your deploy pipeline. Upstream repositories get restructured, change
their distribution terms, move to a different host, or stop publishing a chart line — and each of
those is a deploy pipeline that stops working, at a moment you did not choose.

**Mirror.** `K-11` has the procedure.

## The `K-` failure catalogue

### K-01 · `chart "x" version "y" not found`

**What you see.** That error for a version that definitely exists.

**Mechanism.** `helm search` and `helm install repo/chart` use the locally cached `index.yaml`. If
it predates the release you want, Helm does not know about it.

**Recover.** `helm repo update`.

**Prevent.** `helm repo update` before any install from an HTTP repository, in every script. Or use
OCI, where there is no cache and the problem does not exist.

### K-02 · The chart version was reused

**What you see.** The same chart version produces different output than it did last month. A
`Chart.lock` digest mismatch. Or `helm dependency build` failing with a digest error.

**Mechanism.** The publisher re-pushed a version. Chart versions are immutable by convention, not
by protocol.

**Confirm it.**

```bash
helm pull jetstack/cert-manager --version 1.16.2 -d /tmp/verify
sha256sum /tmp/verify/cert-manager-1.16.2.tgz
# Compare against the digest recorded when you first resolved it
yq -r '.dependencies[] | select(.name=="cert-manager") | .version' charts/riverbend-platform/Chart.lock
yq -r '.digest' charts/riverbend-platform/Chart.lock
```

**Prevent.** Mirror into your own registry with immutable tags enforced at the registry level, so a
re-push is rejected rather than accepted. Most registries support tag immutability as a repository
setting, and turning it on for your chart mirror is a one-time configuration change that removes
this whole failure.

### K-03 · An unpinned dependency changed

Doc 08, `D-03`. The prevention is exact versions plus `helm dependency build`.

### K-04 · The chart is not the chart you reviewed

**What you see.** Nothing. This is the failure you do not see.

**Mechanism.** A chart was reviewed at version 1.16.2 in a pull request, and the deploy resolved
1.16.2 from a repository that had since changed it — or the review happened on the *upstream* chart
and the deploy used a locally modified copy, or vice versa.

**Prevent.** The chain has to be unbroken, and each link is a specific mechanism:

| Link | Mechanism |
|---|---|
| The reviewed artefact is identified | Exact version in `Chart.yaml`, committed |
| The resolved artefact matches | `Chart.lock` digest, `helm dependency build` |
| The artefact came from who you think | cosign verify with an identity constraint |
| Nothing changed it in transit | The registry's own digest, which OCI gives you |
| The deployed output matches the reviewed output | `helm diff` in the pull request (doc 14, `Q-09`) |

That last one is the most valuable and the least technical. **Reviewing the rendered diff rather
than the chart change** means you are reviewing what will actually happen, which catches a changed
upstream even if every other link failed.

### K-05 · The upstream repository changed underneath you

**What you see.** `helm repo update` fails, or a chart line stops receiving updates, or the images a
chart references move to a registry with different access terms.

**Mechanism.** You depend on somebody else's distribution decisions. This is not a hypothetical
class — upstream projects reorganise their chart repositories, change hosting, deprecate chart
lines in favour of an operator, and revise the terms under which their prebuilt images are
available. Any of those lands on you as a broken pipeline.

**Prevent.** Mirror (`K-11`). The point of a mirror is not only availability; it is that **an
upstream change becomes a decision you make on your own schedule** rather than an outage you
discover.

### K-06 · The chart installed and the pods cannot pull

**What you see.** `ImagePullBackOff` after a successful `helm upgrade`.

**Mechanism.** You mirrored the chart and not the images, or you mirrored the images the chart's
defaults reference and the chart also pulls a sidecar, an init container, or a `kube-webhook-certgen`
image you did not notice.

**Confirm it.**

```bash
kubectl get pods -A -o json | jq -r '.items[]
  | select(.status.containerStatuses[]?.state.waiting.reason == "ImagePullBackOff"
        or .status.initContainerStatuses[]?.state.waiting.reason == "ImagePullBackOff")
  | "\(.metadata.namespace)/\(.metadata.name)\t\(.spec.containers[].image)"'
```

**Prevent.** The render-and-extract check above, run in CI, asserting every image is in your
mirror:

```bash
helm template "$REL" "$CHART" --version "$VER" -f "$VALUES" \
  | yq -r '.. | select(has("image")) | .image' 2>/dev/null | grep -v '^null$' | sort -u \
  | while read -r img; do
      case "$img" in
        registry.riverbend.internal/*) crane manifest "$img" >/dev/null 2>&1 \
            || echo "NOT IN MIRROR: $img" ;;
        *) echo "NOT MIRRORED AT ALL: $img" ;;
      esac
    done | tee /tmp/img-check
[ ! -s /tmp/img-check ] || exit 1
```

⚠️ And enforce it at admission as a backstop, because the CI check only covers images the render
exposes. A validating policy that rejects any pod whose image is not from
`registry.riverbend.internal` catches the operator-constructed references the render check cannot
see — and it fails at pod creation, which is loud.

### K-07 · Provenance files that nobody verifies

**What you see.** `.prov` files in your repository and no verification anywhere.

**Mechanism.** `--verify` is opt-in and was never added to the deploy script.

**Prevent.** Either verify in a place that cannot be skipped, or stop signing and be honest about
it. The place that cannot be skipped is admission control: a policy controller that rejects
workloads whose images lack a valid signature, plus a CI gate that rejects a chart artefact whose
signature does not verify against an identity you expect. A flag in a shell script is not that
place.

### K-08 · OCI surprises

Three things that catch people moving from HTTP to OCI:

**There is no `helm repo add` for OCI.** You use the URL directly. `helm registry login` handles
credentials.

**The chart name is part of the URL, not a separate argument.**

```bash
# HTTP
helm install checkout-api riverbend/checkout-api --version 4.11.3
# OCI — the chart name is in the path
helm install checkout-api oci://registry.riverbend.internal/charts/checkout-api --version 4.11.3
```

**`helm search repo` finds nothing.** Discovery is your registry's problem. For an internal
registry, a listing in a README or a catalogue page is usually enough; expect to build something
small if you have many charts.

### K-09 · A chart created something you did not expect

**What you see.** A `ClusterRoleBinding` to `cluster-admin`. A `DaemonSet` with `hostPath` mounts. A
`ValidatingWebhookConfiguration` with `failurePolicy: Fail` on `*/*` — which, if its backing pod
dies, breaks every write in the cluster.

**Mechanism.** A chart renders arbitrary manifests and Helm applies them with your credentials.
There is no sandbox, no capability declaration, and no manifest of what a chart intends to create.

**Confirm it — the pre-install review, which takes two minutes and should be mandatory for any new
third-party chart:**

```bash
CHART=prometheus-community/kube-prometheus-stack; VER=66.2.1; VALUES=envs/prod-use1/kps.yaml
helm template audit "$CHART" --version "$VER" -f "$VALUES" > /tmp/audit.yaml

echo "== what kinds, how many =="
yq -r 'select(.kind!=null) | .kind' /tmp/audit.yaml | sort | uniq -c | sort -rn

echo "== cluster-scoped objects =="
yq -r 'select(.kind=="ClusterRole" or .kind=="ClusterRoleBinding" or
              .kind=="CustomResourceDefinition" or .kind=="ValidatingWebhookConfiguration" or
              .kind=="MutatingWebhookConfiguration" or .kind=="APIService" or
              .kind=="PriorityClass" or .kind=="StorageClass")
       | "\(.kind)/\(.metadata.name)"' /tmp/audit.yaml

echo "== wildcard or escalating RBAC =="
yq -r 'select(.kind=="ClusterRole") | .metadata.name as $n
       | .rules[]? | select((.verbs // [] | index("*")) or
                            (.resources // [] | index("*")) or
                            (.apiGroups // [] | index("*")) or
                            (.resources // [] | index("secrets")))
       | "\($n): \(.apiGroups) \(.resources) \(.verbs)"' /tmp/audit.yaml

echo "== host access and privilege =="
yq -r 'select(.spec.template.spec != null) | .metadata.name as $n | .spec.template.spec
       | select(.hostNetwork == true or .hostPID == true or .hostIPC == true or
                (.volumes // [] | map(select(has("hostPath"))) | length > 0) or
                (.containers // [] | map(select(.securityContext.privileged == true)) | length > 0))
       | "\($n): hostNetwork=\(.hostNetwork) hostPID=\(.hostPID) privileged/hostPath present"' /tmp/audit.yaml

echo "== webhooks that can break the cluster =="
yq -r 'select(.kind=="ValidatingWebhookConfiguration" or .kind=="MutatingWebhookConfiguration")
       | .metadata.name as $n | .webhooks[]
       | "\($n)/\(.name): failurePolicy=\(.failurePolicy // "Fail") rules=\([.rules[]? | "\(.apiGroups)/\(.resources)"] | join(","))"' /tmp/audit.yaml
```

Run that once per new chart and once per major version bump. It is the highest-value thing in this
doc relative to how long it takes, and the "webhooks that can break the cluster" section in
particular has caught genuinely dangerous defaults.

**Prevent.** Doc 13, and admission policy as the real control — a Gatekeeper or Kyverno rule that
rejects a `ClusterRoleBinding` to `cluster-admin` regardless of which chart asked for it.

### K-10 · `appVersion` and the deployed image disagree

**What you see.** `helm list` says `APP VERSION 1.16.2` and the running pods are on `1.15.3`.

**Mechanism.** `appVersion` is metadata. The image tag is a value. A chart whose values override
the image tag — which yours does, because CI sets it — makes `appVersion` a lie.

**Confirm it.**

```bash
helm list -A -o json | jq -r '.[] | "\(.namespace)\t\(.name)\t\(.app_version)"' \
  | while IFS=$'\t' read -r ns name av; do
      imgs=$(kubectl get pods -n "$ns" -l "app.kubernetes.io/instance=$name" \
        -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' 2>/dev/null \
        | sort -u | tr '\n' ' ')
      printf '%-18s %-28s app_version=%-14s running=%s\n' "$ns" "$name" "$av" "$imgs"
    done
```

**Prevent.** Decide which one is authoritative and make the other derive from it. The cleanest is
to let the image tag be the truth and stop treating `appVersion` as meaningful for your own charts —
or, if you want them consistent, have CI set both:

```bash
helm upgrade "$REL" "$CHART" --install -n "$NS" \
  --set-string image.tag="$GIT_SHA" --app-version "$GIT_SHA" ...
```

⚠️ `--app-version` is a `helm package` flag, not an `upgrade` flag, so for an unpackaged chart the
practical approach is CI rewriting `Chart.yaml`'s `appVersion` before packaging. For a chart
deployed straight from a directory, accept that `appVersion` is the chart author's declaration and
read the pods for the truth.

### K-11 · Mirroring, including air-gapped

**The procedure**, which is short enough to be worth having written down.

**Step 1 — mirror the chart into your registry.**

```bash
VER=1.16.2
helm pull jetstack/cert-manager --version "$VER" -d /tmp/mirror
helm push "/tmp/mirror/cert-manager-${VER}.tgz" oci://registry.riverbend.internal/charts/mirror
```

**Step 2 — enumerate and mirror the images.**

```bash
helm template x "/tmp/mirror/cert-manager-${VER}.tgz" \
  | yq -r '.. | select(has("image")) | .image' 2>/dev/null | grep -v '^null$' | sort -u \
  | while read -r src; do
      dst="registry.riverbend.internal/mirror/${src#*/}"
      crane copy "$src" "$dst"
      # record the digest, so values can pin bytes rather than a tag
      echo -e "${dst}\t$(crane digest "$dst")"
    done | tee "/tmp/mirror/cert-manager-${VER}.images"
```

⚠️ `crane copy` preserves the digest, so the mirrored image is byte-identical and its digest is the
same as upstream's. That is what makes the digest in your values file verifiable against the
upstream you reviewed.

**Step 3 — write the values file that points at the mirror**, using the digests from step 2.

**Step 4 — for a genuine air gap**, move everything across as archives:

```bash
# Outside
helm pull oci://registry.riverbend.internal/charts/mirror/cert-manager --version "$VER" -d ./bundle
while IFS=$'\t' read -r img dig; do
  crane pull "${img}@${dig}" "./bundle/$(echo "$img" | tr '/:' '__').tar"
done < "/tmp/mirror/cert-manager-${VER}.images"
tar czf "cert-manager-${VER}-bundle.tgz" ./bundle

# Inside
tar xzf "cert-manager-${VER}-bundle.tgz"
for t in bundle/*.tar; do crane push "$t" "airgap.internal/mirror/$(basename "$t" .tar)"; done
helm install cert-manager "./bundle/cert-manager-${VER}.tgz" -f values-airgap.yaml
```

**What to mirror, and when.** Mirror on the way in, as part of the pull request that adopts a chart
version — not lazily at deploy time, because then the mirror is on your critical path at the worst
moment. Riverbend's rule: a chart version bump is a pull request containing the new version in
`Chart.yaml`, the updated `Chart.lock`, the updated image digests in the environment values, and
the audit output from `K-09` pasted in the description. That makes adopting a new upstream version
a deliberate act with a reviewable record, which is the whole point.

## What to take away

1. **A chart is a program that creates arbitrary objects using your credentials.** There is no
   sandbox and no capability declaration. Everything else in this doc follows from that.
2. **"Pinned" is four properties, not one**: the chart version, the chart content digest, the images
   the chart references, and the repository. Pinning the version alone — the common case — gives you
   none of the other three.
3. **The images are the gap almost everyone has.** You pinned the chart; the thing that runs is
   still a mutable tag from somebody else's registry, resolved by the kubelet at pod start, on every
   node replacement, bypassing your scanning.
4. **Mirror on the way in, as a reviewed change** — not at deploy time. A mirror is not just
   availability; it converts an upstream change from an outage you discover into a decision you
   make.
5. **`crane copy` preserves digests**, so a mirrored image is verifiable against the upstream you
   reviewed, and your values file can pin bytes instead of a tag.
6. **Prefer OCI.** No index staleness, content-addressed installs, one artefact store with the same
   auth and retention as your images, and cosign works on it. The cost is that `helm search` no
   longer works, which matters for public browsing and not for an internal registry.
7. **A signature nobody verifies is documentation.** If you sign, enforce verification somewhere
   that cannot be skipped — a CI gate and an admission policy, not a flag in a script.
8. **Run the chart audit before adopting any third-party chart**, and on every major bump. Kinds,
   cluster-scoped objects, wildcard RBAC, host access, and webhook failure policies. Two minutes,
   and the webhook check alone justifies it.
9. **Review the rendered diff, not the chart version bump.** It is the one check that catches a
   changed upstream even when every other link in the chain failed.
10. **`appVersion` is metadata and your CI overrides the image tag**, so `helm list`'s app version
    is frequently a lie. Read the pods for the truth, or make CI set both.
