"""Shared, asyncio-safe finding store.

Purpose (the classic multi-agent redundancy problem): without a shared space,
two subagents independently re-discover the same fact. The scratchpad gives
dependent subagents a place to read upstream findings before they start, and
gives the synthesizer the whole set in one read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from .contracts import ResearchFinding


class Scratchpad:
    def __init__(self) -> None:
        self._findings: dict[int, ResearchFinding] = {}
        self._lock = asyncio.Lock()

    async def put(self, finding: ResearchFinding) -> None:
        async with self._lock:
            self._findings[finding.sub_question_id] = finding

    async def get(self, ids: Iterable[int]) -> list[ResearchFinding]:
        """Return findings for the given ids that are already available."""
        async with self._lock:
            return [self._findings[i] for i in ids if i in self._findings]

    async def all(self) -> list[ResearchFinding]:
        async with self._lock:
            return list(self._findings.values())

    def context_for(self, findings: list[ResearchFinding]) -> str:
        """Render a set of upstream findings into a context block for a subagent."""
        if not findings:
            return ""
        blocks = "\n\n".join(f.as_context() for f in findings)
        return (
            "Upstream findings from sub-questions you depend on are below. "
            "Build on these — do not re-research what they already establish.\n\n"
            f"{blocks}"
        )
