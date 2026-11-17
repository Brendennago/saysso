# saysso

**Your AI cited it. Does the source say so?**

AI research tools add footnotes to everything, and the links usually work. Whether the page behind a
link actually says what the sentence claims is another matter. saysso checks that for you: give it a
report, and for every cited claim it opens the source, looks for the claim, and shows you the exact
quote it found (or tells you it found nothing).

It runs on your computer, works without any AI model or API key, and costs nothing unless you choose
to add a model.

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

In plain terms: the tower's height is wrong (the source says 330 m, not 512 m), one claim has no link at
all, and three claims need a closer look, because matching words cannot tell "is visible" from "is not
visible". Supported claims are hidden unless you add `-v`.

## Why

Deep-research tools cite confidently, and the links usually work. The pages often do not say what the
sentence next to them claims. PwC's study
[Cited but Not Verified](https://arxiv.org/abs/2605.06635) (arXiv 2605.06635) tested 14 models: for 12
of them more than 94% of cited links worked, yet the share of claims their sources actually supported
ranged from 24% to 77%, and it fell as agents made more tool calls. You cannot see that by clicking a
link. saysso reads the page for you and keeps the receipts.


## Install

You need Python 3.10 or newer. Install saysso as a command-line tool with
[pipx](https://pipx.pypa.io) or [uv](https://docs.astral.sh/uv/):

```bash
pipx install "git+https://github.com/Brendennago/saysso"
```

or

```bash
uv tool install "git+https://github.com/Brendennago/saysso"
```

Check that it worked:

```bash
saysso version
```

saysso is not on PyPI yet. Once it is, `pipx install saysso` will do the same thing. If your reports
cite PDFs, install the PDF extra instead:
`pipx install "saysso[pdf] @ git+https://github.com/Brendennago/saysso"`.

## Check your first report

**1. Save the report as a file.** Copy the report out of ChatGPT, Claude, Gemini, Perplexity or any
other tool and paste it into a plain text file, for example `report.md`. Keep two things:

- the citation markers in the text, such as `[1]`, `[2, 3]` or `[^1]` (inline links work too), and
- the list of sources at the end, with their links.

saysso also reads saved HTML pages and the raw JSON from the Anthropic and OpenAI APIs.

**2. Run it.**

```bash
saysso check report.md
```

It fetches each cited page (this takes a few seconds per source), then prints the claims that did not
pass. Add `-v` to see every claim, including the ones that passed, with the quote that supports each.

**3. Want something easier to read or share?** Write a self-contained web page:

```bash
saysso check report.md --html report.html
```

## Reading the results

Each claim gets one verdict:

| Mark | Verdict | What it means | What to do |
|---|---|---|---|
| ✔ | SUPPORTED | The source says it. The quote is shown with `-v`. | Nothing. |
| ◐ | PARTIAL | The source says something close, but a number, date, scope or hedge differs. | Read the quote and fix the wording. |
| ✘ | UNSUPPORTED | The source was read, and the claim is not in it. | Find a better source, or try `--fix`. |
| ≠ | CONTRADICTED | The source says something different, for example another number. | Correct the claim. |
| ⇄ | WRONG-SOURCE | Not in the cited page, but `--fix` found another page that supports it. | Swap in the new link. |
| ∅ | SOURCE-UNREACHABLE | The page could not be read: dead, blocked, paywalled, or no link at all. This says nothing about whether the claim is true. | Open the link yourself, or supply a working one. |
| ? | UNCERTAIN | Word matching alone cannot decide (synonyms, negation). | Check it yourself, or add a model (next section). |

The summary line gives a **faithfulness** score: of the claims saysso could judge, the share whose source
backs them up (PARTIAL counts as half). Unreachable and uncertain claims are left out of the score and
counted separately, so a low number of judged claims means you should look at those.

## Settle the uncertain claims with a model

Without a model, saysso only uses exact checks: matching wording, matching numbers, and keyword overlap.
To settle the UNCERTAIN claims, give it a model. Only those claims are sent, in small batches, along with
a few short passages from the source rather than whole pages. Set your provider's API key, then pass
`--model provider/model-name`:

```bash
export ANTHROPIC_API_KEY=...          # or OPENAI_API_KEY, GEMINI_API_KEY, and so on
saysso check report.md --model anthropic/<model-name>
```

Use any model name your provider offers. To avoid typing it each time, set `SAYSSO_MODEL` instead of
passing `--model`. Results are cached, so running the same report again costs nothing, and
`--max-llm-calls` (default 60) caps spending. Local models through Ollama
(`--model ollama/<model-name>`) cost nothing. All providers are listed under
[Modes and cost](#modes-and-cost).

## More things you can do

```bash
# Check one sentence against one page
saysso claim "The tower is 330 metres tall." https://en.wikipedia.org/wiki/Eiffel_Tower

# Find where a quote appears in a page, with exact character positions
saysso locate "the tallest structure in Paris" https://en.wikipedia.org/wiki/Eiffel_Tower

# Search the web for real sources for the claims that failed (writes report.saysso.md)
saysso check report.md --fix --search openalex,crossref

# Save the full evidence as JSON
saysso check report.md --json results.json
```

`saysso --help` and `saysso check --help` list every option.

## Troubleshooting

- **"no cited claims found"**: the file has no citation markers that saysso recognises. Make sure the
  `[1]`-style markers and the source list survived copying, or that the links are still links.
- **"ANTHROPIC_API_KEY is not set"** (or another key): you asked for a model but the key is not in your
  environment. Run `export ANTHROPIC_API_KEY=...` in the same terminal, or leave out `--model`.
- **"model spec must look like provider/model"**: write the provider first, for example
  `--model openai/<model-name>`, not just the model name.
- **Lots of SOURCE-UNREACHABLE**: some sites block automated readers or sit behind paywalls. Dead links
  are retried through the Wayback Machine automatically. PDFs need the PDF extra (see [Install](#install)).
- **Results look stale**: pages are cached between runs. Add `--no-cache` to fetch them again.
- **Using it in CI**: `saysso check` exits with 1 when `--fail-under` is set and faithfulness falls
  below it, and with 2 for usage errors.

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

Add saysso to a GitHub workflow to check reports on every pull request:

```yaml
name: citations
on: pull_request
jobs:
  saysso:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Brendennago/saysso@<full-commit-sha>   # pin by SHA, not by tag
        with:
          files: "docs/**/*.md"
          fail-under: "0.9"
```

The job fails when faithfulness drops below the threshold and writes `saysso.sarif`; upload it with
`github/codeql-action/upload-sarif` to see findings on the lines they refer to. To use a model, pass
`model:` and put the provider key in the step's `env` from a repository secret, never in `with`.
Without a `version`, the action installs saysso from the same commit you pinned.

## Use it from an agent

**Claude Code (MCP):**

```bash
claude mcp add --transport stdio saysso -- uvx --from "saysso[mcp] @ git+https://github.com/Brendennago/saysso" saysso mcp
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

GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE).
