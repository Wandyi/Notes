# 04 — Parsing and Chunking: Where Retrieval Quality Is Actually Decided

Most discussion of RAG quality is about embedding models and rerankers. At Kestrel, when the
pilot's wrong answers were traced back to their cause, the largest single cause was neither. It
was text that was wrong *before* any model saw it: a two-column PDF read straight across both
columns, a table flattened into a row of numbers with no headers, a scanned Hindi circular where
OCR turned ₹1,50,000 into ₹1,50,0O0, an exception sentence cut off from the rule it modified.

No retriever recovers a passage that was garbled at ingestion. And — this is the part that
connects parsing to the hallucination guarantee — the verifier in doc
[08](08-grounded-generation-and-verification.md) checks that a claim matches the **stored
text**. If the stored text is a wrong transcription of the PDF, the verifier will faithfully
confirm claims that match the wrong transcription. **The guarantee is only as good as the
parse.**

This document covers turning five file formats into clean, structured, citable text, and then
cutting that text into chunks along its structure.

---

## 1. What goes wrong, concretely

Five real failures from Kestrel's first parsing pass, before any of the fixes below:

**Columns read across.** A two-column HR newsletter, extracted in raw PDF text order, produced:

> "Employees in Pune may carry forward The canteen will be closed on up to 12 days of unused
> leave 15 August for Independence Day."

Two unrelated sentences interleaved line by line. Embedded, this chunk is close to nothing.

**Headers and footers on every page.** Every page of a 40-page policy ended with *"Kestrel Group —
Confidential — Page 7 of 40 — Uncontrolled when printed"*. Repeated 40 times, that text was the
most frequent phrase in the document, and the keyword index happily matched "controlled" queries to
it.

**Tables flattened.** The per-diem table:

| City | Per-diem | Covers |
|---|---|---|
| Madrid | €85 | Meals, local transport |
| Mumbai | ₹4,500 | Meals |

came out as `City Per-diem Covers Madrid €85 Meals, local transport Mumbai ₹4,500 Meals`. A
question about Mumbai retrieves it, but the model now has to guess which number belongs to which
city — and the verifier, checking only that ₹4,500 appears in the cited text, cannot tell if the
model guessed right.

**OCR errors in numbers.** A scanned circular from the Chennai plant: `₹1,50,0O0` (letter O for
zero), `2O25`, `l5 days` (letter l for 1). The number parser rejects the first two, so claims
citing them fail verification — the system degrades safely. The third is worse: `l5` is not
recognised as a number at all, so a claim "15 days" citing that sentence fails, while the PDF
clearly says 15.

**Exceptions cut off.** A fixed 512-token chunker split:

> Chunk 14: "…Employees receive 24 days of paid annual leave per calendar year."
> Chunk 15: "This does not apply to fixed-term staff in India, who receive 18 days. …"

A question about annual leave retrieves chunk 14 and not chunk 15. The answer "24 days" is
supported by its evidence and wrong for 9,000 people.

Every section below exists because of one of these five.

---

## 2. One intermediate representation for five formats

Each format has its own parser, but they all produce the same output: an ordered list of
**blocks**. Downstream code — normalisation, chunking, sentence splitting — never needs to know
which format a document came from.

```json
{
  "doc_id": "doc_01JB7K3V9Q2R", "version_seq": 5, "locator_kind": "page",
  "blocks": [
    { "type": "heading", "level": 2, "text": "4 Per-diem", "page": 7 },
    { "type": "heading", "level": 3, "text": "4.2 Europe", "page": 7 },
    { "type": "paragraph", "text": "Employees travelling to Madrid may claim a per-diem of €85 per day.",
      "page": 7, "lang": "en" },
    { "type": "table", "page": 7, "caption": "Table 4.2 Per-diem rates",
      "header": ["City", "Per-diem", "Covers"],
      "rows": [["Madrid", "€85", "Meals, local transport"], ["Mumbai", "₹4,500", "Meals"]] },
    { "type": "paragraph", "text": "Contractors are not eligible for per-diem allowances.",
      "page": 7, "lang": "en" }
  ],
  "parse_quality": 0.97,
  "warnings": []
}
```

The block types are `heading` (with a level), `paragraph`, `list_item` (with nesting depth),
`table` (header plus rows), `code`, `caption` and `quote`. Every block carries its page (or, for
unpaginated formats, its section path and paragraph number) and its detected language.

### The format router

The file extension is not trusted. The router reads the first bytes of the file (its "magic
number") and the declared content type, and if they disagree it uses the bytes. A `.pdf` that is
really a ZIP archive is rejected rather than parsed.

| Format | Parser approach | The hard part |
|---|---|---|
| **PDF with a text layer** | Layout-aware extraction: detect columns, reading order, tables and repeated page furniture from the page geometry | Reading order in multi-column layouts; tables without ruling lines |
| **PDF without a text layer** (scanned) | Render each page to an image, run OCR with Latin and Devanagari models, then the same layout analysis | Number accuracy; Hindi conjunct characters; skewed scans |
| **DOCX** | Read structure from the DOCX XML (headings, lists, tables are explicit); render once to PDF with a pinned LibreOffice version for page numbers; map each block to its rendered page | Keeping the two views aligned |
| **HTML** | Strip navigation, sidebars and footers; convert Confluence macros (expand panels, info boxes) into their content; keep headings, lists, tables, code | Boilerplate that looks like content |
| **Markdown** | Headings, lists, tables and code fences are explicit | Front-matter and embedded HTML |
| **Plain text** | Paragraphs at blank lines; heading heuristics (short line, followed by blank line, numbered "3.2") | No structure to rely on |

### Detecting a scan that pretends to have text

Some scanned PDFs have a text layer — added by a scanner's built-in OCR years ago, usually badly.
Trusting it gives the errors above. The parser computes a quality score for each page's text layer
(§4) and, if it is below 0.6, discards the layer and re-OCRs the page. About 30% of Kestrel's
"scanned" PDFs are detected this way rather than by having no text at all.

### Removing page furniture

Headers, footers, page numbers and watermarks are found geometrically: text blocks that appear at
the same position, with the same or nearly the same content, on more than half the pages. They are
removed from the block list. The page *number* printed in the footer is kept, separately, as the
page's **label**, because a document whose printed numbering starts at "iv" should be cited the way
a reader will look it up.

---

## 3. Normalisation that never changes what a quote says

Text has to be normalised before it is indexed: otherwise the same word is stored three ways and
the keyword index treats them as three words. But Lodestar shows stored text to users as verbatim
quotes (guarantee G1, doc [00](00-requirements-and-what-zero-hallucination-can-mean.md) §6). So
the rule is:

> **Normalise only in ways a reader comparing the quote to the PDF would not notice.**

| Applied | Example | Why it is safe |
|---|---|---|
| Unicode NFC | `ज़` stored as one code point (U+095B) or as `ज` + nukta (U+091C U+093C) → always the composed form | Identical on screen. Without it, the keyword index sees two different words and exact matching fails for about 4% of Hindi terms |
| Ligature expansion | `ﬁnance` (one `ﬁ` glyph) → `finance` | Identical on screen; the ligature is a typesetting artefact |
| Soft-hyphen removal | `reim­bursement` (U+00AD inside) → `reimbursement` | Invisible character |
| Line-break de-hyphenation | `reim-` / `bursement` across a line → `reimbursement` | The reader sees one word on the page anyway |
| Whitespace collapse | Runs of spaces, tabs and line breaks inside a paragraph → one space | Layout, not content |

**Deliberately not applied:**

| Not applied | Why not |
|---|---|
| Converting Devanagari digits `२,००,०००` to `2,00,000` | The quote would no longer match the document. The verifier normalises digits *when comparing* (doc 11 §6), not when storing |
| Full NFKC compatibility normalisation | It rewrites `½` as `1⁄2`, `²` as `2` and `ｆｕｌｌ` as `full` — visible changes to what the source says |
| Spelling correction of OCR output | A "corrected" word is a word the model invented. Low-quality OCR is flagged instead (§4) |
| Lower-casing | Done inside the keyword analyzer, never in stored text |

---

## 4. Parse quality is part of the hallucination guarantee

Each page, and then each document, gets a **parse quality score** between 0 and 1, combining:

- the share of tokens that are valid words in the detected language (from a word list per
  language);
- for OCR'd pages, the OCR engine's mean character confidence;
- the share of characters that are replacement characters, isolated symbols or mixed-script tokens
  like `2O25` (digits and letters mixed inside a number);
- whether the page produced any text at all.

What happens at each level:

| Score | Action |
|---|---|
| ≥ 0.90 | Indexed normally |
| 0.75 – 0.90 | Indexed; shown in the admin dashboard as "review recommended" |
| 0.60 – 0.75 (after OCR) | Indexed with **`OCR_LOW_CONFIDENCE`**: excluded from strict-mode answers; citations show "Text extracted from a scan; check the original" |
| < 0.60 after re-OCR | Not indexed for answering; the document is searchable by title only, and the job reports `PARSE_FAILED` with the page numbers |

Why exclude low-confidence text from strict mode in particular? Strict mode's promise is "these are
the document's exact words". For text that may be a mis-transcription, that promise cannot be kept,
so those documents are cited, but never quoted as authoritative.

At Kestrel, 2.1% of documents (about 210,000, almost all scanned contracts and plant circulars) land
in the `OCR_LOW_CONFIDENCE` band. That number is reported weekly, per source, because it is the
measure of how much of the corpus the guarantee does not fully cover.

---

## 5. Locators: making "page 7" mean page 7

The citation must take the user to the right place (doc
[00](00-requirements-and-what-zero-hallucination-can-mean.md) §3.3). Every **sentence**, not every
chunk, gets a locator, because a chunk can start on page 7 and end on page 8.

- **PDF**: physical page index, plus the printed label from §2 if present. The citation reads
  "page 9 (printed iv)" when they differ.
- **DOCX**: the page of the pinned rendering. The renderer version is recorded on the version row,
  so if LibreOffice is upgraded, existing versions keep their page numbers; only new versions use
  the new renderer. The citation viewer opens the stored rendering, not the user's local Word, so the
  page the citation names is the page the user sees.
- **HTML, Markdown, plain text**: the heading path and the paragraph number within the last heading:
  `§ 4 Per-diem › 4.2 Europe ¶ 1`. The viewer scrolls to and highlights the sentence.

---

## 6. Chunking: cut along the structure, never across a condition

### The naive chunker, measured

The common default is fixed-size chunks — 512 tokens — with 128 tokens of overlap between
neighbours, so that a sentence cut at a boundary appears whole in one of the two. Three costs, in
order of how obvious they are:

**It inflates the index by a third.** With 128 tokens of overlap, each new chunk advances only 512
− 128 = 384 tokens. Every token is stored, on average, 512 ÷ 384 = **1.33 times**. For Kestrel's
corpus that is 4,800 ÷ 384 = 12.5 chunks per document instead of 4,800 ÷ 512 = 9.4 — a third more
vectors, memory and embedding work, all of it duplicate.

**It spends top-5 slots on the same passage.** If the answer sits near a chunk boundary, *both*
neighbours contain it and both rank highly. Two of the five slots the requirement cares about now
hold one passage. At Kestrel, a quarter of pilot queries had near-duplicate overlapping chunks in
their top five.

**It cuts across meaning.** Overlap protects a sentence from being cut in half. It does not protect
a rule from being separated from its exception two sentences later, or a table row from its header
two hundred tokens earlier. That is failure 5 in §1, and overlap does nothing about it.

### Lodestar's chunker

The chunker works on the block list from §2, in five steps.

**Step 1 — build the section tree.** Headings define a tree: `4 Per-diem` contains `4.1 India` and
`4.2 Europe`. Chunks never cross a level-2 heading boundary; they may cross deeper boundaries only to
merge tiny sections (below).

**Step 2 — make atomic units.** A unit is something that must never be split:

| Unit | Rule |
|---|---|
| A sentence | Split by language-aware rules (§7) |
| A list with its lead-in | "Employees are eligible if:" stays with all its items, up to the maximum chunk size |
| A table row group | Rows packed up to 400 tokens, **with the header row repeated at the top of every group** |
| A code block | Whole, or split at blank lines if longer than the maximum |
| A qualifier sentence | Any sentence that opens with a qualifier cue — "However", "Except", "This does not apply", "Sin embargo", "Esto no aplica", "लेकिन", "यह … पर लागू नहीं" — is glued to the unit before it |

The last rule is the fix for failure 5. Whatever else happens, *"This does not apply to fixed-term
staff in India"* is in the same chunk as the rule it qualifies.

**Step 3 — pack units into chunks.** Within a section, units are packed greedily to a target of 400
tokens, with a minimum of 120 and a hard maximum of 512. A unit that would push a chunk past 512
starts a new chunk — unless it is a qualifier sentence, in which case it is allowed to exceed the
target up to the maximum.

**Step 4 — merge small sections.** A section under 120 tokens ("4.3 Asia: See 4.1.") merges with its
next sibling, so the index does not fill with fragments too short to embed meaningfully.

**Step 5 — attach the breadcrumb.** Each chunk records its heading path, such as *"Travel and Expense
Policy › 4 Per-diem › 4.2 Europe"*. The breadcrumb is **prepended when embedding** and indexed in its
own keyword field, but it is **not part of the chunk's text**. That distinction keeps quotes
verbatim: a user hovering over a citation sees the sentence the document contains, not a heading the
system added.

### Why no overlap

Overlap's job — making sure the model sees the sentences around the best match — is done better at
query time. When a chunk is admitted to the context, the assembler also fetches the sentences
immediately before and after it from the chunk store (doc
[07](07-retrieval-reranking-and-context.md) §6). That gives the model context exactly where it is
needed, for the few chunks that are actually used, instead of duplicating every boundary in the
120-million-chunk index.

### The result on the corpus

| | Fixed 512 / 128 overlap | Lodestar structure-aware |
|---|---:|---:|
| Chunks per document (mean) | 12.5 | 12.0 |
| Rule and its exception in the same chunk | 81% | 99.6% |
| Table rows that carry their header | 34% | 100% |
| Pilot queries with duplicate passages in top 5 | 25% | 3% (true near-duplicate documents, §8) |

The 400-token target is not sacred. Doc [14](14-evaluation.md) §8 describes the experiment that chose
it, and it should be re-run whenever the embedding model changes, because the best chunk size depends
on how much text the embedder can represent well.

---

## 7. Sentences: the unit of citation

The model cites sentences, the verifier checks claims against sentences, and users see sentences in
citation popups. So sentence splitting is not cosmetic. A sentence split in the wrong place produces
a citation that shows half a rule.

The rules differ by language, and [reference_impl/grounding.py](reference_impl/grounding.py)
implements and tests them (`split_sentences`):

- **English and Spanish** end sentences at `.`, `?` and `!` followed by a space — except after a
  known abbreviation (`e.g.`, `No.`, `Dr.`, `Art.`, `pág.`, `núm.`, `Sr.`) and except between digits
  (`2.5 per cent`).
- **Spanish** opens questions and exclamations with `¿` and `¡`. These belong to the *following*
  sentence: *"Véase el Art. 5 del reglamento. ¿Aplica a becarios? No."* is three sentences, the second
  starting with `¿`.
- **Hindi** ends sentences with the **danda**, `।` (U+0964), not a full stop. A splitter that only
  knows `.` treats a whole Hindi paragraph as one sentence, and every citation shows the entire
  paragraph.
- **List items** without terminal punctuation are each one sentence.
- **Table rows** are each one sentence, rendered with their header for display: *"City: Madrid ·
  Per-diem: €85 · Covers: Meals, local transport"*. A citation to a table row shows the row with its
  column names, so the user can see which number is which.

At 22 tokens per sentence on average, Kestrel's corpus has 2.18 billion of them, stored as character
ranges inside each chunk (doc [03](03-apis-and-data-model.md) §7).

---

## 8. Metadata extracted at ingestion

Some metadata comes from the source system (department from the SharePoint site, author from the
file owner). Some has to be read from the text.

**Effective dates.** Policies say when they take effect, and users mean "in effect now" (doc
[03](03-apis-and-data-model.md) §3). Patterns in all three languages — *"Effective from 1 April
2025"*, *"Vigente a partir del 1 de abril de 2025"*, *"1 अप्रैल 2025 से प्रभावी"* — are extracted
within the first two pages and the last page. When none is found, the effective date falls back to
the source's last-modified date and is marked `inferred`, which the ranking treats as weaker
evidence of currency.

**Document codes.** Kestrel's codes (`FIN-POL-017`, `HR-IN-044`, `SOP-PUN-0312`) and common external
identifiers (`Form 16`, `ISO 45001`, `section 80C`) are extracted by pattern into the keyword index's
exact-match `codes` field (doc [03](03-apis-and-data-model.md) §8).

**Language per block.** About 6% of Kestrel's Hindi documents mix Hindi paragraphs with English
tables or English legal boilerplate. Language is detected per block, not per document, so each block
goes to the right analyzer.

**Authority.** Documents from the official policy repositories are marked `authoritative`; copies on
team drives and wiki pages are not. When two sources conflict, authority is one of the signals for
which to present first (doc [07](07-retrieval-reranking-and-context.md) §8).

**Strict-mode class.** Assigned by the answer policy from document type and source — contracts, legal
templates, compliance notices and HR policies — never by the uploader, who could otherwise opt a
document out of strict mode.

---

## 9. Near-duplicate documents

Enterprise corpora are full of copies. The same travel policy lives on the Finance SharePoint site,
the India HR site and three team drives, sometimes in different versions. At Kestrel, **18% of
documents have at least one near-duplicate.**

Near-duplicates are found with **MinHash**: each document is reduced to 128 hash values computed over
overlapping 5-word sequences, in such a way that the share of matching values between two documents
estimates the share of shared 5-word sequences between them. Documents whose estimated overlap
(Jaccard similarity) is at least 0.9 join the same **cluster**. The signatures are small — 10M × 128
× 4 bytes = 5.1 GB — and locality-sensitive hashing finds candidate pairs without comparing every
document to every other.

The cluster ID is stored on every chunk and used in three places:

1. **Retrieval diversity.** At most one chunk per cluster is admitted for the same passage, so
   five copies of the same paragraph cannot fill the context (doc
   [07](07-retrieval-reranking-and-context.md) §6).
2. **Choosing which copy to cite.** The authoritative copy, then the most recently effective one.
3. **Detecting conflicts.** Two documents in the same cluster that state *different numbers* for
   the same thing are a conflict — usually an old copy someone forgot to delete. Those are surfaced
   in the answer (doc [08](08-grounded-generation-and-verification.md) §8) and listed for the
   document owners in the admin dashboard.

---

## 10. Contextual headers: free from structure, expensive from a model

A chunk like *"This applies to all full-time staff from the date of joining"* is hard to retrieve on
its own: it does not say *what* applies. One published remedy is to have an LLM read the whole
document and write a sentence or two of context for each chunk before embedding it.

Here is what that costs at Kestrel, using the prices from doc
[01](01-the-running-system-and-its-numbers.md) §6. Per document, the model reads the document once
uncached (4,800 tokens), then once more from the provider's cache for each of 12 chunks, and writes
about 60 tokens of context per chunk:

> 4,800 × $1.00/M + 12 × 4,800 × $0.10/M + 12 × 60 × $5.00/M = **$0.0142 per document**
> One-off for 10M documents: **$141,600**
> Ongoing, at 100,000 ingestion events a day: $0.0142 × 100,000 × 30 = **$42,500 a month**

$42,500 a month would be 42% of the cost of the entire system as designed. The breadcrumb from §6 provides
most of the same context — the heading path tells the embedder *what* the chunk is about — for free.

So Lodestar uses breadcrumbs everywhere and generated context only for the roughly **10% of
documents with no usable headings** (plain-text files and some flat HTML pages), at about **$4,250 a
month**. That allocation should be revisited with evidence: if the golden set shows generated context
lifts recall on headed documents by more than a point or two, the cost may be worth paying.

---

## 11. Parsers are an attack surface

Every file Lodestar parses was written by someone else. PDF and DOCX parsers are large, complex
programs with a long history of security vulnerabilities, and a document uploaded by one employee is
parsed on infrastructure that can reach every other employee's documents.

So parsing runs:

- **in a sandbox** (gVisor) with **no network access** — a parser that is exploited cannot call out;
- with **limits** on CPU time (30 s per page), memory (2 GB) and output size;
- with **decompression limits**: a DOCX is a ZIP archive, and one that expands more than 100× is
  rejected before parsing (a "zip bomb" can expand a few kilobytes into gigabytes);
- **never executing** document content: PDF JavaScript, DOCX macros and embedded objects are ignored;
- after a **malware scan** of the raw file.

Parsed text also passes a **prompt-injection screen** before it is indexed: text addressed to an AI
("ignore previous instructions", "you are now…") is flagged, and flagged chunks are excluded from
generation context while staying searchable for administrators. Doc
[13](13-access-control-security-and-privacy.md) §6 explains why this is a screen and not the whole
defence.

---

## What to take away

1. **The guarantee is only as good as the parse.** The verifier checks claims against stored text; if
   the stored text is a wrong transcription, verified claims are still wrong.
2. **Normalise only invisibly.** NFC, ligatures and soft hyphens are safe; converting digits or
   correcting OCR spelling changes what a verbatim quote says.
3. **Score parse quality and act on it.** Low-confidence OCR is indexed but never quoted in strict
   mode, and its share of the corpus is reported weekly.
4. **Every sentence gets its own locator.** Chunks straddle pages; citations must not.
5. **Chunk along structure, and glue every exception to its rule.** The qualifier rule moved
   rule-plus-exception co-location from 81% to 99.6%.
6. **Drop overlap; expand at query time instead.** Overlap inflates the index by a third and fills
   top-5 slots with duplicates; neighbours fetched for admitted chunks give the same context where it
   is needed.
7. **Repeat the header on every table chunk, and render table rows with their column names.** A
   number without its column is evidence the verifier cannot interpret.
8. **Breadcrumbs give most of the benefit of LLM-generated chunk context at none of the cost.** At
   Kestrel's update rate, generated context everywhere would cost $42,500 a month.
9. **Treat parsers as hostile-input processors.** Sandbox, no network, hard limits, no execution.

Continue to [05 — The ingestion pipeline and versions](05-ingestion-pipeline-and-versions.md).
