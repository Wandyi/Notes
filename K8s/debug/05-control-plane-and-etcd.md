# Control Plane & etcd

The control plane is the brain: apiserver (the only thing that talks to etcd), etcd (the
source of truth), scheduler, controller-manager, and cloud-controller-manager. When it's
degraded, the whole cluster feels slow or "frozen" even though pods keep running.

> On managed clusters (EKS/GKE/AKS) the control plane is the provider's. You debug it through
> their metrics/logs/console and, past a point, a support case. The *symptoms* below still
> apply — you just can't restart etcd yourself.

## Fast triage

| Symptom | Likely | Jump to |
|---|---|---|
| `kubectl` slow / times out cluster-wide | apiserver overload or etcd slow | [apiserver](#apiserver-slow-or-erroring) |
| `etcdserver: request timed out` / `leader changed` | etcd unhealthy | [etcd](#etcd-health) |
| Objects created but nothing happens | controller-manager or scheduler down | [Controllers](#controller-manager--scheduler) |
| `admission webhook ... timeout/refused` on every write | Failing webhook | [Webhooks](#admission-webhook-stalls) |
| Everything stops reconciling at once | Leader election / control-plane cert | [Leader/cert](#leader-election--certs) |
| Running pods fine but no new changes take effect | Data plane OK, control plane stuck | (this whole doc) |

Key discriminator: **do existing pods keep serving traffic while nothing new reconciles?**
That's classic control-plane-down — the data plane runs on last-known desired state.

---

## apiserver slow or erroring

Every `kubectl`, controller, and kubelet talks to apiserver. When it's slow, *everything* is.

```bash
kubectl get --raw='/readyz?verbose'
kubectl get --raw='/livez?verbose'
# latency & load (metrics endpoint / Prometheus):
#   apiserver_request_duration_seconds  (by verb, resource)
#   apiserver_current_inflight_requests (read/write saturation)
#   apiserver_flowcontrol_rejected_requests_total (APF throttling)
kubectl get --raw='/metrics' | grep apiserver_current_inflight_requests
```

Ranked causes:

1. **etcd is the real bottleneck** — apiserver latency is usually downstream of etcd slowness.
   Check etcd first if apiserver `WATCH`/`LIST` are slow (see below).
2. **Expensive LIST/WATCH** ⚠️ — a client doing unpaginated `LIST pods --all-namespaces` in a
   huge cluster, or a hot controller re-listing, hammers apiserver and etcd. `apiserver`
   audit logs / `apiserver_request_total` by `user-agent` finds the offender. This is the most
   common self-inflicted control-plane outage. Use pagination, field/label selectors, and
   informers, not raw lists.
3. **API Priority & Fairness (APF) throttling** — apiserver sheds load by flowschema; a
   flood in one priority level gets `429`s (`apiserver_flowcontrol_rejected_requests_total`).
   The 429 is protecting the cluster — find who's flooding rather than raising limits blindly.
4. **Too many objects / large objects** — huge ConfigMaps/Secrets, millions of events, giant
   CRDs bloat etcd and slow list/watch.
5. **Audit logging to a slow sink** — synchronous audit backend blocking request handling.
6. **apiserver CPU/mem saturation** — under-provisioned control plane for cluster size.

---

## etcd health

etcd is a consistent, quorum-based key-value store. It is latency-sensitive to **disk fsync**
and **network between members**. Most etcd pain is slow disk.

Self-managed etcd:
```bash
ETCDCTL_API=3 etcdctl endpoint health --cluster
ETCDCTL_API=3 etcdctl endpoint status --cluster -w table   # leader, db size, raft index
# key metrics:
#   etcd_disk_wal_fsync_duration_seconds (p99 should be < ~10ms; >100ms = trouble)
#   etcd_disk_backend_commit_duration_seconds
#   etcd_server_leader_changes_seen_total (frequent changes = instability)
#   etcd_mvcc_db_total_size_in_bytes (approaching quota?)
```

Ranked causes:

1. **Slow disk** — WAL fsync latency spikes → request timeouts, leader elections. etcd needs
   low-latency SSD; noisy-neighbor disk or network storage kills it. #1 root cause.
2. **DB size at quota** ⚠️ — default 2GB (often raised to 8GB). At quota, etcd goes
   **read-only** (`mvcc: database space exceeded`) and the cluster can't write — no new pods,
   no updates. Caused by history accumulation without compaction, or event/object bloat. Fix:
   compact + defrag, then raise quota / clean up. Defrag is per-member and briefly blocks that
   member — do it carefully, one at a time.
   ```bash
   etcdctl compact <rev>; etcdctl defrag --cluster
   ```
3. **Lost quorum** — with 3 members, losing 2 means no quorum → cluster is read-only/frozen.
   Restore a member or from snapshot. Always run odd numbers (3 or 5); never 2 or 4.
4. **Network between members** — partition or high latency causes leader thrash.
5. **Frequent leader changes** — instability from disk/network; every change pauses writes.

Backups: `etcdctl snapshot save` — and *test restores*. An untested etcd backup is not a backup.

---

## Controller-manager & scheduler

These are active-passive (leader-elected). If the leader dies and can't fail over, reconciliation
stops even though apiserver/etcd are fine: you can *create* objects but Deployments don't spawn
pods, nodes don't get cleaned up, endpoints don't update.

```bash
kubectl get --raw='/readyz?verbose' | grep -iE 'scheduler|controller'   # self-managed static pods
kubectl -n kube-system get pods -l component=kube-scheduler -o wide
kubectl -n kube-system logs -l component=kube-controller-manager --tail=100
kubectl -n kube-system get lease -A | grep -E 'scheduler|controller'    # who holds the lease?
```

- **Symptom of scheduler down**: new pods stay `Pending` with **no** `FailedScheduling` event
  at all (nothing is even trying to schedule them). Contrast with doc 01 where the scheduler
  *tried* and reported a reason.
- **Symptom of controller-manager down**: ReplicaSets don't reach desired count, Services'
  endpoints go stale, PVCs don't bind, nodes stuck `NotReady` aren't garbage-collected.
- **Leader election flapping** — check the `lease` renew times; TLS/etcd latency can cause the
  leader to lose its lease repeatedly, so nothing ever makes progress.

---

## Admission webhook stalls

Dynamic admission webhooks (validating/mutating) sit **in the write path**. A misbehaving
webhook can wedge every create/update in the cluster — including the very pods needed to run
the webhook itself (chicken-and-egg).

```bash
kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations
kubectl get validatingwebhookconfiguration <name> -o jsonpath='{.webhooks[*].failurePolicy}'; echo
```

- **`failurePolicy: Fail` + webhook backend down** ⚠️ → all matching API writes fail with
  `failed calling webhook ... connection refused/timeout`. If the webhook's `namespaceSelector`
  isn't excluding `kube-system` / the webhook's own namespace, you can deadlock the cluster
  (can't start the webhook pods because admission requires the webhook). Classic self-inflicted
  outage after installing a policy controller (OPA/Gatekeeper, Kyverno) or a mesh injector.
- **Timeout** — webhook slow; `timeoutSeconds` (max 30) exhausted on every request → global
  write latency. Scale the webhook, fix its latency, or narrow its `rules`/selectors.
- **Emergency mitigation**: delete the webhook configuration (not the deployment) to unblock
  the cluster, fix the backend, re-apply. Know which webhooks are `Fail` vs `Ignore` *before*
  an incident.
- **Cert rotation** — webhook TLS cert (CA bundle) expired/mismatched → `x509` errors on every
  call. cert-manager or the injector must keep the `caBundle` current.

---

## Leader election & certs

- **Control-plane cert expiry** (self-managed, kubeadm) ⚠️ — certs default to 1-year validity.
  On the anniversary, apiserver/etcd/kubelet peers reject each other and the cluster falls over
  cluster-wide with `x509: certificate has expired`. `kubeadm certs check-expiration`. Rotate
  proactively; this is a predictable, avoidable, total outage.
- **`kubectl` auth suddenly fails** — client cert / token / kubeconfig expired, or OIDC issuer
  down. Distinguish "I can't auth" (your creds) from "cluster is down" (everyone can't) before
  escalating.

---

## Managed control plane (EKS/GKE/AKS) notes

- You can't restart apiserver/etcd — but you *can* reduce load: kill the runaway LIST/WATCH
  client, delete the bad webhook, prune events/objects, back off controllers.
- Provider control-plane metrics/logs (CloudWatch control-plane logs, GKE control-plane
  observability) show apiserver latency, audit, authenticator, scheduler, controller-manager.
  Turn these on *before* you need them.
- Scaling the cluster (nodes/pods) can exceed the managed control plane's implicit sizing —
  providers scale it based on load but not instantly. Massive fan-out (10k pods at once) can
  transiently degrade it.

---

## Prevention checklist

- Put etcd on dedicated low-latency SSD; alert on `wal_fsync` p99 and db size vs quota.
- Odd-sized etcd (3/5); automated, *restore-tested* snapshots.
- Monitor control-plane cert expiry; rotate ahead of time.
- Every admission webhook: scope with `namespaceSelector` excluding kube-system + its own ns,
  set sane `timeoutSeconds`, and decide `Fail` vs `Ignore` deliberately. Keep a documented
  "delete this webhook to unwedge" runbook.
- Enforce pagination/informers in your controllers and operators; audit for unpaginated LISTs.
- Enable control-plane audit/metrics logging before incidents.
