"""Serve the live queue in the shape retriage-demo already consumes.

RetriageQueue.tsx does `import samplePatients from './sample_patients.json'`.
Rather than ask Priyam to restructure the component, this emits that exact
shape from live data, so the integration is a one-line change on their side:

    -import samplePatients from './sample_patients.json';
    +const samplePatients = await (await fetch(FEED_URL)).json();

Their shape is already most of the way there - patient_id, concerns[],
provisional_esi, grounded_esi and retrieval_ms come straight from the
grounding output. Only `profile`, `initial_vitals` and `arrival_minute` need
building, and all three are derivable from what we hold.
"""
from __future__ import annotations

import re

from service.store import Record

# NEWS2 parameter name -> the key retriage-demo expects
VITALS_KEYS = {
    "respiration_rate": "respiratory_rate",
    "spo2_scale_1": "spo2",
    "spo2_scale_2": "spo2",
    "systolic_bp": "systolic_bp",
    "pulse": "pulse",
    "temperature": "temperature_c",
}

# The intake prose rarely says "female" outright - it says "Her heart rate".
# Pronouns carry it more often than the noun does. Bare "M"/"F" is deliberately
# excluded: case-insensitively it matches any stray single letter.
FEMALE = re.compile(r"\b(female|woman|girl|she|her|hers)\b", re.I)
MALE = re.compile(r"\b(male|man|boy|he|him|his)\b", re.I)


def _profile(record: Record) -> dict:
    """Best-effort demographics. The intake prose is all we get."""
    note = " ".join(v for v in record.initial.values() if isinstance(v, str))

    # Count rather than first-match: a note can mention a relative
    # ("her father"), and the patient is whoever dominates.
    female, male = len(FEMALE.findall(note)), len(MALE.findall(note))
    sex = None
    if female or male:
        sex = "F" if female > male else "M" if male > female else None

    return {
        "patientId": record.patient_id,
        "age": record.age_years,
        "sex": sex,
        "arrivalMode": None,
        "knownConditions": [],
        "medications": [],
        "allergies": [],
    }


def _initial_vitals(record: Record) -> dict:
    """Rebuild the vitals block from the NEWS2 parameters we scored."""
    news2 = record.grounded.get("news2") or {}
    out: dict = {}
    for param in news2.get("parameters", []):
        key = VITALS_KEYS.get(param["parameter"])
        if key:
            out[key] = param["value"]
        elif param["parameter"] == "air_or_oxygen":
            out["on_supplemental_oxygen"] = param["value"] == "oxygen"
        elif param["parameter"] == "consciousness_acvpu":
            out["consciousness"] = "A" if param["score"] == 0 else "C"
    return out


def as_demo_patient(record: Record, now_minute: int) -> dict:
    """One record in retriage-demo's sample_patients.json shape."""
    grounded = record.grounded
    return {
        "patient_id": record.patient_id,
        "profile": _profile(record),
        "concerns": grounded.get("concerns", []),
        "provisional_esi": grounded.get("provisional_esi"),
        "grounded_esi": grounded.get("grounded_esi"),
        # esi_floor is what the ratchet has moved it to; grounded_esi is where
        # it started. The UI sorts on the live one.
        "current_esi_floor": record.state.esi_floor,
        "retrieval_ms": grounded.get("retrieval_ms"),
        "initial_vitals": _initial_vitals(record),
        "arrival_minute": record.admitted_minute - now_minute,
        "news2_applicable": record.news2_applicable,
        "retriage_events": record.events,
    }
