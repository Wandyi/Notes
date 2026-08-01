# Storage & Volumes

Storage bugs are high-stakes because the failure modes touch *data*. The recurring discipline:
**never force your way past a storage safety mechanism (multi-attach, finalizers, fencing)
without understanding what it's protecting.**

## The binding & attach lifecycle (know the stages)

```
PVC created ──> (StorageClass) provisioner creates PV ──> PVC Bound to PV
Pod scheduled to node ──> CSI ControllerPublish (attach volume to node)
                     ──> CSI NodeStage (format+mount to a global path)
                     ──> CSI NodePublish (bind-mount into the pod)
```

A failure at each stage has a distinct signature. Localize the stage first.

## Fast triage

| Symptom | Stage | Jump to |
|---|---|---|
| `PVC` stuck `Pending` | Provisioning / binding | [PVC Pending](#pvc-stuck-pending) |
| Pod `ContainerCreating`, `FailedAttachVolume` | Controller attach | [Attach](#failedattachvolume--attach-stuck) |
| Pod `ContainerCreating`, `FailedMount` | Node stage/publish | [Mount](#failedmount--mount-stuck) |
| `Multi-Attach error` | RWO volume on 2 nodes | [Multi-attach](#multi-attach-error) |
| StatefulSet pod won't move after node loss | Fencing / RWO safety | [StatefulSet](#statefulset-volume-issues) |
| `PVC` resize not taking effect | Expansion | [Expansion](#volume-expansion-stuck) |
| Pod evicted, `ephemeral-storage` | Node local disk | [Ephemeral](#ephemeral-storage--emptydir) |

```bash
kubectl -n $NS get pvc,pv | grep <name>
kubectl -n $NS describe pvc $PVC          # events tell you provisioning vs binding
kubectl -n $NS describe pod $POD          # attach/mount events
kubectl get volumeattachments | grep <pv>
```

---

## PVC stuck Pending

The PVC hasn't bound to a PV. `describe pvc` events are decisive.

1. **`WaitForFirstConsumer`** (most common, and *not a bug*) — the StorageClass uses
   `volumeBindingMode: WaitForFirstConsumer`, so the PV isn't provisioned until a pod that
   uses the PVC is scheduled. If no pod references it, it stays Pending forever *by design*.
   Create/schedule the consuming pod. This mode exists to co-locate the volume in the pod's
   zone — essential for zonal disks.
2. **No provisioner / wrong StorageClass** — `storageClassName` typo, or the referenced class
   doesn't exist, or no default class and none specified. `kubectl get storageclass`.
3. **Provisioner failing** — CSI controller can't create the disk: cloud quota exhausted, IAM
   permissions, invalid parameters. Check the external-provisioner sidecar:
   ```bash
   kubectl -n <csi-ns> logs deploy/<csi-controller> -c csi-provisioner --tail=100
   ```
4. **Static PV, no match** — you expected to bind to a pre-created PV but capacity, access
   modes, `storageClassName`, or selector don't match. Binding is a mutual match.
5. ⚠️ **Zone/topology deadlock** — `Immediate` binding provisioned the PV in zone A, but the
   pod can only schedule in zone B (taints/affinity) → `volume node affinity conflict`, pod
   Pending forever. Use `WaitForFirstConsumer` to avoid this entirely.

---

## FailedAttachVolume / attach stuck

PVC is Bound, pod scheduled, but the volume won't attach to the node (CSI ControllerPublish).

```bash
kubectl -n $NS describe pod $POD | grep -i -A3 attach
kubectl get volumeattachment | grep <pv>
kubectl -n <csi-ns> logs deploy/<csi-controller> -c csi-attacher --tail=100
```

- **Volume still attached to another node** (previous node didn't detach cleanly, often after
  a node crash) → see [Multi-attach](#multi-attach-error). This is the #1 attach failure.
- **Cloud API throttling / errors** — attach calls rate-limited or failing; csi-attacher logs
  show the cloud error. Common during mass reschedules.
- **Max volumes per node** — cloud instances cap attachable disks (e.g. ~26 on many AWS
  instance types, fewer with Nitro limits). Node full → attach fails. Spread pods or use
  bigger nodes.
- **Detach stuck on the old node** — a leaked `VolumeAttachment` object. If the old node is
  truly gone, you may need to delete the stale VolumeAttachment (carefully, after confirming
  the node is dead).

---

## FailedMount / mount stuck

Attached to the node, but NodeStage/NodePublish (format+mount) fails.

- **Filesystem/format issues** — corrupted fs, wrong `fsType`, first-use format failing.
- **Stale mount / already mounted** — leftover mount from a crashed pod; `nodeplugin` logs.
- **Permissions / `fsGroup`** — the pod can't write because ownership doesn't match
  `securityContext.fsGroup`. Large volumes + `fsGroupChangePolicy: Always` can also make mount
  *slow* (recursive chown of millions of files) — set `fsGroupChangePolicy: OnRootMismatch`.
- **Secret/ConfigMap volume** — "mount" failures here are just the referenced object missing
  (doc 01), not CSI.
- **Subpath issues** — `subPath` referencing a path that doesn't exist yet.

```bash
kubectl -n <csi-ns> logs ds/<csi-node> -c <driver> --tail=100
# on the node:
mount | grep <pv>; dmesg | tail
```

---

## Multi-Attach error

`Multi-Attach error for volume "pvc-…" Volume is already exclusively attached to one node`.
A `ReadWriteOnce` volume can be mounted by pods on **one node only**. This appears when a pod
is rescheduled to a new node while the old attachment persists — almost always after a **node
failure** or during a rolling update where the old pod isn't fully gone.

- **Normal case**: the old pod is terminating; wait for detach (can take minutes). It resolves
  itself once the old attachment releases.
- **Node crashed**: the control plane can't confirm the volume detached from the dead node, so
  it *refuses* to attach elsewhere — protecting you from two writers corrupting data. This is
  the safety mechanism working. Resolution: confirm the old node is truly dead, then the
  node's pods get force-deleted (or you delete the stale VolumeAttachment) so attach can
  proceed. ⚠️ Never force this if the old node might still be running the pod.
- **Design fix**: if you need multi-node access, use `ReadWriteMany` (NFS/EFS/CephFS) — most
  block storage (EBS/PD) is inherently RWO.

---

## StatefulSet volume issues

StatefulSets bind stable identity to stable storage, which makes node loss delicate.

- **Pod won't reschedule after node loss** — StatefulSet controller will *not* create a
  replacement pod with the same ordinal while the old one might still exist (split-brain
  protection). The old pod stays `Terminating`/`Unknown`. You must confirm and force-delete:
  ```bash
  kubectl -n $NS delete pod $POD --grace-period=0 --force   # only after node confirmed dead
  ```
  Then the RWO volume can detach and reattach to the new node. This is intentional friction —
  for a database, two pods with the same identity + volume is data-loss.
- **PVCs are not deleted** with the StatefulSet by default (`persistentVolumeClaimRetentionPolicy`
  is `Retain`-like historically). Scaling down leaves PVCs; scaling back up reuses them. If you
  *want* cleanup on delete/scale, set the retention policy explicitly (1.27+ GA).
- **Ordinal + volume mismatch** after manual PVC surgery → pod `<name>-2` always mounts
  `<pvc>-<name>-2`. Don't hand-edit these mappings.

---

## Volume expansion stuck

You increased `spec.resources.requests.storage` but the pod doesn't see more space.

1. StorageClass must have `allowVolumeExpansion: true` — otherwise the edit is rejected/ignored.
2. Expansion is two phases: **ControllerExpand** (grow the cloud disk) then **NodeExpand**
   (grow the filesystem). The filesystem grow often requires the volume to be mounted; some
   drivers need a **pod restart** to complete online expansion. Check PVC conditions:
   ```bash
   kubectl -n $NS get pvc $PVC -o jsonpath='{.status.conditions}'; echo
   ```
   `FileSystemResizePending` → restart/reschedule the pod to finish.
3. You can only **grow**, never shrink. And some drivers don't support online expansion at all.

---

## Ephemeral storage / emptyDir

Not all storage is a PVC. Node-local ephemeral storage causes evictions that look mysterious.

- **`The node had condition: [DiskPressure]` / pod Evicted with `ephemeral-storage`** — the
  pod's writable layer, logs, or `emptyDir` exceeded its `ephemeral-storage` limit, or the
  node's disk filled. `emptyDir` counts against the node's ephemeral storage. Set
  `resources.limits.ephemeral-storage`; ship logs off-box; watch for unbounded log growth.
- **`emptyDir.medium: Memory`** ⚠️ — a tmpfs `emptyDir` counts against the container's
  **memory** limit and can OOM the pod. Sizing surprise.
- **Image/log churn filling node disk** — see doc 04 (DiskPressure and garbage collection).

---

## Diagnosing "data is there but app can't read it"

- Check the mount is actually where the app expects: `kubectl exec $POD -- mount | grep <path>`
  and `ls -la` the mount point (permissions/ownership vs `runAsUser`/`fsGroup`).
- Read-only mount when the app needs write: `readOnly: true` on the volume or a RO filesystem
  from `securityContext.readOnlyRootFilesystem`.
- Wrong PVC bound (copy-paste in a StatefulSet or multi-volume pod).

---

## Prevention checklist

- Default to `volumeBindingMode: WaitForFirstConsumer` for zonal block storage.
- Set `resources.limits.ephemeral-storage` and keep logs off local disk.
- Use `fsGroupChangePolicy: OnRootMismatch` for large volumes.
- Set an explicit `persistentVolumeClaimRetentionPolicy` on StatefulSets — know whether your
  PVCs survive scale-down.
- Snapshot before any manual PV/VolumeAttachment/finalizer surgery.
- For multi-writer needs, pick an RWX backend up front — don't try to force RWO.
