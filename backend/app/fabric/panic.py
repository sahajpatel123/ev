"""D10 kill switch: panic denies all effects until the owner re-enables.

State is a small JSON file (default ~/.ev/fabric_panic.json, overridable
via EV_FABRIC_PANIC_FILE for tests) so panic survives process restarts:
a panicked Evie stays denied until the owner explicitly clears it.
Panic also revokes every standing grant and stops tracked shell sessions.
Reads are unaffected; only effects are denied.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _state_path() -> Path:
    from app.config import settings

    raw = str(getattr(settings, "fabric_panic_file", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".ev" / "fabric_panic.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def panicked() -> bool:
    return bool(_read().get("panicked"))


def panic_state() -> dict[str, Any]:
    state = _read()
    return {
        "panicked": bool(state.get("panicked")),
        "at": state.get("at"),
        "reason": state.get("reason"),
    }


def panic(reason: str = "", *, roots: list | None = None) -> dict[str, Any]:
    """Engage panic: deny effects, revoke grants, stop shell sessions."""
    from .grants import revoke_all

    stopped = 0
    errors: list[str] = []
    try:
        from .roots import default_roots
        from .shell_sessions import ShellSessions

        for root in list(roots) if roots is not None else default_roots():
            try:
                sessions = ShellSessions(root=root).list().get("sessions", [])
            except Exception:
                continue
            for sid in sessions:
                try:
                    res = ShellSessions(root=root).cancel(sid)
                    if res.get("ok"):
                        stopped += 1
                except Exception as exc:
                    errors.append(f"{sid}: {type(exc).__name__}")
    except Exception as exc:  # panic must never fail because cleanup did
        errors.append(f"cleanup: {type(exc).__name__}")
    revoked = revoke_all()
    from .cancel import cancel_all

    cancel_all()
    payload = {"panicked": True, "at": time.time(), "reason": reason or "owner panic"}
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        written = True
    except OSError as exc:
        written = False
        errors.append(f"state: {exc}")
    return {
        "ok": written,
        "verified": written and panicked(),
        "evidence": {"stopped_sessions": stopped, "grants_revoked": revoked.get("ok", False)},
        "error": None if written else "; ".join(errors),
        "undo": {"clear": True},
    }


def clear() -> dict[str, Any]:
    """Owner re-enables effects. Auth is enforced at the API/CLI layer."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"panicked": False, "at": time.time()}))
        ok = not panicked()
        return {"ok": ok, "verified": ok, "evidence": {}, "error": None, "undo": None}
    except OSError as exc:
        return {"ok": False, "verified": True, "evidence": {}, "error": str(exc), "undo": None}
