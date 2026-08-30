"""What changes when the department is swamped.

A quiet shift and a surge are not the same problem. Grounding costs 6-15s of
retrieval and a model call per patient; at three times normal arrivals that
queue grows faster than it drains, and the patients who suffer are the ones
still waiting to be seen at all.

WHAT WE GIVE UP, AND WHAT WE REFUSE TO
--------------------------------------
Under surge the expensive path is rationed by acuity, never by arrival order:

  ESI 1-3   full grounding. Citations are what make a high-acuity decision
            reviewable, and these are the patients where being wrong costs
            most. They are never degraded.

  ESI 4-5   gate and deterministic NEWS2 only, no retrieval. The acuity still
            comes from the red-flag rules and the score; what is lost is the
            protocol quotation behind it, and the record says so.

The alternative - degrading everyone equally - trades a little latency for the
sick in exchange for a lot of it, which is the wrong way round. The alternative
of degrading nobody means the twentieth patient waits for the nineteenth.

Nothing here can lower an acuity. A patient triaged in surge mode carries
`degraded: true` and keeps their full confidence reasoning, so a clinician can
see they were assessed under load and ask for the full workup if they want it.
"""
from __future__ import annotations

import logging
import time
from collections import deque

log = logging.getLogger(__name__)

# Arrivals per minute above which the department counts as surging. A typical
# ED at 100-500 visits/day runs well under one arrival a minute; three times
# that is the brief's surge. Deliberately low so the demo can show the switch.
SURGE_ARRIVALS_PER_MIN = 3.0
WINDOW_SECONDS = 120.0

# Acuity at or below which retrieval may be skipped while surging.
DEGRADE_AT_OR_ABOVE_ESI = 4


class SurgeMonitor:
    """Tracks arrival rate over a rolling window."""

    def __init__(self, threshold: float = SURGE_ARRIVALS_PER_MIN,
                 window: float = WINDOW_SECONDS):
        self.threshold = threshold
        self.window = window
        self._arrivals: deque[float] = deque()
        self.forced: bool | None = None      # manual override for demos

    def record_arrival(self) -> None:
        now = time.monotonic()
        self._arrivals.append(now)
        while self._arrivals and now - self._arrivals[0] > self.window:
            self._arrivals.popleft()

    @property
    def arrivals_per_min(self) -> float:
        if not self._arrivals:
            return 0.0
        span = max(time.monotonic() - self._arrivals[0], 1.0)
        return len(self._arrivals) * 60.0 / span

    @property
    def surging(self) -> bool:
        if self.forced is not None:
            return self.forced
        return self.arrivals_per_min >= self.threshold

    def should_degrade(self, provisional_esi: int | None) -> bool:
        """Skip retrieval for this patient?

        Only while surging, and only for the least urgent. An unknown acuity
        is treated as urgent - not knowing is not a reason to do less.
        """
        if not self.surging:
            return False
        if provisional_esi is None:
            return False
        return provisional_esi >= DEGRADE_AT_OR_ABOVE_ESI

    def status(self) -> dict:
        return {
            "surging": self.surging,
            "arrivals_per_min": round(self.arrivals_per_min, 1),
            "threshold_per_min": self.threshold,
            "forced": self.forced,
            "policy": (f"Surging: ESI {DEGRADE_AT_OR_ABOVE_ESI}+ triaged without "
                       f"retrieval to keep capacity for the sick."
                       if self.surging else
                       "Normal load: every patient gets full grounding."),
        }


monitor = SurgeMonitor()


def as_degraded(initial: dict, news2: dict, esi: int, gate: dict) -> dict:
    """A grounded-shaped result produced without retrieval."""
    return {
        "patient_id": initial.get("patient_id") or "",
        "concerns": [{
            "concern": initial.get("concern") or "Low-acuity presentation",
            "clinical_shorthand": initial.get("concern", "")[:40] or "low acuity",
            "implied_esi": initial.get("implied_esi") or esi,
            "final_esi": esi,
            "time_to_treatment_minutes": None,
            "evidence": [],
            "nurse_summary": (
                f"ESI {esi} from the red-flag screen and NEWS2 only. Retrieval was "
                f"skipped because the department is surging and capacity is being "
                f"kept for higher-acuity patients. No protocol citation behind this "
                f"one - ask for a full assessment if anything looks off."),
        }],
        "provisional_esi": initial.get("implied_esi") or esi,
        "grounded_esi": esi,
        "news2": news2,
        "retrieval_ms": 0,
        "degraded": True,
        "degraded_reason": "surge",
        "red_flag_gate": gate,
        "confidence": {
            "level": "moderate",
            "score": 0.6,
            "reasons": [
                "Triaged under surge without protocol retrieval.",
                "Acuity is from deterministic rules and the NEWS2 score, which are "
                "unaffected by load; only the citation is missing.",
            ],
            "escalated_for_uncertainty": False,
        },
        "_audit": {
            "model": "none - surge mode",
            "retriever": "none",
            "evidence_blocks_supplied": 0, "pages_supplied": [],
            "lookups": [], "evidence_blocks": [],
            "tokens": {"in": 0, "out": 0}, "total_ms": 0,
            "citations_rejected": [], "citations_not_verbatim": [],
            "citations_repaired": [], "citations_repaged": [],
            "unsupported_comparisons": [], "unsupported_timing": [],
            "interventions": 0, "clean": True,
        },
    }
