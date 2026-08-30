import math
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass
from grounding_module import news2

@dataclass
class Rule:
    id: str
    tier: int
    esi: int
    condition: Callable[[Dict[str, Any]], bool]
    citation: Dict[str, Any]
    reasoning: str

def get_age(data: Dict[str, Any]) -> Optional[float]:
    val = data.get("age")
    return float(val) if val is not None else None

def get_avpu(data: Dict[str, Any]) -> Optional[str]:
    """Normalise AVPU to a single letter before any rule sees it.

    The intake form's radio buttons are worded - "unresponsive", not "U" - so
    rules comparing against ["U", "P"] silently missed. An unresponsive patient
    fell past R2 (ESI 1, bypass) down to R13 and came out ESI 2 with no bypass:
    under-triaged by a level, and made to wait for retrieval, on exactly the
    presentation this gate exists to catch. Priyam's fixtures use letters, so
    the tests passed throughout.

    news2.AVPU_WORDS is the shared mapping, so "V" means the same thing here
    and in the scorer rather than being defined twice.
    """
    raw = data.get("avpu")
    if raw is None:
        return None
    return news2.AVPU_WORDS.get(str(raw).strip().lower())

def get_vital(data: Dict[str, Any], key: str) -> Optional[float]:
    vitals = data.get("vitals_read", {})
    if not isinstance(vitals, dict):
        return None
    val = vitals.get(key)
    return float(val) if val is not None else None

def r13_aggregate_physiological_check(data: Dict[str, Any]) -> bool:
    age = get_age(data)
    if age is None or age < 13:
        return False
    vitals = data.get("vitals_read", {})
    if not isinstance(vitals, dict):
        return False
    
    # We pass the vitals dict along with age and avpu to news2 module
    result = news2.score(
        vitals=vitals,
        on_oxygen=None,
        consciousness=get_avpu(data),
        age_years=age
    )
    if not result.applicable:
        return False
    if result.total is not None and result.total >= 5:
        return True
    if result.red_score:
        return True
    return False

def check_outside_20_percent(val: Optional[float], low: float, high: float) -> bool:
    if val is None:
        return False
    # Avoid floating point representation edge cases
    return val < (low * 0.8 - 1e-9) or val > (high * 1.2 + 1e-9)

def r14_pediatric_vitals_check(data: Dict[str, Any]) -> bool:
    age = get_age(data)
    if age is None or age >= 13:
        return False

    rr = get_vital(data, "respiratory_rate")
    hr = get_vital(data, "heart_rate")
    sbp = get_vital(data, "systolic_bp")

    if rr is not None and rr > 60:
        return True

    # Bands
    sbp_cutoff = None
    rr_range = None
    hr_range = None

    if age <= (4 / 365.25):
        sbp_cutoff, rr_range, hr_range = 60, (30, 60), (120, 170)
    elif age < 1:
        sbp_cutoff, rr_range, hr_range = 70, (30, 53), (100, 160)
    elif age < 3:
        sbp_cutoff, rr_range, hr_range = 70 + (2 * age), (22, 37), (98, 140)
    elif age < 6:
        sbp_cutoff, rr_range, hr_range = 70 + (2 * age), (20, 28), (80, 120)
    elif age < 10:
        sbp_cutoff, rr_range, hr_range = 90, (18, 25), (75, 118)
    else:
        sbp_cutoff, rr_range, hr_range = 90, (16, 22), (60, 100)

    if sbp is not None and sbp < sbp_cutoff:
        return True
    if check_outside_20_percent(rr, rr_range[0], rr_range[1]):
        return True
    if check_outside_20_percent(hr, hr_range[0], hr_range[1]):
        return True

    return False


RULES = [
    # --- TIER 1 ---
    Rule(
        id="R1",
        tier=1,
        esi=1,
        condition=lambda d: d.get("pulse_present") is False or d.get("breathing") is False,
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point A"},
        reasoning="Patient is pulseless or apneic."
    ),
    Rule(
        id="R2",
        tier=1,
        esi=1,
        condition=lambda d: get_avpu(d) in ["U", "P"],
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point A — unresponsiveness defined as requiring a painful/noxious stimulus or non-response to any stimulus."},
        reasoning="Unresponsive or responds only to pain."
    ),
    Rule(
        id="R3",
        tier=1,
        esi=1,
        condition=lambda d: get_vital(d, "spo2") is not None and get_vital(d, "spo2") < 90,
        citation={"document": "NEWS2 (RCP, 2017)", "page": 14, "criterion": "SpO2 <=91 scores maximum severity; <90 used here as the ESI-1 immediate-intervention cutoff per ESI v4 Handbook Decision Point A (severe hypoxia)."},
        reasoning="Severe hypoxia (SpO2 < 90)."
    ),
    Rule(
        id="R4",
        tier=1,
        esi=1,
        condition=lambda d: "penetrating_trauma" in d.get("mechanism_flags", []) and (
            (get_vital(d, "systolic_bp") is not None and get_vital(d, "systolic_bp") < 90) or 
            (get_avpu(d) is not None and get_avpu(d) != "A")
        ),
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point A — unstable trauma requiring immediate life-saving intervention."},
        reasoning="Unstable penetrating trauma."
    ),
    Rule(
        id="R5",
        tier=1,
        esi=1,
        condition=lambda d: "uncontrolled_hemorrhage" in d.get("mechanism_flags", []),
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point A."},
        reasoning="Uncontrolled hemorrhage."
    ),
    Rule(
        id="R6",
        tier=1,
        esi=1,
        condition=lambda d: "active_seizure" in d.get("mechanism_flags", []),
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point A."},
        reasoning="Active seizure."
    ),
    Rule(
        id="R7",
        tier=1,
        esi=1,
        condition=lambda d: get_age(d) is not None and get_age(d) < (28/365) and get_vital(d, "temperature_c") is not None and get_vital(d, "temperature_c") >= 38.0,
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 6 (Pediatric) — neonatal fever under 28 days treated as automatic high-risk given sepsis risk."},
        reasoning="Neonatal fever (<28 days, >= 38.0C)."
    ),

    # --- TIER 2 ---
    Rule(
        id="R8",
        tier=2,
        esi=2,
        condition=lambda d: get_avpu(d) == "V",
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point B — new altered mental status without identified cause is high-risk."},
        reasoning="Altered mental status (V)."
    ),
    Rule(
        id="R9",
        tier=2,
        esi=2,
        condition=lambda d: "stroke_signs" in d.get("mechanism_flags", []),
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point B."},
        reasoning="Stroke signs."
    ),
    Rule(
        id="R10",
        tier=2,
        esi=2,
        condition=lambda d: "chest_pain" in d.get("mechanism_flags", []) and get_vital(d, "systolic_bp") is not None and get_vital(d, "systolic_bp") < 100,
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point B — high-risk with hemodynamic compromise."},
        reasoning="Chest pain with hypotension."
    ),
    Rule(
        id="R11",
        tier=2,
        esi=2,
        condition=lambda d: "airway_swelling" in d.get("mechanism_flags", []),
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point B."},
        reasoning="Airway swelling."
    ),
    Rule(
        id="R12",
        tier=2,
        esi=2,
        condition=lambda d: "burns_over_10_percent" in d.get("mechanism_flags", []),
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point B."},
        reasoning="Burns >10% BSA."
    ),
    Rule(
        id="R13",
        tier=2,
        esi=2,
        condition=r13_aggregate_physiological_check,
        citation={"document": "NEWS2 (RCP, 2017)", "page": 14, "criterion": "aggregate score >= 5 OR single_param_red == True"},
        reasoning="Adult NEWS2 aggregate >=5 or single red parameter."
    ),
    Rule(
        id="R14",
        tier=2,
        esi=2,
        condition=r14_pediatric_vitals_check,
        citation={"document": "PALS Guidelines, 2015", "page": None, "criterion": "Pediatric vital signs reference chart."},
        reasoning="Pediatric vitals outside safe thresholds."
    ),
    Rule(
        id="R15",
        tier=2,
        esi=2,
        # Elderly patients frequently do not mount a fever or tachycardic response to serious infection 
        # (project research: NEWS2 in frail older adults, Helsinki cohort, median age 85, AUC 0.70 for 30-day mortality 
        # — moderate performance only, supporting a lower escalation threshold in this age group).
        condition=lambda d: get_age(d) is not None and get_age(d) >= 65 and get_avpu(d) is not None and get_avpu(d) != "A",
        citation={"document": "Team design decision", "page": None, "criterion": "Geriatric silent-presentation check"},
        reasoning="Altered mental status in elderly (>=65)."
    ),
    Rule(
        id="R16",
        tier=2,
        esi=2,
        # Known limitation: self-reported pain is documented to be under-recorded for women and non-English speakers;
        # this is why it is the LAST rule checked, not the first.
        condition=lambda d: get_vital(d, "pain_score") is not None and get_vital(d, "pain_score") >= 7,
        citation={"document": "ESI v4 Handbook (AHRQ, 2012)", "page": None, "criterion": "Ch. 3, Decision Point B — severe pain/distress alone is sufficient for high-risk classification."},
        reasoning="Severe pain (>= 7)."
    )
]

def evaluate_red_flag_gate(patient_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates patient data against a strict, deterministic set of clinical rules.
    Bypasses LLM pipeline for clear ESI 1 or 2 cases.
    """
    missing_fields = []
    required = ["age", "avpu", "pulse_present", "breathing"]
    for r in required:
        if patient_data.get(r) is None:
            missing_fields.append(r)
            
    base_response = {
        "patient_id": patient_data.get("patient_id", ""),
        "gate_result": "no_match",
        "esi": None,
        "matched_rule_id": None,
        "reasoning": "",
        "citation": None,
        "bypasses_pipeline": False,
        "missing_fields": missing_fields,
        "low_confidence": False
    }

    if len(missing_fields) >= 2:
        base_response["low_confidence"] = True
        base_response["confidence_note"] = "Insufficient structured data for red-flag screening — route through full pipeline with elevated priority."
        return base_response

    for rule in RULES:
        # Evaluate safely
        try:
            matched = rule.condition(patient_data)
        except Exception:
            matched = False
            
        if matched:
            base_response.update({
                "gate_result": f"ESI_{rule.esi}",
                "esi": rule.esi,
                "matched_rule_id": rule.id,
                "reasoning": rule.reasoning,
                "citation": rule.citation,
                "bypasses_pipeline": True
            })
            return base_response

    return base_response
