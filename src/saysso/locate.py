"""Find a quote inside a source and return exact character offsets.

This is the part that never trusts a model: a quote only counts as evidence if it can be located here.
Tiers: 1 = exact (after normalization), 2 = exact pieces around an ellipsis, 3 = fuzzy alignment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote as urlquote

from rapidfuzz import fuzz

from .models import Evidence
from .normalize import NormText, normalize_with_map

_ELLIPSIS_SPLIT = re.compile(r"\s*\[?\.\.\.\]?\s*")
_WRAPPING = "\"'`“”‘’«» "


@dataclass
class Match:
    start: int
    end: int
    tier: int
    score: float


def locate(quote: str, source: NormText | str, *, min_fuzzy: float = 90.0, min_len: int = 12) -> Match | None:
    src = source if isinstance(source, NormText) else NormText(source)
    q, _ = normalize_with_map(quote.strip().strip(_WRAPPING), strip_markdown=True)
    if len(q) < min_len or not src.norm:
        return None

    i = src.norm.find(q)
    if i >= 0:
        s, e = src.span(i, i + len(q))
        return Match(s, e, 1, 100.0)

    parts = [p for p in _ELLIPSIS_SPLIT.split(q) if len(p) >= 6]
    if len(parts) > 1:
        pos, first = 0, None
        for p in parts:
            j = src.norm.find(p, pos)
            if j < 0:
                break
            if first is None:
                first = j
            pos = j + len(p)
        else:
            s, e = src.span(first, pos)
            return Match(s, e, 2, 100.0)

    if len(q) > len(src.norm):
        return None
    needle = q[:600]
    res = fuzz.partial_ratio_alignment(needle, src.norm, score_cutoff=min_fuzzy)
    if res and res.dest_end > res.dest_start:
        s, e = src.span(res.dest_start, res.dest_end)
        return Match(s, e, 3, float(res.score))
    return None


def text_fragment(url: str | None, exact: str) -> str | None:
    """Build a #:~:text= link that scrolls a browser to the quote (best effort; not for PDFs)."""
    if not url:
        return None
    base = url.split("#", 1)[0]
    if base.lower().split("?", 1)[0].endswith(".pdf"):
        return None
    words = exact.split()
    if not words:
        return None

    def enc(s: str) -> str:
        return urlquote(s, safe="").replace("-", "%2D")

    if len(words) <= 8:
        directive = enc(" ".join(words))
    else:
        directive = enc(" ".join(words[:4])) + "," + enc(" ".join(words[-4:]))
    return f"{base}#:~:text={directive}"


def make_evidence(
    src: NormText,
    start: int,
    end: int,
    *,
    url: str | None,
    kind: str = "support",
    tier: int = 1,
    score: float = 100.0,
    passage_id: str | None = None,
    verified: bool = True,
    ctx: int = 32,
) -> Evidence:
    raw = src.raw
    exact = raw[start:end]
    return Evidence(
        quote=exact,
        start=start,
        end=end,
        kind=kind,
        prefix=raw[max(0, start - ctx) : start],
        suffix=raw[end : end + ctx],
        match_tier=tier,
        match_score=round(score, 1),
        passage_id=passage_id,
        fragment_url=text_fragment(url, exact) if verified else None,
        verified=verified,
    )
