"""Native WhatsApp transport: AX wrapper, desktop routing, ticket vocabulary, preflight."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.cognitive.graph import TaskNode, WorkerReceipt
from app.cognitive.supervisor import VerdictNext, local_verdict, supervise
from app.cognitive.worker import WorkerCtx, _probe_whatsapp_route, run_node
from app.db import SessionLocal
from app.ev.messaging import whatsapp_ax
from app.ev.messaging.approval import graph_ticket_call
from app.ev.messaging.routing import RouteBinding, route_channel
from app.integrations.life_helper import (
    LifeHelperError,
    LifeHelperUnavailableError,
    LifePermissionDeniedError,
)


def _node(**overrides) -> TaskNode:
    base = {"id": "n1", "label": "Send it", "detail": "Send it.", "tier": "D",
            "tool": "life.send",
            "arguments": {"to": "Mom", "text": "hi", "channel": "whatsapp"}}
    base.update(overrides)
    return TaskNode.model_validate(base)


def _ok_helper(data: dict):
    async def fake(command, args, **kwargs):
        return SimpleNamespace(command=command, data=dict(data), delivery={})

    return fake


# --------------------------------------------------------------------------- #
# Chat matching: whole-token only
# --------------------------------------------------------------------------- #


def test_match_exact_name():
    chats = [{"name": "Mom"}, {"name": "Mom Smith"}]
    assert whatsapp_ax.match_ax_chat(chats, "Mom") == {"name": "Mom"}


def test_match_whole_token_multiword():
    chats = [{"name": "Mom Smith"}, {"name": "Dad"}]
    assert whatsapp_ax.match_ax_chat(chats, "mom smith") == {"name": "Mom Smith"}


def test_match_rejects_substring():
    chats = [{"name": "Johnson"}, {"name": "Dad"}]
    assert whatsapp_ax.match_ax_chat(chats, "John") is None


def test_match_reports_ambiguity():
    chats = [{"name": "Mom Smith"}, {"name": "Mom Jones"}]
    out = whatsapp_ax.match_ax_chat(chats, "Mom")
    assert out is not None and sorted(out["ambiguous"]) == ["Mom Jones", "Mom Smith"]


def test_match_empty_query_is_none():
    assert whatsapp_ax.match_ax_chat([{"name": "Mom"}], "  ") is None


# --------------------------------------------------------------------------- #
# Status probe + send wrapper over a faked helper
# --------------------------------------------------------------------------- #


async def test_ax_status_reachable(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.life_helper.run_life_helper",
        _ok_helper({"installed": True, "running": True, "hidden": True,
                    "accessibility_trusted": True, "chat_count": 12,
                    "composer_available": False}))
    status = await whatsapp_ax.ax_status(refresh=True)
    assert status.reachable is True
    assert status.reason == ""
    assert status.hidden is True


async def test_ax_status_names_missing_grant(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.life_helper.run_life_helper",
        _ok_helper({"installed": True, "accessibility_trusted": False}))
    status = await whatsapp_ax.ax_status(refresh=True)
    assert status.reachable is False
    assert status.reason == "accessibility_not_granted"


async def test_ax_status_helper_down_is_unreachable(monkeypatch):
    async def boom(command, args, **kwargs):
        raise LifeHelperUnavailableError("no binary")

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", boom)
    status = await whatsapp_ax.ax_status(refresh=True)
    assert status.reachable is False
    assert status.reason.startswith("helper_unavailable")


async def test_ax_send_success_receipt(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.life_helper.run_life_helper",
        _ok_helper({"to": "Mom", "sent": True, "verified_in_thread": True,
                    "chat_verified": True, "focus_stolen": False,
                    "focus_restored": True, "hidden_restored": True}))
    out = await whatsapp_ax.ax_send(to="Mom", text="hi")
    assert out["ok"] is True and out["sent"] is True
    assert out["verified_in_thread"] is True
    assert out["focus_restored"] is True
    assert out["retry_safe"] is False


async def test_ax_send_ambiguity_returns_candidates(monkeypatch):
    async def fake(command, args, **kwargs):
        raise LifeHelperError(
            "no evidence", error_code="missing_delivery_evidence",
            data={"error": "ambiguous_chat", "candidates": ["Mom S", "Mom J"]})

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", fake)
    out = await whatsapp_ax.ax_send(to="Mom", text="hi")
    assert out["error"] == "ax_ambiguous_chat"
    assert out["candidates"] == ["Mom S", "Mom J"]


async def test_ax_send_unconfirmed_never_retries(monkeypatch):
    async def fake(command, args, **kwargs):
        raise LifeHelperError(
            "no evidence", error_code="missing_delivery_evidence",
            data={"error": "send_not_confirmed", "composer_cleared": True})

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", fake)
    out = await whatsapp_ax.ax_send(to="Mom", text="hi")
    assert out["error"] == "ax_send_unconfirmed"
    assert out["retry_safe"] is False
    assert "twice" in out["spoken"]


async def test_ax_send_permission_names_setup(monkeypatch):
    async def fake(command, args, **kwargs):
        raise LifePermissionDeniedError("grant Accessibility to EVLifeHelper")

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", fake)
    out = await whatsapp_ax.ax_send(to="Mom", text="hi")
    assert out["error"] == "ax_permission_denied"
    assert "Accessibility" in out["spoken"]


# --------------------------------------------------------------------------- #
# Ticket vocabulary: semantic tools become executable dispatch calls
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("life.send", {"to": "Mom", "text": "hi"},
         ("send_message", {"to": "Mom", "text": "hi"})),
        ("life.send", {"to": "a@b.c", "text": "hi", "channel": "mail", "subject": "s"},
         ("send_mail", {"to": "a@b.c", "body": "hi", "subject": "s"})),
        ("phone.call", {"op": "call", "contact": "Mom"},
         ("place_call", {"destination": "Mom", "kind": "tel"})),
        ("phone.call", {"op": "facetime", "contact": "Mom"},
         ("place_call", {"destination": "Mom", "kind": "facetime"})),
        ("phone.call", {"op": "message", "contact": "Mom", "message": "hi"},
         ("send_message", {"to": "Mom", "text": "hi"})),
        ("computer.perform_effect", {"effect": "open Music"},
         ("computer", {"goal": "open Music"})),
        ("digital.act", {"service": "whatsapp", "operation": "send",
                         "args": {"to": "Mom", "text": "hi"}},
         ("send_message", {"to": "Mom", "text": "hi", "channel": "whatsapp"})),
        ("calculate", {"expression": "2+2"}, ("calculate", {"expression": "2+2"})),
        ("brief.me", {"topic": "am"}, ("brief_me", {"topic": "am"})),
        ("device.control", {"op": "present", "args": {"card": "x"}},
         ("present", {"card": "x"})),
        ("media.capture", {"op": "capture_photo", "args": {}},
         ("capture_photo", {})),
    ],
)
def test_ticket_call_translations(tool, arguments, expected):
    assert graph_ticket_call(tool, arguments) == expected


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("life.send", {"to": "Mom"}),
        ("life.send", {"text": "hi"}),
        ("phone.call", {"op": "call"}),
        ("phone.call", {"op": "dance", "contact": "Mom"}),
        ("computer.perform_effect", {"effect": "  "}),
        ("digital.act", {"service": "gmail", "operation": "send", "args": {}}),
        ("device.control", {"op": "launch_missiles"}),
        ("memory.search", {"query": "x"}),
        ("nope.unknown", {}),
    ],
)
def test_ticket_call_untranslatable_is_none(tool, arguments):
    assert graph_ticket_call(tool, arguments) is None


# --------------------------------------------------------------------------- #
# Routing: web first, native AX next, both named when down
# --------------------------------------------------------------------------- #


def test_whatsapp_prefers_web():
    routing = route_channel("whatsapp", helper_available=False,
                            web_available=True, desktop_available=True)
    assert routing.provider == "web"
    assert routing.requires_approval is True


def test_whatsapp_falls_back_to_desktop():
    routing = route_channel("whatsapp", helper_available=False,
                            web_available=False, desktop_available=True)
    assert routing.provider == "desktop"
    assert routing.helper_command == "whatsapp.ax_send"
    assert routing.requires_approval is True


def test_whatsapp_unavailable_names_both_transports():
    routing = route_channel("whatsapp", helper_available=False,
                            web_available=False, desktop_available=False)
    assert routing.mode == "unavailable"
    assert "Web isn't linked" in routing.spoken
    assert "Mac app" in routing.spoken


def test_desktop_binding_satisfies_desktop_route():
    binding = RouteBinding.of(route_channel(
        "whatsapp", helper_available=False, web_available=False,
        desktop_available=True))
    live = route_channel("whatsapp", helper_available=False,
                         web_available=True, desktop_available=True)
    assert binding.satisfies(live) == "provider"


# --------------------------------------------------------------------------- #
# Park-time probe: pin what is reachable, fail terminally otherwise
# --------------------------------------------------------------------------- #


async def test_probe_pins_web_when_linked(monkeypatch):
    async def fake_available(refresh=False):
        return True

    async def fake_resolve(to, **kwargs):
        return {"status": "unique", "display": "Mom",
                "peer": {"phone": "+1555"}, "chat_ref": "c1"}

    monkeypatch.setattr(whatsapp_ax, "ax_status", None)  # must not be consulted
    import app.ev.messaging.whatsapp_web as web

    monkeypatch.setattr(web, "web_available", fake_available)
    monkeypatch.setattr(web, "resolve", fake_resolve)
    binding, address, failure = await _probe_whatsapp_route(
        _node(), {"to": "Mom", "text": "hi"})
    assert failure is None
    assert binding is not None and binding.provider == "web"
    assert address == "+1555"


async def test_probe_falls_through_to_ax(monkeypatch):
    import app.ev.messaging.whatsapp_web as web

    async def fake_available(refresh=False):
        return False

    async def fake_status(**kwargs):
        return whatsapp_ax.AXStatus(installed=True, running=True,
                                    ax_trusted=True, reachable=True)

    async def fake_chats(**kwargs):
        return [{"name": "Mom Smith"}]

    monkeypatch.setattr(web, "web_available", fake_available)
    monkeypatch.setattr(whatsapp_ax, "ax_status", fake_status)
    monkeypatch.setattr(whatsapp_ax, "ax_chats", fake_chats)
    binding, address, failure = await _probe_whatsapp_route(
        _node(), {"to": "Mom Smith", "text": "hi"})
    assert failure is None
    assert binding is not None and binding.provider == "desktop"
    assert address == "Mom Smith"


async def test_probe_both_down_is_terminal(monkeypatch):
    import app.ev.messaging.whatsapp_web as web

    async def fake_available(refresh=False):
        return False

    async def fake_status(**kwargs):
        return whatsapp_ax.AXStatus(installed=True, ax_trusted=False,
                                    reachable=False,
                                    reason="accessibility_not_granted")

    monkeypatch.setattr(web, "web_available", fake_available)
    monkeypatch.setattr(whatsapp_ax, "ax_status", fake_status)
    binding, address, failure = await _probe_whatsapp_route(
        _node(), {"to": "Mom", "text": "hi"})
    assert binding is None
    assert failure is not None
    assert failure.error == "transport_unavailable"
    assert "Web isn't linked" in failure.spoken
    assert "Accessibility" in failure.spoken
    assert failure.resumption is None


async def test_probe_ax_ambiguous_lists_candidates(monkeypatch):
    import app.ev.messaging.whatsapp_web as web

    async def fake_available(refresh=False):
        return False

    async def fake_status(**kwargs):
        return whatsapp_ax.AXStatus(installed=True, ax_trusted=True,
                                    reachable=True)

    async def fake_chats(**kwargs):
        return [{"name": "Mom Smith"}, {"name": "Mom Jones"}]

    monkeypatch.setattr(web, "web_available", fake_available)
    monkeypatch.setattr(whatsapp_ax, "ax_status", fake_status)
    monkeypatch.setattr(whatsapp_ax, "ax_chats", fake_chats)
    binding, address, failure = await _probe_whatsapp_route(
        _node(), {"to": "Mom", "text": "hi"})
    assert failure is not None
    assert failure.error == "chat_unresolved"
    assert "Mom Smith" in failure.spoken


async def test_park_pins_translated_dispatch_call(monkeypatch):
    import app.ev.messaging.whatsapp_web as web

    async def fake_available(refresh=False):
        return True

    async def fake_resolve(to, **kwargs):
        return {"status": "unique", "display": "Mom",
                "peer": {"phone": "+1555"}, "chat_ref": "c1"}

    monkeypatch.setattr(web, "web_available", fake_available)
    monkeypatch.setattr(web, "resolve", fake_resolve)
    receipt = await run_node(_node(), WorkerCtx(actor="master"), job_id="j")
    assert receipt.error == "confirmation_required"
    assert "via the WhatsApp app" not in receipt.spoken
    action_id = receipt.resumption["params"]["action_id"]
    async with SessionLocal() as db:
        from app.models import ApprovedAction

        action = await db.get(ApprovedAction, UUID(action_id))
        assert action is not None
        assert action.action_type == "send_message"
        assert action.payload["channel"] == "whatsapp"


async def test_park_desktop_names_transport(monkeypatch):
    import app.ev.messaging.whatsapp_web as web

    async def fake_available(refresh=False):
        return False

    async def fake_status(**kwargs):
        return whatsapp_ax.AXStatus(installed=True, running=True,
                                    ax_trusted=True, reachable=True)

    async def fake_chats(**kwargs):
        return [{"name": "Mom"}]

    monkeypatch.setattr(web, "web_available", fake_available)
    monkeypatch.setattr(whatsapp_ax, "ax_status", fake_status)
    monkeypatch.setattr(whatsapp_ax, "ax_chats", fake_chats)
    node = _node(arguments={"to": "Mom", "text": "hi", "channel": "whatsapp"})
    receipt = await run_node(node, WorkerCtx(actor="master"), job_id="j")
    assert receipt.error == "confirmation_required"
    assert "(via the WhatsApp app)" in receipt.spoken


# --------------------------------------------------------------------------- #
# Terminal verdicts: no model, no retry, no owner question for missing routes
# --------------------------------------------------------------------------- #


def test_terminal_errors_escalate_locally():
    for code in ("transport_unavailable", "chat_unresolved"):
        receipt = WorkerReceipt(node_id="n1", ok=False, worker="router",
                                spoken="Gone.", error=code)
        verdict = local_verdict(_node(tier="D"), receipt)
        assert verdict.next is VerdictNext.ESCALATE


async def test_terminal_errors_skip_decider():
    receipt = WorkerReceipt(node_id="n1", ok=False, worker="router",
                            spoken="Gone.", error="transport_unavailable")
    verdict = await supervise(_node(tier="D"), receipt)
    assert verdict.next is VerdictNext.ESCALATE


async def test_notify_fallback_persists_notification():
    from app.voice.live.session import LiveSession

    await LiveSession._notify_delegated_result(
        None, {"job_id": "notify-1", "spoken": "Task finished."},
        reason="session_gone")
    async with SessionLocal() as db:
        from sqlalchemy import select

        from app.models import Notification

        rows = (await db.execute(select(Notification).where(
            Notification.fingerprint == "delegate:notify-1:session_gone"
        ))).scalars().all()
        assert len(rows) == 1
        assert "Task finished." in (rows[0].body or "")
