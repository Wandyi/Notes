# Nodes & Kubelet

When the blast radius is "everything on one node" (or a set of nodes), you're below the pod
layer. This doc is about the node agent (kubelet), the container runtime, and the node's
physical resources.

## Fast triage

| Symptom | Jump to |
|---|---|
| Node `NotReady` | [NotReady](#node-notready) |
| Pods on a node all failing / stuck | [Kubelet/runtime](#kubelet--runtime-down) |
| Pods `Evicted` | [Eviction](#eviction--resource-pressure) |
| Node `SchedulingDisabled` | cordoned — `kubectl uncordon` if unintended |
| New pods won't schedule to a healthy-looking node | [Pressure conditions](#node-conditions--pressure) |
| High node CPU/mem but pods look fine | [Node vs pod accounting](#node-level-vs-pod-level-resource-accounting) |

```bash
kubectl get nodes -o wide
kubectl describe node <node>              # Conditions, Allocatable, Allocated, Events
kubectl get node <node> -o jsonpath='{.status.conditions}' | jq
```

---

## Node NotReady

`NotReady` means the kubelet stopped posting healthy status to the apiserver (node lease /
heartbeat). After `node-monitor-grace-period` (~40s) the node is marked NotReady; after the
pod eviction timeout, pods are marked for eviction/rescheduling.

Discriminate — is it the **node**, the **kubelet**, or the **network between kubelet and
apiserver**?

```bash
kubectl describe node <node> | sed -n '/Conditions/,/Addresses/p'
# On the node (SSH / SSM / debug node):
systemctl status kubelet
journalctl -u kubelet -n 200 --no-pager
```

Ranked causes:

1. **Kubelet crashed / can't reach apiserver** — cert expired, network partition, apiserver
   overloaded, or kubelet OOM. Check kubelet logs and whether *other* nodes are also flapping
   (→ suspect control plane / network, not this node).
2. **Resource exhaustion on the node** — CPU starvation so severe the kubelet can't heartbeat,
   or memory pressure. Often self-inflicted by a runaway pod without limits.
3. **Container runtime down** — containerd/CRI-O crashed; kubelet reports `NotReady` with
   `container runtime is down` / `PLEG is not healthy`. See below.
4. **Disk full** — kubelet can't write, `no space left on device`. See DiskPressure.
5. **Node hardware / cloud** — VM stopped, hardware fault, spot reclamation, network ACL
   change. Check cloud console / instance status.
6. **Clock skew / cert issues** — kubelet client cert expired (common on long-lived
   self-managed nodes) → apiserver rejects it → NotReady. `journalctl` shows TLS errors.

⚠️ Many nodes NotReady at once is almost never "all the nodes broke" — it's the control plane,
DNS, a CNI rollout, a bad kubelet config push, or cert expiry. Widen the lens.

---

## Kubelet / runtime down

`PLEG is not healthy` (Pod Lifecycle Event Generator) means the kubelet can't list/inspect
containers from the runtime within its timeout — the runtime is wedged or overloaded.

```bash
# on the node:
systemctl status containerd kubelet
crictl info                               # runtime reachable?
crictl ps -a                              # containers the runtime sees
journalctl -u containerd -n 200 --no-pager
```

- **containerd hung** — too many containers, a stuck image pull, or a kernel/storage issue.
  Restarting containerd is disruptive (restarts pod sandboxes) but often the fix; drain first
  if you can.
- **Too many pods / images** — image GC not keeping up, thousands of dead containers. Tune
  eviction/GC thresholds; `crictl rmi --prune`.
- **crictl is your friend here** — it talks directly to the CRI socket, bypassing kubelet, so
  it works even when `kubectl` can't reach the node. `crictl logs`, `crictl inspect`,
  `crictl stats`.

---

## Node conditions & pressure

`kubectl describe node` Conditions section is the node's self-report. Anything `True` other
than `Ready` is a problem the scheduler reacts to:

| Condition | Meaning | Effect |
|---|---|---|
| `MemoryPressure` | Available memory below eviction threshold | Kubelet evicts pods; taints node NoSchedule for best-effort |
| `DiskPressure` | nodefs/imagefs below threshold | Evicts pods, blocks new pods, triggers image/container GC |
| `PIDPressure` | Too many PIDs | Blocks new pods |
| `NetworkUnavailable` | Node network (CNI) not configured | Node not usable |

DiskPressure specifics (very common):
```bash
# on the node:
df -h /var/lib/containerd /var/lib/kubelet /
du -sh /var/log/pods/* 2>/dev/null | sort -h | tail
crictl images                             # image bloat
```
Causes: unbounded pod logs, image accumulation, a pod writing to `emptyDir`/root fs, or a
too-small root disk. Fixes: log rotation, image GC thresholds (`--image-gc-high-threshold`),
bigger disk, `ephemeral-storage` limits on pods (doc 03).

---

## Eviction / resource pressure

The kubelet evicts pods to reclaim node resources when under pressure, choosing victims by
**QoS class** and how far over requests they are:

- **Order of sacrifice**: `BestEffort` (no requests/limits) first → `Burstable` (over its
  requests) → `Guaranteed` (requests==limits) last. This is *why* you set requests==limits on
  critical pods (doc 06).
- **Evicted pod** status shows `Evicted` with a reason (`The node was low on resource: memory`).
  The pod object lingers (status Failed) until GC'd — you'll see piles of Evicted pods after a
  pressure event; clean them up:
  ```bash
  kubectl -n $NS get pods --field-selector=status.phase=Failed
  kubectl -n $NS delete pods --field-selector=status.phase=Failed
  ```
- **Node-level OOM vs eviction**: soft/hard eviction is kubelet-driven and graceful-ish;
  kernel OOM is abrupt (a process gets SIGKILL). Under fast memory spikes the kernel OOM-killer
  can beat the kubelet's eviction — you'll see `oom-kill` in `dmesg` and a killed process
  rather than a clean eviction.

```bash
dmesg -T | grep -i -E "oom|killed process"
kubectl get events -A --field-selector reason=Evicted --sort-by=.lastTimestamp | tail
```

---

## Node-level vs pod-level resource accounting

A frequent confusion: `kubectl top node` says 90% CPU but every pod's `top pod` looks modest.

- **Allocatable ≠ capacity**: the node reserves memory/CPU for system daemons and the kubelet
  (`--system-reserved`, `--kube-reserved`, eviction threshold). Allocatable is what pods can
  use. `describe node` shows both.
- **Requests vs usage**: the scheduler packs by **requests**; actual usage can be lower (over-
  provisioned) or, for limitless pods, much higher (node oversubscribed and at risk). "Allocated
  resources" in `describe node` is the *requests* sum, not live usage.
- **System daemons** (kubelet, containerd, CNI, log agents, node-exporter) consume real CPU/mem
  that pod metrics don't show. A DaemonSet gone rogue starves everything on the node.

---

## Cordon / drain / uncordon (safe node operations)

```bash
kubectl cordon <node>                     # stop new pods landing (SchedulingDisabled)
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --grace-period=... 
kubectl uncordon <node>                   # return to service
```

- Drain respects **PodDisruptionBudgets** — it'll block if evicting would violate a PDB
  (doc 06/07). That's a feature; if drain hangs, a PDB is protecting availability (or a
  misconfigured PDB with `minAvailable` == replicas makes drain impossible ⚠️).
- `--delete-emptydir-data` is required (and destructive) for pods with emptyDir — know what
  you're discarding.
- DaemonSet pods can't be gracefully drained (they'll just reschedule) — `--ignore-daemonsets`.

---

## Node-level debugging without SSH (managed clusters)

When you can't SSH, use a privileged debug pod pinned to the node:
```bash
kubectl debug node/<node> -it --image=nicolaka/netshoot
# lands in a pod with host namespaces; chroot /host for host fs, then journalctl etc.
```
This is the staff move on EKS/GKE/AKS where nodes have no SSH — you still get `journalctl`,
`crictl`, `df`, `dmesg` via the host mounts.

---

## Prevention checklist

- Set `--system-reserved` / `--kube-reserved` so the kubelet never loses its own resources.
- Requests==limits (Guaranteed QoS) for critical workloads; always set *some* requests so
  BestEffort pods aren't scheduled onto critical nodes.
- Log rotation + image GC thresholds tuned; ship logs off-node.
- Monitor node Conditions and lease/heartbeat as first-class alerts (NotReady flapping is an
  early warning).
- Track cert expiry for self-managed kubelet client certs.
