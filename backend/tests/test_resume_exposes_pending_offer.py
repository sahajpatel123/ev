"""A conversation resumed on another device must carry the offer Evie made.

Owner-observed failure class: the Mac hears "do you want me to read out the
full mail?", the owner picks up the phone and says "yes" — and the phone has no
referent, because ``resume_context`` only exposed ``ConversationState
.pending_questions`` (a durable ask queue) and never the question Evie had just
spoken. The payload is client-visible, so the fix is additive: a new
``pending_offer`` key, omitted when there is nothing live to answer.
"""

from __future__ import annotations

import pytest

from app.cognitive import intent, session_store
from app.everywhere.continuity import resume_context

OFFER = "I found the March invoice — do you want me to read out the full mail?"

# The payload contract as it stood before the additive key. Every one of these
# fields is read by clients, so a rename or repurpose is a break, not a fix.
BASELINE_KEYS = frozenset(
    {
        "ok",
        "thread_id",
        "last_activity",
        "requested_from_device",
        "focus",
        "recent_topics",
        "pending_questions",
        "rollup",
        "situation_refs",
        "resume_hint",
        "generated_at",
    }
)


async def _resume(db_session):
    return await resume_context(
        db_session, actor="device:Primary iPhone", device_name="Primary iPhone"
    )


async def test_resume_payload_carries_the_live_offer(db_session):
    intent.set_pending_offer(
        session_store.current(),
        OFFER,
        action={"tool": "mail_read", "args": {"rowid": 42}},
    )
    # The offer was armed by another process (the Mac edge); the resuming
    # device must see it from the shared session file, not an in-process cache.
    session_store.forget_live_cache()

    out = await _resume(db_session)

    assert out["ok"] is True
    assert set(out) == BASELINE_KEYS | {"pending_offer"}
    offer = out["pending_offer"]
    assert offer["text"] == OFFER
    assert offer["action"] == {"tool": "mail_read", "args": {"rowid": 42}}
    # The offer is to speak an artifact through, so the readout hint rides too.
    assert offer["readout"] is True


async def test_resume_payload_is_otherwise_unchanged_without_an_offer(db_session):
    out = await _resume(db_session)

    assert set(out) == BASELINE_KEYS
    assert out["pending_questions"] == []
    assert out["focus"] is None
    assert out["recent_topics"] == []
    assert out["rollup"] == {
        "summary": "",
        "open_questions": [],
        "decisions": [],
        "covered_turn_count": 0,
    }
    assert out["situation_refs"]["top_project"] is None
    assert out["resume_hint"] == "No active context found."


async def test_unreadable_session_omits_the_offer_without_raising(
    db_session, monkeypatch: pytest.MonkeyPatch
):
    def _boom():
        raise OSError("storage root is not readable")

    monkeypatch.setattr(session_store, "current", _boom)

    out = await _resume(db_session)

    assert out["ok"] is True
    assert "pending_offer" not in out
    assert set(out) == BASELINE_KEYS


async def test_http_resume_response_exposes_the_offer(client):
    """The payload is client-visible: the resuming device reads the endpoint."""

    intent.set_pending_offer(
        session_store.current(),
        OFFER,
        action={"tool": "mail_read", "args": {"rowid": 42}},
    )
    session_store.forget_live_cache()

    response = await client.get("/v1/everywhere/conversation/resume_context")

    assert response.status_code == 200
    body = response.json()
    assert body["pending_offer"]["text"] == OFFER
    assert body["pending_offer"]["action"]["tool"] == "mail_read"
    assert body["pending_offer"]["readout"] is True
