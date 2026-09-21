# Testing for Failure — Proving It Works Before You Need It

Every doc so far has recommended mechanisms. Timeouts, bulkheads, fallbacks, fail-static
discovery, cell isolation, failover procedures. Here is the uncomfortable property they all
share:

> **They only execute during failures. So in a system that has not failed recently, they are the
> least-tested code you own — and they run at the worst possible moment.**

A fallback that has never executed does not work (doc 03, `P-13`). A standby region that has
never served traffic does not work (doc 13, `I-08`). A rollback procedure that has never been
run takes four times longer than anyone expects (doc 11, `G-01`). A circuit breaker whose
thresholds were copied from a tutorial will trip at the wrong time or not at all.

The only way to know is to make the failure happen deliberately, when you are watching, when you
have chosen the moment, and when the blast radius is bounded.

This doc is a graded programme for doing that, starting from "we have never done this" and
ending at continuous automated fault injection, plus a catalogue of specific experiments mapped
to the POF classes in this collection.

## The principle: a hypothesis, not a stunt

Chaos engineering is often described as "randomly break things in production," which makes it
sound reckless and makes it easy to dismiss. The actual discipline is narrower and more
scientific, and stating it properly is what gets it approved:

1. **Define steady state** as a measurable output of the system, not an internal property.
   "Checkout success rate above 99.9% with p99 under 2 s" — not "all pods running."
2. **Hypothesise that steady state continues** through a specific fault. Write it down.
   "If `promotions-service` returns errors for 100% of requests, checkout success rate will
   remain above 99.9% and p99 will not exceed 2.2 s."
3. **Inject the fault**, with the smallest blast radius that can produce a valid result.
4. **Measure.** Did steady state hold?
5. **If it did not, you have found a real defect** before a customer did. That is the deliverable
   — not the injection.

The difference between this and a stunt is step 2. An experiment with a written hypothesis
produces a result either way: the hypothesis holds (you now have evidence, not belief) or it does
not (you have a bug with a reproduction). An injection with no hypothesis produces an anecdote.

And a rule that makes the whole thing safe: **every experiment must have an abort condition and
a tested abort mechanism, decided before it starts.** "If checkout success rate drops below 99%,
stop the experiment" — with a button, or a command, that has been verified to work.

## The graded programme

Do not start at the end. Each level's prerequisites are the previous level's outputs, and
skipping is how organisations have a bad experience and abandon the practice.

### Level 0 — You can observe failure

**Prerequisite for everything else.** Before injecting anything, verify you would see it.

- Can you tell, from dashboards alone, which dependency is slow?
- Do you have queue time, attempts-per-request, and per-instance success distributions (doc 14)?
- Would an alert fire? How long would it take?

The exercise: pick a past incident and ask whether your current dashboards would have identified
it in under five minutes. If not, fix observability first. **Injecting faults into a system you
cannot observe produces confusion, not information.**

### Level 1 — Fault injection in tests

Cheap, fast, runs in CI, and catches a surprising amount.

For every outbound dependency, an automated test that asserts behaviour when the dependency:

- returns 500
- returns 429
- times out (a server that accepts and never responds — this is the important one, and it is
  different from an error)
- returns malformed data
- is slow but successful (`R-14`)
- refuses the connection

The test asserts what doc 00's hard/soft classification says should happen: a soft dependency's
failure produces a degraded-but-successful response within the normal latency budget; a hard
dependency's failure produces a clean error, quickly.

```python
def test_promotions_hang_does_not_block_checkout(hanging_server):
    """promotions is a SOFT dependency: a hang must not affect checkout latency."""
    with patch_dependency("promotions", hanging_server):
        start = time.monotonic()
        response = client.post("/checkout", json=VALID_ORDER)
        elapsed = time.monotonic() - start

    assert response.status_code == 201
    assert response.json()["promotion_applied"] is False
    assert response.json()["promotion_status"] == "UNAVAILABLE"   # explicit, not null
    assert elapsed < 0.6, f"soft dependency hang added {elapsed:.2f}s"
```

That last assertion is the one that matters and the one usually missing. It is the automated form
of doc 00's test for a soft dependency: **if it hangs, does my latency change?**

A library of these — one per dependency, six cases each — is a day's work per service and it
catches missing timeouts, missing bulkheads, and fallbacks that return the wrong shape.

### Level 2 — The load test that finds the collapse point

Most load testing answers "can we handle X?" That is the less useful question. The useful ones
are: **where does goodput start falling, and once it has fallen, what does it take to recover?**

The procedure, which measures doc 04's hysteresis directly:

```
1. Ramp load up in steps, holding each step long enough to reach steady state
   (at least one autoscaling period and one cache TTL).
2. At each step record: goodput, p50/p99 latency, error rate, queue time,
   attempts/requests, and saturation of every resource.
3. Continue past the point where goodput stops increasing.
4. Continue until goodput starts DECREASING. Record that load — the collapse point.
5. Now ramp DOWN, in steps, holding each.
6. Record the load at which the system returns to healthy — the recovery point.
```

The output is three numbers that almost nobody has:

```
Riverbend checkout-api, measured:
  Peak goodput:      4,200 req/s at 88% of collapse load
  Collapse point:    4,800 req/s        ← beyond this, goodput falls
  Recovery point:    1,100 req/s        ← must drop below this to recover
  Hysteresis gap:    4.4×
```

That 4.4× gap is the operational fact that matters most: **during an incident, restoring traffic
to 4,000 req/s — a level the system handled comfortably an hour ago — will not recover it. You
must get below 1,100.** Knowing that in advance turns doc 04's procedure from theory into a
number on a runbook.

Run this quarterly, per service, in an environment that resembles production. And run it again
after any significant architectural change, because the collapse point moves.

### Level 3 — Fault injection in a pre-production environment

Now inject into a running system, in staging, with production-like traffic (replayed or
synthetic).

The experiments to run first, in this order, because each one is a common total-outage cause:

| Experiment | POF class | Hypothesis to test |
|---|---|---|
| Block the service registry / control plane for 15 min | `D-01` | Traffic continues unaffected; only new deploys fail |
| Unblock it and watch the recovery | `D-09`, `F-09` | The control plane survives every client reconnecting |
| Kill the primary database and force failover | `S-07`, `S-09` | Recovery within RTO; lost writes within RPO; no connection storm |
| Make one dependency hang (not error) | `R-14`, `P-05` | Throughput unchanged for unrelated requests |
| Flush the entire cache | `C-05`, `C-04` | Origin survives; degraded, not down |
| Fill a disk | `S-14` | Graceful degradation; the alert fired before it mattered |
| Partition one AZ from the others | `I-06` | Correct failover; no split brain; capacity holds |
| Introduce 200 ms of clock skew on one node | `L-08` | No correctness violations |
| Send a malformed / oversized request | `F-07` | One failed request, not a fleet crash |
| Roll back a deploy | `G-01`, `G-02` | Completes within the expected time; measure it |

That last one deserves emphasis: **measure your rollback time** as an experiment, with a
stopwatch, from "decide" to "old version serving 100%." It is a number every on-call engineer
should know and almost nobody does.

### Level 4 — Fault injection in production, with bounded blast radius

This is where the real findings are, because staging differs from production in exactly the ways
that matter: traffic mix, data volume, cache state, dependency behaviour, and the long tail of
clients.

The controls that make it responsible:

- **Start with one instance, one cell, or a small traffic percentage.** If you have cells (doc
  13), this is straightforward: run the experiment in one cell. That is one of the strongest
  arguments for cells.
- **Business hours, with the owning team watching.** Not at night to "minimise impact" — at the
  moment you have the most people able to respond. Minimising impact is the blast radius
  control's job.
- **An abort condition and a tested abort.** Automated abort if the steady-state metric degrades
  past a threshold.
- **Announced.** Everyone who could be paged knows it is happening, and the experiment is
  annotated on dashboards.
- **A written hypothesis**, so the result means something.

Start with the read path, where a failure is recoverable by a retry, before touching the write
path.

### Level 5 — Continuous, automated fault injection

Faults injected continuously, at low rates, automatically, as part of normal operation. This is
where the strongest systems end up, and the reason is not thoroughness — it is that **continuous
injection prevents regression.** A fallback that works today will rot in six months unless
something exercises it. Doc 03's "force 0.1% of traffic down the fallback path permanently" is
this principle at its smallest.

At this level, resilience is not a property you achieved once; it is a property continuously
verified, and a change that breaks it fails in the same way a change that breaks a unit test
fails.

## What to inject, and how

| Fault | Mechanism | Finds |
|---|---|---|
| **Latency** on a dependency | Mesh fault filter, toxiproxy, `tc netem` | Missing timeouts, missing bulkheads, wrong timeout values (`R-01`, `R-03`, `P-05`) |
| **Errors** from a dependency | Mesh fault filter (abort with 503) | Missing fallbacks, breaker thresholds, retry classification (`R-07`, `P-01`) |
| **A hang** (accept, never respond) | A purpose-built null server | The most valuable single fault. Timeouts and bulkheads, specifically (`R-14`) |
| **Process kill** | `kubectl delete pod`, Chaos Mesh | Graceful shutdown, in-flight request handling, startup (`E-13`, `F-10`) |
| **Resource exhaustion** | `stress-ng` in a sidecar; lower a limit | Throttling behaviour, OOM handling, degradation (`N-06`, `N-07`) |
| **Network partition** | `iptables DROP`, Chaos Mesh network chaos | Split brain, quorum behaviour, fail-static (`L-05`, `D-01`) |
| **Packet loss / jitter** | `tc netem loss 3%` | Gray failure handling, retry behaviour, HTTP/2 HOL blocking (`R-12`) |
| **Clock skew** | `libfaketime`, Chaos Mesh time chaos | Timestamp-based correctness (`L-08`) |
| **DNS failure** | Break the resolver for one pod | `E-01`, `E-02`, and resolution caching |
| **Certificate expiry** | Deploy an expired cert to one instance | `E-07` and, more usefully, whether monitoring catches it |
| **Poison input** | Send a crafted request | `F-07` fleet-crash resistance |
| **Zone failure** | Cordon and drain every node in one zone | Capacity headroom, spread constraints, cross-zone routing (`N-01`, `I-06`) |
| **Dependency version skew** | Run one instance on an old version | `D-07`, `G-10` |

A service mesh makes most of these one configuration change, applied to a subset of traffic,
reversible instantly. That is one of the better arguments for a mesh (doc 21) and it is rarely
the one made.

```yaml
# Istio: 100% of traffic from one deployment to promotions gets a 5s delay.
# Scoped tightly, removable in seconds.
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
spec:
  hosts: [promotions]
  http:
    - match:
        - sourceLabels: {app: checkout-api, chaos-target: "true"}
      fault:
        delay: {percentage: {value: 100}, fixedDelay: 5s}
      route:
        - destination: {host: promotions}
```

## Game days

A game day is a scheduled exercise where a team responds to a simulated (or real, injected)
incident. It tests things fault injection alone cannot: **the humans, the runbooks, the tooling,
and the communication.**

What they find that nothing else does:

- The runbook references a dashboard that was deleted.
- The person who knows how to do the failover is on holiday and nobody else can.
- The emergency access procedure requires an approval from a system that is also down.
- Two teams each believed the other owned the recovery.
- The rollback takes eleven minutes, not two.
- Nobody knows who decides to evacuate a region (`I-10`).

A format that works:

```
Before (1 week)
  - Pick a scenario from a real risk (not an exotic one)
  - Name a facilitator who knows the scenario and a team who does not
  - Define steady state, abort conditions, and the abort mechanism
  - Announce it

During (90 minutes)
  - 00:00  Inject. The team is paged as they would be normally.
  - 00:00–01:00  They respond using only what they would really have:
                 dashboards, runbooks, and each other. The facilitator
                 answers questions about the environment but does not help.
  - 01:00  Stop, whether or not it is resolved.
  - 01:00–01:30  Debrief, immediately, while it is fresh.

After (same week)
  - Every gap becomes a ticket with an owner
  - Re-run the same scenario in a quarter to verify the fixes
```

Two rules that determine whether people are willing to do it again:

1. **Blameless.** The purpose is to find gaps in the system and the process, not in the people.
   If someone did not know how to do something, that is a documentation or training finding.
2. **Stop at the time limit even if unresolved.** An unresolved game day is a *better* result
   than a resolved one, because it found more. Running over erodes willingness to participate.

Start with a tabletop version — no injection, just "here is the alert, what do you do?" — which
finds most of the runbook and ownership gaps at zero risk, and builds the confidence to do the
real thing.

## The experiment catalogue, by POF class

A concrete backlog. Each is a hypothesis you can write down, inject, and measure.

| Class | Experiment | Expected result if healthy |
|---|---|---|
| `E` | Break DNS for one pod | It uses cached resolution; existing connections work |
| `E` | Expire a certificate on one instance | Monitoring alerted ≥7 days ago; that instance is removed from the pool |
| `E` | Remove all healthy targets from one target group | Load balancer fails open rather than returning 503 |
| `R` | Hang one dependency completely | Unrelated endpoints unaffected; throughput unchanged |
| `R` | Add 500 ms to a dependency's latency | Requests still complete within the deadline; no thread exhaustion |
| `P` | Force a circuit breaker open | Fallback engages; user sees degradation, not an error |
| `P` | Exhaust a bulkhead | Excess requests fail fast, not slowly |
| `F` | Ramp load past the collapse point, then reduce | Recovery point measured; hysteresis gap known |
| `F` | Send a poison request | One failed request; no pod restarts |
| `D` | Block the control plane for 15 minutes | No user impact; `discovery_cache_age` alert fires |
| `D` | Unblock it | Control plane survives the reconnect storm |
| `S` | Force a database failover | Within RTO; lost writes within RPO; no connection storm |
| `S` | Introduce 60 s of replication lag | Reads route to the primary or are rejected; no stale-read correctness bug |
| `T` | Kill the process between two writes of a dual write | Reconciliation detects and repairs it within its interval |
| `T` | Replay a message 100 times | Exactly one effect |
| `C` | Flush the cache | Origin degrades gracefully; the cold-start runbook works |
| `C` | Make the cache unreachable | Requests succeed via origin, with shedding if load-bearing |
| `Q` | Stop a consumer for 30 minutes | Age alert fires within the SLO; drain is rate-limited on restart |
| `Q` | Inject an unprocessable message | It reaches the DLQ; the partition keeps moving |
| `L` | Pause a lock holder for longer than its lease (`SIGSTOP`) | Its writes are fenced and rejected |
| `L` | Skew one node's clock by 200 ms | No correctness violation |
| `G` | Roll back a deploy | Completes within the target; measure it |
| `G` | Push an invalid config | Validation rejects it, or the staged rollout aborts at 1% |
| `N` | Drain one availability zone | Survivors stay under 70% utilisation; no errors |
| `N` | Lower a CPU limit to force throttling | The throttle metric fires; latency degrades predictably |
| `I` | Fail one cell completely | Exactly one cell's users affected; the rest see nothing |
| `I` | Evacuate a region | Within the target RTO; the decision criteria were usable |

Working through even half of this list will find real defects in any system that has not done it
before. The ones that find the most, in most organisations, are: the control-plane block
(`D-01`), the dependency hang (`R-14`), the cache flush (`C-05`), and the measured rollback
(`G-01`).

## What to take away

1. **Resilience mechanisms only execute during failures, which makes them the least-tested code
   you own, running at the worst moment.** Untested resilience is not resilience.
2. **An experiment needs a written hypothesis, a steady-state metric, an abort condition, and a
   tested abort mechanism.** That is what separates chaos engineering from a stunt, and it is
   what gets it approved.
3. **Level 0 first: verify you could observe the failure.** Injecting faults into a system you
   cannot observe produces confusion, not information.
4. **Injecting a *hang* is more valuable than injecting an error**, because a hang is the failure
   mode your error-triggered mechanisms do not detect.
5. **The load test that matters measures three numbers**: peak goodput, collapse point, and
   recovery point. The gap between the last two — Riverbend's is 4.4× — tells you how far you
   must reduce load during an incident, and it is a number that belongs on the runbook.
6. **Measure your rollback time with a stopwatch.** Every on-call engineer should know it and
   almost nobody does.
7. **Production injection with a bounded blast radius finds what staging cannot** — traffic mix,
   data volume, cache state, and the long tail of clients. Cells make this straightforward, which
   is one of the better arguments for cells.
8. **Run experiments during business hours with the team watching.** Minimising impact is the
   blast radius control's job, not the clock's.
9. **Continuous low-rate injection prevents regression.** Resilience that is verified once rots;
   the 0.1%-through-the-fallback pattern is this principle at its smallest.
10. **Game days test the humans, the runbooks, and the tooling** — the things fault injection
    cannot. Blameless, time-boxed, and stop at the limit even if unresolved, because an
    unresolved game day found more.
11. **Start with a tabletop.** It finds most of the runbook and ownership gaps at zero risk and
    builds the willingness to do the real thing.
12. **The four experiments that find the most in most organisations**: block the control plane,
    hang a dependency, flush the cache, and time a rollback.

Next: the case studies. [16-case-ecommerce-riverbend.md](16-case-ecommerce-riverbend.md) takes
everything in part 1 through part 5 and applies it to one system end to end.
