import unittest
from datetime import datetime, timezone

from knowledge_agent.core import Answer, Citation, Source
from knowledge_agent.evaluation.evaluator import TrajectoryEvaluator, run_offline_eval
from knowledge_agent.observability.tracing import Trace

NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def _citation():
    return Citation(marker="[1]", source=Source.GITHUB, title="t", url="u",
                    chunk_id="c", updated_at=NOW)


def _good_trace():
    t = Trace()
    with t.span("r", "retrieval"):
        pass
    with t.span("tool", "tool"):
        pass
    with t.span("llm", "llm"):
        pass
    return t


class TestTrajectoryEvaluator(unittest.TestCase):
    def setUp(self):
        self.ev = TrajectoryEvaluator(groundedness_floor=0.55)

    def test_high_quality_run_scores_high(self):
        ans = Answer(request_id="r", query="q", text="answer",
                     citations=[_citation()], groundedness=0.9,
                     sources_consulted=[Source.GITHUB, Source.HELM])
        report = self.ev.evaluate(ans, _good_trace())
        self.assertGreaterEqual(report.score, 0.9)
        self.assertEqual(report.criteria["cited"], 1.0)
        self.assertEqual(report.criteria["grounded"], 1.0)

    def test_ungrounded_uncited_scores_low(self):
        ans = Answer(request_id="r", query="q", text="answer",
                     citations=[], groundedness=0.1, sources_consulted=[])
        report = self.ev.evaluate(ans, Trace())
        self.assertLess(report.score, 0.5)
        self.assertTrue(report.notes)

    def test_span_errors_penalized(self):
        t = Trace()
        sp = t.event("bad", "tool")
        sp.error = "boom"
        ans = Answer(request_id="r", query="q", text="a", citations=[_citation()],
                     groundedness=0.9, sources_consulted=[Source.GITHUB, Source.HELM])
        report = self.ev.evaluate(ans, t)
        self.assertLess(report.criteria["clean_trajectory"], 1.0)

    def test_offline_eval_averages(self):
        good = Answer(request_id="r", query="q", text="a", citations=[_citation()],
                      groundedness=0.9, sources_consulted=[Source.GITHUB, Source.HELM])
        cases = [(good, _good_trace()), (good, _good_trace())]
        self.assertGreaterEqual(run_offline_eval(cases, self.ev), 0.9)


class TestTracing(unittest.TestCase):
    def test_span_records_error_and_reraises(self):
        t = Trace()
        with self.assertRaises(ValueError):
            with t.span("x", "tool"):
                raise ValueError("nope")
        self.assertEqual(len(t.spans), 1)
        self.assertIn("ValueError", t.spans[0].error)

    def test_summary_counts_by_kind(self):
        t = Trace()
        with t.span("a", "retrieval"):
            pass
        t.event("b", "llm")
        summary = t.summary()
        self.assertEqual(summary["by_kind"]["retrieval"], 1)
        self.assertEqual(summary["by_kind"]["llm"], 1)


if __name__ == "__main__":
    unittest.main()
