# saysso

**Your AI cited it. Does the source say so?**

saysso is pytest for citations. Give it any AI research report and it checks, claim by claim, whether each
cited source actually says what the report claims, with the exact quote and character range as proof.

```
$ saysso check examples/demo_report.md
saysso 0.1.0: examples/demo_report.md
7 cited claim(s), 4 source(s), 0 unreachable

  ≠ c2    CONTRADICTED       line 3    [1] en.wikipedia.org
        The tower is 512 metres tall.
        [1] number-gate: claim says 512, source says 330 metres
            source: medium; tertiary source: prefer the primary source it cites
  ? c3    UNCERTAIN          line 5    [2] en.wikipedia.org
        Mount Everest's elevation of 8,849 m was established by a Chinese-Nepali survey.
        [2] lexical: partial overlap (38% of key terms); needs a model or a human
            source: medium; tertiary source: prefer the primary source it cites
  ? c4    UNCERTAIN          line 5    [3] en.wikipedia.org
        The Great Wall of China is visible to the naked eye from the Moon.
        [3] lexical: negation differs between claim and source; needs a model or a human
            source: medium; tertiary source: prefer the primary source it cites
  ? c6    UNCERTAIN          line 7    [4] en.wikipedia.org
        Python is the most widely used language for embedded firmware on microcontrollers.
        [4] lexical: negation differs between claim and source; needs a model or a human
            source: medium; tertiary source: prefer the primary source it cites
  ∅ c7    SOURCE-UNREACHABLE line 9    [5] -
        A 2024 industry survey found that 73% of firms adopted the framework.
        [5] parse: reference has no URL (title only: State of the Framework Report 2024, Example Analytics)

======== 2 supported, 1 contradicted, 1 source-unreachable, 3 uncertain in 1.25s ========
faithfulness 66.7% of 3 judged (3 need a model or human; try --model) | no model (deterministic checks only) | 4 fetches, 0 searches
```

That run used no model and cost nothing. The three "uncertain" claims are the ones keyword matching
cannot settle (synonyms, negation); add `--model` and only those go to a model.

## Why

Deep-research tools cite confidently, and the links usually work. The pages often do not say what the
sentence next to them claims. PwC's study
[Cited but Not Verified](https://arxiv.org/abs/2605.06635) (arXiv 2605.06635) tested 14 models: for 12
of them more than 94% of cited links worked, yet the share of claims their sources actually supported
ranged from 24% to 77%, and it fell as agents made more tool calls. You cannot see that by clicking a
link. saysso reads the page for you and keeps the receipts.

## Quickstart

```bash
uvx saysso check report.md                                     # try it with no install

uv tool install saysso                                         # or: pipx install saysso
saysso check report.md                                         # deterministic checks only, no cost
saysso check report.md --model anthropic/claude-haiku-5-5      # a model, only for uncertain claims
saysso check report.md --model codex                           # your ChatGPT plan (see below)
saysso check report.md --fix --search brave                    # hunt for the real source of failures
saysso claim "The tower is 330 metres tall." https://en.wikipedia.org/wiki/Eiffel_Tower
saysso locate "the tallest structure in Paris" https://en.wikipedia.org/wiki/Eiffel_Tower
```

PDF sources need `pip install "saysso[pdf]"`; the MCP server needs `"saysso[mcp]"`.

**Inputs it understands:** markdown or text with `[1]`, `[1, 3]`, `[2-4]`, `[^1]` markers and a Sources
list; inline links; footnote definitions; HTML; and the JSON of Anthropic Messages responses (with
citations) and OpenAI Responses or Chat Completions responses (with `url_citation` annotations).
References that have a title but no link (common in app exports) are reported as unreachable, or
resolved by search when `--search` is set.

## How it works

Cheap and deterministic first. A model runs only when it can change the answer.

1. **Verbatim.** The claim's own wording appears in the source (exact or near-exact): supported.
2. **Numbers.** Every number, date and percentage in the claim must appear in the source. Rounding that
   the claim signals ("about", "nearly") or implies by precision is accepted: 1.2 million matches
   1,234,567, but 8,849 m does not match 8,844 m. A same-unit mismatch ("512 metres" vs "330 metres") is
   a contradiction.
3. **Key terms.** Co-occurrence of the claim's content words within two sentences, with a negation check.
4. **Local grounding model (optional).** `--nli ollama/<model>` runs a small checker on your machine.
5. **Language model (optional).** Claims are batched per source and see only the 4 most relevant
   passages, never whole pages. The model must quote its evidence; the quote is then located in the
   stored page. If it cannot be found, the verdict is downgraded.
6. **Second opinion (optional).** `--recheck-model` re-judges negative verdicts; disagreement becomes
   "needs review".

Blocked, paywalled, empty or dead pages are reported as **source-unreachable**, never as "unsupported".
Dead links fall back to the Wayback Machine and the report says so.

## Modes and cost

| Mode | Flag | Cost |
|---|---|---|
| Deterministic only | (none) | Free. Fetches pages, runs steps 1 to 3 |
| Local model | `--nli ollama/bespoke-minicheck` or `--model ollama/<model>` | Your hardware |
| Bring your own key | `--model <provider>/<model>` | Your provider bill, capped by `--max-llm-calls` and `--max-cost` |
| ChatGPT plan | `--model codex` | Uses your Codex CLI sign-in |

Providers and their environment variables:

| Spec | Key |
|---|---|
| `anthropic/<model>` | `ANTHROPIC_API_KEY` (prompt caching on large source blocks) |
| `openai/<model>` | `OPENAI_API_KEY` |
| `openrouter/<model>` | `OPENROUTER_API_KEY` |
| `gemini/<model>`, `groq/<model>`, `deepseek/<model>` | `GEMINI_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY` |
| `ollama/<model>` | none (`OLLAMA_BASE_URL` if not localhost) |
| `compat/<model>` | `SAYSSO_BASE_URL`, `SAYSSO_API_KEY` for any OpenAI-compatible server |
| `codex[/<model>]` | none: run `codex login` once and choose Sign in with ChatGPT |

Cost controls: results are cached on disk (rerunning a report costs nothing), `--max-llm-calls`,
`--max-cost` with `--price-in` and `--price-out`, and `--no-llm-queries` to keep search free of model calls.

**ChatGPT sign-in is experimental.** It runs `codex exec` as a subprocess and uses whatever you are
signed in with. If you are not signed in it stops at once and tells you to run `codex login`. OpenAI limits plan usage in third-party tools to open-source and local personal use, so
use it for your own machine, not as a hosted service. Anthropic subscriptions are not supported for
third-party tools; use an API key.

The Bespoke-MiniCheck weights have their own license (non-commercial when this was written). saysso
never downloads them; you pull the model yourself.

## Finding the real source (`--fix`)

For claims that fail, saysso searches the way a fact-checker does, and says why it stopped:

1. **Upstream.** Follow links on the cited page whose surrounding text matches the claim. Free.
2. **Differently shaped queries.** Exact title, distinctive phrase (numbers, names), entities, keywords,
   `site:.gov` / `site:.edu` for statistics-style claims, and OpenAlex / Crossref for research-style claims.
3. **Model-proposed queries (optional).** One small call per claim.

Each candidate page is judged on its own, never as a pile of search results in one prompt. It stops when
a supporting source is found, the per-claim search or fetch budget is spent (`--max-queries`,
`--max-fetches`), or the last three searches turned up no new domains. The search trail is in the report.
Output goes to `report.saysso.md`; your original is never rewritten.

Search providers: `brave`, `serper`, `tavily`, `searxng` (self-hosted, no key), `openalex`, `crossref`
(the last two are free).

## Is the source itself any good?

Reported separately from whether it supports the claim:

- **Retractions and concerns:** looked up by DOI in Crossref (which carries Retraction Watch data) and
  OpenAlex. They are combined and disagreement is shown. A claim supported only by a retracted paper does
  not pass.
- **Source type:** government, academic, news, preprint (not peer reviewed), reference work (tertiary),
  user-generated, and AI chat answers cited as evidence (circular).
- **Staleness:** the source predates a year the claim refers to.
- **Syndication:** near-duplicate pages are flagged so five reprints do not count as five sources.
- **Template pages:** different URLs serving identical text are treated as unreachable.

## Outputs

- **Text** (default): lists only claims that did not pass; `-v` shows everything.
- **JSON** (`--json out.json` or `--format json`): the full evidence record, schema `saysso/1`. Each
  verdict carries the quote, `start`/`end` character offsets into the stored copy of the source, the
  source's SHA-256, prefix/suffix context, a text-fragment link, validity flags, and the search trail.
- **HTML** (`--html report.html`): a single offline file, light and dark, usable on a phone.
- **SARIF** (`--sarif out.sarif`): for GitHub code scanning.
- **Annotated copy** (`--fix`): failing claims marked, supporting links added as comments.
- `saysso agree --pred out.json --labels labels.csv` computes raw agreement and Cohen's kappa against
  human labels, with a confusion matrix.

## CI

```yaml
- uses: Brendennago/saysso@<full-commit-sha>   # pin by SHA, not by tag
  with:
    files: "docs/**/*.md"
    fail-under: "0.9"
```

The job fails when faithfulness drops below the threshold and writes `saysso.sarif`; upload it with
`github/codeql-action/upload-sarif` to see findings on the lines they refer to. Provider keys go in the
job's `env`, never in `with`.

## Use it from an agent

**Claude Code (MCP):**

```bash
claude mcp add --transport stdio saysso -- uvx --from "saysso[mcp]" saysso mcp
```

Tools: `verify_report`, `check_claim`, `locate_quote`. Results are compact (only failing claims) to keep
the agent's context small.

**Skill:** `skills/saysso/SKILL.md` follows the open Agent Skills format. Copy the folder into your
agent's skills directory (Claude Code, Codex, OpenClaw and Hermes all read it), or install it with
`npx skills add Brendennago/saysso --skill saysso` once the repo is public. It tells the agent to check
its own answers before presenting them.

## Security

- Fetched pages are untrusted. Their text is fenced before any model sees it, and instructions inside a
  page are ignored.
- An SSRF guard blocks localhost and private addresses unless you pass `--allow-private`.
- saysso never runs commands taken from a page. The only subprocess it starts is the `codex` CLI, and
  only if you choose `--model codex`.
- Two direct runtime dependencies (`httpx`, `rapidfuzz`). PDF and MCP support are optional extras.
- The GitHub Action passes inputs through environment variables, so they cannot inject shell commands.

## Status

Tested: parsing, quote location with offsets, number rules, the verification cascade, the retrieval
step, the repair loop, fetch failures, HTML and SARIF output, the MCP server over a real stdio session,
and the CLI, against local test servers, a fake model, and live Wikipedia pages.

Not yet exercised against live paid APIs: the provider adapters are written from the public API docs and
tested with a fake model, so expect to report a bug if one provider's response shape differs. `--model
codex` has not been run against a live ChatGPT login in CI.

## Not in v0.1

PDF page-aware links, picking the Wayback snapshot closest to the report date, Unpaywall resolution for
paywalled papers, model-based claim decomposition, non-English queries, and a public benchmark across
commercial deep-research tools with human labels.

## License

MIT.
