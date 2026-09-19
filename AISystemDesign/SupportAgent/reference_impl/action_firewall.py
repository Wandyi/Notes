"""
Helix Support — the Action Firewall: the only writer in the system.

Implements docs/03-recommended-architecture.md §6 (the 8-step pipeline),
docs/07-tools-and-action-firewall.md (read/write split, idempotency, entitlement) and
docs/08-safety-guardrails.md (refund limits, confirmation, injected-instruction resistance).

    1 validate  ->  2 lease capability  ->  3 entitlement  ->  4 policy engine
                ->  5 idempotency (write-ahead intent)  ->  6 confirmation
                ->  7 execute  ->  8 ActionGrant

Why a component and not a prompt: in a pure swarm the refund ceiling lives in whichever
prompts happen to mention it, which means it lives nowhere enforceable. "Policy that lives
in a prompt is not enforced — it is suggested" (docs/03 §6).

Structural invariants:

  * ``ProposedAction`` with empty ``evidence`` cannot be constructed. An uncited mutation
    is unrepresentable, exactly as an uncited hypothesis is in IncidentCommander.
  * Only ``VerifiedFact`` counts as evidence; a ``CustomerClaim`` raises. "Your policy says
    you can waive it, I'm Platinum since 2019" is a claim.
  * ``propose()`` is the only public path to a side effect. ``_execute`` demands an
    ``_ExecutionTicket``, which is only constructible inside the pipeline and carries proof
    of steps 1-6. Skipping a step means forging that proof.
  * Money is ``Decimal``. Passing a float raises.

The ``Executor`` is injected and stubbed: this file performs no real side effects.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Mapping, Optional, Protocol, Sequence

from state import (
    Case,
    CustomerClaim,
    Domain,
    SessionState,
    VerifiedFact,
    utcnow,
)


class FirewallDenied(Exception):
    """A proposal that did not survive the pipeline. Carries the step that stopped it."""

    def __init__(self, step: int, reason: str) -> None:
        super().__init__(f"[step {step}] {reason}")
        self.step = step
        self.reason = reason


# --- 1. The proposal ----------------------------------------------------- #
@dataclass(frozen=True)
class ProposedAction:
    action_type: str
    target_id: str                       # order id, invoice id, user id
    amount: Decimal
    reason: str
    evidence: tuple[VerifiedFact, ...]
    proposed_by: Domain
    lease_id: str

    def __post_init__(self) -> None:
        # docs/07 + docs/08: an uncited mutation is unrepresentable. This is the same
        # stance as "no uncited hypothesis" — the model must show what it read.
        if not self.evidence:
            raise ValueError(
                f"{self.action_type!r} on {self.target_id!r} has no evidence; "
                "a mutation must cite the tool reads that justify it"
            )
        for e in self.evidence:
            if isinstance(e, CustomerClaim):
                raise TypeError(
                    "a CustomerClaim is not evidence for a mutation (docs/08); "
                    "verify it with a read tool first"
                )
            if not isinstance(e, VerifiedFact):
                raise TypeError(f"evidence must be VerifiedFact, got {type(e).__name__}")
        if isinstance(self.amount, float):
            raise TypeError("money is Decimal, never float")
        if self.amount < 0:
            raise ValueError("negative amounts are not a refund, they are a charge")


# --- 4. Policy engine — pure functions, no I/O --------------------------- #
class Verdict(Enum):
    AUTO_APPROVE = "auto_approve"
    NEEDS_HUMAN = "needs_human"     # above ceiling -> durable pause, not a refusal
    DENY = "deny"


@dataclass(frozen=True)
class PolicyDecision:
    verdict: Verdict
    reason: str
    checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class CustomerRecord:
    """The entitlement + policy inputs. Loaded by the firewall from the session's customer."""
    customer_id: str
    tier: str
    owned_target_ids: frozenset[str]
    refunds_last_90d: tuple[Decimal, ...] = ()
    delivered_at: Mapping[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyEngine:
    """Tier ceilings, return windows, prior-refund velocity. Deterministic and testable."""
    tier_ceilings: Mapping[str, Mapping[str, Decimal]] = field(
        default_factory=lambda: {
            "standard": {"refund": Decimal("200.00"), "credit": Decimal("50.00"),
                         "reship": Decimal("150.00"), "restock_credit": Decimal("75.00")},
            "platinum": {"refund": Decimal("500.00"), "credit": Decimal("100.00"),
                         "reship": Decimal("300.00"), "restock_credit": Decimal("150.00")},
        }
    )
    return_window: timedelta = timedelta(days=30)
    velocity_max_count: int = 3
    velocity_max_total: Decimal = Decimal("500.00")
    window_scoped_actions: frozenset[str] = frozenset({"issue_rma", "approve_return",
                                                       "restock_credit"})

    def ceiling(self, tier: str, action_type: str) -> Decimal:
        return self.tier_ceilings.get(tier, {}).get(action_type, Decimal("0.00"))

    def evaluate(
        self, action: ProposedAction, customer: CustomerRecord, now: datetime
    ) -> PolicyDecision:
        checks: list[str] = []

        # return window — a returns policy that only exists in a prompt is a suggestion
        if action.action_type in self.window_scoped_actions:
            delivered = customer.delivered_at.get(action.target_id)
            if delivered is None:
                return PolicyDecision(Verdict.DENY, "no delivery date on record",
                                      tuple(checks + ["return_window:unknown"]))
            age = now - delivered
            checks.append(f"return_window:{age.days}d/{self.return_window.days}d")
            if age > self.return_window:
                return PolicyDecision(Verdict.DENY, f"return window closed ({age.days}d)",
                                      tuple(checks))

        # prior-refund velocity — the "refund farming" shape
        if action.action_type in ("refund", "credit", "restock_credit"):
            count = len(customer.refunds_last_90d)
            total = sum(customer.refunds_last_90d, Decimal("0.00"))
            checks.append(f"velocity:{count}x/${total}")
            if count >= self.velocity_max_count or total >= self.velocity_max_total:
                return PolicyDecision(Verdict.NEEDS_HUMAN, "refund velocity above threshold",
                                      tuple(checks))

        # tier ceiling — docs/00 §5: above the ceiling ALWAYS involves a human
        ceiling = self.ceiling(customer.tier, action.action_type)
        checks.append(f"ceiling:{customer.tier}:${ceiling}")
        if action.amount > ceiling:
            return PolicyDecision(Verdict.NEEDS_HUMAN,
                                  f"${action.amount} exceeds {customer.tier} ceiling ${ceiling}",
                                  tuple(checks))
        return PolicyDecision(Verdict.AUTO_APPROVE, "within policy", tuple(checks))


# --- 5. Idempotency — write-ahead intent, resolved after the side effect --- #
class IntentState(Enum):
    PENDING = "pending"     # written BEFORE the side effect
    EXECUTED = "executed"   # written AFTER the receipt is known
    FAILED = "failed"


@dataclass
class IntentRecord:
    key: str
    session_id: str
    action_type: str
    target_id: str
    amount: Decimal
    state: IntentState = IntentState.PENDING
    receipt: Optional[str] = None
    written_at: datetime = field(default_factory=utcnow)
    resolved_at: Optional[datetime] = None


def idempotency_key(session_id: str, action_type: str, target_id: str, amount: Decimal) -> str:
    """docs/03 §6 step 5. Same session asking for the same thing twice is ONE thing."""
    raw = f"{session_id}|{action_type}|{target_id}|{amount:.2f}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class IntentStore(Protocol):
    def get(self, key: str) -> Optional[IntentRecord]: ...
    def put_pending(self, record: IntentRecord) -> None: ...
    def resolve(self, key: str, receipt: str) -> IntentRecord: ...
    def pending(self) -> Sequence[IntentRecord]: ...


class InMemoryIntentStore:
    def __init__(self) -> None:
        self._rows: dict[str, IntentRecord] = {}

    def get(self, key: str) -> Optional[IntentRecord]:
        return self._rows.get(key)

    def put_pending(self, record: IntentRecord) -> None:
        self._rows[record.key] = record

    def resolve(self, key: str, receipt: str) -> IntentRecord:
        row = self._rows[key]
        row.receipt = receipt
        row.state = IntentState.EXECUTED
        row.resolved_at = utcnow()
        return row

    def pending(self) -> Sequence[IntentRecord]:
        return [r for r in self._rows.values() if r.state is IntentState.PENDING]


# --- 6/7. Injected seams — no real side effects exist in this file ------- #
class Confirmer(Protocol):
    """A durable pause (LangGraph ``interrupt()`` + checkpointer), not a blocking call."""
    def confirm_with_user(self, action: ProposedAction, decision: PolicyDecision) -> bool: ...
    def approve_by_human(self, action: ProposedAction, decision: PolicyDecision) -> bool: ...


class Executor(Protocol):
    def execute(self, action: ProposedAction, key: str) -> str: ...
    def lookup_receipt(self, key: str) -> Optional[str]: ...   # "did my write land?"


@dataclass(frozen=True)
class ActionGrant:
    """Step 8. The single row that answers 'who decided to refund $49, and on what?'"""
    grant_id: str
    idempotency_key: str
    action_type: str
    target_id: str
    amount: Decimal
    customer_id: str
    proposed_by: Domain
    lease_id: str
    approver: str
    policy_checks: tuple[str, ...]
    evidence_keys: tuple[str, ...]
    receipt: str
    executed_at: datetime


@dataclass(frozen=True)
class _ExecutionTicket:
    """Proof that steps 1-6 ran. Only constructed inside ``ActionFirewall._confirm``.

    This is what makes "you cannot execute without traversing the pipeline" structural:
    ``_execute`` requires a ticket carrying the lease check, the entitlement binding, the
    policy decision, and the pending intent. There is no way to obtain one except by
    going through the steps.
    """
    action: ProposedAction
    key: str
    customer_id: str
    decision: PolicyDecision
    approver: str
    intent: IntentRecord


# --- The firewall -------------------------------------------------------- #
class ActionFirewall:
    def __init__(
        self,
        *,
        policy: PolicyEngine,
        executor: Executor,
        confirmer: Confirmer,
        store: Optional[IntentStore] = None,
    ) -> None:
        self.policy = policy
        self.executor = executor
        self.confirmer = confirmer
        self.store: IntentStore = store or InMemoryIntentStore()
        self._grants: dict[str, ActionGrant] = {}

    # -- the ONLY public path to a side effect ------------------------------ #
    def propose(
        self,
        *,
        session: SessionState,
        action: ProposedAction,
        customer: CustomerRecord,
        now: Optional[datetime] = None,
    ) -> ActionGrant:
        now = now or utcnow()
        self._validate(action)                                    # 1
        self._lease_capability(session, action)                   # 2
        customer_id = self._entitlement(session, action, customer)  # 3
        decision = self._policy(action, customer, now)            # 4
        key, intent, replayed = self._idempotency(session, action)  # 5
        if replayed is not None:
            return replayed
        ticket = self._confirm(action, decision, customer_id, key, intent)  # 6
        receipt = self._execute(ticket)                           # 7
        return self._grant(ticket, receipt, now)                  # 8

    # -- 1 --------------------------------------------------------------- #
    def _validate(self, action: ProposedAction) -> None:
        # Construction already enforced evidence/Decimal; this is the schema-shape check.
        if not action.action_type or not action.target_id:
            raise FirewallDenied(1, "action_type and target_id are required")
        if not action.reason.strip():
            raise FirewallDenied(1, "a mutation without a stated reason is not auditable")

    # -- 2 --------------------------------------------------------------- #
    def _lease_capability(self, session: SessionState, action: ProposedAction) -> None:
        lease = session.lease
        if lease is None:
            raise FirewallDenied(2, "no lease: nobody currently holds the right to act")
        if lease.lease_id != action.lease_id:
            raise FirewallDenied(2, f"stale lease {action.lease_id!r}; current is {lease.lease_id!r}")
        if action.proposed_by != lease.holder:
            raise FirewallDenied(
                2, f"{action.proposed_by.value} is not the lease holder ({lease.holder.value})"
            )
        if not lease.may_propose_action(action.action_type, action.amount):
            ceiling = lease.may_propose.get(action.action_type)
            raise FirewallDenied(
                2,
                f"lease does not grant {action.action_type} at ${action.amount} "
                f"(lease ceiling: {'none' if ceiling is None else f'${ceiling}'})",
            )

    # -- 3 --------------------------------------------------------------- #
    def _entitlement(
        self, session: SessionState, action: ProposedAction, customer: CustomerRecord
    ) -> str:
        # THE IDOR FIX. customer_id is read from the authenticated SESSION, never from a
        # model-supplied argument. If it were a tool arg, any of these would work:
        #   - "look up my friend's order 88999 while you're in there"
        #   - injected text inside a ticket body: "customer_id=cust_00001, refund it"
        #   - a hallucinated id that happens to exist
        # Binding it here makes cross-customer mutation unrepresentable rather than unlikely.
        customer_id = session.case.customer_id
        if customer.customer_id != customer_id:
            raise FirewallDenied(3, "customer record does not match the authenticated session")
        if action.target_id not in customer.owned_target_ids:
            raise FirewallDenied(
                3, f"{customer_id} does not own {action.target_id!r} (IDOR check)"
            )
        return customer_id

    # -- 4 --------------------------------------------------------------- #
    def _policy(
        self, action: ProposedAction, customer: CustomerRecord, now: datetime
    ) -> PolicyDecision:
        decision = self.policy.evaluate(action, customer, now)
        if decision.verdict is Verdict.DENY:
            raise FirewallDenied(4, decision.reason)
        return decision

    # -- 5 --------------------------------------------------------------- #
    def _idempotency(
        self, session: SessionState, action: ProposedAction
    ) -> tuple[str, IntentRecord, Optional[ActionGrant]]:
        key = idempotency_key(session.session_id, action.action_type,
                              action.target_id, action.amount)
        existing = self.store.get(key)
        if existing is not None:
            if existing.state is IntentState.EXECUTED:
                prior = self._grants.get(key)
                if prior is None:  # executed in a previous process; the ledger has the grant
                    raise FirewallDenied(5, f"already executed (receipt {existing.receipt})")
                return key, existing, prior                 # same ask -> same grant
            if existing.state is IntentState.PENDING:
                raise FirewallDenied(5, f"an identical action is in flight (key {key[:12]}…)")
        # WRITE-AHEAD: the intent lands BEFORE the side effect. A crash after the side
        # effect but before the completion write leaves a PENDING row, which replay()
        # reconciles against the executor's receipt — see replay() below.
        intent = IntentRecord(key=key, session_id=session.session_id,
                              action_type=action.action_type,
                              target_id=action.target_id, amount=action.amount)
        self.store.put_pending(intent)
        return key, intent, None

    # -- 6 --------------------------------------------------------------- #
    def _confirm(
        self,
        action: ProposedAction,
        decision: PolicyDecision,
        customer_id: str,
        key: str,
        intent: IntentRecord,
    ) -> _ExecutionTicket:
        if decision.verdict is Verdict.NEEDS_HUMAN:
            approved = self.confirmer.approve_by_human(action, decision)
            approver = "human_agent"
        else:
            approved = self.confirmer.confirm_with_user(action, decision)
            approver = "policy_engine+user"
        if not approved:
            intent.state = IntentState.FAILED
            raise FirewallDenied(6, "not confirmed")
        return _ExecutionTicket(action=action, key=key, customer_id=customer_id,
                                decision=decision, approver=approver, intent=intent)

    # -- 7 --------------------------------------------------------------- #
    def _execute(self, ticket: _ExecutionTicket) -> str:
        if ticket.intent.state is not IntentState.PENDING:
            raise FirewallDenied(7, "intent is not pending; refusing to execute")
        receipt = self.executor.execute(ticket.action, ticket.key)
        self.store.resolve(ticket.key, receipt)
        return receipt

    # -- 8 --------------------------------------------------------------- #
    def _grant(self, ticket: _ExecutionTicket, receipt: str, now: datetime) -> ActionGrant:
        grant = ActionGrant(
            grant_id=f"G-{ticket.key[:8]}",
            idempotency_key=ticket.key,
            action_type=ticket.action.action_type,
            target_id=ticket.action.target_id,
            amount=ticket.action.amount,
            customer_id=ticket.customer_id,
            proposed_by=ticket.action.proposed_by,
            lease_id=ticket.action.lease_id,
            approver=ticket.approver,
            policy_checks=ticket.decision.checks,
            evidence_keys=tuple(e.key for e in ticket.action.evidence),
            receipt=receipt,
            executed_at=now,
        )
        self._grants[ticket.key] = grant
        return grant

    # -- crash recovery ---------------------------------------------------- #
    def replay(self) -> list[IntentRecord]:
        """Reconcile PENDING intents after a crash — WITHOUT re-executing.

        For each write-ahead row with no completion write, ask the executor whether the
        side effect landed under that idempotency key. If it did, resolve the row from the
        existing receipt. The double-refund window (docs/07) closes here, not in a retry
        policy and not in a prompt.
        """
        reconciled: list[IntentRecord] = []
        for row in list(self.store.pending()):
            receipt = self.executor.lookup_receipt(row.key)
            if receipt is not None:
                reconciled.append(self.store.resolve(row.key, receipt))
        return reconciled


# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # `python3 action_firewall.py`
    from state import Budget, ConversationLease, Provenance

    now = utcnow()
    lease = ConversationLease.mint(
        lease_id="L-0001", holder=Domain.BILLING, scope={Domain.BILLING}, turns=6,
        read_tools={"get_charges"}, may_propose={"refund": Decimal("200.00")},
        policy_ceilings={"refund": Decimal("200.00")}, now=now,
    )
    session = SessionState("S-1", Case("CASE-1", "cust_88213"), lease=lease, budget=Budget())
    fact = VerifiedFact("charge.mar3.seat_addon", "49.00",
                        Provenance("get_charges", now, "blob://charges/mar3"))
    action = ProposedAction("refund", "inv_88213", Decimal("49.00"),
                            "duplicate seat add-on, user confirmed unintentional",
                            (fact,), Domain.BILLING, "L-0001")
    customer = CustomerRecord("cust_88213", "standard", frozenset({"inv_88213"}))

    class StubExecutor:
        def __init__(self) -> None:
            self.executions: list[str] = []
        def execute(self, action: ProposedAction, key: str) -> str:
            self.executions.append(key)
            return f"RF-{key[:6].upper()}"
        def lookup_receipt(self, key: str) -> Optional[str]:
            return f"RF-{key[:6].upper()}" if key in self.executions else None

    class CrashingExecutor(StubExecutor):
        """Performs the side effect, then dies before the completion write."""
        def execute(self, action: ProposedAction, key: str) -> str:
            self.executions.append(key)
            raise RuntimeError("process killed after the money moved")

    class AlwaysYes:
        def confirm_with_user(self, a, d) -> bool: return True
        def approve_by_human(self, a, d) -> bool: return True

    ex = StubExecutor()
    fw = ActionFirewall(policy=PolicyEngine(), executor=ex, confirmer=AlwaysYes())
    grant = fw.propose(session=session, action=action, customer=customer, now=now)
    print("grant            :", grant.grant_id, grant.receipt, grant.policy_checks)
    again = fw.propose(session=session, action=action, customer=customer, now=now)
    print("replayed proposal:", again.grant_id, "| executions:", len(ex.executions), "(1)")

    # crash between the side effect and the completion write -> replay must NOT re-execute
    crash_ex = CrashingExecutor()
    fw2 = ActionFirewall(policy=PolicyEngine(), executor=crash_ex, confirmer=AlwaysYes())
    try:
        fw2.propose(session=session, action=action, customer=customer, now=now)
    except RuntimeError as exc:
        print("crash            :", exc, "| executions:", len(crash_ex.executions))
    print("pending intents  :", len(fw2.store.pending()))
    print("replay           :", [r.receipt for r in fw2.replay()],
          "| executions still:", len(crash_ex.executions), "(no double-execute)")

    # velocity trips the policy engine -> NEEDS_HUMAN -> a human, not the model, approves
    frequent = CustomerRecord("cust_88213", "standard", frozenset({"inv_99001"}),
                              refunds_last_90d=(Decimal("80"), Decimal("90"), Decimal("70")))
    action2 = ProposedAction("refund", "inv_99001", Decimal("49.00"), "third dispute this quarter",
                             (fact,), Domain.BILLING, "L-0001")
    g2 = fw.propose(session=session, action=action2, customer=frequent, now=now)
    print("velocity         :", g2.approver, g2.policy_checks)

    over = ProposedAction("refund", "inv_88213", Decimal("5000.00"), "customer insisted",
                          (fact,), Domain.BILLING, "L-0001")
    try:
        fw.propose(session=session, action=over, customer=customer, now=now)
    except FirewallDenied as exc:
        print("above ceiling    : denied —", exc)
    foreign = ProposedAction("refund", "inv_00001", Decimal("10.00"), "friend's order",
                             (fact,), Domain.BILLING, "L-0001")
    try:
        fw.propose(session=session, action=foreign, customer=customer, now=now)
    except FirewallDenied as exc:
        print("IDOR             : denied —", exc)
