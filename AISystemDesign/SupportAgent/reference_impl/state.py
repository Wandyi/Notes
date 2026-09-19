"""
Helix Support — session state: modes, facts, the lease, budgets, channels.

Implements docs/03-recommended-architecture.md §3 (mode machine), §4 (the lease) and
§5 (turn ledger); docs/04-agent-runtime.md (budgets, degradation); docs/05-state-and-memory.md
(channels + reducers); docs/08-safety-guardrails.md (verified fact vs. customer claim).

Four invariants are STRUCTURAL here — they raise, they are not linted:

  1. A ``VerifiedFact`` cannot be produced from a ``CustomerClaim``. There is no
     constructor, classmethod, or coercion that crosses the boundary, and a Provenance
     naming an untrusted channel is rejected. Customer/ticket text is attacker
     controllable (docs/08); a fact is something a tool *we* called returned.
  2. A ``ConversationLease`` is minted in exactly one place (``mint()``, called only by
     ``handoff.Arbiter``), always has a non-empty scope, and can never grant an action
     ceiling above the policy ceiling it was minted against (docs/03 §4).
  3. A lease with ``turns_remaining <= 0`` is invalid — the turn budget is enforced by
     the check, not by a specialist's good intentions.
  4. Two nodes writing the same single-writer channel in one step is an *error*, not a
     race (docs/05). This mirrors LangGraph's ``InvalidUpdateError`` on LastValue channels.

Nothing here calls a model, a tool, or a datastore.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

UTC = timezone.utc
ARBITER = "arbiter"  # the single writer of the control-plane channels


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- Enums --------------------------------------------------------------- #
class Domain(Enum):
    BILLING = "billing"
    ORDERS = "orders"
    TECHNICAL = "technical"
    ACCOUNT = "account"
    RETURNS = "returns"


class Mode(Enum):
    """The mode machine of docs/03 §3. The hot path is LEASED -> LEASED."""
    INTAKE = "INTAKE"
    TRIAGE = "TRIAGE"
    LEASED = "LEASED"
    FANOUT = "FANOUT"
    ARBITRATE = "ARBITRATE"
    ACTING = "ACTING"
    SUSPENDED = "SUSPENDED"
    DEFLECT = "DEFLECT"
    ESCALATE = "ESCALATE"
    RESOLVED = "RESOLVED"


class RevocationReason(Enum):
    """The N release reasons that replace the swarm's N x (N-1) transfer mesh.

    Defined here (not in handoff.py) so ``ConversationLease.check`` can return one
    without a circular import; ``handoff`` re-exports it as part of the handoff contract.
    """
    OUT_OF_SCOPE = "out_of_scope"
    RESOLVED = "resolved"
    NEEDS_HUMAN = "needs_human"
    ADDITIONAL_DOMAIN = "additional_domain"
    TURN_BUDGET_EXHAUSTED = "turn_budget_exhausted"
    NO_PROGRESS = "no_progress"
    POLICY_ESCALATION = "policy_escalation"
    SENTIMENT = "sentiment"
    LEASE_EXPIRED = "lease_expired"
    USER_REQUESTED_HUMAN = "user_requested_human"


DEFAULT_REVOKE_ON: tuple[RevocationReason, ...] = (
    RevocationReason.OUT_OF_SCOPE,
    RevocationReason.ADDITIONAL_DOMAIN,
    RevocationReason.NO_PROGRESS,
    RevocationReason.SENTIMENT,
    RevocationReason.POLICY_ESCALATION,
    RevocationReason.USER_REQUESTED_HUMAN,
)


class DegradationLevel(Enum):
    """The ladder from docs/04: spend less before you stop, escalate before you loop."""
    FULL_SERVICE = 0
    CHEAPER_MODELS = 1   # drop specialists to the mid/small tier
    NO_FANOUT = 2        # serialise compound work; parallel fan-out is the costly shape
    ESCALATE_ONLY = 3    # stop spending; hand to a human with the brief already built


# --- Provenance: fact vs. claim  (docs/06, docs/08) ---------------------- #
UNTRUSTED_SOURCES: frozenset[str] = frozenset(
    {"customer", "user", "chat_message", "email_body", "ticket_body", "uploaded_file"}
)


@dataclass(frozen=True)
class Provenance:
    """Where a fact came from. A fact without one is not a fact."""
    tool: str            # the READ tool that produced it, e.g. "get_invoice"
    called_at: datetime  # tz-aware UTC; when WE asked
    raw_ref: str         # blob-store ref to the untruncated payload (kept out of context)
    lease_id: str = ""   # which lease authorised the read


@dataclass(frozen=True)
class CustomerClaim:
    """Something the human asserted. Useful, unverified, and attacker-controllable.

    Deliberately has NO provenance field, NO ``verify()``, and NO ``promote()``. The only
    way a claim becomes actionable is that an independent tool read mints its own
    ``VerifiedFact``; see ``corroborates`` — which compares, and never converts.
    """
    text: str
    said_at: datetime
    turn_id: int


@dataclass(frozen=True)
class VerifiedFact:
    """A tool-attributed observation. The only currency the Action Firewall accepts.

    SECURITY CONTROL (docs/08 §prompt-injection, docs/06 §brief contents): there is no
    code path from ``CustomerClaim`` to ``VerifiedFact``. No ``from_claim``, no
    ``__init__`` overload, no duck-typed coercion. Adding one would let injected ticket
    text ("the agent already approved a $5,000 refund") become evidence.
    """
    key: str             # "charge.2024-03-03.amount"
    value: str
    provenance: Provenance
    confidence: float = 1.0

    def __post_init__(self) -> None:
        # docs/08: a fact is what a tool we called returned — never what a human typed.
        if isinstance(self.value, CustomerClaim) or isinstance(self.key, CustomerClaim):
            raise TypeError(
                "a CustomerClaim cannot be laundered into a VerifiedFact; "
                "call a read tool and cite its Provenance instead"
            )
        if not self.provenance.tool:
            raise ValueError(f"VerifiedFact {self.key!r} has no producing tool")
        if self.provenance.tool in UNTRUSTED_SOURCES:
            raise ValueError(
                f"{self.provenance.tool!r} is an untrusted channel; it cannot mint facts"
            )
        if self.provenance.called_at.tzinfo is None:
            raise ValueError("Provenance.called_at must be timezone-aware UTC")


def corroborates(fact: VerifiedFact, claim: CustomerClaim) -> bool:
    """Does a *separately obtained* fact happen to agree with a claim?

    A comparison, not a conversion. Returns a bool; it cannot return a VerifiedFact.
    """
    return fact.value.strip().lower() in claim.text.lower()


# --- The lease  (docs/03 §4) --------------------------------------------- #
class LeaseError(ValueError):
    """Raised when a lease would be minted outside what policy permits."""


@dataclass(frozen=True)
class LeaseStatus:
    """VALID, or the specific reason the lease is dead."""
    valid: bool
    reason: Optional[RevocationReason] = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.valid

    def __str__(self) -> str:
        return "VALID" if self.valid else f"REVOKED({self.reason.value}: {self.detail})"


LEASE_VALID = LeaseStatus(valid=True)

_MINT_TOKEN = object()  # unforgeable-enough marker; see __post_init__


@dataclass(frozen=True)
class ConversationLease:
    """Bounded, revocable permission to *speak to the user* — docs/03 §4.

    Immutable: ``consume_turn`` returns a new lease. ``may_propose`` is wrapped in a
    read-only mapping so a specialist cannot widen its own ceiling at runtime.
    """
    lease_id: str
    holder: Domain
    scope: frozenset[Domain]              # accepts any iterable, e.g. {Domain.BILLING}
    turns_remaining: int
    read_tools: frozenset[str]            # least-privilege reads
    may_propose: Mapping[str, Decimal]    # action type -> max amount (Decimal, never float)
    granted_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoke_on: tuple[RevocationReason, ...] = DEFAULT_REVOKE_ON
    _mint_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # docs/03 §4: "Leases are granted by the Arbiter" — one mint point, so the policy
        # ceiling check in mint() cannot be routed around by calling the ctor directly.
        if self._mint_token is not _MINT_TOKEN:
            raise LeaseError(
                "leases may only be created via ConversationLease.mint(), which is called "
                "only by handoff.Arbiter.mint_lease (docs/03 §4)"
            )
        object.__setattr__(self, "scope", frozenset(self.scope))
        object.__setattr__(self, "read_tools", frozenset(self.read_tools))
        object.__setattr__(self, "may_propose", MappingProxyType(dict(self.may_propose)))
        # docs/03 §4: an empty scope grants nothing but reads like it grants everything.
        if not self.scope:
            raise LeaseError(f"lease {self.lease_id!r} has an empty scope")
        if self.holder not in self.scope:
            raise LeaseError(f"holder {self.holder.value!r} is outside its own scope")
        if self.turns_remaining < 0:
            raise LeaseError("turns_remaining cannot be negative")
        for amount in self.may_propose.values():
            if not isinstance(amount, Decimal):
                raise TypeError("may_propose ceilings must be Decimal, never float")

    # -- the one constructor ------------------------------------------------ #
    @classmethod
    def mint(
        cls,
        *,
        lease_id: str,
        holder: Domain,
        scope: Iterable[Domain],
        turns: int,
        read_tools: Iterable[str],
        may_propose: Mapping[str, Decimal],
        policy_ceilings: Mapping[str, Decimal],
        now: datetime,
        idle_ttl: timedelta = timedelta(minutes=30),
        absolute_ttl: timedelta = timedelta(hours=24),
        revoke_on: tuple[RevocationReason, ...] = DEFAULT_REVOKE_ON,
    ) -> "ConversationLease":
        if turns < 1:
            raise LeaseError("minting a lease with zero turns is a bug, not a policy")
        # docs/03 §4 + docs/07: a lease is a *narrowing* of policy, never a widening.
        # "Returns proposing a $5,000 refund" is the failure this line prevents.
        for action, ceiling in may_propose.items():
            allowed = policy_ceilings.get(action)
            if allowed is None:
                raise LeaseError(f"action {action!r} has no policy ceiling; cannot be leased")
            if ceiling > allowed:
                raise LeaseError(
                    f"lease would grant {action}<={ceiling} above policy ceiling {allowed}"
                )
        return cls(
            lease_id=lease_id,
            holder=holder,
            scope=frozenset(scope),
            turns_remaining=turns,
            read_tools=frozenset(read_tools),
            may_propose=dict(may_propose),
            granted_at=now,
            idle_expires_at=now + idle_ttl,
            absolute_expires_at=now + absolute_ttl,
            revoke_on=revoke_on,
            _mint_token=_MINT_TOKEN,
        )

    # -- the deterministic hot-path check (zero model calls) ---------------- #
    def check(
        self,
        now: datetime,
        *,
        requested_domain: Optional[Domain] = None,
        requested_tool: Optional[str] = None,
        signals: Iterable[RevocationReason] = (),
    ) -> LeaseStatus:
        if now >= self.absolute_expires_at:
            return LeaseStatus(False, RevocationReason.LEASE_EXPIRED, "absolute TTL")
        if now >= self.idle_expires_at:
            return LeaseStatus(False, RevocationReason.LEASE_EXPIRED, "idle TTL")
        if self.turns_remaining <= 0:
            return LeaseStatus(False, RevocationReason.TURN_BUDGET_EXHAUSTED, "0 turns left")
        if requested_domain is not None and requested_domain not in self.scope:
            return LeaseStatus(False, RevocationReason.OUT_OF_SCOPE, requested_domain.value)
        if requested_tool is not None and requested_tool not in self.read_tools:
            return LeaseStatus(False, RevocationReason.OUT_OF_SCOPE, f"tool {requested_tool}")
        for signal in signals:
            if signal in self.revoke_on:
                return LeaseStatus(False, signal, "declared revocation condition")
        return LEASE_VALID

    def may_propose_action(self, action_type: str, amount: Decimal) -> bool:
        ceiling = self.may_propose.get(action_type)
        return ceiling is not None and amount <= ceiling

    def consume_turn(self) -> "ConversationLease":
        if self.turns_remaining <= 0:
            raise LeaseError("cannot consume a turn from an exhausted lease")
        return replace(self, turns_remaining=self.turns_remaining - 1)


# --- Budgets  (docs/04, docs/10) ----------------------------------------- #
@dataclass(frozen=True)
class BudgetStatus:
    level: DegradationLevel
    utilisation: float
    breached: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.level is not DegradationLevel.ESCALATE_ONLY


@dataclass
class Budget:
    """Bounded everything (README stance 5). Exceeding any cap forces arbitration."""
    max_turns: int = 24
    max_hops: int = 3          # kept in sync with handoff.MAX_HOPS
    max_tool_calls: int = 40
    max_tokens: int = 250_000
    max_usd: Decimal = Decimal("0.50")
    turns: int = 0
    hops: int = 0
    tool_calls: int = 0
    tokens: int = 0
    usd: Decimal = Decimal("0.00")

    def spend(
        self,
        *,
        turns: int = 0,
        hops: int = 0,
        tool_calls: int = 0,
        tokens: int = 0,
        usd: Decimal = Decimal("0.00"),
    ) -> BudgetStatus:
        if isinstance(usd, float):
            raise TypeError("money is Decimal, never float")
        self.turns += turns
        self.hops += hops
        self.tool_calls += tool_calls
        self.tokens += tokens
        self.usd += usd
        return self.check()

    def check(self) -> BudgetStatus:
        ratios = {
            "turns": self.turns / self.max_turns,
            "hops": self.hops / self.max_hops,
            "tool_calls": self.tool_calls / self.max_tool_calls,
            "tokens": self.tokens / self.max_tokens,
            "usd": float(self.usd / self.max_usd),
        }
        worst = max(ratios.values())
        breached = tuple(sorted(k for k, v in ratios.items() if v >= 1.0))
        if worst >= 1.0:
            level = DegradationLevel.ESCALATE_ONLY
        elif worst >= 0.90:
            level = DegradationLevel.NO_FANOUT
        elif worst >= 0.75:
            level = DegradationLevel.CHEAPER_MODELS
        else:
            level = DegradationLevel.FULL_SERVICE
        return BudgetStatus(level=level, utilisation=worst, breached=breached)


# --- The turn ledger  (docs/03 §5) — auditability without inference ------ #
@dataclass(frozen=True)
class TurnRecord:
    turn_id: int
    at: datetime
    mode: Mode
    speaker: str                       # "billing", "arbiter", "triage", "firewall"
    lease_id: str = ""
    tools_called: tuple[str, ...] = ()
    actions_proposed: tuple[str, ...] = ()
    policy_checks: tuple[str, ...] = ()
    detectors: tuple[str, ...] = ()
    model_calls: int = 0
    cost_usd: Decimal = Decimal("0.00")


# --- Channels + reducers  (docs/05) — the concurrency invariant, executable --- #
class InvalidUpdateError(RuntimeError):
    """Mirrors ``langgraph.errors.InvalidUpdateError`` (docs/05).

    Two parallel branches writing one single-writer channel is a *design* error. Silently
    picking a winner is how a fan-out ends with two leases and one of them unaccounted for.
    """


@dataclass(frozen=True)
class ChannelUpdate:
    channel: str
    value: Any
    writer: str   # node name: "billing", "orders", "arbiter", ...


Reducer = Callable[[Any, Sequence[ChannelUpdate], str], Any]


def append_only(current: Any, updates: Sequence[ChannelUpdate], channel: str) -> Any:
    """Many writers, no deletes. This is why fan-out can write facts concurrently."""
    out = list(current or [])
    for u in updates:
        out.extend(u.value if isinstance(u.value, (list, tuple)) else [u.value])
    return out


def last_write_wins(current: Any, updates: Sequence[ChannelUpdate], channel: str) -> Any:
    """Exactly one update per step — LangGraph's LastValue semantics."""
    if len(updates) > 1:
        raise InvalidUpdateError(
            f"channel {channel!r} received {len(updates)} updates in one step from "
            f"{[u.writer for u in updates]}; it accepts one value per step"
        )
    return updates[0].value


def arbiter_only(current: Any, updates: Sequence[ChannelUpdate], channel: str) -> Any:
    """Single value AND single privileged writer — the control-plane channels."""
    if len(updates) > 1:
        raise InvalidUpdateError(
            f"parallel write to arbiter-only channel {channel!r} from "
            f"{[u.writer for u in updates]} (docs/05: control-plane channels have one writer)"
        )
    if updates[0].writer != ARBITER:
        raise InvalidUpdateError(
            f"{updates[0].writer!r} may not write control-plane channel {channel!r}; "
            f"only {ARBITER!r} may"
        )
    return updates[0].value


CHANNEL_REDUCERS: Mapping[str, Reducer] = MappingProxyType(
    {
        "messages": append_only,
        "facts": append_only,
        "claims": append_only,
        "actions": append_only,
        "ledger": append_only,
        "lease": arbiter_only,
        "mode": arbiter_only,
        "budget": last_write_wins,
        "case": last_write_wins,
    }
)


@dataclass(frozen=True)
class Case:
    case_id: str
    customer_id: str          # the entitlement binding; see action_firewall.py step 3
    tier: str = "standard"
    opened_at: datetime = field(default_factory=utcnow)


@dataclass
class SessionState:
    session_id: str
    case: Case
    mode: Mode = Mode.INTAKE
    lease: Optional[ConversationLease] = None
    budget: Budget = field(default_factory=Budget)
    messages: list[str] = field(default_factory=list)
    facts: list[VerifiedFact] = field(default_factory=list)          # append-only
    claims: list[CustomerClaim] = field(default_factory=list)        # append-only
    actions: list[str] = field(default_factory=list)                 # ActionGrant ids
    ledger: list[TurnRecord] = field(default_factory=list)           # append-only


def merge(state: SessionState, updates: Sequence[ChannelUpdate]) -> SessionState:
    """Apply one superstep of channel updates through the declared reducers."""
    grouped: dict[str, list[ChannelUpdate]] = {}
    for u in updates:
        if u.channel not in CHANNEL_REDUCERS:
            raise InvalidUpdateError(f"unknown channel {u.channel!r}")
        grouped.setdefault(u.channel, []).append(u)
    new: dict[str, Any] = {}
    for channel, group in grouped.items():
        reducer = CHANNEL_REDUCERS[channel]
        new[channel] = reducer(getattr(state, channel), group, channel)
    return replace(state, **new)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # smoke test: `python3 state.py`
    now = utcnow()
    lease = ConversationLease.mint(
        lease_id="L-1", holder=Domain.BILLING, scope={Domain.BILLING}, turns=2,
        read_tools={"get_invoice"}, may_propose={"refund": Decimal("200.00")},
        policy_ceilings={"refund": Decimal("200.00")}, now=now,
    )
    print("lease check      :", lease.check(now))
    print("out-of-scope     :", lease.check(now, requested_domain=Domain.ORDERS))
    print("exhausted        :", lease.consume_turn().consume_turn().check(now))
    fact = VerifiedFact("charge.amount", "49.00", Provenance("get_charges", now, "blob://1"))
    claim = CustomerClaim("I was charged 49.00 twice", now, turn_id=1)
    print("corroborates     :", corroborates(fact, claim), "(a bool — never a fact)")
    s = merge(SessionState("S-1", Case("C-1", "cust_88213")),
              [ChannelUpdate("facts", fact, "billing"),
               ChannelUpdate("claims", claim, "intake")])
    print("channels merged  :", len(s.facts), "fact(s),", len(s.claims), "claim(s)")
    try:
        merge(s, [ChannelUpdate("lease", lease, "billing"),
                  ChannelUpdate("lease", lease, "orders")])
    except InvalidUpdateError as exc:
        print("parallel lease   : rejected —", exc)
    print("budget ladder    :", Budget(max_turns=4, turns=4).check().level.name)
