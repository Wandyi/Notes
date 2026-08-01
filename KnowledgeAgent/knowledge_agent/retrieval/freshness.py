"""Freshness / staleness scoring for the corpus.

Enterprise knowledge rots: a runbook from two years ago may confidently state
a replica count that Terraform has since changed. We score every chunk by age
with an exponential decay (half-life configurable) and flag chunks older than a
staleness window so the answer can down-weight — and visibly warn about — stale
evidence.
"""
from __future__ import annotations

import math
from datetime import datetime

from ..config import RetrievalConfig
from ..core import utcnow


def freshness_score(updated_at: datetime, half_life_days: float, now: datetime | None = None) -> float:
    """Exponential decay in [0, 1]; 1.0 == brand new, 0.5 == one half-life old."""
    now = now or utcnow()
    age_days = max(0.0, (now - updated_at).total_seconds() / 86400.0)
    return math.pow(0.5, age_days / half_life_days) if half_life_days > 0 else 1.0


def is_stale(updated_at: datetime, staleness_days: float, now: datetime | None = None) -> bool:
    now = now or utcnow()
    age_days = (now - updated_at).total_seconds() / 86400.0
    return age_days > staleness_days


def blend_final_score(relevance: float, freshness: float, config: RetrievalConfig) -> float:
    """Blend rerank relevance with freshness so recent, relevant chunks win."""
    w = config.freshness_weight
    return (1 - w) * relevance + w * freshness
