"""Data model. Everything that ends up in the JSON evidence format lives here."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    WRONG_SOURCE = "WRONG-SOURCE"
    UNREACHABLE = "SOURCE-UNREACHABLE"
    UNCERTAIN = "UNCERTAIN"

    @property
    def label(self) -> str:
        return _LABELS[self.value]

    @classmethod
    def parse(cls, value: str) -> "Verdict":
        v = (value or "").strip().upper().replace("_", "-").replace(" ", "-")
        aliases = {
            "SUPPORTS": "SUPPORTED",
            "SUPPORT": "SUPPORTED",
            "PARTIALLY-SUPPORTED": "PARTIAL",
            "PARTIALLY": "PARTIAL",
            "NOT-SUPPORTED": "UNSUPPORTED",
            "NOT-ENOUGH-INFO": "UNSUPPORTED",
            "NEI": "UNSUPPORTED",
            "CONTRADICTS": "CONTRADICTED",
            "REFUTED": "CONTRADICTED",
            "REFUTES": "CONTRADICTED",
            "UNREACHABLE": "SOURCE-UNREACHABLE",
        }
        v = aliases.get(v, v)
        for member in cls:
            if member.value == v:
                return member
        raise ValueError(f"unknown verdict: {value!r}")


_LABELS = {
    "SUPPORTED": "Supported",
    "PARTIAL": "Partly supported",
    "UNSUPPORTED": "Not supported",
    "CONTRADICTED": "Contradicted",
    "WRONG-SOURCE": "Wrong source",
    "SOURCE-UNREACHABLE": "Source unreachable",
    "UNCERTAIN": "Needs review",
}

FAILING = {Verdict.UNSUPPORTED, Verdict.CONTRADICTED, Verdict.WRONG_SOURCE}


@dataclass
class Citation:
    id: str
    url: str | None = None
    title: str | None = None
    raw: str = ""
    cited_text: str | None = None  # the quote the generator itself attached (e.g. Claude API citations)


@dataclass
class Claim:
    id: str
    text: str  # cleaned claim text, markers removed
    start: int  # offsets into the original report text
    end: int
    line: int
    citation_ids: list[str]
    raw: str = ""
    inherited: bool = False


@dataclass
class Link:
    href: str
    text: str
    start: int
    end: int


@dataclass
class SourceDoc:
    url: str
    final_url: str | None = None
    ok: bool = False
    status: int | None = None
    reason: str | None = None
    content_type: str | None = None
    title: str | None = None
    text: str = ""
    published: str | None = None
    doi: str | None = None
    sha256: str | None = None
    fetched_at: str | None = None
    archived_from: str | None = None
    links: list[Link] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = to_jsonable(self)
        d.pop("from_cache", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceDoc":
        d = dict(d)
        d["links"] = [Link(**x) for x in d.get("links", [])]
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def summary(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "content_type": self.content_type,
            "title": self.title,
            "published": self.published,
            "doi": self.doi,
            "sha256": self.sha256,
            "chars": len(self.text),
            "fetched_at": self.fetched_at,
            "archived_from": self.archived_from,
        }


@dataclass
class Evidence:
    quote: str
    start: int  # character offsets into the stored source text (see sources[url].sha256)
    end: int
    kind: str = "support"  # support | contradiction | closest
    prefix: str = ""
    suffix: str = ""
    match_tier: int = 1  # 1 exact, 2 exact around an ellipsis, 3 fuzzy, 0 passage-level only
    match_score: float = 100.0
    passage_id: str | None = None
    fragment_url: str | None = None
    verified: bool = True  # True when located deterministically in the stored source text


@dataclass
class Validity:
    tier: str = "UNKNOWN"  # HIGH | MEDIUM | LOW | INVALID | UNKNOWN
    score: float = 0.5
    domain_class: str = "other"
    flags: list[str] = field(default_factory=list)
    doi: str | None = None
    retraction: str | None = None
    published: str | None = None


@dataclass
class SourceCheck:
    citation_id: str
    url: str | None
    verdict: Verdict
    confidence: float
    method: str  # verbatim | lexical | number-gate | nli | llm | fetch | parse
    reason: str = ""
    evidence: Evidence | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    validity: Validity | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class SearchStep:
    strategy: str
    query: str
    provider: str
    results: list[str] = field(default_factory=list)


@dataclass
class Repair:
    attempted: bool = False
    steps: list[SearchStep] = field(default_factory=list)
    candidates: list[SourceCheck] = field(default_factory=list)
    found: SourceCheck | None = None
    stop_reason: str = ""
    fetches: int = 0
    queries: int = 0


@dataclass
class ClaimResult:
    claim: Claim
    verdict: Verdict
    confidence: float
    checks: list[SourceCheck] = field(default_factory=list)
    repair: Repair | None = None
    notes: list[str] = field(default_factory=list)
    passes: bool = False


@dataclass
class RunReport:
    input: str
    generated_at: str
    version: str
    claims: list[ClaimResult]
    sources: dict[str, dict[str, Any]]
    stats: dict[str, Any]
    usage: dict[str, Any]
    config: dict[str, Any]
    uncited: list[dict[str, Any]] = field(default_factory=list)
    schema: str = "saysso/1"

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return obj
