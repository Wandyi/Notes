"""
Incident Commander — core contracts (reference implementation).

These are the typed objects the whole system agrees on; the incident *blackboard*
(see docs/04-memory-context.md) is a graph of these. They are intentionally
dependency-free dataclasses so the design reads as executable documentation.

Two invariants are encoded structurally:
  1. Every Evidence is timestamped + attributed to a producing agent.
  2. Every Hypothesis links >=1 Evidence  -> "evidence-based" enforced at construction.
     A Hypothesis with no evidence raises at __post_init__, so an uncited claim
     cannot even be represented (see docs/06-safety-guardrails.md, docs/13-data-model.md).

Nothing here talks to a real cluster / LLM / datastore; production swaps the
behavior behind these types without changing the control-plane, orchestration,
or safety logic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Severity(Enum):
    SEV1 = 1  # full outage / revenue critical
    SEV2 = 2  # major degradation (the worked example)
    SEV3 = 3  # minor / partial
    SEV4 = 4  # noise / flapping


class State(Enum):
    DETECT = "DETECT"
    TRIAGE = "TRIAGE"
    PLAN = "PLAN"
    INVESTIGATE = "INVESTIGATE"
    CORRELATE = "CORRELATE"
    HYPOTHESIZE = "HYPOTHESIZE"
    DEBATE = "DEBATE"
    SCORE = "SCORE"
    APPROVE = "APPROVE"
    REMEDIATE = "REMEDIATE"
    VERIFY = "VERIFY"
    RECOMMEND = "RECOMMEND"
    DOCUMENT = "DOCUMENT"
    CLOSE = "CLOSE"
    SUSPENDED = "SUSPENDED"  # budget / error / injection -> checkpoint + page


class EvidenceKind(Enum):
    METRIC = "metric"
    LOG_SIGNATURE = "log_signature"
    DEPLOY = "deploy"
    CONFIG_DIFF = "config_diff"
    EVENT = "event"
    HISTORICAL = "historical"   # recalled prior; must be re-confirmed, never present-tense fact
    CHATTER = "chatter"         # Slack/human signal; lowest reliability


class HypothesisStatus(Enum):
    CANDIDATE = "candidate"
    SURVIVING = "surviving"
    REJECTED = "rejected"


class DecisionVerb(Enum):
    APPROVE = "approve"
    RECOMMEND_ONLY = "recommend_only"
    REJECT = "reject"
    NEED_MORE = "need_more"
    AUTO_REMEDIATE = "auto_remediate"  # narrow allowlist only


class Reversibility(Enum):
    REVERSIBLE = "reversible"
    PARTIAL = "partial"
    IRREVERSIBLE = "irreversible"


class DataSafety(Enum):
    NO_DATA_IMPACT = "no_data_impact"
    DATA_AFFECTING = "data_affecting"  # never auto; senior approver


class ResourceScope(Enum):
    SERVICE = "service"
    NAMESPACE = "namespace"
    CLUSTER = "cluster"
    GLOBAL = "global"


class RunStatus(Enum):
    DRY_RUN = "dry_run"
    APPLIED = "applied"
    VERIFIED = "verified"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class LifecycleState(Enum):
    REGISTERED = "REGISTERED"
    EVALUATING = "EVALUATING"
    STAGED = "STAGED"
    PRODUCTION = "PRODUCTION"
    STALE = "STALE"
    RETIRED = "RETIRED"


# --------------------------------------------------------------------------- #
# Identity / isolation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Principal:
    """Isolation key. Every incident, credential, budget, and memory is scoped to one."""
    tenant: str
    team: str
    service: str = ""


@dataclass(frozen=True)
class Citation:
    """A reproducible pointer back to the source that produced an Evidence."""
    source_system: str          # "prometheus"
    query: str                  # the exact query issued
    link: str = ""              # deep link into the source UI
    payload_ref: str = ""       # blob-store ref to the full payload (kept out of context)


# --------------------------------------------------------------------------- #
# Evidence  (the atom)
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    id: str
    agent: str                  # producing investigation agent, e.g. "prometheus"
    kind: EvidenceKind
    summary: str                # bounded, human-readable
    citation: Citation
    observed_at: float          # WHEN THE OBSERVED THING HAPPENED (not query time)
    reliability: float = 0.5    # source-class weight: metric > log > chatter
    degraded: bool = False      # true if this stands in for a failed/timed-out agent
    created_at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# Hypothesis / confidence  (the reasoning core)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Prediction:
    """A falsifiable claim used by the debate skeptic (docs/10)."""
    text: str                   # "if true, Redis latency should be FLAT"
    should_hold: bool = True    # True = must be confirmed; False = must NOT be observed


@dataclass
class Confidence:
    value: float                       # [0, 1]
    independent_classes: int           # count of INDEPENDENT evidence classes (not lines)
    temporal_score: float              # cause-precedes-symptom tightness
    survived_falsification: bool
    contradictions: int
    coverage: float                    # fraction of key agents that responded
    rationale: str                     # the human-readable "0.88 because ..." sentence


@dataclass
class Hypothesis:
    id: str
    statement: str
    evidence: list[str]                        # EvidenceId[]  -- REQUIRED, >=1
    predictions: list[Prediction] = field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.CANDIDATE
    rejected_reason: Optional[str] = None      # shown to human + written into postmortem
    proposed_runbook: Optional[str] = None     # RunbookRef
    confidence: Optional[Confidence] = None

    def __post_init__(self) -> None:
        # The structural enforcement of "no uncited claims" (docs/06, docs/13).
        if not self.evidence:
            raise ValueError(
                f"Hypothesis {self.id!r} has no linked evidence; "
                "evidence-based reasoning is required by construction."
            )


# --------------------------------------------------------------------------- #
# Remediation contracts  (the ONLY mutation path -> docs/08, docs/11)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BlastRadius:
    scope: ResourceScope
    reversibility: Reversibility
    data_safety: DataSafety

    @property
    def auto_remediation_eligible(self) -> bool:
        """Only narrow + reversible + no-data-impact may ever be on the auto allowlist."""
        return (
            self.scope == ResourceScope.SERVICE
            and self.reversibility == Reversibility.REVERSIBLE
            and self.data_safety == DataSafety.NO_DATA_IMPACT
        )


@dataclass
class VerifySpec:
    slos: list[str]                     # which SLOs must recover ("error_rate","p99_latency")
    thresholds: dict[str, float]        # recovery bars
    stabilization_seconds: int          # must hold for the FULL window to count as recovered


@dataclass
class Runbook:
    name: str
    version: str
    owner: Principal                    # mandatory ownership (docs/08)
    params: dict[str, str]
    blast_radius: BlastRadius
    verify: VerifySpec
    has_rollback: bool                  # its own inverse must exist for auto-rollback
    lifecycle: LifecycleState = LifecycleState.REGISTERED


@dataclass(frozen=True)
class ResourceRef:
    kind: str                           # "helm_release", "deployment", ...
    id: str


@dataclass
class ExecutionGrant:
    """The signed capability that unlocks the Executor. Executor refuses anything outside it."""
    principal: Principal
    runbook_version: str
    resources: list[ResourceRef]        # scope; Executor refuses out-of-scope resources
    approver: str                       # human id, or "policy-engine" for allowlisted auto
    expiry: float
    signature: bytes = b""              # verified by the Executor (docs/08)

    def valid_at(self, now: float) -> bool:
        return now < self.expiry


@dataclass
class EffectSet:
    """Simulated result of a dry-run; compared to the runbook's declared blast radius."""
    changed: list[ResourceRef]
    data_impact: bool
    reversible: bool


@dataclass
class RunbookRun:
    id: str
    runbook: str                        # pinned RunbookRef
    grant: ExecutionGrant
    idempotency_key: str                # incident + runbook_version + attempt
    dry_run_effect: Optional[EffectSet] = None
    status: RunStatus = RunStatus.DRY_RUN
    steps_completed: int = 0


@dataclass
class HealthCheck:
    slo: str
    value: float
    threshold: float
    in_window: bool
    passed: bool
    at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# Decisions / audit
# --------------------------------------------------------------------------- #
@dataclass
class Decision:
    id: str
    about: str                          # HypothesisId
    verb: DecisionVerb
    actor: str                          # human approver, or "system" for allowlisted auto
    rendered_view_ref: str              # exactly what the human saw (audit)
    at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# Budgets  (docs/02, docs/09)
# --------------------------------------------------------------------------- #
@dataclass
class BudgetCounters:
    max_steps: int
    max_tool_calls: int
    max_wall_clock_s: float
    max_dollars: float
    steps: int = 0
    tool_calls: int = 0
    started_at: float = field(default_factory=time.time)
    dollars: float = 0.0

    def exceeded(self) -> Optional[str]:
        if self.steps >= self.max_steps:
            return "STEPS"
        if self.tool_calls >= self.max_tool_calls:
            return "TOOL_CALLS"
        if time.time() - self.started_at >= self.max_wall_clock_s:
            return "WALL_CLOCK"
        if self.dollars >= self.max_dollars:
            return "DOLLARS"
        return None


# --------------------------------------------------------------------------- #
# Timeline + Incident (the blackboard root)
# --------------------------------------------------------------------------- #
@dataclass
class TimelineEvent:
    at: float
    source: str
    summary: str
    evidence_id: Optional[str] = None


@dataclass
class Incident:
    id: str
    principal: Principal
    severity: Severity
    state: State = State.DETECT
    alert_summary: str = ""
    budget: Optional[BudgetCounters] = None
    evidence: list[Evidence] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    remediations: list[RunbookRun] = field(default_factory=list)
    verifications: list[HealthCheck] = field(default_factory=list)
    degraded_agents: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # --- convenience used by the state machine / evaluator -----------------
    def leading_hypothesis(self) -> Optional[Hypothesis]:
        surviving = [h for h in self.hypotheses if h.status == HypothesisStatus.SURVIVING]
        scored = [h for h in surviving if h.confidence is not None]
        if not scored:
            return None
        return max(scored, key=lambda h: h.confidence.value)  # type: ignore[union-attr]

    def build_timeline(self) -> list[TimelineEvent]:
        """Deterministic reconstruction (sort, not synthesis) -> reproducible postmortems."""
        events = [
            TimelineEvent(at=e.observed_at, source=e.agent, summary=e.summary, evidence_id=e.id)
            for e in self.evidence
        ]
        events.sort(key=lambda ev: (ev.at, ev.source, ev.evidence_id or ""))
        self.timeline = events
        return events
