"""Tmux-backed shell sessions + one-shot run with policy gate.

Session registry is persisted under root/.ev-shell-sessions.json so a NEW
ShellSessions instance (simulating an API restart) can reattach to live tmux
sessions. tmux itself is the persistence layer; without tmux, sessions are
subprocess-backed and reattach reports honestly that the session is gone.
"""
from __future__ import annotations

import contextlib
import json
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from .policy import decide

ONE_SHOT_CAP_S = 300
_REGISTRY = ".ev-shell-sessions.json"

# Phase 4 classification. Read-only verbs AUTO; project build/test verbs
# AUTO_JOURNAL; destructive / privesc / exfil patterns CONFIRM or DENY.
# NOTE: patterns match the LOWERCASED command — keep them lowercase.
_READONLY_BINARIES = frozenset({
    "echo", "printf", "cat", "head", "tail", "less", "more", "ls", "pwd", "whoami",
    "date", "uname", "which", "file", "wc", "sort", "uniq", "grep", "rg", "find",
    "git", "lsb_release", "ps", "df", "du", "uptime", "hostname", "id",
})
# Conventional build/test loops: journaled, no park. Bare interpreters are
# NOT here — `python script.py` runs opaque code (CONFIRM).
_JOURNAL_BINARIES = frozenset({"make", "pytest", "go", "cargo"})
_INTERPRETERS = frozenset(
    {"python", "python3", "node", "deno", "bun", "ruby", "perl", "php", "lua", "r"})
_INLINE_FLAGS = frozenset({"-c", "-e", "-r"})
_DANGEROUS_RM_TARGETS = frozenset(
    {"/", "/etc", "/usr", "/bin", "/sbin", "/var", "/private"})
_DECODE_PIPES = (
    r"\bbase64\b[^\|]*(-d\b|-D\b|--decode\b)[^\|]*\|\s*(ba|da|z)?sh\b",
    r"\bxxd\b[^\|]*-r\b[^\|]*\|\s*(ba|da|z)?sh\b",
    r"\bopenssl\s+enc\b[^\|]*-d\b[^\|]*\|\s*(ba|da|z)?sh\b",
)
_SECRET_SUBSTRINGS = (
    r"/\.ssh/", r"~/\.ssh\b", r"/\.aws/", r"/\.gnupg",
    r"\bid_rsa\b", r"\bid_ed25519\b", r"\bid_ecdsa\b", r"\.pem\b",
    r"\bcredentials\.json\b", r"(^|\s)\.env\b",
    r"/etc/passwd\b", r"/etc/shadow\b", r"/etc/sudoers\b", r"/etc/master\.passwd\b",
)
_DENY_PATTERNS = (
    r"\brm\s+(-[a-z]*r[a-z]*\s+|--recursive\b.*)(/\s*$|/\*\s*$|/\s+|/etc\b|/usr\b|/bin\b|/sbin\b|/var\b|/private\b|~/?(\s|$))",
    r"\brm\s+.*--no-preserve-root\b",
    r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+\.\s*$",
    r"\bchmod\s+-[a-z]*r[a-z]*\s+777(\s+/\s*$|\s+~/\s*$|\s+~\s*$)",
    r"\bcurl\b.*\|\s*(ba|da|z)?sh\b", r"\bwget\b.*\|\s*(ba|da|z)?sh\b",
    r"\b(ba|da|z)?sh\s+-c\b.*(\$\(|`|\bcurl\b|\bwget\b)",
    *_DECODE_PIPES,
    *_SECRET_SUBSTRINGS,
    r"mkfs\b", r"dd\s+.*of=/dev/", r":\(\)\s*\{\s*:",
    r"sudo\b", r"su\b", r"doas\b",
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
    if low.startswith("cd ") and low.count("&&") == 1 and ";" not in low:
        rest = low.split("&&", 1)[1].strip()
        if rest and not rest.startswith("cd "):
            return classify_command(rest)
    for pat in _DENY_PATTERNS:
        if re.search(pat, low):
            return "DENY"
    words = low.split()
    first = words[0].split("/")[-1] if words else ""
    second = words[1] if len(words) > 1 else ""
    if first in _READONLY_BINARIES and not any(t in low for t in (" > ", ">>", "| sh", "|sh", "`", "$(")):
        # git can write; only read-only git verbs stay AUTO
        if first == "git" and not re.search(
            r"git(?:\s+-c\s+\S+)?\s+(status|log|diff|show|branch|remote|stash\s+list)\b",
            low,
        ):
            return "CONFIRM"
        return "AUTO"
    if first in _JOURNAL_BINARIES:
        return "AUTO_JOURNAL"
    if first in _INTERPRETERS:
        # Opaque code execution in every form (scripts and -c alike).
        return "CONFIRM"
    if first in _CONFIRM_BINARIES:
        return "CONFIRM"
    if second in _INLINE_FLAGS and first not in _READONLY_BINARIES:
        return "CONFIRM"
    return "CONFIRM"


def redact(text: str) -> str:
    return _SECRET_REDACT.sub(r"\1=<redacted>", text or "")


def _expand_tokens(cmd: str) -> list[str]:
    """Split a command into tokens with $HOME/~ expanded for path checks."""
    import shlex

    home = str(Path.home())
    try:
        toks = shlex.split(cmd, posix=True)
    except ValueError:
        return []
    out = []
    for tok in toks:
        tok = tok.replace("$HOME", home).replace("${HOME}", home)
        if tok.startswith("~"):
            tok = home + tok[1:]
        out.append(tok)
    return out


def _looks_like_path(tok: str) -> bool:
    return "/" in tok or tok.startswith(".") or tok.startswith("~")


def shell_secret_hit(cmd: str, cwd: str | None = None) -> bool:
    """True when the command names a secrets path (lane-level, resolved)."""
    from .files import _secret_reason

    toks = _expand_tokens(cmd)
    if not toks:
        # Unparseable: fall back to raw-substring conservatism.
        low = (cmd or "").lower()
        return any(h in low for h in ("id_rsa", ".ssh/", ".pem", "credentials", ".gnupg"))
    base = Path(str(cwd or "."))
    for tok in toks:
        if not _looks_like_path(tok):
            continue
        try:
            candidate = (base / tok).resolve() if not Path(tok).is_absolute() else Path(tok).resolve()
        except OSError:
            continue
        if _secret_reason(candidate) is not None:
            return True
    return False


def rm_system_target(cmd: str, cwd: str | None = None, root=None) -> bool:
    """True when an rm-like command targets a system location or workspace root."""
    toks = _expand_tokens(cmd)
    if not toks:
        return False
    first = toks[0].split("/")[-1].lower()
    if first not in {"rm", "rmdir", "unlink", "shred"}:
        return False
    base = Path(str(cwd or (str(root) if root is not None else ".")))
    try:
        home = Path.home().resolve()
        lane_root = Path(str(root)).resolve() if root is not None else None
    except OSError:
        home, lane_root = None, None
    for tok in toks[1:]:
        if tok.startswith("-") or tok in {"*", ".", ".."}:
            continue
        try:
            candidate = (base / tok).resolve() if not Path(tok).is_absolute() else Path(tok).resolve()
        except OSError:
            continue
        text = str(candidate)
        if text in _DANGEROUS_RM_TARGETS or text == "/":
            return True
        if any(text == rooted or text.startswith(rooted + "/")
               for rooted in _DANGEROUS_RM_TARGETS):
            return True
        if home is not None and candidate == home:
            return True
        if lane_root is not None and candidate == lane_root:
            return True
    return False


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
        if shell_secret_hit(cmd, cwd or str(self.root)):
            cls = "DENY"
        if rm_system_target(cmd, cwd or str(self.root), self.root):
            cls = "DENY"
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
        if shell_secret_hit(cmd, cwd or str(self.root)):
            cls = "DENY"
        if rm_system_target(cmd, cwd or str(self.root), self.root):
            cls = "DENY"
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
            # Keep the pane after the command so the exit marker stays readable.
            # A nested shell so `exit` and parentheses inside the command
            # cannot skip or break the exit marker.
            quoted = shlex.quote(cmd)
            args += ["sh", "-c", f"sh -c {quoted}; echo __EV_EXIT:$?; exec sleep 86400"]
            created = subprocess.run(args, capture_output=True, text=True)
            if created.returncode != 0 or not self._tmux_alive(name):
                self._sessions.pop(sid, None)
                detail = (created.stderr or created.stdout or "tmux session did not start").strip()
                return {"ok": False, "verified": False,
                        "error": detail[:300] or "tmux session did not start",
                        "evidence": {"cmd": cmd}, "undo": None}
            self._sessions[sid]["tmux"] = name
        else:
            try:
                p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     cwd=cwd or str(self.root))
            except OSError as exc:
                self._sessions.pop(sid, None)
                return {"ok": False, "verified": False, "error": str(exc),
                        "evidence": {"cmd": cmd}, "undo": None}
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
            alive = self._tmux_alive(s["tmux"])
            if alive:
                p = subprocess.run(["tmux", "capture-pane", "-p", "-t", s["tmux"]],
                                   capture_output=True, text=True)
                s["out"] = redact(p.stdout or "")
            marker = re.search(r"__EV_EXIT:(\d+)", s.get("out") or "")
            if marker:
                s["exit_code"] = int(marker.group(1))
            return s.get("out", "")
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
        # Typed input is executed (Enter is pressed): same gates as run.
        if shell_secret_hit(data, str(self.root)) or classify_command(data) == "DENY":
            return {"ok": False, "verified": True, "error": "denied by policy",
                    "evidence": {}, "undo": None}
        if "tmux" in s:
            if not self._tmux_alive(s["tmux"]):
                return {"ok": False, "verified": True, "error": "session exited",
                        "evidence": {}, "undo": None}
            sent = subprocess.run(
                ["tmux", "send-keys", "-t", s["tmux"], data, "Enter"],
                capture_output=True, text=True,
            )
            if sent.returncode != 0:
                detail = (sent.stderr or sent.stdout or "send-keys failed").strip()
                return {"ok": False, "verified": False, "error": detail[:300],
                        "evidence": {}, "undo": None}
            visible = False
            out = ""
            deadline = time.time() + 1.5
            while time.time() < deadline:
                out = self._collect(session_id)
                if data and data in out:
                    visible = True
                    break
                time.sleep(0.1)
            return {"ok": True, "verified": visible,
                    "evidence": {"output": out[-500:]},
                    "error": None if visible else "input was not visible in the pane",
                    "undo": None}
        else:
            return {"ok": False, "verified": True, "error": "input unsupported without tmux",
                    "evidence": {}, "undo": None}

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
            with contextlib.suppress(Exception):
                s["proc"].kill()
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
        return {"ok": True, "verified": code is not None, "exit_code": code,
                "evidence": {}, "error": None, "undo": None}


def _has_tmux() -> bool:
    try:
        p = subprocess.run(["which", "tmux"], capture_output=True)
        return p.returncode == 0
    except Exception:
        return False
