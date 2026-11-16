import asyncio

from saysso.cache import Cache
from saysso.config import Config
from saysso.fetch import Fetcher, canonical_url
from saysso.models import Verdict
from saysso.parse import parse_text
from saysso.pipeline import run

from test_pipeline import FakeLLM, by_text, cfg


def fetch_one(url, **cfg_kw):
    async def go():
        f = Fetcher(Config(no_cache=True, wayback=False, **cfg_kw), Cache(enabled=False))
        try:
            return await f.fetch(url)
        finally:
            await f.aclose()
    return asyncio.run(go())


def test_ssrf_guard_blocks_loopback_and_metadata_endpoint(server):
    for url in (f"{server}/tower", "http://127.0.0.1:1/x", "http://169.254.169.254/latest/meta-data/",
                "http://[::1]/x", "http://10.0.0.5/admin"):
        doc = fetch_one(url)
        assert not doc.ok and "SSRF guard" in (doc.reason or ""), url
    assert fetch_one(f"{server}/tower", allow_private=True).ok


def test_non_http_schemes_refused():
    for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x"):
        assert not fetch_one(url).ok


def test_canonical_url_strips_tracking_and_fragments():
    assert canonical_url("HTTPS://Example.com/a?utm_source=x&id=2#frag") == "https://example.com/a?id=2"


def test_budget_caps_model_calls_and_run_still_completes(server, monkeypatch):
    import saysso.pipeline as pl

    made = {}

    def fake_make(spec, budget, cache, client):
        if not spec:
            return None
        made["llm"] = FakeLLM(budget, cache, client, lambda i, p, s: {"verdict": "UNSUPPORTED", "quote": ""})
        return made["llm"]

    monkeypatch.setattr(pl, "make_llm", fake_make)
    md = f"""The tower was visible from the Moon according to legend [1]. The tower was painted yellow during the war [1].
The tower closed to every visitor in the winter [1].

## Sources
1. Tower {server}/tower
"""
    rep = asyncio.run(run(parse_text(md, suffix=".md"),
                          cfg(model="fake/m", max_llm_calls=1, batch_size=1), "t.md"))
    assert rep.usage["calls"] == 1 and len(made["llm"].prompts) == 1
    assert rep.usage["budget_note"] and "budget" in rep.usage["budget_note"]
    methods = [r.checks[0].method for r in rep.claims]
    assert methods.count("llm") == 1
    assert sum("not sent to model" in n for r in rep.claims for n in r.checks[0].notes) >= 1
    assert len(rep.claims) == 3  # run completed and every claim still has a verdict
    assert all(r.verdict in set(Verdict) for r in rep.claims)
