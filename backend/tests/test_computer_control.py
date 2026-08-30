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


def test_computer_status_and_inspect_are_owner_auto() -> None:
    for name in ("computer_status", "inspect_ui", "list_apps", "screen_look"):
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
