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

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from grounding_module.news2 import age_from_schema
from retriage_loop import NewVitals, PatientState, retriage_check


@dataclass
class Record:
    patient_id: str
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
        patient_id = grounded.get("patient_id") or f"P-{len(self._records) + 1:03d}"
        minute = self.now_minute()

        initial = initial or {}
        record = Record(
            patient_id=patient_id,
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

        event = retriage_check(
            record.state,
            current_minute=self.now_minute(),
            new_vitals=NewVitals(
                news2=vitals.get("news2"),
                single_param_red=bool(vitals.get("single_param_red")),
                note=vitals.get("note", ""),
            ),
        )
        self._apply(record, event, new_news2=vitals.get("news2"),
                    new_red=bool(vitals.get("single_param_red")))
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
            record.events.append(event)


registry = Registry()
