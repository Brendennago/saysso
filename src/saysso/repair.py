"""Find the real source for a claim that its citation does not support.

Modeled on how fact-checkers work (SIFT / lateral reading): trace the claim upstream first, then
search with several different query shapes, and stop for a stated reason instead of giving up silently.

Order of attempts (cheapest first):
  1. upstream   follow links on the cited page whose anchor text/context matches the claim (free)
  2. title      exact-title search for title-only references
  3. phrase     exact distinctive phrase from the claim (numbers, named entities)
  4. entities   entities + numbers
  5. keywords   key terms
  6. primary    site:.gov / site:.edu restricted queries for statistics-style claims
  7. scholarly  OpenAlex / Crossref for research-style claims
  8. model      optional: one small model call proposes 3 more queries

Every candidate is judged on its own, in its own context (never a pile of search results in one
prompt; accuracy drops as context fills with retrieval noise).

Stops when: enough supporting sources found | query budget spent | fetch budget spent |
last 3 searches surfaced no new domains | model budget spent.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

from .gates import extract_numbers
from .llm import BudgetExceeded, LLMError
from .models import Claim, Repair, SearchStep, SourceCheck, Verdict
from .normalize import STOPWORDS, content_terms
from .search import Provider, SearchError
from .validity import domain_class

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Context

log = logging.getLogger("saysso")

_NAV_ANCHORS = {"home", "about", "login", "log in", "sign in", "subscribe", "contact", "privacy", "terms",
                "cookie", "cookies", "share", "tweet", "facebook", "next", "previous", "menu", "skip to content",
                "edit", "help", "donate", "jobs", "careers", "advertise", "newsletter"}
_SKIP_HOSTS = ("facebook.com", "twitter.com", "x.com", "linkedin.com", "pinterest.com", "reddit.com",
               "instagram.com", "whatsapp.com", "t.me", "mailto:", "web.archive.org")
_STATS_HINT = re.compile(r"\b(percent|%|rate|population|gdp|unemployment|inflation|survey|census|statistic|"
                         r"per capita|deaths|cases|emissions|budget|spending|revenue|million|billion)\b", re.I)
_SCHOLAR_HINT = re.compile(r"\b(study|studies|researchers?|paper|trial|meta-analysis|journal|published in|"
                           r"peer[- ]reviewed|cohort|randomi[sz]ed|preprint|findings|experiment)\b", re.I)
_ENTITY_RE = re.compile(r"\b([A-Z][\w&.'-]*(?:\s+(?:of|the|and|for|de|du|von|van|la|le)?\s*[A-Z][\w&.'-]*)*)")

QUERY_SYSTEM = """You write web search queries that would find the ORIGINAL source of a factual claim
(official statistics, the study itself, the press release, the primary document). Return JSON:
{"queries": ["...", "...", "..."]}. Three short, different queries. No explanations."""
QUERY_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
    "additionalProperties": False,
}


def _domain(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def distinctive_phrase(claim: str, max_words: int = 8) -> str | None:
    words = claim.rstrip(".").split()
    if len(words) < 4:
        return None
    anchors = [i for i, w in enumerate(words) if re.search(r"\d", w) or (i > 0 and w[:1].isupper())]
    if not anchors:
        return None
    i = anchors[0]
    a = max(0, i - max_words // 2)
    phrase = " ".join(words[a : a + max_words]).strip(",;:()\"'")
    return phrase if len(phrase.split()) >= 4 else None


def entities(claim: str) -> list[str]:
    out = []
    for m in _ENTITY_RE.finditer(claim):
        e = m.group(1).strip(".,'")
        if m.start() == 0 and e.lower() in STOPWORDS:
            continue
        if len(e) > 2 and e.lower() not in STOPWORDS:
            out.append(e)
    return list(dict.fromkeys(out))


def keywords(claim: str, n: int = 8) -> list[str]:
    seen, out = set(), []
    for w in re.findall(r"[\w$%.,'-]+", claim):
        w2 = w.strip(".,'")
        lw = w2.lower()
        if not w2 or lw in STOPWORDS or (len(w2) < 3 and not w2[0].isdigit()) or lw in seen:
            continue
        seen.add(lw)
        out.append(w2)
    out.sort(key=lambda w: (not w[0].isdigit(), not w[0].isupper(), -len(w)))
    return out[:n]


def generate_queries(claim: str, title: str | None = None) -> list[tuple[str, str]]:
    q: list[tuple[str, str]] = []
    if title and len(title) > 12:
        q.append(("title", f'"{title.strip()[:150]}"'))
    p = distinctive_phrase(claim)
    if p:
        q.append(("phrase", f'"{p}"'))
    ents = entities(claim)
    nums = [n.raw for n in extract_numbers(claim)][:2]
    if ents or nums:
        q.append(("entities", " ".join(ents[:3] + nums)))
    kws = keywords(claim)
    if kws:
        q.append(("keywords", " ".join(kws)))
    if _STATS_HINT.search(claim) and kws:
        q.append(("primary", " ".join(kws[:6]) + " site:.gov"))
    if _SCHOLAR_HINT.search(claim) and kws:
        q.append(("scholarly", " ".join(kws[:8])))
        q.append(("primary", " ".join(kws[:6]) + " site:.edu"))
    seen, out = set(), []
    for s, query in q:
        if query.lower() not in seen:
            seen.add(query.lower())
            out.append((s, query))
    return out


def rank_links(claim: Claim, doc, limit: int = 4) -> list[str]:
    """Outbound links on a cited page, ranked by how well their anchor text and context match the claim."""
    terms = set(content_terms(claim.text))
    if not terms or not doc.links:
        return []
    base = doc.final_url or doc.url
    scored: dict[str, float] = {}
    for link in doc.links:
        href = urljoin(base, link.href)
        if not href.startswith(("http://", "https://")) or href.split("#")[0] == base.split("#")[0]:
            continue
        if any(s in href for s in _SKIP_HOSTS) or link.text.strip().lower() in _NAV_ANCHORS:
            continue
        ctx = doc.text[max(0, link.start - 200) : link.end + 200]
        cov = len(terms & set(content_terms(ctx))) / len(terms)
        bonus = 0.25 if domain_class(href) in ("government", "academic", "preprint") else 0.0
        score = cov + bonus
        if score >= 0.35:
            scored[href] = max(score, scored.get(href, 0.0))
    return [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:limit]]


async def find_support(ctx: "Context", claim: Claim, failed: list[SourceCheck], title: str | None) -> Repair:
    cfg = ctx.cfg
    rep = Repair(attempted=True)
    seen = {c.url for c in failed if c.url}
    supporting: list[SourceCheck] = []
    plan: deque = deque()

    for chk in failed:
        doc = ctx.docs.get(chk.url) if chk.url else None
        if doc is not None and doc.ok:
            for href in rank_links(claim, doc):
                plan.append(("upstream", href, None))

    web = [p for p in ctx.search if not p.scholarly]
    scholarly = [p for p in ctx.search if p.scholarly]
    queries = generate_queries(claim.text, title)
    if ctx.llm and cfg.llm_queries and (web or scholarly):
        try:
            data = await ctx.llm.complete_json(QUERY_SYSTEM, f"Claim: {claim.text}", "", QUERY_SCHEMA, 200)
            for qq in (data.get("queries") or [])[:3] if isinstance(data, dict) else []:
                if isinstance(qq, str) and qq.strip():
                    queries.append(("model", qq.strip()[:200]))
        except (BudgetExceeded, LLMError) as e:
            log.info("query generation skipped: %s", e)
    for i, (strategy, query) in enumerate(queries):
        pool = scholarly if strategy == "scholarly" and scholarly else web or scholarly
        if not pool:
            continue
        provider = pool[i % len(pool)]
        plan.append((strategy, query, provider))

    domains: set[str] = set()
    stale_searches = 0
    while plan:
        if len(supporting) >= cfg.need_sources:
            rep.stop_reason = f"found {len(supporting)} supporting source(s)"
            break
        if rep.fetches >= cfg.max_fetches_per_claim:
            rep.stop_reason = f"fetch budget spent ({cfg.max_fetches_per_claim} pages)"
            break
        strategy, target, provider = plan.popleft()
        if provider is None:
            urls = [target]
            rep.steps.append(SearchStep("upstream", target, "link", [target]))
        else:
            if rep.queries >= cfg.max_queries_per_claim:
                rep.stop_reason = f"query budget spent ({cfg.max_queries_per_claim} searches)"
                break
            try:
                hits = await provider.search(target, 6)
            except SearchError as e:
                rep.steps.append(SearchStep(strategy, target, provider.name, []))
                log.info("search failed: %s", e)
                continue
            rep.queries += 1
            urls = [h.url for h in hits]
            rep.steps.append(SearchStep(strategy, target, provider.name, urls[:6]))
            new = {_domain(u) for u in urls} - domains
            domains |= new
            stale_searches = 0 if new else stale_searches + 1
            if stale_searches >= 3:
                rep.stop_reason = "stopped: the last 3 searches surfaced no new domains"
                break
        for u in urls[:3]:
            if u in seen or rep.fetches >= cfg.max_fetches_per_claim:
                continue
            seen.add(u)
            doc = await ctx.fetcher.fetch(u)
            rep.fetches += 1
            if not doc.ok:
                continue
            ctx.docs.setdefault(doc.url, doc)
            try:
                check = await ctx.check_one(claim, doc, "repair")
            except BudgetExceeded as e:
                rep.stop_reason = str(e)
                plan.clear()
                break
            rep.candidates.append(check)
            if check.verdict == Verdict.SUPPORTED and check.confidence >= cfg.accept_threshold and (
                check.validity is None or check.validity.tier != "INVALID"
            ):
                supporting.append(check)
                # a secondary source that supports the claim: look one hop further for the primary
                if check.validity and check.validity.domain_class in ("news", "other", "reference", "ugc"):
                    for href in reversed(rank_links(claim, doc, limit=2)):
                        if href not in seen:
                            plan.appendleft(("upstream", href, None))
    else:
        if not rep.stop_reason:
            rep.stop_reason = "exhausted every strategy" if (ctx.search or rep.steps) else (
                "no search provider configured (set --search) and no matching links on the cited page")

    if supporting:
        rank = {"government": 5, "academic": 4, "preprint": 3, "news": 3, "other": 2, "reference": 1, "ugc": 0}
        rep.found = max(supporting, key=lambda c: (rank.get(c.validity.domain_class if c.validity else "other", 2),
                                                  c.confidence))
        if not rep.stop_reason:
            rep.stop_reason = f"found {len(supporting)} supporting source(s)"
    return rep
