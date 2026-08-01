"""Central configuration for the KnowledgeAgent platform.

Every knob a staff-level review would probe — budgets, model tiers, guardrail
thresholds, freshness half-life, staleness windows — lives here rather than
being scattered as magic numbers through the code.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChunkingConfig:
    target_tokens: int = 120        # ~ words per chunk (approximate token proxy)
    overlap_tokens: int = 24        # sliding-window overlap to preserve context
    min_tokens: int = 20


@dataclass
class RetrievalConfig:
    embedding_dim: int = 256
    rrf_k: int = 60                 # reciprocal-rank-fusion constant
    dense_candidates: int = 20
    sparse_candidates: int = 20
    rerank_top_n: int = 12          # how many fused candidates to re-rank
    min_rerank: float = 0.05        # lexical-relevance floor; below this a chunk
                                    # is not treated as genuine evidence (guards
                                    # against spurious hashed-embedding collisions)
    freshness_half_life_days: float = 45.0
    staleness_days: float = 120.0   # chunks older than this are flagged stale
    freshness_weight: float = 0.25  # blend of freshness into final score


@dataclass
class ModelTier:
    name: str
    model_id: str
    input_per_1k: float             # USD per 1k input tokens
    output_per_1k: float


@dataclass
class RoutingConfig:
    """Model tiering: route cheap first, escalate on uncertainty."""
    cheap: ModelTier = field(
        default_factory=lambda: ModelTier("cheap", "claude-haiku-4-5", 0.001, 0.005)
    )
    strong: ModelTier = field(
        default_factory=lambda: ModelTier("strong", "claude-opus-5", 0.005, 0.025)
    )
    # Escalate to the strong model when the retrieved evidence is thin/conflicting.
    escalate_below_evidence: int = 3          # fewer than N grounded chunks
    escalate_on_conflict: bool = True


@dataclass
class GuardrailConfig:
    max_query_chars: int = 4000
    groundedness_floor: float = 0.55          # below this → warn / re-check
    block_on_injection: bool = True


@dataclass
class BudgetConfig:
    """Per-request and per-tenant budgets to contain runaway loops and cost."""
    max_steps: int = 12                       # runtime state-machine step ceiling
    max_tool_calls: int = 16
    max_wall_clock_s: float = 30.0
    max_cost_usd_per_request: float = 0.50
    max_cost_usd_per_tenant_day: float = 250.0


@dataclass
class Config:
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    guardrails: GuardrailConfig = field(default_factory=GuardrailConfig)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)

    # Governance: the registered agent identity + its accountable owner.
    agent_name: str = "engineering-knowledge-assistant"
    agent_owner: str = "platform-eng@infoblox.com"
    staleness_review_days: float = 90.0       # registry staleness detection


DEFAULT_CONFIG = Config()
