"""Generic visual identity for memorize/recall — not a book/remote special case."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.recall import _spoken_from_evidence, build_explicit_recall_payload
from app.memory.visual import (
    extract_visual_identity,
    is_empty_visual_scene,
    is_generic_label_scene,
    is_keep_ack_only,
    is_keep_identity_speech,
    is_keep_injection_spoken,
    is_nonvisual_keep_speech,
    keep_owner_spoken,
    keep_perception_allow_raw,
    keep_sight_text,
    looks_like_visual_description,
    owner_memory_hit_text,
    persist_visual_observation,
    recall_spoken_from_keep,
)
from app.voice.live.grok_voice import life_record_force_line


def test_identity_strips_owner_request_and_waffle() -> None:
    identity = extract_visual_identity(
        scene=(
            "So, I am holding a remote. I want you to memorize it. "
            "Oh, I see it this time—yep, that’s a remote in your hand. "
            "You’ve got it held up pretty clearly, and I can see the black "
            "device with buttons and a display area."
        ),
        keep_request="So, I am holding a remote. I want you to memorize it.",
        colors=["black"],
    )
    assert identity["object"] == "remote"
    assert "black" in identity["colors"]
    recall = identity["recall"].lower()
    assert "remote" in recall
    assert "i want you to memorize" not in recall
    assert "asked evie" not in recall
    assert "they said" not in recall
    spoken = identity["spoken"].lower()
    assert "remote" in spoken
    assert "hold it in the camera" not in spoken


def test_identity_works_for_unseen_objects() -> None:
    lantern = extract_visual_identity(
        scene="You're holding a brass lantern with a glass chimney.",
        keep_request="memorize this",
        labels=["lantern"],
        colors=["gold"],
    )
    assert "lantern" in lantern["object"]
    assert "lantern" in lantern["recall"].lower()
    mug = extract_visual_identity(
        scene="A white mug with a blue wave logo sitting in your hand.",
        keep_request="I want you to memorize this.",
        labels=["mug"],
        colors=["white", "blue"],
    )
    assert mug["object"] == "mug"
    assert "mug" in mug["recall"].lower()
    assert "wave" in mug["recall"].lower() or "white" in mug["recall"].lower()


def test_empty_scene_does_not_claim_a_seen_object() -> None:
    identity = extract_visual_identity(
        scene="I don’t see any text, objects, or people. Nothing was detected.",
        keep_request="So, I am holding a lantern. I want you to memorize it.",
    )
    assert identity["object"] == "lantern"
    assert identity["usable"] is False
    assert "hold it in the camera" in identity["spoken"].lower()
    assert "lantern" in identity["recall"].lower()


def test_keep_recall_does_not_speak_the_question_back() -> None:
    from app.memory.recall import _spoken_from_evidence
    from app.memory.visual import is_keep_recall_query

    asked = "What I ask you to memorize or remember"
    assert is_keep_recall_query(asked)
    spoken = _spoken_from_evidence(
        [
            {
                "text": "What I ask you to memorize and remember",
                "reason": "visual_keep",
                "kind": "visual_keep",
            }
        ],
        asked,
    ).lower()
    assert "what i ask" not in spoken
    assert "cannot find" in spoken
    named = _spoken_from_evidence(
        [
            {
                "text": "What I ask you to memorize and remember",
                "reason": "visual_keep",
                "kind": "visual_keep",
                "when": "2026-09-06T03:00:00+00:00",
            },
            {
                "text": (
                    "A Davidoff mocha chewing coffee candy tin with gold "
                    "lettering on a brown pack."
                ),
                "reason": "visual_keep",
                "kind": "visual_keep",
                "when": "2026-09-06T02:00:00+00:00",
            },
        ],
        asked,
    ).lower()
    assert "davidoff" in named or "mocha" in named or "candy" in named
    assert "what i ask" not in named


def test_keep_fact_is_a_recall_line_not_the_owner_transcript() -> None:
    text = keep_sight_text(
        user_text="memorize this",
        scene="A green water bottle with a dented cap.",
        labels=["bottle"],
        colors=["green"],
    ).lower()
    assert "bottle" in text
    assert "you asked me to remember" in text
    assert "they said:" not in text
    assert keep_owner_spoken(
        scene="A green water bottle with a dented cap.",
        labels=["bottle"],
        colors=["green"],
        keep_request="memorize this",
    ).lower().startswith("a green water bottle")


def test_keep_recall_enrich_fits_live_websocket() -> None:
    from app.memory.visual import KEEP_RECALL_ENRICH_SECONDS

    assert 12.0 <= KEEP_RECALL_ENRICH_SECONDS <= 35.0


def test_old_keep_blob_still_speaks_the_scene() -> None:
    blob = (
        "Owner asked Evie to remember what they showed. They said: memorize this. "
        "I can see outdoor, sky, night sky. I'll remember that."
    )
    line = recall_spoken_from_keep(blob).lower()
    assert "night sky" in line
    assert "asked evie" not in line
    assert "they said" not in line
    forced = life_record_force_line(blob).lower()
    assert "night sky" in forced
    assert "asked evie" not in forced


def test_live_memory_hits_speak_the_shown_thing_not_the_keep_header() -> None:
    line = owner_memory_hit_text(
        "You asked me to remember a container. "
        "A stainless steel thermos with a black lid and a dent near the base.",
        {
            "description": (
                "A stainless steel thermos with a black lid and a dent near the base."
            ),
            "kind": "visual_keep",
        },
    ).lower()
    assert "thermos" in line
    assert "dent" in line or "lid" in line
    assert "you asked me to remember" not in line
    assert "container-shaped" not in line
    mummy = owner_memory_hit_text("Person: Mummy. WhatsApp thread with Mummy.")
    assert "mummy" in mummy.lower()
    assert "whatsapp" in mummy.lower()


def test_header_prefixed_keep_index_still_speaks_identity() -> None:
    line = owner_memory_hit_text(
        "You asked me to remember a phone. "
        "A matte black handset with a silver rim and a dent on the left edge.",
        {"kind": "visual_keep"},
    ).lower()
    assert "handset" in line or "dent" in line or "silver" in line
    assert "you asked me to remember" not in line
    spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "fact",
                "reason": "visual_keep",
                "attachment_id": "att-new",
                "when": "2026-09-06T00:00:00Z",
                "text": (
                    "You asked me to remember a phone. "
                    "A matte black handset with a silver rim and a dent on the left edge."
                ),
            },
            {
                "memory_type": "fact",
                "reason": "visual_keep",
                "attachment_id": "att-old",
                "when": "2026-08-01T00:00:00Z",
                "text": (
                    "A stainless steel thermos with a black lid "
                    "and a dent near the base."
                ),
            },
        ],
        "What did I just ask you to remember?",
    ).lower()
    assert "handset" in spoken or "lettering" in spoken or "matte" in spoken or "dent" in spoken
    assert "thermos" not in spoken
    assert "you asked me to remember" not in spoken


def test_spoken_evidence_prefers_identity_over_request_echo() -> None:
    spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "fact",
                "reason": "visual_keep",
                "text": (
                    "Owner asked Evie to remember what they showed. "
                    "They said: memorize this book. Printed text: Atomic Habits."
                ),
            }
        ],
        "Did you remember the book?",
    ).lower()
    assert "atomic habits" in spoken
    assert "they said" not in spoken


@pytest.mark.asyncio
async def test_two_keeps_generic_recall_is_latest_named_is_stable(
    db_session: AsyncSession,
) -> None:
    first = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["mug"],
            "colors": ["white"],
            "media_kind": "frame",
            "spoken": "A white mug with a blue wave logo.",
            "keep_request": "memorize this",
            "request_id": "keep-mug",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert first and first.get("kept")
    second = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["sunglasses"],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": "Black sunglasses with gold hinges.",
            "keep_request": "I want you to memorize this.",
            "request_id": "keep-sunglasses",
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert second and second.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "sunglass" in spoken
    assert "they said" not in spoken
    mug = await build_explicit_recall_payload(db_session, "did you remember the mug", k=6)
    mug_spoken = str(mug.get("spoken") or "").lower()
    assert "mug" in mug_spoken
    assert "cannot find" not in mug_spoken


@pytest.mark.asyncio
async def test_generic_keep_recall_does_not_speak_older_keep(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    older_id = str(uuid4())
    newer_id = str(uuid4())
    first = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["thermos"],
            "colors": ["silver", "black"],
            "media_kind": "frame",
            "spoken": (
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            "keep_request": "memorize this",
            "attachment_id": older_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert first and first.get("kept")
    second = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["mug"],
            "colors": ["white"],
            "media_kind": "frame",
            "spoken": "",
            "keep_request": "memorize this",
            "attachment_id": newer_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert second and second.get("kept")

    async def no_enrich(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.memory.visual._enrich_keep_from_attachment", no_enrich)
    monkeypatch.setattr("app.memory.visual.adopt_recent_spoken_keep", no_enrich)
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "thermos" not in spoken
    assert "dent" not in spoken
    assert "lid" not in spoken
    assert "you asked me to remember" not in spoken
    assert "cannot find" in spoken


@pytest.mark.asyncio
async def test_keep_intent_without_camera_does_not_hide_named_jpeg(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from app.memory.visual import persist_keep_intent

    keep_id = str(uuid4())
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["lantern"],
            "colors": ["brass"],
            "media_kind": "frame",
            "spoken": "A brass lantern with a glass chimney and a dent on the door.",
            "keep_request": "memorize this",
            "attachment_id": keep_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert written and written.get("kept")
    intent = await persist_keep_intent(
        db_session,
        "memorize this",
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert intent is not None

    async def no_enrich(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.memory.visual._enrich_keep_from_attachment", no_enrich)
    monkeypatch.setattr("app.memory.visual.adopt_recent_spoken_keep", no_enrich)
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "lantern" in spoken
    assert "cannot find" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_first_look_replaces_ocr_class_stub(
    db_session: AsyncSession,
) -> None:
    from app.memory.visual import remember_spoken_scene

    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["people", "adult", "material", "curtain"],
            "colors": [],
            "media_kind": "frame",
            "spoken": "That's a material. It reads Dltvl DOFF.",
            "ocr_text": "Dltvl DOFF",
            "keep_request": "memorize this",
            "attachment_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "encoded_bytes": 1,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    bound = await remember_spoken_scene(
        db_session,
        (
            "A Davidoff coffee candy tin with a dark body "
            "and a lighter label band in the middle."
        ),
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert bound and bound.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "davidoff" in spoken or "candy" in spoken or "label" in spoken
    assert "that's a material" not in spoken
    assert "dltvl" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_recall_adopts_same_turn_first_look_when_keep_is_thin(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from app.schemas import EventCreate
    from app.services.event_service import EventService

    async def no_enrich(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.memory.visual._enrich_keep_from_attachment", no_enrich)
    keep_id = str(uuid4())
    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["green"],
            "media_kind": "frame",
            "spoken": "",
            "keep_request": "memorize this",
            "attachment_id": keep_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    await EventService(db_session, actor="owner").create(
        EventCreate(
            source="live",
            event_type="message.assistant",
            text=(
                "A lime-green placard with SERIAL 4K2 printed in black "
                "near the top."
            ),
            device_id="mac-1",
        )
    )
    await db_session.commit()
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "placard" in spoken or "4k2" in spoken or "serial" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_label_only_keep_recall_does_not_name_a_vague_class(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["phone", "container", "electronics"],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": "",
            "keep_request": "memorize this",
            "attachment_id": str(uuid4()),
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert written and written.get("kept")

    async def no_enrich(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.memory.visual._enrich_keep_from_attachment", no_enrich)
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "you asked me to remember" not in spoken
    assert "container" not in spoken
    assert "phone" not in spoken
    assert "cannot find" in spoken


@pytest.mark.asyncio
async def test_later_placement_fact_does_not_replace_keep_identity(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    kept = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["thermos"],
            "colors": ["silver"],
            "media_kind": "frame",
            "spoken": (
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            "keep_request": "memorize this",
            "attachment_id": str(uuid4()),
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert kept and kept.get("kept")
    later = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["mug"],
            "colors": ["white"],
            "media_kind": "frame",
            "spoken": "A white mug sitting on the wooden desk.",
            "attachment_id": str(uuid4()),
            "encoded_bytes": 80000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert later

    async def no_enrich(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.memory.visual._enrich_keep_from_attachment", no_enrich)
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "mug" not in spoken
    assert "desk" not in spoken
    assert "you asked me to remember" not in spoken


def test_pack_evidence_pins_keep_ahead_of_later_facts() -> None:
    from app.memory.recall import _pack_evidence, _spoken_from_evidence

    later = [
        {
            "id": f"place-{index}",
            "when": f"2026-09-06T01:0{index}:00Z",
            "text": f"A white mug sitting on the wooden desk {index}.",
            "kind": "memory",
            "memory_type": "fact",
            "reason": "visual_observation",
            "source": "evie",
        }
        for index in range(8)
    ]
    keep = {
        "id": "keep-1",
        "when": "2026-09-06T00:00:00Z",
        "text": (
            "A stainless steel thermos with a black lid "
            "and a dent near the base."
        ),
        "kind": "memory",
        "memory_type": "fact",
        "reason": "visual_keep",
        "keep_request": "memorize this",
        "attachment_id": "att-keep",
        "source": "evie",
        "recall": (
            "A stainless steel thermos with a black lid "
            "and a dent near the base."
        ),
    }
    packed = _pack_evidence(
        "What did I just ask you to remember?",
        events=later + [keep],
        memories=[],
        episodes=[],
        entities=[],
        neighbors=[],
        k=3,
    )
    assert packed
    assert packed[0]["id"] == "keep-1"
    spoken = _spoken_from_evidence(
        packed, "What did I just ask you to remember?"
    ).lower()
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "mug" not in spoken
    assert "desk" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_spoken_keep_identity_survives_container_labels(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    spoken_identity = (
        "A lime-green chroma test slide with a red stripe across the middle. "
        "SERIAL 4K2 is printed in black near the top."
    )
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["green"],
            "media_kind": "frame",
            "spoken": spoken_identity,
            "keep_request": "memorize this",
            "attachment_id": str(uuid4()),
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert written and written.get("kept")

    async def no_enrich(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.memory.visual._enrich_keep_from_attachment", no_enrich)
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "4k2" in spoken or "chroma" in spoken or "lime" in spoken
    assert "serial" in spoken or "stripe" in spoken or "4k2" in spoken
    assert "container" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_spoken_identity_binds_to_newest_keep_jpeg(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from app.memory.visual import remember_spoken_scene

    older_id = str(uuid4())
    newer_id = str(uuid4())
    first = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["thermos"],
            "colors": ["silver"],
            "media_kind": "frame",
            "spoken": (
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            "keep_request": "memorize this",
            "attachment_id": older_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert first and first.get("kept")
    second = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["mug"],
            "media_kind": "frame",
            "spoken": "",
            "keep_request": "memorize this",
            "attachment_id": newer_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert second and second.get("kept")
    upgraded = await remember_spoken_scene(
        db_session,
        (
            "A matte black handset with a silver rim, a dent on the left edge, "
            "and tiny white lettering near the base."
        ),
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert upgraded and upgraded.get("kept")

    async def no_enrich(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.memory.visual._enrich_keep_from_attachment", no_enrich)
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "handset" in spoken or "lettering" in spoken or "matte" in spoken
    assert "thermos" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_thin_keep_persist_does_not_adopt_older_spoken_identity(
    db_session: AsyncSession,
) -> None:
    from uuid import uuid4

    from app.schemas import EventCreate
    from app.services.event_service import EventService

    older_id = str(uuid4())
    newer_id = str(uuid4())
    first = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["thermos"],
            "colors": ["silver", "black"],
            "media_kind": "frame",
            "spoken": (
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            "keep_request": "memorize this",
            "attachment_id": older_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert first and first.get("kept")
    await EventService(db_session, actor="owner").create(
        EventCreate(
            source="live",
            event_type="message.assistant",
            text=(
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            device_id="mac-1",
        )
    )
    second = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["mug"],
            "colors": ["white"],
            "media_kind": "frame",
            "spoken": "",
            "keep_request": "memorize this",
            "attachment_id": newer_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert second and second.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "thermos" not in spoken
    assert "dent" not in spoken
    assert "you asked me to remember" not in spoken


IPHONE_KEEP = (
    "Okay, so I want you to open camera and remember the item I am showing you. "
    "This is my iPhone 16 Pro, my primary phone."
)


def test_clarity_hedge_with_delivered_frame_names_the_object() -> None:
    from app.memory.visual import is_clarity_hedge, keep_topic

    assert is_clarity_hedge("I cannot see the phone clearly.")
    assert not is_clarity_hedge("You've got it held up pretty clearly")
    assert "iphone" in keep_topic(IPHONE_KEEP)
    spoken = keep_owner_spoken(
        scene="I cannot see the phone clearly.",
        colors=["black"],
        keep_request=IPHONE_KEEP,
        frame_ok=True,
    ).lower()
    assert "iphone" in spoken
    assert "black" in spoken
    assert "cannot see" not in spoken
    assert "hold it in the camera" not in spoken
    empty = keep_owner_spoken(
        scene="I cannot see the phone clearly.",
        keep_request=IPHONE_KEEP,
        frame_ok=False,
    ).lower()
    assert "hold it in the camera" in empty


@pytest.mark.asyncio
async def test_delivered_frame_keep_persists_named_object_not_clarity_hedge(
    db_session: AsyncSession,
) -> None:
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": [],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": "I cannot see the phone clearly.",
            "keep_request": IPHONE_KEEP,
            "request_id": "keep-iphone-16",
            "attachment_id": "att-iphone-16",
            "encoded_bytes": 139000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert written and written.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "iphone" in spoken
    assert "cannot see" not in spoken
    assert "they said" not in spoken


def test_scene_description_beats_generic_classifier_labels() -> None:
    identity = extract_visual_identity(
        scene="A stainless steel thermos with a black lid and a dent near the base.",
        keep_request="I want you to memorize this.",
        labels=["container", "indoor", "object"],
        colors=["black", "silver"],
    )
    assert "thermos" in identity["object"]
    assert "container" not in identity["object"]
    recall = identity["recall"].lower()
    assert "thermos" in recall
    assert "dent" in recall or "lid" in recall
    spoken = keep_owner_spoken(
        scene="A stainless steel thermos with a black lid and a dent near the base.",
        labels=["container", "indoor"],
        colors=["black", "silver"],
        keep_request="I want you to memorize this.",
        frame_ok=True,
    ).lower()
    assert "thermos" in spoken
    assert "container" not in spoken


def test_unnamed_keep_stores_the_visual_description() -> None:
    jar = keep_sight_text(
        user_text="memorize this",
        scene="A small ceramic jar with a cracked glaze and a cork stopper.",
        labels=["container", "shape"],
        colors=["brown"],
    ).lower()
    assert "jar" in jar or "ceramic" in jar
    assert "cracked" in jar or "cork" in jar
    assert "container" not in jar
    watch = keep_sight_text(
        user_text="remember this",
        scene="That's a silver wristwatch with a scratched face and a frayed leather strap.",
        labels=["electronics", "product"],
        colors=["silver"],
    ).lower()
    assert "watch" in watch
    assert "leather" in watch or "scratched" in watch
    assert "electronics" not in watch


def test_classifier_labels_are_not_a_keep_identity() -> None:
    text = keep_sight_text(
        user_text="memorize this",
        labels=["phone", "container", "electronics"],
        colors=["black"],
    ).lower()
    assert "phone" not in text
    assert "container" not in text
    assert "you asked me to remember what you showed" in text
    hit = owner_memory_hit_text(
        "",
        {
            "kind": "visual_keep",
            "description": (
                "A matte black handset with a silver rim and a dent on the left edge."
            ),
        },
    ).lower()
    assert "handset" in hit or "dent" in hit
    assert "you asked me to remember" not in hit


@pytest.mark.asyncio
async def test_later_recall_describes_the_shown_thing_not_a_shape_label(
    db_session: AsyncSession,
) -> None:
    from app.memory.visual import remember_spoken_scene

    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["silver"],
            "media_kind": "frame",
            "spoken": "I can see container, indoor.",
            "keep_request": "memorize this",
            "request_id": "keep-thermos-stub",
            "attachment_id": "att-thermos",
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    upgraded = await remember_spoken_scene(
        db_session,
        "A stainless steel thermos with a black lid and a dent near the base.",
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert upgraded is not None
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "container shape" not in spoken
    assert "they said" not in spoken


@pytest.mark.asyncio
async def test_mini_identity_binds_when_look_was_on_another_device(
    db_session: AsyncSession,
) -> None:
    from app.memory.visual import remember_spoken_scene

    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["silver"],
            "media_kind": "frame",
            "spoken": "I can see container, indoor.",
            "keep_request": "memorize this",
            "request_id": "keep-other-device",
            "attachment_id": "11111111-1111-1111-1111-111111111111",
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-camera",
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    upgraded = await remember_spoken_scene(
        db_session,
        "A stainless steel thermos with a black lid and a dent near the base.",
        actor="owner",
        device_id="mac-talk",
    )
    await db_session.commit()
    assert upgraded and upgraded.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


def test_perception_summary_keeps_distinctive_sentences() -> None:
    from app.ev.vision import _extract_summary

    text = _extract_summary(
        "A container-shaped object on the desk.\n"
        "It is a stainless steel thermos with a black lid and a dent near the base.\n"
        "LABEL: container 0.91\n"
        "LABEL: indoor 0.80"
    ).lower()
    assert "thermos" in text
    assert "dent" in text or "lid" in text
    assert "label:" not in text
    assert "container-shaped" not in text


def test_keep_compose_keeps_more_than_the_first_line() -> None:
    from app.ev.look import _compose_spoken

    spoken = _compose_spoken(
        summary=(
            "A stainless steel thermos with a black lid. "
            "There is a dent near the base and a small paper tag."
        ),
        ocr_text="",
        labels=["container"],
        things=[],
        people=[],
        roster_text=[],
        confirmed=[],
        focus="auto",
        keep=True,
    ).lower()
    assert "thermos" in spoken
    assert "dent" in spoken or "tag" in spoken
    assert "container" not in spoken


@pytest.mark.asyncio
async def test_analysis_description_is_what_later_recall_speaks(
    db_session: AsyncSession,
) -> None:
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["silver", "black"],
            "media_kind": "frame",
            "spoken": (
                "A stainless steel thermos with a black lid. "
                "There is a dent near the base."
            ),
            "keep_request": "memorize this",
            "request_id": "keep-thermos-analysis",
            "attachment_id": "att-thermos-2",
            "encoded_bytes": 121000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert written and written.get("kept")
    from sqlalchemy import select

    from app.models import Memory

    keep_rows = [
        row
        for row in (await db_session.execute(select(Memory))).scalars().all()
        if str((row.payload or {}).get("kind") or "") == "visual_keep"
    ]
    assert keep_rows
    stored_recall = str((keep_rows[-1].payload or {}).get("recall") or "").lower()
    assert "thermos" in stored_recall
    assert "you asked me to remember" not in stored_recall
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "you asked me to remember" not in spoken
    assert "container-shaped" not in spoken
    assert "container shape" not in spoken
    assert "they said" not in spoken


def test_keep_injection_spoken_is_not_identity() -> None:
    from app.ev.look import KEEP_CAPTURED_SPOKEN, _keep_injection_only

    prompt = KEEP_CAPTURED_SPOKEN + " Grounding: container, indoor. Image 1280 by 720."
    assert is_keep_injection_spoken(prompt)
    assert _keep_injection_only({"spoken": prompt})
    assert is_keep_injection_spoken("I can see container, indoor.")
    assert is_keep_injection_spoken("That's a black phone.")
    assert is_keep_injection_spoken("")
    assert not is_keep_injection_spoken(
        "A stainless steel thermos with a black lid and a dent near the base."
    )
    assert is_keep_identity_speech("It reads SERIAL 4K2 on a lime-green card.")


def test_generic_shape_is_not_an_identity() -> None:
    assert is_generic_label_scene("You're holding a container-shaped thing.")
    assert is_generic_label_scene("I can see container, indoor.")
    assert is_generic_label_scene("That's a container.")
    assert is_generic_label_scene("container, indoor")
    assert is_generic_label_scene("You asked me to remember a container-shaped thing.")
    assert is_generic_label_scene("You asked me to remember what you showed.")
    assert recall_spoken_from_keep("A container-shaped thing.") == ""
    assert recall_spoken_from_keep("You're holding a container-shaped thing.") == ""
    assert not is_keep_identity_speech("That's a shaped.")
    assert "shaped" not in (recall_spoken_from_keep("A container-shaped thing.") or "").lower()
    assert not is_generic_label_scene(
        "A stainless steel thermos with a black lid and a dent near the base."
    )
    assert not is_generic_label_scene(
        "Matte black finish, silver rim, a dent on the left edge, "
        "and tiny white lettering near the base."
    )
    spoken = recall_spoken_from_keep(
        "You asked me to remember a container. "
        "A stainless steel thermos with a black lid and a dent near the base.",
        {
            "description": (
                "A stainless steel thermos with a black lid and a dent near the base."
            ),
            "recall": (
                "You asked me to remember a container. "
                "A stainless steel thermos with a black lid and a dent near the base."
            ),
            "keep_request": "memorize this",
        },
    ).lower()
    assert spoken.startswith("a stainless steel thermos")
    assert "you asked me to remember" not in spoken
    assert "container-shaped" not in spoken
    assert is_generic_label_scene("That's a material. It reads Dltvl DOFF.")
    assert recall_spoken_from_keep(
        "You asked me to remember a material. That's a material. It reads Dltvl DOFF.",
        {
            "kind": "visual_keep",
            "object": "material",
            "printed": "Dltvl DOFF",
            "ocr_text": "Dltvl DOFF",
            "labels": ["people", "adult", "material", "curtain"],
            "usable_scene": True,
            "recall": (
                "You asked me to remember a material. "
                "That's a material. It reads Dltvl DOFF."
            ),
        },
    ) == ""


def test_archive_speech_is_not_a_visual_identity() -> None:
    mummy = (
        'I can see "Mummy" as a contact, but I don\'t have the phone number listed.'
    )
    github = (
        "Last you talked with [sahajpatel123/Multi-AI-Agents] Run failed "
        "on the latest check."
    )
    card = (
        "A lime-green card with SERIAL 4K2 printed along the bottom edge."
    )
    assert is_nonvisual_keep_speech(mummy)
    assert is_nonvisual_keep_speech(github)
    assert not is_nonvisual_keep_speech(card)
    assert not looks_like_visual_description(mummy)
    assert not looks_like_visual_description(github)
    assert not is_keep_identity_speech(mummy)
    assert not is_keep_identity_speech(github)
    assert is_keep_identity_speech(card)
    assert recall_spoken_from_keep(
        mummy,
        {"kind": "visual_keep", "description": mummy, "recall": mummy},
    ) == ""
    spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "fact",
                "reason": "visual_keep",
                "kind": "visual_keep",
                "text": mummy,
                "description": mummy,
                "when": "2026-09-06T06:58:44Z",
            }
        ],
        "What did I just ask you to remember?",
    ).lower()
    assert "mummy" not in spoken
    assert "cannot find" in spoken
    greeting = "Hey there! Nice to hear you. What’s up?"
    assert is_nonvisual_keep_speech(greeting)
    assert not looks_like_visual_description(greeting)
    assert not is_keep_identity_speech(greeting)
    assert recall_spoken_from_keep(
        "You asked me to remember what you showed. Hey there! Nice to hear you. What’s up?.",
        {"kind": "visual_keep", "description": greeting, "recall": greeting},
    ) == ""
    greet_spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "fact",
                "reason": "visual_keep",
                "kind": "visual_keep",
                "text": "You asked me to remember what you showed. " + greeting,
                "description": greeting,
                "when": "2026-09-06T01:52:50Z",
            }
        ],
        "What did I just ask you to remember?",
    ).lower()
    assert "hey there" not in greet_spoken
    assert "cannot find" in greet_spoken
    weather = (
        "Let me think about the weather for today. I can't fetch live weather "
        "right now. I don't have a weather function connected."
    )
    assert not is_keep_identity_speech(weather)
    assert recall_spoken_from_keep(
        weather,
        {"kind": "visual_keep", "description": weather, "recall": weather},
    ) == ""
    weather_spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "fact",
                "reason": "visual_keep",
                "kind": "visual_keep",
                "text": "You asked me to remember what you showed. " + weather,
                "description": weather,
                "when": "2026-09-06T02:16:14Z",
            }
        ],
        "What did I just ask you to remember?",
    ).lower()
    assert "weather" not in weather_spoken
    assert "cannot find" in weather_spoken
    from app.ev.look import TIMEOUT_SPOKEN, UNAVAILABLE_SPOKEN

    assert is_empty_visual_scene(TIMEOUT_SPOKEN)
    assert not is_keep_identity_speech(TIMEOUT_SPOKEN)
    assert not is_keep_identity_speech(UNAVAILABLE_SPOKEN)


@pytest.mark.asyncio
async def test_archive_speech_does_not_bind_to_keep(
    db_session: AsyncSession,
) -> None:
    from app.memory.visual import remember_spoken_scene

    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "media_kind": "frame",
            "spoken": "I can see container, indoor.",
            "keep_request": "memorize this",
            "attachment_id": "att-archive-speech",
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    bound = await remember_spoken_scene(
        db_session,
        'I can see "Mummy" as a contact, but I don\'t have the phone number listed.',
        actor="owner",
        device_id="mac-1",
    )
    assert bound is None
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "mummy" not in spoken
    assert "phone number" not in spoken
    weather = (
        "Let me think about the weather for today. I can't fetch live weather "
        "right now. I don't have a weather function connected."
    )
    assert not is_keep_identity_speech(weather)
    weather_bound = await remember_spoken_scene(
        db_session,
        weather,
        actor="owner",
        device_id="mac-1",
    )
    assert weather_bound is None
    grocery = (
        "I can't update your grocery list directly from here, so if Sareen "
        "is a new item, please add it yourself."
    )
    assert is_nonvisual_keep_speech(grocery)
    assert not is_keep_identity_speech(grocery)
    grocery_bound = await remember_spoken_scene(
        db_session,
        grocery,
        actor="owner",
        device_id="mac-1",
    )
    assert grocery_bound is None
    line = (
        "Okay, are you trying to add something new, or did you want to undo "
        "something? I see a line that says milk."
    )
    assert is_nonvisual_keep_speech(line)
    assert await remember_spoken_scene(
        db_session, line, actor="owner", device_id="mac-1"
    ) is None
    from app.memory.visual import persist_keep_intent

    intent = await persist_keep_intent(
        db_session,
        "memorize this",
        actor="owner",
        device_id="mac-no-pixels",
    )
    await db_session.commit()
    assert intent and intent.get("kept")
    hallucinated = await remember_spoken_scene(
        db_session,
        "A stainless steel thermos with a black lid and a dent near the base.",
        actor="owner",
        device_id="mac-no-pixels",
    )
    assert hallucinated is None


def test_ack_and_class_stub_are_not_reusable_identity() -> None:
    from app.memory.visual import _keep_is_thin

    assert is_keep_ack_only("Okay, I'll remember that.")
    assert is_keep_ack_only("Got it.")
    assert is_keep_ack_only("I'll remember that.")
    assert not is_keep_ack_only(
        "A stainless steel thermos with a black lid. I'll remember that."
    )
    assert not is_keep_identity_speech("Okay, I'll remember that.")
    assert not is_keep_identity_speech("That's a phone.")
    assert not is_keep_identity_speech("That's a bottle.")
    assert not is_keep_identity_speech("That's a black phone.")
    assert not is_keep_identity_speech("That's a silver bottle.")
    assert is_keep_identity_speech(
        "A black handset with a camera island and a scratch near the mute switch."
    )
    assert is_keep_identity_speech(
        "Handset with a camera island, titanium sides, and a scratch near the mute switch."
    )
    assert _keep_is_thin(
        {
            "kind": "visual_keep",
            "description": "That's a phone.",
            "usable_scene": True,
            "object": "phone",
        },
        "That's a phone.",
    )
    assert _keep_is_thin(
        {
            "kind": "visual_keep",
            "description": "That's a black phone.",
            "usable_scene": True,
            "object": "phone",
            "colors": ["black"],
        },
        "That's a black phone.",
    )
    assert not _keep_is_thin(
        {
            "kind": "visual_keep",
            "description": (
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            "usable_scene": True,
            "object": "thermos",
        },
        "A stainless steel thermos with a black lid and a dent near the base.",
    )


def test_retain_visual_keep_identity_keeps_richer_first_look() -> None:
    from app.memory.visual import _keep_is_thin, retain_visual_keep_identity

    first = (
        "A stainless steel thermos with a black lid and a dent near the base."
    )
    later = "A black smartphone with a screen."
    assert _keep_is_thin(
        {"kind": "visual_keep", "description": later, "usable_scene": True},
        later,
    )
    assert retain_visual_keep_identity(
        {"kind": "visual_keep", "description": first, "usable_scene": True},
        first,
        {"kind": "visual_keep", "description": later, "usable_scene": True},
        later,
    )
    assert not retain_visual_keep_identity(
        {"kind": "visual_keep", "description": later, "usable_scene": True},
        later,
        {"kind": "visual_keep", "description": first, "usable_scene": True},
        first,
    )


def test_concise_first_look_does_not_need_jpeg_reread() -> None:
    from uuid import uuid4

    from app.memory.visual import _keep_line_is_identity, _keep_needs_enrichment

    line = "A black iPhone 16 Pro with a titanium camera island."
    assert _keep_line_is_identity(line)
    aid = str(uuid4())
    assert not _keep_needs_enrichment(
        {
            "attachment_id": aid,
            "description": line,
            "recall": "You asked me to remember an iPhone. " + line,
            "text": "You asked me to remember an iPhone.",
        }
    )
    assert _keep_needs_enrichment(
        {
            "attachment_id": aid,
            "description": "",
            "recall": "You asked me to remember what you showed.",
            "text": "You asked me to remember what you showed.",
        }
    )


def test_live_first_look_outranks_later_reread_waffle() -> None:
    from app.memory.visual import retain_visual_keep_identity

    first = "A black iPhone 16 Pro with a titanium camera island."
    later = (
        "A black handheld smartphone with a large glossy screen, "
        "rounded corners, and a triple-lens housing on the back panel."
    )
    assert retain_visual_keep_identity(
        {
            "kind": "visual_keep",
            "description": first,
            "usable_scene": True,
            "identity_source": "live",
        },
        first,
        {
            "kind": "visual_keep",
            "description": later,
            "usable_scene": True,
            "identity_source": "reread",
        },
        later,
    )
    assert not retain_visual_keep_identity(
        {
            "kind": "visual_keep",
            "description": later,
            "usable_scene": True,
            "identity_source": "reread",
        },
        later,
        {
            "kind": "visual_keep",
            "description": first,
            "usable_scene": True,
            "identity_source": "live",
        },
        first,
    )


def test_thin_class_is_not_what_later_recall_speaks() -> None:
    line = recall_spoken_from_keep(
        "You asked me to remember a phone.",
        {
            "kind": "visual_keep",
            "description": "That's a black phone.",
            "object": "phone",
            "colors": ["black"],
            "usable_scene": True,
        },
    ).lower()
    assert "you asked me to remember" not in line
    assert "black phone" not in line
    spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "fact",
                "reason": "visual_keep",
                "kind": "visual_keep",
                "text": "You asked me to remember a phone.",
                "description": "That's a black phone.",
            }
        ],
        "What did I just ask you to remember?",
    ).lower()
    assert "you asked me to remember" not in spoken
    assert "black phone" not in spoken
    assert "container-shaped" not in spoken


@pytest.mark.asyncio
async def test_ack_speech_does_not_replace_keep_identity(
    db_session: AsyncSession,
) -> None:
    from app.memory.visual import remember_spoken_scene

    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["silver"],
            "media_kind": "frame",
            "spoken": "I can see container, indoor.",
            "keep_request": "memorize this",
            "attachment_id": "att-ack",
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    ack = await remember_spoken_scene(
        db_session,
        "Okay, I'll remember that.",
        actor="owner",
        device_id="mac-1",
    )
    assert ack is None
    named = await remember_spoken_scene(
        db_session,
        "A stainless steel thermos with a black lid and a dent near the base.",
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert named and named.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "thermos" in spoken
    assert "okay" not in spoken
    assert "you asked me to remember" not in spoken


def test_spoken_evidence_uses_stored_description_not_keep_header() -> None:
    spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "fact",
                "reason": "visual_keep",
                "text": (
                    "You asked me to remember a container. "
                    "A stainless steel thermos with a black lid."
                ),
                "recall": (
                    "You asked me to remember a container. "
                    "A stainless steel thermos with a black lid."
                ),
                "description": (
                    "A stainless steel thermos with a black lid and a dent near the base."
                ),
            }
        ],
        "What did I just ask you to remember?",
    ).lower()
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "you asked me to remember" not in spoken
    assert "container-shaped" not in spoken


@pytest.mark.asyncio
async def test_thin_keep_rereads_stored_frame_on_later_recall(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    attachment_id = str(uuid4())
    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["silver"],
            "media_kind": "frame",
            "spoken": "I can see container, indoor.",
            "keep_request": "memorize this",
            "request_id": "keep-thin-frame",
            "attachment_id": attachment_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert stub and stub.get("kept")

    async def fake_enrich(session, item, **_kwargs):
        return await persist_visual_observation(
            session,
            {
                "ok": True,
                "labels": ["container"],
                "colors": ["silver", "black"],
                "media_kind": "frame",
                "spoken": (
                    "A stainless steel thermos with a black lid "
                    "and a dent near the base."
                ),
                "keep_request": item.get("keep_request") or "memorize this",
                "attachment_id": item.get("attachment_id"),
                "encoded_bytes": 120000,
                "image_ready": True,
            },
            actor="owner",
            device_id="mac-1",
        )

    monkeypatch.setattr("app.memory.visual._enrich_keep_from_attachment", fake_enrich)
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken

    spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "fact",
                "reason": "visual_keep",
                "text": (
                    "You asked me to remember a container. "
                    "A stainless steel thermos with a black lid."
                ),
                "recall": (
                    "You asked me to remember a container. "
                    "A stainless steel thermos with a black lid."
                ),
                "description": (
                    "A stainless steel thermos with a black lid and a dent near the base."
                ),
            }
        ],
        "What did I just ask you to remember?",
    ).lower()
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "you asked me to remember" not in spoken
    assert "container-shaped" not in spoken


def test_keep_perception_reads_pixels_only_for_memorize() -> None:
    assert keep_perception_allow_raw("memorize this") is True
    assert keep_perception_allow_raw("I want you to remember this") is True
    assert keep_perception_allow_raw("what do you see") is False
    assert keep_perception_allow_raw(None) is False


@pytest.mark.asyncio
async def test_keep_polish_does_not_rewrite_jpeg_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev import look

    class Spark:
        name = "meta_muse_spark"
        api_key = "must-not-run"

        async def chat(self, *args, **kwargs):
            raise AssertionError("usable keep identity must not be rewritten from labels")

    monkeypatch.setattr("app.ev.look.get_chat_provider", lambda: Spark())
    out = await look._polish_spoken(
        "A stainless steel thermos with a black lid and a dent near the base.",
        {"keep": True, "labels": ["container", "indoor"], "ocr_text": ""},
    )
    lowered = out.lower()
    assert "thermos" in lowered
    assert "dent" in lowered or "lid" in lowered
    assert "container-shaped" not in lowered


@pytest.mark.asyncio
async def test_keep_look_stores_jpeg_identity_for_later_recall(
    db_session: AsyncSession,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.ev.look import look_now
    from app.ev.tools import dispatch

    captured: dict = {}

    async def fake_analyze(session, attachment_id, **kwargs):
        captured.update(kwargs)
        captured["attachment_id"] = str(attachment_id)
        return SimpleNamespace(
            id="perc-keep-jpeg",
            payload={
                "summary": (
                    "A stainless steel thermos with a black lid "
                    "and a dent near the base."
                ),
                "labels": [
                    {"label": "container", "confidence": 0.8},
                    {"label": "indoor", "confidence": 0.7},
                    {"label": "thermos", "confidence": 0.9},
                ],
                "ocr_text": "",
                "raw_sent": bool(kwargs.get("allow_raw")),
                "colors": ["silver", "black"],
            },
        )

    monkeypatch.setattr("app.ev.vision.analyze_attachment", fake_analyze)
    resp = await client.post(
        "/v1/attachments",
        files={"file": ("keep.png", b"KEEP-FRAME", "image/png")},
        data={"privacy_level": "normal", "event_type": "camera.look"},
    )
    assert resp.status_code == 201, resp.text
    attachment_id = resp.json()["attachment"]["id"]
    result = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        attachment_id=attachment_id,
        focus="auto",
    )
    await db_session.commit()
    assert captured.get("allow_raw") is True
    assert result.get("ok") is True
    assert result.get("raw_sent") is True
    spoken_now = str(result.get("spoken") or "").lower()
    assert "thermos" in spoken_now
    assert "dent" in spoken_now or "lid" in spoken_now
    assert "container-shaped" not in spoken_now

    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken

    generic = await dispatch(
        db_session,
        "look",
        {"attachment_id": attachment_id, "focus": "auto"},
        actor="owner",
        allow_sensitive=True,
        channel="action",
    )
    assert captured.get("allow_raw") is False
    assert generic.ok is True
    assert (generic.result or {}).get("raw_sent") is False


@pytest.mark.asyncio
async def test_keep_enrich_rereads_the_stored_jpeg(
    db_session: AsyncSession,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.memory.visual import _enrich_keep_from_attachment

    resp = await client.post(
        "/v1/attachments",
        files={"file": ("keep.png", b"KEEP-FRAME", "image/png")},
        data={"privacy_level": "normal", "event_type": "camera.look"},
    )
    assert resp.status_code == 201, resp.text
    attachment_id = resp.json()["attachment"]["id"]
    captured: dict = {}

    async def fake_analyze(session, attachment_id, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="perc-keep-enrich",
            payload={
                "summary": (
                    "A stainless steel thermos with a black lid "
                    "and a dent near the base."
                ),
                "labels": [
                    {"label": "container", "confidence": 0.8},
                    {"label": "indoor", "confidence": 0.7},
                ],
                "ocr_text": "",
                "raw_sent": True,
                "colors": ["silver", "black"],
            },
        )

    monkeypatch.setattr("app.ev.vision.analyze_attachment", fake_analyze)
    enriched = await _enrich_keep_from_attachment(
        db_session,
        {
            "attachment_id": attachment_id,
            "keep_request": "memorize this",
            "description": "I can see container, indoor.",
            "labels": ["container", "indoor"],
        },
        actor="owner",
    )
    await db_session.commit()
    assert captured.get("allow_raw") is True
    from app.ev.look import KEEP_LOOK_PROMPT

    assert captured.get("prompt") == KEEP_LOOK_PROMPT
    assert enriched and enriched.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_thin_keep_reread_helper_stores_jpeg_identity(
    db_session: AsyncSession,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.ev.look import KEEP_REREAD_PROMPT
    from app.memory.visual import reread_keep_identity_from_attachment

    resp = await client.post(
        "/v1/attachments",
        files={"file": ("keep.png", b"KEEP-FRAME", "image/png")},
        data={"privacy_level": "normal", "event_type": "camera.look"},
    )
    assert resp.status_code == 201, resp.text
    attachment_id = resp.json()["attachment"]["id"]
    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["silver"],
            "media_kind": "frame",
            "spoken": "I can see container, indoor.",
            "keep_request": "memorize this",
            "attachment_id": attachment_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert stub and stub.get("kept")

    captured: dict = {}

    async def fake_analyze(session, _attachment_id, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="perc-keep-reread",
            payload={
                "summary": (
                    "A stainless steel thermos with a black lid "
                    "and a dent near the base."
                ),
                "labels": [{"label": "thermos", "confidence": 0.9}],
                "ocr_text": "",
                "raw_sent": bool(kwargs.get("allow_raw")),
                "colors": ["silver", "black"],
            },
        )

    monkeypatch.setattr("app.ev.vision.analyze_attachment", fake_analyze)
    upgraded = await reread_keep_identity_from_attachment(
        db_session,
        attachment_id,
        "memorize this",
        actor="owner",
        prompt=KEEP_REREAD_PROMPT,
    )
    await db_session.commit()
    assert captured.get("prompt") == KEEP_REREAD_PROMPT
    assert upgraded and upgraded.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_label_stub_does_not_replace_first_look_identity(
    db_session: AsyncSession,
) -> None:
    from uuid import uuid4

    attachment_id = str(uuid4())
    first = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["thermos"],
            "colors": ["silver", "black"],
            "media_kind": "frame",
            "spoken": (
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            "keep_request": "memorize this",
            "attachment_id": attachment_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert first and first.get("kept")
    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["silver"],
            "media_kind": "frame",
            "spoken": "I can see container, indoor.",
            "keep_request": "memorize this",
            "attachment_id": attachment_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_keep_adopts_first_look_speech_spoken_before_persist(
    db_session: AsyncSession,
) -> None:
    from uuid import uuid4

    from app.schemas import EventCreate
    from app.services.event_service import EventService

    attachment_id = str(uuid4())
    await EventService(db_session, actor="owner").create(
        EventCreate(
            source="live",
            event_type="message.assistant",
            text=(
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            device_id="mac-1",
        )
    )
    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["silver"],
            "media_kind": "frame",
            "spoken": "I can see container, indoor.",
            "keep_request": "memorize this",
            "attachment_id": attachment_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_first_look_speech_binds_to_keep_after_later_glance(
    db_session: AsyncSession,
) -> None:
    from uuid import uuid4

    from app.memory.visual import remember_spoken_scene

    keep_id = str(uuid4())
    later_id = str(uuid4())
    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["silver"],
            "media_kind": "frame",
            "spoken": "I can see container, indoor.",
            "keep_request": "memorize this",
            "attachment_id": keep_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    glance = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["indoor"],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": "A current camera image is attached. Describe what you actually see.",
            "attachment_id": later_id,
            "encoded_bytes": 90000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert glance is not None
    upgraded = await remember_spoken_scene(
        db_session,
        (
            "A stainless steel thermos with a black lid "
            "and a dent near the base."
        ),
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert upgraded is not None
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken
    assert "current camera image is attached" not in spoken


UNNAMED_KEEP_SCENE = (
    "Matte black finish, silver rim, a dent on the left edge, "
    "and tiny white lettering near the base."
)


@pytest.mark.asyncio
async def test_later_label_glance_does_not_erase_unnamed_identity(
    db_session: AsyncSession,
) -> None:
    from uuid import uuid4

    first_id = str(uuid4())
    first = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["indoor"],
            "colors": ["black", "silver"],
            "media_kind": "frame",
            "spoken": UNNAMED_KEEP_SCENE,
            "keep_request": "memorize this",
            "attachment_id": first_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert first and first.get("kept")
    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor", "shape"],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": "I can see container, indoor.",
            "keep_request": "memorize this",
            "attachment_id": first_id,
            "encoded_bytes": 90000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "dent" in spoken
    assert "lettering" in spoken or "silver rim" in spoken or "matte" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_thin_this_shown_does_not_supersede_stored_identity(
    db_session: AsyncSession,
) -> None:
    first = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["indoor"],
            "colors": ["black", "silver"],
            "media_kind": "frame",
            "spoken": UNNAMED_KEEP_SCENE,
            "keep_request": "memorize this",
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert first and first.get("kept")
    stub = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "shape"],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": "I can see container, shape.",
            "keep_request": "memorize this",
            "encoded_bytes": 80000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert stub and stub.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "dent" in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


def test_compact_keep_hits_drop_shape_labels() -> None:
    from app.voice.live.layer import compact_live_tool_json

    parsed = __import__("json").loads(
        compact_live_tool_json(
            {
                "ok": True,
                "name": "recall",
                "spoken": UNNAMED_KEEP_SCENE,
                "result": {
                    "count": 2,
                    "grounding": "evidence",
                    "spoken": UNNAMED_KEEP_SCENE,
                    "lines": [
                        UNNAMED_KEEP_SCENE,
                        "You asked me to remember a container-shaped thing.",
                    ],
                },
            }
        )
    )
    hits = " ".join(parsed["result"]["hits"]).lower()
    spoken = str(parsed["result"]["spoken"] or "").lower()
    assert "dent" in spoken
    assert "container-shaped" not in hits
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_live_keep_returns_jpeg_without_spark_and_binds_first_look(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.ev import look as look_mod
    from app.ev.camera_runtime import LookFrame, reset_pending_observations
    from app.ev.look import look_now
    from app.memory.visual import remember_spoken_scene

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 120 + b"\xff\xd9"

    async def fake_wait(**kwargs):
        frame = LookFrame(
            request_id="owner-keep",
            jpeg=jpeg,
            width=1280,
            height=720,
            labels=["container", "indoor"],
            colors=["silver"],
            last=True,
        )
        return SimpleNamespace(), frame

    async def boom(*args, **kwargs):
        raise AssertionError("live keep must not wait on Spark")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", fake_wait)
    monkeypatch.setattr("app.ev.vision.analyze_attachment", boom)
    scheduled: list[dict] = []

    def fake_schedule(_session, *, attachment_id, keep_request, actor):
        scheduled.append(
            {
                "attachment_id": attachment_id,
                "keep_request": keep_request,
                "actor": actor,
            }
        )

    monkeypatch.setattr(
        "app.memory.visual._schedule_keep_reread_after_commit", fake_schedule
    )
    result = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="mini-look-1",
    )
    await db_session.commit()
    spoken = str(result.get("spoken") or "").lower()
    assert result.get("ok") is True
    assert result.get("kept") is True
    assert result.get("attachment_id")
    assert scheduled
    assert scheduled[0].get("attachment_id") == result.get("attachment_id")
    assert spoken.startswith("a current camera image is attached")
    assert "container-shaped" not in spoken

    upgraded = await remember_spoken_scene(
        db_session,
        "A stainless steel thermos with a black lid and a dent near the base.",
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert upgraded and upgraded.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    recalled = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in recalled
    assert "dent" in recalled or "lid" in recalled
    assert "container-shaped" not in recalled
    assert "you asked me to remember" not in recalled
    assert "camera image is attached" not in recalled


@pytest.mark.asyncio
async def test_classifier_phone_label_does_not_skip_first_look_identity(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.ev import look as look_mod
    from app.ev.camera_runtime import LookFrame, reset_pending_observations
    from app.ev.look import look_now
    from app.memory.visual import remember_spoken_scene

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 120 + b"\xff\xd9"

    async def fake_wait(**kwargs):
        frame = LookFrame(
            request_id="owner-keep",
            jpeg=jpeg,
            width=1280,
            height=720,
            labels=["phone", "electronics", "indoor"],
            colors=["black"],
            last=True,
        )
        return SimpleNamespace(), frame

    async def boom(*args, **kwargs):
        raise AssertionError("live keep must not wait on Spark")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", fake_wait)
    monkeypatch.setattr("app.ev.vision.analyze_attachment", boom)
    result = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep-phone",
        device_id="mac-1",
        request_id="mini-look-phone",
    )
    await db_session.commit()
    spoken = str(result.get("spoken") or "").lower()
    assert result.get("ok") is True
    assert result.get("kept") is True
    assert spoken.startswith("a current camera image is attached")
    assert "that's a phone" not in spoken
    assert "iphone" not in spoken

    upgraded = await remember_spoken_scene(
        db_session,
        (
            "A black iPhone 16 Pro with a titanium frame, camera island, "
            "and a small scratch near the mute switch."
        ),
        actor="owner",
        device_id="mac-1",
    )
    await db_session.commit()
    assert upgraded and upgraded.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    recalled = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "iphone" in recalled
    assert "titanium" in recalled or "scratch" in recalled or "camera island" in recalled
    assert "you asked me to remember" not in recalled
    assert "container-shaped" not in recalled
    assert "camera image is attached" not in recalled


@pytest.mark.asyncio
async def test_mini_keep_look_reuses_broker_jpeg(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev import look as look_mod
    from app.ev.camera_runtime import (
        CameraObservation,
        peek_observations,
        pop_observations,
        reset_pending_observations,
        stash_observation,
    )
    from app.ev.look import KEEP_HOLD_CALL_ID, look_now

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 120 + b"\xff\xd9"
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

    async def boom(**kwargs):
        raise AssertionError("mini keep look must reuse the already captured JPEG")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", boom)
    result = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="mini-look-2",
    )
    await db_session.commit()
    assert result.get("ok") is True
    assert result.get("kept") is True
    stashed = pop_observations("mini-look-2")
    assert stashed and stashed[0].jpeg == jpeg
    held = peek_observations("owner-keep") or peek_observations(KEEP_HOLD_CALL_ID)
    assert held and held[0].jpeg == jpeg


@pytest.mark.asyncio
async def test_mini_keep_look_reuses_hold_after_inject_pops_owner_keep(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev import look as look_mod
    from app.ev.camera_runtime import (
        CameraObservation,
        pop_observations,
        reset_pending_observations,
        stash_observation,
    )
    from app.ev.look import KEEP_HOLD_CALL_ID, look_now

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 120 + b"\xff\xd9"
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
    assert pop_observations("owner-keep")

    async def boom(**kwargs):
        raise AssertionError("mini keep look must reuse the hold JPEG after inject")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", boom)
    result = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="mini-look-hold",
    )
    await db_session.commit()
    assert result.get("ok") is True
    assert result.get("kept") is True
    stashed = pop_observations("mini-look-hold")
    assert stashed and stashed[0].jpeg == jpeg


@pytest.mark.asyncio
async def test_mini_keep_look_binds_existing_keep_attachment(
    db_session: AsyncSession,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev import look as look_mod
    from app.ev.camera_runtime import (
        CameraObservation,
        reset_pending_observations,
        stash_observation,
    )
    from app.ev.look import look_now

    jpeg = b"\xff\xd8" + b"\x00" * 120 + b"\xff\xd9"
    resp = await client.post(
        "/v1/attachments",
        files={"file": ("keep.jpg", jpeg, "image/jpeg")},
        data={"privacy_level": "normal", "event_type": "camera.look"},
    )
    assert resp.status_code == 201, resp.text
    attachment_id = resp.json()["attachment"]["id"]
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container"],
            "media_kind": "frame",
            "spoken": "",
            "keep_request": "memorize this",
            "attachment_id": attachment_id,
            "encoded_bytes": len(jpeg),
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert written and written.get("kept")
    reset_pending_observations()
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

    async def boom(**kwargs):
        raise AssertionError("mini keep look must reuse the already captured JPEG")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", boom)
    result = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="mini-look-bind",
    )
    await db_session.commit()
    assert result.get("ok") is True
    assert result.get("attachment_id") == attachment_id


@pytest.mark.asyncio
async def test_mini_keep_look_reloads_stored_jpeg_when_hold_is_empty(
    db_session: AsyncSession,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev import look as look_mod
    from app.ev.camera_runtime import reset_pending_observations
    from app.ev.look import look_now

    jpeg = b"\xff\xd8" + b"\x00" * 120 + b"\xff\xd9"
    resp = await client.post(
        "/v1/attachments",
        files={"file": ("keep.jpg", jpeg, "image/jpeg")},
        data={"privacy_level": "normal", "event_type": "camera.look"},
    )
    assert resp.status_code == 201, resp.text
    attachment_id = resp.json()["attachment"]["id"]
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["mug"],
            "media_kind": "frame",
            "spoken": "",
            "keep_request": "memorize this",
            "attachment_id": attachment_id,
            "encoded_bytes": len(jpeg),
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert written and written.get("kept")
    reset_pending_observations()

    async def boom(**kwargs):
        raise AssertionError("mini keep look must reload the stored JPEG, not recapture")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", boom)
    result = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="mini-look-reload",
    )
    await db_session.commit()
    assert result.get("ok") is True
    assert result.get("kept") is True
    assert result.get("attachment_id") == attachment_id


@pytest.mark.asyncio
async def test_concurrent_keep_looks_share_one_jpeg(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from types import SimpleNamespace

    from app.ev import look as look_mod
    from app.ev.camera_runtime import LookFrame, reset_pending_observations
    from app.ev.look import look_now

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 120 + b"\xff\xd9"
    captures = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_wait(**kwargs):
        captures["n"] += 1
        started.set()
        await release.wait()
        return SimpleNamespace(), LookFrame(
            request_id=str(kwargs.get("request_id") or "cap"),
            jpeg=jpeg,
            width=1280,
            height=720,
            last=True,
        )

    async def boom(*_args, **_kwargs):
        raise AssertionError("live keep must not wait on Spark")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", slow_wait)
    monkeypatch.setattr("app.ev.vision.analyze_attachment", boom)

    async def broker():
        return await look_now(
            db_session,
            actor="owner",
            prompt="memorize this",
            live_session_id="talk-keep",
            device_id="mac-1",
            request_id="broker-keep",
        )

    async def mini():
        await started.wait()
        return await look_now(
            db_session,
            actor="owner",
            prompt="memorize this",
            live_session_id="talk-keep",
            device_id="mac-1",
            request_id="mini-keep",
        )

    first = asyncio.create_task(broker())
    second = asyncio.create_task(mini())
    await started.wait()
    await asyncio.sleep(0.05)
    assert captures["n"] == 1
    release.set()
    broker_result, mini_result = await asyncio.gather(first, second)
    await db_session.commit()
    assert captures["n"] == 1
    assert broker_result.get("kept") is True
    assert mini_result.get("kept") is True
    assert broker_result.get("attachment_id")
    assert mini_result.get("attachment_id") == broker_result.get("attachment_id")


@pytest.mark.asyncio
async def test_owner_keep_look_reuses_mini_jpeg_instead_of_second_capture(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.ev import look as look_mod
    from app.ev.camera_runtime import LookFrame, reset_pending_observations
    from app.ev.look import look_now

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 120 + b"\xff\xd9"
    captures = {"n": 0}

    async def one_wait(**kwargs):
        captures["n"] += 1
        return SimpleNamespace(), LookFrame(
            request_id=str(kwargs.get("request_id") or "cap"),
            jpeg=jpeg,
            width=1280,
            height=720,
            last=True,
        )

    async def boom(*_args, **_kwargs):
        raise AssertionError("live keep must not wait on Spark")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", one_wait)
    monkeypatch.setattr("app.ev.vision.analyze_attachment", boom)

    mini = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="call_mini_first",
    )
    broker = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="owner-keep",
    )
    await db_session.commit()
    assert captures["n"] == 1
    assert mini.get("kept") is True
    assert broker.get("kept") is True
    assert mini.get("attachment_id")
    assert broker.get("attachment_id") == mini.get("attachment_id")


@pytest.mark.asyncio
async def test_reused_keep_bytes_do_not_bind_previous_object(
    db_session: AsyncSession,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev import look as look_mod
    from app.ev.camera_runtime import (
        CameraObservation,
        reset_pending_observations,
        stash_observation,
    )
    from app.ev.look import KEEP_HOLD_CALL_ID, look_now

    jpeg_old = b"\xff\xd8" + b"\x01" * 120 + b"\xff\xd9"
    jpeg_new = b"\xff\xd8" + b"\x02" * 120 + b"\xff\xd9"
    resp = await client.post(
        "/v1/attachments",
        files={"file": ("keep-old.jpg", jpeg_old, "image/jpeg")},
        data={"privacy_level": "normal", "event_type": "camera.look"},
    )
    assert resp.status_code == 201, resp.text
    older_id = resp.json()["attachment"]["id"]
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["thermos"],
            "media_kind": "frame",
            "spoken": (
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            "keep_request": "memorize this",
            "attachment_id": older_id,
            "encoded_bytes": len(jpeg_old),
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert written and written.get("kept")
    reset_pending_observations()
    stash_observation(
        CameraObservation(
            request_id="owner-keep",
            call_id=KEEP_HOLD_CALL_ID,
            jpeg=jpeg_new,
            width=1280,
            height=720,
            detail="high",
        )
    )

    async def boom(**kwargs):
        raise AssertionError("keep look must reuse the hold JPEG")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", boom)
    result = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="mini-look-new-object",
    )
    await db_session.commit()
    assert result.get("ok") is True
    assert result.get("kept") is True
    assert result.get("attachment_id")
    assert result.get("attachment_id") != older_id


@pytest.mark.asyncio
async def test_mini_keep_look_binds_hold_attachment_not_older(
    db_session: AsyncSession,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev import look as look_mod
    from app.ev.camera_runtime import (
        CameraObservation,
        peek_observations,
        reset_pending_observations,
        stash_observation,
    )
    from app.ev.look import KEEP_HOLD_CALL_ID, look_now

    jpeg_a = b"\xff\xd8" + b"\x01" * 120 + b"\xff\xd9"
    jpeg_b = b"\xff\xd8" + b"\x02" * 120 + b"\xff\xd9"
    resp_a = await client.post(
        "/v1/attachments",
        files={"file": ("keep-a.jpg", jpeg_a, "image/jpeg")},
        data={"privacy_level": "normal", "event_type": "camera.look"},
    )
    resp_b = await client.post(
        "/v1/attachments",
        files={"file": ("keep-b.jpg", jpeg_b, "image/jpeg")},
        data={"privacy_level": "normal", "event_type": "camera.look"},
    )
    assert resp_a.status_code == 201, resp_a.text
    assert resp_b.status_code == 201, resp_b.text
    older_id = resp_a.json()["attachment"]["id"]
    newer_id = resp_b.json()["attachment"]["id"]
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["thermos"],
            "media_kind": "frame",
            "spoken": (
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            "keep_request": "memorize this",
            "attachment_id": older_id,
            "encoded_bytes": len(jpeg_a),
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert written and written.get("kept")
    reset_pending_observations()
    stash_observation(
        CameraObservation(
            request_id=newer_id,
            call_id=KEEP_HOLD_CALL_ID,
            jpeg=jpeg_b,
            width=1280,
            height=720,
            detail="high",
        )
    )

    async def boom(**kwargs):
        raise AssertionError("mini keep look must reuse the hold JPEG")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", boom)
    result = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="mini-look-newer",
    )
    await db_session.commit()
    assert result.get("ok") is True
    assert result.get("attachment_id") == newer_id
    held = peek_observations(KEEP_HOLD_CALL_ID)
    assert held
    from app.memory.visual import _attachment_uuid

    assert _attachment_uuid(held[0].request_id) == newer_id


@pytest.mark.asyncio
async def test_first_look_identity_survives_thinner_spark_reread(
    db_session: AsyncSession,
) -> None:
    from uuid import uuid4

    keep_id = str(uuid4())
    first = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": ["black", "silver"],
            "media_kind": "frame",
            "spoken": (
                "A stainless steel thermos with a black lid "
                "and a dent near the base."
            ),
            "keep_request": "memorize this",
            "attachment_id": keep_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert first and first.get("kept")
    later = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["electronics", "product"],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": "A black smartphone with a screen.",
            "keep_request": "memorize this",
            "attachment_id": keep_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert later is not None
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert latest.get("grounding") == "evidence"
    assert "thermos" in spoken
    assert "dent" in spoken or "lid" in spoken
    assert "smartphone" not in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_concise_first_look_survives_longer_spark_reread(
    db_session: AsyncSession,
) -> None:
    from uuid import uuid4

    keep_id = str(uuid4())
    first_line = "A black iPhone 16 Pro with a titanium camera island."
    first = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["phone", "electronics"],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": first_line,
            "keep_request": "memorize this",
            "attachment_id": keep_id,
            "encoded_bytes": 120000,
            "image_ready": True,
            "identity_source": "live",
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert first and first.get("kept")
    later = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["smartphone", "container"],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": (
                "A black handheld smartphone with a large glossy screen, "
                "rounded corners, and a triple-lens housing on the back panel."
            ),
            "keep_request": "memorize this",
            "attachment_id": keep_id,
            "encoded_bytes": 120000,
            "image_ready": True,
            "identity_source": "reread",
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert later is not None
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "iphone" in spoken
    assert "titanium" in spoken or "island" in spoken
    assert "smartphone" not in spoken
    assert "container-shaped" not in spoken
    assert "you asked me to remember" not in spoken


@pytest.mark.asyncio
async def test_live_first_look_replaces_earlier_spark_reread(
    db_session: AsyncSession,
) -> None:
    from uuid import uuid4

    keep_id = str(uuid4())
    await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["smartphone"],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": (
                "A black handheld smartphone with a large glossy screen "
                "and a triple-lens housing on the back panel."
            ),
            "keep_request": "memorize this",
            "attachment_id": keep_id,
            "encoded_bytes": 120000,
            "image_ready": True,
            "identity_source": "reread",
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    live = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["phone"],
            "colors": ["black"],
            "media_kind": "frame",
            "spoken": "A black iPhone 16 Pro with a titanium camera island.",
            "keep_request": "memorize this",
            "attachment_id": keep_id,
            "encoded_bytes": 120000,
            "image_ready": True,
            "identity_source": "live",
        },
        actor="owner",
        device_id="mac-1",
        adopt_spoken=False,
    )
    await db_session.commit()
    assert live and live.get("kept")
    latest = await build_explicit_recall_payload(
        db_session, "What did I just ask you to remember?", k=6
    )
    spoken = str(latest.get("spoken") or "").lower()
    assert "iphone" in spoken
    assert "titanium" in spoken or "island" in spoken
    assert "smartphone" not in spoken
    assert "you asked me to remember" not in spoken


def test_keep_reread_is_skipped_inside_pytest() -> None:
    from app.memory.visual import _KEEP_REREAD_IN_FLIGHT, schedule_keep_identity_reread

    before = set(_KEEP_REREAD_IN_FLIGHT)
    schedule_keep_identity_reread(
        "11111111-1111-1111-1111-111111111111",
        "memorize this",
        actor="owner",
    )
    assert before == _KEEP_REREAD_IN_FLIGHT


def test_keep_reread_look_target_uses_committed_jpeg() -> None:
    from app.memory.visual import keep_reread_look_target

    aid = "11111111-1111-1111-1111-111111111111"
    assert keep_reread_look_target({"attachment_id": aid}) is None
    target = keep_reread_look_target(
        {
            "attachment_id": aid,
            "kept": True,
            "keep_request": "memorize this",
        }
    )
    assert target == (aid, "memorize this")
    assert keep_reread_look_target(
        {"attachment_id": aid, "keep_request": "what is this"}
    ) is None
    from app.ev.look import KEEP_LOOK_PROMPT

    target = keep_reread_look_target(
        {"attachment_id": aid, "kept": True},
        arguments={"prompt": KEEP_LOOK_PROMPT},
        transcript="memorize this",
    )
    assert target == (aid, "memorize this")
    target = keep_reread_look_target(
        {
            "attachment_id": aid,
            "kept": True,
            "keep_request": KEEP_LOOK_PROMPT,
        }
    )
    assert target == (aid, "memorize this")


@pytest.mark.asyncio
async def test_thin_keep_persist_kicks_reread_after_commit(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    called: list[tuple[str, str]] = []

    def fake_schedule(attachment_id, keep_request, *, actor="owner", loop=None):
        called.append((str(attachment_id), str(keep_request)))

    monkeypatch.setattr(
        "app.memory.visual.schedule_keep_identity_reread", fake_schedule
    )
    attachment_id = str(uuid4())
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["container", "indoor"],
            "colors": [],
            "media_kind": "frame",
            "spoken": "",
            "keep_request": "memorize this",
            "attachment_id": attachment_id,
            "encoded_bytes": 120000,
            "image_ready": True,
        },
        actor="owner",
        device_id="mac-1",
    )
    assert written and written.get("kept")
    assert called == []
    await db_session.commit()
    assert called
    assert called[0][0] == attachment_id
    assert "memorize" in called[0][1].lower()


@pytest.mark.asyncio
async def test_await_keep_reread_waits_for_in_flight() -> None:
    import asyncio

    from app.memory.visual import _KEEP_REREAD_IN_FLIGHT, _await_keep_reread

    needle = "22222222-2222-2222-2222-222222222222"
    _KEEP_REREAD_IN_FLIGHT.add(needle)

    async def clear() -> None:
        await asyncio.sleep(0.05)
        _KEEP_REREAD_IN_FLIGHT.discard(needle)

    task = asyncio.create_task(clear())
    try:
        await _await_keep_reread(needle, timeout=2.0)
        assert needle not in _KEEP_REREAD_IN_FLIGHT
    finally:
        _KEEP_REREAD_IN_FLIGHT.discard(needle)
        await task


def test_spark_keep_payload_sends_the_jpeg_pixels() -> None:
    from app.contracts import ChatMessage, MediaPart
    from app.ev.look import KEEP_LOOK_PROMPT
    from app.ev.vision import _perception_system_prompt
    from app.gateway.muse_spark import MuseSparkProvider, _payload_has_input_image

    provider = MuseSparkProvider(
        base_url="https://opencode.ai/zen/go/v1", api_key="test-key"
    )
    payload = provider._payload(
        [
            ChatMessage(
                role="system",
                content=_perception_system_prompt(KEEP_LOOK_PROMPT),
            ),
            ChatMessage(
                role="user",
                content=KEEP_LOOK_PROMPT,
                media=[
                    MediaPart(
                        kind="image",
                        content_type="image/jpeg",
                        data_url="data:image/jpeg;base64,xx",
                    )
                ],
            ),
        ],
        model=None,
        tools=None,
        stream=False,
    )
    assert _payload_has_input_image(payload)
    blob = str(payload)
    assert "input_image" in blob
    assert "data:image/jpeg;base64,xx" in blob
    assert "LABEL: name" not in _perception_system_prompt(KEEP_LOOK_PROMPT)


def test_keep_reread_timeout_outlasts_jpeg_http_read() -> None:
    from app.config import settings
    from app.ev.look import keep_reread_timeout_seconds
    from app.gateway.muse_spark import _spark_timeout

    text_timeout = _spark_timeout(
        {"input": [{"content": [{"type": "input_text", "text": "hi"}]}]}
    )
    image_timeout = _spark_timeout(
        {
            "input": [
                {
                    "content": [
                        {"type": "input_image", "image_url": "data:image/jpeg;base64,xx"}
                    ]
                }
            ]
        }
    )
    budget = keep_reread_timeout_seconds()
    assert budget > settings.model_read_timeout_seconds
    assert float(image_timeout.read or 0) > float(text_timeout.read or 0)
    assert budget > float(image_timeout.read or 0)


def test_keep_reread_prompt_forbids_label_lines() -> None:
    from app.ev.look import KEEP_REREAD_PROMPT
    from app.ev.vision import _perception_system_prompt
    from app.memory.visual import is_camera_prompt_echo

    blob = KEEP_REREAD_PROMPT.lower()
    assert "do not output label" in blob
    assert "concrete noun" in blob
    assert is_camera_prompt_echo(KEEP_REREAD_PROMPT)
    text = _perception_system_prompt(KEEP_REREAD_PROMPT).lower()
    assert "do not output label:" in text
    assert "suggested labels" not in text


def test_keep_perception_prompt_names_the_thing_not_label_lines() -> None:
    from app.ev.look import KEEP_LOOK_PROMPT
    from app.ev.vision import (
        _extract_summary,
        _perception_system_prompt,
        _usable_perception_labels,
    )

    text = _perception_system_prompt(KEEP_LOOK_PROMPT).lower()
    assert "label: name" not in text
    assert "suggested labels" not in text
    assert "concrete noun" in text
    assert "do not output label:" in text
    assert _usable_perception_labels(
        [
            {"label": "container", "confidence": 0.9},
            {"label": "indoor", "confidence": 0.8},
        ],
        raw_sent=True,
    ) == []
    assert _usable_perception_labels(
        [
            {"label": "container", "confidence": 0.9},
            {"label": "thermos", "confidence": 0.8},
        ],
        raw_sent=True,
    ) == [{"label": "thermos", "confidence": 0.8}]
    stub = _extract_summary("LABEL: container 0.91\nLABEL: indoor 0.80")
    assert "container" not in stub.lower()
    assert is_empty_visual_scene(stub)
    assert not is_keep_identity_speech(stub)


def test_perception_stub_is_not_a_reusable_identity() -> None:
    from app.memory.visual import looks_like_visual_description

    stub = "Perception completed."
    assert is_empty_visual_scene(stub)
    assert is_generic_label_scene(stub) or is_empty_visual_scene(stub)
    assert not looks_like_visual_description(stub)
    assert not is_keep_identity_speech(stub)
    spoken = keep_owner_spoken(
        scene=stub,
        labels=["container", "indoor"],
        keep_request="memorize this",
        frame_ok=True,
    ).lower()
    assert "container" not in spoken
    assert "perception" not in spoken


def test_printed_text_is_identity_even_without_a_named_object() -> None:
    identity = extract_visual_identity(
        scene="",
        ocr="SERIAL 4K2",
        labels=["container", "indoor"],
        keep_request="memorize this",
    )
    assert identity["usable"] is True
    blob = f"{identity['recall']} {identity['spoken']}".lower()
    assert "4k2" in blob or "serial" in blob
    assert "container" not in blob
    spoken = keep_owner_spoken(
        scene="",
        ocr="SERIAL 4K2",
        labels=["container"],
        keep_request="memorize this",
        frame_ok=True,
    ).lower()
    assert "serial" in spoken or "4k2" in spoken
    assert "container" not in spoken


def test_look_instructions_are_not_keep_identity() -> None:
    from app.ev.camera_runtime import camera_image_prompt
    from app.ev.look import KEEP_LOOK_PROMPT
    from app.memory.visual import is_camera_prompt_echo

    assert is_camera_prompt_echo(KEEP_LOOK_PROMPT)
    assert is_camera_prompt_echo(camera_image_prompt("look"))
    assert not is_keep_identity_speech(KEEP_LOOK_PROMPT)
    assert not is_keep_identity_speech(
        "Start with a, an, or the plus the specific thing they are showing."
    )
    assert is_keep_identity_speech(
        "A matte black handset with a silver rim and a dent on the left edge."
    )


def test_spoken_evidence_prefers_first_look_over_class_stub() -> None:
    spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "fact",
                "reason": "visual_keep",
                "object": "phone",
                "text": "You asked me to remember a phone.",
            },
            {
                "memory_type": "observation",
                "reason": "visual_observation",
                "text": (
                    "A matte black handset with a silver rim, a dent on the left edge, "
                    "and tiny white lettering near the base."
                ),
            },
        ],
        "What did I just ask you to remember?",
    ).lower()
    assert "dent" in spoken or "lettering" in spoken or "matte black" in spoken
    assert "you asked me to remember a phone" not in spoken


@pytest.mark.asyncio
async def test_label_only_jpeg_reread_does_not_store_a_shape(
    db_session: AsyncSession,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.memory.visual import _enrich_keep_from_attachment

    resp = await client.post(
        "/v1/attachments",
        files={"file": ("keep.png", b"KEEP-FRAME", "image/png")},
        data={"privacy_level": "normal", "event_type": "camera.look"},
    )
    assert resp.status_code == 201, resp.text
    attachment_id = resp.json()["attachment"]["id"]

    async def fake_analyze(session, attachment_id, **kwargs):
        return SimpleNamespace(
            id="perc-keep-labels",
            payload={
                "summary": "Perception completed.",
                "labels": [
                    {"label": "container", "confidence": 0.9},
                    {"label": "indoor", "confidence": 0.8},
                ],
                "ocr_text": "",
                "raw_sent": True,
                "colors": [],
            },
        )

    monkeypatch.setattr("app.ev.vision.analyze_attachment", fake_analyze)
    enriched = await _enrich_keep_from_attachment(
        db_session,
        {
            "attachment_id": attachment_id,
            "keep_request": "memorize this",
            "description": "",
            "recall": "You asked me to remember what you showed.",
        },
        actor="owner",
    )
    assert enriched is None


@pytest.mark.asyncio
async def test_keep_look_waits_for_live_session_then_stores_jpeg(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.ev.camera_runtime import LookFrame, reset_pending_observations
    from app.ev.look import look_now

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 120 + b"\xff\xd9"
    holder: dict[str, object] = {"live": None}

    class _LateLive:
        async def request_look_frame(self, **_kwargs):
            return LookFrame(
                request_id="late-keep",
                jpeg=jpeg,
                width=1280,
                height=720,
                last=True,
            )

    def fake_session(_sid):
        return holder["live"]

    def fake_device(_did):
        return holder["live"]

    monkeypatch.setattr("app.voice.live.layer.live_for_session", fake_session)
    monkeypatch.setattr("app.voice.live.layer.live_for_device", fake_device)

    async def boom(*_args, **_kwargs):
        raise AssertionError("live keep must not wait on Spark")

    monkeypatch.setattr("app.ev.vision.analyze_attachment", boom)

    async def appear():
        await asyncio.sleep(0.25)
        holder["live"] = _LateLive()

    asyncio.create_task(appear())
    result = await look_now(
        db_session,
        actor="owner",
        prompt="memorize this",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="owner-keep",
    )
    await db_session.commit()
    assert result.get("ok") is True
    assert result.get("kept") is True
    assert result.get("attachment_id")
    assert int(result.get("encoded_bytes") or 0) > 0


@pytest.mark.asyncio
async def test_owner_keep_call_id_stores_jpeg_even_without_prompt(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.ev import look as look_mod
    from app.ev.camera_runtime import LookFrame, reset_pending_observations
    from app.ev.look import look_now

    reset_pending_observations()
    jpeg = b"\xff\xd8" + b"\x00" * 120 + b"\xff\xd9"

    async def fake_wait(**kwargs):
        return SimpleNamespace(), LookFrame(
            request_id="owner-keep",
            jpeg=jpeg,
            width=1280,
            height=720,
            last=True,
        )

    async def boom(*_args, **_kwargs):
        raise AssertionError("owner-keep must store JPEG without Spark")

    monkeypatch.setattr(look_mod, "_wait_for_live_frame", fake_wait)
    monkeypatch.setattr("app.ev.vision.analyze_attachment", boom)
    result = await look_now(
        db_session,
        actor="owner",
        prompt="",
        live_session_id="talk-keep",
        device_id="mac-1",
        request_id="owner-keep",
    )
    await db_session.commit()
    assert result.get("ok") is True
    assert result.get("kept") is True
    assert result.get("attachment_id")
    assert int(result.get("encoded_bytes") or 0) > 0

