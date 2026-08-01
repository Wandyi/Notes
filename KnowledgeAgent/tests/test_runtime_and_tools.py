import unittest

from knowledge_agent.config import BudgetConfig
from knowledge_agent.core import Principal, QueryRequest, Source
from knowledge_agent.data_plane.runtime import (
    AgentRuntime, Phase, RunContext, TerminationReason,
)
from knowledge_agent.data_plane.tools.base import ToolManifest
from knowledge_agent.data_plane.tools.connectors import (
    CorpusConnector, FailingConnector, SlowConnector, MANIFESTS,
)
from knowledge_agent.data_plane.tools.registry import ToolRegistry
from knowledge_agent.observability.tracing import Trace
from knowledge_agent.seed.corpus import build_corpus


def _request():
    return QueryRequest(query="deploy payment-service", principal=Principal("u", "t", ["engineer"]))


class TestRuntime(unittest.TestCase):
    def test_runs_all_phases_in_order(self):
        seen = []

        def make(phase, nxt):
            def h(ctx):
                seen.append(phase)
                return nxt
            return h

        handlers = {
            Phase.PERCEIVE: make(Phase.PERCEIVE, Phase.PLAN),
            Phase.PLAN: make(Phase.PLAN, Phase.ACT),
            Phase.ACT: make(Phase.ACT, Phase.OBSERVE),
            Phase.OBSERVE: make(Phase.OBSERVE, Phase.SYNTHESIZE),
            Phase.SYNTHESIZE: make(Phase.SYNTHESIZE, Phase.COMPLETE),
            Phase.COMPLETE: lambda ctx: Phase.COMPLETE,
        }
        rt = AgentRuntime(BudgetConfig())
        ctx = rt.run(_request(), Trace(), handlers)
        self.assertEqual(seen, [Phase.PERCEIVE, Phase.PLAN, Phase.ACT, Phase.OBSERVE, Phase.SYNTHESIZE])
        self.assertEqual(ctx.reason, TerminationReason.COMPLETED)

    def test_early_termination_short_circuits(self):
        def perceive(ctx):
            return ctx.terminate(TerminationReason.GUARDRAIL, "blocked")

        rt = AgentRuntime(BudgetConfig())
        ctx = rt.run(_request(), Trace(), {Phase.PERCEIVE: perceive})
        self.assertTrue(ctx.terminated)
        self.assertEqual(ctx.reason, TerminationReason.GUARDRAIL)

    def test_step_budget_terminates(self):
        # A handler that never terminates would loop; step ceiling stops it.
        def loop(ctx):
            return Phase.PERCEIVE  # bounce back to itself
        rt = AgentRuntime(BudgetConfig(max_steps=3))
        ctx = rt.run(_request(), Trace(), {Phase.PERCEIVE: loop})
        self.assertEqual(ctx.reason, TerminationReason.BUDGET)

    def test_tool_call_budget_terminates(self):
        def act(ctx):
            ctx.add_tool_calls(999)  # exceeds budget -> BudgetExceeded
            return Phase.OBSERVE
        rt = AgentRuntime(BudgetConfig(max_tool_calls=5))
        ctx = rt.run(_request(), Trace(), {Phase.PERCEIVE: lambda c: Phase.ACT, Phase.ACT: act})
        self.assertEqual(ctx.reason, TerminationReason.BUDGET)

    def test_snapshot_is_checkpointable(self):
        ctx = RunContext(request=_request(), trace=Trace(), budgets=BudgetConfig())
        ctx.blackboard["scored"] = [1, 2]
        snap = ctx.snapshot()
        self.assertIn("scored", snap["blackboard_keys"])
        self.assertEqual(snap["request_id"], ctx.request.request_id)


class TestToolRegistry(unittest.TestCase):
    def setUp(self):
        self.corpus = build_corpus()
        self.registry = ToolRegistry([CorpusConnector(MANIFESTS[s], self.corpus) for s in MANIFESTS])

    def test_dynamic_selection_prefers_relevant(self):
        trace = Trace()
        selected = self.registry.select("helm chart replicas values", 3, trace)
        names = [t.manifest.name for t in selected]
        self.assertIn("helm-charts", names)

    def test_selection_bounded_by_budget(self):
        selected = self.registry.select("deploy payment service", 4, Trace())
        self.assertLessEqual(len(selected), 4)

    def test_gather_skips_failing_tool_without_cascading(self):
        good = CorpusConnector(MANIFESTS[Source.GITHUB], self.corpus)
        bad = FailingConnector(ToolManifest(name="broken", source=Source.SLACK, description="x"))
        reg = ToolRegistry([good, bad])
        result = reg.gather([good, bad], "deploy payment-service to production", 4, Trace())
        self.assertTrue(result.documents)                       # good tool still returned docs
        self.assertTrue(any("broken" in w for w in result.warnings))  # failure recorded, not raised

    def test_gather_times_out_slow_tool(self):
        slow = SlowConnector(
            ToolManifest(name="slow", source=Source.SLACK, description="x", timeout_s=0.05),
            delay_s=0.4,
        )
        result = ToolRegistry([slow]).gather([slow], "deploy", 4, Trace())
        self.assertTrue(any("timed out" in w for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
