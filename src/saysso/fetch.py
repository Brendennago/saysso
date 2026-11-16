"""Fetch cited sources safely and honestly.

- Blocked, paywalled, empty or JS-only pages become SOURCE-UNREACHABLE, never "unsupported".
- An SSRF guard refuses private and loopback addresses unless explicitly allowed.
- Dead links fall back to the Wayback Machine (raw snapshot, no toolbar), and that is recorded.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx

from .cache import Cache
from .config import Config
from .extract import MissingDependency, html_extract, pdf_extract
from .models import SourceDoc

_BLOCK_RE = re.compile(
    r"(just a moment\.\.\.|attention required|enable javascript and cookies|verify (that )?you are (a )?human|"
    r"access denied|are you a robot|unusual traffic|captcha|checking your browser|request blocked|"
    r"please enable javascript|javascript is (disabled|required))",
    re.I,
)
_PAYWALL_RE = re.compile(
    r"(subscribe to (continue|read)|to continue reading|subscribers only|already a subscriber|"
    r"sign in to (continue|read)|create a free account to (continue|read)|this content is (only )?available to)",
    re.I,
)
_MIN_TEXT = 200
SOURCE_TTL = 7 * 24 * 3600
FAILED_TTL = 6 * 3600


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = "&".join(
        q for q in parts.query.split("&") if q and not q.lower().startswith(("utm_", "fbclid=", "gclid=", "ref_src="))
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))


class Fetcher:
    def __init__(self, cfg: Config, cache: Cache, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.cache = cache
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=cfg.timeout,
            headers={
                "User-Agent": cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,text/plain;q=0.8,*/*;q=0.5",
                "Accept-Language": "en;q=0.9,*;q=0.5",
            },
            follow_redirects=False,
        )
        self.sem = asyncio.Semaphore(cfg.concurrency)
        self.live_fetches = 0
        self._inflight: dict[str, asyncio.Task] = {}

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    async def fetch(self, url: str) -> SourceDoc:
        """Fetch once per run per canonical URL; concurrent callers share the same task."""
        key = canonical_url(url)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch_cached(url, key))
            self._inflight[key] = task
        return await task

    async def _fetch_cached(self, url: str, key: str) -> SourceDoc:
        cached = self.cache.get("src", key, max_age=SOURCE_TTL)
        if cached and (cached.get("ok") or self.cache.get("src", key, max_age=FAILED_TTL)):
            doc = SourceDoc.from_dict(cached)
            doc.from_cache = True
            return doc
        async with self.sem:
            doc = await self._fetch_live(url)
        if not doc.ok and self.cfg.wayback and _worth_archive(doc):
            async with self.sem:
                archived = await self._wayback(url)
            if archived is not None and archived.ok:
                archived.archived_from = archived.final_url
                archived.url = url
                archived.reason = f"original unreachable ({doc.reason}); using Wayback Machine snapshot"
                doc = archived
        self.cache.set("src", key, doc.to_dict())
        return doc

    async def _guard(self, url: str) -> str | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return f"unsupported URL scheme: {parts.scheme or 'none'}"
        host = parts.hostname
        if not host:
            return "URL has no host"
        if self.cfg.allow_private:
            return None
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
        except OSError:
            if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("ALL_PROXY"):
                return None  # the proxy resolves names for us
            return "DNS lookup failed"
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return "blocked: resolves to a private or internal address (SSRF guard; use --allow-private to override)"
        return None

    async def _fetch_live(self, url: str) -> SourceDoc:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        current = url
        status, ctype, body, charset, reason = None, "", b"", None, None
        for _hop in range(6):
            err = await self._guard(current)
            if err:
                return SourceDoc(url=url, final_url=current, ok=False, reason=err, fetched_at=now)
            try:
                async with self.client.stream("GET", current) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        current = urljoin(current, r.headers["location"])
                        continue
                    status = r.status_code
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    charset = r.charset_encoding
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > self.cfg.max_bytes:
                            reason = "truncated: source larger than max size"
                            break
                    body = bytes(buf)
            except httpx.TimeoutException:
                return SourceDoc(url=url, final_url=current, ok=False, reason="timed out", fetched_at=now)
            except httpx.HTTPError as e:
                return SourceDoc(url=url, final_url=current, ok=False, reason=f"network error ({type(e).__name__})", fetched_at=now)
            break
        else:
            return SourceDoc(url=url, final_url=current, ok=False, reason="too many redirects", fetched_at=now)
        self.live_fetches += 1
        return build_doc(url, current, status, ctype, body, charset, fetched_at=now, note=reason)

    async def _wayback(self, url: str) -> SourceDoc | None:
        api = f"https://archive.org/wayback/available?url={quote(url, safe='')}"
        try:
            r = await self.client.get(api, timeout=self.cfg.timeout)
            snap = r.json().get("archived_snapshots", {}).get("closest") or {}
        except (httpx.HTTPError, ValueError):
            return None
        if not snap.get("available") or not snap.get("url"):
            return None
        m = re.match(r"https?://web\.archive\.org/web/(\d+)/(.*)", snap["url"])
        if not m:
            return None
        raw_url = f"https://web.archive.org/web/{m.group(1)}id_/{m.group(2)}"
        return await self._fetch_live(raw_url)


def _worth_archive(doc: SourceDoc) -> bool:
    r = (doc.reason or "").lower()
    if r.startswith(("blocked: resolves", "unsupported url")):
        return False
    return True


def build_doc(
    url: str,
    final_url: str,
    status: int | None,
    ctype: str,
    body: bytes,
    charset: str | None,
    *,
    fetched_at: str | None = None,
    note: str | None = None,
) -> SourceDoc:
    doc = SourceDoc(url=url, final_url=final_url, status=status, content_type=ctype or None, fetched_at=fetched_at)
    if status is not None and status >= 400:
        doc.reason = f"HTTP {status}"
        return doc
    is_pdf = ctype == "application/pdf" or body[:5] == b"%PDF-"
    try:
        if is_pdf:
            ex = pdf_extract(body)
            doc.content_type = "application/pdf"
        elif "html" in ctype or "xml" in ctype or body.lstrip()[:1] == b"<":
            ex = html_extract(_decode(body, charset))
        else:
            from .extract import Extracted

            ex = Extracted(text=_decode(body, charset))
    except MissingDependency as e:
        doc.reason = str(e)
        return doc
    except Exception as e:  # corrupt PDF, etc.
        doc.reason = f"could not extract text ({type(e).__name__})"
        return doc
    doc.text = ex.text
    doc.title = ex.title
    doc.links = ex.links
    doc.published = ex.published
    doc.doi = ex.doi
    doc.meta = {k: v for k, v in ex.meta.items() if k.startswith(("citation_", "dc.", "og:", "article:", "prism."))}
    doc.sha256 = hashlib.sha256(doc.text.encode("utf-8")).hexdigest()
    head = (doc.title or "") + " " + doc.text[:3000]
    if _BLOCK_RE.search(head) and len(doc.text) < 5000:
        doc.reason = "blocked by bot protection"
    elif len(doc.text) < _MIN_TEXT:
        doc.reason = "page has almost no text (blocked, JavaScript-only, or empty)"
    elif _PAYWALL_RE.search(doc.text[:6000]) and len(doc.text) < 4000:
        doc.reason = "paywalled: only teaser text available"
    else:
        doc.ok = True
        doc.reason = note
    return doc


def _decode(body: bytes, charset: str | None) -> str:
    if not charset:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
        charset = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
