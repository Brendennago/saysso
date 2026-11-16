import asyncio

from saysso.config import Config
from saysso.llm import BaseLLM
from saysso.models import Verdict
from saysso.parse import parse_text
from saysso.pipeline import run


def cfg(**kw):
    base = dict(allow_private=True, no_cache=True, wayback=False, retraction_check=False)
    base.update(kw)
    return Config(**base)


def report(server, body):
    md = body + f"""

## Sources
1. Tower {server}/tower
2. Blocked {server}/blocked
3. Missing {server}/nope
4. Reactor {server}/injection
"""
    return parse_text(md, suffix=".md")


def by_text(rep, needle):
    return next(r for r in rep.claims if needle in r.claim.text)


def test_offline_pipeline(server):
    p = report(server, "The tower is 330 metres tall and was completed in 1889 [1]. The tower is 512 metres tall [1]. "
                       "It reopened after the war [2]. Something else happened in 2020 [3].")
    rep = asyncio.run(run(p, cfg(), "t.md"))
    assert by_text(rep, "330 metres").verdict == Verdict.SUPPORTED
    sup = by_text(rep, "330 metres").checks[0]
    assert sup.evidence and "330 metres" in sup.evidence.quote and sup.evidence.verified
    assert by_text(rep, "512").verdict == Verdict.CONTRADICTED
    blocked = by_text(rep, "reopened").checks[0]
    assert blocked.verdict == Verdict.UNREACHABLE and "bot protection" in blocked.reason
    assert by_text(rep, "2020").checks[0].reason == "HTTP 404"
    assert rep.stats["counts"]["SUPPORTED"] == 1 and rep.usage["calls"] == 0


class FakeLLM(BaseLLM):
    provider = "fake"

    def __init__(self, budget, cache, client, answers):
        super().__init__("m", budget, cache, client)
        self.answers = answers
        self.prompts = []

    async def _call(self, system, prefix, suffix, schema, max_tokens):
        import json
        import re
        self.prompts.append(prefix + suffix)
        ids = re.findall(r'<claim id="(c\d+)">', suffix)
        rows = [dict(claim_id=i, passage_ids=["P1"], reason="r", confidence=0.9, **self.answers(i, prefix, suffix))
                for i in ids]
        return json.dumps({"results": rows}), 100, 50, 0


def test_llm_escalation_quote_verification_and_injection_fencing(server, monkeypatch):
    import saysso.pipeline as pl

    def answers(cid, prefix, suffix):
        if "Moon" in suffix:
            return {"verdict": "CONTRADICTED", "quote": "It was not visible from the Moon"}
        return {"verdict": "SUPPORTED", "quote": "a sentence the model invented that is not in the page"}

    made = {}

    def fake_make(spec, budget, cache, client):
        if not spec:
            return None
        made["llm"] = FakeLLM(budget, cache, client, answers)
        return made["llm"]

    monkeypatch.setattr(pl, "make_llm", fake_make)
    p = report(server, "The tower was visible from the Moon according to legend [1]. "
                       "The reactor produced 42 megawatts in 2021 and powered the whole region [4].")
    rep = asyncio.run(run(p, cfg(model="fake/m"), "t.md"))
    moon = by_text(rep, "Moon")
    assert moon.verdict == Verdict.CONTRADICTED and moon.checks[0].evidence.kind == "contradiction"
    assert moon.checks[0].evidence.verified
    reactor = by_text(rep, "reactor").checks[0]
    assert reactor.confidence <= 0.5 and any("not found in the source" in n for n in reactor.notes)
    prompt = "\n".join(made["llm"].prompts)
    assert "<passage" in prompt and "\u2039" not in prompt.split("<claims>")[1]
    assert rep.usage["calls"] >= 1


def test_repair_follows_upstream_link(server):
    p = report(server, "The tower welcomed about 6.9 million visitors in 2015 and the lift system was renovated in 2017 [2].")
    p.citations["2"].url = f"{server}/tower"
    rep = asyncio.run(run(p, cfg(fix=True), "t.md"))
    r = rep.claims[0]
    assert r.repair and r.repair.attempted
    assert any(s.strategy == "upstream" for s in r.repair.steps)
    assert r.repair.found and r.repair.found.url.endswith("/primary")


def test_offline_never_accuses_a_true_claim_that_uses_synonyms(server):
    """Keyword overlap cannot bridge 'elevation' vs 'height' or 'established' vs 'announced'.
    Without a model the honest answer is UNCERTAIN, never UNSUPPORTED."""
    md = f"""The mountain's elevation of 8,849 metres was established by a Chinese-Nepali survey [1].

## Sources
1. Peak {server}/synonyms
"""
    rep = asyncio.run(run(parse_text(md, suffix=".md"), cfg(), "t.md"))
    assert rep.claims[0].verdict == Verdict.UNCERTAIN, rep.claims[0].checks[0].reason


def test_offline_still_flags_a_claim_the_source_is_not_about(server):
    md = f"""Bananas contain more potassium than oranges and are grown mostly in tropical plantations [1].

## Sources
1. Peak {server}/synonyms
"""
    rep = asyncio.run(run(parse_text(md, suffix=".md"), cfg(), "t.md"))
    assert rep.claims[0].verdict == Verdict.UNSUPPORTED
