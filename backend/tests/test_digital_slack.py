"""Slack Web API adapter: history reads, ts receipts, classified failures."""

import pytest

from app.config import settings
from app.digital.adapters.slack import SlackAdapter
from app.digital.fabric import OpContext, execute
from app.digital.types import AutonomyLevel, OpStatus


class FakeSlackTransport:
    def __init__(self, *, token="xoxb-test") -> None:
        self._token = token
        self.calls: list[tuple[str, dict]] = []
        self.posts: list[dict] = []

    async def call(self, token, method, params):
        assert token == self._token, "wrong bot token used"
        self.calls.append((method, dict(params)))
        if method == "conversations.history":
            if params.get("channel") == "nope":
                return {"ok": False, "error": "channel_not_found"}
            return {"ok": True, "messages": [
                {"ts": "1700000001.001", "user": "U1", "text": "standup at 9"},
                {"ts": "1700000002.002", "user": "U2", "text": "ack", "thread_ts": "1700000001.001"},
            ]}
        if method == "conversations.list":
            return {"ok": True, "channels": [
                {"id": "C1", "name": "general", "is_im": False},
                {"id": "D1", "name": None, "is_im": True},
            ]}
        if method == "chat.postMessage":
            if params.get("channel") == "nope":
                return {"ok": False, "error": "not_in_channel"}
            if params.get("channel") == "locked":
                return {"ok": False, "error": "restricted_action"}
            post = {"channel": params["channel"], "text": params["text"],
                    "thread_ts": params.get("thread_ts")}
            self.posts.append(post)
            return {"ok": True, "channel": params["channel"], "ts": "1700000099.099"}
        return {"ok": False, "error": "unknown_method"}


@pytest.fixture()
def token_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    return tmp_path


def _ctx(transport):
    return OpContext(transport=transport, autonomy=AutonomyLevel.SAFE_DELEGATED, confirmed=True)


async def test_send_returns_ts_receipt(token_env):
    transport = FakeSlackTransport()
    result = await execute("slack", "send", {"channel": "C1", "text": "hi"}, ctx=_ctx(transport))
    assert result.status == OpStatus.COMPLETED_VERIFIED
    assert result.verification == {"ts": "1700000099.099", "sent": True}
    assert result.payload["sent"] is True


async def test_send_without_token_never_calls_http(monkeypatch):
    monkeypatch.setattr(settings, "slack_bot_token", None)
    transport = FakeSlackTransport()
    result = await execute("slack", "send", {"channel": "C1", "text": "hi"}, ctx=_ctx(transport))
    assert result.status == OpStatus.SERVICE_AUTH_REQUIRED
    assert result.error == "slack_token_missing"
    assert transport.calls == []


async def test_restricted_channel_is_scope_missing(token_env):
    transport = FakeSlackTransport()
    result = await execute("slack", "send", {"channel": "locked", "text": "hi"}, ctx=_ctx(transport))
    assert result.status == OpStatus.FAILED
    assert result.error == "slack_scope_missing"
    assert result.diagnosis == "scope_missing"
    assert result.payload["sent"] is False
    assert transport.posts == []


async def test_unknown_channel_is_named(token_env):
    transport = FakeSlackTransport()
    result = await execute("slack", "send", {"channel": "nope", "text": "hi"}, ctx=_ctx(transport))
    assert result.error == "slack_channel_unavailable"
    assert result.diagnosis == "chat_not_found"


async def test_read_returns_history_with_thread_markers(token_env):
    transport = FakeSlackTransport()
    result = await execute("slack", "read", {"channel": "C1", "limit": 5}, ctx=_ctx(transport))
    assert result.status == OpStatus.COMPLETED_VERIFIED
    assert [m["text"] for m in result.payload["messages"]] == ["standup at 9", "ack"]
    assert result.payload["messages"][1]["thread_ts"] == "1700000001.001"


async def test_chats_lists_channels(token_env):
    transport = FakeSlackTransport()
    result = await execute("slack", "chats", {}, ctx=_ctx(transport))
    assert result.verification == {"channel_count": 2}
    assert result.payload["channels"][0]["name"] == "general"


async def test_reply_threads_by_ts(token_env):
    transport = FakeSlackTransport()
    result = await execute(
        "slack", "reply",
        {"channel": "C1", "text": "ack", "thread_ts": "1700000001.001"},
        ctx=_ctx(transport),
    )
    assert result.status == OpStatus.COMPLETED_VERIFIED
    assert transport.calls[0][1]["thread_ts"] == "1700000001.001"


async def test_token_never_leaks_into_outputs(token_env):
    transport = FakeSlackTransport()
    result = await execute("slack", "send", {"channel": "nope", "text": "hi"}, ctx=_ctx(transport))
    assert "xoxb-test" not in str(result.as_model())


def test_descriptors_require_confirmation_for_writes():
    adapter = SlackAdapter(connected=True)
    ops = {d.operation: d for d in adapter.descriptors()}
    assert set(ops) == {"read", "chats", "send", "reply"}
    assert ops["send"].requires_confirmation is True
    assert ops["read"].requires_confirmation is False
