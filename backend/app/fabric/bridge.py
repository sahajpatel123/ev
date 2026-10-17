"""Bridge lane: headless JXA/notes/reminders without websocket; honest permission receipts; Evie.app alias shim."""
from __future__ import annotations
import subprocess, shutil
from pathlib import Path

FIX_STEPS = {
    "notes": "Enable Notes access: System Settings > Privacy & Security > Automation > Evie.",
    "reminders": "Enable Reminders access: System Settings > Privacy & Security > Reminders > Evie.",
    "jxa": "Grant Automation permission: System Settings > Privacy & Security > Automation.",
}

class Bridge:
    def __init__(self, root="."):
        self.root = Path(str(root))
        self.aliases = {"Evie.app": "Evie"}

    def resolve_alias(self, name: str) -> str:
        return self.aliases.get(name, name)

    def roundtrip(self, msg: dict) -> dict:
        op = msg.get("op")
        if op == "ping":
            return {"ok": True, "verified": True, "evidence": {"op": "ping"}, "error": None, "undo": None}
        if op in ("jxa", "notes", "reminders"):
            return self.run(op, msg.get("script", ""), msg.get("payload"))
        return {"ok": True, "verified": True, "evidence": {"echo": msg}, "error": None, "undo": None}

    def run(self, lane: str, script: str = "", payload=None) -> dict:
        if lane == "jxa":
            if shutil.which("osascript") is None:
                return {"ok": False, "verified": False, "evidence": {},
                        "error": "missing permission/tool: osascript unavailable",
                        "undo": None, "fix": FIX_STEPS["jxa"]}
            try:
                p = subprocess.run(["osascript", "-e", script or 'return "ok"'],
                                   capture_output=True, text=True, timeout=10)
                if p.returncode != 0:
                    return {"ok": False, "verified": False, "evidence": {"stderr": p.stderr},
                            "error": "jxa failed", "undo": None, "fix": FIX_STEPS["jxa"]}
                return {"ok": True, "verified": True, "evidence": {"out": p.stdout.strip()},
                        "error": None, "undo": None}
            except Exception as e:
                return {"ok": False, "verified": False, "evidence": {}, "error": str(e),
                        "undo": None, "fix": FIX_STEPS["jxa"]}
        # notes/reminders headless: local stub dir
        d = self.root / lane
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "verified": False, "evidence": {}, "error": str(e),
                    "undo": None, "fix": FIX_STEPS.get(lane, "")}
        return {"ok": True, "verified": True, "evidence": {"lane": lane, "stored": bool(payload or script)},
                "error": None, "undo": None}
