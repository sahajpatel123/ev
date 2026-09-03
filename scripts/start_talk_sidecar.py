"""Talk sidecar on 127.0.0.1:18000.

EV.app reads EV_API_URL from ~/Library/Application Support/EV/api.env.
This process must share the owner Postgres that holds ingested life-archive
events. Isolating it onto an empty sqlite file made live Evie report no
reliable record while the archive lived on :8000's database.

Do not start a bare ``uv run uvicorn … --port 18000``. That process loads
shared ``backend/.env`` with ``EV_LAPTOP_FILES=false`` and Talk then
speaks "Local file access is not enabled on this API."
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path("/Users/sahajpatel/Code/ev")
SUPPORT = Path.home() / "Library" / "Application Support" / "EV"
LOGS = Path.home() / "Library" / "Logs" / "ev"


def daemonize() -> None:
    """Detach so Talk survives the agent shell that launched this script."""

    if os.environ.get("EV_TALK_SIDECAR_FOREGROUND") == "1":
        return
    LOGS.mkdir(parents=True, exist_ok=True)
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir(str(REPO / "backend"))
    devnull = os.open("/dev/null", os.O_RDONLY)
    out = os.open(
        str(LOGS / "talk-sidecar.out.log"),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    err = os.open(
        str(LOGS / "talk-sidecar.err.log"),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    os.dup2(devnull, 0)
    os.dup2(out, 1)
    os.dup2(err, 2)
    for fd in (devnull, out, err):
        if fd not in (0, 1, 2):
            os.close(fd)


def load(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        os.environ.setdefault(key, val)


def main() -> None:
    daemonize()
    load(REPO / ".env")
    load(REPO / "backend" / ".env")
    SUPPORT.mkdir(parents=True, exist_ok=True)
    # Keep EV_DATABASE_URL and EV_VOICE_LIVE_MODE from .env so Talk sees the
    # same archive and shadow surface the owner actually uses.
    os.environ["EV_ENVIRONMENT"] = "dev"
    # Must overwrite after load(): shared backend/.env keeps this false so
    # production :8000 never grows a Python-side home-folder writer.
    os.environ["EV_LAPTOP_FILES"] = "true"
    os.environ["EV_HOME_STATION_MODE"] = "false"
    os.environ["EV_PROCESSING_MODE"] = "sync"
    os.environ["EV_MAINTENANCE_MODE"] = "0"
    os.environ.setdefault("EV_VOICEPRINT_PROVIDER", "campp")
    os.environ.setdefault("EV_SEARCH_PROVIDER", "live")
    os.environ["PATH"] = (
        "/Users/sahajpatel/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
        + os.environ.get("PATH", "")
    )
    os.chdir(REPO / "backend")
    os.execv(
        "/Users/sahajpatel/.local/bin/uv",
        ["uv", "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "18000"],
    )


if __name__ == "__main__":
    main()
