"""Output formats: text, JSON, SARIF, the --fix annotated copy, and HTML (see html_report.py)."""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlsplit

from .models import RunReport, Verdict

_SYM = {
    Verdict.SUPPORTED: ("\u2714", "ok", "32"),
    Verdict.PARTIAL: ("\u25d0", "~~", "33"),
    Verdict.UNSUPPORTED: ("\u2718", "XX", "31"),
    Verdict.CONTRADICTED: ("\u2260", "!=", "35"),
    Verdict.WRONG_SOURCE: ("\u21c4", "<>", "31"),
    Verdict.UNREACHABLE: ("\u2205", "--", "90"),
    Verdict.UNCERTAIN: ("?", "??", "34"),
}


def _color_ok(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty() and not os.environ.get("NO_COLOR")


def _unicode_ok(stream) -> bool:
    enc = (getattr(stream, "encoding", None) or "").lower()
    return "utf" in enc


def _domain(url: str | None) -> str:
    if not url:
        return "-"
    h = urlsplit(url).hostname or url
    return h[4:] if h.startswith("www.") else h


def _short(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def render_text(rep: RunReport, *, verbose: bool = False, stream=None) -> str:
    stream = stream or sys.stdout
    color, uni = _color_ok(stream), _unicode_ok(stream)

    def paint(s: str, code: str) -> str:
        return f"\033[{code}m{s}\033[0m" if color else s

    lines = [f"saysso {rep.version}: {rep.input}"]
    st = rep.stats
    lines.append(f"{st['claims']} cited claim(s), {st['sources']} source(s), {st['sources_unreachable']} unreachable")
    lines.append("")
    for r in rep.claims:
        sym, asc, code = _SYM[r.verdict]
        mark = sym if uni else asc
        if r.verdict == Verdict.SUPPORTED and not verbose:
            continue
        cits = " ".join(f"[{c.citation_id}]" for c in r.checks) or "-"
        dom = _domain(r.checks[0].url) if r.checks else "-"
        lines.append(f"  {paint(mark, code)} {r.claim.id:<5} {paint(r.verdict.value, code):<{18 + (9 if color else 0)}}"
                     f" line {r.claim.line:<4} {cits} {dom}")
        lines.append(f"        {_short(r.claim.text, 110)}")
        for c in r.checks:
            if c.reason:
                lines.append(f"        [{c.citation_id}] {c.method}: {_short(c.reason, 120)}")
            if c.evidence and c.evidence.kind != "closest" and verbose:
                lines.append(f"            \"{_short(c.evidence.quote, 140)}\" @{c.evidence.start}-{c.evidence.end}")
            for n in c.notes:
                lines.append(f"            note: {_short(n, 140)}")
            if c.validity and c.validity.flags:
                lines.append(f"            source: {c.validity.tier.lower()}; {'; '.join(c.validity.flags)}")
        if r.repair and r.repair.attempted:
            if r.repair.found:
                f = r.repair.found
                lines.append(f"        fix: {f.url}")
                if f.evidence:
                    lines.append(f"            \"{_short(f.evidence.quote, 140)}\"")
            lines.append(f"        search: {r.repair.queries} quer(ies), {r.repair.fetches} page(s); {r.repair.stop_reason}")
        for n in r.notes:
            lines.append(f"        note: {_short(n, 140)}")
    c = st["counts"]
    parts = [f"{c[v.value]} {v.value.lower()}" for v in Verdict if c[v.value]]
    fa = st["faithfulness"]
    summary = ", ".join(parts) or "no cited claims found"
    lines.append("")
    lines.append(f"{'=' * 8} {summary} in {st.get('elapsed_s', 0)}s {'=' * 8}")
    u = rep.usage
    cost = f", ~${u['cost_usd']:.4f}" if u.get("cost_usd") else ""
    model = f"model {u['model']}: {u['calls']} call(s){cost}, {u['cached_hits']} cached" if u.get("model") else \
        "no model (deterministic checks only)"
    review = c["UNCERTAIN"]
    score = (f"faithfulness {fa:.1%} of {st['judged']} judged" if fa is not None else "faithfulness n/a") + (
        f" ({review} need a model or human; try --model)" if review else "")
    lines.append(f"{score} | {model} | {u.get('live_fetches', 0)} fetches, {u.get('searches', 0)} searches")
    if st.get("uncited_factual_sentences"):
        lines.append(f"{st['uncited_factual_sentences']} sentence(s) with numbers have no citation (see JSON 'uncited')")
    if u.get("budget_note"):
        lines.append(f"note: {u['budget_note']}")
    return "\n".join(lines)


def to_json(rep: RunReport, indent: int | None = 2) -> str:
    return json.dumps(rep.to_dict(), ensure_ascii=False, indent=indent)


def patched_markdown(original: str, rep: RunReport) -> str:
    """For --fix: annotate failing claims in a copy of the report. Never rewrites the claim itself."""
    inserts: list[tuple[int, str]] = []
    for r in rep.claims:
        if r.passes or r.verdict == Verdict.UNCERTAIN:
            continue
        if r.repair and r.repair.found:
            f = r.repair.found
            q = (f.evidence.quote if f.evidence else "").replace("--", "\u2013")
            inserts.append((r.claim.end, f" ([verified source]({f.url}))"
                            f"<!-- saysso: {r.verdict.value}; supporting quote: \"{q[:300]}\" -->"))
        elif r.verdict in (Verdict.UNSUPPORTED, Verdict.CONTRADICTED, Verdict.UNREACHABLE, Verdict.PARTIAL):
            why = "; ".join(c.reason for c in r.checks if c.reason)[:300].replace("--", "\u2013")
            tag = "disputed" if r.verdict == Verdict.CONTRADICTED else "citation needed"
            inserts.append((r.claim.end, f" [{tag}]<!-- saysso: {r.verdict.value}: {why} -->"))
    out = original
    for pos, text in sorted(inserts, key=lambda x: -x[0]):
        out = out[:pos] + text + out[pos:]
    return out


def render_html(rep: RunReport) -> str:
    from .html_report import render_html as _render

    return _render(rep)


_SARIF_RULES = {
    Verdict.UNSUPPORTED: ("citation-unsupported", "error", "The cited source does not support the claim"),
    Verdict.CONTRADICTED: ("citation-contradicted", "error", "The cited source contradicts the claim"),
    Verdict.WRONG_SOURCE: ("citation-wrong-source", "error",
                           "The claim is supported elsewhere, but not by the cited source"),
    Verdict.PARTIAL: ("citation-partial", "warning", "The cited source supports only part of the claim"),
    Verdict.UNREACHABLE: ("citation-unreachable", "warning", "The cited source could not be checked"),
    Verdict.UNCERTAIN: ("citation-needs-review", "note", "The claim needs a model or a human to judge"),
}


def _line_col(text: str, offset: int) -> tuple[int, int]:
    offset = max(0, min(offset, len(text)))
    line = text.count("\n", 0, offset) + 1
    col = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return line, col


def render_sarif(reports: list[RunReport], texts: dict[str, str]) -> str:
    """SARIF 2.1.0 for GitHub code scanning. One result per claim that does not pass."""
    from . import __version__

    rules = [
        {"id": rid, "name": rid, "shortDescription": {"text": desc},
         "defaultConfiguration": {"level": level}}
        for rid, level, desc in _SARIF_RULES.values()
    ]
    results = []
    for rep in reports:
        text = texts.get(rep.input, "")
        for r in rep.claims:
            if r.passes or r.verdict == Verdict.SUPPORTED:
                continue
            rid, level, _ = _SARIF_RULES[r.verdict]
            reason = next((c.reason for c in r.checks if c.reason), "")
            msg = f"{r.verdict.label}: {reason + '. ' if reason else ''}{r.claim.text}"
            sl, sc = _line_col(text, r.claim.start)
            el, ec = _line_col(text, r.claim.end)
            results.append({
                "ruleId": rid,
                "level": level,
                "message": {"text": msg[:400]},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": rep.input},
                    "region": {"startLine": sl, "startColumn": sc, "endLine": el, "endColumn": ec},
                }}],
            })
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "saysso", "version": __version__,
                "informationUri": "https://github.com/Brendennago/saysso", "rules": rules,
            }},
            "results": results,
        }],
    }
    return json.dumps(doc, indent=2)
