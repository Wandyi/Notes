"""Researcher subagent: answer ONE sub-question in an isolated context.

Runs an agentic loop with the server-side ``web_search`` tool. The loop handles
``pause_turn`` (the server-tool iteration limit) by re-sending the accumulated
turn, capped by ``max_continuations``. Web-search results are collected across
every turn, not just the last one.

The subagent sees only its own sub-question plus any upstream findings it
depends on — never the other researchers' work. What it returns is a single
``ResearchFinding``.
"""

from __future__ import annotations

import time

from anthropic import AsyncAnthropic

from .config import Settings
from .contracts import FindingStatus, ResearchFinding, Source, SubQuestion
from .tracing import Span, Tracer

_SYSTEM = """You are a research subagent in a fan-out research system. You are \
responsible for exactly ONE sub-question.

- Use the web_search tool to gather current, factual information. Search \
iteratively: start broad, then refine based on what you find.
- Then write a SELF-CONTAINED findings summary (roughly 150-300 words) that \
answers your sub-question directly. It will be read by a synthesizer that never \
saw your searches, so state conclusions plainly and include the concrete facts, \
figures, and names that matter.
- Ground claims in the sources you found. Do not speculate beyond them; if the \
evidence is thin or conflicting, say so.
- You are operating autonomously. Do not ask questions or request confirmation \
— research and report."""


def _extract_sources(content: list, seen: dict[str, Source]) -> int:
    """Collect web_search_result sources from one response's content blocks.

    Returns the number of web-search *calls* observed in this response (each
    ``web_search_tool_result`` block corresponds to one search).
    """
    searches = 0
    for block in content:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        searches += 1
        results = block.content
        # A successful result's content is a list; an error's content is a
        # single error object — guard before iterating.
        if not isinstance(results, list):
            continue
        for r in results:
            if getattr(r, "type", None) == "web_search_result":
                if r.url not in seen:
                    seen[r.url] = Source(title=getattr(r, "title", "") or r.url, url=r.url)
    return searches


def _extract_text(content: list) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


async def research(
    client: AsyncAnthropic,
    settings: Settings,
    tracer: Tracer,
    sub_question: SubQuestion,
    upstream_context: str,
    attempt: int,
) -> ResearchFinding:
    user_content = f"Your sub-question:\n\n{sub_question.question}"
    if upstream_context:
        user_content = f"{upstream_context}\n\n---\n\n{user_content}"

    messages: list[dict] = [{"role": "user", "content": user_content}]
    tools = [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": settings.max_web_searches,
        }
    ]

    seen_sources: dict[str, Source] = {}
    span = Span(name=f"researcher[{sub_question.id}]", model=settings.researcher_model)
    started = time.monotonic()
    final_text = ""
    continuations = 0

    while True:
        response = await client.messages.create(
            model=settings.researcher_model,
            max_tokens=settings.max_tokens,
            system=_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": settings.researcher_effort},
            tools=tools,
            messages=messages,
        )

        span.input_tokens += getattr(response.usage, "input_tokens", 0)
        span.output_tokens += getattr(response.usage, "output_tokens", 0)
        span.web_searches += _extract_sources(response.content, seen_sources)
        final_text = _extract_text(response.content) or final_text

        # Server tool paused (hit its per-turn iteration cap) — resume by
        # re-sending the accumulated assistant turn, bounded by a cap.
        if response.stop_reason == "pause_turn" and continuations < settings.max_continuations:
            messages.append({"role": "assistant", "content": response.content})
            continuations += 1
            continue
        break

    span.duration_s = time.monotonic() - started
    await tracer.record(span)

    if not final_text:
        raise RuntimeError("Researcher produced no findings text.")

    return ResearchFinding(
        sub_question_id=sub_question.id,
        question=sub_question.question,
        status=FindingStatus.OK,
        summary=final_text,
        sources=list(seen_sources.values()),
        attempts=attempt,
    )
