"""Cost accounting — per-agent and per-tenant, built in (not bolted on).

Multi-agent fan-out and orchestrator overhead can turn a $0.50 test workflow
into a five-figure monthly bill. Every LLM call is priced from the model tier
and attributed to a tenant; per-request and per-tenant/day budgets are enforced
so runaway loops are contained. `CostExceeded` is raised when a budget is hit.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..config import BudgetConfig, ModelTier
from ..core import utcnow


class CostExceeded(Exception):
    pass


@dataclass
class RequestCost:
    request_id: str
    tenant_id: str
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0


def price_call(tier: ModelTier, input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1000.0) * tier.input_per_1k + (output_tokens / 1000.0) * tier.output_per_1k


class CostAccountant:
    def __init__(self, budgets: BudgetConfig) -> None:
        self.budgets = budgets
        self._per_request: dict[str, RequestCost] = {}
        # (tenant_id, YYYY-MM-DD) -> usd
        self._per_tenant_day: dict[tuple[str, str], float] = defaultdict(float)

    def open_request(self, request_id: str, tenant_id: str) -> RequestCost:
        rc = RequestCost(request_id=request_id, tenant_id=tenant_id)
        self._per_request[request_id] = rc
        return rc

    def _today(self) -> str:
        return utcnow().strftime("%Y-%m-%d")

    def charge(self, request_id: str, tier: ModelTier, input_tokens: int, output_tokens: int) -> float:
        rc = self._per_request[request_id]
        cost = price_call(tier, input_tokens, output_tokens)

        projected_request = rc.usd + cost
        key = (rc.tenant_id, self._today())
        projected_tenant = self._per_tenant_day[key] + cost

        if projected_request > self.budgets.max_cost_usd_per_request:
            raise CostExceeded(
                f"request budget exceeded: ${projected_request:.4f} > "
                f"${self.budgets.max_cost_usd_per_request:.4f}"
            )
        if projected_tenant > self.budgets.max_cost_usd_per_tenant_day:
            raise CostExceeded(
                f"tenant daily budget exceeded for {rc.tenant_id}: "
                f"${projected_tenant:.2f} > ${self.budgets.max_cost_usd_per_tenant_day:.2f}"
            )

        rc.usd = projected_request
        rc.input_tokens += input_tokens
        rc.output_tokens += output_tokens
        rc.llm_calls += 1
        self._per_tenant_day[key] = projected_tenant
        return cost

    def request_cost(self, request_id: str) -> RequestCost:
        return self._per_request[request_id]

    def tenant_day_cost(self, tenant_id: str) -> float:
        return self._per_tenant_day[(tenant_id, self._today())]
