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
from service.feed import as_demo_patient
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
    try:
        grounded = ground(initial)
    except Exception as exc:
        log.exception("grounding failed")
        raise HTTPException(500, f"Grounding failed: {type(exc).__name__}: {exc}")

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
    }


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
