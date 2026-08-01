"""Observability — the layer every other layer depends on.

Every LLM call, tool invocation, retrieval, guardrail check, and state
transition emits a span into a `Trace`. Without this, a bad answer is
undebuggable: you can't tell whether the prompt, the retrieval, or a tool was
at fault. Traces are also what the evaluator reads to score the trajectory.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..core import new_id, utcnow


@dataclass
class Span:
    id: str
    name: str
    kind: str                      # "retrieval" | "llm" | "tool" | "guardrail" | "state" | "orchestration"
    start_ts: float
    end_ts: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        if self.end_ts is None:
            return 0.0
        return (self.end_ts - self.start_ts) * 1000.0


@dataclass
class Trace:
    trace_id: str = field(default_factory=lambda: new_id("trace"))
    request_id: str | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    spans: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, kind: str, **attributes: Any) -> Iterator[Span]:
        sp = Span(id=new_id("span"), name=name, kind=kind, start_ts=time.perf_counter(), attributes=dict(attributes))
        self.spans.append(sp)
        try:
            yield sp
        except Exception as exc:  # record the failure, then re-raise
            sp.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            sp.end_ts = time.perf_counter()

    def event(self, name: str, kind: str, **attributes: Any) -> Span:
        """Record a zero-duration point event (a decision, a routing choice)."""
        now = time.perf_counter()
        sp = Span(id=new_id("span"), name=name, kind=kind, start_ts=now, end_ts=now, attributes=dict(attributes))
        self.spans.append(sp)
        return sp

    def spans_of_kind(self, kind: str) -> list[Span]:
        return [s for s in self.spans if s.kind == kind]

    def summary(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "span_count": len(self.spans),
            "total_ms": round(sum(s.duration_ms for s in self.spans), 2),
            "errors": [s.error for s in self.spans if s.error],
            "by_kind": {
                kind: len(self.spans_of_kind(kind))
                for kind in {s.kind for s in self.spans}
            },
        }
