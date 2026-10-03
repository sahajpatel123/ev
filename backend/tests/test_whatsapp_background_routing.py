"""Owner-facing WhatsApp operations use the linked background connection."""
from __future__ import annotations

import pytest

from app.digital.tools import handle_digital_tool
from app.ev import tools
from app.ev.confirm import pol_meta
from app.ev.messaging import whatsapp_web
from app.ev.messaging.approval import latest_pending
from app.ev.messaging.routing import RouteBinding, route_channel


async def _linked(**_kwargs):
    return True


async def _john(_to):
    return {"status": "unique", "display": "John Smith", "chat_ref": "John Smith"}


def _forbid_foreground(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Background WhatsApp must never open an app or use a foreground/helper route")

    for attr in ("_open_whatsapp_compose", "_open_whatsapp_app", "_bring_whatsapp_forward", "_resolve_send_destination"):
        monkeypatch.setattr(tools, attr, forbidden)
    monkeypatch.setattr("app.ev.messaging.whatsapp_desktop.available", forbidden)
    monkeypatch.setattr("app.ev.messaging.whatsapp_desktop.send", forbidden)
    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", forbidden)


async def test_linked_send_without_native_helper_preserves_approved_identity(monkeypatch):
    _forbid_foreground(monkeypatch)
    monkeypatch.setattr(whatsapp_web, "web_available", _linked)
    monkeypatch.setattr("app.ev.apps.discover_life_helper_path", lambda: None)
    monkeypatch.setattr("app.services.life_stream_daemon.life_stream_should_run", lambda: False)
    sent = []

    async def send(to, text):
        sent.append((to, text))
        return {"ok": True, "sent": True, "verified_in_thread": True, "to": to, "focus_theft": 0}

    monkeypatch.setattr(whatsapp_web, "send", send)
    binding = RouteBinding.of(route_channel("whatsapp", helper_available=False, web_available=True))
    result = await tools._mac_hub_life_write(
        "send_message", {"to": "John", "text": "hello", "channel": "whatsapp"},
        approved_route=binding, approved_address="John Smith",
    )
    assert result["sent"] is True
    assert result["focus_theft"] == 0
    assert sent == [("John Smith", "hello")]


async def test_disconnected_send_never_opens_windows_or_falls_back(monkeypatch):
    _forbid_foreground(monkeypatch)

    async def unavailable(**_kwargs):
        return False

    monkeypatch.setattr(whatsapp_web, "web_available", unavailable)
    result = await tools._send_via_helper(
        {"to": "John", "text": "hello", "channel": "whatsapp"}, helper_path="/tmp/helper",
    )
    assert result["ok"] is False
    assert result["error"] == "whatsapp_background_unavailable"
    assert not result.get("sent")


async def test_explicit_disconnected_whatsapp_still_requires_approval_gate():
    assert await tools._whatsapp_send_needs_approval(
        "send_message", {"to": "John", "text": "hello", "channel": "whatsapp"},
    )


async def test_digital_model_confirmation_cannot_send_and_ticket_is_session_bound(db_session, monkeypatch):
    _forbid_foreground(monkeypatch)
    monkeypatch.setattr(whatsapp_web, "web_available", _linked)
    monkeypatch.setattr(whatsapp_web, "resolve", _john)
    result = await handle_digital_tool(
        db_session, "digital_act",
        {"service": "whatsapp", "operation": "reply", "confirmed": True,
         "args": {"chat_ref": "John", "text": "hello"}},
        actor="master", live_session_id="owner-voice-session",
    )
    assert result["pending_approval"] is True
    assert result["error"] == "confirmation_required"
    assert result["sent"] is False
    pending = await latest_pending(db_session, live_session_id="owner-voice-session")
    meta = pol_meta(pending.payload)
    assert meta["route"]["provider"] == "web"
    assert meta["address"] == "John Smith"
    assert meta.get("live_session_id") == "owner-voice-session"


async def test_digital_compose_is_a_draft_and_never_a_parked_send(db_session, monkeypatch):
    from app.digital.fabric import OpResult
    from app.digital.types import Availability, OpStatus

    calls = []

    async def execute(service, operation, args, *, ctx):
        calls.append((service, operation, args))
        assert ctx.confirmed is False
        return OpResult(status=OpStatus.PREPARED, service=service, operation=operation,
                        availability=Availability.OPERATED, payload={"prepared": True, "sent": False})

    monkeypatch.setattr("app.digital.tools.execute", execute)
    result = await handle_digital_tool(
        db_session, "digital_act", {"service": "whatsapp", "operation": "compose",
                                    "args": {"chat_ref": "John", "text": "hello"}}, actor="master",
    )
    assert result["status"] == "PREPARED"
    assert result["payload"]["sent"] is False
    assert calls[0][:2] == ("whatsapp", "compose")
    assert await latest_pending(db_session) is None


async def test_real_digital_factory_reads_background_connection_not_mac_copy(db_session, monkeypatch):
    from app.ev.messaging import whatsapp_cdp

    async def status():
        return {"authenticated": True, "background": True, "focus_theft": 0}

    async def search(query, **_kwargs):
        return {"ok": True, "chats": [{"name": "John Smith", "chat_ref": "John Smith"}]}

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("WhatsApp must not read the stale Desktop Mac hub")

    monkeypatch.setattr(whatsapp_cdp, "status", status)
    monkeypatch.setattr(whatsapp_cdp, "search_chats", search)
    monkeypatch.setattr("app.digital.orchestrate._mac_whatsapp_read", forbidden)
    result = await handle_digital_tool(
        db_session, "digital_act", {"service": "whatsapp", "operation": "search_chats", "args": {}}, actor="master",
    )
    assert result["ok"] is True
    assert result["payload"]["chats"][0]["name"] == "John Smith"
    assert result["authority"] == "DATA"


async def test_live_whatsapp_read_does_not_require_mac_stream_or_open_app(monkeypatch):
    from app.digital.fabric import OpResult
    from app.digital.types import Availability, OpStatus

    _forbid_foreground(monkeypatch)
    monkeypatch.setattr("app.services.life_stream_daemon.life_stream_should_run", lambda: False)
    calls = []

    async def execute(service, operation, args, *, ctx):
        calls.append((service, operation, args))
        return OpResult(status=OpStatus.COMPLETED_VERIFIED, service=service, operation=operation,
                        availability=Availability.OPERATED, payload={"chats": [{"name": "John"}]})

    monkeypatch.setattr("app.digital.fabric.execute", execute)
    result = await tools._mac_hub_life_read("list_messages", {"query": "latest WhatsApp messages"})
    assert result["source"] == "background_whatsapp"
    assert result["history_complete"] is False
    assert result["count"] == 1
    assert calls[0][:2] == ("whatsapp", "search_chats")


@pytest.mark.parametrize("operation", ["forward", "delete"])
async def test_unsupported_destructive_operations_do_not_become_sends(db_session, operation):
    result = await handle_digital_tool(
        db_session, "digital_act", {"service": "whatsapp", "operation": operation,
                                    "args": {"chat_ref": "John", "text": "hello"}}, actor="master",
    )
    assert result["ok"] is False
    assert result.get("pending_approval") is not True
    assert await latest_pending(db_session) is None


@pytest.mark.parametrize("query", ["read WhatsApp", "fetch WhatsApp messages", "summarize my latest WhatsApp chat", "understand the conversation on WhatsApp"])
def test_whatsapp_read_intents_select_live_background_path(query):
    from app.ev.tool_select import select_tool

    assert select_tool(query).selected == "list_messages"


async def test_internal_helper_cannot_bypass_bound_whatsapp_approval(monkeypatch):
    _forbid_foreground(monkeypatch)
    monkeypatch.setattr(whatsapp_web, "web_available", _linked)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("No send before bound owner approval")

    monkeypatch.setattr(whatsapp_web, "send", forbidden)
    result = await tools._send_via_helper(
        {"to": "John", "text": "hello", "channel": "whatsapp", "confirm": True}, helper_path=None,
    )
    assert result["sent"] is False
    assert result["error"] == "confirmation_required"


@pytest.mark.parametrize("utterance", ["read my WhatsApp", "Send John a WhatsApp saying hello"])
async def test_kernel_whatsapp_never_runs_legacy_desktop_preempt(db_session, monkeypatch, utterance):
    import time
    from types import SimpleNamespace

    from app.cognitive import kernel

    async def no_explain(*_args, **_kwargs):
        return None

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Legacy WhatsApp flow must never run")

    monkeypatch.setattr(kernel, "_dispatch_kernel_explain", no_explain)
    monkeypatch.setattr("app.ev.whatsapp_flow.handle_whatsapp_turn", forbidden)
    result = await kernel._dispatch_kernel_code(
        db_session, utterance, cognition=SimpleNamespace(prepare_only=False),
        actor="master", live_session_id="test-owner", modality="text",
        steering_seen=0, started=time.perf_counter(),
    )
    assert result is None


def test_short_worker_turns_offer_background_app_connector():
    from app.cognitive.speed import tool_specs_for_turn

    assert "digital.act" in {spec.name for spec in tool_specs_for_turn(compact=True)}


async def test_attachment_send_never_silently_becomes_text_only(db_session):
    result = await handle_digital_tool(
        db_session, "digital_act", {"service": "whatsapp", "operation": "send",
            "args": {"chat_ref": "John", "text": "here is the file", "attachment": "/tmp/example.pdf"}},
        actor="master",
    )
    assert result["error"] == "whatsapp_attachment_unsupported"
    assert result["sent"] is False
    assert await latest_pending(db_session) is None


@pytest.mark.parametrize("operation, action_id, expected_kind", [
    ("reply", "server-issued-approved-action", "send_approval"),
    ("read_thread", "untrusted-content-id", "muse"),
    ("reply", None, "muse"),
])
async def test_worker_returns_exact_approval_question_without_another_model_round(db_session, monkeypatch, tmp_path, operation, action_id, expected_kind):
    from app.cognitive import kernel
    from app.cognitive.session_store import reset_for_tests
    from app.config import settings
    from app.contracts import ChatResult, ToolCall

    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")
    monkeypatch.setattr(settings, "cognitive_role", "kernel")
    monkeypatch.setattr(settings, "laptop_files", False)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    question = "Should I send hello to John on WhatsApp?"

    class Provider:
        calls = 0

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls > 1:
                return ChatResult(text="Read result returned.")
            return ChatResult(text="", tool_calls=[ToolCall(
                id="approval-test", name="digital.act",
                arguments={"service": "whatsapp", "operation": operation,
                           "args": {"chat_ref": "John", "text": "hello"}},
            )])

    async def parked(*_args, **_kwargs):
        return {"ok": False, "pending_approval": True, "spoken": question, "sent": False,
                "action_id": action_id}

    provider = Provider()
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: provider)
    monkeypatch.setattr(kernel, "execute_semantic", parked)
    reset_for_tests()
    try:
        result = await kernel.handle_turn(
            transcript="Review the latest WhatsApp discussion with John and prepare a suitable reply",
            modality="text", session=db_session,
        )
        assert result.kind == expected_kind
        if expected_kind == "send_approval":
            assert result.spoken == question
            assert result.evidence[0]["sent"] is False
            assert provider.calls == 1
        else:
            assert result.spoken == "Read result returned."
            assert provider.calls == 2
    finally:
        reset_for_tests()


async def test_live_background_read_preserves_full_chat_name(monkeypatch):
    from app.digital.fabric import OpResult
    from app.digital.types import Availability, OpStatus

    calls = []

    async def execute(service, operation, args, *, ctx):
        calls.append((service, operation, args))
        return OpResult(status=OpStatus.COMPLETED_VERIFIED, service=service, operation=operation,
                        availability=Availability.OPERATED, payload={"messages": []})

    monkeypatch.setattr("app.digital.fabric.execute", execute)
    await tools._mac_hub_life_read("list_messages", {"query": "read the latest messages from John Smith on WhatsApp"})
    assert calls == [("whatsapp", "read_thread", {"chat_ref": "John Smith", "limit": 8})]
