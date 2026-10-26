"""Agent tool surface: shell, files, web, apps — one registry, honest receipts.

Every tool is a thin adapter over an existing engine (fabric shell/files lanes
for effects, the search provider registry for web search). No tool reimplements
policy: containment, secret-path denial, and command classification stay in the
lanes, and their DENY verdicts are final. Read-only helpers here (listing,
searching, fetching) carry their own explicit containment and SSRF guards
because the fabric lanes do not cover them.

Receipts mirror the fabric shape (``ok`` / ``verified`` / ``evidence`` /
``error`` / ``undo``) so the loop, the API, and the audit trail all read one
vocabulary. A receipt may carry ``needs_confirm: True`` meaning the lane would
run the effect only with explicit owner approval; the loop turns that into an
approval request, never a silent retry.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import settings
from app.contracts import ToolSpec

logger = logging.getLogger("ev.agent.tools")

# Tools the loop intercepts instead of executing through the registry.
CONTROL_TOOLS = frozenset({"finish", "ask_owner"})

MAX_LIST_ENTRIES = 300
MAX_SEARCH_RESULTS = 100
MAX_SEARCH_FILES = 4000
_MAX_REDIRECTS = 5


@dataclass
class ToolContext:
    """Everything one tool call is allowed to know about its run."""

    root: Path
    approved: bool = False
    run_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTool:
    spec: ToolSpec
    run: Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


def _receipt(
    *,
    ok: bool,
    error: str | None = None,
    result: Any = None,
    verified: bool = False,
    evidence: dict[str, Any] | None = None,
    undo: Any = None,
    needs_confirm: bool = False,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "verified": verified,
        "error": error,
        "result": result,
        "evidence": evidence or {},
        "undo": undo,
        "needs_confirm": needs_confirm,
    }


class NoWorkspaceRoot(RuntimeError):
    """No workspace root is configured, and defaulting to $HOME is not allowed.

    The owner's home folder is not a workspace: treating it as one made a
    hello-world listing walk all of ~, and made every relative path in a run
    resolve against the whole Mac. A missing root fails closed with the setup
    hint instead (D1/B4: explicit roots only).
    """


#: Where Evie keeps state when a caller needs a ToolContext but no workspace.
def _no_workspace_root() -> Path:
    configured = str(getattr(settings, "agent_trace_dir", "") or "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".ev" / "agent"
    path = base / "no-workspace"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return path
    return path


def configured_root() -> Path | None:
    """The explicitly configured workspace root, or None."""

    raw = str(getattr(settings, "agent_root", "") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _norm_root(root: Path | str | None) -> Path:
    if root is None:
        configured = configured_root()
        if configured is None:
            raise NoWorkspaceRoot(
                "No workspace root is configured. Set EV_AGENT_ROOT to a folder "
                "Evie may work in (for example ~/Desktop/evie-workspace); Evie "
                "will not treat your home folder as the workspace."
            )
        root = configured
    return Path(root).expanduser().resolve()


def action_context(*, run_id: str, approved: bool = True) -> ToolContext:
    """A context for tools that never touch the filesystem (e.g. open_app).

    Its root exists and is Evie-owned, so no owner folder is implied and no
    relative path can escape into one.
    """

    root = configured_root() or _no_workspace_root()
    return ToolContext(root=root, approved=approved, run_id=run_id)


#: The plan states a todo item may hold.
_TODO_STATUSES = ("pending", "in_progress", "completed")


async def _todo_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Record the run's visible plan.

    Grok's harness exposes ``todo_write`` for exactly this reason, and
    OpenBot has to switch the Claude SDK's own plan tools on explicitly
    ("without a terminal, the CLI gives the newer models no TodoWrite or task
    tools, so the agent has no plan for OpenBot to show as a task list").
    A model that keeps its plan in one place finishes more of what it starts,
    and the owner can see the shape of the work while it runs.

    The plan is state, not effect: it writes nothing to the Mac. It is
    recorded on the run's context and published on the run's event stream.
    """
    raw = args.get("todos")
    if not isinstance(raw, list):
        return _receipt(ok=False, error="todos must be a list", verified=True)

    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return _receipt(ok=False, error="each todo must be an object", verified=True)
        content = str(entry.get("content") or "").strip()
        if not content:
            return _receipt(ok=False, error="each todo needs a content string", verified=True)
        status = str(entry.get("status") or "pending").strip().lower()
        if status not in _TODO_STATUSES:
            return _receipt(
                ok=False,
                error=f"todo status must be one of {', '.join(_TODO_STATUSES)}",
                verified=True,
            )
        items.append({"content": content[:400], "status": status})

    if not items:
        return _receipt(ok=False, error="todos must not be empty", verified=True)

    extra = getattr(ctx, "extra", None)
    if extra is None:  # pragma: no cover - the loop always passes a context
        return _receipt(ok=False, error="no run context", verified=True)

    plan = list(items)
    if bool(args.get("merge")):
        previous = {
            str(row.get("content") or ""): row
            for row in (extra.get("plan") or [])
            if isinstance(row, dict)
        }
        for row in items:
            previous[str(row["content"])] = row
        plan = list(previous.values())
    extra["plan"] = plan

    emit = extra.get("emit")
    if callable(emit):
        try:
            emit("plan_updated", todos=plan)
        except Exception:  # noqa: BLE001 - telemetry must not fail the plan
            logger.debug("plan event sink failed", exc_info=True)

    return _receipt(
        ok=True,
        verified=True,
        result={"todos": plan, "count": len(plan)},
        evidence={"tool": "todo_write"},
    )



def _resolve(path: str, root: Path) -> Path:
    candidate = Path(str(path)).expanduser()
    return (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _inside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_roots_config() -> str:
    """The configured boundary for this run.

    Read from the environment first, then from settings. ``Settings`` is
    constructed once at import, so a boundary set after import — a test
    sandbox entering its scope, for instance — would be invisible to it. The
    boundary is a runtime thing, so it is read at runtime.
    """
    return (
        str(os.environ.get("EV_AGENT_ALLOWED_ROOTS") or "").strip()
        or str(getattr(settings, "agent_allowed_roots", "") or "").strip()
    )


def _allowed_roots(ctx: ToolContext) -> list[Path]:
    """Roots this run may touch: the run root, plus any configured extras."""
    roots: list[Path] = []
    with contextlib.suppress(OSError):
        roots.append(Path(ctx.root).resolve())
    configured = _allowed_roots_config()
    for raw in configured.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            candidate = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def _outside_allowed_roots(target: Path, ctx: ToolContext) -> bool:
    """True when a path is outside every root this run may touch.

    Empty configuration means no extra boundary: the fabric lanes keep their
    own rules (secret paths, journal roots) and an approved step may go
    where the owner allows. When ``EV_AGENT_ALLOWED_ROOTS`` is set, the run
    is confined, which is what a test sandbox and a careful "only touch
    these folders" request both need.
    """
    if not _allowed_roots_config():
        return False
    try:
        resolved = Path(target).resolve()
    except OSError:
        return False
    return all(not _inside(root, resolved) for root in _allowed_roots(ctx))


def _path_receipt(target: Path, ctx: ToolContext) -> dict[str, Any] | None:
    """A refusal receipt when a path is outside the run's allowed roots."""
    if not _outside_allowed_roots(target, ctx):
        return None
    return _receipt(
        ok=False,
        error=(
            f"path is outside this run's allowed roots: {target}. "
            "Ask the owner, or run the task with the right roots."
        ),
        verified=True,
        evidence={"path": str(target)},
    )


def _secret_reason(target: Path) -> str | None:
    from app.fabric.files import _secret_reason as fabric_secret_reason

    try:
        return fabric_secret_reason(target)
    except Exception:  # noqa: BLE001 - a broken guard must not open the gate
        return "path guard unavailable"


def _guard_read(target: Path, root: Path) -> dict[str, Any] | None:
    """None when a read may proceed; a denial/park receipt otherwise."""
    secret = _secret_reason(target)
    if secret is not None:
        return _receipt(ok=False, error=secret, verified=True)
    if not _inside(root, target):
        return _receipt(
            ok=False,
            error="outside workspace root needs confirm",
            verified=True,
            needs_confirm=True,
        )
    return None


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #


#: Guidance appended to shell denials so the brain adapts instead of
#: retrying near-duplicates. Policy itself is unchanged: the lane already
#: decided DENY; these words only name the rule class that fired (detected
#: with the lane's own helpers, never a reimplementation).
_DENY_PRIVILEGE = (
    "denied by policy: privilege escalation (sudo/su/doas) is never allowed. "
    "Rerun without it; paths you cannot read stay unread."
)
_DENY_SECRET = (
    "denied by policy: the command names a secret path (everything under "
    "~/Library except iCloud Drive, plus ~/.ssh, ~/.aws, ~/.gnupg, *.pem, "
    ".env, credentials). Measure from a parent directory instead "
    "(for example du -sh ~/*), or name safe subpaths."
)
_DENY_DESTRUCTIVE = (
    "denied by policy: destructive or download-pipe pattern "
    "(rm -rf /, curl|sh, decode-pipes). Restate the goal without it."
)


def _deny_hint(cmd: str, cwd: str | None) -> str:
    """Name the fired rule class for a lane-decided shell DENY."""
    from app.fabric.shell_sessions import shell_secret_hit

    low = (cmd or "").lower()
    if re.search(r"\bsudo\b|\bsu\b|\bdoas\b", low):
        return _DENY_PRIVILEGE
    try:
        if shell_secret_hit(cmd, cwd):
            return _DENY_SECRET
    except Exception:  # noqa: BLE001 - guidance must never break the denial
        logger.debug("deny hint check failed", exc_info=True)
    return _DENY_DESTRUCTIVE


async def _run_shell(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.fabric.shell_sessions import ShellSessions, classify_command

    cmd = str(args.get("cmd") or "").strip()
    if not cmd:
        return _receipt(ok=False, error="empty command")
    # An agent that runs top/vim/less/tail -f hangs until the tool timeout and
    # then retries. Refuse the interactive forms up front and say why.
    if re.match(
        r"^\s*(?:sudo\s+)?(?:top|htop|watch|less|more|vim|nano|emacs|tail\s+-f)\b",
        cmd,
        re.IGNORECASE,
    ):
        return _receipt(
            ok=False,
            error=(
                "interactive or unbounded command refused; use a non-interactive "
                "form (for example tail -n 50, ps aux | head, or a bounded timeout)"
            ),
            verified=True,
        )
    sleep_match = re.match(r"^\s*sleep\s+(\d+)", cmd, re.IGNORECASE)
    if sleep_match and int(sleep_match.group(1)) > 60:
        return _receipt(
            ok=False,
            error="sleep longer than 60s refused; use a bounded wait in the tool call",
            verified=True,
        )
    cls = classify_command(cmd)
    if cls == "DENY":
        return _receipt(
            ok=False, error=_deny_hint(cmd, None), verified=True,
            evidence={"cmd": cmd, "class": cls},
        )
    if cls == "CONFIRM" and not ctx.approved:
        return _receipt(
            ok=False, error="confirm required", verified=True,
            evidence={"cmd": cmd, "class": cls, "needs_confirm": True},
            needs_confirm=True,
        )
    cwd = args.get("cwd")
    cwd_path = _resolve(str(cwd), ctx.root) if cwd else ctx.root
    refusal = _path_receipt(cwd_path, ctx)
    if refusal is not None:
        return refusal
    if not cwd_path.is_dir():
        return _receipt(ok=False, error=f"cwd is not a directory: {cwd_path}")
    if not _inside(ctx.root, cwd_path) and not ctx.approved:
        return _receipt(
            ok=False, error="cwd outside workspace root needs confirm", verified=True,
            evidence={"cwd": str(cwd_path), "needs_confirm": True}, needs_confirm=True,
        )
    timeout = float(args.get("timeout_s") or settings.agent_shell_timeout_s)
    shells = ShellSessions(root=ctx.root)
    started = time.perf_counter()
    receipt = await asyncio.to_thread(
        shells.run, cmd, min(max(timeout, 1.0), 300.0), ctx.approved, "agent", str(cwd_path)
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    evidence = dict(receipt.get("evidence") or {})
    evidence["elapsed_ms"] = elapsed_ms
    err = receipt.get("error")
    if not receipt.get("ok") and str(err or "").strip().lower() == "denied by policy":
        # Lane-level DENY (secret path, rm target, provenance gate): the
        # verdict stands, but the brain gets the rule class it tripped.
        err = _deny_hint(cmd, str(cwd_path))
    return _receipt(
        ok=bool(receipt.get("ok")),
        error=err,
        verified=bool(receipt.get("verified", True)),
        result={
            "exit_code": evidence.get("exit_code"),
            "stdout": evidence.get("out", ""),
            "class": evidence.get("class", cls),
        },
        evidence=evidence,
        needs_confirm=bool(receipt.get("evidence", {}).get("needs_confirm")),
    )


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def _approval_guard(tool: str, ctx: ToolContext) -> dict[str, Any] | None:
    """Mutating tools refuse at the tool boundary without explicit approval.

    The loop already parks these for the owner; this is defense in depth for
    any direct registry caller (tests, future surfaces).
    """
    if ctx.approved:
        return None
    return _receipt(
        ok=False, error="confirm required", verified=True,
        evidence={"tool": tool, "needs_confirm": True}, needs_confirm=True,
    )


def _read_file_sync(path: str, root: Path, max_bytes: int, approved: bool) -> dict[str, Any]:
    from app.fabric.files import read_file

    receipt = read_file(path, root=root, confirm=approved)
    if not receipt.get("ok"):
        return _receipt(
            ok=False,
            error=receipt.get("error"),
            verified=True,
            needs_confirm=bool(receipt.get("confirm")),
        )
    body = str((receipt.get("evidence") or {}).get("body") or "")
    truncated = len(body.encode("utf-8", "ignore")) > max_bytes
    if truncated:
        body = body.encode("utf-8", "ignore")[:max_bytes].decode("utf-8", "replace")
    return _receipt(
        ok=True, verified=True,
        result={"path": path, "content": body, "truncated": truncated},
        evidence={"path": str(_resolve(path, root)), "bytes": len(body)},
    )


async def _read_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = str(args.get("path") or "")
    if not path:
        return _receipt(ok=False, error="path required")
    target = _resolve(path, ctx.root)
    refusal = _path_receipt(target, ctx)
    if refusal is not None:
        return refusal
    guard = _guard_read(target, ctx.root)
    if guard is not None and not (ctx.approved and guard.get("needs_confirm")):
        return guard
    max_bytes = int(args.get("max_bytes") or 200_000)
    return await asyncio.to_thread(_read_file_sync, path, ctx.root, max_bytes, ctx.approved)


def _list_dir_sync(path: str, root: Path, approved: bool) -> dict[str, Any]:
    target = _resolve(path or ".", root) if path else root
    guard = _guard_read(target, root)
    if guard is not None and not (approved and guard.get("needs_confirm")):
        return guard
    if not target.is_dir():
        return _receipt(ok=False, error="not a directory")
    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if len(entries) >= MAX_LIST_ENTRIES:
                    break
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
                except OSError:
                    is_dir, size = False, 0
                entries.append({"name": entry.name, "dir": is_dir, "bytes": size})
    except OSError as exc:
        return _receipt(ok=False, error=str(exc))
    entries.sort(key=lambda item: (not item["dir"], item["name"].lower()))
    return _receipt(
        ok=True, verified=True,
        result={"path": str(target), "entries": entries, "count": len(entries)},
        evidence={"path": str(target)},
    )


async def _list_dir(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await asyncio.to_thread(
        _list_dir_sync, str(args.get("path") or ""), ctx.root, ctx.approved
    )


def _write_file_sync(path: str, content: str, root: Path, approved: bool) -> dict[str, Any]:
    from app.fabric.files import create

    receipt = create(path, content, root=root, confirm=approved)
    evidence = dict(receipt.get("evidence") or {})
    return _receipt(
        ok=bool(receipt.get("ok")),
        error=receipt.get("error"),
        verified=bool(receipt.get("verified")),
        result={"path": evidence.get("path", path), "sha256": evidence.get("sha256"),
                "size": evidence.get("size")},
        evidence=evidence,
        undo=receipt.get("undo"),
        needs_confirm=bool(receipt.get("confirm")),
    )


async def _write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    parked = _approval_guard("write_file", ctx)
    if parked is not None:
        return parked
    path = str(args.get("path") or "")
    if not path:
        return _receipt(ok=False, error="path required")
    content = args.get("content")
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    if len(content.encode("utf-8", "ignore")) > 1_048_576:
        return _receipt(ok=False, error="content larger than 1 MiB")
    target = _resolve(path, ctx.root)
    refusal = _path_receipt(target, ctx)
    if refusal is not None:
        return refusal
    secret = _secret_reason(target)
    if secret is not None:
        return _receipt(ok=False, error=secret, verified=True)
    return await asyncio.to_thread(_write_file_sync, path, content, ctx.root, ctx.approved)


def _delete_sync(path: str, root: Path, approved: bool) -> dict[str, Any]:
    from app.fabric.files import delete

    receipt = delete(path, root=root, confirm=approved)
    return _receipt(
        ok=bool(receipt.get("ok")),
        error=receipt.get("error"),
        verified=bool(receipt.get("verified")),
        result={"trashed": (receipt.get("evidence") or {}).get("trashed", path)},
        evidence=dict(receipt.get("evidence") or {}),
        undo=receipt.get("undo"),
        needs_confirm=bool(receipt.get("confirm")),
    )


async def _delete_path(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    parked = _approval_guard("delete_path", ctx)
    if parked is not None:
        return parked
    path = str(args.get("path") or "")
    if not path:
        return _receipt(ok=False, error="path required")
    target = _resolve(path, ctx.root)
    refusal = _path_receipt(target, ctx)
    if refusal is not None:
        return refusal
    secret = _secret_reason(target)
    if secret is not None:
        return _receipt(ok=False, error=secret, verified=True)
    return await asyncio.to_thread(_delete_sync, path, ctx.root, ctx.approved)


_SEARCH_NOISE = frozenset(
    {"node_modules", "__pycache__", ".venv", "venv", ".build", "deriveddata",
     ".mypy_cache", ".ruff_cache", ".git"}
)


def _rg_search(query: str, target: Path, glob: str | None, max_results: int) -> dict[str, Any]:
    argv = ["rg", "--line-number", "--no-heading", "--color", "never",
            "--max-columns", "300", "-m", "3"]
    if glob:
        argv += ["--glob", glob]
    argv += ["-e", query, str(target)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if proc.returncode not in (0, 1):
        return _receipt(ok=False, error=(proc.stderr or "ripgrep failed")[:400])
    matches: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        if len(matches) >= max_results:
            break
        parts = line.split(":", 2)
        if len(parts) == 3:
            matches.append({"path": parts[0], "line": parts[1], "text": parts[2][:300]})
        else:
            matches.append({"path": line[:300]})
    return _receipt(ok=True, verified=True, result={"matches": matches, "count": len(matches)})


def _python_search(query: str, target: Path, glob: str | None, max_results: int) -> dict[str, Any]:
    import fnmatch

    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches: list[dict[str, Any]] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SEARCH_NOISE]
        for name in filenames:
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            path = Path(dirpath) / name
            scanned += 1
            if scanned > MAX_SEARCH_FILES or len(matches) >= max_results:
                return _receipt(
                    ok=True, verified=True,
                    result={"matches": matches, "count": len(matches), "truncated": True},
                )
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    matches.append({"path": str(path), "line": str(number), "text": line[:300]})
                    break
    return _receipt(ok=True, verified=True, result={"matches": matches, "count": len(matches)})


def _search_sync(
    query: str, path: str, glob: str | None, root: Path, max_results: int, approved: bool
) -> dict[str, Any]:
    target = _resolve(path or ".", root)
    guard = _guard_read(target, root)
    if guard is not None and not (approved and guard.get("needs_confirm")):
        return guard
    if not target.exists():
        return _receipt(ok=False, error="path not found")
    if shutil.which("rg"):
        return _rg_search(query, target, glob, max_results)
    return _python_search(query, target, glob, max_results)


async def _search_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "")
    if not query:
        return _receipt(ok=False, error="query required")
    max_results = min(int(args.get("max_results") or 50), MAX_SEARCH_RESULTS)
    return await asyncio.to_thread(
        _search_sync, query, str(args.get("path") or "."), args.get("glob"),
        ctx.root, max_results, ctx.approved,
    )


def _write_text_verified(path: str, content: str, root: Path, approved: bool) -> dict[str, Any]:
    from app.fabric.files import create

    receipt = create(path, content, root=root, confirm=approved)
    evidence = dict(receipt.get("evidence") or {})
    return _receipt(
        ok=bool(receipt.get("ok")),
        error=receipt.get("error"),
        verified=bool(receipt.get("verified")),
        result={"path": evidence.get("path", path), "sha256": evidence.get("sha256"),
                "size": evidence.get("size")},
        evidence=evidence,
        undo=receipt.get("undo"),
        needs_confirm=bool(receipt.get("confirm")),
    )


def _append_sync(path: str, content: str, root: Path, approved: bool) -> dict[str, Any]:
    target = _resolve(path, root)
    secret = _secret_reason(target)
    if secret is not None:
        return _receipt(ok=False, error=secret, verified=True)
    try:
        previous = target.read_bytes() if target.exists() else b""
    except OSError as exc:
        return _receipt(ok=False, error=str(exc))
    if len(previous) + len(content.encode("utf-8", "ignore")) > 1_048_576:
        return _receipt(ok=False, error="resulting file would exceed 1 MiB")
    body = (previous + content.encode("utf-8")).decode("utf-8", "replace")
    return _write_text_verified(path, body, root, approved)


async def _append_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    parked = _approval_guard("append_file", ctx)
    if parked is not None:
        return parked
    path = str(args.get("path") or "")
    if not path:
        return _receipt(ok=False, error="path required")
    content = args.get("content")
    content = "" if content is None else str(content)
    return await asyncio.to_thread(_append_sync, path, content, ctx.root, ctx.approved)


def _edit_sync(
    path: str, find: str, replace: str, count: int, root: Path, approved: bool
) -> dict[str, Any]:
    target = _resolve(path, root)
    secret = _secret_reason(target)
    if secret is not None:
        return _receipt(ok=False, error=secret, verified=True)
    try:
        body = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _receipt(ok=False, error="not found")
    except OSError as exc:
        return _receipt(ok=False, error=str(exc))
    occurrences = body.count(find)
    if occurrences == 0:
        return _receipt(ok=False, error="find text is not present; nothing changed")
    replacements = min(occurrences, max(1, count))
    new_body = body.replace(find, replace, replacements)
    written = _write_text_verified(path, new_body, root, approved)
    if written.get("ok"):
        written["result"]["replacements"] = replacements
    return written


async def _edit_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    parked = _approval_guard("edit_file", ctx)
    if parked is not None:
        return parked
    path = str(args.get("path") or "")
    # Accept the common alias pair too: a model that has seen "replace this
    # exact string" elsewhere must not fail on a naming difference.
    find = str(args.get("find") or args.get("old_string") or "")
    if not path or not find:
        return _receipt(ok=False, error="path and find are required")
    raw_replace = args.get("replace")
    if raw_replace is None:
        raw_replace = args.get("new_string")
    replace = "" if raw_replace is None else str(raw_replace)
    count = int(args.get("count") or 1)
    if bool(args.get("replace_all")):
        count = 1_000_000
    return await asyncio.to_thread(
        _edit_sync, path, find, replace, count, ctx.root, ctx.approved
    )


def _transfer_sync(src: str, dst: str, root: Path, approved: bool, *, copy: bool) -> dict[str, Any]:
    import hashlib
    import shutil

    source = _resolve(src, root)
    target = _resolve(dst, root)
    for path in (source, target):
        secret = _secret_reason(path)
        if secret is not None:
            return _receipt(ok=False, error=secret, verified=True)
    if not source.exists():
        return _receipt(ok=False, error=f"source missing: {src}")
    if target.exists():
        return _receipt(ok=False, error=f"destination already exists: {dst}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if copy:
            shutil.copy2(source, target)
        else:
            shutil.move(str(source), str(target))
        digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else ""
        source_ok = (not source.exists()) if not copy else True
        ok = target.exists() and source_ok
        return _receipt(
            ok=ok, verified=ok,
            error=None if ok else "transfer did not verify",
            result={"source": str(source), "destination": str(target),
                    "sha256": digest, "copied": copy},
            evidence={"destination": str(target), "sha256": digest},
        )
    except OSError as exc:
        return _receipt(ok=False, error=str(exc))


async def _move_path(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    parked = _approval_guard("move_path", ctx)
    if parked is not None:
        return parked
    src, dst = str(args.get("source") or ""), str(args.get("destination") or "")
    if not src or not dst:
        return _receipt(ok=False, error="source and destination are required")
    return await asyncio.to_thread(_transfer_sync, src, dst, ctx.root, ctx.approved, copy=False)


async def _copy_path(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    parked = _approval_guard("copy_path", ctx)
    if parked is not None:
        return parked
    src, dst = str(args.get("source") or ""), str(args.get("destination") or "")
    if not src or not dst:
        return _receipt(ok=False, error="source and destination are required")
    return await asyncio.to_thread(_transfer_sync, src, dst, ctx.root, ctx.approved, copy=True)


def _sql_sync(sql: str, db_path: str, root: Path, approved: bool) -> dict[str, Any]:
    from app.fabric.sql_lane import classify, execute

    target = str(_resolve(db_path, root)) if db_path else ""
    if target:
        secret = _secret_reason(Path(target))
        if secret is not None:
            return _receipt(ok=False, error=secret, verified=True)
    receipt = execute(sql, db_path=target or None, root=root, confirm=approved)
    evidence = dict(receipt.get("evidence") or {})
    kind = str(evidence.get("kind") or classify(sql))
    return _receipt(
        ok=bool(receipt.get("ok")),
        error=receipt.get("error"),
        verified=bool(receipt.get("verified")),
        result={
            "kind": kind,
            "rows": evidence.get("rows", []),
            "affected": evidence.get("affected"),
            "mode": evidence.get("mode") or evidence.get("preview"),
        },
        evidence=evidence,
        needs_confirm=bool(receipt.get("confirm")),
    )


async def _sql_query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.fabric.sql_lane import classify

    sql = str(args.get("sql") or "").strip()
    if not sql:
        return _receipt(ok=False, error="sql required")
    db_path = str(args.get("db_path") or "").strip()
    if classify(sql) != "read":
        parked = _approval_guard("sql_query", ctx)
        if parked is not None:
            return parked
    return await asyncio.to_thread(_sql_sync, sql, db_path, ctx.root, ctx.approved)


# --------------------------------------------------------------------------- #
# Web
# --------------------------------------------------------------------------- #


def _host_blocked(host: str) -> bool:
    """True when the host resolves to a loopback/private/link-local address."""
    if getattr(settings, "agent_allow_local_fetch", False):
        return False
    if not host:
        return True
    lowered = host.lower().strip("[]")
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".local"):
        return True
    try:
        infos = socket.getaddrinfo(lowered, None)
    except OSError:
        return True  # unresolvable: refuse rather than guess
    for info in infos:
        raw = info[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return True
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return True
    return False


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)


def _html_to_text(body: str) -> str:
    body = _SCRIPT_RE.sub(" ", body)
    body = _TAG_RE.sub(" ", body)
    body = html.unescape(body)
    return re.sub(r"[ \t\r\f\v]+", " ", body).strip()


async def _fetch_url(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    import httpx

    url = str(args.get("url") or "").strip()
    if not url:
        return _receipt(ok=False, error="url required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return _receipt(ok=False, error="only http(s) URLs are fetchable")
    if _host_blocked(parsed.hostname or ""):
        return _receipt(
            ok=False,
            error="refusing to fetch a loopback/private/reserved host",
            verified=True,
        )
    limit = int(getattr(settings, "agent_fetch_max_bytes", 2_000_000))
    current = url
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS):
            try:
                async with client.stream(
                    "GET", current, headers={"User-Agent": "Evie-Agent/1.0"}
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            return _receipt(ok=False, error="redirect without location")
                        nxt = httpx.URL(current).join(location)
                        if nxt.scheme not in {"http", "https"} or _host_blocked(nxt.host or ""):
                            return _receipt(ok=False, error="redirect target refused")
                        current = str(nxt)
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        room = limit - total
                        if len(chunk) > room:
                            chunks.append(chunk[:room])
                            total = limit
                            truncated = True
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    raw = b"".join(chunks)
                    body = raw.decode(response.encoding or "utf-8", "replace")
                    text = _html_to_text(body) if "<" in body[:2000] else body
                    return _receipt(
                        ok=True, verified=True,
                        result={
                            "url": str(response.url),
                            "status": response.status_code,
                            "text": text[: int(getattr(settings, "agent_max_output_chars", 8000))],
                            "truncated": truncated,
                        },
                        evidence={"url": str(response.url), "status": response.status_code},
                    )
            except httpx.HTTPError as exc:
                return _receipt(ok=False, error=f"fetch failed: {exc}")
    return _receipt(ok=False, error="too many redirects")


async def _search_web(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    del ctx
    query = str(args.get("query") or "").strip()
    if not query:
        return _receipt(ok=False, error="query required")
    from app.search.providers import get_search_provider

    provider = get_search_provider()
    if provider is None:
        return _receipt(
            ok=False,
            error="search provider is not configured (EV_SEARCH_PROVIDER / EV_BRAVE_SEARCH_API_KEY)",
            verified=True,
        )
    limit = min(int(args.get("limit") or 5), 10)
    try:
        results = await provider.search(query, limit=limit)
    except Exception as exc:  # noqa: BLE001 - provider failure is a tool failure
        return _receipt(ok=False, error=f"search failed: {exc}")
    return _receipt(
        ok=True, verified=True,
        result={
            "results": [
                {"title": item.title, "url": item.url, "snippet": item.snippet}
                for item in results
            ],
            "count": len(results),
        },
    )


# --------------------------------------------------------------------------- #
# Apps
# --------------------------------------------------------------------------- #


def _open_sync(argv: list[str]) -> dict[str, Any]:
    if sys.platform != "darwin":
        return _receipt(ok=False, error="opening apps is only wired for macOS here")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return _receipt(ok=False, error="open timed out")
    except OSError as exc:
        return _receipt(ok=False, error=str(exc))
    if proc.returncode != 0:
        return _receipt(ok=False, error=(proc.stderr or "open failed")[:300])
    return _receipt(ok=True, verified=True, result={"opened": argv[-1]})


async def _open_app(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    parked = _approval_guard("open_app", ctx)
    if parked is not None:
        return parked
    name = str(args.get("name") or "").strip()
    if not name:
        return _receipt(ok=False, error="name required")
    return await asyncio.to_thread(_open_sync, ["open", "-a", name[:120]])


async def _open_url(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    parked = _approval_guard("open_url", ctx)
    if parked is not None:
        return parked
    url = str(args.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return _receipt(ok=False, error="only http(s) URLs can be opened")
    return await asyncio.to_thread(_open_sync, ["open", url[:2000]])


# --------------------------------------------------------------------------- #
# Computer use (bounded AX operator: observe -> act -> verify)
# --------------------------------------------------------------------------- #


def _observe_sync(app: str, limit: int) -> dict[str, Any]:
    from app.fabric.operator import observe

    return observe(app, limit=limit)


async def _observe_app(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    del ctx
    app = str(args.get("app") or "").strip()
    if not app:
        return _receipt(ok=False, error="app required")
    limit = min(int(args.get("limit") or 200), 400)
    res = await asyncio.to_thread(_observe_sync, app, limit)
    if not res.get("ok"):
        return _receipt(
            ok=False, verified=bool(res.get("verified", True)),
            error=str(res.get("error") or "observe failed"),
        )
    evidence = dict(res.get("evidence") or {})
    elements = list(evidence.get("elements") or [])
    compact = [
        {
            "ref": element.get("i"),
            "role": str(element.get("role") or "")[:40],
            "name": str(element.get("name") or "")[:80],
            "value": str(element.get("value") or "")[:120],
        }
        for element in elements[:limit]
    ]
    return _receipt(
        ok=True, verified=True,
        result={
            "app": app,
            "frame_id": evidence.get("frame_id"),
            "elements": compact,
            "count": len(compact),
            "total": evidence.get("n", len(compact)),
        },
        evidence={"frame_id": evidence.get("frame_id"), "app": app},
    )


def _act_sync(app: str, frame_id: str, action: dict[str, Any], approved: bool) -> dict[str, Any]:
    from app.fabric.operator import act

    return act(app, frame_id, action, approved=approved)


async def _act_in_app(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    parked = _approval_guard("act_in_app", ctx)
    if parked is not None:
        return parked
    app = str(args.get("app") or "").strip()
    frame_id = str(args.get("frame_id") or "").strip()
    action = args.get("action")
    if not app:
        return _receipt(ok=False, error="app required")
    if not isinstance(action, dict) or not action.get("kind"):
        return _receipt(ok=False, error="action object with a kind is required")
    res = await asyncio.to_thread(_act_sync, app, frame_id, action, ctx.approved)
    return _receipt(
        ok=bool(res.get("ok")),
        verified=bool(res.get("verified", True)),
        error=res.get("error"),
        result={"kind": action.get("kind"), "evidence": res.get("evidence")},
        evidence=dict(res.get("evidence") or {}),
    )


def _verify_sync(app: str, expectation: dict[str, Any]) -> dict[str, Any]:
    from app.fabric.operator import verify

    return verify(app, expectation)


async def _verify_app(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    del ctx
    app = str(args.get("app") or "").strip()
    if not app:
        return _receipt(ok=False, error="app required")
    expectation: dict[str, Any] = {}
    if str(args.get("contains_text") or "").strip():
        expectation["contains_text"] = str(args["contains_text"])[:500]
    elif str(args.get("absent_text") or "").strip():
        expectation["absent_text"] = str(args["absent_text"])[:500]
    else:
        return _receipt(ok=False, error="contains_text or absent_text is required")
    res = await asyncio.to_thread(_verify_sync, app, expectation)
    return _receipt(
        ok=bool(res.get("ok")),
        verified=bool(res.get("verified")),
        error=res.get("error"),
        result={"expectation": expectation, "evidence": res.get("evidence")},
        evidence=dict(res.get("evidence") or {}),
    )


# --------------------------------------------------------------------------- #
# Vision + terminal control (Evie.app bridge = the TCC identity)
# --------------------------------------------------------------------------- #

_SHOT_DIR = Path.home() / ".ev" / "agent" / "screenshots"
_KEY_CODES = {
    "return": 36,
    "enter": 76,
    "tab": 48,
    "escape": 53,
    "esc": 53,
    "space": 49,
    "delete": 51,
    "backspace": 51,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
}


def _bridge_call(op: str, args: dict[str, Any], *, timeout: float = 45.0) -> dict[str, Any]:
    from app.fabric.bridge_client import BridgeClient

    return BridgeClient(timeout=timeout).call(op, args, timeout=timeout)


def _bridge_available() -> bool:
    from app.fabric.bridge_client import bridge_available

    return bridge_available()


def _jxa(script: str, *, timeout: float = 45.0) -> dict[str, Any]:
    return _bridge_call("jxa", {"script": script}, timeout=timeout)


async def _image_receipt(path: Path, *, source: str) -> dict[str, Any]:
    """OCR one image with the configured local provider, honestly on failure."""
    try:
        data = await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        return _receipt(ok=False, error=f"unreadable image: {exc}")
    if len(data) > 25 * 1024 * 1024:
        return _receipt(ok=False, error="image larger than 25 MiB")
    from app.vision.providers import get_vision_provider

    provider = get_vision_provider()
    try:
        result = await provider.analyze(
            data=data,
            content_type="image/png" if path.suffix.lower() == ".png" else None,
            filename=path.name,
        )
    except Exception as exc:  # noqa: BLE001 - provider failure is a tool failure
        return _receipt(ok=False, error=f"OCR failed: {type(exc).__name__}: {exc}")
    text = (result.ocr_text or "").strip()
    if result.degraded and not text:
        return _receipt(
            ok=False,
            error="OCR is degraded: no vision engine is available",
            verified=True,
            evidence={"provider": result.provider, "degraded": True},
        )
    return _receipt(
        ok=True,
        verified=not result.degraded,
        result={
            "text": text[: int(getattr(settings, "agent_max_output_chars", 8000))],
            "provider": result.provider,
            "degraded": bool(result.degraded),
            "lines": len(result.lines or []),
            "path": str(path),
            "source": source,
        },
        evidence={"provider": result.provider, "degraded": bool(result.degraded)},
    )


async def _read_image(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = str(args.get("path") or "").strip()
    if not path:
        return _receipt(ok=False, error="path required")
    target = _resolve(path, ctx.root)
    refusal = _path_receipt(target, ctx)
    if refusal is not None:
        return refusal
    guard = _guard_read(target, ctx.root)
    if guard is not None and not (ctx.approved and guard.get("needs_confirm")):
        return guard
    if not target.is_file():
        return _receipt(ok=False, error="not a file")
    return await _image_receipt(target, source="file")


async def _capture_screen(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Screenshot through Evie.app (TCC), fall back to screencapture, then OCR."""
    del ctx
    if _bridge_available():
        shot = await asyncio.to_thread(_bridge_call, "screenshot", {})
        if not shot.get("ok"):
            return _receipt(
                ok=False,
                error=str(shot.get("error") or "screenshot failed"),
                evidence={"bridge": True},
            )
        result = shot.get("result") or shot.get("evidence") or {}
        path = Path(str(result.get("path") or ""))
        if not path.is_file():
            return _receipt(ok=False, error="screenshot path missing")
        return await _image_receipt(path, source="screen")
    _SHOT_DIR.mkdir(parents=True, exist_ok=True)
    target = _SHOT_DIR / f"shot-{int(time.time() * 1000)}.png"
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["/usr/sbin/screencapture", "-x", str(target)],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _receipt(ok=False, error=f"screencapture failed: {exc}")
    if proc.returncode != 0 or not target.is_file():
        return _receipt(
            ok=False,
            error=(proc.stderr or "screencapture failed").strip()[:300],
            evidence={"fix": "Launch Evie.app so it can capture with its TCC grant."},
        )
    return await _image_receipt(target, source="screen")


def _key_script(app: str, key: str, modifiers: list[str]) -> str:
    code = _KEY_CODES.get(key.lower())
    mod_map = {"command": "command down", "shift": "shift down",
               "option": "option down", "control": "control down"}
    mods = [mod_map[m] for m in modifiers if m in mod_map]
    using = "{using: [" + ", ".join(mods) + "]}" if mods else ""
    lines = [
        f'const app = Application({json.dumps(app)});',
        'app.activate();',
        'delay(0.25);',
        'const se = Application("System Events");',
    ]
    if code is not None and not mods:
        lines.append(f"se.keyCode({code});")
    elif code is not None:
        lines.append(f"se.keyCode({code}, {using});")
    else:
        lines.append(f"se.keystroke({json.dumps(key)}{', ' + using if using else ''});")
    lines.append('"pressed";')
    return "\n".join(lines)


def _type_and_return_script(app: str, command: str, wait_s: float) -> str:
    return "\n".join([
        f'const app = Application({json.dumps(app)});',
        'app.activate();',
        'delay(0.45);',
        'const se = Application("System Events");',
        f'se.keystroke({json.dumps(command)});',
        'delay(0.15);',
        'se.keyCode(36);',
        f'delay({max(0.0, min(float(wait_s), 30.0))});',
        '"typed";',
    ])


async def _press_key(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    parked = _approval_guard("press_key", ctx)
    if parked is not None:
        return parked
    app = str(args.get("app") or "").strip()
    key = str(args.get("key") or "").strip()
    if not app or not key:
        return _receipt(ok=False, error="app and key are required")
    if not _bridge_available():
        return _receipt(
            ok=False,
            error="bridge unavailable",
            evidence={"fix": "Launch Evie.app, then retry."},
        )
    modifiers = [str(item) for item in (args.get("modifiers") or [])]
    script = _key_script(app, key, modifiers)
    res = await asyncio.to_thread(_jxa, script, timeout=45)
    if not res.get("ok"):
        return _receipt(ok=False, error=str(res.get("error") or "key press failed"))
    return _receipt(ok=True, verified=True, result={"app": app, "key": key},
                    evidence={"bridge": True})


async def _run_in_terminal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Launch/activate a terminal app, type one command, press Return, verify."""
    parked = _approval_guard("run_in_terminal", ctx)
    if parked is not None:
        return parked
    app = str(args.get("app") or "Ghostty").strip() or "Ghostty"
    command = str(args.get("command") or "").strip()
    if not command:
        return _receipt(ok=False, error="command required")
    from app.fabric.shell_sessions import classify_command

    cls = classify_command(command)
    if cls == "DENY":
        return _receipt(ok=False, error="denied by policy", verified=True,
                        evidence={"command": command, "class": cls})
    if not _bridge_available():
        return _receipt(
            ok=False,
            error="bridge unavailable: Evie.app must be running to type into an app",
            evidence={"fix": "Launch Evie.app, then retry."},
        )
    opened = await asyncio.to_thread(_open_sync, ["open", "-a", app[:120]])
    if not opened.get("ok"):
        return _receipt(ok=False, error=str(opened.get("error") or f"could not open {app}"))
    await asyncio.sleep(0.8)
    wait_s = float(args.get("wait_s") or 1.5)
    res = await asyncio.to_thread(
        _jxa, _type_and_return_script(app, command, wait_s), timeout=60
    )
    if not res.get("ok"):
        return _receipt(
            ok=False,
            error=str(res.get("error") or "typing failed"),
            evidence={"app": app, "command": command, "class": cls},
        )
    evidence: dict[str, Any] = {
        "app": app, "command": command, "class": cls,
        "verified": False, "bridge": True,
    }
    expect = str(args.get("expect_text") or "").strip()
    if expect:
        shot = await _capture_screen({}, ctx)
        text = str((shot.get("result") or {}).get("text") or "")
        found = expect.lower() in text.lower()
        evidence["verified"] = bool(shot.get("ok")) and found
        evidence["expect_text"] = expect
        if not found:
            return _receipt(
                ok=False,
                error=f"typed the command but {expect!r} is not on screen",
                verified=bool(shot.get("ok")),
                result={"screen_text": text[:3000]},
                evidence=evidence,
            )
    return _receipt(
        ok=True,
        verified=bool(evidence["verified"]),
        result={
            "app": app,
            "command": command,
            "typed": True,
            "pressed_return": True,
            "verified": bool(evidence["verified"]),
        },
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def _spec(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    read_only: bool,
    permission: str,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        },
        read_only=read_only,
        permission=permission,
        risk_class="R0" if read_only else "R2",
    )


_STR = {"type": "string"}

REGISTRY: dict[str, AgentTool] = {}


def _register(tool: AgentTool) -> None:
    REGISTRY[tool.spec.name] = tool


_register(AgentTool(
    _spec(
        "run_shell",
        "Run one shell command in the workspace root. Read-only commands run "
        "immediately; writes/builds are journaled; anything destructive parks "
        "for owner approval and hard-denied patterns (sudo, secret paths, "
        "rm -rf /) never run. Returns exit_code and captured stdout.",
        {"cmd": {**_STR, "description": "The command line to run."},
         "cwd": {**_STR, "description": "Optional working directory."},
         "timeout_s": {"type": "integer", "minimum": 1, "maximum": 300}},
        ["cmd"], read_only=False, permission="shell:run",
    ),
    _run_shell,
))

_register(AgentTool(
    _spec(
        "read_file",
        "Read a UTF-8 text file inside the workspace root. Secret paths "
        "(.ssh, .env, keys) are denied by policy.",
        {"path": _STR, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 2000000}},
        ["path"], read_only=True, permission="files:read",
    ),
    _read_file,
))

_register(AgentTool(
    _spec(
        "list_dir",
        "List the entries of a directory inside the workspace root.",
        {"path": {**_STR, "description": "Directory path; defaults to the root."}},
        [], read_only=True, permission="files:read",
    ),
    _list_dir,
))

_register(AgentTool(
    _spec(
        "write_file",
        "Create or overwrite a file inside the workspace root. The write is "
        "verified by reading it back (sha256); deleting is a separate tool.",
        {"path": _STR, "content": _STR},
        ["path", "content"], read_only=False, permission="files:write",
    ),
    _write_file,
))

_register(AgentTool(
    _spec(
        "delete_path",
        "Move a file or directory to the workspace trash (undoable). Never "
        "deletes secret paths.",
        {"path": _STR},
        ["path"], read_only=False, permission="files:delete",
    ),
    _delete_path,
))

_register(AgentTool(
    _spec(
        "append_file",
        "Append text to a file (creating it if missing) inside the workspace. "
        "The resulting file is read back and hashed; 1 MiB cap.",
        {"path": _STR, "content": _STR},
        ["path", "content"], read_only=False, permission="files:write",
    ),
    _append_file,
))

_register(AgentTool(
    _spec(
        "edit_file",
        "Find and replace text inside a UTF-8 file (count replacements, default 1). "
        "Fails without changing anything when the find text is absent.",
        {"path": _STR, "find": _STR, "replace": _STR,
         "count": {"type": "integer", "minimum": 1, "maximum": 1000}},
        ["path", "find"], read_only=False, permission="files:write",
    ),
    _edit_file,
))

_register(AgentTool(
    _spec(
        "move_path",
        "Move or rename a file or directory. Refuses to overwrite an existing "
        "destination and never touches secret paths.",
        {"source": _STR, "destination": _STR},
        ["source", "destination"], read_only=False, permission="files:write",
    ),
    _move_path,
))

_register(AgentTool(
    _spec(
        "copy_path",
        "Copy a file or directory to a new path. Refuses to overwrite, returns "
        "the destination sha256.",
        {"source": _STR, "destination": _STR},
        ["source", "destination"], read_only=False, permission="files:write",
    ),
    _copy_path,
))

_register(AgentTool(
    _spec(
        "sql_query",
        "Run SQL against a local SQLite database (default: <root>/.ev-scratch.db). "
        "SELECT runs directly; DML/DDL (INSERT/UPDATE/DELETE/ALTER/CREATE) needs "
        "approval and is executed transactionally. Dangerous constructs are DENY.",
        {"sql": _STR,
         "db_path": {**_STR, "description": "SQLite file; relative to the workspace root."}},
        ["sql"], read_only=False, permission="sql:write",
    ),
    _sql_query,
))

_register(AgentTool(
    _spec(
        "search_files",
        "Search file contents under a path for a literal string (ripgrep when "
        "available, bounded Python walk otherwise).",
        {"query": _STR,
         "path": {**_STR, "description": "Directory to search; defaults to the root."},
         "glob": {**_STR, "description": "Optional filename glob, e.g. '*.py'."},
         "max_results": {"type": "integer", "minimum": 1, "maximum": 100}},
        ["query"], read_only=True, permission="files:read",
    ),
    _search_files,
))

_register(AgentTool(
    _spec(
        "fetch_url",
        "Fetch one http(s) URL and return its visible text (bounded). "
        "Loopback/private/reserved hosts are refused.",
        {"url": _STR},
        ["url"], read_only=True, permission="web:read",
    ),
    _fetch_url,
))

_register(AgentTool(
    _spec(
        "search_web",
        "Search the web through the configured search provider. Refuses "
        "honestly when no provider is configured rather than inventing results.",
        {"query": _STR, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
        ["query"], read_only=True, permission="web:read",
    ),
    _search_web,
))

_register(AgentTool(
    _spec(
        "open_app",
        "Open a macOS application by name (front-most).",
        {"name": _STR},
        ["name"], read_only=False, permission="computer:run",
    ),
    _open_app,
))

_register(AgentTool(
    _spec(
        "open_url",
        "Open an http(s) URL in the default browser.",
        {"url": _STR},
        ["url"], read_only=False, permission="computer:run",
    ),
    _open_url,
))

_SPEC_ACTION = {
    "type": "object",
    "description": (
        "One UI action. click: {kind:'click', ref}; set_value: "
        "{kind:'set_value', ref, text}; keystroke: {kind:'keystroke', text, "
        "modifiers?: ['command']}."
    ),
    "properties": {
        "kind": {"type": "string", "enum": ["click", "set_value", "keystroke"]},
        "ref": {"type": "integer", "minimum": 0},
        "text": _STR,
        "modifiers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kind"],
}

_register(AgentTool(
    _spec(
        "observe_app",
        "Read the accessibility tree of one running macOS app (front window): "
        "roles, names, values and a frame_id. Read-only; use before act_in_app.",
        {"app": _STR, "limit": {"type": "integer", "minimum": 20, "maximum": 400}},
        ["app"], read_only=True, permission="computer:read",
    ),
    _observe_app,
))

_register(AgentTool(
    _spec(
        "act_in_app",
        "Perform one bounded UI action in an app using a frame_id from "
        "observe_app: click an element, set a field's value, or send keys. "
        "Needs owner approval; secure fields are always refused.",
        {"app": _STR, "frame_id": _STR, "action": _SPEC_ACTION},
        ["app", "action"], read_only=False, permission="computer:run",
    ),
    _act_in_app,
))

_register(AgentTool(
    _spec(
        "verify_app",
        "Re-observe an app and check that a text is present (or absent) after "
        "an action. Returns verified=true only when the screen actually matches.",
        {"app": _STR, "contains_text": _STR, "absent_text": _STR},
        ["app"], read_only=True, permission="computer:read",
    ),
    _verify_app,
))


_register(AgentTool(
    _spec(
        "press_key",
        "Press one key in an app: return, enter, tab, escape, space, delete, "
        "backspace, up/down/left/right, home/end, pageup/pagedown — optionally "
        "with command/shift/option/control modifiers. Needs approval.",
        {"app": _STR, "key": _STR,
         "modifiers": {"type": "array", "items": {"type": "string"}}},
        ["app", "key"], read_only=False, permission="computer:run",
    ),
    _press_key,
))

_register(AgentTool(
    _spec(
        "run_in_terminal",
        "Launch or focus a terminal app (Ghostty, Terminal, iTerm), type one "
        "command, press Return, and optionally verify text on screen. This is "
        "how a command is sent to a real terminal session instead of a hidden "
        "shell. Needs approval.",
        {"app": {**_STR, "description": "Terminal app name; defaults to Ghostty."},
         "command": _STR,
         "wait_s": {"type": "number", "minimum": 0, "maximum": 30},
         "expect_text": {**_STR, "description": "Optional text that must appear on screen."}},
        ["command"], read_only=False, permission="computer:run",
    ),
    _run_in_terminal,
))

# Local hands (terminal, screen vision, AppleScript, Mac semantic bus) live in
# their own module to keep this file readable; they register into the same
# registry and therefore inherit the same loop, policy, and receipts.
from app.agent.local_tools import TOOLS as _LOCAL_TOOLS  # noqa: E402

for _local_tool in _LOCAL_TOOLS:
    _register(_local_tool)


# The semantic capability graph (mail, messages, memory, people, goals, files,
# code, research, timers, PIM, digital ops, ...) reaches the model as one
# typed tool per capability, not as one generic dispatcher that takes a name
# string. That is the shape both reference harnesses use, and it is what makes
# "any command the owner can think of" reachable without a Python edit.
from app.agent.capability_bridge import capability_tools as _CAPABILITY_TOOLS  # noqa: E402

for _capability_tool in _CAPABILITY_TOOLS():
    _register(_capability_tool)


# The model's own plan, kept in one place so a multi-step goal survives a
# long run and the owner can watch the shape of the work.
_register(AgentTool(
    _spec(
        "todo_write",
        "Create or update this run's task list. The owner sees this list live "
        "while you work. Call it BEFORE any other tool on any task that "
        "needs more than one step, and update it as you go. Skip it only for "
        "a genuinely single-step task.",
        {
            "todos": {
                "type": "array",
                "description": "The plan. One entry per step.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {**_STR, "description": "What this step does."},
                        "status": {
                            "type": "string",
                            "enum": list(_TODO_STATUSES),
                            "description": "Where this step stands.",
                        },
                    },
                    "required": ["content"],
                },
            },
            "merge": {
                "type": "boolean",
                "description": "Merge into the current list instead of replacing it.",
            },
        },
        ["todos"],
        read_only=True,
        permission="agent:control",
    ),
    _todo_write,
))


def agent_tool_specs() -> list[ToolSpec]:
    """Tool specs offered to the brain, including the two control tools."""
    specs = [tool.spec for tool in REGISTRY.values()]
    specs.append(ToolSpec(
        name="finish",
        description=(
            "Finish the run. Call exactly once when the goal is complete or "
            "cannot proceed, with a one-paragraph summary of what happened and "
            "what was verified. Never claim an effect a tool receipt did not show."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {"summary": _STR, "ok": {"type": "boolean"}},
            "required": ["summary"],
        },
        read_only=True,
        permission="agent:control",
        risk_class="R0",
    ))
    specs.append(ToolSpec(
        name="ask_owner",
        description=(
            "Stop and ask the owner one short question when the goal is "
            "ambiguous or a decision is theirs to make. Use instead of guessing. "
            "Offer options when the answer is a choice between known paths; "
            "the owner can also answer in their own words."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "question": _STR,
                "options": {
                    "type": "array",
                    "description": "Optional choices to offer the owner.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": _STR,
                            "description": _STR,
                        },
                        "required": ["label"],
                    },
                },
            },
            "required": ["question"],
        },
        read_only=True,
        permission="agent:control",
        risk_class="R0",
    ))
    return specs


def tool_json(receipt: dict[str, Any], *, limit: int | None = None) -> str:
    """One compact JSON line for the model, clipped to the configured budget."""
    budget = limit or int(getattr(settings, "agent_max_output_chars", 8000))
    payload = {
        "ok": receipt.get("ok"),
        "error": receipt.get("error"),
        "result": receipt.get("result"),
    }
    text = json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) > budget:
        text = text[:budget] + '... [clipped]'
    return text
