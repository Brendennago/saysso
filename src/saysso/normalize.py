"""Normalization that remembers where every character came from.

Quotes are matched in normalized space (case, Unicode, quotes, dashes, ligatures, whitespace,
line-break hyphenation) and then mapped back to exact character offsets in the original text.
"""

from __future__ import annotations

import re
import unicodedata

_QUOTES = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u00ab": '"', "\u00bb": '"', "\u2033": '"',
}
_DASHES = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
}
_DROP = {"\u00ad", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}
_MARKDOWN = set("*_`")


def normalize_with_map(text: str, *, lower: bool = True, strip_markdown: bool = False) -> tuple[str, list[int]]:
    """Return (normalized_text, index_map) where index_map[i] is the original index of normalized char i."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        # Re-join words hyphenated across a line break: "infor-\nmation" -> "information".
        if ch == "-" and i > 0 and text[i - 1].isalpha():
            j = i + 1
            while j < n and text[j] in " \t":
                j += 1
            if j < n and text[j] == "\n":
                j += 1
                while j < n and text[j] in " \t":
                    j += 1
                if j < n and text[j].islower():
                    i = j
                    continue
        if ch in _DROP or (strip_markdown and ch in _MARKDOWN):
            i += 1
            continue
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                idx.append(i)
                prev_space = True
            i += 1
            continue
        mapped = _QUOTES.get(ch) or _DASHES.get(ch)
        if mapped is None:
            mapped = unicodedata.normalize("NFKC", ch)
        if lower:
            mapped = mapped.casefold()
        for m in mapped:
            if m.isspace():
                if not prev_space:
                    out.append(" ")
                    idx.append(i)
                    prev_space = True
            elif unicodedata.category(m) == "Mn":
                continue  # drop combining marks so "café" == "cafe"
            else:
                out.append(m)
                idx.append(i)
                prev_space = False
        i += 1
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def normalize(text: str, **kw) -> str:
    return normalize_with_map(text, **kw)[0]


class NormText:
    """Original text plus its normalized form and offset map, computed once per source."""

    __slots__ = ("raw", "norm", "map")

    def __init__(self, raw: str):
        self.raw = raw
        self.norm, self.map = normalize_with_map(raw)

    def span(self, a: int, b: int) -> tuple[int, int]:
        """Map a normalized half-open span [a, b) back to original offsets."""
        if not self.map:
            return 0, 0
        a = max(0, min(a, len(self.map) - 1))
        b = max(a + 1, min(b, len(self.map)))
        return self.map[a], self.map[b - 1] + 1


STOPWORDS = frozenset(
    """
a an the and or but if then else of in on at to for from by with within into onto over under about above below
between among as is are was were be been being am do does did done has have had having it its this that these
those there here which who whom whose what when where why how than so such very can could may might must shall
should will would also just not no nor only own same too s t now i you he she we they them his her their our your
my me us per via et al vs etc according said says say reported reports report new one ones
""".split()
)

_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?|[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


def tokens(text: str) -> list[str]:
    norm = normalize(text)
    out = []
    for m in _TOKEN_RE.finditer(norm):
        t = m.group(0)
        if t[0].isdigit():
            t = t.replace(",", "").rstrip(".")
        out.append(t)
    return out


def stem(t: str) -> str:
    if not t or t[0].isdigit():
        return t
    if t.endswith("'s"):
        t = t[:-2]
    for suf, rep in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("edly", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(t) > len(suf) + 2 and t.endswith(suf):
            if suf == "s" and t.endswith("ss"):
                break
            t = t[: -len(suf)] + rep
            break
    if len(t) > 4 and t.endswith("e"):
        t = t[:-1]
    return t


def content_terms(text: str) -> list[str]:
    """Stemmed content words and numbers, used for retrieval and lexical coverage."""
    return [stem(t) for t in tokens(text) if t not in STOPWORDS and (len(t) > 2 or t[0].isdigit())]
