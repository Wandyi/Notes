# Networking & DNS

Networking bugs are where "it works on my node but not yours" lives. The key discipline:
**test one hop at a time** and know which component owns each hop.

## The request path (know the hops)

```
pod A ──(1 CNI/pod netns)──> pod A's veth ──(2 node routing)──> node
      ──(3 kube-proxy/IPVS/iptables: ClusterIP -> endpoint)──>
      ──(4 CNI overlay/underlay)──> node B ──> pod B's veth ──> pod B
DNS:  pod ──> /etc/resolv.conf (CoreDNS ClusterIP) ──> CoreDNS pod ──> upstream
Ingress: client ──> LB ──> Ingress controller pod ──> Service ──> pod
```

Each hop has an owner: (1)(4) CNI plugin, (2) node kernel routing, (3) kube-proxy,
DNS → CoreDNS, north-south → Ingress/LB.

## Fast triage

| Symptom | Most likely | Test first |
|---|---|---|
| `Service` name won't resolve | DNS (CoreDNS / ndots / resolv.conf) | [DNS](#dns-coredns) |
| Resolves but connection refused/times out | Endpoints empty, or NetworkPolicy | [Service/Endpoints](#service-has-no-endpoints), [NetworkPolicy](#networkpolicy) |
| Works to some pods, not others | Cross-node CNI / one bad node | [Cross-node](#cross-node-pod-to-pod-fails) |
| Intermittent resets/timeouts under load | conntrack full / MTU / probe flap | [conntrack](#conntrack-table-full), [MTU](#mtu--fragmentation) |
| External LB → 503/timeout | Ingress/Service targets or health checks | [North-south](#ingress--loadbalancer) |
| New pods stuck ContainerCreating (network) | CNI IPAM exhausted | [IPAM](#cni--ipam) |

Set up a throwaway debug pod once; reuse it for every test below:
```bash
kubectl -n $NS run netshoot --rm -it --image=nicolaka/netshoot --restart=Never -- bash
# inside: dig, nslookup, curl, nc, tcpdump, ip, conntrack, mtr all available
```

---

## Service has no Endpoints

A `ClusterIP` is just a virtual IP; it forwards to the pods listed in its `EndpointSlice`.
No endpoints → connection refused / timeout even though the Service "exists".

```bash
kubectl -n $NS get endpointslices -l kubernetes.io/service-name=$SVC -o wide
kubectl -n $NS get svc $SVC -o jsonpath='{.spec.selector}'; echo
kubectl -n $NS get pods --show-labels | grep -i <app>
```

Ranked causes:

1. **Selector ↔ pod label mismatch** — the Service selects `app=api` but pods are labeled
   `app=api-server`. Endpoints empty. Fix labels or selector. (#1 cause by far.)
2. **No Ready pods** — pods exist but are `0/1 READY` (readiness failing), so they're excluded
   from endpoints. This is *correct* behavior masking an app problem — go fix readiness (doc 01).
3. **`targetPort` wrong** — Service points at a port the container isn't listening on.
   Connection refused. Verify `containerPort` / actual listen port.
4. **Named port mismatch** — `targetPort: http` but the container port isn't named `http`.
5. **Pods on a NotReady node** are pruned from endpoints.

⚠️ A `headless` Service (`clusterIP: None`) returns pod IPs via DNS instead of load-balancing;
"no ClusterIP" there is expected, not a bug. StatefulSets use these.

---

## DNS (CoreDNS)

DNS is the most common "networking" incident and it's rarely the network — it's resolv.conf
semantics, CoreDNS capacity, or upstream.

```bash
# from netshoot:
nslookup kubernetes.default
nslookup $SVC.$NS.svc.cluster.local
cat /etc/resolv.conf                       # nameserver = CoreDNS ClusterIP, search, ndots
kubectl -n kube-system get pods -l k8s-app=kube-dns
kubectl -n kube-system logs -l k8s-app=kube-dns --tail=100
```

- **`ndots:5` tax** ⚠️ — default `resolv.conf` has `ndots:5`, so any name with fewer than 5
  dots is tried against every `search` suffix *first*. `curl api.example.com` → 4–5 failed
  lookups before the real one. Under load this multiplies DNS QPS 5x and adds latency. Fixes:
  use FQDNs with a trailing dot (`api.example.com.`) to skip search, or set a per-pod
  `dnsConfig` with lower `ndots`, or use NodeLocal DNSCache.
- **CoreDNS overloaded / rate-limited** — high latency or SERVFAIL under load. Check CoreDNS
  CPU and the `coredns_dns_request_duration_seconds` / cache hit metrics. Scale CoreDNS
  replicas, enable `cache`, deploy **NodeLocal DNSCache** (huge win: caches on each node, cuts
  cross-node DNS and conntrack churn).
- **Upstream resolution failing** — external names fail but in-cluster works → CoreDNS
  `forward` upstream (node's resolv.conf / cloud DNS) is broken. Check the `forward` plugin
  target and node egress.
- **Intermittent DNS timeouts** ⚠️ — the classic race: parallel UDP DNS lookups through
  SNAT/conntrack drop due to a kernel race (DNAT insertion race). Symptoms: 5s stalls
  (glibc DNS retry timer). Mitigate with NodeLocal DNSCache (uses TCP/local), `single-request`
  options, or musl-based images. This one has burned everyone.
- **`Pod` vs `Service` DNS policy** — pods with `hostNetwork: true` default to
  `dnsPolicy: Default` (node's resolv.conf) and *won't* resolve cluster names unless you set
  `dnsPolicy: ClusterFirstWithHostNet`.

---

## NetworkPolicy

If DNS resolves and endpoints exist but the connection still hangs/refuses, suspect policy —
especially "it broke right after we added a NetworkPolicy" or "only in namespace X".

Key mental model: **NetworkPolicies are additive allow-lists, and they are deny-by-default
*once any policy selects a pod*.** A pod with zero policies is fully open; the moment one
ingress policy selects it, all non-matching ingress is denied.

```bash
kubectl -n $NS get networkpolicy
kubectl -n $NS describe networkpolicy <name>
# Which policies select this pod?
kubectl -n $NS get networkpolicy -o yaml | grep -A5 podSelector
```

Debugging checklist:

1. Is there a `default-deny` policy in the namespace? Then every allowed flow needs an
   explicit rule — including **DNS egress to kube-system** (a common miss: default-deny egress
   silently breaks DNS for the whole namespace).
2. Ingress AND egress are separate — allowing ingress on the server doesn't help if egress is
   denied on the client. You need both sides to permit the flow.
3. `namespaceSelector` vs `podSelector` semantics: in one `from`/`to` entry, combining them
   (no `-`) means AND (pods matching X *in* namespaces matching Y); as separate list items it's
   OR. This trips people constantly.
4. ⚠️ Your CNI must *enforce* NetworkPolicy (Calico, Cilium, Antrea do; flannel alone does
   not). If policies are ignored entirely, the CNI doesn't implement them.
5. CIDR-based `ipBlock` rules and SNAT: traffic that egresses and comes back SNAT'd may not
   match the pod-selector you expect.

Cilium/Calico give you flow-level visibility that makes this tractable:
```bash
# Cilium:
kubectl -n kube-system exec ds/cilium -- hubble observe --to-pod $NS/$POD --verdict DROPPED
# Calico:
calicoctl get networkpolicy -o wide
```

---

## Cross-node pod-to-pod fails

Same-node works, cross-node fails (or one specific node is unreachable) → CNI overlay/underlay
or node routing.

```bash
# from a pod on node A, curl a pod IP on node B:
kubectl -n $NS get pods -o wide            # get IPs + nodes
# from netshoot on node A:
ping <podB-IP>; curl <podB-IP>:<port>; traceroute <podB-IP>
```

- **One node broken** — CNI agent (calico-node / cilium / aws-node) crashlooping on that node,
  stale routes, or the node dropped out of the overlay mesh. `kubectl -n kube-system get pods
  -o wide | grep <node>`; check the CNI daemonset pod on that node.
- **All cross-node broken** — overlay tunnel down (VXLAN/IPIP/WireGuard), security group /
  firewall blocking the overlay port (e.g. VXLAN UDP 8472, IPIP proto 4, BGP 179), or an
  underlay routing/peering issue.
- **Security groups / firewall** (cloud) — the classic: node SG doesn't allow the pod CIDR or
  the overlay port between nodes. Managed-cluster gotcha after a manual SG change.

---

## MTU / fragmentation

Symptom: small requests fine, large payloads or TLS handshakes hang/reset. Overlay
encapsulation (VXLAN adds 50 bytes, etc.) shrinks the effective MTU; if the pod interface MTU
is wrong, large packets get silently dropped when DF is set (no ICMP path back).

```bash
# inside netshoot:
ip link show eth0                          # note MTU
ping -M do -s 1400 <remote-pod-ip>         # find the DF cutoff; failures => MTU too big
```

Fix: set the CNI MTU to account for encapsulation overhead (e.g. 1450 for VXLAN on a 1500
underlay; lower on clouds with jumbo/again-encapsulated networks). This is a "works for
months then a new payload size breaks" bug — insidious.

---

## conntrack table full

Netfilter tracks every connection; when `nf_conntrack` fills, new connections are dropped and
you see `nf_conntrack: table full, dropping packet` in `dmesg` and random timeouts under load.

```bash
# on the node:
sudo sysctl net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max
dmesg | grep -i conntrack
```

Fix: raise `nf_conntrack_max`, reduce churn (keep-alive, connection pooling, NodeLocal
DNSCache to kill short-lived DNS conns), and check for a connection leak in the app. High
conntrack is often a *symptom* of the DNS ndots problem above.

---

## CNI / IPAM

New pods stuck `ContainerCreating` with `failed to setup network for sandbox` / IP allocation
errors → the CNI can't hand out an IP on that node.

- **AWS VPC CNI**: each node has a max ENI/IP budget (instance-type dependent). At the limit,
  new pods can't get an IP. Check `kubectl -n kube-system logs -l k8s-app=aws-node`; consider
  prefix delegation to raise the per-node IP ceiling.
- **Subnet exhaustion**: the pod/subnet CIDR ran out of addresses cluster-wide. Bigger CIDR /
  additional subnets.
- **CNI agent down** on the node — daemonset pod crashlooping.

---

## Ingress / LoadBalancer

North-south (external) traffic. Debug outside-in *and* inside-out:

```bash
kubectl -n $NS get ingress $ING -o wide
kubectl -n $NS describe ingress $ING           # backend, events, controller annotations
kubectl -n $NS get svc $SVC -o wide            # LB EXTERNAL-IP, ports
kubectl -n <ingress-ns> logs deploy/<ingress-controller> --tail=100
```

- **LB `EXTERNAL-IP` stuck `<pending>`** — cloud-controller can't provision (IAM, quota,
  subnet tags, wrong `loadBalancerClass`). Check cloud-controller-manager events/logs.
- **LB healthy but 503** — target Service has no ready endpoints, or the LB health check hits a
  path the app 404s. Align the health-check path/port with a real readiness endpoint.
- **`externalTrafficPolicy: Local`** ⚠️ — preserves client source IP but only routes to pods
  *on the node the LB targeted*; if a node has no pod for the Service, its health check fails
  and you get uneven/failed routing. Great for source IP, sharp edge for availability.
- **Ingress path/host mismatch** — `pathType` (`Prefix` vs `Exact` vs `ImplementationSpecific`)
  and host rules don't match the request. `describe ingress` shows the resolved backend.
- **TLS/cert** — wrong/absent secret, SNI mismatch, cert-manager didn't issue. Controller logs
  will say.

---

## Prevention checklist

- Deploy **NodeLocal DNSCache** and tune `ndots` — removes the two nastiest DNS classes at once.
- If you use default-deny NetworkPolicies, always ship an explicit **allow-DNS-egress** rule.
- Pin CNI **MTU** correctly for your overlay/cloud from day one.
- Align **LB/Ingress health-check paths** with real readiness endpoints.
- Keep a `netshoot` manifest handy in every cluster for instant in-cluster diagnosis.
