"""Discord bot adapter: channel reads, message-id receipts, classified failures."""

import pytest

from app.config import settings
from app.digital.adapters.discord import DiscordAdapter
from app.digital.fabric import OpContext, execute
from app.digital.types import AutonomyLevel, OpStatus


class FakeDiscordTransport:
    def __init__(self, *, token="discord-test-token") -> None:
        self._token = token
        self.calls: list[tuple[str, str, dict]] = []
        self.posts: list[dict] = []

    async def call(self, token, method, path, payload=None):
        assert token == self._token, "wrong bot token used"
        payload = payload or {}
        self.calls.append((method, path, dict(payload)))
        if method == "GET" and path.endswith("/messages"):
            if "/nope/" in path:
                return {"ok": False, "status": 404, "code": 10003, "message": "Unknown Channel"}
            return {"ok": True, "status": 200, "result": [
                {"id": "m1", "author": {"username": "ann"}, "content": "deploy at 5?", "timestamp": "2026-01-01T00:00:00Z"},
                {"id": "m2", "author": {"username": "bob"}, "content": "ship it", "timestamp": "2026-01-01T00:01:00Z"},
            ]}
        if method == "GET" and "/channels" in path:
            return {"ok": True, "status": 200, "result": [
                {"id": "c1", "name": "general", "type": 0},
                {"id": "c2", "name": "random", "type": 0},
            ]}
        if method == "POST":
            if "/locked/" in path:
                return {"ok": False, "status": 403, "code": 50013, "message": "Missing Permissions"}
            post = {"channel": path, **payload}
            self.posts.append(post)
            return {"ok": True, "status": 200, "id": "m99"}
        return {"ok": False, "status": 400, "message": "bad request"}


@pytest.fixture()
def token_env(monkeypatch):
    monkeypatch.setattr(settings, "discord_bot_token", "discord-test-token")


def _ctx(transport):
    return OpContext(transport=transport, autonomy=AutonomyLevel.SAFE_DELEGATED, confirmed=True)


async def test_send_returns_message_id_receipt(token_env):
    transport = FakeDiscordTransport()
    result = await execute("discord", "send", {"channel": "c1", "text": "hi"}, ctx=_ctx(transport))
    assert result.status == OpStatus.COMPLETED_VERIFIED
    assert result.verification == {"message_id": "m99", "sent": True}
    assert result.payload["sent"] is True


async def test_send_without_token_never_calls_http(monkeypatch):
    monkeypatch.setattr(settings, "discord_bot_token", None)
    transport = FakeDiscordTransport()
    result = await execute("discord", "send", {"channel": "c1", "text": "hi"}, ctx=_ctx(transport))
    assert result.status == OpStatus.SERVICE_AUTH_REQUIRED
    assert result.error == "discord_token_missing"
    assert transport.calls == []


async def test_locked_channel_is_scope_missing(token_env):
    transport = FakeDiscordTransport()
    result = await execute("discord", "send", {"channel": "locked", "text": "hi"}, ctx=_ctx(transport))
    assert result.status == OpStatus.FAILED
    assert result.error == "discord_scope_missing"
    assert result.diagnosis == "scope_missing"
    assert result.payload["sent"] is False
    assert transport.posts == []


async def test_unknown_channel_is_named(token_env):
    transport = FakeDiscordTransport()
    result = await execute("discord", "read", {"channel": "nope"}, ctx=_ctx(transport))
    assert result.error == "discord_channel_unavailable"
    assert result.diagnosis == "chat_not_found"


async def test_read_returns_messages(token_env):
    transport = FakeDiscordTransport()
    result = await execute("discord", "read", {"channel": "c1", "limit": 5}, ctx=_ctx(transport))
    assert result.status == OpStatus.COMPLETED_VERIFIED
    assert [m["content"] for m in result.payload["messages"]] == ["deploy at 5?", "ship it"]


async def test_chats_needs_guild_and_list_channels(token_env):
    transport = FakeDiscordTransport()
    missing = await execute("discord", "chats", {}, ctx=_ctx(transport))
    assert missing.error == "missing_guild"
    result = await execute("discord", "chats", {"guild": "g1"}, ctx=_ctx(transport))
    assert result.verification == {"channel_count": 2}


async def test_reply_threads_by_reference(token_env):
    transport = FakeDiscordTransport()
    result = await execute(
        "discord", "reply",
        {"channel": "c1", "text": "ack", "reply_to_message_id": "m1"},
        ctx=_ctx(transport),
    )
    assert result.status == OpStatus.COMPLETED_VERIFIED
    assert transport.calls[0][2]["message_reference"] == {"message_id": "m1"}


async def test_token_never_leaks_into_outputs(token_env):
    transport = FakeDiscordTransport()
    result = await execute("discord", "read", {"channel": "nope"}, ctx=_ctx(transport))
    assert "discord-test-token" not in str(result.as_model())


def test_descriptors_require_confirmation_for_writes():
    adapter = DiscordAdapter(connected=True)
    ops = {d.operation: d for d in adapter.descriptors()}
    assert set(ops) == {"read", "chats", "send", "reply"}
    assert ops["send"].requires_confirmation is True
    assert ops["read"].requires_confirmation is False
