"""Passage retrieval. Models only ever see the few passages that matter, never whole pages."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, replace

from .normalize import content_terms

_SENT_SPLIT = re.compile(r"(?<=[.!?])(?:\[\d{1,3}\])*[\"'\u201d\u2019)\]]*\s+(?=[\"'\u201c\u2018(\[]?[A-Z0-9])")


@dataclass
class Passage:
    id: str
    start: int
    end: int
    text: str
    score: float = 0.0


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for line in re.finditer(r"[^\n]+", text):
        ls, lt = line.start(), line.group(0)
        pos = 0
        for m in _SENT_SPLIT.finditer(lt):
            if lt[pos : m.start()].strip():
                spans.append((ls + pos, ls + m.start()))
            pos = m.end()
        if lt[pos:].strip():
            spans.append((ls + pos, ls + len(lt)))
    return spans


def chunk(text: str, max_chars: int = 700, hard_max: int = 1100) -> list[Passage]:
    """Sentence-aware windows with a one-sentence overlap so quotes across boundaries survive."""
    sents: list[tuple[int, int]] = []
    for a, b in sentence_spans(text):
        while b - a > hard_max:  # unpunctuated PDF blocks, tables, etc.
            cut = text.rfind(" ", a, a + max_chars)
            cut = cut if cut > a + max_chars // 2 else a + max_chars
            sents.append((a, cut))
            a = cut
        sents.append((a, b))
    passages: list[Passage] = []
    i = 0
    while i < len(sents):
        a = sents[i][0]
        j = i
        while j + 1 < len(sents) and sents[j + 1][1] - a <= max_chars:
            j += 1
        b = sents[j][1]
        passages.append(Passage(id=f"P{len(passages) + 1}", start=a, end=b, text=text[a:b]))
        if j + 1 >= len(sents):
            break
        i = j if j > i else j + 1  # overlap by one sentence
    return passages


class Index:
    """BM25 over passages of one source document."""

    def __init__(self, text: str, k1: float = 1.4, b: float = 0.75):
        self.passages = chunk(text)
        self.tfs = [Counter(content_terms(p.text)) for p in self.passages]
        self.lens = [sum(tf.values()) or 1 for tf in self.tfs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 1.0
        self.df: Counter = Counter()
        for tf in self.tfs:
            self.df.update(tf.keys())
        self.n = len(self.passages)
        self.k1, self.b = k1, b

    def search(self, query: str, k: int = 4, boost_terms: tuple[str, ...] = ()) -> list[Passage]:
        if not self.passages:
            return []
        if self.n <= k:
            return [replace(p, score=1.0) for p in self.passages]
        q = set(content_terms(query))
        scores: list[float] = []
        for tf, length in zip(self.tfs, self.lens):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                idf = math.log(1 + (self.n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / self.avg))
            for t in boost_terms:
                if t in tf:
                    s += 1.5
            scores.append(s)
        ranked = sorted(range(self.n), key=lambda i: scores[i], reverse=True)[:k]
        return [replace(self.passages[i], score=round(scores[i], 3)) for i in ranked if scores[i] > 0]
