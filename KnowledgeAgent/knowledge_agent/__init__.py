"""KnowledgeAgent — a RAG-based enterprise knowledge assistant.

Federates engineering/operational questions across GitHub, Helm, Terraform,
Slack, Jira, Runbooks, Kubernetes, Architecture docs, and Confluence; reasons
over the results; resolves conflicts; and returns a single cited answer.

Cleanly separates a governed control plane from a data plane. See docs/ for the
architecture and flow diagrams.
"""
from .assistant import KnowledgeAssistant
from .config import Config, DEFAULT_CONFIG
from .core import Answer, Principal, QueryRequest, Source
from .seed.corpus import build_corpus

__all__ = [
    "KnowledgeAssistant",
    "Config",
    "DEFAULT_CONFIG",
    "Answer",
    "Principal",
    "QueryRequest",
    "Source",
    "build_corpus",
]
