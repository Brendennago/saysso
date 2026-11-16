"""Orchestration: parsed report in, RunReport out."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone

import httpx

from . import __version__
from .cache import Cache
from .config import Config
from .fetch import Fetcher
from .gates import Num, claim_years, extract_numbers, number_gate
from .judge import Item, Judge
from .llm import Budget, LLMError, MiniCheckNLI, make_llm
from .models import ClaimResult, Claim, RunReport, SourceCheck, SourceDoc, Validity, Verdict
from .normalize import NormText
from .parse import ParsedReport
from .repair import find_support
from .retrieve import Index
from .search import make_providers
from .validity import RetractionChecker, apply_retraction, base_validity, duplicate_clusters, stale_flag

log = logging.getLogger("saysso")

_ORDER = [Verdict.SUPPORTED, Verdict.PARTIAL, Verdict.CONTRADICTED, Verdict.UNSUPPORTED, Verdict.UNCERTAIN,
          Verdict.UNREACHABLE]


class Context:
    def __init__(self, cfg: Config, cache: Cache, client: httpx.AsyncClient):
        self.cfg = cfg
        self.cache = cache
        self.client = client
        self.fetcher = Fetcher(cfg, cache, client)
        self.budget = Budget(cfg.max_llm_calls, cfg.max_cost_usd, cfg.price_in, cfg.price_out)
        self.llm = make_llm(cfg.model, self.budget, cache, client)
        self.recheck = make_llm(cfg.recheck_model, self.budget, cache, client)
        self.nli = MiniCheckNLI(cfg.nli, cache, client) if cfg.nli else None
        self.search = make_providers(cfg.search, client, cache, cfg.email) if cfg.search else []
        self.judge = Judge(cfg, self.llm, self.recheck, self.nli)
        self.retractions = RetractionChecker(cfg, cache, client)
        self.docs: dict[str, SourceDoc] = {}
        self._index: dict[str, Index] = {}
        self._norm: dict[str, NormText] = {}
        self._nums: dict[str, list[Num]] = {}
        self._validity: dict[str, Validity] = {}
        self._vlock: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def index(self, doc: SourceDoc) -> Index:
        if doc.url not in self._index:
            self._index[doc.url] = Index(doc.text)
        return self._index[doc.url]

    def norm(self, doc: SourceDoc) -> NormText:
        if doc.url not in self._norm:
            self._norm[doc.url] = NormText(doc.text)
        return self._norm[doc.url]

    def numbers(self, doc: SourceDoc) -> list[Num]:
        if doc.url not in self._nums:
            self._nums[doc.url] = extract_numbers(doc.text)
        return self._nums[doc.url]

    def item(self, claim: Claim, doc: SourceDoc, cid: str) -> Item:
        boost = tuple(n.raw.replace(",", "").rstrip("%") for n in extract_numbers(claim.text))
        passages = self.index(doc).search(claim.text, k=self.cfg.top_k, boost_terms=boost)
        gate = number_gate(claim.text, " ".join(p.text for p in passages), self.numbers(doc), doc.text)
        return Item(claim, cid, passages, gate)

    async def validity_for(self, doc: SourceDoc) -> Validity:
        async with self._vlock[doc.url]:
            if doc.url not in self._validity:
                v = base_validity(doc)
                if v.doi and self.cfg.retraction_check:
                    try:
                        v = apply_retraction(v, await self.retractions.status(v.doi))
                    except Exception as e:  # never let a metadata lookup kill a run
                        log.info("retraction lookup failed for %s: %s", v.doi, e)
                self._validity[doc.url] = v
            return self._validity[doc.url]

    async def attach_validity(self, chk: SourceCheck, doc: SourceDoc, claim: Claim) -> SourceCheck:
        v = replace(await self.validity_for(doc))
        v.flags = list(v.flags)
        flag = stale_flag(v, claim_years(claim.text))
        if flag:
            v.flags.append(flag)
        chk.validity = v
        return chk

    async def check_one(self, claim: Claim, doc: SourceDoc, cid: str) -> SourceCheck:
        """Judge a single claim against a single document (used by the repair loop)."""
        [chk] = await self.judge.judge(doc, self.norm(doc), [self.item(claim, doc, cid)])
        if self.judge.budget_hit and chk.method != "llm" and chk.verdict == Verdict.UNCERTAIN:
            from .llm import BudgetExceeded

            raise BudgetExceeded(self.judge.budget_hit)
        return await self.attach_validity(chk, doc, claim)


def _unreachable(cid: str, url: str | None, reason: str, method: str = "fetch") -> SourceCheck:
    return SourceCheck(citation_id=cid, url=url, verdict=Verdict.UNREACHABLE, confidence=1.0, method=method,
                       reason=reason)


def combine(checks: list[SourceCheck]) -> tuple[Verdict, float, list[str]]:
    notes: list[str] = []
    if not checks:
        return Verdict.UNREACHABLE, 1.0, ["no citation could be resolved"]
    by = defaultdict(list)
    for c in checks:
        by[c.verdict].append(c)
    for v in _ORDER:
        if by[v]:
            best = max(by[v], key=lambda c: c.confidence)
            if v == Verdict.SUPPORTED and by[Verdict.CONTRADICTED]:
                ids = ", ".join(f"[{c.citation_id}]" for c in by[Verdict.CONTRADICTED])
                notes.append(f"conflict: {ids} contradicts this claim")
            return v, best.confidence, notes
    return Verdict.UNCERTAIN, 0.4, notes


async def run(parsed: ParsedReport, cfg: Config, input_name: str, progress=None) -> RunReport:
    t0 = time.monotonic()
    say = progress or (lambda msg: None)
    cache = Cache(cfg.cache_dir, enabled=not cfg.no_cache)
    async with httpx.AsyncClient(
        timeout=cfg.timeout,
        headers={"User-Agent": cfg.user_agent},
        follow_redirects=False,
    ) as client:
        ctx = Context(cfg, cache, client)
        cits = parsed.citations

        # 1. resolve title-only references by search (if a provider is configured)
        if ctx.search:
            for c in cits.values():
                if not c.url and c.title and not c.cited_text:
                    try:
                        hits = await ctx.search[0].search(f'"{c.title[:150]}"', 3)
                    except Exception as e:
                        log.info("title search failed: %s", e)
                        continue
                    if hits:
                        c.url = hits[0].url
                        c.raw += "  [url resolved by title search]"

        # 2. fetch every cited URL once, concurrently
        urls = list(dict.fromkeys(
            cits[cid].url for cl in parsed.claims for cid in cl.citation_ids if cid in cits and cits[cid].url
        ))
        say(f"fetching {len(urls)} source(s)")
        fetched = await asyncio.gather(*(ctx.fetcher.fetch(u) for u in urls))
        docs = [replace(d, url=u) for u, d in zip(urls, fetched)]  # key by the URL as cited
        for d in docs:
            ctx.docs[d.url] = d

        # template pages: different URLs, identical short text -> not real content
        by_hash = defaultdict(list)
        for d in docs:
            if d.ok and d.sha256 and len(d.text) < 5000:
                by_hash[d.sha256].append(d)
        for group in by_hash.values():
            if len(group) > 1:
                for d in group:
                    d.ok = False
                    d.reason = "identical text served for several different URLs (template or error page)"

        # pseudo-documents for citations that carry their own quoted text but no URL (Claude API documents)
        for c in cits.values():
            if not c.url and c.cited_text:
                key = f"cited-text:{c.id}"
                ctx.docs[key] = SourceDoc(url=key, ok=True, text=c.cited_text, title=c.title,
                                          reason="judged against the generator's cited_text only")

        dups = duplicate_clusters([d for d in ctx.docs.values()])

        # 3. build work items grouped by source
        per_claim: dict[str, list[SourceCheck]] = defaultdict(list)
        work: dict[str, list[Item]] = defaultdict(list)
        for cl in parsed.claims:
            for cid in cl.citation_ids:
                c = cits.get(cid)
                if c is None:
                    per_claim[cl.id].append(_unreachable(cid, None, f"no reference entry for [{cid}]", "parse"))
                    continue
                key = c.url or (f"cited-text:{c.id}" if c.cited_text else None)
                if key is None:
                    why = f"reference has no URL (title only: {c.title})" if c.title else "reference has no URL"
                    per_claim[cl.id].append(_unreachable(cid, None, why, "parse"))
                    continue
                d = ctx.docs[key]
                if not d.ok:
                    per_claim[cl.id].append(_unreachable(cid, c.url, d.reason or "unreachable"))
                    continue
                work[key].append(ctx.item(cl, d, cid))

        # 4. judge, one task per source
        say(f"checking {sum(len(v) for v in work.values())} claim-citation pair(s)")
        claims_by_id = {cl.id: cl for cl in parsed.claims}
        sem = asyncio.Semaphore(4)

        async def judge_source(key: str, items: list[Item]) -> None:
            d = ctx.docs[key]
            async with sem:
                checks = await ctx.judge.judge(d, ctx.norm(d), items)
            for it, chk in zip(items, checks):
                await ctx.attach_validity(chk, d, it.claim)
                if key in dups:
                    chk.validity.flags.append(f"near-duplicate of another cited source ({dups[key]})")
                c = cits.get(it.citation_id)
                if c and c.cited_text and not key.startswith("cited-text:"):
                    from .locate import locate

                    found = locate(c.cited_text, ctx.norm(d)) is not None
                    chk.notes.append("the generator's quoted text was found in the page" if found
                                     else "the generator's quoted text was NOT found in the page")
                per_claim[it.claim.id].append(chk)

        await asyncio.gather(*(judge_source(k, v) for k, v in work.items()))

        # 5. combine per claim
        results: list[ClaimResult] = []
        for cl in parsed.claims:
            checks = sorted(per_claim[cl.id], key=lambda c: cl.citation_ids.index(c.citation_id)
                            if c.citation_id in cl.citation_ids else 99)
            verdict, conf, notes = combine(checks)
            if cl.inherited:
                notes.append("citation inherited from a later sentence in the same paragraph")
            results.append(ClaimResult(claim=cl, verdict=verdict, confidence=conf, checks=checks, notes=notes))

        # 6. repair loop: persistent search for real sources
        if cfg.fix:
            targets = [r for r in results
                       if r.verdict in (Verdict.UNSUPPORTED, Verdict.CONTRADICTED, Verdict.UNREACHABLE,
                                        Verdict.PARTIAL)][: cfg.max_repair_claims]
            say(f"searching for real sources for {len(targets)} claim(s)")
            rsem = asyncio.Semaphore(2)

            async def repair(r: ClaimResult) -> None:
                title = next((cits[c.citation_id].title for c in r.checks
                              if c.citation_id in cits and cits[c.citation_id].title), None)
                async with rsem:
                    r.repair = await find_support(ctx, r.claim, r.checks, title)
                if r.repair.found:
                    if r.verdict == Verdict.UNSUPPORTED:
                        r.verdict = Verdict.WRONG_SOURCE
                    r.notes.append(f"support found at {r.repair.found.url}")
                    if r.verdict == Verdict.CONTRADICTED:
                        r.notes.append("another source supports this claim while the cited source contradicts it")

            await asyncio.gather(*(repair(r) for r in targets))

        # 7. pass/fail per claim: supported AND by a source that is not invalid (e.g. retracted)
        for r in results:
            sup = [c for c in r.checks if c.verdict == Verdict.SUPPORTED]
            r.passes = bool(sup) and r.verdict == Verdict.SUPPORTED and any(
                (c.validity is None or c.validity.tier != "INVALID") for c in sup)
            if r.verdict == Verdict.SUPPORTED and not r.passes:
                r.notes.append("supported only by an invalid source (e.g. retracted)")

        await ctx.fetcher.aclose()

    stats = compute_stats(results, ctx.docs, parsed)
    stats["elapsed_s"] = round(time.monotonic() - t0, 2)
    usage = ctx.budget.as_dict()
    usage.update({
        "model": ctx.llm.name if ctx.llm else None,
        "live_fetches": ctx.fetcher.live_fetches,
        "searches": sum(p.calls for p in ctx.search),
        "nli_calls": ctx.nli.calls if ctx.nli else 0,
        "metadata_lookups": ctx.retractions.lookups,
        "budget_note": ctx.judge.budget_hit,
    })
    cache.close()
    sources = {}
    for k, d in ctx.docs.items():
        s = d.summary()
        if k in ctx._validity:
            from .models import to_jsonable

            s["validity"] = to_jsonable(ctx._validity[k])
        sources[k] = s
    return RunReport(
        input=input_name,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        version=__version__,
        claims=results,
        sources=sources,
        stats=stats,
        usage=usage,
        config=cfg.public(),
        uncited=parsed.uncited,
    )


def compute_stats(results: list[ClaimResult], docs: dict[str, SourceDoc], parsed: ParsedReport) -> dict:
    counts = {v.value: 0 for v in Verdict}
    for r in results:
        counts[r.verdict.value] += 1
    judged = len(results) - counts[Verdict.UNREACHABLE.value] - counts[Verdict.UNCERTAIN.value]
    passing = sum(1 for r in results if r.passes)
    score = (passing + 0.5 * counts[Verdict.PARTIAL.value]) / judged if judged else None
    real = [d for k, d in docs.items() if not k.startswith("cited-text:")]
    return {
        "claims": len(results),
        "counts": counts,
        "passing": passing,
        "judged": judged,
        "faithfulness": round(score, 4) if score is not None else None,
        "sources": len(real),
        "sources_unreachable": sum(1 for d in real if not d.ok),
        "uncited_factual_sentences": len(parsed.uncited),
        "format": parsed.format,
    }


def check_sync(parsed: ParsedReport, cfg: Config, input_name: str = "-", progress=None) -> RunReport:
    return asyncio.run(run(parsed, cfg, input_name, progress))


__all__ = ["run", "check_sync", "combine", "compute_stats", "Context", "LLMError"]
