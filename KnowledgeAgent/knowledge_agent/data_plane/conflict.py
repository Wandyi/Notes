"""Conflict resolution across federated sources.

When GitHub, Helm, Terraform, and a runbook all describe the same service, they
frequently disagree — a runbook says "3 replicas", the live Helm values say
"6". The resolver aggregates structured `claims` attached to retrieved chunks,
finds keys where sources disagree, and resolves each by **freshness** (the most
recently updated source wins), recording the full disagreement for transparency.

In production, claims are extracted by an NER/LLM pass over chunk text; here
they are attached to seed documents so resolution is deterministic and testable.
The policy (freshest-wins) is pluggable.
"""
from __future__ import annotations

from ..core import Conflict, ScoredChunk


def resolve_conflicts(scored: list[ScoredChunk]) -> list[Conflict]:
    # key -> list of (value, ScoredChunk)
    by_key: dict[str, list[tuple[str, ScoredChunk]]] = {}
    for sc in scored:
        for key, value in sc.chunk.claims.items():
            by_key.setdefault(key, []).append((value, sc))

    conflicts: list[Conflict] = []
    for key, entries in by_key.items():
        distinct_values = {v for v, _ in entries}
        if len(distinct_values) < 2:
            continue  # everyone agrees — not a conflict

        # Freshest-source-wins resolution.
        winner_value, winner_chunk = max(
            entries, key=lambda e: e[1].chunk.updated_at
        )
        competing = [
            {
                "source": sc.chunk.source.value,
                "value": val,
                "url": sc.chunk.url,
                "updated_at": sc.chunk.updated_at.isoformat(),
            }
            for val, sc in entries
        ]
        conflicts.append(
            Conflict(
                key=key,
                resolved_value=winner_value,
                winning_source=winner_chunk.chunk.source,
                competing=competing,
            )
        )
    return conflicts
