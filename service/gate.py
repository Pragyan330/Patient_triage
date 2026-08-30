"""Run the red-flag gate ahead of grounding, and decide what that means.

The gate is deterministic and fast: it reads structured fields and returns an
ESI without touching an LLM. Grounding takes 6-15s because it retrieves and
cites. So the gate goes first.

WHY WE DO NOT HONOUR bypasses_pipeline FOR TIER 2
-------------------------------------------------
`evaluate_red_flag_gate` sets bypasses_pipeline on every match, tier 1 and
tier 2 alike. Taken literally that guts the system: R13 fires on a NEWS2 of 5
or any single red parameter, which is the reference sepsis patient and a large
share of everyone who actually matters. Those patients would reach the nurse
with an ESI and no citation behind it.

The latency argument only holds where seconds count:

  tier 1 (ESI 1)  pulseless, apneic, unresponsive, severe hypoxia.
                  Bypass. Nobody waits 15s for a citation on an arrest.

  tier 2 (ESI 2)  high-risk but breathing with a pulse. Ground them - the
                  citations are the point, and 6-15s is not the difference
                  between outcomes at this level.

Either way the gate's ESI is a FLOOR. It is deterministic where the model is
not, so grounding may make a patient more urgent and never less.
"""
from __future__ import annotations

import logging

from red_flag_gate import evaluate_red_flag_gate

log = logging.getLogger(__name__)

BYPASS_TIERS = {1}          # ESI levels fast enough to matter


def run_gate(initial: dict) -> dict:
    """Evaluate the gate, never raising into the request path."""
    try:
        result = evaluate_red_flag_gate(initial)
    except Exception as exc:                       # a broken rule must not 500
        log.exception("red-flag gate raised")
        return {"gate_result": "error", "esi": None, "bypasses_pipeline": False,
                "error": f"{type(exc).__name__}: {exc}", "missing_fields": [],
                "low_confidence": True}

    matched = result.get("esi")
    # Override the flag rather than the gate: see the module docstring.
    result["bypasses_pipeline"] = matched in BYPASS_TIERS
    if matched is not None:
        log.info("gate matched %s -> ESI %s (%s)", result.get("matched_rule_id"),
                 matched, "bypass" if result["bypasses_pipeline"] else "ground anyway")
    return result


def as_grounded(initial: dict, gate: dict, news2: dict) -> dict:
    """Build a grounded-shaped payload for a tier-1 bypass.

    Same shape the rest of the system expects, so the registry, the queue and
    the feed need no special case. The single citation is whatever the rule
    carries; it goes through the same verifier as everything else, and the
    rules cite chapters rather than pages, so most come back unverified. That
    is honest - an ESI 1 for "pulseless" does not rest on a quotation.
    """
    citation = gate.get("citation") or {}
    evidence = []
    if citation.get("document"):
        evidence.append({
            "document": citation["document"],
            "page": citation.get("page"),
            "criterion": citation.get("criterion") or gate.get("reasoning", ""),
        })

    return {
        "patient_id": initial.get("patient_id") or "",
        "concerns": [{
            "concern": gate.get("reasoning") or "Immediate life-saving intervention required",
            "clinical_shorthand": f"!{gate.get('matched_rule_id', 'red flag')}",
            "implied_esi": initial.get("implied_esi") or gate["esi"],
            "final_esi": gate["esi"],
            "time_to_treatment_minutes": 0,
            "evidence": evidence,
            "nurse_summary": (
                f"RED FLAG {gate.get('matched_rule_id')}: {gate.get('reasoning')} "
                f"ESI {gate['esi']} — immediate intervention. Retrieval was skipped "
                f"deliberately; this decision is rule-based, not model-based."),
        }],
        "provisional_esi": initial.get("implied_esi") or gate["esi"],
        "grounded_esi": gate["esi"],
        "news2": news2,
        "retrieval_ms": 0,
        "red_flag_gate": gate,
        "_audit": {
            "model": "none - red-flag gate bypassed retrieval",
            "retriever": "none",
            "evidence_blocks_supplied": 0,
            "pages_supplied": [],
            "lookups": [],
            "evidence_blocks": [],
            "tokens": {"in": 0, "out": 0},
            "total_ms": 0,
            "citations_rejected": [],
            "citations_not_verbatim": [],
            "citations_repaired": [],
            "citations_repaged": [],
            "unsupported_comparisons": [],
            "unsupported_timing": [],
            "interventions": 0,
            "clean": True,
        },
    }


def apply_floor(grounded: dict, gate: dict) -> dict:
    """Let the gate raise urgency after grounding, never lower it."""
    gate_esi = gate.get("esi")
    if gate_esi is None:
        return grounded

    current = grounded.get("grounded_esi")
    if current is None or gate_esi < current:
        log.info("gate floor: ESI %s -> %s (%s)", current, gate_esi,
                 gate.get("matched_rule_id"))
        grounded["grounded_esi"] = gate_esi
        for concern in grounded.get("concerns", []):
            if concern.get("final_esi", 9) > gate_esi:
                concern["final_esi"] = gate_esi

    grounded["red_flag_gate"] = gate
    return grounded
