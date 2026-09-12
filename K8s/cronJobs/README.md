# Kubernetes CronJobs — Blueprint, Failure Modes, and Operating Practices

A blueprint for running scheduled work on Kubernetes: what a CronJob is, how it behaves, the
ways it fails, and how to operate a fleet of them. It is broken into numbered docs by
**aspect** — timing, concurrency, retries, correctness, resources, security, observability,
operations, scale — so that each one can be read on its own and linked to from a review comment
or a runbook.

Where a problem is really a pod problem rather than a scheduling problem, this collection points
at [`../debug/`](../debug/README.md) rather than restating it. The two are complementary: that
one is for when you are already paged, this one is for making scheduled work stop paging you.

The bias throughout: **understand the mechanism, then choose the knob.** A CronJob has about a
dozen configurable fields, and almost every production incident involving scheduled work traces
back to one of them being left at its default by someone who did not know the default existed.
So every field here is explained by first showing what breaks without it.

## Who this is for

You should read this if you can say yes to two or more of these:

- You have more than ~20 CronJobs and no consistent template for them.
- You have at least one CronJob whose failure would be noticed by a customer or an auditor,
  rather than by an engineer reading logs a week later.
- You have ever discovered that a CronJob silently stopped running, and found out from a
  downstream symptom rather than an alert.
- You have ever had two copies of the same scheduled job running at once, and had to work out
  afterwards whether that corrupted anything.

If you have three CronJobs that tidy up log files, you do not need a blueprint. Set
`ttlSecondsAfterFinished`, read doc 00 for the mental model, and stop there.

## Start here, in this order

1. **[00-cronjob-primer.md](00-cronjob-primer.md)** — start here even if you have written
   CronJobs for years. It builds the three-object chain (CronJob → Job → Pod), explains which
   controller owns which decision, and walks every field of a real manifest. Every later doc
   assumes this vocabulary.
2. **[01-schedule-syntax-and-time.md](01-schedule-syntax-and-time.md)** — cron expressions from
   first principles, then time zones, daylight saving, missed schedules, and clock skew.
3. Then read in whatever order matches your problem. The docs cross-reference rather than
   assuming you read them in sequence.

If you are here because something is broken right now, start at
**[04-failure-scenarios.md](04-failure-scenarios.md)** for the symptom catalogue, and
**[09-operating-cronjobs.md](09-operating-cronjobs.md)** for the commands and procedures.

## Topics

| Doc | Covers |
|-----|--------|
| [00-cronjob-primer.md](00-cronjob-primer.md) | What a CronJob actually is, the CronJob → Job → Pod chain, the controllers involved, a field-by-field manifest walkthrough |
| [01-schedule-syntax-and-time.md](01-schedule-syntax-and-time.md) | Cron expression semantics, the day-of-month/day-of-week trap, `timeZone`, DST, missed schedules, `startingDeadlineSeconds`, the 100-missed-schedules wall |
| [02-concurrency-and-overlap.md](02-concurrency-and-overlap.md) | `concurrencyPolicy` Allow/Forbid/Replace mechanics, what each one silently drops, overlap detection, external locking when the policy is not enough |
| [03-job-and-pod-mechanics.md](03-job-and-pod-mechanics.md) | `backoffLimit`, `restartPolicy`, retries and backoff timing, `activeDeadlineSeconds`, `podFailurePolicy`, parallelism, history limits, TTL cleanup |
| [04-failure-scenarios.md](04-failure-scenarios.md) | The catalogue: 18 concrete failure modes with mechanism, detection signal, and fix |
| [05-idempotency-and-data-correctness.md](05-idempotency-and-data-correctness.md) | Why Kubernetes gives you at-least-once and never exactly-once, idempotency keys, watermarks, checkpointing, safe backfills |
| [06-resources-cost-and-scheduling-pressure.md](06-resources-cost-and-scheduling-pressure.md) | Requests/limits for bursty work, CPU throttling, QoS, autoscaler cold starts, spot and preemption, priority classes, quota, cost arithmetic |
| [07-security-and-tenancy.md](07-security-and-tenancy.md) | Per-job service accounts, RBAC blast radius, secret handling, Pod Security Admission, network policy, image provenance |
| [08-observability-and-alerting.md](08-observability-and-alerting.md) | Defining an SLO for scheduled work, the metrics that exist, concrete PromQL alerts, the staleness alert that catches silent stalls, logs, tracing and run ledgers |
| [09-operating-cronjobs.md](09-operating-cronjobs.md) | The day-to-day: triage order, manual triggering, suspend/resume discipline, safe deletion, testing in CI, staging parity, GitOps and rollout, and the per-job runbook |
| [10-best-practices-and-golden-template.md](10-best-practices-and-golden-template.md) | The annotated golden manifest, a three-tier model so low-stakes jobs stay simple, and platform-level enforcement via admission policy and lint rules |
| [11-scale-and-alternatives.md](11-scale-and-alternatives.md) | What a fleet of 400 CronJobs costs the control plane, consolidation strategies, and when to move to Argo Workflows, Temporal, Airflow, or an in-process scheduler |
| [12-worked-examples.md](12-worked-examples.md) | Four complete CronJobs from the running example, every field justified, including the two that should not be CronJobs at all |

## The running example used throughout

Abstract examples make these docs harder, not easier, so every doc draws on one made-up but
concrete system. **Riverbend** is an online marketplace running on a managed Kubernetes cluster
(EKS, control plane 1.29). It has **412 CronJobs spread across 38 namespaces**, which is the
scale at which fleet-level problems start to dominate individual-job problems.

Seven of those 412 appear repeatedly, because between them they cover every failure class in
this collection:

| CronJob | Namespace | Schedule | Typical runtime | Why it is interesting |
|---|---|---|---|---|
| `session-reaper` | `identity` | `*/5 * * * *` | ~20 seconds | Runs 288 times a day. Naturally idempotent. The easy case — and the one that generates the most control-plane churn. |
| `invoice-rollup` | `billing` | `10 * * * *` | ~7 minutes | Aggregates the previous hour's orders (up to 240,000 at peak) into invoice lines. Double-running it double-bills. |
| `payout-settlement` | `billing` | `0 2 * * *` | ~40 minutes | Moves roughly $4.2M per day to 18,000 sellers. Must run once per day, and a missed run is a business incident, not a technical one. |
| `catalog-reindex` | `search` | `0 3 * * *` | 1h50m typical, over 3h on catalogue-import days | Sometimes runs longer than its own interval allows. The overlap case. |
| `partner-sftp-export` | `integrations` | `0 6 * * 1-5` | ~4 minutes, fails maybe 1 run in 12 | Depends on a third party's SFTP server. The "retries are the whole design" case. |
| `db-vacuum` | `platform` | `0 4 * * 0` | ~2 hours, I/O heavy | Weekly, expensive, and a noisy neighbour. |
| `cert-expiry-audit` | `platform` | `30 7 * * *` | ~15 seconds | Trivial to run, but its *output* matters, so silent failure is the risk. |

When a doc says "recall that `catalog-reindex` takes almost two hours", it is referring to this
table. Numbers stay consistent across docs so you can follow one job's full story from schedule
choice through alerting.

## Conventions used across docs

- Commands assume `kubectl` 1.27 or newer against a 1.27+ cluster. Where a feature's
  availability depends on the version, the doc says so explicitly, because CronJob-adjacent
  features have moved a lot between 1.24 and 1.33.
- `$NS` = namespace, `$CJ` = CronJob name, `$JOB` = Job name, `$POD` = pod name. Substitute
  inline or export them.
- ⚠️ marks a foot-gun that regularly burns experienced engineers.
- "Control plane" means the apiserver, etcd, and the controllers inside
  `kube-controller-manager` (including the CronJob and Job controllers). "Data plane" means
  kubelet and your pods. The split matters because on a managed cluster you can read control
  plane *behaviour* through the API but you cannot read its logs or restart it.
- Where a manifest is shown, it is complete enough to apply. Fragments are marked as fragments.
