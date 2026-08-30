"""Allowlisted Mac open/close actions through the existing EVLifeHelper.

POL Phase 3 software allowlist: open an approved https URL, or open/quit a
named owner app. This reuses the macos_life helper already used for messages.
It does not invent a fourth registry or a general automation agent.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.actuator import evidence_base
from app.integrations.life_helper import (
    LifeHelperError,
    LifeHelperUnavailableError,
    LifePermissionDeniedError,
    run_life_helper,
)
from app.models import Integration
from app.utils.text import utcnow

ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
MACOS_LIFE_ADAPTERS = ("messaging", "phone", "mail", "contacts")
PROTECTED_QUIT = frozenset(
    {
        "com.apple.finder",
        "com.apple.loginwindow",
        "com.ev.suit",
    }
)

ALLOWED_APPS: dict[str, str] = {
    "safari": "com.apple.Safari",
    "messages": "com.apple.MobileSMS",
    "mail": "com.apple.mail",
    "calendar": "com.apple.iCal",
    "finder": "com.apple.finder",
    "notes": "com.apple.Notes",
    "music": "com.apple.Music",
    "photos": "com.apple.Photos",
    "maps": "com.apple.Maps",
    "facetime": "com.apple.FaceTime",
    "reminders": "com.apple.reminders",
    "settings": "com.apple.systempreferences",
    "terminal": "com.apple.Terminal",
    "chrome": "com.google.Chrome",
    "arc": "company.thebrowser.Browser",
    "slack": "com.tinyspeck.slackmacgap",
    "spotify": "com.spotify.client",
    "textedit": "com.apple.TextEdit",
    "calculator": "com.apple.calculator",
    "cursor": "com.todesktop.230313mzl4w4u92",
    "vscode": "com.microsoft.VSCode",
    "code": "com.microsoft.VSCode",
}

APP_ALIASES: dict[str, str] = {
    "google chrome": "chrome",
    "google-chrome": "chrome",
    "imessage": "messages",
    "i message": "messages",
    "apple mail": "mail",
    "apple calendar": "calendar",
    "system settings": "settings",
    "system preferences": "settings",
    "sysprefs": "settings",
    "photos app": "photos",
    "text edit": "textedit",
    "text-edit": "textedit",
    "calc": "calculator",
    "vs code": "vscode",
    "visual studio code": "vscode",
    "browser": "safari",
}

_NOT_CONNECTED_STEP = (
    "install a macos_life messaging bridge and set EV_LIFE_HELPER_PATH "
    "(open-url bridge / apps.activate)"
)


def allowed_app_names() -> list[str]:
    return sorted(ALLOWED_APPS)


def display_app_name(name: str) -> str:
    special = {
        "facetime": "FaceTime",
        "imessage": "Messages",
        "messages": "Messages",
    }
    return special.get(name, name[:1].upper() + name[1:])


def resolve_app(raw: str) -> tuple[str, str] | None:
    key = re.sub(r"\s+", " ", (raw or "").strip().lower())
    if not key:
        return None
    key = APP_ALIASES.get(key, key)
    bundle = ALLOWED_APPS.get(key)
    if bundle:
        return key, bundle
    for name, identifier in ALLOWED_APPS.items():
        if identifier.lower() == key:
            return name, identifier
    return None


def parse_owner_url(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        return None
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return text


def is_macos_life_row(row: Integration | None) -> bool:
    if row is None or str(row.status or "") != "active":
        return False
    config = row.config if isinstance(row.config, dict) else {}
    return str(config.get("provider") or "").strip() == "macos_life"


def _row_helper_path(row: Integration | None) -> str | None:
    config = row.config if row is not None and isinstance(row.config, dict) else {}
    return str(config.get("helper_path") or "").strip() or None


def _executable_helper(path: str | None) -> str | None:
    text = (path or "").strip()
    if not text:
        return None
    if os.path.isfile(text) and os.access(text, os.X_OK):
        return text
    return None


def discover_life_helper_path() -> str | None:
    """Resolve a real EVLifeHelper binary. Missing file is not a path."""

    candidates = [
        str(settings.life_helper_path or "").strip(),
        str(os.environ.get("EV_LIFE_HELPER_PATH") or "").strip(),
        os.path.join(os.getcwd(), ".build", "debug", "EVLifeHelper"),
        os.path.join(os.getcwd(), "macos", ".build", "debug", "EVLifeHelper"),
    ]
    seen: set[str] = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        found = _executable_helper(path)
        if found:
            return found
    return None


def _prefer_helper_row(rows: list[Integration]) -> Integration | None:
    if not rows:
        return None

    def rank(row: Integration) -> tuple[int, int]:
        adapter = str(row.adapter or "")
        try:
            adapter_rank = MACOS_LIFE_ADAPTERS.index(adapter)
        except ValueError:
            adapter_rank = len(MACOS_LIFE_ADAPTERS)
        has_helper = 0 if _executable_helper(_row_helper_path(row)) else 1
        return (has_helper, adapter_rank)

    return sorted(rows, key=rank)[0]


def macos_life_from_rows(provider_rows: dict[str, Integration]) -> Integration | None:
    rows = []
    for slug in MACOS_LIFE_ADAPTERS:
        row = provider_rows.get(slug)
        if is_macos_life_row(row):
            rows.append(row)
    return _prefer_helper_row(rows)


async def find_macos_life_integration(session: AsyncSession) -> Integration | None:
    rows = [
        row
        for row in (
            await session.execute(
                select(Integration)
                .where(Integration.status == "active")
                .order_by(Integration.created_at.asc())
            )
        )
        .scalars()
        .all()
        if is_macos_life_row(row)
    ]
    chosen = _prefer_helper_row(rows)
    if chosen is None:
        return None
    path = helper_path_for(chosen)
    if path and _row_helper_path(chosen) != path:
        config = dict(chosen.config) if isinstance(chosen.config, dict) else {}
        config["helper_path"] = path
        chosen.config = config
    return chosen


def helper_path_for(row: Integration | None) -> str | None:
    return _executable_helper(_row_helper_path(row)) or discover_life_helper_path()


def _unavailable(reason: str, *, next_step: str | None = None) -> dict:
    step = next_step or _NOT_CONNECTED_STEP
    return {
        "ok": False,
        "degraded": True,
        "next_step": step,
        "reason": reason,
        "error": reason,
        "spoken": f"I couldn't do that yet. {step}",
    }


def _ok(*, spoken: str, source: str, **payload: object) -> dict:
    now = utcnow()
    return {
        "ok": True,
        "spoken": spoken,
        "evidence": evidence_base(source=source, accepted=True, observed=True, now=now),
        **payload,
    }


async def open_url(session: AsyncSession, args: dict, *, actor: str) -> dict:
    del actor
    url = parse_owner_url(str(args.get("url") or ""))
    if url is None:
        return {
            "ok": False,
            "error": "invalid_url",
            "spoken": "I can only open http or https links.",
        }
    row = await find_macos_life_integration(session)
    path = helper_path_for(row)
    if row is None or not path:
        return _unavailable("no open-url bridge is installed")
    try:
        result = await run_life_helper("open.url", {"url": url}, helper_path=path)
    except LifeHelperUnavailableError as exc:
        return _unavailable("open-url bridge is unavailable", next_step=str(exc))
    except LifePermissionDeniedError as exc:
        return _unavailable("open-url permission denied", next_step=str(exc))
    except LifeHelperError as exc:
        return _unavailable("open-url bridge failed", next_step=str(exc))
    if result.data.get("opened") is not True:
        return _unavailable(
            "open-url bridge returned no open evidence",
            next_step="EVLifeHelper must set data.opened=true",
        )
    return _ok(
        spoken=f"Opened {url}.",
        source="macos_life",
        url=url,
        opened=True,
    )


async def open_app(session: AsyncSession, args: dict, *, actor: str) -> dict:
    del actor
    resolved = resolve_app(str(args.get("name") or args.get("app") or ""))
    if resolved is None:
        names = ", ".join(allowed_app_names())
        return {
            "ok": False,
            "error": "not_allowlisted",
            "spoken": f"I can only open {names}.",
            "allowlist": allowed_app_names(),
        }
    name, bundle_id = resolved
    row = await find_macos_life_integration(session)
    path = helper_path_for(row)
    if row is None or not path:
        return _unavailable("no open-url bridge is installed")
    try:
        result = await run_life_helper(
            "apps.activate",
            {"bundle_id": bundle_id, "name": name},
            helper_path=path,
        )
    except LifeHelperUnavailableError as exc:
        return _unavailable("app bridge is unavailable", next_step=str(exc))
    except LifePermissionDeniedError as exc:
        return _unavailable("app permission denied", next_step=str(exc))
    except LifeHelperError as exc:
        return _unavailable("app bridge failed", next_step=str(exc))
    if result.data.get("activated") is not True:
        return _unavailable(
            "app bridge returned no activation evidence",
            next_step="EVLifeHelper must set data.activated=true",
        )
    return _ok(
        spoken=f"Opened {display_app_name(name)}.",
        source="macos_life",
        name=name,
        bundle_id=bundle_id,
        opened=True,
        launched=bool(result.data.get("launched")),
    )


async def close_app(session: AsyncSession, args: dict, *, actor: str) -> dict:
    del actor
    resolved = resolve_app(str(args.get("name") or args.get("app") or ""))
    if resolved is None:
        names = ", ".join(allowed_app_names())
        return {
            "ok": False,
            "error": "not_allowlisted",
            "spoken": f"I can only close {names}.",
            "allowlist": allowed_app_names(),
        }
    name, bundle_id = resolved
    if bundle_id.lower() in PROTECTED_QUIT:
        return {
            "ok": False,
            "error": "protected",
            "spoken": f"I won't quit {display_app_name(name)}.",
        }
    row = await find_macos_life_integration(session)
    path = helper_path_for(row)
    if row is None or not path:
        return _unavailable("no open-url bridge is installed")
    try:
        result = await run_life_helper(
            "apps.quit",
            {"bundle_id": bundle_id, "name": name},
            helper_path=path,
        )
    except LifeHelperUnavailableError as exc:
        return _unavailable("app bridge is unavailable", next_step=str(exc))
    except LifePermissionDeniedError as exc:
        return _unavailable("app permission denied", next_step=str(exc))
    except LifeHelperError as exc:
        return _unavailable("app bridge failed", next_step=str(exc))
    if result.data.get("quit") is not True:
        return _unavailable(
            "app bridge returned no quit evidence",
            next_step="EVLifeHelper must set data.quit=true",
        )
    if result.data.get("already_closed"):
        spoken = f"{display_app_name(name)} wasn't open."
    else:
        spoken = f"Closed {display_app_name(name)}."
    return _ok(
        spoken=spoken,
        source="macos_life",
        name=name,
        bundle_id=bundle_id,
        closed=True,
        already_closed=bool(result.data.get("already_closed")),
    )
