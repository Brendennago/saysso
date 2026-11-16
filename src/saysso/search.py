"""Search providers for the repair loop. Bring your own key, or self-host SearXNG for free.

  brave     BRAVE_API_KEY          independent web index
  serper    SERPER_API_KEY         Google results, cheap
  tavily    TAVILY_API_KEY
  searxng   SEARXNG_URL            self-hosted metasearch, no key
  openalex  (free; OPENALEX_API_KEY optional)   scholarly works, open-access links
  crossref  (free)                 scholarly metadata, DOIs
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from .cache import Cache


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""


class SearchError(RuntimeError):
    pass


class Provider:
    name = "base"
    scholarly = False

    def __init__(self, client: httpx.AsyncClient, cache: Cache, email: str | None = None):
        self.client = client
        self.cache = cache
        self.email = email
        self.calls = 0

    async def search(self, query: str, n: int = 6) -> list[SearchHit]:
        key = Cache.key(self.name, query, n)
        hit = self.cache.get("search", key, max_age=3 * 24 * 3600)
        if hit is not None:
            return [SearchHit(**h) for h in hit]
        try:
            hits = await self._search(query, n)
        except httpx.HTTPError as e:
            raise SearchError(f"{self.name}: {type(e).__name__}") from e
        self.calls += 1
        self.cache.set("search", key, [h.__dict__ for h in hits])
        return hits

    async def _search(self, query: str, n: int) -> list[SearchHit]:
        raise NotImplementedError


class Brave(Provider):
    name = "brave"

    async def _search(self, query, n):
        r = await self.client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": n},
            headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"},
        )
        r.raise_for_status()
        res = (r.json().get("web") or {}).get("results") or []
        return [SearchHit(x.get("url", ""), x.get("title", ""), x.get("description", "")) for x in res if x.get("url")]


class Serper(Provider):
    name = "serper"

    async def _search(self, query, n):
        r = await self.client.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": n},
            headers={"X-API-KEY": os.environ["SERPER_API_KEY"]},
        )
        r.raise_for_status()
        res = r.json().get("organic") or []
        return [SearchHit(x.get("link", ""), x.get("title", ""), x.get("snippet", "")) for x in res if x.get("link")]


class Tavily(Provider):
    name = "tavily"

    async def _search(self, query, n):
        r = await self.client.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": n, "search_depth": "basic"},
            headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
        )
        r.raise_for_status()
        res = r.json().get("results") or []
        return [SearchHit(x.get("url", ""), x.get("title", ""), x.get("content", "")[:300]) for x in res if x.get("url")]


class SearXNG(Provider):
    name = "searxng"

    async def _search(self, query, n):
        base = os.environ["SEARXNG_URL"].rstrip("/")
        r = await self.client.get(f"{base}/search", params={"q": query, "format": "json"})
        r.raise_for_status()
        res = r.json().get("results") or []
        return [SearchHit(x.get("url", ""), x.get("title", ""), x.get("content", "")) for x in res[:n] if x.get("url")]


class OpenAlex(Provider):
    name = "openalex"
    scholarly = True

    async def _search(self, query, n):
        params = {"search": query, "per-page": n,
                  "select": "id,doi,display_name,publication_year,best_oa_location,primary_location"}
        if self.email:
            params["mailto"] = self.email
        if os.environ.get("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        r = await self.client.get("https://api.openalex.org/works", params=params)
        r.raise_for_status()
        hits = []
        for w in r.json().get("results") or []:
            oa = w.get("best_oa_location") or {}
            prim = w.get("primary_location") or {}
            url = oa.get("pdf_url") or oa.get("landing_page_url") or w.get("doi") or prim.get("landing_page_url")
            if url:
                hits.append(SearchHit(url, w.get("display_name") or "", str(w.get("publication_year") or "")))
        return hits


class Crossref(Provider):
    name = "crossref"
    scholarly = True

    async def _search(self, query, n):
        params = {"query.bibliographic": query, "rows": n, "select": "DOI,title,URL,issued"}
        if self.email:
            params["mailto"] = self.email
        r = await self.client.get("https://api.crossref.org/works", params=params)
        r.raise_for_status()
        items = (r.json().get("message") or {}).get("items") or []
        return [SearchHit(f"https://doi.org/{x['DOI']}", (x.get("title") or [""])[0]) for x in items if x.get("DOI")]


_REGISTRY = {"brave": (Brave, "BRAVE_API_KEY"), "serper": (Serper, "SERPER_API_KEY"),
             "tavily": (Tavily, "TAVILY_API_KEY"), "searxng": (SearXNG, "SEARXNG_URL"),
             "openalex": (OpenAlex, None), "crossref": (Crossref, None)}


def make_providers(names: list[str], client: httpx.AsyncClient, cache: Cache, email: str | None) -> list[Provider]:
    out: list[Provider] = []
    for name in names:
        name = name.strip().lower()
        if name not in _REGISTRY:
            raise SearchError(f"unknown search provider {name!r}; choose from {', '.join(_REGISTRY)}")
        cls, env = _REGISTRY[name]
        if env and not os.environ.get(env):
            raise SearchError(f"{name} needs {env} to be set")
        out.append(cls(client, cache, email))
    return out
