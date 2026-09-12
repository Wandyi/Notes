# Resources, Cost, and Scheduling Pressure

Batch work has a different resource profile from a service, and copying a Deployment's
`resources` block into a `jobTemplate` is how you get a job that is throttled to four times its
necessary runtime, or evicted the moment a node gets busy, or that costs more in idle node
capacity than it does in compute. This doc works through what changes when the workload is
short-lived and bursty rather than continuously running.

## Why a batch profile is not a service profile

Four differences drive everything else:

1. **There is no steady state.** A service's usage over an hour is roughly its usage at any
   instant. `invoice-rollup` uses 3.5 cores for 7 minutes and nothing for 53. Averages describe
   it badly.
2. **Latency does not matter; throughput does.** Nobody is waiting on a p99 response. What
   matters is that the run finishes before the next firing, which makes CPU throttling a much
   worse trade than it is for a service.
3. **Usage scales with input size, not request rate.** A service handling 2× traffic runs 2× as
   many pods. A job handling 2× data runs one pod using 2× the memory — so it grows into its
   limit and OOMs rather than scaling out (F-14).
4. **The work is interruptible, or should be.** That makes batch a natural fit for cheap,
   preemptible capacity — but only if the job is checkpointable (doc 05).

## CPU: request for placement, and think hard before limiting

The two fields do different things and the distinction matters more for batch than anywhere else:

- **`requests.cpu`** is what the scheduler reserves, and it becomes the pod's share weight under
  contention. It does not cap anything.
- **`limits.cpu`** is a hard ceiling enforced by the kernel's CFS quota mechanism: the cgroup gets
  *N* microseconds of CPU per 100 ms period, and when it is spent, every thread is **stopped
  until the next period**.

For a service, a CPU limit is a reasonable blast-radius control. For batch, work out what it
costs. `invoice-rollup` is parallelisable and will happily use 3.5 cores. Give it
`limits.cpu: "1"`:

- Work available: 3.5 core-minutes per minute of wall clock it wants to consume.
- Work permitted: 1 core-minute per minute.
- Runtime becomes roughly 3.5 × 7 = **24.5 minutes instead of 7**.

On a 60-minute interval with `Forbid`, you have moved from 12% interval utilisation to 41% — and
the p99 day, which was 34 minutes, becomes nearly two hours and starts skipping firings. One
innocuous-looking line quadrupled the job's exposure to every overlap failure in doc 02.

Throttling is also worse than the arithmetic suggests for multi-threaded runtimes. A JVM or Go
program with 8 runnable threads burns its 100 ms quota in 12.5 ms of wall clock and then every
thread sleeps for 87.5 ms. Latency inside the job becomes lumpy, connection pools time out, and
progress is worse than a simple ratio predicts.

**Recommendation for batch:**

```yaml
resources:
  requests:
    cpu: "1"          # what it needs to make reasonable progress; drives scheduling
    memory: "2560Mi"
  limits:
    memory: "2560Mi"  # memory limit: yes, and equal to the request
    # cpu: deliberately unset — let the job use idle cores and finish sooner
```

Leaving CPU unlimited lets the job burst into whatever the node is not using, so a run that needs
7 minutes takes 7 minutes.

⚠️ Three conditions on that advice:

- **It requires that batch not be co-located with latency-sensitive services**, or the burst
  becomes someone else's p99 problem. The node-pool section below is the answer.
- **A `LimitRange` in the namespace may inject a default CPU limit** whether you want one or not.
  Check before concluding you have no limit:
  ```bash
  kubectl -n $NS get limitrange -o yaml
  kubectl -n $NS get pod $POD -o jsonpath='{.spec.containers[0].resources}{"\n"}'
  ```
- **Some organisations mandate limits** for cost attribution or policy reasons. If so, set them
  *generously* — at the p99 observed usage, not at the request — rather than at 1 core because
  that is what the Deployment next to it uses.

Confirm whether throttling is actually happening rather than guessing:

```promql
# Fraction of periods in which the container was throttled. Anything sustained above ~0.05
# on a batch job is worth investigating; above 0.25 it is dominating your runtime.
rate(container_cpu_cfs_throttled_periods_total{pod=~"invoice-rollup-.*"}[5m])
  / rate(container_cpu_cfs_periods_total{pod=~"invoice-rollup-.*"}[5m])
```

## Memory: set the limit, set it equal to the request, and size it from data

Memory is not compressible. Exceed the limit and the kernel kills the process (F-14). So unlike
CPU, you do want a limit — the question is what number.

Measure, do not guess:

```promql
# Peak working set per run, over the last 30 days, for this job's pods.
max_over_time(
  container_memory_working_set_bytes{container="rollup", pod=~"invoice-rollup-.*"}[30d]
)
```

`invoice-rollup` measures 700 MB at the median and **1.6 GB at p99**, and the p99 corresponds to
the 240,000-order peak hour. Sizing rules that work:

- **Start from p99, not the median.** A limit at the median OOMs half the time.
- **Add headroom for growth**, because input size grows with the business. 1.6 GB × 1.5 = 2.4 GB,
  rounded to **2560Mi**.
- **Set `requests.memory` equal to `limits.memory`.** This makes the pod **Guaranteed** QoS, which
  matters because of the eviction ordering below.

⚠️ Headroom is not a substitute for bounded memory. If memory scales with input, a limit only
buys time — the honest fix is to make the job **stream**: read in batches of 500, hold a bounded
buffer, never materialise the whole input. Then memory is a function of batch size, and the job
that handled 40,000 orders also handles 4,000,000. Any job whose memory graph is a straight line
against input volume will eventually OOM on your best business day, and raising the limit just
picks a later date.

### QoS classes and who gets evicted first

When a node runs short of memory, the kubelet evicts in a defined order, and your pod's class is
determined entirely by how you set requests and limits:

| QoS class | How you get it | Eviction order |
|---|---|---|
| `BestEffort` | no requests or limits at all | **evicted first** |
| `Burstable` | requests set, limits absent or higher than requests | evicted second, worst-offender first |
| `Guaranteed` | requests == limits for CPU *and* memory | **evicted last** |

Batch pods are disproportionately `BestEffort`, because "it's just a cron job, it doesn't need
requests" is a natural thought. The consequence is that your scheduled work is the first thing
sacrificed whenever any node gets tight, which shows up as F-10: pods dying with no application
error, retry budget draining, nobody able to explain it.

⚠️ Strict `Guaranteed` requires CPU requests to equal CPU limits too — which conflicts with the
"no CPU limit" advice above. You cannot have both. Resolve it by tier:

- **Critical, must-not-be-evicted jobs** (`payout-settlement`): set CPU request == limit and
  memory request == limit. Accept the CPU cap; a settlement run that takes 50 minutes instead of
  40 is fine, being evicted is not.
- **Everything else**: memory request == limit, CPU request set with no CPU limit. That is
  `Burstable`, but with a memory request equal to its real usage it is a *well-behaved*
  `Burstable` pod and is not a likely eviction candidate, because the kubelet ranks by usage
  relative to requests.

## Priority and preemption

`PriorityClass` decides who wins when capacity is short. Two classes cover most batch needs:

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: batch-low
value: 100
preemptionPolicy: Never       # will wait for room; will never evict anything to get it
globalDefault: false
description: "Interruptible scheduled work. Yields to services and to batch-critical."
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: batch-critical
value: 100000                 # above the default for services in this cluster
preemptionPolicy: PreemptLowerPriority
globalDefault: false
description: "Money movement and compliance jobs. Must run on schedule."
```

`preemptionPolicy: Never` on `batch-low` is the important detail and is often missed. It means a
low-priority job pod can still be scheduled the instant there is room, but it will never evict
another pod to create that room. Without it, a low-priority pod can still preempt pods of even
lower priority, and in a cluster where `BestEffort` batch exists you get batch jobs killing other
batch jobs.

`payout-settlement` gets `batch-critical` because 02:00 is not negotiable. `catalog-reindex`,
`db-vacuum`, and `session-reaper` get `batch-low`: they can wait for capacity.

⚠️ Priority is also an *eviction* input under node pressure, so a high priority class is a second
lever alongside QoS for "this must not be killed". Do not hand it out widely — a fleet where 200
of 412 CronJobs are `batch-critical` has no priorities at all.

## Autoscaler interaction and the cost of a cold start

A job pod that does not fit triggers a cluster scale-up. Walk the timeline for a new node:

| Step | Typical time |
|---|---|
| Pod goes Pending; autoscaler notices on its next scan | 10–30s |
| Cloud provider creates and boots the instance | 60–180s |
| Node registers, CNI and DaemonSets become ready | 15–45s |
| Image pull (1.2 GB image on a cold node) | 30–60s |
| **Total time to first line of your code** | **roughly 2–5 minutes** |

For `db-vacuum` (two hours), a four-minute cold start is noise. For `session-reaper` (20 seconds),
it is a **12× overhead**, and it happens every time the reaper's node was scaled away. And
remember from F-09 that Pending time is charged against `activeDeadlineSeconds` — a job with a
300-second deadline can exhaust most of it waiting for a node.

Three ways to buy predictable start latency, in increasing cost:

1. **Right-size `startingDeadlineSeconds` and the deadline to include cold-start time.** Free.
   Just arithmetic: if a scale-up can take 5 minutes, a 300-second `activeDeadlineSeconds` is a
   liability.
2. **Keep a small warm batch node pool** — a minimum size of one or two nodes on the batch pool,
   so short jobs never wait for a boot. For Riverbend, one always-on `m5.xlarge` costs roughly
   $0.19/hour ≈ **$140/month** and removes cold starts for the 288-runs-a-day tier. Compare that
   to an engineer investigating "why did the reaper time out" once a quarter and it is
   straightforwardly worth it.
3. **Overprovisioning with placeholder pods** — a Deployment of low-priority `pause` pods sized to
   one node's worth of capacity. Real batch pods (at normal priority) preempt them instantly, and
   the autoscaler then replaces the placeholders in the background. You pay for one idle node and
   get near-zero scheduling latency. Worth it when many jobs are latency-sensitive; overkill for
   a handful.

Also protect long jobs from scale-*down*. The autoscaler consolidating an under-used node will
happily evict a two-hour vacuum 90 minutes in:

```yaml
jobTemplate:
  spec:
    template:
      metadata:
        annotations:
          cluster-autoscaler.kubernetes.io/safe-to-evict: "false"
```

⚠️ Pair this with `activeDeadlineSeconds` without exception. A hung job carrying
`safe-to-evict: "false"` pins a node forever, and you will find it on the cost report rather than
in your monitoring.

## Node pools: keep batch away from services

The cleanest structural fix for most of this doc is separation. A dedicated batch pool lets you
leave CPU unlimited, use cheap capacity, and stop batch bursts from touching service latency:

```yaml
# On the batch node pool: taint  workload=batch:NoSchedule
jobTemplate:
  spec:
    template:
      spec:
        tolerations:
          - key: workload
            operator: Equal
            value: batch
            effect: NoSchedule
        nodeSelector:
          workload: batch
```

Note both halves: the **toleration** lets the pod onto the tainted batch nodes, and the
**nodeSelector** stops it from landing anywhere else. A toleration alone permits but does not
compel.

Spot or preemptible instances belong here too, and the economics are compelling — typically 60–70%
cheaper — but only for jobs that satisfy two conditions: the work is checkpointable (doc 05), and
`podFailurePolicy` ignores `DisruptionTarget` so reclamations do not consume the retry budget
(doc 03). `payout-settlement` fails the first condition on principle; it runs on on-demand
capacity. `catalog-reindex` passes both and runs on spot.

⚠️ A dedicated pool means jobs are the *only* thing keeping those nodes alive, so the pool scales
to zero between runs and every firing pays a cold start. That is the trade for isolation, and it
is why option 2 above (a warm minimum of one node) usually accompanies this pattern.

## Quota: the failure mode nobody predicts

`ResourceQuota` limits both compute and object counts per namespace:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: batch-quota
  namespace: billing
spec:
  hard:
    requests.cpu: "40"
    requests.memory: 80Gi
    count/cronjobs.batch: "60"
    count/jobs.batch: "200"      # <-- this one causes surprises
    pods: "150"
```

`count/jobs.batch` counts Job objects, **including finished ones that have not been cleaned up.**
Work through how that becomes an outage:

- `billing` has 34 CronJobs. Default history limits keep 3 successful + 1 failed = up to 4 Jobs
  each, so about 136 Job objects at steady state.
- Someone raises `failedJobsHistoryLimit` to 10 across the namespace to improve debugging (good
  instinct, doc 03 recommends it). Ceiling rises toward 34 × 13 = 442.
- The 200-object quota is hit. Every subsequent firing fails with `FailedCreate: exceeded quota`
  (F-06), **for every CronJob in the namespace**, and the cause is a debugging improvement made
  three weeks earlier.

The defences are to set `ttlSecondsAfterFinished` so Job objects actually leave (doc 03), and to
compute the quota from the arithmetic rather than picking a round number:

> `count/jobs.batch` ≥ (number of CronJobs) × (successfulJobsHistoryLimit + failedJobsHistoryLimit)
> + headroom for manual and backfill Jobs.

For `billing` with 34 CronJobs at 3 + 10, that is 34 × 13 = 442, plus headroom: **500**.

⚠️ Quota is also evaluated against `requests`, so a job that requests 8 CPU can be rejected at
admission even though the cluster has capacity, and the event is on the Job rather than on
anything you were looking at.

## What the fleet costs, and where the waste is

Two pieces of arithmetic are worth doing for your own fleet, because the answers are usually
surprising in opposite directions.

**Batch compute is almost always cheap.** Take the heaviest job. `catalog-reindex` sharded eight
ways at 2 vCPU each for 15 minutes is 8 × 2 × 0.25 = **4 vCPU-hours per day**. At roughly
$0.04 per vCPU-hour that is about **$0.16/day, under $5/month** for the single biggest scheduled
workload in the cluster. Optimising the *runtime* of batch jobs is rarely a cost decision; do it
for overlap safety and for bounded blast radius, not for the invoice.

**Reserved-but-idle capacity is where the money goes.** The comparison from doc 00 makes this
concrete. A sleep-loop Deployment doing `session-reaper`'s work reserves its request 24/7:
1440 minutes/day of reserved capacity to perform 288 × 20s = **96 minutes** of work — a 6.7%
duty cycle. The CronJob form pays for the 96 minutes. Extended across a fleet of 412 jobs, the
difference between "every scheduled task is a sleeping Deployment" and "every scheduled task is a
CronJob" is roughly an order of magnitude of reserved capacity, which is real money.

The corollary is the actual fleet-scale waste to look for: **over-requested batch pods**. A job
requesting 4 CPU and 8 GB "to be safe" while using 0.3 CPU and 400 MB forces the autoscaler to
provision capacity for a workload that does not exist — and at 288 firings a day it does so
repeatedly. Audit requests against measured p99 usage:

```promql
# Requested vs actually used, per batch pod. Ratios above ~4 are over-provisioned.
kube_pod_container_resource_requests{resource="memory", namespace="billing"}
  / on(pod, container) group_right
    max_over_time(container_memory_working_set_bytes{namespace="billing"}[7d])
```

## The image-size tax

For short jobs, image pull can dominate. A 1.2 GB image pulled cold takes 30–60 seconds; for
`session-reaper`'s 20-second run that is more time pulling than working, and it is paid on every
new node.

- **Build small.** A static Go binary in a distroless base is 20–40 MB and pulls in about a
  second. The same job built `FROM ubuntu` with a full toolchain is 800 MB. This is usually a
  one-afternoon change with permanent returns.
- **Keep `imagePullPolicy: IfNotPresent`** (which is the default for digest-pinned and tagged
  images other than `:latest`) so a warm node reuses the layers. Digest pinning from doc 07
  makes this safe — the cache can never be stale, because the digest *is* the identity.
- **Share base layers** across your batch images so a node that has run any job has most of the
  layers for the next one.

## What to take away

1. Batch is bursty, throughput-oriented, and scales with input size. A service's `resources`
   block is the wrong starting point.
2. Think twice before setting a CPU limit on batch. A 1-core limit on a job that can use 3.5
   cores does not save money — it quadruples the runtime and pushes the job into the overlap
   failures of doc 02.
3. Always set a memory limit, size it from measured p99 plus growth headroom, and set
   `requests.memory` equal to it. Then make the job stream, because headroom only postpones the
   OOM.
4. Know your QoS class. `BestEffort` batch pods are the first thing the kubelet evicts, which is
   the invisible cause of many "it just failed" reports.
5. Use two priority classes — `batch-low` with `preemptionPolicy: Never`, and a narrowly granted
   `batch-critical`.
6. A cold start costs 2–5 minutes and is charged against `activeDeadlineSeconds`. For frequent
   short jobs, a warm minimum node or overprovisioning pods is cheap insurance.
7. Annotate long jobs `safe-to-evict: "false"`, and never do that without also setting
   `activeDeadlineSeconds`.
8. Separate batch onto its own tainted node pool, put checkpointable work on spot, and keep
   money-movement work on on-demand.
9. Compute `count/jobs.batch` quota from history limits × CronJob count. Finished Jobs consume
   quota, and a namespace-wide `FailedCreate` outage is the result when they accumulate.
10. Batch compute is cheap; reserved-but-unused capacity and oversized images are where the real
    cost and latency live.
