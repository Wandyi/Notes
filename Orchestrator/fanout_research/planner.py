"""Planner: decompose a broad question into independent-where-possible sub-questions.

Uses structured outputs (``messages.parse``) so the decomposition comes back as
a validated ``ResearchPlan`` rather than free text we'd have to parse.
"""

from __future__ import annotations

import time

from anthropic import AsyncAnthropic

from .config import Settings
from .contracts import ResearchPlan
from .tracing import Span, Tracer

_SYSTEM = """You are the planning stage of a fan-out research system.

Given a broad research question, decompose it into focused sub-questions that \
can be researched independently and in parallel. Good decomposition is the whole \
game here:

- Prefer INDEPENDENT sub-questions (empty `depends_on`) so they can fan out in \
parallel. Only add a dependency when one sub-question genuinely cannot be \
answered without another's findings.
- Each sub-question must be self-contained: a researcher will see only that \
question (plus any upstream findings it depends on), never the others.
- Cover the top-level question without overlap. Overlapping sub-questions waste \
a subagent and produce duplicate findings.
- Aim for {min}-{max} sub-questions. Fewer if the question is narrow.

Assign contiguous integer ids starting at 0."""


async def plan(
    client: AsyncAnthropic,
    settings: Settings,
    tracer: Tracer,
    question: str,
) -> ResearchPlan:
    system = _SYSTEM.format(min=settings.min_subquestions, max=settings.max_subquestions)
    started = time.monotonic()

    response = await client.messages.parse(
        model=settings.planner_model,
        max_tokens=settings.max_tokens,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": f"Top-level research question:\n\n{question}"}],
        output_format=ResearchPlan,
    )

    await tracer.record(
        Span(
            name="planner",
            model=settings.planner_model,
            input_tokens=getattr(response.usage, "input_tokens", 0),
            output_tokens=getattr(response.usage, "output_tokens", 0),
            cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            duration_s=time.monotonic() - started,
        )
    )

    plan_obj = response.parsed_output
    if plan_obj is None or not plan_obj.sub_questions:
        raise RuntimeError("Planner returned no sub-questions.")
    return plan_obj
