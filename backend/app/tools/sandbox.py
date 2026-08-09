"""Sandboxed command and file tools for approved actions.

Commands run without a shell, inside the sandbox root, with a minimal
environment, hard timeouts, and bounded output. File tools resolve every path
against the sandbox root and reject traversal, so the host filesystem is never
exposed to a tool call.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from app.config import settings

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300
MAX_COMMAND_LENGTH = 4_000
MAX_OUTPUT_BYTES = 64 * 1024
MAX_FILE_BYTES = 1024 * 1024


class SandboxError(ValueError):
    """Raised for invalid, escaping, or failed sandboxed operations."""


def sandbox_root() -> Path:
    root = Path(os.getenv("EV_SANDBOX_ROOT") or (Path(settings.storage_root) / "sandbox"))
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _resolve_inside(path: str, *, directory: bool = False) -> Path:
    root = sandbox_root()
    if not path or len(path) > 512:
        raise SandboxError("path must be 1-512 characters")
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise SandboxError("path escapes the sandbox root")
    if directory:
        target.mkdir(parents=True, exist_ok=True)
    return target


def run_command(
    command: str,
    *,
    cwd: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Execute one command inside the sandbox without a shell."""
    if not command or len(command) > MAX_COMMAND_LENGTH:
        raise SandboxError(f"command must be 1-{MAX_COMMAND_LENGTH} characters")
    timeout = max(1, min(timeout_seconds, MAX_TIMEOUT_SECONDS))
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise SandboxError(f"invalid command: {exc}") from exc
    if not args:
        raise SandboxError("empty command")
    workdir = _resolve_inside(cwd or ".", directory=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(sandbox_root()),
        "LANG": "C.UTF-8",
    }
    try:
        proc = subprocess.run(
            args,
            cwd=workdir,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SandboxError(f"command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"command timed out after {timeout}s") from exc
    stdout = proc.stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr = proc.stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": len(proc.stdout) > MAX_OUTPUT_BYTES,
        "stderr_truncated": len(proc.stderr) > MAX_OUTPUT_BYTES,
        "command": command[:200],
    }


def read_file(path: str) -> dict:
    """Read a file inside the sandbox root (bounded, text-safe)."""
    target = _resolve_inside(path)
    if not target.is_file():
        raise SandboxError("not a file inside the sandbox")
    data = target.read_bytes()
    return {
        "path": str(target.relative_to(sandbox_root())),
        "size_bytes": target.stat().st_size,
        "content": data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "truncated": len(data) > MAX_OUTPUT_BYTES,
    }


def write_file(path: str, content: str) -> dict:
    """Write a file inside the sandbox root with a hard size cap."""
    if len(content) > MAX_FILE_BYTES:
        raise SandboxError("file content exceeds the sandbox size cap")
    target = _resolve_inside(path)
    if target.is_dir():
        raise SandboxError("path is a directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "path": str(target.relative_to(sandbox_root())),
        "bytes": target.stat().st_size,
    }
