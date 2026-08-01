"""Lightweight observability for a run.

A multi-agent loop that you can't observe is a multi-agent loop you can't
operate. Every model call records a span (tokens, latency, web searches); the
tracer rolls them up into a per-role and per-subagent cost/latency summary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .config import WEB_SEARCH_COST_PER_CALL, Settings


@dataclass
class Span:
    name: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    web_searches: int = 0
    duration_s: float = 0.0
    ok: bool = True


class Tracer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.spans: list[Span] = []
        self._lock = asyncio.Lock()

    async def record(self, span: Span) -> None:
        async with self._lock:
            self.spans.append(span)

    def cost(self, span: Span) -> float:
        p = self.settings.pricing(span.model)
        token_cost = (
            span.input_tokens / 1e6 * p["input"]
            + span.output_tokens / 1e6 * p["output"]
        )
        return token_cost + span.web_searches * WEB_SEARCH_COST_PER_CALL

    def summary(self, wall_clock_s: float) -> str:
        lines: list[str] = []
        lines.append("─" * 78)
        lines.append("RUN TRACE")
        lines.append("─" * 78)
        header = f"{'span':<26}{'model':<18}{'in':>8}{'out':>8}{'web':>5}{'sec':>7}{'$':>9}"
        lines.append(header)
        lines.append("-" * 78)

        total_in = total_out = total_web = 0
        total_cost = 0.0
        for s in self.spans:
            c = self.cost(s)
            total_in += s.input_tokens
            total_out += s.output_tokens
            total_web += s.web_searches
            total_cost += c
            flag = "" if s.ok else "  ✗"
            lines.append(
                f"{s.name[:25]:<26}{s.model[:17]:<18}"
                f"{s.input_tokens:>8}{s.output_tokens:>8}{s.web_searches:>5}"
                f"{s.duration_s:>7.1f}{c:>9.4f}{flag}"
            )

        lines.append("-" * 78)
        lines.append(
            f"{'TOTAL':<26}{'':<18}{total_in:>8}{total_out:>8}{total_web:>5}"
            f"{wall_clock_s:>7.1f}{total_cost:>9.4f}"
        )
        # Wall clock < summed span time is the payoff of the parallel fan-out.
        summed = sum(s.duration_s for s in self.spans)
        if summed > 0:
            lines.append(
                f"parallel speedup: {summed / wall_clock_s:.2f}x "
                f"(summed model time {summed:.1f}s vs wall clock {wall_clock_s:.1f}s)"
            )
        lines.append("─" * 78)
        return "\n".join(lines)
