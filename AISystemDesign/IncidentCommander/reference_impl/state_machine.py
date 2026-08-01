"""
Incident Commander supervisor -- the explicit state machine (docs/02).

This is the runtime skeleton: transitions are CODE, not LLM output. Each handler
owns exactly one concern and returns the next State. Every transition:
  * checks the budget first  (breach -> SUSPENDED, not a crash),
  * runs its handler,
  * checkpoints durably      (stateless workers -> resumable).

The dangerous transition (REMEDIATE) is idempotency-keyed so a crash+resume
cannot double-execute a runbook. Effects (agent reads, runbook writes) go through
gated services, never inline here.

Handlers below are intentionally thin stubs that show the control flow and the
guardrails; the reasoning/execution behavior is injected via the `deps` object
so production swaps real LLMs/executors without touching this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from contracts import (
    Confidence,
    DecisionVerb,
    Incident,
    State,
)


class BudgetExceeded(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# Injected dependencies (all the "real work" lives behind these seams)
# --------------------------------------------------------------------------- #
class Deps(Protocol):
    # data plane
    def triage(self, inc: Incident) -> None: ...
    def plan(self, inc: Incident) -> None: ...
    def investigate(self, inc: Incident) -> None: ...      # bounded parallel fan-out
    def correlate(self, inc: Incident) -> None: ...        # -> timeline, bounded reduce
    def hypothesize(self, inc: Incident) -> None: ...
    def debate(self, inc: Incident) -> None: ...           # falsification (docs/10)
    def score(self, inc: Incident) -> Confidence | None: ...
    def render_for_human(self, inc: Incident) -> str: ...  # returns rendered_view_ref
    def await_decision(self, inc: Incident) -> DecisionVerb: ...  # durable wait, no budget burn
    def remediate(self, inc: Incident) -> None: ...        # dry-run -> apply (idempotency-keyed)
    def verify(self, inc: Incident) -> bool: ...           # stabilization window; auto-rollback
    def document(self, inc: Incident) -> None: ...
    # control plane
    def checkpoint(self, inc: Incident) -> None: ...
    def audit(self, inc: Incident, event: str) -> None: ...
    def page_human(self, inc: Incident, reason: str) -> None: ...


@dataclass
class SupervisorConfig:
    confidence_threshold: float = 0.7        # gate: below -> more investigation / recommend-only
    max_investigation_rounds: int = 3
    max_remediation_attempts: int = 1        # then a human drives


# --------------------------------------------------------------------------- #
# The machine
# --------------------------------------------------------------------------- #
class Supervisor:
    def __init__(self, deps: Deps, cfg: SupervisorConfig | None = None) -> None:
        self.deps = deps
        self.cfg = cfg or SupervisorConfig()
        self._rounds = 0
        self._remediation_attempts = 0

    def run(self, inc: Incident) -> Incident:
        """Drive the incident to a terminal state. Guaranteed to terminate (bounded loops)."""
        terminal = {State.CLOSE, State.SUSPENDED}
        while inc.state not in terminal:
            try:
                self._check_budget(inc)
                inc.state = self._dispatch(inc)
            except BudgetExceeded as exc:
                self.deps.audit(inc, f"SUSPENDED(BUDGET:{exc.reason})")
                self.deps.page_human(inc, f"budget:{exc.reason} — partial cited investigation ready")
                inc.state = State.SUSPENDED
            self.deps.checkpoint(inc)   # durable snapshot after EVERY transition
        return inc

    # ---- transition table ------------------------------------------------- #
    def _dispatch(self, inc: Incident) -> State:
        return {
            State.DETECT: self._detect,
            State.TRIAGE: self._triage,
            State.PLAN: self._plan,
            State.INVESTIGATE: self._investigate,
            State.CORRELATE: self._correlate,
            State.HYPOTHESIZE: self._hypothesize,
            State.DEBATE: self._debate,
            State.SCORE: self._score,
            State.APPROVE: self._approve,
            State.REMEDIATE: self._remediate,
            State.VERIFY: self._verify,
            State.RECOMMEND: self._recommend,
            State.DOCUMENT: self._document,
        }[inc.state](inc)

    # ---- handlers (one concern each) -------------------------------------- #
    def _detect(self, inc: Incident) -> State:
        self.deps.audit(inc, "incident_opened")
        return State.TRIAGE

    def _triage(self, inc: Incident) -> State:
        self.deps.triage(inc)             # dedup, enrich, topology, severity
        return State.PLAN

    def _plan(self, inc: Incident) -> State:
        self.deps.plan(inc)               # select relevant agents + build DAG
        return State.INVESTIGATE

    def _investigate(self, inc: Incident) -> State:
        self.deps.investigate(inc)        # bounded parallel fan-out; degraded != crash
        return State.CORRELATE

    def _correlate(self, inc: Incident) -> State:
        self.deps.correlate(inc)          # normalize + timeline + top-k reduce
        return State.HYPOTHESIZE

    def _hypothesize(self, inc: Incident) -> State:
        self.deps.hypothesize(inc)        # >=1 cited candidate (enforced in contracts)
        return State.DEBATE

    def _debate(self, inc: Incident) -> State:
        self.deps.debate(inc)             # skeptic falsification; records rejection reasons
        return State.SCORE

    def _score(self, inc: Incident) -> State:
        conf = self.deps.score(inc)       # evidence-weighted, explainable
        self._rounds += 1
        low = conf is None or conf.value < self.cfg.confidence_threshold
        if low and self._rounds < self.cfg.max_investigation_rounds:
            return State.PLAN             # bounded loop: gather more, targeted
        return State.APPROVE

    def _approve(self, inc: Incident) -> State:
        # Confidence is an INPUT to a human decision, not a bypass (docs/06, docs/10).
        self.deps.render_for_human(inc)   # persists rendered_view_ref for audit
        verb = self.deps.await_decision(inc)  # durable wait -> no wall-clock/$ burn
        self.deps.audit(inc, f"decision:{verb.value}")
        if verb in (DecisionVerb.APPROVE, DecisionVerb.AUTO_REMEDIATE):
            return State.REMEDIATE
        if verb == DecisionVerb.NEED_MORE and self._rounds < self.cfg.max_investigation_rounds:
            return State.PLAN
        return State.RECOMMEND            # recommend_only / reject -> no mutation

    def _remediate(self, inc: Incident) -> State:
        self._remediation_attempts += 1
        self.deps.remediate(inc)          # dry-run -> apply; idempotency-keyed (docs/11)
        return State.VERIFY

    def _verify(self, inc: Incident) -> State:
        if self.deps.verify(inc):         # recovered + stable for the full window
            return State.DOCUMENT
        # Fix ineffective / made it worse: auto-rollback already ran inside verify().
        if self._remediation_attempts < self.cfg.max_remediation_attempts:
            return State.REMEDIATE
        self.deps.page_human(inc, "remediation ineffective — human to drive")
        return State.APPROVE              # re-decide with the new evidence

    def _recommend(self, inc: Incident) -> State:
        self.deps.audit(inc, "recommendation_posted")
        return State.DOCUMENT

    def _document(self, inc: Incident) -> State:
        self.deps.document(inc)           # blameless postmortem from the trajectory
        self.deps.audit(inc, "postmortem_drafted")
        return State.CLOSE

    # ---- budget guard ----------------------------------------------------- #
    def _check_budget(self, inc: Incident) -> None:
        if inc.state == State.APPROVE:
            return  # human wait is free; don't burn wall-clock waiting on a person
        if inc.budget is not None:
            inc.budget.steps += 1
            reason = inc.budget.exceeded()
            if reason:
                raise BudgetExceeded(reason)
