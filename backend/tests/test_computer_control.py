"""Mac computer control: tools, overlay, stale refs, cycle detection, live RPC."""

from __future__ import annotations

import asyncio

from app.ev import camera_runtime  # noqa: F401  - registers camera projector
from app.ev.computer import classify_ui_risk, handle_computer_tool
from app.ev.computer_runtime import (
    COMPUTER_TOOLS,
    ComputerState,
    action_signature,
    computer_operator_line,
    ensure_state,
    guard_loop,
    overlay_computer_entry,
    readiness_from_computer_state,
    remember_snapshot,
    reset_computer_states,
    validate_element_ref,
    validate_frame_click,
)
from app.ev.policy import OWNER_AUTO_PERCEPTION, evaluate_policy
from app.ev.tool_select import LIVE_VOICE_TOOLS
from app.ev.tools import get_spec
from app.voice.live.events import ComputerRequestEvent
from app.voice.live.layer import reset_live_registry
from app.voice.live.session import LiveSession


def _drain(session: LiveSession) -> list:
    items = []
    while True:
        try:
            items.append(session.outbound.get_nowait())
        except asyncio.QueueEmpty:
            return items


def test_computer_tools_are_live_and_compact() -> None:
    assert COMPUTER_TOOLS <= LIVE_VOICE_TOOLS
    for name in (
        "computer_status",
        "list_apps",
        "activate_app",
        "inspect_ui",
        "ui_action",
        "screen_look",
        "open_app",
        "close_app",
        "app_action",
    ):
        spec = get_spec(name)
        assert spec is not None, name
        assert "permission" not in spec["parameters"].get("properties", {})
    inspect = get_spec("inspect_ui")
    assert "query" in inspect["parameters"]["properties"]
    assert inspect["parameters"]["properties"]["query"].get("default") is None
    app_action = get_spec("app_action")
    assert app_action is not None
    assert "playlist" in app_action["parameters"]["properties"]
    assert "index" in app_action["parameters"]["properties"]
    actions = get_spec("ui_action")["parameters"]["properties"]["action"]["enum"]
    assert "append" in actions
    assert "replace" in actions
    assert "click_at" in actions
    assert "double_click" in actions
    assert "right_click" in actions
    assert "drag" in actions


def test_computer_tools_include_ui_verbs() -> None:
    assert {"read", "see", "click", "double_click", "right_click", "type", "paste", "key", "scroll", "drag"} <= COMPUTER_TOOLS


def test_computer_status_and_inspect_are_owner_auto() -> None:
    for name in ("computer_status", "inspect_ui", "list_apps", "screen_look", "read", "see"):
        assert name in OWNER_AUTO_PERCEPTION
        decision = evaluate_policy(
            name,
            actor="master",
            channel="voice",
            training_wheels_complete=True,
            provider_connected=True,
        )
        assert decision.allowed is True
        assert decision.confirmation_required is False


def test_open_app_stays_r1_without_confirmation() -> None:
    decision = evaluate_policy(
        "open_app",
        actor="master",
        channel="voice",
        training_wheels_complete=True,
        provider_connected=True,
        arguments={"name": "TextEdit"},
    )
    assert decision.allowed is True
    assert decision.confirmation_required is False
    assert decision.risk_class == "R1"


def test_ui_risk_classifier() -> None:
    assert classify_ui_risk({"action": "press", "element_ref": "e1"}) == "low"
    assert classify_ui_risk({"action": "force_quit"}) == "high"
    assert classify_ui_risk({"action": "press"}, {"title": "Send"}) == "high"
    assert classify_ui_risk({"action": "press"}, {"title": "Don't Save"}) == "high"
    assert classify_ui_risk({"action": "press"}, {"title": "Bluetooth"}) == "low"


def test_overlay_hides_ui_without_mac_client() -> None:
    entry = {
        "name": "inspect_ui",
        "availability": "available",
        "model_exposed": True,
        "realtime_eligible": True,
        "executable": True,
    }
    hidden = overlay_computer_entry(
        entry,
        readiness_from_computer_state({}, client_connected=False, helper_ready=True),
    )
    assert hidden["availability"] == "not_connected"
    assert hidden["realtime_eligible"] is False
    hidden_verb = overlay_computer_entry(
        {**entry, "name": "read"},
        readiness_from_computer_state({}, client_connected=False, helper_ready=True),
    )
    assert hidden_verb["availability"] == "not_connected"
    hidden_see = overlay_computer_entry(
        {**entry, "name": "see"},
        readiness_from_computer_state({}, client_connected=False, helper_ready=True),
    )
    assert hidden_see["availability"] == "not_connected"
    ready = overlay_computer_entry(
        entry,
        readiness_from_computer_state(
            {"accessibility_permission": "authorized"},
            client_connected=True,
            realtime_provider="openai",
        ),
    )
    assert ready["availability"] == "available"
    assert ready["computer"]["generic_ui_control_ready"] is True
    line = computer_operator_line(ready["computer"])
    assert "AVAILABLE" in line
    probed = overlay_computer_entry(
        entry,
        readiness_from_computer_state(
            {
                "accessibility_permission": "authorized",
                "generic_ui_control_ready": False,
                "accessibility_probe": {"ok": False},
            },
            client_connected=True,
            realtime_provider="openai",
        ),
    )
    assert probed["computer"]["generic_ui_control_ready"] is False


def test_overlay_f4_computer_broker_ready_when_mac_is_connected() -> None:
    entry = {
        "name": "computer",
        "availability": "not_connected",
        "model_exposed": False,
        "realtime_eligible": False,
        "executable": False,
    }
    hidden = overlay_computer_entry(
        entry,
        readiness_from_computer_state({}, client_connected=False, helper_ready=False),
    )
    assert hidden["availability"] == "not_connected"
    ready = overlay_computer_entry(
        entry,
        readiness_from_computer_state({}, client_connected=True, helper_ready=False),
    )
    assert ready["availability"] == "available"
    assert ready["executable"] is True
    assert ready["realtime_eligible"] is True


def test_overlay_ax_denied_is_not_spoken_ready() -> None:
    from app.ev.protocols import _is_spoken_ready

    denied = overlay_computer_entry(
        {
            "name": "inspect_ui",
            "availability": "available",
            "model_exposed": True,
            "realtime_eligible": True,
            "executable": True,
        },
        readiness_from_computer_state(
            {"accessibility_permission": "denied", "screen_capture_permission": "denied"},
            client_connected=True,
            realtime_provider="openai",
        ),
    )
    assert denied["computer"]["generic_ui_control_ready"] is False
    assert denied["generic_ui_control_ready"] is False
    assert _is_spoken_ready(denied) is False
    line = computer_operator_line(denied["computer"])
    assert "AVAILABLE" not in line or "PARTIAL" in line
    assert "Accessibility" in line


def test_capability_registry_includes_camera_and_computer() -> None:
    from app.ev.capability_registry import registered_capabilities

    names = {item.name for item in registered_capabilities()}
    assert "camera" in names
    assert "computer" in names


def test_overlay_lifecycle_with_helper_only() -> None:
    entry = {
        "name": "open_app",
        "availability": "not_connected",
        "model_exposed": False,
        "realtime_eligible": False,
        "executable": False,
    }
    ready = overlay_computer_entry(
        entry,
        readiness_from_computer_state({}, client_connected=False, helper_ready=True),
    )
    assert ready["availability"] == "available"
    assert "helper" in ready["availability_reason"]


def test_stale_element_and_frame_guards() -> None:
    reset_computer_states()
    state = ensure_state("s1")
    assert state is not None
    remember_snapshot(
        state,
        {
            "snapshot_id": "s9",
            "generation": 9,
            "app": "TextEdit",
            "bundle_id": "com.apple.TextEdit",
            "elements": [{"ref": "e1", "role": "button", "title": "Bold"}],
        },
    )
    assert validate_element_ref(state, "e1") is None
    stale = validate_element_ref(state, "e99")
    assert stale is not None
    assert stale["error"] == "stale_element"
    secure = ComputerState(session_id="s2")
    remember_snapshot(
        secure,
        {"elements": [{"ref": "e2", "role": "AXSecureTextField", "secure": True}]},
    )
    blocked = validate_element_ref(secure, "e2")
    assert blocked is not None
    assert blocked["error"] == "sensitive_field"
    missing = validate_frame_click(state, frame_id="frame_1", bundle_id="com.apple.TextEdit")
    assert missing is not None
    assert missing["error"] in {"stale_frame", "missing_frame"}
    reset_computer_states()


def test_cycle_detector_stops_repeated_press() -> None:
    reset_computer_states()
    state = ensure_state("loop")
    arguments = {"action": "press", "element_ref": "e12"}
    first = guard_loop(state, "ui_action", arguments)
    second = guard_loop(state, "ui_action", arguments)
    third = guard_loop(state, "ui_action", arguments)
    assert first is None
    assert second is None
    assert third is not None
    assert third["error"] == "cycle_detected"
    assert action_signature("ui_action", arguments).startswith("ui_action")
    reset_computer_states()


async def test_live_computer_request_roundtrip() -> None:
    reset_live_registry()
    session = LiveSession(session_id="comp-1", device_id="mac", backchannel_enabled=False)
    task = asyncio.create_task(
        session.request_computer("inspect_ui", {}, timeout=2, request_id="req-ui")
    )
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "computer_request":
                request = event
                break
        if request is not None:
            break
    assert isinstance(request, ComputerRequestEvent)
    assert request.command == "inspect_ui"
    assert request.request_id == "req-ui"
    await session.handle_client(
        {
            "type": "computer_result",
            "request_id": "req-ui",
            "ok": True,
            "app": "TextEdit",
            "compact": 'window: w1 "Untitled"\ntextarea: e1 ""',
            "snapshot_id": "s1",
            "elements": [{"ref": "e1", "role": "textarea", "title": ""}],
            "spoken": "I'm looking at TextEdit.",
        }
    )
    result = await task
    assert result["ok"] is True
    assert result["app"] == "TextEdit"
    session.close()
    reset_live_registry()


async def test_computer_request_timeout() -> None:
    reset_live_registry()
    session = LiveSession(session_id="comp-timeout", device_id="mac", backchannel_enabled=False)
    result = await session.request_computer("inspect_ui", {}, timeout=0.05, request_id="req-t")
    assert result["ok"] is False
    assert result["error"] == "timeout"
    session.close()
    reset_live_registry()


async def test_inspect_ui_without_live_client_is_honest(db_session) -> None:
    reset_computer_states()
    result = await handle_computer_tool(
        db_session,
        "inspect_ui",
        {},
        actor="master",
        live_session_id=None,
        device_id=None,
    )
    assert result["ok"] is False
    assert "EV.app" in result["spoken"] or "live" in result["spoken"].lower()


async def test_screen_look_stashes_jpeg_for_realtime() -> None:
    from app.ev.camera_runtime import pop_observations, reset_pending_observations
    from app.ev.computer_runtime import stash_screen_observation, validate_jpeg

    reset_pending_observations()
    jpeg = b"\xff\xd8" + bytes(
        [0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x10, 0x00, 0x10, 0x01, 0x01, 0x11, 0x00]
    ) + (b"\x00" * 80) + b"\xff\xd9"
    assert validate_jpeg(jpeg) is not None
    stash_screen_observation(
        call_id="call-1",
        request_id="frame-1",
        jpeg=jpeg,
        width=16,
        height=16,
        app_name="TextEdit",
    )
    frames = pop_observations("call-1")
    assert len(frames) == 1
    assert frames[0].jpeg.startswith(b"\xff\xd8")
    assert frames[0].camera_name == "TextEdit"
    reset_pending_observations()


def test_save_dialog_instructions_and_dont_save_risk() -> None:
    from app.ev.computer import classify_ui_risk
    from app.ev.computer_runtime import computer_model_instructions

    text = computer_model_instructions(
        {
            "generic_ui_control_ready": True,
            "screen_vision_ready": True,
            "app_lifecycle_ready": True,
            "mac_client_connected": True,
        }
    )
    assert "don't save" in text.lower()
    assert classify_ui_risk({"action": "press"}, {"title": "Don’t Save"}) == "high"


def test_ephemeral_ui_refs_are_not_memories() -> None:
    from datetime import UTC, datetime

    from app.memory.extraction import Extractor
    from app.models import Event

    event = Event(
        source="voice",
        event_type="message.user",
        content={"text": "Remember e14 and frame_72 for the next click."},
        occurred_at=datetime.now(UTC),
        sha256="0" * 64,
    )
    found = Extractor().extract(event)
    assert not any("e14" in (item.text or "") or "frame_72" in (item.text or "") for item in found)


def test_music_goal_preserves_first_ordinal() -> None:
    from app.ev.computer_runtime import apply_goal_continuation, parse_owner_computer_goal

    goal = parse_owner_computer_goal(
        "Open the Music application, find the Chess playlist, and play the first track from it."
    )
    assert goal.playlist == "Chess"
    assert goal.ordinal == 1
    assert goal.play_requested is True
    assert goal.lifecycle_only is False
    assert "track 1" in goal.requested_outcome.lower() or "first" in goal.requested_outcome.lower()
    apply_goal_continuation(goal, "Now play the second one.")
    assert goal.ordinal == 2
    apply_goal_continuation(goal, "Go back to the first one.")
    assert goal.ordinal == 1


def test_open_app_does_not_complete_play_goal() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("goal-music")
    note_goal(state, "Open Music, find the Chess playlist, and play the first track.")
    assert state.goal is not None
    assert state.goal.ordinal == 1
    stamped = stamp_computer_receipt(
        {"ok": True, "app": "Music", "spoken": "Opened Music."},
        state,
        name="open_app",
        executed=True,
    )
    assert stamped["executed"] is True
    assert stamped["verified"] is False
    assert stamped["must_continue"] is True
    assert stamped["completion_claim_allowed"] is False
    assert "not the full request" in stamped["spoken"].lower()


def test_missing_playlist_is_truthful_failure() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("goal-neptune")
    note_goal(state, "Find the Project Neptune playlist and play it.")
    stamped = stamp_computer_receipt(
        {
            "ok": False,
            "executed": True,
            "verified": False,
            "error": "playlist_not_found",
            "action": "play",
            "spoken": "I couldn't find a playlist named Project Neptune.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert stamped["verified"] is False
    assert stamped["ok"] is False
    assert state.goal is not None
    assert state.goal.status == "failed"
    assert stamped["completion_claim_allowed"] is True


def test_false_success_rate_controlled_cases() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )
    from app.voice.live.layer import compact_live_tool_json

    reset_computer_states()
    cases = []
    fat = {
        "ok": True,
        "name": "inspect_ui",
        "spoken": "I'm looking at Music.",
        "result": {
            "ok": True,
            "elements": [{"title": "x" * 200, "role": "AXRow"} for _ in range(80)],
        },
    }
    blob = compact_live_tool_json(fat)
    parsed = __import__("json").loads(blob)
    cases.append(parsed.get("verified") is not True)
    assert __import__("json").loads(blob)
    assert len(blob) <= 3500 or parsed.get("error") == "tool_output_compacted"

    state = ensure_state("false-success")
    note_goal(state, "Play the Chess playlist first track.")
    truncated_style = stamp_computer_receipt(
        {"ok": True, "app": "Music"},
        state,
        name="open_app",
        executed=True,
    )
    cases.append(truncated_style.get("verified") is not True)
    cases.append(truncated_style.get("completion_claim_allowed") is not True)

    missing = stamp_computer_receipt(
        {"ok": False, "executed": True, "error": "playlist_not_found", "action": "play"},
        state,
        name="app_action",
        executed=True,
    )
    cases.append(missing.get("verified") is not True)
    false_success_rate = 0 if all(cases) else 1
    assert false_success_rate == 0


def test_compact_live_tool_json_keeps_recall_lines() -> None:
    from app.voice.live.layer import compact_live_tool_json

    blob = compact_live_tool_json(
        {
            "ok": True,
            "name": "recall",
            "spoken": "Person: Mummy. WhatsApp thread with Mummy.",
            "result": {
                "ok": True,
                "count": 2,
                "lines": ["Person: Mummy.", "WhatsApp thread with Mummy."],
                "grounding": "evidence",
                "life_shelf": "chats",
                "spoken": "Person: Mummy. WhatsApp thread with Mummy.",
            },
        }
    )
    parsed = __import__("json").loads(blob)
    assert parsed["ok"] is True
    assert parsed["result"]["count"] == 2
    assert parsed["result"]["hits"]
    assert parsed["result"]["life_shelf"] == "chats"
    assert "Mummy" in " ".join(parsed["result"]["hits"])
    import json

    from app.voice.live.layer import compact_live_tool_json

    payload = {
        "ok": True,
        "name": "computer",
        "executed": True,
        "verified": True,
        "must_continue": False,
        "completion_claim_allowed": True,
        "spoken": "Chrome is showing search results for Hugging Face.",
        "app": "Chrome",
        "action": "search",
        "query": "Hugging Face",
        "url": "https://www.google.com/search?q=Hugging+Face",
        "working": {"goal": {"requested_outcome": "x" * 2000}, "step_count": 9},
        "evidence": {"source": "mac_control", "note": "y" * 1500},
        "goal": {
            "goal_id": "g1",
            "status": "complete",
            "verified": True,
            "must_continue": False,
            "completion_claim_allowed": True,
            "requested_outcome": "Search Chrome for Hugging Face. " * 80,
            "remaining": "",
            "observed": {"app": "Chrome", "action": "search", "query": "Hugging Face"},
            "subgoals": [{"title": "z" * 400} for _ in range(12)],
        },
        "result": {
            "ok": True,
            "executed": True,
            "verified": True,
            "must_continue": False,
            "url": "https://www.google.com/search?q=Hugging+Face",
            "query": "Hugging Face",
            "elements": [{"title": "n" * 120} for _ in range(40)],
        },
    }
    blob = compact_live_tool_json(payload, limit=1200)
    parsed = json.loads(blob)
    assert parsed.get("verified") is True
    assert parsed.get("must_continue") is not True
    assert parsed.get("executed") is True
    assert parsed.get("error") != "tool_output_compacted"
    assert "Hugging Face" in str(parsed.get("query") or parsed.get("spoken") or "")
    assert len(blob) <= 1200


async def test_barge_in_does_not_cancel_in_flight_computer() -> None:
    reset_live_registry()
    session = LiveSession(session_id="barge-comp", device_id="mac", backchannel_enabled=False)
    task = asyncio.create_task(
        session.request_computer(
            "app_action",
            {"app": "Chrome", "action": "search", "query": "x"},
            timeout=2,
            request_id="req-keep",
        )
    )
    request = None
    for _ in range(40):
        await asyncio.sleep(0)
        for event in _drain(session):
            if event.type == "computer_request":
                request = event
                break
        if request is not None:
            break
    assert request is not None
    await session.handle_client({"type": "control", "action": "barge_in"})
    await session.handle_client(
        {
            "type": "computer_result",
            "request_id": "req-keep",
            "ok": True,
            "executed": True,
            "verified": True,
            "url": "https://www.google.com/search?q=x",
        }
    )
    result = await asyncio.wait_for(task, timeout=1)
    assert result.get("ok") is True
    assert result.get("error") != "cancelled"
    session.close()


async def test_computer_result_ignores_mismatched_request_id() -> None:
    reset_live_registry()
    session = LiveSession(session_id="comp-id", device_id="mac", backchannel_enabled=False)
    task = asyncio.create_task(
        session.request_computer("app_action", {}, timeout=2, request_id="req-a")
    )
    for _ in range(40):
        await asyncio.sleep(0)
        if any(event.type == "computer_request" for event in _drain(session)):
            break
    await session.handle_client(
        {"type": "computer_result", "request_id": "other", "ok": False, "error": "stray"}
    )
    await session.handle_client(
        {"type": "computer_result", "request_id": "req-a", "ok": True, "executed": True}
    )
    result = await asyncio.wait_for(task, timeout=1)
    assert result.get("ok") is True
    assert result.get("error") != "stray"
    session.close()


def test_compact_live_tool_json_never_slices_invalid() -> None:
    import json

    from app.voice.live.layer import compact_live_tool_json

    payload = {
        "ok": True,
        "name": "inspect_ui",
        "spoken": "I'm looking at Music.",
        "result": {
            "ok": True,
            "compact": "row: Chess\n" * 400,
            "elements": [{"ref": f"e1_{i}", "title": "Chess " * 40} for i in range(60)],
        },
    }
    blob = compact_live_tool_json(payload)
    parsed = json.loads(blob)
    assert isinstance(parsed, dict)
    assert parsed.get("ok") in {True, False}
    if parsed.get("error") == "tool_output_compacted":
        # Compaction may drop bulky UI, but a verified success must not be
        # rewritten as must_continue. inspect_ui fuzz payloads are unverified.
        if parsed.get("verified") is True:
            assert parsed.get("must_continue") is not True
        else:
            assert parsed.get("verified") is False
            assert parsed.get("completion_claim_allowed") is False


def test_app_action_overlay_does_not_need_ax() -> None:
    from app.ev.computer_runtime import overlay_computer_entry, readiness_from_computer_state

    entry = {
        "name": "app_action",
        "availability": "available",
        "model_exposed": True,
        "realtime_eligible": True,
        "executable": True,
    }
    ready = overlay_computer_entry(
        entry,
        readiness_from_computer_state(
            {"accessibility_permission": "denied", "screen_capture_permission": "denied"},
            client_connected=True,
            realtime_provider="openai",
        ),
    )
    assert ready["availability"] == "available"
    assert ready["realtime_eligible"] is True


def test_open_app_returns_music_control_route() -> None:
    from app.ev.computer import _shape_lifecycle

    shaped = _shape_lifecycle(
        "open_app",
        {"name": "Music"},
        {"ok": True, "app": "Music", "name": "Music", "bundle_id": "com.apple.Music"},
        source="mac_control",
    )
    assert shaped["goal_complete"] is False
    assert shaped["control"]["preferred"] == "semantic_adapter"
    assert "play" in shaped["control"]["supported_actions"]
    assert "app_action" in shaped["suggested_fallbacks"]


def test_ordinal_mismatch_is_rejected_before_execution() -> None:
    from app.ev.computer_runtime import (
        constraint_violation,
        ensure_state,
        note_goal,
        reset_computer_states,
    )

    reset_computer_states()
    state = ensure_state("ord")
    note_goal(state, "Open Music, find the Chess playlist, and play the first track.")
    blocked = constraint_violation(state, "app_action", {"action": "play", "index": 2, "playlist": "Chess"})
    assert blocked is not None
    assert blocked["error"] == "ordinal_mismatch"
    reset_computer_states()


def test_budget_resets_on_new_goal() -> None:
    from app.ev.computer_runtime import ensure_state, guard_loop, note_goal, reset_computer_states

    reset_computer_states()
    state = ensure_state("budget")
    note_goal(state, "Open Music and play the Chess playlist.")
    for i in range(3):
        assert guard_loop(state, "app_action", {"action": "status", "playlist": f"p{i}"}) is None
    assert state.budget_used.get("semantic") == 3
    note_goal(state, "Open Notes and write hello.")
    assert state.step_count == 0
    assert state.budget_used == {}
    reset_computer_states()


def test_strategy_budget_switches_after_ax_cap() -> None:
    from app.ev.computer_runtime import ensure_state, guard_loop, note_goal, reset_computer_states

    reset_computer_states()
    state = ensure_state("ax-cap")
    note_goal(state, "Open Safari and search for OpenAI.")
    last = None
    for i in range(9):
        last = guard_loop(state, "inspect_ui", {"app": "Safari", "query": f"q{i}"})
    assert last is not None
    assert last["error"] == "strategy_switch"
    assert last.get("next_strategy") == "keyboard"
    reset_computer_states()


def test_next_strategy_includes_keyboard_and_coordinates() -> None:
    from app.ev.computer_strategy import classify_tool_strategy, next_strategy

    assert next_strategy("semantic") == "ax"
    assert next_strategy("ax") == "keyboard"
    assert next_strategy("keyboard") == "vision"
    assert next_strategy("vision") == "coordinate"
    assert next_strategy("coordinate") is None
    assert classify_tool_strategy("ui_action", {"action": "paste"}) == "keyboard"
    assert classify_tool_strategy("ui_action", {"action": "click_at"}) == "coordinate"
    assert classify_tool_strategy("app_action", {"app": "Calculator", "action": "search"}) == "keyboard"


def test_two_strike_non_progress_switches_strategy() -> None:
    from app.ev.computer_runtime import ensure_state, guard_loop, note_goal, reset_computer_states
    from app.ev.computer_strategy import NON_PROGRESS_SWITCH_AFTER

    reset_computer_states()
    state = ensure_state("two-strike")
    note_goal(state, "Open Safari and open the first result.")
    state.strategy = "ax"
    state.non_progress_streak = NON_PROGRESS_SWITCH_AFTER
    blocked = guard_loop(state, "inspect_ui", {"app": "Safari", "query": "result"})
    assert blocked is not None
    assert blocked["error"] == "strategy_switch"
    assert blocked.get("next_strategy") == "keyboard"
    reset_computer_states()


def test_safari_open_first_result_goal_helper() -> None:
    from app.ev.computer import _calculator_expression, _wants_open_first_result
    from app.ev.computer_runtime import ComputerState, note_goal, reset_computer_states

    reset_computer_states()
    state = ComputerState(session_id="s")
    note_goal(state, "Open Safari, search for OpenAI, and open the first result.")
    assert _wants_open_first_result(state) is True
    reset_computer_states()
    state = ComputerState(session_id="s-search-result")
    note_goal(state, "Open Safari and search for YouTube, then open the first search result.")
    assert _wants_open_first_result(state) is True
    reset_computer_states()
    state = ComputerState(session_id="s-result-link")
    note_goal(state, "search for youtube and open the first result link")
    assert _wants_open_first_result(state) is True
    reset_computer_states()
    state = ComputerState(session_id="s2")
    note_goal(state, "Find the first result but don't open it.")
    assert _wants_open_first_result(state) is False
    assert _calculator_expression("Open Calculator and calculate 187 times 43.") == "187*43"
    reset_computer_states()


def test_speech_grounding_and_schema_hash() -> None:
    from app.ev.computer_strategy import (
        computer_tool_schema_hash,
        evaluate_provider_computer_schema,
        speech_is_grounded,
    )
    from app.ev.tools import get_spec

    assert speech_is_grounded("It's playing.", verified=True, goal_complete=True, failed=False)
    assert not speech_is_grounded("It's playing.", verified=False, goal_complete=False, failed=False)
    assert speech_is_grounded("I couldn't find that playlist.", verified=False, goal_complete=False, failed=True) is True
    tools = [get_spec(name) for name in ("inspect_ui", "app_action", "open_app") if get_spec(name)]
    local = computer_tool_schema_hash(tools)
    assert len(local) == 16
    ack = evaluate_provider_computer_schema(
        advertised_tools=[get_spec("inspect_ui"), get_spec("app_action")],
        acknowledged_names=["open_app"],
        acknowledged_schemas=[{"name": "open_app", "property_names": ["name"]}],
    )
    assert ack["tool_schema_match"] is False
    assert ack["computer_control_ready"] is False
    broker = evaluate_provider_computer_schema(
        advertised_tools=[{"name": "computer", "parameters": {"properties": {"goal": {}, "target_app": {}}, "required": ["goal"]}}],
        acknowledged_names=["computer"],
        acknowledged_schemas=[{"name": "computer", "property_names": ["goal", "target_app"]}],
    )
    assert broker["tool_schema_match"] is True
    assert broker["computer_control_ready"] is True
    assert broker["missing_tools"] == []


def test_shadow_computer_schema_matches_advertised_verbs() -> None:
    from app.ev.computer_strategy import (
        adapter_for,
        classify_tool_strategy,
        evaluate_provider_computer_schema,
    )
    from app.ev.tools import get_spec
    from app.voice.live.grok_voice import grok_voice_tools

    names = (
        "computer_status",
        "list_apps",
        "open_app",
        "activate_app",
        "close_app",
        "open_url",
        "read",
        "see",
        "click",
        "type",
        "key",
        "inspect_ui",
        "app_action",
    )
    advertised = grok_voice_tools(
        [get_spec(name) for name in names if get_spec(name)],
        mode="shadow",
    )
    advertised_names = {item["name"] for item in advertised}
    assert {"read", "see", "click", "open_url", "open_app", "app_action"} <= advertised_names
    assert "inspect_ui" not in advertised_names
    ack_schemas = [
        {
            "name": item["name"],
            "property_names": list((item.get("parameters") or {}).get("properties") or {}),
        }
        for item in advertised
    ]
    matched = evaluate_provider_computer_schema(
        advertised_tools=advertised,
        acknowledged_names=[item["name"] for item in advertised],
        acknowledged_schemas=ack_schemas,
    )
    assert matched["missing_tools"] == []
    assert matched["tool_schema_match"] is True
    assert adapter_for("Spotify")["semantic_adapter"] == "spotify"
    assert adapter_for("Chrome")["semantic_adapter"] == "chrome"
    assert "open_item" in adapter_for("Chrome")["supported_actions"]
    assert "open_item" in adapter_for("Spotify")["supported_actions"]
    assert classify_tool_strategy("read") == "ax"
    assert classify_tool_strategy("see") == "vision"
    assert classify_tool_strategy("type") == "keyboard"
    assert classify_tool_strategy("open_url") == "lifecycle"


def test_in_app_computer_goal_routes_to_adapters() -> None:
    from app.ev.computer_strategy import resolve_in_app_computer_goal

    chrome = resolve_in_app_computer_goal("In Google Chrome, search for OpenAI.")
    assert chrome == ("app_action", {"app": "Chrome", "action": "search", "query": "OpenAI"})
    safari = resolve_in_app_computer_goal("Search for OpenAI in Safari")
    assert safari is not None and safari[1]["app"] == "Safari" and safari[1]["query"] == "OpenAI"
    notes = resolve_in_app_computer_goal("Create a note that says Evie live computer use")
    assert notes is not None and notes[1]["app"] == "Notes" and "Evie live computer use" in notes[1]["text"]
    notes_read = resolve_in_app_computer_goal("Read the current note in Notes")
    assert notes_read == ("app_action", {"app": "Notes", "action": "read"})
    music = resolve_in_app_computer_goal(
        "Open Music, find the Chess playlist, and play the first track."
    )
    assert music is not None and music[1]["app"] == "Music" and music[1]["playlist"] == "Chess"
    assert resolve_in_app_computer_goal("What did we decide about SQLite?") is None
    youtube = resolve_in_app_computer_goal(
        "In Safari, search for YouTube and open the first result."
    )
    assert youtube is not None
    assert youtube[1]["app"] == "Safari"
    assert youtube[1]["action"] == "search"
    assert youtube[1]["query"] == "YouTube"
    assert "youtube.com" not in youtube[1]["query"].lower()
    rewritten = resolve_in_app_computer_goal(
        "Open Safari and search for YouTube, then open the first search result."
    )
    assert rewritten is not None
    assert rewritten[1]["action"] == "search"
    assert rewritten[1]["query"] == "YouTube"
    rust = resolve_in_app_computer_goal(
        "In Safari, search for rust borrow checker and open the first result."
    )
    assert rust is not None
    assert rust[1]["action"] == "search"
    assert rust[1]["query"] == "rust borrow checker"
    openai = resolve_in_app_computer_goal(
        "search for OpenAI and open the first result"
    )
    assert openai is not None
    assert openai[1]["action"] == "search"
    assert openai[1]["query"] == "OpenAI"


def test_owner_search_is_exact_and_domains_navigate() -> None:
    from app.ev.computer_strategy import (
        look_should_use_screen,
        navigation_url_from_text,
        navigation_url_in_utterance,
        resolve_generic_computer_goal,
        resolve_in_app_computer_goal,
        resolve_screen_observation_goal,
        wants_play_media,
        wants_screen_observation,
        _search_query_from_goal,
    )

    assert _search_query_from_goal(
        "Open Safari and search for OpenAI, then open the first result."
    ) == "OpenAI"
    assert _search_query_from_goal(
        'Search for "cats and dogs" in Safari please'
    ) == "cats and dogs"
    assert navigation_url_from_text("youtube.com") == "https://youtube.com"
    assert navigation_url_from_text("Sahaj-File-Test.txt") is None
    assert navigation_url_from_text("index.html") is None
    assert navigation_url_from_text("youtube . com") == "https://youtube.com"
    assert navigation_url_from_text("https://www.youtube.com/watch?v=1") == (
        "https://www.youtube.com/watch?v=1"
    )
    assert navigation_url_from_text("YouTube") is None
    assert navigation_url_from_text("OpenAI") is None

    domain = resolve_in_app_computer_goal("Open Safari and search for youtube.com")
    assert domain is not None
    assert domain[0] == "app_action"
    assert domain[1]["action"] == "navigate"
    assert "youtube.com" in domain[1]["query"].lower()
    assert "google.com/search" not in domain[1]["query"].lower()
    assert "open safari" not in domain[1]["query"].lower()

    spoken = resolve_in_app_computer_goal("search for youtube . com")
    assert spoken is not None
    assert spoken[1]["action"] == "navigate"

    polluted = resolve_in_app_computer_goal(
        "Perform a search in Safari for 'youtube.com' and open the first result continue=True"
    )
    assert polluted is not None
    assert polluted[1]["action"] == "navigate"
    assert polluted[1]["query"].lower().startswith("https://")
    assert "youtube.com" in polluted[1]["query"].lower()
    assert "in safari" not in polluted[1]["query"].lower()
    assert _search_query_from_goal("search in Safari for OpenAI") == "OpenAI"
    assert _search_query_from_goal(
        "In Safari, search for the word 'youtube' and then open the first search result"
    ) == "youtube"

    glued_open = resolve_in_app_computer_goal(
        "Open Safari, and then in the new tab, search for youtube.com. Open"
    )
    assert glued_open is not None
    assert glued_open[1]["action"] == "navigate"
    assert glued_open[1]["query"].lower().rstrip("/") in {
        "https://youtube.com",
        "https://www.youtube.com",
    } or (
        "youtube.com" in glued_open[1]["query"].lower()
        and ".open" not in glued_open[1]["query"].lower()
    )
    assert navigation_url_from_text("youtube.com.open") == "https://youtube.com"
    assert navigation_url_from_text("youtube.com.in") == "https://youtube.com"
    assert navigation_url_from_text("gov.in") == "https://gov.in"
    assert navigation_url_in_utterance("search for youtube.com. Open a new tab") == (
        "https://youtube.com"
    )
    assert _search_query_from_goal("just search youtube.com nothing else") == "youtube.com"
    clarified = resolve_in_app_computer_goal("just search youtube.com nothing else")
    assert clarified is not None
    assert "youtube.com" in clarified[1]["query"].lower()
    assert ".in" not in clarified[1]["query"].replace("https://", "").lower()
    assert wants_play_media("Open Safari and search for youtube.com in a new tab") is False
    slack = resolve_generic_computer_goal("In Slack, search for standup notes")
    assert slack is not None
    assert slack[1]["app"].lower() == "slack"
    assert slack[1]["action"] == "search"
    assert slack[1]["query"].lower() == "standup notes"

    close = resolve_in_app_computer_goal("Close Safari")
    assert close == ("close_app", {"name": "safari"})
    assert resolve_in_app_computer_goal("Quit Google Chrome") == (
        "close_app",
        {"name": "google chrome"},
    )
    assert resolve_in_app_computer_goal("Open Safari") == ("open_app", {"name": "safari"})

    tab = resolve_in_app_computer_goal("Open a new tab in Safari")
    assert tab is not None
    assert tab[0] == "app_action"
    assert tab[1]["app"] == "Safari"
    assert tab[1]["action"] == "new_tab"
    chrome_tab = resolve_in_app_computer_goal("open another tab in Chrome")
    assert chrome_tab is not None and chrome_tab[1]["action"] == "new_tab"
    close_tab = resolve_in_app_computer_goal("Close this tab in Safari")
    assert close_tab is not None and close_tab[1]["action"] == "close_tab"

    mixed_tab = resolve_in_app_computer_goal(
        "Open a new tab and search for youtube and open the first result"
    )
    assert mixed_tab is not None
    assert mixed_tab[0] == "app_action"
    assert mixed_tab[1]["action"] == "search"
    assert mixed_tab[1]["query"].lower() == "youtube"
    rewrite_tab = resolve_in_app_computer_goal(
        "Safari is already open, open a new tab and search for YouTube "
        "and open the first result link"
    )
    assert rewrite_tab is not None
    assert rewrite_tab[1]["action"] == "search"
    assert rewrite_tab[1]["query"].lower() == "youtube"
    assert resolve_in_app_computer_goal("Close Slack") == (
        "close_app",
        {"name": "slack"},
    )
    assert resolve_in_app_computer_goal("Quit Notes") == (
        "close_app",
        {"name": "notes"},
    )
    assert resolve_in_app_computer_goal("Hide Calendar") == (
        "close_app",
        {"name": "calendar"},
    )
    assert resolve_in_app_computer_goal("Close Finder") == (
        "close_app",
        {"name": "finder"},
    )
    assert resolve_in_app_computer_goal("Close the finder app") == (
        "close_app",
        {"name": "finder"},
    )
    assert resolve_in_app_computer_goal(
        "Close Finder by dismissing its windows (do not quit the process)."
    ) == ("close_app", {"name": "finder"})
    assert resolve_in_app_computer_goal(
        "Dismiss all Finder windows without quitting Finder."
    ) == ("close_app", {"name": "finder"})

    assert look_should_use_screen("Take a look at the window, which app is open")
    assert wants_screen_observation("What's on my screen?")
    assert resolve_screen_observation_goal("which app is open")[0] == "screen_look"
    assert not look_should_use_screen("Look at me and tell me what I'm holding")
    assert not look_should_use_screen("Look out the window at the room")
    assert not wants_screen_observation("Open Safari and search for OpenAI")
    assert not look_should_use_screen(
        "open YouTube.com in Safari and play the first video appearing on the screen"
    )
    assert resolve_screen_observation_goal(
        "open YouTube.com in Safari and play the first video appearing on the screen"
    ) is None
    identify = "Identify the currently open app based on this Mac's active window."
    assert look_should_use_screen(identify)
    assert resolve_screen_observation_goal(identify)[0] == "screen_look"
    assert not look_should_use_screen("List the files on my desktop")
    assert not look_should_use_screen(
        "(system confirmation — speak this to the owner now) On Desktop: a.txt."
    )
    assert resolve_screen_observation_goal("List the files on my desktop") is None


def test_generic_computer_goal_opens_and_closes_any_app() -> None:
    from app.ev.computer_strategy import resolve_generic_computer_goal

    assert resolve_generic_computer_goal("Open TextEdit") == (
        "open_app",
        {"name": "textedit"},
    )
    assert resolve_generic_computer_goal("Open the app Ghostty.") == (
        "open_app",
        {"name": "ghostty"},
    )
    assert resolve_generic_computer_goal("Close Slack") == (
        "close_app",
        {"name": "slack"},
    )
    slack = resolve_generic_computer_goal("In Slack, click Messages")
    assert slack is not None
    assert slack[0] == "app_action"
    assert slack[1]["app"] == "Slack"
    assert slack[1]["action"] == "open_item"
    assert "Messages" in slack[1]["query"]
    compound = resolve_generic_computer_goal("Open Slack and search for standup notes")
    assert compound is not None
    assert compound[0] == "app_action"
    assert compound[1]["app"].lower() == "slack"
    assert compound[1]["action"] == "search"
    assert compound[1]["query"].lower() == "standup notes"
    glued = resolve_generic_computer_goal("Open Slack and search for youtube.com.open")
    assert glued is not None
    assert glued[1]["action"] == "search"
    assert glued[1]["query"].lower() == "youtube.com"
    typed = resolve_generic_computer_goal("Open TextEdit and type hello world")
    assert typed is not None
    assert typed[1]["app"].lower() == "textedit"
    assert "hello world" in typed[1]["query"].lower()
    assert "open" not in typed[1]["query"].lower()


def test_verified_chrome_search_completes_search_only_goal() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("chrome-search-complete")
    note_goal(state, "In Google Chrome, search for OpenAI.")
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "must_continue": True,
            "app": "Chrome",
            "action": "search",
            "url": "https://www.google.com/search?q=OpenAI",
            "spoken": "Chrome is showing search results for OpenAI.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status == "complete"
    assert state.goal.verified is True
    assert stamped["must_continue"] is False
    assert stamped["verified"] is True


def test_executed_chrome_search_without_ui_proof_must_continue() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("chrome-search-no-readback")
    note_goal(state, "In Google Chrome, search for Anthropic.")
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": False,
            "must_continue": True,
            "app": "Chrome",
            "action": "search",
            "query": "Anthropic",
            "url": "",
            "spoken": "Chrome is showing search results for Anthropic.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status != "complete"
    assert state.goal.verified is False
    assert stamped["must_continue"] is True
    assert stamped["verified"] is False


def test_verified_notes_read_completes_goal() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("notes-read-complete")
    note_goal(state, "Read the current note in Notes.")
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Notes",
            "action": "read",
            "body": "talk path works",
            "spoken": "The note says: talk path works",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status == "complete"
    assert stamped["must_continue"] is False
    assert stamped["verified"] is True


def test_verified_empty_notes_read_completes_then_report() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("notes-read-empty-front")
    note_goal(state, "Read the front note in Apple Notes, then tell me what it says.")
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Notes",
            "action": "read",
            "body": "",
            "spoken": "The front note is empty.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status == "complete"
    assert stamped["must_continue"] is False
    assert stamped["verified"] is True


def test_verified_notes_read_completes_then_report_subgoal() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("notes-read-then-report")
    note_goal(state, "Read the front note in Apple Notes, then report what it says.")
    assert state.goal is not None
    assert state.goal.subgoals
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Notes",
            "action": "read",
            "body": "Evie notes proof 163045",
            "spoken": "The note says: Evie notes proof 163045",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal.status == "complete"
    assert stamped["must_continue"] is False
    assert stamped["verified"] is True


def test_verified_notes_read_completes_then_summarize_subgoal() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("notes-read-then-summarize")
    note_goal(
        state,
        "Open Apple Notes and read the front/first note in the list; then summarize its content in plain text.",
    )
    assert state.goal is not None
    assert state.goal.subgoals
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Notes",
            "action": "read",
            "body": "exactly: Evie notes proof 165442.",
            "spoken": "The note says: exactly: Evie notes proof 165442.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal.status == "complete"
    assert stamped["must_continue"] is False
    assert stamped["verified"] is True


def test_verified_notes_read_completes_open_and_read_then_report() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("notes-open-and-read-then-report")
    note_goal(
        state,
        "Open Apple Notes and read the frontmost note content, then report what it says.",
    )
    assert state.goal is not None
    assert state.goal.subgoals
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Notes",
            "action": "read",
            "body": "Evie notes proof 163045",
            "spoken": "The note says: Evie notes proof 163045",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal.status == "complete"
    assert stamped["must_continue"] is False
    assert stamped["verified"] is True


def test_verified_notes_create_keeps_follow_on_read_subgoal() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("notes-write-then-read")
    note_goal(state, "Write a note that says hello, then read it back.")
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Notes",
            "action": "create",
            "spoken": "Created a note.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status == "acting"
    assert stamped["must_continue"] is True


def test_verified_safari_search_completes_search_only_goal() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("safari-search-complete")
    note_goal(state, "In Safari, search for Wikipedia.")
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "must_continue": True,
            "app": "Safari",
            "action": "search",
            "url": "https://www.google.com/search?q=Wikipedia",
            "spoken": "Safari is showing search results for Wikipedia.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status == "complete"
    assert stamped["must_continue"] is False
    assert stamped["verified"] is True


def test_live_projection_exposes_ui_verbs_when_mac_connected() -> None:
    from app.ev.capabilities import live_tool_projection
    from app.ev.tools import get_spec

    readiness = readiness_from_computer_state(
        {
            "accessibility_permission": "authorized",
            "screen_capture_permission": "authorized",
        },
        client_connected=True,
        realtime_provider="openai",
        realtime_session_connected=True,
        provider_tools_confirmed=True,
        tool_schema_match=True,
    )
    assert "DEGRADED" not in computer_operator_line(readiness)
    entries = []
    for name in ("read", "see", "click", "type", "open_app", "inspect_ui"):
        spec = get_spec(name)
        assert spec is not None, name
        entries.append(
            overlay_computer_entry(
                {
                    "name": name,
                    "availability": "available",
                    "model_exposed": True,
                    "realtime_eligible": True,
                    "executable": True,
                    "risk_class": spec["risk_class"],
                    "parameters": spec["parameters"],
                    "type": "function",
                },
                readiness,
            )
        )
    projected = {item["name"] for item in live_tool_projection(entries)}
    assert {"read", "see", "click", "type", "open_app", "inspect_ui"} <= projected
    assert all(item["availability"] == "available" for item in entries)


def test_compact_json_fuzz_stays_valid() -> None:
    import json
    import random

    from app.voice.live.layer import compact_live_tool_json

    rng = random.Random(4)
    for _ in range(20):
        payload = {
            "ok": rng.choice([True, False]),
            "name": "inspect_ui",
            "spoken": "x" * rng.randint(0, 400),
            "result": {
                "ok": True,
                "compact": "row\n" * rng.randint(1, 200),
                "elements": [{"title": "y" * rng.randint(1, 80)} for _ in range(rng.randint(0, 40))],
            },
        }
        blob = compact_live_tool_json(payload)
        parsed = json.loads(blob)
        assert isinstance(parsed, dict)


def test_dont_play_constraint() -> None:
    from app.ev.computer_runtime import parse_owner_computer_goal

    goal = parse_owner_computer_goal(
        "Find my Chess playlist and tell me its first track, but don't play it."
    )
    assert goal.find_only is True
    assert goal.play_allowed is False
    assert goal.playlist == "Chess"
    assert goal.ordinal == 1


def test_find_only_does_not_complete_without_track() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("goal-find-only")
    note_goal(
        state,
        "Find my Chess playlist and tell me its first track, but don't play it.",
    )
    missing = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "action": "find_playlist",
            "playlist": "Chess",
            "spoken": "Found playlist Chess.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert missing["verified"] is False
    assert missing["must_continue"] is True
    listed = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "action": "find_playlist",
            "playlist": "Chess",
            "track": "Cinnamon Girl",
            "tracks": [{"index": 1, "name": "Cinnamon Girl"}],
            "spoken": "Found playlist Chess. The first track is Cinnamon Girl.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert listed["verified"] is True
    assert listed["must_continue"] is False
    assert state.goal is not None
    assert state.goal.track == "Cinnamon Girl"


def test_play_goal_skips_silent_open() -> None:
    from app.ev.computer_runtime import (
        allowed_computer_arguments,
        skip_silent_lifecycle_for,
    )

    assert skip_silent_lifecycle_for(
        "Open Music, find the Chess playlist, and play the first track."
    )
    assert not skip_silent_lifecycle_for("Open Music")
    assert not skip_silent_lifecycle_for("what time is it")
    stripped = allowed_computer_arguments(
        "open_app",
        {"name": "Music", "playlist": "Chess", "goal": "play first track"},
    )
    assert stripped == {"name": "Music"}


def test_open_app_unknown_args_are_stripped_by_realtime_validator() -> None:
    from app.ev.tools import get_spec
    from app.voice.live.grok_voice import GrokVoiceBridge

    spec = get_spec("open_app")
    assert spec is not None
    events: list = []
    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event),
        api_key="test",
        provider="openai",
        tool_specs=[spec],
    )
    bridge._upstream_session_ready = True
    bridge._upstream_tool_names = ("open_app",)
    effective, err = bridge._validate_function_call(
        "open_app",
        {"name": "Music", "playlist": "Chess", "goal": "play first"},
    )
    assert err is None
    assert effective == {"name": "Music"}


def test_send_text_records_computer_transcript() -> None:
    import asyncio

    from app.voice.live.grok_voice import GrokVoiceBridge

    events: list = []
    bridge = GrokVoiceBridge(
        on_event=lambda event: events.append(event),
        api_key="test",
        provider="openai",
        tool_specs=[],
    )
    bridge._closed = False

    async def fake_start() -> bool:
        return False

    bridge.start = fake_start  # type: ignore[method-assign]
    asyncio.run(
        bridge.send_text("Open Music, find the Chess playlist, and play the first track.")
    )
    assert "Chess" in bridge._last_input_transcript
    assert bridge._last_input_transcript_at > 0


def test_safari_first_result_subgoal_completes_after_navigate() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("safari-first-result")
    note_goal(state, "Search for YouTube, then open the first result.")
    stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Safari",
            "action": "search",
            "url": "https://www.google.com/search?q=YouTube",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status == "acting"
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Safari",
            "action": "navigate",
            "url": "https://www.youtube.com/",
            "title": "YouTube",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal.status == "complete"
    assert state.goal.verified is True
    assert stamped["must_continue"] is False


async def test_verified_safari_search_still_opens_first_result(db_session) -> None:
    from app.voice.live.layer import register_live

    reset_computer_states()
    reset_live_registry()
    session = LiveSession(
        session_id="safari-first-nav",
        device_id="mac",
        backchannel_enabled=False,
    )
    calls: list[tuple[str, dict, str | None]] = []

    async def script(command, arguments=None, *, timeout=12.0, request_id=None):
        args = dict(arguments or {})
        calls.append((command, args, request_id))
        if args.get("action") == "search":
            query = str(args.get("query") or "YouTube")
            return {
                "ok": True,
                "executed": True,
                "verified": True,
                "app": "Safari",
                "action": "search",
                "query": query,
                "url": f"https://www.google.com/search?q={query}",
                "spoken": f"Safari is showing search results for {query}.",
            }
        if args.get("action") == "navigate":
            return {
                "ok": True,
                "executed": True,
                "verified": True,
                "app": "Safari",
                "action": "navigate",
                "url": "https://doc.rust-lang.org/book/",
                "title": "The Rust Programming Language",
                "spoken": "Opened the first result.",
            }
        return {"ok": True, "executed": True}

    session.request_computer = script  # type: ignore[method-assign]
    register_live(session)
    try:
        result = await handle_computer_tool(
            db_session,
            "app_action",
            {
                "app": "Safari",
                "action": "search",
                "query": "rust borrow checker",
                "goal": "In Safari, search for rust borrow checker and open the first result.",
            },
            actor="master",
            live_session_id="safari-first-nav",
            device_id="mac",
        )
        assert any(item[1].get("action") == "navigate" for item in calls)
        assert any(item[1].get("action") == "search" for item in calls)
        search_call = next(item[1] for item in calls if item[1].get("action") == "search")
        assert search_call.get("query") == "rust borrow checker"
        nav_call = next(item[1] for item in calls if item[1].get("action") == "navigate")
        assert nav_call.get("query") == "rust borrow checker"
        assert "google.com/search" not in str(result.get("url") or "").lower()
        assert result.get("verified") is True
        assert result.get("must_continue") is False
    finally:
        session.close()
        reset_live_registry()
        reset_computer_states()


def test_verified_new_tab_completes_once() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("safari-new-tab-once")
    note_goal(state, "Open a new tab in Safari")
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "must_continue": True,
            "app": "Safari",
            "action": "new_tab",
            "spoken": "Opened a new Safari tab.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status == "complete"
    assert state.goal.verified is True
    assert stamped["must_continue"] is False
    assert stamped["verified"] is True

    reset_computer_states()
    state = ensure_state("safari-close-tab-once")
    note_goal(state, "Close this tab in Safari")
    closed = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "must_continue": True,
            "app": "Safari",
            "action": "close_tab",
            "spoken": "Closed the Safari tab.",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal.status == "complete"
    assert closed["must_continue"] is False


def test_finder_close_windows_is_not_protected_quit() -> None:
    from app.ev.computer import _shape_lifecycle

    shaped = _shape_lifecycle(
        "close_app",
        {"name": "finder"},
        {
            "ok": True,
            "bundle_id": "com.apple.finder",
            "closed_windows": True,
            "closed": True,
            "quit": False,
            "spoken": "Closed Finder windows.",
            "name": "Finder",
        },
        source="mac_control",
    )
    assert shaped["ok"] is True
    assert "won't quit" not in str(shaped.get("spoken") or "").lower()
    assert "manually" not in str(shaped.get("spoken") or "").lower()

    evie = _shape_lifecycle(
        "close_app",
        {"name": "EV"},
        {
            "ok": True,
            "bundle_id": "com.ev.suit",
            "closed_windows": True,
            "closed": True,
            "quit": False,
            "spoken": "Closed EV windows.",
            "name": "EV",
        },
        source="mac_control",
    )
    assert evie["ok"] is True
    assert "won't quit" not in str(evie.get("spoken") or "").lower()

    refused = _shape_lifecycle(
        "close_app",
        {"name": "finder"},
        {
            "ok": False,
            "error": "protected",
            "bundle_id": "com.apple.finder",
            "spoken": "I won't quit Finder.",
            "name": "Finder",
        },
        source="mac_control",
    )
    assert refused["ok"] is False


def test_close_named_app_completes_once() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("close-slack-once")
    note_goal(state, "Close Slack")
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "name": "Slack",
            "app": "Slack",
            "spoken": "Closed Slack.",
        },
        state,
        name="close_app",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status == "complete"
    assert stamped["must_continue"] is False
    assert stamped["verified"] is True

    reset_computer_states()
    state = ensure_state("close-finder-once")
    note_goal(state, "Close the Finder app")
    dismissed = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "name": "Finder",
            "bundle_id": "com.apple.finder",
            "closed_windows": True,
            "spoken": "Closed Finder windows.",
        },
        state,
        name="close_app",
        executed=True,
    )
    assert state.goal.status == "complete"
    assert dismissed["must_continue"] is False


async def test_app_action_close_goal_dispatches_close_app(db_session) -> None:
    from app.voice.live.layer import register_live

    reset_computer_states()
    reset_live_registry()
    session = LiveSession(
        session_id="close-via-app-action",
        device_id="mac",
        backchannel_enabled=False,
    )
    calls: list[tuple[str, dict, str | None]] = []

    async def script(command, arguments=None, *, timeout=12.0, request_id=None):
        args = dict(arguments or {})
        calls.append((command, args, request_id))
        if command == "close_app":
            return {
                "ok": True,
                "name": args.get("name"),
                "app": args.get("name"),
                "spoken": f"Closed {args.get('name')}.",
            }
        raise AssertionError(f"close goal must not dispatch {command} {args}")

    session.request_computer = script  # type: ignore[method-assign]
    register_live(session)
    try:
        result = await handle_computer_tool(
            db_session,
            "app_action",
            {
                "app": "Slack",
                "action": "status",
                "goal": "Close Slack",
            },
            actor="master",
            live_session_id="close-via-app-action",
            device_id="mac",
        )
        assert calls and calls[0][0] == "close_app"
        assert calls[0][1].get("name") == "slack"
        assert result.get("ok") is True
        assert result.get("must_continue") is False
        assert "won't quit" not in str(result.get("spoken") or "").lower()
        assert "manually" not in str(result.get("spoken") or "").lower()
    finally:
        session.close()
        reset_live_registry()
        reset_computer_states()


def test_rewritten_search_keeps_first_result_intent() -> None:
    from app.ev.computer import _wants_open_first_result
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )

    reset_computer_states()
    state = ensure_state("rewrite-first-result")
    note_goal(state, "search for youtube and open the first result link")
    note_goal(state, "search for youtube")
    assert _wants_open_first_result(state) is True
    stamped = stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Safari",
            "action": "search",
            "query": "youtube",
            "url": "https://www.google.com/search?q=youtube",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status == "acting"
    assert stamped["must_continue"] is True


def test_owner_search_does_not_steal_into_play() -> None:
    from app.ev.computer import _wants_play_media, _wants_first_on_page_item
    from app.ev.computer_runtime import ensure_state, note_goal, reset_computer_states

    reset_computer_states()
    state = ensure_state("no-play-steal")
    note_goal(state, "Open Safari, and then in the new tab, search for youtube.com")
    note_goal(state, "play the first video appearing on the screen")
    assert state.original_owner_request
    assert "youtube.com" in state.original_owner_request.lower()
    assert _wants_play_media(state, "play the first video") is False
    assert _wants_first_on_page_item(state, "play the first video") is False


async def test_owner_safari_youtube_com_is_navigate_not_play(db_session) -> None:
    from app.voice.live.layer import register_live

    reset_computer_states()
    reset_live_registry()
    session = LiveSession(
        session_id="safari-youtube-intent",
        device_id="mac",
        backchannel_enabled=False,
    )
    calls: list[dict] = []

    async def script(command, arguments=None, *, timeout=12.0, request_id=None):
        args = dict(arguments or {})
        calls.append(args)
        if args.get("action") == "play":
            raise AssertionError("owner search/open must not dispatch play")
        query = str(args.get("query") or args.get("url") or "")
        return {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Safari",
            "action": args.get("action"),
            "query": query,
            "url": query if query.startswith("http") else f"https://{query}",
            "player_state": "none",
            "spoken": "Opened YouTube.",
        }

    session.request_computer = script  # type: ignore[method-assign]
    register_live(session)
    try:
        from app.ev.computer_runtime import ensure_state, note_goal

        state = ensure_state("safari-youtube-intent")
        note_goal(state, "Open Safari, and then in the new tab, search for youtube.com")
        polluted = await handle_computer_tool(
            db_session,
            "app_action",
            {
                "app": "Safari",
                "action": "search",
                "query": "youtube.com.open",
                "goal": "search for youtube.com.open",
            },
            actor="master",
            live_session_id="safari-youtube-intent",
            device_id="mac",
        )
        steal = await handle_computer_tool(
            db_session,
            "app_action",
            {
                "app": "Safari",
                "action": "play",
                "query": "first",
                "goal": "play the first video appearing on the screen",
            },
            actor="master",
            live_session_id="safari-youtube-intent",
            device_id="mac",
        )
        assert calls
        assert all(item.get("action") != "play" for item in calls)
        nav = next(item for item in calls if item.get("action") == "navigate")
        blob = f"{nav.get('query')} {nav.get('url')}".lower()
        assert "youtube.com" in blob
        assert ".open" not in blob
        assert polluted.get("verified") is True
        assert steal.get("verified") is True
    finally:
        session.close()
        reset_live_registry()
        reset_computer_states()


async def test_mixed_new_tab_search_still_opens_first_result(db_session) -> None:
    from app.voice.live.layer import register_live

    reset_computer_states()
    reset_live_registry()
    session = LiveSession(
        session_id="safari-mixed-tab",
        device_id="mac",
        backchannel_enabled=False,
    )
    calls: list[tuple[str, dict, str | None]] = []

    async def script(command, arguments=None, *, timeout=12.0, request_id=None):
        args = dict(arguments or {})
        calls.append((command, args, request_id))
        if args.get("action") == "new_tab":
            raise AssertionError("mixed search+first-result must not dispatch new_tab")
        if args.get("action") == "search":
            query = str(args.get("query") or "")
            return {
                "ok": True,
                "executed": True,
                "verified": True,
                "app": "Safari",
                "action": "search",
                "query": query,
                "url": f"https://www.google.com/search?q={query}",
                "spoken": f"Safari is showing search results for {query}.",
            }
        if args.get("action") == "navigate":
            return {
                "ok": True,
                "executed": True,
                "verified": True,
                "app": "Safari",
                "action": "navigate",
                "url": "https://en.wikipedia.org/wiki/OpenAI",
                "title": "OpenAI",
                "spoken": "Opened the first result.",
            }
        return {"ok": True, "executed": True}

    session.request_computer = script  # type: ignore[method-assign]
    register_live(session)
    try:
        result = await handle_computer_tool(
            db_session,
            "app_action",
            {
                "app": "Safari",
                "action": "new_tab",
                "goal": (
                    "Open a new tab and search for OpenAI and open the first result link"
                ),
            },
            actor="master",
            live_session_id="safari-mixed-tab",
            device_id="mac",
        )
        assert any(item[1].get("action") == "search" for item in calls)
        assert any(item[1].get("action") == "navigate" for item in calls)
        search_call = next(item[1] for item in calls if item[1].get("action") == "search")
        assert search_call.get("query") == "OpenAI"
        assert "google.com/search" not in str(result.get("url") or "").lower()
        assert result.get("verified") is True
        assert result.get("must_continue") is False
    finally:
        session.close()
        reset_live_registry()
        reset_computer_states()


def test_web_research_is_not_a_safari_search() -> None:
    from app.ev.computer_strategy import (
        looks_like_web_research,
        resolve_browser_computer_goal,
        resolve_in_app_computer_goal,
        web_search_query_from_text,
        wants_first_on_page_item,
        _search_query_from_goal,
    )

    book = (
        "search it on the web and give me info what this book is about"
    )
    assert looks_like_web_research(book)
    assert resolve_browser_computer_goal(book) is None
    assert resolve_in_app_computer_goal(book) is None
    assert "search it on the web" in web_search_query_from_text(book).lower()

    titled = "search the web for AI and Machine Learning for Coders"
    assert looks_like_web_research(titled)
    assert "AI and Machine Learning for Coders" in web_search_query_from_text(titled)

    domain_play = "search for youtube.com and play the first video that appears on the screen"
    assert not looks_like_web_research(domain_play)
    assert wants_first_on_page_item(domain_play)
    assert _search_query_from_goal(domain_play).lower() == "youtube.com"
    routed = resolve_in_app_computer_goal(domain_play)
    assert routed is not None
    assert routed[1]["action"] == "navigate"
    assert "youtube.com" in routed[1]["query"].lower()

    music = "Open Music, find the Chess playlist, and play the first track."
    assert not looks_like_web_research(music)
    music_goal = resolve_in_app_computer_goal(music)
    assert music_goal is not None
    assert music_goal[1]["app"] == "Music"


def test_first_on_page_item_does_not_complete_a_homepage_land() -> None:
    from app.ev.computer_runtime import (
        ensure_state,
        note_goal,
        reset_computer_states,
        stamp_computer_receipt,
    )
    from app.ev.computer_strategy import looks_like_opened_content_item

    assert not looks_like_opened_content_item("https://www.youtube.com/")
    assert not looks_like_opened_content_item("https://www.youtube.com/feed/you")
    assert looks_like_opened_content_item("https://www.youtube.com/watch?v=abc")
    assert not looks_like_opened_content_item("https://www.google.com/search?q=cats")

    reset_computer_states()
    state = ensure_state("first-video")
    note_goal(state, "search for youtube.com and play the first video")
    stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Safari",
            "action": "navigate",
            "url": "https://www.youtube.com/",
            "title": "YouTube",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal is not None
    assert state.goal.status == "acting"
    stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Safari",
            "action": "play",
            "url": "https://www.youtube.com/watch?v=abc",
            "title": "First video",
            "player_state": "paused",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal.status == "acting"
    stamp_computer_receipt(
        {
            "ok": True,
            "executed": True,
            "verified": True,
            "app": "Safari",
            "action": "play",
            "url": "https://www.youtube.com/watch?v=abc",
            "title": "First video",
            "player_state": "playing",
        },
        state,
        name="app_action",
        executed=True,
    )
    assert state.goal.status == "complete"


async def test_domain_land_then_opens_first_on_page_item(db_session) -> None:
    from app.voice.live.layer import register_live

    reset_computer_states()
    reset_live_registry()
    session = LiveSession(
        session_id="first-on-page",
        device_id="mac",
        backchannel_enabled=False,
    )
    calls: list[tuple[str, dict, str | None]] = []

    async def script(command, arguments=None, *, timeout=12.0, request_id=None):
        args = dict(arguments or {})
        calls.append((command, args, request_id))
        query = str(args.get("query") or args.get("url") or "")
        if "youtube.com" in query.lower() and "/watch" not in query.lower() and "results" not in query.lower():
            return {
                "ok": True,
                "executed": True,
                "verified": True,
                "app": "Safari",
                "action": "navigate",
                "url": "https://www.youtube.com/",
                "title": "YouTube",
            }
        if args.get("action") == "play":
            return {
                "ok": True,
                "executed": True,
                "verified": True,
                "app": "Safari",
                "action": "play",
                "url": "https://www.youtube.com/watch?v=abc",
                "title": "First video",
                "clicked": "https://www.youtube.com/watch?v=abc",
                "player_state": "playing",
            }
        return {"ok": True, "executed": True, "url": "https://www.youtube.com/"}

    session.request_computer = script  # type: ignore[method-assign]
    register_live(session)
    try:
        result = await handle_computer_tool(
            db_session,
            "app_action",
            {
                "app": "Safari",
                "action": "navigate",
                "query": "https://youtube.com",
                "url": "https://youtube.com",
                "goal": "search for youtube.com and play the first video that appears",
            },
            actor="master",
            live_session_id="first-on-page",
            device_id="mac",
        )
        nav_calls = [item[1] for item in calls if item[1].get("action") == "navigate"]
        play_calls = [item[1] for item in calls if item[1].get("action") == "play"]
        assert len(nav_calls) >= 1
        assert play_calls
        assert any(
            "youtube.com" in str(item.get("query") or item.get("url") or "").lower()
            for item in nav_calls
        )
        assert play_calls[0].get("query") == "first"
        assert "watch" in str(result.get("url") or "").lower()
        assert result.get("player_state") == "playing"
        assert result.get("verified") is True
    finally:
        session.close()
        reset_live_registry()
        reset_computer_states()


def test_play_any_named_or_local_video_routes_and_verifies() -> None:
    from app.ev.computer_strategy import (
        adapter_for,
        looks_like_local_media_goal,
        media_query_from_goal,
        navigation_url_in_utterance,
        resolve_in_app_computer_goal,
        resolve_screen_observation_goal,
        video_search_url,
        wants_play_media,
    )

    assert "play" in adapter_for("Safari")["supported_actions"]
    assert "play" in adapter_for("Chrome")["supported_actions"]
    assert "play" in adapter_for("Finder")["supported_actions"]

    sentence = "open YouTube.com in Safari and play the first video appearing on the screen"
    assert navigation_url_in_utterance(sentence) == "https://YouTube.com"
    assert wants_play_media(sentence)
    assert resolve_screen_observation_goal(sentence) is None
    opened = resolve_in_app_computer_goal(sentence)
    assert opened is not None
    assert opened[1]["action"] == "navigate"
    assert "youtube.com" in opened[1]["query"].lower()

    safari_only = resolve_in_app_computer_goal("Open Safari and play the first video")
    assert safari_only == ("app_action", {"app": "Safari", "action": "play", "query": "first"})

    named = resolve_in_app_computer_goal("search youtube for never gonna give you up and play it")
    assert named is not None
    assert named[1]["action"] == "navigate"
    assert "search_query=" in named[1]["query"]
    assert "never" in named[1]["query"].lower()

    on_site = resolve_in_app_computer_goal("play never gonna give you up on youtube")
    assert on_site is not None
    assert "search_query=" in on_site[1]["query"]
    assert media_query_from_goal("play never gonna give you up on youtube").lower().startswith("never")

    vimeo = video_search_url("https://vimeo.com", "cats")
    assert "vimeo.com/search" in vimeo

    local = "open Finder and play the video called vacation"
    assert looks_like_local_media_goal(local)
    finder = resolve_in_app_computer_goal(local)
    assert finder is not None
    assert finder[1]["app"] == "Finder"
    assert finder[1]["action"] == "play"
    assert "vacation" in finder[1]["query"].lower()

    first_local = resolve_in_app_computer_goal("open finder and play the first video")
    assert first_local == ("app_action", {"app": "Finder", "action": "play", "query": "first"})

    music = resolve_in_app_computer_goal(
        "Open Music, find the Chess playlist, and play the first track."
    )
    assert music is not None
    assert music[1]["app"] == "Music"
    assert not wants_play_media("Open Music, find the Chess playlist, and play the first track.")

    youtube_word = resolve_in_app_computer_goal(
        "In Safari, search for YouTube and open the first result."
    )
    assert youtube_word is not None
    assert youtube_word[1]["action"] == "search"
    assert youtube_word[1]["query"] == "YouTube"


async def test_named_video_search_then_plays(db_session) -> None:
    from app.voice.live.layer import register_live

    reset_computer_states()
    reset_live_registry()
    session = LiveSession(
        session_id="named-video",
        device_id="mac",
        backchannel_enabled=False,
    )
    calls: list[tuple[str, dict, str | None]] = []

    async def script(command, arguments=None, *, timeout=12.0, request_id=None):
        args = dict(arguments or {})
        calls.append((command, args, request_id))
        query = str(args.get("query") or args.get("url") or "")
        if "results?search_query=" in query:
            return {
                "ok": True,
                "executed": True,
                "verified": True,
                "app": "Safari",
                "action": "navigate",
                "url": "https://www.youtube.com/results?search_query=cats",
                "title": "cats - YouTube",
            }
        if args.get("action") == "play":
            return {
                "ok": True,
                "executed": True,
                "verified": True,
                "app": "Safari",
                "action": "play",
                "url": "https://www.youtube.com/watch?v=cats1",
                "title": "Cats",
                "player_state": "playing",
            }
        return {"ok": True, "executed": True, "url": query}

    session.request_computer = script  # type: ignore[method-assign]
    register_live(session)
    try:
        result = await handle_computer_tool(
            db_session,
            "app_action",
            {
                "app": "Safari",
                "action": "navigate",
                "query": "https://www.youtube.com/results?search_query=cats",
                "url": "https://www.youtube.com/results?search_query=cats",
                "goal": "search youtube for cats and play it",
            },
            actor="master",
            live_session_id="named-video",
            device_id="mac",
        )
        assert any(item[1].get("action") == "play" for item in calls)
        assert result.get("player_state") == "playing"
        assert result.get("verified") is True
    finally:
        session.close()
        reset_live_registry()
        reset_computer_states()


