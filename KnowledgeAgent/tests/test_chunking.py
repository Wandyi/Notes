import unittest
from datetime import datetime, timezone

from knowledge_agent.config import ChunkingConfig
from knowledge_agent.core import Document, Source
from knowledge_agent.retrieval.chunking import chunk_document


def _doc(content: str) -> Document:
    return Document(
        id="d1", source=Source.GITHUB, title="Deploy Guide", content=content,
        url="http://x", updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        claims={"replicas": "6"}, metadata={"team": "payments"},
    )


class TestChunking(unittest.TestCase):
    def test_splits_on_paragraph_boundaries(self):
        doc = _doc("First paragraph about deploys.\n\nSecond paragraph about rollback.")
        chunks = chunk_document(doc, ChunkingConfig(target_tokens=50, overlap_tokens=5, min_tokens=1))
        self.assertEqual(len(chunks), 2)
        self.assertIn("First", chunks[0].text)
        self.assertIn("Second", chunks[1].text)

    def test_long_block_uses_sliding_window_with_overlap(self):
        words = " ".join(f"w{i}" for i in range(200))
        doc = _doc(words)
        cfg = ChunkingConfig(target_tokens=50, overlap_tokens=10, min_tokens=1)
        chunks = chunk_document(doc, cfg)
        self.assertGreater(len(chunks), 1)
        # Overlap: the tail of chunk 0 reappears at the head of chunk 1.
        tail = chunks[0].text.split()[-10:]
        head = chunks[1].text.split()[:10]
        self.assertEqual(tail, head)

    def test_chunks_inherit_claims_and_provenance(self):
        chunks = chunk_document(_doc("Some deploy content here."), ChunkingConfig(min_tokens=1))
        self.assertEqual(chunks[0].claims, {"replicas": "6"})
        self.assertEqual(chunks[0].source, Source.GITHUB)
        self.assertEqual(chunks[0].doc_id, "d1")
        self.assertEqual(chunks[0].url, "http://x")

    def test_positions_are_monotonic(self):
        doc = _doc("a b c\n\nd e f\n\ng h i")
        chunks = chunk_document(doc, ChunkingConfig(target_tokens=50, min_tokens=1))
        self.assertEqual([c.position for c in chunks], list(range(len(chunks))))

    def test_empty_document_yields_no_chunks(self):
        self.assertEqual(chunk_document(_doc("   "), ChunkingConfig()), [])


if __name__ == "__main__":
    unittest.main()
