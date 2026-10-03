"""Legacy WhatsApp Desktop names routed to the headless background workspace.

Helper discovery remains available for diagnostic compatibility. Public
WhatsApp operations never use Accessibility, launch the Desktop app, or
request system permissions; caller policy still authorizes each send.
"""

from __future__ import annotations

import logging
import os
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
    """Compatibility alias. Desktop AX no longer grants WhatsApp capability."""
    from app.ev.messaging import whatsapp_cdp

    return await whatsapp_cdp.available(refresh=refresh)


async def list_chats(limit: int = 30) -> dict[str, Any]:
    from app.ev.messaging import whatsapp_cdp

    return await whatsapp_cdp.search_chats("", limit=limit)


async def read_recent(to: str, *, limit: int = 20) -> dict[str, Any]:
    from app.ev.messaging import whatsapp_cdp

    return await whatsapp_cdp.read_recent(to, limit=limit)


async def send(to: str, text: str) -> dict[str, Any]:
    """Compatibility alias for already-authorized sends; never foreground AX.

    Policy/approval remains the caller's responsibility. No app is activated,
    no Accessibility permission dialog is requested, and no fallback runs.
    """
    from app.ev.messaging import whatsapp_cdp

    return await whatsapp_cdp.send(to, text)
