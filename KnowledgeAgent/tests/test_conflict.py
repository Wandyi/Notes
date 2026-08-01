import unittest
from datetime import datetime, timedelta, timezone

from knowledge_agent.core import Chunk, ScoredChunk, Source
from knowledge_agent.data_plane.conflict import resolve_conflicts

NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def _scored(source, claims, days_old):
    chunk = Chunk(id=f"c_{source.value}", doc_id="d", source=source, title="t",
                  text="text", url=f"http://{source.value}",
                  updated_at=NOW - timedelta(days=days_old), position=0, claims=claims)
    return ScoredChunk(chunk=chunk, final_score=1.0)


class TestConflictResolution(unittest.TestCase):
    def test_detects_conflict_and_picks_freshest(self):
        scored = [
            _scored(Source.RUNBOOK, {"replicas": "3"}, days_old=200),   # stale
            _scored(Source.HELM, {"replicas": "6"}, days_old=15),       # fresh -> should win
            _scored(Source.KUBERNETES, {"replicas": "6"}, days_old=20),
        ]
        conflicts = resolve_conflicts(scored)
        self.assertEqual(len(conflicts), 1)
        c = conflicts[0]
        self.assertEqual(c.key, "replicas")
        self.assertEqual(c.resolved_value, "6")
        self.assertEqual(c.winning_source, Source.HELM)
        self.assertEqual(len(c.competing), 3)

    def test_agreement_is_not_a_conflict(self):
        scored = [
            _scored(Source.HELM, {"namespace": "payments-prod"}, days_old=15),
            _scored(Source.KUBERNETES, {"namespace": "payments-prod"}, days_old=20),
        ]
        self.assertEqual(resolve_conflicts(scored), [])

    def test_multiple_independent_conflicts(self):
        scored = [
            _scored(Source.RUNBOOK, {"replicas": "3", "port": "8080"}, days_old=200),
            _scored(Source.HELM, {"replicas": "6", "port": "9090"}, days_old=10),
        ]
        keys = {c.key for c in resolve_conflicts(scored)}
        self.assertEqual(keys, {"replicas", "port"})

    def test_no_claims_no_conflict(self):
        self.assertEqual(resolve_conflicts([_scored(Source.SLACK, {}, days_old=1)]), [])


if __name__ == "__main__":
    unittest.main()
