"""Deterministic WhatsApp read/summarize fast path (cache-backed)."""

from __future__ import annotations


def test_is_whatsapp_turn_and_chat_name() -> None:
    from app.ev.whatsapp_flow import chat_name_from_text, is_whatsapp_turn

    assert is_whatsapp_turn("read my WhatsApp")
    assert is_whatsapp_turn("send a whatsapp to mom")
    assert not is_whatsapp_turn("check my mail")
    assert chat_name_from_text("read my whatsapp from Mom") == "Mom"
    assert chat_name_from_text("summarize my whatsapp") is None


def _set_egress(monkeypatch, allowed: bool) -> None:
    from app.compliance import policy

    monkeypatch.setattr(policy, "remote_processing_allowed", lambda *_a, **_k: allowed)


def _fake_local(monkeypatch, *, chats, by_query=None, messages=None):
    from app.ev.messaging import whatsapp_local

    async def fake_search(query: str = "", *, limit: int = 30):
        if query:
            return {"ok": True, "chats": (by_query or {}).get(query, [])[:limit]}
        return {"ok": True, "chats": list(chats)[:limit]}

    async def fake_read(name: str, *, limit: int = 20):
        rows = (messages or {}).get(name, [])
        if not rows:
            return {"ok": False, "error": "chat_not_found", "messages": []}
        return {"ok": True, "to": name, "messages": list(rows)[-limit:]}

    monkeypatch.setattr(whatsapp_local, "search_chats", fake_search)
    monkeypatch.setattr(whatsapp_local, "read_recent", fake_read)


async def test_whatsapp_read_speaks_real_messages(monkeypatch) -> None:
    from app.ev import whatsapp_flow
    from app.gateway import roles

    class _Presenter:
        text = "Latest from Mansi: Wru, after your Karde."

    async def fake_chat(messages, **kwargs):
        assert "Wru" in messages[-1].content
        return _Presenter()

    monkeypatch.setattr(roles, "chat_via_role", fake_chat)
    _set_egress(monkeypatch, True)
    _fake_local(
        monkeypatch,
        chats=[{"name": "Mansi"}, {"name": "Job"}],
        messages={
            "Mansi": [{"from_me": True, "body": "Karde"}, {"from_me": False, "body": "Wru"}],
            "Job": [{"from_me": False, "body": "Toh niche aao"}],
        },
    )
    result = await whatsapp_flow.handle_whatsapp_turn("Read my latest WhatsApp messages")
    assert result is not None
    assert result["kind"] == "whatsapp_read"
    assert "Mansi" in result["spoken"]
    assert "Wru" in result["spoken"]


async def test_whatsapp_named_read_resolves_off_top_list(monkeypatch) -> None:
    from app.ev import whatsapp_flow
    from app.gateway import roles

    captured: dict = {}

    async def fake_chat(messages, **kwargs):
        captured["prompt"] = messages[-1].content

        class _Presenter:
            text = "Suresh sent a reel and wants you to check it."

        return _Presenter()

    monkeypatch.setattr(roles, "chat_via_role", fake_chat)
    _set_egress(monkeypatch, True)
    _fake_local(
        monkeypatch,
        chats=[{"name": "Mansi"}],
        by_query={".Suresh": [{"name": ".Suresh"}]},
        messages={".Suresh": [{"from_me": False, "body": "Check this reel."}]},
    )
    result = await whatsapp_flow.handle_whatsapp_turn("Read my WhatsApp from .Suresh")
    assert result is not None
    assert "Check this reel." in captured["prompt"]
    assert "Suresh" in result["spoken"]


async def test_whatsapp_egress_denied_speaks_deterministic_fallback(monkeypatch) -> None:
    from app.ev import whatsapp_flow

    async def should_not_run(messages, **kwargs):
        raise AssertionError("message bodies must not leave the machine")

    from app.gateway import roles

    monkeypatch.setattr(roles, "chat_via_role", should_not_run)
    _set_egress(monkeypatch, False)
    _fake_local(
        monkeypatch,
        chats=[{"name": "Mansi"}],
        messages={
            "Mansi": [
                {"from_me": False, "body": "Wru"},
                {"from_me": True, "body": "On my way"},
            ]
        },
    )
    result = await whatsapp_flow.handle_whatsapp_turn("Read my WhatsApp from Mansi")
    assert result is not None
    assert "They said: Wru" in result["spoken"]
    assert "You said: On my way" in result["spoken"]


async def test_whatsapp_sendish_read_still_takes_fast_path(monkeypatch) -> None:
    from app.ev import whatsapp_flow
    from app.gateway import roles

    async def fake_chat(messages, **kwargs):
        class _Presenter:
            text = "Mansi says Wru."

        return _Presenter()

    monkeypatch.setattr(roles, "chat_via_role", fake_chat)
    _set_egress(monkeypatch, True)
    _fake_local(
        monkeypatch,
        chats=[{"name": "Mansi"}],
        messages={"Mansi": [{"from_me": False, "body": "Wru"}]},
    )
    result = await whatsapp_flow.handle_whatsapp_turn("tell me my latest WhatsApp messages")
    assert result is not None
    assert result["kind"] == "whatsapp_read"


async def test_whatsapp_summary_uses_mimo_with_fetched_text(monkeypatch) -> None:
    from app.contracts import ChatResult
    from app.ev import whatsapp_flow
    from app.gateway import roles

    _set_egress(monkeypatch, True)
    _fake_local(
        monkeypatch,
        chats=[{"name": "Mansi"}],
        messages={"Mansi": [{"from_me": False, "body": "Wru"}]},
    )
    captured: dict = {}

    async def fake_chat(messages, **kwargs):
        captured["prompt"] = messages[1].content
        return ChatResult(text="Mansi asked where you are.")

    monkeypatch.setattr(roles, "chat_via_role", fake_chat)
    result = await whatsapp_flow.handle_whatsapp_turn("Summarize my WhatsApp chats")
    assert result is not None
    assert result["kind"] == "whatsapp_summary"
    assert "Mansi" in result["spoken"]
    assert "Wru" in captured["prompt"]


async def test_whatsapp_send_is_left_to_the_approval_bus(monkeypatch) -> None:
    from app.ev import whatsapp_flow

    result = await whatsapp_flow.handle_whatsapp_turn("send a whatsapp to Mom saying hi")
    assert result is None


async def test_non_whatsapp_turn_returns_none(monkeypatch) -> None:
    from app.ev import whatsapp_flow

    assert await whatsapp_flow.handle_whatsapp_turn("what's on my calendar") is None
