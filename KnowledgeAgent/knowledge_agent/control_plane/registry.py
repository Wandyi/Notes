"""Agent registry — the control plane's system of record.

Governs agent identity, ownership, capabilities, and an evaluation-gated
lifecycle state machine. Every agent has a named owner (an unowned agent is a
compliance red flag) and a status that advances only through valid transitions.
Promotion to PRODUCTION is gated on an evaluation score. Staleness detection
flags agents that haven't been re-evaluated within the review window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from ..core import new_id, utcnow


class LifecycleState(str, Enum):
    REGISTERED = "registered"
    EVALUATING = "evaluating"
    STAGED = "staged"
    PRODUCTION = "production"
    RETIRED = "retired"


# Valid state-machine transitions (control-plane governance).
_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.REGISTERED: {LifecycleState.EVALUATING, LifecycleState.RETIRED},
    LifecycleState.EVALUATING: {LifecycleState.STAGED, LifecycleState.REGISTERED, LifecycleState.RETIRED},
    LifecycleState.STAGED: {LifecycleState.PRODUCTION, LifecycleState.EVALUATING, LifecycleState.RETIRED},
    LifecycleState.PRODUCTION: {LifecycleState.STAGED, LifecycleState.RETIRED},
    LifecycleState.RETIRED: set(),
}


class RegistryError(Exception):
    pass


@dataclass
class AgentRecord:
    id: str
    name: str
    owner: str
    capabilities: list[str]
    state: LifecycleState = LifecycleState.REGISTERED
    version: int = 1
    created_at: datetime = field(default_factory=utcnow)
    last_evaluated_at: datetime | None = None
    last_eval_score: float | None = None
    history: list[tuple[str, str]] = field(default_factory=list)  # (iso_ts, note)

    def _log(self, note: str) -> None:
        self.history.append((utcnow().isoformat(), note))


class AgentRegistry:
    def __init__(self, promotion_threshold: float = 0.7, staleness_days: float = 90.0) -> None:
        self.promotion_threshold = promotion_threshold
        self.staleness_days = staleness_days
        self._agents: dict[str, AgentRecord] = {}

    def register(self, name: str, owner: str, capabilities: list[str]) -> AgentRecord:
        if not owner:
            raise RegistryError("agent must have a named owner (unowned agents are prohibited)")
        rec = AgentRecord(id=new_id("agent"), name=name, owner=owner, capabilities=list(capabilities))
        rec._log(f"registered by owner {owner}")
        self._agents[rec.id] = rec
        return rec

    def get(self, agent_id: str) -> AgentRecord:
        if agent_id not in self._agents:
            raise RegistryError(f"unknown agent {agent_id}")
        return self._agents[agent_id]

    def transition(self, agent_id: str, target: LifecycleState) -> AgentRecord:
        rec = self.get(agent_id)
        if target not in _TRANSITIONS[rec.state]:
            raise RegistryError(f"illegal transition {rec.state.value} -> {target.value}")
        rec.state = target
        rec._log(f"transitioned to {target.value}")
        return rec

    def record_evaluation(self, agent_id: str, score: float) -> AgentRecord:
        rec = self.get(agent_id)
        rec.last_evaluated_at = utcnow()
        rec.last_eval_score = score
        rec._log(f"evaluated: score={score:.3f}")
        return rec

    def promote(self, agent_id: str) -> AgentRecord:
        """Evaluation-gated promotion STAGED -> PRODUCTION."""
        rec = self.get(agent_id)
        if rec.state is not LifecycleState.STAGED:
            raise RegistryError("only STAGED agents can be promoted to PRODUCTION")
        if rec.last_eval_score is None or rec.last_eval_score < self.promotion_threshold:
            raise RegistryError(
                f"promotion blocked: eval score {rec.last_eval_score} "
                f"< threshold {self.promotion_threshold}"
            )
        return self.transition(agent_id, LifecycleState.PRODUCTION)

    def is_stale(self, agent_id: str, now: datetime | None = None) -> bool:
        rec = self.get(agent_id)
        now = now or utcnow()
        if rec.last_evaluated_at is None:
            return True
        return (now - rec.last_evaluated_at) > timedelta(days=self.staleness_days)

    def stale_agents(self, now: datetime | None = None) -> list[AgentRecord]:
        return [
            r for r in self._agents.values()
            if r.state is not LifecycleState.RETIRED and self.is_stale(r.id, now)
        ]

    def all(self) -> list[AgentRecord]:
        return list(self._agents.values())
