import asyncio
import json

from saysso.config import Config
from saysso.parse import parse_text
from saysso.pipeline import run
from saysso.report import render_html, render_sarif


def make(server, extra=""):
    md = f"""The tower is 330 metres tall and was completed in 1889 [1]. The tower is 512 metres tall [1].
It reopened after the war [2]. <script>alert(1)</script> The tower sold 99 tickets in 2020 [1]. {extra}

## Sources
1. Tower {server}/tower
2. Blocked {server}/blocked
"""
    cfg = Config(allow_private=True, no_cache=True, wayback=False, retraction_check=False)
    return md, asyncio.run(run(parse_text(md, suffix=".md"), cfg, "report.md"))


def test_html_is_self_contained_and_escaped(server):
    _, rep = make(server)
    html = render_html(rep)
    assert "<script src" not in html and '<link rel="stylesheet"' not in html
    assert "prefers-color-scheme: dark" in html or "prefers-color-scheme:dark" in html
    for r in rep.claims:
        assert f'id="{r.claim.id}"' in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "aria-pressed" in html and 'class="tally"' in html


def test_html_empty_state():
    from saysso.models import RunReport
    rep = RunReport("x.md", "2026-01-01T00:00:00+00:00", "0.1.0", [], {},
                    {"claims": 0, "counts": {v: 0 for v in ["SUPPORTED", "PARTIAL", "UNSUPPORTED", "CONTRADICTED",
                     "WRONG-SOURCE", "SOURCE-UNREACHABLE", "UNCERTAIN"]}, "passing": 0, "sources_unreachable": 0},
                    {}, {})
    assert "No cited claims found" in render_html(rep)


def test_sarif_structure_and_lines(server):
    md, rep = make(server)
    s = json.loads(render_sarif([rep], {"report.md": md}))
    assert s["version"] == "2.1.0" and s["runs"][0]["tool"]["driver"]["name"] == "saysso"
    res = s["runs"][0]["results"]
    ids = {r["ruleId"] for r in res}
    assert "citation-contradicted" in ids and "citation-unreachable" in ids
    levels = {r["ruleId"]: r["level"] for r in res}
    assert levels["citation-contradicted"] == "error" and levels["citation-unreachable"] == "warning"
    for r, claim in zip(res, [c for c in rep.claims if not c.passes]):
        assert r["locations"][0]["physicalLocation"]["region"]["startLine"] == claim.claim.line


def test_sources_table_tells_pages_on_one_domain_apart(server):
    _, rep = make(server)
    html = render_html(rep)
    assert "The Tower (127.0.0.1)" in html  # page title, not just the bare domain
