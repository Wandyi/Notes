# Secrets and Sensitive Values

Helm has no secret management. It has values, and some of your values are secrets, and Helm treats
them exactly the same as a replica count — which means it writes them to places you did not think
about.

The thesis:

> **Every secret you put in a Helm value ends up in at least four places**: the values file, the CI
> job's environment and logs, the rendered manifest, and the release Secret's `config` field —
> where it stays for the next ten revisions, readable by anyone with `get secrets` in that
> namespace. The fix is not to encrypt the value better. It is to **stop putting the secret in a
> value at all**, and have the chart reference a secret that a purpose-built system materialises.

## The four places, traced

Take `--set database.password=hunter2` and follow it.

**Place 1 — wherever the value came from.** A values file in Git; a CI variable; a shell command.
Each has its own exposure: Git keeps it forever and in every clone; a CI variable is visible to
anyone who can edit the pipeline; a shell command lands in `~/.zsh_history` and, for the duration of
the command, in `/proc/<pid>/cmdline` where any process on the machine can read it.

**Place 2 — the CI log.** Most CI systems mask *declared* secret variables in output. They do not
mask a value that arrives via a different path, and they do not mask it inside Helm's own `--debug`
output. And ⚠️ a `helm upgrade` that fails often prints the computed values, which is exactly when
somebody re-runs it with `--debug`.

**Place 3 — the rendered manifest.** Which means a Kubernetes `Secret` object, which is the correct
destination — but also, if you ran `helm template` in CI and stored the output as an artefact, a
file in your CI system's artefact store.

**Place 4 — the release Secret's `config` field.** This is the one people do not know about. Doc 04
showed that a release revision stores the user-supplied values verbatim. So:

```bash
helm get values checkout-api -n checkout
```
```yaml
database:
  password: hunter2
```

That is not a bug; it is the design. And it means:

- **Anyone with `get secrets` in the `checkout` namespace can read it**, because the release Secret
  is a Secret in that namespace and is not protected differently from any other. RBAC that grants
  `secrets: [get, list]` for a legitimate reason — a debugging role, a monitoring agent, a
  backup tool — also grants read access to every release's values.
- **It persists for `--history-max` revisions.** Rotating the password does not remove the old one;
  revision 42's Secret still has it until pruning removes revision 42.
- **It is in every backup.** Velero, `etcd` snapshots, a namespace export.

**Prove it to yourself** on a cluster you own, because this is the demonstration that changes
people's minds:

```bash
# Every non-empty user-supplied value across every release, with keys that look sensitive
kubectl get secrets -A --field-selector type=helm.sh/release.v1 -o json \
  | jq -r '.items[] | "\(.metadata.namespace)\t\(.metadata.name)"' \
  | while IFS=$'\t' read -r ns name; do
      kubectl get secret -n "$ns" "$name" -o jsonpath='{.data.release}' 2>/dev/null \
        | base64 -d | base64 -d | gzip -d 2>/dev/null \
        | jq -r --arg ns "$ns" --arg n "$name" '
            (.config // {}) | paths(scalars) as $p
            | select($p | map(tostring) | join(".")
                     | test("pass|secret|token|key|credential|apikey|dsn|url"; "i"))
            | "\($ns)/\($n): \($p | join("."))"' 2>/dev/null
    done | sort -u
```

At Riverbend the first run of that found 61 release/key pairs across 23 namespaces, including four
database passwords and a third-party API token.

## The five mechanisms, honestly compared

| | Plain values | SOPS + `helm-secrets` | Sealed Secrets | **External Secrets Operator** | CSI Secrets Store |
|---|---|---|---|---|---|
| Secret in Git | plaintext ❌ | encrypted ✅ | encrypted ✅ | not at all ✅ | not at all ✅ |
| Secret in the release `config` | yes ❌ | **yes, decrypted** ❌ | no ✅ | no ✅ | no ✅ |
| Rotation without a deploy | no | no | no | **yes** ✅ | yes ✅ |
| Works under GitOps render-only | yes | needs a plugin | ✅ | ✅ | ✅ |
| Needs a controller in-cluster | no | no | yes | yes | yes |
| Needs an external secret store | no | KMS for the key | no | yes | yes |
| Audit trail on secret access | none | none | none | the store's ✅ | the store's ✅ |
| Secret exists as a k8s `Secret` | yes | yes | yes | yes | ⚠️ optional — can be file-only |

Read the second row carefully, because it is the one that surprises people: **SOPS decrypts before
Helm sees the value, so the plaintext still lands in the release Secret.** SOPS solves "secret in
Git" and does not solve "secret in release state". That is a real improvement and it is half the
problem.

### What each one actually is

**SOPS with `helm-secrets`.** Values files encrypted at the leaf level with a KMS key, decrypted by
a Helm plugin at deploy time:

```yaml
# envs/20-prod-use1.secrets.yaml — committed, encrypted
database:
    password: ENC[AES256_GCM,data:x8Kp2Qw...,iv:...,tag:...,type:str]
sops:
    kms:
        - arn: arn:aws:kms:us-east-1:481516234290:key/9c2f...
```

```bash
helm secrets upgrade checkout-api ./charts/checkout-api -n checkout \
  -f envs/00-base.yaml -f envs/20-prod-use1.yaml -f envs/20-prod-use1.secrets.yaml
```

**Good:** the Git problem is genuinely solved, the diff shows *which keys* changed without revealing
values, and KMS gives you an access audit trail for decryption. **Bad:** the plaintext reaches the
release Secret, rotation is a deploy, and it is a plugin — so it needs installing in every CI image
and it does not work under Argo CD's render-only model without a config-management plugin.

**Sealed Secrets.** A controller holds a private key; you encrypt with the public key; the
`SealedSecret` in Git is decryptable only by that cluster's controller:

```bash
kubectl create secret generic checkout-api-db --dry-run=client -o yaml \
  --from-literal=password="$PASSWORD" \
  | kubeseal --controller-namespace sealed-secrets --format yaml \
  > charts/checkout-api/templates/sealed-db-secret.yaml
```

**Good:** nothing plaintext anywhere, no external dependency, works in any model. **Bad:** rotation
is a deploy; the sealing key is a per-cluster secret you must back up (lose it and every
`SealedSecret` is unreadable); and a `SealedSecret` is bound to one namespace and name, so
promoting the same secret across four clusters means four sealings.

**External Secrets Operator.** The chart creates an `ExternalSecret` custom resource naming a path
in a secret store; a controller reads the store and materialises a `Secret`:

```yaml
# templates/externalsecret.yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: {{ include "checkout-api.fullname" . }}-db
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: {{ .Values.secrets.storeName }}
    kind: ClusterSecretStore
  target:
    name: {{ include "checkout-api.fullname" . }}-db
    creationPolicy: Owner
  data:
    - secretKey: password
      remoteRef:
        key: {{ .Values.secrets.pathPrefix }}/checkout-api/db
        property: password
    - secretKey: url
      remoteRef:
        key: {{ .Values.secrets.pathPrefix }}/checkout-api/db
        property: url
```

```yaml
# envs/20-prod-use1.yaml — a PATH, not a secret. Committable, diffable, layerable.
secrets:
  storeName: aws-secretsmanager-use1
  pathPrefix: riverbend/prod-use1
```

**Good:** the chart contains no secret material at all, so places 1 through 4 are all clean. The
value in Git is a *path*, which means it participates in the environment layering (doc 10) and the
drift audit like any other value. Rotation happens in the store and the controller picks it up on
`refreshInterval` — **no deploy**. The store gives you an access audit trail. And it works in every
GitOps model.

**Bad:** a controller and an external store to run; the secret store becomes a dependency of your
pods starting; and ⚠️ a rotated secret propagates to the `Secret` object but **does not restart your
pods**, so an application that reads its password once at startup does not pick up the rotation.
That is `Y-10`, and it is the thing people get wrong about ESO.

**CSI Secrets Store driver.** Mounts secrets from an external store directly as files in the pod,
optionally also syncing a `Secret`. **Good:** the secret can exist *only* as a file in the pod, never
as a Kubernetes `Secret`, which removes it from `etcd` entirely. **Bad:** the pod cannot start if the
store is unavailable, and file-only mode means nothing else can reference it (no `envFrom`, no other
workload sharing it).

### The recommendation

**External Secrets Operator, with the secret store as the system of record.** The argument is not
primarily about encryption strength; it is about **where the secret lives**:

- With SOPS or Sealed Secrets, the secret lives in Git, encrypted. Git is not designed to hold
  secrets, has no rotation model, no access audit per secret, and no expiry — and anything committed
  is committed forever in the history.
- With ESO, the secret lives in a secret manager, which is designed for exactly this: versioning,
  rotation, per-secret IAM, access logs, and automatic rotation for supported services like RDS.

And the operational consequence that settles it: **rotation without a deploy.** When a credential
must be rotated urgently — a leak, a departing employee, a compliance requirement — the SOPS answer
is "edit, commit, deploy 240 services" and the ESO answer is "rotate in the store".

**Use Sealed Secrets when** you cannot run an external secret store — an air-gapped cluster, an
on-premises deployment with no cloud KMS. It is a good answer for that constraint.

**Use SOPS when** you are already using it and the migration cost is not yet justified, or for
things that are secret-ish but not credentials — a licence key, an internal hostname you would
rather not publish.

## The `Y-` failure catalogue

### Y-01 · A password on a command line

**What you see.** Nothing, until an audit.

**Mechanism.** `--set database.password=hunter2` exposes the value in the shell history, the process
list for the duration of the command, and the CI log unless masked.

**Confirm it.**

```bash
grep -rnE "\-\-set[a-z-]* *[A-Za-z0-9_.]*(pass|secret|token|key|credential)" \
  --include='*.sh' --include='*.yml' --include='*.yaml' . | grep -v '^\s*#'
history | grep -E 'helm .*--set.*(pass|token|secret)'
```

**Recover.** Rotate the credential. It has been in a log.

**Prevent.** If you must pass a secret to Helm at all, use `--set-file` from a path, or a values
file created at runtime with restrictive permissions and deleted after:

```bash
umask 077
tmp=$(mktemp)
trap 'shred -u "$tmp" 2>/dev/null || rm -f "$tmp"' EXIT
printf 'database:\n  password: %s\n' "$DB_PASSWORD" > "$tmp"
helm upgrade checkout-api ./charts/checkout-api -n checkout -f envs/20-prod-use1.yaml -f "$tmp"
```

⚠️ This is mitigation, not a fix — the value still reaches place 4. The fix is ESO.

### Y-02 · A secret committed to Git

**What you see.** A plaintext credential in a values file.

**Mechanism.** Somebody needed it to work.

**Confirm it.** Scan the history, not just the working tree — a secret removed in a later commit is
still in the history and in every clone:

```bash
# Requires gitleaks; any secret scanner works
gitleaks detect --source . --no-banner

# Or targeted at values files specifically
git log --all -p -- 'envs/*.yaml' 'charts/*/values.yaml' \
  | grep -nEi '^\+.*(password|secret|token|apikey|api_key|private_key):\s*[^ ].*' \
  | grep -vE ': *(""|null|ENC\[|\{\{)'
```

**Recover.** **Rotate the credential.** Rewriting Git history does not help — every clone and every
fork still has it, and you cannot know who cloned. Removing it from the history is worth doing
afterwards for hygiene; it is not the remediation.

**Prevent.** A pre-commit hook and a CI secret scan, plus ESO so nobody has a reason to.

### Y-03 · `helm get values` reveals the password

**What you see.** The command from the top of this doc returning credentials.

**Mechanism.** Place 4. The release Secret stores `config` verbatim.

**Recover.** Migrating to ESO stops *new* revisions containing it. The old revisions still do, until
pruned. To remove them immediately:

```bash
# ⚠️ DESTRUCTIVE: removes rollback targets. Do this deliberately, and rotate the credential anyway.
# 1. See which revisions contain it
helm history checkout-api -n checkout --max 20
# 2. Deploy the ESO-based chart, so the CURRENT revision is clean
# 3. Force pruning by upgrading with a small history-max
helm upgrade checkout-api ./charts/checkout-api -n checkout -f ... --history-max 1
# 4. Confirm
kubectl get secrets -n checkout -l 'owner=helm,name=checkout-api'
```

⚠️ **Rotate the credential regardless.** It has been in `etcd`, in backups, and readable by anyone
with `get secrets` for however long it was there. Deleting the Secrets does not un-expose it.

**Prevent.** ESO. And as a second layer, `EncryptionConfiguration` so Secrets are encrypted at rest
in `etcd` — which protects against an `etcd` backup leaking and does nothing against a user with
`get secrets`, so it is a useful layer and not a solution.

### Y-04 · RBAC on release Secrets is broader than you think

**What you see.** A role granting `secrets: [get, list]` for a legitimate purpose, which
incidentally grants read access to every release's values in that namespace.

**Mechanism.** The release Secret is an ordinary Secret. There is no separate resource type and no
way to grant "read Secrets except Helm's".

**Confirm it.** Find every subject that can read Secrets in a namespace holding releases:

```bash
NS=checkout
kubectl get rolebindings,clusterrolebindings -A -o json | jq -r --arg ns "$NS" '
  .items[] | .roleRef.name as $role | .metadata.name as $rb
  | (.subjects // [])[] | "\($rb)\t\($role)\t\(.kind)/\(.name)"' | sort -u
# then, for each role, check whether it grants secrets access
kubectl get role,clusterrole -A -o json | jq -r '.items[]
  | .metadata.name as $n | (.rules // [])[]
  | select((.resources // []) | index("secrets") or index("*"))
  | select((.verbs // []) | index("get") or index("list") or index("*"))
  | "GRANTS SECRET READ: \($n) — \(.verbs)"' | sort -u
```

**Prevent.** Three things, in order of effectiveness:

1. **ESO**, so there is nothing sensitive in the release `config` to protect.
2. **Narrow the roles.** `resourceNames` on a Role restricts it to named Secrets, which is how a
   debugging role can read the two Secrets it needs and not the release history:
   ```yaml
   rules:
     - apiGroups: [""]
       resources: ["secrets"]
       resourceNames: ["checkout-api-tls"]     # not a blanket grant
       verbs: ["get"]
   ```
   ⚠️ `resourceNames` does not work with `list` or `watch` — Kubernetes cannot filter a list by
   name. So a role that needs `list` on Secrets gets all of them, which is a reason to design
   tooling around `get` on known names.
3. **Consider the `sql` storage driver** (doc 04, `R-03`), which moves release state out of the
   namespace entirely. It has its own costs and this is one of the genuine arguments for it.

### Y-05 · The External Secrets path was wrong and nothing failed

**What you see.** Pods with an empty password. The `ExternalSecret` exists. No error in the deploy.

**Mechanism.** `helm upgrade` created the `ExternalSecret` successfully — that is all Helm was asked
to do. The controller then failed to resolve the path, and `--wait` does not understand
`ExternalSecret` (doc 05, `U-04`), so the release reported success.

If `creationPolicy: Owner` and the store lookup fails, the target `Secret` is **not created**, so
pods referencing it fail to start — which is loud. But if the `Secret` already exists from a
previous successful sync and only *one key* now fails to resolve, you can get a stale or partial
Secret, which is quiet.

**Confirm it.** The `ExternalSecret`'s status is the authoritative answer:

```bash
kubectl get externalsecret -n checkout -o custom-columns=\
'NAME:.metadata.name,STORE:.spec.secretStoreRef.name,STATUS:.status.conditions[0].reason,MSG:.status.conditions[0].message'
```

Anything other than `SecretSynced` is a problem.

**Prevent.** Three layers, and use all three:

1. **A `post-upgrade` hook that blocks on it**, converting an unwaitable thing into a waitable one
   (doc 05's pattern):
   ```yaml
   command: ["kubectl","wait","--for=condition=Ready",
             "externalsecret/{{ include "checkout-api.fullname" . }}-db","--timeout=90s"]
   ```
2. **An alert on any `ExternalSecret` not synced**, which is the fleet-wide backstop:
   ```promql
   externalsecret_status_condition{condition="Ready",status="false"} == 1
   ```
3. **Path consistency by construction.** The path is built from values that already participate in
   the environment layering (`secrets.pathPrefix`), so a wrong path in one environment is visible
   in doc 10's `E-05` audit rather than discovered at runtime.

### Y-06 · Sealed Secret unsealing failed after a cluster rebuild

**What you see.** `SealedSecret` objects that never produce a `Secret`. The controller logs
`no key could decrypt secret`.

**Mechanism.** The sealing key is generated per controller installation. Rebuild the cluster, or
reinstall the controller without restoring the key, and every existing `SealedSecret` is
permanently undecryptable.

**Confirm it.**

```bash
kubectl get sealedsecret -A -o custom-columns=\
'NS:.metadata.namespace,NAME:.metadata.name,SYNCED:.status.conditions[0].status'
kubectl logs -n sealed-secrets deploy/sealed-secrets-controller --tail=50 | grep -i 'decrypt'
```

**Recover.** Restore the sealing key from backup, or re-seal every secret from the original
plaintext — which requires still having the plaintext somewhere, which is the point at which people
discover they do not.

**Prevent.** **Back up the sealing key as a first-class operational artefact**, and test the
restore:

```bash
kubectl get secret -n sealed-secrets -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o yaml > sealed-secrets-key-backup.yaml
# → into your secret manager, not into Git
```

⚠️ This is a genuine argument against Sealed Secrets: you have created a single secret whose loss is
unrecoverable and whose backup is itself a secret-management problem. ESO does not have this
property, because the secrets live in a system that already has a durability and access story.

### Y-07 · Generating a secret in a template

Covered in doc 02 (`T-11`), doc 11 (`G-03`), and `CS-1`. The one-line summary: `lookup` returns
empty on any dry run, so the "generate once and reuse" pattern silently rotates the credential — and
under a reconciling GitOps controller it does so on every reconcile.

The CI check that bans it:

```bash
grep -rnE 'randAlpha|randNumeric|randAscii|genCA|genSelfSignedCert|genSignedCert|uuidv4|derivePassword' \
  charts/*/templates/ \
  && { echo "generating secrets in templates — see Y-07. Use ExternalSecret."; exit 1; }
```

### Y-08 · A secret in a ConfigMap

**What you see.** A credential inside a rendered `ConfigMap` rather than a `Secret`.

**Mechanism.** An application's configuration file contains a connection string. The chart templates
the whole file into a ConfigMap, connection string included.

This is worse than putting it in a Secret, because ConfigMaps are not covered by
`EncryptionConfiguration`, are commonly readable by much broader RBAC, and are frequently dumped
whole in debugging output.

**Confirm it.**

```bash
kubectl get cm -A -o json | jq -r '.items[] | .metadata.namespace as $ns | .metadata.name as $n
  | (.data // {}) | to_entries[]
  | select(.value | test("password|secret|token|://[^/@]*:[^/@]*@"; "i"))
  | "\($ns)/\($n): key=\(.key)"'
```

That regular expression's last alternative catches connection strings with embedded credentials —
`postgres://user:pass@host/db` — which is how this usually happens.

**Prevent.** Split the configuration: the non-sensitive parts in a ConfigMap, the credential in a
Secret, and the application composes them from environment variables. Most applications support
this; for ones that insist on a single file, an init container or an entrypoint that substitutes the
credential from the environment at startup is the standard workaround.

### Y-09 · A secret in the rendered output stored as a CI artefact

**What you see.** A `helm template` output file in your CI artefact store containing a Secret's
base64 data.

**Mechanism.** A CI step renders the chart for review or for `kubeconform`, and the artefact is
retained by default.

⚠️ And note that base64 is not encryption. A Secret's `data` in a rendered manifest is one
`base64 -d` away from plaintext, and people treat base64 as if it obscured something.

**Prevent.** Redact before storing, and store the *diff* rather than the render:

```bash
helm template checkout-api ./charts/checkout-api -f envs/20-prod-use1.yaml \
  | yq '(select(.kind=="Secret") | .data) |= with_entries(.value = "REDACTED")' \
  > artefacts/rendered.yaml
```

With ESO there is no Secret in the render at all, which is the cleaner answer.

### Y-10 · A rotated secret that pods never picked up

**What you see.** The secret was rotated in the store. The Kubernetes `Secret` was updated. The
application still uses the old value, and fails when the old credential is revoked.

**Mechanism.** Two layers of caching:

- A `Secret` mounted as a **volume** is updated by the kubelet on its sync interval (around a
  minute, and up to the kubelet's cache TTL). A `Secret` consumed via **`env` or `envFrom` is not
  updated at all** — environment variables are set at container start and never change.
- Even with a volume, the application must re-read the file. Most read their configuration once at
  startup.

So ESO's "rotation without a deploy" is only true if the application cooperates, and most do not.

**Confirm it.** Check how the secret is consumed:

```bash
kubectl get deploy -n checkout checkout-api -o json | jq -r '
  .spec.template.spec as $s
  | ([$s.containers[].env[]? | select(.valueFrom.secretKeyRef) | "env:\(.valueFrom.secretKeyRef.name)"] +
     [$s.containers[].envFrom[]? | select(.secretRef) | "envFrom:\(.secretRef.name)"] +
     [$s.volumes[]? | select(.secret) | "volume:\(.secret.secretName)"])[]'
```

Anything prefixed `env:` or `envFrom:` will **never** see a rotation without a pod restart.

**Prevent.** Choose one, per service:

1. **Mount as a volume and have the application watch the file.** Best, and requires application
   support.
2. **Trigger a restart on change.** Reloader or Stakater's `reloader` annotation watches the Secret
   and rolls the Deployment:
   ```yaml
   metadata:
     annotations:
       secret.reloader.stakater.com/reload: "checkout-api-db"
   ```
   ⚠️ This means a credential rotation causes a rolling restart of the service. That is usually fine
   and it is a real availability event you should know about rather than discover.
3. **Accept that rotation requires a deploy**, and make the deploy fast. Honest, and it removes
   ESO's main advantage — so decide it deliberately rather than by default.

**And the thing to plan for:** overlapping validity. A rotation where the old credential is revoked
the instant the new one is created cannot be done without downtime by *any* of these mechanisms.
Rotation needs the old and new credentials to both work for a window longer than your restart time.
That is a property of the credential system, not of Kubernetes, and it is the part that gets
forgotten until the first real rotation.

## What to take away

1. **Every secret in a Helm value reaches four places**: its source, the CI log, the rendered
   manifest, and the release Secret's `config` — where it persists for `--history-max` revisions and
   is readable by anyone with `get secrets` in that namespace.
2. **Run the release-values scan on your own cluster.** It is the demonstration that changes minds,
   and it found 61 sensitive keys across 23 namespaces at Riverbend.
3. **SOPS solves half the problem.** It keeps the secret out of Git and the plaintext still lands in
   the release Secret, because Helm sees the decrypted value.
4. **The External Secrets Operator is the right default**, because the chart then contains a *path*
   instead of a secret. A path is an ordinary value: it layers across environments, it appears in
   the drift audit, and it is safe in Git.
5. **The decisive argument is rotation without a deploy.** With a secret in Git, urgent rotation
   means editing, committing, and deploying 240 services. With a secret manager, it means rotating
   in the store.
6. **Use Sealed Secrets when you cannot run an external store** — air-gapped, on-premises — and back
   up the sealing key as a first-class artefact, because losing it makes every existing
   `SealedSecret` permanently unreadable.
7. **`resourceNames` narrows a Secret-reading Role, but not for `list` or `watch`** — Kubernetes
   cannot filter a list by name. Design tooling around `get` on known names.
8. **A credential in a ConfigMap is worse than one in a Secret**: no encryption at rest, broader
   RBAC, and it appears in debugging dumps. Connection strings with embedded credentials are how it
   usually happens.
9. **Never generate secrets in templates.** `lookup` returns empty on every dry run and every
   GitOps render, so the pattern rotates the credential instead of preserving it. Ban the functions
   in CI.
10. **Rotation without a deploy is only true if the application cooperates.** A Secret consumed via
    `env` or `envFrom` never updates. And any rotation needs the old and new credentials to overlap
    for longer than a restart takes — which is a property of the credential system, and the part
    everybody forgets.
