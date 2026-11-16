"""Cheap deterministic checks that run before any model.

The number gate is the workhorse: a claim that says "47%" when the source never says 47% (or
anything that rounds to it) is not supported, no matter how fluent the sentence is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import content_terms

_WORDNUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "dozen": 12,
}
_SCALES = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9,
           "b": 1e9, "trillion": 1e12, "tn": 1e12}

NUM_RE = re.compile(
    r"(?<![\w.])(?P<cur>[$\u20ac\u00a3\u00a5])?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<scale>thousand|million|billion|trillion|bn|mn|tn|[kmb])(?![a-z]))?"
    r"\s?(?P<pct>%|percent\b|per cent\b|pct\b|percentage points?\b)?",
    re.I,
)
WORDNUM_RE = re.compile(
    r"\b(?P<w>" + "|".join(sorted(_WORDNUM, key=len, reverse=True)) + r")\b"
    r"(?:\s(?P<scale>thousand|million|billion|trillion))?(?:\s?(?P<pct>percent|per cent))?",
    re.I,
)
APPROX_RE = re.compile(
    r"\b(about|around|approximately|approx\.?|roughly|nearly|almost|over|more than|less than|under|some|"
    r"up to|at least|close to|upwards of|in excess of)\s*$|~\s*$",
    re.I,
)
NEG_RE = re.compile(
    r"\b(not|no|never|none|neither|nor|cannot|can't|won't|isn't|aren't|wasn't|weren't|doesn't|don't|didn't|"
    r"hasn't|haven't|hadn't|without|fail(?:s|ed)? to|myth|false|incorrect|misconception|debunked|untrue|"
    r"contrary to)\b",
    re.I,
)


@dataclass
class Num:
    raw: str
    value: float
    decimals: int
    unit: float
    pct: bool
    year: bool
    approx: bool
    pos: int = 0


def extract_numbers(text: str) -> list[Num]:
    out: list[Num] = []
    for m in NUM_RE.finditer(text):
        digits = m.group("num").replace(",", "")
        try:
            val = float(digits)
        except ValueError:
            continue
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        scale = (m.group("scale") or "").lower()
        if scale in ("k", "m", "b") and not m.group("cur"):
            scale = ""  # "330 m" is metres, not millions
        unit = _SCALES.get(scale, 1.0)
        pct = bool(m.group("pct"))
        year = (
            not pct and unit == 1.0 and not m.group("cur") and decimals == 0 and "," not in m.group("num")
            and len(digits) == 4 and 1500 <= val <= 2100
        )
        approx = bool(APPROX_RE.search(text[max(0, m.start() - 24) : m.start()]))
        out.append(Num(m.group(0).strip(), val * unit, decimals, unit, pct, year, approx, m.start()))
    for m in WORDNUM_RE.finditer(text):
        val = float(_WORDNUM[m.group("w").lower()])
        unit = _SCALES.get((m.group("scale") or "").lower(), 1.0)
        approx = bool(APPROX_RE.search(text[max(0, m.start() - 24) : m.start()]))
        out.append(Num(m.group(0).strip(), val * unit, 0, unit, bool(m.group("pct")), False, approx, m.start()))
    return out


def num_match(c: Num, s: Num) -> bool:
    """Exact, or the source rounds to the claim at the claim's own precision, or within 10% when the
    claim says "about/nearly/over". No blanket tolerance: 8,849 m and 8,844 m are different numbers."""
    if c.year or s.year:
        return c.year and s.year and int(c.value) == int(s.value)
    if c.pct != s.pct:
        return False
    if c.value == s.value:
        return True
    if c.value == 0 or s.value == 0:
        return False
    if c.decimals == 0:
        whole = int(round(c.value / c.unit))
        zeros = len(str(whole)) - len(str(whole).rstrip("0")) if whole >= 10 else 0
        prec = 10 ** zeros
        if round(s.value / c.unit / prec) * prec == whole:
            return True
    elif round(s.value / c.unit, c.decimals) == round(c.value / c.unit, c.decimals):
        return True
    if c.approx and abs(s.value - c.value) / abs(s.value) <= 0.1:
        return True
    return False


_UNITS = {
    "m": "m", "metre": "m", "metres": "m", "meter": "m", "meters": "m", "km": "km", "kilometres": "km",
    "kilometers": "km", "ft": "ft", "feet": "ft", "foot": "ft", "mi": "mi", "miles": "mi", "kg": "kg",
    "tonnes": "t", "tons": "t", "people": "people", "years": "years", "%": "%", "percent": "%",
    "dollars": "$", "usd": "$", "degrees": "deg", "°c": "degc", "°f": "degf",
}
_UNIT_AFTER = re.compile(r"\s?(%|°[cf]|[a-z]+)", re.I)


def _unit_after(text: str, n: Num) -> str | None:
    if n.pct:
        return "%"
    end = n.pos + len(n.raw)
    m = _UNIT_AFTER.match(text, end)
    if not m:
        return None
    return _UNITS.get(m.group(1).lower())


def number_conflicts(claim: str, window: str) -> list[tuple[str, str]]:
    """Claim numbers whose unit appears in the evidence window with a different value.
    "The tower is 512 metres tall" vs "The tower is 330 metres (1,083 ft) tall" -> [("512", "330 metres")]."""
    out = []
    wnums = extract_numbers(window)
    for c in extract_numbers(claim):
        unit = _unit_after(claim, c)
        if not unit or c.year:
            continue
        same_unit = [w for w in wnums if not w.year and _unit_after(window, w) == unit]
        if same_unit and not any(num_match(c, w) for w in same_unit):
            w = same_unit[0]
            out.append((c.raw, window[w.pos : w.pos + len(w.raw) + 8].strip()))
    return out


def number_gate(claim: str, passages_text: str, doc_numbers: list[Num], doc_text: str = "") -> dict:
    """A claim number counts as found only in the retrieved passages, or elsewhere in the document
    when the surrounding text is about the claim (a stray "512" in a footnote does not count)."""
    cnums = extract_numbers(claim)
    if not cnums:
        return {"status": "none", "numbers": [], "missing": []}
    pnums = extract_numbers(passages_text)
    results = []
    for c in cnums:
        hit = next((s for s in pnums if num_match(c, s)), None)
        where = "passage" if hit else None
        if hit is None and doc_text:
            for s in doc_numbers:
                if num_match(c, s) and coverage(claim, doc_text[max(0, s.pos - 300) : s.pos + 300]) >= 0.5:
                    hit, where = s, "document"
                    break
        results.append({"claim": c.raw, "found": where, "source": hit.raw if hit else None})
    missing = [r["claim"] for r in results if not r["found"]]
    if not missing:
        status = "pass"
    elif len(missing) == len(results):
        status = "fail"
    else:
        status = "partial"
    return {"status": status, "numbers": results, "missing": missing}


def coverage(claim: str, passage: str) -> float:
    c = set(content_terms(claim))
    if not c:
        return 0.0
    p = set(content_terms(passage))
    return len(c & p) / len(c)


def has_negation(text: str) -> bool:
    return bool(NEG_RE.search(text))


def claim_years(text: str) -> list[int]:
    return sorted({int(n.value) for n in extract_numbers(text) if n.year})
