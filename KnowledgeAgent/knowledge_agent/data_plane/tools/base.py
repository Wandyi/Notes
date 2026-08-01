"""Standardized tool interface + manifest.

Every connector is a `Tool` with a declarative `ToolManifest` (name,
description, the source it federates, and keywords used for dynamic selection).
The manifest is the MCP-style contract: a clear, machine-readable description so
the orchestrator can reason about which tools to load without stuffing every
schema into the model context. Poor manifests confuse selection, so keywords
and descriptions are first-class.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ...core import Document, Source


@dataclass
class ToolManifest:
    name: str
    source: Source
    description: str
    keywords: list[str] = field(default_factory=list)
    # Declared reliability characteristics used by the runtime's error handling.
    timeout_s: float = 5.0


class ToolError(Exception):
    """Raised by a connector on failure; the runtime degrades gracefully."""


class Tool(Protocol):
    manifest: ToolManifest

    def fetch(self, query: str, limit: int) -> list[Document]:
        """Return documents relevant to `query` from this tool's source."""
        ...
