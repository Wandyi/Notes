# Best Practices, the Golden Template, and Enforcing Them

Everything so far has been reasoning. This doc is the output: a default configuration you can copy,
a tiering model so that a log-tidying job does not need the same ceremony as a settlement run, and
the platform machinery that makes the defaults stick across 412 CronJobs written by 30 teams.

The framing matters. At fleet scale, **a best practice that depends on every author remembering it
is not a practice, it is a hope.** The last third of this doc is about converting the list into
admission policy and CI checks, because that is the only version that survives.

## Tier the fleet first

Applying every recommendation in this collection to all 412 CronJobs would be both expensive and
counterproductive — reviewers stop reading checklists that are mostly irrelevant. Three tiers,
assigned by **blast radius of a failure**, keep the ceremony proportional:

| | **Tier 3 — housekeeping** | **Tier 2 — business logic** | **Tier 1 — critical** |
|---|---|---|---|
| Examples | `session-reaper`, `cert-expiry-audit`, log tidying | `invoice-rollup`, `catalog-reindex`, `partner-sftp-export` | `payout-settlement`, compliance exports, anything moving money |
| If it silently stops for a week | Nobody notices | A team notices; data is recoverable | Money, legal, or contractual exposure |
| `startingDeadlineSeconds` | required | required | required |
| `activeDeadlineSeconds` | required | required | required |
| `concurrencyPolicy` | `Forbid` unless justified | `Forbid` or `Replace` | `Forbid` **plus** data-level exclusion |
| Idempotency | nice to have | required | required, **with tests** (doc 05) |
| Run ledger | no | recommended | required |
| Outcome metric | no | required | required |
| Staleness alert | ticket | page during business hours | page 24/7 |
| Digest-pinned image | required | required | required |
| Dedicated ServiceAccount | required | required | required |
| Runbook | one line | one page | one page, reviewed quarterly |
| Backfill procedure | not needed | documented | documented **and rehearsed** |

Two things are required at every tier, and they are the cheapest items on the list:
`startingDeadlineSeconds` (or the job eventually stops forever — F-01) and `activeDeadlineSeconds`
(or one hang stops every future run — F-08). If your organisation adopts exactly two rules from
this collection, adopt those.

Assign the tier as a label so tooling and alert routing can read it:

```yaml
metadata:
  labels:
    riverbend.io/tier: "1"
```

## The golden template

This is the Tier 1 form, with `payout-settlement` as the subject. Every field carries the reason it
is there and a pointer to the doc that derives it, so it can be read as a checklist as well as
copied.

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: payout-settlement                 # <= 52 chars: 11 are appended to the Job name (doc 00)
  namespace: billing
  labels:
    app.kubernetes.io/name: payout-settlement
    app.kubernetes.io/managed-by: argocd
    riverbend.io/team: billing-platform   # who to wake (doc 07)
    riverbend.io/tier: "1"                # drives alert severity and review depth
  annotations:
    riverbend.io/runbook: "https://wiki.riverbend.io/runbooks/payout-settlement"
    riverbend.io/oncall: "#billing-oncall"
    riverbend.io/max-success-age-seconds: "108000"   # 30h: staleness threshold (doc 08)
    argocd.argoproj.io/sync-options: Delete=false     # never pruned automatically (doc 09)

spec:
  # ---------- Timing (doc 01) ----------
  schedule: "0 6 * * *"            # 06:00 UTC. UTC deliberately: 02:00 New York would be
                                   # skipped on spring-forward day (F-04).
  timeZone: "Etc/UTC"              # always explicit; the default is the controller's own zone
  startingDeadlineSeconds: 3600    # a payout up to 1h late is fine; later than that, skip and
                                   # alert. Also bounds the missed-schedule search (F-01).

  # ---------- Overlap (doc 02) ----------
  concurrencyPolicy: Forbid        # two settlement runs would double-pay sellers.
                                   # NOT sufficient on its own: the job also claims its run in
                                   # the job_runs table, which is what stops a manual run
                                   # colliding with the scheduled one (F-15).

  # ---------- Retention (doc 03) ----------
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 10       # default is 1, which loses the evidence you need
  suspend: false                   # declared, so drift is visible (doc 09)

  jobTemplate:
    metadata:
      labels:
        cronjob: payout-settlement     # our own join label; Kubernetes does not add one (doc 04)
    spec:
      # ---------- Work definition (doc 03) ----------
      backoffLimit: 2                  # 3 attempts. Failures here are transient (provider 5xx)
                                       # or permanent (bad config); the policy below separates them.
      # Budget: 3 attempts x 40min p99 (2400s) + backoff waits (10s + 20s) = 7230s.
      # Rounded up with margin, and comfortably inside the 24h interval:
      activeDeadlineSeconds: 7800
      ttlSecondsAfterFinished: 604800  # keep Job objects 7 days: a Monday question about
                                       # Saturday's run must still be answerable (doc 03)
      completions: 1
      parallelism: 1

      podFailurePolicy:                # doc 03; needs restartPolicy: Never
        rules:
          - action: Ignore             # node preemption is not the job's fault
            onPodConditions:
              - type: DisruptionTarget
          - action: FailJob            # bad config or credential: retrying 2 more times is
            onExitCodes:               # 80 wasted minutes and a delayed page
              containerName: settle
              operator: In
              values: [78, 79]

      template:
        metadata:
          labels:
            app: payout-settlement     # what NetworkPolicy selects on (doc 07)
            cronjob: payout-settlement
          annotations:
            cluster-autoscaler.kubernetes.io/safe-to-evict: "false"   # doc 06; safe only
                                                                      # because of the deadline above
        spec:
          restartPolicy: Never              # one pod per attempt = one log per attempt (doc 03)
          serviceAccountName: payout-settlement   # its own identity (doc 07)
          automountServiceAccountToken: false     # it never calls the Kubernetes API
          terminationGracePeriodSeconds: 60       # time to finish the in-flight transfer and
                                                  # mark the run aborted in the ledger
          priorityClassName: batch-critical       # 06:00 is not negotiable (doc 06)

          securityContext:
            runAsNonRoot: true
            runAsUser: 10001
            seccompProfile:
              type: RuntimeDefault

          containers:
            - name: settle
              # Digest, not tag: reproducible, and a registry compromise cannot change what
              # runs without changing this file (doc 07)
              image: registry.riverbend.internal/billing/payout-settlement@sha256:9c4d2f1a8b...
              args:
                - --provider-timeout=30s
                - --max-sellers-per-batch=250      # bounds memory: doc 06
              env:
                - name: RUN_KEY                    # the idempotency key (doc 05)
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.labels['batch.kubernetes.io/job-name']
                - name: POD_NAME                   # attempt id for the ledger
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.name
                - name: DATABASE_URL
                  valueFrom:
                    secretKeyRef:                  # a reference, never a literal value (doc 07)
                      name: billing-db
                      key: url
              resources:
                requests:
                  cpu: "2"
                  memory: "3Gi"
                limits:
                  cpu: "2"                         # Tier 1: request == limit for Guaranteed QoS,
                  memory: "3Gi"                    # accepting the CPU cap to avoid eviction (doc 06)
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities:
                  drop: ["ALL"]
              volumeMounts:
                - name: tmp
                  mountPath: /tmp
          volumes:
            - name: tmp
              emptyDir:
                sizeLimit: 1Gi                     # a runaway temp file must not evict
                                                   # other pods on the node (doc 07)
```

That is long, and deliberately so for a job moving $4.2M a day. The Tier 3 form is much shorter,
and shortening it is the point of tiering:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: session-reaper
  namespace: identity
  labels:
    riverbend.io/team: identity
    riverbend.io/tier: "3"
spec:
  schedule: "7,22,37,52 * * * *"   # offset from :00 to avoid the fleet's herd (doc 01)
  timeZone: "Etc/UTC"
  startingDeadlineSeconds: 200     # required at every tier
  concurrencyPolicy: Forbid
  failedJobsHistoryLimit: 3
  jobTemplate:
    metadata:
      labels: { cronjob: session-reaper }
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 240   # required at every tier
      ttlSecondsAfterFinished: 3600
      template:
        metadata:
          labels: { app: session-reaper, cronjob: session-reaper }
        spec:
          restartPolicy: Never
          serviceAccountName: session-reaper
          automountServiceAccountToken: false
          priorityClassName: batch-low
          securityContext:
            runAsNonRoot: true
            runAsUser: 10001
            seccompProfile: { type: RuntimeDefault }
          containers:
            - name: reaper
              image: registry.riverbend.internal/identity/session-reaper@sha256:9f2c4e...
              args: ["--older-than=24h", "--batch-size=500"]
              resources:
                requests: { cpu: "100m", memory: "128Mi" }
                limits:   { memory: "256Mi" }       # memory limit yes, CPU limit no (doc 06)
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities: { drop: ["ALL"] }
```

Around 35 lines, and it still carries every field whose absence causes a permanent silent failure.
Note the schedule: `7,22,37,52` rather than `*/15`, because the offset is what keeps this job out
of the midnight herd (doc 01) — same frequency, deliberately unaligned.

## The defaults table

If you read only one summary of this collection, read this one. Every row is a Kubernetes default
that is wrong for scheduled work, with the reason and the recommendation.

| Field | Kubernetes default | Recommend | Why the default hurts |
|---|---|---|---|
| `timeZone` | controller's local zone | `"Etc/UTC"`, explicit | Invisible dependency that can change under you (F-03) |
| `startingDeadlineSeconds` | unset | 200s–3600s, sized to the interval | Unset means the missed-schedule wall can stop the job forever (F-01) |
| `concurrencyPolicy` | `Allow` | `Forbid` | `Allow` is safe only for genuinely concurrent-safe work; the default silently permits double execution (F-15) |
| `activeDeadlineSeconds` | unset | 1.5–3 × p99 duration | Unset means a hang runs forever and, with `Forbid`, blocks every future firing (F-08) |
| `backoffLimit` | 6 | 0–3 | Seven attempts spanning 10m30s of backoff is delay, not resilience (doc 03) |
| `restartPolicy` | — (must be set) | `Never` | `OnFailure` discards per-attempt logs |
| `failedJobsHistoryLimit` | 1 | 3–10 | One failure's evidence is overwritten by the next failure |
| `ttlSecondsAfterFinished` | unset | 1h–7d by tier | Unset means objects accumulate until quota breaks the namespace (F-06) |
| `successfulJobsHistoryLimit` | 3 | 3 | Fine as-is |
| `automountServiceAccountToken` | `true` | `false` unless the job calls the API | An unnecessary credential in every pod (doc 07) |
| `serviceAccountName` | `default` | a dedicated SA | Shared identity defeats attribution and least privilege |
| `imagePullPolicy` / image ref | tag-based | digest-pinned | A mutable tag means untested code runs unattended (F-17) |
| `resources.limits.cpu` | unset | leave unset for Tier 2/3; set == request for Tier 1 | A tight CPU limit can multiply runtime and cause overlap (doc 06) |
| `resources.limits.memory` | unset | set, from measured p99 | Unset means node memory pressure decides your fate (doc 06) |
| `priorityClassName` | unset (priority 0) | `batch-low` or `batch-critical` | Unset batch competes equally with everything (doc 06) |

## Enforcing it with admission policy

A list is advice. A `ValidatingAdmissionPolicy` is a rule. This one encodes the non-negotiable
subset in CEL, with no webhook to run or keep alive:

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: cronjob-standards
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups:   ["batch"]
        apiVersions: ["v1"]
        operations:  ["CREATE", "UPDATE"]
        resources:   ["cronjobs"]
  validations:
    # F-01: without this, one long suspension stops the job permanently.
    - expression: >-
        has(object.spec.startingDeadlineSeconds) &&
        object.spec.startingDeadlineSeconds >= 10
      message: "spec.startingDeadlineSeconds must be set and >= 10 (see K8s/cronJobs doc 01)"

    # F-08: without this, one hang blocks every future firing.
    - expression: "has(object.spec.jobTemplate.spec.activeDeadlineSeconds)"
      message: "jobTemplate.spec.activeDeadlineSeconds must be set (see doc 03)"

    # F-07: 11 characters are appended to form the Job name.
    - expression: "size(object.metadata.name) <= 52"
      message: "CronJob name must be <= 52 characters"

    # F-15: Allow is permitted, but only with a written justification on the object.
    - expression: >-
        object.spec.concurrencyPolicy != 'Allow' ||
        (has(object.metadata.annotations) &&
         'riverbend.io/concurrency-justification' in object.metadata.annotations)
      message: "concurrencyPolicy: Allow requires a riverbend.io/concurrency-justification annotation"

    # F-17: mutable tags mean untested code runs unattended.
    - expression: >-
        object.spec.jobTemplate.spec.template.spec.containers.all(c, c.image.contains('@sha256:'))
      message: "container images must be digest-pinned"

    # doc 07: ownership must be discoverable from the object.
    - expression: >-
        has(object.metadata.labels) &&
        'riverbend.io/team' in object.metadata.labels &&
        'riverbend.io/tier' in object.metadata.labels
      message: "riverbend.io/team and riverbend.io/tier labels are required"
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: cronjob-standards-binding
spec:
  policyName: cronjob-standards
  validationActions: ["Deny"]
  matchResources:
    namespaceSelector:
      matchExpressions:
        - key: riverbend.io/enforce-cronjob-standards
          operator: In
          values: ["true"]
```

Three notes on making this land in a live cluster:

⚠️ **Check the API version for your cluster.** `ValidatingAdmissionPolicy` reached GA
(`admissionregistration.k8s.io/v1`) in 1.30. On 1.29 it is `v1beta1` and requires the
`ValidatingAdmissionPolicy` feature gate — so on Riverbend's 1.29 control plane the manifest above
needs the `v1beta1` group version. If neither is available, Kyverno or Gatekeeper express the same
rules; the value is in the rules, not the engine.

⚠️ **Roll out with `validationActions: ["Warn", "Audit"]` first.** Going straight to `Deny` on a
fleet of 412 existing CronJobs breaks the next GitOps sync for everyone whose manifest predates the
policy. Warn, publish the list of violations, let teams fix them, then deny. The namespace selector
in the binding lets you do that namespace by namespace.

⚠️ **Enforce at admission *and* in CI.** Admission is the guarantee; CI is the fast feedback. A
developer who learns about the rule from a failed `kubectl apply` at deploy time has already lost
the afternoon, so run the same checks — as `conftest`/OPA, or the plain script below — in the pull
request.

```bash
#!/usr/bin/env bash
# Minimal CI gate. Not a substitute for admission policy; a faster version of it.
set -euo pipefail
fail=0
for f in "$@"; do
  kind=$(yq '.kind' "$f")
  [[ "$kind" == "CronJob" ]] || continue
  name=$(yq '.metadata.name' "$f")

  (( ${#name} <= 52 )) || { echo "$f: name '$name' is ${#name} chars (max 52)"; fail=1; }

  [[ $(yq '.spec.timeZone // "null"' "$f")                       != "null" ]] \
    || { echo "$f: spec.timeZone not set"; fail=1; }
  [[ $(yq '.spec.startingDeadlineSeconds // "null"' "$f")        != "null" ]] \
    || { echo "$f: spec.startingDeadlineSeconds not set"; fail=1; }
  [[ $(yq '.spec.jobTemplate.spec.activeDeadlineSeconds // "null"' "$f") != "null" ]] \
    || { echo "$f: activeDeadlineSeconds not set"; fail=1; }
  [[ $(yq '.spec.concurrencyPolicy // "Allow"' "$f")             != "Allow" ]] \
    || { echo "$f: concurrencyPolicy is Allow — justify it or change it"; fail=1; }

  yq -e '.spec.jobTemplate.spec.template.spec.containers[].image | test("@sha256:")' "$f" >/dev/null \
    || { echo "$f: image is not digest-pinned"; fail=1; }

  # Schedules aligned to :00 concentrate the fleet into one instant (doc 01).
  case "$(yq '.spec.schedule' "$f")" in
    "@daily"|"@hourly"|"@midnight"|0\ *) echo "$f: aligned schedule — add a per-job offset"; fail=1;;
  esac
done
exit $fail
```

## Share the template, do not copy it

At 412 CronJobs, copy-paste means 412 places to fix when you learn something new — and this
collection is a record of things learned the hard way. Put the defaults in one place:

- **A Helm library chart** (or a Kustomize component) that renders a CronJob from a handful of
  values: name, schedule, image digest, tier, resources. Tier selects the defaults, so a Tier 3 job
  is six lines of values and still gets every field in the table above.
- **Compute the schedule offset in the template**, from a hash of the job name (doc 01), so the
  herd problem is solved by construction rather than by reviewer vigilance. Render the result
  explicitly so the manifest still states the real schedule.
- **Version the chart and record which version each job uses.** When you discover a new default
  worth having, the upgrade is a chart bump and a list of jobs to move, not an archaeology
  exercise.

The payoff is concrete: Riverbend's F-01 incident was fixed once in the chart, and 412 CronJobs got
`startingDeadlineSeconds` on their next sync.

## The reviewer's ten questions

For a pull request that adds or changes a CronJob. These are the questions whose answers are not
visible in the diff, which is exactly why a reviewer has to ask them.

1. **What happens if this runs twice?** If the answer is not "nothing", where is the idempotency
   enforced? (doc 05)
2. **What happens if it does not run today?** Is the next run self-healing via a watermark, or does
   someone owe a backfill? (doc 05)
3. **How long does it take at p99, and what fraction of the interval is that?** Above 50%, the
   schedule or the job needs to change. (doc 02)
4. **Is `activeDeadlineSeconds` larger than a p99 run and smaller than the interval?** Show the
   arithmetic. (doc 03)
5. **Which failures are transient?** Does `backoffLimit` match that answer, rather than the
   default 6? (doc 03)
6. **Does the schedule collide with the fleet's aligned times?** (doc 01)
7. **What is the memory ceiling as input grows?** Is memory a function of batch size or of total
   input? (doc 06)
8. **What credentials does it hold, and is the ServiceAccount dedicated and narrow?** (doc 07)
9. **How will we know it silently stopped?** Name the alert. (doc 08)
10. **Who is paged, and is the runbook written?** (doc 09)

## Anti-patterns, collected

Each of these is a real pattern with a real cost, and each one is derived somewhere in this
collection:

- **`@daily` / `@hourly` / `0 0 * * *` across a fleet.** Concentrates hundreds of firings into one
  instant (F-18).
- **A CronJob that polls.** `*/1 * * * *` to check whether a file has arrived is an event-driven
  problem wearing a schedule; it produces 1,440 firings a day to do nothing (doc 11).
- **A sleep loop in a Deployment.** Reserves capacity 24/7 for a 6.7% duty cycle (doc 00).
- **A shell entrypoint without `set -euo pipefail`.** The most common cause of green-but-broken
  (F-13).
- **A cleanup job bound to `cluster-admin`.** Recurring unattended code execution with full cluster
  control (doc 07).
- **`:latest` on a job that runs nightly.** Untested code, unattended, at 03:00 (F-17).
- **`kubectl delete pod --force` to "unstick" a job.** Removes the API object without confirming
  the container is gone, so two instances can run (doc 04's F-11).
- **A CronJob per tenant.** 500 tenants becomes 500 CronJobs and a control-plane problem; one job
  that loops over tenants is almost always right (doc 11).
- **Chaining jobs by clock offset** — "reindex at 03:00 because rollup finishes by 02:50". Runtimes
  drift and the dependency is invisible. That is a workflow (doc 11).
- **Alerting only on job failure.** Misses six of the eight worst failure modes (doc 08).

## What to take away

1. Tier the fleet by blast radius. Tier 1 gets the full template; Tier 3 gets 35 lines. A checklist
   applied uniformly to 412 jobs is a checklist nobody reads.
2. Two fields are required at every tier because their absence causes *permanent silent* failure:
   `startingDeadlineSeconds` and `activeDeadlineSeconds`.
3. Most Kubernetes defaults are wrong for scheduled work — `concurrencyPolicy: Allow`,
   `backoffLimit: 6`, `failedJobsHistoryLimit: 1`, unset deadlines, an automounted token, an
   implicit time zone. The defaults table is the short version of this whole collection.
4. Encode the non-negotiable subset as a `ValidatingAdmissionPolicy`, roll it out in `Warn` mode
   first, and mirror it in CI so authors get the feedback in the pull request.
5. Render CronJobs from a shared, versioned chart. Then a lesson learned once is applied
   everywhere, which is the only way a fleet this size stays consistent.
6. The reviewer's ten questions are about what the diff does not show: what happens if it runs
   twice, what happens if it does not run, and how you would find out.
