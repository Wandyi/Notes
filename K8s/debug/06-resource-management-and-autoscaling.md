# Resource Management & Autoscaling

Half of "Kubernetes is slow/flaky" incidents are really resource-model misunderstandings:
requests vs limits, QoS, CPU throttling, and autoscalers that can't do what you assumed. This
doc is about getting the resource model right and debugging when it bites.

## The model in one screen

- **request** = what the scheduler reserves; determines *placement* and QoS. Guarantees you
  *at least* this much.
- **limit** = the hard ceiling the kubelet/kernel enforce. CPU over limit → **throttled**
  (not killed). Memory over limit → **OOMKilled** (killed).
- **QoS class** (derived, not set directly):
  - `Guaranteed`: every container has requests==limits for both cpu & mem. Evicted last.
  - `Burstable`: has requests but not the above. Evicted after BestEffort.
  - `BestEffort`: no requests/limits. Evicted first, first to be OOM-killed.

The single most important asymmetry: **CPU is compressible (throttle), memory is not (kill).**
That's why "no CPU limit" is often fine-to-good, but "no memory limit" is dangerous.

## Fast triage

| Symptom | Cause | Jump to |
|---|---|---|
| App slow/latency spikes, CPU looks "fine" | CPU throttling at the limit | [Throttling](#cpu-throttling) |
| Pod OOMKilled | Memory limit / leak | doc 01 (OOMKilled) |
| Pods Pending "Insufficient cpu/memory" | Requests > allocatable | doc 01 (Pending) |
| HPA not scaling | Metrics / target / bounds | [HPA](#hpa-not-scaling) |
| Cluster Autoscaler not adding nodes | Unschedulable-reason / limits | [CA](#cluster-autoscaler-not-scaling) |
| `drain`/upgrade blocked | PodDisruptionBudget | [PDB](#poddisruptionbudgets-blocking-drain) |
| Pods evicted despite low usage | QoS / requests too low | [QoS](#qos--eviction) |

---

## CPU throttling

The most under-diagnosed performance bug in Kubernetes. Your service has p99 latency spikes,
dashboards show CPU well under the limit *on average*, and everyone's confused. The kernel
enforces CPU limits via **CFS quota over 100ms periods** — a burst that exceeds the quota
*within a period* gets throttled to the next period, adding up to ~100ms of stall even though
the 1-minute-average CPU is low.

```bash
# Prometheus — the smoking gun:
#   rate(container_cpu_cfs_throttled_periods_total[5m])
#     / rate(container_cpu_cfs_periods_total[5m])   > 0.25  => meaningful throttling
#   container_cpu_cfs_throttled_seconds_total
kubectl -n $NS get pod $POD -o jsonpath='{.spec.containers[*].resources.limits.cpu}'; echo
```

- **Low-latency, bursty services** (request/response, especially JVM/Go with parallel GC) are
  hit hardest: they need short bursts of many cores, and a tight CPU limit throttles exactly
  those bursts. Raising average CPU won't show the problem — you must look at throttling ratio.
- **Fixes**: raise or **remove the CPU limit** (keep the request for scheduling) for latency-
  sensitive services — a widely adopted practice; the request still guarantees a floor and the
  scheduler still packs correctly. If you must keep limits, size them well above steady-state.
- ⚠️ **CPU-manager / runtime awareness**: the JVM and Go read available CPUs to size thread
  pools / `GOMAXPROCS`. On a big node with a small CPU *limit*, they may spin up threads for
  all host cores and then throttle brutally. Set `GOMAXPROCS` (via `automaxprocs`) and JVM
  `ActiveProcessorCount` to match the *limit*, not the node.
- Old kernels had a CFS throttling bug (pre-5.4-ish) that over-throttled; on ancient nodes,
  upgrade the kernel.

---

## QoS & eviction

Under node memory pressure the kubelet evicts by QoS then by how far over-request a pod is
(doc 04). Consequences you debug:

- **Critical pod got evicted** — it was `Burstable`/`BestEffort` and lost the lottery. Make it
  `Guaranteed` (requests==limits for cpu+mem) so it's evicted last.
- **BestEffort pods everywhere** — someone shipped workloads with no requests; they schedule
  "for free," oversubscribe nodes, and are the first casualties + the cause of node pressure.
  Enforce requests via a `LimitRange` (defaults) and `ResourceQuota` per namespace.
- **Node oversubscription** — sum of *limits* >> allocatable is fine until everyone bursts at
  once, then the node OOMs/throttles. Sum of *requests* > allocatable can't even schedule.
  Watch the requests/limits/usage triangle.

```bash
kubectl -n $NS get pod $POD -o jsonpath='{.status.qosClass}'; echo
kubectl -n $NS describe limitrange
kubectl -n $NS describe resourcequota
```

---

## HPA not scaling

HorizontalPodAutoscaler adjusts replica count from metrics. When it "doesn't work", it's
almost always metrics availability or a math/bounds issue.

```bash
kubectl -n $NS get hpa $HPA
kubectl -n $NS describe hpa $HPA          # conditions: AbleToScale, ScalingActive, events
```

Ranked causes:

1. **`unknown`/`<unknown>` target metric** → metrics pipeline broken. For CPU/mem HPAs you need
   **metrics-server** running and pods must have **resource requests set** (utilization % is
   `usage/request` — no request means no percentage, HPA can't compute). #1 cause.
   ```bash
   kubectl top pods -n $NS               # if this fails, metrics-server is the problem
   kubectl -n kube-system get deploy metrics-server
   ```
2. **Custom/external metrics adapter down** — `ScalingActive: False` with a metrics API error.
   Check the Prometheus adapter / KEDA scaler.
3. **At `maxReplicas`** already — it *is* scaling, just capped. Raise the ceiling.
4. **Stabilization window / policies** — scale-down is deliberately slow (default 5m
   stabilization) to avoid flapping; "not scaling down fast" is often by design.
5. **Can't schedule the new pods** — HPA bumped replicas but they're Pending (no capacity, and
   CA can't help) → effective desired never reached. Chase the Pending pods (doc 01).
6. ⚠️ **HPA + VPA on the same resource** fight each other unless VPA is in `Off`/recommendation
   mode or scoped to different resources. Don't let both drive the same signal.

---

## Cluster Autoscaler not scaling

CA adds/removes *nodes*. It only acts on pods that are **Pending due to insufficient
resources** and only if a node group *can* satisfy them.

```bash
kubectl -n kube-system logs deploy/cluster-autoscaler --tail=200 | grep -iE 'scale_up|no.node.group|pod.*didn'
kubectl -n kube-system describe configmap cluster-autoscaler-status   # if enabled
```

Reasons CA won't add a node:

1. **Pod isn't Pending-for-capacity** — it's Pending for a different reason (taint, affinity,
   volume zone, PVC). CA won't fix those; it only reacts to resource-shortage Pending.
2. **No node group can fit the pod** — pod requests exceed the largest instance type in any
   scalable group, or its nodeSelector/affinity matches no group. ⚠️ CA can't invent a shape
   you don't have.
3. **At `--max-nodes` / cloud quota / no capacity** — group at max, account quota hit, or the
   cloud has no spot/on-demand capacity in that AZ. Logs say so.
4. **Zone/topology mismatch** — pod needs zone A (pinned volume), group only scales zone B.
5. **`WaitForFirstConsumer` interplay** — usually helps CA (it picks the right zone), but
   misconfig can deadlock.
6. **Scale-down blocked** (opposite complaint: nodes won't remove) — pods with no controller,
   `local` storage, restrictive PDBs, or the
   `cluster-autoscaler.kubernetes.io/safe-to-evict: false` annotation pin the node up.

> Karpenter (increasingly common) reasons differently — it provisions right-sized nodes per
> pending pod shape. Its `kubectl logs` explain per-pod why it did/didn't provision, and it
> avoids the "no matching node group" class of problem.

---

## PodDisruptionBudgets blocking drain

PDBs cap *voluntary* disruптion (drain, upgrades) — not crashes. They protect availability but
can also **block node drains and cluster upgrades** if misconfigured.

```bash
kubectl -n $NS get pdb
kubectl -n $NS describe pdb <name>        # ALLOWED DISRUPTIONS, currentHealthy vs desired
```

- ⚠️ **`minAvailable` == replicas** (or `maxUnavailable: 0`) → zero allowed disruptions →
  `drain` hangs forever and upgrades stall. Very common misconfig. Leave headroom.
- **`ALLOWED DISRUPTIONS: 0` because pods aren't Ready** — if replicas are unhealthy, the PDB
  won't allow more going down; fix the underlying readiness first.
- PDBs don't stop involuntary loss (node crash, OOM). They're for planned operations only.

---

## Getting requests/limits right (the actual fix)

- **Set memory request == limit** for anything you care about → Guaranteed QoS, predictable,
  evicted last. Size from `container_memory_working_set_bytes` p99 + headroom, not guesses.
- **Set CPU request** from steady-state usage; **consider omitting CPU limit** for latency-
  sensitive services (or set it generously) to avoid throttling.
- Use **VPA in recommendation mode** to get data-driven request suggestions before committing.
- Enforce floors/defaults with **LimitRange**, budget namespaces with **ResourceQuota**, so no
  BestEffort surprises.
- Right-sizing is iterative: measure → set → observe throttling/OOM/eviction → adjust.

---

## Prevention checklist

- Alert on CPU throttling ratio, not just CPU utilization.
- Every HPA target: ensure metrics-server + resource requests exist.
- Audit PDBs for `minAvailable == replicas` / `maxUnavailable: 0`.
- Set `GOMAXPROCS`/JVM processor count to the CPU *limit*.
- LimitRange defaults so nothing lands as BestEffort by accident.
