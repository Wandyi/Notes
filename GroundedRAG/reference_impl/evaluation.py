#!/usr/bin/env python3
"""
evaluation.py - the statistics behind Lodestar's evaluation programme (doc 14), runnable.

Run:  python3 evaluation.py      (exits non-zero if any number drifts from the docs)

Four things live here, because each is a place where evaluation programmes quietly lie:

  1. Sample sizes. How many labelled questions it takes before "recall@5 = 91%" means
     anything, and how many claims before "zero unsupported claims observed" means anything.
  2. The precision@5 ceiling. Most questions have one or two relevant chunks, so a perfect
     retriever scores about 40% precision@5. The requirement as written cannot be met.
  3. Averages that hide a language. Recall decomposed into candidate recall x reranker
     recall, per language, weighted by query mix: the blend passes while Hindi fails.
  4. Labels that survive re-chunking. Relevance is labelled as a character span in a
     document version, and a chunk is relevant if it covers the span. Change the chunker and
     the labels still hold.

It also estimates the true unsupported-claim rate from an LLM judge's flags, correcting for
the judge's measured precision and recall.

Stdlib only.
"""

from __future__ import annotations

import math
import sys

_failures: list = []
VERBOSE = "--verbose" in sys.argv


def check(name: str, actual: float, expected: float, rel_tol: float = 0.005) -> None:
    ok = math.isclose(actual, expected, rel_tol=rel_tol, abs_tol=1e-9)
    if VERBOSE or not ok:
        print(f"  [{'ok  ' if ok else 'DRIFT'}] {name}: {actual:,.4f} (docs say {expected:,.4f})")
    if not ok:
        _failures.append(name)


def section(title: str) -> None:
    print(f"\n== {title}")


Z95 = 1.959964


# ---------------------------------------------------------------------------------------
# 1. Sample sizes
# ---------------------------------------------------------------------------------------
def n_for_proportion(p: float, margin: float, z: float = Z95) -> int:
    """Questions needed so a measured proportion near p has a +/- margin 95% interval."""
    return math.ceil(z * z * p * (1 - p) / (margin * margin))


def n_for_zero_failures(rate: float, confidence: float = 0.95) -> int:
    """Observations with zero failures needed to claim the true rate is below `rate`."""
    return math.ceil(math.log(1 - confidence) / math.log(1 - rate))


def wilson(successes: float, n: int, z: float = Z95) -> tuple:
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


section("1. Sample sizes")
n_recall = n_for_proportion(0.90, 0.02)
n_abstain = n_for_proportion(0.95, 0.03)
golden = 3 * (n_recall + n_abstain)
check("answerable questions per language (recall 90% +/- 2pt)", n_recall, 865)
check("unanswerable questions per language (95% +/- 3pt)", n_abstain, 203)
check("golden set size, three languages", golden, 3_204)
check("annotator hours at 6 min per question", golden * 6 / 60, 320.4)
check("claims with zero failures to show rate < 0.1%", n_for_zero_failures(0.001), 2_995)
check("claims with zero failures to show rate < 0.01%", n_for_zero_failures(0.0001), 29_956)
check("rule of three for 0.1%", 3 / 0.001, 3_000)

# What a 200-question "eval set" can and cannot tell you
lo, hi = wilson(182, 200)
check("200 questions, 91% measured: lower bound (%)", 100 * lo, 86.22)
check("200 questions, 91% measured: upper bound (%)", 100 * hi, 94.23)
lo, hi = wilson(0.91 * 865, 865)
check("865 questions, 91% measured: lower bound (%)", 100 * lo, 88.91)

# ---------------------------------------------------------------------------------------
# 2. The precision@5 ceiling
# ---------------------------------------------------------------------------------------
# Share of golden questions by number of relevant chunks in the whole corpus (Lodestar's
# pilot labels; replace with yours). "5" means five or more.
RELEVANT_COUNT_MIX = {1: 0.46, 2: 0.27, 3: 0.14, 4: 0.06, 5: 0.07}

section("2. Precision@5 ceiling")
ceiling = sum(share * min(r, 5) / 5 for r, share in RELEVANT_COUNT_MIX.items())
check("best possible mean precision@5 (%)", 100 * ceiling, 40.2)
check("questions where precision@5 >= 80% is possible (%)",
      100 * sum(s for r, s in RELEVANT_COUNT_MIX.items() if min(r, 5) / 5 >= 0.8), 13.0)


def context_precision(admitted: list, relevant: set) -> float:
    """Precision over what was actually put in front of the model (dynamic k)."""
    return sum(c in relevant for c in admitted) / len(admitted) if admitted else 1.0


# A single-fact question: one relevant chunk. Fixed k=5 caps precision at 20%; a reranker
# threshold that admits two chunks scores 50%, and admitting only the right one scores 100%.
check("fixed k=5, one relevant (%)", 100 * context_precision(["c7", "c2", "c9", "c4", "c1"], {"c7"}), 20.0)
check("dynamic k=2, one relevant (%)", 100 * context_precision(["c7", "c2"], {"c7"}), 50.0)
check("dynamic k=1, one relevant (%)", 100 * context_precision(["c7"], {"c7"}), 100.0)

# ---------------------------------------------------------------------------------------
# 3. Recall decomposition per language
# ---------------------------------------------------------------------------------------
# recall@5 = P(relevant chunk in the 40 fused candidates) x P(reranker keeps it in top 5)
LANG = {                       # candidate recall@40, reranker conditional, query share
    "en": (0.970, 0.950, 0.64),
    "es": (0.965, 0.940, 0.22),
    "hi": (0.930, 0.920, 0.14),
}
section("3. Recall decomposition")
per_lang = {k: c * r for k, (c, r, _) in LANG.items()}
blend = sum(per_lang[k] * LANG[k][2] for k in LANG)
check("English recall@5 (%)", 100 * per_lang["en"], 92.15)
check("Spanish recall@5 (%)", 100 * per_lang["es"], 90.71)
check("Hindi recall@5 (%)", 100 * per_lang["hi"], 85.56)
check("query-weighted blend (%)", 100 * blend, 90.91)
check("Hindi shortfall against 90% (points)", 100 * (0.90 - per_lang["hi"]), 4.44)
# Fixing candidate generation alone (0.930 -> 0.980) only just clears 90%, with no margin.
check("Hindi with candidate recall 0.98 (%)", 100 * 0.98 * 0.92, 90.16)

# ---------------------------------------------------------------------------------------
# 4. Span labels that survive re-chunking
# ---------------------------------------------------------------------------------------
def chunk_spans(doc_len: int, size: int) -> list:
    return [(i, min(i + size, doc_len)) for i in range(0, doc_len, size)]


def relevant_chunks(spans: list, evidence: tuple, min_cover: float = 0.5) -> set:
    """A chunk is relevant if it covers at least half of the labelled evidence span."""
    s0, s1 = evidence
    out = set()
    for idx, (c0, c1) in enumerate(spans):
        overlap = max(0, min(c1, s1) - max(c0, s0))
        if overlap >= min_cover * (s1 - s0):
            out.add(idx)
    return out


section("4. Span labels across two chunkers")
DOC_LEN = 24_000                 # characters, a 6-page policy
EVIDENCE = (9_310, 9_520)        # the per-diem table row, labelled once
old = relevant_chunks(chunk_spans(DOC_LEN, 1_700), EVIDENCE)
new = relevant_chunks(chunk_spans(DOC_LEN, 1_200), EVIDENCE)
check("relevant chunk under 1,700-char chunking", min(old), 5)
check("relevant chunk under 1,200-char chunking", min(new), 7)
check("labels needing rework after re-chunking", 0, 0)
# A label stored as a chunk ID would now point at chunk 5 of the new chunking, which covers
# characters 6,000-7,200 and has nothing to do with the per-diem.
stale = chunk_spans(DOC_LEN, 1_200)[5]
check("stale chunk-ID label start (chars)", stale[0], 6_000)

# ---------------------------------------------------------------------------------------
# 5. Estimating the true unsupported-claim rate from judge flags
# ---------------------------------------------------------------------------------------
section("5. Production hallucination estimate")
DAILY_ANSWERS_SAMPLED = 9_000            # 2% of ~450k answered queries
CLAIMS_PER_ANSWER = 3.2
JUDGE_RECALL = 0.85                      # measured on the perturbation suite, doc 14
claims_judged = DAILY_ANSWERS_SAMPLED * CLAIMS_PER_ANSWER
flags, confirmed = 41, 30                # one day: judge flags, humans confirm
judge_precision = confirmed / flags
estimated_true = confirmed / JUDGE_RECALL
rate = estimated_true / claims_judged
lo, hi = wilson(estimated_true, int(claims_judged))
check("claims judged per day", claims_judged, 28_800)
check("judge precision that day (%)", 100 * judge_precision, 73.17)
check("estimated unsupported claims", estimated_true, 35.29)
check("estimated rate (%)", 100 * rate, 0.1225)
check("95% upper bound (%)", 100 * hi, 0.1701)
# Without the recall correction the dashboard would read 30 / 28,800:
check("uncorrected rate (%)", 100 * confirmed / claims_judged, 0.1042)

print()
if _failures:
    print(f"DRIFT in {len(_failures)} number(s): {', '.join(_failures)}")
    sys.exit(1)
print("evaluation.py: every number matches the docs.")
