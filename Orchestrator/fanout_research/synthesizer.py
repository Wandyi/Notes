"""Synthesizer: merge all findings into one cited report.

The synthesis stage is a separate model invocation from the researchers on
purpose — an agent grading/merging its own work is the most common failure mode
in this pattern. It reads the whole finding set once (via the scratchpad) and is
told explicitly which sub-questions failed so it can flag the gaps instead of
inventing around them.

Streams the response, since a synthesized report can be long.
"""

from __future__ import annotations

import time

from anthropic import AsyncAnthropic

from .config import Settings
from .contracts import ResearchFinding, ResearchPlan
from .tracing import Span, Tracer

_SYSTEM = """You are the synthesis stage of a fan-out research system. You are \
given a top-level question and the findings from several research subagents.

Write a single, coherent report that answers the top-level question:

- Integrate the findings — do not just concatenate them. Draw connections and \
resolve or surface disagreements between subagents.
- Use inline numbered citations like [1], [2] that map to the Sources list you \
append at the end. Reuse a number for repeated use of the same source.
- If a sub-question failed or its findings were thin, note the gap explicitly \
rather than papering over it.
- Lead with the direct answer, then supporting detail. Be readable first, \
concise second."""


def _render_findings(findings: list[ResearchFinding]) -> tuple[str, list[str]]:
    """Build the findings block and a flat, de-duplicated source list."""
    url_to_num: dict[str, int] = {}
    ordered_urls: list[str] = []
    blocks: list[str] = []

    for f in sorted(findings, key=lambda x: x.sub_question_id):
        header = f"### Sub-question {f.sub_question_id}: {f.question}"
        if not f.ok:
            blocks.append(f"{header}\n[FAILED after {f.attempts} attempt(s): {f.error}]")
            continue
        cited = []
        for s in f.sources:
            if s.url not in url_to_num:
                ordered_urls.append(s.url)
                url_to_num[s.url] = len(ordered_urls)
            cited.append(f"[{url_to_num[s.url]}] {s.title} — {s.url}")
        src_block = "\n".join(cited) if cited else "(no sources captured)"
        blocks.append(f"{header}\n{f.summary}\n\nSources for this sub-question:\n{src_block}")

    numbered_sources = [f"[{i + 1}] {u}" for i, u in enumerate(ordered_urls)]
    return "\n\n".join(blocks), numbered_sources


async def synthesize(
    client: AsyncAnthropic,
    settings: Settings,
    tracer: Tracer,
    question: str,
    plan: ResearchPlan,
    findings: list[ResearchFinding],
) -> str:
    findings_block, numbered_sources = _render_findings(findings)
    sources_hint = "\n".join(numbered_sources) if numbered_sources else "(none)"

    user = (
        f"TOP-LEVEL QUESTION:\n{question}\n\n"
        f"PLANNER'S INTERPRETATION:\n{plan.interpretation}\n\n"
        f"SUBAGENT FINDINGS:\n{findings_block}\n\n"
        f"CANONICAL SOURCE NUMBERING (use these numbers for inline citations, "
        f"and reproduce this list under a 'Sources' heading at the end):\n{sources_hint}"
    )

    started = time.monotonic()
    async with client.messages.stream(
        model=settings.synthesizer_model,
        max_tokens=settings.max_tokens,
        system=_SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": settings.synthesizer_effort},
        messages=[{"role": "user", "content": user}],
    ) as stream:
        message = await stream.get_final_message()

    await tracer.record(
        Span(
            name="synthesizer",
            model=settings.synthesizer_model,
            input_tokens=getattr(message.usage, "input_tokens", 0),
            output_tokens=getattr(message.usage, "output_tokens", 0),
            cache_read_input_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            duration_s=time.monotonic() - started,
        )
    )

    return "\n".join(b.text for b in message.content if b.type == "text").strip()
