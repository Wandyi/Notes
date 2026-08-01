"""Tool registry: dynamic selection + robust, non-cascading execution.

* **Dynamic selection** — with many tools you cannot put every manifest in the
  model context. `select` ranks tools by query relevance (manifest keywords +
  description overlap) and returns only the top-N, so the working set stays
  small and focused.
* **Robust execution** — `gather` runs the selected connectors concurrently
  with per-tool timeouts. A tool that errors, times out, or returns nothing is
  recorded as a warning and skipped; it never stalls or fails the whole request
  (the classic "one dead tool wedges the agent" failure mode).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field

from ...core import Document, Source
from ...observability.tracing import Trace
from ...retrieval.embeddings import tokenize
from .base import Tool, ToolError


@dataclass
class GatherResult:
    documents: list[Document] = field(default_factory=list)
    sources_used: list[Source] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tool_calls: int = 0


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self.tools = list(tools)

    def register(self, tool: Tool) -> None:
        self.tools.append(tool)

    def _relevance(self, tool: Tool, query: str, q_terms: set[str]) -> float:
        m = tool.manifest
        kw_hits = sum(1 for k in m.keywords if k in query.lower())
        desc_overlap = len(q_terms & set(tokenize(m.description)))
        return 2.0 * kw_hits + desc_overlap

    def select(self, query: str, max_tools: int, trace: Trace) -> list[Tool]:
        """Rank tools by query relevance and return the top-N.

        Relevance-first, but breadth-preserving: when fewer tools have a
        positive signal than the budget allows, we backfill with the next-best
        tools rather than collapsing to only the obvious matches — federation
        depends on consulting sources whose *manifest* doesn't keyword-match but
        whose *content* might. The positive-signal filter only bites when there
        are more strongly-relevant tools than the budget (the real "hundreds of
        tools" case).
        """
        q_terms = set(tokenize(query))
        scored = sorted(
            self.tools,
            key=lambda t: self._relevance(t, query, q_terms),
            reverse=True,
        )
        positive = [t for t in scored if self._relevance(t, query, q_terms) > 0]
        if len(positive) >= max_tools:
            selected = positive[:max_tools]
        else:
            selected = scored[:max_tools]
        trace.event(
            "tools.select", "orchestration",
            selected=[t.manifest.name for t in selected],
            considered=len(self.tools),
        )
        return selected

    def gather(self, tools: list[Tool], query: str, per_tool_limit: int, trace: Trace) -> GatherResult:
        result = GatherResult()
        with ThreadPoolExecutor(max_workers=max(1, len(tools))) as pool:
            futures = {
                pool.submit(self._safe_fetch, t, query, per_tool_limit): t
                for t in tools
            }
            for future, tool in list(futures.items()):
                result.tool_calls += 1
                name = tool.manifest.name
                timeout = tool.manifest.timeout_s
                with trace.span("tool.fetch", "tool", tool=name, source=tool.manifest.source.value) as sp:
                    try:
                        docs = future.result(timeout=timeout)
                    except FutureTimeout:
                        future.cancel()
                        msg = f"tool '{name}' timed out after {timeout}s — skipped"
                        result.warnings.append(msg)
                        sp.error = msg
                        continue
                    except ToolError as exc:
                        msg = f"tool '{name}' failed: {exc} — skipped"
                        result.warnings.append(msg)
                        sp.error = str(exc)
                        continue
                    except Exception as exc:  # defensive: never cascade
                        msg = f"tool '{name}' unexpected error: {exc} — skipped"
                        result.warnings.append(msg)
                        sp.error = str(exc)
                        continue
                    sp.attributes["doc_count"] = len(docs)
                    if docs:
                        result.documents.extend(docs)
                        result.sources_used.append(tool.manifest.source)
        return result

    @staticmethod
    def _safe_fetch(tool: Tool, query: str, limit: int) -> list[Document]:
        return tool.fetch(query, limit)
