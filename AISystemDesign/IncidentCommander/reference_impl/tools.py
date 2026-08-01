"""
Investigation tool layer (reference implementation).

The 16 agents are READ-ONLY connectors behind a uniform, MCP-style manifest
(docs/05-tool-integration.md). This module encodes:

  * ToolManifest         -- the declarative contract used for discovery/selection.
  * InvestigationAgent   -- the single-method interface every agent implements.
  * ToolRegistry.select  -- dynamic selection (can't context-stuff 16 schemas).
  * ToolRegistry.gather  -- concurrent invoke with per-agent timeout + error
                            isolation; failures become `degraded` notes, never crashes.

The read/write firewall is structural: a manifest CANNOT declare a write
capability -- there is no such value. The only writer is the Executor
(docs/11), reachable only via a signed ExecutionGrant.
"""
from __future__ import annotations

import concurrent.futures as cf
from dataclasses import dataclass, field
from typing import Callable, Protocol

from contracts import Evidence, Principal


# --------------------------------------------------------------------------- #
# Manifest + interface
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ToolManifest:
    name: str                       # "prometheus"
    description: str                # used by the LLM relevance ranker
    capabilities: tuple[str, ...]   # e.g. ("range_query", "instant_query") -- all READ
    timeout_ms: int = 3000
    max_output_bytes: int = 64_000  # bounded evidence
    cost_hint: float = 1.0          # relative latency/$ prior
    # NOTE: there is deliberately no `writes` field. Investigation tools cannot mutate.


@dataclass
class Query:
    """A typed query built by the Planner/Collector -- never free text from a log line."""
    intent: str                     # "recent_error_rate", "logs_around", ...
    params: dict[str, str] = field(default_factory=dict)


class InvestigationAgent(Protocol):
    def manifest(self) -> ToolManifest: ...
    def invoke(self, query: Query, principal: Principal) -> list[Evidence]: ...


# --------------------------------------------------------------------------- #
# Registry: dynamic selection + resilient concurrent gather
# --------------------------------------------------------------------------- #
@dataclass
class GatherResult:
    evidence: list[Evidence]
    degraded_agents: list[str]      # timed-out / failed -> lower confidence, not a crash


class ToolRegistry:
    def __init__(self, agents: dict[str, InvestigationAgent]) -> None:
        self._agents = agents

    def manifests(self) -> list[ToolManifest]:
        return [a.manifest() for a in self._agents.values()]

    def select(
        self,
        priors: list[str],
        dependency_agents: list[str],
        relevance_rank: Callable[[list[ToolManifest]], list[str]],
        k: int,
    ) -> list[str]:
        """Merge deterministic priors + dependency expansion + LLM relevance; top-k.

        k is a function of severity (docs/03). Only selected agents' schemas ever
        enter a reasoning context.
        """
        ranked = relevance_rank(self.manifests())
        ordered: list[str] = []
        for name in [*priors, *dependency_agents, *ranked]:
            if name in self._agents and name not in ordered:
                ordered.append(name)
        return ordered[:k]

    def gather(
        self,
        selected: list[str],
        query_for: Callable[[str], Query],
        principal: Principal,
    ) -> GatherResult:
        """Concurrent invoke with per-agent timeout. One flaky source never cascades."""
        evidence: list[Evidence] = []
        degraded: list[str] = []

        with cf.ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
            futures = {}
            for name in selected:
                agent = self._agents[name]
                timeout_s = agent.manifest().timeout_ms / 1000.0
                fut = pool.submit(agent.invoke, query_for(name), principal)
                futures[fut] = (name, timeout_s)

            for fut in list(futures):
                name, timeout_s = futures[fut]
                try:
                    evidence.extend(fut.result(timeout=timeout_s))
                except cf.TimeoutError:
                    degraded.append(name)          # timeline note, not an exception
                except Exception:                  # noqa: BLE001 - isolate any connector fault
                    degraded.append(name)

        return GatherResult(evidence=evidence, degraded_agents=degraded)
