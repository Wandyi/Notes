# 00 — The Requirements, Where They Conflict, and What "Zero Hallucinations" Can Honestly Mean

Every design in this collection starts from a requirements list that looks complete and is not.
That is normal. A requirements list is written by people describing the product they want, and
they describe it in product language: "zero hallucinations", "page number", "within 5 minutes".
Each of those phrases sounds precise. Each of them has two or three reasonable meanings, and the
meanings lead to different systems.

The job of this document is to read the list the way an architecture reviewer would: find the
phrases that are undefined, the targets that cannot be met as written, and the requirements that
are missing entirely. Then it rewrites the list into something you can build and test, and it
spends most of its length on the hardest phrase — **zero hallucinations** — because the whole
design follows from what you decide that phrase means.

If you read only one document in this collection, read this one. The other nineteen are the
consequences.

---

## 1. The system we are designing

Abstract requirements are hard to reason about, so this collection uses one concrete system all
the way through. It is described fully in [01](01-the-running-system-and-its-numbers.md); here
is enough to start.

**Kestrel Group** is a multinational with 120,000 employees: 55% in India, 25% across Spain and
Mexico, and 20% in the US and UK. Its documents — HR policies, finance procedures, legal
templates, engineering runbooks, factory safety instructions, sales playbooks — live in
SharePoint, Confluence, a contract system and a dozen team drives. There are **10 million of
them**, in English, Spanish and Hindi.

**Lodestar** is the assistant Kestrel is building over that corpus. An employee in Pune types
*"Can I claim per-diem for a three-day trip to Madrid?"* and gets back an answer like this:

```
Yes. Employees travelling to Madrid can claim a per-diem of €85 per day. [1]
For a three-day trip that is €255. [1]
The per-diem covers meals and local transport. [1]

  Note from the source: "Contractors are not eligible for per-diem allowances." [1]

[1] Travel and Expense Policy (FIN-POL-017), version 4, page 7
```

Every sentence carries a citation. Hovering over `[1]` shows the exact source sentence. The
note at the bottom is not written by the model at all — it is a sentence copied from the policy,
because the policy attaches a condition the answer did not mention. Doc
[08](08-grounded-generation-and-verification.md) explains how each part of that answer is
produced and checked.

---

## 2. The requirements as given

For reference, these are the requirements this collection was asked to meet.

**Functional**

1. Upload PDF, DOCX, HTML, Markdown and plain text, up to 10M documents.
2. Answer natural-language questions grounded strictly in the uploaded documents — no outside
   knowledge.
3. Every answer includes inline citations to the exact source chunk, document title and page
   number.
4. Multi-turn conversations, where follow-ups use prior context ("What did it say about that
   policy?").
5. Metadata filters on queries: date range, department, document type, author.
6. When a document is re-uploaded, answers based on the previous version must not be served.
7. Deleted documents are fully purged from all indices within 5 minutes.
8. Admins can monitor ingestion job status and query logs via a dashboard.

**Non-functional**

1. Zero hallucinations — a hard product guarantee, not a best-effort goal.
2. Query latency p50 < 1.5 s and p99 < 3 s end to end, including LLM generation.
3. Ingest 100K new documents per day (~1.2 per second on average), with 10× spike tolerance.
4. Retrieval recall@5 > 90%.
5. Retrieval precision@5 > 80%.
6. 99.9% availability.
7. Updated or deleted documents stop appearing in answers within 5 minutes.
8. English, Hindi and Spanish at minimum.

It is a good list. It names the hard problems — hallucination, freshness, deletion, multilingual
— instead of hiding them. The rest of this section is about what it leaves undefined.

---

## 3. Eleven places the list is ambiguous, impossible, or silent

Each item below follows the same pattern: what the requirement says, why that is not yet
buildable, and what this design decides. The decisions are collected into a revised list in
§8.

### 3.1 "Zero hallucinations" has no definition

This is the big one, and it gets §4–§7 to itself. In short: no generative model produces zero
unsupported sentences, so a guarantee of zero has to come from somewhere other than the model.
The design splits the requirement into **things that are guaranteed by construction** (zero
fabricated quotes, zero citations to deleted documents) and **one thing that is measured against
a budget** (sentences that paraphrase their evidence incorrectly).

### 3.2 "Precision@5 > 80%" cannot be met by a perfect retriever

Precision@5 means: of the five chunks the retriever returns, what fraction are relevant? It
sounds like a reasonable quality bar. Now consider the question *"What is the per-diem for
Madrid?"* The answer is one row of one table. Exactly **one** chunk in the 120-million-chunk
corpus is relevant to it.

A retriever that ranks that chunk first, and then returns four more chunks because it was asked
for five, scores 1 ÷ 5 = **20% precision@5**. That is the best any retriever can do on this
question. A retriever cannot score 80% on a question that has only one right answer.

How common are such questions? In Lodestar's pilot labelling, 46% of questions had exactly one
relevant chunk and 27% had exactly two. Working through the whole distribution (doc
[14](14-evaluation.md) §3 shows it), a **perfect** retriever averages **40.2% precision@5**, and
only 13% of questions even make 80% possible.

The requirement is reaching for something real — *don't stuff the model's context with
irrelevant text* — and measuring it the wrong way. The fix is to stop fixing k at 5. Lodestar
admits a variable number of chunks (between one and eight) depending on how confident the
reranker is, and measures **context precision**: of the chunks actually put in front of the
model, what fraction are relevant. That target can be 80%, and it measures the thing the
requirement cares about. Doc [07](07-retrieval-reranking-and-context.md) §7 has the mechanism.

### 3.3 "Page number" does not exist for half the formats

PDFs have pages. HTML pages, Markdown files and plain-text files do not. A DOCX file *seems* to
have pages, but its page breaks are computed by whatever program renders it, and Word on a Mac,
Word on Windows and LibreOffice will paginate the same file differently. A citation saying
"page 7" of a DOCX can point to the wrong place for the person who opens it.

Lodestar's decision:

| Format | Locator shown in the citation |
|---|---|
| PDF | The physical page, plus the printed page label when the PDF has one ("p. 7, printed as 'iv'") |
| DOCX | Converted once to PDF at ingestion with a pinned renderer version; that PDF is what the citation opens, so the page is stable |
| HTML, Markdown, plain text | Heading path and paragraph number: "§ 3.2 Travel Advances ¶ 4" |

This needs product sign-off, because it changes what "page number" means for 30% of the corpus.

### 3.4 "End-to-end latency including generation" — to which moment?

An answer is streamed. The user sees the first sentence after perhaps 700 ms and the last after
perhaps 1,300 ms. Which of those is "end to end"?

This design takes the strict reading: **the time until the last verified sentence has been
delivered and the answer is marked complete.** It also reports the time to the first verified
sentence, because that is what the user perceives as "it started answering", but the SLO is on
completion. Doc [12](12-latency-budget-and-caching.md) shows that the strict reading is
achievable — p50 1,338 ms and p99 2,593 ms — but only with no room to spare, and only after
removing two things that commonly appear in RAG designs.

### 3.5 "Old answers must not be served" collides with ingestion time

When someone re-uploads a 300-page scanned contract, re-ingesting it — optical character
recognition, chunking, embedding, indexing — takes minutes. During those minutes there are only
two choices:

- keep answering from the **old** version until the new one is ready, which serves exactly the
  answers requirement F6 forbids; or
- stop answering from the document entirely until the new version is ready, which makes a
  document the user just updated temporarily invisible.

Neither is free. Lodestar does the first if the new version is ready within the 5-minute window
(which it is for about 99% of updates), and the second if it is not, and it tells the user which
happened: *"Travel and Expense Policy is being updated and was not used for this answer."* Doc
[09](09-freshness-updates-and-deletion.md) §3 calls this **swap or blackout**.

### 3.6 "Fully purged from all indices" — the indices are not where the problem is

The vector index and the keyword index are the two copies everybody remembers. A document's text
also lands, in a naive design, in at least a dozen other places: the raw upload, the parsed
intermediate files, the chunk store, the message queue between pipeline stages, the embedding
cache, the answer cache, the conversation history, the request traces, the LLM provider's logs,
the evaluation dataset, the analytics warehouse and the nightly backups.

There is also a subtler problem inside the indexes themselves. Deleting a vector from an HNSW
graph (the structure most vector databases use, explained in doc
[06](06-indexing-at-120m-chunks.md)) usually marks it with a **tombstone** — a flag that says
"skip this" — and the bytes stay on disk until a background compaction rewrites the segment.
Lucene, which powers Elasticsearch and OpenSearch, behaves the same way. Is a tombstoned
document "fully purged"? It cannot be retrieved, but its text is still on the disk.

Lodestar defines three levels and gives each its own deadline:

| Level | Meaning | Deadline |
|---|---|---|
| **Unusable** | Cannot be cited, quoted or used as evidence by any answer | At the moment the delete commits |
| **Unretrievable** | No index, cache or store returns it to any query | 5 minutes, verified by an auditor |
| **Physically erased** | Bytes reclaimed from live disks; backups expire or are replayed on restore | 24 hours on live systems; 35-day backup retention |

The requirement as written is the middle row. The first row is stricter and comes for free from
the architecture. The third row is what a privacy regulator will actually ask about, and it needs
legal sign-off.

### 3.7 Missing: who is allowed to see which document

The list never mentions permissions. A 10-million-document enterprise corpus without access
control does not exist. Kestrel's legal templates, salary bands, disciplinary procedures and
acquisition memos are not readable by all 120,000 employees.

This is not a detail to add later, because it changes the design in three places. The indexes
must filter by permission during the search, not after it, or search quality collapses for users
with narrow access (doc [06](06-indexing-at-120m-chunks.md) §6). The answer cache must never
serve one user's answer to another user who cannot read the sources (doc
[12](12-latency-budget-and-caching.md) §6). And a permission *revocation* is a freshness event
exactly like a deletion: someone removed from the M&A group at 14:00 must not be reading
acquisition memos through the assistant at 14:10.

The revised list adds: **every answer is built only from documents the asking user can read,
checked at the time of the answer; a permission revocation takes effect within 5 minutes, the
same SLA as deletion.**

### 3.8 Missing: how many questions per second

Latency, availability and cost all depend on load, and the list gives none. A system answering
five questions a second and a system answering five thousand a second share almost no capacity
plan.

Doc [01](01-the-running-system-and-its-numbers.md) §3 derives Lodestar's load from Kestrel's
headcount: **500,000 queries a day, 33 per second at the busiest second of the busiest hour,
provisioned for 100.** Doc [16](16-availability-degradation-scaling-and-cost.md) §7 shows which
parts of the design change if the true number is 5,000 per second — some components scale
linearly, and a few do not change at all.

### 3.9 "Multiple languages" — the hard case is the cross-lingual one

Supporting English, Hindi and Spanish could mean three separate systems, one per language. That
is the easy reading and the wrong one. Kestrel's Indian employees ask questions in Hindi about
policies written in English. Its Mexican employees ask in Spanish about global policies written
in English. So the requirement is really:

- a question in any of the three languages retrieves evidence in any of the three;
- the answer is written in the language of the question;
- the citation shows the source sentence in the language it was written in;
- and the verification that the answer is supported works *across* languages — a Hindi sentence
  checked against an English source.

A fourth language hides inside the third: about a third of Kestrel's Hindi queries are typed in
Latin script ("leave policy mein kitne din milte hain"), which is called **romanized Hindi**,
and is often mixed with English. Doc [11](11-multilingual-en-hi-es.md) covers all of this, and
shows that a system can pass its recall target on average while failing it badly for Hindi.

### 3.10 "No outside knowledge" — not even arithmetic?

Read literally, "grounded strictly in the documents" forbids the answer *"For a three-day trip
that is €255"*, because the number 255 appears nowhere in the policy. The policy says €85 per
day. The user said three days. The model multiplied.

That arithmetic is exactly the kind of thing users want, and exactly the kind of thing models get
wrong. Lodestar allows it under one condition: a computed claim must carry its arithmetic
(`85 × 3 = 255`), every operand must come from the cited evidence or from the user's question,
and the verifier recomputes it. Arithmetic the model does *without* declaring it is rejected.
Doc [08](08-grounded-generation-and-verification.md) §6 shows the check.

### 3.11 "100K new documents a day" and "10M documents total" cannot both be true for long

At 100,000 new documents a day, a 10-million-document corpus is full in 100 days, and after a
year it holds 36.5 million. Either the cap is wrong, or most of those 100,000 are not new
documents.

At Kestrel it is the second. When the connectors were instrumented, the daily ingestion events
broke down as **75,000 new versions of documents that already exist** (someone edited a
Confluence page or re-saved a SharePoint file), **25,000 genuinely new documents**, and,
separately, **22,000 deletions** from retention policies and archiving. Net growth is 25,000 −
22,000 = 3,000 documents a day, about 1.1 million a year, or 11%.

This matters well beyond capacity planning. It means **75% of ingestion is updates**, and every
update is a freshness event under requirement F6: an old version that must stop being used, and
twelve old chunks that must leave every index. A design that treats updates as a rare edge case
gets the common case wrong. Doc [05](05-ingestion-pipeline-and-versions.md) §2 sizes the
pipeline for this mix, and doc [06](06-indexing-at-120m-chunks.md) §5 shows what 900,000 chunk
replacements a day do to an HNSW graph.

---

## 4. What a hallucination actually is

"Hallucination" is used loosely to mean "the AI said something wrong". To design against it you
need a sharper definition, because different kinds of wrong are caught by completely different
mechanisms.

Here is the working definition this collection uses:

> **A hallucination is a statement in a delivered answer that is not supported by evidence the
> user was entitled to see, in the version of the document that was current when the answer was
> delivered.**

Every word in that sentence is doing work. "Delivered" — a sentence the system generated and
then deleted before showing it is not a hallucination; it is the system working. "Entitled to
see" — a correct statement drawn from a document the user cannot read is not a hallucination,
but it is a data leak, which is worse. "Current" — a statement correctly quoted from last
month's version of a policy is a hallucination by this definition, because the user will act on
it today.

Now the kinds. Each row is a distinct failure, with an example from Kestrel's corpus. The last
column names the layer of this design that catches it; you will meet each layer in later docs.

| Kind | Example | Caught by |
|---|---|---|
| **Fabrication** — the claim is in no document at all | "Employees get a €40 laptop allowance." No such allowance exists. | Every claim must cite sentence IDs that were in the context; the entailment check finds no support (doc 08) |
| **Fabricated quote** — a citation shows text the source never contained | The citation popup shows "per-diem of €95" when the PDF says €85 | Impossible by construction: the model never writes quotes, the system fetches them (doc 08 §2) |
| **Misattribution** — the claim is true somewhere, but cited to the wrong place | The €85 figure is cited to the Spain policy, but it comes from the global one | Each claim is checked against the sentences it cites, not against the whole context (doc 08 §4) |
| **Number distortion** | Source says €85, answer says €95; source says ₹1,50,000, answer says ₹15,000 | Deterministic quantity check with per-locale number parsing (doc 08 §5, doc 11 §6) |
| **Polarity flip** | Source: "Contractors are **not** eligible". Answer: "Contractors are eligible." | Deterministic negation check, then entailment (doc 08 §5) |
| **Modality shift** | Source: "Employees **may** book business class". Answer: "Employees **must** book business class." | Deterministic modality check (doc 08 §5) |
| **Scope swap** | Source is about employees. Answer applies it to contractors, or to Spain instead of India. | Deterministic scope check (doc 08 §5) |
| **Stale evidence** | Answer quotes version 3 of a policy that was replaced by version 4 an hour ago | The hydration gate: only live versions can become evidence (doc 09 §2) |
| **Unauthorized evidence** | Answer quotes a document the user cannot open | The hydration gate checks permissions at answer time (doc 13 §3) |
| **Conflation** — two true sources combined into a claim neither supports | Doc A: "Madrid per-diem €85". Doc B: "Interns receive 50% of standard allowances". Answer: "Interns in Madrid receive €42.50" — when doc B is about a different allowance | Each claim must be entailed by its own cited sentences; the arithmetic must be declared (doc 08 §6) |
| **Misleading omission** — every word is supported, but a condition is missing | "Employees get 24 days of annual leave" — the next sentence says "except fixed-term staff in India, who get 18" | Qualifier check attaches the omitted sentence verbatim (doc 08 §7) |
| **Interpretation error** — a correct answer to the wrong question | A follow-up "what about that policy?" is resolved to the wrong policy | Shown to the user as "Interpreting as: …" with the resolved document named (doc 10 §4) |
| **Answering from memory** — the model knows the answer and skips the documents | "Spain's statutory minimum annual leave is 30 calendar days" — true, and in no Kestrel document | The sufficiency gate abstains before generation; uncited text cannot be displayed (doc 08 §3) |

Two observations from that table matter more than any single row.

**First, most rows are caught by checks that do not use a model at all.** Fabricated quotes, stale
evidence, unauthorized evidence, wrong numbers, flipped negations, swapped scope, undeclared
arithmetic — these are found by string matching, number parsing and database lookups. They are
cheap, fast and, within their scope, certain. That is the foundation of the guarantee in §6.

**Second, one row is not caught with certainty by anything.** A claim that paraphrases its
evidence in a subtly wrong way — no number changed, no negation flipped, no scope term swapped —
can only be judged by a model that reads both and decides whether one follows from the other.
Models that make that judgement are good and not perfect. That residual is the honest limit of
the design, and §5 puts a number on it.

### Grounded is not the same as true

One more distinction before the arithmetic. Suppose a team wiki page, uploaded by someone in
Sales, says *"The per-diem for Madrid is €120."* It is out of date and was never official. Lodestar
retrieves it and answers *"The per-diem for Madrid is €120 [Sales wiki: Travel tips]."*

That answer is not a hallucination by the definition above. It is perfectly grounded. It is also
wrong. **A RAG system can guarantee faithfulness to its sources; it cannot guarantee the sources
are right.** That problem is handled differently — by source authority ranking, conflict
detection and showing the document's owner and date in the citation (doc
[07](07-retrieval-reranking-and-context.md) §8, doc [08](08-grounded-generation-and-verification.md)
§8) — and it should be named separately in any product promise, so that nobody discovers the
difference during an incident.

---

## 5. Why a better model cannot deliver "zero"

The intuitive plan is to choose the most faithful model available, write a strict prompt, and
call it zero. Here is why that plan cannot meet a hard guarantee, using Lodestar's own volume.

Lodestar generates about **423,000 answers a day** (500,000 queries, less 10% served from the
cache and 6% that abstain before generating; doc [01](01-the-running-system-and-its-numbers.md)
§4 derives both). The average answer makes **3.2 claims** — 3.2 separate statements, each with
its own citation. So:

> 423,000 answers × 3.2 claims = **1,353,600 claims a day**

Suppose you find a model that is 99.9% faithful — one claim in a thousand unsupported. That is
an extraordinarily good model by current standards. It still produces:

> 1,353,600 × 0.001 = **about 1,354 unsupported claims every day**

or roughly one every minute of the working day, each one a statement a Kestrel employee may act
on. A 99.99% model produces 135 a day. No model improvement reaches zero, because a rate times a
large volume is never zero.

The conclusion is not "give up on the requirement". It is that **zero has to come from the
shape of the system, not from the quality of the model.** Specifically:

- Make the most dangerous failures — fabricated quotes, stale or unauthorized evidence —
  **structurally impossible**, so that their rate is zero regardless of the model.
- Make most of the rest **mechanically detectable**, so the system removes them before delivery.
- **Measure** what remains, publish the number, and give the people who own high-stakes
  documents a mode where what remains is nothing.

---

## 6. The guarantee, rewritten so it can be kept

Here is what Lodestar promises. Read it as the contract between the engineering team and the
product owner; every later document either implements one of these lines or measures it.

### Guaranteed by construction

These hold for every delivered answer, independently of which model is used, because the
architecture leaves no path by which they could fail.

| | Guarantee | Why it holds |
|---|---|---|
| **G1** | Every quote shown to the user exists, character for character, in the document version it cites | The model never writes quotes. It cites sentence IDs; the system fetches the sentence text from the chunk store (doc 08 §2) |
| **G2** | Every cited document version was the live version, and readable by the asking user, at the moment of delivery | Evidence is fetched through the ledger's gate at answer time, and re-checked at delivery (doc 09 §2, §6) |
| **G3** | No sentence is displayed without at least one citation, except the fixed template text around the answer | Uncited claims are dropped by the parser before verification (doc 08 §4) |
| **G4** | Every number, amount, percentage and date in a displayed claim appears in its cited evidence, or is recomputed from that evidence by declared arithmetic | Deterministic quantity check with per-locale parsing (doc 08 §5) |
| **G5** | When retrieved evidence is insufficient, the system says so and does not answer from the model's own knowledge | Sufficiency gate before generation; all-claims-dropped falls back to quotes or abstention (doc 08 §3, §9) |

One clarification about G4. It guarantees the number is *present in the evidence*; it does not
guarantee the number is *about the right thing*. A claim "the hotel cap is €85" citing a sentence
that says "the per-diem is €85 and the hotel cap is €140" passes G4 and is wrong. That case falls
to the entailment check below — which is exactly why the next table exists.

### Measured against a published budget

These cannot be guaranteed by construction, so they are measured continuously and reported with
confidence intervals, and they have targets that gate every release.

| | Measure | Target | How it is measured |
|---|---|---|---|
| **M1** | Unsupported claims: claims whose cited evidence does not entail them | ≤ 0.10% of claims, 95% upper bound, weekly | LLM judge on a 2% production sample, corrected for the judge's measured recall; human review of every flag (doc 14 §6) |
| **M2** | Material omissions: answers missing a condition that changes their meaning | ≤ 1% of golden-set answers | Golden-set questions written specifically around exceptions (doc 14 §4) |
| **M3** | Correct abstention on questions the corpus cannot answer | ≥ 95% | 203 unanswerable questions per language in the golden set (doc 14 §2) |
| **M4** | False abstention on questions the corpus can answer | ≤ 5% | The answerable part of the golden set |

The M1 target of 0.10% is not arbitrary. Doc [08](08-grounded-generation-and-verification.md)
§10 derives the residual rate from the verifier's measured error rates: with an off-the-shelf
entailment model the design leaks **0.122% of claims**, which misses the target; with an
entailment model fine-tuned on Kestrel's own documents it leaks **0.066%**, which meets it. The
design does not meet its own target until that fine-tuning is done, and it says so.

### Strict mode: where "zero" is literal

For some documents, any unsupported paraphrase is unacceptable. A wrongly paraphrased indemnity
clause, disciplinary procedure or regulatory filing deadline can cost more than the entire
system. For those document classes — at Kestrel, **legal, compliance and HR policy**, which the
evidence of about 22% of queries comes from — Lodestar answers in **strict mode**: the answer
consists of the relevant source sentences quoted verbatim, under a fixed template, with no
paraphrase at all.

```
The Travel and Expense Policy says:

  "Employees travelling to Madrid may claim a per-diem of €85 per day." [1]
  "The per-diem covers meals and local transport." [1]
  "Contractors are not eligible for per-diem allowances." [1]

[1] Travel and Expense Policy (FIN-POL-017), version 4, page 7
```

Strict-mode answers are less fluent and cannot synthesize across documents. They have no
paraphrase, so the M1 failure cannot occur in them. What remains possible is choosing the wrong
sentences to quote, which is a relevance failure, not a hallucination, and is visible to the
reader.

---

## 7. The conversation to have with the product owner

The requirement says "zero hallucinations — a hard product guarantee". The honest response is
not "impossible" and not "yes". It is a choice between three products, each described with its
real numbers. These come from doc [08](08-grounded-generation-and-verification.md) §10 and the
capacity model in [reference_impl/capacity.py](reference_impl/capacity.py).

| Mode | What the user gets | Answers per day containing an unsupported claim |
|---|---|---|
| **Generative everywhere** (fine-tuned verifier) | Fluent answers that combine sources, with every quote, number and citation guaranteed (G1–G5) | About **892** — 0.21% of answers |
| **Generative, strict mode for legal, compliance and HR** (this design) | Same, except answers drawn from high-stakes documents are verbatim quotes | About **696**, none of them drawn from high-stakes documents |
| **Extractive everywhere** | Every answer is quoted source sentences under a template | **Zero** paraphrase errors; answers are longer, cannot combine sources, and feel like search results |

Notice what the middle row buys. It does not lower the count much — 892 to 696 — because most
queries are about engineering runbooks, sales material and general information, where generative
answers are kept. What it changes is *where* the remaining errors can occur: never in a document
whose misreading creates legal or regulatory exposure. That is the same idea as the sibling design
[`../AISystemDesign/ModelTiering`](../AISystemDesign/ModelTiering/README.md) applies to model
choice: put the strictest control where an error does the most damage, not uniformly.

What you should **not** do is promise "zero hallucinations" for the generative modes and hope.
The first time a user screenshots an answer that misstates a policy, the conversation is about
whether the team knew. Publishing M1 weekly, with its confidence interval, is what turns that
conversation into one about whether the number is acceptable.

---

## 8. The revised requirements

This is the list the rest of the collection builds against. Changes from §2 are marked.

**Functional**

| # | Requirement | Change |
|---|---|---|
| F1 | Ingest PDF, DOCX, HTML, Markdown, plain text; 10M documents; up to 2,000 pages or 50 MB per file | Size limit added |
| F2 | Answer only from documents the asking user may read, in their current version | Permissions and version added |
| F3 | Every claim cites a document, version, locator and the exact source sentence | "Page number" generalised to **locator** (§3.3); sentence-level, not chunk-level |
| F4 | Multi-turn conversations; the system shows how it interpreted a follow-up | Interpretation display added |
| F5 | Filters on effective date, upload date, department, document type and author | "Date" split into two dates |
| F6 | An updated document's previous version stops being used within 5 minutes; if the new version is not ready by then, the document is excluded and the answer says so | Swap or blackout (§3.5) |
| F7 | A deleted document is unusable at commit, unretrievable from every store within 5 minutes (verified), and physically erased within 24 hours on live systems | Three levels (§3.6) |
| F8 | Admin dashboard for ingestion jobs and query logs, with pseudonymised users and audited reveal | Privacy controls added |
| F9 | **New:** a permission revocation takes effect within 5 minutes | Added (§3.7) |
| F10 | **New:** computed claims carry their arithmetic and are recomputed by the verifier | Added (§3.10) |

**Non-functional**

| # | Requirement | Change |
|---|---|---|
| N1 | Hallucination: G1–G5 always; M1 ≤ 0.10% of claims; strict mode for legal, compliance and HR | Split into guarantees and measures (§6) |
| N2 | Latency to the last verified claim: p50 < 1.5 s, p99 < 3 s, at 100 queries per second | Moment defined; load stated |
| N3 | 100K ingestion events per day sustained (75% of them new versions of existing documents), 10× that rate for a spike of up to 6 hours | Update share and spike duration stated (§3.11) |
| N4 | Recall@5 > 90% **in each language separately** | Per-language |
| N5 | Context precision > 80% over the chunks admitted to the model | Replaces precision@5 (§3.2) |
| N6 | 99.9% availability, where "available" means a verified answer, a quote-only answer, or a correct abstention within the latency SLO | "Available" defined (doc 16 §2) |
| N7 | Freshness: F6, F7 and F9 | Unchanged in intent |
| N8 | English, Spanish, Hindi including romanized Hindi; cross-lingual retrieval and verification | Cross-lingual made explicit (§3.9) |

---

## 9. The design in three moves

Everything in the remaining documents follows from three decisions. They are introduced here so
you can recognise them when they appear.

**Move 1 — the model points, it never quotes.** The model is shown sentences with IDs like
`[S2.4]`, and it writes claims that reference those IDs. It never writes evidence text itself.
Every quote the user sees is fetched by the system from the chunk store by ID. A fabricated quote
has no path to the screen. (Doc [08](08-grounded-generation-and-verification.md).)

**Move 2 — the ledger is the truth; the indexes are disposable copies.** The vector index and
the keyword index return only chunk IDs and scores. Text becomes evidence only when it is fetched
through the **ledger** — the authoritative database of documents, versions and permissions — at
the moment the answer is built. An update, deletion or revocation takes effect when it commits,
not when every index has caught up, so the 5-minute SLA becomes a deadline for cleaning up
copies, not for correctness. (Docs [02](02-architecture-and-the-ledger.md) and
[09](09-freshness-updates-and-deletion.md).)

**Move 3 — abstention is a first-class answer, and the residual is a published number.** The
system decides whether the evidence is sufficient *before* it spends money generating. When it is
not, it says so and shows the closest documents. What cannot be verified is removed or replaced by
the source sentence. What leaks past verification is measured weekly against a budget, and the
document classes where any leak is unacceptable get strict mode. (Docs
[08](08-grounded-generation-and-verification.md) and [14](14-evaluation.md).)

---

## What to take away

1. **Read every requirement for its undefined words.** "Zero", "page number", "end to end",
   "fully purged" and "within 5 minutes" each had several meanings, and each meaning leads to a
   different system. Decide them in writing before designing anything.
2. **Precision@5 > 80% cannot be met by a perfect retriever** when most questions have one or two
   relevant chunks; the best possible score on Kestrel's questions is 40.2%. Measure precision
   over the chunks you actually admit to the model, with a variable k.
3. **A hallucination is a claim not supported by current evidence the user may see.** That
   definition makes stale and unauthorized evidence part of the problem, which is where most
   designs have gaps.
4. **No model reaches zero at volume.** 1.35 million claims a day at 99.9% faithfulness is about
   1,354 bad claims a day. Zero has to come from the structure of the system.
5. **Split the guarantee in two.** Five properties (no fabricated quotes, no stale or
   unauthorized evidence, no uncited claims, no unsupported numbers, no answering from memory) are
   guaranteed by construction. One property — faithful paraphrase — is measured against a
   published budget.
6. **Offer strict mode where an error is most expensive.** Verbatim quotes for legal, compliance
   and HR documents remove paraphrase errors exactly where they would do the most damage.
7. **Grounded is not true.** The system can be faithful to a wrong document. Name that limitation
   separately in the product promise.
8. **Add the missing requirements: permissions, load and cross-lingual behaviour.** Each one
   changes the architecture, so adding it later means redesigning.

Continue to [01 — The running system and its numbers](01-the-running-system-and-its-numbers.md).
