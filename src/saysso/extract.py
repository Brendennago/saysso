"""Turn fetched bytes into plain text we can quote from, plus metadata (title, date, DOI, links).

The full visible text is kept (not just "main content") so a supporting sentence in a table, caption
or sidebar still counts. Offsets of links are recorded against that exact text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .models import Link

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>?#]+)", re.I)

_DATE_META_KEYS = (
    "citation_publication_date", "citation_date", "citation_online_date", "article:published_time",
    "og:article:published_time", "dc.date.issued", "dcterms.issued", "dc.date", "dcterms.date",
    "prism.publicationdate", "pubdate", "publish-date", "publishdate", "parsely-pub-date", "sailthru.date",
    "date", "datepublished",
)
_DOI_META_KEYS = ("citation_doi", "dc.identifier", "prism.doi", "bepress_citation_doi", "dc.identifier.doi")


class MissingDependency(RuntimeError):
    pass


@dataclass
class Extracted:
    text: str
    title: str | None = None
    meta: dict[str, str] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    published: str | None = None
    doi: str | None = None


class _Extractor(HTMLParser):
    BLOCK = {
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section",
        "article", "header", "footer", "nav", "aside", "blockquote", "pre", "dd", "dt", "dl", "figcaption",
        "figure", "main", "hr", "caption", "summary", "details", "address", "thead", "tbody",
    }
    SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "canvas", "object", "embed"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.length = 0
        self.skip = 0
        self.in_title = False
        self.in_jsonld = False
        self.title = ""
        self.meta: dict[str, str] = {}
        self.jsonld: list[str] = []
        self.times: list[str] = []
        self.links: list[tuple[str, int, int]] = []
        self._a: list[tuple[str, int]] = []

    # text emission keeps offsets stable: no post-processing after the fact
    def _emit(self, s: str) -> None:
        s = re.sub(r"\s+", " ", s)
        if not s:
            return
        if not self.parts or self.parts[-1].endswith(("\n", " ")):
            s = s.lstrip(" ")
            if not s:
                return
        self.parts.append(s)
        self.length += len(s)

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            if self.parts[-1].endswith(" "):
                self.parts[-1] = self.parts[-1][:-1]
                self.length -= 1
            self.parts.append("\n")
            self.length += 1

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in self.SKIP:
            if tag == "script" and a.get("type", "").lower() == "application/ld+json":
                self.in_jsonld = True
            self.skip += 1
            return
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        if tag in self.BLOCK:
            self._newline()
        elif tag in ("td", "th"):
            self._emit(" ")
        if tag == "a" and a.get("href"):
            self._a.append((a["href"], self.length))

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            self.in_jsonld = False
            return
        if tag == "title":
            self.in_title = False
        if tag == "a" and self._a:
            href, start = self._a.pop()
            self.links.append((href, start, self.length))
        if tag in self.BLOCK:
            self._newline()

    def handle_data(self, data):
        if self.in_jsonld:
            self.jsonld.append(data)
            return
        if self.skip:
            return
        if self.in_title:
            self.title += data
            return
        self._emit(data)


def _norm_date(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(1[89]\d\d|20\d\d)(?:[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?", s)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if mo and d:
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    if mo:
        return f"{y}-{int(mo):02d}"
    return y


def html_extract(html: str) -> Extracted:
    p = _Extractor()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: keep what we have
        pass
    text = "".join(p.parts).strip("\n ")
    # parts may have a leading newline we stripped; keep link offsets consistent with stripped text
    lead = len("".join(p.parts)) - len("".join(p.parts).lstrip("\n "))
    links = []
    for href, s, e in p.links:
        s2, e2 = max(0, s - lead), max(0, e - lead)
        anchor = text[s2:e2].strip()
        links.append(Link(href=href, text=anchor[:300], start=s2, end=e2))
    published = None
    for k in _DATE_META_KEYS:
        if p.meta.get(k):
            published = _norm_date(p.meta[k])
            if published:
                break
    if not published:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', " ".join(p.jsonld))
        if m:
            published = _norm_date(m.group(1))
    if not published and p.times:
        published = _norm_date(p.times[0])
    doi = None
    for k in _DOI_META_KEYS:
        v = p.meta.get(k)
        if v and DOI_RE.search(v):
            doi = DOI_RE.search(v).group(1).rstrip(".")
            break
    title = re.sub(r"\s+", " ", p.title).strip() or p.meta.get("og:title") or p.meta.get("citation_title")
    return Extracted(text=text, title=title, meta=p.meta, links=links, published=published, doi=doi)


def pdf_extract(data: bytes, max_pages: int = 400) -> Extracted:
    try:
        import fitz  # PyMuPDF
    except ImportError as e:  # pragma: no cover - depends on optional extra
        raise MissingDependency("PDF support needs PyMuPDF: pip install 'saysso[pdf]'") from e
    pages: list[str] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        meta = {k: str(v) for k, v in (doc.metadata or {}).items() if v}
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pages.append(page.get_text("text"))
    text = "\n".join(pages).strip()
    head = text[:8000]
    m = DOI_RE.search(head)
    doi = m.group(1).rstrip(".") if m else None
    published = _norm_date(meta.get("creationDate", "")[2:10] if meta.get("creationDate") else None)
    return Extracted(text=text, title=meta.get("title"), meta=meta, published=published, doi=doi)
