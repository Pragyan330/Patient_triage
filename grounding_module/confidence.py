"""How much the assistant should be believed, and what to do when it should not.

Every score leaves this module with a confidence attached. A number with no
stated uncertainty invites a tired clinician to read it as fact, and the
inputs here are routinely incomplete: half of arrivals have no prior record,
vitals go unmeasured, and a first-time patient may offer nothing but what is
observed in the moment.

THE ASYMMETRY
-------------
Missing a critical patient is categorically worse than over-prioritising a
minor one. One is a death, the other is a wait. So this does not merely
*report* uncertainty - it acts on it. Low confidence escalates the patient by
one level and says so on the record.

That is a deliberate bias, not an accuracy bug. A model tuned for average
accuracy would sit on the fence when data is thin; under-triage is exactly
where the harm lives, so the thin-data case resolves upward.

The escalation stops at ESI 2. ESI 1 means "needs a life-saving intervention
right now" and is a claim about the patient, not about our certainty -
manufacturing it out of missing paperwork would cry wolf at the one level that
must never be noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Weight per problem. Tuned so that any two significant gaps drop a patient
# out of "high", and a genuinely bare record lands in "low".
PENALTY = {
    "vitals_missing": 0.10,        # per unmeasured NEWS2 parameter
    "news2_not_applicable": 0.25,  # no validated physiological score for this age
    "no_citations": 0.25,          # nothing in the corpus supported the concern
    "citation_dropped": 0.10,      # a quote failed verification and was removed
    "no_prior_record": 0.15,       # first attendance, nothing to compare against
    "age_unknown": 0.20,           # cannot age-gate, cannot pick the right bands
    "gate_incomplete": 0.15,       # red-flag screen ran without its inputs
}

HIGH, MODERATE, LOW = "high", "moderate", "low"
HIGH_FLOOR, MODERATE_FLOOR = 0.75, 0.45

# Uncertainty may push a patient to ESI 2. It may never manufacture an ESI 1.
MOST_URGENT_FROM_UNCERTAINTY = 2


@dataclass
class Confidence:
    level: str
    score: float
    reasons: list[str] = field(default_factory=list)
    escalated_for_uncertainty: bool = False
    esi_before_uncertainty: int | None = None

    def as_dict(self) -> dict:
        d = {
            "level": self.level,
            "score": round(self.score, 2),
            "reasons": self.reasons or ["All expected inputs present."],
            "escalated_for_uncertainty": self.escalated_for_uncertainty,
        }
        if self.escalated_for_uncertainty:
            d["esi_before_uncertainty"] = self.esi_before_uncertainty
        return d


def _has_prior_record(initial: dict) -> bool:
    """Did this patient arrive with any history at all?

    The schema has no explicit flag, so fall back to whether anything
    history-shaped was recorded. Absence of all of it is the zero-history
    case: a first-time patient we know nothing about beyond this moment.
    """
    if isinstance(initial.get("has_prior_record"), bool):
        return initial["has_prior_record"]
    for key in ("medication_effect", "allergy_note", "known_conditions",
                "medications", "prior_history"):
        value = initial.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, dict)) and value:
            return True
    return False


def assess(initial: dict, grounded: dict, news2: dict,
           gate: dict | None = None) -> Confidence:
    """Score how much of what we needed we actually had."""
    score = 1.0
    reasons: list[str] = []

    missing = [m for m in (news2.get("missing") or []) if m != "air_or_oxygen"]
    if missing:
        score -= PENALTY["vitals_missing"] * len(missing)
        reasons.append(f"{len(missing)} vital sign(s) not measured: {', '.join(missing)}.")

    if not news2.get("applicable", True):
        score -= PENALTY["news2_not_applicable"]
        reasons.append("NEWS2 does not apply at this age, so there is no validated "
                       "physiological score behind the level.")

    citations = sum(len(c.get("evidence") or []) for c in grounded.get("concerns", []))
    if citations == 0:
        score -= PENALTY["no_citations"]
        reasons.append("No protocol text in the corpus supported this presentation.")

    audit = grounded.get("_audit") or {}
    dropped = len(audit.get("citations_not_verbatim") or []) + \
        len(audit.get("citations_rejected") or [])
    if dropped:
        score -= PENALTY["citation_dropped"] * min(dropped, 3)
        reasons.append(f"{dropped} citation(s) failed verification and were removed.")

    if not _has_prior_record(initial):
        score -= PENALTY["no_prior_record"]
        reasons.append("No prior record: nothing to compare today's presentation against.")

    if initial.get("age") is None and news2.get("applicable", True) and not missing:
        pass  # age was parsed from prose successfully enough to score
    if initial.get("age") is None and not news2.get("applicable", True):
        score -= PENALTY["age_unknown"]
        reasons.append("Age not recorded as a number.")

    if gate and (gate.get("missing_fields") or gate.get("low_confidence")):
        score -= PENALTY["gate_incomplete"]
        fields = ", ".join(gate.get("missing_fields") or []) or "structured fields"
        reasons.append(f"Red-flag screen ran without: {fields}.")

    score = max(0.0, min(1.0, score))
    level = HIGH if score >= HIGH_FLOOR else MODERATE if score >= MODERATE_FLOOR else LOW
    return Confidence(level=level, score=score, reasons=reasons)


def apply_to(grounded: dict, confidence: Confidence) -> dict:
    """Attach the confidence, and escalate if it is low.

    Escalating on uncertainty is the whole point: when we cannot tell, the
    patient is seen sooner rather than later. The original level is kept on
    the record so a clinician can see what the data alone said, separately
    from what our doubt did to it.
    """
    esi = grounded.get("grounded_esi")

    if (confidence.level == LOW and isinstance(esi, int)
            and esi > MOST_URGENT_FROM_UNCERTAINTY):
        confidence.escalated_for_uncertainty = True
        confidence.esi_before_uncertainty = esi
        new_esi = esi - 1
        grounded["grounded_esi"] = new_esi
        for concern in grounded.get("concerns", []):
            if concern.get("final_esi", 9) > new_esi:
                concern["final_esi"] = new_esi
        confidence.reasons.append(
            f"Escalated ESI {esi} to {new_esi} because confidence is low. "
            f"Under-triage is the costlier error, so thin data resolves upward.")

    grounded["confidence"] = confidence.as_dict()
    return grounded
