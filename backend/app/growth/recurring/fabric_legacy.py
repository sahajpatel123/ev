"""rec-fabric-legacy: EV_FABRIC=0 legacy path still works (A6, $0)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, wilson

PROBE = """
import asyncio
from app.fabric.facade import fabric_enabled
assert fabric_enabled() is False, "fabric should be off under EV_FABRIC=0"
from app.cognitive.kernel import handle_turn
res = asyncio.run(handle_turn(transcript="hello", modality="typed",
                              session=None, actor="master", persist_memory=False))
print("KIND=" + str(getattr(res, "kind", "?")))
print("UNAV=" + str(bool(getattr(res, "unavailable", False))))
"""


def run() -> dict:
    res = new_result("rec-fabric-legacy")
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="ge-fabric-legacy-"))
    env = suite_env()
    env["EV_FABRIC"] = "0"
    env["EV_CHAT_PROVIDER"] = "echo"
    env["EV_STORAGE_ROOT"] = str(tmp / "storage")
    env["EV_DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/legacy.db"
    backend = Path(__file__).resolve().parents[3]
    p = subprocess.run([sys.executable, "-c", PROBE], cwd=str(backend),
                       capture_output=True, text=True, env=env, timeout=300)
    out = p.stdout or ""
    kind = next((ln.split("=", 1)[1] for ln in out.splitlines() if ln.startswith("KIND=")), "?")
    unav = next((ln.split("=", 1)[1] for ln in out.splitlines() if ln.startswith("UNAV=")), "True")
    ok = p.returncode == 0 and unav == "False"
    res["cases_run"] = ["legacy-turn-echo"]
    if not ok:
        res["failures"] = [{"id": "legacy-turn-echo",
                            "repro": "EV_FABRIC=0 EV_CHAT_PROVIDER=echo python -c <probe>",
                            "detail": ((p.stdout or "") + (p.stderr or ""))[-300:]}]
    res["coverage_note"] = (f"EV_FABRIC=0 + echo provider; turn kind={kind}; "
                            "legacy path must complete without the fabric")
    return finish(res, counts=(1, 1 if ok else 0, 0 if ok else 1, 0, 0),
                  categories=[{"name": "legacy", "n": 1, "passed": 1 if ok else 0,
                               "rate": 1.0 if ok else 0.0,
                               "wilson95": wilson(1, 1 if ok else 0)}])
