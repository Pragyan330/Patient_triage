"""Map PDF page index -> the page number actually printed on the paper.

Neither corpus PDF carries usable /PageLabels: NEWS2 declares a plain 1:1
decimal run (wrong - it has 23 pages of front matter) and the ESI handbook
declares none. A single fixed offset does not work either, because NEWS2
numbers its front matter in roman numerals and restarts at arabic 1 on PDF
page 24 - a blanket offset produced citations like "page -4".

So derive it: read the folio printed in each page's header/footer, keep the
anchors that agree with their neighbours, and interpolate the rest. Works for
any PDF dropped into corpus/ without new constants.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

ARABIC = re.compile(r"^\d{1,3}$")
ROMAN = re.compile(r"^(?=[ivxlcdm]+$)m*(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$", re.I)

ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def roman_to_int(s: str) -> int:
    s = s.lower()
    total, prev = 0, 0
    for ch in reversed(s):
        v = ROMAN_VALUES[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def int_to_roman(n: int) -> str:
    pairs = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
             (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))
    out = []
    for value, sym in pairs:
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


@dataclass
class Anchor:
    pdf_page: int      # 1-based
    kind: str          # "arabic" | "roman"
    value: int


def _folio_candidates(lines: list[str]) -> list[tuple[str, int]]:
    """A folio sits alone on its line, at the very top or very bottom."""
    found = []
    for line in lines[:2] + lines[-2:]:
        token = line.strip()
        if ARABIC.match(token):
            found.append(("arabic", int(token)))
        elif ROMAN.match(token) and len(token) <= 7:
            found.append(("roman", roman_to_int(token)))
    return found


def build(pages_text: list[str]) -> dict[int, str]:
    """pdf page (1-based) -> printed label, e.g. 29 or 'xviii'."""
    anchors: list[Anchor] = []
    for i, text in enumerate(pages_text, start=1):
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            continue
        for kind, value in _folio_candidates(lines):
            anchors.append(Anchor(i, kind, value))

    # A genuine folio has offset (pdf - printed) shared with its neighbours.
    # Numbers that happen to sit alone in body text do not.
    by_kind: dict[str, list[Anchor]] = {}
    for a in anchors:
        by_kind.setdefault(a.kind, []).append(a)

    mapping: dict[int, str] = {}
    for kind, group in by_kind.items():
        offsets = Counter(a.pdf_page - a.value for a in group)
        if not offsets:
            continue
        offset, hits = offsets.most_common(1)[0]
        if hits < 3:                      # too few to trust
            continue
        good = [a for a in group if a.pdf_page - a.value == offset]
        lo, hi = min(a.pdf_page for a in good), max(a.pdf_page for a in good)
        for pdf_page in range(lo, hi + 1):
            printed = pdf_page - offset
            if printed < 1:
                continue
            mapping[pdf_page] = (str(printed) if kind == "arabic"
                                 else int_to_roman(printed))
    return mapping


def label(mapping: dict[int, str], pdf_page: int) -> str:
    """Printed label if known, else the PDF page marked as such."""
    return mapping.get(pdf_page, f"PDF{pdf_page}")
