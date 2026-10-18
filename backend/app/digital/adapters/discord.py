"""Native Discord adapter — official bot API over a scoped bot token.

Reads channel history and posts messages with Discord message-id receipts.
The bot only sees guilds and channels it is invited to — DMs and uninvited
servers are invisible by platform design, and every result says what it
covered. Permission failures surface as ``scope_missing`` — never silent,
never bypassed. No user-token ("self-bot") automation anywhere: that violates
Discord ToS and will get the account banned.
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.types import Availability, OpStatus, Verb

_READ_OPS = frozenset({"read", "chats"})
_WRITE_OPS = frozenset({"send", "reply"})

# Discord error codes: https://discord.com/developers/docs/topics/opcodes-and-status-codes
_AUTH_CODES = frozenset({40002})  # verify_account / auth-shaped; 401 handled by status
_SCOPE_CODES = frozenset({50001, 50013})  # Missing Access, Missing Permissions
_MEMBERSHIP_CODES = frozenset({10003, 10004, 50001})  # Unknown Channel/Guild (+access)


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


class HttpxDiscordTransport:
    """Live bot API transport. Bot auth, REST JSON."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        self._timeout = timeout

    async def call(
        self, token: str, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        import httpx

        base = str(settings.discord_api_base or "https://discord.com/api/v10").rstrip("/")
        headers = {"Authorization": f"Bot {token}"}
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
            if method == "GET":
                response = await client.get(f"{base}{path}", params=payload or {})
            else:
                response = await client.post(f"{base}{path}", json=payload or {})
        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 429:
            retry = response.headers.get("Retry-After") or response.headers.get("retry-after")
            return {"ok": False, "status": 429, "retry_after": retry, "message": "rate_limited"}
        if not isinstance(body, (dict, list)):
            body = {"message": f"http_{response.status_code}"}
        if isinstance(body, list):
            return {"ok": response.status_code < 400, "status": response.status_code, "result": body}
        body = dict(body)
        body.setdefault("status", response.status_code)
        body.setdefault("ok", response.status_code < 400)
        return body


class DiscordAdapter:
    slug = "discord"
    display_name = "Discord"

    def __init__(self, *, connected: bool | None = None) -> None:
        self._connected_override = connected

    def _token(self) -> str:
        return str(settings.discord_bot_token or "")

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        if self._connected_override is not None:
            availability = (
                Availability.NATIVE if self._connected_override else Availability.CONNECTION_REQUIRED
            )
        else:
            availability = Availability.NATIVE  # capability exists; runtime may be CONNECTION_REQUIRED
        common: dict[str, Any] = dict(
            credential_type="discord_bot_token",
            data_classification="external_chat",
            backing="discord.com/api",
        )
        return [
            cap("discord", "read", Verb.READ, availability, read_write="read", risk="R0",
                verification_method="message_ids", notes="invited channels only", **common),
            cap("discord", "chats", Verb.LIST, availability, read_write="read", risk="R0",
                verification_method="channel_ids", notes="guild channels the bot can see", **common),
            cap("discord", "send", Verb.SEND, availability, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="discord_message_id", **common),
            cap("discord", "reply", Verb.REPLY, availability, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="discord_message_id", **common),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        token = self._token()
        if not token:
            return OpResult(
                status=OpStatus.SERVICE_AUTH_REQUIRED,
                service="discord",
                operation=operation,
                availability=Availability.CONNECTION_REQUIRED,
                error="discord_token_missing",
                diagnosis="token_missing",
                payload={"sent": False},
            )
        if operation not in _READ_OPS | _WRITE_OPS:
            return OpResult(
                status=OpStatus.FAILED, service="discord", operation=operation,
                availability=Availability.NATIVE, error="unknown_operation",
            )
        transport = ctx.transport or HttpxDiscordTransport()
        try:
            if operation == "read":
                return await self._read_history(transport, token, args)
            if operation == "chats":
                return await self._list_channels(transport, token, args)
            return await self._post_message(transport, token, operation, args)
        except Exception as exc:  # noqa: BLE001 - transport failures become honest FAILED
            return OpResult(
                status=OpStatus.FAILED,
                service="discord",
                operation=operation,
                availability=Availability.NATIVE,
                error="discord_api_error",
                diagnosis="transport_failed",
                payload={"sent": False, "detail": _scrub(str(exc), token)},
            )

    def _classified_failure(
        self, operation: str, token: str, body: dict[str, Any]
    ) -> OpResult:
        status = int(body.get("status") or 0)
        code = body.get("code")
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        message = _scrub(str(body.get("message") or "unknown"), token)
        if status == 401 or code in _AUTH_CODES:
            return OpResult(
                status=OpStatus.SERVICE_AUTH_REQUIRED, service="discord", operation=operation,
                availability=Availability.CONNECTION_REQUIRED,
                error="discord_token_invalid", diagnosis="token_invalid",
                payload={"sent": False},
            )
        if code in _SCOPE_CODES - _MEMBERSHIP_CODES or (code == 50001 and operation in _WRITE_OPS):
            return OpResult(
                status=OpStatus.FAILED, service="discord", operation=operation,
                availability=Availability.NATIVE,
                error="discord_scope_missing", diagnosis="scope_missing",
                payload={"sent": False, "discord_code": code},
            )
        if code in _MEMBERSHIP_CODES:
            return OpResult(
                status=OpStatus.FAILED, service="discord", operation=operation,
                availability=Availability.NATIVE,
                error="discord_channel_unavailable", diagnosis="chat_not_found",
                payload={"sent": False, "discord_code": code},
            )
        if status == 429:
            return OpResult(
                status=OpStatus.FAILED, service="discord", operation=operation,
                availability=Availability.NATIVE, error="discord_rate_limited",
                diagnosis="rate_limited",
                payload={"sent": False, "retry_after": body.get("retry_after")},
            )
        return OpResult(
            status=OpStatus.FAILED, service="discord", operation=operation,
            availability=Availability.NATIVE, error="discord_api_error",
            diagnosis="send_failed" if operation in _WRITE_OPS else "read_failed",
            payload={"sent": False, "detail": message},
        )

    async def _read_history(self, transport: Any, token: str, args: dict[str, Any]) -> OpResult:
        channel_id = args.get("channel") or args.get("channel_id")
        if not channel_id:
            return OpResult(status=OpStatus.CLARIFY, service="discord", operation="read",
                            availability=Availability.NATIVE, error="missing_channel",
                            payload={"sent": False})
        body = await transport.call(
            token, "GET", f"/channels/{channel_id}/messages", {"limit": int(args.get("limit") or 20)}
        )
        if not body.get("ok"):
            return self._classified_failure("read", token, body)
        messages = [
            {"id": m.get("id"), "author": (m.get("author") or {}).get("username"),
             "content": m.get("content"), "timestamp": m.get("timestamp")}
            for m in (body.get("result") or []) if isinstance(m, dict)
        ]
        return OpResult(
            status=OpStatus.COMPLETED_VERIFIED, service="discord", operation="read",
            availability=Availability.NATIVE,
            payload={"messages": messages, "channel_id": str(channel_id)},
            verification={"message_count": len(messages)},
            taint={"origin": "EXTERNAL_CONTENT", "authority": "DATA"},
        )

    async def _list_channels(self, transport: Any, token: str, args: dict[str, Any]) -> OpResult:
        guild_id = args.get("guild") or args.get("guild_id")
        if not guild_id:
            return OpResult(status=OpStatus.CLARIFY, service="discord", operation="chats",
                            availability=Availability.NATIVE, error="missing_guild",
                            payload={"sent": False})
        body = await transport.call(token, "GET", f"/guilds/{guild_id}/channels", {})
        if not body.get("ok"):
            return self._classified_failure("chats", token, body)
        channels = [
            {"id": c.get("id"), "name": c.get("name"), "type": c.get("type")}
            for c in (body.get("result") or []) if isinstance(c, dict)
        ]
        return OpResult(
            status=OpStatus.COMPLETED_VERIFIED, service="discord", operation="chats",
            availability=Availability.NATIVE,
            payload={"channels": channels, "guild_id": str(guild_id)},
            verification={"channel_count": len(channels)},
            taint={"origin": "EXTERNAL_CONTENT", "authority": "DATA"},
        )

    async def _post_message(
        self, transport: Any, token: str, operation: str, args: dict[str, Any]
    ) -> OpResult:
        channel_id = args.get("channel") or args.get("channel_id")
        text = str(args.get("text") or args.get("body") or "")
        if not channel_id:
            return OpResult(status=OpStatus.CLARIFY, service="discord", operation=operation,
                            availability=Availability.NATIVE, error="missing_channel",
                            payload={"sent": False})
        if not text.strip():
            return OpResult(status=OpStatus.CLARIFY, service="discord", operation=operation,
                            availability=Availability.NATIVE, error="missing_text",
                            payload={"sent": False})
        payload: dict[str, Any] = {"content": text}
        reply_to = args.get("reply_to_message_id")
        if operation == "reply" and reply_to:
            payload["message_reference"] = {"message_id": str(reply_to)}
        body = await transport.call(token, "POST", f"/channels/{channel_id}/messages", payload)
        if not body.get("ok"):
            return self._classified_failure(operation, token, body)
        message_id = body.get("id")
        if not message_id:
            return OpResult(
                status=OpStatus.FAILED, service="discord", operation=operation,
                availability=Availability.NATIVE, error="discord_no_delivery_evidence",
                diagnosis="missing_delivery_evidence",
                payload={"sent": False},
            )
        return OpResult(
            status=OpStatus.COMPLETED_VERIFIED, service="discord", operation=operation,
            availability=Availability.NATIVE,
            payload={"sent": True, "message_id": str(message_id), "channel_id": str(channel_id)},
            verification={"message_id": str(message_id), "sent": True},
        )
