import pytest

from saysso.models import SourceDoc
from saysso.validity import (apply_retraction, base_validity, crossref_status, domain_class,
                                duplicate_clusters, stale_flag)

CASES = [
    ("https://www.who.int/news/item/x", "government"),
    ("https://www.cdc.gov/flu/data", "government"),
    ("https://www.gov.uk/government/statistics", "government"),
    ("https://www.nature.com/articles/s41586-020-1", "academic"),
    ("https://www.cs.stanford.edu/people/x", "academic"),
    ("https://arxiv.org/abs/2605.06635", "preprint"),
    ("https://en.wikipedia.org/wiki/Python", "reference"),
    ("https://www.reuters.com/world/x", "news"),
    ("https://www.reddit.com/r/science/x", "ugc"),
    ("https://chatgpt.com/share/abc123", "ai-generated"),
    ("https://www.perplexity.ai/search/xyz", "ai-generated"),
    ("https://example.com/post", "other"),
]


@pytest.mark.parametrize("url,expected", CASES)
def test_domain_class(url, expected):
    assert domain_class(url) == expected


def test_crossref_status_fixtures():
    assert crossref_status({"updated-by": [{"type": "retraction"}]})["retracted"] is True
    assert crossref_status({"updated-by": [{"type": "expression_of_concern"}]})["concern"] is True
    assert crossref_status({"updated-by": [{"type": "correction"}]})["corrected"] is True
    assert crossref_status({"relation": {"is-retracted-by": [{"id": "10.1/x"}]}})["retracted"] is True
    clean = crossref_status({"type": "journal-article"})
    assert not (clean["retracted"] or clean["concern"] or clean["corrected"])


def doc(url, **kw):
    return SourceDoc(url=url, ok=True, **kw)


def test_retraction_invalidates_even_a_top_source():
    v = base_validity(doc("https://www.nature.com/articles/x"))
    assert v.tier == "HIGH"
    v = apply_retraction(v, {"crossref": {"retracted": True}, "openalex": {"retracted": False}})
    assert v.tier == "INVALID" and v.retraction == "retracted"
    assert any("databases disagree" in f for f in v.flags)


def test_openalex_only_and_concern_and_preprint_type():
    v = apply_retraction(base_validity(doc("https://example.org/p")), {"crossref": None, "openalex": {"retracted": True}})
    assert v.tier == "INVALID" and any("OpenAlex only" in f for f in v.flags)
    v = apply_retraction(base_validity(doc("https://www.nature.com/articles/x")),
                         {"crossref": {"concern": True, "retracted": False}, "openalex": None})
    assert v.retraction == "expression of concern" and v.score <= 0.45
    v = apply_retraction(base_validity(doc("https://example.org/p")),
                         {"crossref": {"type": "posted-content", "retracted": False}, "openalex": None})
    assert any("preprint" in f for f in v.flags)


def test_staleness_and_ai_source_flags():
    v = base_validity(doc("https://example.org/p", published="2019-04-01"))
    assert "2021" in stale_flag(v, [2021])
    assert stale_flag(v, [2018]) is None
    ai = base_validity(doc("https://chatgpt.com/share/abc"))
    assert ai.tier == "INVALID" and any("circular" in f for f in ai.flags)


def test_syndicated_duplicates_cluster():
    body = " ".join(f"word{i}" for i in range(400))
    a, b = doc("https://a.example/x", text=body), doc("https://b.example/y", text=body + " extra tail words here")
    c = doc("https://c.example/z", text=" ".join(f"other{i}" for i in range(400)))
    assert duplicate_clusters([a, b, c]) == {"https://b.example/y": "https://a.example/x"}
