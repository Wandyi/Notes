import unittest
from datetime import datetime, timedelta, timezone

from knowledge_agent.config import BudgetConfig, ModelTier
from knowledge_agent.control_plane.cost import CostAccountant, CostExceeded, price_call
from knowledge_agent.control_plane.rbac import RBAC
from knowledge_agent.control_plane.registry import (
    AgentRegistry, LifecycleState, RegistryError,
)
from knowledge_agent.core import Principal, Source

NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


class TestRBAC(unittest.TestCase):
    def setUp(self):
        self.rbac = RBAC()

    def test_engineer_gets_all_sources(self):
        d = self.rbac.evaluate(Principal("u", "t", ["engineer"]))
        self.assertTrue(d.allowed)
        self.assertEqual(d.allowed_sources, set(Source))

    def test_contractor_denied_slack_and_jira(self):
        d = self.rbac.evaluate(Principal("u", "t", ["contractor"]))
        self.assertTrue(d.allowed)
        self.assertNotIn(Source.SLACK, d.allowed_sources)
        self.assertNotIn(Source.JIRA, d.allowed_sources)
        self.assertIn(Source.RUNBOOK, d.allowed_sources)

    def test_unknown_role_denied(self):
        d = self.rbac.evaluate(Principal("u", "t", ["marketing"]))
        self.assertFalse(d.allowed)

    def test_no_roles_denied(self):
        self.assertFalse(self.rbac.evaluate(Principal("u", "t", [])).allowed)

    def test_multiple_roles_union_sources(self):
        d = self.rbac.evaluate(Principal("u", "t", ["support", "contractor"]))
        self.assertTrue(d.allowed)
        # union is broader than support-only
        self.assertIn(Source.GITHUB, d.allowed_sources)


class TestCostAccounting(unittest.TestCase):
    def setUp(self):
        self.tier = ModelTier("t", "m", input_per_1k=0.001, output_per_1k=0.005)

    def test_pricing(self):
        self.assertAlmostEqual(price_call(self.tier, 1000, 1000), 0.006, places=6)

    def test_charge_accumulates(self):
        acct = CostAccountant(BudgetConfig())
        acct.open_request("r1", "tenant")
        acct.charge("r1", self.tier, 1000, 200)
        acct.charge("r1", self.tier, 500, 100)
        rc = acct.request_cost("r1")
        self.assertEqual(rc.llm_calls, 2)
        self.assertEqual(rc.input_tokens, 1500)

    def test_per_request_budget_enforced(self):
        acct = CostAccountant(BudgetConfig(max_cost_usd_per_request=0.01))
        acct.open_request("r1", "tenant")
        with self.assertRaises(CostExceeded):
            acct.charge("r1", self.tier, 100000, 100000)

    def test_per_tenant_daily_budget_enforced(self):
        acct = CostAccountant(BudgetConfig(max_cost_usd_per_request=100.0,
                                           max_cost_usd_per_tenant_day=0.02))
        acct.open_request("r1", "tenant")
        acct.charge("r1", self.tier, 1000, 1000)  # 0.006
        acct.open_request("r2", "tenant")
        with self.assertRaises(CostExceeded):
            acct.charge("r2", self.tier, 5000, 5000)  # would exceed daily cap

    def test_tenant_isolation(self):
        acct = CostAccountant(BudgetConfig())
        acct.open_request("r1", "a")
        acct.charge("r1", self.tier, 1000, 1000)
        self.assertGreater(acct.tenant_day_cost("a"), 0)
        self.assertEqual(acct.tenant_day_cost("b"), 0.0)


class TestAgentRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = AgentRegistry(promotion_threshold=0.7, staleness_days=90.0)

    def test_registration_requires_owner(self):
        with self.assertRaises(RegistryError):
            self.reg.register("a", owner="", capabilities=[])

    def test_lifecycle_happy_path(self):
        rec = self.reg.register("a", "owner@x", ["rag"])
        self.assertEqual(rec.state, LifecycleState.REGISTERED)
        self.reg.transition(rec.id, LifecycleState.EVALUATING)
        self.reg.record_evaluation(rec.id, 0.85)
        self.reg.transition(rec.id, LifecycleState.STAGED)
        self.reg.promote(rec.id)
        self.assertEqual(self.reg.get(rec.id).state, LifecycleState.PRODUCTION)

    def test_illegal_transition_rejected(self):
        rec = self.reg.register("a", "owner@x", ["rag"])
        with self.assertRaises(RegistryError):
            self.reg.transition(rec.id, LifecycleState.PRODUCTION)  # skip stages

    def test_promotion_gated_on_eval_score(self):
        rec = self.reg.register("a", "owner@x", ["rag"])
        self.reg.transition(rec.id, LifecycleState.EVALUATING)
        self.reg.record_evaluation(rec.id, 0.5)  # below threshold
        self.reg.transition(rec.id, LifecycleState.STAGED)
        with self.assertRaises(RegistryError):
            self.reg.promote(rec.id)

    def test_staleness_detection(self):
        rec = self.reg.register("a", "owner@x", ["rag"])
        self.assertTrue(self.reg.is_stale(rec.id, NOW))  # never evaluated
        self.reg.record_evaluation(rec.id, 0.9)
        # Age the evaluation past the window by querying a future 'now'.
        future = NOW + timedelta(days=200)
        self.assertTrue(self.reg.is_stale(rec.id, future))
        self.assertFalse(self.reg.is_stale(rec.id, NOW))

    def test_retired_is_terminal(self):
        rec = self.reg.register("a", "owner@x", ["rag"])
        self.reg.transition(rec.id, LifecycleState.RETIRED)
        with self.assertRaises(RegistryError):
            self.reg.transition(rec.id, LifecycleState.EVALUATING)


if __name__ == "__main__":
    unittest.main()
