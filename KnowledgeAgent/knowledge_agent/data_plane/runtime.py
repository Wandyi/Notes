"""Agent runtime & execution model.

Agents are non-deterministic, so the loop is an *explicit state machine* with
step limits and termination conditions rather than free-form reasoning — this is
what keeps control flow from breaking.

Phases: PERCEIVE → PLAN → ACT → OBSERVE → SYNTHESIZE → COMPLETE.

Each phase handler runs against a shared `RunContext` blackboard and returns the
next phase (or terminates early). The runtime enforces per-request budgets
(steps, tool calls, wall-clock) so a runaway loop is contained, and every
transition is traced. State is checkpointable (`RunContext.snapshot`) so a
long-running run could be resumed after a crash.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..config import BudgetConfig
from ..core import QueryRequest
from ..observability.tracing import Trace


class Phase(str, Enum):
    PERCEIVE = "perceive"
    PLAN = "plan"
    ACT = "act"
    OBSERVE = "observe"
    SYNTHESIZE = "synthesize"
    COMPLETE = "complete"
    TERMINATED = "terminated"


class TerminationReason(str, Enum):
    COMPLETED = "completed"
    GUARDRAIL = "guardrail_blocked"
    ACCESS_DENIED = "access_denied"
    NO_EVIDENCE = "no_evidence"
    BUDGET = "budget_exceeded"
    ERROR = "error"


class BudgetExceeded(Exception):
    pass


@dataclass
class RunContext:
    request: QueryRequest
    trace: Trace
    budgets: BudgetConfig
    blackboard: dict[str, Any] = field(default_factory=dict)
    steps: int = 0
    tool_calls: int = 0
    started_at: float = field(default_factory=time.perf_counter)
    terminated: bool = False
    reason: TerminationReason | None = None
    note: str | None = None

    def elapsed_s(self) -> float:
        return time.perf_counter() - self.started_at

    def check_budget(self) -> None:
        if self.steps > self.budgets.max_steps:
            raise BudgetExceeded(f"step limit {self.budgets.max_steps} exceeded")
        if self.tool_calls > self.budgets.max_tool_calls:
            raise BudgetExceeded(f"tool-call limit {self.budgets.max_tool_calls} exceeded")
        if self.elapsed_s() > self.budgets.max_wall_clock_s:
            raise BudgetExceeded(f"wall-clock {self.budgets.max_wall_clock_s}s exceeded")

    def add_tool_calls(self, n: int) -> None:
        self.tool_calls += n
        self.check_budget()

    def terminate(self, reason: TerminationReason, note: str | None = None) -> Phase:
        self.terminated = True
        self.reason = reason
        self.note = note
        return Phase.TERMINATED

    def snapshot(self) -> dict[str, Any]:
        """Checkpoint for durable/resumable execution."""
        return {
            "request_id": self.request.request_id,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "elapsed_s": round(self.elapsed_s(), 4),
            "blackboard_keys": sorted(self.blackboard.keys()),
            "terminated": self.terminated,
            "reason": self.reason.value if self.reason else None,
        }


Handler = Callable[[RunContext], Phase]

# Linear pipeline order; a handler may skip ahead by returning COMPLETE/TERMINATED.
_PIPELINE: list[Phase] = [
    Phase.PERCEIVE, Phase.PLAN, Phase.ACT,
    Phase.OBSERVE, Phase.SYNTHESIZE, Phase.COMPLETE,
]


class AgentRuntime:
    def __init__(self, budgets: BudgetConfig) -> None:
        self.budgets = budgets

    def run(self, request: QueryRequest, trace: Trace, handlers: dict[Phase, Handler]) -> RunContext:
        ctx = RunContext(request=request, trace=trace, budgets=self.budgets)
        phase = Phase.PERCEIVE
        try:
            while phase not in (Phase.COMPLETE, Phase.TERMINATED):
                ctx.steps += 1
                ctx.check_budget()
                handler = handlers.get(phase)
                with trace.span(f"state.{phase.value}", "state", step=ctx.steps) as sp:
                    if handler is None:
                        next_phase = self._advance(phase)
                    else:
                        next_phase = handler(ctx)
                    sp.attributes["next"] = next_phase.value
                phase = next_phase
            if phase is Phase.COMPLETE:
                handler = handlers.get(Phase.COMPLETE)
                if handler is not None:
                    with trace.span("state.complete", "state", step=ctx.steps + 1):
                        handler(ctx)
                ctx.reason = ctx.reason or TerminationReason.COMPLETED
        except BudgetExceeded as exc:
            trace.event("runtime.budget_exceeded", "state", error=str(exc))
            ctx.terminate(TerminationReason.BUDGET, str(exc))
        return ctx

    def _advance(self, phase: Phase) -> Phase:
        idx = _PIPELINE.index(phase)
        return _PIPELINE[idx + 1] if idx + 1 < len(_PIPELINE) else Phase.COMPLETE
