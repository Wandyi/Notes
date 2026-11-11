#!/usr/bin/env python3
"""
grounding.py - the deterministic half of Lodestar's claim verifier (doc 08), runnable.

Run:  python3 grounding.py          (runs the self-tests, exits non-zero on any failure)

The generator never writes evidence. It emits claims that *point at* sentence IDs it was
shown, in a line format like this:

    C1 [S1.1] Employees travelling to Madrid can claim a per-diem of EUR 85 per day.
    C2 [S1.3] {q:C1} Contractors are not eligible for the per-diem.
    C3 [S1.1] {calc:85*3=255} A three-day trip to Madrid gives EUR 255 of per-diem.

This module checks everything about a claim that can be checked without a model:

  1. Structure  - every claim cites at least one ID, and every ID was in the context.
  2. Quantities - every number, amount, percentage and date in the claim appears in the cited
                  sentences, after locale-aware normalisation. A claim in en-US citing a
                  sentence in es-ES is parsed with en-US rules and the evidence with es-ES rules.
  3. Polarity   - a negation in the evidence is not dropped in the claim, or added to it.
  4. Modality   - the claim is not stronger than the evidence ("may" never becomes "must").
  5. Scope      - the claim does not swap the population or place ("employees" -> "contractors").
  6. Arithmetic - a computed claim carries its expression, and the verifier recomputes it.

Then it applies the decision policy: deterministic FAIL drops the claim, GRAY forces model
review, the NLI score decides the rest, qualifier sentences the answer left out are attached
verbatim, and the answer gets one of four statuses.

The NLI model and the gray-zone judge are callables you pass in. The token-overlap function at
the bottom exists only so this file runs end to end. It is NOT a verifier.

Stdlib only.
"""

from __future__ import annotations

import ast
import math
import operator
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

# ---------------------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Sentence:
    sid: str                  # "S2.4": chunk 2 of the prompt, sentence 4
    text: str
    locale: str               # "en-US", "en-IN", "es-ES", "es-MX", "hi-IN"
    doc_id: str
    version_id: str
    locator: str              # "p.7" for paginated formats, "§3.2 ¶4" otherwise
    section: str              # section key, for neighbour lookup
    ordinal: int              # position within the section
    doc_scope: frozenset = frozenset()   # concepts implied by the document itself


@dataclass
class Claim:
    cid: str
    text: str
    cites: list
    locale: str
    qualifies: Optional[str] = None
    calc: Optional[str] = None


@dataclass
class Verdict:
    cid: str
    accepted: bool
    reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)     # sentence IDs shown verbatim under the claim
    nli: Optional[float] = None


VERIFIED, PARTIAL, EXTRACTIVE, ABSTAINED = "VERIFIED", "PARTIAL", "EXTRACTIVE", "ABSTAINED"

PASS, GRAY, FAIL = "PASS", "GRAY", "FAIL"

# ---------------------------------------------------------------------------------------
# Parsing the generator's output
# ---------------------------------------------------------------------------------------

CLAIM_LINE = re.compile(r"^C(\d+)\s+\[([^\]]*)\]\s*(\{[^}]*\})?\s*(.+)$")


def parse_output(raw: str, locale: str) -> tuple[list, bool]:
    """Returns (claims, not_found). Lines that do not parse are ignored, never repaired."""
    claims, not_found = [], False
    for line in raw.strip().splitlines():
        line = line.strip()
        if line == "NOT_FOUND":
            not_found = True
            continue
        m = CLAIM_LINE.match(line)
        if not m:
            continue
        cid = f"C{m.group(1)}"
        cites = [c.strip() for c in m.group(2).split(",") if c.strip()]
        opts = {}
        if m.group(3):
            for part in m.group(3)[1:-1].split(","):
                if ":" in part:
                    k, v = part.split(":", 1)
                    opts[k.strip()] = v.strip()
        claims.append(Claim(cid, m.group(4).strip(), cites, locale,
                            qualifies=opts.get("q"), calc=opts.get("calc")))
    return claims, not_found


# ---------------------------------------------------------------------------------------
# Sentence splitting (doc 04): the sentence is the unit of citation, so this is not cosmetic
# ---------------------------------------------------------------------------------------

ABBREVIATIONS = {
    "e.g.", "i.e.", "etc.", "no.", "dr.", "mr.", "ms.", "vs.", "approx.", "dept.",
    "sr.", "sra.", "art.", "pág.", "núm.", "aprox.", "ud.", "uds.",
    "rs.",
}


def split_sentences(text: str, lang: str) -> list:
    """Split on sentence terminators for the language.

    Hindi ends sentences with the danda U+0964 '।', not a full stop. Spanish opens questions
    and exclamations with '¿' and '¡', which belong to the following sentence. English and
    Spanish full stops after a known abbreviation do not end a sentence.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    terminators = "।?!" if lang == "hi" else ".?!"
    out, start, i = [], 0, 0
    while i < len(text):
        ch = text[i]
        if ch in terminators or (lang == "hi" and ch == "."):
            j = i + 1
            while j < len(text) and text[j] in "\"')»”":
                j += 1
            at_end = j >= len(text)
            next_ok = at_end or text[j] == " "
            last_word = text[start:i + 1].split(" ")[-1].lower()
            is_abbrev = ch == "." and last_word in ABBREVIATIONS
            is_decimal = ch == "." and i + 1 < len(text) and text[i + 1].isdigit()
            if next_ok and not is_abbrev and not is_decimal:
                out.append(text[start:j].strip())
                start = j
            i = j
            continue
        i += 1
    if text[start:].strip():
        out.append(text[start:].strip())
    return out


# ---------------------------------------------------------------------------------------
# Quantities: numbers, amounts, percentages, dates - normalised per locale
# ---------------------------------------------------------------------------------------

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Spain writes 1.500,50; Mexico writes 1,500.50. Decimal convention is a property of the
# country, not of the language, so it is keyed on the full locale.
DECIMAL_COMMA_LOCALES = {"es-ES"}

MONTHS = {
    # en
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # es
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
    # hi
    "जनवरी": 1, "फ़रवरी": 2, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
    "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "अक्तूबर": 10,
    "नवंबर": 11, "नवम्बर": 11, "दिसंबर": 12, "दिसम्बर": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

DATE_PATTERNS = [
    ("ymd", re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")),
    ("numeric", re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)")),
    ("d_month_y", re.compile(r"(?<!\d)(\d{1,2})\s+(?:de\s+)?(" + _MONTH_ALT + r")\s*,?\s+(?:de\s+)?(\d{4})(?!\d)",
                             re.IGNORECASE)),
    ("month_d_y", re.compile(r"(" + _MONTH_ALT + r")\s+(\d{1,2}),?\s+(\d{4})(?!\d)", re.IGNORECASE)),
]

MULTIPLIERS = [
    (r"mil\s+millones", 1e9), (r"crores?|करोड़", 1e7), (r"lakhs?|लाख", 1e5),
    (r"millones|millón|millions?", 1e6), (r"thousand|mil|हज़ार|हजार", 1e3),
]
_MULT_RE = re.compile(r"^\s*(" + "|".join(p for p, _ in MULTIPLIERS) + r")(?![a-z])", re.IGNORECASE)

UNIT_AFTER = [
    (r"%|per\s*cent|percent|por\s+ciento|प्रतिशत", "%"),
    (r"euros?|eur(?![a-z])|€", "EUR"),
    (r"rupees?|रुपये|रुपए|inr(?![a-z])", "INR"),
    (r"pesos|mxn(?![a-z])", "MXN"),
    (r"dollars?|dólares|usd(?![a-z])", "USD"),
]
_UNIT_AFTER_RE = [(re.compile(r"^\s*(?:" + p + r")", re.IGNORECASE), u) for p, u in UNIT_AFTER]
_UNIT_BEFORE_RE = re.compile(r"(€|₹|£|\$|rs\.?\s*|inr\s*|usd\s*|eur\s*|mxn\s*)$", re.IGNORECASE)

NUMBER_TOKEN = re.compile(r"(?<![\d,.])\d[\d,.  ]*\d(?![\d])|(?<![\d,.])\d(?![\d])")

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4,
    "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "quince": 15,
    "veinte": 20, "treinta": 30, "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5,
    "दस": 10,
}


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: Optional[str] = None    # "EUR", "INR", "MXN", "USD", "GBP", "%", "DATE" or None


def _parse_number(tok: str, locale: str) -> Optional[float]:
    tok = tok.replace(" ", "").replace(" ", "")
    if locale in DECIMAL_COMMA_LOCALES:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", tok):
            return float(tok.replace(".", "").replace(",", "."))
        if re.fullmatch(r"\d+(,\d+)?", tok):
            return float(tok.replace(",", "."))
        return None
    # Indian grouping 1,50,000 is unambiguous: two-digit groups ending in a three-digit group
    if re.fullmatch(r"\d{1,2}(,\d{2})+,\d{3}(\.\d+)?", tok):
        return float(tok.replace(",", ""))
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", tok):
        return float(tok.replace(",", ""))
    if re.fullmatch(r"\d+(\.\d+)?", tok):
        return float(tok)
    return None


def _currency_symbol(sym: str, locale: str) -> str:
    sym = sym.strip().lower().rstrip(".").strip()
    if sym == "$":
        return "MXN" if locale == "es-MX" else "USD"   # Mexico writes pesos with "$"
    return {"€": "EUR", "₹": "INR", "£": "GBP", "rs": "INR", "inr": "INR",
            "usd": "USD", "eur": "EUR", "mxn": "MXN"}.get(sym, sym.upper())


def extract_quantities(text: str, locale: str, include_words: bool = False) -> tuple[list, bool]:
    """Returns (quantities, had_unparseable). Dates are extracted first and masked out, so the
    digits inside a date are not also read as free-standing numbers."""
    text = text.translate(DEVANAGARI_DIGITS)
    out: list = []
    masked = list(text)
    for kind, pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            if any(c == "\0" for c in masked[m.start():m.end()]):
                continue
            g = m.groups()
            if kind == "ymd":
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            elif kind == "numeric":
                a, b, y = int(g[0]), int(g[1]), int(g[2])
                mo, d = (a, b) if locale == "en-US" else (b, a)   # 01/04 is April in India
            elif kind == "d_month_y":
                d, mo, y = int(g[0]), MONTHS[g[1].lower()], int(g[2])
            else:
                mo, d, y = MONTHS[g[0].lower()], int(g[1]), int(g[2])
            if 1 <= mo <= 12 and 1 <= d <= 31:
                out.append(Quantity(y * 10_000 + mo * 100 + d, "DATE"))
                out.append(Quantity(float(y), None))      # "the 2025 policy" may cite a date
            for k in range(m.start(), m.end()):
                masked[k] = "\0"
    work = "".join(masked)
    bad = False
    for m in NUMBER_TOKEN.finditer(work):
        val = _parse_number(m.group(0), locale)
        if val is None:
            bad = True
            continue
        after = work[m.end():m.end() + 24]
        mm = _MULT_RE.match(after)
        if mm:
            for pat, mult in MULTIPLIERS:
                if re.fullmatch(pat, mm.group(1), re.IGNORECASE):
                    val *= mult
                    break
            after = after[mm.end():]
        unit = None
        for rx, u in _UNIT_AFTER_RE:
            if rx.match(after):
                unit = u
                break
        if unit is None:
            mb = _UNIT_BEFORE_RE.search(work[max(0, m.start() - 6):m.start()])
            if mb:
                unit = _currency_symbol(mb.group(1), locale)
        out.append(Quantity(val, unit))
    if include_words:
        for tok in tokens(work):
            for part in tok.split("-"):
                if part in NUMBER_WORDS:
                    out.append(Quantity(float(NUMBER_WORDS[part]), None))
    return out, bad


def _same(q: Quantity, e: Quantity) -> bool:
    if (q.unit == "DATE") != (e.unit == "DATE"):
        return False
    if not math.isclose(q.value, e.value, rel_tol=1e-9, abs_tol=1e-9):
        return False
    return q.unit is None or e.unit is None or q.unit == e.unit


# ---------------------------------------------------------------------------------------
# Lexical cues: tokens, polarity, modality, scope, qualifiers
# ---------------------------------------------------------------------------------------

def tokens(text: str) -> list:
    # Devanagari vowel signs are combining marks, which \w does not cover in every engine;
    # the explicit block keeps a Hindi word in one token.
    return re.findall(r"[\wऀ-ॿ']+", text.lower())


NEGATION = {
    "en": {"not", "no", "never", "none", "cannot", "neither", "nor", "without", "ineligible"},
    "es": {"no", "nunca", "jamás", "ningún", "ninguna", "ninguno", "sin", "tampoco", "ni"},
    "hi": {"नहीं", "न", "मत", "बिना"},
}
OBLIGATION = {
    "en": {"must", "shall", "required", "mandatory", "obligatory", "obliged"},
    "es": {"debe", "deben", "deberá", "deberán", "obligatorio", "obligatoria",
           "obligatorios", "obligatorias"},
    "hi": {"अनिवार्य", "आवश्यक", "ज़रूरी", "जरूरी"},
}
PERMISSION = {
    "en": {"may", "can", "allowed", "permitted", "eligible", "entitled"},
    "es": {"puede", "pueden", "podrá", "podrán", "permitido", "permitida", "elegible"},
    "hi": {"सकते", "सकता", "सकती", "सकेंगे", "पात्र", "हकदार"},
}
QUALIFIER_WORDS = {
    "en": {"except", "unless", "excluding", "however", "only"},
    "es": {"excepto", "salvo", "únicamente", "solo", "sólo"},
    "hi": {"सिवाय", "छोड़कर", "बशर्ते", "लेकिन", "केवल", "हालांकि", "परंतु", "परन्तु"},
}
QUALIFIER_PHRASES = {
    "en": ["provided that", "subject to", "does not apply", "only if"],
    "es": ["a menos que", "siempre que", "sin embargo", "no aplica"],
    "hi": ["लागू नहीं"],
}
# Scope families. Swapping one member for another inside a family is a FAIL; adding a member
# the evidence does not mention at all is only a doubt (GRAY).
SCOPE = {
    "worker": {
        "employee": {"employee", "employees", "staff", "empleado", "empleados", "empleada",
                     "empleadas", "कर्मचारी", "कर्मचारियों"},
        "contractor": {"contractor", "contractors", "contratista", "contratistas", "ठेकेदार",
                       "संविदा"},
        "intern": {"intern", "interns", "becario", "becarios", "becaria", "becarias", "इंटर्न",
                   "प्रशिक्षु"},
    },
    "place": {
        "india": {"india", "indian", "भारत", "भारतीय"},
        "spain": {"spain", "españa", "स्पेन"},
        "mexico": {"mexico", "méxico", "मेक्सिको"},
        "madrid": {"madrid", "मैड्रिड"},
        "mumbai": {"mumbai", "मुंबई"},
        "pune": {"pune", "पुणे"},
    },
}


def lang_of(locale: str) -> str:
    return locale.split("-")[0]


def _has(text: str, locale: str, table: dict) -> bool:
    toks = tokens(text)
    words = table[lang_of(locale)]
    return any(t in words or (lang_of(locale) == "en" and table is NEGATION and t.endswith("n't"))
               for t in toks)


def is_negated(text: str, locale: str) -> bool:
    return _has(text, locale, NEGATION)


def has_obligation(text: str, locale: str) -> bool:
    return _has(text, locale, OBLIGATION)


def has_permission(text: str, locale: str) -> bool:
    return _has(text, locale, PERMISSION)


def has_qualifier(text: str, locale: str) -> bool:
    lang = lang_of(locale)
    low = text.lower()
    return (any(t in QUALIFIER_WORDS[lang] for t in tokens(text))
            or any(p in low for p in QUALIFIER_PHRASES[lang]))


def scope_concepts(text: str) -> dict:
    toks = set(tokens(text))
    found: dict = {}
    for family, members in SCOPE.items():
        for concept, forms in members.items():
            if toks & forms:
                found.setdefault(family, set()).add(concept)
    return found


# ---------------------------------------------------------------------------------------
# The six deterministic checks
# ---------------------------------------------------------------------------------------

def check_structure(claim: Claim, context: dict) -> tuple[str, str]:
    if not claim.cites:
        return FAIL, "no citation"
    unknown = [c for c in claim.cites if c not in context]
    if unknown:
        return FAIL, f"cites IDs not in context: {unknown}"
    return PASS, ""


def check_quantities(claim: Claim, cited: list, question_numbers: list) -> tuple[str, str]:
    cq, claim_bad = extract_quantities(claim.text, claim.locale)
    evidence: list = []
    for s in cited:
        eq, _ = extract_quantities(s.text, s.locale, include_words=True)
        evidence.extend(eq)
    allowed = list(evidence)
    if claim.calc:
        status, msg, result = check_calc(claim.calc, evidence, question_numbers)
        if status == FAIL:
            return FAIL, msg
        allowed.append(Quantity(result))
        allowed.extend(question_numbers)
    missing = [q for q in cq if not any(_same(q, e) for e in allowed)]
    if missing:
        shown = ", ".join(f"{q.value:g}{'' if q.unit is None else ' ' + q.unit}" for q in missing)
        return FAIL, f"quantity not in evidence: {shown}"
    if claim_bad:
        return GRAY, "claim contains a number that did not parse"
    return PASS, ""


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv}


def _eval(node: ast.AST, operands: list) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body, operands)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        operands.append(float(node.value))
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left, operands), _eval(node.right, operands))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand, operands)
    raise ValueError("only + - * / and numbers are allowed")


def check_calc(calc: str, evidence: list, question_numbers: list) -> tuple[str, str, float]:
    if "=" not in calc:
        return FAIL, "calc without a stated result", 0.0
    expr, stated = calc.split("=", 1)
    try:
        operands: list = []
        value = _eval(ast.parse(expr.strip(), mode="eval"), operands)
        stated_v = float(stated.strip())
    except (ValueError, SyntaxError, ZeroDivisionError) as exc:
        return FAIL, f"calc does not evaluate: {exc}", 0.0
    pool = [e.value for e in evidence] + [q.value for q in question_numbers]
    orphan = [o for o in operands if not any(math.isclose(o, p) for p in pool)]
    if orphan:
        return FAIL, f"calc operand not in evidence or question: {orphan}", 0.0
    if not math.isclose(value, stated_v, rel_tol=1e-9, abs_tol=0.005):
        return FAIL, f"calc says {stated_v:g}, recomputed {value:g}", 0.0
    return PASS, "", value


def check_polarity(claim: Claim, cited: list) -> tuple[str, str]:
    c = is_negated(claim.text, claim.locale)
    if any(is_negated(s.text, s.locale) == c for s in cited):
        return PASS, ""
    return FAIL, "negation added" if c else "negation dropped"


def check_modality(claim: Claim, cited: list) -> tuple[str, str]:
    if has_obligation(claim.text, claim.locale) and not any(
            has_obligation(s.text, s.locale) for s in cited):
        return FAIL, "modality strengthened (permission or fact became obligation)"
    if (has_permission(claim.text, claim.locale)
            and any(has_obligation(s.text, s.locale) for s in cited)
            and not any(has_permission(s.text, s.locale) for s in cited)):
        return GRAY, "modality weakened (obligation became permission)"
    return PASS, ""


def check_scope(claim: Claim, cited: list) -> tuple[str, str]:
    claimed = scope_concepts(claim.text)
    ev: dict = {}
    for s in cited:
        for fam, cs in scope_concepts(s.text).items():
            ev.setdefault(fam, set()).update(cs)
        for concept in s.doc_scope:
            for fam, members in SCOPE.items():
                if concept in members:
                    ev.setdefault(fam, set()).add(concept)
    verdict, why = PASS, ""
    for fam, cs in claimed.items():
        extra = cs - ev.get(fam, set())
        if not extra:
            continue
        if ev.get(fam):
            return FAIL, f"scope swapped: claim says {sorted(extra)}, evidence says {sorted(ev[fam])}"
        verdict, why = GRAY, f"scope added: {sorted(extra)} not mentioned in evidence"
    return verdict, why


# ---------------------------------------------------------------------------------------
# Decision policy
# ---------------------------------------------------------------------------------------

ACCEPT_AT = 0.92
REJECT_BELOW = 0.50

NLI = Callable[[str, str], float]           # (premise, hypothesis) -> entailment probability
Judge = Callable[[str, str], Optional[bool]]  # None means it timed out


def neighbours(s: Sentence, context: dict, radius: int = 2) -> list:
    return [o for o in context.values()
            if o.doc_id == s.doc_id and o.section == s.section and o.sid != s.sid
            and abs(o.ordinal - s.ordinal) <= radius]


def verify_claim(claim: Claim, context: dict, nli: NLI, judge: Judge,
                 question_numbers: list) -> Verdict:
    v = Verdict(claim.cid, accepted=False)
    status, why = check_structure(claim, context)
    if status == FAIL:
        v.reasons.append(why)
        return v
    cited = [context[c] for c in claim.cites]
    force_gray = False
    for check in (lambda: check_quantities(claim, cited, question_numbers),
                  lambda: check_polarity(claim, cited),
                  lambda: check_modality(claim, cited),
                  lambda: check_scope(claim, cited)):
        status, why = check()
        if status == FAIL:
            v.reasons.append(why)
            return v
        if status == GRAY:
            v.reasons.append(why)
            force_gray = True
    premise = " ".join(s.text for s in cited)
    v.nli = nli(premise, claim.text)
    if v.nli < REJECT_BELOW:
        v.reasons.append(f"NLI {v.nli:.2f} below {REJECT_BELOW}")
        return v
    if v.nli >= ACCEPT_AT and not force_gray:
        v.accepted = True
        return v
    ruling = judge(premise, claim.text)
    if ruling is True:
        v.accepted = True
        v.reasons.append("accepted by gray-zone judge")
    else:
        v.reasons.append("gray-zone judge rejected" if ruling is False
                         else "gray-zone judge timed out (fail closed)")
    return v


def verify_answer(raw: str, locale: str, context: dict, nli: NLI, judge: Judge,
                  question: str = "", evidence_sufficient: bool = True) -> tuple[str, list]:
    """Returns (status, verdicts). Status is VERIFIED, PARTIAL, EXTRACTIVE or ABSTAINED."""
    claims, not_found = parse_output(raw, locale)
    if not_found and not claims:
        return ABSTAINED, []
    qnums, _ = extract_quantities(question, locale, include_words=True)
    verdicts = {c.cid: verify_claim(c, context, nli, judge, qnums) for c in claims}
    by_id = {c.cid: c for c in claims}

    # A qualifier without its parent means nothing; a parent without its qualifier misleads.
    for c in claims:
        if not c.qualifies or c.qualifies not in verdicts:
            continue
        parent, child = verdicts[c.qualifies], verdicts[c.cid]
        if not parent.accepted and child.accepted:
            child.accepted = False
            child.reasons.append(f"parent {c.qualifies} was dropped")
        elif parent.accepted and not child.accepted:
            parent.notes.extend(s for s in c.cites if s in context)

    # Qualifiers in or next to the evidence that no surviving claim carries get shown verbatim.
    cited_by_accepted = {s for cid, v in verdicts.items() if v.accepted for s in by_id[cid].cites}
    for cid, v in verdicts.items():
        if not v.accepted:
            continue
        claim = by_id[cid]
        for sid in claim.cites:
            s = context[sid]
            if has_qualifier(s.text, s.locale) and not has_qualifier(claim.text, claim.locale):
                v.notes.append(sid)
            for n in neighbours(s, context):
                if has_qualifier(n.text, n.locale) and n.sid not in cited_by_accepted:
                    v.notes.append(n.sid)
        v.notes = list(dict.fromkeys(v.notes))

    accepted = [v for v in verdicts.values() if v.accepted]
    if accepted and len(accepted) == len(verdicts):
        return VERIFIED, list(verdicts.values())
    if accepted:
        return PARTIAL, list(verdicts.values())
    return (EXTRACTIVE if evidence_sufficient else ABSTAINED), list(verdicts.values())


def overlap_nli(premise: str, hypothesis: str) -> float:
    """Demo stand-in ONLY. Share of the hypothesis's content tokens found in the premise."""
    stop = {"the", "a", "an", "of", "for", "to", "in", "is", "are", "and", "per", "de", "la",
            "el", "los", "las", "y", "en", "है", "के", "की", "का", "में"}
    h = [t for t in tokens(hypothesis) if t not in stop]
    p = set(tokens(premise))
    return sum(t in p for t in h) / max(1, len(h))


# ---------------------------------------------------------------------------------------
# Self-tests: every example quoted in docs 08 and 11 runs here
# ---------------------------------------------------------------------------------------

def _ctx() -> dict:
    T = "FIN-POL-017"
    rows = [
        Sentence("S1.1", "Employees travelling to Madrid may claim a per-diem of €85 per day.",
                 "en-US", T, "v4", "p.7", "4.2", 1),
        Sentence("S1.2", "The per-diem covers meals and local transport.",
                 "en-US", T, "v4", "p.7", "4.2", 2),
        Sentence("S1.3", "Contractors are not eligible for per-diem allowances.",
                 "en-US", T, "v4", "p.7", "4.2", 3),
        Sentence("S1.4", "Employees may book business class for flights longer than 6 hours.",
                 "en-US", T, "v4", "p.8", "4.3", 1),
        Sentence("S2.1", "La dieta diaria para Ciudad de México es de 1.500,50 pesos.",
                 "es-ES", "FIN-POL-017-ES", "v2", "p.3", "2.1", 1),
        Sentence("S2.2", "El reembolso del 15 por ciento se aplica a gastos de hotel.",
                 "es-ES", "FIN-POL-017-ES", "v2", "p.3", "2.1", 2),
        Sentence("S3.1", "The relocation allowance is ₹1,50,000.",
                 "en-IN", "HR-IN-044", "v1", "p.2", "1.1", 1),
        Sentence("S3.2", "अधिकतम राशि ₹२,००,००० है।",
                 "hi-IN", "HR-IN-045", "v3", "p.1", "1.1", 1, frozenset({"india"})),
        Sentence("S3.3", "The policy is effective from 01/04/2025.",
                 "en-IN", "HR-IN-044", "v1", "p.1", "0.1", 1),
        Sentence("S4.1", "Employees get 24 days of annual leave.",
                 "en-US", "HR-GL-002", "v7", "p.4", "3.1", 1),
        Sentence("S4.2", "This does not apply to staff on fixed-term contracts in India, "
                 "who get 18 days.", "en-US", "HR-GL-002", "v7", "p.4", "3.1", 2),
        Sentence("S4.3", "Employees must submit expense reports within 30 days.",
                 "en-US", "HR-GL-002", "v7", "p.5", "3.4", 1),
    ]
    return {s.sid: s for s in rows}


def _always(score: float) -> NLI:
    return lambda premise, hypothesis: score


def _judge(answer: Optional[bool]) -> Judge:
    return lambda premise, hypothesis: answer


def _one(line: str, locale: str = "en-US", score: float = 0.97, question: str = "",
         judge: Optional[bool] = True) -> Verdict:
    claims, _ = parse_output(line, locale)
    qn, _ = extract_quantities(question, locale, include_words=True)
    return verify_claim(claims[0], _ctx(), _always(score), _judge(judge), qn)


def _tests() -> list:
    t = []

    def case(name):
        def deco(fn):
            t.append((name, fn))
            return fn
        return deco

    @case("faithful claim passes every check")
    def _():
        v = _one("C1 [S1.1] Employees travelling to Madrid can claim a per-diem of €85 per day.")
        assert v.accepted, v.reasons

    @case("wrong amount fails without any model call")
    def _():
        v = _one("C1 [S1.1] Employees travelling to Madrid can claim €95 per day.", score=0.99)
        assert not v.accepted and "95" in v.reasons[0], v.reasons

    @case("citing an ID that was never in the context fails")
    def _():
        v = _one("C1 [S9.9] Employees can claim €85 per day.")
        assert not v.accepted and "not in context" in v.reasons[0]

    @case("uncited claim fails")
    def _():
        v = _one("C1 [] Employees can claim €85 per day.")
        assert not v.accepted and v.reasons == ["no citation"]

    @case("dropped negation fails even with a confident NLI score")
    def _():
        v = _one("C1 [S1.3] Contractors are eligible for per-diem allowances.", score=0.99)
        assert not v.accepted and v.reasons == ["negation dropped"], v.reasons

    @case("'may' turned into 'must' fails")
    def _():
        v = _one("C1 [S1.4] Employees must book business class for flights longer than 6 hours.")
        assert not v.accepted and "strengthened" in v.reasons[0]

    @case("'must' turned into 'can' is sent to the judge, and fails closed on timeout")
    def _():
        v = _one("C1 [S4.3] Employees can submit expense reports within 30 days.", judge=None)
        assert not v.accepted and "timed out" in v.reasons[-1], v.reasons

    @case("employees swapped for contractors fails")
    def _():
        v = _one("C1 [S1.1] Contractors travelling to Madrid can claim €85 per day.")
        assert not v.accepted and "swapped" in v.reasons[0], v.reasons

    @case("es-ES 1.500,50 equals en-US 1,500.50")
    def _():
        v = _one("C1 [S2.1] The daily allowance for Mexico City is 1,500.50 pesos.")
        assert v.accepted, v.reasons

    @case("en-US '1.500 pesos' is 1.5 pesos, and fails against es-ES 1.500,50")
    def _():
        v = _one("C1 [S2.1] The daily allowance for Mexico City is 1.500 pesos.")
        assert not v.accepted and "1.5" in v.reasons[0], v.reasons

    @case("15 por ciento equals 15%")
    def _():
        v = _one("C1 [S2.2] A 15% reimbursement applies to hotel costs.")
        assert v.accepted, v.reasons

    @case("1.5 lakh rupees equals ₹1,50,000")
    def _():
        v = _one("C1 [S3.1] The relocation allowance is 1.5 lakh rupees.", locale="en-IN")
        assert v.accepted, v.reasons

    @case("Devanagari ₹२,००,००० equals ₹2,00,000, and rejects ₹20,000")
    def _():
        ok = _one("C1 [S3.2] The maximum amount in India is ₹2,00,000.", locale="en-IN")
        bad = _one("C1 [S3.2] The maximum amount in India is ₹20,000.", locale="en-IN")
        assert ok.accepted, ok.reasons
        assert not bad.accepted and "20000" in bad.reasons[0], bad.reasons

    @case("en-IN 01/04/2025 is 1 April, so 'April 1, 2025' passes and 'January 4' fails")
    def _():
        ok = _one("C1 [S3.3] The policy is effective from April 1, 2025.")
        bad = _one("C1 [S3.3] The policy is effective from January 4, 2025.")
        assert ok.accepted, ok.reasons
        assert not bad.accepted, bad.reasons

    @case("computed claim is recomputed: 85*3=255 passes, 85*3=265 fails")
    def _():
        q = "How much per-diem do I get for a three-day trip to Madrid?"
        ok = _one("C1 [S1.1] {calc:85*3=255} Employees on a three-day trip to Madrid get €255.",
                  question=q)
        bad = _one("C1 [S1.1] {calc:85*3=265} Employees on a three-day trip to Madrid get €265.",
                   question=q)
        assert ok.accepted, ok.reasons
        assert not bad.accepted and "recomputed 255" in bad.reasons[0], bad.reasons

    @case("arithmetic without a calc tag fails: the model may not compute silently")
    def _():
        v = _one("C1 [S1.1] Employees on a three-day trip to Madrid get €255.",
                 question="three-day trip")
        assert not v.accepted and "255" in v.reasons[0], v.reasons

    @case("NLI bands: below 0.50 rejects, 0.50-0.92 goes to the judge")
    def _():
        low = _one("C1 [S1.2] The per-diem covers meals and local transport.", score=0.31)
        mid_yes = _one("C1 [S1.2] The per-diem covers meals and local transport.", score=0.80)
        mid_no = _one("C1 [S1.2] The per-diem covers meals and local transport.", score=0.80,
                      judge=False)
        assert not low.accepted and mid_yes.accepted and not mid_no.accepted

    @case("an omitted neighbouring exception is attached verbatim as a note")
    def _():
        status, vs = verify_answer("C1 [S4.1] Employees get 24 days of annual leave.",
                                   "en-US", _ctx(), _always(0.97), _judge(True))
        assert status == VERIFIED and vs[0].notes == ["S4.2"], (status, vs)

    @case("a dropped qualifier claim leaves its source sentence on the parent")
    def _():
        raw = ("C1 [S1.1] Employees travelling to Madrid can claim a per-diem of €85 per day.\n"
               "C2 [S1.3] {q:C1} Contractors are eligible for the per-diem.")
        status, vs = verify_answer(raw, "en-US", _ctx(), _always(0.97), _judge(True))
        c1 = next(v for v in vs if v.cid == "C1")
        assert status == PARTIAL and "S1.3" in c1.notes, (status, vs)

    @case("a qualifier whose parent was dropped is dropped too")
    def _():
        raw = ("C1 [S1.1] Employees travelling to Madrid can claim €95 per day.\n"
               "C2 [S1.3] {q:C1} Contractors are not eligible for per-diem allowances.")
        status, vs = verify_answer(raw, "en-US", _ctx(), _always(0.97), _judge(True))
        assert status == EXTRACTIVE and not any(v.accepted for v in vs), (status, vs)

    @case("NOT_FOUND with no claims abstains")
    def _():
        status, _ = verify_answer("NOT_FOUND", "en-US", _ctx(), _always(0.97), _judge(True))
        assert status == ABSTAINED

    @case("all claims dropped and evidence insufficient abstains rather than extracts")
    def _():
        status, _ = verify_answer("C1 [S1.1] Employees can claim €95.", "en-US", _ctx(),
                                  _always(0.97), _judge(True), evidence_sufficient=False)
        assert status == ABSTAINED

    @case("sentence splitter: Hindi danda, Spanish ¿, abbreviations and decimals")
    def _():
        hi = split_sentences("कर्मचारी 24 दिन की छुट्टी ले सकते हैं। ठेकेदार पात्र नहीं हैं।", "hi")
        es = split_sentences("Véase el Art. 5 del reglamento. ¿Aplica a becarios? No.", "es")
        en = split_sentences("The rate is 2.5 per cent, e.g. for Pune. It changes yearly.", "en")
        assert len(hi) == 2 and hi[1].startswith("ठेकेदार"), hi
        assert es == ["Véase el Art. 5 del reglamento.", "¿Aplica a becarios?", "No."], es
        assert len(en) == 2, en

    @case("the demo overlap scorer is not a verifier: it accepts a meaning flip")
    def _():
        # Word overlap is high, meaning is opposite. This is why the polarity check exists,
        # and why a real entailment model is required behind it.
        score = overlap_nli("Contractors are not eligible for per-diem allowances.",
                            "Contractors are eligible for per-diem allowances.")
        assert score >= ACCEPT_AT, score

    return t


def main() -> int:
    failed = 0
    for name, fn in _tests():
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\ngrounding.py: {len(_tests()) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
