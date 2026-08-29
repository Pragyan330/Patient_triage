"""Grounding pipeline: an initial assessment in, a cited triage output out.

    initial schema
        |-- lookups[]  -> BM25 retrieval over the protocol corpus
        |                 -> printed page numbers -> verbatim page text
        |-- vitals     -> news2.py (deterministic; never the model)
        '-- both       -> Mistral structured output
                          -> verifier: strips or repairs every unsupported claim

The verifier is the load-bearing part. Structured output guarantees the shape
of the JSON and nothing about its truth: given a required evidence[] field and
no evidence, the model invents plausible page numbers. Everything it emits is
checked back against the retrieved text before it leaves this module.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass

from . import news2
from .config import Config, load_dotenv_if_present
from .retrieve import LocalRetriever
from .schema import Grounded

log = logging.getLogger(__name__)

# Printed page numbers come from pagemap.py, which reads the folio off each
# page. A fixed offset does not work: NEWS2 numbers 23 pages of front matter
# in roman numerals before restarting at arabic 1.


def _normalise(text: str) -> str:
    """Fold away differences that are not paraphrase.

    A quote lifted mid-sentence gets its first letter capitalised, and PDF
    text carries typographic dashes and quotes. Comparing raw strings flags
    those as paraphrase and buries the real misquotes in noise.
    """
    text = unicodedata.normalize("NFKD", text).lower()
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                 ("–", "-"), ("—", "-"), ("−", "-")):
        text = text.replace(a, b)
    # list glyphs are layout, not wording
    text = re.sub(r"[•▪◦‣⇒➔→]", " ", text)
    # PDFs break words across lines: "decision- maker" is the same word as
    # "decision-maker". Letters only, so numeric ranges like 21-24 survive.
    text = re.sub(r"(?<=[a-z])-\s*(?=[a-z])", "", text)
    return " ".join(text.split())


_SENTENCE_SPLIT = re.compile(r"(?<=[.;:])\s+|\s*[•▪◦‣⇒]\s*")


def _spans(page_text: str) -> list[str]:
    """Candidate contiguous quotes: sentences, clauses and bullets.

    Layout newlines are collapsed first. A PDF wraps mid-sentence, so treating
    "\\n" as a boundary chopped real sentences in half and the repair then
    snapped quotes onto fragments starting mid-clause.
    """
    flat = " ".join(page_text.split())
    return [p.strip() for p in _SENTENCE_SPLIT.split(flat) if len(p.strip()) >= 25]


def repair_quote(criterion: str, page_text: str, floor: float = 0.85) -> str | None:
    """Snap a near-miss quote onto the real span it came from.

    The model reliably splices - lifting "An aggregate NEW score of" from one
    clause and "7 or more should trigger a high-level alert" from the next,
    which states a rule the document does not. Telling it not to in the prompt
    did not stop it. Rather than drop the citation and leave the nurse with
    nothing, find the actual span and substitute the document's own words.

    Scored by how much of the SPAN the model's text vouches for, not by
    two-way similarity: a short span fully contained in the quote is the thing
    the model was reading, while similarity alone prefers whichever neighbour
    happens to be longest. Returns None when nothing is close enough.
    """
    from difflib import SequenceMatcher

    target = _normalise(criterion)
    if not target:
        return None

    # Two ways a span can be the right answer, and they pull in opposite
    # directions:
    #   containment - the span holds essentially the whole quote, so the model
    #                 quoted a prefix of a longer sentence. Return the sentence.
    #   coverage    - the span sits entirely inside the quote, so the model
    #                 spliced this span together with its neighbours. Return
    #                 just this span.
    # Scoring on only one of them drops half the real cases.
    by_containment, best_contain = None, 0.0
    by_coverage, best_cov, best_len = None, 0.0, 0

    for span in _spans(page_text):
        norm_span = _normalise(span)
        if not norm_span:
            continue
        matcher = SequenceMatcher(None, target, norm_span, autojunk=False)
        matched = sum(block.size for block in matcher.get_matching_blocks())

        containment = matched / len(target)
        if containment > best_contain:
            by_containment, best_contain = span, containment

        coverage = matched / len(norm_span)
        if coverage > best_cov or (coverage == best_cov and len(norm_span) > best_len):
            by_coverage, best_cov, best_len = span, coverage, len(norm_span)

    if best_contain >= 0.9:
        return by_containment
    if best_cov >= floor:
        return by_coverage
    return None


@dataclass
class EvidenceBlock:
    handle: str
    document: str
    page: int          # physical PDF page, used internally
    printed: int       # page number printed on the paper - what gets cited
    text: str

    def render(self) -> str:
        return (f"<evidence document=\"{self.document}\" page=\"{self.printed}\">\n"
                f"{self.text.strip()}\n</evidence>")


SYSTEM = """You are the grounding step of a hospital triage assistant. A nurse reads your output.

You are given: (1) evidence blocks retrieved verbatim from triage protocol PDFs, each tagged with
its document name and page number, and (2) a NEWS2 score already computed deterministically from
the patient's vitals.

Hard rules:
- Every evidence[] entry MUST come from a supplied <evidence> block. Copy the document name and
  page number exactly as given in the block's attributes.
- criterion MUST be ONE CONTIGUOUS RUN of characters copied from inside that block - a single
  sentence, or a single bullet. Never join text across a semicolon, a bullet, a heading, or two
  sentences: splicing "An aggregate NEW score of" onto "7 or more should trigger a high-level
  alert" invents a rule the document does not state. Quote less rather than stitching.
- If no supplied block supports a concern, give that concern an empty evidence list. An empty list
  is correct and useful; an invented citation is a patient-safety failure.
- Never recompute or second-guess the NEWS2 total. Use the number given.
- If the NEWS2 block says applicable is false, NEWS2 does not apply to this patient. Do not
  quote a NEWS2 threshold, do not raise a concern about the score, and do not let it influence
  the ESI. Say in the nurse_summary which tool should be used instead.
- Before writing that a value is high, low, or outside a threshold, compare the two numbers.
  A value of 168 against a threshold of ">180" has NOT exceeded it. If a vital sits inside the
  cited range, say so plainly rather than reaching for the alarming reading.
- Where two criteria in the corpus disagree at a boundary (an age cut-off, a temperature cut-off),
  cite both, take the more cautious level, and name the disagreement in the nurse_summary.
- concern is plain language a patient would understand. clinical_shorthand is the clinical tag.
- ESI runs 1 (most urgent) to 5 (least). ESI 1 is ONLY for a patient who needs an immediate
  life-saving intervention right now - airway, ventilation, CPR, immediate haemodynamic rescue.
  A high NEWS2, a high-risk history, or "emergency response" wording is ESI 2, not ESI 1: an
  emergency clinical review is not the same thing as a life-saving intervention. If the patient
  is maintaining their own airway and breathing, they are not ESI 1.
- time_to_treatment_minutes: ONLY a figure stated in one of the evidence blocks you cite for
  that concern, and cite the block that states it. Your own clinical knowledge does not count:
  if no supplied block gives a time, the value is null. A number with no citation behind it is
  the same failure as an invented page - worse, because there is no quote to check it against.
"""


# The upstream schema decides what to look up, and it often asks only about
# NEWS2 - leaving the model to pick an ESI level with no ESI text in front of
# it. That is how a NEWS2 of 7 became "ESI 1". Always retrieve the level
# definitions so the acuity decision has something to stand on.
ESI_LEVEL_ANCHOR = {
    "intent": "esi_level_definition",
    "question": ("Which patients require immediate life-saving intervention and are ESI level 1, "
                 "and which are high-risk or confused and therefore ESI level 2?"),
    "presentation_terms": ["immediate life-saving intervention", "level 1", "level 2",
                           "high risk situation", "should not wait"],
    "prefer_document": "esi",
    "answer_shape": "criterion",
    "priority": 99,
}


class Grounder:
    """Turns an initial assessment into a grounded, cited triage output."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        load_dotenv_if_present(self.config)
        key = self.config.api_key()

        self.retriever = LocalRetriever(self.config)
        if not self.retriever.n:
            missing = ", ".join(self.retriever.missing) or str(self.config.corpus_dir)
            raise FileNotFoundError(
                f"No readable protocol PDFs found. Expected: {missing}. "
                f"Run scripts/fetch_corpus.py to download them.")
        log.info("retriever ready: %d pages (%d front-matter pages skipped)",
                 self.retriever.n, self.retriever.skipped_front_matter)

        from mistralai.client import Mistral
        self.llm = Mistral(api_key=key)
        self.retrieval_ms = 0
        self.last_blocks: list[EvidenceBlock] = []

    @property
    def model(self) -> str:
        return self.config.model

    # ------------------------------------------------------------ retrieval

    def retrieve(self, lookup: dict) -> list[EvidenceBlock]:
        question = lookup.get("question") or ""
        terms = lookup.get("presentation_terms") or []
        handle = (lookup.get("prefer_document") or "").lower().strip() or None

        started = time.perf_counter()
        hits = self.retriever.search(
            question, terms,
            handle=handle if handle in self.config.documents else None,
            k=self.config.pages_per_lookup,
            answer_shape=lookup.get("answer_shape"))
        log.debug("[%s] %.0f ms -> pages %s", handle or "all",
                  (time.perf_counter() - started) * 1000,
                  [p.printed for p, _ in hits] or "none")

        return [EvidenceBlock(p.handle, self.config.citation_name(p.handle),
                              p.page, p.printed, p.text[:self.config.chars_per_page])
                for p, _ in hits]

    # ------------------------------------------------------------ generation

    def run(self, initial: dict, patient_id: str | None = None) -> dict:
        wall = time.perf_counter()
        # upstream now carries patient_id in the schema; the argument is only
        # a fallback for callers that have one and a schema that does not
        patient_id = initial.get("patient_id") or patient_id or "P-000"

        score = news2.from_schema(initial)

        blocks: list[EvidenceBlock] = []
        seen: set[tuple[str, int]] = set()
        lookups = sorted(initial.get("lookups") or [], key=lambda x: x.get("priority", 99))
        lookups = lookups + [ESI_LEVEL_ANCHOR]
        for lookup in lookups:
            for b in self.retrieve(lookup):
                if (b.handle, b.page) not in seen:
                    seen.add((b.handle, b.page))
                    blocks.append(b)
        self.retrieval_ms = int((time.perf_counter() - wall) * 1000)
        self.last_blocks = blocks

        evidence_text = "\n\n".join(b.render() for b in blocks) or "(no evidence retrieved)"
        user = (
            f"PATIENT ID: {patient_id}\n\n"
            f"INITIAL ASSESSMENT (upstream LLM):\n{json.dumps(initial, indent=2)}\n\n"
            f"NEWS2, COMPUTED DETERMINISTICALLY - use this total, do not recalculate:\n"
            f"{json.dumps(score.as_dict(), indent=2)}\n\n"
            f"RETRIEVED EVIDENCE - the only citations you may use:\n{evidence_text}"
        )

        response = self._parse_with_retry(
            model=self.config.model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            response_format=Grounded,
            temperature=self.config.temperature,
            random_seed=self.config.random_seed,
        )
        parsed: Grounded = response.choices[0].message.parsed
        out = parsed.model_dump()

        audit = self._verify(out, blocks)
        out["news2"] = score.as_dict()
        out["retrieval_ms"] = self.retrieval_ms
        out["_audit"] = {
            "model": self.config.model,
            "retriever": "bm25-local",
            "evidence_blocks_supplied": len(blocks),
            "pages_supplied": sorted({f"{b.handle}:{b.printed}" for b in blocks}),
            "evidence_blocks": [
                {"handle": b.handle, "document": b.document, "page": b.printed,
                 "snippet": " ".join(b.text.split())[:400]} for b in blocks],
            "lookups": [
                {"intent": lk.get("intent"), "question": lk.get("question"),
                 "prefer_document": lk.get("prefer_document"),
                 "answer_shape": lk.get("answer_shape")} for lk in lookups],
            "tokens": {"in": response.usage.prompt_tokens, "out": response.usage.completion_tokens},
            "total_ms": int((time.perf_counter() - wall) * 1000),
            **audit,
        }
        return out

    def _parse_with_retry(self, **kwargs):
        """Mistral 429s readily on a low tier; back off rather than lose the run."""
        tries = self.config.max_retries
        delay = self.config.initial_backoff_seconds
        for attempt in range(1, tries + 1):
            try:
                return self.llm.chat.parse(**kwargs)
            except Exception as exc:
                retryable = "429" in str(exc) or "rate limit" in str(exc).lower()
                if not retryable or attempt == tries:
                    raise
                log.warning("rate limited, retry %d/%d in %.0fs", attempt, tries - 1, delay)
                time.sleep(delay)
                delay *= 2

    # ------------------------------------------------------------ verifier

    def _verify(self, out: dict, blocks: list[EvidenceBlock]) -> dict:
        """Strip citations that did not come from a supplied block.

        This exists because the model demonstrably invents plausible page
        numbers to satisfy a required field.
        """
        allowed = {(b.document, b.printed): b.text for b in blocks}
        rejected: list[dict] = []
        unquoted: list[dict] = []
        repaired: list[dict] = []
        repaged: list[dict] = []

        for concern in out.get("concerns", []):
            kept = []
            for ev in concern.get("evidence", []):
                key = (ev.get("document"), ev.get("page"))
                if key not in allowed:
                    # The quote may still be real and simply attributed to the
                    # wrong supplied page - the model sees several at once and
                    # mixes up neighbours. Correct the page rather than binning
                    # a citation the corpus genuinely supports.
                    moved = self._find_page(ev.get("criterion") or "", blocks)
                    if moved is not None:
                        repaged.append({"document": ev.get("document"),
                                        "cited": ev.get("page"), "actual": moved})
                        ev["page"] = moved
                        key = (ev.get("document"), moved)
                    else:
                        rejected.append({"reason": "page not retrieved", **ev})
                        continue
                needle = _normalise(ev.get("criterion") or "")
                haystack = _normalise(allowed[key])
                if needle and needle not in haystack:
                    fixed = repair_quote(ev.get("criterion") or "", allowed[key])
                    if fixed:
                        repaired.append({"document": ev.get("document"), "page": ev.get("page"),
                                         "was": (ev.get("criterion") or "")[:110],
                                         "now": fixed[:110]})
                        ev["criterion"] = fixed
                        kept.append(ev)
                        continue
                    # nothing close enough on the page: drop it rather than
                    # leave a reworded quote wearing a real page number
                    unquoted.append({"reason": "criterion not verbatim on page",
                                     "document": ev.get("document"), "page": ev.get("page"),
                                     "criterion": (ev.get("criterion") or "")[:120]})
                    continue
                kept.append(ev)
            concern["evidence"] = kept

        bad_claims = self._check_numeric_claims(out)
        bad_timing = self._check_timing(out)

        # "clean" describes the JSON we emit, not the road to it. A dropped or
        # repaired citation means a guard fired and the output is safe; an
        # invented page or a backwards comparison means it is not.
        return {"citations_rejected": rejected, "citations_not_verbatim": unquoted,
                "citations_repaired": repaired, "citations_repaged": repaged,
                "unsupported_comparisons": bad_claims,
                "unsupported_timing": bad_timing,
                "interventions": len(unquoted) + len(repaired) + len(repaged) + len(bad_timing),
                "clean": not rejected and not bad_claims}

    @staticmethod
    def _find_page(criterion: str, blocks: list[EvidenceBlock]) -> int | None:
        """Which supplied page does this quote actually appear on?"""
        needle = _normalise(criterion)
        if len(needle) < 25:
            return None
        for b in blocks:
            if needle in _normalise(b.text):
                return b.printed
        return None

    # Times the corpus actually states, e.g. "an ECG should be performed
    # within 10 minutes of patient arrival".
    _TIME_PATTERNS = [
        (re.compile(r"within\s+(\d+)\s*min", re.I), 1),
        (re.compile(r"within\s+(\d+)\s*hour", re.I), 60),
        (re.compile(r"within\s+(?:one|an|1)\s+hour", re.I), None),   # -> 60
        (re.compile(r"(\d+)\s*minutes?\s+of\s+(?:patient\s+)?arrival", re.I), 1),
    ]

    def _check_timing(self, out: dict) -> list[dict]:
        """Null out any time target the cited evidence does not state.

        criterion is verified verbatim, but time_to_treatment_minutes was
        walking straight past the verifier - a bare number with no quote to
        check it against. The sepsis "60 minutes" was pure recall: the word
        "antibiotic" does not appear in NEWS2 at all.
        """
        stripped: list[dict] = []
        for concern in out.get("concerns", []):
            claimed = concern.get("time_to_treatment_minutes")
            if claimed is None:
                continue

            supported: set[int] = set()
            for ev in concern.get("evidence", []):
                text = ev.get("criterion") or ""
                for pattern, unit in self._TIME_PATTERNS:
                    for m in pattern.finditer(text):
                        supported.add(60 if unit is None else int(m.group(1)) * unit)

            if int(claimed) not in supported:
                stripped.append({
                    "concern": concern.get("clinical_shorthand") or concern.get("concern", "")[:60],
                    "claimed_minutes": claimed,
                    "supported_by_citations": sorted(supported) or None,
                })
                concern["time_to_treatment_minutes"] = None
        return stripped

    # ------------------------------------------------------------ inference

    def _check_numeric_claims(self, out: dict) -> list[dict]:
        """Catch conclusions that contradict the threshold they cite.

        A verbatim quote proves nothing about the inference drawn from it. The
        observed failure: a cited table reads ">180 >50" for an infant, the
        patient is HR 168 / RR 48, and the summary says the vitals "exceed
        pediatric danger-zone thresholds". Real page, real quote, backwards
        conclusion - and nothing else in the pipeline looks at it.
        """
        # An exceedance word inside a negation is the model doing the right
        # thing - "HR 168 does NOT exceed >180" is the correct reading, and
        # flagging it buries the real failures under false positives.
        exceeds = re.compile(
            r"(?<!not )(?<!do not )(?<!does not )(?<!did not )"
            r"\b(?:exceed(?:s|ed|ing)?|above|outside|beyond|greater than|higher than)\b",
            re.I)
        negated = re.compile(
            r"\b(?:not|no|never|nor|without|below|within|under|inside|neither)\b[^.]{0,40}?"
            r"\b(?:exceed\w*|above|outside|beyond|greater than|higher than)\b", re.I)
        threshold_re = re.compile(r"(?:>|≥|greater than\s+|at least\s+)\s*(\d+(?:\.\d+)?)")
        number_re = re.compile(r"\b(\d+(?:\.\d+)?)\b")

        problems: list[dict] = []
        for concern in out.get("concerns", []):
            summary = concern.get("nurse_summary") or ""
            # split into clauses so one negated clause does not excuse an
            # unnegated claim elsewhere in the same summary
            claims = [cl for cl in re.split(r"[.;]", summary)
                      if exceeds.search(cl) and not negated.search(cl)]
            if not claims:
                continue

            thresholds = []
            for ev in concern.get("evidence", []):
                thresholds += [float(x) for x in threshold_re.findall(ev.get("criterion") or "")]
            if not thresholds:
                continue

            # Strip the thresholds the summary quotes back at us. Without this
            # ">180" contributes 180 as if it were a patient value, and 180<=180
            # flags every correctly-worded summary.
            claim_text = threshold_re.sub(" ", " ".join(claims))
            for raw in number_re.findall(claim_text):
                value = float(raw)
                # pair each asserted value with the threshold of comparable
                # magnitude; anything else is a different parameter entirely
                near = [t for t in thresholds if t and 0.5 <= value / t <= 2.0]
                if near and all(value <= t for t in near):
                    problems.append({
                        "reason": "summary claims exceedance but the cited threshold is not met",
                        "value": value,
                        "cited_threshold": min(near, key=lambda t: abs(t - value)),
                        "concern": concern.get("clinical_shorthand") or concern.get("concern", "")[:60],
                        "summary": " ".join(claims)[:140],
                    })
        return problems
