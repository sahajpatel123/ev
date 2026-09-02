"""Companion loop: visual memory, heading-out, visor HUD, verified computer hands."""

from __future__ import annotations

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


def test_keep_visible_routes_to_look_not_a_glance_refusal() -> None:
    assert wants_keep_visible("memorise this")
    assert wants_keep_visible("memorize this")
    assert wants_keep_visible("remember this")
    assert not wants_keep_visible("Remember that I'm calling this experiment Project Harbor.")
    assert not wants_keep_visible("Do you remember what I asked you to keep?")
    assert wants_keep_visible("memorize a lantern")
    assert wants_keep_visible("remember this mug")
    assert not wants_keep_visible("did you memorize the lantern")
    assert not wants_keep_visible("did you remember the mug")
    assert not wants_keep_visible("have you memorized the lantern")
    assert not is_visual_recall_query("memorise this")
    assert is_visual_recall_query("what did I ask you to remember")
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
    from app.ev.look import resolve_keep_request

    assert resolve_keep_request("memorize a lantern") == "memorize a lantern"
    assert resolve_keep_request("Describe visible people, objects, colors, and the scene.") == ""
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
