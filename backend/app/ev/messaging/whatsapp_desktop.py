"""WhatsApp Desktop (background) via EVLifeHelper's Accessibility commands.

The owner keeps WhatsApp open in the background — or fully closed; the helper
launches it headless first. Chat *listing* and the composer are driven by the
Accessibility API directly. WhatsApp (Catalyst) only navigates between chats
while it is frontmost, so opening a chat briefly activates the app and then
restores the previously frontmost app (and re-hides WhatsApp if it was
hidden). That window is reported honestly as ``focus_theft``; no Chrome tab is
needed or kept open.

Transport policy: Desktop AX only. WhatsApp Web/CDP is not selected for these
sends; when the helper cannot drive the desktop app the send refuses with a
named reason instead of switching transports.

Honest side effect: opening a chat marks its unread messages as read — the
same thing a person glancing at that chat would do. Delivery is only ever
reported when the helper sees the message in the thread as an outgoing row.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from app.integrations.life_helper import (
    LifeHelperError,
    LifeHelperUnavailableError,
    LifePermissionDeniedError,
)

logger = logging.getLogger("ev.messaging.whatsapp_desktop")

STATUS_CACHE_SECONDS = 4.0
SEND_TIMEOUT_SECONDS = 45.0
READ_TIMEOUT_SECONDS = 30.0
APP_BUNDLE_HELPER = "/Applications/Evie.app/Contents/MacOS/EVLifeHelper"

_status_cache: tuple[float, bool, str] | None = None
_last_access_prompt: float = 0.0
ACCESS_PROMPT_INTERVAL_SECONDS = 300.0


def _under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _dev_helper_candidates() -> list[str]:
    """Repo EVLifeHelper builds, newest first, independent of the cwd.

    The AX commands ship with the same source as the life bridges; a stale
    build (or the installed app bundle) must never shadow a fresh one.
    """

    roots = [
        Path(__file__).resolve().parents[4] / "macos",
        Path.cwd().parent / "macos",
        Path.cwd() / "macos",
    ]
    seen: list[Path] = []
    for root in roots:
        for config in ("release", "debug"):
            candidate = root / ".build" / config / "EVLifeHelper"
            if candidate not in seen:
                seen.append(candidate)
    existing = [path for path in seen if path.is_file()]
    existing.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [str(path) for path in existing]


def helper_candidates() -> list[str]:
    """Every place a usable EVLifeHelper may live, best first."""

    from app.config import settings

    preferred: list[str] = []
    for raw in (
        str(os.environ.get("EV_WHATSAPP_AX_HELPER") or "").strip(),
        str(getattr(settings, "life_helper_path", "") or "").strip(),
        str(os.environ.get("EV_LIFE_HELPER_PATH") or "").strip(),
    ):
        if raw and raw not in preferred:
            preferred.append(raw)
    ordered = preferred + _dev_helper_candidates() + [APP_BUNDLE_HELPER]
    usable: list[str] = []
    for candidate in ordered:
        if candidate and candidate not in usable and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            usable.append(candidate)
    return usable


def helper_path() -> str | None:
    """Newest usable EVLifeHelper: explicit overrides, then repo build, then app."""

    candidates = helper_candidates()
    return candidates[0] if candidates else None


async def _run(
    command: str,
    args: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    from app.integrations.life_helper import run_life_helper

    candidates = helper_candidates()
    if not candidates:
        return {"ok": False, "error": "helper_missing"}
    last: dict[str, Any] = {"ok": False, "error": "helper_missing"}
    for path in candidates:
        try:
            result = await run_life_helper(command, args, helper_path=path, timeout=timeout)
        except LifePermissionDeniedError:
            return {"ok": False, "error": "accessibility_not_granted"}
        except LifeHelperUnavailableError:
            continue
        except LifeHelperError as exc:
            code = str(exc.error_code or "helper_failed")
            if code in {"unsupported_command", "bad_arguments"}:
                # An older helper that predates the AX commands: try the next
                # build instead of reporting the transport unavailable.
                logger.warning(
                    "whatsapp helper %s is outdated for %s; trying next build", path, command
                )
                last = {"ok": False, "error": "helper_outdated"}
                continue
            logger.warning("whatsapp helper %s failed %s: %s", path, command, code)
            return {"ok": False, "error": code}
        data = result.data if isinstance(result.data, dict) else {}
        return {"ok": True, **data}
    return last


def _spoken_failure(target: str, error: str, candidates: list[str] | None = None) -> str:
    if error == "chat_not_found":
        return f"I couldn't find {target} in WhatsApp, so nothing was sent."
    if error == "ambiguous_chat":
        names = ", ".join(str(name) for name in (candidates or []) if name)
        return (
            f"I found more than one WhatsApp chat for {target}"
            + (f": {names}" if names else "")
            + ". Which one?"
        )
    if error == "send_not_confirmed":
        return (
            f"I composed the message to {target} but couldn't confirm it was sent, "
            "so I cleared the box. Nothing went out."
        )
    if error == "compose_failed":
        return f"I couldn't put the message into {target}'s WhatsApp chat, so nothing was sent."
    if error == "accessibility_not_granted":
        return (
            "WhatsApp control needs Accessibility permission. Open System Settings → "
            "Privacy & Security → Accessibility and enable EVLifeHelper, then ask me again."
        )
    if error in {"helper_missing", "helper_outdated"}:
        return f"The WhatsApp bridge on this Mac isn't current, so I didn't send to {target}."
    if error in {"whatsapp_not_installed", "whatsapp_not_available"}:
        return "WhatsApp isn't available on this Mac right now, so I didn't send it."
    return f"I couldn't send that WhatsApp to {target}."


def unavailable_next_step(diagnosis: str) -> str:
    """Owner-facing next step for a probe that failed before any send."""

    if diagnosis == "accessibility_not_granted":
        return (
            "WhatsApp control needs Accessibility permission. Open System Settings → "
            "Privacy & Security → Accessibility and enable EVLifeHelper, then ask again."
        )
    if diagnosis in {"helper_missing", "helper_outdated"}:
        return "The WhatsApp bridge on this Mac isn't current, so nothing was prepared."
    if diagnosis == "whatsapp_not_installed":
        return "WhatsApp isn't installed on this Mac, so nothing was prepared."
    if diagnosis == "pytest":
        return "Tests never drive the live WhatsApp transport."
    return "WhatsApp Desktop control isn't available right now, so nothing was prepared."


async def available(*, refresh: bool = False) -> tuple[bool, str]:
    """(usable, diagnosis). Usable includes a cold app the helper can launch."""

    global _status_cache
    if _under_pytest():
        return False, "pytest"
    from app.config import settings

    if not getattr(settings, "digital_ops_enabled", True):
        return False, "digital_ops_disabled"
    now = time.monotonic()
    if not refresh and _status_cache is not None:
        stamped, usable, diagnosis = _status_cache
        if now - stamped < STATUS_CACHE_SECONDS:
            return usable, diagnosis
    status = await _run("whatsapp.ax_status", {}, timeout=15.0)
    if not status.get("ok"):
        usable, diagnosis = False, str(status.get("error") or "helper_unavailable")
    elif status.get("installed") is False:
        usable, diagnosis = False, "whatsapp_not_installed"
    elif status.get("accessibility_trusted") is not True:
        usable, diagnosis = False, "accessibility_not_granted"
    elif status.get("running"):
        usable, diagnosis = True, "ok"
    else:
        usable, diagnosis = True, "cold"
    if diagnosis == "accessibility_not_granted" and not _under_pytest():
        global _last_access_prompt
        if now - _last_access_prompt >= ACCESS_PROMPT_INTERVAL_SECONDS:
            _last_access_prompt = now
            # One system prompt per window: the owner clicks Allow and the
            # helper binary lands in System Settings without hunting for it.
            await _run("whatsapp.ax_request_access", {}, timeout=10.0)
    if not usable:
        logger.warning(
            "WhatsApp transport unusable: %s (helper=%s)", diagnosis, helper_path()
        )
    _status_cache = (now, usable, diagnosis)
    return usable, diagnosis


async def list_chats(limit: int = 30) -> dict[str, Any]:
    return await _run("whatsapp.ax_chats", {"limit": limit}, timeout=READ_TIMEOUT_SECONDS)


async def read_recent(to: str, *, limit: int = 20) -> dict[str, Any]:
    outcome = await _run(
        "whatsapp.ax_read",
        {"to": to, "limit": limit},
        timeout=READ_TIMEOUT_SECONDS,
    )
    if not outcome.get("ok") or outcome.get("error"):
        error = str(outcome.get("error") or "helper_failed")
        return {
            "ok": False,
            "error": error,
            "spoken": _spoken_failure(to, error),
        }
    messages = outcome.get("messages") or []
    return {
        "ok": True,
        "to": str(outcome.get("to") or to),
        "messages": messages if isinstance(messages, list) else [],
        "focus_theft": 1 if outcome.get("focus_stolen") else 0,
        "focus_restored": bool(outcome.get("focus_restored", True)),
        "hidden_restored": bool(outcome.get("hidden_restored", True)),
    }


async def send(to: str, text: str) -> dict[str, Any]:
    """Send through the background app. Never claims sent without evidence."""

    body = (text or "").strip()
    target = (to or "").strip()
    if not body or not target:
        return {
            "ok": False,
            "sent": False,
            "channel": "whatsapp",
            "error": "empty_message",
            "spoken": "There's nothing to send.",
            "focus_theft": 0,
        }
    outcome = await _run(
        "whatsapp.ax_send",
        {"to": target, "text": body},
        timeout=SEND_TIMEOUT_SECONDS,
    )
    error = str(outcome.get("error") or "")
    focus_theft = 1 if outcome.get("focus_stolen") else 0
    if outcome.get("ok") and outcome.get("sent") is True and outcome.get("verified_in_thread") is True:
        display = str(outcome.get("to") or target)
        return {
            "ok": True,
            "sent": True,
            "channel": "whatsapp",
            "to": display,
            "verified_in_thread": True,
            "focus_theft": focus_theft,
            "focus_restored": bool(outcome.get("focus_restored", True)),
            "hidden_restored": bool(outcome.get("hidden_restored", True)),
            "driver": "desktop_ax",
            "spoken": f"Sent WhatsApp to {display}.",
        }
    if not outcome.get("ok"):
        error = error or str(outcome.get("error") or "helper_failed")
    if not error:
        error = "send_not_confirmed"
    return {
        "ok": False,
        "sent": False,
        "channel": "whatsapp",
        "to": target,
        "error": error,
        "candidates": list(outcome.get("candidates") or []),
        "focus_theft": focus_theft,
        "driver": "desktop_ax",
        "spoken": _spoken_failure(target, error, list(outcome.get("candidates") or [])),
    }
