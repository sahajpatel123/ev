"""Native Slack adapter — official Web API over a workspace bot token.

Reads channel history and posts messages with ``ts`` receipts. Reads never
mark anything read (history fetches are side-effect free). Admin/DLP blocks
surface as ``scope_missing``/``admin_block`` — never silent, never bypassed.
No user-token automation anywhere: bot token only.

The token lives in server env (``EV_SLACK_BOT_TOKEN``) and never appears in
payloads, errors, or logs.
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.types import Availability, OpStatus, Verb

_READ_OPS = frozenset({"read", "chats"})
_WRITE_OPS = frozenset({"send", "reply"})

_AUTH_ERRORS = frozenset({"invalid_auth", "token_revoked", "account_inactive", "token_expired"})
_SCOPE_ERRORS = frozenset({"missing_scope", "restricted_action", "admin_restricted", "ekm_blocked"})
_MEMBERSHIP_ERRORS = frozenset({"channel_not_found", "not_in_channel", "is_archived", "is_private"})


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


class HttpxSlackTransport:
    """Live Web API transport. Bearer auth, one method call per request."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        self._timeout = timeout

    async def call(self, token: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        import httpx

        base = str(settings.slack_api_base or "https://slack.com/api").rstrip("/")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{base}/{method}",
                json=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        try:
            body = response.json()
        except ValueError:
            body = {"ok": False, "error": f"http_{response.status_code}"}
        if not isinstance(body, dict):
            body = {"ok": False, "error": f"http_{response.status_code}"}
        body.setdefault("http_status", response.status_code)
        return body


class SlackAdapter:
    slug = "slack"
    display_name = "Slack"

    def __init__(self, *, connected: bool | None = None) -> None:
        self._connected_override = connected

    def _token(self) -> str:
        return str(settings.slack_bot_token or "")

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        if self._connected_override is not None:
            availability = (
                Availability.NATIVE if self._connected_override else Availability.CONNECTION_REQUIRED
            )
        else:
            availability = Availability.NATIVE  # capability exists; runtime may be CONNECTION_REQUIRED
        common = dict(
            credential_type="slack_bot_token",
            data_classification="external_chat",
            backing="slack.com/api",
        )
        return [
            cap("slack", "read", Verb.READ, availability, read_write="read", risk="R0",
                verification_method="message_ts", notes="history fetch; marks nothing read", **common),
            cap("slack", "chats", Verb.LIST, availability, read_write="read", risk="R0",
                verification_method="channel_ids", **common),
            cap("slack", "send", Verb.SEND, availability, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="slack_ts", **common),
            cap("slack", "reply", Verb.REPLY, availability, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="slack_ts", **common),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        token = self._token()
        if not token:
            return OpResult(
                status=OpStatus.SERVICE_AUTH_REQUIRED,
                service="slack",
                operation=operation,
                availability=Availability.CONNECTION_REQUIRED,
                error="slack_token_missing",
                diagnosis="token_missing",
                payload={"sent": False},
            )
        if operation not in _READ_OPS | _WRITE_OPS:
            return OpResult(
                status=OpStatus.FAILED, service="slack", operation=operation,
                availability=Availability.NATIVE, error="unknown_operation",
            )
        transport = ctx.transport or HttpxSlackTransport()
        try:
            if operation == "read":
                return await self._read_history(transport, token, args)
            if operation == "chats":
                return await self._list_channels(transport, token, args)
            return await self._post_message(transport, token, operation, args)
        except Exception as exc:  # noqa: BLE001 - transport failures become honest FAILED
            return OpResult(
                status=OpStatus.FAILED,
                service="slack",
                operation=operation,
                availability=Availability.NATIVE,
                error="slack_api_error",
                diagnosis="transport_failed",
                payload={"sent": False, "detail": _scrub(str(exc), token)},
            )

    async def _api(
        self, transport: Any, token: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        result = await transport.call(token, method, params)
        return result if isinstance(result, dict) else {"ok": False, "error": "bad_response"}

    def _classified_failure(
        self, operation: str, token: str, error: str
    ) -> OpResult:
        code = str(error or "unknown_error")
        if code in _AUTH_ERRORS:
            return OpResult(
                status=OpStatus.SERVICE_AUTH_REQUIRED, service="slack", operation=operation,
                availability=Availability.CONNECTION_REQUIRED,
                error="slack_token_invalid", diagnosis="token_invalid",
                payload={"sent": False},
            )
        if code in _SCOPE_ERRORS:
            return OpResult(
                status=OpStatus.FAILED, service="slack", operation=operation,
                availability=Availability.NATIVE,
                error="slack_scope_missing", diagnosis="scope_missing",
                payload={"sent": False, "slack_error": code},
            )
        if code in _MEMBERSHIP_ERRORS:
            return OpResult(
                status=OpStatus.FAILED, service="slack", operation=operation,
                availability=Availability.NATIVE,
                error="slack_channel_unavailable", diagnosis="chat_not_found",
                payload={"sent": False, "slack_error": code},
            )
        return OpResult(
            status=OpStatus.FAILED, service="slack", operation=operation,
            availability=Availability.NATIVE, error="slack_api_error",
            diagnosis="send_failed" if operation in _WRITE_OPS else "read_failed",
            payload={"sent": False, "detail": _scrub(code, token)},
        )

    async def _read_history(self, transport: Any, token: str, args: dict[str, Any]) -> OpResult:
        channel = args.get("channel") or args.get("channel_id")
        if not channel:
            return OpResult(status=OpStatus.CLARIFY, service="slack", operation="read",
                            availability=Availability.NATIVE, error="missing_channel",
                            payload={"sent": False})
        body = await self._api(
            transport, token, "conversations.history",
            {"channel": channel, "limit": int(args.get("limit") or 20)},
        )
        if not body.get("ok"):
            return self._classified_failure("read", token, str(body.get("error") or ""))
        messages = [
            {"ts": m.get("ts"), "user": m.get("user"), "text": m.get("text"), "thread_ts": m.get("thread_ts")}
            for m in (body.get("messages") or []) if isinstance(m, dict)
        ]
        return OpResult(
            status=OpStatus.COMPLETED_VERIFIED, service="slack", operation="read",
            availability=Availability.NATIVE,
            payload={"messages": messages, "channel": channel},
            verification={"message_count": len(messages)},
            taint={"origin": "EXTERNAL_CONTENT", "authority": "DATA"},
        )

    async def _list_channels(self, transport: Any, token: str, args: dict[str, Any]) -> OpResult:
        body = await self._api(
            transport, token, "conversations.list",
            {"types": "public_channel,private_channel,im,mpim", "limit": int(args.get("limit") or 100)},
        )
        if not body.get("ok"):
            return self._classified_failure("chats", token, str(body.get("error") or ""))
        channels = [
            {"id": c.get("id"), "name": c.get("name"), "is_im": bool(c.get("is_im"))}
            for c in (body.get("channels") or []) if isinstance(c, dict)
        ]
        return OpResult(
            status=OpStatus.COMPLETED_VERIFIED, service="slack", operation="chats",
            availability=Availability.NATIVE,
            payload={"channels": channels},
            verification={"channel_count": len(channels)},
            taint={"origin": "EXTERNAL_CONTENT", "authority": "DATA"},
        )

    async def _post_message(
        self, transport: Any, token: str, operation: str, args: dict[str, Any]
    ) -> OpResult:
        channel = args.get("channel") or args.get("channel_id")
        text = str(args.get("text") or args.get("body") or "")
        if not channel:
            return OpResult(status=OpStatus.CLARIFY, service="slack", operation=operation,
                            availability=Availability.NATIVE, error="missing_channel",
                            payload={"sent": False})
        if not text.strip():
            return OpResult(status=OpStatus.CLARIFY, service="slack", operation=operation,
                            availability=Availability.NATIVE, error="missing_text",
                            payload={"sent": False})
        params: dict[str, Any] = {"channel": channel, "text": text}
        thread_ts = args.get("thread_ts")
        if operation == "reply" and thread_ts:
            params["thread_ts"] = thread_ts
        body = await self._api(transport, token, "chat.postMessage", params)
        if not body.get("ok"):
            return self._classified_failure(operation, token, str(body.get("error") or ""))
        ts = body.get("ts")
        if not ts:
            return OpResult(
                status=OpStatus.FAILED, service="slack", operation=operation,
                availability=Availability.NATIVE, error="slack_no_delivery_evidence",
                diagnosis="missing_delivery_evidence",
                payload={"sent": False},
            )
        return OpResult(
            status=OpStatus.COMPLETED_VERIFIED, service="slack", operation=operation,
            availability=Availability.NATIVE,
            payload={"sent": True, "ts": ts, "channel": body.get("channel") or channel},
            verification={"ts": ts, "sent": True},
        )
