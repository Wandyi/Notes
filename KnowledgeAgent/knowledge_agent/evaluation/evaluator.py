"""Evaluation — offline gates and trajectory-level analysis.

For agents you evaluate the whole *trajectory* (retrieval + tool calls +
reasoning + citations), not just the final string. `TrajectoryEvaluator` reads
the trace and scores the run on concrete, checkable criteria. The same evaluator
is used two ways:

* **continuous** — scoring live traffic (what the assistant attaches to each
  answer), to catch regressions as they happen;
* **offline** — running a fixed eval set to gate a change before it ships and to
  gate promotion in the registry lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core import Answer
from ..observability.tracing import Trace


@dataclass
class TrajectoryReport:
    score: float
    criteria: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class TrajectoryEvaluator:
    def __init__(self, groundedness_floor: float = 0.55) -> None:
        self.groundedness_floor = groundedness_floor

    def evaluate(self, answer: Answer, trace: Trace) -> TrajectoryReport:
        criteria: dict[str, float] = {}
        notes: list[str] = []

        # 1. Retrieval actually ran.
        retrieval_spans = trace.spans_of_kind("retrieval")
        criteria["retrieval_ran"] = 1.0 if retrieval_spans else 0.0

        # 2. Multiple tools/sources were consulted (federation, not single-doc).
        tool_spans = [s for s in trace.spans_of_kind("tool") if not s.error]
        criteria["multi_source"] = 1.0 if len(answer.sources_consulted) >= 2 else (
            0.5 if answer.sources_consulted else 0.0
        )

        # 3. The answer is grounded above the floor.
        criteria["grounded"] = 1.0 if answer.groundedness >= self.groundedness_floor else 0.0
        if answer.groundedness < self.groundedness_floor:
            notes.append(f"groundedness {answer.groundedness:.2f} below floor {self.groundedness_floor}")

        # 4. Every answer carries citations (provenance).
        criteria["cited"] = 1.0 if answer.citations else 0.0
        if not answer.citations and not answer.refused:
            notes.append("answer has no citations")

        # 5. No unhandled span errors in the trajectory.
        errored = [s for s in trace.spans if s.error]
        criteria["clean_trajectory"] = 1.0 if not errored else max(0.0, 1.0 - 0.2 * len(errored))
        if errored:
            notes.append(f"{len(errored)} span error(s) in trajectory")

        # 6. Conflicts, if present, were explicitly resolved (not silently dropped).
        if answer.conflicts:
            criteria["conflict_handling"] = 1.0
            notes.append(f"{len(answer.conflicts)} conflict(s) detected and resolved")
        else:
            criteria["conflict_handling"] = 1.0

        score = sum(criteria.values()) / len(criteria)
        return TrajectoryReport(score=score, criteria=criteria, notes=notes)


def run_offline_eval(cases: list[tuple[Answer, Trace]], evaluator: TrajectoryEvaluator) -> float:
    """Average trajectory score across an offline eval set (a ship gate)."""
    if not cases:
        return 0.0
    return sum(evaluator.evaluate(a, t).score for a, t in cases) / len(cases)
