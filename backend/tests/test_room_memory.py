"""Room memory: look → last-seen object, not people or coding files."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.recall import build_explicit_recall_payload
from app.memory.room import (
    extract_placement,
    looks_like_object_locate,
    object_locate_noun,
    spoken_object_locate,
)
from app.memory.visual import persist_visual_observation


def test_leave_object_intent_is_not_a_person_hunt() -> None:
    assert looks_like_object_locate("where did I leave my charger")
    assert looks_like_object_locate("where's my wallet")
    assert object_locate_noun("where did I leave my charger") == "charger"
    assert not looks_like_object_locate("where is Rahul")
    assert not looks_like_object_locate("where's my dad")
    assert not looks_like_object_locate("where is the file saved")
    assert not looks_like_object_locate("find my backpack tag")


def test_placement_extracts_surface_from_a_look() -> None:
    hit = extract_placement(
        scene="A black charger on the kitchen table next to a mug.",
        labels=["charger"],
        named=None,
    )
    assert "charger" in " ".join(hit["objects"]).lower()
    assert hit["surface"] in {"kitchen table", "kitchen", "table"}
    assert "charger" in str(hit["phrase"] or "").lower()


def test_spoken_locate_is_honest_when_empty() -> None:
    spoken = spoken_object_locate("where did I leave my keys", None)
    assert "keys" in spoken.lower()
    assert "haven't seen" in spoken.lower() or "not" in spoken.lower()


@pytest.mark.asyncio
async def test_look_then_where_did_i_leave_it(db_session: AsyncSession) -> None:
    written = await persist_visual_observation(
        db_session,
        {
            "ok": True,
            "labels": ["charger"],
            "colors": ["black"],
            "person_count": 0,
            "media_kind": "frame",
            "spoken": "A black charger on the kitchen table.",
            "request_id": "leave-x-1",
        },
        actor="owner",
        device_id="phone-1",
    )
    await db_session.commit()
    assert written is not None
    pack = await build_explicit_recall_payload(
        db_session, "where did I leave my charger", k=6
    )
    spoken = str(pack.get("spoken") or "").lower()
    assert "charger" in spoken
    assert "haven't seen" not in spoken
    assert "cannot find that particular record" not in spoken
    assert any(token in spoken for token in ("table", "kitchen", "last time", "last seen"))
