"""Model access, kept dependency-free (plain HTTP via httpx) so the supply chain stays small.

Spec strings:
  anthropic/<model>          ANTHROPIC_API_KEY  (prompt caching on the source block)
  openai/<model>             OPENAI_API_KEY     (strict JSON schema output)
  openrouter/<model>         OPENROUTER_API_KEY
  gemini/<model>             GEMINI_API_KEY     (OpenAI-compatible endpoint)
  groq/<model>               GROQ_API_KEY
  deepseek/<model>           DEEPSEEK_API_KEY
  ollama/<model>             local, OLLAMA_BASE_URL (default http://localhost:11434)
  compat/<model>             any OpenAI-compatible server: SAYSSO_BASE_URL + SAYSSO_API_KEY
  codex[/<model>]            your ChatGPT plan through the Codex CLI ("Sign in with ChatGPT").
                             Run `codex login` once. Personal/local use only, per OpenAI's terms.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from .cache import Cache


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class Usage:
    calls: int = 0
    cached_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None


class Budget:
    def __init__(self, max_calls: int = 60, max_cost: float | None = None,
                 price_in: float | None = None, price_out: float | None = None):
        self.max_calls = max_calls
        self.max_cost = max_cost
        self.price_in = price_in
        self.price_out = price_out
        self.usage = Usage(cost_usd=0.0 if price_in is not None and price_out is not None else None)
        self._lock = asyncio.Lock()

    async def reserve(self) -> None:
        async with self._lock:
            if self.usage.calls >= self.max_calls:
                raise BudgetExceeded(f"model call budget reached ({self.max_calls}); raise --max-llm-calls")
            if self.max_cost is not None and self.usage.cost_usd is not None and self.usage.cost_usd >= self.max_cost:
                raise BudgetExceeded(f"cost budget reached (${self.max_cost:.2f}); raise --max-cost")
            self.usage.calls += 1

    def record(self, tin: int, tout: int, cache_read: int = 0) -> None:
        u = self.usage
        u.input_tokens += tin
        u.output_tokens += tout
        u.cache_read_tokens += cache_read
        if u.cost_usd is not None:
            u.cost_usd += (tin * (self.price_in or 0) + tout * (self.price_out or 0)) / 1_000_000

    def as_dict(self) -> dict[str, Any]:
        return asdict(self.usage)


def parse_json_loose(text: str) -> Any:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(t[a : b + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError("model did not return valid JSON")


class BaseLLM:
    provider = "base"

    def __init__(self, model: str, budget: Budget, cache: Cache, client: httpx.AsyncClient):
        self.model = model
        self.budget = budget
        self.cache = cache
        self.client = client

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider

    async def complete_json(self, system: str, prefix: str, suffix: str, schema: dict, max_tokens: int = 800) -> Any:
        """prefix = large reusable block (source passages), suffix = small varying block (claims)."""
        key = Cache.key(self.name, system, prefix, suffix, json.dumps(schema, sort_keys=True))
        hit = self.cache.get("llm", key)
        if hit is not None:
            self.budget.usage.cached_hits += 1
            return hit
        await self.budget.reserve()
        text, tin, tout, cread = await self._call(system, prefix, suffix, schema, max_tokens)
        self.budget.record(tin, tout, cread)
        data = parse_json_loose(text)
        self.cache.set("llm", key, data)
        return data

    async def _call(self, system, prefix, suffix, schema, max_tokens) -> tuple[str, int, int, int]:
        raise NotImplementedError

    async def _post(self, url: str, headers: dict, body: dict, timeout: float = 120.0) -> dict:
        delay = 2.0
        for attempt in range(4):
            try:
                r = await self.client.post(url, headers=headers, json=body, timeout=timeout)
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise LLMError(f"{self.name}: network error ({type(e).__name__})") from e
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if r.status_code in (429, 500, 502, 503, 504, 529) and attempt < 3:
                wait = r.headers.get("retry-after")
                await asyncio.sleep(float(wait) if wait and wait.replace(".", "").isdigit() else delay)
                delay *= 2
                continue
            if r.status_code >= 400:
                raise LLMError(f"{self.name}: HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        raise LLMError(f"{self.name}: gave up after retries")


class OpenAICompatible(BaseLLM):
    def __init__(self, provider: str, model: str, base_url: str, api_key: str | None, **kw):
        super().__init__(model, **kw)
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._schema_ok = True

    async def _call(self, system, prefix, suffix, schema, max_tokens):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider == "openrouter":
            headers["X-Title"] = "saysso"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prefix + "\n\n" + suffix},
            ],
        }
        if self.provider == "openai":
            body["max_completion_tokens"] = max_tokens + 4000  # headroom for reasoning tokens
            if os.environ.get("SAYSSO_REASONING_EFFORT"):
                body["reasoning_effort"] = os.environ["SAYSSO_REASONING_EFFORT"]
            if self._schema_ok:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "verdicts", "schema": schema, "strict": True},
                }
        else:
            body["max_tokens"] = max_tokens
            body["temperature"] = 0
            if self._schema_ok:
                body["response_format"] = {"type": "json_object"}
        try:
            data = await self._post(f"{self.base_url}/chat/completions", headers, body)
        except LLMError as e:
            if self._schema_ok and "HTTP 400" in str(e) and "response_format" in body:
                self._schema_ok = False  # provider/model without JSON mode: fall back to plain text
                body.pop("response_format")
                data = await self._post(f"{self.base_url}/chat/completions", headers, body)
            else:
                raise
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{self.name}: unexpected response shape") from e
        u = data.get("usage") or {}
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        return text, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)), int(cached)


class AnthropicLLM(BaseLLM):
    provider = "anthropic"

    def __init__(self, model: str, api_key: str | None, **kw):
        super().__init__(model, **kw)
        self.api_key = api_key
        self.base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")

    async def _call(self, system, prefix, suffix, schema, max_tokens):
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        first: dict[str, Any] = {"type": "text", "text": prefix}
        if len(prefix) > 6000:  # big enough to clear the minimum cacheable prompt size on most models
            first["cache_control"] = {"type": "ephemeral"}
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": system + "\nRespond with a single JSON object only.",
            "messages": [{"role": "user", "content": [first, {"type": "text", "text": suffix}]}],
        }
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        data = await self._post(f"{self.base_url}/v1/messages", headers, body)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        u = data.get("usage") or {}
        tin = int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0) or 0)
        return text, tin, int(u.get("output_tokens", 0)), int(u.get("cache_read_input_tokens", 0) or 0)


class CodexCLI(BaseLLM):
    """Runs `codex exec` non-interactively: uses whatever the user signed in with (ChatGPT plan or API key)."""

    provider = "codex"
    _sem: asyncio.Semaphore | None = None
    _checked = False

    async def _preflight(self, exe: str) -> None:
        """`codex exec` does not fail without credentials, it stalls retrying. `codex login status` answers
        at once, so check first and fail with instructions instead of hanging."""
        if self._checked or os.environ.get("SAYSSO_CODEX_SKIP_PREFLIGHT"):
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "login", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=20)
        except (asyncio.TimeoutError, OSError) as e:
            raise LLMError("could not run `codex login status`; is the Codex CLI installed correctly?") from e
        if proc.returncode != 0:
            raise LLMError("Codex CLI is not signed in. Run `codex login` and choose Sign in with ChatGPT "
                           "(set SAYSSO_CODEX_SKIP_PREFLIGHT=1 if you authenticate another way).")
        self._checked = True

    async def _call(self, system, prefix, suffix, schema, max_tokens):
        exe = shutil.which("codex")
        if not exe:
            raise LLMError("Codex CLI not found. Install it (npm i -g @openai/codex), run `codex login`, "
                           "and choose Sign in with ChatGPT.")
        prompt = (
            f"{system}\n\nDo not run commands, read files or browse. Answer only from the text below.\n\n"
            f"{prefix}\n\n{suffix}"
        )
        await self._preflight(exe)
        if self._sem is None:
            self._sem = asyncio.Semaphore(2)
        async with self._sem:
            with tempfile.TemporaryDirectory(prefix="saysso-codex-") as tmp:
                schema_path = Path(tmp) / "schema.json"
                out_path = Path(tmp) / "out.txt"
                schema_path.write_text(json.dumps(schema))
                cmd = [exe, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                       "--ignore-user-config", "--ignore-rules", "--color", "never", "-C", tmp,
                       "--output-schema", str(schema_path), "-o", str(out_path)]
                if self.model:
                    cmd += ["-m", self.model]
                cmd.append("-")
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, err = await asyncio.wait_for(proc.communicate(prompt.encode()),
                                                   timeout=float(os.environ.get("SAYSSO_CODEX_TIMEOUT", "300")))
                except asyncio.TimeoutError as e:
                    proc.kill()
                    raise LLMError("codex exec timed out") from e
                if proc.returncode != 0 or not out_path.exists():
                    tail = (err or b"").decode(errors="replace")[-400:]
                    hint = " Run `codex login` first." if "login" in tail.lower() or "auth" in tail.lower() else ""
                    raise LLMError(f"codex exec failed (exit {proc.returncode}).{hint} {tail}")
                text = out_path.read_text(encoding="utf-8", errors="replace")
        return text, 0, 0, 0


PROVIDERS = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
}


def make_llm(spec: str | None, budget: Budget, cache: Cache, client: httpx.AsyncClient) -> BaseLLM | None:
    if not spec:
        return None
    provider, _, model = spec.partition("/")
    provider = provider.lower()
    kw = {"budget": budget, "cache": cache, "client": client}
    if provider == "codex":
        return CodexCLI(model, **kw)
    if not model:
        raise LLMError(f"model spec must look like provider/model, got {spec!r}")
    if provider == "anthropic":
        return AnthropicLLM(model, os.environ.get("ANTHROPIC_API_KEY"), **kw)
    if provider == "ollama":
        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/") + "/v1"
        return OpenAICompatible("ollama", model, base, None, **kw)
    if provider == "compat":
        base = os.environ.get("SAYSSO_BASE_URL")
        if not base:
            raise LLMError("compat/<model> needs SAYSSO_BASE_URL (and SAYSSO_API_KEY if required)")
        return OpenAICompatible("compat", model, base, os.environ.get("SAYSSO_API_KEY"), **kw)
    if provider in PROVIDERS:
        base, env = PROVIDERS[provider]
        key = os.environ.get(env)
        if not key:
            raise LLMError(f"{env} is not set (needed for {spec})")
        return OpenAICompatible(provider, model, base, key, **kw)
    raise LLMError(f"unknown provider {provider!r}; use anthropic, openai, openrouter, gemini, groq, deepseek, "
                   "ollama, compat, or codex")


class MiniCheckNLI:
    """Small local grounding checker served by Ollama (e.g. bespoke-minicheck). Answers Yes/No.

    Note: Bespoke-MiniCheck weights carry their own license (non-commercial at the time of writing).
    saysso never downloads them; you pull the model yourself.
    """

    def __init__(self, spec: str, cache: Cache, client: httpx.AsyncClient):
        provider, _, model = spec.partition("/")
        if provider.lower() != "ollama" or not model:
            raise LLMError("--nli currently supports ollama/<model>, e.g. ollama/bespoke-minicheck")
        self.model = model
        self.base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        self.cache = cache
        self.client = client
        self.calls = 0

    async def supports(self, claim: str, document: str) -> bool:
        key = Cache.key("nli", self.model, claim, document)
        hit = self.cache.get("nli", key)
        if hit is not None:
            return bool(hit)
        body = {"model": self.model, "prompt": f"Document: {document}\nClaim: {claim}", "stream": False,
                "options": {"temperature": 0, "num_predict": 3}}
        try:
            r = await self.client.post(f"{self.base}/api/generate", json=body, timeout=120)
            r.raise_for_status()
            ans = (r.json().get("response") or "").strip().lower()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"NLI model unavailable at {self.base}: {type(e).__name__}") from e
        self.calls += 1
        ok = ans.startswith("yes")
        self.cache.set("nli", key, ok)
        return ok
