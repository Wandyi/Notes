# Staff Interview Questions

Eighteen questions with full model answers, the follow-ups a strong answer invites, and — at the end
— the three that most reliably separate "has used Helm" from "has operated Helm".

These work in both directions. If you are being interviewed, the model answers are what a strong
response covers. If you are interviewing, the follow-ups are where the signal is: the first answer
to most of these is memorised, and the second is not.

## Part 1 — Mechanism

### Q1 · Walk me through what `helm upgrade` actually does, end to end

**Why it is asked.** It is the single best opening question about Helm, because a complete answer
requires knowing that Helm is three things, and an incomplete answer is almost always incomplete in
the same place: the state store.

**Model answer.** Ten steps:

1. **Load the chart.** Parse `Chart.yaml`, check `apiVersion` and `kubeVersion` against the cluster,
   resolve `dependencies:` against what is in `charts/` on disk. Fails here if `charts/` does not
   contain what `Chart.yaml` asks for — it does not go to the network unless you ran
   `helm dependency update`.
2. **Coalesce values.** Subchart defaults, parent defaults, each `-f` file left to right, then
   `--set`. Maps merge deeply, lists replace wholesale.
3. **Validate against `values.schema.json`**, if present. Earliest and cheapest possible failure.
4. **Render.** Every non-underscore file in `templates/` executed as a Go template against
   `.Values`, `.Release`, `.Chart`, `.Capabilities`, `.Files`. Output concatenated, split on `---`,
   parsed as YAML. Documents that are only whitespace and comments produce no object.
5. **Read the current release** from the Secret `sh.helm.release.v1.<name>.v<n>` in the release
   namespace, highest revision. If its status is `pending-*`, stop with "another operation in
   progress".
6. **Run `pre-upgrade` hooks**, in weight order, waiting for each. A failure aborts before anything
   else changes.
7. **Compute and apply the change.** For each object: a three-way strategic merge patch between the
   previous revision's manifest, the new manifest, and the live object. Objects in the old manifest
   and not the new one are deleted. Applied in a fixed order by resource kind.
8. **Wait**, if `--wait` or `--atomic` — for the kinds Helm has readiness logic for.
9. **Write the new release record.** New revision's Secret created, previous marked `superseded`,
   revisions beyond `--history-max` pruned.
10. **Run `post-upgrade` hooks.** A failure marks the release `failed` — and under `--atomic`
    triggers a rollback of an upgrade that succeeded.

**Follow-ups worth asking.**

- *"At which step does Helm first talk to the cluster?"* — Step 5 for the release, but step 4 may
  too, if the chart uses `lookup` or `.Capabilities`. That is the interesting answer, because it is
  what makes `helm template` and `helm upgrade` produce different output.
- *"Which of those steps writes state you could lose?"* — Step 9. And a process killed between 5 and
  9 leaves `pending-upgrade`.
- *"What happens if step 7 half-succeeds?"* — Without `--atomic`, the release is `failed` and the
  cluster is in whatever partial state was reached. With `--atomic`, a rollback is attempted, which
  can also fail.

---

### Q2 · What is stored in a release Secret, and why does it matter?

**Model answer.** `sh.helm.release.v1.<release>.v<revision>`, one per revision, in the release
namespace, labelled `owner=helm`, `name`, `version`, `status`, `modifiedAt`. The single `data` key is
base64(gzip(JSON)) — and Kubernetes base64-encodes Secret data again, hence the double decode.

The JSON holds `name`, `namespace`, `version`, `info` (status, timestamps, rendered notes), `config`
(the user-supplied values), `manifest` (the rendered YAML), and `chart` — **the entire chart,
including every template file and everything in `charts/` and `crds/`**.

Four things follow, and this is where the answer shows depth:

- **`helm rollback` works without the chart source**, because the chart is in the record. You can
  roll back a release whose chart was deleted from the registry.
- **The chart is most of the bytes** — around 73% for a typical service release — so chart size is
  paid on every revision of every release.
- **`helm get values` shows your secrets**, because `config` is stored verbatim, readable by anyone
  with `get secrets` in that namespace, for `--history-max` revisions.
- **The authoritative status is inside the payload**, not the label. The label is a copy for cheap
  listing, which is why patching the label does not unstick a pending release.

**Follow-ups.**

- *"Roughly how big is one?"* — Tens of kilobytes for a service chart; over 1 MiB for something like
  `kube-prometheus-stack` with its CRDs, which is a hard failure because Secrets cap at 1 MiB.
- *"How would you find out how much `etcd` is release history?"* — List Secrets with
  `--field-selector type=helm.sh/release.v1`, sum the data lengths, multiply by 0.75 to undo the
  base64 the API response added.
- *"Why is it base64'd inside a Secret, which is already base64?"* — Historical: the `configmap`
  driver needed a printable string, and Helm kept one payload format for both drivers.

---

### Q3 · Explain the three-way merge. What happens to a field I changed with `kubectl edit`?

**Model answer.** Three inputs: **old** (the previous revision's manifest, from the release Secret),
**new** (what the chart just rendered), and **live** (a fresh read from the API server). The rule:

> A field's value comes from **new** if new specifies it. A field in **old** and absent from **new**
> is removed. A field in **live** and in neither manifest is left alone.

So a `kubectl edit` to a field the chart manages is **reverted** on the next upgrade — new specifies
it, so new wins. A field added by a mutating webhook or another controller **survives**, because it
is in neither manifest.

That last clause is what makes Helm coexist with sidecar injection and with other controllers.

**The practical consequence, which a strong answer volunteers:** if your chart renders
`spec.replicas` and you run an HPA, every deploy resets the replica count and the HPA spends minutes
climbing back. The fix is to *omit* the field so it falls into the leave-alone case:

```yaml
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
```

**Follow-ups.**

- *"Why 'strategic'?"* — Kubernetes built-in types carry patch-merge-key metadata, so `containers`
  merges on `name` and `env` merges on `name`, rather than lists being replaced.
- *"Does that work for CustomResources?"* — **No.** CRs have no merge metadata, so Helm falls back
  to a JSON merge patch and lists are replaced wholesale. This is the answer that separates people
  who have read about the merge from people who have been bitten by it.
- *"Name another field you should not render."* — `resources` if you run VPA in auto mode. Same
  pattern, same reason.

---

### Q4 · What does `--wait` wait for?

**Model answer.** Only the kinds Helm has readiness logic compiled in for: `Pod`, `Deployment`,
`StatefulSet`, `DaemonSet`, `ReplicaSet`, `ReplicationController`, `PersistentVolumeClaim`,
`Service` (a LoadBalancer needs an address), `CustomResourceDefinition` (the `Established`
condition), and `Job` — but only with `--wait-for-jobs`.

**Everything else, including every custom resource, is "ready" the instant the API server accepts
it.** So a release that installs only a `Certificate`, a `Prometheus`, or an `ExternalSecret`
completes in under a second and verifies nothing. `Ingress` is not in the list either, so `--wait`
does not wait for a route to be programmed.

**The fixes, in order of robustness:** let a GitOps controller with real health assessment do the
waiting (Argo CD's per-kind health checks, Flux's `healthChecks`); add a `post-upgrade` hook Job that
blocks on `kubectl wait --for=condition=Ready`, since hooks *are* waited for; or verify after the
deploy as a pipeline step.

**Follow-ups.**

- *"So what does a green `--atomic --wait` actually guarantee?"* — That the kinds Helm understands
  reached the state Helm checks. A useful and narrow guarantee.
- *"What does that imply for a chart whose entire purpose is a custom resource?"* — `--wait` is
  decorative. This is one of the better arguments for running Helm under Argo CD or Flux.

---

### Q5 · What does `helm rollback` not restore?

**Model answer.** It restores every Kubernetes object in the target revision's manifest. It does not
restore:

1. **Your database.** A `pre-upgrade` migration is not undone.
2. **CRDs.** Anything in `crds/` is never touched — not on upgrade, rollback, or uninstall.
3. **Hook side effects.** A `post-upgrade` hook that sent a webhook or invalidated a cache has
   already done it. `pre-rollback`/`post-rollback` are a *different* set of hooks.
4. **Objects annotated `helm.sh/resource-policy: keep`.**
5. **The image.** The old manifest references a tag; if the registry garbage-collected it, or if a
   mutable tag was overwritten, you get `ImagePullBackOff`.

The fifth is the most common outright rollback failure and is entirely preventable: immutable tags
plus a registry retention window longer than your `--history-max` depth.

**Follow-ups.**

- *"So what makes rollback safe?"* — Backward-compatible migrations. If every schema change works
  with the previous release's code, rolling back the code is sufficient. That is expand/contract, and
  it is the actual answer to the migration problem.
- *"Does rollback need the chart?"* — No, it is in the release Secret. What it does need is the image
  and applicable API versions, both checkable in ten seconds before you commit.
- *"A Kubernetes upgrade happened last week. Anything change?"* — Yes: older revisions' manifests may
  reference API versions the cluster has removed, so those rollback targets are now invalid. Deploy
  everything forward after a control-plane upgrade so rollback stays a valid answer.

## Part 2 — Design

### Q6 · When would you *not* use Helm?

**Why it is asked.** Most candidates cannot answer it, which is the signal. Someone who has only
been told Helm is the standard will reach for "when you have very few services".

**Model answer.** Helm earns its complexity when the template author and the values author are
separated — by organisation, trust boundary, or release cadence. Four cases where they are not:

- **One service, one or two environments, one team.** Kustomize is better: the base is a real
  manifest you can read and validate, and an overlay cannot produce a YAML indentation error. Choose
  Helm here only for `helm rollback` as an in-cluster capability, or because you already run Helm
  for third-party charts.
- **Run-time configuration.** A cache TTL or a feature flag in `values.yaml` builds a configuration
  system whose unit of change is a pod restart. The test: how fast must a change take effect, and who
  may make it? Under a minute, or someone without repository access, means a flag service or a
  watched ConfigMap.
- **Anything with ongoing operational behaviour.** A chart installs a Postgres; an operator runs one
  — failover, backup, volume resize, minor-version upgrade. If a human currently performs that
  behaviour, a chart will not replace them.
- **Bundling a whole platform as one release.** One release is one failure domain, one timeout bound
  by the slowest component, and one shared `global:` namespace. And it cannot express ordering, which
  is the one thing an umbrella is supposed to give you.

**And the case where Helm is unambiguous:** consuming software you did not write, and shipping
software to customers. Nothing else does versioned, parameterised, mirrorable distribution as well.

**Follow-up.** *"You have 14 third-party charts already. Does that change your answer for your own
services?"* — Yes, materially. The tooling, expertise, registry and runbooks are already paid for, so
the marginal cost of the fifteenth chart is small. Cost the tool once, not per use.

---

### Q7 · One umbrella release or a release per component?

**Model answer.** **A release is a failure domain.** So the question is: if component A fails to
upgrade, should component B be rolled back?

Yes for a genuinely atomic unit — a database StatefulSet with its backup CronJob and its monitoring
rules; a webhook and the CA bundle that validates it. No for things that merely install at the same
time, which is most of a platform.

The costs of an umbrella, concretely: a render failure in any subchart blocks upgrading every other
one; `--wait` is bounded by the slowest component for every change including trivial ones;
`--atomic` rolls back the components that worked; `global:` becomes a flat namespace shared by
however many third-party charts you vendored; and the release Secret may exceed 1 MiB.

**And the decisive argument: an umbrella cannot express ordering.** Everything in one release is
ordered only by the kind sort. "cert-manager before external-dns" is inexpressible. What you want for
"one command to build a cluster" is an orchestrator over many releases — Helmfile `needs:`, Flux
`dependsOn`, Argo sync waves.

**Follow-up.** *"Then why do umbrella charts exist?"* — Because distribution is a real use case: a
vendor shipping a stack to a customer wants one artefact and one install command. The failure-domain
cost is worth paying when the alternative is the customer orchestrating 31 releases.

---

### Q8 · Design the values surface for 240 similar services

**Model answer.** Start from the decisions a consumer must make, not from the Kubernetes API. Walking
the API produces a values file that is a slightly worse Deployment manifest — all the indirection of
templating with none of the abstraction.

For a service, the honest list is about 18 leaf values: which image, how many, how big, is it
reachable from outside, what does it talk to, what identity does it use, and how critical is it.

**Everything else is a platform decision, not a knob** — security context, Prometheus annotations,
topology spread, `terminationGracePeriodSeconds`, the label set, the `preStop` hook. The test: *can
you name a legitimate reason for two of these services to differ on it?* If not, it is not a value;
it is a default that a value would only let someone get wrong.

**Prefer one semantic value to seven mechanical ones.** `tier: 1` derives the PDB percentage, the
priority class, the spread constraint's `whenUnsatisfiable`, the grace period, the `preStop` sleep,
and the alert route. One value the service owner understands replaces seven they do not — and the
platform can change what tier 1 *means* across 240 services in one library release.

**Then a `values.schema.json` with `additionalProperties: false`.** That single line turns a
silently-ignored typo into a build failure, and it is the difference between a values file you can
trust and one you cannot.

**Follow-ups.**

- *"Someone asks for a value you have decided is a platform default. What do you say?"* — That they
  open a pull request against the library chart and we discuss whether the default is wrong. That
  conversation is the feature; a value is a way of avoiding it 187 times.
- *"How do you get back from 187 values?"* — Freeze the surface, add the schema so you learn who uses
  what, delete the knobs no template reads, promote the knobs everyone sets identically to defaults,
  and group the genuinely-divergent remainder behind a semantic value. The first two categories are
  usually most of it.
- *"Why is `extraEnv` a map and not a list?"* — Because lists replace wholesale and maps merge. A
  list means an override adding one variable silently drops the platform's OTel endpoint.

---

### Q9 · How do you handle CRDs?

**Model answer.** Helm installs `crds/` once and never touches it again — not on upgrade, rollback,
or uninstall. That is deliberate: deleting a CRD deletes every resource of that kind cluster-wide,
and no package manager should do that as a side effect.

So you own the lifecycle. The question that decides how: **how many releases use this kind?**

- **More than one** — it is shared infrastructure and does not belong inside one release. Take it out
  of Helm: a separate pipeline running `kubectl apply --server-side`, with its own review.
- **Exactly one, and you author the chart** — `templates/` behind an `installCRDs` flag defaulting to
  false, plus `helm.sh/resource-policy: keep` so upgrade works and uninstall does not destroy the
  cluster's data.

**`--server-side` is required, not optional.** Client-side apply stores the whole object in the
`last-applied-configuration` annotation, and large CRDs exceed the 256 KiB annotation cap.

**The failure when you get it wrong is silent in both directions:** the new controller writes a field
the old schema does not know, the API server prunes it without error, and reading the object back
shows it absent.

**Follow-ups.**

- *"What is the argument for taking CRDs out of Helm, in one sentence?"* — A schema change affects
  every team in the cluster, so it should go through a change process every team can see rather than
  arriving inside one team's chart bump.
- *"`--skip-crds` — anything to watch?"* — It is half a change. The other half is the CRD apply step,
  in the same commit. Every incremental change works on a cluster that already has them, so only a
  fresh-cluster bootstrap test catches the omission.
- *"You need to uninstall a chart that ships CRDs. What do you do first?"* — Check whether the
  uninstall will delete them and how many objects depend on each. Two commands, and it is the
  difference between an uninstall and a cluster-wide data-destruction event.

---

### Q10 · Library chart or subchart for shared templates?

**Model answer.** Library chart, and the reason is value scoping.

A **subchart** renders itself inside your release with its own scoped `.Values` — it sees only its
own section of the coalesced values, so the parent's `image.tag` is invisible to it. Bridging that
needs `global:` (a flat namespace shared with every other chart in the release) or `import-values`.

A **library chart** defines named templates that your chart `include`s. `include` executes with the
context you pass it, which is the parent's — so `.Values` inside the library template is the
*service chart's* values, at the top level. No scoping, no `global:`, no bridge.

So for sharing templates across a fleet, a library chart is what you actually want, and it is
noticeably less machinery.

**The costs, honestly:** the library is a fleet-wide single point of failure — a bug reaches
everything that deploys that day. And there is indirection: a developer debugging their Deployment has
to read a template in another repository.

**Which means:** pin exact library versions so a release reaches nobody until somebody bumps it;
canary it through low-tier services; golden-file renders across tiers so the pull request shows the
rendered consequence; and test the *upgrade* path, not just the render, because an immutable-field
change is how a library does the most damage.

**Follow-up.** *"A service needs a shape the library cannot express. What do you do?"* — Let it opt
out of that template and write its own, using the library's smaller helpers for the conventions. A
library that can express every shape has become a values-driven manifest generator, which is what you
built it to avoid. A non-zero opt-out count is a sign the design is healthy.

## Part 3 — Operations

### Q11 · A release is stuck in `pending-upgrade`. Walk me through it.

**Model answer.** First, what this state is: Helm writes a `pending-*` record before doing the work
and updates it afterwards. If the process dies in between — `Ctrl-C`, a CI job timeout, a killed
runner — the record stays forever. **There is no lease, no owner, and no expiry**, so Helm cannot
distinguish a live operation from a dead one and has to refuse.

Then:

**Establish nothing is running.** Read the record's `modifiedAt` label and compare against your
longest `--timeout` plus a rollback. If it is two minutes old, wait — you may be about to collide
with a live deploy. And check whether a pipeline is running, which is the step people skip.

**Then, in order:**

1. **`helm rollback` to the last `deployed` revision.** The supported path, works whenever such a
   revision exists, leaves clean state. Pass the revision explicitly — with no argument it goes to
   the *previous* revision, which may be the pending one.
2. **Delete the pending revision's Secret**, if there is no `deployed` revision to roll back to.
   Back it up first; it is 28 KB. Afterwards, check for orphans — objects the failed revision created
   that the next manifest does not.
3. **Edit the status inside the payload.** Last resort. ⚠️ And note that the commonly-cited fix —
   patching the `status` label — does not work, because the authoritative status is inside the
   gzipped JSON. A label-only fix produces a release that looks healthy in `helm list` and still
   refuses to upgrade.

**Prevention:** never `Ctrl-C` an upgrade (a timeout writes `failed`, which is recoverable; an
interrupt writes nothing, which is not); set the CI job timeout to at least twice `--timeout` plus a
margin; serialise deploys per release with a concurrency group and `cancel-in-progress: false`; and
alert on `pending-*` older than fifteen minutes, because the state never clears itself.

**This is the question where the label-versus-payload detail is the whole signal.** Almost everyone
knows the state; far fewer know that the popular fix is wrong.

---

### Q12 · `helm upgrade` fails with `field is immutable`. What are the options?

**Model answer.** Some fields cannot change after creation — a Deployment's `spec.selector`, most of
a StatefulSet, a Service's `clusterIP`, a Job's `spec.template`, a PVC's size downward.

**First, check whether the change was intentional.** A chart refactor that pulled a version label
into `spec.selector` is a bug, and fixing the template costs nothing. This is the case more often
than people assume, and skipping the check leads straight to a disruptive recovery.

**If it was intentional**, for a Deployment:

```bash
kubectl delete deploy -n checkout checkout-api --cascade=orphan
helm upgrade ... --atomic
```

`--cascade=orphan` is what makes this zero-downtime and is the flag people omit: it leaves the
ReplicaSet and pods serving while Helm creates the new Deployment, which adopts or replaces them
through a normal rolling update. Then clean up the orphaned ReplicaSet.

**For a Service**, there is no orphan option and deleting it changes the `clusterIP`, so it needs a
window or a new Service and a migration.

**`--force` is the wrong answer.** It replaces rather than patches, which for a Deployment means
delete-and-recreate with the default cascade — every pod terminated at once, a full outage for the
duration of the new rollout. It is one flag, which is why people reach for it first.

**Prevention:** selector labels are the stable subset only — `app.kubernetes.io/name` and
`app.kubernetes.io/instance` — with versions and chart versions in `metadata.labels` and the pod
template but never the selector. Plus an upgrade test in CI: install the previous chart version, then
upgrade to this one, in a throwaway cluster. That single test catches the entire class.

---

### Q13 · How do you run a database migration with Helm?

**Model answer.** Two options, and the choice matters less than the thing that makes either safe.

**`pre-upgrade` hook.** The migration and the code are one artefact at one version, and Helm waits.
If it fails, the upgrade aborts before any pod is replaced — the cleanest possible failure. Costs:
`--atomic` can roll back the code and leave the schema migrated; you cannot easily run it alone; and
it is inside the deploy's timeout budget.

**A separate pipeline step.** Its own timeout, retries, and approval gate; the deploy is a pure
deploy so `--atomic` is safe. Costs: two artefacts that can drift, and somebody will eventually
deploy without migrating.

Use a hook for fast migrations and a pipeline step for anything taking minutes.

**But the thing that actually matters: every migration must be backward compatible with the
currently-running code, for at least one release.** Then the ordering question dissolves — `--atomic`
is safe, rollback is safe, and a rollout stuck half-done with both versions running is a non-event.

That is expand/contract. To rename a column: add the new one and dual-write; move reads; stop
writing the old; drop it. Four deploys, and at no point is there a schema the previous release's code
cannot run against.

**The rules that follow:** never drop or rename in the same release that stops using it; never add a
`NOT NULL` column without a default; never narrow a type or add a constraint existing rows could
violate; and make it idempotent, because a hook will run more than once — retries, timeouts,
`--atomic`, concurrent deploys.

**Follow-ups.**

- *"What about `pre-rollback`?"* — Never attach anything destructive to it. A rollback fires
  automatically from a failure with no human deciding, and a down-migration under those conditions
  is how you drop a column you just backfilled at two in the morning. With expand/contract you do
  not need down-migrations in the deploy path at all.
- *"Your hook needs to read a Secret. What RBAC?"* — Ideally none: a `secretKeyRef` in the pod spec
  is resolved by the kubelet, not the pod. If it genuinely needs API access, the RBAC must itself be
  a hook at a lower weight, because hooks run before the manifest — and scoped with `resourceNames`
  to the one Secret.
- *"`backoffLimit`?"* — Above zero is a claim that the migration is idempotent. If it is not, zero,
  and accept that a network blip fails the deploy.

---

### Q14 · How do you handle secrets in Helm?

**Model answer.** Start with why it is a problem: every secret in a Helm value reaches four places —
its source, the CI log, the rendered manifest, and **the release Secret's `config` field**, where it
persists for `--history-max` revisions and is readable by anyone with `get secrets` in that
namespace. `helm get values` prints it.

The options:

- **Plain values** — plaintext in Git. No.
- **SOPS with `helm-secrets`** — solves the Git problem, and ⚠️ **the plaintext still lands in the
  release Secret**, because Helm sees the decrypted value. Half the problem.
- **Sealed Secrets** — nothing plaintext anywhere, works in any GitOps model, no external
  dependency. Rotation requires a deploy, and the sealing key is a per-cluster secret whose loss
  makes every existing `SealedSecret` permanently unreadable.
- **External Secrets Operator** — the chart contains a *path*, which is an ordinary value: it layers
  across environments, appears in the drift audit, and is safe in Git. The secret lives in a manager
  designed for it, with versioning, per-secret IAM, and access logs.

**ESO is the right default, and the decisive argument is rotation without a deploy.** When a
credential must be rotated urgently, the Git-based answer is "edit, commit, deploy 240 services" and
the ESO answer is "rotate in the store".

**Follow-ups.**

- *"ESO rotates a secret. Do your pods pick it up?"* — Usually not. A Secret consumed via `env` or
  `envFrom` **never** updates — environment variables are set at container start. A volume-mounted
  Secret is refreshed by the kubelet, but the application must re-read the file, and most read
  configuration once at startup. So you need a reloader, or application support, or to accept that
  rotation needs a restart.
- *"And the thing people forget?"* — Overlapping validity. A rotation where the old credential is
  revoked the instant the new one exists cannot be done without downtime by any mechanism. The old
  and new must both work for longer than a restart takes.
- *"What about generating a password in the chart with `lookup`?"* — Never. `lookup` returns empty on
  every dry run and every GitOps render, so the pattern rotates the credential instead of preserving
  it — and under a reconciling controller, twenty times an hour.

## Part 4 — Scale and integration

### Q15 · `helm list -A` takes 38 seconds. Why, and what do you do?

**Model answer.** `helm list` does not read labels. It lists every Secret matching `owner=helm` and
**decodes the full payload of each** — base64, gunzip, JSON-parse — to build a release object. So the
cost scales with **revisions**, not releases: at 7,240 revisions and roughly 5 ms each, that is 36
seconds, plus the API server serialising 200 MB.

Three responses:

- **Scope it.** `helm list -n <namespace>` is under a second, and you usually know the namespace.
- **Read labels instead.** Everything `helm list` shows except the chart version is in the Secret's
  labels — name, revision, status, `modifiedAt`. A `kubectl get secrets --field-selector
  type=helm.sh/release.v1` with a jsonpath takes about a second for the same 7,240 Secrets, because
  it never decodes a payload.
- **Reduce the revisions.** `--history-max 5` halves it.

**And the real answer: build an inventory**, because the problem is not the 38 seconds. It is that at
940 releases, five basic questions — which releases are on the old library chart, which have not
deployed in 90 days, which cannot roll back, which are stuck, which differ from Git — go from a
glance to a script. **Inconvenient questions in a large fleet go unasked until an incident forces
them.** Helm emits no metrics, so a small exporter reading release Secrets and emitting Prometheus
series is the fix.

**Follow-up.** *"What do you alert on, versus report?"* — Alert on the states that do not
self-resolve: `pending-*` older than your timeout, a `failed` release still failed hours later, and
the revision count dropping (something is deleting release Secrets). Report on stale releases, old
library versions, orphans and rollback-ability — those drive planned work, and alerting on them
produces a channel nobody reads.

---

### Q16 · You run Argo CD. What changes about Helm?

**Model answer.** The question to answer first: **does your controller *use* Helm, or merely *render*
Helm charts?**

Argo CD defaults to **render-only** — it runs the equivalent of `helm template` and applies the
output itself. So there is **no release Secret**: `helm list`, `helm history`, `helm get values` and
`helm rollback` all return nothing, and Argo CD's own history replaces them. Flux's `HelmRelease`
defaults to a **real release** — the controller performs an actual `helm upgrade`, so all of that
works.

What render-only loses:

- **`lookup` always returns empty.** Not sometimes — there is no cluster connection during a render.
  Every chart generating a password from a `lookup` fallback rewrites it on every reconcile.
- **Hooks map partially.** `pre-install`/`pre-upgrade` → `PreSync`, `post-*` → `PostSync`, and
  `pre-delete`, both rollback phases, and `test` **do not run**.
- **No `helm history`**, which matters because your runbooks reference it.

What render-only *wins*, and a strong answer volunteers this:

- **Custom-resource readiness.** Argo CD assesses health per kind, with Lua for custom kinds — so it
  gates on a `Certificate` actually being issued, which Helm's `--wait` cannot do. If your platform
  is CR-heavy this is the strongest argument for it.
- **Continuous drift correction**, and no release Secrets in `etcd` at all.

**And the worst state: two owners on one object.** Argo CD with `selfHeal` reverts a manual
`helm upgrade` within minutes, while Helm's release Secret now records state that does not match the
cluster. Both systems are working correctly and the result is incoherent. Make it an RBAC boundary —
exactly one writer per namespace — not a convention.

**Follow-ups.**

- *"How do you tell which model a release is on, in one command?"* — Read the ownership metadata on
  any object. `meta.helm.sh/release-name` means Helm's runtime created it;
  `argocd.argoproj.io/instance` means Argo. ⚠️ And note the `app.kubernetes.io/managed-by: Helm`
  *label* proves nothing, because it comes from the chart's template and `helm template` emits it
  too.
- *"An engineer scales a Deployment during an incident and Argo reverts it. What is your answer?"* —
  Decide in advance: commit the change, deliberately pause auto-sync with an alert on being paused,
  or make the field unmanaged. The third is best where it applies — do not render `replicas` under an
  HPA, and Git then has no opinion for Argo to enforce.

---

### Q17 · How would you test a chart?

**Model answer.** The framing first: **testing that a chart renders is not testing that an upgrade
works.** Render failures are cheap and deterministic. The expensive classes — immutable fields, hook
re-runs, a template that silently renders to nothing and therefore *deletes* a live object, a stale
CRD — are properties of the *transition*, and a pipeline that installs the new chart into a clean
cluster cannot see any of them.

In value order:

1. **`helm diff` in the pull request.** Not a test; catches more than everything else combined,
   because it shows a human exactly what will change including things nobody wrote an assertion for.
   And gate on `has been removed`, requiring an explicit acknowledgement in the PR body — a chart
   change that deletes a live object is almost always a bug.
2. **`values.schema.json` with `additionalProperties: false`**, plus a test that the schema actually
   rejects a typo.
3. **Render plus `kubeconform -strict`**, at your cluster's Kubernetes version, with your CRD schemas
   and `--api-versions` so capability-gated objects appear.
4. **`kubectl apply --dry-run=server`** — the only check that tests admission, Pod Security and
   quota.
5. **The upgrade test.** Install the previous commit's chart, upgrade to this one, assert the object
   set did not shrink, then roll back. Five minutes per chart in `kind`.
6. **Golden-file renders**, normalising the image tag and chart-version label so the diffs stay
   readable. Worth it mainly for shared templates.

**`helm lint` is well down that list** — it does not validate against the Kubernetes schema, so
`kind: Deploymnet` passes.

**Follow-ups.**

- *"240 charts and a library-chart change. Do you run the upgrade test 240 times?"* — No. One
  representative chart per tier — three tests, fifteen minutes, covering the shapes that differ.
  Testing all 240 would take twenty hours and find nothing the three do not.
- *"What single check do you add if you can only add one?"* — `helm diff` in the PR.
- *"What makes all the others degrade?"* — Not enforcing the chart version bump. Without it the
  upgrade test has no baseline and golden files diff against the wrong thing.

---

### Q18 · Your `etcd` is growing. Is Helm responsible?

**Model answer.** Probably, and the way to find out is one query.

At a 940-release fleet with `--history-max 10`, release Secrets were **82% of the logical contents of
`etcd`** and **96% of all Secret bytes** — 203 MB against about 249 MB total. Nobody expects that,
because nobody counts it: 7,240 Secrets at 28 KB each, where the 28 KB is mostly the *embedded
chart*, stored in every revision.

```bash
kubectl get secrets -A --field-selector type=helm.sh/release.v1 -o json \
  | jq '[.items[] | ((.data.release // "") | length) * 0.75] | add / 1048576 | floor'
```

⚠️ The `* 0.75` undoes the base64 the API response added; without it you overstate by a third.

**Is it a problem?** 249 MB against `etcd`'s practical 8 GB ceiling is 3%, so it is not an
availability problem. It is a latency, memory and legibility problem: `helm list -A` at 38 seconds,
250–300 MB of API server watch-cache per replica, and every controller with an unfiltered Secret
informer holding its own copy.

**The exception that *is* an availability problem:** `--history-max 0`. At 355 upgrades a weekday
that is 9.9 MB/day, or 2.6 GB/year, reaching the ceiling in about three years — and `etcd` hitting
its limit is a cluster-wide write outage. ⚠️ And the flag is not persisted, so **one** `helm upgrade`
somewhere without it reverts that release to keeping ten.

**Three levers, which took Riverbend from 203 MB to 33 MB:** `--history-max 5`; shrinking the library
chart (dead templates and a proper `.helmignore`); and `--skip-crds` on the platform charts. None is
a trade-off you would not make for other reasons.

**Follow-ups.**

- *"Does the on-disk size match?"* — No. MVCC keeps superseded revisions until compaction and boltdb
  does not shrink without a defrag, so a 249 MB logical dataset commonly occupies 600–900 MB. Compare
  `etcd_mvcc_db_total_size_in_bytes` against `..._in_use_in_bytes`.
- *"Does not vendoring dependencies shrink the release Secret?"* — **No**, and this is a common
  misconception. Helm embeds whatever is in `charts/` at render time either way. Vendoring is about
  your Git repository and build hermeticity. ⚠️ And the advice inverts by renderer: under Argo CD,
  vendor — otherwise every render does a registry round trip; under CI-driven Helm, do not.
- *"Would you move to the `sql` driver?"* — It removes the 1 MiB limit and the `etcd` growth, and it
  puts an external database on your deploy path: if it is down you cannot deploy *or* roll back. It
  is also a much less-travelled path. Shrinking the charts gets you further for less risk.

## The three questions that separate "has used" from "has operated"

If you have time for three, these are the ones. Each has a memorised first answer and a revealing
second.

### 1 · "A release is stuck in `pending-upgrade`. What is the fix you would find on the internet, and why is it wrong?"

The internet says patch the `status` label. **It does not work**, because the authoritative status is
inside the gzipped JSON payload — the label is a copy Helm keeps for cheap listing. Patching only the
label produces a release that looks `deployed` in `helm list` and still refuses to upgrade, and you
lose twenty minutes at the worst moment.

Knowing this requires having opened a release Secret, which is the thing that distinguishes people
who have debugged Helm from people who have used it.

### 2 · "What does `helm rollback` restore, and what does it not?"

Anyone can list "not the database". The revealing part is the image — that a rollback commonly fails
outright with `ImagePullBackOff` because a mutable tag was overwritten or the registry
garbage-collected it — and that a Kubernetes upgrade silently invalidates older rollback targets,
because the stored manifest references API versions the cluster has removed.

Both are things you learn by attempting a rollback under pressure and finding it does not work.

### 3 · "You are adopting Argo CD for charts that currently deploy with `helm upgrade`. What breaks?"

The memorised answer is "hooks work differently". The operated answer names `lookup`: that it returns
empty on every render, that this is not intermittent, and that the failure direction is *dangerous* —
a generate-a-password-once chart rewrites the credential on every reconcile rather than failing. And
then the second-order point: the dev environment cannot catch it, because a dev environment that was
*also* migrated is also being rewritten constantly and nobody noticed.

That chain — mechanism, failure direction, and why the obvious test misses it — is the answer that
comes from having done the migration.
