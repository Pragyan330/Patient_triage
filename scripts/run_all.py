"""Start all three modules on localhost and keep them running.

    python scripts/run_all.py            # everything
    python scripts/run_all.py grounding  # just one or two

    8080  intake      Express (OP)      http://localhost:8080/patient_info
    8000  grounding   FastAPI (us)      http://localhost:8000/docs
    5173  queue UI    Vite (Priyam)     http://localhost:5173

No hosting and no public URLs: each module talks to the next over loopback.
Ctrl-C stops all of them together.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from grounding_module.config import Config, load_dotenv_if_present
from service.ports import (GROUNDED_ENDPOINT, GROUNDING_PORT, INTAKE_PORT,
                           QUEUE_UI_PORT)


def mistral_key() -> str | None:
    """Find the key wherever it lives and hand it to every child process.

    The key sits in a .env *outside* the repo so it cannot be committed. The
    Python side looks there; `dotenv.config()` in app.js only looks in ./ and
    finds nothing. Passing it through the environment beats writing a second
    copy of the key inside the repo, one `git add -f` away from leaking.
    """
    load_dotenv_if_present()
    try:
        return Config().api_key()
    except RuntimeError:
        return None

VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PYTHON = str(VENV_PY) if VENV_PY.exists() else sys.executable

KEY = None      # resolved in main(), passed to child processes

COLOURS = {"grounding": "\033[36m", "intake": "\033[33m", "queue-ui": "\033[35m"}
DIM, OFF = "\033[90m", "\033[0m"


class Service:
    def __init__(self, name: str, cmd: list[str], cwd: Path, port: int,
                 needs: Path | None = None, env: dict | None = None):
        self.name, self.cmd, self.cwd, self.port = name, cmd, cwd, port
        self.needs = needs          # a path that must exist, else skip
        self.env = env or {}
        self.proc: subprocess.Popen | None = None

    def missing_reason(self) -> str | None:
        if self.needs and not self.needs.exists():
            return f"{self.needs.relative_to(ROOT)} not found"
        return None

    def start(self) -> bool:
        reason = self.missing_reason()
        if reason:
            print(f"  {DIM}skip  {self.name:10s} {reason}{OFF}")
            return False
        environment = {**os.environ, **self.env}
        self.proc = subprocess.Popen(
            self.cmd, cwd=str(self.cwd), env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            shell=(os.name == "nt" and self.cmd[0] in ("npm", "npx")),
        )
        threading.Thread(target=self._pump, daemon=True).start()
        print(f"  start {self.name:10s} :{self.port}")
        return True

    def _pump(self) -> None:
        tag = f"{COLOURS.get(self.name, '')}{self.name:>9}{OFF}"
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            print(f"{tag} | {line.rstrip()}", flush=True)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def wait_until_up(url: str, timeout: float = 45.0) -> bool:
    """Poll a URL until it answers. Opening a tab before the server is
    listening just shows the browser's own error page."""
    import urllib.error
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except urllib.error.HTTPError:
            return True          # answered, just not with a 200
        except Exception:
            time.sleep(0.5)
    return False


def open_tabs(started: list[Service]) -> None:
    """Open the two pages you actually work in, once they are ready.

    Only the intake form and the queue UI. The API docs and the raw feed are
    printed above for when they are wanted - opening four tabs every launch is
    noise, not convenience.
    """
    import webbrowser

    running = {s.name for s in started}
    targets = []
    if "intake" in running:
        targets.append((f"http://localhost:{INTAKE_PORT}/patient_info",
                        f"http://localhost:{INTAKE_PORT}/"))
    if "queue-ui" in running:
        targets.append((f"http://localhost:{QUEUE_UI_PORT}/",
                        f"http://localhost:{QUEUE_UI_PORT}/"))

    def opener() -> None:
        for page, probe in targets:
            if wait_until_up(probe):
                webbrowser.open(page)
                time.sleep(0.8)     # let the browser settle between tabs
            else:
                print(f"  {DIM}not opening {page} - never came up{OFF}")

    threading.Thread(target=opener, daemon=True).start()


def build(names: set[str]) -> list[Service]:
    services = [
        Service(
            "grounding",
            [PYTHON, "-m", "uvicorn", "service.app:app",
             "--host", "127.0.0.1", "--port", str(GROUNDING_PORT)],
            ROOT, GROUNDING_PORT,
        ),
        Service(
            "intake",
            ["node", "app.js"],
            ROOT, INTAKE_PORT,
            needs=ROOT / "node_modules",
            # so OP's server forwards to us, and can reach Mistral, without
            # anyone editing a .env
            env={"PRAGYAN_SERVER_URL": GROUNDED_ENDPOINT,
                 **({"MISTRAL_API_KEY": KEY} if KEY else {})},
        ),
        Service(
            "queue-ui",
            ["npm", "run", "dev", "--", "--port", str(QUEUE_UI_PORT)],
            ROOT / "retriage-demo", QUEUE_UI_PORT,
            needs=ROOT / "retriage-demo" / "node_modules",
        ),
    ]
    return [s for s in services if not names or s.name in names]


def main() -> None:
    global KEY
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    KEY = mistral_key()

    if not KEY:
        print("  \033[33mwarning\033[0m no Mistral key found - intake will fail. "
              "Set mistral_api or MISTRAL_API_KEY in a .env.")

    # flags are not service names; without this, `--no-open` was read as a
    # service, matched nothing, and the launcher exited
    wanted = {a for a in sys.argv[1:] if not a.startswith("-")}
    services = build(wanted)
    if not services:
        sys.exit(f"No such service: {', '.join(sorted(wanted))}. "
                 f"Choose from: grounding, intake, queue-ui")

    print("\n\033[1mpatient triage - all local\033[0m")
    started = [s for s in services if s.start()]
    if not started:
        sys.exit("\nNothing started. Run `npm install` for the Node services.")

    print(f"\n  intake form   http://localhost:{INTAKE_PORT}/patient_info")
    print(f"  queue UI      http://localhost:{QUEUE_UI_PORT}")
    print(f"  API docs      http://localhost:{GROUNDING_PORT}/docs")
    print(f"  live feed     http://localhost:{GROUNDING_PORT}/api/patients.json")
    print(f"\n  {DIM}intake forwards to {GROUNDED_ENDPOINT}{OFF}")
    print(f"  {DIM}Ctrl-C to stop everything{OFF}\n")

    if "--no-open" not in sys.argv:
        open_tabs(started)

    def shutdown(*_):
        print("\n  stopping...")
        for s in started:
            s.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    try:
        while True:
            for s in started:
                if s.proc and s.proc.poll() is not None:
                    print(f"\n  {s.name} exited with code {s.proc.returncode}")
                    shutdown()
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
