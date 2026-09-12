"""EVLifeHelper subprocess runner and its exact wire contract.

Agent 18 (SUIT) owns the Swift binary. This module is the backend half of the
contract implemented in ``macos/Sources/EVLifeHelper/main.swift``:

- CLI: ``EVLifeHelper <command> [--flag value ...]``
- commands: ``contacts.list | contacts.resolve --query | messages.list
  [--limit N] | messages.send --to --text [--service auto|imessage|sms] |
  whatsapp.send --to --text |
  mail.list [--limit N] |
  mail.send --to --subject --body | call.place --destination [--kind
  tel|facetime] | call.check | apps.frontmost | apps.activate |
  apps.quit | open.url``
- stdout: one JSON object (UTF-8). stderr is human diagnostics only.
- success envelope: ``{"ok": true, "data": {...}}``
- error envelope: ``{"ok": false, "error": {"code": "...", "message": "..."}}``
- exit codes: 0 ok · 1 generic failure · 3 permission denied (never success)
  · 4 not available · 5 bad arguments.
- delivery evidence: ``messages.send`` / ``mail.send`` must set
  ``data.sent == true``, and ``call.place`` must set ``data.opened == true``.
  ``data.dry_run == true`` is NOT delivery. Without real confirmation the
  backend refuses to report success. ``whatsapp.send`` confirms
  ``data.opened == true`` (WhatsApp compose UI). It does not claim the
  message was tapped Send.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from app.config import settings

logger = logging.getLogger("ev.integrations.life_helper")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PERMISSION_DENIED = 3
EXIT_NOT_AVAILABLE = 4
EXIT_BAD_ARGUMENTS = 5

DELIVERY_COMMANDS = {
    "messages.send",
    "mail.send",
    "call.place",
    "whatsapp.send",
}

CONFIRMATION_FIELD = {
    "messages.send": "sent",
    "mail.send": "sent",
    "call.place": "opened",
    "whatsapp.send": "opened",
}

COMMAND_FLAGS: dict[str, tuple[tuple[str, str], ...]] = {
    "contacts.list": (),
    "contacts.resolve": (("query", "--query"),),
    "contacts.create": (("name", "--name"), ("phone", "--phone"), ("email", "--email"), ("company", "--company")),
    "contacts.update": (("id", "--id"), ("query", "--query"), ("name", "--name"), ("phone", "--phone"), ("email", "--email"), ("company", "--company")),
    "messages.list": (("limit", "--limit"),),
    "messages.send": (("to", "--to"), ("text", "--text"), ("service", "--service")),
    "whatsapp.send": (("to", "--to"), ("text", "--text")),
    "mail.list": (("limit", "--limit"),),
    "mail.send": (("to", "--to"), ("subject", "--subject"), ("body", "--body")),
    "call.place": (("destination", "--destination"), ("kind", "--kind")),
    "call.check": (("destination", "--destination"), ("kind", "--kind")),
    "apps.frontmost": (),
    "apps.list": (("query", "--query"), ("running", "--running")),
    "apps.activate": (("bundle_id", "--bundle-id"), ("name", "--name")),
    "apps.quit": (("bundle_id", "--bundle-id"), ("name", "--name")),
    "open.url": (("url", "--url"),),
}

MAX_ARGS_BYTES = 200_000  # argv is bounded; message bodies stay well under


class LifeHelperUnavailableError(Exception):
    """The helper binary is missing or EV_LIFE_HELPER_PATH is not set."""


class LifePermissionDeniedError(Exception):
    """Helper exit 3 / permission_denied: TCC or entitlement missing."""

    def __init__(self, message: str, permission: str | None = None) -> None:
        super().__init__(message)
        self.permission = permission


class LifeHelperError(Exception):
    """Helper failed, returned invalid output, or timed out."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.error_code = error_code


class LifeTimeoutError(LifeHelperError):
    pass


class AmbiguousRecipientError(Exception):
    """A name matched more than one distinct contact. Never guess.

    ``candidates`` are short human labels ("John Smith · +1555…, John Doe")
    the caller must read back so the owner can pick. Transports catch this
    and speak a clarification instead of sending to the first match.
    """

    def __init__(self, recipient: str, candidates: list[str]) -> None:
        self.recipient = recipient
        self.candidates = list(candidates or [])
        names = ", ".join(self.candidates[:4]) or "no details"
        super().__init__(
            f"I found more than one contact for {recipient}: {names}. "
            "Which one?"
        )


@dataclass(frozen=True)
class LifeHelperResult:
    command: str
    data: dict
    delivery: dict


def _build_argv(command: str, args: dict) -> list[str]:
    flags = COMMAND_FLAGS.get(command)
    if flags is None:
        raise LifeHelperError(
            f"EVLifeHelper command '{command}' is not supported by the contract",
            error_code="unsupported_command",
        )
    argv = [command]
    for key, flag in flags:
        value = args.get(key)
        if value is None:
            continue
        argv.extend([flag, str(value)])
    return argv


def _argv_bytes(argv: list[str]) -> int:
    return sum(len(part.encode("utf-8")) for part in argv)


def resolve_helper_path(helper_path: str | None = None) -> str:
    """A usable EVLifeHelper, preferring the caller's path but never trusting it blind.

    An integration row keeps its own ``helper_path`` in config. That value goes
    stale the moment the app is rebuilt, moved, or reinstalled — and a stale
    path used to take the entire bridge down with "EVLifeHelper is not an
    executable file", even though a perfectly good binary was configured in
    ``EV_LIFE_HELPER_PATH``. Every life bridge (mail, messages, contacts, calls)
    goes through this one resolver, so a stale row can no longer kill the
    feature: the configured default is used instead, and the substitution is
    recorded so it is visible rather than silent.
    """

    candidates = (
        str(helper_path or "").strip(),
        str(getattr(settings, "life_helper_path", "") or "").strip(),
        str(os.environ.get("EV_LIFE_HELPER_PATH") or "").strip(),
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            if candidate != candidates[0] and candidates[0]:
                logger.warning(
                    "life helper path in config is stale (%r); using %r instead",
                    candidates[0],
                    candidate,
                )
            return candidate
    attempted = ", ".join(repr(c) for c in candidates if c)
    if not attempted:
        raise LifeHelperUnavailableError(
            "EV_LIFE_HELPER_PATH is not set; configure the EVLifeHelper binary "
            "(see docs/INTEGRATIONS.md § Life bridges)"
        )
    raise LifeHelperUnavailableError(
        f"no executable EVLifeHelper found; tried {attempted}"
    )


async def run_life_helper(
    command: str,
    args: dict,
    *,
    helper_path: str | None = None,
    timeout: float | None = None,
) -> LifeHelperResult:
    """Exec EVLifeHelper once and parse its JSON envelope.

    Raises :class:`LifePermissionDeniedError` on exit 3, and
    :class:`LifeHelperError` / :class:`LifeTimeoutError` on any other failure.
    Never returns success without helper-confirmed delivery evidence for
    send/call commands.
    """
    path = resolve_helper_path(helper_path)
    argv = _build_argv(command, args)
    if _argv_bytes(argv) > MAX_ARGS_BYTES:
        raise LifeHelperError("life helper arguments exceed the size limit")
    effective_timeout = timeout if timeout is not None else settings.life_helper_timeout_seconds
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), effective_timeout)
    except TimeoutError as exc:
        if proc is not None:
            proc.kill()
            await proc.wait()
        raise LifeTimeoutError(
            f"EVLifeHelper timed out after {effective_timeout}s",
            exit_code=None,
            error_code="timeout",
        ) from exc
    if len(stdout) > settings.life_helper_max_output_bytes:
        raise LifeHelperError(
            "EVLifeHelper output exceeds the size limit",
            exit_code=proc.returncode,
            error_code="output_too_large",
        )
    exit_code = proc.returncode
    try:
        envelope = json.loads(stdout.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifeHelperError(
            "EVLifeHelper returned non-JSON output",
            exit_code=exit_code,
            error_code="invalid_json",
        ) from exc
    if not isinstance(envelope, dict):
        raise LifeHelperError(
            "EVLifeHelper returned a non-object envelope",
            exit_code=exit_code,
            error_code="invalid_json",
        )

    raw_error = envelope.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    error_code = str(error.get("code") or "")[:64]
    error_message = str(error.get("message") or "")[:512]

    if exit_code == EXIT_PERMISSION_DENIED or error_code == "permission_denied":
        raise LifePermissionDeniedError(
            "Apple life permission denied by EVLifeHelper"
            ": grant the requested permission in System Settings → "
            "Privacy & Security (see docs/INTEGRATIONS.md § Life bridges)",
        )
    if exit_code != EXIT_OK:
        detail = error_message or f"exit code {exit_code}"
        raise LifeHelperError(
            f"EVLifeHelper failed ({error_code or 'exit ' + str(exit_code)}): {detail}",
            exit_code=exit_code,
            error_code=error_code or "failed",
        )
    if not envelope.get("ok"):
        raise LifeHelperError(
            f"EVLifeHelper reported failure: {error_message or 'ok=false'}",
            exit_code=exit_code,
            error_code=error_code or "failed",
        )

    delivery = envelope.get("delivery")
    if not isinstance(delivery, dict):
        delivery = {}
    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {}
    if command in DELIVERY_COMMANDS:
        confirmation_field = CONFIRMATION_FIELD[command]
        confirmed = data.get(confirmation_field) is True
        dry_run = data.get("dry_run") is True
        if not confirmed or dry_run:
            raise LifeHelperError(
                "EVLifeHelper returned ok without real delivery evidence"
                + (" (dry_run is not delivery)" if dry_run else "")
                + "; "
                "refusing to report success",
                exit_code=exit_code,
                error_code="missing_delivery_evidence",
            )
        delivery = {
            "confirmed": True,
            "evidence": {
                "provider": "EVLifeHelper",
                "confirmed_by": confirmation_field,
                **{
                    key: value
                    for key, value in data.items()
                    if key in ("to", "destination", "kind", "subject", "sent", "opened")
                },
            },
        }
    return LifeHelperResult(command=command, data=data, delivery=delivery)


def life_provider_configured() -> bool:
    return bool(settings.life_helper_path)
