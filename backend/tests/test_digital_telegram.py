"""Telegram Bot API adapter: send evidence, offset reads, honest failures."""

import pytest

from app.config import settings
from app.digital.adapters.telegram import TelegramAdapter
from app.digital.fabric import OpContext, execute
from app.digital.types import AutonomyLevel, OpStatus


class FakeTelegramTransport:
    def __init__(self, *, token="TESTTOKEN") -> None:
        self._token = token
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self._next_id = 100

    async def call(self, token, method, params):
        assert token == self._token, "wrong bot token used"
        self.calls.append((method, dict(params)))
        if method == "getUpdates":
            offset = params.get("offset") or 0
            pending = [u for u in self.updates if u["update_id"] >= offset]
            return {"ok": True, "result": pending}
        if method == "sendMessage":
            chat_id = params.get("chat_id")
            if chat_id in ("nope", -1):
                return {"ok": False, "description": "Bad Request: bot is not a member of the chat"}
            self._next_id += 1
            message = {"message_id": self._next_id, "chat": {"id": chat_id}, "text": params.get("text")}
            self.sent.append(message)
            return {"ok": True, "result": message}
        return {"ok": False, "description": f"unknown method {method}"}

    def add_message(self, update_id, *, chat_id=-100, text="hi", sender="rahul"):
        self.updates.append({
            "update_id": update_id,
            "message": {
                "message_id": update_id, "date": 1700000000,
                "chat": {"id": chat_id, "type": "group", "title": "Family"},
                "from": {"username": sender, "first_name": sender},
                "text": text,
            },
        })


@pytest.fixture()
def token_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "telegram_bot_token", "TESTTOKEN")
    monkeypatch.setattr(settings, "telegram_offset_file", str(tmp_path / "offset"))
    return tmp_path


def _ctx(transport):
    return OpContext(transport=transport, autonomy=AutonomyLevel.SAFE_DELEGATED, confirmed=True)


async def test_send_returns_message_id_evidence(token_env):
    transport = FakeTelegramTransport()
    result = await execute("telegram", "send", {"chat_id": -100, "text": "hi"}, ctx=_ctx(transport))
    assert result.status == OpStatus.COMPLETED_VERIFIED
    assert result.verification["sent"] is True
    assert result.verification["message_id"] == 101
    assert result.payload["sent"] is True


async def test_send_without_token_never_calls_http(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "telegram_bot_token", None)
    monkeypatch.setattr(settings, "telegram_offset_file", str(tmp_path / "offset"))
    transport = FakeTelegramTransport()
    result = await execute("telegram", "send", {"chat_id": -100, "text": "hi"}, ctx=_ctx(transport))
    assert result.status == OpStatus.SERVICE_AUTH_REQUIRED
    assert result.error == "telegram_token_missing"
    assert transport.calls == []


async def test_invalid_token_is_auth_required(token_env):
    class BadToken(FakeTelegramTransport):
        async def call(self, token, method, params):
            return {"ok": False, "description": "Unauthorized"}

    result = await execute("telegram", "send", {"chat_id": -100, "text": "hi"}, ctx=_ctx(BadToken()))
    assert result.status == OpStatus.SERVICE_AUTH_REQUIRED
    assert result.error == "telegram_token_invalid"


async def test_bot_not_member_is_named_not_generic(token_env):
    transport = FakeTelegramTransport()
    result = await execute("telegram", "send", {"chat_id": "nope", "text": "hi"}, ctx=_ctx(transport))
    assert result.status == OpStatus.FAILED
    assert result.error == "telegram_bot_not_member"
    assert result.payload["sent"] is False
    assert transport.sent == []


async def test_read_advances_offset_second_read_only_newer(token_env):
    transport = FakeTelegramTransport()
    transport.add_message(10, text="first")
    transport.add_message(11, text="second")
    first = await execute("telegram", "read", {}, ctx=_ctx(transport))
    assert [m["text"] for m in first.payload["messages"]] == ["first", "second"]
    assert first.payload["offset_advanced"] is True
    second = await execute("telegram", "read", {}, ctx=_ctx(transport))
    assert second.payload["messages"] == []
    assert (token_env / "offset").read_text() == "12"


async def test_chats_peeks_without_consuming(token_env):
    transport = FakeTelegramTransport()
    transport.add_message(10, text="first")
    chats = await execute("telegram", "chats", {}, ctx=_ctx(transport))
    assert chats.payload["chats"][0]["chat_title"] == "Family"
    assert chats.payload["offset_advanced"] is False
    read = await execute("telegram", "read", {}, ctx=_ctx(transport))
    assert [m["text"] for m in read.payload["messages"]] == ["first"]


async def test_reply_passes_reply_parameters(token_env):
    transport = FakeTelegramTransport()
    result = await execute(
        "telegram", "reply",
        {"chat_id": -100, "text": "ack", "reply_to_message_id": 55},
        ctx=_ctx(transport),
    )
    assert result.status == OpStatus.COMPLETED_VERIFIED
    assert transport.calls[0][1]["reply_parameters"] == {"message_id": 55}


async def test_token_never_leaks_into_outputs(token_env):
    transport = FakeTelegramTransport()
    result = await execute("telegram", "send", {"chat_id": "nope", "text": "hi"}, ctx=_ctx(transport))
    assert "TESTTOKEN" not in str(result.as_model())


async def test_unknown_operation_fails(token_env):
    transport = FakeTelegramTransport()
    result = await execute("telegram", "explode", {}, ctx=_ctx(transport))
    assert result.status == OpStatus.FAILED
    assert result.error == "unknown_capability"


def test_descriptors_advertise_bot_only_coverage():
    adapter = TelegramAdapter(connected=True)
    ops = {d.operation: d for d in adapter.descriptors()}
    assert set(ops) == {"read", "chats", "send", "reply"}
    assert ops["send"].requires_confirmation is True
    assert "bot-visible" in ops["read"].notes
