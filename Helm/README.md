# Helm in Production — Use Cases, Failure Modes, Case Studies, and How to Handle Them

A staff-level collection on Helm: what it is actually for, the situations where it is the right
tool and the situations where reaching for it is the mistake, every way it breaks in a real
cluster, and the procedures for getting out of each one.

Helm is unusual among infrastructure tools in that **most of what goes wrong with it has nothing
to do with templating**, which is the only part anybody teaches. Templating is the visible part
and the easy part. The hard parts are the two things Helm quietly also is:

- **Helm is a state store.** Every release keeps its own history inside the cluster, in a Secret
  whose contents most engineers have never looked at. Half the incidents in this collection are
  disagreements between that stored state, the manifests in Git, and the objects actually running.
- **Helm is a lifecycle engine.** It decides ordering, waits for readiness with rules it does not
  document on the command line, runs hooks that are not part of the release it belongs to, and
  rolls back in a way that restores Kubernetes objects and nothing else. The other half of the
  incidents live here.

So this collection is organised around those three Helms — the templater, the state store, the
lifecycle engine — and it keeps saying the same thing in different forms:

> **Helm is a very good manifest renderer wearing the costume of a package manager.** Every time
> you treat it as a package manager — expecting transactions, dependency resolution, uninstall
> that cleans up, rollback that undoes — you are relying on a guarantee it does not make. The
> guarantees it *does* make are narrow, precise, and enough, once you know what they are.

## Who this is for

Read this if two or more of these are true:

- You have more than about twenty Helm releases and no shared template between them, so every
  chart is somebody's personal dialect of YAML.
- You have at least one release that is stuck, or has been stuck, in `pending-upgrade`, and the
  fix somebody found on the internet involved deleting a Secret.
- You have run `helm rollback` in anger and discovered afterwards that something did not roll
  back — a CRD, a database migration, a PVC, a resource somebody had edited by hand.
- You are running Helm underneath Argo CD or Flux and are not completely sure which of the two is
  the source of truth for any given field.
- You are being asked to choose between Helm, Kustomize, an operator, and "just check in the
  YAML", and the comparisons you can find are written by people who maintain one of them.
- Your `etcd` is bigger than you expected and nobody has checked how much of it is Helm release
  history.
- You are preparing for a platform or SRE interview where "walk me through what `helm upgrade`
  does" is a question, because it is a good question.

**If you deploy three services from one repository into one cluster, stop here.** Read
[00](00-what-helm-is-and-what-problem-it-solves.md) for the mental model, read
[01](01-use-cases-when-helm-fits-and-when-it-does-not.md) to check whether you need Helm at all
(you may not), set `--history-max 3` and `--atomic`, and get on with your life. Everything else
in this collection is the cost of *fleet* scale, *multi-environment* promotion, and *third-party*
charts — and if you have none of those three, you have not bought the problems.

## Start here, in this order

1. **[00-what-helm-is-and-what-problem-it-solves.md](00-what-helm-is-and-what-problem-it-solves.md)**
   — read this first even if you have written charts for years. It separates the three Helms,
   defines every term the rest of the collection uses without re-explaining (chart, release,
   revision, manifest, hook, subchart, ownership metadata), and lays out the **sixteen failure
   classes** that every later doc slots into.
2. **[01-use-cases-when-helm-fits-and-when-it-does-not.md](01-use-cases-when-helm-fits-and-when-it-does-not.md)**
   — the use-case doc. Eight concrete situations, scored honestly, plus the five alternatives and
   a decision procedure you can run in ten minutes. If you are choosing a tool rather than
   operating one, this is your doc and you can stop after it.
3. **[04-the-release-as-state.md](04-the-release-as-state.md)** — if you read only one more, read
   this one. Understanding what is inside the release Secret and how the three-way merge uses it
   explains more production Helm behaviour than anything else in the collection.
4. Then read in whatever order matches your problem. The docs cross-reference by scenario ID
   rather than assuming you read them in sequence.

If something is **broken right now**, go straight to
[17-failure-modes-and-the-operations-playbook.md](17-failure-modes-and-the-operations-playbook.md)
— it opens with a triage tree keyed on the error message Helm printed at you — and use
[20-golden-defaults-and-checklists.md](20-golden-defaults-and-checklists.md) for the commands.

If you are **designing a platform**, read 01, then 08 (composition), then 10 (environments), then
11 (GitOps), then 15 (scale). Those five are the architecture of the thing.

If you are **preparing for an interview**, read 00, 04, 05, then the two case studies closest to
your domain in [18](18-case-studies.md), then [19](19-staff-interview-questions.md).

## Topics

### Part 1 — Foundations

| Doc | Covers |
|-----|--------|
| [00-what-helm-is-and-what-problem-it-solves.md](00-what-helm-is-and-what-problem-it-solves.md) | The problem Helm was built for and the three separate tools it turned into; chart / release / revision / manifest / hook defined; what changed from Helm 2 to Helm 3 and why it still matters; the sixteen failure classes and the letters that identify them |
| [01-use-cases-when-helm-fits-and-when-it-does-not.md](01-use-cases-when-helm-fits-and-when-it-does-not.md) | Eight use cases worked through — distributing software you did not write, one service across many environments, a fleet of similar services, operators, day-two configuration, ephemeral environments, bundling a stack, and infrastructure-as-code integration — each with the honest verdict; Kustomize, operators, cdk8s/Timoni/jsonnet, Terraform and plain YAML compared on the axes that matter; a decision procedure |

### Part 2 — Rendering and configuration

| Doc | Covers |
|-----|--------|
| [02-chart-anatomy-and-the-rendering-pipeline.md](02-chart-anatomy-and-the-rendering-pipeline.md) | `T-01…T-14`: every file in a chart and who reads it; the render pipeline step by step; Go templates and Sprig without the hand-waving; `include` versus `template`, `tpl`, `required`, `lookup`, `.Capabilities`; whitespace control derived from the parser's behaviour; YAML type coercion, the 53-character release-name wall, and the install-order sort |
| [03-values-design-and-precedence.md](03-values-design-and-precedence.md) | `V-01…V-12`: coalescing rules derived rather than memorised; the full precedence chain with the two places it surprises people; maps merge and lists do not; `--set` and its four siblings; `null` as a delete instruction; `values.schema.json` and what it cannot check; designing a values contract that survives 240 consumers; the 300-key `values.yaml` problem |
| [04-the-release-as-state.md](04-the-release-as-state.md) | `R-01…R-13`: what is inside `sh.helm.release.v1.<name>.v<n>`, byte by byte and derived; the storage drivers and when to change one; the 1 MiB wall and how charts hit it; `--history-max`; ownership metadata and the "cannot be imported" error; the three-way strategic merge patch worked through on a real object; drift, and the four ways state diverges |

### Part 3 — Lifecycle

| Doc | Covers |
|-----|--------|
| [05-install-upgrade-and-rollback-mechanics.md](05-install-upgrade-and-rollback-mechanics.md) | `U-01…U-15`: what `helm install` and `helm upgrade` do differently; exactly which resource kinds `--wait` understands and which it silently ignores; `--atomic`, `--timeout`, `--force`, `--cleanup-on-fail` and the interactions between them; immutable-field failures and the three ways out; the pending states, how you get into them and how you get out; rollback's five blind spots |
| [06-hooks-migrations-and-ordering.md](06-hooks-migrations-and-ordering.md) | `H-01…H-12`: all nine hook phases and what runs when; weights and why they are not priorities; delete policies and the orphan Jobs you have right now; the critical fact that hook resources are not part of the release; database migrations as a `pre-upgrade` hook versus as a separate pipeline step, costed both ways; making a hook idempotent and re-runnable; the `pre-rollback` hook that made an incident worse |
| [07-crds-and-the-helm-blind-spot.md](07-crds-and-the-helm-blind-spot.md) | `C-01…C-10`: why `crds/` exists, what Helm deliberately refuses to do with it, and the exact sentence in the documentation people miss; the four strategies for CRD lifecycle with the trade-off of each; CRDs inside `templates/` and what it costs; conversion webhooks and upgrade ordering; why your release Secret is 1.4 MB |

### Part 4 — Composition and distribution

| Doc | Covers |
|-----|--------|
| [08-dependencies-subcharts-and-umbrella-charts.md](08-dependencies-subcharts-and-umbrella-charts.md) | `D-01…D-12`: `Chart.yaml` dependencies, `Chart.lock`, and what `helm dependency update` actually resolves; `condition` versus `tags`; `alias` for two copies of the same thing; `import-values`; the `global:` namespace and the collisions it causes at 31 subcharts; library charts as the alternative to inheritance; and the big architectural fork — one umbrella release or 240 small ones — decided with arithmetic |
| [09-packaging-repositories-and-supply-chain.md](09-packaging-repositories-and-supply-chain.md) | `K-01…K-11`: `index.yaml` repositories versus OCI registries, and why OCI won; chart immutability and the version you cannot trust; provenance files, PGP, and cosign; pinning, mirroring, and the "upstream chart changed under us" class of incident; what an SBOM for a chart even means; the arbitrary-resource problem in any chart you did not write |

### Part 5 — Running it for real

| Doc | Covers |
|-----|--------|
| [10-multi-environment-and-multi-cluster.md](10-multi-environment-and-multi-cluster.md) | `E-01…E-12`: the values-layering problem and the four layouts people try; what belongs in the chart versus in the environment, with a test for deciding; promotion that actually promotes the same artefact; per-region and per-cell rollout; Helmfile and what it adds; the environment-drift audit; why your staging cluster is not a staging cluster |
| [11-helm-with-gitops-argo-and-flux.md](11-helm-with-gitops-argo-and-flux.md) | `G-01…G-11`: the two integration models — render-only and real-release — and everything that follows from the choice; what Argo CD's `helm template` approach loses, hook by hook and flag by flag; Flux's `HelmRelease` and the helm-controller; drift detection and self-heal versus Helm's three-way merge; which to pick, and the migration path if you picked wrong |
| [12-secrets-and-sensitive-values.md](12-secrets-and-sensitive-values.md) | `Y-01…Y-10`: why `--set password=` leaks into three places you did not think about; SOPS and `helm-secrets`; Sealed Secrets; the External Secrets Operator and why it is usually the right answer; the `lookup`-then-generate pattern for passwords and the dry-run trap that makes it rotate your credentials during a template render; secrets inside the release Secret |
| [13-security-rbac-and-tenancy.md](13-security-rbac-and-tenancy.md) | `Z-01…Z-11`: Helm 3 uses your credentials, which is both the fix for Tiller and a new problem; what RBAC a CI deploy identity actually needs and the escalation it enables; a chart as an arbitrary-resource-creation primitive; admission policy as the real control; Pod Security Admission interactions; namespace tenancy, quotas, and the tenant who cannot uninstall their own release |
| [14-testing-linting-and-ci.md](14-testing-linting-and-ci.md) | `Q-01…Q-11`: what `helm lint` does and does not catch; `values.schema.json` as a test; `helm unittest` and golden-file rendering; `ct` (chart-testing) in CI; `kubeconform` against the real API version; policy as a test with Conftest; `helm diff` in the pull request as the highest-value check in the list; testing the chart *and* testing the upgrade path, which are different |

### Part 6 — Scale, visibility, and operations

| Doc | Covers |
|-----|--------|
| [15-how-helm-breaks-at-scale.md](15-how-helm-breaks-at-scale.md) | `S-01…S-13`: the scale doc. Riverbend at 40 releases versus 940, with every breakpoint derived — release-Secret bytes in `etcd`, `helm list -A` latency, apiserver watch pressure, render time on an umbrella chart, `--wait` on a fleet, chart-repository fan-in, the CI minutes, and the human limit that shows up before any of the technical ones |
| [16-observability-and-release-inventory.md](16-observability-and-release-inventory.md) | `I-01…I-08`: Helm emits no metrics, so this is about building the inventory yourself; what questions you must be able to answer about 940 releases; a release exporter and the PromQL on top of it; drift detection as a scheduled job; alerts that catch the failures in docs 04–07; the deploy ledger and how it pays for itself in the first incident |
| [17-failure-modes-and-the-operations-playbook.md](17-failure-modes-and-the-operations-playbook.md) | `O-01…O-12`: the triage tree keyed on Helm's actual error strings; the full procedure for each stuck state, including the release-Secret surgery you should almost never do and how to do it safely when you must; adopting existing resources; forced ownership transfer; recovering a release whose chart no longer exists; the uninstall that left things behind; and the runbook template |

### Part 7 — Cases, questions, defaults

| Doc | Covers |
|-----|--------|
| [18-case-studies.md](18-case-studies.md) | `CS-1…CS-9`: nine incidents walked mechanism → signal → recovery → prevention, across all six running systems — including two where Helm behaved exactly as documented and one where the outage was caused by the rollback rather than the deploy |
| [19-staff-interview-questions.md](19-staff-interview-questions.md) | A staff-level question bank with full model answers, the follow-ups a strong answer invites, and the three questions that separate "has used Helm" from "has operated Helm" |
| [20-golden-defaults-and-checklists.md](20-golden-defaults-and-checklists.md) | The annotated golden chart; the golden flag set with every flag justified; a three-tier model so a low-stakes release stays a low-stakes release; new-chart, new-environment, upgrade-day, and incident checklists; the commands, in the order you need them |

## The running systems used throughout

Abstract examples make these docs harder, not easier. Every number in this collection belongs to
one of six concrete systems, and the numbers do not change between docs — so when doc 15 says
"recall that release history is 41% of all Secret bytes", it is pointing at an arithmetic step you
can go and check.

### Riverbend — the primary example

**Riverbend** is an online marketplace. It is the same Riverbend as
[`K8s/cronJobs`](../K8s/cronJobs/README.md), [`Observability`](../Observability/README.md),
[`Kafka`](../Kafka/README.md), [`MicroservicesPOF`](../MicroservicesPOF/README.md) and
[`LogCollector`](../LogCollector/README.md), and this collection adds the Helm facts to it rather
than inventing a new company.

Already established elsewhere, reused here: **240 services**, **38 namespaces**, **412 CronJobs**,
`checkout-api` at 640 → 3,400 requests/second, 67 orders/second, and a production cluster of
**380 nodes running 3,100 pods** on EKS with a 1.29 control plane.

New in this collection, and used everywhere below:

| Fact | Value | Why it matters |
|---|---|---|
| Clusters | 4 — `rb-dev` (60 nodes), `rb-stg` (80), `rb-prod-use1` (380), `rb-prod-euw1` (140) | The multi-environment problem is four-dimensional, not two |
| Service charts | 240, each a 60-line wrapper | The fleet case. They are nearly identical, which is both the opportunity and the trap |
| Library chart | `riverbend-common`, currently `v4.2.0` | 240 charts depend on it. Bumping it is a fleet-wide deploy |
| Third-party charts | 31 (ingress-nginx, cert-manager, external-dns, kube-prometheus-stack, vault, external-secrets, …) | Charts nobody at Riverbend can read all of |
| Releases | **940** — 816 service releases plus 124 platform releases | 240 in dev, 240 in staging, 240 in `use1`, 96 in `euw1` (the customer-facing subset only) |
| Release revisions retained | **7,240** Secrets at `--history-max` default of 10 | Derived in doc 04, and the subject of doc 15 |
| Upgrades per weekday | 45 in production, ~310 in dev | Sets the blast radius of a bad library-chart release |
| Bytes per release revision | **~28 KB** in `etcd` | Derived step by step in doc 04. Every scale claim descends from this number |

⚠️ One unit to be clear about, because two later derivations depend on it: a Riverbend "service
release" is **one service in one cluster**, so `checkout-api` is four releases, not one. When a doc
says "940 releases" it means release-instances across the fleet. When it says "240 services" it
means distinct charts.

### The five systems reused from earlier collections

These appear in doc 18's case studies and wherever a Riverbend-sized example is too small to make
the point. Their scale numbers come from
[`../MicroservicesPOF`](../MicroservicesPOF/README.md) and
[`../LogCollector`](../LogCollector/README.md), which in turn take them from
[`../../../system-design-notes`](../../../system-design-notes), so the three repositories agree.

| System | Shape | Scale that matters here | The Helm problem it demonstrates |
|---|---|---|---|
| **Lantern** | Log platform | 10 TB/hour, 12,000 hosts, 140,000 containers, 24 Kafka brokers, 40 tenants | A chart whose `DaemonSet` reaches 12,000 machines, and a release Secret that will not fit |
| **Gateline** | Ticketing | 5,000 QPS baseline → **500,000 QPS in 10 seconds**, on a published schedule | `--atomic` and `--timeout` when you have a hard deadline and a pre-scaled cluster |
| **Northlight** | Service mesh platform | 100,000 sidecars, 2,500 services, ≤50,000 sidecars per control-plane shard | Composition at 2,500 charts, and a library chart that is a single point of failure |
| **Corridor** | Professional network | 1B members, 3.5M tracking events/s, versioned derived stores | Argo CD's render-only model silently skipping a schema migration |
| **Waypoint** | Ride-hailing | 750,000 GPS writes/s, 290 trips/s, a money ledger alongside a lossy stream | A `pre-rollback` hook that ran a migration in reverse |

## Conventions used across docs

- **Version.** Assume **Helm 3.17** and **Kubernetes 1.29** unless a doc says otherwise. Helm's
  behaviour has changed in ways that matter across 3.x — `--wait` semantics, OCI support,
  `--take-ownership`, `--dry-run=server` — so where a behaviour is version-dependent the doc says
  which version introduced it, marked ⚠ **version**. If you are on a newer release or on Helm 4,
  re-check anything carrying that mark before relying on it; the *mechanism* explanations hold,
  the flag names may not.
- **Scenario IDs.** Failure scenarios have stable IDs and are cited by ID across docs. The letter
  identifies the owning doc: **T**emplating, **V**alues, **R**elease state, **U**pgrade
  lifecycle, **H**ooks, **C**RDs, **D**ependencies, **K** packaging, **E**nvironments, **G**itOps,
  **Y** secrets, **Z** security, **Q** quality/testing, **S**cale, **I**nventory,
  **O**perations. Case studies are `CS-n`.
- **Scenario shape.** Every scenario follows the same five parts: *What you see*, *Mechanism*,
  *Confirm it*, *Recover*, *Prevent*. If you are mid-incident you want the first and third. If you
  are in a design review you want the second and fifth.
- ⚠️ marks a foot-gun that regularly catches experienced engineers — a default that is wrong for
  most people, or a behaviour that contradicts a reasonable assumption.
- **Words used precisely, because Helm's own documentation does not always.** A *chart* is the
  package. A *release* is one installation of a chart under a name in a namespace. A *revision* is
  one version of a release, numbered from 1. The *manifest* is the rendered YAML that a revision
  installed. *Deploying* is ambiguous and is avoided; the docs say install, upgrade, rollback, or
  uninstall.
- **Commands** assume `helm` on your `$PATH`, `$KUBECONFIG` pointing at the cluster in question,
  and `$NS` set to the namespace. Where a command is destructive it is marked and the
  non-destructive way to see what it would do comes first.
- **Derivations are shown.** Where a doc states a number — 28 KB, 41%, 7,240 — the arithmetic that
  produced it is on the page, in steps, with the assumptions named. If an assumption is a guess
  rather than a measurement, it says so, because a number you cannot re-derive is a number you
  cannot argue with in a design review.
