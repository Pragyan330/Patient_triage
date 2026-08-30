"""Hold re-triage citations to the same standard as grounded ones.

`retriage_loop` carries hardcoded citations. They are clinically sound, but
they were written from memory rather than read out of the corpus, and it
shows:

    claimed: NEWS2 p.14 - "An aggregate score of 5 or above is the threshold
             for urgent clinical review."
    actual:  NEWS2 p.29 - "An aggregate NEW score of 5 or more is a key
             threshold that should trigger an urgent clinical review"

    claimed: NEWS2 p.14 - "A score of 3 in any single parameter triggers
             urgent review regardless of aggregate total."
    actual:  no verbatim source. The rule is real but lives in the thresholds
             table on p.30, which extracts as unaligned cells - the same
             table problem that keeps the NEWS2 scorer in code.

A nurse cannot tell a checked citation from an unchecked one by looking, so
everything the queue shows goes through here first. Corrections are applied,
unverifiable claims are marked rather than quietly deleted: the escalation
still stands on its own reasoning, it just is not carrying a page number it
cannot honour.
"""
from __future__ import annotations

import functools
import logging

from grounding_module import Config
from grounding_module.grounder import repair_quote
from grounding_module.grounder import _normalise
from grounding_module.retrieve import LocalRetriever

log = logging.getLogger(__name__)

# Looser than the grounding module's floor: these are paraphrases written from
# memory, not spliced quotes, so they start further from the source text.
REPAIR_FLOOR = 0.55
SEARCH_PAGES = 4


@functools.lru_cache(maxsize=1)
def _retriever() -> LocalRetriever:
    return LocalRetriever(Config())


def _handle_for(document: str) -> str | None:
    doc = (document or "").lower()
    if "news" in doc:
        return "news2"
    if "esi" in doc:
        return "esi"
    return None


def verify_citation(citation: dict) -> dict:
    """Return the citation with document/page/criterion corrected where possible.

    Adds `verified`, and on failure `verification_note`.
    """
    criterion = citation.get("criterion") or ""
    handle = _handle_for(citation.get("document", ""))
    retriever = _retriever()

    # Already exact on the page it names?
    claimed_page = citation.get("page")
    for page in retriever.pages:
        if page.printed == claimed_page and (handle is None or page.handle == handle):
            if _normalise(criterion) in _normalise(page.text):
                return {**citation, "verified": True}

    # Otherwise go looking for it.
    hits = retriever.search(criterion, [], handle=handle, k=SEARCH_PAGES,
                            answer_shape="numeric_threshold")
    for page, _score in hits:
        fixed = repair_quote(criterion, page.text, floor=REPAIR_FLOOR)
        if fixed:
            corrected = {
                **citation,
                "page": page.printed,
                "criterion": fixed,
                "verified": True,
            }
            if page.printed != claimed_page:
                corrected["corrected_from_page"] = claimed_page
                log.info("citation repaged %s -> %s", claimed_page, page.printed)
            return corrected

    return {
        **citation,
        "verified": False,
        "verification_note": (
            "No verbatim source found in the corpus for this wording. The rule "
            "may still be correct - the NEWS2 thresholds table does not survive "
            "text extraction - but this page number is not evidence for it."),
    }


def verify_event(event: dict) -> dict:
    """Verify every citation on a re-triage event, in place on a copy."""
    citations = event.get("evidence") or []
    if not citations:
        return event

    checked = [verify_citation(c) for c in citations]
    unverified = [c for c in checked if not c.get("verified")]
    repaged = [c for c in checked if c.get("corrected_from_page") is not None]

    out = {**event, "evidence": checked}
    out["citation_audit"] = {
        "checked": len(checked),
        "verified": len(checked) - len(unverified),
        "repaged": len(repaged),
        "unverified": len(unverified),
    }
    if unverified:
        # Surface it on the line the nurse actually reads.
        out["nurse_summary"] = (
            f"{event.get('nurse_summary', '')} "
            f"[{len(unverified)} citation(s) could not be verified against the corpus]"
        ).strip()
    return out
