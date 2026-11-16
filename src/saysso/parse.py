"""Turn a report into claims and citations.

Supported inputs:
- Markdown / plain text with numbered markers ([1], [1, 3], [2-4], [^1], 【3】, [1](url)) plus a
  Sources / References / Works cited list, footnote definitions, or link reference definitions.
- Inline markdown links: "[the WHO report](https://...)" makes the sentence a claim citing that URL.
- Title-only references (ChatGPT and Gemini app exports often drop links): kept, and resolved later
  by search when a search provider is configured.
- HTML (converted to markdown-ish text first).
- JSON from the Anthropic Messages API (text blocks with citations) and the OpenAI Responses /
  Chat Completions APIs (url_citation annotations).
"""

from __future__ import annotations

import bisect
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .models import Citation, Claim

URL_RE = re.compile(r"https?://[^\s<>\"'`\[\]]+", re.I)
MD_LINK_RE = re.compile(
    r"(?P<img>!?)\[(?P<text>[^\]\n]{0,300})\]\(\s*(?P<url><[^>\n]+>|(?:[^()\s]+|\([^()\s]*\))+)(?:\s+\"[^\"\n]*\")?\s*\)"
)
FOOTNOTE_REF_RE = re.compile(r"\[\^(?P<id>[^\]\s]{1,20})\](?!:)")
NUM_GROUP_RE = re.compile(r"\[(?P<ids>\d{1,3}(?:\s*[-\u2013,]\s*\d{1,3})*)\](?![:(\[])")
FULLWIDTH_RE = re.compile(r"\u3010(?P<id>\d{1,3})(?:\u2020[^\u3011]*)?\u3011")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.*?)\s*#*\s*$")
REF_HEADINGS = re.compile(
    r"^(?:sources?|references?|citations?|works cited|bibliography|notes|footnotes|links|cited sources|"
    r"sources cited|further reading|endnotes)\s*:?\s*$",
    re.I,
)
REFDEF_RE = re.compile(r"^\s{0,3}\[(?P<fn>\^?)(?P<id>[^\]]{1,40})\]:\s*(?P<rest>.+)$")
REFLIST_RE = re.compile(r"^\s*(?:[-*+]\s+)?(?:\[(?P<a>\d{1,3})\]|(?P<b>\d{1,3})[.)])\s+(?P<rest>.+)$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
HR_RE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$")

_ABBREV = {
    "e.g", "i.e", "etc", "vs", "dr", "mr", "mrs", "ms", "prof", "inc", "ltd", "co", "corp", "jr", "sr", "st",
    "no", "fig", "figs", "al", "approx", "est", "u.s", "u.k", "u.n", "e.u", "jan", "feb", "mar", "apr", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec", "mt", "ft", "vol", "pp", "ed", "eds", "rev", "gen",
    "gov", "sen", "rep", "ca", "cf", "ph.d", "u.s.a", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
}
_TRAIL = ".,;:!?'\"*_"


def clean_url(u: str) -> str:
    u = u.strip().strip("<>")
    while u:
        c = u[-1]
        if c in _TRAIL or c in "]}>":
            u = u[:-1]
        elif c == ")" and u.count(")") > u.count("("):
            u = u[:-1]
        else:
            break
    return u


@dataclass
class Marker:
    start: int
    end: int
    ids: list[str]
    kind: str  # num | footnote | fullwidth | numlink | textlink | bareurl | annotation


@dataclass
class ParsedReport:
    text: str
    claims: list[Claim]
    citations: dict[str, Citation]
    uncited: list[dict] = field(default_factory=list)
    format: str = "markdown"


# ----------------------------------------------------------------------------- helpers


def _expand_ids(s: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"\s*,\s*", s.strip()):
        m = re.fullmatch(r"(\d+)\s*[-\u2013]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < b - a <= 30:
                out.extend(str(x) for x in range(a, b + 1))
                continue
        if part.isdigit():
            out.append(str(int(part)))
    return out


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def _strip_markdown(s: str) -> str:
    s = MD_LINK_RE.sub(lambda m: "" if m.group("text").strip().isdigit() else m.group("text"), s)
    s = FOOTNOTE_REF_RE.sub("", s)
    s = NUM_GROUP_RE.sub("", s)
    s = FULLWIDTH_RE.sub("", s)
    s = BULLET_RE.sub("", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
    s = s.replace("`", "")
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([.,;:!?%])", r"\1", s)
    s = s.lstrip(",;:\u2014\u2013- ").strip()
    return s


def _parse_ref_rest(rest: str) -> tuple[str | None, str | None]:
    """Extract (url, title) from the remainder of a reference-list line."""
    m = MD_LINK_RE.search(rest)
    if m and not m.group("img"):
        url = clean_url(m.group("url"))
        title = m.group("text").strip() or None
        if title and URL_RE.fullmatch(title):
            title = None
        tail = (rest[: m.start()] + rest[m.end() :]).strip(" -\u2013\u2014.,:")
        return url, title or (tail or None)
    m = URL_RE.search(rest)
    if m:
        url = clean_url(m.group(0))
        title = (rest[: m.start()] + rest[m.end() :]).strip(" -\u2013\u2014.,:()\"'<>")
        title = re.sub(r"\s+", " ", title) or None
        return url, title
    title = rest.strip(" -\u2013\u2014.,:\"'") or None
    return None, title


# ----------------------------------------------------------------------------- markers


def find_markers(text: str, citations: dict[str, Citation], link_ids: dict[str, str]) -> list[Marker]:
    """Find citation markers in a paragraph. Inline links register citations as a side effect."""
    masked = INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), text)
    markers: list[Marker] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in taken)

    for m in MD_LINK_RE.finditer(masked):
        if m.group("img"):
            taken.append(m.span())
            continue
        url = clean_url(m.group("url"))
        if not url.lower().startswith(("http://", "https://")):
            continue
        label = m.group("text").strip()
        if label.isdigit():
            cid = str(int(label))
            c = citations.setdefault(cid, Citation(id=cid, raw=m.group(0)))
            c.url = c.url or url
            markers.append(Marker(m.start(), m.end(), [cid], "numlink"))
        else:
            cid = link_ids.get(url)
            if cid is None:
                cid = f"L{len(link_ids) + 1}"
                link_ids[url] = cid
                citations[cid] = Citation(id=cid, url=url, title=label or None, raw=m.group(0))
            kind = "numlink" if _looks_like_source_label(label, url) else "textlink"
            markers.append(Marker(m.start(), m.end(), [cid], kind))
        taken.append(m.span())

    for rx, kind in ((FOOTNOTE_REF_RE, "footnote"), (FULLWIDTH_RE, "fullwidth")):
        for m in rx.finditer(masked):
            if free(*m.span()):
                markers.append(Marker(m.start(), m.end(), [m.group("id")], kind))
                taken.append(m.span())
    for m in NUM_GROUP_RE.finditer(masked):
        if free(*m.span()):
            ids = _expand_ids(m.group("ids"))
            if ids:
                markers.append(Marker(m.start(), m.end(), ids, "num"))
                taken.append(m.span())
    for m in URL_RE.finditer(masked):
        if not free(*m.span()):
            continue
        url = clean_url(m.group(0))
        cid = link_ids.get(url)
        if cid is None:
            cid = f"L{len(link_ids) + 1}"
            link_ids[url] = cid
            citations[cid] = Citation(id=cid, url=url, raw=url)
        end = m.start() + len(url)
        # swallow wrapping parentheses: "(https://...)"
        a, b = m.start(), end
        if a > 0 and masked[a - 1] == "(" and b < len(masked) and masked[b] == ")":
            a, b = a - 1, b + 1
        markers.append(Marker(a, b, [cid], "numlink"))
        taken.append((a, b))
    markers.sort(key=lambda mk: mk.start)
    return markers


def _looks_like_source_label(label: str, url: str) -> bool:
    """OpenAI/Perplexity style "([apnews.com](...))" labels behave like markers, not prose."""
    lab = label.lower().strip()
    if not lab:
        return True
    if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(?:/\S*)?", lab):
        return True
    if lab in {"source", "link", "here", "ref", "via"}:
        return True
    return False


_PURE = {"num", "footnote", "fullwidth", "numlink", "annotation"}


def split_sentences(text: str, markers: list[Marker]) -> list[tuple[int, int]]:
    """Sentence spans; citation markers right after the end punctuation stay with their sentence."""
    pure_at = {m.start: m for m in markers if m.kind in _PURE}
    spans: list[tuple[int, int]] = []
    start, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ".!?":
            if ch == ".":
                if 0 < i < n - 1 and text[i - 1].isdigit() and text[i + 1].isdigit():
                    i += 1
                    continue
                k = i - 1
                while k >= 0 and (text[k].isalpha() or text[k] == "."):
                    k -= 1
                word = text[k + 1 : i].lower()
                if word in _ABBREV or (len(word) == 1 and word.isalpha() and k >= 0 and text[k] in " ("):
                    i += 1
                    continue
            j = i + 1
            while j < n and text[j] in ".!?\"'\u201d\u2019)]*_":
                j += 1
            while True:
                k = j
                while k < n and text[k] == " ":
                    k += 1
                mk = pure_at.get(k)
                if mk:
                    j = mk.end
                    continue
                break
            if j >= n:
                spans.append((start, n))
                start = n
                break
            if text[j].isspace():
                k = j
                while k < n and text[k].isspace():
                    k += 1
                if k >= n or text[k].isupper() or text[k].isdigit() or text[k] in "\"'\u201c\u2018([*_":
                    spans.append((start, j))
                    start = k
                    i = k
                    continue
            i = j
            continue
        i += 1
    if start < n and text[start:].strip():
        spans.append((start, n))
    return spans


def _group_markers(text: str, ms: list[Marker]) -> list[list[Marker]]:
    groups: list[list[Marker]] = []
    for m in ms:
        if groups and re.fullmatch(r"[\s,;]*", text[groups[-1][-1].end : m.start]):
            groups[-1].append(m)
        else:
            groups.append([m])
    return groups


# ----------------------------------------------------------------------------- markdown


def parse_markdown(text: str, *, inherit: bool = False) -> ParsedReport:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    starts = _line_starts(text)
    kinds: list[str] = []
    in_fence = False
    in_refs = False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            kinds.append("code")
            continue
        if in_fence:
            kinds.append("code")
            continue
        h = HEADING_RE.match(line)
        bold_heading = re.fullmatch(r"\s*(\*\*|__)(.+?)\1\s*:?\s*", line)
        heading_text = (h.group("text") if h else (bold_heading.group(2) if bold_heading else line.strip()))
        heading_text = heading_text.strip("*_ :")
        if (h or bold_heading or line.strip().endswith(":") or len(line.strip()) < 40) and REF_HEADINGS.match(
            heading_text
        ):
            in_refs = True
            kinds.append("heading")
            continue
        if h:
            in_refs = False
            kinds.append("heading")
            continue
        if REFDEF_RE.match(line):
            kinds.append("refdef")
            continue
        if in_refs:
            kinds.append("ref" if line.strip() else "blank")
            continue
        if not line.strip():
            kinds.append("blank")
        elif HR_RE.match(line):
            kinds.append("hr")
        elif line.lstrip().startswith("|"):
            kinds.append("table")
        else:
            kinds.append("body")

    citations: dict[str, Citation] = {}
    unnumbered: list[tuple[str | None, str | None, str]] = []
    for line, kind in zip(lines, kinds):
        if kind == "refdef":
            m = REFDEF_RE.match(line)
            assert m
            cid = m.group("id").strip()
            cid = str(int(cid)) if cid.isdigit() else cid
            url, title = _parse_ref_rest(m.group("rest"))
            citations[cid] = Citation(id=cid, url=url, title=title, raw=line.strip())
        elif kind == "ref":
            m = REFLIST_RE.match(line)
            if m:
                cid = str(int(m.group("a") or m.group("b")))
                url, title = _parse_ref_rest(m.group("rest"))
                citations[cid] = Citation(id=cid, url=url, title=title, raw=line.strip())
            else:
                url, title = _parse_ref_rest(BULLET_RE.sub("", line))
                if url or title:
                    unnumbered.append((url, title, line.strip()))
    if unnumbered and not any(c.isdigit() for c in citations):
        for n, (url, title, raw) in enumerate(unnumbered, 1):
            citations[str(n)] = Citation(id=str(n), url=url, title=title, raw=raw)

    # paragraphs: runs of body lines; each list item and table row is its own unit
    units: list[tuple[int, int]] = []
    cur: list[int] = []

    def flush() -> None:
        if cur:
            a = starts[cur[0]]
            b = starts[cur[-1]] + len(lines[cur[-1]])
            units.append((a, b))
            cur.clear()

    for li, (line, kind) in enumerate(zip(lines, kinds)):
        if kind == "body":
            if BULLET_RE.match(line) and cur:
                flush()
            cur.append(li)
        elif kind == "table":
            flush()
            if not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", line):
                units.append((starts[li], starts[li] + len(line)))
        else:
            flush()
    flush()

    claims: list[Claim] = []
    uncited: list[dict] = []
    link_ids: dict[str, str] = {}
    for ua, ub in units:
        para = text[ua:ub]
        markers = find_markers(para, citations, link_ids)
        sentences = split_sentences(para, markers)
        para_claims: list[tuple[int, int, list[str]]] = []
        pending_uncited: list[tuple[int, int]] = []
        for sa, sb in sentences:
            ms = [m for m in markers if m.start >= sa and m.end <= sb]
            pure = [m for m in ms if m.kind in _PURE]
            textlinks = [m for m in ms if m.kind == "textlink"]
            if not pure and not textlinks:
                pending_uncited.append((sa, sb))
                continue
            if inherit and pending_uncited and pure:
                ids = [i for m in _group_markers(para, pure)[0] for i in m.ids]
                for pa, pb in pending_uncited:
                    para_claims.append((pa, pb, ids + ["__inherited__"]))
            else:
                for pa, pb in pending_uncited:
                    _note_uncited(para[pa:pb], ua + pa, starts, uncited)
            pending_uncited = []
            groups = _group_markers(para, pure)
            if len(groups) <= 1:
                ids = [i for m in (groups[0] if groups else []) for i in m.ids]
                ids += [i for m in textlinks for i in m.ids]
                para_claims.append((sa, sb, ids))
                continue
            seg_start = sa
            segs: list[list] = []
            for g in groups:
                ids = [i for m in g for i in m.ids]
                segs.append([seg_start, g[-1].end, ids])
                seg_start = g[-1].end
            if re.search(r"\w{2,}", para[seg_start:sb]):
                segs[-1][1] = sb
            for tl in textlinks:
                for s in segs:
                    if s[0] <= tl.start < s[1]:
                        s[2] = s[2] + tl.ids
                        break
            merged: list[list] = []
            for s in segs:
                if merged and len(re.findall(r"\w+", _strip_markdown(para[s[0] : s[1]]))) < 3:
                    merged[-1][1] = s[1]
                    merged[-1][2] = merged[-1][2] + s[2]
                else:
                    merged.append(s)
            para_claims.extend((a, b, ids) for a, b, ids in merged)
        for pa, pb in pending_uncited:
            _note_uncited(para[pa:pb], ua + pa, starts, uncited)
        for a, b, ids in para_claims:
            inherited = "__inherited__" in ids
            ids = [i for i in dict.fromkeys(ids) if i != "__inherited__"]
            raw = para[a:b]
            clean = _strip_markdown(_cut_markers(para, a, b, markers))
            if raw.lstrip().startswith("|"):
                clean = "; ".join(c.strip() for c in clean.strip(" |").split("|") if c.strip())
            if len(re.findall(r"\w+", clean)) < 3:
                continue
            start = ua + a
            claims.append(
                Claim(
                    id=f"c{len(claims) + 1}",
                    text=clean,
                    start=start,
                    end=ua + b,
                    line=bisect.bisect_right(starts, start),
                    citation_ids=ids,
                    raw=raw.strip(),
                    inherited=inherited,
                )
            )
    return ParsedReport(text=text, claims=claims, citations=citations, uncited=uncited, format="markdown")


def _cut_markers(text: str, a: int, b: int, markers: list[Marker]) -> str:
    parts, last = [], a
    for m in markers:
        if m.kind in _PURE and a <= m.start and m.end <= b:
            parts.append(text[last : m.start])
            last = m.end
    parts.append(text[last:b])
    return "".join(parts)


def _note_uncited(sentence: str, offset: int, starts: list[int], out: list[dict]) -> None:
    clean = _strip_markdown(sentence)
    words = re.findall(r"\w+", clean)
    if len(words) >= 6 and re.search(r"\d", clean):
        out.append({"text": clean, "line": bisect.bisect_right(starts, offset), "start": offset})


# ----------------------------------------------------------------------------- HTML


class _HtmlToMarkdown(HTMLParser):
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "blockquote",
             "br", "ul", "ol", "table", "header", "footer", "main", "aside", "figcaption", "dd", "dt", "pre"}
    SKIP = {"script", "style", "noscript", "template", "svg", "head", "nav"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.href_stack: list[tuple[str | None, int]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag in self.BLOCK:
            self.out.append("\n\n" if tag not in ("br", "tr") else "\n")
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                self.out.append("#" * int(tag[1]) + " ")
            if tag == "li":
                self.out.append("- ")
        if tag == "a":
            self.href_stack.append((a.get("href"), len(self.out)))
            self.out.append("[")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "a" and self.href_stack:
            href, pos = self.href_stack.pop()
            if href and href.startswith(("http://", "https://")):
                self.out.append(f"]({href})")
            else:
                self.out[pos] = ""
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_markdown(html: str) -> str:
    p = _HtmlToMarkdown()
    p.feed(html)
    p.close()
    md = "".join(p.out)
    # <a href="u">[1]</a> becomes "[[1]](u)"; collapse to "[1](u)" so it parses as a numbered citation link
    md = re.sub(r"\[\[(\d{1,3})\]\](\((?:[^()\s]+|\([^()\s]*\))+\))", r"[\1]\2", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


# ----------------------------------------------------------------------------- JSON (API responses)


def parse_json_report(data: dict) -> ParsedReport:
    if isinstance(data, dict) and isinstance(data.get("content"), list) and (
        data.get("type") == "message" or data.get("role") == "assistant"
    ):
        return _parse_anthropic(data)
    if isinstance(data, dict) and isinstance(data.get("output"), list):
        return _parse_openai_responses(data)
    if isinstance(data, dict) and isinstance(data.get("choices"), list):
        return _parse_openai_chat(data)
    raise ValueError(
        "Unrecognized JSON. Supported: Anthropic Messages responses, OpenAI Responses or Chat Completions responses."
    )


def _parse_anthropic(data: dict) -> ParsedReport:
    text_parts: list[str] = []
    claims: list[Claim] = []
    citations: dict[str, Citation] = {}
    by_key: dict[tuple, str] = {}
    pos = 0
    for block in data["content"]:
        if block.get("type") != "text":
            continue
        t = block.get("text", "")
        start = pos
        text_parts.append(t)
        pos += len(t)
        cits = block.get("citations") or []
        if not cits or len(re.findall(r"\w+", t)) < 3:
            continue
        ids = []
        for c in cits:
            url = c.get("url")
            title = c.get("title") or c.get("document_title")
            cited = c.get("cited_text")
            key = (url, title, cited)
            cid = by_key.get(key)
            if cid is None:
                cid = f"A{len(by_key) + 1}"
                by_key[key] = cid
                citations[cid] = Citation(id=cid, url=url, title=title, cited_text=cited, raw=json.dumps(c)[:500])
            ids.append(cid)
        claims.append(
            Claim(
                id=f"c{len(claims) + 1}",
                text=_strip_markdown(t),
                start=start,
                end=start + len(t),
                line=1,
                citation_ids=list(dict.fromkeys(ids)),
                raw=t.strip(),
            )
        )
    full = "".join(text_parts)
    starts = _line_starts(full)
    for c in claims:
        c.line = bisect.bisect_right(starts, c.start)
    return ParsedReport(text=full, claims=claims, citations=citations, format="anthropic-json")


def _annotated_text(text: str, annotations: list[dict], base: int, citations: dict, by_url: dict) -> list[Claim]:
    markers: list[Marker] = []
    for a in annotations:
        if a.get("type") != "url_citation":
            continue
        inner = a.get("url_citation", a)
        url = inner.get("url")
        if not url:
            continue
        cid = by_url.get(url)
        if cid is None:
            cid = f"O{len(by_url) + 1}"
            by_url[url] = cid
            citations[cid] = Citation(id=cid, url=url, title=inner.get("title"), raw=url)
        s, e = int(inner.get("start_index", 0)), int(inner.get("end_index", 0))
        if e <= s:
            e = s
        markers.append(Marker(s, e, [cid], "annotation"))
    markers.sort(key=lambda m: m.start)
    sentences = split_sentences(text, markers)
    claims: list[Claim] = []
    for sa, sb in sentences:
        ids = [i for m in markers if (sa <= m.start < sb) or (m.start <= sa < m.end) for i in m.ids]
        if not ids:
            continue
        raw = text[sa:sb]
        clean_parts, last = [], sa
        for m in markers:
            if sa <= m.start < sb and m.end > m.start:
                clean_parts.append(text[last : m.start])
                last = min(m.end, sb)
        clean_parts.append(text[last:sb])
        clean = _strip_markdown("".join(clean_parts))
        if len(re.findall(r"\w+", clean)) < 3:
            continue
        claims.append(
            Claim(id="", text=clean, start=base + sa, end=base + sb, line=1, citation_ids=list(dict.fromkeys(ids)),
                  raw=raw.strip())
        )
    return claims


def _parse_openai_responses(data: dict) -> ParsedReport:
    texts: list[str] = []
    claims: list[Claim] = []
    citations: dict[str, Citation] = {}
    by_url: dict[str, str] = {}
    pos = 0
    for item in data["output"]:
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") not in ("output_text", "text"):
                continue
            t = part.get("text", "")
            claims += _annotated_text(t, part.get("annotations") or [], pos, citations, by_url)
            texts.append(t)
            pos += len(t) + 2
    return _finish_json(texts, claims, citations, "openai-responses-json")


def _parse_openai_chat(data: dict) -> ParsedReport:
    texts: list[str] = []
    claims: list[Claim] = []
    citations: dict[str, Citation] = {}
    by_url: dict[str, str] = {}
    pos = 0
    for choice in data["choices"][:1]:
        msg = choice.get("message", {})
        t = msg.get("content") or ""
        claims += _annotated_text(t, msg.get("annotations") or [], pos, citations, by_url)
        texts.append(t)
        pos += len(t) + 2
    return _finish_json(texts, claims, citations, "openai-chat-json")


def _finish_json(texts, claims, citations, fmt) -> ParsedReport:
    full = "\n\n".join(texts)
    starts = _line_starts(full)
    for n, c in enumerate(claims, 1):
        c.id = f"c{n}"
        c.line = bisect.bisect_right(starts, c.start)
    return ParsedReport(text=full, claims=claims, citations=citations, format=fmt)


# ----------------------------------------------------------------------------- entry point


def load_report(path: str, *, inherit: bool = False, stdin_text: str | None = None) -> ParsedReport:
    if path == "-":
        raw = stdin_text if stdin_text is not None else ""
        suffix = ""
    else:
        p = Path(path)
        raw = p.read_text(encoding="utf-8", errors="replace")
        suffix = p.suffix.lower()
    return parse_text(raw, suffix=suffix, inherit=inherit)


def parse_text(raw: str, *, suffix: str = "", inherit: bool = False) -> ParsedReport:
    stripped = raw.lstrip()
    if suffix == ".json" or (not suffix and stripped.startswith("{")):
        try:
            return parse_json_report(json.loads(raw))
        except json.JSONDecodeError:
            pass
    if suffix in (".html", ".htm") or (not suffix and re.match(r"(?is)<!doctype html|<html", stripped)):
        rep = parse_markdown(html_to_markdown(raw), inherit=inherit)
        rep.format = "html"
        return rep
    return parse_markdown(raw, inherit=inherit)
