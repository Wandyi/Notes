# Debugging Methodology

The goal of this doc is to make triage *reproducible* instead of intuitive. When you get
paged at 3am you don't want to rely on remembering which of 40 things to check — you want a
loop that converges.

## The layered mental model

A running workload depends on a stack. When something breaks, it broke at one layer, and the
symptom usually shows up one or two layers *above* the fault. Debug top-down for symptoms,
but confirm bottom-up for root cause.

```
  Application logic (your code, config, dependencies)
  ── Container (image, entrypoint, env, filesystem, resource limits)
  ── Pod (scheduling, probes, init/sidecars, volumes, service account)
  ── Workload controller (Deployment/StatefulSet/Job — desired vs actual)
  ── Node (kubelet, runtime, cgroups, disk, kernel)
  ── Cluster networking (CNI, kube-proxy, DNS, NetworkPolicy)
  ── Control plane (apiserver, etcd, scheduler, controller-manager, webhooks)
  ── Infra (cloud API, LB, IAM, quotas, hardware)
```

A useful reflex: **"Is this one pod, one node, one namespace, or the whole cluster?"** The
blast radius tells you which layer to suspect first.

| Blast radius | Suspect first |
|---|---|
| One pod | App/container: crash, probe, config, image, OOM |
| All pods of one workload | Controller/manifest, image tag, bad config/secret, quota |
| All pods on one node | Node: kubelet, runtime, disk pressure, CNI on that node |
| One namespace | RBAC, ResourceQuota, NetworkPolicy, admission webhook scoped to ns |
| Cluster-wide | Control plane, DNS (CoreDNS), CNI, admission webhook, cert expiry |

## The triage loop

Repeat until root cause is isolated:

1. **State the symptom precisely.** "Pods are broken" is not a symptom. "3 of 6 `api` pods
   are `CrashLoopBackOff`, started 12 min ago, others fine" is. Precision picks the doc.
2. **Establish the timeline.** What changed and when? Deploys, config/secret edits, node
   pool scaling, cert rotation, cloud maintenance. `kubectl rollout history`, git, CI logs,
   cloud audit logs. **~80% of incidents correlate to a recent change.** Find the change.
3. **Localize the blast radius** (table above).
4. **Pull the four primary signals** (next section) for the suspected layer.
5. **Form one hypothesis and pick the cheapest discriminating test.** Don't shotgun fixes.
   A good test is one whose result rules a cause *in or out* — not one that just "might help".
6. **Fix, or descend a layer** and repeat.

## The four primary signals

For almost any object, these four answer 90% of questions. Learn to read them fast.

1. **`kubectl describe`** — the object's spec + status + recent Events. Events are the
   single highest-yield signal in Kubernetes and are time-limited (default TTL 1h), so grab
   them early. Scheduling failures, probe failures, image pulls, volume mounts, evictions,
   OOM all surface here.
   ```bash
   kubectl -n $NS describe pod $POD
   kubectl -n $NS get events --sort-by=.lastTimestamp | tail -40
   ```
2. **Logs** — current and previous container. `--previous` is how you see *why the last
   instance died* in a crashloop; without it you only see the new (possibly still-healthy)
   instance.
   ```bash
   kubectl -n $NS logs $POD -c $CONTAINER --previous --timestamps
   kubectl -n $NS logs deploy/$DEPLOY --all-containers --since=15m
   ```
3. **Status/conditions** — `kubectl get -o wide` for placement and restart counts;
   `-o yaml` `.status.conditions` and `.status.containerStatuses[].state/lastState` for the
   machine-readable truth (exit codes, reasons, timestamps) that `describe` summarizes.
   ```bash
   kubectl -n $NS get pod $POD -o wide
   kubectl -n $NS get pod $POD -o jsonpath='{range .status.containerStatuses[*]}{.name}{": "}{.state}{" last="}{.lastState}{"\n"}{end}'
   ```
4. **Metrics** — `kubectl top` for a quick read; Prometheus for the real picture (throttling,
   working set vs limit, restart rate, saturation). Metrics distinguish "slow" from "broken".

> If you only memorize one thing: **describe + events + `--previous` logs**, in that order,
> resolves the majority of pod-level incidents before you touch anything else.

## Control plane vs data plane

Split every problem this way — it changes *where you look* and *what you can touch*:

- **Control plane** (apiserver, etcd, scheduler, controller-manager, cloud-controller): the
  brain. Decides desired state, stores it, reconciles. On managed clusters this is the
  provider's; you get logs/metrics through their console/API, not SSH.
- **Data plane** (kubelet, container runtime, kube-proxy, CNI, CSI, your pods): the muscle.
  Runs on nodes you (often) can reach.

Heuristic: **"Is desired state wrong, or is desired state right but not happening?"** Wrong
desired state → control plane / your manifests / a controller. Right desired state not
realized → data plane (node, runtime, network, storage).

## When to escalate / stop

- You've isolated the layer and it's the managed provider's control plane → open a support
  case with the exact timestamps, request IDs, and object names. Don't keep poking.
- Cluster-wide impact + you don't have a hypothesis in ~10 min → declare an incident, pull in
  a second set of eyes, and start a timeline doc. Staff move: parallelize investigation, don't
  serialize it in your own head.
- The "fix" requires deleting data (PVs, etcd, force-deleting stateful pods) → stop, snapshot
  first, and get a second opinion. Most catastrophic outages are a hasty fix, not the original
  fault.

## Cheap habits that pay off mid-incident

- Capture state before you mutate: `kubectl get pod $POD -o yaml > /tmp/pod-before.yaml`.
  You'll want the "before" when you're writing the postmortem.
- Prefer read-only discrimination over mutating fixes until you have a hypothesis. `describe`,
  `logs`, `get -o yaml` never make it worse.
- Watch, don't poll: `kubectl get pods -w` (or `-o wide --watch`) to see transitions live.
- Diff against a known-good peer: another replica, the same workload in staging, the last
  good revision. Difference isolates cause faster than absolute inspection.
