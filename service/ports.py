"""Everything runs on localhost. One port per module, no hosting.

    8080  intake      Express (OP)      form + Mistral -> initial schema
    8000  grounding   FastAPI (this)    ground + re-triage + queue feed
    5173  queue UI    Vite (Priyam)     nurse-facing queue

Each is overridable by environment variable so nobody has to edit code when a
port is already taken. The defaults are wired to work with zero configuration
on a fresh clone - that is the point.
"""
from __future__ import annotations

import os

INTAKE_PORT = int(os.getenv("INTAKE_PORT", "8080"))
GROUNDING_PORT = int(os.getenv("GROUNDING_PORT", "8000"))
QUEUE_UI_PORT = int(os.getenv("QUEUE_UI_PORT", "5173"))

HOST = os.getenv("SERVICE_HOST", "127.0.0.1")


def url(port: int, path: str = "") -> str:
    return f"http://{HOST}:{port}{path}"


INTAKE_URL = url(INTAKE_PORT)
GROUNDING_URL = url(GROUNDING_PORT)
QUEUE_UI_URL = url(QUEUE_UI_PORT)

# What OP's server should POST its generated schema to.
GROUNDED_ENDPOINT = url(GROUNDING_PORT, "/api/grounded")

# Browsers enforce origin on the UI ports; allow both loopback spellings
# because a browser will use whichever the user typed.
CORS_ORIGINS = [
    f"http://localhost:{INTAKE_PORT}",
    f"http://127.0.0.1:{INTAKE_PORT}",
    f"http://localhost:{QUEUE_UI_PORT}",
    f"http://127.0.0.1:{QUEUE_UI_PORT}",
]
