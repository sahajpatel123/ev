"""Scripted Ghostty backend (WP4): native windows/tabs/splits/input/read-back.

Ghostty 1.3.1 ships a real AppleScript dictionary (Resources/Ghostty.sdef):
new window/tab, split (right/left/down/up), input text, perform action,
stable window/tab/terminal ids, cwd introspection. Read-back rides the
`write_screen_file:copy` action (clipboard carries the dump path; the
clipboard is saved and restored around every read).

tmux stays the persistence/addressing layer inside each pane: Ghostty owns
the visible surfaces, tmux owns reattach/capture/send-keys. When Ghostty
is absent or unscriptable, TmuxLayout alone covers everything headlessly.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import time
import uuid

BUNDLE = "/Applications/Ghostty.app"
SDEF = BUNDLE + "/Contents/Resources/Ghostty.sdef"

_DIRECTIONS = ("right", "left", "down", "up")


def _osa(script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], capture_output=True,
                          text=True, timeout=timeout)


def _esc(text: str) -> str:
    return str(text or "").replace("\\", "\\\\").replace('"', '\\"')


def probe() -> dict:
    """Empirical scripting surface. Never assumes; only observes."""
    found: dict = {
        "bundle_present": False,
        "sdef_present": False,
        "osascript": shutil.which("osascript"),
        "version": None,
        "scriptable": False,
        "windows": None,
    }
    import os

    found["bundle_present"] = os.path.exists(BUNDLE)
    found["sdef_present"] = os.path.exists(SDEF)
    cli = BUNDLE + "/Contents/MacOS/ghostty"
    if os.path.exists(cli):
        try:
            p = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=5)
            first = ((p.stdout or p.stderr or "").strip().splitlines() or [""])[0]
            found["version"] = first[:80]
        except Exception as e:
            found["version"] = f"probe failed: {e}"
    if found["bundle_present"] and found["sdef_present"] and found["osascript"]:
        try:
            p = _osa('tell application "Ghostty" to return (count of windows)', timeout=15)
            if p.returncode == 0:
                found["scriptable"] = True
                found["windows"] = int((p.stdout or "0").strip() or 0)
            else:
                found["error"] = (p.stderr or "").strip()[:200]
        except Exception as e:
            found["error"] = f"{type(e).__name__}: {e}"
    return {"ok": True, "verified": True, "evidence": found, "error": None, "undo": None}


def _receipt(ok: bool, evidence=None, error=None, undo=None):
    return {"ok": ok, "verified": bool(ok), "evidence": evidence or {},
            "error": error, "undo": undo}


class GhosttyScripted:
    """One-concern driver: AppleScript verbs + receipt shaping."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def _run(self, script: str) -> tuple[bool, str]:
        try:
            p = _osa(script, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return False, "osascript timeout"
        if p.returncode != 0:
            return False, (p.stderr or "").strip()[:400] or "osascript failed"
        return True, (p.stdout or "").strip()

    def list_windows(self) -> dict:
        ok, out = self._run('tell application "Ghostty" to return id of every window')
        if not ok:
            return _receipt(False, error=out)
        ids = [w.strip() for w in out.split(",") if w.strip()]
        return _receipt(True, {"windows": ids, "n": len(ids)})

    def _config_block(self, *, command: str = "", cwd: str = "",
                      initial_input: str = "", wait: bool = False) -> tuple[str, str]:
        """(setup statements, expression) for a surface configuration.

        The dictionary supports `make new surface configuration` plus
        property assignment — not `with properties` (rejected, -2741).
        Returns ("", "missing value") when no property is set.
        """
        sets = []
        if command:
            sets.append(f'set command of cfg to "{_esc(command)}"')
        if cwd:
            sets.append(f'set initial working directory of cfg to "{_esc(cwd)}"')
        if initial_input:
            sets.append(f'set initial input of cfg to "{_esc(initial_input)}"')
        if wait:
            sets.append("set wait after command of cfg to true")
        if not sets:
            return "", "missing value"
        setup = "set cfg to new surface configuration\n  " + "\n  ".join(sets)
        return setup, "cfg"

    def open_window(self, *, command: str = "", cwd: str = "",
                    initial_input: str = "") -> dict:
        setup, expr = self._config_block(command=command, cwd=cwd,
                                         initial_input=initial_input,
                                         wait=bool(command))
        if expr == "missing value":
            script = 'tell application "Ghostty" to new window'
        else:
            script = ("tell application \"Ghostty\"\n  " + setup + "\n"
                      f"  new window with configuration {expr}\nend tell")
        ok, out = self._run(script)
        if not ok:
            return _receipt(False, error=out)
        # `new window` returns a window reference; resolve its stable id.
        wid = out.replace("window id ", "").strip()
        if not wid or " " in wid and "," in wid:
            listed = self.list_windows()
            ids = listed.get("evidence", {}).get("windows", [])
            wid = ids[-1] if ids else ""
        if not wid:
            return _receipt(False, error="window id unresolvable")
        return _receipt(True, {"window_id": wid}, undo={"window_id": wid})

    def split(self, window_id: str, direction: str = "right", *,
              command: str = "", cwd: str = "") -> dict:
        if direction not in _DIRECTIONS:
            return _receipt(False, error=f"bad direction '{direction}'")
        setup, expr = self._config_block(command=command, cwd=cwd,
                                         wait=bool(command))
        if expr == "missing value":
            script = (f'tell application "Ghostty" to tell window id "{_esc(window_id)}"\n'
                      f"  split first terminal direction {direction}\n"
                      "end tell")
        else:
            script = (f'tell application "Ghostty"\n  {setup}\n'
                      f'  tell window id "{_esc(window_id)}"\n'
                      f"    split first terminal direction {direction} with configuration {expr}\n"
                      "  end tell\nend tell")
        ok, out = self._run(script)
        if not ok:
            return _receipt(False, error=out)
        return _receipt(True, {"terminal": out[:120]})

    def terminals(self, window_id: str) -> dict:
        ok, out = self._run(
            f'tell application "Ghostty" to return id of every terminal of window id "{_esc(window_id)}"')
        if not ok:
            return _receipt(False, error=out)
        ids = [t.strip() for t in out.split(",") if t.strip()]
        return _receipt(True, {"terminals": ids, "n": len(ids)})

    def input_text(self, window_id: str, text: str, *, terminal_index: int = 1,
                   enter: bool = True) -> dict:
        payload = text + ("\n" if enter and not text.endswith("\n") else "")
        ordinal = ["first", "second", "third", "fourth"][min(max(terminal_index - 1, 0), 3)] \
            if terminal_index <= 4 else f"terminal {terminal_index}"
        script = (f'tell application "Ghostty" to tell window id "{_esc(window_id)}"\n'
                  f'  input text "{_esc(payload)}" to {ordinal} terminal\n'
                  "end tell")
        ok, out = self._run(script)
        if not ok:
            return _receipt(False, error=out)
        return _receipt(True, {"sent_bytes": len(payload)})

    def read_screen(self, window_id: str, *, terminal_index: int = 1) -> dict:
        """Screen text via write_screen_file:copy (clipboard saved/restored)."""
        saved = ""
        try:
            p = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10)
            saved = p.stdout or ""
        except Exception:
            pass
        ordinal = ["first", "second", "third", "fourth"][min(max(terminal_index - 1, 0), 3)] \
            if terminal_index <= 4 else f"terminal {terminal_index}"
        script = (f'tell application "Ghostty" to tell window id "{_esc(window_id)}"\n'
                  f'  perform action "write_screen_file:copy,plain" on {ordinal} terminal\n'
                  "end tell")
        ok, out = self._run(script)
        path = ""
        body = ""
        if ok:
            time.sleep(0.5)
            try:
                p = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10)
                path = (p.stdout or "").strip().splitlines()[0] if (p.stdout or "").strip() else ""
            except Exception:
                path = ""
            if path:
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        body = fh.read()
                except OSError as exc:
                    ok, out = False, f"dump unreadable: {exc}"
        with contextlib.suppress(Exception):
            subprocess.run(["pbcopy"], input=saved, capture_output=True, text=True, timeout=10)
        if not ok:
            return _receipt(False, error=out)
        return _receipt(True, {"text": body, "chars": len(body), "dump": path})

    def focus_window(self, window_id: str) -> dict:
        ok, out = self._run(
            f'tell application "Ghostty" to tell window id "{_esc(window_id)}" to activate window')
        time.sleep(0.5)
        return _receipt(ok, {}, None if ok else out)

    def close_window(self, window_id: str) -> dict:
        ok, out = self._run(
            f'tell application "Ghostty" to tell window id "{_esc(window_id)}" to close window')
        if not ok:
            return _receipt(False, error=out)
        time.sleep(0.5)
        listed = self.list_windows()
        ids = listed.get("evidence", {}).get("windows", []) if listed.get("ok") else []
        gone = window_id not in ids
        return _receipt(gone, {}, None if gone else "window still listed")

    def close_terminal(self, window_id: str, terminal_index: int = 1) -> dict:
        ordinal = ["first", "second", "third", "fourth"][min(max(terminal_index - 1, 0), 3)] \
            if terminal_index <= 4 else f"terminal {terminal_index}"
        ok, out = self._run(
            f'tell application "Ghostty" to tell window id "{_esc(window_id)}"\n'
            f"  close {ordinal} terminal\nend tell")
        return _receipt(ok, {}, None if ok else out)


def session_token() -> str:
    return f"ev-ghostty-{uuid.uuid4().hex[:8]}"
