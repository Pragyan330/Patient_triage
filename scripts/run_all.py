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


def node_env() -> dict[str, str]:
    """Secrets the Node side needs, found wherever they actually live.

    They sit in a .env *outside* the repo so they cannot be committed. The
    Python side looks there; `dotenv.config()` in app.js only looks in ./ and
    finds nothing, so the intake server would run with no Mistral key and no
    database. Forwarding through the environment beats writing a second copy
    of the secrets inside the repo, one `git add -f` away from leaking.

    MONGO_URI is optional - app.js warns and skips persistence without it.
    """
    load_dotenv_if_present()
    passed: dict[str, str] = {}
    try:
        passed["MISTRAL_API_KEY"] = Config().api_key()
    except RuntimeError:
        pass
    mongo = os.getenv("MONGO_URI")
    if mongo:
        passed["MONGO_URI"] = mongo
    return passed

VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PYTHON = str(VENV_PY) if VENV_PY.exists() else sys.executable

NODE_ENV: dict = {}     # resolved in main(), passed to child processes

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


def port_owner(port: int) -> str | None:
    """Return a description of whatever is already listening on `port`.

    Checked up front because the failure is otherwise unreadable: uvicorn dies
    with WinError 10048, the launcher reports "exited with code 3", and Vite
    quietly drifts to 5175 while the intake form still redirects to 5173. The
    usual cause is simply an earlier run that was never stopped.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        if probe.connect_ex(("127.0.0.1", port)) != 0:
            return None

    try:                                     # name the process if we can
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=8).stdout
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                pid = line.split()[-1]
                name = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True, text=True, timeout=8).stdout.split()
                return f"PID {pid}" + (f" ({name[0]})" if name else "")
    except Exception:
        pass
    return "another process"


def check_ports(services: list[Service]) -> list[Service]:
    """Refuse to start a service whose port is taken, and say what has it."""
    free = []
    for s in services:
        owner = port_owner(s.port)
        if owner:
            print(f"  {DIM}skip  {s.name:10s} :{s.port} already in use by {owner}{OFF}")
        else:
            free.append(s)
    return free


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
            env={"PRAGYAN_SERVER_URL": GROUNDED_ENDPOINT, **NODE_ENV},
        ),
        Service(
            "queue-ui",
            # --strictPort because Vite otherwise falls forward to the next
            # free port, leaving the UI on 5175 while the intake form still
            # redirects to 5173. Failing loudly beats moving silently.
            ["npm", "run", "dev", "--", "--port", str(QUEUE_UI_PORT), "--strictPort"],
            ROOT / "retriage-demo", QUEUE_UI_PORT,
            needs=ROOT / "retriage-demo" / "node_modules",
        ),
    ]
    return [s for s in services if not names or s.name in names]


def main() -> None:
    global NODE_ENV
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    NODE_ENV = node_env()

    if "MISTRAL_API_KEY" not in NODE_ENV:
        print("  \033[33mwarning\033[0m no Mistral key found - intake will fail. "
              "Set mistral_api or MISTRAL_API_KEY in a .env.")
    if "MONGO_URI" not in NODE_ENV:
        print(f"  {DIM}no MONGO_URI - intake runs without persistence{OFF}")

    # flags are not service names; without this, `--no-open` was read as a
    # service, matched nothing, and the launcher exited
    wanted = {a for a in sys.argv[1:] if not a.startswith("-")}
    services = build(wanted)
    if not services:
        sys.exit(f"No such service: {', '.join(sorted(wanted))}. "
                 f"Choose from: grounding, intake, queue-ui")

    print("\n\033[1mpatient triage - all local\033[0m")
    services = check_ports(services)
    if not services:
        sys.exit("\nEvery port is already in use. An earlier run is probably still "
                 "going - stop it, or close the terminal it is in, and try again.")

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
