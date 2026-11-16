# saysso

"Your AI cited it. Does the source say so?" A Python CLI (plus MCP server, agent skill and GitHub
Action) that checks each cited claim in a report against the source it cites, with exact quotes and
character offsets as evidence.

## Commands

```bash
pip install -e ".[dev]"                         # dev setup (adds pytest, pymupdf, mcp)
python -m pytest -q                             # full suite; hermetic, needs no network
saysso check examples/demo_report.md -v         # live demo; fetches Wikipedia pages
python -m build                                 # sdist + wheel
```

The test suite runs against a local HTTP server on 127.0.0.1 (tests pass `allow_private=True`), so it
works offline. The demo needs outbound HTTPS. If a sandbox blocks it, that is the environment, not a bug.

## Map

| File | Role |
|---|---|
| `src/saysso/models.py` | Data contract. `RunReport.to_dict()` is the JSON evidence format, schema `saysso/1` |
| `parse.py` | Reports (markdown, HTML, Anthropic/OpenAI API JSON) into claims + citations |
| `fetch.py`, `extract.py` | Safe fetching (SSRF guard, block/paywall detection, Wayback), HTML/PDF text |
| `normalize.py`, `locate.py` | Offset-preserving normalization; deterministic quote location |
| `retrieve.py`, `gates.py` | BM25 passages; number/unit/negation/coverage checks |
| `judge.py` | The cascade: verbatim, numbers, lexical, optional NLI, batched LLM, recheck |
| `llm.py` | Providers (anthropic, openai, openrouter, gemini, groq, deepseek, ollama, compat, codex), Budget |
| `validity.py` | Source quality: domain class, retractions (Crossref + OpenAlex), staleness, duplicates |
| `search.py`, `repair.py` | `--fix`: persistent source finding with explicit stopping rules |
| `pipeline.py` | Orchestration: `run(parsed, cfg, name) -> RunReport` |
| `report.py`, `html_report.py` | Text, JSON, SARIF, HTML, the `--fix` annotated copy |
| `mcp_server.py`, `cli.py`, `agree.py` | MCP tools, CLI entry point, Cohen's kappa vs human labels |

## Rules (1 to 5 are enforced by the test suite; 6 to 9 are review rules)

1. **Never trust a model's quote.** Evidence only counts if `locate.py` finds it in the stored source
   text. A model quote that cannot be located caps confidence at 0.5 and adds a note.
2. **Offline mode must not accuse on weak evidence.** Keyword overlap cannot bridge synonyms, so a
   lexical-only verdict is UNSUPPORTED only when passage coverage is below 0.15; otherwise UNCERTAIN.
   See `test_offline_never_accuses_a_true_claim_that_uses_synonyms`.
3. **Fetch failures are SOURCE-UNREACHABLE, never UNSUPPORTED** (blocked, paywalled, empty, 4xx/5xx,
   template pages).
4. **Two direct runtime dependencies** (`httpx`, `rapidfuzz`). `mcp` and `pymupdf` are optional extras,
   imported lazily. Do not add runtime dependencies.
5. **Fetched text is untrusted.** It is fenced before any model sees it (`judge._fence`). The SSRF guard
   stays on by default.
6. **No `${{ inputs.* }}` inside `run:` blocks** in `action.yml`; inputs go through `env:`.
7. **Change verdict logic only with a failing test first** (judge, gates, parse, locate, pipeline).
8. **No hardcoded model names as defaults.** Model names appear only in docs and examples.
9. **Docs must match the tool.** The README demo block is real output of `examples/demo_report.md`;
   re-check it after any change to output formatting.

Naming: package, command and import are all `saysso`; env vars are `SAYSSO_*`.

## First tasks, in order

1. Replace placeholders with the real owner from `git remote -v`: `YOUR-GITHUB` in `pyproject.toml`,
   `src/saysso/config.py` (user agent) and `src/saysso/report.py` (SARIF `informationUri`); `OWNER` in
   `README.md` and the comment in `action.yml`.
2. Push and confirm CI (`.github/workflows/ci.yml`) is green on Python 3.10 and 3.12.
3. Smoke-test one real provider with a real key (`--model anthropic/<model>` or `openai/<model>`). The
   adapters are only tested against a fake model; fix any response-shape bug with a test.
4. Before tagging `v0.1.0`: set up PyPI trusted publishing (GitHub environment `pypi`) for
   `.github/workflows/release.yml`. Consider also reserving the typo `sayso` on PyPI (free at the time
   of writing) so it cannot be squatted.
5. Then the benchmark that will drive adoption: collect deep-research exports from ChatGPT, Gemini,
   Perplexity and Claude on the same prompts, hand-label about 300 claims, report agreement with
   `saysso agree`, and publish per-product citation faithfulness with raw data.

## Known gaps (v0.1)

- `--model codex` (ChatGPT sign-in via the Codex CLI) has a tested command and a login pre-flight, but
  has never run against a live login.
- Without a model, tricky claims come back UNCERTAIN by design; that is the honest answer offline.
- Not built: PDF page-aware links, picking the Wayback snapshot closest to the report date, Unpaywall
  resolution, model-based claim decomposition, non-English queries.
