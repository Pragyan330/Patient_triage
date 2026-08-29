"""Deterministic NEWS2 scoring.

WHY THIS IS CODE AND NOT RAG
----------------------------
The NEWS2 observation chart (Chart 3, PDF p.58 / printed p.35 of the RCP 2017
report) is a spatial grid. PyMuPDF text extraction returns the point values
and the value bands as two unaligned runs - the score/band association is
lost. Mistral OCR skips it entirely and emits `![img-0.jpeg]`. Neither route
can recover which band earns which point, so the arithmetic cannot be
grounded by retrieval.

It does not need to be. The bands are a small fixed table that has not changed
since 2017. We encode them here, cite the chart, and compute exactly. RAG is
for the *prose* thresholds ("aggregate >=5 triggers urgent review"), which
extract cleanly and genuinely need a citation.

SCOPE GATE
----------
NEWS2 is validated for acutely ill ADULTS (16+), and explicitly not for
children or pregnancy. Feeding infant vitals through adult bands produces a
confident, meaningless number - a 3-month-old with entirely normal
observations scores 10. This module refuses rather than returns that.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CHART = "NEWS2 (RCP, 2017)"
CHART_PAGE_PDF = 58
CHART_PAGE_PRINTED = 35
MIN_ADULT_AGE_YEARS = 16


@dataclass
class ParamScore:
    parameter: str
    value: object
    score: int
    band: str

    def as_dict(self) -> dict:
        return {"parameter": self.parameter, "value": self.value,
                "score": self.score, "band": self.band}


@dataclass
class News2Result:
    total: int | None
    parameters: list[ParamScore]
    missing: list[str]
    red_score: bool
    risk: str
    response: str
    applicable: bool = True
    not_applicable_reason: str | None = None
    scale: int = 1
    source: dict = field(default_factory=lambda: {
        "document": CHART, "page": CHART_PAGE_PDF, "printed_page": CHART_PAGE_PRINTED})

    def as_dict(self) -> dict:
        d = {
            "applicable": self.applicable,
            "total": self.total,
            "spo2_scale": self.scale,
            "parameters": [p.as_dict() for p in self.parameters],
            "missing": self.missing,
            "red_score": self.red_score,
            "risk": self.risk,
            "response": self.response,
            "source": self.source,
        }
        if not self.applicable:
            d["not_applicable_reason"] = self.not_applicable_reason
        return d


def not_applicable(reason: str) -> News2Result:
    return News2Result(None, [], [], False, "not_scored",
                       "NEWS2 does not apply - use an age-appropriate tool",
                       applicable=False, not_applicable_reason=reason)


def _band(value: float, bands: list[tuple[float, float, int, str]]) -> ParamScore | None:
    for low, high, score, label in bands:
        if low <= value <= high:
            return ParamScore("", value, score, label)
    return None


RESPIRATION = [(-1e9, 8, 3, "<=8"), (9, 11, 1, "9-11"), (12, 20, 0, "12-20"),
               (21, 24, 2, "21-24"), (25, 1e9, 3, ">=25")]

SPO2_SCALE1 = [(-1e9, 91, 3, "<=91"), (92, 93, 2, "92-93"),
               (94, 95, 1, "94-95"), (96, 1e9, 0, ">=96")]

# Scale 2: for confirmed hypercapnic respiratory failure with a prescribed
# 88-92% target. Scoring depends on whether the patient is on oxygen.
SPO2_SCALE2_AIR = [(-1e9, 83, 3, "<=83"), (84, 85, 2, "84-85"),
                   (86, 87, 1, "86-87"), (88, 1e9, 0, ">=88 on air")]

SPO2_SCALE2_OXYGEN = [(-1e9, 83, 3, "<=83"), (84, 85, 2, "84-85"),
                      (86, 87, 1, "86-87"), (88, 92, 0, "88-92 target"),
                      (93, 94, 1, "93-94 on O2"), (95, 96, 2, "95-96 on O2"),
                      (97, 1e9, 3, ">=97 on O2")]

SYSTOLIC = [(-1e9, 90, 3, "<=90"), (91, 100, 2, "91-100"), (101, 110, 1, "101-110"),
            (111, 219, 0, "111-219"), (220, 1e9, 3, ">=220")]

PULSE = [(-1e9, 40, 3, "<=40"), (41, 50, 1, "41-50"), (51, 90, 0, "51-90"),
         (91, 110, 1, "91-110"), (111, 130, 2, "111-130"), (131, 1e9, 3, ">=131")]

TEMPERATURE = [(-1e9, 35.0, 3, "<=35.0"), (35.1, 36.0, 1, "35.1-36.0"),
               (36.1, 38.0, 0, "36.1-38.0"), (38.1, 39.0, 1, "38.1-39.0"),
               (39.1, 1e9, 2, ">=39.1")]

ALERT_WORDS = {"a", "alert"}


def score(vitals: dict, *, on_oxygen: bool | None = None,
          consciousness: str | None = None, scale: int = 1,
          age_years: float | None = None) -> News2Result:
    """Score a NEWS2 observation set.

    `vitals` uses the key names from the initial schema's vitals_read. Anything
    absent is reported in `missing` rather than assumed normal - an unmeasured
    parameter is not a zero, and pretending otherwise understates the score.
    """
    if age_years is not None and age_years < MIN_ADULT_AGE_YEARS:
        return not_applicable(
            f"NEWS2 is validated for adults (>={MIN_ADULT_AGE_YEARS}y); "
            f"patient is {age_years:g}y. Adult bands misread normal paediatric "
            f"physiology as extreme. Use a paediatric tool (e.g. PEWS) and the "
            f"ESI paediatric criteria.")

    params: list[ParamScore] = []
    missing: list[str] = []

    def add(name: str, key: str, bands: list) -> None:
        value = vitals.get(key)
        if value is None:
            missing.append(key)
            return
        hit = _band(float(value), bands)
        if hit is None:
            missing.append(key)
            return
        hit.parameter, hit.value = name, value
        params.append(hit)

    add("respiration_rate", "respiratory_rate", RESPIRATION)

    if scale == 2:
        spo2_bands = SPO2_SCALE2_OXYGEN if on_oxygen else SPO2_SCALE2_AIR
        add("spo2_scale_2", "spo2", spo2_bands)
    else:
        add("spo2_scale_1", "spo2", SPO2_SCALE1)

    add("systolic_bp", "systolic_bp", SYSTOLIC)
    add("pulse", "heart_rate", PULSE)
    add("temperature", "temperature_c", TEMPERATURE)

    if on_oxygen is not None:
        params.append(ParamScore("air_or_oxygen", "oxygen" if on_oxygen else "air",
                                 2 if on_oxygen else 0, "oxygen" if on_oxygen else "air"))
    else:
        missing.append("air_or_oxygen")

    if consciousness is not None:
        alert = consciousness.strip().lower() in ALERT_WORDS
        params.append(ParamScore("consciousness_acvpu", consciousness,
                                 0 if alert else 3, "Alert" if alert else "C/V/P/U"))
    else:
        missing.append("consciousness_acvpu")

    total = sum(p.score for p in params)
    red = any(p.score == 3 for p in params)

    if total >= 7:
        risk, response = "high", "emergency response - urgent/emergency care team"
    elif total >= 5:
        risk, response = "medium", "urgent response - key threshold, urgent clinical review"
    elif red:
        risk, response = "low-medium", "urgent review by ward-based doctor (single red score)"
    else:
        risk, response = "low", "ward-based response"

    result = News2Result(total, params, missing, red, risk, response)
    result.scale = scale
    return result


# ------------------------------------------------------------------ parsing

# Order matters: explicit "N months old" beats a bare number. The bare-number
# and "At N," forms are anchored to the START of a field, because unanchored
# they happily match "father had a heart attack at 60" as the patient's age.
AGE_PATTERNS = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*[-\s]?month[s\-\s]*old", re.I), 1 / 12),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[-\s]?week[s\-\s]*old", re.I), 1 / 52),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[-\s]?day[s\-\s]*old", re.I), 1 / 365),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[-\s]?(?:year|yr|y)[s\-\s]*old", re.I), 1.0),
    (re.compile(r"^\s*(?:at\s+)?(\d{1,3})\s*(?:,|\s+year)", re.I), 1.0),
    (re.compile(r"^\s*(\d+(?:\.\d+)?)\s*month", re.I), 1 / 12),
]

# fields most likely to carry the age, most reliable first
AGE_FIELDS = ("age_years", "age", "age_sex_note", "demographics", "concern",
              "what_keeps_it_open")

# CVPU: anything that is not "alert" scores 3.
NOT_ALERT = re.compile(
    r"\bnew[- ]onset confusion\b|\bnew confusion\b|\bconfus(?:ed|ion)\b|\bdisorient|"
    r"\bdelirium\b|\bunresponsive\b|\bunrespons|\bresponds? only to (?:voice|pain)\b|"
    r"\bdrowsy\b|\bobtunded\b|\blethargic\b|\bnot rousable\b|\bunconscious\b", re.I)

ALERT_PHRASE = re.compile(
    r"\balert and orientated\b|\balert and oriented\b|\bfully alert\b|\bgcs 15\b", re.I)

ON_OXYGEN = re.compile(
    r"\bon oxygen\b|\bsupplemental oxygen\b|\bnasal cannula\b|\bvia mask\b|"
    r"\b\d+(?:\.\d+)?\s*l\s*/?\s*min\b|\bhome oxygen\b|\bventuri\b|"
    r"\bbag[- ]valve[- ]mask\b|\bbvm\b|\bnon[- ]rebreathe\b", re.I)

ON_AIR = re.compile(r"\broom air\b|\bon air\b|\bself[- ]ventilating on air\b", re.I)

SCALE2 = re.compile(
    r"\bhypercapnic respiratory failure\b|\bscale 2\b|\b88\s*[-–]\s*92\s*%?\b|"
    r"\btarget saturation[s]? (?:range )?(?:of |is |are )?88\b", re.I)


def _prose(initial: dict) -> str:
    parts = []
    for v in initial.values():
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.extend(str(x) for x in v if isinstance(x, str))
    return " ".join(parts)


def parse_age_years(text: str) -> float | None:
    for pattern, factor in AGE_PATTERNS:
        m = pattern.search(text)
        if m:
            return float(m.group(1)) * factor
    return None


def age_from_schema(initial: dict) -> float | None:
    """Search the likeliest fields first, each on its own.

    Searching the concatenated prose is what let "heart attack at 60" win over
    the patient's actual "58," - the anchored patterns only mean anything when
    they are applied per field.
    """
    if isinstance(initial.get("age_years"), (int, float)):
        return float(initial["age_years"])
    for key in AGE_FIELDS:
        value = initial.get(key)
        if isinstance(value, str):
            found = parse_age_years(value)
            if found is not None:
                return found
    return parse_age_years(_prose(initial))


def from_schema(initial: dict) -> News2Result:
    """Score straight from an initial-judge schema.

    Everything the upstream LLM records in prose - age, whether the patient is
    alert, whether they are on oxygen - has to be read back out here, because
    the schema has no fields for them. That is fragile by construction; the
    real module should ask upstream for structured fields.
    """
    vitals = dict(initial.get("vitals_read") or {})
    for k in (initial.get("vitals_read", {}) or {}).get("not_measured", []) or []:
        vitals.pop(k, None)
    vitals.pop("not_measured", None)

    blob = _prose(initial)

    age = age_from_schema(initial)

    consciousness = None
    if NOT_ALERT.search(blob):
        consciousness = "C/V/P/U"
    elif ALERT_PHRASE.search(blob):
        consciousness = "A"

    on_oxygen = None
    if ON_OXYGEN.search(blob):
        on_oxygen = True
    elif ON_AIR.search(blob):
        on_oxygen = False

    scale = 2 if SCALE2.search(blob) else 1

    return score(vitals, on_oxygen=on_oxygen, consciousness=consciousness,
                 scale=scale, age_years=age)
