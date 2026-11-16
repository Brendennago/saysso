"""saysso command line.

  saysso check report.md [--model anthropic/claude-haiku-5-5] [--fix --search brave] [--html] [--json out.json]
  saysso claim "The Eiffel Tower is 330 metres tall." https://en.wikipedia.org/wiki/Eiffel_Tower
  saysso locate "exact quote" https://example.com/page
  saysso mcp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .llm import LLMError
from .models import Citation, Claim, Verdict
from .parse import ParsedReport, load_report
from .search import SearchError

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def _common(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("models (all optional; without one, only deterministic checks run, at zero cost)")
    g.add_argument("--model", help="judge model, e.g. anthropic/claude-haiku-5-5, openai/<model>, ollama/qwen3:8b, codex")
    g.add_argument("--recheck-model", help="second model for negative verdicts (another model family is best)")
    g.add_argument("--nli", help="small local grounding model, e.g. ollama/bespoke-minicheck")
    g.add_argument("--judge-all", action="store_true", help="send every claim to the model, not just uncertain ones")
    b = p.add_argument_group("budget")
    b.add_argument("--max-llm-calls", type=int, default=60)
    b.add_argument("--max-cost", type=float, help="stop calling the model after this many USD (needs --price-in/out)")
    b.add_argument("--price-in", type=float, help="USD per 1M input tokens, for cost tracking")
    b.add_argument("--price-out", type=float, help="USD per 1M output tokens")
    f = p.add_argument_group("fetching")
    f.add_argument("--timeout", type=float, default=20.0)
    f.add_argument("--concurrency", type=int, default=8)
    f.add_argument("--no-cache", action="store_true")
    f.add_argument("--no-wayback", action="store_true")
    f.add_argument("--no-retraction-check", action="store_true")
    f.add_argument("--allow-private", action="store_true", help="allow localhost/private addresses (testing only)")
    f.add_argument("--email", help="contact for Crossref/OpenAlex polite pools")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="saysso",
        description="pytest for citations: checks that each cited source actually supports the claim it is attached to.",
    )
    sub = p.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="check a report (markdown, text, HTML, or Claude/OpenAI API JSON)")
    c.add_argument("files", nargs="+", help="report files, or - for stdin")
    _common(c)
    r = c.add_argument_group("repair (persistent search for the real source)")
    r.add_argument("--fix", action="store_true", help="search for real sources for failing claims; writes <file>.saysso.md")
    r.add_argument("--search", help="comma list: brave, serper, tavily, searxng, openalex, crossref")
    r.add_argument("--max-queries", type=int, default=8, help="searches per failing claim")
    r.add_argument("--max-fetches", type=int, default=10, help="pages fetched per failing claim")
    r.add_argument("--no-llm-queries", action="store_true", help="don't let the model propose search queries")
    o = c.add_argument_group("output")
    o.add_argument("--format", choices=["text", "json"], default="text", help="what to print on stdout")
    o.add_argument("--json", metavar="PATH", help="write the full JSON evidence report")
    o.add_argument("--html", nargs="?", const="saysso-report.html", metavar="PATH")
    o.add_argument("--sarif", metavar="PATH", help="SARIF for GitHub code scanning")
    o.add_argument("--fail-under", type=float, help="exit 1 if faithfulness is below this (0-1)")
    o.add_argument("--fail-on-unreachable", action="store_true")
    o.add_argument("--inherit-citations", action="store_true",
                   help="uncited sentences inherit the next citation in their paragraph")

    cl = sub.add_parser("claim", help="check one claim against one URL")
    cl.add_argument("claim")
    cl.add_argument("url")
    _common(cl)
    cl.add_argument("--format", choices=["text", "json"], default="text")

    lo = sub.add_parser("locate", help="find a quote in a URL or file; prints exact character offsets")
    lo.add_argument("quote")
    lo.add_argument("source", help="URL or local file")
    lo.add_argument("--allow-private", action="store_true")
    lo.add_argument("--no-cache", action="store_true")

    ag = sub.add_parser("agree", help="agreement (Cohen's kappa) between saysso JSON output and human labels")
    ag.add_argument("--pred", required=True, help="JSON written by `saysso check --json`")
    ag.add_argument("--labels", required=True, help="CSV with columns id,label")
    ag.add_argument("--format", choices=["text", "json"], default="text")

    sub.add_parser("mcp", help="run the MCP server (stdio)")
    sub.add_parser("version", help="print version")
    return p


def _config(a: argparse.Namespace, **extra) -> Config:
    return Config.from_env(
        model=a.model, recheck_model=a.recheck_model, nli=a.nli, judge_all=a.judge_all or None,
        max_llm_calls=a.max_llm_calls, max_cost_usd=a.max_cost, price_in=a.price_in, price_out=a.price_out,
        timeout=a.timeout, concurrency=a.concurrency, no_cache=a.no_cache or None,
        wayback=False if a.no_wayback else None, retraction_check=False if a.no_retraction_check else None,
        allow_private=a.allow_private or None, email=a.email, **extra,
    )


def _progress(quiet: bool):
    if quiet or not sys.stderr.isatty():
        return None
    return lambda msg: print(f"  {msg}\u2026", file=sys.stderr)


def cmd_check(a: argparse.Namespace) -> int:
    from .pipeline import check_sync
    from .report import patched_markdown, render_html, render_sarif, render_text, to_json

    extra = dict(fix=a.fix or None, max_queries_per_claim=a.max_queries, max_fetches_per_claim=a.max_fetches,
                 llm_queries=False if a.no_llm_queries else None, inherit_citations=a.inherit_citations or None)
    if a.search:
        extra["search"] = [s.strip() for s in a.search.split(",") if s.strip()]
    cfg = _config(a, **extra)
    stdin_text = sys.stdin.read() if "-" in a.files else None
    reports, texts, failed = [], {}, False
    for path in a.files:
        try:
            parsed = load_report(path, inherit=cfg.inherit_citations, stdin_text=stdin_text)
        except (OSError, ValueError) as e:
            print(f"saysso: cannot read {path}: {e}", file=sys.stderr)
            return EXIT_USAGE
        texts[path] = parsed.text
        if not parsed.claims:
            print(f"saysso: {path}: no cited claims found (no [n] markers, footnotes, or links)", file=sys.stderr)
        try:
            rep = check_sync(parsed, cfg, path, _progress(a.quiet or a.format == "json"))
        except (LLMError, SearchError) as e:
            print(f"saysso: {e}", file=sys.stderr)
            return EXIT_USAGE
        reports.append(rep)
        if a.format == "text" and not a.quiet:
            print(render_text(rep, verbose=a.verbose))
            print()
        fa = rep.stats["faithfulness"]
        if a.fail_under is not None and fa is not None and fa < a.fail_under:
            failed = True
        if a.fail_on_unreachable and rep.stats["counts"][Verdict.UNREACHABLE.value]:
            failed = True
        if a.fix and path != "-":
            out = Path(path).with_suffix(".saysso.md")
            out.write_text(patched_markdown(parsed.text, rep), encoding="utf-8")
            print(f"annotated copy: {out}", file=sys.stderr)

    if a.format == "json":
        payload = reports[0].to_dict() if len(reports) == 1 else [r.to_dict() for r in reports]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if a.json:
        payload = reports[0].to_dict() if len(reports) == 1 else [r.to_dict() for r in reports]
        Path(a.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.html:
        if len(reports) == 1:
            Path(a.html).write_text(render_html(reports[0]), encoding="utf-8")
        else:
            for rep in reports:
                Path(Path(rep.input).stem + ".saysso.html").write_text(render_html(rep), encoding="utf-8")
    if a.sarif:
        Path(a.sarif).write_text(render_sarif(reports, texts), encoding="utf-8")
    return EXIT_FAIL if failed else EXIT_OK


def cmd_claim(a: argparse.Namespace) -> int:
    from .pipeline import check_sync
    from .report import render_text

    cfg = _config(a)
    parsed = ParsedReport(
        text=a.claim,
        claims=[Claim(id="c1", text=a.claim, start=0, end=len(a.claim), line=1, citation_ids=["1"], raw=a.claim)],
        citations={"1": Citation(id="1", url=a.url)},
        format="single-claim",
    )
    try:
        rep = check_sync(parsed, cfg, a.url)
    except LLMError as e:
        print(f"saysso: {e}", file=sys.stderr)
        return EXIT_USAGE
    if a.format == "json":
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_text(rep, verbose=True))
    return EXIT_OK if rep.claims and rep.claims[0].passes else EXIT_FAIL


def cmd_locate(a: argparse.Namespace) -> int:
    from .cache import Cache
    from .fetch import Fetcher
    from .locate import locate, make_evidence
    from .normalize import NormText

    if Path(a.source).exists():
        text, url = Path(a.source).read_text(encoding="utf-8", errors="replace"), None
    else:
        async def get():
            cfg = Config.from_env(allow_private=a.allow_private or None, no_cache=a.no_cache or None)
            f = Fetcher(cfg, Cache(cfg.cache_dir, enabled=not cfg.no_cache))
            try:
                return await f.fetch(a.source)
            finally:
                await f.aclose()

        doc = asyncio.run(get())
        if not doc.ok:
            print(json.dumps({"found": False, "reason": doc.reason}))
            return EXIT_FAIL
        text, url = doc.text, doc.final_url or doc.url
    norm = NormText(text)
    m = locate(a.quote, norm)
    if not m:
        print(json.dumps({"found": False, "reason": "quote not found (exact, ellipsis, or fuzzy >= 90)"}))
        return EXIT_FAIL
    ev = make_evidence(norm, m.start, m.end, url=url, tier=m.tier, score=m.score)
    from .models import to_jsonable

    print(json.dumps({"found": True, **to_jsonable(ev)}, ensure_ascii=False, indent=2))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if getattr(a, "verbose", False) else logging.WARNING,
                        format="saysso: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.cmd == "check":
        return cmd_check(a)
    if a.cmd == "claim":
        return cmd_claim(a)
    if a.cmd == "locate":
        return cmd_locate(a)
    if a.cmd == "agree":
        from . import agree

        try:
            res = agree.agreement(agree.load_predictions(a.pred), agree.load_labels(a.labels))
        except (OSError, ValueError, KeyError) as e:
            print(f"saysso: {e}", file=sys.stderr)
            return EXIT_USAGE
        print(json.dumps(res, indent=2) if a.format == "json" else agree.render(res))
        return EXIT_OK
    if a.cmd == "mcp":
        try:
            import mcp  # noqa: F401  (the SDK is imported lazily inside the server, so probe it here)

            from .mcp_server import main as mcp_main
        except ImportError as e:
            print(f"saysso: MCP server unavailable ({e}). Install: pip install 'saysso[mcp]'", file=sys.stderr)
            return EXIT_USAGE
        mcp_main()
        return EXIT_OK
    if a.cmd == "version":
        print(__version__)
        return EXIT_OK
    p.print_help()
    return EXIT_USAGE
