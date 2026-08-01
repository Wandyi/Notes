"""Role-based access control.

Enforced centrally in the control plane: which roles may invoke the agent at
all, and which knowledge sources each role may read. This is where the "improper
data exposure" risk is contained — a contractor role, for example, is denied the
Slack and Jira sources even though the connectors exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core import Principal, Source


@dataclass
class RolePolicy:
    can_query: bool
    allowed_sources: set[Source]


@dataclass
class AccessDecision:
    allowed: bool
    allowed_sources: set[Source]
    reason: str | None = None


_ALL = set(Source)


class RBAC:
    def __init__(self, policies: dict[str, RolePolicy] | None = None) -> None:
        self.policies: dict[str, RolePolicy] = policies or {
            "admin": RolePolicy(can_query=True, allowed_sources=set(_ALL)),
            "engineer": RolePolicy(can_query=True, allowed_sources=set(_ALL)),
            "sre": RolePolicy(can_query=True, allowed_sources=set(_ALL)),
            "support": RolePolicy(
                can_query=True,
                allowed_sources={Source.RUNBOOK, Source.CONFLUENCE, Source.ARCH_DOCS},
            ),
            "contractor": RolePolicy(
                can_query=True,
                # Contractors cannot read internal chatter or ticketing.
                allowed_sources=_ALL - {Source.SLACK, Source.JIRA},
            ),
        }

    def evaluate(self, principal: Principal) -> AccessDecision:
        if not principal.roles:
            return AccessDecision(False, set(), "principal has no roles assigned")

        can_query = False
        allowed: set[Source] = set()
        known = False
        for role in principal.roles:
            policy = self.policies.get(role)
            if policy is None:
                continue
            known = True
            can_query = can_query or policy.can_query
            allowed |= policy.allowed_sources

        if not known:
            return AccessDecision(False, set(), f"no known role among {principal.roles}")
        if not can_query:
            return AccessDecision(False, set(), "roles do not permit querying")
        return AccessDecision(True, allowed)
