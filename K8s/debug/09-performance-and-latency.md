# Performance & Latency

"It's slow" is the hardest class because slow isn't down — everything looks green. The
discipline: **make the latency observable, decompose it by hop, and separate saturation from
a code/algorithmic problem.** Slow is a distribution, not a number — always look at p99/p999,
not averages.

## Fast triage

| Observation | Suspect | Jump to |
|---|---|---|
| p99 spikes, avg CPU low | CPU throttling / GC / probe flap | [Throttling](#cpu-throttling-masquerading-as-app-latency) |
| Latency correlates with a neighbor's load | Noisy neighbor / oversubscription | [Noisy neighbor](#noisy-neighbors--oversubscription) |
| Periodic 5s stalls | DNS (ndots / conntrack race) | doc 02 (DNS) |
| Slow only cross-node / cross-zone | Network path / topology | [Network latency](#network-latency--topology) |
| Slow dependency calls | Downstream service / pool exhaustion | [Downstream](#downstream--connection-pools) |
| Whole cluster's control ops slow | apiserver/etcd | doc 05 |
| Tail latency on some replicas only | One bad node / pod | [Outlier](#outlier-replica-isolation) |

---

## Decompose the latency

Before blaming Kubernetes, place the latency on the path:

```
client → LB/Ingress → service mesh/kube-proxy → pod → app → downstream (DB/cache/API)
```

Instrument each hop. Without traces you're guessing.

- **RED metrics** per service: Rate, Errors, Duration (p50/p90/p99/p999). If you only have
  one dashboard, make it this.
- **Distributed tracing** (OTel/Jaeger/Tempo) — the single best tool for "where did the time
  go". A trace turns "the request is slow" into "78ms of the 90ms is in the DB call".
- **Compare a slow request to a fast one** of the same type — the diff is the cause.

```bash
kubectl top pods -n $NS --sort-by=cpu
kubectl top nodes
# Prometheus: histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, route))
```

---

## CPU throttling masquerading as app latency

The #1 "mystery latency" in Kubernetes (full treatment in doc 06). Symptoms: p99 latency
spikes, average CPU comfortably under the limit, no errors. The CFS quota throttles short
bursts within 100ms windows even when the 1-minute average is low.

```bash
# the metric that reveals it:
# rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m])
```

If the throttling ratio is non-trivial (>~10–25%), that's your latency. Fix: raise/remove the
CPU limit for latency-sensitive services, set `GOMAXPROCS`/JVM processor count to the limit.
Do not chase the app code until you've ruled this out — it wastes days.

---

## Noisy neighbors & oversubscription

Kubernetes packs multiple pods per node; without proper requests they contend for CPU, memory
bandwidth, disk, and network.

- **CPU contention** — pods without requests (BestEffort) or with tiny requests get starved
  when a co-tenant bursts. Set real CPU requests so the scheduler and CFS give you a share.
- **Memory bandwidth / cache** — not schedulable in vanilla K8s; a memory-heavy neighbor can
  degrade you invisibly. Detect by correlating your latency with node-level pressure and the
  neighbor's activity. Mitigate with anti-affinity or dedicated node pools for latency-critical
  workloads.
- **Disk I/O** — a batch job saturating local disk stalls everyone's writes/logs. Same node,
  same disk. Separate I/O-heavy workloads.
- **Diagnosis**: line up your p99 against `node_*` saturation and per-pod CPU on the *same
  node*. If your latency tracks a neighbor's CPU, that's it.

```bash
kubectl get pods -A -o wide --field-selector spec.nodeName=<node>   # who else is on this node
```

Structural fixes: requests==limits for critical pods (Guaranteed), `podAntiAffinity` to spread,
dedicated node pools + taints for latency-sensitive services, topology-aware placement.

---

## Network latency & topology

- **Cross-zone hops** cost real milliseconds and money. A service chatting to a dependency in
  another AZ adds latency per call; N+1 call patterns amplify it. Use **Topology Aware Routing**
  / `internalTrafficPolicy: Local` to keep traffic in-zone where safe.
- **kube-proxy iptables vs IPVS** — at large Service/endpoint counts, iptables rule evaluation
  grows and adds latency + slow updates. IPVS (or Cilium eBPF replacing kube-proxy) scales
  better. Symptom: latency and endpoint-update lag rising with cluster size.
- **Service mesh overhead** — an Envoy sidecar adds a hop each way (~ms) plus mTLS. Usually
  worth it, but measure; a mesh in the hot path of a chatty internal call can dominate. Ambient/
  sidecarless meshes reduce this.
- **DNS in the latency path** — the ndots + conntrack-race 5s stalls (doc 02) show up as
  bimodal latency (fast, or +5s). NodeLocal DNSCache is the fix.
- **MTU/fragmentation** (doc 02) — large payloads slow/failing while small ones are fine.

---

## Downstream & connection pools

Often the "slow pod" is just waiting on something.

- **Connection pool exhaustion** — the app's DB/HTTP client pool is too small for concurrency;
  requests queue for a connection. Looks like app latency; it's a config number. Check pool
  saturation metrics and raise the pool (bounded by the downstream's capacity).
- **No keep-alive / new TCP+TLS per call** — each downstream call pays handshake cost; also
  churns conntrack (doc 02). Reuse connections.
- **Downstream is the real bottleneck** — the DB is slow, the cache is cold, the external API
  is rate-limiting. Trace it; don't scale the front-end into a slow backend (that just moves the
  queue and can worsen the downstream).
- **Retries amplifying load** — aggressive client retries on a struggling dependency create a
  retry storm that deepens the outage. Budget retries; use circuit breakers.

---

## Outlier replica isolation

When *some* replicas are slow and others fine, isolate the outlier — it's usually a bad node.

```bash
# latency by pod (Prometheus), or:
kubectl get pods -l app=$APP -o wide      # note the nodes of the slow ones
kubectl top pods -l app=$APP
```

- Slow pods clustered on one node → that node is throttled, has a noisy neighbor, degraded
  disk/NIC, or a kernel issue (doc 04). Cordon+drain it and see if latency normalizes.
- Slow pods random → not node-local; likely app/GC/downstream.
- A single replica with a memory leak approaching its limit → GC thrash before OOM shows as
  rising latency then a restart (doc 01).

---

## Profiling in-cluster

When it's genuinely the app, profile it where it runs:

- **pprof (Go)**: expose `/debug/pprof`, then `kubectl port-forward $POD 6060` and
  `go tool pprof http://localhost:6060/debug/pprof/profile?seconds=30` (CPU) or `/heap`.
- **JVM**: async-profiler / JFR via an ephemeral debug container sharing the process namespace
  (`kubectl debug -it $POD --image=... --target=<container>`), doc 10.
- **eBPF, no app changes** — `bpftrace`, `parca`/`pyroscope` continuous profiling, or
  `kubectl debug node` + perf to get flame graphs without redeploying. This is the staff move
  for "can't reproduce, can't add instrumentation, need the truth from prod".
- **Ephemeral containers** (doc 10) let you attach strace/perf/tcpdump to a running pod without
  rebuilding the image or restarting it.

---

## A repeatable latency investigation

1. Confirm it's latency, not errors, and get the **percentile** (p99/p999), not the average.
2. Rule out **CPU throttling** (cheap, common) before anything else.
3. Get a **trace** of a slow request; find which hop owns the time.
4. If it's a hop you own, decide: **saturation** (add capacity / raise limits / pool size) vs
   **code/algorithm** (profile). These have opposite fixes — don't scale a code problem.
5. Check for an **outlier node/replica** before assuming it's systemic.
6. Verify the fix moved the **tail**, not just the average.

---

## Prevention checklist

- RED metrics + tracing on every service *before* you need them.
- Alert on **CPU throttling ratio** and **tail latency**, not averages/CPU%.
- Requests==limits + anti-affinity / dedicated pools for latency-critical workloads.
- NodeLocal DNSCache, connection reuse, keep-alives, bounded retries with circuit breakers.
- Topology-aware routing to avoid needless cross-zone hops.
- IPVS/eBPF dataplane at scale instead of iptables kube-proxy.
