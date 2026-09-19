"""
Helix Support — the handoff contract: briefs, release reasons, no-progress, the Arbiter.

Implements docs/06-handoff-contract.md (structured briefs, hop budgets, the N^2 problem,
anti-amnesia) and docs/03-recommended-architecture.md §4 (release-not-transfer, arbitration).

THE LOAD-BEARING IDEA — specialists cannot transfer to each other.

    swarm:   Billing --transfer_to_orders()--> Orders          (N x (N-1) = 20 edges at N=5)
    here:    Billing --release(reason, brief)--> Arbiter --mint_lease()--> Orders

A specialist emits a ``ReleaseRequest``. That object has no ``target_agent`` field: a
specialist may *suspect* a domain inside the brief, but it cannot route. Routing and lease
minting happen in exactly one place, ``Arbiter.mint_lease``. That is what collapses the
N x (N-1) handoff mesh into N release reasons, and it is why loop containment is provable
here and not in a pure swarm: every hop passes through a component that counts hops.

Two invariants are structural:

  1. ``HandoffBrief`` refuses to be constructed past ``MAX_HOPS``. The ping-pong of docs/11
     ("that's a shipping problem" / "that's a billing problem") terminates at a human by
     construction, not by a prompt asking the model to be reasonable.
  2. ``verified_facts`` and ``customer_claims`` are separate fields with separate types,
     runtime-checked. No method moves an item between them (docs/06, docs/08).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from state import (  # noqa: F401  (RevocationReason re-exported: it is the handoff vocabulary)
    ARBITER,
    ConversationLease,
    CustomerClaim,
    Domain,
    RevocationReason,
    VerifiedFact,
    utcnow,
)

__all__ = [
    "MAX_HOPS",
    "HandoffBrief",
    "HopBudgetExceeded",
    "EscalationRequired",
    "NoProgressDetector",
    "ReleaseRequest",
    "LeasePolicy",
    "Arbiter",
    "RevocationReason",
    "DEFAULT_LEASE_POLICY",
]

MAX_HOPS = 3  # docs/06: three domains is a compound issue, not a chain. Four is a loop.


class HopBudgetExceeded(ValueError):
    pass


class EscalationRequired(RuntimeError):
    """The Arbiter refuses to keep the machine going; a human takes it, brief attached."""

    def __init__(self, reason: RevocationReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


# --- The brief  (docs/06) — a handoff carries a brief, not a transcript --- #
@dataclass(frozen=True)
class HandoffBrief:
    goal: str
    originating_domain: Domain
    suspected_domain: Domain
    verified_facts: tuple[VerifiedFact, ...] = ()
    already_asked: tuple[tuple[str, str], ...] = ()   # (question, answer) — anti-amnesia
    customer_claims: tuple[CustomerClaim, ...] = ()   # SEPARATE from verified_facts
    constraints: tuple[str, ...] = ()
    expected_output: str = ""
    hop_count: int = 0
    prior_holders: tuple[Domain, ...] = ()

    def __post_init__(self) -> None:
        # docs/06 §hop budgets: the termination proof. Unrepresentable > unlikely.
        if self.hop_count > MAX_HOPS:
            raise HopBudgetExceeded(
                f"hop_count={self.hop_count} exceeds MAX_HOPS={MAX_HOPS}; "
                "the conversation escalates to a human instead of hopping again"
            )
        if self.hop_count < 0:
            raise HopBudgetExceeded("hop_count cannot be negative")
        if not self.goal.strip():
            raise ValueError("a brief without a goal is a transcript; state the goal")
        # docs/08: the fact/claim boundary is enforced at the wire, not by convention.
        for f in self.verified_facts:
            if not isinstance(f, VerifiedFact):
                raise TypeError(
                    f"verified_facts got {type(f).__name__}; only tool-attributed "
                    "VerifiedFact may travel in that field"
                )
        for c in self.customer_claims:
            if not isinstance(c, CustomerClaim):
                raise TypeError(f"customer_claims got {type(c).__name__}")

    def has_asked(self, question: str) -> Optional[str]:
        """Anti-amnesia: the next holder must not re-ask what the user already answered."""
        for q, a in self.already_asked:
            if q.strip().lower() == question.strip().lower():
                return a
        return None

    def next_hop(self, *, suspected_domain: Domain, goal: Optional[str] = None) -> "HandoffBrief":
        """Build the successor brief. Raises at the boundary rather than looping."""
        return HandoffBrief(
            goal=goal or self.goal,
            originating_domain=self.suspected_domain,
            suspected_domain=suspected_domain,
            verified_facts=self.verified_facts,
            already_asked=self.already_asked,
            customer_claims=self.customer_claims,
            constraints=self.constraints,
            expected_output=self.expected_output,
            hop_count=self.hop_count + 1,
            prior_holders=self.prior_holders + (self.suspected_domain,),
        )


@dataclass(frozen=True)
class ReleaseRequest:
    """What a specialist may emit. Note the absence of a ``target_agent`` field."""
    lease_id: str
    reason: RevocationReason
    brief: HandoffBrief
    addressed_to: str = ARBITER   # always. There is no other destination.


# --- No-progress detector  (docs/03 §4, docs/06) — deterministic, zero inference --- #
_WORD = re.compile(r"[a-z0-9']+")


def token_similarity(a: str, b: str) -> float:
    """Jaccard overlap on lowercased word tokens. Stdlib, deterministic, explainable.

    Production swaps in an embedding cosine; the *rule* — not the metric — is the contract.
    """
    ta, tb = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class NoProgressDetector:
    """Fires after 2 consecutive turns with no new facts, no tool calls, and near-repeat text.

    This is the backstop for imperfect self-reported out-of-scope (docs/03 §4 and the
    "honest limitations" §9.3). It runs per turn at zero inference cost; firing emits the
    ``NO_PROGRESS`` signal, which is in every lease's ``revoke_on``, which returns the
    session to ARBITRATE. That chain is the termination proof for the leased hot path.
    """

    def __init__(self, *, similarity_threshold: float = 0.85, trip_after: int = 2) -> None:
        self.similarity_threshold = similarity_threshold
        self.trip_after = trip_after
        self._previous_text: str = ""
        self.streak: int = 0

    def observe(
        self,
        *,
        agent_text: str,
        new_fact_ids: Sequence[str] = (),
        tool_calls: Sequence[str] = (),
    ) -> bool:
        similarity = token_similarity(agent_text, self._previous_text)
        stalled = (
            not new_fact_ids
            and not tool_calls
            and similarity >= self.similarity_threshold
        )
        self.streak = self.streak + 1 if stalled else 0
        self._previous_text = agent_text
        return self.streak >= self.trip_after

    def reset(self) -> None:
        self._previous_text = ""
        self.streak = 0


# --- Arbitration  (docs/03 §4) — the ONLY place a lease is minted -------- #
# Proposable action types per domain — docs/00 §1 ("may *propose* writes").
DOMAIN_ACTIONS: Mapping[Domain, frozenset[str]] = {
    Domain.BILLING: frozenset({"refund", "credit", "retry_charge", "plan_change"}),
    Domain.ORDERS: frozenset({"reship", "cancel_order", "address_change"}),
    Domain.TECHNICAL: frozenset({"create_bug_ticket"}),      # otherwise advisory
    Domain.ACCOUNT: frozenset({"reset_mfa", "change_email", "transfer_ownership"}),
    Domain.RETURNS: frozenset({"issue_rma", "approve_return", "restock_credit"}),
}

DOMAIN_READ_TOOLS: Mapping[Domain, frozenset[str]] = {
    Domain.BILLING: frozenset({"get_invoice", "get_charges", "get_plan"}),
    Domain.ORDERS: frozenset({"get_order", "get_tracking", "get_warehouse_exception"}),
    Domain.TECHNICAL: frozenset({"search_kb", "get_error_logs", "get_status_page"}),
    Domain.ACCOUNT: frozenset({"get_user", "get_auth_events", "get_seats"}),
    Domain.RETURNS: frozenset({"get_rma", "get_return_window", "get_order"}),
}


@dataclass(frozen=True)
class LeasePolicy:
    """What the Arbiter is permitted to grant. Tier-derived, deterministic, no inference."""
    max_turns: int = 6
    idle_ttl: timedelta = timedelta(minutes=30)
    absolute_ttl: timedelta = timedelta(hours=24)
    ceilings: Mapping[str, Decimal] = field(
        default_factory=lambda: {
            "refund": Decimal("200.00"),
            "credit": Decimal("50.00"),
            "retry_charge": Decimal("0.00"),
            "plan_change": Decimal("0.00"),
            "reship": Decimal("150.00"),
            "cancel_order": Decimal("0.00"),
            "address_change": Decimal("0.00"),
            "create_bug_ticket": Decimal("0.00"),
            "reset_mfa": Decimal("0.00"),
            "change_email": Decimal("0.00"),
            "transfer_ownership": Decimal("0.00"),
            "issue_rma": Decimal("0.00"),
            "approve_return": Decimal("0.00"),
            "restock_credit": Decimal("75.00"),
        }
    )


DEFAULT_LEASE_POLICY = LeasePolicy()


class Arbiter:
    """Runs ONLY on lease break (docs/03 §2) — roughly once per conversation, not per turn."""

    def __init__(self) -> None:
        self._minted = 0

    def mint_lease(self, brief: HandoffBrief, policy: LeasePolicy) -> ConversationLease:
        """The single mint point. Every hop in the system passes through this counter."""
        # docs/06: the hop budget terminates in a human, not in another hop.
        if brief.hop_count >= MAX_HOPS:
            raise EscalationRequired(
                RevocationReason.NEEDS_HUMAN,
                f"hop budget exhausted at {brief.hop_count}/{MAX_HOPS}",
            )
        # docs/11 §ping-pong: a domain that already held and released does not get it back.
        if brief.suspected_domain in brief.prior_holders:
            raise EscalationRequired(
                RevocationReason.NO_PROGRESS,
                f"{brief.suspected_domain.value} already held this session; refusing ping-pong",
            )
        holder = brief.suspected_domain
        may_propose = {
            action: policy.ceilings[action] for action in sorted(DOMAIN_ACTIONS[holder])
        }
        self._minted += 1
        return ConversationLease.mint(
            lease_id=f"L-{self._minted:04d}",
            holder=holder,
            scope={holder},                       # one domain per lease; compound -> FANOUT
            turns=policy.max_turns,
            read_tools=DOMAIN_READ_TOOLS[holder],
            may_propose=may_propose,
            policy_ceilings=policy.ceilings,       # mint() enforces narrowing-only
            now=utcnow(),
            idle_ttl=policy.idle_ttl,
            absolute_ttl=policy.absolute_ttl,
        )

    @property
    def leases_minted(self) -> int:
        return self._minted


# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # smoke test: `python3 handoff.py`
    brief = HandoffBrief(
        goal="explain the second March charge",
        originating_domain=Domain.BILLING,
        suspected_domain=Domain.ORDERS,
        already_asked=(("Was the seat add-on intentional?", "No, I clicked it by accident."),),
    )
    arb = Arbiter()
    lease = arb.mint_lease(brief, DEFAULT_LEASE_POLICY)
    print("minted           :", lease.lease_id, lease.holder.value,
          "may_propose=", {k: str(v) for k, v in lease.may_propose.items()})
    print("anti-amnesia     :", brief.has_asked("was the seat add-on intentional?"))
    hops = brief
    for _ in range(MAX_HOPS + 1):
        try:
            hops = hops.next_hop(suspected_domain=Domain.TECHNICAL)
        except HopBudgetExceeded as exc:
            print("hop budget       : rejected —", exc)
            break
    try:
        arb.mint_lease(hops, DEFAULT_LEASE_POLICY)
    except EscalationRequired as exc:
        print("arbiter          : refuses to hop again —", exc)
    det = NoProgressDetector()
    text = "I'm still looking into that charge for you."
    print("no-progress turn1:", det.observe(agent_text=text))
    print("no-progress turn2:", det.observe(agent_text=text))
    print("no-progress turn3:", det.observe(agent_text=text), "<- fires -> ARBITRATE")
