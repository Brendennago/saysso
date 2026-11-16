from saysso.parse import parse_text

HTML = """<!doctype html><html><head><title>Report</title><style>p{color:red}</style></head><body>
<h1>Findings</h1>
<p>The tower is 330 metres tall and was completed in 1889 <sup><a href="https://example.com/tower">[1]</a></sup>.
It reopened after the war, <a href="https://example.com/history">the city archive says</a>.</p>
<p>Visitors numbered 6.9 million in 2015<a href="https://example.com/visitors">[2]</a>.</p>
<h2>Sources</h2>
<ol><li><a href="https://example.com/tower">Tower facts</a></li><li><a href="https://example.com/visitors">Visitor survey</a></li></ol>
</body></html>"""


def test_html_report_citations_become_claims():
    r = parse_text(HTML, suffix=".html")
    assert r.format == "html"
    by = {c.text: c.citation_ids for c in r.claims}
    tower = next(t for t in by if "330 metres" in t)
    assert r.citations[by[tower][0]].url == "https://example.com/tower"
    visitors = next(t for t in by if "6.9 million" in t)
    assert r.citations[by[visitors][0]].url == "https://example.com/visitors"
    assert any("reopened" in t for t in by)  # a prose link makes the sentence a cited claim
    assert "color:red" not in r.text  # <style> is dropped


def test_html_numbered_link_markers_without_a_sources_list():
    h = ('<p>The tower is 330 metres tall and was completed in 1889 <a href="https://example.com/tower">[1]</a>. '
         'Visitors numbered 6.9 million in 2015 <a href="https://example.com/visitors">[2]</a>.</p>')
    r = parse_text(h, suffix=".html")
    assert [c.text for c in r.claims] == ["The tower is 330 metres tall and was completed in 1889.",
                                          "Visitors numbered 6.9 million in 2015."]
    assert [r.citations[c.citation_ids[0]].url for c in r.claims] == ["https://example.com/tower",
                                                                       "https://example.com/visitors"]
