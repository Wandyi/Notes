"""Source connectors.

Each connector federates one enterprise source (GitHub, Helm, Terraform, Slack,
Jira, Runbooks, Kubernetes, Architecture docs, Confluence). In production these
wrap real APIs; here `CorpusConnector` serves documents from an in-memory seed
corpus filtered to its source, doing a cheap lexical pre-filter so it returns
plausibly relevant docs for the query (mirroring a source-side search API).

`FailingConnector` and `SlowConnector` exist to exercise the runtime's
tool-error fallback paths in tests.
"""
from __future__ import annotations

import time

from ...core import Document, Source
from ..tools.base import Tool, ToolError, ToolManifest
from ...retrieval.embeddings import tokenize


class CorpusConnector:
    """A connector backed by the shared seed corpus, filtered to one source."""

    def __init__(self, manifest: ToolManifest, corpus: list[Document]) -> None:
        self.manifest = manifest
        self._docs = [d for d in corpus if d.source == manifest.source]

    def fetch(self, query: str, limit: int) -> list[Document]:
        q_terms = set(tokenize(query))
        scored: list[tuple[float, Document]] = []
        for doc in self._docs:
            terms = set(tokenize(doc.title + " " + doc.content))
            overlap = len(q_terms & terms)
            # Keyword hint from the manifest gives a small relevance boost.
            kw_boost = sum(1 for k in self.manifest.keywords if k in query.lower())
            score = overlap + 0.5 * kw_boost
            if score > 0:
                scored.append((score, doc))
        # If nothing matched lexically, still return a couple docs from the source
        # (the downstream hybrid retriever + reranker will filter precisely).
        if not scored and self._docs:
            return self._docs[:limit]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _s, d in scored[:limit]]


class FailingConnector:
    """Always raises — used to prove a single tool failure does not cascade."""

    def __init__(self, manifest: ToolManifest) -> None:
        self.manifest = manifest

    def fetch(self, query: str, limit: int) -> list[Document]:
        raise ToolError(f"{self.manifest.name} backend unavailable")


class SlowConnector:
    """Sleeps past its declared timeout — used to test timeout handling."""

    def __init__(self, manifest: ToolManifest, delay_s: float) -> None:
        self.manifest = manifest
        self.delay_s = delay_s

    def fetch(self, query: str, limit: int) -> list[Document]:
        time.sleep(self.delay_s)
        return []


# --- Manifests for the nine federated sources ------------------------------

MANIFESTS: dict[Source, ToolManifest] = {
    Source.GITHUB: ToolManifest(
        name="github-search", source=Source.GITHUB,
        description="Source code, READMEs, CI config, and deployment scripts in GitHub repos.",
        keywords=["code", "repo", "ci", "workflow", "deploy", "build", "makefile", "readme"],
    ),
    Source.HELM: ToolManifest(
        name="helm-charts", source=Source.HELM,
        description="Helm chart values, templates, and release configuration for services.",
        keywords=["helm", "chart", "values", "replicas", "image", "release"],
    ),
    Source.TERRAFORM: ToolManifest(
        name="terraform-state", source=Source.TERRAFORM,
        description="Terraform modules and state describing cloud infra, IAM, and networking.",
        keywords=["terraform", "infra", "iam", "network", "vpc", "module", "provisioning"],
    ),
    Source.SLACK: ToolManifest(
        name="slack-history", source=Source.SLACK,
        description="Team discussions, incident chatter, and tribal knowledge in Slack.",
        keywords=["slack", "discussion", "incident", "asked", "thread", "channel"],
    ),
    Source.JIRA: ToolManifest(
        name="jira-issues", source=Source.JIRA,
        description="Jira tickets: bugs, changes, incidents, and release tracking.",
        keywords=["jira", "ticket", "issue", "bug", "change", "release", "epic"],
    ),
    Source.RUNBOOK: ToolManifest(
        name="runbooks", source=Source.RUNBOOK,
        description="Operational runbooks and on-call procedures for services.",
        keywords=["runbook", "on-call", "procedure", "rollback", "deploy", "restart", "oncall"],
    ),
    Source.KUBERNETES: ToolManifest(
        name="kubernetes-live", source=Source.KUBERNETES,
        description="Live Kubernetes manifests, deployments, and cluster state.",
        keywords=["kubernetes", "k8s", "pod", "deployment", "namespace", "manifest", "cluster"],
    ),
    Source.ARCH_DOCS: ToolManifest(
        name="architecture-docs", source=Source.ARCH_DOCS,
        description="Architecture decision records and system design docs.",
        keywords=["architecture", "design", "adr", "diagram", "component", "system"],
    ),
    Source.CONFLUENCE: ToolManifest(
        name="confluence-wiki", source=Source.CONFLUENCE,
        description="Confluence wiki pages: onboarding, standards, and how-to guides.",
        keywords=["confluence", "wiki", "guide", "howto", "standard", "onboarding"],
    ),
}


def build_default_connectors(corpus: list[Document]) -> list[Tool]:
    return [CorpusConnector(MANIFESTS[src], corpus) for src in MANIFESTS]
