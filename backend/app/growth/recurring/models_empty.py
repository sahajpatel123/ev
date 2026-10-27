"""rec-models-empty: `ev doctor --fabric` with an empty models dir (A9).

A9's question is "does an empty models dir break the stack?" — not "is the
app launched?". Verdict:
- any FAIL line naming models/weights/imports -> FAIL (A9 violation);
- no FAIL lines at all -> PASS;
- FAILs confined to bridge/TCC while the bridge socket is absent (app
  genuinely not launched, proven by the missing socket) -> SKIP with an
  honest note (the empty-models path was still exercised: nothing
  model-related failed);
- anything else -> FAIL with the lines.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, wilson

MODEL_RE = re.compile(r"model|weight|onnx|torch|checkpoint|\.bin|\.pt\b|ImportError|ModuleNotFound", re.I)
ENV_OK = ("bridge", "tcc", "accessibility", "full_disk", "screen_recording",
          "microphone", "input_monitoring", "contacts", "calendars", "reminders")


def run() -> dict:
    res = new_result("rec-models-empty")
    tmp = Path(tempfile.mkdtemp(prefix="ge-models-empty-"))
    env = suite_env()
    env["EV_ML_MODEL_DIR"] = str(tmp)
    env["EV_MODEL_DIR"] = str(tmp)
    backend = Path(__file__).resolve().parents[3]
    p = subprocess.run([sys.executable, "-m", "clients.cli", "doctor", "--fabric"],
                       cwd=str(backend), capture_output=True, text=True,
                       env=env, timeout=300)
    out = (p.stdout or "") + (p.stderr or "")
    fails = [ln.strip() for ln in out.splitlines() if "[FAIL]" in ln]
    res["cases_run"] = ["doctor-fabric-empty-models"]
    model_fails = [ln for ln in fails if MODEL_RE.search(ln)]
    if model_fails:
        res["failures"] = [{"id": "doctor-fabric-empty-models",
                            "repro": "EV_ML_MODEL_DIR=$(mktemp -d) ev doctor --fabric",
                            "detail": "A9: " + " | ".join(model_fails)[:280]}]
        counts: tuple = (1, 0, 1, 0, 0)
        note = f"exit={p.returncode}; model-related failure with empty models dir"
    elif not fails and p.returncode == 0:
        counts = (1, 1, 0, 0, 0)
        note = "exit=0; clean with empty models dir"
    elif not fails:
        res["failures"] = [{"id": "doctor-fabric-empty-models",
                            "repro": "EV_ML_MODEL_DIR=$(mktemp -d) ev doctor --fabric",
                            "detail": f"exit={p.returncode} with no [FAIL] line (crash?) :: {out[-200:]}"}]
        counts = (1, 0, 1, 0, 0)
        note = f"exit={p.returncode} with no [FAIL] line"
    else:
        from app.fabric.bridge_client import socket_path
        app_down = not socket_path().exists()
        environmental = all(any(k in ln for k in ENV_OK) for ln in fails)
        if environmental and app_down:
            counts = (1, 0, 0, 1, 0)
            note = ("empty-models path exercised, nothing model-related failed; "
                    "bridge/TCC FAILs only because Evie.app is not launched "
                    "(socket absent) — OWNER-ACTION, not an A9 verdict")
        else:
            res["failures"] = [{"id": "doctor-fabric-empty-models",
                                "repro": "EV_ML_MODEL_DIR=$(mktemp -d) ev doctor --fabric",
                                "detail": f"exit={p.returncode} :: " + " | ".join(fails)[:280]}]
            counts = (1, 0, 1, 0, 0)
            note = f"exit={p.returncode}; non-environmental failures"
    res["coverage_note"] = note
    n, passed, failed, skipped, blocked = counts
    return finish(res, counts=counts,
                  categories=[{"name": "doctor", "n": n, "passed": passed,
                               "rate": round(passed / n, 4) if n else 0.0,
                               "wilson95": wilson(n, passed)}])
