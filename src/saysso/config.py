"""Run configuration. Every knob has an env var so CI and agents can set defaults once."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

from . import __version__


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def _env_float(name: str) -> float | None:
    v = _env(name)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


@dataclass
class Config:
    # Models. None means "no model": deterministic checks only, zero API cost.
    model: str | None = None  # e.g. anthropic/claude-haiku-5-5, openai/gpt-5-mini, ollama/qwen3:8b, codex
    recheck_model: str | None = None  # second opinion for negative verdicts (ideally another model family)
    nli: str | None = None  # small local grounding model, e.g. ollama/bespoke-minicheck

    # Judging
    judge_all: bool = False  # send every claim to the model (benchmark rigor) instead of only uncertain ones
    accept_threshold: float = 0.75  # confidence at which a non-LLM SUPPORTED verdict is final
    top_k: int = 4  # passages retrieved per claim
    batch_size: int = 8  # claims per model call (same source)
    inherit_citations: bool = False  # uncited sentences inherit the next citation in their paragraph

    # Budget
    max_llm_calls: int = 60
    max_cost_usd: float | None = None
    price_in: float | None = None  # USD per 1M input tokens (enables --max-cost)
    price_out: float | None = None

    # Repair loop (persistent source finding)
    fix: bool = False
    search: list[str] = field(default_factory=list)  # brave, serper, tavily, searxng, openalex, crossref
    llm_queries: bool = True  # let the model propose extra queries (one small call per claim)
    max_queries_per_claim: int = 8
    max_fetches_per_claim: int = 10
    max_repair_claims: int = 25
    need_sources: int = 1  # stop after this many independent supporting sources

    # Fetching
    concurrency: int = 8
    timeout: float = 20.0
    max_bytes: int = 15_000_000
    allow_private: bool = False  # SSRF guard: block localhost/private IPs unless set
    wayback: bool = True
    retraction_check: bool = True
    email: str | None = None  # polite-pool contact for Crossref/OpenAlex
    user_agent: str = f"saysso/{__version__} (+https://github.com/Brendennago/saysso)"

    # Cache
    cache_dir: str | None = None
    no_cache: bool = False

    @classmethod
    def from_env(cls, **overrides: Any) -> "Config":
        cfg = cls(
            model=_env("SAYSSO_MODEL"),
            recheck_model=_env("SAYSSO_RECHECK_MODEL"),
            nli=_env("SAYSSO_NLI"),
            search=[s.strip() for s in (_env("SAYSSO_SEARCH") or "").split(",") if s.strip()],
            price_in=_env_float("SAYSSO_PRICE_IN"),
            price_out=_env_float("SAYSSO_PRICE_OUT"),
            email=_env("SAYSSO_EMAIL"),
            cache_dir=_env("SAYSSO_CACHE_DIR"),
        )
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        if cfg.email and "mailto:" not in cfg.user_agent:
            cfg.user_agent = f"{cfg.user_agent[:-1]}; mailto:{cfg.email})"
        return cfg

    def public(self) -> dict[str, Any]:
        """Config as recorded in reports (no secrets live here, but keep it tidy)."""
        d = asdict(self)
        d.pop("user_agent", None)
        d.pop("cache_dir", None)
        return d
