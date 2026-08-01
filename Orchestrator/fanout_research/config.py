"""Configuration and cost tables for the fan-out research orchestrator.

Everything tunable lives here so the orchestration code reads as policy, not
as a pile of magic numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

# Token pricing per 1M tokens (USD). Used only for the tracer's cost estimate;
# keep in sync with https://platform.claude.com/docs/en/about-claude/pricing.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}

# Web search server tool bills separately from tokens: $10 per 1,000 searches.
WEB_SEARCH_COST_PER_CALL = 10.0 / 1000.0


@dataclass(frozen=True)
class Settings:
    """Immutable run configuration.

    Model routing is intentionally per-role: a broad research fan-out is a good
    place to run the leaf researchers on a cheaper model and reserve the most
    capable model for planning and synthesis. Defaults keep all three on Opus
    4.8 so the out-of-the-box behavior is "best quality everywhere."
    """

    planner_model: str = "claude-opus-4-8"
    researcher_model: str = "claude-opus-4-8"
    synthesizer_model: str = "claude-opus-4-8"

    # Decomposition bounds handed to the planner as guidance.
    min_subquestions: int = 3
    max_subquestions: int = 6

    # Fan-out is *bounded* — cap concurrent researcher subagents so a wide
    # decomposition doesn't trip organization rate limits.
    max_concurrency: int = 4

    # Failure / re-delegation policy: how many times to retry a researcher that
    # errors out before marking its sub-question failed (and telling the
    # synthesizer about the gap rather than crashing the whole run).
    max_researcher_retries: int = 2

    # Per-researcher web-search budget and pause_turn continuation cap.
    max_web_searches: int = 5
    max_continuations: int = 6

    # Effort per role (see the effort parameter in the Claude API docs).
    researcher_effort: str = "high"
    synthesizer_effort: str = "high"

    max_tokens: int = 16000

    def pricing(self, model: str) -> dict[str, float]:
        return MODEL_PRICING.get(model, {"input": 0.0, "output": 0.0})
