import unittest
from datetime import datetime, timezone

from knowledge_agent.config import ChunkingConfig, RetrievalConfig
from knowledge_agent.core import Chunk, Document, Source
from knowledge_agent.retrieval.chunking import chunk_documents
from knowledge_agent.retrieval.embeddings import HashingEmbeddingProvider, cosine, tokenize
from knowledge_agent.retrieval.reranker import LexicalReranker
from knowledge_agent.retrieval.sparse import BM25Index
from knowledge_agent.retrieval.vector_store import DenseVectorStore

NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def _chunk(text, title="t", source=Source.GITHUB):
    return Chunk(id="c" + text[:4], doc_id="d", source=source, title=title, text=text,
                 url="http://x", updated_at=NOW, position=0)


class TestEmbeddings(unittest.TestCase):
    def test_deterministic(self):
        e = HashingEmbeddingProvider(64)
        self.assertEqual(e.embed("deploy payment service"), e.embed("deploy payment service"))

    def test_related_more_similar_than_unrelated(self):
        e = HashingEmbeddingProvider(256)
        q = e.embed("how to deploy payment service to production")
        related = e.embed("deploying the payment service into production namespace")
        unrelated = e.embed("rotate tls certificates on the edge proxy")
        self.assertGreater(cosine(q, related), cosine(q, unrelated))

    def test_stopwords_removed(self):
        self.assertNotIn("the", tokenize("the deploy and a service"))
        self.assertIn("deploy", tokenize("the deploy and a service"))


class TestBM25(unittest.TestCase):
    def setUp(self):
        self.chunks = [
            _chunk("deploy payment service to production via helm upgrade"),
            _chunk("rotate tls certificates on the edge proxy fleet"),
            _chunk("payment service replicas scale horizontally"),
        ]
        self.idx = BM25Index()
        self.idx.index(self.chunks)

    def test_exact_term_match_ranks_first(self):
        hits = self.idx.search("helm upgrade", top_n=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0][0], 0)

    def test_no_match_returns_empty(self):
        self.assertEqual(self.idx.search("quantum entanglement banana", top_n=3), [])

    def test_scores_descending(self):
        hits = self.idx.search("payment service", top_n=3)
        scores = [s for _i, s in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestDenseStore(unittest.TestCase):
    def test_ranks_semantically_closest(self):
        store = DenseVectorStore(HashingEmbeddingProvider(256))
        store.index([
            _chunk("deploy payment service to production"),
            _chunk("edge proxy tls certificate rotation guide"),
        ])
        hits = store.search("payment service deployment production", top_n=2)
        self.assertEqual(hits[0][0], 0)


class TestReranker(unittest.TestCase):
    def test_full_coverage_scores_higher(self):
        r = LexicalReranker()
        high = r.score("deploy payment service", _chunk("how to deploy the payment service now"))
        low = r.score("deploy payment service", _chunk("unrelated proxy certificate content"))
        self.assertGreater(high, low)

    def test_title_match_bonus(self):
        r = LexicalReranker()
        with_title = r.score("deploy payment", _chunk("some text", title="deploy payment guide"))
        without_title = r.score("deploy payment", _chunk("some text", title="unrelated"))
        self.assertGreater(with_title, without_title)

    def test_no_overlap_is_zero(self):
        self.assertEqual(LexicalReranker().score("banana quantum", _chunk("deploy payment service")), 0.0)


if __name__ == "__main__":
    unittest.main()
