"""Bounded coding workspace: read, patch, search, and run allowlisted programs.

This is Evie's software hands, not her brain. Luna decides what to change;
this module enforces the jail:

- no shell
- work stays inside an owner-allowed project root
- only named interpreters/tools, with extra git/uv subcommand fences
- no network installers, no privilege tools, no secret-looking filenames
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from app.config import settings

ALLOWED_BINARIES = frozenset(
    {
        "python3",
        "python",
        "pytest",
        "node",
        "ruby",
        "php",
        "perl",
        "lua",
        "java",
        "javac",
        "dart",
        "tsc",
        "ruff",
        "mypy",
        "swift",
        "swiftc",
        "go",
        "uv",
        "git",
        "cargo",
        "rustc",
    }
)
FORBIDDEN_BINARIES = frozenset(
    {
        "rm",
        "sudo",
        "chmod",
        "chown",
        "dd",
        "mkfs",
        "diskutil",
        "launchctl",
        "kill",
        "killall",
        "reboot",
        "shutdown",
        "halt",
        "csrutil",
        "dscl",
        "security",
        "osascript",
        "curl",
        "wget",
        "ssh",
        "scp",
        "nc",
        "ncat",
        "bash",
        "sh",
        "zsh",
        "fish",
        "dash",
        "csh",
        "ksh",
        "npm",
        "npx",
        "yarn",
        "pnpm",
        "bun",
        "deno",
        "pip",
        "pip3",
        "make",
    }
)
GIT_SUBCOMMANDS = frozenset(
    {"status", "diff", "log", "show", "rev-parse", "ls-files", "branch", "describe"}
)
UV_SUBCOMMANDS = frozenset({"run", "tree"})
CARGO_SUBCOMMANDS = frozenset({"test", "check", "build", "clippy", "fmt"})
GO_SUBCOMMANDS = frozenset({"test", "run", "build", "fmt", "vet"})
SWIFT_SUBCOMMANDS = frozenset({"test", "build", "run"})
INLINE_EVAL_FLAGS = {
    "node": ("-e", "--eval"),
    "ruby": ("-e", "--eval"),
    "php": ("-r", "-R"),
    "perl": ("-e", "-E"),
    "lua": ("-e",),
}
PYTHON_MODULES = frozenset({"pytest", "unittest", "ruff", "mypy"})
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".build",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        "DerivedData",
        ".next",
        "coverage",
        ".pytest_cache",
        "Pods",
        ".ev",
        ".cursor",
    }
)
PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "Package.swift",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "Package.resolved",
)
GENERIC_PROJECT_NAMES = frozenset(
    {"code", "src", "app", "test", "tests", "workspace", "project", "repo", "code-workspace"}
)
SECRET_NAME_RE = re.compile(
    r"(^\.env$|\.pem$|id_rsa|id_ed25519|credentials\.json$|secrets\.json$)",
    re.IGNORECASE,
)
UNSAFE_ARG_RE = re.compile(r"[;|&`$]|\$\(|\n")
MAX_LISTING = 80
MAX_READ_CHARS = 24_000
MAX_STDOUT = 16_384
MAX_SEARCH_HITS = 40
MAX_SEARCH_FILES = 800

_active_root: ContextVar[Path | None] = ContextVar("ev_code_active_root", default=None)


class CodeJailError(ValueError):
    """Raised when a coding operation is outside the bounded workspace."""


def set_active_project(root: Path | None):
    """Bind subsequent jail calls to one allowed project root."""

    return _active_root.set(root.resolve() if root is not None else None)


def reset_active_project(token) -> None:
    _active_root.reset(token)


def workspace_root() -> Path:
    active = _active_root.get()
    if active is not None:
        return active
    configured = str(getattr(settings, "code_workspace", None) or "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        root = Path.home() / "Library" / "Application Support" / "EV" / "code-workspace"
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def projects_root() -> Path | None:
    raw = getattr(settings, "code_projects_root", None)
    if raw is not None:
        text = str(raw).strip()
        if not text:
            return None
        path = Path(text).expanduser()
        return path.resolve() if path.exists() and path.is_dir() else None
    candidate = Path.home() / "Code"
    if candidate.is_dir():
        return candidate.resolve()
    return None


def list_projects() -> list[dict[str, str]]:
    """Named roots Evie may edit. Default sandbox plus owner Code projects."""

    found: dict[str, Path] = {}
    default = _default_workspace_path()
    if not default.exists():
        default.mkdir(parents=True, exist_ok=True)
    found[_unique_name(default.name.lower() or "workspace", found)] = default.resolve()

    extra = str(getattr(settings, "code_projects", None) or "").strip()
    for part in re.split(r"[,;]", extra):
        part = part.strip()
        if not part:
            continue
        name, path = _parse_project_entry(part)
        if path is None or not path.exists() or not path.is_dir():
            continue
        found[_unique_name(name, found)] = path

    root = projects_root()
    if root is not None:
        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir() or child.name.startswith(".") or child.name in SKIP_DIR_NAMES:
                continue
            if not _looks_like_project(child):
                continue
            key = child.name.lower()
            if key in found and found[key] == child.resolve():
                continue
            found[_unique_name(key, found)] = child.resolve()

    return [{"name": name, "path": str(path)} for name, path in found.items()]


def select_project(goal: str) -> Path:
    """Pick an allowed root from the owner phrasing, else the default workspace."""

    catalog = {item["name"]: Path(item["path"]) for item in list_projects()}
    lowered = (goal or "").lower()
    named = [
        name
        for name in sorted(catalog, key=len, reverse=True)
        if name not in GENERIC_PROJECT_NAMES
        and len(name) >= 2
        and re.search(
            rf"(?:in |on |inside |from )(?:the |my |our )?{re.escape(name)}\b|"
            rf"\b{re.escape(name)} (?:repo|project|codebase|app|package|tree)\b",
            lowered,
        )
    ]
    if named:
        return catalog[named[0]]
    return workspace_root()


def use_project(name: str) -> dict[str, Any]:
    wanted = (name or "").strip().lower()
    catalog = {item["name"]: Path(item["path"]) for item in list_projects()}
    if wanted not in catalog:
        return {
            "ok": False,
            "error": "unknown_project",
            "detail": "Project is not in the owner allowlist.",
            "projects": [{"name": item["name"], "path": item["path"]} for item in list_projects()],
        }
    set_active_project(catalog[wanted])
    return {"ok": True, "project": wanted, "path": str(catalog[wanted])}


def resolve_workspace_path(rel: str, *, directory: bool = False, create: bool = False) -> Path:
    raw = (rel or "").strip()
    if not raw or len(raw) > 512:
        raise CodeJailError("path must be 1-512 characters")
    if raw.startswith("~") or raw.startswith("/"):
        raise CodeJailError("path must be relative to the coding workspace")
    root = workspace_root()
    target = (root / raw).resolve()
    if target != root and root not in target.parents:
        raise CodeJailError("path escapes the coding workspace")
    if create and directory:
        target.mkdir(parents=True, exist_ok=True)
        return target
    if create and not directory:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def list_dir(rel: str = ".") -> dict[str, Any]:
    target = resolve_workspace_path(rel or ".", directory=True, create=False)
    if not target.exists() or not target.is_dir():
        raise CodeJailError("not a directory")
    names: list[str] = []
    children = sorted(target.iterdir(), key=lambda item: item.name.lower())
    visible = [child for child in children if child.name not in SKIP_DIR_NAMES][:MAX_LISTING]
    for child in visible:
        suffix = "/" if child.is_dir() else ""
        names.append(child.name + suffix)
    return {
        "ok": True,
        "path": _rel(target),
        "entries": names,
        "truncated": len(children) > MAX_LISTING,
        "project": str(workspace_root()),
    }


def read_file(rel: str, *, offset: int = 1, limit: int | None = None) -> dict[str, Any]:
    target = resolve_workspace_path(rel, create=False)
    if SECRET_NAME_RE.search(target.name):
        raise CodeJailError("refusing to read a secret-looking filename")
    if not target.is_file():
        raise CodeJailError("not a file inside the coding workspace")
    data = target.read_bytes()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    start = max(1, int(offset or 1))
    if limit is None:
        chunk = "".join(lines[start - 1 :])[:MAX_READ_CHARS]
        sliced = False
    else:
        end = start - 1 + max(1, int(limit))
        chunk = "".join(lines[start - 1 : end])
        sliced = end < len(lines)
        if len(chunk) > MAX_READ_CHARS:
            chunk = chunk[:MAX_READ_CHARS]
            sliced = True
    return {
        "ok": True,
        "path": _rel(target),
        "content": chunk,
        "offset": start,
        "line_count": len(lines),
        "truncated": sliced or len(data) > MAX_READ_CHARS,
        "bytes": len(data),
    }


def write_file(rel: str, content: str) -> dict[str, Any]:
    name = Path(rel or "").name
    if SECRET_NAME_RE.search(name):
        raise CodeJailError("refusing to write a secret-looking filename")
    cap = int(getattr(settings, "code_max_file_bytes", 256_000) or 256_000)
    encoded = (content or "").encode("utf-8")
    if len(encoded) > cap:
        raise CodeJailError(f"file exceeds the {cap} byte coding cap")
    target = resolve_workspace_path(rel, create=True)
    target.write_bytes(encoded)
    return {
        "ok": True,
        "path": _rel(target),
        "bytes": len(encoded),
    }


def replace_in_file(rel: str, old: str, new: str, *, replace_all: bool = False) -> dict[str, Any]:
    if not old:
        raise CodeJailError("old text is required")
    target = resolve_workspace_path(rel, create=False)
    if SECRET_NAME_RE.search(target.name):
        raise CodeJailError("refusing to edit a secret-looking filename")
    if not target.is_file():
        raise CodeJailError("not a file inside the coding workspace")
    cap = int(getattr(settings, "code_max_file_bytes", 256_000) or 256_000)
    data = target.read_bytes()
    if len(data) > cap:
        raise CodeJailError(f"file exceeds the {cap} byte coding cap")
    text = data.decode("utf-8")
    count = text.count(old)
    if count == 0:
        raise CodeJailError("old text was not found")
    if count > 1 and not replace_all:
        raise CodeJailError(f"old text matched {count} times; pass replace_all or a unique snippet")
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    encoded = updated.encode("utf-8")
    if len(encoded) > cap:
        raise CodeJailError(f"file exceeds the {cap} byte coding cap")
    target.write_bytes(encoded)
    return {
        "ok": True,
        "path": _rel(target),
        "replacements": count if replace_all else 1,
        "bytes": len(encoded),
    }


def search_text(
    pattern: str,
    rel: str = ".",
    *,
    glob: str = "",
    max_hits: int = MAX_SEARCH_HITS,
) -> dict[str, Any]:
    raw = (pattern or "").strip()
    if not raw or len(raw) > 200:
        raise CodeJailError("search pattern must be 1-200 characters")
    try:
        compiled = re.compile(raw)
    except re.error as exc:
        raise CodeJailError(f"invalid search pattern: {exc}") from exc
    root = resolve_workspace_path(rel or ".", directory=True, create=False)
    if not root.exists():
        raise CodeJailError("search path does not exist")
    hits: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    for path in _walk_source_files(root):
        scanned += 1
        if scanned > MAX_SEARCH_FILES:
            truncated = True
            break
        if glob and not (
            fnmatch.fnmatch(path.name, glob)
            or fnmatch.fnmatch(_rel(path), glob)
        ):
            continue
        if SECRET_NAME_RE.search(path.name):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for index, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                hits.append(
                    {
                        "path": _rel(path),
                        "line": index,
                        "text": line.strip()[:240],
                    }
                )
                if len(hits) >= max(1, min(int(max_hits or MAX_SEARCH_HITS), 80)):
                    return {
                        "ok": True,
                        "pattern": raw,
                        "hits": hits,
                        "truncated": True,
                        "scanned": scanned,
                    }
    return {
        "ok": True,
        "pattern": raw,
        "hits": hits,
        "truncated": truncated,
        "scanned": scanned,
    }


def run_argv(argv: list[str], *, timeout_seconds: int | None = None) -> dict[str, Any]:
    if not argv:
        raise CodeJailError("command is empty")
    binary = Path(str(argv[0])).name
    if binary in FORBIDDEN_BINARIES or binary not in ALLOWED_BINARIES:
        raise CodeJailError(f"binary '{binary}' is not allowlisted")
    cleaned: list[str] = []
    for item in argv:
        text = str(item)
        if len(text) > 2_000:
            raise CodeJailError("argument too long")
        if UNSAFE_ARG_RE.search(text):
            raise CodeJailError("shell metacharacters are not allowed")
        cleaned.append(text)
    _fence_argv(binary, cleaned)
    cleaned[0] = _resolve_binary(binary)
    timeout = timeout_seconds
    if timeout is None:
        timeout = int(getattr(settings, "code_command_timeout_seconds", 20) or 20)
    timeout = max(1, min(timeout, 180))
    env = _run_env()
    try:
        proc = subprocess.run(
            cleaned,
            cwd=workspace_root(),
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CodeJailError(f"binary not found: {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CodeJailError(f"command timed out after {timeout}s") from exc
    stdout = _clip(proc.stdout)
    stderr = _clip(proc.stderr)
    return {
        "ok": proc.returncode == 0,
        "argv": [binary, *cleaned[1:]],
        "exit_code": proc.returncode,
        "stdout": stdout["text"],
        "stderr": stderr["text"],
        "stdout_truncated": stdout["truncated"],
        "stderr_truncated": stderr["truncated"],
        "cwd": str(workspace_root()),
    }


def _default_workspace_path() -> Path:
    configured = str(getattr(settings, "code_workspace", None) or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Application Support" / "EV" / "code-workspace"


def _looks_like_project(path: Path) -> bool:
    return any((path / marker).exists() for marker in PROJECT_MARKERS)


def _parse_project_entry(part: str) -> tuple[str, Path | None]:
    if ":" in part:
        name, _, rest = part.partition(":")
        rest = rest.strip()
        if rest.startswith("/") or rest.startswith("~"):
            path = Path(rest).expanduser()
            try:
                return (name.strip().lower() or path.name.lower(), path.resolve())
            except OSError:
                return name.strip().lower(), None
    path = Path(part).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        return path.name.lower(), None
    return resolved.name.lower(), resolved


def _unique_name(name: str, existing: dict[str, Path]) -> str:
    key = name or "project"
    if key not in existing:
        return key
    index = 2
    while f"{key}-{index}" in existing:
        index += 1
    return f"{key}-{index}"


def _walk_source_files(root: Path):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES and not name.startswith(".")
        ]
        current = Path(dirpath)
        for filename in filenames:
            if filename.startswith("."):
                continue
            path = current / filename
            if SECRET_NAME_RE.search(filename):
                continue
            try:
                probe = path.read_bytes()[:1024]
            except OSError:
                continue
            if b"\0" in probe:
                continue
            yield path


def _fence_argv(binary: str, cleaned: list[str]) -> None:
    if binary in {"python", "python3"}:
        if len(cleaned) >= 2 and cleaned[1] == "-c":
            raise CodeJailError("python -c is not allowed")
        if len(cleaned) >= 3 and cleaned[1] == "-m" and cleaned[2] not in PYTHON_MODULES:
            raise CodeJailError(f"python -m {cleaned[2]} is not allowlisted")
    flags = INLINE_EVAL_FLAGS.get(binary)
    if flags:
        for item in cleaned[1:]:
            if item in flags:
                raise CodeJailError(f"{binary} inline eval is not allowed")
            for flag in flags:
                if len(flag) != 2:
                    continue
                if (
                    item.startswith(flag)
                    and len(item) > 2
                    and not item.startswith("--")
                    and not item.startswith("-encoding")
                ):
                    raise CodeJailError(f"{binary} inline eval is not allowed")
    if binary == "git":
        if len(cleaned) < 2 or cleaned[1].startswith("-"):
            raise CodeJailError("git subcommand required")
        if cleaned[1] not in GIT_SUBCOMMANDS:
            raise CodeJailError(f"git {cleaned[1]} is not allowlisted")
    if binary == "uv":
        if len(cleaned) < 2 or cleaned[1] not in UV_SUBCOMMANDS:
            raise CodeJailError("uv may only run or tree")
        if cleaned[1] == "run":
            rest = [item for item in cleaned[2:] if not item.startswith("-")]
            if not rest or Path(rest[0]).name not in ALLOWED_BINARIES:
                raise CodeJailError("uv run is limited to allowlisted programs")
    if binary == "cargo":
        if len(cleaned) < 2 or cleaned[1] not in CARGO_SUBCOMMANDS:
            raise CodeJailError("cargo subcommand is not allowlisted")
    if binary == "go":
        if len(cleaned) < 2 or cleaned[1] not in GO_SUBCOMMANDS:
            raise CodeJailError("go subcommand is not allowlisted")
    if binary == "swift":
        rest = cleaned[1:]
        if not rest:
            raise CodeJailError("swift subcommand or source file required")
        if rest[0] not in SWIFT_SUBCOMMANDS and not rest[0].endswith(".swift"):
            raise CodeJailError("swift subcommand is not allowlisted")
    if binary == "dart":
        if len(cleaned) >= 2 and cleaned[1] in {"pub", "aotrun"}:
            raise CodeJailError("dart pub is not allowlisted")


def _run_env() -> dict[str, str]:
    path = os.environ.get("PATH") or "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"
    env = {
        "PATH": path,
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER") or "",
        "LANG": os.environ.get("LANG") or "en_US.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "TERM": "dumb",
    }
    tmp = os.environ.get("TMPDIR")
    if tmp:
        env["TMPDIR"] = tmp
    return env


def _resolve_binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise CodeJailError(f"binary not found: {name}")
    return found


def _rel(path: Path) -> str:
    return str(path.relative_to(workspace_root()))


def _clip(raw: bytes) -> dict[str, Any]:
    text = raw[:MAX_STDOUT].decode("utf-8", errors="replace")
    return {"text": text, "truncated": len(raw) > MAX_STDOUT}
