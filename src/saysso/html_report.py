"""Self-contained HTML report: an editor's proof sheet.

Claims are set like galley text. The verdict is a proofreader's mark on the claim, and the evidence
sits in a margin note beside it. One file, inline CSS and JS, nothing loaded from the network.
Everything that comes from a source or a report goes through html.escape.
"""

from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

from .models import ClaimResult, RunReport, SourceCheck, Verdict

_CSS = """
:root{color-scheme:light dark;--paper:#F7F9FC;--ink:#1B2230;--muted:#5B6578;--rule:#C9D6E6;--blue:#2F6FB3;
--ok:#2E7D57;--part:#A86B12;--bad:#C2362B;--contra:#8F1D3F;--gray:#7A8394;--panel:#EDF2F9}
@media (prefers-color-scheme:dark){:root{--paper:#151B26;--ink:#E6EBF3;--muted:#9AA5B8;--rule:#2B3648;
--blue:#7FB0E8;--ok:#5BC08C;--part:#E0A84A;--bad:#F07468;--contra:#E07AA0;--gray:#8A94A6;--panel:#1B2331}}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;color:var(--ink);background:var(--paper);font:16px/1.6 "Iowan Old Style","Palatino Linotype",
Palatino,"Book Antiqua",Georgia,serif}
a{color:var(--blue)}
a:focus-visible,button:focus-visible,summary:focus-visible{outline:3px solid var(--blue);outline-offset:2px;
border-radius:2px}
.side{font-family:"Avenir Next Condensed","Roboto Condensed","Arial Narrow",system-ui,sans-serif}
header,main,footer{max-width:68rem;margin:0 auto;padding:0 1.25rem}
header{padding-top:2.25rem;padding-bottom:1rem}
.mark{font-family:"Avenir Next Condensed","Roboto Condensed","Arial Narrow",system-ui,sans-serif;font-weight:700;
color:var(--blue);letter-spacing:.01em}
.meta{color:var(--muted);font-family:"Avenir Next Condensed","Roboto Condensed","Arial Narrow",system-ui,sans-serif;
font-size:.95rem;margin:.15rem 0 1.1rem;overflow-wrap:anywhere}
h1{font-size:clamp(1.7rem,4.5vw,2.6rem);line-height:1.15;margin:0 0 .35rem;font-weight:600}
.counts{margin:0 0 1.25rem;color:var(--muted)}
.tally{display:flex;gap:2px;height:2.6rem;align-items:stretch;margin:0 0 .35rem}
.tally a{flex:1 1 0;min-width:2px;max-width:14px;border-radius:1px;display:block}
.tally a:hover,.tally a:focus-visible{transform:scaleY(1.12)}
.legend{color:var(--muted);font-size:.9rem;margin:0 0 1rem}
.filters{display:flex;flex-wrap:wrap;gap:.4rem;margin:.5rem 0 1.5rem}
.filters button{font:inherit;font-size:.95rem;font-family:"Avenir Next Condensed","Roboto Condensed","Arial Narrow",
system-ui,sans-serif;color:var(--ink);background:transparent;border:1px solid var(--rule);border-radius:999px;
padding:.15rem .8rem;cursor:pointer}
.filters button[aria-pressed=true]{background:var(--panel);border-color:var(--ink)}
.filters button[aria-pressed=false]{color:var(--muted);text-decoration:line-through}
.row{display:grid;grid-template-columns:minmax(0,62ch) minmax(0,22rem);gap:2.5rem;padding:1.15rem 0;
border-top:1px solid var(--rule);scroll-margin-top:1rem}
.row:target .claim{background:var(--panel)}
.row[hidden]{display:none}
.claim{margin:0;font-size:1.08rem;padding:.1rem .25rem;margin-left:-.25rem}
.claim .u{text-decoration-thickness:2px;text-underline-offset:.28em}
.v-SUPPORTED .u{text-decoration:underline solid var(--ok)}
.v-PARTIAL .u{text-decoration:underline dotted var(--part)}
.v-UNSUPPORTED .u{text-decoration:underline wavy var(--bad)}
.v-CONTRADICTED .u{text-decoration:line-through var(--contra)}
.v-WRONG-SOURCE .u{text-decoration:underline dashed var(--bad)}
.v-SOURCE-UNREACHABLE .u{text-decoration:underline dashed var(--gray)}
.v-UNCERTAIN .u{text-decoration:underline dotted var(--blue)}
.line{color:var(--muted);font-size:.85rem;margin-top:.3rem}
.note{font-size:.95rem;line-height:1.45;min-width:0}
.note .vl{font-weight:600}
.c-SUPPORTED{color:var(--ok)}.c-PARTIAL{color:var(--part)}.c-UNSUPPORTED,.c-WRONG-SOURCE{color:var(--bad)}
.c-CONTRADICTED{color:var(--contra)}.c-SOURCE-UNREACHABLE{color:var(--gray)}.c-UNCERTAIN{color:var(--blue)}
.chk{margin:.55rem 0 0}
.chk .src{overflow-wrap:anywhere}
blockquote{margin:.35rem 0;padding:0 0 0 .7rem;border-left:2px solid var(--rule);font-family:Georgia,serif;
font-style:italic;color:var(--ink);overflow-wrap:anywhere}
blockquote.closest{color:var(--muted)}
.fine{color:var(--muted);font-size:.88rem}
.tag{display:inline-block;border:1px solid var(--rule);border-radius:3px;padding:0 .35rem;margin:.15rem .25rem 0 0;
font-size:.82rem;color:var(--muted)}
details{margin-top:.5rem}
summary{cursor:pointer;color:var(--blue)}
ol.trail{margin:.4rem 0 0;padding-left:1.2rem}
.found{margin-top:.6rem;padding:.4rem .6rem;border:1px solid var(--ok);border-radius:3px}
.empty{padding:2rem 0;color:var(--muted)}
footer{padding-top:2rem;padding-bottom:3rem;border-top:1px solid var(--rule);margin-top:1.5rem}
footer h2{font-size:1.05rem;margin:1.5rem 0 .5rem}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th,td{text-align:left;padding:.3rem .6rem .3rem 0;border-bottom:1px solid var(--rule);vertical-align:top;
overflow-wrap:anywhere}
.tbl{overflow-x:auto}
.num{font-variant-numeric:tabular-nums}
@media (max-width:760px){
.row{grid-template-columns:minmax(0,1fr);gap:.6rem}
.note{padding-left:.7rem;border-left:2px solid var(--rule)}
.tally{height:2rem}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

_JS = """
(function(){
var btns=document.querySelectorAll('.filters button');
for(var i=0;i<btns.length;i++){btns[i].addEventListener('click',function(){
var on=this.getAttribute('aria-pressed')!=='true';this.setAttribute('aria-pressed',on?'true':'false');
apply();});}
function apply(){
var shown={};
for(var i=0;i<btns.length;i++){shown[btns[i].getAttribute('data-verdict')]=btns[i].getAttribute('aria-pressed')==='true';}
var rows=document.querySelectorAll('.row');
for(var j=0;j<rows.length;j++){rows[j].hidden=shown[rows[j].getAttribute('data-verdict')]===false;}
}
})();
"""

_COLOR = {
    "SUPPORTED": "var(--ok)", "PARTIAL": "var(--part)", "UNSUPPORTED": "var(--bad)",
    "CONTRADICTED": "var(--contra)", "WRONG-SOURCE": "var(--bad)", "SOURCE-UNREACHABLE": "var(--gray)",
    "UNCERTAIN": "var(--blue)",
}
_METHODS = {
    "verbatim": "exact text match", "fuzzy": "near-exact text match", "lexical": "key-term overlap",
    "number-gate": "number check", "nli": "local grounding model", "llm": "language model",
    "fetch": "fetch", "parse": "report parsing",
}


def _domain(url: str | None) -> str:
    if not url:
        return "no URL"
    h = urlsplit(url).hostname or url
    return h[4:] if h.startswith("www.") else h


def _safe_href(url: str | None) -> str | None:
    if url and url.lower().startswith(("http://", "https://")):
        return escape(url, quote=True)
    return None


def _link(url: str | None, label: str) -> str:
    href = _safe_href(url)
    if not href:
        return escape(label)
    return f'<a href="{href}" rel="noopener noreferrer">{escape(label)}</a>'


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _headline(rep: RunReport) -> str:
    st = rep.stats
    total = st["claims"]
    if not total:
        return "No cited claims found."
    return f"{st['passing']} of {total} cited claims hold up."


def _count_sentence(rep: RunReport) -> str:
    c = rep.stats["counts"]
    parts: list[str] = []
    order = [("UNSUPPORTED", "not supported"), ("CONTRADICTED", "contradicted"), ("WRONG-SOURCE", "cite the wrong source"),
             ("PARTIAL", "only partly supported"), ("UNCERTAIN", "need review"),
             ("SOURCE-UNREACHABLE", "could not be checked")]
    for key, label in order:
        if c.get(key):
            parts.append(f"{c[key]} {label}")
    text = ", ".join(parts)
    out = (text[0].upper() + text[1:] + ".") if text else "Every claim was checked."
    unreachable = rep.stats.get("sources_unreachable", 0)
    if unreachable:
        out += f" {_plural(unreachable, 'source')} unreachable."
    return out


def _check_html(chk: SourceCheck) -> str:
    out = ['<div class="chk">']
    label = f"Source [{chk.citation_id}]: {_domain(chk.url)}"
    out.append(f'<div class="src">{_link(chk.url, label)}</div>')
    ev = chk.evidence
    if ev and ev.quote:
        closest = ev.kind == "closest"
        if closest:
            out.append('<div class="fine">Closest passage</div>')
        elif ev.kind == "contradiction":
            out.append('<div class="fine">Passage that conflicts</div>')
        out.append(f'<blockquote class="{"closest" if closest else "support"}">{escape(ev.quote)}</blockquote>')
        bits = []
        if ev.fragment_url and _safe_href(ev.fragment_url):
            bits.append(_link(ev.fragment_url, "Open at quote"))
        bits.append(f'<span class="num">Characters {ev.start}\u2013{ev.end}</span>')
        if not ev.verified:
            bits.append("not located in the source text")
        out.append(f'<div class="fine">{" · ".join(bits)}</div>')
    method = _METHODS.get(chk.method, chk.method)
    out.append(f'<div class="fine">Checked by: {escape(method)}, confidence {chk.confidence:.2f}</div>')
    if chk.reason:
        out.append(f'<div class="fine">{escape(chk.reason)}</div>')
    if chk.validity:
        tags = [f"source quality: {chk.validity.tier.lower()}"] + list(chk.validity.flags)
        out.append("<div>" + "".join(f'<span class="tag">{escape(t)}</span>' for t in tags) + "</div>")
    for n in chk.notes:
        out.append(f'<div class="fine">{escape(n)}</div>')
    out.append("</div>")
    return "".join(out)


def _repair_html(r: ClaimResult) -> str:
    rp = r.repair
    if not rp or not rp.attempted:
        return ""
    out = []
    if rp.found:
        f = rp.found
        out.append('<div class="found"><strong>Found support elsewhere</strong><br>')
        out.append(_link(f.url, _domain(f.url)))
        if f.evidence and f.evidence.quote:
            out.append(f'<blockquote>{escape(f.evidence.quote)}</blockquote>')
            if f.evidence.fragment_url and _safe_href(f.evidence.fragment_url):
                out.append(f'<div class="fine">{_link(f.evidence.fragment_url, "Open at quote")}</div>')
        out.append("</div>")
    summary = f"Search trail ({_plural(rp.queries, 'search', 'searches')}, {_plural(rp.fetches, 'page')})"
    out.append(f"<details><summary>{escape(summary)}</summary><ol class=\"trail\">")
    for s in rp.steps:
        out.append(f'<li>{escape(s.strategy)} via {escape(s.provider)}: {escape(s.query)}</li>')
    if rp.stop_reason:
        out.append(f'<li class="fine">{escape(rp.stop_reason)}</li>')
    out.append("</ol></details>")
    return "".join(out)


def _row_html(r: ClaimResult) -> str:
    v = r.verdict.value
    claim = escape(r.claim.text)
    parts = [f'<section class="row v-{v}" id="{escape(r.claim.id)}" data-verdict="{v}">']
    parts.append(f'<div><p class="claim"><span class="u">{claim}</span></p>'
                 f'<div class="line side">line {r.claim.line}</div></div>')
    parts.append(f'<div class="note side"><div class="vl c-{v}">{escape(r.verdict.label)}</div>')
    for chk in r.checks:
        parts.append(_check_html(chk))
    for n in r.notes:
        parts.append(f'<div class="fine">{escape(n)}</div>')
    parts.append(_repair_html(r))
    parts.append("</div></section>")
    return "".join(parts)


def _source_label(s: dict) -> str:
    """Title when the page has one (several pages on one domain must be tellable apart), else domain and path."""
    title = " ".join(str(s.get("title") or "").split())
    if title:
        return (title[:70] + "\u2026" if len(title) > 70 else title) + f" ({_domain(s.get('url'))})"
    parts = urlsplit(s.get("url") or "")
    return _domain(s.get("url")) + (parts.path if parts.path not in ("", "/") else "")


def _sources_table(rep: RunReport) -> str:
    rows = []
    for key, s in rep.sources.items():
        if key.startswith("cited-text:"):
            continue
        status = "ok" if s.get("ok") else (s.get("reason") or "unreachable")
        v = s.get("validity") or {}
        flags = "; ".join(v.get("flags") or [])
        sha = (s.get("sha256") or "")[:12]
        rows.append(
            f"<tr><td>{_link(s.get('url'), _source_label(s))}</td><td>{escape(str(status))}</td>"
            f"<td>{escape(str(v.get('tier', '')).lower())}</td><td>{escape(flags)}</td>"
            f"<td class=\"num\">{escape(sha)}</td></tr>"
        )
    if not rows:
        return ""
    head = "<tr><th>Source</th><th>Status</th><th>Quality</th><th>Flags</th><th>Copy hash</th></tr>"
    return f'<h2>Sources</h2><div class="tbl"><table>{head}{"".join(rows)}</table></div>'


def render_html(rep: RunReport) -> str:
    claims = rep.claims
    counts = rep.stats["counts"]
    ticks = "".join(
        f'<a href="#{escape(r.claim.id)}" style="background:{_COLOR[r.verdict.value]}" '
        f'aria-label="{escape(r.claim.id)}: {escape(r.verdict.label)}" title="{escape(r.claim.id)}: '
        f'{escape(r.verdict.label)}"></a>' for r in claims
    )
    buttons = "".join(
        f'<button type="button" aria-pressed="true" data-verdict="{v.value}">{escape(v.label)} {counts[v.value]}</button>'
        for v in Verdict if counts.get(v.value)
    )
    if claims:
        body = "".join(_row_html(r) for r in claims)
    else:
        body = ('<p class="empty">No cited claims found. saysso looks for [1] markers, footnotes and links '
                'in the report.</p>')
    u = rep.usage
    if u.get("model"):
        cost = f", about ${u['cost_usd']:.4f}" if u.get("cost_usd") else ""
        usage = f"Model {u['model']}: {u['calls']} calls{cost}; {u.get('cached_hits', 0)} answered from cache."
    else:
        usage = "No model was used: deterministic checks only."
    usage += f" {u.get('live_fetches', 0)} pages fetched, {u.get('searches', 0)} searches."
    date = rep.generated_at.replace("T", " ").replace("+00:00", " UTC")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>saysso: {escape(rep.input)}</title><style>{_CSS}</style></head><body>"
        f'<header><div class="mark">saysso</div>'
        f'<div class="meta">{escape(rep.input)} · {escape(date)} · version {escape(rep.version)}</div>'
        f"<h1>{escape(_headline(rep))}</h1><p class=\"counts\">{escape(_count_sentence(rep))}</p>"
        f'<div class="tally" role="navigation" aria-label="Claims in report order">{ticks}</div>'
        '<p class="legend side">One tick per claim, in report order. Select a tick to jump to it.</p>'
        f'<div class="filters side" role="group" aria-label="Filter by verdict">{buttons}</div></header>'
        f"<main>{body}</main>"
        f'<footer class="side">{_sources_table(rep)}<h2>How to read this</h2>'
        "<p>Verdicts compare each claim with the text of the source it cites. Quotes are located in the stored "
        "copy of each source; character ranges refer to that copy. Deterministic checks run first; a model is "
        "consulted only for uncertain claims.</p>"
        f'<p class="fine">{escape(usage)}</p></footer>'
        f"<script>{_JS}</script></body></html>"
    )
