"""BM25 page retriever over the protocol corpus - no LLM, no index build.

The corpus is a few hundred pages of protocol PDF with clean text layers, and
every lookup arrives pre-planned by the upstream schema, which already names
the document and the search terms. That is a keyword problem, not a semantic
search problem: this runs in milliseconds and cites real page numbers, which
is the whole requirement.

A reasoning-based index (PageIndex) was evaluated first. It fans out 64
concurrent LLM calls to build its tree and never completed indexing on the
project's API tier; the experiment is kept under sim/, not shipped here.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from . import pagemap
from .config import Config

# clinical wording that PDFs spell differently to the intake form
SYNONYMS = {
    "confusion": ["confusion", "confused", "acvpu", "avpu", "delirium", "disorientation"],
    "breathing": ["respiration", "respiratory", "breaths"],
    "escalation": ["escalation", "escalate", "trigger", "threshold", "urgent", "emergency"],
    "low": ["low", "hypotension", "hypotensive"],
    "score": ["score", "aggregate", "scoring"],
}

STOP = set("""a an and are as at be by for from how in into is it of on or that the to what when
which who why with your you adult adults patient patients does do requires require""".split())

TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP and len(t) > 1]


def expand(terms: list[str]) -> dict[str, float]:
    """Query terms -> weight. Originals count full; synonyms count less.

    Returning a dict rather than a list matters: "new confusion" plus the
    AVPU synonyms used to put `confusion` in the bag five times, which
    dragged the ranking onto any page discussing confusion at all.
    """
    weights: dict[str, float] = {}
    for t in terms:
        weights[t] = 1.0
    for t in list(terms):
        for syn in SYNONYMS.get(t, []):
            weights.setdefault(syn, 0.4)
    return weights


# An answer_shape of "numeric_threshold" means the useful page is the one
# stating a rule, not the one discussing the parameter. Reward that shape.
THRESHOLD_PATTERNS = [
    re.compile(r"\b\d+\s+or\s+(?:more|above|greater)\b", re.I),
    re.compile(r"\bscore of \d+\b", re.I),
    re.compile(r"[≥≤><]\s*\d+"),
    re.compile(r"\bthreshold\b", re.I),
    re.compile(r"\btrigger(?:s|ed)?\b", re.I),
]

SHAPE_BOOST = {"numeric_threshold": 1.6, "criterion": 1.3}


@dataclass
class Page:
    handle: str
    page: int          # physical PDF page
    printed: int       # page number printed on the paper - what we cite
    text: str
    tokens: Counter
    length: int


class LocalRetriever:
    """Okapi BM25 over one page = one document."""

    K1 = 1.5
    B = 0.75

    def __init__(self, config: Config):
        import pymupdf

        self.config = config
        self.pages: list[Page] = []
        self.skipped_front_matter = 0
        self.missing: list[str] = []
        for handle in config.documents:
            path = config.document_path(handle)
            if not path.exists():
                self.missing.append(str(path))
                continue
            with pymupdf.open(path) as doc:
                texts = [p.get_text() for p in doc]
            labels = pagemap.build(texts)
            for i, text in enumerate(texts):
                if len(text.strip()) < 80:          # covers, blank pages, pure charts
                    continue
                label = labels.get(i + 1)
                # Front matter is roman-numbered and unmapped pages have no
                # citable number at all. Both are dropped rather than cited
                # with a page a nurse cannot find; the body restates anything
                # the executive summary says.
                if label is None or not label.isdigit():
                    self.skipped_front_matter += 1
                    continue
                toks = tokenize(text)
                self.pages.append(Page(handle, i + 1, int(label), text,
                                       Counter(toks), len(toks)))

        self.avg_len = (sum(p.length for p in self.pages) / len(self.pages)) if self.pages else 1.0
        self.df = Counter()
        for p in self.pages:
            self.df.update(p.tokens.keys())
        self.n = len(self.pages)

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, query: str, terms: list[str] | None = None,
               handle: str | None = None, k: int = 3,
               answer_shape: str | None = None) -> list[tuple[Page, float]]:
        q = expand(tokenize(query) + tokenize(" ".join(terms or [])))
        if not q:
            return []
        pool = [p for p in self.pages if handle is None or p.handle == handle] or self.pages
        boost = SHAPE_BOOST.get(answer_shape or "", 1.0)

        scored = []
        for p in pool:
            s = 0.0
            for term, weight in q.items():
                tf = p.tokens.get(term, 0)
                if not tf:
                    continue
                denom = tf + self.K1 * (1 - self.B + self.B * p.length / self.avg_len)
                s += weight * self._idf(term) * (tf * (self.K1 + 1)) / denom
            if s <= 0:
                continue
            if boost > 1.0 and any(pat.search(p.text) for pat in THRESHOLD_PATTERNS):
                s *= boost
            scored.append((p, s))

        scored.sort(key=lambda x: -x[1])
        return scored[:k]
