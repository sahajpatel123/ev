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
import sys
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


_META_SECRET_NAMES = frozenset(
    {"META_MODEL_API_KEY", "EV_META_MODEL_API_KEY", "MODEL_API_KEY"}
)


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
        # An empty shell export must not hide the overlay Meta key.
        if key in _META_SECRET_NAMES:
            if val and not (os.environ.get(key) or "").strip():
                os.environ[key] = val
            continue
        os.environ.setdefault(key, val)


def alias_meta_model_keys() -> None:
    """Mirror config.py: Meta docs, overlay, and EV_ names are one secret."""

    docs = (os.environ.get("MODEL_API_KEY") or "").strip()
    meta = (os.environ.get("META_MODEL_API_KEY") or "").strip() or docs
    ev_meta = (os.environ.get("EV_META_MODEL_API_KEY") or "").strip()
    if docs and not (os.environ.get("META_MODEL_API_KEY") or "").strip():
        os.environ["META_MODEL_API_KEY"] = docs
    if meta and not ev_meta:
        os.environ["EV_META_MODEL_API_KEY"] = meta
    elif ev_meta and not meta:
        os.environ["META_MODEL_API_KEY"] = ev_meta


def muse_selected() -> bool:
    intel = (
        os.environ.get("EV_INTELLIGENCE_PROVIDER")
        or os.environ.get("EV_CHAT_PROVIDER")
        or ""
    ).strip().lower()
    hearing = (os.environ.get("EV_VOICE_ASR_PROVIDER") or "").strip().lower()
    return intel in {"meta_muse_spark", "muse", "muse_spark"} or hearing in {
        "meta_muse_voice",
        "muse_voice",
    }


def meta_key_loaded() -> bool:
    return bool(
        (os.environ.get("META_MODEL_API_KEY") or "").strip()
        or (os.environ.get("EV_META_MODEL_API_KEY") or "").strip()
        or (os.environ.get("MODEL_API_KEY") or "").strip()
    )


def refuse_muse_without_key() -> None:
    """Fail closed before daemonize so a missing Meta key is visible."""

    alias_meta_model_keys()
    if muse_selected() and not meta_key_loaded():
        sys.stderr.write(
            "Talk sidecar refused to start: META_MODEL_API_KEY is missing "
            "while Muse is selected.\n"
        )
        raise SystemExit(2)


def talk_listen_pids() -> list[int]:
    """PIDs listening on Talk's port. Never used to touch production :8000."""

    import subprocess

    try:
        proc = subprocess.run(
            ["lsof", "-nP", "-tiTCP:18000", "-sTCP:LISTEN"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for raw in (proc.stdout or "").split():
        try:
            pid = int(raw)
        except ValueError:
            continue
        if pid > 1:
            pids.append(pid)
    return pids


def stop_existing_talk_sidecar() -> None:
    """Replace the current Talk process so Muse can bind :18000.

    Called only after refuse_muse_without_key(). Does not touch ev.api :8000.
    """

    import signal
    import time

    me = os.getpid()
    for pid in talk_listen_pids():
        if pid == me:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.time() + 8.0
    while time.time() < deadline:
        left = [pid for pid in talk_listen_pids() if pid != me]
        if not left:
            return
        time.sleep(0.2)
    for pid in talk_listen_pids():
        if pid == me:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue


def main() -> None:
    load(REPO / ".env")
    load(REPO / "backend" / ".env")
    # Production secrets overlay (META_MODEL_API_KEY). setdefault so an
    # explicit operator env still wins; config.py aliases the Meta names.
    secrets = Path(os.environ.get("EV_SECRETS_FILE", str(Path.home() / ".ev/secrets/production.env"))).expanduser()
    load(secrets)
    refuse_muse_without_key()
    stop_existing_talk_sidecar()
    daemonize()
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
