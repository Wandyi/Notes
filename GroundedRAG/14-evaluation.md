# 14 — Evaluation

Every guarantee and target in this collection is a claim about behaviour, and a claim about behaviour
is only as good as the measurement behind it. This document describes how Lodestar is measured:
before any change ships, and continuously in production. It is also where several measurement traps
are made explicit — sample sizes too small to distinguish pass from fail, labels that silently break
when the chunker changes, averages that hide a failing language, and error counts that undercount
because the tool finding the errors misses some too.

The statistics are in [reference_impl/evaluation.py](reference_impl/evaluation.py), which reproduces
every number in this document:

```bash
python3 reference_impl/evaluation.py --verbose
```

---

## 1. What evaluation must answer

| Question | Measure | Target | Where |
|---|---|---|---|
| Does retrieval find the evidence? | Recall@5, admitted recall, per language | > 90% each language | Golden set |
| Does it admit only relevant evidence? | Context precision | > 80% | Golden set |
| Are claims supported? | M1: unsupported claims | ≤ 0.10% of claims, 95% upper bound | Production sample; verifier test set before release |
| Are conditions left out? | M2: material omissions | ≤ 1% of answers | Golden set exception questions |
| Does it abstain when it should? | M3: correct abstention on unanswerable questions | ≥ 95% | Golden set |
| Does it abstain when it should not? | M4: false abstention on answerable questions | ≤ 5% | Golden set |
| Does it understand follow-ups? | Interpretation accuracy | ≥ 93% | Conversation slice |
| Is it fast enough? | p50 and p99 to last verified claim | < 1,500 / < 3,000 ms | Load test; production |
| Are deletions really gone? | Time to unretrievable | < 300 s p99 | Canary documents (§10); auditor |

Some of these can be measured well before release; one — M1 — can only be measured at the precision it
needs in production. The design accounts for both.

---

## 2. The golden set

### How big it has to be

A **golden set** is a fixed collection of questions with known correct evidence, used to compare one
version of the system against another. The first question is how many questions it needs.

Suppose a golden set of 200 questions, and the new version scores 91% recall@5 — 182 of 200. Is that above
the 90% target? The 95% confidence interval for a proportion measured on 200 samples (computed with the
Wilson method, which behaves well near 0% and 100%) is:

> 91% on 200 questions → **86.2% to 94.2%**

The interval contains 90%. A 200-question set cannot say whether the system passes. Worse, two versions
that differ by two points are indistinguishable on it, so every change looks neutral.

To measure a proportion near 90% to within ±2 points with 95% confidence:

> n = 1.96² × 0.9 × 0.1 ÷ 0.02² = **865 questions**

On 865 questions, a measured 91% has a lower bound of 88.9% — still not a clean pass, which is why the
target has margin built in at the design stage, not at the measurement stage. And because the target applies
**per language** (doc [11](11-multilingual-en-hi-es.md) §5), that is 865 answerable questions **in each
language**.

Unanswerable questions are needed to measure M3. To measure 95% correct abstention to ±3 points:

> n = 1.96² × 0.95 × 0.05 ÷ 0.03² = **203 questions per language**

Total: 3 × (865 + 203) = **3,204 questions**. At about 6 minutes per question to write and label with
evidence spans, that is **320 annotator-hours** — a few weeks for a small team drawn from each region. It is
the single most valuable artefact in the project, and the one most often under-funded.

### What is in it

Questions are written by native speakers from each region who know the documents, not translated from
English (doc [11](11-multilingual-en-hi-es.md) §10). Within each language:

| Type | Share | Example |
|---|---:|---|
| Single fact | 40% | "What is the per-diem for Madrid?" |
| Fact with a condition or exception | 15% | "How many days of annual leave do fixed-term staff in India get?" |
| Numeric, including computed | 10% | "How much per-diem for a three-day trip to Madrid?" |
| Multi-sentence or multi-section | 15% | "What does the travel policy cover besides the per-diem?" |
| Comparison across documents | 8% | "How do the leave rules differ between Spain and Mexico?" |
| Identifier lookup | 7% | "What does SOP-PUN-0312 require before a line restart?" |
| Romanized Hindi (Hindi only) | about a third of Hindi | "Madrid trip ka per diem kitna hai" |
| Filtered | 5% | "What was the per-diem policy in effect in March 2025?" |

The 203 unanswerable questions per language are of three kinds, and the third is the one teams forget:

1. **Not in the corpus at all**: "What is Kestrel's policy on pets in the office?"
2. **Answerable from general knowledge but not from any Kestrel document**: "What is Spain's statutory minimum
   annual leave?" — the model knows; the system must not answer (doc
   [08](08-grounded-generation-and-verification.md) §3).
3. **Answerable only from a document the asking persona cannot read**: the question is run as a persona without
   access, and the correct result is an abstention that does not reveal the document exists (doc
   [13](13-access-control-security-and-privacy.md) §3).

The **conversation slice** — 400 conversations of two to four turns — is described in doc
[10](10-multi-turn-conversations.md) §9.

### Keeping it alive

A golden set rots. Documents are updated and deleted, so labelled evidence changes. Users start asking about
things that did not exist when it was written. So:

- Questions whose evidence document is deleted are **retired automatically**; questions whose evidence is in a
  document that has a new version are queued for re-labelling.
- 10% of questions are retired and replaced each quarter, preferring new questions drawn from production traffic
  (§7).
- Every change to the set is versioned, and every score is reported with the set version, so a score change
  caused by changing the questions is never mistaken for a change in the system.

---

## 3. Labels that survive re-chunking, and the precision ceiling

### Label spans, not chunks

The obvious way to label evidence is by chunk ID: "the answer is in chunk `7f3a…`". That works until the chunker
changes — a new chunk size, a fix to table handling — and every chunk ID changes with it. The labels now point at
whatever chunk happens to have that ID, and recall silently becomes meaningless.

Lodestar labels evidence as a **character span in a document version**: *"FIN-POL-017 version 4, characters
9,310–9,520"* — the per-diem table row. When recall is computed, a retrieved chunk counts as relevant if it covers
at least half of a labelled span. Change the chunker and the labels still hold:

| Chunking | Chunk covering characters 9,310–9,520 |
|---|---|
| 1,700-character chunks | Chunk 5 (8,500–10,200) |
| 1,200-character chunks | Chunk 7 (8,400–9,600) |
| A chunk-ID label from the first chunking, read under the second | Chunk 5 = characters 6,000–7,200 — **an unrelated passage** |

`evaluation.py` section 4 reproduces this table.

### The precision ceiling

Doc [00](00-requirements-and-what-zero-hallucination-can-mean.md) §3.2 claimed that a perfect retriever scores about
40% precision@5 on Kestrel's questions. Here is the calculation. In the pilot labels, the share of questions by number
of relevant chunks was:

| Relevant chunks | 1 | 2 | 3 | 4 | 5 or more |
|---|---:|---:|---:|---:|---:|
| Share of questions | 46% | 27% | 14% | 6% | 7% |
| Best possible precision@5 | 20% | 40% | 60% | 80% | 100% |

> 0.46 × 0.2 + 0.27 × 0.4 + 0.14 × 0.6 + 0.06 × 0.8 + 0.07 × 1.0 = **40.2%**

Only questions with four or more relevant chunks — **13%** of them — can reach 80% at all. This is why context
precision over a variable number of admitted chunks replaced precision@5 (doc
[07](07-retrieval-reranking-and-context.md) §7).

---

## 4. Questions designed to break the system

A golden set drawn only from typical questions measures typical behaviour. The failures that matter most are not
typical. Lodestar's set includes targeted questions for each known weakness:

| Weakness | Questions written for it | Measure |
|---|---|---|
| **Omitted conditions** | The 15% "fact with a condition" questions, each labelled with the condition that must appear (as a claim or a verbatim note) | **M2**: answers missing the condition |
| **Number formats** | Questions whose answers are in es-ES, es-MX and en-IN number formats, Devanagari digits, lakh and crore | Claims dropped wrongly; claims accepted wrongly |
| **Cross-lingual** | Hindi and Spanish questions with English-only evidence | Per-pair recall |
| **Conflicts** | Questions where a non-authoritative copy disagrees with the authoritative source | Did the answer use the authoritative source, or show the conflict? |
| **Freshness** | Questions about facts changed in a new version uploaded during the evaluation run | Old fact never appears after the flip |
| **Permissions** | The same question asked by two personas with different access | No content or existence leak (§2) |
| **Injection** | Evaluation-only documents containing instructions addressed to the model | Instruction never followed; flagged at ingestion |

### Using models to write questions, carefully

Writing 3,204 questions by hand is slow, and it is tempting to have a model generate them from documents. This is
where HyDE-style generation is genuinely useful (doc [07](07-retrieval-reranking-and-context.md) §2): a model reads a
document section and drafts candidate questions. But model-written questions have a recognisable shape — they reuse
the document's own vocabulary, which makes retrieval look easier than it is. So drafts are only a starting point: a
person rewrites each into how an employee would actually ask, and no more than 30% of any slice may originate from
model drafts.

---

## 5. Release gates

### Every change goes through the same gate

Any change to anything that affects answers — the chunker, the parsers, the embedder, the reranker, its thresholds,
the condenser, the generator, the system prompt, the entailment model, the judge, the verifier thresholds — is
registered in the model and prompt registry (doc [02](02-architecture-and-the-ledger.md) §4) and cannot reach
production until it passes:

| Gate | Threshold |
|---|---|
| Recall@5, **each language** | ≥ 90%, and no language drops by more than 1 point |
| Context precision | ≥ 80% |
| Admitted recall | ≥ recall@5 − 1 point |
| Follow-up recall@5 and interpretation accuracy | ≥ 90% and ≥ 93% |
| M2, material omissions | ≤ 1% |
| M3, correct abstention, each language | ≥ 95% |
| M4, false abstention, each language | ≤ 5% |
| Verifier test set: false accepts and false rejects, each language pair (§9) | No regression beyond the confidence interval |
| Load test at 100 QPS: p50, p99 | < 1,500 ms, < 3,000 ms |
| Canary: 5% of traffic for 48 hours | Production M1 sample doubled; no rise in thumbs-down, edit rate or abstention |

The "no language drops by more than 1 point" rule matters as much as the absolute threshold. A change that raises
English by two points and lowers Hindi by one is rejected, even if Hindi stays above 90%, because otherwise Hindi
erodes one acceptable step at a time.

### Changes that depend on each other

Some settings are only valid for the model they were tuned on. The admission and sufficiency thresholds are set on
the reranker's **calibrated** scores (doc [07](07-retrieval-reranking-and-context.md) §5); a new reranker has
different raw scores, so its calibration and thresholds must be re-derived. The registry records these dependencies,
and a change to a model that does not include re-tuned dependent settings is rejected before evaluation runs.

### Gating a new source

When a new connector is added — the contract system, say — its documents are ingested but **not enabled for
answering** until golden-set questions whose evidence lives in that source pass (doc
[05](05-ingestion-pipeline-and-versions.md) §7). If there are no such questions yet, 100 are written first. The
contract system was held back four days at Kestrel while its Devanagari OCR was tuned.

---

## 6. Measuring M1 in production

### Why production

M1's target is 0.10% of claims, and the gate needs the **upper bound** of the estimate to be below it. To show a rate is
below 0.1% with 95% confidence when you observe zero failures, you need:

> n = ln(0.05) ÷ ln(1 − 0.001) = **2,995 claims**, with none failing

The golden set produces about 3,204 × 2.5 ≈ 8,000 claims per run — enough to catch a gross regression, not enough to
measure a rate of 0.066% with useful precision. Production produces 1.35 million claims a day. So M1 is a production
measurement, and the golden set's job for M1 is to catch big regressions before they get there.

### The daily sample

1. **Sample 2% of answered queries**: about 9,000 answers, 28,800 claims a day, stratified so each language pair and
   each document class is represented.
2. **De-identify** them (doc [13](13-access-control-security-and-privacy.md) §7).
3. **Judge every claim** with a strong LLM judge of a different model family from both the generator and the online
   gray-zone judge, given the claim and its cited sentences.
4. **Every flagged claim goes to a person**, who confirms or rejects it.

### Why the confirmed count is not the rate

On one day in the pilot, the judge flagged 41 claims, and reviewers confirmed 30. It is tempting to report 30 ÷ 28,800
= **0.104%**. That is an undercount, because the judge does not flag every unsupported claim. Its **recall** — the share
of truly unsupported claims it flags — was measured at 0.85 on the verifier test set (§9). So 30 confirmed claims are
about 85% of the true number:

> Estimated unsupported claims: 30 ÷ 0.85 = **35.3**
> Estimated rate: 35.3 ÷ 28,800 = **0.1225%**, with a 95% upper bound of **0.170%**

That day was before the entailment model was fine-tuned, and 0.1225% sits right on the off-the-shelf residual predicted
in doc [08](08-grounded-generation-and-verification.md) §10 (0.122%). It fails the M1 target, as predicted. The
uncorrected 0.104% would have reported a near-pass.

`evaluation.py` section 5 reproduces this calculation. Two practices keep it honest:

- **Re-measure the judge's recall monthly**, on the verifier test set plus every confirmed production error from the
  previous month that the judge had flagged — and, importantly, on errors found by other means (user feedback, reviewer
  spot checks of unflagged claims) that the judge had missed.
- **Report weekly, not daily.** A week is about 200,000 claims, which narrows the interval enough for a weekly M1 number
  to mean something. Daily numbers are for spotting a sudden jump.

M1 is reported **per language pair** (doc [11](11-multilingual-en-hi-es.md) §8) and **per document class**, because an
aggregate can hide a concentrated problem in exactly the way a recall blend can.

---

## 7. Signals from users

Users find problems no evaluation set anticipated. Four signals are tracked:

| Signal | What it usually means |
|---|---|
| Thumbs-down with reason `WRONG` or `WRONG_DOCUMENT` | A retrieval or verification failure; sampled for review |
| Thumbs-down with reason `OUTDATED` | A freshness problem — often a connector delay, sometimes a stale copy outranking the current version |
| Interpretation edits on follow-ups | Condenser errors (doc 10 §9) |
| Abstentions followed by the user opening a document from the "nearest documents" list | The answer existed and retrieval or the sufficiency gate missed it — the most valuable signal for M4 |

Every week, a reviewer turns the most informative of these into golden-set candidates, de-identified and rewritten, with
evidence spans labelled. About 50 are added each month. That is how the golden set keeps pace with what people actually
ask.

---

## 8. The experiments that set the parameters

Every number in the retrieval and verification design was chosen by an experiment on the golden set, and should be
re-chosen when the components it depends on change. The chunk size from doc [04](04-parsing-and-chunking.md) §6 is a
typical example:

| Chunk target (structure-aware) | English recall@5 | Spanish | Hindi | Chunks in the corpus |
|---:|---:|---:|---:|---:|
| 300 tokens | 91.0% | 89.8% | 84.9% | 160M |
| **400 tokens** | **92.2%** | **90.7%** | **85.6%** | **120M** |
| 512 tokens | 91.6% | 90.1% | 84.2% | 94M |

Smaller chunks give the embedder too little context; larger ones dilute it with text unrelated to the question. 400 is
best in every language here, and it is also cheaper than 300. With a different embedder the peak may move, which is why
the experiment is re-run with any embedder change.

The other parameters chosen the same way: the admission threshold τ_admit and the sufficiency threshold τ_suff (doc
[07](07-retrieval-reranking-and-context.md) §7), the entailment accept and reject thresholds (doc
[08](08-grounded-generation-and-verification.md) §5), the number of candidates reranked, the neighbour expansion, the
hedging threshold (doc [12](12-latency-budget-and-caching.md) §4). Each has its experiment recorded in the registry with
the golden-set version it was run on.

Changes users see — the interpretation display, the wording of a partial-answer notice — are tested differently: as
controlled experiments on a fraction of production traffic, measuring edit rates, feedback and follow-up behaviour.

---

## 9. The verifier's own test set

The verifier is the component the guarantee most depends on, so it has its own evaluation, separate from the golden set.

The **verifier test set** has about 8,000 (claim, evidence) pairs, held out from the 60,000 used to fine-tune the
entailment model (doc [08](08-grounded-generation-and-verification.md) §10). Roughly half are supported claims, and half
are unsupported — produced by deliberate perturbation and by collecting real errors found in production. It is sliced by
perturbation type and language pair:

| Perturbation | Example | Caught deterministically | Entailment false-accept (fine-tuned) |
|---|---|---:|---:|
| Number changed | €85 → €95 | ~100% | — |
| Number format misread | 1.500,50 read as 1.5 | ~100% | — |
| Negation flipped | "not eligible" → "eligible" | ~97% | 1.8% |
| Modality strengthened | "may" → "must" | ~92% | 2.5% |
| Scope swapped | employees → contractors | ~88% | 2.2% |
| Date shifted | 1 April → 4 January | ~100% | — |
| Qualifier dropped | rule kept, exception removed | 0% (by design: it is an omission) | — (caught by the qualifier check, M2) |
| Paraphrase that changes meaning, no cue words | "receipts not required" → "no documentation needed" | 0% | **7.9%** |
| All unsupported claims, in the mix the generator actually produces | | **45%** | **3.0%** |

The last two rows are where the residual lives, and the last row supplies the two inputs that doc
[08](08-grounded-generation-and-verification.md) §10 uses. The table also explains why the 45% deterministic catch rate
is not higher: deterministic checks are nearly perfect for the error types they target, and blind to the ones they do
not — and meaning-changing paraphrase, which they cannot see, is a large share of what the generator gets wrong.

The judge's recall, used in §6, is measured on this set too.

---

## 10. Evaluating the indexes and the freshness path directly

Two parts of the system are evaluated with probes rather than questions.

**Index quality.** Every night, 2,000 of the previous day's queries are run through the vector index and through an exact
scan of one shard, and the overlap at k = 50 is reported — about 98.6% (doc [06](06-indexing-at-120m-chunks.md) §2). The
1% most selective filters from the previous week are replayed the same way and reported separately (doc
[06](06-indexing-at-120m-chunks.md) §6).

**Canary documents.** Every five minutes, a synthetic document with a unique nonsense phrase ("the quartz-heron
allowance is 417 units") is uploaded through the interactive lane, queried until it is answerable, updated to a new
value, queried until the new value appears, deleted, and then queried — and each store is checked directly — until it is
gone. Each step's time is recorded. The canary measures the whole freshness path end to end, from the outside, the way a
user would experience it, and it is the first thing to page if any part of that path degrades. It runs in its own
department-scoped visibility so no real user ever sees it.

---

## What to take away

1. **Size the golden set from the precision you need.** 200 questions cannot tell 91% from 90%; 865 per language is the
   minimum for a ±2-point interval, plus 203 unanswerable per language.
2. **Label evidence as spans in a document version, not as chunk IDs.** Chunk-ID labels silently point at the wrong
   passage after any chunker change.
3. **Include unanswerable questions the model could answer from memory, and ones only a restricted document answers.**
   They test G5 and permission behaviour.
4. **Write questions to break known weaknesses**: exceptions, number formats, cross-lingual, conflicts, freshness,
   permissions, injection.
5. **Gate every change on every language, and reject any language dropping by more than a point.**
6. **Measure M1 in production and correct for the judge's recall.** Confirmed errors undercount: 30 confirmed meant about
   35 real, 0.1225% rather than 0.104%.
7. **Give the verifier its own test set, sliced by perturbation type and language pair.** It shows exactly where the
   residual lives: meaning-changing paraphrase with no cue words.
8. **Probe freshness from the outside.** A canary document uploaded, updated and deleted every five minutes measures what
   users experience.

Continue to [15 — Observability, SLOs and the admin dashboard](15-observability-slos-and-admin-dashboard.md).
