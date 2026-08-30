"""In-memory patient registry that joins the three modules.

The grounding module produces exactly what the re-triage loop needs to track a
patient, which is not a coincidence - both are built around NEWS2:

    grounded["patient_id"]        -> PatientState.patient_id
    grounded["grounded_esi"]      -> PatientState.esi_floor
    grounded["news2"]["total"]    -> PatientState.news2
    grounded["news2"]["red_score"]-> PatientState.single_param_red

One join needs care. `news2.total` is None when NEWS2 does not apply - a
paediatric patient, where adult bands would read normal physiology as
extreme. `retriage_check` does arithmetic on that field, so a None would
crash the sweep. Those patients stay in the queue on the time-based paths
(recheck window, starvation guard) and are excluded from automatic
physiological escalation, with `news2_applicable: false` on the record so the
nurse can see the difference rather than infer it from silence.

Not a database. Restarting the service loses the queue; that is fine for a
demo and must not ship to a ward.
"""
from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from grounding_module import news2 as news2_mod
from grounding_module.news2 import age_from_schema
from retriage_loop import NewVitals, PatientState, retriage_check

log = logging.getLogger(__name__)


# The intake LLM fills patient_id from whatever the form gave it. Observed in
# live runs: a person's full name ("Abhishek_Sonparote_202311XX") and the
# string "null". A null-ish id is worse than missing - the registry keys on it,
# so two of them silently overwrite each other. Anything that is not clearly an
# identifier gets a minted one, and the original is kept for traceability.
NULLISH = {"", "null", "none", "nil", "undefined", "n/a", "na", "unknown", "-"}
_MINTED = itertools.count(1)

# An earlier version demanded digits immediately after an optional prefix
# (^[A-Za-z]{0,6}[-_ ]?\d+$). That rejected perfectly ordinary identifiers -
# P-OV1, ED-2024-001 - and replaced them with a minted TEST-nnn, which is worse
# than the problem it was solving: a real MRN silently swapped for a fake one.
MAX_ID_LENGTH = 20
HAS_DIGIT = re.compile(r"\d")
WORD = re.compile(r"[^\s_]+")


def clean_patient_id(raw: object) -> tuple[str, str | None]:
    """Return (usable_id, rejected_original).

    An identifier is short, contains a digit, and is not a sentence. A name
    fails on the digit ("Jane Doe") or on length and word count
    ("Abhishek_Sonparote_202311XX"), both of which were seen in live runs.
    Whatever is rejected is kept as source_patient_id, never discarded.
    """
    text = "" if raw is None else str(raw).strip()
    if text.lower() in NULLISH:
        return f"TEST-{next(_MINTED):03d}", text or None

    looks_like_id = (
        len(text) <= MAX_ID_LENGTH
        and HAS_DIGIT.search(text) is not None
        and len(WORD.findall(text)) <= 2
    )
    if looks_like_id:
        return text, None
    return f"TEST-{next(_MINTED):03d}", text


@dataclass
class Record:
    patient_id: str
    source_patient_id: str | None       # what upstream sent, if we rejected it
    initial: dict                       # what the intake LLM produced
    grounded: dict                      # full output of grounding_module.ground()
    state: PatientState
    news2_applicable: bool
    admitted_minute: int
    age_years: float | None = None
    events: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        concerns = self.grounded.get("concerns") or []
        top = concerns[0] if concerns else {}
        return {
            "patient_id": self.patient_id,
            "source_patient_id": self.source_patient_id,
            "esi_floor": self.state.esi_floor,
            "grounded_esi": self.grounded.get("grounded_esi"),
            "news2": self.state.news2 if self.news2_applicable else None,
            "news2_applicable": self.news2_applicable,
            "single_param_red": self.state.single_param_red,
            "concern": top.get("concern"),
            "clinical_shorthand": top.get("clinical_shorthand"),
            "nurse_summary": top.get("nurse_summary"),
            "citations": sum(len(c.get("evidence") or []) for c in concerns),
            "last_check_minute": self.state.last_check_minute,
            "escalations": sum(1 for e in self.events if e.get("escalated")),
            "requires_human_review": any(
                e.get("requires_human_review") for e in self.events[-1:]),
        }


class Registry:
    """Thread-safe patient store. The clock is minutes since service start."""

    def __init__(self) -> None:
        self._records: dict[str, Record] = {}
        self._lock = threading.Lock()
        self._started = time.monotonic()

    # ---------------------------------------------------------------- clock

    def now_minute(self) -> int:
        return int((time.monotonic() - self._started) / 60)

    # ------------------------------------------------------------ admission

    def admit(self, grounded: dict, initial: dict | None = None) -> Record:
        news2 = grounded.get("news2") or {}
        applicable = bool(news2.get("applicable")) and news2.get("total") is not None
        patient_id, rejected = clean_patient_id(grounded.get("patient_id"))
        with self._lock:
            while patient_id in self._records:      # never overwrite a patient
                patient_id = f"TEST-{next(_MINTED):03d}"
        minute = self.now_minute()

        initial = initial or {}
        record = Record(
            patient_id=patient_id,
            source_patient_id=rejected,
            initial=initial,
            grounded=grounded,
            age_years=age_from_schema(initial) if initial else None,
            news2_applicable=applicable,
            admitted_minute=minute,
            state=PatientState(
                patient_id=patient_id,
                esi_floor=int(grounded.get("grounded_esi") or 3),
                last_check_minute=minute,
                # 0 is a placeholder for the inapplicable case and is never
                # used: those patients skip the vitals-delta path entirely.
                news2=int(news2["total"]) if applicable else 0,
                single_param_red=bool(news2.get("red_score")),
            ),
        )
        with self._lock:
            self._records[patient_id] = record
        return record

    # -------------------------------------------------------------- reading

    def get(self, patient_id: str) -> Record | None:
        return self._records.get(patient_id)

    def all(self) -> list[Record]:
        return list(self._records.values())

    def queue(self) -> list[dict]:
        """Most urgent first; within a level, longest waiting first."""
        rows = [r.summary() for r in self._records.values()]
        rows.sort(key=lambda r: (r["esi_floor"], r["last_check_minute"]))
        return rows

    # ------------------------------------------------------------- re-triage

    def score_vitals(self, record: Record, vitals: dict) -> tuple[int | None, bool]:
        """Turn whatever the caller sent into (news2_total, single_param_red).

        The UI sends raw observations - heart_rate, respiratory_rate and so on.
        Scoring them is this module's job, not the caller's: the whole reason
        news2.py exists is that the arithmetic must not be done anywhere else.
        A pre-computed `news2` is still accepted for callers that have one.
        """
        if vitals.get("news2") is not None:
            return int(vitals["news2"]), bool(vitals.get("single_param_red"))

        raw = {k: vitals.get(k) for k in
               ("respiratory_rate", "spo2", "systolic_bp", "temperature_c")}
        raw["heart_rate"] = vitals.get("heart_rate", vitals.get("pulse"))
        raw = {k: v for k, v in raw.items() if v is not None}
        if not raw:
            return None, False

        consciousness = vitals.get("consciousness")
        oxygen = vitals.get("on_supplemental_oxygen")
        scored = news2_mod.score(
            raw,
            on_oxygen=None if oxygen is None else bool(oxygen),
            consciousness=consciousness,
            scale=(record.grounded.get("news2") or {}).get("spo2_scale", 1),
            age_years=record.age_years,
        )
        if not scored.applicable:
            return None, False
        return scored.total, scored.red_score

    def record_vitals(self, patient_id: str, vitals: dict) -> dict:
        record = self._records[patient_id]

        if not record.news2_applicable:
            return {
                "patient_id": patient_id,
                "skipped": True,
                "reason": ("NEWS2 does not apply to this patient, so automatic "
                           "escalation on a NEWS2 delta is not available. The "
                           "recheck window and starvation guard still apply."),
                "news2_not_applicable_because":
                    (record.grounded.get("news2") or {}).get("not_applicable_reason"),
            }

        total, red = self.score_vitals(record, vitals)
        previous = record.state.news2          # _apply overwrites this below
        event = retriage_check(
            record.state,
            current_minute=self.now_minute(),
            new_vitals=NewVitals(news2=total, single_param_red=red,
                                 note=vitals.get("note", "")),
        )
        self._apply(record, event, new_news2=total, new_red=red)
        event["news2"] = {"total": total, "single_param_red": red,
                          "previous": previous}
        return event

    def override_esi(self, patient_id: str, esi: int, *, reason: str,
                     by: str = "nurse") -> dict:
        """A nurse setting the ESI by hand. The one thing allowed to de-escalate.

        retriage_loop is an escalate-only ratchet by design: automatic urgency
        can move down the numbers and never back up, "except by a separate,
        explicitly logged nurse action that is NOT part of this function."
        This is that action, kept deliberately outside the loop.

        A de-escalation is the dangerous direction - it moves a patient further
        down the queue - so it requires a reason and is recorded as one. The
        event goes in the same history as the automatic ones, flagged manual,
        so the trail shows who decided what rather than implying the system did.
        """
        record = self._records[patient_id]
        previous = record.state.esi_floor
        esi = int(esi)
        if not 1 <= esi <= 5:
            raise ValueError(f"ESI must be 1-5, got {esi}")

        direction = ("no change" if esi == previous
                     else "escalation" if esi < previous else "de-escalation")
        minute = self.now_minute()

        event = {
            "patient_id": patient_id,
            "retriage_timestamp_min": minute,
            "minutes_since_last_check": minute - record.state.last_check_minute,
            "trigger": {"type": "manual_override",
                        "detail": f"{by} set ESI {previous} -> {esi}. {reason}".strip()},
            "previous_esi_floor": previous,
            "new_esi_floor": esi,
            "escalated": esi < previous,
            "manual": True,
            "overridden_by": by,
            "override_reason": reason,
            "direction": direction,
            "confidence": {"level": "human",
                           "basis": "Clinical judgement at the bedside, not a computed score."},
            "evidence": [],
            "nurse_summary": (f"MANUAL {direction.upper()}: ESI {previous} -> {esi} "
                              f"by {by}. {reason}").strip(),
            # A human de-escalation should be visible to the next person
            # looking at the queue, not filed away silently.
            "requires_human_review": direction == "de-escalation",
            "next_check_due_minutes": 0,
        }

        with self._lock:
            record.state.esi_floor = esi
            record.state.last_check_minute = minute
            record.events.append(self._stamp(event))

        log.info("manual override %s: ESI %s -> %s by %s (%s)",
                 patient_id, previous, esi, by, reason)
        return event

    def sweep(self) -> list[dict]:
        """Routine pass over everyone waiting. No new vitals."""
        minute = self.now_minute()
        events = []
        for record in list(self._records.values()):
            event = retriage_check(record.state, current_minute=minute)
            self._apply(record, event)
            if event["trigger"]["type"] != "none":
                events.append(event)
        return events

    @staticmethod
    def _stamp(event: dict) -> dict:
        """Put a real clock time on every event.

        retriage_loop counts in simulation minutes, which is right for the
        loop and useless for an audit trail: "T+14m" cannot be reconciled with
        a shift rota, a handover, or a complaint six weeks later. Stamped here
        rather than in the loop so that function stays pure.
        """
        if "recorded_at" not in event:
            now = datetime.now(timezone.utc)
            event["recorded_at"] = now.isoformat(timespec="seconds")
            event["recorded_at_local"] = now.astimezone().strftime("%H:%M:%S")
        return event

    def _apply(self, record: Record, event: dict, *,
               new_news2: int | None = None, new_red: bool | None = None) -> None:
        """Persist the ratchet. The loop is pure; storing is the caller's job."""
        with self._lock:
            record.state.esi_floor = event["new_esi_floor"]
            record.state.last_check_minute = event["retriage_timestamp_min"]
            if new_news2 is not None:
                record.state.news2 = int(new_news2)
            if new_red is not None:
                record.state.single_param_red = new_red
            record.events.append(self._stamp(event))


registry = Registry()
