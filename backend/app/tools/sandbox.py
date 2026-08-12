"""Sandboxed command and file tools for approved actions.

Commands run without a shell, inside the sandbox root, with a minimal
environment, hard timeouts, bounded output, and — when the host supports it —
real OS isolation:

* macOS: ``sandbox-exec`` seatbelt profile — no network, host filesystem
  read/write denied except one scratch directory, CPU/memory/file-size/process
  rlimits applied before exec.
* Linux with Docker: ``docker run --network none --read-only --tmpfs`` with
  memory/CPU/pids limits and a bind-mounted scratch directory.
* Last resort: the previous process-level jail (documented honestly in the
  ``isolation`` field of every result; it is NOT a security boundary).

If seatbelt is selected but cannot be applied, the call fails closed instead
of silently degrading to the weaker jail.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from app.config import settings

logger = logging.getLogger("ev.tools.sandbox")

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300
MAX_COMMAND_LENGTH = 4_000
MAX_OUTPUT_BYTES = 64 * 1024
MAX_FILE_BYTES = 1024 * 1024
DEFAULT_MEMORY_MB = 512
DEFAULT_NPROC_LIMIT = 128
DEFAULT_FILE_SIZE_MB = 8
DOCKER_IMAGE = "python:3.12-slim"
SANDBOX_PATH = "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"


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


def _docker_available() -> bool:
    """True when the Docker daemon is reachable right now."""

    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def effective_isolation() -> str:
    """Strongest isolation this host can apply right now: seatbelt|docker|process."""

    if shutil.which("sandbox-exec"):
        return "seatbelt"
    if shutil.which("docker") and _docker_available():
        return "docker"
    return "process"


def _seatbelt_profile(scratch: Path) -> str:
    """Seatbelt policy: no network, host fs locked, scratch r/w allowed.

    ``allow`` operations listed after a ``deny`` override it in seatbelt, so
    the scratch subpath remains usable even when it lives under a denied
    prefix (e.g. ``/Users`` or ``/private/tmp``).
    """

    scratch = scratch.resolve()
    if '"' in str(scratch):
        raise SandboxError("sandbox root path must not contain double quotes")
    parts = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        '(deny file-write* (subpath "/"))',
        f'(allow file-write* (subpath "{scratch}"))',
        f'(allow file-write* (literal "{scratch}"))',
        '(deny file-read* (subpath "/Users"))',
        '(deny file-read* (subpath "/private/etc"))',
        '(deny file-read* (subpath "/etc"))',
        '(deny file-read* (subpath "/Volumes"))',
        '(deny file-read* (subpath "/tmp"))',
        '(deny file-read* (subpath "/private/tmp"))',
        '(deny file-read* (subpath "/private/var/folders"))',
        '(deny file-read* (subpath "/private/var/db"))',
        '(deny file-read* (subpath "/private/var/root"))',
        '(deny file-read* (subpath "/Library/Keychains"))',
        f'(allow file-read* (subpath "{scratch}"))',
        f'(allow file-read* (literal "{scratch}"))',
    ]
    return " ".join(parts)


def _child_env(scratch: Path) -> dict[str, str]:
    scratch_tmp = scratch / "tmp"
    scratch_tmp.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": SANDBOX_PATH,
        "HOME": str(scratch),
        "TMPDIR": str(scratch_tmp),
        "LANG": "C.UTF-8",
    }


def _resource_limits(
    *,
    timeout_seconds: int,
    memory_mb: int,
    nproc: int,
    file_size_mb: int,
) -> Callable[[], None]:
    """Return a preexec_fn that applies hard rlimits in the child."""

    import resource

    cpu_seconds = max(timeout_seconds * 2, timeout_seconds + 5)
    memory_bytes = memory_mb * 1024 * 1024
    file_size_bytes = file_size_mb * 1024 * 1024

    def apply() -> None:
        for name, limit in (
            ("RLIMIT_CPU", (cpu_seconds, cpu_seconds)),
            ("RLIMIT_AS", (memory_bytes, memory_bytes)),
            ("RLIMIT_FSIZE", (file_size_bytes, file_size_bytes)),
            ("RLIMIT_NOFILE", (64, 64)),
        ):
            try:
                resource.setrlimit(getattr(resource, name), limit)
            except (ValueError, OSError) as exc:
                logger.debug("could not apply %s in sandbox child: %s", name, exc)
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
            target = nproc if hard == resource.RLIM_INFINITY else min(nproc, hard)
            resource.setrlimit(resource.RLIMIT_NPROC, (target, target))
        except (ValueError, OSError) as exc:
            logger.debug("could not apply RLIMIT_NPROC in sandbox child: %s", exc)

    return apply


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with suppress(OSError):
            proc.kill()


def _rss_kb(pid: int) -> int:
    """RSS in KiB of one process (0 when unreadable)."""

    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        text = out.stdout.strip()
        return int(text.splitlines()[0].strip()) if text else 0
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return 0


def _memory_watchdog(
    proc: subprocess.Popen, memory_mb: int
) -> tuple[threading.Thread, list[bool]] | None:
    """Kill the process group when the command's RSS exceeds the budget.

    macOS refuses to lower RLIMIT_AS, so a live RSS watchdog is the portable
    hard memory limit: it polls ``ps`` and SIGKILLs the whole group.
    """

    if shutil.which("ps") is None:
        logger.warning("ps unavailable; sandbox memory watchdog disabled")
        return None
    limit_kb = memory_mb * 1024
    killed: list[bool] = [False]

    def watch() -> None:
        while proc.poll() is None:
            rss_kb = _rss_kb(proc.pid)
            if rss_kb > limit_kb:
                killed[0] = True
                _kill_process_group(proc)
                return
            time.sleep(0.05)

    thread = threading.Thread(target=watch, name="sandbox-memory-watchdog", daemon=True)
    thread.start()
    return thread, killed


def _build_argv(
    command: str,
    *,
    isolation: str,
    scratch: Path,
    memory_mb: int,
    nproc: int,
) -> list[str]:
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise SandboxError(f"invalid command: {exc}") from exc
    if not args:
        raise SandboxError("empty command")
    if isolation == "seatbelt":
        sandbox_exec = shutil.which("sandbox-exec")
        if sandbox_exec is None:
            raise SandboxError("seatbelt isolation requested but sandbox-exec is unavailable")
        return [sandbox_exec, "-p", _seatbelt_profile(scratch), *args]
    if isolation == "docker":
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/scratch:size=64m,exec",
            "--memory",
            f"{memory_mb}m",
            "--cpus",
            "1",
            "--pids-limit",
            str(nproc),
            "-v",
            f"{scratch}:/scratch",
            "-w",
            "/scratch",
            "-e",
            "HOME=/scratch",
            "-e",
            "TMPDIR=/scratch/tmp",
            "-e",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "-e",
            "LANG=C.UTF-8",
            DOCKER_IMAGE,
            *args,
        ]
    return args


def _run_argv(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    memory_mb: int,
    nproc: int,
    file_size_mb: int,
    isolation: str,
) -> tuple[int, bytes, bytes]:
    limiter = _resource_limits(
        timeout_seconds=timeout_seconds,
        memory_mb=memory_mb,
        nproc=nproc,
        file_size_mb=file_size_mb,
    )
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=limiter,
        )
    except Exception as exc:  # noqa: BLE001 - translate any spawn failure
        raise SandboxError(f"could not start sandboxed command: {exc}") from exc
    watchdog: tuple[threading.Thread, list[bool]] | None = None
    try:
        watchdog = _memory_watchdog(proc, memory_mb)
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        if watchdog is not None and watchdog[1][0]:
            raise SandboxError(
                f"command exceeded the sandbox memory limit of {memory_mb}MB"
            )
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()
        raise SandboxError(f"command timed out after {timeout_seconds}s") from exc
    except BaseException:
        _kill_process_group(proc)
        proc.wait()
        raise
    finally:
        if watchdog is not None:
            watchdog[0].join(timeout=1)
    if (
        isolation == "seatbelt"
        and proc.returncode == 71
        and b"Operation not permitted" in stderr
    ):
        raise SandboxError(
            "could not apply seatbelt isolation (Operation not permitted); "
            "refusing to run unsandboxed"
        )
    return proc.returncode, stdout, stderr


def run_command(
    command: str,
    *,
    cwd: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    isolation: str | None = None,
    memory_limit_mb: int = DEFAULT_MEMORY_MB,
    process_limit: int = DEFAULT_NPROC_LIMIT,
    file_size_mb: int = DEFAULT_FILE_SIZE_MB,
) -> dict:
    """Execute one command inside the sandbox without a shell.

    ``isolation`` is one of ``seatbelt``, ``docker``, ``process``, or ``None``
    (auto: strongest available). OS-level isolation is never silently
    downgraded: if seatbelt cannot be applied, the call raises.
    """

    if not command or len(command) > MAX_COMMAND_LENGTH:
        raise SandboxError(f"command must be 1-{MAX_COMMAND_LENGTH} characters")
    timeout = max(1, min(timeout_seconds, MAX_TIMEOUT_SECONDS))
    memory_mb = max(32, min(memory_limit_mb, 2048))
    nproc = max(16, min(process_limit, 512))
    resolved_isolation = isolation or effective_isolation()
    if resolved_isolation not in ("seatbelt", "docker", "process"):
        raise SandboxError(f"unknown isolation mode {resolved_isolation!r}")
    if resolved_isolation == "seatbelt" and shutil.which("sandbox-exec") is None:
        raise SandboxError("seatbelt isolation requested but sandbox-exec is unavailable")
    if resolved_isolation == "docker" and not _docker_available():
        raise SandboxError("docker isolation requested but the Docker daemon is unreachable")

    workdir = _resolve_inside(cwd or ".", directory=True)
    scratch = sandbox_root()
    env = _child_env(scratch)
    try:
        first_arg = shlex.split(command)[0]
    except ValueError as exc:
        raise SandboxError(f"invalid command: {exc}") from exc
    if shutil.which(first_arg, path=SANDBOX_PATH) is None:
        raise SandboxError(f"command not found: {first_arg}")
    args = _build_argv(
        command,
        isolation=resolved_isolation,
        scratch=scratch,
        memory_mb=memory_mb,
        nproc=nproc,
    )
    exit_code, stdout_bytes, stderr_bytes = _run_argv(
        args,
        cwd=workdir,
        env=env,
        timeout_seconds=timeout,
        memory_mb=memory_mb,
        nproc=nproc,
        file_size_mb=file_size_mb,
        isolation=resolved_isolation,
    )
    stdout = stdout_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr = stderr_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": len(stdout_bytes) > MAX_OUTPUT_BYTES,
        "stderr_truncated": len(stderr_bytes) > MAX_OUTPUT_BYTES,
        "command": command[:200],
        "isolation": resolved_isolation,
        "network": "blocked" if resolved_isolation != "process" else "unrestricted",
        "memory_limit_mb": memory_mb,
        "process_limit": nproc,
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
