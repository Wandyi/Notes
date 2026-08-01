"""Audit log — full traceability of agent actions.

Append-only record of every governance-relevant event (access decisions, tool
usage, refusals, answers, cost). This is the hard requirement in regulated
domains and the substrate for the EU AI Act logging/human-oversight
obligations. In production this ships to an immutable sink (WORM store / SIEM).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core import new_id, utcnow


@dataclass
class AuditEvent:
    id: str
    ts: str
    event_type: str
    request_id: str | None
    user_id: str | None
    tenant_id: str | None
    detail: dict[str, Any] = field(default_factory=dict)


class AuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(self, event_type: str, request_id: str | None = None,
               user_id: str | None = None, tenant_id: str | None = None,
               **detail: Any) -> AuditEvent:
        ev = AuditEvent(
            id=new_id("audit"),
            ts=utcnow().isoformat(),
            event_type=event_type,
            request_id=request_id,
            user_id=user_id,
            tenant_id=tenant_id,
            detail=detail,
        )
        self._events.append(ev)
        return ev

    def events(self, request_id: str | None = None) -> list[AuditEvent]:
        if request_id is None:
            return list(self._events)
        return [e for e in self._events if e.request_id == request_id]

    def __len__(self) -> int:
        return len(self._events)
