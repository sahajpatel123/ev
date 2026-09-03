"""Generic visual identity for memorize/recall — not a book/remote special case."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.recall import _spoken_from_evidence, build_explicit_recall_payload
from app.memory.visual import (
    extract_visual_identity,
    keep_owner_spoken,
    keep_sight_text,
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
