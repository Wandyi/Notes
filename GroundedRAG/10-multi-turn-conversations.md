# 10 — Multi-Turn Conversations

A third of Lodestar's queries are follow-ups. They are short, they lean on what came before, and
they cannot be retrieved as written: the words "what about contractors?" retrieve contractor
onboarding guides, vendor payment terms and a dozen other things, none of which is the per-diem
rule the user means.

This document covers how a follow-up is turned into something retrievable, how the system shows
the user what it understood, why the model's own previous answers are never treated as evidence,
and what happens when a document cited three turns ago has changed since.

---

## 1. A conversation, and what each turn needs

Priya, in Pune, over about two minutes:

```
Turn 1  Priya:    Can I claim per-diem for a three-day trip to Madrid?
        Lodestar: Yes. Employees travelling to Madrid can claim a per-diem of €85 per day. [1]
                  For a three-day trip that is €255. [1] ...
                  [1] Travel and Expense Policy (FIN-POL-017), version 4, page 7

Turn 2  Priya:    what about contractors?

Turn 3  Priya:    aur interns ke liye?          ("and for interns?", romanized Hindi)

Turn 4  Priya:    What did it say about receipts?
```

Each follow-up depends on the conversation in a different way:

| Turn | What it leaves out | What it needs from earlier turns |
|---|---|---|
| 2 | The topic (per-diem), the place (Madrid), the document | Everything except the new population, "contractors" |
| 3 | Everything, in a different language and script | The same, with "interns"; the answer should be in Hindi |
| 4 | Which "it" | That "it" is the Travel and Expense Policy cited in turn 1 |

A system that retrieves on the raw text of turns 2–4 fails all three. A system that does
something sensible with them has to make a decision the verifier cannot check: *what did the user
mean?* That makes conversation handling a hallucination risk of its own — the "interpretation
error" row of doc [00](00-requirements-and-what-zero-hallucination-can-mean.md) §4 — and the design
below treats it as one.

---

## 2. What the conversation store keeps

Each turn is stored (doc [03](03-apis-and-data-model.md) §7) with:

- the question exactly as asked;
- the **standalone question** the system resolved it to (§3);
- the filters applied;
- the outcome (`VERIFIED`, `PARTIAL` …);
- the claims, each with its **sentence keys** (`chk_7f3a:2`) and cited document versions — references,
  not quote text.

Conversations expire 30 days after their last turn. They are stored under a pseudonymised user ID (doc
[13](13-access-control-security-and-privacy.md) §7).

What is **not** passed forward into later turns is just as important: the full text of previous answers.
§5 explains why.

---

## 3. Turning a follow-up into a standalone question

### Two naive approaches

**Retrieve on the raw follow-up.** "what about contractors?" has one content word. Retrieval returns
whatever is most about contractors. The generator, given those chunks and the conversation, may well
write a fluent and well-cited answer about contractor onboarding. Every claim would verify. The answer
would be to a question Priya did not ask.

**Concatenate the previous question.** Retrieve on *"Can I claim per-diem for a three-day trip to Madrid?
what about contractors?"*. This works surprisingly often, and costs nothing. It fails when the
conversation has shifted topic (turn 4's "receipts" is not about Madrid), when references span more than
one turn back, and when the follow-up is in another language (turn 3).

### Condensation

Lodestar **condenses**: a small model rewrites the follow-up into a standalone question, given a compact
view of the conversation. Its input, for turn 3:

```
Recent turns (most recent last):
  T1 asked: "Can I claim per-diem for a three-day trip to Madrid?"
     resolved: "Can an employee claim per-diem for a three-day trip to Madrid?"
     cited: [R1] Travel and Expense Policy (FIN-POL-017) v4
  T2 asked: "what about contractors?"
     resolved: "Can a contractor claim per-diem for a trip to Madrid?"
     cited: [R1] Travel and Expense Policy (FIN-POL-017) v4

New question: "aur interns ke liye?"

Return JSON with: standalone_question, answer_language, referenced (a subset of the R-ids above,
or empty), filter_changes.
```

Its output:

```json
{
  "standalone_question": "Can an intern claim per-diem for a trip to Madrid?",
  "answer_language": "hi",
  "referenced": ["R1"],
  "filter_changes": {}
}
```

Four design choices are visible in that exchange:

1. **The condenser sees questions and citations, not answers.** It needs to know what was asked and
   which documents were involved. It does not need — and should not be influenced by — what the
   previous answers *said*. That also keeps its input to about 650 tokens.
2. **References are chosen from a list.** The condenser can only point at `R1`, `R2` … — documents that
   were actually cited earlier. It cannot invent a document, any more than the generator can invent a
   sentence (doc [08](08-grounded-generation-and-verification.md) §2). The same idea, applied one step
   earlier.
3. **The answer language is decided here.** Turn 3 is romanized Hindi, so the answer is in Hindi, even
   though the standalone question is written in English for retrieval (doc
   [11](11-multilingual-en-hi-es.md) §7).
4. **Filter changes are explicit.** "What about last year's policy?" becomes `{"effective_on":
   "2025-10-21"}`, which is shown to the user as a filter chip like any other.

### Which model, and what it costs

The condenser is a small instruction-following model (about 3 billion parameters) running on the same H100s
as the reranker, which have spare capacity outside the peak (doc
[01](01-the-running-system-and-its-numbers.md) §7: the rerank load is 1.84 GPU-seconds per second at full
provisioned capacity, on three GPUs). Self-hosting it instead of calling the hosted model matters for
latency, not cost:

| Condenser | p50 | p99 | Follow-up p50 end to end |
|---|---:|---:|---:|
| Hosted fast-tier model | 200 ms | 520 ms | **1,543 ms** — over the 1,500 ms target |
| Self-hosted 3B model on the H100s | 150 ms | 400 ms | **1,490 ms** |

Both rows come from the simulation in doc [12](12-latency-budget-and-caching.md) §3. The 50 ms difference
is the whole margin. If the self-hosted condenser's quality on the follow-up golden set (§8) fell more than
a point below the hosted model's, the trade would need revisiting — but it would be a trade between two
measured numbers, not a guess.

The cost line in doc [01](01-the-running-system-and-its-numbers.md) §6 ($0.00018 per query, amortised) is
priced at hosted rates, so it is conservative.

---

## 4. Showing the user what was understood

The condenser's output is an LLM's interpretation of what the user meant. **Nothing downstream verifies
it.** The verifier checks that the answer is supported by the evidence; it has no way to check that the
evidence is about what the user had in mind. If the condenser resolves "that policy" to the wrong policy,
every claim in the answer can verify perfectly, against the wrong document.

The only party who can check an interpretation is the user. So it is the first thing they see:

```
Interpreting as: "Can an intern claim per-diem for a trip to Madrid?"
in Travel and Expense Policy (FIN-POL-017)                                        [edit]
```

This is the `interpretation` event at the start of every response stream (doc
[03](03-apis-and-data-model.md) §3). It arrives about 200 ms after the question, before any claim. If it
is wrong, the user can edit it, and the edited text is used as the standalone question — no second
condensation.

It is shown for **every** follow-up, even when the interpretation looks obvious, for two reasons. A user
cannot notice an error in an interpretation they are not shown. And consistent display makes the edit
affordance familiar, so it gets used in the cases that matter.

For first-turn questions, the interpretation line shows only when the system changed something — an
applied filter chip, a recognised document code, a language other than the one the user's profile
expects.

---

## 5. The model's own previous answers are not evidence

### The failure

In a naive multi-turn design, the generator's prompt contains the full conversation: previous questions
*and previous answers*. Consider what that does at turn 4:

> Turn 1 answer (in history): "…a per-diem of €85 per day…"
> Turn 4 question: "What did it say about receipts?"

Between turns 1 and 4, Finance's update to version 5 went live, and the Madrid per-diem is now €90. The
generator, writing about receipts, adds a helpful reminder: *"As mentioned, the per-diem is €85 per day,
and receipts are not required."* The €85 comes from its own earlier answer, sitting in the prompt. Nothing
in the current evidence says €85.

In Lodestar this cannot reach the user, for two independent reasons:

1. **Previous answer text is not in the generator's prompt.** The history it receives is the condensed form
   — standalone questions and cited document titles, about 250 tokens. There is no €85 in it to copy.
2. **Even if it were, it could not be cited.** A claim must cite sentence IDs from the *current* context,
   and the verifier checks quantities against those sentences only. A claim carrying €85 against a version 5
   sentence that says €90 fails the quantity check.

The general rule: **the system's own earlier output is never a source.** It may be shown to the user as
history. It may not be fed back as evidence.

---

## 6. Retrieval for follow-ups: prefer what was cited, without requiring it

When the condenser marks a previous document as referenced (`"referenced": ["R1"]`), retrieval **boosts**
that document: chunks from it get a fixed bonus in fusion (doc [07](07-retrieval-reranking-and-context.md)
§4). It does not **restrict** retrieval to it.

The difference matters at turn 3. Interns' per-diem eligibility is not in the travel policy at all; it is
in the *Internship Programme Guidelines*, which says interns receive "50% of the standard travel
allowances". A hard filter to `FIN-POL-017` would find nothing about interns and abstain, or worse, find
the general eligibility sentence and answer from it. A boost keeps the travel policy near the top while
letting the guidelines in.

Only when the user explicitly scopes the question — "in that policy", "en ese documento", "उसी पॉलिसी में" —
does the condenser emit a document filter instead of a boost, and the filter appears as a chip.

---

## 7. When a cited document has changed since an earlier turn

Each stored turn records the version of every document it cited. At each follow-up, the orchestrator
compares the versions referenced in the condensed history with the ledger's current state:

| What happened since | What the user sees, before the answer |
|---|---|
| A new version went live | *"Travel and Expense Policy has been updated since my earlier answer (version 4 → 5). This answer uses version 5."* |
| The document is in blackout | *"Travel and Expense Policy is being updated and was not used for this answer."* |
| The document was deleted | *"A document I cited earlier has been removed and can no longer be used."* |
| The user lost access | Nothing that names the document — the condenser's reference to it is silently dropped |

The first row matters most. Without it, Priya would see €85 in turn 1 and €90 in turn 4, in the same
conversation, with no explanation — and the natural conclusion is that the assistant is unreliable. With
it, the change is visible and attributed.

When a user scrolls back through history, earlier quotes are re-hydrated through the ledger (doc
[09](09-freshness-updates-and-deletion.md) §4). A quote from the superseded version 4 shows as `[source
superseded]`, with a link to the current version.

---

## 8. Limits, budgets and language

**How much history.** The condenser sees the last **three** turns. Kestrel's pilot found that references
more than three turns back were rare (under 2% of follow-ups), and the ones that occurred were usually to the
first turn's document, which is still visible in turn 3's "cited" list because each turn carries its
predecessor's references forward. The full conversation remains in the store for display.

**Language switching.** Turn 3 switched from English to romanized Hindi. The answer language follows the
**latest** question, so turn 3's answer is in Hindi and turn 4's in English. The standalone question is
always written in the language best suited to retrieval — English when the referenced documents are English
— while the original question's language decides the answer's.

**Long conversations.** A conversation with more than 30 turns usually means the user is exploring rather than
asking, and the condensed history stops helping. After 30 turns, the interface suggests starting a new
conversation. This is a product nudge, not a hard limit.

---

## 9. Evaluating conversations

Follow-ups get their own slice of the golden set: **400 conversations of two to four turns**, written by
annotators from the three regions, in all three languages and with language switches, each turn labelled with
its correct standalone question and its evidence spans. Three measures are tracked on it:

| Measure | What it catches | Pilot result |
|---|---|---:|
| **Interpretation accuracy**: the standalone question is judged equivalent to the labelled one | Wrong referent, lost constraint, invented constraint | 94.5% |
| **Follow-up recall@5**: as in doc 07, on follow-up turns only | Retrieval failures specific to condensed questions | 89.8% |
| **Edit rate**: production follow-ups where the user edited the interpretation | Interpretation errors users noticed | 1.9% |

Follow-up recall@5 was just under the 90% target in the pilot, and almost all of the shortfall was in turns
that switched language. That is tracked as its own line in the release gate (doc [14](14-evaluation.md) §5).

The edit rate is the one production signal that measures interpretation directly. A rise in it after a
condenser change is a regression the golden set may not have caught.

---

## What to take away

1. **A follow-up is a hallucination risk before any evidence is retrieved.** If the interpretation is wrong, a
   perfectly verified answer is an answer to the wrong question.
2. **Condense with a small model that sees questions and citations, not answers**, and let it reference only
   documents that were actually cited.
3. **Show the interpretation on every follow-up, with an edit control.** The user is the only verifier for
   intent.
4. **The system's own earlier output is never a source.** Keep previous answer text out of the generator's
   prompt; the citation grammar would reject it anyway.
5. **Boost previously cited documents; do not restrict to them.** The answer to "and for interns?" is often in a
   different document.
6. **Tell the user when a document changed between turns.** A silent change from €85 to €90 inside one
   conversation reads as unreliability.
7. **Self-host the condenser if latency requires it.** At Kestrel it is the 50 ms that keeps follow-ups under
   the p50 target.
8. **Evaluate conversations separately.** Interpretation accuracy and the production edit rate measure
   something no single-turn metric can.

Continue to [11 — Multilingual: English, Hindi and Spanish](11-multilingual-en-hi-es.md).
