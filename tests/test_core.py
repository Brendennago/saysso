from saysso.gates import extract_numbers, num_match, number_conflicts
from saysso.locate import locate, text_fragment
from saysso.normalize import NormText
from saysso.parse import parse_text


def test_locate_exact_with_ligatures_hyphenation_and_quotes():
    src = "The e\ufb03cient infor-\nmation system \u201cworks\u201d  well."
    m = locate('efficient information system "works" well', NormText(src))
    assert m and m.tier == 1
    assert src[m.start:m.end].startswith("e\ufb03cient") and src[m.start:m.end].endswith("works\u201d  well")


def test_locate_ellipsis_and_fuzzy_and_miss():
    src = "Revenue grew strongly in the third quarter, and margins improved across every region we track."
    assert locate("Revenue grew strongly ... margins improved", src).tier == 2
    assert locate("Revenue grew strongely in the third quarter and margins improved", src).tier == 3
    assert locate("Profits collapsed in the fourth quarter everywhere", src) is None


def test_text_fragment_encodes_dashes():
    u = text_fragment("https://ex.com/a#old", "a well-known fact")
    assert u == "https://ex.com/a#:~:text=a%20well%2Dknown%20fact"


def test_number_matching_rules():
    n = lambda s: extract_numbers(s)[0]
    assert num_match(n("1.2 million"), n("1,234,567"))
    assert num_match(n("40%"), n("39.6%"))
    assert num_match(n("8,849 m"), n("8,848.86 m"))
    assert not num_match(n("8,849 m"), n("8,844 m"))
    assert num_match(n("about 7 million"), n("6.9 million"))
    assert not num_match(n("40%"), n("40"))
    assert not num_match(n("1990"), n("1991"))


def test_number_conflict_same_unit():
    assert number_conflicts("The tower is 512 metres tall", "The tower is 330 metres (1,083 ft) tall")
    assert not number_conflicts("The tower is 330 metres tall", "The tower is 330 metres (1,083 ft) tall")


def test_parse_markers_references_segments_and_links():
    md = """Intro sentence without citation but with 12 numbers in it for sure.

The tower is 330 metres tall [1]. It opened in 1889.[2] Sales rose 4% [1], while costs fell 2% [3].

According to [the WHO](https://www.who.int/x), cases doubled. See ([apnews.com](https://apnews.com/y)) too.

## Sources
1. Tower page https://en.wikipedia.org/wiki/Python_(programming_language)
2. [Britannica](https://www.britannica.com/t)
3. A title-only report, 2024
"""
    r = parse_text(md, suffix=".md")
    texts = [(c.text, c.citation_ids) for c in r.claims]
    assert ("The tower is 330 metres tall.", ["1"]) in texts
    assert ("It opened in 1889.", ["2"]) in texts
    assert ("Sales rose 4%", ["1"]) in texts and ("while costs fell 2%", ["3"]) in texts
    assert any(t == "According to the WHO, cases doubled." for t, _ in texts)
    assert r.citations["1"].url.endswith("Python_(programming_language)")
    assert r.citations["2"].url == "https://www.britannica.com/t"
    assert r.citations["3"].url is None and "title-only" in r.citations["3"].title
    assert r.uncited and r.uncited[0]["line"] == 1
    for c in r.claims:
        assert r.text[c.start:c.end].strip().startswith(c.raw[:10])


def test_parse_anthropic_and_openai_json():
    a = {"type": "message", "role": "assistant", "content": [
        {"type": "text", "text": "The tower is 330 metres tall.", "citations": [
            {"type": "web_search_result_location", "url": "https://ex.com/t", "title": "T", "cited_text": "330 metres"}]}]}
    r = parse_text(__import__("json").dumps(a), suffix=".json")
    assert r.claims[0].citation_ids == ["A1"] and r.citations["A1"].cited_text == "330 metres"
    text = "The tower is 330 metres tall ([ex.com](https://ex.com/t)). Unrelated sentence here."
    s = text.index("([ex"); e = text.index(")).") + 2
    o = {"output": [{"type": "message", "content": [{"type": "output_text", "text": text, "annotations": [
        {"type": "url_citation", "start_index": s, "end_index": e, "url": "https://ex.com/t", "title": "T"}]}]}]}
    r = parse_text(__import__("json").dumps(o), suffix=".json")
    assert len(r.claims) == 1 and r.claims[0].text == "The tower is 330 metres tall."


def test_evidence_window_does_not_cross_line_breaks():
    from saysso.judge import _best_sentence
    from saysso.normalize import NormText
    from saysso.retrieve import Passage

    text = ("Panoramic view during ascent of the tower, 1898\n"
            "Originally commissioned as a temporary structure for the 1889 Exposition, it was kept after.")
    p = Passage("P1", 0, len(text), text)
    a, b = _best_sentence(NormText(text), p, "The tower was commissioned for the 1889 Exposition")
    assert "Panoramic" not in text[a:b] and text[a:b].startswith("Originally")
