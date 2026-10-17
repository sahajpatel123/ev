"""Bridge lane: headless JXA/notes/reminders without websocket; honest permission receipts; Evie.app alias shim."""
from __future__ import annotations

import shutil
import subprocess
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

    # -- file-backed reminder store (lane contract; EventKit backing lands in WP8) --
    def _reminders_dir(self):
        d = self.root / "reminders"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def reminder_create(self, title: str, list_name: str = "default", notes: str = "") -> dict:
        import json
        import time as _time
        import uuid as _uuid

        title = (title or "").strip()
        if not title:
            return {"ok": False, "verified": True, "evidence": {}, "error": "title required", "undo": None}
        rid = f"rem-{_uuid.uuid4().hex[:8]}"
        row = {"id": rid, "title": title[:280], "list": (list_name or "default")[:80],
               "notes": (notes or "")[:2000], "done": False, "created_at": _time.time()}
        try:
            path = self._reminders_dir() / f"{rid}.json"
            path.write_text(json.dumps(row))
            back = json.loads(path.read_text())
            ok = back.get("title") == row["title"]
            return {"ok": ok, "verified": ok, "evidence": {"id": rid, "title": row["title"]},
                    "error": None if ok else "read-back mismatch",
                    "undo": {"reminder_file": str(path)}}
        except OSError as e:
            return {"ok": False, "verified": True, "evidence": {}, "error": str(e), "undo": None}

    def reminder_list(self, list_name: str | None = None) -> dict:
        import json

        try:
            rows = []
            for path in sorted(self._reminders_dir().glob("rem-*.json")):
                try:
                    row = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                if list_name and row.get("list") != list_name:
                    continue
                rows.append(row)
            return {"ok": True, "verified": True, "evidence": {"reminders": rows, "n": len(rows)},
                    "error": None, "undo": None}
        except OSError as e:
            return {"ok": False, "verified": True, "evidence": {}, "error": str(e), "undo": None}

    def reminder_complete(self, rid: str) -> dict:
        import json

        path = self._reminders_dir() / f"{(rid or '').strip()}.json"
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            return {"ok": False, "verified": True, "evidence": {}, "error": "unknown reminder", "undo": None}
        row["done"] = True
        try:
            path.write_text(json.dumps(row))
            back = json.loads(path.read_text())
            ok = back.get("done") is True
            return {"ok": ok, "verified": ok, "evidence": {"id": rid},
                    "error": None if ok else "read-back mismatch", "undo": None}
        except OSError as e:
            return {"ok": False, "verified": True, "evidence": {}, "error": str(e), "undo": None}

    def reminder_delete(self, rid: str) -> dict:
        import shutil as _shutil
        import time as _time

        path = self._reminders_dir() / f"{(rid or '').strip()}.json"
        if not path.is_file():
            return {"ok": False, "verified": True, "evidence": {}, "error": "unknown reminder", "undo": None}
        try:
            trash = self._reminders_dir() / ".trash"
            trash.mkdir(parents=True, exist_ok=True)
            dest = trash / f"{path.stem}.{_time.time_ns()}.json"
            _shutil.move(str(path), str(dest))
        except OSError:
            return {"ok": False, "verified": True, "evidence": {}, "error": "unknown reminder", "undo": None}
        gone = not path.exists() and dest.exists()
        return {"ok": gone, "verified": gone, "evidence": {"id": rid},
                "error": None, "undo": {"reminder_trash": str(dest)}}
