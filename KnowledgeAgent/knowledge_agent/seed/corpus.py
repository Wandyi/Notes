"""Seed corpus: a small, cross-source enterprise knowledge base.

Centered on the running example "How do I deploy payment-service to
production?", with documents spread across all nine sources. It deliberately
seeds:

* a **conflict** — the (old) runbook says `replicas: 3`, while the recent Helm
  values and live Kubernetes state say `replicas: 6`. The resolver picks the
  freshest.
* a **stale** document — the runbook is >120 days old, so it is flagged stale
  and down-weighted.
* **agreement** — every source names namespace `payments-prod`, which must NOT
  be reported as a conflict.
* **noise** — an unrelated TLS-rotation doc, so retrieval has to be selective.

`updated_at` values are absolute so tests can pass a fixed `now` for
deterministic freshness/staleness.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..core import Document, Source, new_id

REFERENCE_NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def _dt(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def build_corpus() -> list[Document]:
    docs: list[Document] = []

    def add(source, title, content, url, updated_at, claims=None, metadata=None):
        docs.append(Document(
            id=new_id("doc"), source=source, title=title, content=content.strip(),
            url=url, updated_at=updated_at, claims=claims or {}, metadata=metadata or {},
        ))

    # --- GitHub -----------------------------------------------------------
    add(
        Source.GITHUB, "payment-service — README & deploy scripts",
        """
payment-service is the core payments API. To deploy payment-service to production
you run the release pipeline defined in .github/workflows/deploy.yml. The pipeline
builds the container image, pushes it to the registry, and triggers a Helm upgrade
against the payments-prod namespace. Production deploys require an approved release
ticket in Jira and a green CI run on the main branch.

The deploy script bin/deploy.sh wraps `helm upgrade payment-service ./charts/payment-service`
and waits for the rollout to become healthy before reporting success.
        """,
        "https://github.com/infra/payment-service/blob/main/README.md",
        _dt(2026, 6, 20),
        claims={"namespace": "payments-prod"},
    )
    add(
        Source.GITHUB, "payment-service CI workflow (deploy.yml)",
        """
The deploy workflow runs on tags matching v*. Steps: checkout, run unit and
integration tests, build image, push to ECR, then run the production Helm upgrade.
The workflow blocks on a manual approval gate mapped to the release manager role.
        """,
        "https://github.com/infra/payment-service/blob/main/.github/workflows/deploy.yml",
        _dt(2026, 7, 2),
    )

    # --- Helm (recent, authoritative replica count) -----------------------
    add(
        Source.HELM, "payment-service Helm chart — production values",
        """
Production values for payment-service. The chart deploys into the payments-prod
namespace. Current production configuration runs replicas: 6 behind the internal
load balancer, with a rolling update strategy (maxSurge 1, maxUnavailable 0) so
deploys are zero-downtime. The image tag is pinned per release; a production deploy
is a `helm upgrade` with the new tag.
        """,
        "https://helm.internal/infra/payment-service/values-prod.yaml",
        _dt(2026, 7, 10),
        claims={"replicas": "6", "namespace": "payments-prod"},
    )

    # --- Terraform --------------------------------------------------------
    add(
        Source.TERRAFORM, "payments infra — terraform module",
        """
The payments Terraform module provisions the RDS Postgres instance, the ECR
repository, and the IAM role payment-service-prod that the pods assume via IRSA.
The payments-prod Kubernetes namespace and its network policies are managed here.
Changing production infrastructure requires a terraform plan review and apply
through the infra pipeline.
        """,
        "https://terraform.internal/payments/main.tf",
        _dt(2026, 5, 30),
        claims={"namespace": "payments-prod"},
    )

    # --- Slack ------------------------------------------------------------
    add(
        Source.SLACK, "#payments-oncall — deploy thread",
        """
Thread from #payments-oncall: "Reminder that deploying payment-service to
production always goes through the tagged release pipeline, never a manual helm
command from a laptop. If the rollout stalls, check the approval gate first, then
the image pull. We bumped production capacity a while back."
        """,
        "https://slack.internal/archives/payments-oncall/p1720800000",
        _dt(2026, 7, 12),
    )

    # --- Jira -------------------------------------------------------------
    add(
        Source.JIRA, "PAY-1423 — Release payment-service v3.4 to production",
        """
Release ticket PAY-1423 tracks the production deploy of payment-service v3.4.
Acceptance: green CI on main, release-manager approval recorded, Helm upgrade
applied to payments-prod, and post-deploy smoke tests passing. This ticket is the
required approval artifact referenced by the deploy pipeline.
        """,
        "https://jira.internal/browse/PAY-1423",
        _dt(2026, 7, 18),
        claims={"namespace": "payments-prod"},
    )

    # --- Runbook (STALE + conflicting replica count) ----------------------
    add(
        Source.RUNBOOK, "Runbook: Deploying payment-service to production",
        """
Runbook for deploying payment-service to production. Steps: 1) confirm the release
ticket is approved, 2) ensure main is green, 3) run the tagged release pipeline
which performs the Helm upgrade into the payments-prod namespace, 4) watch the
rollout. Production runs replicas: 3 (note: verify against current Helm values,
this runbook is not always kept in sync). Roll back with `helm rollback
payment-service` if smoke tests fail.
        """,
        "https://runbooks.internal/payment-service/deploy",
        _dt(2026, 1, 5),  # >120 days before REFERENCE_NOW → stale
        claims={"replicas": "3", "namespace": "payments-prod"},
    )

    # --- Kubernetes (live state, agrees with Helm on replicas) ------------
    add(
        Source.KUBERNETES, "Live deployment: payment-service (payments-prod)",
        """
Live Kubernetes deployment payment-service in namespace payments-prod. Observed
replicas: 6 desired / 6 ready. The deployment uses a RollingUpdate strategy and is
fronted by a ClusterIP service. Deploys are applied by the Helm upgrade step of the
release pipeline; do not kubectl edit production directly.
        """,
        "https://k8s.internal/payments-prod/deployments/payment-service",
        _dt(2026, 7, 5),
        claims={"replicas": "6", "namespace": "payments-prod"},
    )

    # --- Architecture docs ------------------------------------------------
    add(
        Source.ARCH_DOCS, "ADR-017: payment-service deployment topology",
        """
ADR-017 records the deployment topology for payment-service: a stateless API
deployed to the payments-prod namespace, scaled horizontally behind an internal
load balancer, with the database provisioned out-of-band by Terraform. Releases are
immutable, tag-driven, and applied via Helm. Manual production changes are
prohibited by policy.
        """,
        "https://arch.internal/adr/017-payment-service",
        _dt(2026, 3, 1),
        claims={"namespace": "payments-prod"},
    )

    # --- Confluence -------------------------------------------------------
    add(
        Source.CONFLUENCE, "Deployment standards — production services",
        """
Company deployment standards for production services. Every production deploy must
be driven by a tagged release, gated on an approved change/release ticket, and
executed by the automated pipeline (no manual kubectl or helm from workstations).
Services deploy into their dedicated *-prod namespace. These standards apply to
payment-service and all tier-1 services.
        """,
        "https://confluence.internal/eng/deployment-standards",
        _dt(2026, 4, 15),
    )

    # --- Noise (unrelated; must be filtered out by retrieval) -------------
    add(
        Source.CONFLUENCE, "How to rotate TLS certificates for the edge proxy",
        """
This guide covers rotating TLS certificates on the edge proxy fleet. It has nothing
to do with application deploys: request a new cert from the internal CA, stage it,
and reload the proxy. Unrelated to payment-service or the release pipeline.
        """,
        "https://confluence.internal/security/tls-rotation",
        _dt(2026, 6, 1),
    )

    return docs
