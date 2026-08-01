# Kubernetes Debugging — Staff Engineer Playbook

A topic-organized collection of debugging runbooks for Kubernetes, written for engineers
who own clusters in production. The bias throughout: **form a hypothesis, reach for the
signal that confirms or kills it fastest, and understand the mechanism** — not just the
`kubectl` incantation that makes the symptom disappear.

## How to use this collection

1. Start with [00-debugging-methodology.md](00-debugging-methodology.md) if you don't already
   have a mental model for triaging. It defines the layers, the "narrow the blast radius"
   loop, and how to read the signals every other doc references.
2. Jump to the topic doc that matches the symptom. Each doc is self-contained: symptom →
   likely causes ranked by probability → commands to discriminate between them → the fix →
   how to prevent recurrence.
3. Every doc has a **"Fast triage"** table at the top for on-call use and a **"Deep dive"**
   section for root-causing the non-obvious 10%.

## Topics

| Doc | Covers |
|-----|--------|
| [00-debugging-methodology.md](00-debugging-methodology.md) | Layered mental model, signal sources, the triage loop, when to stop |
| [01-pod-lifecycle-and-startup.md](01-pod-lifecycle-and-startup.md) | Pending, CrashLoopBackOff, ImagePullBackOff, OOMKilled, Init/sidecar, probes, terminationGracePeriod |
| [02-networking-and-dns.md](02-networking-and-dns.md) | Service/Endpoints, kube-proxy, CNI, NetworkPolicy, CoreDNS, MTU, conntrack |
| [03-storage-and-volumes.md](03-storage-and-volumes.md) | PVC Pending, CSI attach/mount, multi-attach, StatefulSet volumes, expansion, ephemeral disk |
| [04-nodes-and-kubelet.md](04-nodes-and-kubelet.md) | NotReady, kubelet, container runtime, resource pressure/eviction, cgroups, disk |
| [05-control-plane-and-etcd.md](05-control-plane-and-etcd.md) | apiserver latency, etcd, controller-manager, scheduler, admission/webhook stalls |
| [06-resource-management-and-autoscaling.md](06-resource-management-and-autoscaling.md) | requests/limits, CPU throttling, QoS, HPA/VPA, Cluster Autoscaler, PDBs |
| [07-workload-controllers-and-rollouts.md](07-workload-controllers-and-rollouts.md) | Deployment/StatefulSet/DaemonSet/Job rollouts, stuck rollouts, revisions |
| [08-rbac-auth-and-admission.md](08-rbac-auth-and-admission.md) | RBAC forbidden, ServiceAccount tokens, admission webhooks, PSA, image policy |
| [09-performance-and-latency.md](09-performance-and-latency.md) | Application/cluster latency, noisy neighbors, hotspots, profiling in-cluster |
| [10-observability-and-tooling.md](10-observability-and-tooling.md) | The toolbox: kubectl patterns, ephemeral containers, crictl, events, metrics, eBPF |

## Conventions used across docs

- Commands assume a recent `kubectl` (>= 1.27) and a Linux node runtime (containerd).
- `$NS` = namespace, `$POD` = pod name — set them or substitute inline.
- "Control plane" signals (apiserver, etcd, scheduler) are separated from "data plane"
  (kubelet, CNI, workloads) because on managed clusters (EKS/GKE/AKS) you often can't reach
  the control plane hosts directly — the doc notes the managed-cluster path where it differs.
- ⚠️ marks a foot-gun that regularly burns experienced engineers.
