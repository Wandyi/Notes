"""The interface contract between the orchestrator and its subagents.

The single most important design decision in a multi-agent system is *what
state crosses the boundary and in what shape*. Everything here is that shape:

- ``ResearchPlan`` / ``SubQuestion`` — the planner's structured output.
- ``ResearchFinding`` — the ONLY thing a researcher subagent hands back.

Keeping the boundary this narrow is what lets each researcher run in its own
isolated context: the orchestrator never sees a subagent's scratch reasoning,
only its structured finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Planner output — a JSON-schema-constrained structured output from the model. #
# --------------------------------------------------------------------------- #


class SubQuestion(BaseModel):
    """One decomposed research sub-question the orchestrator will delegate."""

    id: int = Field(description="Stable integer id, starting at 0 and contiguous.")
    question: str = Field(description="A focused, self-contained research question.")
    rationale: str = Field(
        description="Why this sub-question matters to answering the top-level question."
    )
    depends_on: list[int] = Field(
        default_factory=list,
        description=(
            "ids of other sub-questions whose findings this one needs first. "
            "Empty means it is independent and can run immediately in parallel. "
            "Use this ONLY for genuine information dependencies."
        ),
    )


class ResearchPlan(BaseModel):
    """The planner's decomposition of the top-level question."""

    interpretation: str = Field(
        description="One or two sentences on how you interpreted the question."
    )
    sub_questions: list[SubQuestion]


# --------------------------------------------------------------------------- #
# Runtime state — what actually flows across the orchestrator/subagent line.   #
# --------------------------------------------------------------------------- #


class FindingStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"


@dataclass
class Source:
    title: str
    url: str


@dataclass
class ResearchFinding:
    """A researcher subagent's structured return value.

    A ``FAILED`` finding is a first-class outcome, not an exception: the
    orchestrator records it, the synthesizer is told the gap exists, and the
    run still completes.
    """

    sub_question_id: int
    question: str
    status: FindingStatus
    summary: str
    sources: list[Source] = field(default_factory=list)
    attempts: int = 1
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is FindingStatus.OK

    def as_context(self) -> str:
        """Digest of this finding used to seed *dependent* subagents.

        This is the deduplication mechanism: a downstream researcher receives
        upstream findings as context so it builds on them instead of
        re-discovering the same facts.
        """
        if not self.ok:
            return f"[Q{self.sub_question_id}] {self.question}\n  (research failed: {self.error})"
        srcs = "; ".join(s.url for s in self.sources[:3]) or "(no sources captured)"
        return (
            f"[Q{self.sub_question_id}] {self.question}\n"
            f"  Findings: {self.summary}\n"
            f"  Sources: {srcs}"
        )
