"""MCP server: lets an agent audit citations before it hands an answer to a person.

Tools return compact results (only the claims that need attention) to keep the host agent's context small.
stdout is the protocol channel in stdio mode, so nothing here prints to it.
"""

from __future__ import annotations

import os
from typing import Any

from .config import Config
from .models import Verdict, to_jsonable
from .parse import ParsedReport, parse_text

INSTRUCTIONS = (
    "saysso checks whether cited sources actually support the claims they are attached to. "
    "Call verify_report on any research answer or report that cites web sources BEFORE presenting it as "
    "verified, and fix or remove claims it flags. Use check_claim for a single claim and URL, and "
    "locate_quote to confirm an exact quote exists in a page. Never say a citation is verified without a "
    "tool result that says so."
)


def compact(rep) -> dict[str, Any]:
    """Summary + only the claims that do not pass. Keeps tool output small."""
    issues = []
    for r in rep.claims:
        if r.passes:
            continue
        chk = next((c for c in r.checks if c.evidence and c.evidence.kind != "closest"), None) or (
            r.checks[0] if r.checks else None)
        ev = chk.evidence if chk else None
        item: dict[str, Any] = {
            "id": r.claim.id, "line": r.claim.line, "verdict": r.verdict.value, "claim": r.claim.text,
            "reason": next((c.reason for c in r.checks if c.reason), ""),
            "url": chk.url if chk else None,
        }
        if ev and ev.quote:
            item["quote"] = ev.quote[:400]
            item["quote_kind"] = ev.kind
            item["fragment_url"] = ev.fragment_url
        flags = [f for c in r.checks if c.validity for f in c.validity.flags]
        if flags:
            item["source_flags"] = flags[:4]
        if r.repair and r.repair.found:
            item["fix_url"] = r.repair.found.url
            if r.repair.found.evidence:
                item["fix_quote"] = r.repair.found.evidence.quote[:400]
        elif r.repair and r.repair.attempted:
            item["search"] = r.repair.stop_reason
        issues.append(item)
    st = rep.stats
    out = {
        "summary": {
            "claims": st["claims"], "passing": st["passing"], "faithfulness": st["faithfulness"],
            "counts": {k: v for k, v in st["counts"].items() if v},
        },
        "issues": issues,
        "usage": {k: rep.usage.get(k) for k in ("model", "calls", "cost_usd", "live_fetches", "searches")},
    }
    if st["counts"].get("UNCERTAIN") and not rep.usage.get("model"):
        out["hint"] = (f"{st['counts']['UNCERTAIN']} claim(s) need a model to judge. "
                       "Set SAYSSO_MODEL (e.g. anthropic/<model>) or pass model.")
    if st["claims"] == 0:
        out["hint"] = "No cited claims found: saysso looks for [1] markers, footnotes, and links."
    return out


def _cfg(model: str | None, **kw) -> Config:
    base = dict(model=model or os.environ.get("SAYSSO_MODEL") or None)
    base.update(kw)
    return Config.from_env(**base)


async def verify_report_impl(text: str, model: str | None = None, fix: bool = False,
                             max_llm_calls: int = 30) -> dict[str, Any]:
    from .pipeline import run

    cfg = _cfg(model, fix=fix or None, max_llm_calls=max_llm_calls)
    parsed: ParsedReport = parse_text(text)
    if not parsed.claims:
        parsed = parse_text(text)
    rep = await run(parsed, cfg, "report")
    return compact(rep)


async def check_claim_impl(claim: str, url: str, model: str | None = None) -> dict[str, Any]:
    from .models import Citation, Claim
    from .pipeline import run

    parsed = ParsedReport(
        text=claim,
        claims=[Claim("c1", claim, 0, len(claim), 1, ["1"], claim)],
        citations={"1": Citation("1", url=url)},
        format="single-claim",
    )
    rep = await run(parsed, _cfg(model), url)
    r = rep.claims[0]
    chk = r.checks[0] if r.checks else None
    ev = chk.evidence if chk else None
    return {
        "verdict": r.verdict.value, "passes": r.passes, "confidence": r.confidence,
        "reason": chk.reason if chk else "", "method": chk.method if chk else None,
        "quote": ev.quote[:400] if ev else None, "quote_kind": ev.kind if ev else None,
        "offsets": [ev.start, ev.end] if ev else None, "fragment_url": ev.fragment_url if ev else None,
        "source_flags": chk.validity.flags if chk and chk.validity else [],
    }


async def locate_quote_impl(quote: str, url: str) -> dict[str, Any]:
    from .cache import Cache
    from .fetch import Fetcher
    from .locate import locate, make_evidence
    from .normalize import NormText

    cfg = Config.from_env()
    fetcher = Fetcher(cfg, Cache(cfg.cache_dir, enabled=not cfg.no_cache))
    try:
        doc = await fetcher.fetch(url)
    finally:
        await fetcher.aclose()
    if not doc.ok:
        return {"found": False, "reason": doc.reason}
    norm = NormText(doc.text)
    m = locate(quote, norm)
    if not m:
        return {"found": False, "reason": "quote not found (exact, ellipsis, or fuzzy >= 90)"}
    return {"found": True, **to_jsonable(make_evidence(norm, m.start, m.end, url=doc.final_url or doc.url,
                                                       tier=m.tier, score=m.score))}


def build_server():
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server  # type: ignore

    try:
        from mcp.types import ToolAnnotations

        ann = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    except Exception:  # older SDKs
        ann = None

    server = Server(name="saysso", instructions=INSTRUCTIONS)
    kw = {"annotations": ann} if ann is not None else {}

    @server.tool(**kw)
    async def verify_report(text: str, model: str | None = None, fix: bool = False,
                            max_llm_calls: int = 30) -> dict:
        """Check every cited claim in a report (markdown with [n] citations, footnotes, or links) against the
        source it cites. Returns a summary and only the claims that fail, with quotes and URLs.
        Set fix=true to search for real sources for failing claims (needs a search API key in the environment)."""
        return await verify_report_impl(text, model, fix, max_llm_calls)

    @server.tool(**kw)
    async def check_claim(claim: str, url: str, model: str | None = None) -> dict:
        """Check one claim against one URL. Returns verdict, the best quote with character offsets, and flags."""
        return await check_claim_impl(claim, url, model)

    @server.tool(**kw)
    async def locate_quote(quote: str, url: str) -> dict:
        """Find an exact quote in a web page or PDF. Deterministic, no model. Returns character offsets and a
        link that scrolls the browser to the quote."""
        return await locate_quote_impl(quote, url)

    return server


def main() -> None:
    build_server().run("stdio")


__all__ = ["build_server", "main", "compact", "Verdict"]
