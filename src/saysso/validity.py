"""Is the source itself any good? Separate from whether it supports the claim.

Mostly deterministic and free: domain class, preprint status, AI-answer-as-source (circular),
staleness against the claim's own dates, near-duplicate (syndicated) sources, and retraction status
via Crossref (which carries Retraction Watch data) unioned with OpenAlex. Disagreement is reported.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote, urlsplit

import httpx

from .cache import Cache
from .config import Config
from .extract import DOI_RE
from .models import SourceDoc, Validity

_GOV_SUFFIXES = (".gov", ".mil", ".gov.uk", ".gc.ca", ".gov.au", ".gouv.fr", ".gob.mx", ".gov.in", ".go.jp",
                 ".govt.nz", ".gov.br", ".gov.sg", ".gov.za", ".gv.at", ".admin.ch", ".bund.de", ".europa.eu",
                 ".int")
_GOV = {"who.int", "un.org", "oecd.org", "worldbank.org", "imf.org", "europa.eu", "bis.org", "wto.org",
        "ilo.org", "unesco.org", "unicef.org", "iea.org", "ipcc.ch", "data.gov", "census.gov", "nasa.gov",
        "esa.int", "ourworldindata.org", "canada.ca"}
_ACADEMIC = {
    "doi.org", "nature.com", "science.org", "sciencedirect.com", "springer.com", "wiley.com", "tandfonline.com",
    "sagepub.com", "plos.org", "cell.com", "thelancet.com", "nejm.org", "jamanetwork.com", "bmj.com", "pnas.org",
    "acm.org", "ieee.org", "jstor.org", "oup.com", "cambridge.org", "mdpi.com", "frontiersin.org",
    "europepmc.org", "semanticscholar.org", "openreview.net", "aclanthology.org", "mlr.press", "neurips.cc",
    "royalsocietypublishing.org", "annualreviews.org", "aps.org", "iop.org", "acs.org", "rsc.org",
    "biomedcentral.com", "elifesciences.org", "peerj.com", "nber.org", "cochranelibrary.com", "aeaweb.org",
    "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "scielo.org", "karger.com", "degruyter.com",
}
_PREPRINT = {"arxiv.org", "biorxiv.org", "medrxiv.org", "ssrn.com", "researchsquare.com", "preprints.org",
             "osf.io", "chemrxiv.org", "psyarxiv.com", "engrxiv.org", "techrxiv.org", "authorea.com",
             "zenodo.org", "eartharxiv.org", "socarxiv.org"}
_REFERENCE = {"wikipedia.org", "britannica.com", "wikiwand.com", "wikimedia.org", "encyclopedia.com",
              "investopedia.com", "dictionary.com", "merriam-webster.com", "scholarpedia.org"}
_NEWS = {"reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com", "wsj.com", "ft.com",
         "theguardian.com", "washingtonpost.com", "bloomberg.com", "cnbc.com", "cnn.com", "npr.org",
         "economist.com", "axios.com", "theverge.com", "techcrunch.com", "arstechnica.com", "wired.com",
         "politico.com", "aljazeera.com", "nbcnews.com", "cbsnews.com", "abcnews.go.com", "latimes.com",
         "usatoday.com", "theatlantic.com", "newyorker.com", "forbes.com", "businessinsider.com", "vox.com",
         "time.com", "statnews.com", "propublica.org", "semafor.com",
         "lemonde.fr", "spiegel.de", "nikkei.com", "scmp.com", "theglobeandmail.com", "abc.net.au"}
_UGC = {"reddit.com", "x.com", "twitter.com", "facebook.com", "instagram.com", "tiktok.com", "youtube.com",
        "youtu.be", "quora.com", "medium.com", "substack.com", "linkedin.com", "threads.net", "bsky.app",
        "tumblr.com", "pinterest.com", "stackexchange.com", "stackoverflow.com", "news.ycombinator.com",
        "discord.com", "t.me", "telegram.me", "github.com", "gist.github.com", "blogspot.com", "wordpress.com",
        "fandom.com"}
_AI_SHARE = (("chatgpt.com", "/share"), ("chat.openai.com", "/share"), ("perplexity.ai", "/"),
             ("gemini.google.com", "/share"), ("g.co", "/gemini"), ("claude.ai", "/share"), ("poe.com", "/"),
             ("copilot.microsoft.com", "/shares"), ("grok.com", "/share"), ("you.com", "/search"),
             ("phind.com", "/"), ("chat.deepseek.com", "/"))

_BASE = {"government": 0.95, "academic": 0.9, "news": 0.75, "preprint": 0.7, "other": 0.65, "reference": 0.6,
         "ugc": 0.35, "ai-generated": 0.05}


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def domain_class(url: str) -> str:
    host = _host(url)
    path = urlsplit(url).path or "/"
    for d, prefix in _AI_SHARE:
        if (host == d or host.endswith("." + d)) and path.startswith(prefix):
            return "ai-generated"
    if host.endswith(_GOV_SUFFIXES) or host in {x.lstrip('.') for x in _GOV_SUFFIXES} or _in(host, _GOV):
        return "government"
    if _in(host, _PREPRINT):
        return "preprint"
    if _in(host, _ACADEMIC) or host.endswith((".edu", ".ac.uk", ".ac.jp", ".edu.au", ".ac.nz")):
        return "academic"
    if _in(host, _REFERENCE):
        return "reference"
    if _in(host, _NEWS):
        return "news"
    if _in(host, _UGC):
        return "ugc"
    return "other"


def doi_of(doc: SourceDoc) -> str | None:
    if doc.doi:
        return doc.doi.lower()
    for u in (doc.final_url or "", doc.url):
        m = DOI_RE.search(u)
        if m and ("doi.org" in u or "/doi/" in u or "/10." in u):
            return m.group(1).rstrip(".").lower()
    return None


def _tier(score: float) -> str:
    if score <= 0.2:
        return "INVALID"
    if score >= 0.8:
        return "HIGH"
    if score >= 0.6:
        return "MEDIUM"
    return "LOW"


def base_validity(doc: SourceDoc) -> Validity:
    cls = domain_class(doc.final_url or doc.url)
    score = _BASE[cls]
    flags: list[str] = []
    if cls == "ai-generated":
        flags.append("an AI chat answer is cited as a source (circular)")
    elif cls == "preprint":
        flags.append("preprint: not peer reviewed")
    elif cls == "reference":
        flags.append("tertiary source: prefer the primary source it cites")
    elif cls == "ugc":
        flags.append("user-generated content")
    if doc.archived_from:
        flags.append("archived copy used (original unreachable)")
    return Validity(tier=_tier(score), score=score, domain_class=cls, flags=flags, doi=doi_of(doc),
                    published=doc.published)


class RetractionChecker:
    def __init__(self, cfg: Config, cache: Cache, client: httpx.AsyncClient):
        self.cfg = cfg
        self.cache = cache
        self.client = client
        self.sem = asyncio.Semaphore(4)
        self.lookups = 0

    async def status(self, doi: str) -> dict:
        key = Cache.key("retraction", doi)
        hit = self.cache.get("retraction", key, max_age=7 * 24 * 3600)
        if hit is not None:
            return hit
        async with self.sem:
            cr, oa = await asyncio.gather(self._crossref(doi), self._openalex(doi))
        result = {"crossref": cr, "openalex": oa}
        self.cache.set("retraction", key, result)
        return result

    async def _crossref(self, doi: str) -> dict | None:
        params = {"mailto": self.cfg.email} if self.cfg.email else None
        try:
            r = await self.client.get(f"https://api.crossref.org/works/{quote(doi, safe='/')}", params=params,
                                      timeout=self.cfg.timeout)
            self.lookups += 1
            if r.status_code != 200:
                return None
            return crossref_status(r.json().get("message", {}))
        except (httpx.HTTPError, ValueError):
            return None

    async def _openalex(self, doi: str) -> dict | None:
        params = {"mailto": self.cfg.email} if self.cfg.email else {}
        try:
            r = await self.client.get(f"https://api.openalex.org/works/doi:{quote(doi, safe='/')}",
                                      params=params, timeout=self.cfg.timeout)
            self.lookups += 1
            if r.status_code != 200:
                return None
            d = r.json()
            return {"retracted": bool(d.get("is_retracted")), "type": d.get("type")}
        except (httpx.HTTPError, ValueError):
            return None


def crossref_status(msg: dict) -> dict:
    """Read retraction / expression-of-concern / correction notices from a Crossref works record."""
    kinds: list[str] = []
    for upd in msg.get("updated-by") or []:
        t = str(upd.get("type", "")).lower().replace("-", "_")
        if t:
            kinds.append(t)
    for rel_type in (msg.get("relation") or {}):
        if "retract" in rel_type.lower():
            kinds.append("retraction")
    retracted = any(k in ("retraction", "withdrawal", "removal") for k in kinds)
    concern = any("concern" in k for k in kinds)
    return {
        "retracted": retracted,
        "concern": concern,
        "corrected": any(k in ("correction", "erratum", "corrigendum") for k in kinds),
        "is_notice": str(msg.get("type", "")).lower() in ("retraction",) or bool(msg.get("update-to")),
        "type": msg.get("type"),
    }


def apply_retraction(v: Validity, status: dict) -> Validity:
    cr, oa = status.get("crossref"), status.get("openalex")
    cr_r = bool(cr and cr.get("retracted"))
    oa_r = bool(oa and oa.get("retracted"))
    if cr and cr.get("type") == "posted-content" and "preprint: not peer reviewed" not in v.flags:
        v.flags.append("preprint: not peer reviewed")
        v.score = min(v.score, 0.7)
    if cr_r or oa_r:
        v.retraction = "retracted"
        who = "Crossref/Retraction Watch and OpenAlex" if cr_r and oa_r else ("Crossref/Retraction Watch" if cr_r else "OpenAlex only")
        v.flags.append(f"retracted ({who})")
        if cr is not None and oa is not None and cr_r != oa_r:
            v.flags.append("retraction databases disagree; check the publisher's page")
        v.score = 0.0
    elif cr and cr.get("concern"):
        v.retraction = "expression of concern"
        v.flags.append("expression of concern issued")
        v.score = min(v.score, 0.45)
    elif cr and cr.get("corrected"):
        v.flags.append("a correction was issued")
    v.tier = _tier(v.score)
    return v


def stale_flag(v: Validity, claim_years_: list[int]) -> str | None:
    if not v.published or not claim_years_:
        return None
    try:
        pub_year = int(v.published[:4])
    except ValueError:
        return None
    latest = max(claim_years_)
    if latest > pub_year:
        return f"source published {pub_year}, but the claim refers to {latest}"
    return None


def shingles(text: str, k: int = 8, cap: int = 4000) -> set[int]:
    words = re.findall(r"\w+", text.lower())
    return {hash(" ".join(words[i : i + k])) for i in range(0, min(len(words) - k + 1, cap))}


def duplicate_clusters(docs: list[SourceDoc], threshold: float = 0.6) -> dict[str, str]:
    """Map url -> representative url for near-duplicate (syndicated) sources."""
    sigs = [(d.url, shingles(d.text)) for d in docs if d.ok and len(d.text) > 800]
    rep: dict[str, str] = {}
    for i, (u, s) in enumerate(sigs):
        if u in rep or not s:
            continue
        for v, t in sigs[i + 1 :]:
            if v in rep or not t:
                continue
            j = len(s & t) / len(s | t)
            if j >= threshold:
                rep[v] = u
    return rep
