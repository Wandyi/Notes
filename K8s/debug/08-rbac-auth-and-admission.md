# RBAC, Auth & Admission

"Forbidden", "Unauthorized", and "admission denied" all look similar but happen at different
gates. The request pipeline is: **Authentication (who are you?) → Authorization/RBAC (may you?)
→ Admission (mutating then validating webhooks/policies) → persisted to etcd.** Identify the
gate first; the error string tells you which.

## Fast triage

| Error text | Gate | Jump to |
|---|---|---|
| `Unauthorized` (401) | Authentication | [AuthN](#authentication-401) |
| `forbidden: User "..." cannot <verb> <resource>` (403) | RBAC | [RBAC](#rbac-forbidden-403) |
| `admission webhook "..." denied the request` | Validating admission | [Admission](#admission-denied) |
| `failed calling webhook ... timeout/refused` | Webhook backend down | doc 05 (webhook stalls) |
| Pod can't call the API from inside | ServiceAccount token/RBAC | [SA tokens](#serviceaccount-tokens-in-pod) |
| `violates PodSecurity "restricted"` | Pod Security Admission | [PSA](#pod-security-admission) |

---

## Authentication (401)

`Unauthorized` = the apiserver can't establish *who* you are. This is credentials, not
permissions.

- **Expired/invalid client cert or token** — kubeconfig cert past validity, bearer token
  expired, or wrong CA. `kubectl` shows `Unauthorized` for *everything*.
- **OIDC issuer down / clock skew** — token can't be validated; check the IdP and node time.
- **ServiceAccount token expired** — bound tokens (projected, default) are short-lived and
  auto-refreshed by the kubelet; a manually-mounted legacy token may be revoked.
- Distinguish "just me" (my creds) from "everyone" (apiserver auth stack / OIDC / cert chain
  broken — closer to doc 05).

```bash
kubectl auth whoami                       # who does the apiserver think I am? (1.26+)
```

---

## RBAC forbidden (403)

`User "system:serviceaccount:ns:sa" cannot get resource "pods" in namespace "x"` — you're
authenticated but not authorized. RBAC is **purely additive allow** (no deny rules); if no
Role/ClusterRole grants the verb+resource+scope, it's forbidden.

The decisive tool is `kubectl auth can-i` — use `--as` to impersonate the subject:

```bash
kubectl auth can-i get pods --as=system:serviceaccount:$NS:$SA -n $NS
kubectl auth can-i --list --as=system:serviceaccount:$NS:$SA -n $NS   # everything they can do
```

Then trace the bindings:

```bash
# What roles is this SA bound to?
kubectl get rolebindings,clusterrolebindings -A -o json \
  | jq -r '.items[] | select(.subjects[]?.name=="'$SA'") | .metadata.namespace+"/"+.metadata.name+" -> "+.roleRef.kind+"/"+.roleRef.name'
kubectl -n $NS describe role <role>       # the actual rules (verbs, resources, apiGroups)
```

Common mistakes, ranked:

1. **Wrong `apiGroups`** ⚠️ — the resource is in the wrong API group in the rule. `pods` are in
   the core group (`""`), `deployments` in `apps`, `ingresses` in `networking.k8s.io`, CRDs in
   their own group. A rule with `apiGroups: [""]` won't grant `deployments`. #1 RBAC bug.
2. **Role vs ClusterRole scope** — a `Role`/`RoleBinding` grants only within one namespace. To
   grant across namespaces or on cluster-scoped resources (nodes, PVs, namespaces themselves)
   you need a `ClusterRole` + `ClusterRoleBinding` (or a ClusterRole referenced by a per-ns
   RoleBinding).
3. **Resource name subtleties** — subresources are separate (`pods/log`, `pods/exec`,
   `pods/portforward`, `deployments/scale`). Granting `pods` doesn't grant `pods/log`.
4. **Wrong subject** — binding names the wrong SA/namespace, or uses `User` where it should be
   `ServiceAccount`. The `namespace` field of a ServiceAccount subject matters.
5. **`resourceNames`** restricting to specific object names when you expected all.

> `kubectl auth can-i --list --as=...` is the fastest way to see the *effective* permission
> set and stop guessing which binding is (not) applying.

---

## ServiceAccount tokens in pod

An app in a pod calling the Kubernetes API (operators, controllers, sidecars) authenticates as
its ServiceAccount. Failures:

```bash
kubectl -n $NS get pod $POD -o jsonpath='{.spec.serviceAccountName}'; echo
# token is projected here:
kubectl -n $NS exec $POD -- cat /var/run/secrets/kubernetes.io/serviceaccount/token | head -c 40; echo
```

- **`automountServiceAccountToken: false`** on the pod or SA → no token mounted → in-cluster
  clients get 401 or "no credentials". Intentional hardening that breaks apps expecting a token.
- **Default SA has no permissions** — the `default` SA in a namespace is intentionally
  powerless. Apps must use a purpose-made SA with an explicit RoleBinding. Running as `default`
  and getting 403s is expected.
- **Bound token audience/expiry** — projected tokens are audience-scoped and short-lived; a
  client that caches the token forever or sends it to the wrong audience fails. Use the SDK's
  in-cluster config which reloads the token.
- **Cross-namespace** — SA in ns A can be granted rights in ns B via a RoleBinding *in ns B*
  naming the ns-A SA. Forgetting the binding lives in the target ns is common.

---

## Admission denied

If AuthN and RBAC pass but the write is still rejected, an **admission** plugin/webhook
rejected it. The message names the webhook/policy.

```bash
kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations
kubectl describe validatingwebhookconfiguration <name>    # rules, namespaceSelector, failurePolicy
```

- **Policy engines** (OPA/Gatekeeper, Kyverno) — the denial text usually cites the constraint
  ("requires label X", "image not from allowed registry", "no privileged"). Fix the manifest to
  comply, or adjust the policy. Check the policy engine's own resources for the rule.
- **Built-in admission controllers** — e.g. `ResourceQuota` (exceeded), `LimitRanger`
  (defaults/violations), `NamespaceLifecycle` (writing to a terminating namespace), image policy.
- **Mutating webhook side effects** — a mutating webhook injected something (sidecar, defaults)
  that then failed validation, or is missing → subtle "why is my pod different / rejected".
- Distinguish **denied** (policy said no — fix your manifest) from **webhook unreachable**
  (`timeout`/`connection refused` — infra problem, doc 05). Different fix entirely.

---

## Pod Security Admission

PSA replaced PodSecurityPolicy. It enforces three levels (`privileged`/`baseline`/`restricted`)
per namespace via labels, at admission time.

```bash
kubectl get ns $NS -o jsonpath='{.metadata.labels}' | tr ',' '\n' | grep pod-security
```

- **`violates PodSecurity "restricted:latest"`** — the pod requests something the level
  forbids: `runAsNonRoot` unset, `allowPrivilegeEscalation: true`, added capabilities, host
  namespaces/paths, `seccompProfile` unset, etc. The error lists each violation. Fix the pod's
  `securityContext` to comply, or (deliberately) relax the namespace level.
- **`enforce` vs `warn` vs `audit`** — `warn`/`audit` don't block (you see warnings but the pod
  runs); only `enforce` rejects. If pods run despite warnings, `enforce` isn't set to that level.
- ⚠️ Bumping a namespace to `restricted` will reject previously-fine workloads at their *next*
  create/update (not existing running pods) — a rollout can suddenly fail admission. Test with
  `warn` first, then flip to `enforce`.

---

## Prevention checklist

- Least-privilege SAs per workload; never rely on `default`; `automountServiceAccountToken:
  false` unless the pod needs the API.
- Use `kubectl auth can-i --list --as=...` in CI to verify a workload's RBAC before shipping.
- Get `apiGroups`/subresources right — the two most common RBAC mistakes.
- Every admission webhook: correct `namespaceSelector` (exclude kube-system + its own ns),
  sane timeout, deliberate `failurePolicy`; keep an "unwedge" runbook (doc 05).
- Roll PSA levels via `warn`/`audit` before `enforce`; pre-fix `securityContext`.
