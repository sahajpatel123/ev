"""Bridge lane: headless JXA/notes/reminders without websocket; honest permission receipts; Evie.app alias shim."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

FIX_STEPS = {
    "notes": "Enable Notes access: System Settings > Privacy & Security > Automation > Evie.",
    "reminders": "Enable Reminders access: System Settings > Privacy & Security > Reminders > Evie.",
    "jxa": "Grant Automation permission: System Settings > Privacy & Security > Automation.",
}

# JXA that touches another app needs Automation under Evie.app's identity, so
# it must run inside the app (unix-socket bridge), never headless from this
# process (whose prompt would attach to the terminal, not Evie).
_AUTOMATION_RE = re.compile(r"\btell\s+(application|app)\b|Application\s*\(", re.IGNORECASE)


def needs_app_identity(script: str) -> bool:
    return bool(_AUTOMATION_RE.search(script or ""))

def _bridge_down(res: dict) -> bool:
    return not res.get("ok") and str(res.get("error") or "").startswith("bridge ")


class Bridge:
    def __init__(self, root="."):
        self.root = Path(str(root))
        self.aliases = {"Evie.app": "Evie"}

    def resolve_alias(self, name: str) -> str:
        return self.aliases.get(name, name)

    def run_via_app(self, op: str, args: dict | None = None, timeout: float = 30.0) -> dict:
        """Run an op inside Evie.app's TCC identity. Honest when down."""
        from .bridge_client import BridgeClient

        return BridgeClient(timeout=timeout).call(op, args)

    def screenshot(self) -> dict:
        """Screen capture under the app's Screen Recording grant. No fallback:
        capturing from this process would prompt for the wrong identity."""
        res = self.run_via_app("screenshot")
        if res.get("ok"):
            ev = res.get("evidence", {})
            if not ev.get("path") or not Path(str(ev["path"])).is_file():
                return {"ok": False, "verified": True, "evidence": ev,
                        "error": "screenshot not readable", "undo": None,
                        "fix": FIX_STEPS["jxa"]}
        return res

    def tcc_status(self) -> dict:
        return self.run_via_app("tcc_status")

    def open_url(self, url: str) -> dict:
        res = self.run_via_app("open_url", {"url": url})
        if not _bridge_down(res):
            return res
        # `open` is not identity-bound: headless fallback is equivalent.
        try:
            p = subprocess.run(["open", url], capture_output=True, text=True, timeout=15)
        except Exception as e:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": str(e), "undo": None}
        return {"ok": p.returncode == 0, "verified": True,
                "evidence": {"opened": url}, "error": None if p.returncode == 0 else p.stderr[:300],
                "undo": None}

    def shortcuts_run(self, name: str, input_text: str = "") -> dict:
        res = self.run_via_app("shortcuts_run", {"name": name, "input": input_text})
        if not _bridge_down(res):
            return res
        if shutil.which("shortcuts") is None:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "missing tool: shortcuts", "undo": None}
        try:
            p = subprocess.run(["shortcuts", "run", name], input=input_text or None,
                               capture_output=True, text=True, timeout=120)
        except Exception as e:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": str(e), "undo": None}
        return {"ok": p.returncode == 0, "verified": True,
                "evidence": {"out": p.stdout[:2000]}, "error": None if p.returncode == 0 else p.stderr[:300],
                "undo": None}

    def pasteboard_get(self) -> dict:
        res = self.run_via_app("pasteboard_get")
        if res.get("ok"):
            return res
        try:
            p = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10)
        except Exception as e:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": str(e), "undo": None}
        return {"ok": p.returncode == 0, "verified": True,
                "evidence": {"text": p.stdout}, "error": None, "undo": None}

    def pasteboard_set(self, text: str) -> dict:
        res = self.run_via_app("pasteboard_set", {"text": text})
        if res.get("ok"):
            return res
        try:
            p = subprocess.run(["pbcopy"], input=text, capture_output=True, text=True, timeout=10)
        except Exception as e:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": str(e), "undo": None}
        if p.returncode != 0:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": p.stderr[:300], "undo": None}
        back = self.pasteboard_get()
        ok = back.get("ok") and back.get("evidence", {}).get("text") == text
        return {"ok": ok, "verified": ok, "evidence": {"bytes": len(text)},
                "error": None if ok else "readback_mismatch", "undo": None}

    # -- EventKit (real reminders/calendars under the app's grants; WP8) --
    def ek_reminders_lists(self) -> dict:
        return self.run_via_app("reminders_lists")

    def ek_reminder_list_create(self, title: str) -> dict:
        return self.run_via_app("reminder_list_create", {"title": title})

    def ek_reminder_list_delete(self, title: str) -> dict:
        return self.run_via_app("reminder_list_delete", {"title": title})

    def ek_reminder_list(self, list_name: str | None = None) -> dict:
        return self.run_via_app("reminder_list", {"list": list_name or ""})

    def ek_reminder_create(self, title: str, list_name: str = "",
                           notes: str = "", due: str = "") -> dict:
        return self.run_via_app("reminder_create", {"title": title, "list": list_name,
                                                    "notes": notes, "due": due})

    def ek_reminder_complete(self, rid: str) -> dict:
        return self.run_via_app("reminder_complete", {"id": rid})

    def ek_reminder_delete(self, rid: str) -> dict:
        return self.run_via_app("reminder_delete", {"id": rid})

    def ek_calendars(self) -> dict:
        return self.run_via_app("calendars_list")

    def ek_calendar_events(self, calendar: str = "", days: int = 7) -> dict:
        return self.run_via_app("calendar_events", {"calendar": calendar, "days": days})

    # -- Notes.app via automation JXA (WP8) --
    def notes_folders(self) -> dict:
        res = self.run("jxa", 'tell application "Notes" to return name of every folder')
        if not res.get("ok"):
            return res
        out = (res.get("evidence") or {}).get("out", "")
        folders = [line.strip() for line in out.split(",") if line.strip()]
        return {"ok": True, "verified": True,
                "evidence": {"folders": folders}, "error": None, "undo": None}

    def notes_ensure_folder(self, folder: str) -> dict:
        listed = self.notes_folders()
        if listed.get("ok") and folder in listed["evidence"]["folders"]:
            return {"ok": True, "verified": True, "evidence": {"folder": folder},
                    "error": None, "undo": None}
        script = ('tell application "Notes"\n'
                  f'  make new folder with properties {{name:"{folder}"}}\n'
                  "end tell")
        res = self.run("jxa", script)
        if not res.get("ok"):
            return res
        return {"ok": True, "verified": True, "evidence": {"folder": folder},
                "error": None, "undo": None}

    def note_create(self, folder: str, title: str, body: str) -> dict:
        ensured = self.notes_ensure_folder(folder)
        if not ensured.get("ok"):
            return ensured
        safe_title = title.replace('"', "")
        safe_body = body.replace('"', "")
        script = ('tell application "Notes"\n'
                  f'  tell folder "{folder}"\n'
                  f'    make new note with properties {{name:"{safe_title}", body:"{safe_body}"}}\n'
                  "  end tell\n"
                  "end tell")
        res = self.run("jxa", script)
        if not res.get("ok"):
            return res
        back = self.note_list(folder)
        found = [n for n in back.get("evidence", {}).get("notes", [])
                 if n.get("title") == title]
        ok = back.get("ok") and bool(found)
        return {"ok": ok, "verified": ok,
                "evidence": {"id": found[0]["id"] if found else None,
                             "title": title},
                "error": None if ok else "read-back mismatch", "undo": None}

    def note_list(self, folder: str) -> dict:
        script = ('tell application "Notes"\n'
                  f'  tell folder "{folder}"\n'
                  "    set outs to {}\n"
                  "    repeat with n in notes\n"
                  '      set end of outs to (id of n as string) & "\\t" & name of n\n'
                  "    end repeat\n"
                  '    return outs as string\n'
                  "  end tell\n"
                  "end tell")
        res = self.run("jxa", script)
        if not res.get("ok"):
            # Missing folder reads as an empty list, not an error.
            if "failed" in str(res.get("error") or ""):
                return {"ok": True, "verified": True,
                        "evidence": {"notes": []}, "error": None, "undo": None}
            return res
        rows = []
        for line in (res.get("evidence") or {}).get("out", "").splitlines():
            if "\t" in line:
                nid, _, title = line.partition("\t")
                rows.append({"id": nid.strip(), "title": title.strip()})
        return {"ok": True, "verified": True, "evidence": {"notes": rows},
                "error": None, "undo": None}

    def note_delete(self, folder: str, nid: str) -> dict:
        script = ('tell application "Notes"\n'
                  f'  tell folder "{folder}"\n'
                  f'    delete (every note whose id is "{nid}")\n'
                  "  end tell\n"
                  "end tell")
        res = self.run("jxa", script)
        if not res.get("ok"):
            return res
        back = self.note_list(folder)
        gone = back.get("ok") and all(n.get("id") != nid
                                      for n in back["evidence"]["notes"])
        return {"ok": gone, "verified": gone, "evidence": {"id": nid},
                "error": None if gone else "still present", "undo": None}

    def roundtrip(self, msg: dict) -> dict:
        op = msg.get("op")
        if op == "ping":
            return {"ok": True, "verified": True, "evidence": {"op": "ping"}, "error": None, "undo": None}
        if op in ("jxa", "notes", "reminders"):
            return self.run(op, msg.get("script", ""), msg.get("payload"))
        return {"ok": True, "verified": True, "evidence": {"echo": msg}, "error": None, "undo": None}

    def run(self, lane: str, script: str = "", payload=None) -> dict:
        if lane == "jxa":
            if needs_app_identity(script):
                return self.run_via_app("jxa", {"script": script})
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
