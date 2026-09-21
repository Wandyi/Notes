# Capacity, Autoscaling, and Noisy Neighbours — Having Enough, and Getting More in Time

Capacity failures feel like the simplest class in this collection: you did not have enough, so
add more. That framing is wrong in three ways, and each of them is a doc-length subject.

**First, "enough" is not a utilisation number.** A system at 85% CPU is not 85% fine — it is
already several times slower than the same system at 50%, for reasons that come from queueing
theory rather than from anything about your code. The relationship between utilisation and
latency is not linear and the curve is much steeper than intuition suggests.

**Second, adding more takes longer than failing does.** Doc 04 derived roughly 3.5 minutes from
"signal crosses the threshold" to "new instance serving at normal hit rate." Almost every failure
in this collection is faster than that. Autoscaling is a mechanism for tracking demand growth; it
is not a mechanism for surviving failures, and using it as one is a common and expensive mistake.

**Third, the limits that bite are rarely the ones you set.** A service does not usually run out
of the resource you provisioned. It runs out of file descriptors, or process IDs, or conntrack
entries, or ephemeral ports, or a cloud API's request quota — none of which appear on a capacity
dashboard.

## The utilisation curve, and why 85% is not fine

This is the arithmetic that makes the rest of the doc make sense.

Model a service as a queue: requests arrive at rate `λ`, the server can process at rate `μ`, and
utilisation `ρ = λ/μ`. For a simple queue with random arrivals (an M/M/1 queue — the assumptions
are not exactly true for your service, but the *shape* of the result is robust), the average time
a request spends in the system, relative to its pure service time, is:

```
latency_multiplier = 1 / (1 − ρ)
```

Tabulate it:

| Utilisation `ρ` | Latency multiplier | A 40 ms request becomes |
|---|---|---|
| 0.10 | 1.11× | 44 ms |
| 0.50 | 2.0× | 80 ms |
| 0.70 | 3.3× | 133 ms |
| 0.80 | 5.0× | 200 ms |
| 0.85 | 6.7× | 267 ms |
| **0.90** | **10×** | **400 ms** |
| 0.95 | 20× | 800 ms |
| 0.99 | 100× | **4,000 ms** |

Three things fall out, and they are the reason capacity planning is not "keep it under 100%."

**1. The curve has a knee, and it is around 70–80%.** Below it, adding load costs you a little
latency. Above it, adding load costs you a lot. Running at 90% is not "10% of headroom left"; it
is *already* operating at 10× your service time, and the next 5% of load doubles that again.

**2. The variance is worse than the average.** The table shows mean latency. The tail grows
faster. At `ρ = 0.9` the p99 is far more than 10× the p99 at low load, because the tail is
driven by the queue occasionally being much deeper than average.

**3. This is why utilisation targets exist.** A CPU target of 60–70% in an autoscaler is not
wasteful conservatism; it is the point at which the system is still on the flat part of the
curve. Setting it to 85% "to save money" moves you onto the steep part, where an ordinary traffic
fluctuation produces a latency incident.

And one important qualification, because the M/M/1 model is pessimistic for multi-core servers:
a service with `c` parallel servers (threads, cores, pods) degrades more gracefully — the knee
moves to higher utilisation as `c` grows. A 64-thread service can run at 85% more comfortably
than a single-threaded one. But **only if the work is genuinely parallel**: a service with 64
threads all contending for one lock or one connection pool behaves like a single server with a
64-deep queue, and the M/M/1 curve applies exactly. This is why the contention failures in docs
06 and 10 present as capacity problems.

## Headroom: the `N − k` calculation

The other arithmetic nobody does. "We run 10 instances at 70% utilisation" sounds healthy. The
question is what happens when you lose some.

```
10 instances, each capable of 1,000 req/s → 10,000 req/s capacity
Current load: 7,000 req/s → 70% utilisation ✓

Lose 1 (a node drain, a crash):
  7,000 / 9 = 778 req/s each → 78%  ✓ still on the flat part

Lose 3 (an availability zone, if instances are spread 3/3/4):
  7,000 / 7 = 1,000 req/s each → 100%  ✗ saturated, latency unbounded

Lose 3 while a deploy is in progress (2 more pods unavailable):
  7,000 / 5 = 1,400 req/s each → 140%  ✗ collapse (F-03)
```

So "70% utilisation" is comfortable for a single instance loss and an outage for a zone loss.
The headroom question is not "what is our utilisation?" but:

> **What is our utilisation when we have lost `k` instances, where `k` is the largest failure we
> intend to survive?**

For a service spread across 3 availability zones, `k` is one third of the fleet, because that is
the failure you designed the three zones to survive. The requirement becomes:

```
utilisation_after_losing_one_AZ ≤ 0.70
load / (instances × 2/3 × per_instance_capacity) ≤ 0.70
→ steady-state utilisation ≤ 0.70 × 2/3 = 0.467
```

**You must run at under 47% to survive losing one of three zones while staying on the flat part
of the curve.** That is the real cost of zonal redundancy and it is much higher than most
capacity plans assume. Many teams discover this during their first zone failure.

The alternatives, if 47% is too expensive:

- **Accept degradation during a zone loss** — run at 60%, accept that a zone failure means
  elevated latency (not errors) for the duration. This is often the right trade, and the point is
  to make it a decision rather than a discovery.
- **Four or more zones**, so losing one costs 25% rather than 33%.
- **Shed load during a zone failure** (doc 03), so the surviving capacity serves the most valuable
  traffic rather than degrading everything.
- **Scale up on zone failure** — but this depends on the control plane and on cloud capacity at
  the worst moment (`N-11`), so it is a plan, not a guarantee.

## The failure catalogue

### N-01 · No headroom for the failure you claim to survive

**What you see.** A single AZ failure or a single node drain produces a full outage rather than a
degradation.

**Mechanism.** The arithmetic above, not done.

**Confirm it.** For each service: current utilisation, instance count, zone distribution, and
per-instance capacity (measured by a load test, not assumed). Compute utilisation at `N − k`.
This is a spreadsheet exercise that takes an afternoon and finds real problems in most fleets.

**Prevent.** Set the utilisation target from the `N − k` requirement, enforce a minimum replica
count that makes `k` a small fraction, and use topology spread constraints so instances are
actually distributed:

```yaml
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule      # ScheduleAnyway silently defeats this
    labelSelector:
      matchLabels: {app: checkout-api}
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway     # host spread is best-effort
```

⚠️ `whenUnsatisfiable: ScheduleAnyway` on the zone constraint is the common mistake: it is the
friendlier setting and it means that under scheduling pressure — which is exactly when a zone
has failed — all your pods can end up in one zone. Use `DoNotSchedule` for zone spread and
`ScheduleAnyway` for host spread.

And add a `PodDisruptionBudget`, or a node drain will happily evict everything at once:

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
spec:
  minAvailable: 80%          # not maxUnavailable: 1, which scales badly
  selector:
    matchLabels: {app: checkout-api}
```

### N-02 · Autoscaling on the wrong signal

**What you see.** The autoscaler does not react to real overload, or reacts to something that is
not overload.

**Mechanism.** CPU is the default scaling metric and it is the right one for a minority of
services. It is wrong whenever CPU is not the bottleneck:

| Service shape | Bottleneck | Correct signal | Why CPU fails |
|---|---|---|---|
| I/O-bound API (most services) | Concurrency / downstream latency | **In-flight requests** or queue time | All threads blocked on a database at 15% CPU |
| Queue consumer | Message backlog | **Consumer lag** | Idle consumer waiting on a slow downstream has low CPU |
| WebSocket / SSE server | Connection count and memory | **Connections per instance** | CPU near zero with 50,000 connections |
| CPU-bound (transcoding, ML inference) | CPU | CPU ✓ | — |
| Memory-bound (cache, in-memory index) | Memory | Memory, or a custom working-set metric | CPU is unrelated |
| Latency-sensitive with a hard SLO | The SLO itself | **p99 latency against target** | CPU can be fine while p99 is not |

The most generally correct signal for a request-serving service is **concurrency** (in-flight
requests per instance), because by Little's law it captures both arrival rate and latency, which
is exactly what determines whether you need more instances.

**Prevent.** Pick the signal from the bottleneck. In Kubernetes this means custom or external
metrics rather than the default:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  metrics:
    - type: Pods
      pods:
        metric: {name: http_inflight_requests}
        target: {type: AverageValue, averageValue: "40"}   # from λ × W at target latency
    - type: Resource                                        # CPU as a backstop only
      resource: {name: cpu, target: {type: Utilization, averageUtilization: 80}}
```

With multiple metrics, the HPA scales to satisfy the *largest* requirement, which is what you
want — the primary signal drives normally and CPU catches the case the primary signal misses.

For consumers, KEDA scales on lag directly:

```yaml
triggers:
  - type: kafka
    metadata:
      consumerGroup: order-processor-group
      topic: orders.created
      lagThreshold: "500"          # messages of lag per replica
```

### N-03 · Scaling slower than the failure

**What you see.** Autoscaling adds capacity for a spike that peaked five minutes ago.

**Mechanism.** Doc 04 (`F-08`) derived the ~3.5-minute floor. Restating with where the time goes,
because knowing which term dominates tells you what to optimise:

```
Metric scrape interval                  15 s
HPA evaluation period                   15 s
Metric averaging window                 60 s   ← often the biggest term
Scheduling decision                      2 s
Node available? (if not, +cluster autoscaler)  +60–180 s
Image pull (if not cached)              10–120 s ← the other big term
Container start                          5–30 s
Readiness + warm-up                     30–120 s
                                        ─────
Typical total                           ~3–5 min; with a new node, 6–10 min
```

**Prevent.** Attack the big terms:

- **Pre-pull images** onto every node (a DaemonSet that pulls, or the node image baked with
  them). This removes the largest variable term.
- **Over-provisioned placeholder pods**: low-priority pods that reserve node capacity and are
  evicted instantly when a real pod needs the space. This converts "wait for a node" into "wait
  for a pod", removing 60–180 seconds.
- **Shorten the metric window** to 30 s, accepting more noise, and compensate with a scale-down
  stabilisation window.
- **Predictive / scheduled scaling** for known patterns. Gateline scales for the sale on a
  schedule; Riverbend scales for the daily peak on a schedule. Reactive autoscaling then only has
  to handle the residual.
- **And most importantly: do not rely on autoscaling for failure survival.** Headroom is the
  mechanism for failures; autoscaling is the mechanism for demand growth. Conflating them is
  `N-01`.

### N-04 · Oscillation

**What you see.** Replica count sawtoothing, with a latency spike at each scale-down.

**Mechanism.** `F-08`. The control loop's period is comparable to its response time, which is the
classic recipe for oscillation in any feedback system.

**Prevent.** Asymmetric behaviour: fast up, slow down.

```yaml
behavior:
  scaleUp:
    stabilizationWindowSeconds: 0        # react immediately
    policies:
      - type: Percent
        value: 100                        # double at most
        periodSeconds: 30
      - type: Pods
        value: 10                         # or +10, whichever is more
        periodSeconds: 30
    selectPolicy: Max
  scaleDown:
    stabilizationWindowSeconds: 600      # 10 minutes of sustained low load
    policies:
      - type: Percent
        value: 10                         # shed at most 10% per minute
        periodSeconds: 60
```

The asymmetry is deliberate and the reasoning is economic: the cost of scaling up unnecessarily
is a few minutes of extra instances. The cost of scaling down prematurely is an incident. Those
are not comparable, so the policy should not be symmetric.

### N-05 · Scale-to-zero and the cold start

**What you see.** The first request after an idle period takes 3 seconds. Or times out.

**Mechanism.** Scaling to zero eliminates idle cost and makes the next request pay the full start
cost — image pull, process start, JIT, cache warm, connection establishment. `F-10`'s cold start,
concentrated into one unlucky user's request.

**Prevent.** Scale to zero only for workloads where the latency is genuinely acceptable (internal
tools, batch triggers, development environments). For anything user-facing, keep a minimum of
one or two warm instances — the cost of two small instances is almost always less than the cost
of the engineering time spent discussing whether to scale to zero. Where the platform supports
it, use provisioned concurrency or a warm pool.

### N-06 · CPU limits and CFS throttling

**What you see.** p99 latency far above p50, in multiples of ~100 ms, with CPU utilisation
reported well below the limit. Profiling shows nothing. This is the strangest-looking failure in
the collection and one of the most common.

**Mechanism.** Worth deriving carefully, because the behaviour is genuinely counter-intuitive and
the reported metrics actively mislead.

A Kubernetes CPU limit is enforced by the Linux Completely Fair Scheduler's bandwidth control:
in each **period** (default 100 ms), the cgroup may use `limit × period` of CPU **quota**. With
`limits.cpu: 1`, that is 100 ms of CPU per 100 ms of wall time.

Now run a process with 8 worker threads on a 16-core node. All 8 threads can run in parallel. In
12.5 ms of wall time they collectively consume `8 × 12.5 ms = 100 ms` of CPU — the entire quota.

**For the remaining 87.5 ms of the period, every thread in the container is stopped.** Not
slowed. Stopped. Descheduled until the next period begins.

```
Period:  |--- 12.5 ms running ---|--------- 87.5 ms throttled ---------|
```

Consequences:

- Any request in flight when the quota is exhausted takes up to 87.5 ms longer, for no reason
  visible in the application.
- Average CPU utilisation reports as `12.5 / 100 = 12.5%` of a core — so the dashboard says the
  container is nearly idle while it is being throttled 87.5% of the time.
- The effect worsens with more threads: the quota is consumed faster, so the throttled fraction
  of each period is longer. **A runtime that sizes its thread pool from the node's core count
  rather than the container's limit is the usual cause**: a JVM or a Go program on a 64-core node
  with `limits.cpu: 2` will create dozens of threads against a 200 ms-per-second budget.
- It feeds `F-05`: higher latency means more concurrent requests means more threads active means
  faster quota burn.

**Confirm it.** The metric is unambiguous and most dashboards do not show it:

```
rate(container_cpu_cfs_throttled_periods_total[5m])
  / rate(container_cpu_cfs_periods_total[5m])
```

Any sustained value above ~0.01 is worth investigating; above 0.1 it is your latency problem. You
can also read it directly:

```bash
cat /sys/fs/cgroup/cpu.stat        # cgroup v2
# nr_periods, nr_throttled, throttled_usec
```

**Prevent.** Three options, and the choice is genuinely debated:

1. **Set CPU requests accurately and omit CPU limits** for latency-sensitive services. Requests
   guarantee a share under contention; without a limit, a container can burst into idle capacity
   instead of being throttled. The risk is a runaway container starving neighbours — mitigated by
   accurate requests (which govern scheduling and the contention share) and by monitoring. This
   is what a growing number of large Kubernetes operators do, and it is the recommendation for
   user-facing services.
2. **Set generous limits** (2–4× the request) so throttling is rare, if your organisation
   requires limits.
3. **Make the runtime container-aware**, always, regardless of the above:
   - Go: `GOMAXPROCS` must match the CPU limit, not the node's core count. Use
     `go.uber.org/automaxprocs` — it reads the cgroup limit at startup. This single import fixes
     a large fraction of real throttling incidents.
   - Java 10+: `-XX:+UseContainerSupport` is on by default and respects cgroup limits for
     `availableProcessors()` and heap sizing. Verify it is not disabled.
   - Node.js: `UV_THREADPOOL_SIZE` and any worker pool must be sized from the limit.

### N-07 · Memory limits and the OOMKill loop

**What you see.** Pods restarting with exit code 137 and reason `OOMKilled`. Often all of them,
around the same time.

**Mechanism.** Memory is *incompressible*: unlike CPU, you cannot give a process less memory and
have it run slower. Exceeding the limit means the kernel kills the process, immediately, with no
opportunity to clean up.

Why it happens in clusters:

- **The runtime does not know its limit.** A JVM with no `-XX:MaxRAMPercentage` defaults to a
  fraction of *host* memory, so a JVM in a 2 GiB container on a 64 GiB node may size its heap
  at 16 GiB and be killed as it grows into it. Same problem as `N-06`, different resource.
- **Off-heap memory is not counted by the runtime but is counted by the cgroup**: direct byte
  buffers, thread stacks (1 MB × thread count), metaspace, JIT code cache, native libraries,
  and the page cache for files the container reads.
- **A slow leak** crosses the limit after hours or days — and because all pods started at the
  same time, they all cross it at the same time, which is a correlated failure (doc 00) and looks
  like an external event.
- **A traffic spike** increases in-flight requests, which increases memory per `R-13`.

And then the loop: the pod is killed, restarts cold (`F-10`), its share of traffic moves to
siblings which are also near their limit, and they are killed too (`F-03`).

**Confirm it.**

```bash
kubectl get pods -o json | jq -r '.items[] |
  select(.status.containerStatuses[]?.lastState.terminated.reason == "OOMKilled") |
  "\(.metadata.name) restarts=\(.status.containerStatuses[0].restartCount)"'
```

```
# Working set against limit — the ratio to watch
container_memory_working_set_bytes / container_spec_memory_limit_bytes
```

**Prevent.**

- **Tell the runtime its limit.** `GOMEMLIMIT` for Go (a soft limit that makes the GC work harder
  rather than being killed); `-XX:MaxRAMPercentage=75` for the JVM (leaving 25% for off-heap);
  `--max-old-space-size` for Node.
- **Set `requests == limits` for memory.** Memory is incompressible, so a memory request lower
  than the limit means you are gambling that the node has spare memory when you need it — and
  when it does not, you are evicted rather than throttled. Guaranteed QoS (`N-08`) is the right
  class for anything that matters.
- **Alert on working set / limit above 80%**, which gives you warning before the kill.
- **Size the limit from measurement under load**, including off-heap, not from the runtime's heap
  setting.

### N-08 · QoS class and eviction order

**What you see.** Pods evicted from a node under pressure, and the ones evicted are the important
ones.

**Mechanism.** Kubernetes assigns a Quality of Service class from the relationship between
requests and limits, and evicts in that order under node pressure:

| Class | Condition | Evicted |
|---|---|---|
| `Guaranteed` | requests == limits, for **both** CPU and memory, on every container | Last |
| `Burstable` | requests set, and less than limits (or limits unset) | Second, ordered by how far usage exceeds the request |
| `BestEffort` | no requests or limits | **First** |

A service with no resource specification at all is `BestEffort` and is the first thing killed
when any node it lands on comes under pressure. A team that omits resources "because we do not
know the right values" has chosen to be evicted first.

**Prevent.** Set requests on everything, and `requests == limits` for memory on anything
user-facing. Combine with `PriorityClass` so that, independently of QoS, the scheduler preempts
low-priority workloads (batch jobs, the placeholder pods from `N-03`) before production ones.

### N-09 · The limit you did not know existed

**What you see.** A failure that has nothing to do with CPU or memory: "too many open files",
"cannot fork", "no space left on device" on a node with a half-empty disk, or connection failures
across every pod on one node.

**Mechanism.** Node-level resources that are shared, finite, and absent from every capacity
dashboard:

| Resource | Default | Symptom when exhausted | Who consumes it |
|---|---|---|---|
| File descriptors | 1,024 soft / 1,048,576 hard per process | `EMFILE: too many open files` | Every socket, file, and pipe. A connection leak is the usual cause. |
| Process IDs (per node) | `kernel.pid_max`, often 4 million; **per-pod `pids_limit` often 4,096** | `fork: resource temporarily unavailable` | Thread-heavy runtimes; a fork bomb in one pod |
| conntrack entries | `nf_conntrack_max`, often 131,072–262,144 | Random connection failures and timeouts, **for every pod on the node** | Every tracked connection; a high connection-rate pod exhausts it for its neighbours |
| Ephemeral ports | 28,232 by default (`E-10`) | `EADDRNOTAVAIL` | Outbound connections without pooling |
| Inodes | Set at filesystem creation | `no space left on device` with free bytes | Many small files: logs, cache files, container layers |
| Ephemeral storage | Node disk | Pod eviction | Logs written to the container filesystem; `emptyDir` |
| ARP table | `gc_thresh3`, default 1,024 | Intermittent network failures in large clusters | Many pods per node |

The two that cause the most confusing incidents are **conntrack** and **inodes**, because both
present as something else entirely and both are node-scoped, so one pod's behaviour breaks every
pod on the node — the purest form of noisy neighbour.

**Confirm it.**

```bash
# conntrack
cat /proc/sys/net/netfilter/nf_conntrack_count /proc/sys/net/netfilter/nf_conntrack_max
# Or via node-exporter: node_nf_conntrack_entries / node_nf_conntrack_entries_limit

# File descriptors, per process
cat /proc/<pid>/limits | grep 'open files'
ls /proc/<pid>/fd | wc -l

# Inodes
df -i

# PIDs in a pod
cat /sys/fs/cgroup/pids.current /sys/fs/cgroup/pids.max
```

**Prevent.** Monitor all of them at the node level — most are exposed by node-exporter and simply
not put on dashboards. Raise limits where appropriate (`nf_conntrack_max`,
`net.ipv4.ip_local_port_range`, `fs.file-max`). Set per-pod `pids_limit` so one pod cannot
exhaust the node's. Ship logs off the node rather than writing them to the container filesystem.
And connection-pool everything, which addresses fds, conntrack, and ephemeral ports at once.

### N-10 · The noisy neighbour

**What you see.** A service's latency degrades with no change to its own traffic, code, or
configuration. Correlated with something else's activity.

**Mechanism.** Shared resources that the scheduler does not account for:

- **CPU cache and memory bandwidth.** Two containers on the same socket compete for L3 cache. A
  batch job doing a large scan evicts a latency-sensitive service's working set from cache, and
  the service's instruction-per-cycle rate halves. Neither container exceeds any limit.
- **Disk I/O.** `requests`/`limits` do not cover IOPS by default. One pod doing a large sequential
  read saturates the node's disk queue, and everyone's fsync latency goes up — which, if one of
  those neighbours is etcd, takes out the cluster's control plane (`D-13`).
- **Network bandwidth.** Not limited by default. One pod can saturate the node's NIC.
- **The conntrack and PID tables** from `N-09`.
- **The kernel itself**: a pod doing very high syscall rates consumes kernel CPU attributed
  oddly.

**Prevent.**

- **Separate node pools by workload class.** Latency-sensitive services on one pool, batch and
  analytics on another, with taints and tolerations enforcing it. This is the blunt and effective
  answer, and it is what most mature clusters converge on.
- **Pod anti-affinity** so replicas of the same service are not co-located (which also serves
  `N-01`).
- **I/O and network limits** where the runtime supports them (`blkio` cgroups, CNI bandwidth
  plugins).
- **Dedicated nodes for control-plane-critical workloads** — etcd should never share a disk with
  anything.
- **Monitor the *victim's* resource efficiency**, not just utilisation: a sudden drop in
  instructions-per-cycle or a rise in cache-miss rate, with no change in traffic, is the
  signature of cache contention and is otherwise invisible.

### N-11 · The cluster autoscaler cannot get nodes

**What you see.** Pods stuck `Pending` with `FailedScheduling`, and no new nodes appear.

**Mechanism.** Autoscaling assumes the cloud has capacity. It does not always. Causes:

- **Instance-type capacity unavailable** in that zone. Common for large or specialised instance
  types (GPU, high-memory) and during a regional incident when everyone is failing over at once —
  which is precisely when you need it.
- **Spot instance reclamation** with no on-demand fallback.
- **Account quota exhausted**: vCPU quota, address quota, volume quota. These are per-region
  limits many teams have never checked.
- **No free IP addresses in the subnet.** Each node and (with some CNIs) each pod consumes one.
  A /24 subnet holds 251 usable addresses, which is a lot of pods until it is not.

The compounding problem is timing: this fails during a regional failover, which is when
everybody else is also asking for capacity in the surviving zones.

**Prevent.**

- **Diversify instance types** in the node group, so "no `m5.2xlarge`" does not mean "no
  capacity."
- **Reserved capacity or capacity reservations** for the baseline you cannot do without.
- **Monitor quota headroom** as a first-class metric, and request increases *before* they bind —
  a quota increase can take days.
- **Size subnets generously.** IP exhaustion is painful to fix later, because it means
  renumbering.
- And again: **headroom instead of scaling for the failure case.** Capacity you already have does
  not require the cloud to agree.

### N-12 · Cloud API rate limits and quota exhaustion

**What you see.** Operations failing with throttling errors during an incident, while the
underlying resources are fine.

**Mechanism.** Every cloud control-plane API is rate-limited. During an incident you are making
far more API calls than usual — describing instances, updating DNS, changing routes, scaling,
attaching volumes — and so is every automated system you run. You hit the limit and your
remediation tooling stops working.

The multiplier: many tools poll. A dozen controllers each describing every instance every 10
seconds is a steady background rate that is fine until the incident adds to it.

**Prevent.** Cache control-plane responses aggressively in your tooling; use event-driven updates
rather than polling where available; back off on throttling errors (with jitter — throttled
callers retrying in lockstep is `F-02`); monitor your API request rate against the published
limits; and know which of your emergency procedures depend on the control plane, because those
are the ones that will fail when you need them.

### N-13 · Scaling the tier that was not the bottleneck

**What you see.** You scale the service and nothing improves. Sometimes it gets worse.

**Mechanism.** The stateless tier scales; the stateful tier does not. Doubling `checkout-api`
from 40 to 80 pods doubles the connections to `orders-db` (`R-09`), doubles the load on
`session-cache`, and doubles the concurrent queries — against a database whose capacity did not
change. The bottleneck moved from your service to the thing behind it, where there is less
headroom and worse admission control.

Doc 06's arithmetic: 80 pods × 20 connections = 1,600 against a `max_connections` of 600. You
have converted a latency problem into a connection-refused problem.

**Prevent.** Know where the bottleneck is before scaling. A simple discriminator: if per-instance
CPU is low and per-instance latency is high, the bottleneck is downstream and adding instances
will make it worse. Put a connection proxy in front of the database so the app tier can scale
independently of the connection count. And set a **maximum** replica count derived from what the
downstream can take — an HPA with `maxReplicas` set from the database's capacity is a crude but
effective circuit breaker on this failure.

### N-14 · Planning against the wrong peak

**What you see.** Capacity is fine on the daily peak and fails on the event you did not model.

**Mechanism.** "Peak" is not one number. The peaks that matter are different multiples of
average, and a plan built on the wrong one fails on the others:

| Peak type | Riverbend | Gateline | Typical multiple of average |
|---|---|---|---|
| Daily | Evening browse | Evening browse | 2–3× |
| Weekly | Weekend | Weekend | 1.5× the daily peak |
| Seasonal | Late-November shopping | Summer tour announcements | 5–10× |
| **Event** | Flash sale | **Sale open** | Riverbend 5×; **Gateline 100×** |
| Failure-induced | One AZ's traffic on two | Same | 1.5× on survivors |
| **Recovery-induced** | Backlog drain (`Q-04`) | Waiting-room release | **3× sustained** |

Gateline's event peak is 100× its baseline (5,000 → 500,000 QPS) and it is *scheduled*. Riverbend's
recovery peak is 3× and it is *self-inflicted*. Neither is reachable by reactive autoscaling, and
neither appears on a graph of last month's traffic.

**Prevent.** Enumerate the peak types for your system explicitly, state the multiple for each,
and decide the mechanism for each one separately: headroom for failure-induced, schedule-based
scaling for known events, rate limiting for recovery-induced, reactive autoscaling for daily
variation. One mechanism does not cover all of them.

### N-15 · The cost optimisation that removed the headroom

**What you see.** Incidents begin a few weeks after a successful cost-reduction programme.

**Mechanism.** Headroom looks exactly like waste on a utilisation dashboard. A service at 40%
average CPU is an obvious target, and right-sizing it to 70% average is an obvious win — until
you notice that 40% was the `N − k` number from the start of this doc and 70% is the number at
which losing a zone is an outage.

The same applies to: reducing replica counts (fewer instances means each failure is a larger
fraction), moving to smaller instance types (less burst headroom), scaling down aggressively
(`N-04`), spot instances without on-demand fallback (`N-11`), and removing "unused" read replicas
that existed for failover.

**Prevent.** Make headroom a *named, documented requirement* with a stated reason, not an
unexplained gap between usage and provisioning. "This service runs at 45% because it must survive
an AZ loss at under 70%" is a line in a config file that survives a cost review. An unexplained
45% does not.

And then do cost optimisation properly, on the things that are genuinely waste: over-provisioned
memory requests (which block scheduling without being used), idle non-production environments,
unattached volumes, cross-AZ traffic (`D-12`), data retention, and instance-type modernisation.
Those are large and do not cost you reliability.

## What to take away

1. **Latency scales as `1/(1 − ρ)`.** At 90% utilisation you are already 10× your service time,
   and the next 5% doubles it again. The knee is at 70–80%, which is why utilisation targets are
   set there — that is engineering, not conservatism.
2. **The headroom question is not "what is our utilisation" but "what is our utilisation after
   losing `k` instances."** Surviving one of three zones while staying on the flat part of the
   curve requires running under 47%. Most plans do not account for this and most teams discover
   it during their first zone failure.
3. **Autoscaling takes 3–5 minutes and is for demand growth, not for surviving failures.**
   Anything faster than that needs headroom. Conflating the two is the most expensive mistake in
   this doc.
4. **CPU is the wrong autoscaling signal for most services.** Use in-flight requests for APIs,
   consumer lag for consumers, connection count for long-lived-connection servers — and keep CPU
   as a secondary backstop.
5. **Scale up fast and scale down slowly.** The costs are not symmetric: scaling up unnecessarily
   costs a few instance-minutes, scaling down prematurely costs an incident.
6. **CFS throttling stops your container completely for up to 87.5 ms per period** while
   reporting near-idle CPU. It is the strangest-looking latency failure there is. Measure
   `throttled_periods / periods`, make the runtime container-aware (`automaxprocs`,
   `UseContainerSupport`), and consider omitting CPU limits on latency-sensitive services.
7. **Memory is incompressible**: exceeding the limit is a kill, not a slowdown. Tell the runtime
   its limit (`GOMEMLIMIT`, `MaxRAMPercentage`), set `requests == limits` for memory, account for
   off-heap, and alert at 80% of the limit.
8. **A pod with no resource specification is `BestEffort` and is evicted first.** Omitting
   resources because you do not know the values is choosing to be killed first.
9. **The limits that bite are the ones not on your dashboard**: file descriptors, PIDs, conntrack,
   ephemeral ports, inodes, ARP table. conntrack and inodes are node-scoped, so one pod exhausts
   them for every pod on the node.
10. **Separate node pools by workload class.** Cache, memory-bandwidth, and disk contention are
    invisible to the scheduler, and the blunt fix is the one that works.
11. **Never let anything share a disk with etcd.**
12. **Autoscaling assumes the cloud has capacity, and during a regional failover it may not** —
    that is exactly when everyone else is asking too. Diversify instance types, reserve baseline
    capacity, monitor quota headroom, and size subnets generously.
13. **Scaling the stateless tier pushes load onto the stateful one.** Low CPU with high latency
    means the bottleneck is downstream and adding instances makes it worse. Cap `maxReplicas` from
    what the database can take.
14. **"Peak" is at least six different numbers** — daily, weekly, seasonal, event, failure-induced,
    recovery-induced — with different multiples and different correct mechanisms. Gateline's event
    peak is 100× and scheduled; Riverbend's recovery peak is 3× and self-inflicted.
15. **Headroom looks identical to waste on a dashboard.** Document it as a named requirement with
    its reason, or a cost-reduction programme will remove it and the incidents will start a few
    weeks later.

Next: [13-isolation-cells-regions-and-blast-radius.md](13-isolation-cells-regions-and-blast-radius.md),
which is about the only structural defence against the failures you did not anticipate: making
sure that when something breaks, it breaks for a bounded fraction of your users.
