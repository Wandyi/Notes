# 11 — Multilingual: English, Hindi and Spanish

Doc [00](00-requirements-and-what-zero-hallucination-can-mean.md) §3.9 argued that "support three
languages" really means four hard things: questions in any language retrieve evidence in any
language; answers come back in the question's language; quotes stay in the source's language; and
verification works across the gap. It also pointed out a fourth language hiding inside the third:
Hindi typed in Latin script.

This document works through each. It is also where the collection's most important measurement
warning lives: **a system can pass its recall target on average while failing it for one
language**, and at Kestrel that language is Hindi.

---

## 1. Who asks what, in which language

| | English | Spanish | Hindi |
|---|---:|---:|---:|
| Share of the corpus | 70% | 18% | 12% |
| Share of questions | 64% | 22% | 14% (a third of it romanized) |

Which language the *answer* comes from is a different question. In Kestrel's pilot:

| Question in | Evidence in English | Evidence in Spanish | Evidence in Hindi |
|---|---:|---:|---:|
| English | 96% | 3% | 1% |
| Spanish | 45% | 55% | — |
| Hindi (either script) | 72% | — | 28% |

So **72% of Hindi questions are answered from English documents**: global policies that apply in
India, written in English. Nearly half of Spanish questions are too. Cross-lingual retrieval is not
an edge case; for a third of Kestrel's users it is the normal case.

---

## 2. What each language costs in tokens

A **tokenizer** splits text into the units a model reads. Tokenizers are built mostly from English
text, so they represent English efficiently and other scripts less so. The same sentence takes more
tokens in Hindi than in English — Lodestar measured about **1.6 times as many** with its generator's
tokenizer *(assumption: measure your own)*, and about 1.15 times for Spanish.

That has three consequences:

- **The context budget holds less Hindi.** 3,000 tokens of context hold about 1,900 tokens' worth of
  English-equivalent Hindi content. The variable-k rule (doc [07](07-retrieval-reranking-and-context.md)
  §7) admits fewer Hindi chunks before the budget is reached.
- **Hindi answers cost more.** If the context and output are Hindi, input rises from 4,390 to about 6,190
  tokens and output from 150 to about 240, so a generation costs about **$0.0064** against $0.0042 — 54%
  more, using doc [01](01-the-running-system-and-its-numbers.md) §6's prices.
- **Hindi answers are slower.** 240 output tokens at the same tokens per second take 50% longer to
  generate than 150. Hindi answers are therefore the ones closest to the p99 deadline (doc
  [12](12-latency-budget-and-caching.md) §4).

The embedding model and reranker use a different tokenizer, built for a hundred languages, where the gap is
much smaller; that is why chunk sizes are defined in *embedder* tokens (doc
[04](04-parsing-and-chunking.md) §6).

---

## 3. Retrieving across languages

### What the multilingual embedder does

The embedder (doc [06](06-indexing-at-120m-chunks.md) §8) was trained so that a sentence and its
translation map to nearby vectors. *"मैड्रिड के लिए दैनिक भत्ता कितना है?"* ("What is the daily allowance
for Madrid?") lands near *"Employees travelling to Madrid may claim a per-diem of €85 per day"*. That is
what makes cross-lingual retrieval possible at all; the keyword index cannot help, beyond shared codes,
numbers and place names.

### Same-language bias

Multilingual embedders are not perfectly language-neutral. Given a Hindi question, a Hindi passage that is
only loosely related often scores higher than an English passage that answers the question exactly —
because the shared script and vocabulary add similarity that has nothing to do with meaning. At Kestrel
this showed up as Hindi questions about global policies retrieving Hindi HR circulars that mention the topic
in passing.

### Dual-query search

The fix is to search twice. For every non-English question, a small **machine translation model** — a
dedicated translation model, not an LLM — translates the question to English in about 25 ms on the online
L4s, in parallel with embedding the original. Both are searched, and both result lists go into reciprocal
rank fusion (doc [07](07-retrieval-reranking-and-context.md) §4) alongside the keyword results.

| Hindi questions, candidate recall@40 | |
|---|---:|
| Original question only | 93.0% |
| Original plus English translation | **96.8%** |

The translation is used **only for retrieval**. It is never shown and never used as evidence, so a
translation error can only cost recall, never introduce a claim. It runs for the 36% of questions that are
not in English, and adds no latency because it runs in parallel with work that takes longer.

---

## 4. Romanized Hindi

About a third of Kestrel's Hindi questions are typed in Latin script, and many mix in English:

> "leave policy mein kitne din milte hain?" (how many days does the leave policy give?)
> "Madrid trip ka per diem kitna hai"
> "chhutti ke liye kaun approve karta hai" (who approves leave?)

There is no standard spelling. छुट्टी (leave) appears as *chhutti*, *chutti*, *chuttee* and *chhuti*. The
same question in Devanagari would be easy for the embedder; in ad hoc Latin spelling it is much harder, and
the keyword index cannot match *chutti* to छुट्टी at all.

Three techniques together handle it:

1. **Detect it.** The language detector labels it `hi-Latn`, using character patterns (*kh, chh, aa*,
   common particles like *ka, ke, mein, hai*) that distinguish it from English.
2. **Transliterate the query to Devanagari** with a transliteration model (about 5 ms), and embed that. "chhutti
   ke liye kaun approve karta hai" becomes "छुट्टी के लिए कौन अप्रूव करता है", which the embedder handles well.
   The English words ("approve") are transliterated too, which is fine: the embedder has seen Hindi text with
   English loanwords in Devanagari.
3. **Search the romanized keyword field** (`text_hi_rom`, doc [06](06-indexing-at-120m-chunks.md) §7) with
   edit-distance tolerance, so *chutti* matches the indexed romanization *chhutti*. And search the English
   field too, because the English words in a mixed question ("leave policy", "per diem") match English
   documents directly.

The answer to a romanized Hindi question is written in **Devanagari Hindi**, unless the user's profile says
otherwise. Kestrel's pilot users preferred that by a wide margin; romanized typing is about keyboards, not
about wanting to read romanized text.

---

## 5. The Hindi recall plan

Doc [07](07-retrieval-reranking-and-context.md) §5 decomposed recall@5 per language:

| Language | Candidate recall@40 | Reranker keeps it | Recall@5 |
|---|---:|---:|---:|
| English | 97.0% | 95.0% | 92.2% |
| Spanish | 96.5% | 94.0% | 90.7% |
| **Hindi** | **93.0%** | **92.0%** | **85.6%** |
| Blend, weighted by question share | | | 90.9% |

The blend passes. **A system judged by the blend would ship with Hindi 4.4 points below target**, and the
14% of users asking in Hindi — disproportionately plant and field staff — would get the worst experience
without anyone being told. That is why every retrieval target is per language (doc
[00](00-requirements-and-what-zero-hallucination-can-mean.md) §8).

The decomposition also shows that both factors need work, because fixing only one does not get there with
margin. Three changes, each measured in a pilot experiment:

| Change | Factor it moves | Effect |
|---|---|---|
| Dual-query search (§3) | Candidate recall | 93.0% → 96.8% |
| Devanagari OCR tuning for scanned circulars: 31% of Hindi documents are scans, against 12% of English ones | Candidate recall | Included in the 96.8% |
| Reranker fine-tuned on 3,000 labelled Hindi and romanized-Hindi pairs from pilot logs | Reranker conditional | 92.0% → 94.5% |

> Hindi recall@5 after both: 0.968 × 0.945 = **91.5%**, 1.5 points of margin

Spanish passes by only 0.7 points, so the same reranker fine-tuning runs on Spanish data too. English, with
2.2 points of margin, is left alone.

---

## 6. Numbers, dates and currencies: the locale decides

Doc [08](08-grounded-generation-and-verification.md) §5.1 showed the quantity check, and why it must parse
each side in its own locale. The conventions it has to know:

| Locale | One thousand five hundred and a half | Large amounts | Dates | Currency notes |
|---|---|---|---|---|
| en-US | 1,500.50 | 1,500,000 | 04/01/2025 = **April 1** | `$` is US dollars |
| en-IN | 1,500.50 | **15,00,000** (fifteen lakh) | 01/04/2025 = **1 April** | `₹`, "Rs.", "INR"; "lakh" = 10⁵, "crore" = 10⁷ |
| hi-IN | 1,500.50 or १,५००.५० | 15,00,000 or १५,००,००० | 01/04/2025 | `₹`, रुपये; लाख, करोड़ |
| es-ES | **1.500,50** | 1.500.000 | 01/04/2025 = 1 April | `€`; "mil" = 10³, "millones" = 10⁶ |
| es-MX | 1,500.50 | 1,500,000 | 01/04/2025 = 1 April | **`$` is Mexican pesos** |

Four rules follow, all implemented and tested in [reference_impl/grounding.py](reference_impl/grounding.py):

1. **Locale is a property of the document version, stored in the ledger** (`doc_versions.locale`, doc
   [03](03-apis-and-data-model.md) §7). It is set from the source (the Spain HR site is `es-ES`, the Mexico
   City site `es-MX`) and confirmed by detection. Language alone is not enough: es-ES and es-MX disagree on
   which symbol is the decimal point, so `1.500` is one and a half in Mexico and fifteen hundred in Spain.
2. **Devanagari digits are kept in stored text and normalised only when comparing.** The quote must match the
   document (doc [04](04-parsing-and-chunking.md) §3); the comparison must see १५ and 15 as equal.
3. **Indian grouping is recognised by its shape**, regardless of locale: `1,50,000` can only mean 150,000.
4. **An ambiguous date in a document of unknown locale is not guessed.** `03/04/2025` in a document whose locale
   could not be established makes any claim citing it a *doubt*, which sends it to the judge. Guessing would be
   right half the time and silently wrong the other half.

Worked through, a claim in en-US citing a sentence in es-ES:

> Evidence (es-ES): "La dieta diaria para Ciudad de México es de 1.500,50 pesos."
> Claim (en-US): "The daily allowance for Mexico City is 1,500.50 pesos." → evidence 1500.5 MXN, claim 1500.5 MXN:
> **passes**
> Claim (en-US): "The daily allowance for Mexico City is 1.500 pesos." → claim 1.5 MXN: **fails**

---

## 7. Which language the answer is in

| Part of the answer | Language | Why |
|---|---|---|
| Claims | The question's language (Devanagari Hindi for romanized questions) | The user reads them |
| Quotes in citations | **The source's language, verbatim** | Guarantee G1: the quote is the document's own words |
| Verbatim notes (qualifiers, conflicts) | The source's language, verbatim | Same |
| Template text ("The sources disagree", "Note from the source") | The question's language | Fixed, translated once by people |
| The standalone question used for retrieval | Usually English | Most evidence is English; the answer language is decided separately (doc 10 §3) |

### Translating quotes

A Hindi-speaking plant supervisor reading an English quote under a Hindi claim may not be able to check it.
Lodestar offers a **machine translation of the quote**, collapsed by default, labelled *"Machine translation —
the original above is the source"*.

This translation is generated text, and it is **outside the guarantee**. The guarantee covers the claim (which
was verified against the original sentence across languages) and the quote (which is the document's own text).
The translation is a reading aid, and the label says so. In strict mode — where the answer *is* the quotes —
this matters more, because a Hindi-speaking user may rely on the translation entirely. For strict-mode answers
the label is shown expanded rather than collapsed, and the product decision to offer translation at all in
strict mode was made explicitly by Legal and HR rather than by default.

---

## 8. Verifying across languages

A Hindi claim checked against an English sentence goes through the same pipeline as an English one (doc
[08](08-grounded-generation-and-verification.md) §5), with each check language-aware:

- **Quantities**: each side parsed in its own locale (§6).
- **Polarity, modality, scope, qualifiers**: each side checked with its own language's cue lists — *नहीं* in the
  claim against *not* in the evidence.
- **Entailment**: the multilingual entailment model accepts a Hindi hypothesis and an English premise directly.

Cross-lingual entailment is harder than same-language entailment, and that shows up in the residual. On
Kestrel's verifier evaluation set, after fine-tuning:

| Claim → evidence | Entailment false-accept rate |
|---|---:|
| English → English | 2.6% |
| Spanish → Spanish | 2.9% |
| Spanish → English | 3.4% |
| Hindi → English | 4.1% |
| Hindi → Hindi | 3.6% |
| **Weighted by traffic** | **3.0%** — the figure doc 08 §10 uses |

So **M1 is reported per language pair**, not only in aggregate. Hindi-to-English claims leak at roughly one and
a half times the rate of English ones, and the plant staff reading them deserve to have that number watched.
The fine-tuning data for the entailment model (doc [08](08-grounded-generation-and-verification.md) §10) is
deliberately weighted towards cross-lingual pairs for this reason.

---

## 9. Mixed-language documents

About 6% of Kestrel's Hindi documents mix scripts and languages within the document: a Hindi circular with an
English table of allowances, or an English legal notice with a Hindi translation underneath. Some Mexican
documents mix Spanish and English section by section.

Language is therefore detected **per block** at parsing (doc [04](04-parsing-and-chunking.md) §8), and each
chunk is assigned the language of most of its text. Each sentence keeps its own language for verification. A
chunk with a Hindi paragraph and an English table row is indexed in both the Hindi and English keyword fields.

---

## 10. Evaluating each language separately

The golden set (doc [14](14-evaluation.md) §2) has **865 answerable and 203 unanswerable questions per
language** — sized so each language's recall is measured to ±2 points on its own, not borrowed from the others.
Within Hindi, about a third of questions are romanized, matching traffic. Questions are written by native
speakers from each region who know the documents, not translated from English: translated questions read like
translations, and real users do not write that way.

Every release gate (doc [14](14-evaluation.md) §5) is evaluated per language. A change that raises English
recall by two points and lowers Hindi by one is a **failed** change, whatever it does to the blend.

---

## What to take away

1. **Cross-lingual is the normal case.** 72% of Kestrel's Hindi questions are answered from English documents.
2. **Measure every retrieval target per language.** The blend passed at 90.9% while Hindi was at 85.6%.
3. **Search twice for non-English questions.** A small translation model's English query, fused with the original,
   lifted Hindi candidate recall from 93.0% to 96.8% — and since the translation is only used for search, it can
   only help.
4. **Romanized Hindi needs detection, transliteration and a fuzzy romanized field.** No single technique is
   enough.
5. **Locale, not language, decides how numbers are written.** Spain and Mexico disagree on the decimal point;
   India groups by lakh; `$` is pesos in Mexico. Store locale per document version.
6. **Never guess an ambiguous date.** Make the claim doubtful and let the judge decide.
7. **Claims in the user's language; quotes in the source's language; translations labelled and outside the
   guarantee.**
8. **Report the hallucination residual per language pair.** Hindi-to-English claims leak about one and a half
   times as often as English ones.
9. **Budget for Hindi tokens.** About 1.6× the tokens, about 54% more cost per answer and the slowest answers in
   the system.

Continue to [12 — The latency budget and caching](12-latency-budget-and-caching.md).
