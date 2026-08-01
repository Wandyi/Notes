# Observability & Tooling

The toolbox the other docs assume. Organized by "what question am I answering" — because the
right tool follows from the question, not the other way around.

## The signal hierarchy (what to reach for, in order)

1. **Events** — `kubectl describe` / `kubectl get events`. Highest yield, time-limited (1h TTL).
2. **Logs** — current + `--previous`, structured if you're lucky.
3. **Object status** — `get -o yaml`, conditions, container states.
4. **Metrics** — `kubectl top`, then Prometheus for the real picture.
5. **Traces** — for latency/flow across services.
6. **Node/runtime internals** — `crictl`, `journalctl`, `dmesg` via node debug.
7. **Packet/syscall level** — ephemeral containers with tcpdump/strace/perf, or eBPF.

Climb only as far as the question requires. Most incidents die at level 1–2.

---

## kubectl patterns worth memorizing

```bash
# Events, newest last, across the namespace (the single best triage command):
kubectl -n $NS get events --sort-by=.lastTimestamp | tail -40
kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp | tail

# Watch state transitions live instead of polling:
kubectl -n $NS get pods -o wide --watch

# Previous container logs (why the last instance died) + timestamps:
kubectl -n $NS logs $POD -c $C --previous --timestamps

# Logs across all pods of a workload, recent window:
kubectl -n $NS logs deploy/$DEPLOY --all-containers --since=15m --prefix

# Machine-readable container state (exit codes, reasons, timestamps):
kubectl -n $NS get pod $POD -o jsonpath='{range .status.containerStatuses[*]}{.name}{" state="}{.state}{" last="}{.lastState}{" restarts="}{.restartCount}{"\n"}{end}'

# Sort pods by restarts / find the noisy ones:
kubectl get pods -A --sort-by=.status.containerStatuses[0].restartCount | tail

# What's scheduled on a node:
kubectl get pods -A -o wide --field-selector spec.nodeName=<node>

# Effective permissions of a subject:
kubectl auth can-i --list --as=system:serviceaccount:$NS:$SA -n $NS

# Explain a field / discover schema without leaving the terminal:
kubectl explain pod.spec.containers.resources --recursive
```

- `-o wide` for placement/IP/node/restarts; `-o yaml` for ground truth; `-o jsonpath`/`-o
  custom-columns` to extract exactly one thing.
- `--show-labels` and `-l key=value` to slice by label — essential for selector debugging.
- `-A`/`--all-namespaces` when the blast radius is unclear.
- Save "before" state before mutating: `kubectl get <obj> -o yaml > /tmp/before.yaml`.

---

## Ephemeral debug containers

The modern way to debug a running pod **without rebuilding the image or restarting the pod** —
critical for distroless/minimal images that have no shell.

```bash
# Attach a debug container sharing the pod's namespaces:
kubectl -n $NS debug -it $POD --image=nicolaka/netshoot --target=$CONTAINER
# Now you can see the target's network + (with --target) process namespace:
#   curl localhost:8080, ss -tnp, tcpdump, strace -p <pid>, cat /proc/<pid>/...
```

- `--target=$CONTAINER` shares that container's process namespace so you can `strace`/inspect
  its PID and localhost — without it you only share the network.
- Distroless app with no `/bin/sh`? The ephemeral container brings its own userland; you debug
  the app's namespaces from a full toolbox image.
- **Copy-and-debug a crashlooping pod** (change the command to keep it alive):
  ```bash
  kubectl -n $NS debug $POD --copy-to=$POD-debug --container=$C -- sleep infinity
  kubectl -n $NS exec -it $POD-debug -- sh
  ```

---

## Node-level debugging (managed clusters, no SSH)

```bash
# Privileged pod pinned to a node, with host namespaces mounted:
kubectl debug node/<node> -it --image=nicolaka/netshoot
# then:
chroot /host                      # host filesystem
journalctl -u kubelet -n 200      # kubelet logs
crictl ps -a; crictl logs <id>    # runtime view, bypasses kubelet
df -h; dmesg -T | tail; top       # disk, kernel (OOM!), live load
```

`crictl` (CRI client) is the node-level equivalent of `kubectl` for containers — works even
when the kubelet can't talk to the apiserver:
```bash
crictl ps -a           # containers
crictl images          # image bloat (DiskPressure)
crictl logs <id>       # container logs directly from the runtime
crictl inspect <id>    # full container config/state
crictl stats           # per-container CPU/mem
```

---

## Metrics: what to actually look at

`kubectl top` is a quick read (needs metrics-server); Prometheus is where you diagnose.
High-signal metrics referenced throughout these docs:

| Question | Metric |
|---|---|
| CPU throttling? | `rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m])` |
| Memory vs limit? | `container_memory_working_set_bytes` vs `kube_pod_container_resource_limits{resource="memory"}` |
| Restart storms? | `rate(kube_pod_container_status_restarts_total[15m])` |
| Pending pods? | `kube_pod_status_phase{phase="Pending"}` |
| apiserver saturation? | `apiserver_current_inflight_requests`, `apiserver_request_duration_seconds` |
| APF throttling? | `apiserver_flowcontrol_rejected_requests_total` |
| etcd disk? | `etcd_disk_wal_fsync_duration_seconds`, `etcd_mvcc_db_total_size_in_bytes` |
| Node pressure? | `kube_node_status_condition{condition=~"MemoryPressure|DiskPressure"}` |
| Service endpoints? | `kube_endpoint_address_available` |

`kube-state-metrics` (object state: desired vs ready, conditions) + `node-exporter` (node OS
metrics) + cAdvisor (container resource) are the standard trio. Learn to join `kube-state`
"desired" with cAdvisor "actual".

⚠️ `container_memory_usage_bytes` includes reclaimable page cache and overstates real usage —
use **`working_set_bytes`** for OOM/limit reasoning.

---

## Logs at scale

- Structured logging (JSON) + a log backend (Loki/ELK/Cloud logging) — `kubectl logs` doesn't
  survive pod deletion; you need shipped logs to debug *after* a crashloop cleans up.
- Correlate by **trace ID / request ID** across services; a single request's logs scattered
  across pods are useless without a join key.
- `stern` / `kubectl logs -f -l app=...` for tailing many pods at once during an incident.

---

## Tracing & flow

- OpenTelemetry → Jaeger/Tempo/Zipkin. The go-to for "where did the time go" and
  "which service in the chain failed" (doc 09).
- Service mesh telemetry (Istio/Linkerd/Cilium Hubble) gives L7 golden signals and flow
  visibility without app changes — Hubble in particular is invaluable for NetworkPolicy and
  connectivity debugging (doc 02).

---

## eBPF-based tooling (the modern edge)

No app changes, low overhead, kernel-level truth:

- **Cilium/Hubble** — flow visibility, policy drops, L7 metrics.
- **Pixie** — auto-instrumented protocol traces (HTTP/gRPC/DNS/SQL) cluster-wide.
- **Parca / Pyroscope** — continuous profiling; historical flame graphs so you can profile a
  spike *after* it happened.
- **bpftrace / bcc** — ad-hoc kernel probes (which syscalls are slow, who's touching this file,
  packet drops) via `kubectl debug node`.

These are how staff engineers answer "prove it" questions that logs and metrics can't.

---

## A minimal incident toolkit to pre-stage

- A `netshoot` manifest/alias for instant in-cluster network+process debugging.
- `kubectl debug` muscle memory (pod ephemeral container + node debug).
- Dashboards for the metrics table above, pre-built.
- Shipped logs + tracing wired *before* the incident.
- A documented "unwedge" runbook for admission webhooks and etcd quota (docs 05, 08).
- `crictl` familiarity for when `kubectl` can't reach a node.

The best debugging tool is the one already installed and the dashboard already built. Set them
up in calm, not at 3am.
