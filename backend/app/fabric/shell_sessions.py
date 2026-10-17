"""Tmux-backed shell sessions + one-shot run with policy gate."""
from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

from .policy import decide

ONE_SHOT_CAP_S = 300


class ShellSessions:
    def __init__(self, root=None):
        self.root = Path(str(root)) if root is not None else Path(".")
        self._sessions: dict[str, dict] = {}

    def run(self, cmd: str, timeout: float = ONE_SHOT_CAP_S) -> dict:
        d = decide("shell", cmd)
        if d == "DENY" or "rm -rf /" in (cmd or ""):
            return {"ok": False, "verified": False, "error": "denied by policy",
                    "evidence": {"cmd": cmd, "class": d}, "undo": None}
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=min(timeout, ONE_SHOT_CAP_S))
            ok = p.returncode == 0
            return {"ok": ok, "verified": ok,
                    "evidence": {"cmd": cmd, "out": (p.stdout or "")[:4000],
                                 "exit_code": p.returncode},
                    "error": None if ok else (p.stderr or "")[:1000],
                    "undo": None}
        except subprocess.TimeoutExpired:
            return {"ok": False, "verified": False, "error": "timeout",
                    "evidence": {"cmd": cmd}, "undo": None}

    def start(self, cmd: str) -> dict:
        sid = f"sess-{uuid.uuid4().hex[:8]}"
        self._sessions[sid] = {"cmd": cmd, "out": "", "proc": None, "exit_code": None}
        if _has_tmux():
            name = f"ev-{sid}"
            subprocess.run(["tmux", "new-session", "-d", "-s", name, cmd],
                           capture_output=True)
            self._sessions[sid]["tmux"] = name
        else:
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
            self._sessions[sid]["proc"] = p
        return {"ok": True, "verified": True, "session_id": sid,
                "evidence": {"cmd": cmd}, "error": None, "undo": None}

    def _collect(self, sid: str) -> str:
        s = self._sessions[sid]
        if "tmux" in s:
            p = subprocess.run(["tmux", "capture-pane", "-p", "-t", s["tmux"]],
                               capture_output=True, text=True)
            s["out"] = p.stdout or ""
        else:
            proc = s.get("proc")
            if proc is not None:
                if proc.poll() is not None:
                    try:
                        out, _ = proc.communicate(timeout=1)
                        s["out"] += out or ""
                    except Exception:
                        pass
                    s["exit_code"] = proc.returncode
        return s["out"]

    def stream(self, session_id: str) -> dict:
        out = self._collect(session_id)
        return {"ok": True, "verified": True, "output": out,
                "evidence": {}, "error": None, "undo": None}

    def tail(self, session_id: str, n: int = 50) -> dict:
        out = self._collect(session_id).splitlines()
        return {"ok": True, "verified": True, "output": "\n".join(out[-n:]),
                "evidence": {}, "error": None, "undo": None}

    def send_input(self, session_id: str, data: str) -> dict:
        s = self._sessions[session_id]
        if "tmux" in s:
            subprocess.run(["tmux", "send-keys", "-t", s["tmux"], data, "Enter"],
                           capture_output=True)
        else:
            proc = s.get("proc")
            if proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(data + "\n")
                    proc.stdin.flush()
                except Exception:
                    pass
        return {"ok": True, "verified": True, "evidence": {}, "error": None, "undo": None}

    def reattach(self, session_id: str) -> dict:
        return {"ok": True, "verified": True, "session_id": session_id,
                "evidence": {}, "error": None, "undo": None}

    def list(self) -> dict:
        return {"ok": True, "verified": True,
                "sessions": list(self._sessions.keys()),
                "evidence": {}, "error": None, "undo": None}

    def cancel(self, session_id: str) -> dict:
        s = self._sessions.get(session_id)
        if s is None:
            return {"ok": False, "verified": False, "error": "unknown session",
                    "evidence": {}, "undo": None}
        if "tmux" in s:
            subprocess.run(["tmux", "kill-session", "-t", s["tmux"]],
                           capture_output=True)
        elif s.get("proc") is not None:
            try:
                s["proc"].kill()
            except Exception:
                pass
        s["exit_code"] = -1
        return {"ok": True, "verified": True, "evidence": {}, "error": None, "undo": None}

    def exit_code(self, session_id: str) -> dict:
        self._collect(session_id)
        return {"ok": True, "verified": True,
                "exit_code": self._sessions[session_id].get("exit_code"),
                "evidence": {}, "error": None, "undo": None}


def _has_tmux() -> bool:
    try:
        p = subprocess.run(["which", "tmux"], capture_output=True)
        return p.returncode == 0
    except Exception:
        return False
