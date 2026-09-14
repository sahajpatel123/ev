"""Companion loop: visual memory, heading-out, visor HUD, verified computer hands."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.computer_strategy import adapter_for
from app.ev.continuity import classify_memory_intent
from app.ev.tool_select import (
    LIVE_VOICE_TOOLS,
    parse_heading_out,
    resolve_live_action,
    select_tool,
)
from app.ev.tools import get_spec
from app.memory.life_archive.locate import classify_shelf
from app.memory.recall import build_explicit_recall_payload
from app.memory.visual import (
    is_keep_recall_query,
    is_visual_recall_query,
    keep_owner_spoken,
    persist_keep_intent,
    persist_visual_observation,
    remember_spoken_scene,
    visual_content_tokens,
    visual_observation_matches,
    visual_observation_text,
    wants_current_visual,
    wants_keep_visible,
    wants_past_visual,
)
from app.schemas import RouteBriefingOut
from app.utils.text import utcnow


async def _await_s2s(live, event):
    """Wait for transcript routing. emit() only schedules it for grok/realtime."""

    routed = await live.emit(event)
    if routed is not None:
        await routed
    return routed


def test_visual_observation_text_keeps_object_color_and_path() -> None:
    text = visual_observation_text(
        labels=["remote", "person"],
        colors=["white"],
        people=1,
        media_kind="video",
        saved_path="/Users/sahaj/Movies/wave.mov",
        spoken="A person in a white shirt waving at the camera, holding a remote.",
    )
    lowered = text.lower()
    assert "recorded a video clip" in lowered
    assert "remote" in lowered
    assert "white" in lowered
    assert "waving" in lowered
    assert "/Users/sahaj/Movies/wave.mov" in text


def test_visual_recall_phrases_route_to_memory_not_live_look() -> None:
    assert is_visual_recall_query("the white remote")
    assert is_visual_recall_query("that clip of me waving")
    assert is_visual_recall_query("what did you see")
    assert is_visual_recall_query("what was I wearing")
    assert is_visual_recall_query("when was the last time you saw me with this remote")
    assert is_visual_recall_query("what t-shirt was I wearing earlier")
    assert not is_visual_recall_query("what t-shirt am I wearing")
    assert not is_visual_recall_query("what am I wearing")
    assert wants_current_visual("what t-shirt am I wearing")
    assert wants_past_visual("when was the last time you saw me with this remote")
    assert not wants_current_visual("when was the last time you saw me with this remote")
    assert classify_memory_intent("that clip of me waving") == "explicit_recall"
    assert classify_memory_intent("when was the last time you saw me with this remote") == (
        "explicit_recall"
    )
    assert classify_shelf("that photo you took of the remote") is None
    assert classify_shelf("photos from 2019") == "photos"
    assert select_tool("the white remote").selected == "search_memory"
    assert select_tool("that clip of me waving").selected == "search_memory"
    assert select_tool("what am I holding").selected == "look"
    assert select_tool("what t-shirt am I wearing").selected == "look"
    assert select_tool("what t-shirt was I wearing earlier").selected == "search_memory"
    assert select_tool("when was the last time you saw me with this remote").selected == (
        "search_memory"
    )
    resolved = resolve_live_action("when was the last time you saw me with this remote")
    assert resolved is not None
    assert resolved[0] == "search_memory"
    from app.memory.room import looks_like_object_locate

    assert looks_like_object_locate("where did I leave my charger")
    assert looks_like_object_locate("where's my keys")
    assert not looks_like_object_locate("where is Rahul")
    assert not looks_like_object_locate("where's my mom")
    assert not looks_like_object_locate("where is the file saved")
    assert is_visual_recall_query("where did I leave my charger")
    assert not is_visual_recall_query("where is Rahul")
    assert select_tool("where did I leave my charger").selected == "search_memory"
    assert select_tool("where's my charger").selected == "search_memory"


def test_keep_visible_routes_to_look_not_a_glance_refusal() -> None:
    assert wants_keep_visible("memorise this")
    assert wants_keep_visible("memorize this")
    assert wants_keep_visible("remember this")
    assert not wants_keep_visible("Remember that I'm calling this experiment Project Harbor.")
    assert not wants_keep_visible("Do you remember what I asked you to keep?")
    assert wants_keep_visible("memorize a lantern")
    assert wants_keep_visible("remember this mug")
    showing = (
        "Okay, so I want you to open camera and remember the item I am showing you. "
        "This is my iPhone 16 Pro, my primary phone."
    )
    assert wants_keep_visible(showing)
    assert wants_keep_visible("remember my iPhone 16 Pro")
    assert wants_keep_visible("open camera and remember the item I am showing you")
    from app.ev.computer_strategy import looks_like_computer_task
    from app.memory.visual import is_clarity_hedge

    assert not looks_like_computer_task(showing)
    assert looks_like_computer_task("open Photo Booth")
    assert is_clarity_hedge("I cannot see the phone clearly")
    assert is_clarity_hedge("I can't see it clearly")
    assert not is_clarity_hedge("You've got it held up pretty clearly")
    assert select_tool(showing).selected == "look"
    assert select_tool("remember my iPhone 16 Pro").selected == "look"
    assert select_tool("Call Ned").selected == "place_call"
    shown = resolve_live_action(showing)
    assert shown is not None and shown[0] == "look"
    mine = resolve_live_action("remember my iPhone 16 Pro")
    assert mine is not None and mine[0] == "look"
    mummy = resolve_live_action("What did mummy send on WhatsApp?")
    assert mummy is not None and mummy[0] in {"recall", "search_memory"}
    from app.voice.live.grok_voice import remap_keep_sight_call

    remapped, args = remap_keep_sight_call(
        "computer",
        {"goal": "open Photo Booth"},
        last_transcript=showing,
    )
    assert remapped == "look"
    assert "prompt" in args
    look_args = remap_keep_sight_call(
        "look",
        {"objective": showing, "detail": "high", "focus": "auto"},
        last_transcript=showing,
    )
    assert look_args[0] == "look"
    assert "iphone" in str(look_args[1].get("prompt") or "").lower()
    safari, safari_args = remap_keep_sight_call(
        "computer",
        {"goal": "open Safari"},
        last_transcript="open Safari",
    )
    assert safari == "computer"
    assert safari_args["goal"] == "open Safari"
    recall_keep, recall_args = remap_keep_sight_call(
        "recall",
        {"query": "Mummy"},
        last_transcript="I am holding something. Memorize this.",
    )
    assert recall_keep == "look"
    assert "memorize" in str(recall_args.get("prompt") or "").lower()
    keep_recall, keep_recall_args = remap_keep_sight_call(
        "recall",
        {"query": "What did I just ask you to remember?"},
        last_transcript="What did I just ask you to remember?",
    )
    assert keep_recall == "recall"
    assert keep_recall_args["query"] == "What did I just ask you to remember?"
    web_keep, web_args = remap_keep_sight_call(
        "search_web",
        {"query": "what's in my hand", "limit": 3},
        last_transcript="I am holding something. Memorize this.",
    )
    assert web_keep == "look"
    web_weather, weather_args = remap_keep_sight_call(
        "search_web",
        {"query": "weather in Surat", "limit": 3},
        last_transcript="Other like today?",
    )
    assert web_weather == "search_web"
    assert weather_args["query"] == "weather in Surat"
    holding_look = (
        "Look at the thing I'm holding in my hand. I want you to look at it "
        "and tell me more info about this item I'm holding"
    )
    web_hold, _ = remap_keep_sight_call(
        "search_web",
        {"query": "item in hand", "limit": 3},
        last_transcript=holding_look,
    )
    assert web_hold == "look"
    computer_hold, _ = remap_keep_sight_call(
        "computer",
        {"goal": "open Photo Booth"},
        last_transcript=holding_look,
    )
    assert computer_hold == "look"
    from app.ev.spark_look import fallback_camera_action
    from app.memory.visual import wants_held_object_look
    from app.voice.live.session import _owner_memory_live_action

    assert wants_current_visual(holding_look)
    assert wants_held_object_look(holding_look)
    assert not wants_keep_visible(holding_look)
    assert fallback_camera_action(holding_look) == "look"
    assert _owner_memory_live_action(holding_look) == (
        "look",
        {"prompt": holding_look[:400], "focus": "auto"},
    )
    assert fallback_camera_action("look this up on the web") is None
    assert not wants_held_object_look("I'm holding a meeting at three")
    assert not wants_keep_visible("did you memorize the lantern")
    assert not wants_keep_visible("did you remember the mug")
    assert not wants_keep_visible("have you memorized the lantern")
    assert not is_visual_recall_query("memorise this")
    assert is_visual_recall_query("what did I ask you to remember")
    assert is_visual_recall_query("what did I just ask you to remember")
    assert is_keep_recall_query("what did I just ask you to remember")
    assert is_keep_recall_query("Just ask you to remember.")
    assert is_keep_recall_query("just ask you to remember")
    assert is_keep_recall_query("What I ask you to memorize or remember")
    assert is_keep_recall_query("What I ask you to memorize and remember")
    assert is_keep_recall_query("What I tell you to memorize")
    assert wants_keep_visible("memorize this")
    assert not is_keep_recall_query("memorize this")
    from app.memory.visual import is_keep_recall_echo

    assert is_keep_recall_echo("What I ask you to memorize and remember")
    assert is_keep_recall_echo("What I ask you to memorize or remember?")
    assert not is_keep_recall_echo(
        "A Davidoff mocha chewing coffee candy tin with gold lettering."
    )
    assert not wants_keep_visible("Just ask you to remember.")
    assert classify_memory_intent("what did I just ask you to remember") == "explicit_recall"
    assert classify_memory_intent("Just ask you to remember.") == "explicit_recall"
    assert select_tool("what did I just ask you to remember").selected == "search_memory"
    just = resolve_live_action("What did I just ask you to remember?")
    assert just is not None and just[0] == "search_memory"
    asr = resolve_live_action("Just ask you to remember.")
    assert asr is not None and asr[0] == "search_memory"
    assert is_visual_recall_query("what was I showing")
    assert is_keep_recall_query("did you memorize the lantern")
    assert is_keep_recall_query("did you remember the mug")
    assert is_visual_recall_query("did you remember the lantern")
    assert is_visual_recall_query("have you memorized the mug")
    assert classify_memory_intent("memorise this") == "pin"
    assert classify_memory_intent("what did I ask you to remember") == "explicit_recall"
    assert classify_memory_intent("did you remember the lantern") == "explicit_recall"
    assert classify_memory_intent("did you memorize the mug") == "explicit_recall"
    assert select_tool("memorise this").selected == "look"
    assert select_tool("memorize a lantern").selected == "look"
    assert select_tool("what did I ask you to remember").selected == "search_memory"
    assert select_tool("did you remember the lantern").selected == "search_memory"
    assert select_tool("did you memorize the mug").selected == "search_memory"
    looked = resolve_live_action("memorise this")
    assert looked is not None
    assert looked[0] == "look"
    recalled = resolve_live_action("did you remember the lantern")
    assert recalled is not None
    assert recalled[0] == "search_memory"
    assert "memory_type" not in recalled[1]
    book = resolve_live_action("did you remember the book")
    assert book is not None
    assert book[0] == "search_memory"
    showed = resolve_live_action("what book was I holding")
    assert showed is not None
    assert showed[0] == "search_memory"
    holding = resolve_live_action("what was I holding")
    assert holding is not None
    assert holding[0] == "search_memory"
    titled = resolve_live_action("what was the book called")
    assert titled is not None
    assert titled[0] == "search_memory"
    from app.ev.look import KEEP_LOOK_PROMPT, resolve_keep_request

    assert resolve_keep_request("memorize a lantern") == "memorize a lantern"
    assert resolve_keep_request("Describe visible people, objects, colors, and the scene.") == ""
    assert resolve_keep_request(KEEP_LOOK_PROMPT) == ""
    prefer = resolve_live_action("What did I prefer before?")
    assert prefer is not None
    assert prefer[0] == "search_memory"
    solved = resolve_live_action("What did we solve?")
    assert solved is not None
    assert solved[0] == "search_memory"
    assert select_tool("What did I prefer before?").selected == "search_memory"
    assert select_tool("What did we solve?").selected == "search_memory"
    assert select_tool("Where did we leave off?").selected == "search_memory"
    assert select_tool("who is Maya?").selected == "get_person"
    leave = resolve_live_action("Where did we leave off?")
    assert leave is not None
    assert leave[0] == "search_memory"


def test_mini_look_prompt_keeps_owner_memorize_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ev.look import KEEP_LOOK_PROMPT, resolve_keep_request

    monkeypatch.setattr(
        "app.ev.look.live_owner_transcript",
        lambda *args, **kwargs: "memorize this",
    )
    assert resolve_keep_request(KEEP_LOOK_PROMPT, live_session_id="live-1") == "memorize this"


def test_reuse_keep_frame_peeks_hold_copy() -> None:
    from app.ev.camera_runtime import (
        CameraObservation,
        peek_observations,
        reset_pending_observations,
        stash_observation,
    )
    from app.ev.look import KEEP_HOLD_CALL_ID, _reuse_pending_keep_frame

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 40 + b"\xff\xd9"
    stash_observation(
        CameraObservation(
            request_id="owner-keep",
            call_id=KEEP_HOLD_CALL_ID,
            jpeg=jpeg,
            width=1280,
            height=720,
            detail="high",
        )
    )
    frame = _reuse_pending_keep_frame("call-mini-look")
    assert frame is not None
    assert frame.jpeg == jpeg
    assert frame.attachment_id is None
    broker = _reuse_pending_keep_frame("owner-keep")
    assert broker is not None
    assert broker.jpeg == jpeg
    held = peek_observations(KEEP_HOLD_CALL_ID)
    assert held and held[0].jpeg == jpeg
    reset_pending_observations()


def test_reuse_keep_frame_carries_attachment_uuid() -> None:
    from app.ev.camera_runtime import (
        CameraObservation,
        peek_observations,
        reset_pending_observations,
        stash_observation,
    )
    from app.ev.look import KEEP_HOLD_CALL_ID, _reuse_pending_keep_frame

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 40 + b"\xff\xd9"
    aid = "11111111-1111-1111-1111-111111111111"
    stash_observation(
        CameraObservation(
            request_id=aid,
            call_id=KEEP_HOLD_CALL_ID,
            jpeg=jpeg,
            width=1280,
            height=720,
            detail="high",
        )
    )
    frame = _reuse_pending_keep_frame("call-mini-look")
    assert frame is not None
    assert frame.jpeg == jpeg
    assert frame.attachment_id == aid
    held = peek_observations(KEEP_HOLD_CALL_ID)
    assert held and held[0].request_id == aid
    reset_pending_observations()


def test_stale_keep_hold_is_not_reused() -> None:
    from app.ev.camera_runtime import (
        CameraObservation,
        now_mono,
        reset_pending_observations,
        stash_observation,
    )
    from app.ev.look import KEEP_HOLD_CALL_ID, _reuse_pending_keep_frame

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 40 + b"\xff\xd9"
    stash_observation(
        CameraObservation(
            request_id="old-hold",
            call_id=KEEP_HOLD_CALL_ID,
            jpeg=jpeg,
            width=1280,
            height=720,
            detail="high",
            t0=now_mono() - 60.0,
        )
    )
    assert _reuse_pending_keep_frame("owner-keep") is None
    assert _reuse_pending_keep_frame("call-mini-look") is None
    reset_pending_observations()


def test_visual_content_match_ignores_question_scaffolding() -> None:
    stored = "I looked. You're holding a black remote."
    query = "when was the last time you saw me with this remote"
    assert "remote" in visual_content_tokens(query)
    assert "last" not in visual_content_tokens(query)
    assert visual_observation_matches(query, stored)
    assert not visual_observation_matches(query, "I looked of a person. Colors: black.")
    prompt = (
        "A current camera image is attached. Describe people. "
        "Grounding: a person; colors: black. Image 1280 by 720."
    )
    text = visual_observation_text(
        labels=["person"],
        colors=["black"],
        people=1,
        spoken=prompt,
    )
    lowered = text.lower()
    assert "person" in lowered
    assert "black" in lowered
    assert "describe people" not in lowered
    named = visual_observation_text(
        labels=["person"],
        people=1,
        spoken="A person is in front of the camera.",
        keep_named="lantern",
    )
    assert "lantern" in named.lower()
    assert visual_observation_matches("did you remember the lantern", named)


def test_heading_out_is_one_live_beat() -> None:
    assert "heading_out" in LIVE_VOICE_TOOLS
    spec = get_spec("heading_out")
    assert spec is not None
    assert spec["risk_class"] == "R0"
    assert select_tool("I'm heading out.").selected == "heading_out"
    assert select_tool("Gotta go").selected == "heading_out"
    assert select_tool("What's the weather?").selected == "get_weather"
    resolved = resolve_live_action("I'm heading out, text Maya I'm late")
    assert resolved is not None
    assert resolved[0] == "heading_out"
    assert resolved[1]["notify_to"] == "Maya"
    parsed = parse_heading_out("I'm leaving this file open")
    assert parsed is None


@pytest.mark.asyncio
async def test_visual_observation_is_recallable(db_session: AsyncSession) -> None:
    result = {
        "ok": True,
        "labels": ["remote"],
        "colors": ["white"],
        "person_count": 1,
        "media_kind": "photo",
        "saved_path": "/Users/sahaj/Pictures/EV/remote.jpg",
        "spoken": "A person holding a white remote.",
        "request_id": "look-test-1",
    }
    written = await persist_visual_observation(
        db_session, result, actor="owner", device_id="mac-1"
    )
    await db_session.commit()
    assert written is not None
    assert result.get("remembered") is True
    pack = await build_explicit_recall_payload(db_session, "the white remote", k=6)
    blob = " ".join(str(item.get("text") or "") for item in pack.get("evidence") or []).lower()
    assert "remote" in blob
    assert "white" in blob
    assert "/users/sahaj/pictures/ev/remote.jpg" in blob


@pytest.mark.asyncio
async def test_boilerplate_look_plus_spoken_scene_is_recallable(
    db_session: AsyncSession,
) -> None:
    prompt = (
        "A current camera image is attached. Describe it to the owner. "
        "Grounding: a person; colors: black. Image 1280 by 720."
    )
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["person"],
            "colors": ["black"],
            "person_count": 1,
            "media_kind": "frame",
            "spoken": prompt,
            "request_id": "look-boilerplate-1",
            "keep_request": "memorize this",
            "attachment_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert written is not None
    empty = await build_explicit_recall_payload(
        db_session,
        "when was the last time you saw me with this remote",
        k=6,
    )
    empty_blob = " ".join(
        str(item.get("text") or "") for item in empty.get("evidence") or []
    ).lower()
    assert "remote" not in empty_blob
    upgraded = await remember_spoken_scene(
        db_session,
        "You're holding a black remote in your right hand.",
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert upgraded is not None
    pack = await build_explicit_recall_payload(
        db_session,
        "when was the last time you saw me with this remote",
        k=6,
    )
    blob = " ".join(str(item.get("text") or "") for item in pack.get("evidence") or []).lower()
    assert "remote" in blob
    assert pack.get("grounding") == "evidence"


@pytest.mark.asyncio
async def test_clothing_then_and_now_are_separate_observations(
    db_session: AsyncSession,
) -> None:
    first = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["person"],
            "colors": ["black"],
            "person_count": 1,
            "media_kind": "frame",
            "spoken": "You're wearing a black t-shirt.",
            "request_id": "look-shirt-1",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    second = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["person"],
            "colors": ["white"],
            "person_count": 1,
            "media_kind": "frame",
            "spoken": "You're wearing a white t-shirt.",
            "request_id": "look-shirt-2",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert first is not None and second is not None
    assert select_tool("what t-shirt am I wearing").selected == "look"
    earlier = await build_explicit_recall_payload(
        db_session, "what t-shirt was I wearing earlier", k=6
    )
    blob = " ".join(
        str(item.get("text") or "") for item in earlier.get("evidence") or []
    ).lower()
    assert "black" in blob
    assert "white" in blob
    assert "t-shirt" in blob or "tshirt" in blob or "shirt" in blob
    texts = [str(item.get("text") or "") for item in earlier.get("evidence") or []]
    black_hit = next(item for item in texts if "black" in item.lower())
    white_hit = next(item for item in texts if "white" in item.lower())
    assert black_hit != white_hit


@pytest.mark.asyncio
async def test_keep_visible_look_is_recallable_later(
    db_session: AsyncSession,
) -> None:
    from app.memory.extraction import Extractor
    from app.memory.turns import record_conversation_turn
    from app.memory.visual import keep_topic, looks_like_visual_description

    refusal = (
        "I can't memorise it automatically once I glanced, unless it's stored "
        "in memory."
    )
    assert not looks_like_visual_description(refusal)
    assert keep_topic("memorise this bottle") == "bottle"
    assert keep_topic("memorize a lantern") == "lantern"
    assert keep_topic("remember the mug") == "mug"
    assert keep_topic("remember this") == "this"
    user = await record_conversation_turn(
        db_session,
        text="memorise this",
        role="user",
        source="voice",
        conversation_id=None,
        device_id="mac-1",
        actor="owner",
    )
    await db_session.commit()
    assert user is not None
    extracted = Extractor().extract(user)
    assert not any("Observed: memorise this" in (item.text or "") for item in extracted)
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["bottle"],
            "colors": ["white"],
            "person_count": 0,
            "media_kind": "frame",
            "ocr_text": "Harbor Spring",
            "spoken": "You're holding a white bottle labeled Harbor Spring.",
            "request_id": "look-keep-1",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert written is not None
    pack = await build_explicit_recall_payload(
        db_session, "what did I ask you to remember", k=6
    )
    blob = " ".join(str(item.get("text") or "") for item in pack.get("evidence") or []).lower()
    assert "harbor spring" in blob
    assert pack.get("grounding") == "evidence"


@pytest.mark.asyncio
async def test_named_keep_request_on_look_survives_missing_user_turn(
    db_session: AsyncSession,
) -> None:
    from app.memory.visual import looks_like_visual_description

    hedge = (
        "I can see what you have in front of the camera, but for future "
        "reference, I cannot guarantee that I am able to tell you in future "
        "about it."
    )
    assert not looks_like_visual_description(hedge)
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["person"],
            "colors": ["black"],
            "person_count": 1,
            "media_kind": "frame",
            "spoken": "A person is in front of the camera.",
            "keep_request": "memorize a lantern",
            "request_id": "look-keep-named-1",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert written is not None
    later = await build_explicit_recall_payload(
        db_session, "did you remember the lantern", k=6
    )
    blob = " ".join(
        str(item.get("text") or "") for item in later.get("evidence") or []
    ).lower()
    assert "lantern" in blob
    assert later.get("grounding") == "evidence"
    other = await build_explicit_recall_payload(
        db_session, "did you memorize the lantern", k=6
    )
    other_blob = " ".join(
        str(item.get("text") or "") for item in other.get("evidence") or []
    ).lower()
    assert "lantern" in other_blob
    assert select_tool("what t-shirt am I wearing").selected == "look"


@pytest.mark.asyncio
async def test_heading_out_composes_weather_calendar_and_optional_text(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev.workbench import handle_heading_out

    async def fake_route(_session):
        return RouteBriefingOut(
            generated_at=utcnow(),
            destination="Design review",
            leave_by=datetime(2026, 9, 1, 14, 15, tzinfo=UTC).isoformat(),
            travel_time_minutes=25,
            notes=["Next commitment from live calendar: Design review at 15:00."],
        )

    async def fake_weather(query: str, limit: int = 3, **_kwargs):
        return [
            SimpleNamespace(
                title="Weather",
                url="https://example.test/weather",
                snippet="Clear and 72 degrees.",
            )
        ]

    monkeypatch.setattr("app.ev.navigation.route_briefing", fake_route)
    monkeypatch.setattr("app.search.live.weather_results", fake_weather)

    async def fake_origin(_session):
        return "home"

    monkeypatch.setattr("app.ev.travel.owner_coarse_origin", fake_origin)

    out = await handle_heading_out(db_session, actor="owner")
    assert out["ok"] is True
    spoken = out["spoken"].lower()
    assert "72" in spoken or "clear" in spoken
    assert "design review" in spoken
    assert "leave by" in spoken
    assert out["evidence"]["leave_by"]
    assert out["hud"]["title"] == "Heading out"


def test_visor_and_hands_contracts() -> None:
    assert adapter_for("Chrome")["supported_actions"].count("open_item") == 1
    chrome_actions = adapter_for("Chrome")["supported_actions"]
    assert "search" in chrome_actions and "open_item" in chrome_actions
    assert "open_item" in adapter_for("Spotify")["supported_actions"]
    assert adapter_for("Notes")["verification"] == "note_body"
    from app.ev.look import _live_image_result

    result = _live_image_result(
        request_id="v1",
        source="live_camera",
        width=320,
        height=240,
        encoded_bytes=12,
        camera_name="FaceTime",
        focus="auto",
        labels=["remote"],
        colors=["white"],
        spoken="A white remote on the desk.",
        saved_path="/tmp/remote.jpg",
        media_kind="photo",
    )
    meta = result["hud"]["meta"]
    assert meta["visor"] is True
    assert meta["saved_path"] == "/tmp/remote.jpg"
    assert result["observe"] is False


@pytest.mark.asyncio
async def test_memorize_book_survives_restart_recall(db_session: AsyncSession) -> None:
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["book"],
            "colors": ["red"],
            "person_count": 0,
            "media_kind": "frame",
            "ocr_text": "The Pragmatic Programmer",
            "spoken": "You're holding a red book titled The Pragmatic Programmer.",
            "keep_request": "memorize this book",
            "request_id": "look-book-1",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert written is not None
    pack = await build_explicit_recall_payload(db_session, "did you remember the book", k=6)
    json.dumps(pack)
    blob = " ".join(str(item.get("text") or "") for item in pack.get("evidence") or []).lower()
    spoken = str(pack.get("spoken") or "").lower()
    assert pack.get("grounding") == "evidence"
    assert "book" in blob
    assert "pragmatic" in blob or "pragmatic" in spoken
    later = await build_explicit_recall_payload(db_session, "what book was I holding", k=6)
    later_blob = " ".join(
        str(item.get("text") or "") for item in later.get("evidence") or []
    ).lower()
    assert "book" in later_blob
    assert later.get("grounding") == "evidence"
    called = await build_explicit_recall_payload(db_session, "what was the book called", k=6)
    called_blob = " ".join(
        str(item.get("text") or "") for item in called.get("evidence") or []
    ).lower()
    assert called.get("grounding") == "evidence"
    assert "pragmatic" in called_blob or "book" in called_blob


@pytest.mark.asyncio
async def test_memorize_after_glance_pins_keep_onto_latest_look(
    db_session: AsyncSession,
) -> None:
    from app.memory.visual import attach_keep_to_latest_look

    glance = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["book"],
            "colors": ["red"],
            "person_count": 0,
            "media_kind": "frame",
            "ocr_text": "The Pragmatic Programmer",
            "spoken": "You're holding a red book titled The Pragmatic Programmer.",
            "request_id": "look-book-glance",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert glance is not None
    pinned = await attach_keep_to_latest_look(
        db_session, "memorize this book", actor="owner", device_id="mac-1"
    )
    await db_session.commit()
    assert pinned is not None
    pack = await build_explicit_recall_payload(db_session, "did you remember the book", k=6)
    blob = " ".join(str(item.get("text") or "") for item in pack.get("evidence") or []).lower()
    assert pack.get("grounding") == "evidence"
    assert "book" in blob
    assert "remember" in blob or "pragmatic" in blob


def test_compact_memory_json_keeps_spoken_hits() -> None:
    import json

    from app.voice.live.layer import compact_live_tool_json

    blob = compact_live_tool_json(
        {
            "ok": True,
            "name": "search_memory",
            "spoken": "You asked me to remember the red book.",
            "evidence": [{"text": "x" * 5000}],
            "result": {
                "count": 1,
                "grounding": "evidence",
                "spoken": "You asked me to remember the red book.",
                "results": [{"text": "Owner asked Evie to remember the book. Printed text: Harbor."}],
            },
        }
    )
    parsed = json.loads(blob)
    assert parsed["spoken"]
    assert "book" in parsed["spoken"].lower()
    assert parsed["result"]["grounding"] == "evidence"
    assert parsed["result"]["hits"]
    assert "evidence" not in parsed


def test_compact_memory_json_does_not_hand_mini_a_container_header() -> None:
    import json

    from app.voice.live.layer import compact_live_tool_json

    blob = compact_live_tool_json(
        {
            "ok": True,
            "name": "search_memory",
            "spoken": (
                "You asked me to remember a container. "
                "A stainless steel thermos with a black lid."
            ),
            "result": {
                "count": 1,
                "grounding": "evidence",
                "spoken": (
                    "You asked me to remember a container. "
                    "A stainless steel thermos with a black lid."
                ),
                "lines": [
                    "You asked me to remember a container. "
                    "A stainless steel thermos with a black lid and a dent near the base."
                ],
                "results": [
                    {
                        "text": "You asked me to remember a container.",
                        "description": (
                            "A stainless steel thermos with a black lid "
                            "and a dent near the base."
                        ),
                        "kind": "visual_keep",
                    }
                ],
            },
        }
    )
    parsed = json.loads(blob)
    spoken = str(parsed.get("spoken") or "").lower()
    hits = " ".join(parsed["result"]["hits"]).lower()
    assert "thermos" in spoken
    assert "thermos" in hits
    assert "you asked me to remember" not in spoken
    assert "you asked me to remember" not in hits
    assert "container-shaped" not in spoken
    assert "container-shaped" not in hits


def test_live_memory_speech_is_not_a_pause_ack() -> None:
    import inspect

    from app.voice.live.grok_voice import GrokVoiceBridge

    ack = inspect.getsource(GrokVoiceBridge.speak_ack)
    record = inspect.getsource(GrokVoiceBridge.speak_life_record)
    assert "One short sentence" in ack
    assert "One short sentence" not in record
    assert "direct record" in record
    # Mini treats "(life record — do not deny)" as a missing-row question.
    # File receipts already speak verbatim from this confirmation envelope.
    assert "speak this to the owner now" in record
    assert "do not have that in" in record
    assert "answer the owner from this now" not in record


def test_look_tool_kicks_keep_reread_after_commit() -> None:
    import inspect

    from app.voice.live.transport import _grok_tool_runner

    source = inspect.getsource(_grok_tool_runner)
    assert "await db.commit()" in source
    assert "kick_keep_identity_reread_from_look" in source
    assert source.index("await db.commit()") < source.index(
        "kick_keep_identity_reread_from_look"
    )


@pytest.mark.asyncio
async def test_live_partial_preempts_people_chats_hedge() -> None:
    from app.voice.live.events import PartialTranscriptEvent
    from app.voice.live.session import LiveSession

    cancelled = {"n": 0}

    class _Grok:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-partial-1"
        _shadow_response_for_turn = None

        async def cancel(self) -> None:
            cancelled["n"] += 1

    live = LiveSession(session_id="preempt-chats", backchannel_enabled=False)
    live.grok_voice = _Grok()
    live.grok_voice._response_active = True

    async def runner(name: str, args: dict, call_id: str) -> str:
        return "{}"

    live.run_live_tool = runner
    try:
        await live.emit(
            PartialTranscriptEvent(
                at_ms=1,
                text="tell me about my conversations with different people",
                sequence=1,
            )
        )
        assert cancelled["n"] == 1
        assert live.grok_voice._shadow_response_for_turn == "turn-partial-1"
        cancelled["n"] = 0
        await live.emit(
            PartialTranscriptEvent(at_ms=2, text="what's the weather", sequence=2)
        )
        assert cancelled["n"] == 0
    finally:
        live.close()


@pytest.mark.asyncio
async def test_live_s2s_transcript_runs_memory_instead_of_hedge() -> None:
    import json

    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []
    cancelled = {"n": 0}

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "spoken": "You asked me to remember the red book.",
                "result": {
                    "grounding": "evidence",
                    "spoken": "You asked me to remember the red book.",
                    "count": 1,
                },
            }
        )

    class _Grok:
        _provider = "xai"
        supports_function_calls = True
        _open_turn_id = "turn-memory-1"
        _shadow_response_for_turn = None

        async def cancel(self) -> None:
            cancelled["n"] += 1

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-memory-talk", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=1,
                text="did you remember the book",
                provider="grok-voice",
            ),
        )
        assert cancelled["n"] == 1
        assert seen == [
            (
                "search_memory",
                {"query": "did you remember the book"},
                "owner-memory",
            )
        ]
        assert spoken
        assert "book" in spoken[0].lower()
    finally:
        live.close()


@pytest.mark.asyncio
async def test_live_s2s_people_chats_run_recall_from_transcript() -> None:
    import json

    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []
    cancelled = {"n": 0}

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "spoken": "You talk with Mummy on WhatsApp.",
                "result": {
                    "ok": True,
                    "count": 2,
                    "lines": ["Person: Mummy.", "WhatsApp thread with Mummy."],
                    "grounding": "evidence",
                    "life_shelf": "chats",
                    "spoken": "You talk with Mummy on WhatsApp.",
                    "hits": ["Person: Mummy.", "WhatsApp thread with Mummy."],
                },
            }
        )

    class _Grok:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-chats-1"
        _shadow_response_for_turn = None

        async def cancel(self) -> None:
            cancelled["n"] += 1

        async def speak_ack(self, text: str) -> bool:
            spoken.append("ack:" + text)
            return True

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-chats-talk", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=1,
                text="tell me about my conversations with different people",
                provider="openai-realtime",
            ),
        )
        assert cancelled["n"] == 1
        assert seen == [
            (
                "recall",
                {"query": "tell me about my conversations with different people"},
                "owner-memory",
            )
        ]
        assert spoken
        assert not spoken[0].startswith("ack:")
        assert "mummy" in spoken[0].lower() or "whatsapp" in spoken[0].lower()
        weather = LiveSession(session_id="owner-chats-weather", backchannel_enabled=False)
        weather.run_live_tool = runner
        weather.grok_voice = _Grok()
        seen.clear()
        await _await_s2s(
            weather,
            FinalTranscriptEvent(
                at_ms=2,
                text="what's the weather?",
                provider="openai-realtime",
            ),
        )
        # A weather ask is work-shaped and Muse Spark may broker it via
        # get_weather (owner-spark). What must never happen is the chats
        # recall broker firing again for a non-chat transcript.
        assert not [item for item in seen if item[0] in {"recall", "search_memory"}]
        weather.close()
    finally:
        live.close()


@pytest.mark.asyncio
async def test_injected_life_record_does_not_recall_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from app.ev.laptop_files import is_system_confirmation
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    assert is_system_confirmation(
        "(life record — answer the owner from this now; do not deny) You talk on WhatsApp with Ada."
    )
    stored: list[str] = []

    def _capture_turn(**kwargs):
        stored.append(str(kwargs.get("text") or ""))

    monkeypatch.setattr("app.memory.turns.schedule_live_turn", _capture_turn)
    seen: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append(name)
        return json.dumps({"ok": True, "spoken": "nope"})

    class _Grok:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-echo-1"
        _shadow_response_for_turn = None

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            return True

        async def speak_life_record(self, text: str) -> bool:
            return True

    live = LiveSession(session_id="owner-chats-echo", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await live.emit(
            FinalTranscriptEvent(
                at_ms=1,
                text=(
                    "(life record — answer the owner from this now; do not deny) "
                    "You talk on WhatsApp with Ada."
                ),
                provider="openai-realtime",
            )
        )
        assert seen == []
        assert stored == []
    finally:
        live.close()


@pytest.mark.asyncio
async def test_live_s2s_memorize_runs_look_from_transcript() -> None:
    import json

    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "spoken": (
                    "A paperback with a torn yellow cover titled Atomic Habits. "
                    "I'll remember that."
                ),
                "result": {
                    "kept": True,
                    "spoken": (
                        "A paperback with a torn yellow cover titled Atomic Habits. "
                        "I'll remember that."
                    ),
                },
            }
        )

    class _Grok:
        _provider = "xai"
        supports_function_calls = True
        _open_turn_id = "turn-keep-1"
        _shadow_response_for_turn = None

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append("ack:" + text)
            return True

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-keep-talk", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=1,
                text="memorize this book",
                provider="grok-voice",
            ),
        )
        assert seen
        assert seen[0][0] == "look"
        assert seen[0][2] == "owner-keep"
        assert spoken
        assert not spoken[0].startswith("ack:")
        assert "remember" in spoken[0].lower() or "book" in spoken[0].lower()
    finally:
        live.close()


@pytest.mark.asyncio
async def test_first_try_hold_look_runs_camera_and_stops_mini_refusal() -> None:
    import json

    from app.ev.look import LIVE_CAPTURED_SPOKEN
    from app.voice.live.events import FinalTranscriptEvent, PartialTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []
    cancelled = {"n": 0}
    holding = (
        "Look at the thing I'm holding in my hand. I want you to look at it "
        "and tell me more info about this item I'm holding"
    )

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "spoken": LIVE_CAPTURED_SPOKEN,
                "result": {
                    "spoken": LIVE_CAPTURED_SPOKEN,
                    "image_ready": True,
                },
            }
        )

    class _Grok:
        _provider = "xai"
        supports_function_calls = True
        _open_turn_id = "turn-hold-look"
        _shadow_response_for_turn = None
        _response_active = True
        _assistant_open = True
        _last_input_transcript = holding
        _pending_tools = 0
        _response_tool_choice_supported = True
        created: list[dict] = []

        async def cancel(self) -> None:
            cancelled["n"] += 1
            self._response_active = False
            self._assistant_open = False

        async def _send(self, payload, timeout_s: float = 2.0) -> bool:
            self.created.append(payload)
            return True

        async def send_text(self, text: str) -> None:
            spoken.append("ask:" + text)

        async def _deliver_camera_images(self, name: str, call_id: str, output: str) -> dict:
            spoken.append("inject:" + call_id)
            return {"image_delivered": True}

        async def speak_ack(self, text: str) -> bool:
            spoken.append("ack:" + text)
            return True

        async def speak_honesty(self, text: str) -> bool:
            spoken.append("honesty:" + text)
            return True

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-hold-look", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await live.emit(
            PartialTranscriptEvent(at_ms=1, text=holding, sequence=1)
        )
        assert cancelled["n"] >= 1
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=2,
                text=holding,
                provider="grok-voice",
            ),
        )
        assert seen
        assert seen[0][0] == "look"
        assert seen[0][2] == "owner-look"
        assert "inject:owner-look" in spoken
        assert live._awaiting_keep_identity is False
        creates = live.grok_voice.created
        assert any(item.get("type") == "response.create" for item in creates)
    finally:
        live.close()


@pytest.mark.asyncio
async def test_live_memorize_leaves_mini_to_name_the_jpeg() -> None:
    import json

    from app.ev.look import KEEP_CAPTURED_SPOKEN
    from app.voice.live.events import FinalTranscriptEvent, PartialTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []
    cancelled = {"n": 0}

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "spoken": KEEP_CAPTURED_SPOKEN,
                "result": {"kept": True, "spoken": KEEP_CAPTURED_SPOKEN, "image_ready": True},
            }
        )

    class _Grok:
        _provider = "xai"
        supports_function_calls = True
        _open_turn_id = "turn-keep-jpeg"
        _shadow_response_for_turn = None
        _response_active = True
        _assistant_open = True
        _last_input_transcript = "memorize this"
        _pending_tools = 0
        _response_tool_choice_supported = True
        created: list[dict] = []

        async def cancel(self) -> None:
            cancelled["n"] += 1

        async def _send(self, payload, timeout_s: float = 2.0) -> bool:
            self.created.append(payload)
            return True

        async def send_text(self, text: str) -> None:
            spoken.append("ask:" + text)

        async def _deliver_camera_images(self, name: str, call_id: str, output: str) -> str:
            spoken.append("inject:" + call_id)
            return output

        async def speak_ack(self, text: str) -> bool:
            spoken.append("ack:" + text)
            return True

        async def speak_honesty(self, text: str) -> bool:
            spoken.append("honesty:" + text)
            return True

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-keep-jpeg", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await live.emit(
            PartialTranscriptEvent(
                at_ms=1,
                text="memorize this",
                sequence=1,
            )
        )
        assert cancelled["n"] == 0
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=2,
                text="memorize this",
                provider="grok-voice",
            ),
        )
        assert seen
        assert seen[0][0] == "look"
        assert seen[0][2] == "owner-keep"
        assert "inject:owner-keep" in spoken
        assert not any(item.startswith("ask:") for item in spoken)
        creates = live.grok_voice.created
        assert any(item.get("type") == "response.create" for item in creates)
        blob = str(creates).lower()
        assert "concrete noun" in blob
        assert "do not read these instructions" in blob
        assert live.grok_voice._last_input_transcript == "memorize this"
        assert live.grok_voice._continuation_sent is True
        assert not any(item == KEEP_CAPTURED_SPOKEN for item in spoken)
        assert not any(item.startswith("ack:") for item in spoken)
    finally:
        live.close()


@pytest.mark.asyncio
async def test_delayed_memorize_transcript_does_not_recapture_keep() -> None:
    import json

    from app.ev.camera_runtime import (
        CameraObservation,
        now_mono,
        reset_pending_observations,
        stash_observation,
    )
    from app.ev.look import KEEP_HOLD_CALL_ID
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append(call_id)
        return json.dumps({"ok": True, "name": name})

    class _Grok:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-keep-skip"
        _pending_tools = 0
        _last_input_transcript = "memorize this"

        async def cancel(self) -> None:
            return None

    reset_pending_observations()
    stash_observation(
        CameraObservation(
            request_id="11111111-1111-1111-1111-111111111111",
            call_id=KEEP_HOLD_CALL_ID,
            jpeg=b"\xff\xd8" + b"\x00" * 40 + b"\xff\xd9",
            width=1280,
            height=720,
            detail="high",
            t0=now_mono(),
        )
    )
    live = LiveSession(session_id="keep-skip-recapture", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    live.note_keep_look(
        arguments={"prompt": "memorize this"},
        body={
            "kept": True,
            "keep_request": "memorize this",
            "image_ready": True,
            "attachment_id": "11111111-1111-1111-1111-111111111111",
        },
        transcript="memorize this",
    )
    try:
        await live.emit(
            FinalTranscriptEvent(
                at_ms=3,
                text="memorize this",
                provider="openai-realtime",
            )
        )
        assert seen == []
        assert live._awaiting_keep_identity is True
    finally:
        live.close()
        reset_pending_observations()


@pytest.mark.asyncio
async def test_keep_describe_create_after_skip_create_goes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.voice.live.session import LiveSession

    monkeypatch.setattr("app.voice.live.session._KEEP_DESCRIBE_IDLE_POLL_S", 0.05)
    monkeypatch.setattr("app.voice.live.session._KEEP_DESCRIBE_IDLE_GRACE_S", 0.05)

    class _Grok:
        supports_function_calls = True
        _provider = "openai"
        _pending_tools = 0
        _tool_boundary_pending = False
        _continuation_sent = True
        _response_active = True
        _assistant_open = True
        _response_tool_choice_supported = True
        created: list[dict] = []

        async def cancel(self) -> None:
            return None

        async def _send(self, payload, timeout_s: float = 2.0) -> bool:
            self.created.append(payload)
            return True

    live = LiveSession(session_id="keep-describe-idle", backchannel_enabled=False)
    live.grok_voice = _Grok()
    live.note_keep_look(
        arguments={"prompt": "memorize this"},
        body={
            "kept": True,
            "keep_request": "memorize this",
            "image_ready": True,
            "attachment_id": "11111111-1111-1111-1111-111111111111",
        },
        transcript="memorize this",
    )
    try:
        await asyncio.sleep(0.15)
        assert live.grok_voice.created == []
        live.grok_voice._continuation_sent = False
        live.grok_voice._response_active = False
        live.grok_voice._assistant_open = False
        for _ in range(40):
            if any(item.get("type") == "response.create" for item in live.grok_voice.created):
                break
            await asyncio.sleep(0.05)
        creates = [item for item in live.grok_voice.created if item.get("type") == "response.create"]
        assert creates
        blob = str(creates).lower()
        assert "concrete noun" in blob
        assert live._keep_idle_describe_sent is True
    finally:
        live.close()


@pytest.mark.asyncio
async def test_keep_jpeg_inject_falls_back_to_hold_copy() -> None:
    import json

    from app.ev.camera_runtime import (
        CameraObservation,
        peek_observations,
        reset_pending_observations,
        stash_observation,
    )
    from app.ev.look import KEEP_CAPTURED_SPOKEN, KEEP_HOLD_CALL_ID
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import _KEEP_INJECT_CALL_ID, LiveSession

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 40 + b"\xff\xd9"
    stash_observation(
        CameraObservation(
            request_id="owner-keep",
            call_id=KEEP_HOLD_CALL_ID,
            jpeg=jpeg,
            width=1280,
            height=720,
            detail="high",
        )
    )
    seen: list[str] = []
    injects: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append(call_id)
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "spoken": KEEP_CAPTURED_SPOKEN,
                "result": {"kept": True, "spoken": KEEP_CAPTURED_SPOKEN},
            }
        )

    class _Grok:
        _provider = "xai"
        supports_function_calls = True
        _open_turn_id = "turn-keep-hold"
        _shadow_response_for_turn = None
        _response_active = True
        _assistant_open = True
        _last_input_transcript = "memorize this"
        _pending_tools = 0
        _response_tool_choice_supported = True
        created: list[dict] = []

        async def cancel(self) -> None:
            return None

        async def _send(self, payload, timeout_s: float = 2.0) -> bool:
            self.created.append(payload)
            return True

        async def _deliver_camera_images(self, name: str, call_id: str, output: str) -> str:
            injects.append(call_id)
            if call_id == "owner-keep":
                return json.dumps({"ok": True, "image_delivered": False, "frames": 0})
            return json.dumps({"ok": True, "image_delivered": True, "frames": 1})

        async def speak_life_record(self, text: str) -> bool:
            return True

    live = LiveSession(session_id="owner-keep-hold", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=2,
                text="memorize this",
                provider="grok-voice",
            ),
        )
        assert seen == ["owner-keep"]
        assert injects == ["owner-keep", _KEEP_INJECT_CALL_ID]
        assert peek_observations(KEEP_HOLD_CALL_ID)
        assert any(item.get("type") == "response.create" for item in live.grok_voice.created)
    finally:
        live.close()
        reset_pending_observations()


@pytest.mark.asyncio
async def test_keep_jpeg_offered_when_compact_spoken_is_stripped() -> None:
    import json

    from app.ev.camera_runtime import (
        CameraObservation,
        reset_pending_observations,
        stash_observation,
    )
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 40 + b"\xff\xd9"
    stash_observation(
        CameraObservation(
            request_id="owner-keep",
            call_id="owner-keep",
            jpeg=jpeg,
            width=1280,
            height=720,
            detail="high",
        )
    )
    injects: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "result": {
                    "kept": True,
                    "attachment_id": "11111111-1111-1111-1111-111111111111",
                    "image_ready": True,
                    "encoded_bytes": 140000,
                },
            }
        )

    class _Grok:
        _provider = "xai"
        supports_function_calls = True
        _open_turn_id = "turn-keep-stripped"
        _shadow_response_for_turn = None
        _response_active = True
        _assistant_open = True
        _last_input_transcript = "memorize this"
        _pending_tools = 0
        _response_tool_choice_supported = True
        created: list[dict] = []

        async def cancel(self) -> None:
            return None

        async def _send(self, payload, timeout_s: float = 2.0) -> bool:
            self.created.append(payload)
            return True

        async def _deliver_camera_images(self, name: str, call_id: str, output: str) -> str:
            injects.append(call_id)
            return json.dumps({"ok": True, "image_delivered": True, "frames": 1})

        async def speak_life_record(self, text: str) -> bool:
            raise AssertionError("stripped keep look must not speak the prompt")

    live = LiveSession(session_id="owner-keep-stripped", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=2,
                text="memorize this",
                provider="grok-voice",
            ),
        )
        assert injects == ["owner-keep"]
        assert any(item.get("type") == "response.create" for item in live.grok_voice.created)
    finally:
        live.close()
        reset_pending_observations()


@pytest.mark.asyncio
async def test_keep_look_timeout_speech_still_offers_jpeg() -> None:
    import json

    from app.ev.camera_runtime import reset_pending_observations
    from app.ev.look import TIMEOUT_SPOKEN
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    injects: list[str] = []
    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "spoken": TIMEOUT_SPOKEN,
                "result": {
                    "kept": True,
                    "spoken": TIMEOUT_SPOKEN,
                    "attachment_id": "11111111-1111-1111-1111-111111111111",
                    "image_ready": True,
                    "encoded_bytes": 140000,
                },
            }
        )

    class _Grok:
        _provider = "xai"
        supports_function_calls = True
        _open_turn_id = "turn-keep-timeout-jpeg"
        _shadow_response_for_turn = None
        _response_active = True
        _assistant_open = True
        _last_input_transcript = "memorize this"
        _pending_tools = 0
        _response_tool_choice_supported = True
        created: list[dict] = []

        async def cancel(self) -> None:
            return None

        async def _send(self, payload, timeout_s: float = 2.0) -> bool:
            self.created.append(payload)
            return True

        async def _deliver_camera_images(self, name: str, call_id: str, output: str) -> str:
            injects.append(call_id)
            return json.dumps({"ok": True, "image_delivered": True, "frames": 1})

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-keep-timeout-jpeg", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=2,
                text="memorize this",
                provider="grok-voice",
            ),
        )
        assert injects == ["owner-keep"]
        assert spoken == []
    finally:
        live.close()
        reset_pending_observations()


def test_compact_look_json_keeps_attachment_id() -> None:
    import json

    from app.voice.live.layer import compact_live_tool_json

    parsed = json.loads(
        compact_live_tool_json(
            {
                "ok": True,
                "name": "look",
                "spoken": "A current camera image is attached.",
                "result": {
                    "ok": True,
                    "spoken": "A current camera image is attached.",
                    "attachment_id": "11111111-1111-1111-1111-111111111111",
                    "encoded_bytes": 140000,
                    "width": 1280,
                    "height": 720,
                    "image_ready": True,
                    "kept": True,
                    "labels": ["container", "indoor"],
                    "visual_facts": "container, indoor",
                    "follow_up": "This look is stored as durable memory.",
                },
            }
        )
    )
    assert parsed["result"]["attachment_id"] == "11111111-1111-1111-1111-111111111111"
    assert parsed["result"]["encoded_bytes"] == 140000
    assert parsed["result"]["kept"] is True
    assert "spoken" not in parsed
    assert "spoken" not in parsed["result"]
    assert "labels" not in parsed["result"]
    assert "visual_facts" not in parsed["result"]
    assert "follow_up" not in parsed["result"]


@pytest.mark.asyncio
async def test_keep_jpeg_inject_reloads_stored_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from app.ev.camera_runtime import reset_pending_observations
    from app.ev.look import KEEP_CAPTURED_SPOKEN
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import _KEEP_INJECT_CALL_ID, LiveSession

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 40 + b"\xff\xd9"
    attachment_id = "11111111-1111-1111-1111-111111111111"
    loaded: list[str] = []

    async def fake_jpeg(_session, needle: str):
        loaded.append(needle)
        return jpeg

    monkeypatch.setattr("app.ev.look.jpeg_bytes_for_keep_attachment", fake_jpeg)

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("app.db.SessionLocal", lambda: _Sess())
    injects: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "spoken": KEEP_CAPTURED_SPOKEN,
                "result": {
                    "kept": True,
                    "spoken": KEEP_CAPTURED_SPOKEN,
                    "attachment_id": attachment_id,
                    "encoded_bytes": len(jpeg),
                    "image_ready": True,
                },
            }
        )

    class _Grok:
        _provider = "xai"
        supports_function_calls = True
        _open_turn_id = "turn-keep-att"
        _shadow_response_for_turn = None
        _response_active = True
        _assistant_open = True
        _last_input_transcript = "memorize this"
        _pending_tools = 0
        _response_tool_choice_supported = True
        created: list[dict] = []

        async def cancel(self) -> None:
            return None

        async def _send(self, payload, timeout_s: float = 2.0) -> bool:
            self.created.append(payload)
            return True

        async def _deliver_camera_images(self, name: str, call_id: str, output: str) -> str:
            injects.append(call_id)
            if call_id == "owner-keep":
                return json.dumps({"ok": True, "image_delivered": False, "frames": 0})
            return json.dumps({"ok": True, "image_delivered": True, "frames": 1})

        async def speak_life_record(self, text: str) -> bool:
            return True

    live = LiveSession(session_id="owner-keep-att", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=2,
                text="memorize this",
                provider="grok-voice",
            ),
        )
        assert loaded == [attachment_id]
        assert injects == ["owner-keep", _KEEP_INJECT_CALL_ID]
        assert any(item.get("type") == "response.create" for item in live.grok_voice.created)
    finally:
        live.close()
        reset_pending_observations()


def test_mini_called_look_still_awaits_keep_identity() -> None:
    from app.voice.live.session import LiveSession

    live = LiveSession(session_id="mini-keep-flag", backchannel_enabled=False)
    try:
        live.note_keep_look(
            arguments={"prompt": "what is this"},
            body={"ok": True, "spoken": "A current camera image is attached."},
            transcript="hello",
        )
        assert live._awaiting_keep_identity is False
        live.note_keep_look(
            arguments={"prompt": "memorize this"},
            body={"ok": True, "kept": True, "keep_request": "memorize this"},
            transcript="memorize this",
        )
        assert live._awaiting_keep_identity is False
        live.note_keep_look(
            arguments={"prompt": "memorize this"},
            body={
                "ok": True,
                "kept": True,
                "keep_request": "memorize this",
                "image_ready": True,
                "attachment_id": "11111111-1111-1111-1111-111111111111",
            },
            transcript="memorize this",
        )
        assert live._awaiting_keep_identity is True
    finally:
        live.close()


@pytest.mark.asyncio
async def test_mini_first_look_speech_persists_without_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.voice.live.events import ReplyEvent
    from app.voice.live.session import LiveSession

    written: dict[str, str] = {}

    async def fake_remember(session, text, **kwargs):
        written["text"] = text
        return {"kept": True}

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("app.memory.visual.remember_spoken_scene", fake_remember)
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Sess())

    class _Grok:
        _model = "gpt-realtime"
        _last_input_transcript = "memorize this"

    live = LiveSession(session_id="mini-keep-persist", backchannel_enabled=False)
    live.grok_voice = _Grok()
    live.note_keep_look(
        arguments={"prompt": "memorize this"},
        body={
            "kept": True,
            "keep_request": "memorize this",
            "image_ready": True,
            "attachment_id": "11111111-1111-1111-1111-111111111111",
        },
        transcript="memorize this",
    )
    try:
        await live.emit(
            ReplyEvent(
                at_ms=3,
                text=(
                    "A matte black handset with a silver rim, a dent on the left "
                    "edge, and tiny white lettering near the base."
                ),
                model="gpt-realtime",
            )
        )
        for _ in range(20):
            if written:
                break
            await asyncio.sleep(0.05)
        assert "handset" in written.get("text", "").lower()
        assert "dent" in written.get("text", "").lower()
    finally:
        live.close()


@pytest.mark.asyncio
async def test_assistant_partial_persists_keep_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.voice.live.events import PartialTranscriptEvent
    from app.voice.live.session import LiveSession

    written: dict[str, str] = {}

    async def fake_remember(session, text, **kwargs):
        written["text"] = text
        return {"kept": True}

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr("app.memory.visual.remember_spoken_scene", fake_remember)
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Sess())

    class _Grok:
        _model = "gpt-realtime"
        _last_input_transcript = "memorize this"

    live = LiveSession(session_id="mini-keep-partial", backchannel_enabled=False)
    live.grok_voice = _Grok()
    live.note_keep_look(
        arguments={"prompt": "memorize this"},
        body={
            "kept": True,
            "keep_request": "memorize this",
            "image_ready": True,
            "attachment_id": "11111111-1111-1111-1111-111111111111",
        },
        transcript="memorize this",
    )
    try:
        await live.emit(
            PartialTranscriptEvent(
                at_ms=3,
                text=(
                    "A matte black handset with a silver rim, a dent on the left "
                    "edge, and tiny white lettering near the base."
                ),
                sequence=1,
                role="assistant",
            )
        )
        for _ in range(20):
            if written:
                break
            await asyncio.sleep(0.05)
        assert "handset" in written.get("text", "").lower()
        assert "dent" in written.get("text", "").lower()
    finally:
        live.close()


@pytest.mark.asyncio
async def test_keep_awaiting_stays_until_identity_row_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.voice.live.events import ReplyEvent
    from app.voice.live.session import LiveSession

    calls: list[str] = []

    async def fake_remember(session, text, **kwargs):
        calls.append(text)
        if len(calls) == 1:
            return None
        return {"kept": True}

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def commit(self):
            return None

        async def rollback(self):
            return None

    monkeypatch.setattr("app.memory.visual.remember_spoken_scene", fake_remember)
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Sess())

    class _Grok:
        _model = "gpt-realtime"
        _last_input_transcript = "memorize this"

    identity = (
        "A matte black handset with a silver rim, a dent on the left "
        "edge, and tiny white lettering near the base."
    )
    live = LiveSession(session_id="mini-keep-retry", backchannel_enabled=False)
    live.grok_voice = _Grok()
    live.note_keep_look(
        arguments={"prompt": "memorize this"},
        body={
            "kept": True,
            "keep_request": "memorize this",
            "image_ready": True,
            "attachment_id": "11111111-1111-1111-1111-111111111111",
        },
        transcript="memorize this",
    )
    try:
        await live.emit(ReplyEvent(at_ms=3, text=identity, model="gpt-realtime"))
        for _ in range(20):
            if calls:
                break
            await asyncio.sleep(0.05)
        assert live._awaiting_keep_identity is True
        richer = identity.replace("lettering", "SERIAL 4K2 lettering")
        await live.emit(ReplyEvent(at_ms=4, text=richer, model="gpt-realtime"))
        for _ in range(20):
            if len(calls) >= 2 and live._awaiting_keep_identity is False:
                break
            await asyncio.sleep(0.05)
        assert len(calls) >= 2
        assert live._awaiting_keep_identity is False
    finally:
        live.close()


@pytest.mark.asyncio
async def test_thin_class_stub_is_not_persisted_as_keep_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.voice.live.events import ReplyEvent
    from app.voice.live.session import LiveSession

    written: dict[str, str] = {}

    async def fake_remember(session, text, **kwargs):
        written["text"] = text
        return {"kept": True}

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("app.memory.visual.remember_spoken_scene", fake_remember)
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Sess())

    class _Grok:
        _model = "gpt-realtime"
        _last_input_transcript = "memorize this"

    live = LiveSession(session_id="mini-keep-thin", backchannel_enabled=False)
    live.grok_voice = _Grok()
    live.note_keep_look(
        arguments={"prompt": "memorize this"},
        body={
            "kept": True,
            "keep_request": "memorize this",
            "image_ready": True,
            "attachment_id": "11111111-1111-1111-1111-111111111111",
        },
        transcript="memorize this",
    )
    try:
        await live.emit(
            ReplyEvent(
                at_ms=3,
                text="That's a phone.",
                model="gpt-realtime",
            )
        )
        await asyncio.sleep(0.2)
        assert written == {}
        assert live._awaiting_keep_identity is True
        await live.emit(
            ReplyEvent(
                at_ms=4,
                text=(
                    "A matte black handset with a silver rim, a dent on the left "
                    "edge, and tiny white lettering near the base."
                ),
                model="gpt-realtime",
            )
        )
        for _ in range(20):
            if written:
                break
            await asyncio.sleep(0.05)
        assert "handset" in written.get("text", "").lower()
    finally:
        live.close()


@pytest.mark.asyncio
async def test_archive_speech_does_not_persist_as_keep_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.voice.live.events import ReplyEvent
    from app.voice.live.session import LiveSession

    written: dict[str, str] = {}

    async def fake_remember(session, text, **kwargs):
        written["text"] = text
        return {"kept": True}

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr("app.memory.visual.remember_spoken_scene", fake_remember)
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Sess())

    class _Grok:
        _model = "gpt-realtime"
        _last_input_transcript = "I am holding something. Memorize this."

    mummy = (
        'I can see "Mummy" as a contact, but I don\'t have the phone number listed.'
    )
    live = LiveSession(session_id="mini-keep-archive", backchannel_enabled=False)
    live.grok_voice = _Grok()
    try:
        await live.emit(ReplyEvent(at_ms=3, text=mummy, model="gpt-realtime"))
        await asyncio.sleep(0.2)
        assert written == {}
        live.note_keep_look(
            arguments={"prompt": "memorize this"},
            body={
            "kept": True,
            "keep_request": "memorize this",
            "image_ready": True,
            "attachment_id": "11111111-1111-1111-1111-111111111111",
        },
            transcript="memorize this",
        )
        await live.emit(ReplyEvent(at_ms=4, text=mummy, model="gpt-realtime"))
        await asyncio.sleep(0.2)
        assert written == {}
        await live.emit(
            ReplyEvent(
                at_ms=5,
                text=(
                    "A matte black handset with a silver rim, a dent on the left "
                    "edge, and tiny white lettering near the base."
                ),
                model="gpt-realtime",
            )
        )
        for _ in range(20):
            if written:
                break
            await asyncio.sleep(0.05)
        assert "handset" in written.get("text", "").lower()
        assert "mummy" not in written.get("text", "").lower()
    finally:
        live.close()


@pytest.mark.asyncio
async def test_keep_look_captures_while_mini_recall_in_flight() -> None:
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, str]] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, call_id))
        return json.dumps({"ok": True, "kept": True, "image_ready": True})

    class _Grok:
        supports_function_calls = True
        _provider = "openai"
        _pending_tools = 1
        _tool_boundary_pending = False
        _continuation_sent = True
        _response_active = True
        _assistant_open = True

        async def cancel(self) -> None:
            return None

        async def _deliver_camera_images(self, *args, **kwargs):
            return {"image_delivered": True}

        async def _send(self, payload, timeout_s: float = 2.0) -> bool:
            return True

    live = LiveSession(session_id="keep-look-during-tool", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=3,
                text="I am holding something. Memorize this.",
                provider="openai-realtime",
            ),
        )
        assert seen == [("look", "owner-keep")]
        assert live._keep_idle_look_sent is False
    finally:
        live.close()


@pytest.mark.asyncio
async def test_live_partial_memory_cancels_hedge_before_final() -> None:
    import json

    from app.voice.live.events import FinalTranscriptEvent, PartialTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []
    cancelled = {"n": 0}

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "spoken": "You asked me to remember the red book.",
                "result": {"spoken": "You asked me to remember the red book.", "count": 1},
            }
        )

    class _Grok:
        _provider = "xai"
        supports_function_calls = True
        _open_turn_id = "turn-partial-1"
        _shadow_response_for_turn = None
        _response_active = True
        _assistant_open = True

        async def cancel(self) -> None:
            cancelled["n"] += 1
            self._response_active = False
            self._assistant_open = False

        async def speak_ack(self, text: str) -> bool:
            return True

    live = LiveSession(session_id="owner-memory-partial", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        from app.ev.camera_runtime import reset_pending_observations

        reset_pending_observations()
        await live.emit(
            PartialTranscriptEvent(
                at_ms=1,
                text="did you remember the book",
                sequence=1,
            )
        )
        assert cancelled["n"] == 1
        assert seen == []
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=2,
                text="did you remember the book",
                provider="grok-voice",
            ),
        )
        assert seen == [
            (
                "search_memory",
                {"query": "did you remember the book"},
                "owner-memory",
            )
        ]
        live.grok_voice._response_active = True
        cancelled["n"] = 0
        await live.emit(
            PartialTranscriptEvent(at_ms=3, text="what's the weather", sequence=2)
        )
        assert cancelled["n"] == 0
        assert len(seen) == 1
    finally:
        live.close()


@pytest.mark.asyncio
async def test_failed_look_still_persists_memorize_for_restart(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev.look import look_with_timeout

    async def no_frame(*_args, **_kwargs):
        return {"ok": False, "spoken": "I could not see that.", "error": "no_frame"}

    monkeypatch.setattr("app.ev.look.look_now", no_frame)
    result = await look_with_timeout(
        db_session, prompt="memorize this book", actor="owner", device_id="mac-1"
    )
    await db_session.commit()
    assert result.get("kept") is True
    pack = await build_explicit_recall_payload(db_session, "did you remember the book", k=6)
    blob = " ".join(str(item.get("text") or "") for item in pack.get("evidence") or []).lower()
    spoken = str(pack.get("spoken") or "").lower()
    assert pack.get("grounding") == "evidence"
    assert "book" in blob or "book" in spoken


@pytest.mark.asyncio
async def test_persist_keep_intent_without_camera_is_recallable(
    db_session: AsyncSession,
) -> None:
    written = await persist_keep_intent(
        db_session,
        "memorize this book",
        actor="owner",
        device_id="mac-1",
        scene="You're holding a red book titled The Pragmatic Programmer.",
        labels=["book"],
    )
    await db_session.commit()
    assert written is not None
    pack = await build_explicit_recall_payload(db_session, "what was I holding", k=6)
    blob = " ".join(str(item.get("text") or "") for item in pack.get("evidence") or []).lower()
    assert pack.get("grounding") == "evidence"
    assert "book" in blob


@pytest.mark.asyncio
async def test_live_session_memorize_then_new_session_recalls_from_store(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spoken keep must survive a new LiveSession the way app quit/reopen does."""

    import json

    from app.ev.look import look_with_timeout
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    async def seen_keep(*_args, **_kwargs):
        written = await persist_keep_intent(
            db_session,
            "memorize this book",
            actor="owner",
            device_id="mac-1",
            scene="You're holding a red book titled The Pragmatic Programmer.",
            labels=["book"],
        )
        await db_session.commit()
        spoken_line = (
            "You're holding a red book titled The Pragmatic Programmer. "
            "I'll remember that."
        )
        return {
            "ok": True,
            "kept": bool(written and written.get("kept")),
            "spoken": spoken_line,
        }

    monkeypatch.setattr("app.ev.look.look_now", seen_keep)

    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        if name == "look":
            result = await look_with_timeout(
                db_session,
                prompt=str(args.get("prompt") or ""),
                actor="owner",
                device_id="mac-1",
            )
            await db_session.commit()
            return json.dumps(
                {
                    "ok": True,
                    "name": name,
                    "spoken": result.get("spoken"),
                    "result": result,
                }
            )
        if name == "search_memory":
            pack = await build_explicit_recall_payload(
                db_session, str(args.get("query") or ""), k=6
            )
            spoken_line = pack.get("spoken")
            return json.dumps(
                {
                    "ok": True,
                    "name": name,
                    "spoken": spoken_line,
                    "result": {
                        "count": pack.get("count"),
                        "grounding": pack.get("grounding"),
                        "spoken": spoken_line,
                        "hits": pack.get("lines") or [],
                    },
                }
            )
        raise AssertionError(name)

    class _Grok:
        _provider = "xai"
        supports_function_calls = True
        _open_turn_id = "turn-keep-restart"
        _shadow_response_for_turn = None

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    first = LiveSession(session_id="keep-then-restart-1", backchannel_enabled=False)
    first.run_live_tool = runner
    first.grok_voice = _Grok()
    try:
        await _await_s2s(
            first,
            FinalTranscriptEvent(
                at_ms=1,
                text="memorize this book",
                provider="grok-voice",
            ),
        )
        assert spoken
        assert (
            "remember" in spoken[-1].lower()
            or "book" in spoken[-1].lower()
            or "camera" in spoken[-1].lower()
        )
    finally:
        first.close()

    spoken.clear()
    later = LiveSession(session_id="keep-then-restart-2", backchannel_enabled=False)
    later.run_live_tool = runner
    later.grok_voice = _Grok()
    try:
        await _await_s2s(
            later,
            FinalTranscriptEvent(
                at_ms=2,
                text="did you remember the book",
                provider="grok-voice",
            ),
        )
        assert spoken
        blob = spoken[-1].lower()
        assert "book" in blob
        assert "cannot find that particular record" not in blob
        assert "do not know" not in blob
        assert "tell me" not in blob
    finally:
        later.close()


def test_memory_tool_keeps_broker_query_when_asr_is_stale() -> None:
    from app.voice.live.transport import bind_memory_tool_query

    kept = bind_memory_tool_query(
        "search_memory",
        {"query": "did you remember the book"},
        "I have given it to you. Come on, eat here. Hi, this is our uncle and aunt.",
    )
    assert kept["query"] == "did you remember the book"
    filled = bind_memory_tool_query("search_memory", {}, "did you remember the book")
    assert filled["query"] == "did you remember the book"
    recall = bind_memory_tool_query(
        "recall",
        {"query": "what did mummy tell me"},
        "Come on, eat here.",
    )
    assert recall["query"] == "what did mummy tell me"


@pytest.mark.asyncio
async def test_typed_owner_text_searches_the_utterance_not_stale_asr() -> None:
    import json

    from app.voice.live.session import LiveSession

    seen: list[dict] = []
    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append(dict(args))
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "spoken": "You asked me to remember the red book.",
                "result": {
                    "grounding": "evidence",
                    "spoken": "You asked me to remember the red book.",
                    "count": 1,
                },
            }
        )

    class _Grok:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-typed-1"
        _shadow_response_for_turn = None
        _last_input_transcript = (
            "I have given it to you. Come on, eat here. Hi, this is our uncle and aunt."
        )
        _last_input_transcript_at = 0.0

        async def cancel(self) -> None:
            return None

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    grok = _Grok()
    live = LiveSession(session_id="owner-typed-book", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = grok
    try:
        await live.handle_client({"type": "text", "text": "did you remember the book"})
        task = live._owner_text_task
        if task is not None:
            await task
        assert grok._last_input_transcript == "did you remember the book"
        assert seen
        assert seen[0].get("query") == "did you remember the book"
        assert spoken
        assert "book" in spoken[0].lower()
        assert "uncle" not in spoken[0].lower()
    finally:
        live.close()
    from app.voice.live.grok_voice import is_memory_ungrounded_hedge

    assert is_memory_ungrounded_hedge(
        "I cannot tell because I do not have a direct record from which I could tell"
    )
    assert is_memory_ungrounded_hedge("I don't know, if you tell me I could tell you")
    assert is_memory_ungrounded_hedge("I have no reliable record of that")
    assert not is_memory_ungrounded_hedge("You asked me to remember the red book.")
    assert not is_memory_ungrounded_hedge("I don't know the capital of France.")
    assert not is_memory_ungrounded_hedge("Preference: VS Code")
    assert is_memory_ungrounded_hedge(
        "I can help with remembering things, but in this setup I don't have a "
        "dedicated memory tool available."
    )
    assert is_memory_ungrounded_hedge("I do not have that in record.")
    assert is_memory_ungrounded_hedge("I don't have that in my record")
    assert is_memory_ungrounded_hedge("I cannot find that particular record.")


@pytest.mark.asyncio
async def test_life_record_hedge_is_cancelled_and_forced_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from app.voice.live.grok_voice import GrokVoiceBridge

    class _WS:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    ws = _WS()
    bridge = GrokVoiceBridge(on_event=lambda _e: None, api_key="k", provider="openai")
    bridge._ws = ws
    bridge._audio_accepting = True
    bridge._response_active = True
    bridge._honesty_speech = True
    bridge._pending_life_record = (
        "You're holding AI and Machine Learning for Coders. I'll remember that."
    )
    await bridge._handle_upstream(
        {
            "type": "response.output_audio_transcript.delta",
            "delta": (
                "I cannot tell because I do not have a direct record "
                "from which I could tell"
            ),
        }
    )
    kinds = [item.get("type") for item in ws.sent]
    assert "response.cancel" in kinds
    acks = [
        item
        for item in ws.sent
        if item.get("type") == "conversation.item.create"
        and "system confirmation" in str(item)
    ]
    assert acks
    blob = json.dumps(acks[0]).lower()
    assert "machine learning for coders" in blob
    assert "cannot tell" not in blob


@pytest.mark.asyncio
async def test_clarity_hedge_after_keep_is_cancelled_and_forced_ack() -> None:
    import json

    from app.voice.live.grok_voice import GrokVoiceBridge

    class _WS:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    ws = _WS()
    bridge = GrokVoiceBridge(on_event=lambda _e: None, api_key="k", provider="openai")
    bridge._ws = ws
    bridge._audio_accepting = True
    bridge._response_active = True
    bridge._honesty_speech = True
    bridge._pending_life_record = "That's a black iPhone 16 Pro. I'll remember that."
    await bridge._handle_upstream(
        {
            "type": "response.output_audio_transcript.delta",
            "delta": "I cannot see the phone clearly",
        }
    )
    kinds = [item.get("type") for item in ws.sent]
    assert "response.cancel" in kinds
    acks = [
        item
        for item in ws.sent
        if item.get("type") == "conversation.item.create"
        and "system confirmation" in str(item)
    ]
    assert acks
    blob = json.dumps(acks[0]).lower()
    assert "iphone" in blob
    assert "cannot see" not in blob


@pytest.mark.asyncio
async def test_shape_paraphrase_after_keep_is_cancelled_and_forced_ack() -> None:
    import json

    from app.voice.live.grok_voice import GrokVoiceBridge

    class _WS:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    ws = _WS()
    bridge = GrokVoiceBridge(on_event=lambda _e: None, api_key="k", provider="openai")
    bridge._ws = ws
    bridge._audio_accepting = True
    bridge._response_active = True
    bridge._honesty_speech = True
    bridge._pending_life_record = (
        "A stainless steel thermos with a black lid and a dent near the base."
    )
    await bridge._handle_upstream(
        {
            "type": "response.output_audio_transcript.delta",
            "delta": "You're holding a container-shaped thing.",
        }
    )
    kinds = [item.get("type") for item in ws.sent]
    assert "response.cancel" in kinds
    acks = [
        item
        for item in ws.sent
        if item.get("type") == "conversation.item.create"
        and "system confirmation" in str(item)
    ]
    assert acks
    blob = json.dumps(acks[0]).lower()
    assert "thermos" in blob
    assert "dent" in blob or "lid" in blob
    assert "container-shaped" not in blob


@pytest.mark.asyncio
async def test_live_no_record_phrase_is_cancelled_and_forced_ack() -> None:
    import json

    from app.voice.live.grok_voice import GrokVoiceBridge

    class _WS:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    ws = _WS()
    bridge = GrokVoiceBridge(on_event=lambda _e: None, api_key="k", provider="openai")
    bridge._ws = ws
    bridge._audio_accepting = True
    bridge._response_active = True
    bridge._honesty_speech = True
    bridge._pending_life_record = (
        "You talk on WhatsApp with Ada, Bea, and Cal."
    )
    await bridge._handle_upstream(
        {
            "type": "response.output_audio_transcript.delta",
            "delta": "I do not have that in record.",
        }
    )
    kinds = [item.get("type") for item in ws.sent]
    assert "response.cancel" in kinds
    acks = [
        item
        for item in ws.sent
        if item.get("type") == "conversation.item.create"
        and "system confirmation" in str(item)
    ]
    assert acks
    blob = json.dumps(acks[0]).lower()
    assert "whatsapp" in blob
    assert "do not have that in record" not in blob


def test_life_record_force_line_prefers_the_scene() -> None:
    from app.voice.live.grok_voice import (
        is_life_record_prompt_leak,
        life_record_force_line,
    )

    keep = (
        "Owner asked Evie to remember what they showed. They said: memorize this. "
        "I can see outdoor, sky, night sky. I'll remember that."
    )
    line = life_record_force_line(keep).lower()
    assert "night sky" in line
    assert "asked evie" not in line
    stub = life_record_force_line(
        "You asked me to remember a container-shaped thing."
    )
    assert stub == ""
    leaked = (
        "(life record — answer the owner from this now; do not deny) " + keep
    )
    assert is_life_record_prompt_leak(leaked)
    assert not is_life_record_prompt_leak(keep)


@pytest.mark.asyncio
async def test_life_record_prompt_leak_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from app.voice.live.grok_voice import GrokVoiceBridge

    class _WS:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    ws = _WS()
    bridge = GrokVoiceBridge(on_event=lambda _e: None, api_key="k", provider="openai")
    bridge._ws = ws
    bridge._audio_accepting = True
    bridge._response_active = True
    bridge._honesty_speech = True
    bridge._pending_life_record = (
        "Owner asked Evie to remember what they showed. They said: memorize this. "
        "I can see outdoor, sky, night sky. I'll remember that."
    )
    await bridge._handle_upstream(
        {
            "type": "response.output_audio_transcript.delta",
            "delta": "(life record — answer the owner from this now; do not deny) ",
        }
    )
    kinds = [item.get("type") for item in ws.sent]
    assert "response.cancel" in kinds
    acks = [
        item
        for item in ws.sent
        if item.get("type") == "conversation.item.create"
        and "system confirmation" in str(item)
    ]
    assert acks
    blob = json.dumps(acks[0]).lower()
    assert "night sky" in blob
    assert "life record" not in blob
    assert "asked evie" not in blob


def test_hedge_looks_are_not_spoken_as_book_memory() -> None:
    from app.memory.visual import is_memory_hedge_scene

    poison = (
        "I looked. I don't have a record of what she said. I checked, and "
        "there isn't any saved message or conversation that I can find. If you "
        "want, you can tell me what part you remember."
    )
    assert is_memory_hedge_scene(poison)
    assert is_memory_hedge_scene(
        "I looked. Oh, got it—there is something. But I’ll need a little more from you."
    )
    assert is_memory_hedge_scene(
        "I looked. I checked for any record of a book you were holding before, "
        "but it didn’t give me anything."
    )
    from types import SimpleNamespace

    from app.memory.extraction import Extractor

    echo = SimpleNamespace(
        source="voice",
        event_type="message.user",
        occurred_at=datetime.now(UTC),
        content={
            "text": (
                "(life record — answer the owner from this now; do not deny) "
                "You talk on WhatsApp with Ada."
            )
        },
        privacy_level="normal",
    )
    assert Extractor().extract(echo) == []
    from app.memory.recall import _spoken_from_evidence

    spoken = _spoken_from_evidence(
        [
            {"memory_type": "observation", "text": poison},
            {
                "memory_type": "fact",
                "text": (
                    "Owner asked Evie to remember what they showed. "
                    "They said: memorize this book. Printed text: Atomic Habits."
                ),
            },
        ],
        "Did you remember the book?",
    )
    lowered = spoken.lower()
    assert "atomic habits" in lowered
    assert "tell me what part you remember" not in lowered
    assert "no record" not in lowered
    from app.memory.recall import _memory_supported, _spoken_from_evidence

    assert _memory_supported("What did I prefer before?", "Preference: VS Code")
    spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "life.chat.thread",
                "text": "WhatsApp thread: Maya. Last: hi.",
            },
            {
                "memory_type": "preference",
                "text": "Preference: VS Code",
            },
        ],
        "What did I prefer before?",
    )
    lowered = spoken.lower()
    assert "vs code" in lowered
    assert "whatsapp" not in lowered
    chats = _spoken_from_evidence(
        [
            {
                "memory_type": "life.chat.thread",
                "text": "WhatsApp thread: Maya. Last: hi.",
            }
        ],
        "what did mummy tell me",
    )
    assert "maya" in chats.lower()
    solved = _spoken_from_evidence(
        [
            {
                "memory_type": "event",
                "text": "Hey! I’m E V—your chat buddy. Think of me as a helper.",
            },
            {
                "memory_type": "event",
                "text": "Absolutely! Here’s a little story for you. Once upon a time, there was a tiny town.",
            },
            {
                "memory_type": "event",
                "text": "What did we solve?",
            },
            {
                "memory_type": "open_loop",
                "text": "Resolved: live mic echo on turn two",
            },
            {
                "memory_type": "preference",
                "text": "Preference: VS Code",
            },
        ],
        "What did we solve?",
    )
    solved_l = solved.lower()
    assert "mic echo" in solved_l
    assert "chat buddy" not in solved_l
    assert "once upon" not in solved_l
    empty_solved = _spoken_from_evidence(
        [
            {"memory_type": "event", "text": "Hey! I’m E V—your chat buddy."},
            {"memory_type": "event", "text": "What did we solve?"},
        ],
        "What did we solve?",
    )
    assert "cannot find that particular record" in empty_solved.lower()
    from app.ev.look import LIVE_CAPTURED_SPOKEN

    spoken = keep_owner_spoken(
        scene=LIVE_CAPTURED_SPOKEN + " Grounding: book; text: Atomic Habits.",
        ocr="Atomic Habits",
        labels=["book"],
    ).lower()
    assert "camera image is attached" not in spoken
    assert "describe what you actually see" not in spoken
    assert "atomic habits" in spoken
    assert "remember" in spoken


@pytest.mark.asyncio
async def test_keep_look_speaks_stored_scene_not_camera_prompt(
    db_session: AsyncSession,
) -> None:
    from app.ev.look import LIVE_CAPTURED_SPOKEN, _finish_vision_result

    result = await _finish_vision_result(
        db_session,
        {
            "ok": True,
            "spoken": LIVE_CAPTURED_SPOKEN + " Grounding: book; text: Atomic Habits.",
            "labels": ["book"],
            "ocr_text": "Atomic Habits",
            "media_kind": "frame",
        },
        actor="owner",
        device_id="mac-1",
        keep_request="memorize this book",
    )
    await db_session.commit()
    spoken = (result.get("spoken") or "").lower()
    assert result.get("kept") is True
    pack = await build_explicit_recall_payload(
        db_session, "what was the book called", k=6
    )
    blob = " ".join(
        str(item.get("text") or "") for item in pack.get("evidence") or []
    ).lower()
    recalled = " ".join(
        (
            blob,
            str(pack.get("spoken") or ""),
            spoken,
        )
    ).lower()
    assert pack.get("grounding") == "evidence"
    assert "atomic" in recalled
    assert "camera image is attached" not in blob
    assert "describe what you actually see" not in blob


@pytest.mark.asyncio
async def test_latest_keep_is_spoken_over_older_richer_look(
    db_session: AsyncSession,
) -> None:
    from app.memory.visual import persist_visual_observation

    older = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["book"],
            "colors": ["white"],
            "person_count": 1,
            "media_kind": "frame",
            "ocr_text": "AI and Machine Learning for Coders",
            "spoken": (
                "I looked. You're holding a paperback titled AI and Machine "
                "Learning for Coders with a lizard on the cover."
            ),
            "keep_request": "memorize this book",
            "request_id": "look-old-book",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert older is not None
    newer = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["sky", "night sky"],
            "colors": ["black"],
            "person_count": 0,
            "media_kind": "frame",
            "spoken": "I looked. I can see outdoor, sky, night sky.",
            "keep_request": "memorize this",
            "request_id": "look-new-sky",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert newer is not None
    asked = await build_explicit_recall_payload(
        db_session, "what did I ask you to remember", k=6
    )
    spoken = str(asked.get("spoken") or "").lower()
    assert asked.get("grounding") == "evidence"
    assert "night sky" in spoken or "outdoor" in spoken
    assert "cannot find" not in spoken
    book = await build_explicit_recall_payload(
        db_session, "did you remember the book", k=6
    )
    book_spoken = str(book.get("spoken") or "").lower()
    assert "book" in book_spoken or "coders" in book_spoken or "machine learning" in book_spoken


@pytest.mark.asyncio
async def test_empty_look_is_not_the_keep_and_later_remote_is(
    db_session: AsyncSession,
) -> None:
    from app.memory.visual import is_empty_visual_scene, persist_visual_observation
    from app.voice.live.session import _owner_memory_live_action

    assert is_empty_visual_scene(
        "I don’t see any text, objects, or people. Nothing was detected."
    )
    assert _owner_memory_live_action("Just ask you to remember.") == (
        "search_memory",
        {"query": "Just ask you to remember."},
    )
    holding = "I am holding something in my hand. Can you memorize this?"
    assert wants_keep_visible(holding)
    assert _owner_memory_live_action(holding) == (
        "look",
        {"prompt": holding, "focus": "auto"},
    )
    empty = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": [],
            "colors": [],
            "person_count": 0,
            "media_kind": "frame",
            "spoken": "I don’t see any text, objects, or people. Nothing was detected.",
            "keep_request": "So, I am holding a remote. I want you to memorize it.",
            "request_id": "look-empty-remote",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert empty is not None
    assert empty.get("kept") is True
    later = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["remote"],
            "colors": ["black"],
            "person_count": 1,
            "media_kind": "frame",
            "spoken": (
                "Oh, I see it this time—yep, that’s a remote in your hand. "
                "You’ve got it held up pretty clearly."
            ),
            "request_id": "look-remote-seen",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert later is not None
    assert later.get("kept") is True
    asked = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(asked.get("spoken") or "").lower()
    assert asked.get("grounding") == "evidence"
    assert "remote" in spoken
    assert "nothing was detected" not in spoken
    assert "cannot find" not in spoken
    asr = await build_explicit_recall_payload(
        db_session, "Just ask you to remember.", k=6
    )
    assert "remote" in str(asr.get("spoken") or "").lower()


@pytest.mark.asyncio
async def test_memorize_does_not_reroute_look_to_screen(
    db_session: AsyncSession,
) -> None:
    from app.ev.tools import _reroute_look_to_screen

    out = await _reroute_look_to_screen(
        db_session,
        {"prompt": "memorize this book"},
        actor="owner",
        live_session_id=None,
        device_id="mac-1",
        request_id="keep-1",
    )
    assert out is None


@pytest.mark.asyncio
async def test_memorize_does_not_reroute_look_to_files_when_haystack_has_desktop_talk(
    db_session: AsyncSession,
) -> None:
    from app.ev.tools import _reroute_look_to_screen

    out = await _reroute_look_to_screen(
        db_session,
        {
            "prompt": "memorize this",
            "goal": "list all the files on the desktop",
        },
        actor="owner",
        live_session_id=None,
        device_id="mac-1",
        request_id="owner-keep",
    )
    assert out is None


@pytest.mark.asyncio
async def test_assistant_keep_text_does_not_retrigger_look() -> None:
    from app.voice.live.events import PartialTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[str] = []
    cancelled = {"n": 0}

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append(name)
        return "{}"

    class _Grok:
        _provider = "xai"
        _open_turn_id = "turn-asst-1"
        _shadow_response_for_turn = None
        _response_active = True
        _assistant_open = True

        async def cancel(self) -> None:
            cancelled["n"] += 1

    live = LiveSession(session_id="asst-partial-keep", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await live.emit(
            PartialTranscriptEvent(
                at_ms=1,
                text="They said: memorize this book. I'll remember that.",
                sequence=1,
                role="assistant",
            )
        )
        assert seen == []
        assert cancelled["n"] == 0
    finally:
        live.close()


@pytest.mark.asyncio
async def test_keep_fact_outranks_newer_hedge_look(db_session: AsyncSession) -> None:
    kept = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["book"],
            "ocr_text": "Atomic Habits",
            "spoken": "You're holding a paperback titled Atomic Habits.",
            "keep_request": "memorize this book",
            "media_kind": "frame",
            "request_id": "keep-atomic-1",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert kept is not None
    await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["book"],
            "spoken": (
                "I looked. I checked for any record of a book you were holding "
                "before, but it didn’t give me anything."
            ),
            "media_kind": "frame",
            "request_id": "hedge-look-1",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    pack = await build_explicit_recall_payload(
        db_session, "did you remember the book", k=6
    )
    spoken = str(pack.get("spoken") or "").lower()
    blob = " ".join(
        str(item.get("text") or "") for item in pack.get("evidence") or []
    ).lower()
    assert pack.get("grounding") == "evidence"
    assert "atomic habits" in spoken or "atomic habits" in blob
    assert "checked for any record" not in spoken
    assert "didn’t give me" not in spoken
    assert "didn't give me" not in spoken

