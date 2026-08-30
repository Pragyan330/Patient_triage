"""HTTP service joining the three modules end to end.

    intake form (OP, Express :8080)
        -> POST /api/grounded        this service grounds it
        -> registry                  patient enters the queue
        -> POST /api/patients/{id}/vitals   or   POST /api/sweep
        -> retriage_loop (Priyam), citations verified against the corpus
        -> GET /api/queue            what the nurse sees

Everything is localhost - no hosting, no public URLs. Ports in service/ports.py:

    8080  intake      Express (OP)
    8000  grounding   FastAPI (this)
    5173  queue UI    Vite (Priyam)

Run all three:
    python scripts/run_all.py

Or this one alone:
    .venv/Scripts/python.exe -m uvicorn service.app:app --port 8000 --reload
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# retriage_loop.py sits at the repo root, not in a package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grounding_module import ground
from grounding_module import news2
from service.feed import as_demo_patient
from service import surge
from service.gate import apply_floor, as_grounded, run_gate
from service.ports import CORS_ORIGINS, GROUNDED_ENDPOINT, GROUNDING_PORT
from service.store import registry
from service.verify import verify_event

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
log = logging.getLogger("service")

app = FastAPI(title="Patient triage - grounding + re-triage",
              version="0.1.0")

# The two UIs are served from their own localhost ports.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    from service.verify import _retriever
    retriever = _retriever()
    return {
        "ok": True,
        "corpus_pages": retriever.n,
        "patients": len(registry.all()),
        "clock_minute": registry.now_minute(),
        "grounded_endpoint": GROUNDED_ENDPOINT,
        "port": GROUNDING_PORT,
    }


@app.post("/api/grounded")
def receive_initial(initial: dict = Body(...)) -> dict:
    """Entry point. Takes the upstream initial assessment, grounds it, admits.

    This is the URL OP's server forwards to (PRAGYAN_SERVER_URL).
    """
    if not isinstance(initial, dict) or "vitals_read" not in initial:
        raise HTTPException(422, "Expected an initial assessment schema with vitals_read")

    started = time.perf_counter()

    # Deterministic screen first. It reads structured fields and returns in
    # microseconds, so a pulseless patient is never held behind a 15s retrieval.
    gate = run_gate(initial)
    surge.monitor.record_arrival()

    try:
        if gate.get("bypasses_pipeline"):
            grounded = as_grounded(initial, gate, news2.from_schema(initial).as_dict())
        elif surge.monitor.should_degrade(gate.get("esi") or initial.get("implied_esi")):
            # Ration retrieval by acuity, never by arrival order. See surge.py.
            scored = news2.from_schema(initial).as_dict()
            esi = gate.get("esi") or initial.get("implied_esi") or 4
            grounded = apply_floor(surge.as_degraded(initial, scored, esi, gate), gate)
            log.info("surge: %s triaged without retrieval at ESI %s",
                     initial.get("patient_id"), esi)
        else:
            grounded = apply_floor(ground(initial), gate)
    except Exception as exc:
        # A patient must never fall out of the queue because retrieval failed.
        # Returning 500 here meant the intake server logged a warning and the
        # patient simply did not exist - the worst outcome the system can
        # produce, worse than any mis-triage, and it happened six times in the
        # first surge run when Mistral rate-limited us.
        #
        # Fail safe instead: admit them on the deterministic score alone, at
        # the more urgent of the gate's level and their provisional one, and
        # say plainly that the assessment is incomplete.
        log.exception("grounding failed for %s - admitting on rules alone",
                      initial.get("patient_id"))
        scored = news2.from_schema(initial).as_dict()
        fallback_esi = min(
            [e for e in (gate.get("esi"), initial.get("implied_esi")) if e] or [2])
        grounded = apply_floor(
            surge.as_degraded(initial, scored, fallback_esi, gate), gate)
        grounded["degraded_reason"] = "grounding_unavailable"
        grounded["confidence"] = {
            "level": "low",
            "score": 0.3,
            "red_flag_rule": gate.get("matched_rule_id"),
            "reasons": [
                f"Protocol retrieval failed ({type(exc).__name__}), so this level "
                f"rests only on the red-flag rules and the NEWS2 score.",
                "Admitted at the more urgent available level rather than dropped: "
                "a patient missing from the queue is worse than one over-triaged.",
                "Needs a full assessment when capacity allows.",
            ],
            "escalated_for_uncertainty": False,
        }
        grounded["concerns"][0]["nurse_summary"] = (
            f"ESI {fallback_esi} from rules and NEWS2 only - protocol retrieval was "
            f"unavailable. No citation behind this. Reassess when you can.")

    record = registry.admit(grounded, initial=initial)
    elapsed = int((time.perf_counter() - started) * 1000)
    audit = grounded.get("_audit", {})
    log.info("admitted %s  esi=%s  news2=%s  %d citations  %d ms",
             record.patient_id, grounded.get("grounded_esi"),
             (grounded.get("news2") or {}).get("total"),
             sum(len(c.get("evidence") or []) for c in grounded.get("concerns", [])),
             elapsed)

    return {
        "patient_id": record.patient_id,
        "grounded": grounded,
        "queue_position": next(
            (i + 1 for i, row in enumerate(registry.queue())
             if row["patient_id"] == record.patient_id), None),
        "took_ms": elapsed,
        "citations_clean": audit.get("clean"),
        "red_flag_gate": {
            "result": gate.get("gate_result"),
            "rule": gate.get("matched_rule_id"),
            "esi": gate.get("esi"),
            "bypassed_retrieval": bool(gate.get("bypasses_pipeline")),
            "missing_fields": gate.get("missing_fields") or [],
            "low_confidence": bool(gate.get("low_confidence")),
        },
    }


@app.get("/api/surge")
def surge_status() -> dict:
    """Whether the department is surging, and what that changes."""
    return surge.monitor.status()


@app.post("/api/surge")
def set_surge(body: dict = Body(...)) -> dict:
    """Force surge mode on or off for a demo. null returns to measuring."""
    surge.monitor.forced = body.get("forced")
    log.info("surge mode forced=%s", surge.monitor.forced)
    return surge.monitor.status()


@app.get("/api/patients.json")
def patients_feed() -> list[dict]:
    """The live queue in retriage-demo's sample_patients.json shape.

    Lets Priyam's component swap its static import for a fetch without
    restructuring anything.
    """
    minute = registry.now_minute()
    rows = [as_demo_patient(r, minute) for r in registry.all()]
    rows.sort(key=lambda r: (r["current_esi_floor"], r["arrival_minute"]))
    return rows


@app.get("/api/queue")
def queue() -> dict:
    """The nurse's view: most urgent first, longest waiting first within a level."""
    return {"clock_minute": registry.now_minute(), "patients": registry.queue()}


@app.get("/api/patients/{patient_id}")
def patient(patient_id: str) -> dict:
    record = registry.get(patient_id)
    if record is None:
        raise HTTPException(404, f"No patient {patient_id}")
    return {
        "summary": record.summary(),
        "grounded": record.grounded,
        "events": record.events,
    }


@app.post("/api/patients/{patient_id}/vitals")
def new_vitals(patient_id: str, vitals: dict = Body(...)) -> dict:
    """New observations for a waiting patient. Runs one re-triage check."""
    if registry.get(patient_id) is None:
        raise HTTPException(404, f"No patient {patient_id}")

    event = registry.record_vitals(patient_id, vitals)
    if event.get("skipped"):
        return event

    event = verify_event(event)
    log.info("retriage %s  %s  esi %s -> %s", patient_id,
             event["trigger"]["type"], event["previous_esi_floor"],
             event["new_esi_floor"])

    # Also answer in PragyanResponse shape so retriage-demo can drop its mock
    # without reshaping anything. The full event stays under `event`.
    esi = event["new_esi_floor"]
    return {
        "patient_id": patient_id,
        "concerns": [{
            "concern": event["trigger"]["detail"] or "Physiological change",
            "clinical_shorthand": f"?{event['trigger']['type'].replace('_', ' ')}",
            "implied_esi": event["previous_esi_floor"],
            "final_esi": esi,
            "time_to_treatment_minutes": event.get("next_check_due_minutes"),
            "evidence": event.get("evidence") or [],
            "nurse_summary": event["nurse_summary"],
        }],
        "provisional_esi": event["previous_esi_floor"],
        "grounded_esi": esi,
        "retrieval_ms": 0,
        "event": event,
    }


@app.post("/api/patients/{patient_id}/override")
def override(patient_id: str, body: dict = Body(...)) -> dict:
    """A nurse setting the ESI by hand — the only path allowed to de-escalate.

    The automatic loop is an escalate-only ratchet on purpose. This sits
    outside it, records who decided and why, and flags a de-escalation for
    review so it is visible to whoever looks at the queue next.
    """
    if registry.get(patient_id) is None:
        raise HTTPException(404, f"No patient {patient_id}")

    esi = body.get("esi")
    if esi is None:
        raise HTTPException(422, "esi is required (1-5)")

    reason = (body.get("reason") or "").strip()
    try:
        target = int(esi)
    except (TypeError, ValueError):
        raise HTTPException(422, f"esi must be a number, got {esi!r}")

    # De-escalation moves a patient further down the queue. That is the one
    # direction that can harm by omission, so it does not happen unexplained.
    current = registry.get(patient_id).state.esi_floor
    if target > current and not reason:
        raise HTTPException(
            422, "A reason is required to de-escalate: this moves the patient "
                 "further down the queue.")

    try:
        event = registry.override_esi(patient_id, target, reason=reason,
                                      by=(body.get("by") or "nurse").strip())
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return event


@app.post("/api/sweep")
def sweep() -> dict:
    """Periodic pass over the whole queue. No new vitals - time-based only."""
    events = [verify_event(e) for e in registry.sweep()]
    return {
        "clock_minute": registry.now_minute(),
        "checked": len(registry.all()),
        "events": events,
        "escalated": [e["patient_id"] for e in events if e.get("escalated")],
    }
