import unittest
from datetime import datetime, timedelta, timezone

from knowledge_agent.config import RetrievalConfig
from knowledge_agent.core import Chunk, Source
from knowledge_agent.retrieval.embeddings import HashingEmbeddingProvider
from knowledge_agent.retrieval.freshness import blend_final_score, freshness_score, is_stale
from knowledge_agent.retrieval.hybrid import HybridRetriever, reciprocal_rank_fusion
from knowledge_agent.retrieval.reranker import LexicalReranker

NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def _chunk(text, days_old=1, title="t", source=Source.GITHUB, cid=None):
    return Chunk(id=cid or ("c" + text[:6]), doc_id="d", source=source, title=title,
                 text=text, url="http://x",
                 updated_at=NOW - timedelta(days=days_old), position=0)


class TestRRF(unittest.TestCase):
    def test_fusion_rewards_agreement(self):
        dense = [(1, 0.9), (2, 0.8)]
        sparse = [(1, 5.0), (3, 4.0)]
        fused = reciprocal_rank_fusion(dense, sparse, k=60)
        # chunk 1 appears in both lists -> highest fused score.
        self.assertEqual(max(fused, key=fused.get), 1)

    def test_higher_rank_contributes_more(self):
        fused = reciprocal_rank_fusion([(1, 0.9), (2, 0.1)], [], k=60)
        self.assertGreater(fused[1], fused[2])


class TestFreshness(unittest.TestCase):
    def test_decay_monotonic(self):
        new = freshness_score(NOW - timedelta(days=1), 45.0, NOW)
        old = freshness_score(NOW - timedelta(days=90), 45.0, NOW)
        self.assertGreater(new, old)

    def test_half_life(self):
        self.assertAlmostEqual(freshness_score(NOW - timedelta(days=45), 45.0, NOW), 0.5, places=3)

    def test_staleness_boundary(self):
        self.assertFalse(is_stale(NOW - timedelta(days=100), 120.0, NOW))
        self.assertTrue(is_stale(NOW - timedelta(days=130), 120.0, NOW))

    def test_blend_weights_freshness(self):
        cfg = RetrievalConfig(freshness_weight=0.5)
        self.assertAlmostEqual(blend_final_score(1.0, 0.0, cfg), 0.5, places=6)


class TestHybridRetriever(unittest.TestCase):
    def setUp(self):
        self.cfg = RetrievalConfig()
        self.retriever = HybridRetriever(HashingEmbeddingProvider(256), LexicalReranker(), self.cfg)

    def test_retrieves_relevant_over_noise(self):
        chunks = [
            _chunk("deploy payment service to production with helm upgrade", cid="good"),
            _chunk("rotate tls certificates on the edge proxy fleet", cid="noise"),
        ]
        self.retriever.index(chunks)
        got = self.retriever.retrieve("how to deploy payment service to production", top_k=1, now=NOW)
        self.assertEqual(got[0].chunk.id, "good")

    def test_min_rerank_floor_drops_irrelevant(self):
        self.retriever.index([_chunk("deploy payment service production")])
        # Completely unrelated query -> no chunk clears the relevance floor.
        self.assertEqual(self.retriever.retrieve("banana quantum velocity", top_k=5, now=NOW), [])

    def test_freshness_breaks_ties_toward_recent(self):
        # Two chunks with identical text (equal rerank); the fresher one wins.
        chunks = [
            _chunk("deploy payment service production", days_old=200, cid="stale"),
            _chunk("deploy payment service production", days_old=2, cid="fresh"),
        ]
        self.retriever.index(chunks)
        got = self.retriever.retrieve("deploy payment service production", top_k=2, now=NOW)
        self.assertEqual(got[0].chunk.id, "fresh")

    def test_stale_flag_propagates(self):
        self.retriever.index([_chunk("deploy payment service production", days_old=200, cid="old")])
        got = self.retriever.retrieve("deploy payment service production", top_k=1, now=NOW)
        self.assertTrue(got[0].is_stale)

    def test_empty_index_returns_empty(self):
        self.assertEqual(self.retriever.retrieve("anything", top_k=3, now=NOW), [])


if __name__ == "__main__":
    unittest.main()
