# 15 — Observability, SLOs and the Admin Dashboard

A RAG system fails in three different ways, and each needs a different kind of instrument.

- **Loud failures**: errors, timeouts, a component down. Request metrics catch them within a minute.
- **Quiet failures**: recall drifting down after a model upgrade, the verifier dropping more claims
  in Hindi, purges falling behind, an embedding model changing underneath you. No request fails.
  These are caught only by metrics designed for them, compared over days.
- **Silent wrong answers**: a fluent, cited, unsupported claim. Nothing in the request path knows it
  happened. Only the evaluation programme (doc [14](14-evaluation.md) §6) finds these.

This document covers the SLOs, the traces, the metrics that catch quiet failures, and the admin
dashboard the requirements ask for — which turns out to need more access control thinking than any
other screen in the system. It assumes the vocabulary of
[`../Observability`](../Observability/README.md) (RED, USE, SLIs, error budgets) and applies it
rather than re-teaching it.

---

## 1. Service level indicators, objectives and budgets

An **SLI** is a measured quantity (the share of queries answered within 3 seconds). An **SLO** is the
target for it (99% of them). The **error budget** is how much failure the target allows — and
spending it is what tells you whether to ship features or fix reliability. The background is in
[`../Observability/04-slis-slos-and-error-budgets.md`](../Observability/04-slis-slos-and-error-budgets.md).

| SLI | SLO | Window | Budget at Kestrel's volume |
|---|---|---|---|
| **Availability**: queries ending in Verified, Partial, Extractive, Abstained or CacheHit within 3 s, out of all valid queries (doc 16 §2) | 99.9% | 30 days | 500 failed queries a day; **15,000 a month** |
| **Latency**: queries complete within 1.5 s | 50% | 7 days | — |
| **Latency**: queries complete within 3 s | 99% | 7 days | 5,000 slow queries a day |
| **Deletion freshness**: deletions unretrievable within 300 s | Every deletion | 30 days | **None.** 660,000 deletions a month; each late one is an incident |
| **Revocation freshness**: directory change enforced within 300 s | 99.9% | 30 days | |
| **Grounding**: M1, unsupported claims, weekly 95% upper bound | ≤ 0.10% | 7 days | About 9,500 of the week's 9.5 million claims |
| **Abstention**: M3 and M4 on the golden set | ≥ 95% and ≤ 5% | Per release | — |

Two of these rows are unusual for an SRE team, and worth explaining.

**Grounding is an SLO with a budget.** M1 is measured weekly from the production sample. If the
weekly upper bound exceeds 0.10%, the budget is exhausted and the same rule applies as for
availability: changes that could affect answer quality — model upgrades, prompt changes, new
sources — are frozen until it recovers. This is what makes the published hallucination number from
doc [00](00-requirements-and-what-zero-hallucination-can-mean.md) §7 operational rather than a
report nobody acts on.

**Deletion freshness has no budget.** For most SLOs, a few misses are acceptable, and the budget says
how many. A deletion that was not purged in time may be a legal matter for a specific person — a
privacy request, a retention obligation — so the target is every deletion, and each miss is
investigated as an incident.

### Burn-rate alerts

Alerting on "availability below 99.9%" fires too late for fast outages and too often for noise. Lodestar
alerts on **burn rate** — how fast the budget is being consumed — using two windows each:

| Alert | Condition | Meaning |
|---|---|---|
| Page | Burn rate above 14.4× over 1 hour **and** over 5 minutes | Consuming 2% of the month's budget in an hour, and still happening now |
| Page | Above 6× over 6 hours and over 30 minutes | Consuming 5% in six hours |
| Ticket | Above 1× over 3 days | On track to exhaust the budget this month |

The short window in each pair stops an alert firing for an incident that has already ended.

---

## 2. End-to-end latency is the alert; stages are the diagnosis

Doc [12](12-latency-budget-and-caching.md) §3 showed that per-stage budgets come from a simulation that
assumes independence, and that real slowness is often correlated. So **alerts are on the end-to-end
number**, per language and per caller class (human or machine). Per-stage latencies are on the dashboard,
for diagnosis once an alert fires. Paging on a single stage's p99 would page for stage slowness that
never reaches users, and miss correlated slowness that does.

Two derived numbers sit next to the latency panel because they move first:

- **Hedge rate** (normally 3.6%): when it rises, the model provider is slow, usually minutes before the
  end-to-end p99 shows it.
- **Truncation rate** (normally 0.29%, higher in Hindi): when it rises, answers are getting longer or output
  speed is falling — often a sign that a generator change made answers wordier.

---

## 3. Traces: one per query, references only

Every query produces one **trace**: a tree of timed **spans**, one per stage of the state machine (doc
[02](02-architecture-and-the-ledger.md) §6), each carrying attributes about what happened in it.

| Span | Attributes worth recording |
|---|---|
| `query` | Language, caller class, persona's permission fingerprint, outcome, registry version of every model used |
| `condense` | Whether condensed, referenced document IDs, filter changes, whether the user later edited the interpretation |
| `retrieve.vector`, `retrieve.keyword` | Candidates returned, filter selectivity, query-planner strategy chosen, shards hedged |
| `hydrate` | Candidates in, chunks out, **and the reason for each drop**: superseded, deleted, blacked out, not permitted |
| `rerank` | Top calibrated scores, number above the admission threshold |
| `admit` | Chunks admitted, tokens, sufficiency decision |
| `generate` | Input and output tokens, time to first token, hedged or not, truncated or not |
| `verify` | Claims emitted, claims accepted, and for each dropped claim **which check dropped it**; judge calls and timeouts; notes attached |
| `deliver` | Delivery-check result, notices sent |

**Traces hold references, never text.** They record chunk IDs and sentence keys, not chunk text; claim IDs
and drop reasons, not claim text. That keeps the trace store out of the deletion copy inventory (doc
[09](09-freshness-updates-and-deletion.md) §4) and out of the privacy problem in §6. A **1% debug sample**
keeps full prompts, outputs and dropped-claim text, with a 7-day TTL, indexed by document ID so deletions
reach it, and readable only by the evaluation role.

### Cardinality

Document IDs, chunk IDs and user IDs belong in **traces**, never as **metric labels**. A metric labelled by
document ID would create ten million time series. Metrics are labelled by language, caller class, outcome,
lane, source, store and check name — tens of values each. (See
[`../Observability/05-instrumentation-and-cardinality.md`](../Observability/05-instrumentation-and-cardinality.md).)

---

## 4. Metrics that catch quiet failures

Each row is a metric whose movement points at a specific cause. Most have no fixed threshold; they are
alerted on a sudden change relative to the previous two weeks.

| Metric | Normal | What a change usually means |
|---|---|---|
| Hydration drops, by reason | ~1% of candidates | "Not permitted" rising: index ACLs lagging. "Superseded" or "deleted" rising: purges behind (doc 17 `F-02`) |
| Claims dropped, by check and language | ~4.9% of claims | Quantity drops jumping after a generator upgrade: the new model rounds or reformats numbers. Entailment drops jumping: verifier and generator have drifted apart |
| Partial-answer rate, by language | ~15% of generative answers | The visible face of the line above |
| Gray-zone rate and judge timeout rate | ~18% of answers; under 0.5% | Timeouts rising: the judge is overloaded, and claims are being dropped because of it |
| Abstention rate, by language | ~6% | A rise in one language only: retrieval broke for that language — an analyzer, the translation model, an OCR change |
| Distribution of the top calibrated reranker score, by language | Stable | A shift with no change deployed: an upstream model changed, or the corpus shifted (doc 17 `X-05`) |
| Nightly ANN recall at k = 50 | ~98.6% | Falling: compaction backlog or a bad parameter change |
| Segment tombstone ratio | Under 10% | Rising: compaction is behind (doc 06 §5) |
| Lifecycle consumer lag; oldest unconfirmed deletion | Seconds; under 120 s | Purge workers falling behind |
| Answer cache hit rate; validation-miss rate | ~10%; low | Validation misses rising: many cited documents are changing — usually a bulk re-version |
| Interpretation edit rate | ~1.9% of follow-ups | Condenser regression |
| Connector detection delay, per source | Seconds, or the poll interval | Source-side problem; not Lodestar's SLO, but users experience it |

To measure drift in the score distribution, Lodestar uses the **population stability index** — a standard
measure of how much one distribution has shifted against another — computed daily on the top-1 calibrated score
per language against the previous four weeks. It needs no labels, which is why it catches upstream changes the
golden set would only catch at the next release.

---

## 5. The admin dashboard: ingestion

Requirement F8 asks for an admin dashboard for ingestion jobs and query logs. The ingestion half is
straightforward, once it is organised around the questions administrators actually ask.

**"Is ingestion keeping up?"** — per lane and per source: queued work in seconds (doc
[05](05-ingestion-pipeline-and-versions.md) §5), registration-to-live percentiles, and the number of documents
currently in blackout.

**"What is failing, and why?"** — the dead-letter queue, **grouped by error code and source**, with counts and
trends: *"120 `PASSWORD_PROTECTED` from contracts, up from 40 last week"* is actionable; *"400 failures"* is not.
Each group lists sample documents and offers retry for transient codes.

**"Is the content any good?"** — parse quality per source; the share of documents in `OCR_LOW_CONFIDENCE`
(doc [04](04-parsing-and-chunking.md) §4); near-duplicate clusters; and **conflicting copies** — non-authoritative
documents that disagree with authoritative ones, listed for their owners (doc
[07](07-retrieval-reranking-and-context.md) §8).

**"What happened to this document?"** — a per-document view: every version with its stages and timings, which
version is live, its visibility and ACL, and for deletions the per-store purge proof from doc
[03](03-apis-and-data-model.md) §2.

**"What are people asking that we cannot answer?"** — the **content gaps report**: abstained questions,
clustered by topic, per department, de-identified. *"Users asked about remote-work allowances in Mexico 1,200
times this month; no document answers it."* This is the report document owners find most useful, because it
tells them what to write.

---

## 6. The admin dashboard: query logs

The query-log half needs care, because the obvious design — an administrator can see any query, its retrieved
documents and its answer — breaks two rules at once.

### Rule 1: the dashboard must not bypass document permissions

An administrator inspecting why a query from the Legal department failed would, in the obvious design, see the
retrieved Legal documents' titles and text. But the administrator may not have access to Legal's documents. The
dashboard would become the easiest way in the company to read restricted material.

So the query detail view applies **the viewer's** permissions, not the asker's. For each retrieved, admitted or
cited chunk, the viewer sees the title, locator and text only if they could read that document themselves;
otherwise they see `[restricted document — you do not have access]` with its ID. Diagnosis still works — the
viewer can see that three candidates were dropped as superseded and the answer was abstained — without exposing
content.

### Rule 2: questions are sensitive, and users are pseudonymised

From doc [13](13-access-control-security-and-privacy.md) §7: users appear as keyed pseudonyms; raw question text
is kept 30 days (7 for sensitive topics); revealing who asked requires a reason, a second approver, and an audit
entry reviewed by the privacy office.

### Who sees what

| Role | Ingestion views | Query aggregates | Query detail | Dropped-claim text | Reveal |
|---|---|---|---|---|---|
| Operations administrator | All | All | Pseudonymised; content filtered by their own permissions | No | Request only |
| Content owner (e.g. HR policy owner) | Their sources | Their documents' topics and gaps | No | No | No |
| Evaluation reviewer | No | All | De-identified samples only | **Yes**, for the 1% debug sample | No |
| Privacy officer | No | No | On approved reveal only | No | Approves |

The "dropped-claim text" column matters. The text of a claim the verifier removed is unverified model output, by
definition. It is valuable for improving the system and must never be shown as if it were an answer — so it is
visible only to the role that evaluates the verifier, inside the de-identified sample.

---

## 7. What pages, and what makes a ticket

| Condition | Severity | First place to look |
|---|---|---|
| Availability burn rate (§1) | Page | Which outcome is failing: errors, or latency over 3 s? |
| Any deletion not unretrievable at 300 s | Page | The auditor's per-store result (doc 09 §5); doc 17 `F-03` |
| Revocation not enforced at 300 s | Page | Directory notification path; permission cache (doc 13 §4) |
| Daily reconciliation finds documents that should not exist | Page | Recent restores (doc 09 §8); doc 17 `F-05` |
| Canary document stuck at any step for > 2× its normal time | Page | The step it is stuck at (doc 14 §10) |
| Weekly M1 upper bound above 0.10% | Freeze quality-affecting changes; ticket to the evaluation owner | Per-language-pair and per-check breakdown |
| Abstention or claim-drop rate shifts in one language | Ticket | That language's analyzers, translation model, OCR |
| Score-distribution drift with no deployment | Ticket, high priority | Upstream model or corpus change (doc 17 `X-05`) |
| Compaction backlog, tombstones over 15% | Ticket | Doc 06 §5 |
| Dead-letter queue group doubling week on week | Ticket to the source owner | The group's sample documents |

Every alert links to its scenario in the failure catalogue (doc [17](17-failure-catalogue.md)).

---

## 8. The weekly grounding report

Once a week, the product owner, the HR and Legal owners and the engineering leads receive one page:

- **M1** — unsupported claims, with its 95% interval, overall and per language pair and per document class, against
  the 0.10% target, and the week's trend;
- **M2, M3, M4** from the latest golden-set run;
- **The outcome mix** — Verified, Partial, Extractive, Abstained, CacheHit — per language;
- **The top five reasons claims were dropped**, with an example of each (de-identified);
- **Freshness** — deletion and revocation SLO attainment, blackout count, slowest connector;
- **Changes shipped** that week, with their golden-set deltas.

This is the published number promised in doc [00](00-requirements-and-what-zero-hallucination-can-mean.md) §7. Its
value is less in any single week's figure than in the fact that it exists, is the same shape every week, and is read
by the people who own the documents the answers come from.

---

## What to take away

1. **Instrument for three kinds of failure.** Request metrics catch loud failures; drift metrics catch quiet ones;
   only the evaluation programme catches silent wrong answers.
2. **Make grounding an SLO with a budget.** If weekly M1 exceeds 0.10%, quality-affecting changes freeze, exactly as
   for an availability budget.
3. **Alert on end-to-end latency per language; use stage latencies to diagnose.**
4. **Record every drop with its reason** — in hydration and in verification. The reason codes are the most diagnostic
   data in the system.
5. **Traces hold references, never text**, with a small, short-lived, access-controlled debug sample.
6. **Watch score distributions without labels.** A shift with no deployment means something upstream changed.
7. **The admin dashboard must apply the viewer's document permissions**, or it becomes the easiest way to read
   restricted material.
8. **Show dropped-claim text only to evaluators.** It is unverified model output by definition.
9. **Publish a weekly grounding report to the people who own the documents.** The number matters less than the habit.

Continue to [16 — Availability, degradation, scaling and cost](16-availability-degradation-scaling-and-cost.md).
