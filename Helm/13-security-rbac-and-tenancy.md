# Security, RBAC, and Tenancy

Helm 3 removed Tiller, which fixed the worst security problem in Helm's history and created a
different one. The fix: Helm now runs as *you*, using your `kubeconfig`, making the same API calls
`kubectl` would. Whatever you cannot do, Helm cannot do for you.

The new problem follows immediately:

> **A chart is an arbitrary-Kubernetes-object-creation primitive that executes with the deployer's
> credentials.** So the real question is not "is this chart safe?" — you cannot answer that for a
> chart you have not read. The question is **"what can the identity that deploys it do?"** That is
> answerable, auditable, and the thing to control.

And a second thesis, about where the control belongs:

> **RBAC controls who can ask; admission control decides what may exist.** RBAC on a deploy
> identity is necessary and it is a blunt instrument — "can create ClusterRoleBinding" is a single
> bit. Admission policy expresses the actual rule you want: "no ClusterRoleBinding to
> `cluster-admin`, from anyone, ever." If you only do one of the two, do admission.

## What RBAC a deploy identity actually needs

Start from what `helm upgrade` does (doc 00, step by step) and derive the permissions.

**On the release Secret**, in the release namespace:

```yaml
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

⚠️ Note `delete` — Helm prunes revisions beyond `--history-max`. And note that this is a blanket
grant on Secrets in the namespace, because Helm's release Secrets are ordinary Secrets and
`resourceNames` cannot be used (the names contain a revision number you cannot enumerate in
advance). **So any Helm deploy identity can read every Secret in the namespaces it deploys to.**
That is unavoidable with the `secret` driver and is worth knowing.

**On every kind the chart creates**, in the release namespace. For a Riverbend service chart:

```yaml
- apiGroups: [""]
  resources: ["services", "serviceaccounts", "configmaps"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["apps"]
  resources: ["deployments", "statefulsets", "daemonsets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["autoscaling"]
  resources: ["horizontalpodautoscalers"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["policy"]
  resources: ["poddisruptionbudgets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["monitoring.coreos.com"]
  resources: ["servicemonitors", "prometheusrules"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["networking.k8s.io"]
  resources: ["networkpolicies", "ingresses"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["external-secrets.io"]
  resources: ["externalsecrets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

Plus the RBAC the hook needs to create (doc 06):

```yaml
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["roles", "rolebindings"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

⚠️ **That last block is a privilege-escalation surface, and Kubernetes has a specific protection
against it that is worth understanding.** The API server refuses to let you create a Role granting
permissions you do not yourself hold — unless you have the `escalate` verb on `roles`. So a deploy
identity limited to the kinds above cannot write itself a Role granting `secrets` cluster-wide,
because it does not hold that permission. The protection works.

**What it does not protect against:** the deploy identity *does* hold full access to its own
namespace, so it can create a Role granting all of that to a ServiceAccount, and bind it to a pod
it controls. That is not an escalation — it is exactly the permissions the identity already has —
but it means a compromised chart in that namespace inherits everything the deploy identity can do
in that namespace, including reading every Secret.

**Derive the list rather than guessing it.** For any chart, the kinds it needs are the kinds it
renders:

```bash
helm template checkout-api ./charts/checkout-api -f envs/20-prod-use1.yaml \
  | yq -r 'select(.kind!=null) | "\(.apiVersion)\t\(.kind)"' | sort -u \
  | awk -F'\t' '{ split($1,a,"/"); g = (a[2]=="" ? "\"\"" : a[1]); print g "\t" tolower($2) "s" }' \
  | sort -u | column -t
```

Generate the Role from that, review it, and commit it. A deploy identity whose Role was derived
from the charts it deploys is meaningfully different from one that was granted `edit` because that
was easier.

## The `Z-` failure catalogue

### Z-01 · The deploy identity has `cluster-admin`

**What you see.** A CI service account bound to `cluster-admin`, usually because a chart needed one
cluster-scoped object once.

**Mechanism.** The path is always the same: a chart fails with `cannot create resource
"clusterroles"`, somebody adds the permission, the next chart needs something else, and after four
iterations the binding is `cluster-admin` because enumerating was tiresome.

**Confirm it.** Enumerate what every deploy identity can do:

```bash
# Every ServiceAccount bound to a powerful ClusterRole
kubectl get clusterrolebindings -o json | jq -r '.items[]
  | select(.roleRef.name | test("^(cluster-admin|admin|edit)$"))
  | .roleRef.name as $r | (.subjects // [])[]
  | select(.kind=="ServiceAccount")
  | "\($r)\t\(.namespace)/\(.name)"' | sort -u | column -t
```

Then ask, for each row, whether that identity is a deploy identity — and if so, what it actually
needs.

⚠️ `kubectl auth can-i --list --as=system:serviceaccount:ci:deployer` is the authoritative answer
for a specific identity, and it is the command to use in a review:

```bash
kubectl auth can-i --list --as=system:serviceaccount:ci:deployer | head -40
kubectl auth can-i create clusterrolebindings --as=system:serviceaccount:ci:deployer
kubectl auth can-i get secrets -A --as=system:serviceaccount:ci:deployer
```

**Recover.** Derive the minimum set from the charts (the command above), grant it, and run the
deploys. Things will fail; each failure names a missing permission; add it deliberately. Expect two
or three iterations. Doing this in a throwaway cluster rather than in production is obviously
better, which is another use for doc 01's ephemeral environments.

**Prevent.** Two structural choices that matter more than the Role itself:

1. **Split the identity by what it deploys.** Riverbend has two: `ci-services`, which can deploy
   the 240 service charts and has no cluster-scoped permissions at all; and `platform-admin`, which
   can deploy the 31 platform charts and is used only from a pipeline requiring a human approval.
   The 45 daily service deploys run as the first, which means the blast radius of a compromised CI
   runner is one namespace's worth of workloads rather than the cluster.
2. **Namespace-scoped RoleBindings, not ClusterRoleBindings.** A `ClusterRole` can be bound with a
   `RoleBinding` to grant its rules in one namespace only — which is the pattern for "the same
   permission set, in 38 namespaces, and nowhere else":
   ```yaml
   # One ClusterRole defining the rule set, bound per namespace
   apiVersion: rbac.authorization.k8s.io/v1
   kind: RoleBinding
   metadata:
     name: ci-services-deploy
     namespace: checkout
   roleRef:
     apiGroup: rbac.authorization.k8s.io
     kind: ClusterRole          # the RULES are cluster-scoped
     name: riverbend-service-deployer
   subjects:
     - kind: ServiceAccount
       name: ci-services
       namespace: ci
   ```
   ⚠️ The distinction people miss: binding a `ClusterRole` with a `RoleBinding` scopes its rules to
   that namespace. It is a `ClusterRoleBinding` that makes them cluster-wide. So you can define the
   rule set once and grant it narrowly.

### Z-02 · A chart created a `ClusterRoleBinding` to `cluster-admin`

**What you see.** In an audit, months later.

**Mechanism.** Many third-party charts request broad cluster permissions, sometimes legitimately
(an operator that watches all namespaces genuinely needs cluster-scoped read) and sometimes because
the chart author took the easy path.

**Confirm it.** For what is installed:

```bash
kubectl get clusterrolebindings -o json | jq -r '.items[]
  | select(.roleRef.name=="cluster-admin")
  | "\(.metadata.name)\thelm-release=\(.metadata.annotations["meta.helm.sh/release-name"] // "-")\t\((.subjects // []) | map("\(.kind)/\(.namespace // "-")/\(.name)") | join(","))"' \
  | column -t
```

For a chart you are about to install, doc 09's `K-09` audit, whose "wildcard or escalating RBAC"
section exists exactly for this.

**Recover.** Whether you can narrow it depends on the chart. Many charts have a value for it
(`rbac.create`, `clusterRole.rules`, `installCRDs`); some do not, and then the options are a fork, a
post-renderer, or accepting it with a documented decision.

**Prevent.** Admission policy, which is the real control here because it applies to every chart
regardless of who wrote it. A Kyverno policy:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: no-cluster-admin-bindings
spec:
  validationFailureAction: Enforce
  rules:
    - name: block-cluster-admin
      match:
        any:
          - resources:
              kinds: ["ClusterRoleBinding", "RoleBinding"]
      exclude:
        any:
          - subjects:
              - kind: Group
                name: system:masters
      validate:
        message: >-
          Binding to cluster-admin is not permitted. Request a scoped ClusterRole
          via the platform team (see Helm/13-security-rbac-and-tenancy.md, Z-02).
        pattern:
          roleRef:
            name: "!cluster-admin"
    - name: block-wildcard-clusterroles
      match:
        any:
          - resources:
              kinds: ["ClusterRole"]
      validate:
        message: "ClusterRole with wildcard verbs on wildcard resources is not permitted."
        deny:
          conditions:
            all:
              - key: "{{ request.object.rules[?contains(verbs, '*')] | length(@) }}"
                operator: GreaterThan
                value: 0
              - key: "{{ request.object.rules[?contains(resources, '*')] | length(@) }}"
                operator: GreaterThan
                value: 0
```

⚠️ Deploy any new policy in `Audit` mode first, look at what it would have blocked, and only then
switch to `Enforce`. A policy switched straight to `Enforce` in a cluster with 31 third-party charts
will block something at the worst moment.

### Z-03 · A chart requested host access

**What you see.** A `DaemonSet` with `hostNetwork: true`, `hostPID: true`, a `hostPath` mount of
`/`, or a privileged container.

**Mechanism.** Log collectors, CNI plugins, CSI drivers, and node-exporters legitimately need some
of this. Other things ask for it because it was convenient.

At Lantern, the log-collector agent DaemonSet genuinely needs `hostPath` on `/var/log` and
`/var/lib/docker/containers` — read-only — and needs nothing else. A chart asking for `privileged:
true` and `hostPath: /` to do the same job is asking for far more than the task requires, and the
difference is worth pushing back on.

**Confirm it.** Doc 09's `K-09` audit has the check. For what is running:

```bash
kubectl get pods -A -o json | jq -r '.items[]
  | select(.spec.hostNetwork == true or .spec.hostPID == true or .spec.hostIPC == true
        or ((.spec.volumes // []) | map(select(has("hostPath"))) | length > 0)
        or ((.spec.containers // []) | map(select(.securityContext.privileged == true)) | length > 0))
  | "\(.metadata.namespace)/\(.metadata.name)\thostNet=\(.spec.hostNetwork // false)\thostPID=\(.spec.hostPID // false)\tpaths=\((.spec.volumes // []) | map(select(has("hostPath")) | .hostPath.path) | join(","))"' \
  | sort -u | column -t
```

**Prevent.** Pod Security Admission at the namespace level, which is the cheapest control available
because it is built in and needs no controller:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: checkout
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: v1.29
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

`restricted` for application namespaces; `privileged` only for the specific namespaces where a
node-level agent runs, with that exception written down and reviewed. `Z-04` is what happens next.

### Z-04 · Pod Security Admission rejected the chart

**What you see.**

```
Error: UPGRADE FAILED: admission webhook denied the request: pods "checkout-api-7d9f" is
forbidden: violates PodSecurity "restricted:v1.29": allowPrivilegeEscalation != false
(container "checkout-api" must set securityContext.allowPrivilegeEscalation=false),
unrestricted capabilities (container "checkout-api" must set
securityContext.capabilities.drop=["ALL"]), runAsNonRoot != true, seccompProfile
```

**Mechanism.** PSA's `restricted` profile requires five things most charts do not set by default.
⚠️ And note a timing subtlety: PSA validates the **pod**, not the Deployment. So `helm upgrade`
succeeds — the Deployment is valid — and the *ReplicaSet controller* then fails to create pods,
which surfaces as a rollout that never progresses with the real error buried in the ReplicaSet's
events rather than in Helm's output.

**Confirm it.** The error is on the ReplicaSet, not the Deployment:

```bash
kubectl -n checkout describe rs -l app.kubernetes.io/instance=checkout-api | sed -n '/Events/,$p'
```

And check a chart against a profile *before* installing, which is the useful preventive check:

```bash
helm template checkout-api ./charts/checkout-api -f envs/20-prod-use1.yaml \
  | kubectl apply --dry-run=server -f - -n checkout
```

⚠️ `--dry-run=server` runs the full admission chain including PSA and your policy controller, so it
is the only dry run that tells you whether the thing will actually be admitted. Worth having in CI
against a real cluster.

**Recover.** Set the fields. In the library chart, once, for all 240 services — which is the
library chart earning its keep:

```yaml
      securityContext:                    # pod level
        runAsNonRoot: true
        runAsUser: 65532
        fsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: {{ .Chart.Name }}
          securityContext:                # container level
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
```

**Prevent.** Set them in the library chart with no value to override them, so a new service cannot
be created without them. Then enable PSA in `warn` and `audit` mode first, read what it flags, fix
those, and only then move to `enforce`. Doing it the other way round means a cluster-wide outage the
next time anything deploys.

### Z-05 · Hook RBAC as a standing grant

**What you see.** In an RBAC audit, a `Role` granting `secrets: [get]` whose `RoleBinding` names a
ServiceAccount that exists only to run a migration Job — and both persist permanently, because doc
06's pattern uses `before-hook-creation` and not `hook-succeeded` on the RBAC.

**Mechanism.** Hook RBAC must outlive the hook, so it is permanent.

**Confirm it.**

```bash
kubectl get rolebindings -A -o json | jq -r '.items[]
  | select((.metadata.annotations // {}) | has("helm.sh/hook"))
  | "\(.metadata.namespace)/\(.metadata.name)\trole=\(.roleRef.name)"' | column -t
```

**Prevent.** Scope it as tightly as the mechanism allows, which is tighter than people bother with:

```yaml
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["checkout-api-db"]      # ⚠️ this one Secret, not all of them
    verbs: ["get"]                          # not list, not watch — see doc 12, Y-04
```

And question whether the hook needs cluster access at all. A migration Job that gets its connection
string from a `secretKeyRef` in its own pod spec needs **no RBAC whatsoever** — the kubelet resolves
the reference, not the pod. That removes the standing grant entirely, and it is the better answer
in most cases:

```yaml
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: {{ include "checkout-api.fullname" . }}-db
                  key: url
          # No serviceAccountName with permissions needed. The kubelet does the read.
```

The version in doc 06 uses a ServiceAccount because the Job there also runs `kubectl wait`; if all
your hook does is read a secret and talk to a database, do not give it an API identity.

### Z-06 · Template injection through `tpl`

**What you see.** Nothing, unless you go looking.

**Mechanism.** `tpl` renders a string as a Go template with the full Helm context, which includes
the `lookup` function. So a chart that does this:

```yaml
  annotations:
    riverbend.io/description: {{ tpl .Values.description . | quote }}
```

will, given this value:

```yaml
description: '{{ (lookup "v1" "Secret" "kube-system" "some-credential").data.token | b64dec }}'
```

render the contents of a Secret in a namespace the value's author cannot access — using the
*deployer's* credentials — into an annotation the value's author can read.

This matters when the person supplying values and the person deploying have different privileges:
a multi-tenant platform where tenants supply values that a platform pipeline deploys, or a
self-service portal that builds a values file from a form.

**Confirm it.** Find every `tpl` and decide, for each, whether the input is trusted:

```bash
grep -rn 'tpl ' charts/*/templates/ charts/*/charts/*/templates/ 2>/dev/null
```

**Prevent.** Three layers:

1. **Do not `tpl` values that cross a trust boundary.** If tenants supply values, no `tpl` on them.
   For the common legitimate use — a hostname containing `{{ .Values.global.baseDomain }}` — build
   it with `printf` in the template instead, so the value is data rather than code:
   ```yaml
     - host: {{ printf "%s.%s" .Values.ingress.subdomain .Values.global.baseDomain | quote }}
   ```
2. **Constrain the value with a schema.** A `pattern` on a hostname value makes template syntax
   unrepresentable:
   ```json
   { "ingress": { "properties": { "subdomain": { "type": "string", "pattern": "^[a-z0-9-]{1,40}$" } } } }
   ```
3. **Reduce the deployer's reach.** The attack is only interesting because the deploy identity can
   read Secrets the tenant cannot. A per-tenant deploy identity scoped to the tenant's namespace
   makes the injection return nothing useful — which is `Z-07`'s architecture.

### Z-07 · Multi-tenant Helm — who is allowed to install

**The situation.** Riverbend's 38 namespaces belong to 22 teams. Should a team be able to run
`helm install` in its own namespace?

**The two models, and they have very different properties:**

**Model A — teams do not run Helm. A pipeline does.** Teams commit values; a central pipeline holds
the deploy identity and runs the upgrade. Tenants have read-only cluster access.

- **Good:** one identity to audit, one place to enforce policy, consistent flags, a deploy ledger
  for free. You know every deploy went through review.
- **Bad:** a tenant cannot debug by rendering against the real cluster, cannot run `helm history`,
  and cannot roll back without the pipeline. That last one matters at 2 a.m.

**Model B — teams hold a namespace-scoped deploy identity.** Each team can `helm upgrade` in its own
namespaces.

- **Good:** self-service, and a team can roll back its own service immediately.
- **Bad:** every one of those identities can read every Secret in its namespaces (unavoidable, per
  the top of this doc), policy enforcement moves entirely to admission control, and consistency of
  flags is a convention rather than a mechanism.

**Riverbend runs A for normal deploys and a constrained B for recovery**, which is the combination
worth describing because it addresses the "2 a.m." objection without giving away routine deploys:

```yaml
# A ClusterRole that permits ONLY rollback-shaped operations: read the release history,
# read and update the objects, and write release Secrets. It cannot create new kinds
# or change RBAC.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: riverbend-service-breakglass
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "list", "create", "update", "patch", "delete"]   # release records
  - apiGroups: ["", "apps", "autoscaling", "policy"]
    resources: ["services", "configmaps", "serviceaccounts", "deployments",
                "statefulsets", "horizontalpodautoscalers", "poddisruptionbudgets"]
    verbs: ["get", "list", "watch", "update", "patch"]              # ⚠️ no create, no delete
  - apiGroups: ["apps"]
    resources: ["deployments/scale", "statefulsets/scale"]
    verbs: ["update", "patch"]
```

Bound per namespace to the owning team's group, and ⚠️ with the binding itself audited: a
`RoleBinding` to `riverbend-service-breakglass` triggers a notification, so using it is visible
rather than silent. The absence of `create` and `delete` on workload kinds is what makes it a
rollback tool rather than a deploy tool.

### Z-08 · A tenant cannot uninstall their own release

**What you see.** `helm uninstall` failing with a permission error on a kind the tenant can create
but not delete, leaving a partially-uninstalled release.

**Mechanism.** A Role granted `create`, `update` and `patch` but not `delete`, because `delete`
felt dangerous. Helm's uninstall needs `delete` on every kind in the manifest, and a partial
uninstall leaves the release in `uninstalling` — which is a `pending`-like state you then have to
recover from.

**Prevent.** Grant `delete` on the kinds the chart creates, or do not let tenants uninstall. A Role
with `create` and without `delete` produces the worst outcome of both.

### Z-09 · ResourceQuota interacting with a chart

**What you see.** A deploy that succeeds and pods that never start, with
`exceeded quota: compute-resources, requested: requests.cpu=2, used: requests.cpu=18, limited:
requests.cpu=20`.

**Mechanism.** Same shape as `Z-04`: quota is enforced at pod creation, so the Deployment is
admitted and the ReplicaSet cannot create pods. `helm upgrade --wait` times out with no useful
message, and the real error is in the ReplicaSet's events.

⚠️ And a specific interaction worth knowing: a `LimitRange` with a default request applies to pods
that do not specify one, so a chart that omits `resources` gets the namespace default — which may be
much larger than the service needs, consuming quota invisibly.

**Confirm it.**

```bash
kubectl -n checkout describe quota
kubectl -n checkout describe rs -l app.kubernetes.io/instance=checkout-api | sed -n '/Events/,$p'
```

**Prevent.** Two things:

1. **Make the rollout's peak usage visible.** A rolling update with `maxSurge: 25%` on 24 replicas
   temporarily needs 30 pods' worth of quota, not 24. A quota sized for steady state blocks every
   deploy. Size quota for steady state × (1 + `maxSurge`), and say so in the quota's annotations so
   the next person understands the number.
2. **Always set `resources.requests` explicitly in the chart**, so `LimitRange` defaults never
   apply. The library chart requiring them — via `required` — makes this structural.

### Z-10 · Admission policy as the real control

The summary of this doc's second thesis, made concrete. The policies worth having, in rough order of
value:

| Policy | Catches |
|---|---|
| No `ClusterRoleBinding` to `cluster-admin` | `Z-02`, from any chart, including ones you have not read |
| No `ClusterRole` with `*` verbs on `*` resources | The same, one level earlier |
| Images only from `registry.riverbend.internal` | Doc 09's `K-06`, including operator-constructed references the render check cannot see |
| Image references by digest, not tag | Doc 09's mutable-tag problem |
| No `hostPath`, `hostNetwork`, `privileged` outside an allow-list of namespaces | `Z-03` |
| Required labels (`riverbend.io/owner`, `riverbend.io/tier`) on every workload | Makes doc 16's inventory possible at all |
| `ValidatingWebhookConfiguration` must have `failurePolicy: Ignore` unless allow-listed | A chart whose webhook can break every write in the cluster |
| Every workload has `resources.requests` | `Z-09` |

⚠️ The last one in the list is the one to be careful with: enforced strictly, it blocks any chart
that omits requests, which includes several third-party charts. Run it in `Audit` first.

**Why admission beats RBAC for these:** RBAC answers "may this identity create a
ClusterRoleBinding", which is a yes-or-no you have to grant because some chart needs it. Admission
answers "may *this* ClusterRoleBinding exist", which is the rule you actually wanted. And admission
applies to every path into the cluster — Helm, `kubectl`, a GitOps controller, an operator — while
RBAC applies per identity and has to be got right on each one.

### Z-11 · Auditing what actually happened

**What you see.** A change nobody can account for.

**Mechanism.** `helm history` tells you a release changed and, with `--description` (doc 05), which
pipeline did it. It does not tell you *who* triggered that pipeline, and it does not record a
`helm upgrade` run from a laptop with anything other than the local username.

**The authoritative record is the Kubernetes audit log**, which captures every API call with the
authenticated identity:

```json
{
  "verb": "patch",
  "user": { "username": "system:serviceaccount:ci:ci-services",
            "groups": ["system:serviceaccounts", "system:authenticated"] },
  "objectRef": { "resource": "deployments", "namespace": "checkout", "name": "checkout-api" },
  "responseStatus": { "code": 200 },
  "requestReceivedTimestamp": "2026-09-29T16:44:03.118Z"
}
```

An audit policy that captures what you need without capturing everything:

```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
  # Release Secrets: metadata only. ⚠️ NEVER RequestResponse — the body contains your values.
  - level: Metadata
    resources:
      - group: ""
        resources: ["secrets"]
  # Workload and RBAC writes: full request, which is what you want during an investigation
  - level: RequestResponse
    verbs: ["create", "update", "patch", "delete"]
    resources:
      - group: "apps"
      - group: "rbac.authorization.k8s.io"
      - group: "admissionregistration.k8s.io"
      - group: "apiextensions.k8s.io"
  - level: Metadata
    omitStages: ["RequestReceived"]
```

⚠️ The comment on the first rule is the important part. `level: RequestResponse` on Secrets writes
every Secret's contents into the audit log, including every release's values — which relocates doc
12's problem into a log stream that is usually more widely readable than the cluster.

**Prevent.** Beyond the audit log: `--description` carrying the pipeline URL, and a deploy ledger
(doc 16) that records who triggered what, since the pipeline knows the human and the cluster does
not.

## What to take away

1. **Helm 3 runs as you**, which fixed Tiller and made the deploy identity's permissions the whole
   security question. You cannot audit an unread chart; you can audit what the identity deploying it
   is allowed to do.
2. **RBAC controls who can ask; admission controls what may exist.** If you only do one, do
   admission — it expresses the rule you actually want and applies to every path into the cluster,
   not per identity.
3. **A Helm deploy identity can read every Secret in the namespaces it deploys to.** Release Secrets
   have unpredictable names, so `resourceNames` cannot narrow it. This is unavoidable with the
   `secret` driver and worth knowing before you grant it.
4. **Derive the deploy Role from the kinds your charts render**, not from `edit`. One command
   produces the list, and a derived Role is meaningfully different from a convenient one.
5. **Bind a `ClusterRole` with a `RoleBinding`** to grant one rule set in many namespaces without
   granting it cluster-wide. This is the distinction people miss.
6. **Split the deploy identity by what it deploys.** Riverbend's 45 daily service deploys run as an
   identity with no cluster-scoped permissions; the platform identity requires a human approval.
7. **Pod Security Admission and ResourceQuota reject the *pod*, not the Deployment** — so
   `helm upgrade` succeeds and the rollout silently never progresses, with the real error in the
   ReplicaSet's events. `kubectl apply --dry-run=server` on the render is the check that catches it
   in advance.
8. **Size quota for the rollout, not steady state.** `maxSurge: 25%` on 24 replicas needs 30 pods'
   worth, and quota sized for 24 blocks every deploy.
9. **`tpl` on a value that crosses a trust boundary is template injection**, and template code can
   call `lookup` with the deployer's credentials. Use `printf`, constrain with a schema pattern, and
   narrow the deployer's reach.
10. **A Role with `create` and without `delete` is the worst of both** — tenants can install and
    cannot cleanly uninstall, leaving releases stuck in `uninstalling`.
11. **Never set `level: RequestResponse` on Secrets in your audit policy.** It writes every
    release's values into a log stream that is usually more widely readable than the cluster itself.
