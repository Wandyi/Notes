"""Fan-out research agent: a multi-agent orchestrator built on the Claude API.

A planner decomposes a broad question into sub-questions; researcher subagents
answer them in parallel (isolated context, server-side web search); a
synthesizer merges the findings into one cited report.
"""

from __future__ import annotations

from .config import Settings
from .contracts import ResearchFinding, ResearchPlan, SubQuestion
from .orchestrator import Orchestrator

__all__ = [
    "Orchestrator",
    "Settings",
    "ResearchPlan",
    "SubQuestion",
    "ResearchFinding",
]
__version__ = "0.1.0"
