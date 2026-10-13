"""Native Telegram adapter — official Bot API over a server-side token.

A bot only sees chats it is added to: groups/channels with the bot as a
member, plus 1:1 chats the owner starts with the bot. It can NEVER read the
owner's personal DM history — that boundary is enforced by Telegram, not by
Evie, and every read result says which chats it covers so the owner is never
misled into thinking coverage is complete.

Reads poll ``getUpdates`` with a persisted offset (``~/.ev/telegram-offset``);
``chats`` peeks the same window without advancing it. Sends go through
``sendMessage`` and require the returned ``message_id`` as delivery evidence.
The token lives in server env (``EV_TELEGRAM_BOT_TOKEN``) and never appears
in payloads, errors, or logs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.config import settings
from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.types import Availability, OpStatus, Verb

_READ_OPS = frozenset({"read", "chats"})
_WRITE_OPS = frozenset({"send", "reply"})


def _offset_path() -> Path:
    return Path(str(settings.telegram_offset_file or "~/.ev/telegram-offset")).expanduser()


def _read_offset() -> int:
    try:
        return int(_offset_path().read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_offset(update_id: int) -> None:
    path = _offset_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(str(int(update_id)), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError:
        # Offset persistence is best-effort: losing it re-delivers updates,
        # it never loses or fabricates them.
        pass


def _scrub(obj: Any, token: str) -> Any:
    if not token:
        return obj
    if isinstance(obj, str):
        return obj.replace(token, "<redacted>")
    if isinstance(obj, dict):
        return {key: _scrub(value, token) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_scrub(value, token) for value in obj]
    return obj


def _shaped_message(message: dict[str, Any]) -> dict[str, Any]:
    _chat: Any = message.get("chat")
    _sender: Any = message.get("from")
    chat: dict[str, Any] = _chat if isinstance(_chat, dict) else {}
    sender: dict[str, Any] = _sender if isinstance(_sender, dict) else {}
    return {
        "message_id": message.get("message_id"),
        "date": message.get("date"),
        "chat_id": chat.get("id"),
        "chat_type": chat.get("type"),
        "chat_title": chat.get("title") or chat.get("username") or chat.get("first_name"),
        "sender": sender.get("username") or sender.get("first_name"),
        "text": message.get("text") or message.get("caption") or "",
    }


class HttpxTelegramTransport:
    """Live Bot API transport. One method call per request, token in path."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        self._timeout = timeout

    async def call(self, token: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        import httpx

        base = str(settings.telegram_api_base or "https://api.telegram.org").rstrip("/")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{base}/bot{token}/{method}", json=params)
        try:
            body = response.json()
        except ValueError:
            body = {"ok": False, "description": f"HTTP {response.status_code}"}
        if not isinstance(body, dict):
            body = {"ok": False, "description": f"HTTP {response.status_code}"}
        body.setdefault("http_status", response.status_code)
        return body


class TelegramAdapter:
    slug = "telegram"
    display_name = "Telegram"

    def __init__(self, *, connected: bool | None = None) -> None:
        self._connected_override = connected

    def _token(self) -> str:
        return str(settings.telegram_bot_token or "")

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        if self._connected_override is not None:
            availability = (
                Availability.NATIVE if self._connected_override else Availability.CONNECTION_REQUIRED
            )
        else:
            availability = Availability.NATIVE  # capability exists; runtime may be CONNECTION_REQUIRED
        common: dict[str, Any] = dict(
            credential_type="telegram_bot_token",
            data_classification="external_chat",
            backing="api.telegram.org",
        )
        return [
            cap("telegram", "read", Verb.READ, availability, read_write="read", risk="R0",
                verification_method="update_ids", notes="bot-visible chats only", **common),
            cap("telegram", "chats", Verb.LIST, availability, read_write="read", risk="R0",
                verification_method="chat_ids", notes="peek; does not advance offset", **common),
            cap("telegram", "send", Verb.SEND, availability, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="telegram_message_id", **common),
            cap("telegram", "reply", Verb.REPLY, availability, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="telegram_message_id", **common),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        token = self._token()
        if not token:
            return OpResult(
                status=OpStatus.SERVICE_AUTH_REQUIRED,
                service="telegram",
                operation=operation,
                availability=Availability.CONNECTION_REQUIRED,
                error="telegram_token_missing",
                diagnosis="token_missing",
                payload={"sent": False},
            )
        if operation not in _READ_OPS | _WRITE_OPS:
            return OpResult(
                status=OpStatus.FAILED, service="telegram", operation=operation,
                availability=Availability.NATIVE, error="unknown_operation",
            )
        transport = ctx.transport or HttpxTelegramTransport()
        try:
            if operation in {"read", "chats"}:
                return await self._read_updates(
                    transport, token, operation, args, advance=(operation == "read")
                )
            return await self._send_message(transport, token, operation, args)
        except Exception as exc:  # noqa: BLE001 - transport failures become honest FAILED
            return OpResult(
                status=OpStatus.FAILED,
                service="telegram",
                operation=operation,
                availability=Availability.NATIVE,
                error="telegram_api_error",
                diagnosis="transport_failed",
                payload={"sent": False, "detail": _scrub(str(exc), token)},
            )

    async def _api(
        self, transport: Any, token: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if hasattr(transport, "call"):
            return await transport.call(token, method, params)
        # Test seam: request(method, url, ...) like the Gmail fake transport.
        base = str(settings.telegram_api_base or "https://api.telegram.org").rstrip("/")
        response = await transport.request(
            "POST", f"{base}/bot{token}/{method}", json_body=params
        )
        body = response.json() if hasattr(response, "json") else response
        return body if isinstance(body, dict) else {"ok": False, "description": "bad_response"}

    def _auth_failure(
        self, operation: str, description: str
    ) -> OpResult | None:
        lowered = str(description or "").lower()
        if "unauthorized" in lowered or "not found" in lowered and "bot" in lowered:
            return OpResult(
                status=OpStatus.SERVICE_AUTH_REQUIRED,
                service="telegram",
                operation=operation,
                availability=Availability.CONNECTION_REQUIRED,
                error="telegram_token_invalid",
                diagnosis="token_invalid",
                payload={"sent": False},
            )
        return None

    async def _read_updates(
        self,
        transport: Any,
        token: str,
        operation: str,
        args: dict[str, Any],
        *,
        advance: bool,
    ) -> OpResult:
        offset = _read_offset()
        body = await self._api(
            transport, token, "getUpdates",
            {"offset": offset or None, "timeout": 0, "allowed_updates": ["message", "channel_post"]},
        )
        if not body.get("ok"):
            auth = self._auth_failure(operation, str(body.get("description") or ""))
            if auth is not None:
                return auth
            return OpResult(
                status=OpStatus.FAILED, service="telegram", operation=operation,
                availability=Availability.NATIVE, error="telegram_api_error",
                diagnosis="read_failed",
                payload={"detail": _scrub(str(body.get("description") or "unknown"), token)},
            )
        _updates: Any = body.get("result")
        updates: list[Any] = _updates if isinstance(_updates, list) else []
        messages: list[dict[str, Any]] = []
        update_ids: list[int] = []
        chats: dict[Any, dict[str, Any]] = {}
        for update in updates:
            if not isinstance(update, dict):
                continue
            if isinstance(update.get("update_id"), int):
                update_ids.append(update["update_id"])
            message = update.get("message") or update.get("channel_post")
            if not isinstance(message, dict):
                continue
            shaped = _shaped_message(message)
            messages.append(shaped)
            if shaped["chat_id"] is not None:
                chats.setdefault(shaped["chat_id"], {
                    "chat_id": shaped["chat_id"],
                    "chat_type": shaped["chat_type"],
                    "chat_title": shaped["chat_title"],
                })
        if advance and update_ids:
            _write_offset(max(update_ids) + 1)
        limit = int(args.get("limit") or 20)
        if operation == "chats":
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED, service="telegram", operation=operation,
                availability=Availability.NATIVE,
                payload={"chats": list(chats.values())[:limit], "offset_advanced": False},
                verification={"chat_count": len(chats)},
                taint={"origin": "EXTERNAL_CONTENT", "authority": "DATA"},
            )
        return OpResult(
            status=OpStatus.COMPLETED_VERIFIED, service="telegram", operation=operation,
            availability=Availability.NATIVE,
            payload={"messages": messages[:limit], "update_ids": update_ids,
                     "offset_advanced": bool(advance and update_ids)},
            verification={"update_ids": update_ids},
            taint={"origin": "EXTERNAL_CONTENT", "authority": "DATA"},
        )

    async def _send_message(
        self, transport: Any, token: str, operation: str, args: dict[str, Any]
    ) -> OpResult:
        chat_id = args.get("chat_id") or args.get("to")
        text = str(args.get("text") or args.get("body") or "")
        if chat_id is None or chat_id == "":
            return OpResult(status=OpStatus.CLARIFY, service="telegram", operation=operation,
                            availability=Availability.NATIVE, error="missing_recipient",
                            payload={"sent": False})
        if not text.strip():
            return OpResult(status=OpStatus.CLARIFY, service="telegram", operation=operation,
                            availability=Availability.NATIVE, error="missing_text",
                            payload={"sent": False})
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        reply_to = args.get("reply_to_message_id")
        if operation == "reply" and reply_to is not None:
            params["reply_parameters"] = {"message_id": reply_to}
        body = await self._api(transport, token, "sendMessage", params)
        if not body.get("ok"):
            description = str(body.get("description") or "")
            auth = self._auth_failure(operation, description)
            if auth is not None:
                return auth
            lowered = description.lower()
            if "not a member" in lowered or "chat not found" in lowered or "bot was blocked" in lowered:
                return OpResult(
                    status=OpStatus.FAILED, service="telegram", operation=operation,
                    availability=Availability.NATIVE, error="telegram_bot_not_member",
                    diagnosis="bot_not_member",
                    payload={"sent": False, "detail": _scrub(description, token)},
                )
            return OpResult(
                status=OpStatus.FAILED, service="telegram", operation=operation,
                availability=Availability.NATIVE, error="telegram_api_error",
                diagnosis="send_failed",
                payload={"sent": False, "detail": _scrub(description or "unknown", token)},
            )
        _send_result: Any = body.get("result")
        result: dict[str, Any] = _send_result if isinstance(_send_result, dict) else {}
        message_id = result.get("message_id")
        if not message_id:
            return OpResult(
                status=OpStatus.FAILED, service="telegram", operation=operation,
                availability=Availability.NATIVE, error="telegram_no_delivery_evidence",
                diagnosis="missing_delivery_evidence",
                payload={"sent": False},
            )
        _sent_chat: Any = result.get("chat")
        sent_chat_id = _sent_chat.get("id") if isinstance(_sent_chat, dict) else chat_id
        return OpResult(
            status=OpStatus.COMPLETED_VERIFIED, service="telegram", operation=operation,
            availability=Availability.NATIVE,
            payload={"sent": True, "message_id": message_id, "chat_id": sent_chat_id},
            verification={"message_id": message_id, "sent": True},
        )
