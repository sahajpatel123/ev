"""Tmux-backed shell sessions + one-shot run with policy gate.

Session registry is persisted under root/.ev-shell-sessions.json so a NEW
ShellSessions instance (simulating an API restart) can reattach to live tmux
sessions. tmux itself is the persistence layer; without tmux, sessions are
subprocess-backed and reattach reports honestly that the session is gone.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path

from .policy import decide

ONE_SHOT_CAP_S = 300
_REGISTRY = ".ev-shell-sessions.json"

# Phase 4 classification. Read-only verbs AUTO; project build/test verbs
# AUTO_JOURNAL; destructive / privesc / exfil patterns CONFIRM or DENY.
_READONLY_BINARIES = frozenset({
    "echo", "printf", "cat", "head", "tail", "less", "more", "ls", "pwd", "whoami",
    "date", "uname", "which", "file", "wc", "sort", "uniq", "grep", "rg", "find",
    "git", "lsb_release",
})
_DENY_PATTERNS = (
    r"rm\s+(-[a-z]*r[a-z]*\s+|--recursive\b).*(^|\s)(/(\s|$)|/etc|/usr|~/?(\s|$))",
    r"rm\s+-rf\s+/\s*$",
    r"mkfs\b", r"dd\s+.*of=/dev/", r":\(\)\s*\{\s*:",
    r"sudo\b", r"su\b", r"doas\b",
    r"\bchmod\s+-R\s+777\s+/", r"\bcurl\b.*\|\s*(ba)?sh\b", r"\bwget\b.*\|\s*(ba)?sh\b",
    r"\.ssh/", r"\.aws/credentials", r"~/\.gnupg",
)
_CONFIRM_BINARIES = frozenset({
    "rm", "mv", "cp", "mkdir", "touch", "chmod", "chown", "ln", "tar", "zip",
    "unzip", "curl", "wget", "ssh", "scp", "rsync", "docker", "pip", "pip3",
    "uv", "npm", "brew", "kill", "killall", "pkill",
})
_SECRET_REDACT = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|bearer)\s*[:=]\s*\S+")


def classify_command(cmd: str) -> str:
    """Return AUTO | AUTO_JOURNAL | CONFIRM | DENY for a shell command string."""
    c = (cmd or "").strip()
    if not c:
        return "CONFIRM"
    low = c.lower()
    for pat in _DENY_PATTERNS:
        if re.search(pat, low):
            return "DENY"
    first = low.split()[0].split("/")[-1] if low.split() else ""
    if first in _READONLY_BINARIES and not any(t in low for t in (" > ", ">>", "| sh", "|sh", "`", "$(")):
        # git can write; only read-only git verbs stay AUTO
        if first == "git" and not re.match(r"git\s+(status|log|diff|show|branch|remote|stash\s+list)\b", low):
            return "CONFIRM"
        return "AUTO"
    if first in ("make", "pytest", "python", "python3", "go", "node", "npm", "uv"):
        return "AUTO_JOURNAL"
    if first in _CONFIRM_BINARIES:
        return "CONFIRM"
    return "CONFIRM"


def redact(text: str) -> str:
    return _SECRET_REDACT.sub(r"\1=<redacted>", text or "")


class ShellSessions:
    def __init__(self, root=None):
        self.root = Path(str(root)) if root is not None else Path(".")
        self._sessions: dict[str, dict] = {}
        self._load_registry()

    # -- registry persistence (API-restart survival) --
    def _reg_path(self) -> Path:
        return self.root / _REGISTRY

    def _load_registry(self) -> None:
        try:
            raw = self._reg_path().read_text()
            data = json.loads(raw)
            if isinstance(data, dict):
                self._sessions.update({k: v for k, v in data.items() if isinstance(v, dict)})
        except (OSError, ValueError):
            pass

    def _save_registry(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            slim = {k: {kk: vv for kk, vv in v.items() if kk != "proc"}
                    for k, v in self._sessions.items()}
            self._reg_path().write_text(json.dumps(slim))
        except OSError:
            pass

    # -- one-shot --
    def run(self, cmd: str, timeout: float = ONE_SHOT_CAP_S, confirm: bool = False,
            provenance: str = "owner_turn", cwd: str | None = None) -> dict:
        cls = classify_command(cmd)
        # Lane classification picks the policy action class; provenance can escalate.
        action = {"AUTO": "read", "AUTO_JOURNAL": "write", "CONFIRM": "run", "DENY": "run"}[cls]
        gate = decide(action, cmd, provenance)
        if cls == "DENY" or gate == "DENY":
            return {"ok": False, "verified": True, "error": "denied by policy",
                    "evidence": {"cmd": cmd, "class": cls}, "undo": None}
        if gate == "CONFIRM" and not confirm:
            return {"ok": False, "verified": True, "error": "confirm required",
                    "evidence": {"cmd": cmd, "class": cls, "needs_confirm": True}, "undo": None}
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=min(timeout, ONE_SHOT_CAP_S), cwd=cwd or str(self.root))
            ok = p.returncode == 0
            return {"ok": ok, "verified": True,
                    "evidence": {"cmd": cmd, "out": redact((p.stdout or "")[:4000]),
                                 "exit_code": p.returncode, "class": cls},
                    "error": None if ok else redact((p.stderr or "")[:1000]),
                    "undo": None}
        except subprocess.TimeoutExpired:
            return {"ok": False, "verified": True, "error": "timeout",
                    "evidence": {"cmd": cmd}, "undo": None}

    # -- sessions --
    def start(self, cmd: str, cwd: str | None = None, confirm: bool = False,
              provenance: str = "owner_turn") -> dict:
        cls = classify_command(cmd)
        action = {"AUTO": "read", "AUTO_JOURNAL": "write", "CONFIRM": "run", "DENY": "run"}[cls]
        gate = decide(action, cmd, provenance)
        if cls == "DENY" or gate == "DENY":
            return {"ok": False, "verified": True, "error": "denied by policy",
                    "evidence": {"cmd": cmd}, "undo": None}
        if gate == "CONFIRM" and not confirm:
            return {"ok": False, "verified": True, "error": "confirm required",
                    "evidence": {"cmd": cmd, "needs_confirm": True}, "undo": None}
        sid = f"sess-{uuid.uuid4().hex[:8]}"
        self._sessions[sid] = {"cmd": cmd, "out": "", "proc": None, "exit_code": None,
                               "started": time.time()}
        if _has_tmux():
            name = f"ev-{sid}"
            args = ["tmux", "new-session", "-d", "-s", name]
            if cwd:
                args += ["-c", cwd]
            # keep pane alive after cmd exits so output stays readable
            args += ["sh", "-c", cmd + "; echo __EV_EXIT:$?"]
            subprocess.run(args, capture_output=True)
            self._sessions[sid]["tmux"] = name
        else:
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 cwd=cwd or str(self.root))
            self._sessions[sid]["proc"] = p
        self._save_registry()
        return {"ok": True, "verified": True, "session_id": sid,
                "evidence": {"cmd": cmd}, "error": None, "undo": None}

    def _tmux_alive(self, name: str) -> bool:
        p = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True)
        return p.returncode == 0

    def _collect(self, sid: str) -> str:
        s = self._sessions[sid]
        if "tmux" in s:
            if not self._tmux_alive(s["tmux"]):
                s["exit_code"] = s.get("exit_code", 0)
                return s.get("out", "")
            p = subprocess.run(["tmux", "capture-pane", "-p", "-t", s["tmux"]],
                               capture_output=True, text=True)
            s["out"] = redact(p.stdout or "")
        else:
            proc = s.get("proc")
            if proc is not None and proc.poll() is not None:
                try:
                    out, _ = proc.communicate(timeout=1)
                    s["out"] = redact((s.get("out", "") + (out or ""))[-8000:])
                except Exception:
                    pass
                s["exit_code"] = proc.returncode
        self._save_registry()
        return s["out"]

    def stream(self, session_id: str) -> dict:
        if session_id not in self._sessions:
            return {"ok": False, "verified": True, "error": "unknown session",
                    "evidence": {}, "undo": None}
        out = self._collect(session_id)
        return {"ok": True, "verified": True, "output": out,
                "evidence": {}, "error": None, "undo": None}

    def tail(self, session_id: str, n: int = 50) -> dict:
        if session_id not in self._sessions:
            return {"ok": False, "verified": True, "error": "unknown session",
                    "evidence": {}, "undo": None}
        out = self._collect(session_id).splitlines()
        return {"ok": True, "verified": True, "output": "\n".join(out[-n:]),
                "evidence": {}, "error": None, "undo": None}

    def send_input(self, session_id: str, data: str) -> dict:
        s = self._sessions.get(session_id)
        if s is None:
            return {"ok": False, "verified": True, "error": "unknown session",
                    "evidence": {}, "undo": None}
        if "tmux" in s:
            if not self._tmux_alive(s["tmux"]):
                return {"ok": False, "verified": True, "error": "session exited",
                        "evidence": {}, "undo": None}
            subprocess.run(["tmux", "send-keys", "-t", s["tmux"], data, "Enter"],
                           capture_output=True)
        else:
            return {"ok": False, "verified": True, "error": "input unsupported without tmux",
                    "evidence": {}, "undo": None}
        return {"ok": True, "verified": True, "evidence": {}, "error": None, "undo": None}

    def reattach(self, session_id: str) -> dict:
        # reload from disk: this is what makes post-restart reattach real
        self._load_registry()
        s = self._sessions.get(session_id)
        if s is None:
            return {"ok": False, "verified": True, "error": "unknown session",
                    "evidence": {}, "undo": None}
        if "tmux" in s:
            alive = self._tmux_alive(s["tmux"])
            out = self._collect(session_id) if alive else s.get("out", "")
            return {"ok": alive, "verified": True, "session_id": session_id,
                    "output": out, "evidence": {"alive": alive},
                    "error": None if alive else "session exited", "undo": None}
        return {"ok": False, "verified": True, "session_id": session_id,
                "error": "subprocess sessions do not survive restart (no tmux)",
                "evidence": {}, "undo": None}

    def list(self) -> dict:
        self._load_registry()
        return {"ok": True, "verified": True,
                "sessions": list(self._sessions.keys()),
                "evidence": {}, "error": None, "undo": None}

    def cancel(self, session_id: str) -> dict:
        s = self._sessions.get(session_id)
        if s is None:
            return {"ok": False, "verified": True, "error": "unknown session",
                    "evidence": {}, "undo": None}
        if "tmux" in s:
            subprocess.run(["tmux", "kill-session", "-t", s["tmux"]],
                           capture_output=True)
            killed = not self._tmux_alive(s["tmux"])
        elif s.get("proc") is not None:
            try:
                s["proc"].kill()
            except Exception:
                pass
            killed = True
        else:
            killed = False
        s["exit_code"] = -1
        self._save_registry()
        return {"ok": killed, "verified": killed, "evidence": {}, "error": None, "undo": None}

    def exit_code(self, session_id: str) -> dict:
        if session_id not in self._sessions:
            return {"ok": False, "verified": True, "error": "unknown session",
                    "evidence": {}, "undo": None}
        self._collect(session_id)
        code = self._sessions[session_id].get("exit_code")
        return {"ok": True, "verified": True, "exit_code": code,
                "evidence": {}, "error": None, "undo": None}


def _has_tmux() -> bool:
    try:
        p = subprocess.run(["which", "tmux"], capture_output=True)
        return p.returncode == 0
    except Exception:
        return False
