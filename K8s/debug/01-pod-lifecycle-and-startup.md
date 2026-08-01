# Pod Lifecycle & Startup Issues

The highest-frequency category. This doc covers a pod that won't schedule, won't start, or
won't stay up.

## Fast triage

| `STATUS` you see | Meaning | Jump to |
|---|---|---|
| `Pending` | Not scheduled, or scheduled but containers not created | [Pending](#pending) |
| `ContainerCreating` (stuck) | Scheduled; kubelet can't create the container | [ContainerCreating](#stuck-in-containercreating) |
| `ImagePullBackOff` / `ErrImagePull` | Can't pull the image | [Image pull](#imagepullbackoff--errimagepull) |
| `CrashLoopBackOff` | Container starts then exits, repeatedly | [CrashLoop](#crashloopbackoff) |
| `Error` / `Completed` (unexpected) | Container exited non-zero / zero | [Exit codes](#reading-exit-codes) |
| `OOMKilled` (in lastState) | Kernel killed it for exceeding memory | [OOMKilled](#oomkilled) |
| `Running` but `0/1 READY` | Readiness probe failing | [Probes](#liveness-readiness-startup-probes) |
| `Terminating` (stuck) | Can't finish shutdown / finalizers | [Stuck terminating](#stuck-in-terminating) |
| `Init:x/y` (stuck) | An init container hasn't completed | [Init containers](#init-containers-and-sidecars) |

First command every time:
```bash
kubectl -n $NS describe pod $POD          # events + container states
kubectl -n $NS get pod $POD -o wide       # node, IP, restarts
```

---

## Pending

The scheduler can't place the pod, **or** it's placed but stuck creating. Distinguish:

```bash
kubectl -n $NS get pod $POD -o wide       # NODE column empty => not scheduled
kubectl -n $NS describe pod $POD          # look at the FailedScheduling event text
```

If unscheduled, the `FailedScheduling` event tells you *exactly* why. Common causes, ranked:

1. **Insufficient resources.** `0/12 nodes are available: 12 Insufficient cpu`. The pod's
   **requests** (not limits) exceed allocatable on every node. Check:
   ```bash
   kubectl describe node <node> | grep -A6 "Allocated resources"
   kubectl -n $NS get pod $POD -o jsonpath='{.spec.containers[*].resources.requests}'
   ```
   Fix: lower requests, add nodes (does the Cluster Autoscaler see the pending pod? see doc 06),
   or free capacity. ⚠️ A pod requesting more than any single node's allocatable will *never*
   schedule and CA won't help — it can't create a node big enough.
2. **Taints without tolerations.** `node(s) had untolerated taint {key: value}`. Node pools
   for GPUs/spot/system are commonly tainted. Add the matching toleration or target a
   different pool.
3. **Node affinity / selector / topology spread** excludes all nodes. `didn't match Pod's
   node affinity/selector` or `didn't match pod topology spread constraints`. Check
   `.spec.nodeSelector`, `.spec.affinity`, `.spec.topologySpreadConstraints`.
4. **Pod anti-affinity** can't be satisfied (e.g. "one replica per node" with more replicas
   than nodes). `didn't satisfy existing pods anti-affinity rules`.
5. **No available PV / volume zone conflict.** `had volume node affinity conflict` — the PV
   is in a zone with no schedulable node, or a `WaitForFirstConsumer` PVC + affinity deadlock.
   See doc 03.
6. **ResourceQuota exhausted** in the namespace — but this usually blocks *admission* with a
   clear error rather than leaving a Pending pod. `kubectl -n $NS describe resourcequota`.

> Staff tip: read the scheduler's message literally and count the nodes it rejected and why —
> it aggregates per-reason (`3 Insufficient memory, 9 had untolerated taint`). That histogram
> is the whole answer.

---

## Stuck in ContainerCreating

Scheduled (NODE is set) but the container never comes up. Events on the pod are the signal;
this is almost always a **node-local** dependency:

- **Volume mount failing** — CSI attach/mount error, missing Secret/ConfigMap referenced as a
  volume, PVC not bound. `MountVolume.SetUp failed` / `FailedMount`. See doc 03.
- **Secret/ConfigMap not found** — a volume or `envFrom` references a missing object.
  `couldn't find key ...` / `secret "x" not found`.
- **CNI can't allocate an IP** — `failed to setup network for sandbox` / IPAM exhausted on
  the node. See doc 02. Common on nodes at their per-node IP/ENI limit.
- **Image still pulling** (large image) — not stuck, just slow; check `describe` for a Pulling
  event without a failure.
- **Runtime problem** — containerd wedged. `kubectl describe node` → check kubelet/runtime;
  see doc 04.

```bash
kubectl -n $NS describe pod $POD | sed -n '/Events/,$p'
# On the node, if reachable:
sudo crictl ps -a | grep $POD
sudo journalctl -u kubelet -n 100 --no-pager
```

---

## ImagePullBackOff / ErrImagePull

The kubelet can't fetch the image. The event text disambiguates:

| Event text | Cause | Fix |
|---|---|---|
| `manifest ... not found` / `not found: manifest unknown` | Wrong tag/name, or tag deleted | Fix image ref; verify tag exists in registry |
| `unauthorized` / `pull access denied` | Missing/wrong `imagePullSecrets` or registry auth | Add/fix pull secret; check SA `imagePullSecrets` |
| `dial tcp ... i/o timeout` / `no such host` | Node can't reach registry (network/DNS/firewall) | Node egress, proxy, private-registry DNS |
| `toomanyrequests` | Docker Hub rate limit | Authenticate, mirror, or use a pull-through cache |
| `no match for platform` | arch mismatch (arm64 vs amd64) | Multi-arch image or correct nodeSelector |

```bash
kubectl -n $NS get pod $POD -o jsonpath='{.spec.containers[*].image}'; echo
kubectl -n $NS get sa <sa> -o jsonpath='{.imagePullSecrets}'; echo
# Test the pull auth directly from a node:
sudo crictl pull <image>
```

⚠️ `imagePullSecrets` must be in the **same namespace** as the pod and referenced either on
the pod or its ServiceAccount. A cluster-wide secret doesn't exist — copy it per namespace.
⚠️ `imagePullPolicy: IfNotPresent` with a mutable tag like `:latest` can serve a *stale*
cached image on some nodes and a fresh one on others — pin digests for reproducibility.

---

## CrashLoopBackOff

The container starts, exits, and kubelet restarts it with exponential backoff (10s → 20s →
… capped at 5m). CrashLoop is a *symptom of the app exiting*, not a cause. **You must read
the previous instance's logs.**

```bash
kubectl -n $NS logs $POD -c $CONTAINER --previous --timestamps
kubectl -n $NS get pod $POD -o jsonpath='{.status.containerStatuses[0].lastState.terminated}'; echo
```

Decision tree by what you find:

- **Logs show an app error / stack trace / panic** → application bug or bad config. Most
  common: missing/invalid env var, unreachable dependency at startup (DB, config server),
  failed migration, bad flag. Fix the config or code.
- **Logs empty + exit code 1/2, dies instantly** → entrypoint/command wrong, binary missing,
  or crash before logging init. Check `.spec.containers[].command/args`; try running the image
  locally; inspect with an ephemeral debug container (doc 10).
- **`lastState.terminated.reason == OOMKilled`** → see [OOMKilled](#oomkilled).
- **Exit code 137 without OOMKilled** → SIGKILL from outside (often a failing *liveness*
  probe killing a healthy-but-slow app — see probes). 143 = SIGTERM (graceful).
- **Exit code 0 but restarting** → the process finished and the container is `Always`
  restart. For run-to-completion work use a Job, not a Deployment.
- **Runs fine for a while, then loops** → not a startup bug; it's a runtime crash or a
  liveness probe. Look at *how long* it survives and the memory curve.

### Reading exit codes

- `0` clean exit · `1`/`2` app error · `126` not executable · `127` command not found ·
  `128+n` killed by signal n → `137` = 128+9 (SIGKILL/OOM) · `143` = 128+15 (SIGTERM).

---

## OOMKilled

The cgroup memory limit was hit and the kernel OOM-killer terminated the process (exit 137,
`reason: OOMKilled`). This is a **hard limit**, enforced by the kernel, not the scheduler.

```bash
kubectl -n $NS get pod $POD -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}{"\n"}'
kubectl -n $NS describe pod $POD | grep -i -A2 "Last State"
# Real memory picture (Prometheus):
#   container_memory_working_set_bytes vs kube_pod_container_resource_limits{resource="memory"}
```

Root-cause it — don't just bump the limit reflexively:

1. **Limit genuinely too low** for the workload's real footprint → raise the limit (and
   usually the request, to match QoS). Validate with the working-set curve, not a guess.
2. **Memory leak** → working set climbs monotonically until the limit, restarts, repeats
   (sawtooth). Bumping the limit only lengthens the sawtooth. Fix the leak; heap-profile.
3. **Load-correlated spike** → GC pressure, large request bodies, unbounded caches/batch
   sizes. Cap the concurrency/cache; for the JVM set `-XX:MaxRAMPercentage`; for Node set
   `--max-old-space-size` *below* the container limit.
4. ⚠️ **Runtime unaware of cgroup limit.** Older JVMs, Node, and some libraries read *host*
   memory and size heaps/pools for the whole node → instant OOM under a small limit. Ensure
   container-aware runtime flags. Go respects `GOMEMLIMIT` (set it ~90% of the cgroup limit
   to make GC back off before the kernel kills you).
5. **It was killed but you didn't set a limit** → the *node* is under memory pressure and
   the kubelet is evicting/OOMing by QoS class. See doc 04 (node eviction) and doc 06 (QoS).

> Distinguish *container* OOM (your limit, exit 137, pod restarts in place) from *node* OOM /
> eviction (node pressure, pod gets `Evicted` status and is rescheduled). Different fix.

---

## Liveness, readiness, startup probes

Probe misconfiguration causes two classic failures: **restart loops** (liveness) and
**never-ready / removed from Service** (readiness).

```bash
kubectl -n $NS describe pod $POD | grep -iE "Liveness|Readiness|Startup|Unhealthy"
kubectl -n $NS get pod $POD -o jsonpath='{.spec.containers[0].livenessProbe}'; echo
```

- **Liveness probe failing → kill + restart.** If a *healthy but slow-to-start* app fails
  liveness during boot, kubelet kills it forever (looks like CrashLoop with exit 137 and
  `Unhealthy` events). Fix: use a **startupProbe** to gate liveness during slow starts, or
  raise `initialDelaySeconds`/`failureThreshold`. ⚠️ Liveness should test "is the process
  wedged", not "are dependencies healthy" — a liveness probe that checks the DB will restart
  every pod when the DB blips, turning a dependency outage into a self-inflicted crashloop.
- **Readiness probe failing → 0/1 READY, pulled from Endpoints.** Traffic stops but no
  restart. Causes: app genuinely not ready (warming cache), probe path/port wrong, probe
  timeout too tight under load, or a dependency the readiness check (correctly) gates on.
- **Startup probe** — for slow starters. While it runs, liveness/readiness are suppressed.
  Budget = `failureThreshold × periodSeconds`; make it generous enough for worst-case boot.
- **Probe timeouts under load** — default `timeoutSeconds: 1`. A busy app can't answer in 1s,
  probes flap, pod bounces in and out of the Endpoints causing intermittent 5xx. Raise
  `timeoutSeconds` and consider a dedicated, cheap health handler off the hot path.

---

## Init containers and sidecars

- **`Init:0/2` stuck** — an init container is running/failing; init containers run
  sequentially and block the app. `kubectl logs $POD -c <init-name>`. Common: waiting on a
  dependency (`wait-for-db`) that isn't coming, or failing and crashlooping the init step.
- **Native sidecars (init containers with `restartPolicy: Always`, GA 1.29+)** start before
  app containers and are terminated *after* them — the right way to run a proxy/log shipper
  so it outlives app shutdown. If a mesh sidecar starts *after* the app, early egress fails;
  native sidecars fix the ordering.
- ⚠️ **Non-native sidecar + Job** = Job never completes: the app container exits but the
  sidecar (e.g. Istio proxy, cloud SQL proxy) keeps running so the pod never reaches
  `Completed`. Use native sidecars, or have the app signal the sidecar to quit.

---

## Stuck in Terminating

A pod stuck `Terminating` past its grace period:

```bash
kubectl -n $NS get pod $POD -o jsonpath='{.metadata.deletionTimestamp} {.metadata.finalizers}'; echo
```

- **Finalizers** present → some controller must run before deletion completes and it isn't
  (controller down, external resource stuck). Fix the controller; only remove the finalizer
  manually if you understand what cleanup you're skipping.
- **Node unreachable / NotReady** → the kubelet can't confirm the pod died. Pods get
  `deletionTimestamp` but linger. Kubernetes won't force-reschedule stateful pods off a lost
  node automatically (data-safety). See doc 04.
- **`terminationGracePeriodSeconds`** too long, or app ignores SIGTERM and only dies on the
  SIGKILL at the end of grace. If your app doesn't handle SIGTERM, shutdown always takes the
  full grace period. Handle SIGTERM; drain connections; then exit.
- ⚠️ `kubectl delete pod --grace-period=0 --force` removes the API object but does **not**
  guarantee the container is gone on the node. For StatefulSets this risks two instances with
  the same identity/volume — see doc 03/07. Use it as a last resort, knowingly.

---

## Prevention checklist

- Set memory **requests == limits** for critical services (Guaranteed QoS) so they're last to
  be evicted; size from real working-set data.
- Always define **startupProbe** for slow starters; keep **liveness** dependency-free.
- Pin images by digest or immutable tags; never `:latest` in prod.
- Give run-to-completion work a **Job**, not a Deployment.
- Handle **SIGTERM** and set a realistic `terminationGracePeriodSeconds`.
- Set `GOMEMLIMIT` / `MaxRAMPercentage` / `--max-old-space-size` under the container limit.
