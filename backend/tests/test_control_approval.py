"""Mac control tickets: per-action voice approval for phone-driven ui_action.

A trusted phone turn that wants Mac CONTROL (not observe) parks here instead
of executing: the owner hears one exact question and a spoken yes/no on any
later turn approves or cancels. Mirrors the send-approval ledger (same table,
same TTL/tamper rules, separate kind) so the two families never answer each
other's tickets.
"""

from __future__ import annotations


async def _control(device_id="dev-1", live="live-1", **over):
    kwargs = dict(
        tool="ui_action",
        arguments={"action": "press", "element_ref": "Send"},
        display="press Send",
        actor="voice",
        device_id=device_id,
        live_session_id=live,
    )
    kwargs.update(over)
    return kwargs


async def test_park_control_question_names_effect(db_session) -> None:
    from app.ev.messaging.approval import park_control, question_for_control

    action = await park_control(db_session, **await _control())
    question = question_for_control(action)
    assert "press Send" in question
    assert "Mac" in question


async def test_control_yes_approves_and_reports_execution(db_session, monkeypatch) -> None:
    from app.ev.messaging import approval as approval_mod
    from app.ev.messaging.approval import handle_parked_approval, park_control
    from app.voice.live.layer import register_live, reset_live_registry, unregister_live
    from app.voice.live.session import LiveSession

    reset_live_registry()
    mac = LiveSession(session_id="mac-live")
    mac._computer_state = {"generic_ui_control_ready": True}
    register_live(mac)
    try:
        action = await park_control(db_session, **await _control())
        await db_session.flush()

        async def fake_execute(session, ticket, **kwargs):
            assert ticket.id == action.id
            return {"ok": True, "executed": True, "spoken": "Pressed Send."}

        monkeypatch.setattr(approval_mod, "execute_control_ticket", fake_execute)
        result = await handle_parked_approval(
            db_session, "yes, do it", actor="voice", device_id="dev-1", live_session_id="live-1"
        )
        assert result is not None
        assert result["ok"] is True
        assert "Pressed Send." in result["spoken"]
    finally:
        unregister_live(mac)
        reset_live_registry()


async def test_control_no_cancels_without_executing(db_session, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from app.ev.messaging import approval as approval_mod
    from app.ev.messaging.approval import handle_parked_approval, park_control

    await park_control(db_session, **await _control())
    await db_session.flush()
    executor = AsyncMock()
    monkeypatch.setattr(approval_mod, "execute_control_ticket", executor)
    result = await handle_parked_approval(
        db_session, "no, cancel", actor="voice", device_id="dev-1", live_session_id="live-1"
    )
    assert result is not None
    assert result["cancelled"] is True
    executor.assert_not_awaited()


async def test_newest_ticket_across_kinds_wins(db_session) -> None:
    """A send ticket and a control ticket pending together: the yes answers
    the NEWER one only, and the older stays pending."""
    from app.ev.messaging.approval import (
        handle_parked_approval,
        latest_pending,
        latest_pending_control,
        park_control,
        park_send,
    )

    send = await park_send(
        db_session, to="Ada", text="hi", display="Ada", actor="voice",
        device_id="dev-1", live_session_id="live-1",
    )
    await db_session.flush()
    control = await park_control(db_session, **await _control())
    await db_session.flush()

    # "yes" to newest (control) via a dry question probe: cancel it so the
    # send ticket demonstrably survives as still-pending.
    result = await handle_parked_approval(
        db_session, "no", actor="voice", device_id="dev-1", live_session_id="live-1"
    )
    assert result is not None and result["cancelled"] is True
    await db_session.flush()
    assert await latest_pending_control(db_session, device_id="dev-1", live_session_id="live-1") is None
    survivor = await latest_pending(db_session, device_id="dev-1", live_session_id="live-1")
    assert survivor is not None and survivor.id == send.id
    assert control.id != send.id


async def _phone_live(session_id="phone-live", *, sandbox=False):
    from app.voice.live.layer import register_live
    from app.voice.live.session import LiveSession

    live = LiveSession(session_id=session_id)
    live.surface = "phone"
    live.memory_scope = "sandbox" if sandbox else "owner"
    register_live(live)
    return live


async def test_phone_ui_action_without_ticket_parks_and_asks(db_session) -> None:
    """A phone live calling ui_action with no ticket parks for approval —
    nothing executes and the spoken line is the approval question."""
    from app.ev.computer import handle_computer_tool
    from app.ev.messaging.approval import latest_pending_control, question_for_control
    from app.voice.live.layer import reset_live_registry, unregister_live

    reset_live_registry()
    phone = await _phone_live()
    try:
        result = await handle_computer_tool(
            db_session,
            "ui_action",
            {"action": "press", "label": "Send"},
            actor="voice",
            live_session_id="phone-live",
            device_id="dev-1",
        )
        assert result.get("confirmation_required") is True
        assert result.get("executed") is False
        assert result.get("action_id")
        ticket = await latest_pending_control(
            db_session, device_id="dev-1", live_session_id="phone-live"
        )
        assert ticket is not None
        assert str(ticket.id) == str(result["action_id"])
        assert result.get("spoken") == question_for_control(ticket)
        assert "press send" in str(result.get("spoken") or "").lower()
    finally:
        unregister_live(phone)
        reset_live_registry()


async def test_phone_ui_action_with_bad_ticket_is_refused(db_session, monkeypatch) -> None:
    """A forged or unknown ticket id never drives the Mac."""
    from unittest.mock import AsyncMock

    import app.ev.computer as computer_mod
    from app.ev.computer import handle_computer_tool
    from app.voice.live.layer import register_live, reset_live_registry, unregister_live
    from app.voice.live.session import LiveSession

    reset_live_registry()
    mac = LiveSession(session_id="mac-live")
    mac._computer_state = {"generic_ui_control_ready": True}
    register_live(mac)
    commander = AsyncMock()
    monkeypatch.setattr(computer_mod, "_live_command", commander)
    try:
        result = await handle_computer_tool(
            db_session,
            "ui_action",
            {"action": "press", "label": "Send"},
            actor="voice",
            live_session_id="mac-live",
            approved_action_id="00000000-0000-0000-0000-000000000000",
        )
        assert result.get("executed") is False
        assert result.get("error_code") == "CONTROL_TICKET_REJECTED"
        commander.assert_not_awaited()
    finally:
        unregister_live(mac)
        reset_live_registry()


async def test_phone_ui_action_with_valid_ticket_executes(db_session, monkeypatch) -> None:
    """An approved+fresh ticket presented with identical arguments executes."""
    from unittest.mock import AsyncMock

    import app.ev.computer as computer_mod
    from app.ev.computer import handle_computer_tool
    from app.ev.messaging.approval import park_control
    from app.voice.live.layer import register_live, reset_live_registry, unregister_live
    from app.voice.live.session import LiveSession

    reset_live_registry()
    mac = LiveSession(session_id="mac-live")
    mac._computer_state = {"generic_ui_control_ready": True}
    register_live(mac)
    commander = AsyncMock(return_value={"ok": True, "executed": True})
    monkeypatch.setattr(computer_mod, "_live_command", commander)
    try:
        args = {"action": "press", "label": "Send"}
        ticket = await park_control(
            db_session, tool="ui_action", arguments=args,
            display="press Send", actor="voice", device_id="dev-1",
            live_session_id="phone-live",
        )
        ticket.status = "approved"
        await db_session.flush()
        result = await handle_computer_tool(
            db_session, "ui_action", dict(args), actor="voice",
            live_session_id="mac-live", approved_action_id=ticket.id,
        )
        commander.assert_awaited_once()
        assert result.get("confirmation_required") is not True
        assert result.get("error_code") != "CONTROL_TICKET_REJECTED"
    finally:
        unregister_live(mac)
        reset_live_registry()


async def test_sandbox_phone_never_parks_control(db_session) -> None:
    """Sandbox gains nothing: no ticket is parked and the Mac is untouched."""
    from app.ev.computer import handle_computer_tool
    from app.ev.messaging.approval import latest_pending_control
    from app.voice.live.layer import reset_live_registry, unregister_live

    reset_live_registry()
    phone = await _phone_live(sandbox=True)
    try:
        result = await handle_computer_tool(
            db_session,
            "ui_action",
            {"action": "press", "label": "Send"},
            actor="voice",
            live_session_id="phone-live",
            device_id="dev-1",
        )
        assert result.get("executed") is False
        assert result.get("error_code") == "SANDBOX_CONTROL_BLOCKED"
        assert result.get("confirmation_required") is not True
        assert (
            await latest_pending_control(db_session, device_id="dev-1", live_session_id="phone-live")
        ) is None
    finally:
        unregister_live(phone)
        reset_live_registry()


async def test_combo_preserves_send_stray_adoption(db_session) -> None:
    """Cross-surface send yes (asked on Mac, answered on phone) still re-asks
    naming the send instead of falling through to the model."""
    from app.ev.messaging.approval import handle_parked_approval, park_send

    stray = await park_send(
        db_session, to="Ada", text="hi there", display="Ada", actor="voice",
        device_id="mac-device", live_session_id="mac-live",
    )
    await db_session.flush()
    result = await handle_parked_approval(
        db_session, "yes, send it", actor="voice",
        device_id="dev-1", live_session_id="phone-live",
    )
    assert result is not None
    assert result.get("pending_approval") is True
    assert result.get("sent") is False
    assert result.get("approval_family") == "send"
    assert str(result.get("action_id")) == str(stray.id)
    assert "Ada" in str(result.get("spoken") or "")


async def test_phone_mac_text_parks_ui_action_and_answers_yes(
    db_session, monkeypatch
) -> None:
    """Trusted phone text 'click Send on my Mac' parks with a question; a
    follow-up 'yes' in the same context resolves the control ticket."""
    from app.device_gateway import phone_mac
    from app.models import Device
    from app.voice.live.layer import register_live, reset_live_registry, unregister_live
    from app.voice.live.session import LiveSession

    device = Device(
        name="Test iPhone", token_hash="c" * 64, role="primary_companion",
        platform="ios", device_type="phone", memory_scope="owner",
    )
    db_session.add(device)
    await db_session.flush()

    reset_live_registry()
    mac = LiveSession(session_id="mac-live")
    mac._computer_state = {"generic_ui_control_ready": True}
    register_live(mac)
    monkeypatch.setattr(phone_mac, "_phrase_action", lambda text: None)
    monkeypatch.setattr(
        "app.ev.tool_select.resolve_live_action",
        lambda text: ("ui_action", {"action": "press", "label": "Send"}),
    )
    try:
        parked = await phone_mac.maybe_phone_mac_act(
            db_session, device=device, text="click Send on my Mac",
        )
        assert parked is not None
        assert parked.get("pending_approval") is True
        assert parked.get("executed") is False
        assert parked.get("action_id")
        assert "Should I do this on your Mac" in str(parked.get("reply") or "")

        from app.ev.messaging import approval as approval_mod

        async def fake_execute(session, ticket, **kwargs):
            return {"ok": True, "executed": True, "spoken": "Pressed Send."}

        monkeypatch.setattr(approval_mod, "execute_control_ticket", fake_execute)
        answered = await phone_mac.maybe_phone_mac_act(
            db_session, device=device, text="yes, do it",
        )
        assert answered is not None
        assert answered.get("executed") is True
        assert answered.get("tool") == "ui_action"
        assert "Pressed Send." in str(answered.get("reply") or "")
    finally:
        unregister_live(mac)
        reset_live_registry()
