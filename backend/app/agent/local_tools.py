"""Local hands for the agent: terminal, screen vision, AppleScript, Mac bus.

These are the tools that make the agent operate the owner's own Mac instead of
describing what it would do. They plug into the same registry and receipt
vocabulary as the shell/file tools, and they reuse existing engines wherever
one exists:

- terminal control rides Ghostty's AppleScript surface with tmux as the
  persistence/addressing layer (:mod:`app.fabric.terminal`);
- image understanding uses the owner's configured vision provider for OCR and
  the agent's own multimodal brain for description;
- AppleScript is one bounded ``osascript`` call for Mail, Messages, Finder,
  Music, and any other scriptable app.

The semantic capabilities (mail, messages, calendar, contacts, weather,
timers, PIM, digital operations, memory, people, files, code, goals) are not
in this module. They reach the model as one typed tool each, through
:mod:`app.agent.capability_bridge`, which dispatches to the same kernel
executor. An earlier shape — one ``mac_call`` tool that took a capability
*name* and a free-form argument bag — is gone: it had no typed parameters,
it carried a hand-maintained list of which names may run without asking, and
it refused ``capability.discover``, the one tool that answers "what can I
do?". One tool per capability removes all three faults.

Every receipt is measured: a terminal action reads the pane back, a capture
returns the file it wrote, and a Mac capability returns the executor's own
result. No tool here claims an effect it did not observe.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from app.agent.tools import AgentTool, ToolContext, _receipt, _resolve
from app.config import settings

MAX_TERMINAL_PANES = 6
MAX_TERMINAL_READ_LINES = 400
MAX_IMAGE_BYTES = 12_000_000
_REGISTRY_NAME = "terminals.json"


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def _state_dir() -> Path:
    raw = str(getattr(settings, "agent_state_db", "") or "").strip()
    if raw:
        base = Path(raw).expanduser()
        return base if base.suffix != ".db" else base.parent
    return Path.home() / ".ev" / "agent"


def _capture_dir() -> Path:
    folder = _state_dir() / "captures"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _run(argv: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _deny_command(cmd: str) -> dict[str, Any] | None:
    """Refuse a terminal command the shell lane would hard-deny."""
    from app.fabric.shell_sessions import classify_command

    cls = classify_command(cmd)
    if cls == "DENY":
        return _receipt(
            ok=False,
            error="denied by policy",
            verified=True,
            evidence={"cmd": cmd, "class": cls},
        )
    return None


def _need_confirm(tool: str) -> dict[str, Any]:
    return _receipt(
        ok=False,
        error="confirm required",
        verified=True,
        evidence={"tool": tool, "needs_confirm": True},
        needs_confirm=True,
    )


# --------------------------------------------------------------------------- #
# Terminal: Ghostty + tmux, Terminal.app fallback
# --------------------------------------------------------------------------- #


def _registry_path() -> Path:
    return _state_dir() / _REGISTRY_NAME


def _load_registry() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = raw.get("terminals") if isinstance(raw, dict) else None
    if not isinstance(rows, dict):
        return {}
    return {str(key): dict(value) for key, value in rows.items() if isinstance(value, dict)}


def _save_registry(rows: dict[str, dict[str, Any]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"terminals": rows}, indent=2, default=str), encoding="utf-8")


def _prune_registry(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Drop entries older than seven days; tmux sessions may still exist."""
    cutoff = time.time() - 7 * 86400
    return {
        key: value
        for key, value in rows.items()
        if float(value.get("created_at") or 0) >= cutoff
    }


def _tmux(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return _run(["tmux", *args], timeout=timeout)


def _pane_ids(session: str) -> list[str]:
    proc = _tmux("list-panes", "-t", session, "-F", "#{pane_id}")
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _pane_for(session: str, pane: int) -> str | None:
    """Resolve a 0-based pane index; out-of-range is an error, never a clamp.

    Silently clamping pane 3 to the last pane made an agent report the wrong
    pane's output as fact. The caller must learn that only 0..N-1 exist.
    """
    ids = _pane_ids(session)
    if not ids:
        return None
    try:
        index = int(pane)
    except (TypeError, ValueError):
        return None
    if index < 0 or index >= len(ids):
        return None
    return ids[index]


def _ghostty_scriptable() -> bool:
    try:
        from app.fabric import ghostty

        return bool(ghostty.probe().get("evidence", {}).get("scriptable"))
    except Exception:  # noqa: BLE001 - a probe failure means "use the fallback"
        return False


def _terminal_fallback(panes: list[dict[str, Any]]) -> dict[str, Any]:
    """Terminal.app: one scripted window per pane. Honest and dependency-free."""
    windows: list[str] = []
    for pane in panes:
        parts: list[str] = []
        cwd = str(pane.get("cwd") or "").strip()
        if cwd:
            parts.append(f"cd {json.dumps(cwd)}")
        command = str(pane.get("command") or "").strip()
        if command:
            parts.append(command)
        script = "; ".join(parts) or "clear"
        escaped = script.replace("\\", "\\\\").replace('"', '\\"')
        proc = _run(
            [
                "osascript",
                "-e",
                'tell application "Terminal"',
                "-e",
                "activate",
                "-e",
                f'set w to do script "{escaped}"',
                "-e",
                "return id of front window",
                "-e",
                "end tell",
            ],
            timeout=30,
        )
        if proc.returncode != 0:
            return _receipt(
                ok=False,
                error=(proc.stderr or "Terminal.app refused the script").strip()[:400],
                verified=True,
            )
        windows.append((proc.stdout or "").strip())
    return _receipt(
        ok=True,
        verified=True,
        result={"kind": "terminal_app", "windows": windows, "panes": len(windows)},
        evidence={"app": "Terminal", "windows": windows},
    )


def _ghostty_layout(panes: list[dict[str, Any]]) -> dict[str, Any]:
    from app.fabric.terminal import GhosttyLayout

    layout = GhosttyLayout(root=Path.home())
    opened = layout.open(panes, confirm=True)
    if not opened.get("ok"):
        return _receipt(
            ok=False,
            error=str(opened.get("error") or "Ghostty layout failed"),
            verified=True,
        )
    session = str(layout.tmux.session or "")
    window_id = str(layout.window_id or "")
    term_id = f"term-{int(time.time() * 1000) % 10_000_000}-{os.getpid()}"
    rows = _prune_registry(_load_registry())
    rows[term_id] = {
        "id": term_id,
        "kind": "ghostty",
        "session": session,
        "window_id": window_id,
        "panes": panes,
        "created_at": time.time(),
    }
    _save_registry(rows)
    pane_ids = _pane_ids(session)
    return _receipt(
        ok=True,
        verified=True,
        result={
            "terminal_id": term_id,
            "kind": "ghostty",
            "session": session,
            "window_id": window_id,
            "pane_count": len(pane_ids),
            # Address panes by this 0-based index, not by the tmux id's digits.
            "panes": [
                {
                    "index": index,
                    "id": pane_id,
                    "command": (panes[index].get("command") if index < len(panes) else ""),
                }
                for index, pane_id in enumerate(pane_ids)
            ],
        },
        evidence={"session": session, "window_id": window_id},
    )


def _ghostty_attach(session: str) -> dict[str, Any]:
    """Visible resume: a Ghostty window born attached to a live session."""
    if not shutil.which("tmux"):
        return _receipt(ok=False, error="tmux not installed", verified=True)
    from app.fabric.terminal import GhosttyLayout

    layout = GhosttyLayout(root=Path.home())
    opened = layout.attach(session)
    if not opened.get("ok"):
        return _receipt(
            ok=False,
            error=str(opened.get("error") or "attach failed"),
            verified=True,
            evidence=dict(opened.get("evidence") or {}),
        )
    window_id = str(layout.window_id or "")
    term_id = f"term-{int(time.time() * 1000) % 10_000_000}-{os.getpid()}"
    rows = _prune_registry(_load_registry())
    rows[term_id] = {
        "id": term_id,
        "kind": "ghostty",
        "session": session,
        "window_id": window_id,
        "panes": [],
        "created_at": time.time(),
    }
    _save_registry(rows)
    return _receipt(
        ok=True,
        verified=True,
        result={
            "terminal_id": term_id,
            "kind": "ghostty",
            "session": session,
            "window_id": window_id,
            "resumed": True,
        },
        evidence={"session": session, "window_id": window_id},
    )


async def _terminal_open(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.approved:
        return _need_confirm("terminal_open")
    attach = str(args.get("attach") or "").strip()
    if attach:
        return await asyncio.to_thread(_ghostty_attach, attach)
    raw_panes = args.get("panes")
    panes: list[dict[str, Any]] = []
    if isinstance(raw_panes, list):
        for row in raw_panes[:MAX_TERMINAL_PANES]:
            if isinstance(row, dict):
                panes.append(
                    {
                        "command": str(row.get("command") or "")[:2000],
                        "cwd": str(row.get("cwd") or "")[:1000],
                        "title": str(row.get("title") or "")[:120],
                    }
                )
    if not panes:
        panes = [
            {
                "command": str(args.get("command") or "")[:2000],
                "cwd": str(args.get("cwd") or ctx.root)[:1000],
            }
        ]
    for pane in panes:
        denied = _deny_command(str(pane.get("command") or ""))
        if denied is not None:
            return denied
        cwd = str(pane.get("cwd") or "").strip()
        if cwd and not Path(cwd).expanduser().is_dir():
            return _receipt(ok=False, error=f"cwd is not a directory: {cwd}")
    choice = str(
        args.get("app") or getattr(settings, "agent_terminal_app", "ghostty") or "ghostty"
    ).lower()
    use_ghostty = choice != "terminal" and _ghostty_scriptable()
    if use_ghostty and shutil.which("tmux"):
        return await asyncio.to_thread(_ghostty_layout, panes)
    return await asyncio.to_thread(_terminal_fallback, panes)


def _entry_for(args: dict[str, Any]) -> dict[str, Any] | None:
    rows = _load_registry()
    term_id = str(args.get("terminal_id") or "").strip()
    if term_id:
        return rows.get(term_id)
    session = str(args.get("session") or "").strip()
    if session:
        for row in rows.values():
            if str(row.get("session") or "") == session:
                return row
        # Attach-by-name: a live but untracked tmux session (the owner's
        # detached opencode/Muse work) becomes addressable on first use.
        if shutil.which("tmux") and _tmux("has-session", "-t", session).returncode == 0:
            tracked = _prune_registry(_load_registry())
            term_id = f"term-attach-{int(time.time() * 1000) % 10_000_000}-{os.getpid()}"
            tracked[term_id] = {
                "id": term_id,
                "kind": "tmux",
                "session": session,
                "window_id": "",
                "panes": [],
                "created_at": time.time(),
            }
            _save_registry(tracked)
            return tracked[term_id]
    # Newest live entry, so a follow-up "run this there" works without an id.
    live = [row for row in rows.values() if str(row.get("session") or "")]
    live.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
    return live[0] if live else None


def _capture(session: str, pane: int, lines: int) -> str:
    pane_id = _pane_for(session, pane)
    if pane_id is None:
        return ""
    proc = _tmux("capture-pane", "-p", "-t", pane_id, "-S", f"-{max(1, lines)}")
    return proc.stdout or "" if proc.returncode == 0 else ""


def _terminal_run_sync(
    entry: dict[str, Any], command: str, pane: int, wait_s: float, lines: int
) -> dict[str, Any]:
    session = str(entry.get("session") or "")
    pane_id = _pane_for(session, pane)
    if pane_id is None:
        count = len(_pane_ids(session))
        return {
            "ok": False,
            "error": (
                f"pane {pane} is out of range; this terminal has {count} pane(s) "
                f"indexed 0..{count - 1}"
            ),
        }
    proc = _tmux("send-keys", "-t", pane_id, command, "Enter")
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or "tmux send-keys failed").strip()[:400],
        }
    if wait_s > 0:
        time.sleep(min(max(wait_s, 0.0), 120.0))
    return {"ok": True, "session": session, "pane_id": pane_id, "output": _capture(session, pane, lines)}


async def _terminal_run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.approved:
        return _need_confirm("terminal_run")
    command = str(args.get("command") or "").strip()
    if not command:
        return _receipt(ok=False, error="command required")
    denied = _deny_command(command)
    if denied is not None:
        return denied
    entry = _entry_for(args)
    if entry is None:
        return _receipt(
            ok=False,
            error="no tracked terminal; call terminal_open first (or pass terminal_id)",
            verified=True,
        )
    if str(entry.get("kind") or "") not in {"ghostty", "tmux"}:
        return _receipt(
            ok=False,
            error="this terminal kind has no read-back; reopen it with terminal_open",
            verified=True,
        )
    pane = int(args.get("pane") or 0)
    _wait = args.get("wait_s")
    wait_s = float(str(_wait)) if _wait is not None else 1.0
    lines = min(int(args.get("lines") or 60), MAX_TERMINAL_READ_LINES)
    result = await asyncio.to_thread(_terminal_run_sync, entry, command, pane, wait_s, lines)
    if not result.get("ok"):
        return _receipt(ok=False, error=str(result.get("error")), verified=True)
    return _receipt(
        ok=True,
        verified=True,
        result={
            "terminal_id": entry.get("id"),
            "session": result["session"],
            "pane_id": result["pane_id"],
            "command": command,
            "output": result.get("output", ""),
        },
        evidence={"session": result["session"], "pane_id": result["pane_id"]},
    )


def _terminal_read_sync(entry: dict[str, Any], pane: int, lines: int) -> str:
    session = str(entry.get("session") or "")
    return _capture(session, pane, lines)


async def _terminal_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    del ctx
    entry = _entry_for(args)
    if entry is None:
        return _receipt(ok=False, error="no tracked terminal", verified=True)
    if str(entry.get("kind") or "") not in {"ghostty", "tmux"}:
        return _receipt(ok=False, error="this terminal kind has no read-back", verified=True)
    lines = min(int(args.get("lines") or 80), MAX_TERMINAL_READ_LINES)
    pane = int(args.get("pane") or 0)
    session = str(entry.get("session") or "")
    pane_id = _pane_for(session, pane)
    if pane_id is None:
        count = len(_pane_ids(session))
        return _receipt(
            ok=False,
            error=(
                f"pane {pane} is out of range; this terminal has {count} pane(s) "
                f"indexed 0..{count - 1}"
            ),
            verified=True,
        )
    text = await asyncio.to_thread(_terminal_read_sync, entry, pane, lines)
    return _receipt(
        ok=True,
        verified=True,
        result={
            "terminal_id": entry.get("id"),
            "session": entry.get("session"),
            "pane": pane,
            "pane_id": pane_id,
            "output": text,
        },
        evidence={"session": entry.get("session"), "pane_id": pane_id, "chars": len(text)},
    )


def _terminal_list_sync() -> dict[str, Any]:
    rows = _load_registry()
    sessions: list[dict[str, Any]] = []
    proc = _tmux("list-sessions", "-F", "#{session_name}\t#{session_windows}")
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            name, _, windows = line.partition("\t")
            if name:
                sessions.append({"session": name, "windows": windows})
    live: list[dict[str, Any]] = []
    for key, row in rows.items():
        session = str(row.get("session") or "")
        alive = bool(session) and _tmux("has-session", "-t", session).returncode == 0
        live.append(
            {
                "terminal_id": key,
                "kind": row.get("kind"),
                "session": session,
                "window_id": row.get("window_id"),
                "alive": alive,
                "panes": _pane_ids(session) if alive else [],
            }
        )
    return {"tracked": live, "tmux_sessions": sessions}


async def _terminal_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    del args, ctx
    data = await asyncio.to_thread(_terminal_list_sync)
    return _receipt(ok=True, verified=True, result=data, evidence={"n": len(data["tracked"])})


# --------------------------------------------------------------------------- #
# Screen capture + image understanding
# --------------------------------------------------------------------------- #


def _capture_sync() -> dict[str, Any]:
    # Prefer Evie.app's bridge: it runs inside the app's TCC identity and can
    # see the screen even when the server process has no Screen Recording
    # grant. Fall back to a native capture and report which one was used.
    try:
        from app.agent import tools as agent_tools

        if agent_tools._bridge_available():
            shot = agent_tools._bridge_call("screenshot", {}, timeout=45)
            if shot.get("ok"):
                payload = shot.get("result") or shot.get("evidence") or {}
                path = Path(str(payload.get("path") or ""))
                if path.is_file():
                    return {
                        "ok": True,
                        "path": str(path),
                        "bytes": path.stat().st_size,
                        "source": "evie_bridge",
                        "info": "",
                    }
    except Exception:  # noqa: BLE001 - bridge absence is not a capture failure
        pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = _capture_dir() / f"screen-{stamp}.png"
    proc = _run(["screencapture", "-x", "-t", "png", str(path)], timeout=30)
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or "screencapture failed").strip()[:400],
            "hint": "grant Screen Recording to the process running Evie, then retry",
        }
    if not path.is_file():
        return {"ok": False, "error": "screencapture wrote no file"}
    size = path.stat().st_size
    dims = _run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], timeout=15
    )
    return {
        "ok": True,
        "path": str(path),
        "bytes": size,
        "source": "screencapture",
        "info": (dims.stdout or "").strip(),
    }


async def _capture_screen(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    data = await asyncio.to_thread(_capture_sync)
    if not data.get("ok"):
        return _receipt(
            ok=False,
            error=str(data.get("error")),
            verified=True,
            evidence={"hint": data.get("hint")},
        )
    question = str(args.get("question") or "").strip()
    seen = await _read_image(
        {"path": data["path"], "question": question or "What is on the screen right now? "
         "Name the front app and read any command, path, error, or prompt verbatim."},
        ctx,
    )
    result: dict[str, Any] = {
        "path": data["path"],
        "bytes": data["bytes"],
        "source": data["source"],
        "dimensions": data.get("info", ""),
    }
    if seen.get("ok"):
        payload = seen.get("result") or {}
        result["description"] = payload.get("description", "")
        result["ocr"] = payload.get("ocr", "")
        result["text"] = payload.get("text") or payload.get("ocr", "") or payload.get(
            "description", ""
        )
    else:
        result["next"] = "call read_image on this path to see it"
        result["vision_error"] = seen.get("error")
    return _receipt(
        ok=True,
        verified=True,
        result=result,
        evidence={"path": data["path"], "source": data["source"]},
    )


def _resize_for_model(path: Path) -> Path:
    """Downscale a copy so one screenshot fits a multimodal request."""
    if path.stat().st_size <= 1_500_000:
        return path
    target = _capture_dir() / f"{path.stem}-model.jpg"
    proc = _run(
        ["sips", "-s", "format", "jpeg", "-Z", "1800", str(path), "--out", str(target)],
        timeout=30,
    )
    if proc.returncode == 0 and target.is_file():
        return target
    return path


def _vision_provider():
    """The agent's multimodal brain (Muse Spark takes image input), or None
    when no key is configured."""
    if str(getattr(settings, "openrouter_api_key", "") or "").strip():
        from app.gateway.openrouter_muse import MuseSparkProvider

        return MuseSparkProvider()
    return None


async def _describe_image(path: Path, question: str) -> dict[str, Any]:
    provider = _vision_provider()
    if provider is None:
        return {"ok": False, "error": "no multimodal brain key is configured"}
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".tiff"}:
        return {"ok": False, "error": f"not an image file: {path.suffix or 'no suffix'}"}
    from app.contracts import ChatMessage, MediaPart

    source = _resize_for_model(path)
    raw = source.read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        return {"ok": False, "error": f"image is too large to read ({len(raw)} bytes)"}
    mime = "image/jpeg" if source.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    prompt = question.strip() or (
        "Describe this image for a computer-use agent that must act on it. "
        "If it is a screenshot: name the app and window, then list every "
        "visible command, path, error, and label verbatim. Do not guess text "
        "you cannot read."
    )
    message = ChatMessage(
        role="user",
        content=prompt,
        media=[MediaPart(kind="image", content_type=mime, data_url=data_url)],
    )
    try:
        reply = await provider.chat([message])
    except Exception as exc:  # noqa: BLE001 - report the provider's own failure
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    text = str(getattr(reply, "text", "") or "").strip()
    if not text:
        return {"ok": False, "error": "the vision brain returned no text"}
    return {"ok": True, "text": text, "provider": getattr(provider, "name", "vision")}


async def _read_image(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return _receipt(ok=False, error="path required")
    target = _resolve(raw_path, ctx.root)
    if not target.is_file():
        return _receipt(ok=False, error=f"no such file: {target}", verified=True)
    if not ctx.approved and not str(target).startswith(str(ctx.root)):
        return _need_confirm("read_image")
    from app.vision.providers import VisionProviderError, get_vision_provider

    data = target.read_bytes()
    ocr = ""
    ocr_provider = ""
    ocr_error = ""
    try:
        suffix = target.suffix.lower()
        content_type = (
            "image/png"
            if suffix == ".png"
            else "image/jpeg"
            if suffix in {".jpg", ".jpeg"}
            else None
        )
        result = await get_vision_provider().analyze(
            data=data, content_type=content_type, filename=target.name
        )
        ocr = str(getattr(result, "ocr_text", "") or "")
        ocr_provider = str(
            getattr(result, "provider", "") or getattr(result, "name", "") or ""
        )
    except (VisionProviderError, OSError) as exc:
        ocr_error = str(exc)
    described = await _describe_image(target, str(args.get("question") or ""))
    if not described.get("ok") and not ocr:
        return _receipt(
            ok=False,
            error=f"{described.get('error')}; OCR: {ocr_error or 'unavailable'}",
            verified=True,
            evidence={"path": str(target)},
        )
    return _receipt(
        ok=True,
        verified=True,
        result={
            "path": str(target),
            "description": described.get("text", ""),
            "ocr": ocr,
            # Compatibility with the first OCR-only tool shape and with any
            # caller that wants one text field regardless of which engine won.
            "text": ocr or described.get("text", ""),
            "ocr_provider": ocr_provider,
            "vision_provider": described.get("provider", ""),
            "degraded": not described.get("ok", False),
        },
        evidence={"path": str(target), "bytes": len(data)},
    )


# --------------------------------------------------------------------------- #
# AppleScript
# --------------------------------------------------------------------------- #


async def _run_applescript(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.approved:
        return _need_confirm("run_applescript")
    from app.fabric.shell_sessions import _allowed_roots

    if _allowed_roots():
        # AppleScript computes its own paths — "path to desktop folder" is
        # the real Desktop whatever $HOME says — so a boundary expressed as
        # roots cannot be enforced by reading the script. A confined run may
        # not use it. With no boundary configured, this is unchanged.
        return _receipt(
            ok=False,
            error=(
                "AppleScript is not available in a confined run: it can reach "
                "paths this run may not touch. Use a file tool instead, or run "
                "the task without a restricted root boundary."
            ),
            verified=True,
        )
    script = str(args.get("script") or "").strip()
    if not script:
        return _receipt(ok=False, error="script required")
    if re.search(r"\bwith\s+administrator\s+privileges\b|\bsudo\b", script, re.IGNORECASE):
        return _receipt(ok=False, error="denied by policy: privilege escalation", verified=True)
    timeout = min(max(float(args.get("timeout_s") or 30), 1.0), 300.0)

    def _exec() -> subprocess.CompletedProcess:
        return _run(["osascript", "-e", script], timeout=timeout)

    try:
        proc = await asyncio.to_thread(_exec)
    except subprocess.TimeoutExpired:
        return _receipt(ok=False, error="osascript timed out", verified=True)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return _receipt(ok=False, error=(err or "osascript failed")[:1000], verified=True)
    return _receipt(
        ok=True,
        verified=True,
        result={"stdout": out[:8000], "chars": len(out)},
        evidence={"exit_code": proc.returncode},
    )


# --------------------------------------------------------------------------- #
# Tool specs
# --------------------------------------------------------------------------- #


_STR = {"type": "string"}


def _spec(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    read_only: bool,
    permission: str,
) -> Any:
    from app.contracts import ToolSpec

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


TOOLS: list[AgentTool] = [
    AgentTool(
        _spec(
            "terminal_open",
            "Open a real terminal window on the owner's Mac (Ghostty, tmux-backed "
            "so panes are addressable and readable) and run each pane's command. "
            "Use for launching CLIs, resuming sessions, or any visible shell work. "
            "Pass attach with a live tmux session name to visibly resume detached "
            "work (opencode/Muse continuity sessions) instead of starting fresh. "
            "Returns a terminal_id and pane count; use terminal_run/terminal_read after.",
            {
                "panes": {
                    "type": "array",
                    "description": "Panes to open: [{command, cwd, title}].",
                    "items": {
                        "type": "object",
                        "properties": {"command": _STR, "cwd": _STR, "title": _STR},
                    },
                },
                "command": {**_STR, "description": "Single-command shorthand."},
                "cwd": _STR,
                "app": {"type": "string", "enum": ["ghostty", "terminal"]},
                "attach": {**_STR, "description": "Live tmux session to visibly resume."},
            },
            [],
            read_only=False,
            permission="computer:run",
        ),
        _terminal_open,
    ),
    AgentTool(
        _spec(
            "terminal_run",
            "Type a command into an open terminal pane and return the screen "
            "output after it runs. Pass terminal_id (or omit for the newest). "
            "This is for visible terminal work; run_shell is for hidden commands.",
            {
                "terminal_id": _STR,
                "session": _STR,
                "pane": {
                    "type": "integer", "minimum": 0, "maximum": 8,
                    "description": "0-based index from terminal_open's panes list.",
                },
                "command": _STR,
                "wait_s": {"type": "number", "minimum": 0, "maximum": 120},
                "lines": {"type": "integer", "minimum": 1, "maximum": 400},
            },
            ["command"],
            read_only=False,
            permission="computer:run",
        ),
        _terminal_run,
    ),
    AgentTool(
        _spec(
            "terminal_read",
            "Read the visible output of an open terminal pane (last N lines).",
            {
                "terminal_id": _STR,
                "session": _STR,
                "pane": {
                    "type": "integer", "minimum": 0, "maximum": 8,
                    "description": "0-based index from terminal_open's panes list.",
                },
                "lines": {"type": "integer", "minimum": 1, "maximum": 400},
            },
            [],
            read_only=True,
            permission="computer:read",
        ),
        _terminal_read,
    ),
    AgentTool(
        _spec(
            "terminal_list",
            "List tracked terminal windows and every live tmux session.",
            {},
            [],
            read_only=True,
            permission="computer:read",
        ),
        _terminal_list,
    ),
    AgentTool(
        _spec(
            "capture_screen",
            "Capture the owner's screen and read it: returns a PNG path plus a "
            "visual description and OCR text. Use to see what an app or terminal "
            "is showing right now, then act on it.",
            {"question": {**_STR, "description": "Optional: what to look for."}},
            [],
            read_only=True,
            permission="computer:read",
        ),
        _capture_screen,
    ),
    AgentTool(
        _spec(
            "read_image",
            "See an image file on the owner's Mac (screenshot, photo, document). "
            "Returns a visual description plus OCR text. Ask a specific question "
            "when the owner refers to what is in a screenshot.",
            {"path": _STR, "question": _STR},
            ["path"],
            read_only=True,
            permission="computer:read",
        ),
        _read_image,
    ),
    AgentTool(
        _spec(
            "run_applescript",
            "Run one AppleScript in a scriptable Mac app (Mail, Messages, Finder, "
            "Music, Reminders, Calendar, Safari...). Use for app actions with no "
            "dedicated tool. Privilege escalation is refused.",
            {"script": _STR, "timeout_s": {"type": "number", "minimum": 1, "maximum": 300}},
            ["script"],
            read_only=False,
            permission="computer:run",
        ),
        _run_applescript,
    ),
]

__all__ = ["TOOLS"]