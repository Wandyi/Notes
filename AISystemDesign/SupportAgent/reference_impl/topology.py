"""
Helix Support — the topology argument, executable.

Implements docs/01-topology-comparison.md and reproduces the model-call table of
docs/02-cost-and-latency-model.md §2 over the two archetypes of docs/00-overview.md §3.

There is no LLM here. Every "model call" is a counter increment, and every assumption is a
named constant at the top of the file. That is the point: doc 02's central claim —
*supervisor pays a per-turn routing+synthesis tax; swarm pays it back in compound latency;
the lease pays neither* — is arithmetic, so it should be runnable and falsifiable. Change
``L_SPEC`` from 3 to 6 and the table changes; if the table stops matching doc 02, one of
them is wrong and you can see which.

Accounting rules, stated so they can be argued with:

  * A "model call" is one request to a model. Specialist inner loops are modelled as
    L_SPEC=3 calls on a lookup/act turn and L_SPEC_PRIME=2 on a no-new-lookup turn
    (docs/02 §1).
  * A "sequential hop" is a model call on the latency critical path. Parallel fan-out of
    k branches costs sum(branches) calls but max(branches) hops.
  * The lease check, ledger append, and budget check are deterministic code: 0 calls,
    0 hops (docs/03 §2). This is the whole reason the hybrid matches swarm on Archetype A.

Run: ``python3 topology.py`` (add ``--trace`` for the per-call log).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Sequence

from handoff import DEFAULT_LEASE_POLICY, Arbiter, HandoffBrief
from state import Domain, utcnow

# --- docs/02 §1 assumptions ------------------------------------------------- #
L_SPEC = 3        # specialist inner loop, lookup/act turn: reason -> tool -> answer
L_SPEC_PRIME = 2  # specialist inner loop, no new lookup: reason -> answer
TRIAGE = 1        # small-model intent + compound classification, once per session
ROUTE = 1         # supervisor routing call, EVERY turn
SYNTH = 1         # supervisor synthesis call, EVERY turn
REDUCE = 1        # arbiter reduce over a fan-out, once per compound turn


# --- Scripted conversations (docs/00 §3) --------------------------------- #
@dataclass(frozen=True)
class ScriptedTurn:
    text: str
    domains: tuple[Domain, ...]
    specialist_calls: int

    @property
    def compound(self) -> bool:
        return len(self.domains) > 1


ARCHETYPE_A: tuple[ScriptedTurn, ...] = (
    ScriptedTurn("Why was I charged twice in March?", (Domain.BILLING,), L_SPEC),
    ScriptedTurn("No, I clicked that by accident.", (Domain.BILLING,), L_SPEC_PRIME),
    ScriptedTurn("What are my options?", (Domain.BILLING,), L_SPEC_PRIME),
    ScriptedTurn("Remove and refund.", (Domain.BILLING,), L_SPEC),
)

ARCHETYPE_B: tuple[ScriptedTurn, ...] = (
    ScriptedTurn(
        "Order #88213 hasn't shipped, I think I was double-charged, and my teammate can't log in.",
        (Domain.ORDERS, Domain.BILLING, Domain.ACCOUNT),
        L_SPEC,
    ),
)


# --- The meter ----------------------------------------------------------- #
@dataclass
class Meter:
    topology: str
    calls: int = 0
    hops: int = 0
    log: list[str] = field(default_factory=list)

    def call(self, who: str, why: str, n: int = 1) -> None:
        """n sequential model calls: they cost n calls and n hops."""
        self.calls += n
        self.hops += n
        self.log.append(f"    {who:<10} {why:<38} +{n} call(s)  +{n} hop(s)")

    def parallel(self, who: str, why: str, branches: Sequence[int]) -> None:
        """k concurrent branches: sum(branches) calls, max(branches) hops."""
        self.calls += sum(branches)
        self.hops += max(branches)
        self.log.append(
            f"    {who:<10} {why:<38} +{sum(branches)} call(s)  +{max(branches)} hop(s)"
        )

    def free(self, who: str, why: str) -> None:
        """Deterministic control-plane work: no inference, no latency worth counting."""
        self.log.append(f"    {who:<10} {why:<38} +0 call(s)  +0 hop(s)")


# --- The three topologies ------------------------------------------------ #
def run_supervisor(script: Sequence[ScriptedTurn]) -> Meter:
    """Star: the supervisor owns the transcript and pays route+synthesise EVERY turn."""
    m = Meter("supervisor")
    for i, turn in enumerate(script, 1):
        m.log.append(f"  turn {i}: {turn.text[:52]}")
        m.call("supervisor", "route (on the least info available)", ROUTE)
        if turn.compound:
            m.parallel("specialists", f"fan-out x{len(turn.domains)} (parallel)",
                       [turn.specialist_calls] * len(turn.domains))
        else:
            m.call(turn.domains[0].value, "specialist inner loop", turn.specialist_calls)
        m.call("supervisor", "synthesise (the telephone game)", SYNTH)
    return m


def run_swarm(script: Sequence[ScriptedTurn]) -> Meter:
    """Mesh: control is a single token one agent holds — so compound work serialises."""
    m = Meter("swarm")
    active: Domain | None = None
    for i, turn in enumerate(script, 1):
        m.log.append(f"  turn {i}: {turn.text[:52]}")
        if active is None:
            m.call("triage", "intent classification (small model)", TRIAGE)
        for domain in turn.domains:  # SEQUENTIAL by construction (docs/01 §2.3)
            if active is not None and active is not domain:
                m.free(active.value, f"transfer_to_{domain.value}() — 1 of N x (N-1) edges")
            m.call(domain.value, "specialist inner loop", turn.specialist_calls)
            active = domain
    return m


def run_leased(script: Sequence[ScriptedTurn]) -> Meter:
    """Leased supervision: deterministic lease check on the hot path, fan-out on compound."""
    m = Meter("leased")
    arbiter = Arbiter()
    lease = None
    for i, turn in enumerate(script, 1):
        m.log.append(f"  turn {i}: {turn.text[:52]}")
        if lease is None:
            m.call("triage", "intent + compound detection (small model)", TRIAGE)
        else:
            status = lease.check(utcnow(), requested_domain=turn.domains[0])
            m.free("lease mgr", f"check -> {status} (dict lookup + 2 int compares)")
            if not status:
                m.call("arbiter", "re-arbitrate after revocation", 1)
                lease = None

        if turn.compound:
            # docs/03 §3: compound BREAKS the lease. Sequential chains are the worst
            # possible shape for independent sub-problems, and this design refuses them.
            m.parallel("specialists", f"FANOUT x{len(turn.domains)} (parallel)",
                       [turn.specialist_calls] * len(turn.domains))
            m.call("arbiter", "reduce + synthesise", REDUCE)
            lease = None
        else:
            domain = turn.domains[0]
            if lease is None or lease.holder is not domain:
                brief = HandoffBrief(goal=turn.text, originating_domain=domain,
                                     suspected_domain=domain)
                lease = arbiter.mint_lease(brief, DEFAULT_LEASE_POLICY)
                m.free("arbiter", f"mint_lease -> {lease.lease_id} (deterministic)")
            m.call(domain.value, "specialist inner loop", turn.specialist_calls)
            lease = lease.consume_turn()
            m.free("ledger", "append TurnRecord (WORM, no LLM)")
    return m


RUNNERS: dict[str, Callable[[Sequence[ScriptedTurn]], Meter]] = {
    "supervisor": run_supervisor,
    "swarm": run_swarm,
    "leased": run_leased,
}

# docs/02 §2 — the published numbers, as (model calls, sequential hops).
# Hops for Archetype A are derived here (doc 02 tabulates hops only for B); everything is
# sequential in a single-domain conversation, so hops == calls for all three topologies.
EXPECTED: dict[str, dict[str, tuple[int, int]]] = {
    "Archetype A — deep single domain (4 turns, 1 domain)": {
        "supervisor": (18, 18),
        "swarm": (11, 11),
        "leased": (11, 11),
    },
    "Archetype B — compound one-shot (1 turn, 3 independent domains)": {
        "supervisor": (11, 5),
        "swarm": (10, 10),
        "leased": (11, 5),
    },
}

SCRIPTS = {
    "Archetype A — deep single domain (4 turns, 1 domain)": ARCHETYPE_A,
    "Archetype B — compound one-shot (1 turn, 3 independent domains)": ARCHETYPE_B,
}


def main(trace: bool = False) -> int:
    print(f"assumptions (docs/02 §1): L_spec={L_SPEC}  L_spec'={L_SPEC_PRIME}  "
          f"triage={TRIAGE}  route={ROUTE}  synth={SYNTH}  reduce={REDUCE}")
    failures = 0
    for title, script in SCRIPTS.items():
        print(f"\n{title}")
        print(f"  {'topology':<12}{'model calls':>12}{'seq. hops':>12}{'doc 02 §2':>14}   ")
        print("  " + "-" * 54)
        for name, runner in RUNNERS.items():
            meter = runner(script)
            want = EXPECTED[title][name]
            got = (meter.calls, meter.hops)
            ok = got == want
            failures += 0 if ok else 1
            print(f"  {name:<12}{meter.calls:>12}{meter.hops:>12}"
                  f"{f'{want[0]} / {want[1]}':>14}   {'OK' if ok else 'MISMATCH'}")
            if trace:
                print("\n".join(meter.log))
    print("\nreading: A is 70% of turns — the supervisor's +64% call count there is the "
          "business case.\n         B is 15% of conversations — the swarm's 2x hop count "
          "there is the latency case.\n         The lease wins A on calls and B on hops "
          "because governance and conversation\n         run at different frequencies "
          "(docs/03 §1).")
    if failures:
        print(f"\n{failures} row(s) disagree with docs/02 §2 — the model and the doc have "
              "drifted; fix one.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(trace="--trace" in sys.argv))
