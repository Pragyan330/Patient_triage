"""
retriage_loop.py

Core logic for PatientTriage.ai's continuous re-triage loop.

Called once per patient, either:
  (a) when new vitals/observations come in, or
  (b) on a periodic sweep of the waiting queue (no new vitals).

Design rules this function enforces (see project brief section 4b):
  - Escalate-only ratchet: urgency (ESI) can only ever move to a lower
    number (more urgent) automatically. It can never be raised back
    (de-escalated) except by a separate, explicitly logged nurse action
    that is NOT part of this function.
  - Starvation guard: an absolute time limit per severity band, so a
    patient can't be silently skipped just because others look worse.
  - Fail-closed: any malformed/missing input defaults to escalate,
    never to "no finding".

This function is pure: given the same inputs it always returns the same
output. It does not mutate any database — the caller is responsible for
persisting the returned `new_esi_floor` and `last_check_minute`.
"""

from dataclasses import dataclass, field
from typing import Optional, Literal
import json


# Max minutes before a mandatory recheck, by ESI level.
# Adapted from NEWS2 monitoring-frequency guidance (RCP, 2017).
CHECK_THRESHOLD_MINUTES = {1: 0, 2: 10, 3: 30, 4: 60, 5: 120}

# A patient who misses their recheck window by this multiple is
# auto-escalated by the starvation guard, independent of anything else.
STARVATION_GUARD_MULTIPLIER = 2

TriggerType = Literal[
    "vitals_worsening", "starvation_guard", "check_due", "none", "malformed_input"
]


@dataclass
class PatientState:
    patient_id: str
    esi_floor: int              # current urgency floor, 1 (most urgent) to 5
    last_check_minute: int      # simulation/wall-clock minute of the last observation
    news2: int                  # last recorded NEWS2 aggregate score
    single_param_red: bool = False  # True if any single vital scored 3 on its own


@dataclass
class NewVitals:
    news2: Optional[int] = None
    single_param_red: bool = False
    note: str = ""


def retriage_check(
    patient: PatientState,
    current_minute: int,
    new_vitals: Optional[NewVitals] = None,
) -> dict:
    """
    Run one re-triage check for a patient.

    Args:
        patient: the patient's current tracked state.
        current_minute: the current simulation/wall-clock minute.
        new_vitals: pass this when a nurse/monitor has just recorded new
            observations. Omit it (None) for a routine queue sweep with
            no new data.

    Returns:
        A JSON-serializable dict — see MODULE docstring for the fields.
        `previous_esi_floor` / `new_esi_floor` are always present so the
        caller can persist the ratchet result directly.
    """
    minutes_since_check = current_minute - patient.last_check_minute
    threshold = CHECK_THRESHOLD_MINUTES[patient.esi_floor]

    # --- Path 1: new vitals were provided ---
    if new_vitals is not None:
        # Fail-closed: malformed input (missing the one field we need)
        # must escalate, never silently pass through.
        if new_vitals.news2 is None:
            new_esi = max(1, patient.esi_floor - 1)
            return _build_event(
                patient_id=patient.patient_id,
                current_minute=current_minute,
                minutes_since_check=minutes_since_check,
                trigger_type="malformed_input",
                trigger_detail="New vitals payload was missing a NEWS2 value. Failing closed.",
                previous_esi=patient.esi_floor,
                new_esi=new_esi,
                confidence_level="low",
                confidence_basis="Vitals payload malformed or incomplete.",
                evidence=[],
                nurse_summary=f"{patient.patient_id}: vitals update could not be parsed. Escalated as a precaution — please recheck in person.",
                next_check_due=0,
            )

        rose = new_vitals.news2 - patient.news2
        triggers = new_vitals.single_param_red or rose >= 2

        if triggers:
            new_esi = max(1, patient.esi_floor - 1)  # ratchet: never raises the number
            citation = (
                {
                    "document": "NEWS2 (RCP, 2017)",
                    "page": 14,
                    "criterion": "A score of 3 in any single parameter triggers urgent review regardless of aggregate total.",
                }
                if new_vitals.single_param_red
                else {
                    "document": "NEWS2 (RCP, 2017)",
                    "page": 14,
                    "criterion": "An aggregate score of 5 or above is the threshold for urgent clinical review.",
                }
            )
            return _build_event(
                patient_id=patient.patient_id,
                current_minute=current_minute,
                minutes_since_check=minutes_since_check,
                trigger_type="vitals_worsening",
                trigger_detail=new_vitals.note or f"NEWS2 rose from {patient.news2} to {new_vitals.news2}.",
                previous_esi=patient.esi_floor,
                new_esi=new_esi,
                confidence_level="high",
                confidence_basis="Full vitals set present, prior baseline available.",
                evidence=[citation],
                nurse_summary=f"{patient.patient_id}: NEWS2 {patient.news2} → {new_vitals.news2}"
                + (" (single-parameter red score)." if new_vitals.single_param_red else ".")
                + f" Escalating ESI {patient.esi_floor} → {new_esi}.",
                next_check_due=0,
            )

        # New vitals came in but nothing worrying — reset the clock, no escalation.
        return _build_event(
            patient_id=patient.patient_id,
            current_minute=current_minute,
            minutes_since_check=minutes_since_check,
            trigger_type="none",
            trigger_detail="New vitals recorded; within safe range.",
            previous_esi=patient.esi_floor,
            new_esi=patient.esi_floor,
            confidence_level="high",
            confidence_basis="Recent vitals present and stable.",
            evidence=[],
            nurse_summary=f"{patient.patient_id}: vitals stable at NEWS2 {new_vitals.news2}. No change.",
            next_check_due=CHECK_THRESHOLD_MINUTES[patient.esi_floor],
        )

    # --- Path 2: routine sweep, no new vitals ---
    if threshold == 0:
        # ESI 1 patients are under continuous watch; this function isn't
        # the mechanism for that, so just report status quo.
        return _build_event(
            patient_id=patient.patient_id,
            current_minute=current_minute,
            minutes_since_check=minutes_since_check,
            trigger_type="none",
            trigger_detail="ESI 1 — continuous monitoring, not on the periodic sweep.",
            previous_esi=patient.esi_floor,
            new_esi=patient.esi_floor,
            confidence_level="high",
            confidence_basis="Continuous observation in progress.",
            evidence=[],
            nurse_summary=f"{patient.patient_id}: under continuous watch.",
            next_check_due=0,
        )

    if minutes_since_check >= threshold * STARVATION_GUARD_MULTIPLIER:
        new_esi = max(1, patient.esi_floor - 1)
        return _build_event(
            patient_id=patient.patient_id,
            current_minute=current_minute,
            minutes_since_check=minutes_since_check,
            trigger_type="starvation_guard",
            trigger_detail=f"No recheck for {minutes_since_check} min (limit {threshold} min, guard at {threshold * STARVATION_GUARD_MULTIPLIER} min).",
            previous_esi=patient.esi_floor,
            new_esi=new_esi,
            confidence_level="low",
            confidence_basis="No observations since last check; data is stale.",
            evidence=[
                {
                    "document": "Project design rule",
                    "page": None,
                    "criterion": "Absolute time limit per severity band, independent of relative ranking (starvation guard).",
                }
            ],
            nurse_summary=f"{patient.patient_id}: missed recheck window. Auto-escalated ESI {patient.esi_floor} → {new_esi} as a precaution.",
            next_check_due=0,
        )

    if minutes_since_check >= threshold:
        return _build_event(
            patient_id=patient.patient_id,
            current_minute=current_minute,
            minutes_since_check=minutes_since_check,
            trigger_type="check_due",
            trigger_detail=f"Recheck window reached ({threshold} min for ESI {patient.esi_floor}).",
            previous_esi=patient.esi_floor,
            new_esi=patient.esi_floor,
            confidence_level="medium",
            confidence_basis="Vitals aging but not yet past the starvation-guard limit.",
            evidence=[
                {
                    "document": "NEWS2 (RCP, 2017)",
                    "page": 12,
                    "criterion": "Monitoring frequency scales with clinical risk; adapted here to ESI recheck bands.",
                }
            ],
            nurse_summary=f"{patient.patient_id}: due for reassessment.",
            next_check_due=0,
        )

    # Nothing due yet.
    return _build_event(
        patient_id=patient.patient_id,
        current_minute=current_minute,
        minutes_since_check=minutes_since_check,
        trigger_type="none",
        trigger_detail="Within recheck window.",
        previous_esi=patient.esi_floor,
        new_esi=patient.esi_floor,
        confidence_level="high",
        confidence_basis="Recent enough observation.",
        evidence=[],
        nurse_summary=f"{patient.patient_id}: no action needed.",
        next_check_due=threshold - minutes_since_check,
    )


def _build_event(
    *,
    patient_id: str,
    current_minute: int,
    minutes_since_check: int,
    trigger_type: TriggerType,
    trigger_detail: str,
    previous_esi: int,
    new_esi: int,
    confidence_level: str,
    confidence_basis: str,
    evidence: list,
    nurse_summary: str,
    next_check_due: int,
) -> dict:
    """Assembles the standard output shape. Every code path returns through here."""
    return {
        "patient_id": patient_id,
        "retriage_timestamp_min": current_minute,
        "minutes_since_last_check": minutes_since_check,
        "trigger": {"type": trigger_type, "detail": trigger_detail},
        "previous_esi_floor": previous_esi,
        "new_esi_floor": new_esi,
        "escalated": new_esi < previous_esi,
        "confidence": {"level": confidence_level, "basis": confidence_basis},
        "evidence": evidence,
        "nurse_summary": nurse_summary,
        "requires_human_review": trigger_type in ("vitals_worsening", "starvation_guard", "malformed_input"),
        "next_check_due_minutes": next_check_due,
    }


if __name__ == "__main__":
    # --- Demo run covering the four cases that matter for Round 2 ---

    print("1) Vitals worsening (P-007 sepsis case):")
    p = PatientState(patient_id="P-007", esi_floor=2, last_check_minute=0, news2=5)
    event = retriage_check(
        p, current_minute=22,
        new_vitals=NewVitals(news2=8, single_param_red=True, note="RR climbed to 34/min, new hypotension 92/58"),
    )
    print(json.dumps(event, indent=2))

    print("\n2) Routine sweep, patient overdue for recheck (starvation guard):")
    p2 = PatientState(patient_id="P-013", esi_floor=3, last_check_minute=0, news2=2)
    event2 = retriage_check(p2, current_minute=65)  # threshold=30, guard=60 -> triggers
    print(json.dumps(event2, indent=2))

    print("\n3) Routine sweep, nothing due yet:")
    p3 = PatientState(patient_id="P-005", esi_floor=4, last_check_minute=10, news2=0)
    event3 = retriage_check(p3, current_minute=25)
    print(json.dumps(event3, indent=2))

    print("\n4) Malformed vitals payload (fail-closed):")
    p4 = PatientState(patient_id="P-004", esi_floor=3, last_check_minute=0, news2=1)
    event4 = retriage_check(p4, current_minute=15, new_vitals=NewVitals(news2=None))
    print(json.dumps(event4, indent=2))