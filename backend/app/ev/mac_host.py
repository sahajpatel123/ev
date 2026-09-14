"""Owner-Mac host commands. This is not the R4 sandbox jail.

The R4 jail cannot see $HOME (seatbelt denies /Users). Evie needs the real
Mac to find files, open apps, and run a small allowlist of inspection
commands. Destructive, networked, and secret-touching argv are refused.
The realtime catalog never sees execute_command; this runner is an
internal computer-broker hand, same as mdfind inside laptop_files.
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.ev.laptop_files import path_denied

logger_name = "ev.mac_host"

DEFAULT_TIMEOUT_S = 12
MAX_OUTPUT_CHARS = 8_000
MAX_ARGV = 24

_ALLOWED_BINS = {
    "mdfind": "/usr/bin/mdfind",
    "mdls": "/usr/bin/mdls",
    "ls": "/bin/ls",
    "stat": "/usr/bin/stat",
    "file": "/usr/bin/file",
    "open": "/usr/bin/open",
    "sw_vers": "/usr/bin/sw_vers",
    "hostname": "/bin/hostname",
    "whoami": "/usr/bin/whoami",
    "uname": "/usr/bin/uname",
    "pgrep": "/usr/bin/pgrep",
    "pwd": "/bin/pwd",
    "date": "/bin/date",
    "find": "/usr/bin/find",
}

_DENIED_TOKENS = frozenset(
    {
        "sudo",
        "rm",
        "rmdir",
        "chmod",
        "chown",
        "mkfs",
        "dd",
        "kill",
        "reboot",
        "shutdown",
        "curl",
        "wget",
        "nc",
        "ssh",
        "scp",
        "python",
        "python3",
        "perl",
        "ruby",
        "osascript",
        "bash",
        "zsh",
        "sh",
        "eval",
        "exec",
        "pbcopy",
        "pbpaste",
    }
)

_DENIED_FIND_FLAGS = frozenset({"-delete", "-exec", "-ok", "-fprint"})

_MAC_CMD_RE = re.compile(
    r"\b(?:"
    r"(?:run|execute)\s+(?:the\s+)?(?:command\s+|in\s+(?:the\s+)?(?:terminal|shell)\s+)?"
    r"(?:ls|mdfind|find|open|hostname|whoami|sw_vers|uname|pgrep|stat|file|pwd|date)\b|"
    r"(?:run|execute)\s+(?:this\s+)?(?:in|from)\s+(?:the\s+)?(?:terminal|shell)\b|"
    r"in\s+(?:the\s+)?terminal\s+(?:run\s+)?|"
    r"using\s+(?:the\s+)?(?:terminal|shell)\s+to\s+|"
    r"what(?:'s| is) (?:this mac|my (?:hostname|os(?: version)?|mac os))|"
    r"\bmdfind\b|"
    r"\bls\s+(?:-l\s+)?(?:~|/Users/|my\s+(?:home|desktop|documents|downloads))"
    r")",
    re.I,
)

_OPEN_A_RE = re.compile(
    r"\b(?:open|launch)\s+(?:the\s+)?(?:app\s+)?(?P<name>[A-Za-z][\w .+-]{1,40}?)(?:\s+app)?\s*$",
    re.I,
)


def mac_context() -> dict[str, Any]:
    """Facts about this Mac that Evie can use without a shell round-trip."""

    home = str(Path.home())
    return {
        "os": platform.system(),
        "os_release": platform.mac_ver()[0] or platform.release(),
        "machine": platform.machine(),
        "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
        "home": home,
        "hostname": platform.node(),
        "cwd": home,
        "shell": os.environ.get("SHELL") or "/bin/zsh",
    }


def looks_like_mac_command(text: str | None) -> bool:
    """True for an owner ask that is a host command, not code or a web lookup."""

    raw = (text or "").strip()
    if not raw:
        return False
    from app.ev.luna_code import looks_like_code_request

    if looks_like_code_request(raw):
        return False
    from app.ev.laptop_files import looks_like_file_task

    if looks_like_file_task(raw) and not _MAC_CMD_RE.search(raw):
        return False
    return bool(_MAC_CMD_RE.search(raw))


def _resolve_bin(name: str) -> str | None:
    key = (name or "").strip().lower()
    pinned = _ALLOWED_BINS.get(key)
    if pinned and Path(pinned).exists():
        return pinned
    found = shutil.which(key)
    if not found:
        return None
    resolved = str(Path(found).resolve())
    allowed = {str(Path(path).resolve()) for path in _ALLOWED_BINS.values() if Path(path).exists()}
    if resolved not in allowed:
        return None
    return resolved


def _argv_denied(argv: list[str]) -> str | None:
    if not argv:
        return "empty_command"
    if len(argv) > MAX_ARGV:
        return "too_many_args"
    head = Path(argv[0]).name.lower()
    if head in _DENIED_TOKENS or any(tok in _DENIED_TOKENS for tok in argv[1:]):
        return "command_denied"
    joined = " ".join(argv).lower()
    if any(flag in joined for flag in (" -exec", " -delete", "&&", "||", ";", "`", "$(")):
        return "command_denied"
    if head == "find" and any(flag.lower() in _DENIED_FIND_FLAGS for flag in argv[1:]):
        return "command_denied"
    if head == "open":
        for item in argv[1:]:
            if item.startswith("-") and item not in {"-a", "-g", "-e", "-t", "-R"}:
                return "command_denied"
    for item in argv[1:]:
        if item.startswith("-") or item in {"-onlyin", "-name", "-iname", "-type", "-maxdepth"}:
            continue
        if "/" in item or item.startswith("~"):
            try:
                candidate = Path(item).expanduser()
                if candidate.exists() and path_denied(candidate) is not None:
                    return "path_denied"
            except OSError:
                return "path_denied"
    return None


def parse_mac_argv(text: str) -> list[str] | None:
    """Best-effort argv from a spoken or typed owner command."""

    raw = (text or "").strip()
    if not raw:
        return None
    stripped = re.sub(
        r"^(?:please\s+|can you\s+|could you\s+|would you\s+)?"
        r"(?:run|execute)\s+(?:the\s+)?(?:command\s+)?"
        r"(?:in\s+(?:the\s+)?(?:terminal|shell)\s+)?",
        "",
        raw,
        flags=re.I,
    ).strip()
    stripped = re.sub(
        r"\s+(?:in|from)\s+(?:the\s+)?(?:terminal|shell)\s*$",
        "",
        stripped,
        flags=re.I,
    ).strip()
    if not stripped:
        return None
    open_app = _OPEN_A_RE.search(stripped)
    if open_app and not re.search(r"\b(?:file|folder|document)\b", stripped, re.I):
        name = open_app.group("name").strip()
        if name.lower() not in {"it", "that", "this", "the", "my"}:
            return ["open", "-a", name]
    try:
        argv = shlex.split(stripped, posix=True)
    except ValueError:
        argv = stripped.split()
    if not argv:
        return None
    head = Path(argv[0]).name.lower()
    if head not in _ALLOWED_BINS:
        return None
    bin_path = _resolve_bin(head)
    if not bin_path:
        return None
    argv[0] = bin_path
    for i, item in enumerate(argv[1:], start=1):
        if item == "~" or item.startswith("~/"):
            argv[i] = str(Path(item).expanduser())
    return argv


def run_argv(argv: list[str], *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    denied = _argv_denied(argv)
    if denied:
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": denied,
            "spoken": "I won't run that on this Mac.",
            "source": "mac_host",
        }
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=max(1.0, min(float(timeout), 30.0)),
            check=False,
            cwd=str(Path.home()),
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(Path.home()),
                "USER": os.environ.get("USER") or "",
                "LANG": "en_US.UTF-8",
            },
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "executed": True,
            "verified": False,
            "error": "timeout",
            "spoken": "That command timed out.",
            "source": "mac_host",
        }
    except OSError as exc:
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "exec_failed",
            "spoken": f"I couldn't run that. {type(exc).__name__}",
            "source": "mac_host",
        }
    out = ((proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")).strip()
    out = out[:MAX_OUTPUT_CHARS]
    ok = proc.returncode == 0
    spoken = out.splitlines()[0][:400] if out else (
        "Done." if ok else "That command failed on this Mac."
    )
    if ok and len(out.splitlines()) > 1:
        spoken = f"{Path(argv[0]).name} returned {min(len(out.splitlines()), 40)} lines."
    return {
        "ok": ok,
        "executed": True,
        "verified": ok,
        "error": None if ok else "exit",
        "exit_code": proc.returncode,
        "output": out,
        "spoken": spoken if ok else (out[:400] or "That command failed on this Mac."),
        "argv": [Path(argv[0]).name, *argv[1:]],
        "source": "mac_host",
    }


def open_url_in_app(app: str, url: str, *, background: bool = True) -> dict[str, Any]:
    """Open a URL in a named Mac app via `/usr/bin/open -a`."""

    name = (app or "").strip()
    dest = (url or "").strip()
    if not name or not dest:
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "missing_target",
            "spoken": "Which app and link should I open?",
            "source": "mac_host",
        }
    if not re.match(r"^https?://", dest, re.I):
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "not_a_url",
            "spoken": "That's not a link I can open.",
            "source": "mac_host",
        }
    bin_path = _resolve_bin("open")
    if not bin_path:
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "open_unavailable",
            "spoken": "I couldn't reach open on this Mac.",
            "source": "mac_host",
        }
    argv = [bin_path]
    if background:
        argv.append("-g")
    argv.extend(["-a", name, dest])
    result = run_argv(argv)
    if result.get("ok"):
        result["spoken"] = f"Opened that in {name}."
        result["url"] = dest
        result["app"] = name
    return result


def run_owner_mac_goal(text: str) -> dict[str, Any]:
    """Parse a spoken host command and run it on this Mac."""

    argv = parse_mac_argv(text)
    if argv is None:
        ctx = mac_context()
        return {
            "ok": True,
            "executed": True,
            "verified": True,
            "action": "context",
            "context": ctx,
            "spoken": (
                f"This Mac is {ctx['hostname']}, user {ctx['user']}, "
                f"home {ctx['home']}."
            ),
            "source": "mac_host",
        }
    result = run_argv(argv)
    result.setdefault("goal", text[:500])
    return result
