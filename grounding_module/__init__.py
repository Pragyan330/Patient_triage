"""Patient triage - grounding module.

Takes the initial assessment produced upstream (a form filled at reception,
turned into a schema by an LLM) and returns a triage judgement in which every
clinical claim is either computed deterministically or quoted from a protocol
document with a page number a nurse can turn to.

    from grounding_module import ground

    result = ground(initial_assessment_dict)
    result["grounded_esi"]        # 1 (most urgent) .. 5
    result["concerns"][0]["evidence"]   # verbatim quotes + printed page numbers
    result["_audit"]              # what the verifier had to strip or repair

Design notes worth knowing before changing anything:

* The NEWS2 score is computed in `news2.py`, never asked of the model. The
  observation chart is a spatial grid that neither text extraction nor OCR can
  recover, so the arithmetic cannot be grounded by retrieval - and a model
  asked to do it gets it wrong in ways that look right.
* NEWS2 refuses to score anyone under 16. Adult bands read normal infant
  physiology as extreme; a well 3-month-old scores 10.
* Page numbers are the ones printed on the paper, derived per document by
  `pagemap.py`. A fixed offset does not work - NEWS2 numbers its front matter
  in roman numerals.
* Everything the model emits is verified against the retrieved text. See
  `grounder.Grounder._verify`.
"""
from __future__ import annotations

from .config import Config
from .grounder import EvidenceBlock, Grounder
from .schema import Evidence, Grounded, InitialAssessment

__all__ = ["Config", "EvidenceBlock", "Grounder", "Evidence", "Grounded",
           "InitialAssessment", "ground"]

__version__ = "0.1.0"

_default: Grounder | None = None


def ground(initial: dict, *, config: Config | None = None) -> dict:
    """Ground one initial assessment. Reuses a shared Grounder across calls.

    The corpus is parsed once on first use (roughly a second); pass an explicit
    `config` to get an isolated instance instead of the shared one.
    """
    global _default
    if config is not None:
        return Grounder(config).run(initial)
    if _default is None:
        _default = Grounder()
    return _default.run(initial)
