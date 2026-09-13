"""Proactive speech must wait for a live owner turn, and still land afterwards.

The failure this guards: Evie offers to read a mail out, the owner answers
"yes", and a parked callout talks over that answer — leaving the owner's reply
answering a question the model can no longer see.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Callout
from app.voice.live.events import ReplyEvent, TtsChunkEvent
from app.voice.live.layer import (
    LIVE_MAIL_KEY,
    LIVE_MAIL_SOURCE,
    deliver_pending_live_mail,
    register_live,
    reset_live_registry,
    speak_on_live,
)
from app.voice.live.session import LiveSession


def _drain(session: LiveSession) -> list:
    items = []
    while not session.outbound.empty():
        items.append(session.outbound.get_nowait())
    return items


def _spoken(session: LiveSession) -> list[str]:
    return [
        str(event.text or "")
        for event in _drain(session)
        if isinstance(event, (ReplyEvent, TtsChunkEvent))
    ]


@pytest.fixture(autouse=True)
def _isolated_cognition(tmp_path, monkeypatch):
    from app.cognitive.session_store import reset_for_tests
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()
    yield
    reset_for_tests()


def _arm_read_aloud_offer() -> str:
    from app.cognitive.intent import set_pending_offer
    from app.cognitive.session_store import current

    offer = "Do you want me to read out the full mail?"
    set_pending_offer(current(), offer)
    return offer


async def _parked_row(session, text: str) -> Callout | None:
    return (
        await session.execute(
            select(Callout).where(
                Callout.source == LIVE_MAIL_SOURCE, Callout.text == text
            )
        )
    ).scalar_one_or_none()


async def test_proactive_line_parks_while_offer_awaits_the_answer(db_session) -> None:
    reset_live_registry()
    _arm_read_aloud_offer()
    text = "Deferred callout: the print job finished."
    live = LiveSession(session_id="defer-1", device_id="mac-defer", backchannel_enabled=False)
    register_live(live)

    delivered = await speak_on_live(text, device_id="mac-defer", db=db_session)

    assert delivered is False
    assert _spoken(live) == []
    row = await _parked_row(db_session, text)
    assert row is not None and row.spoken is False
    assert (row.hud or {})[LIVE_MAIL_KEY]["pending"] is True
    live.close()
    reset_live_registry()


async def test_proactive_line_parks_while_owner_turn_is_in_flight(db_session) -> None:
    reset_live_registry()
    text = "Deferred lookout: someone is at the door."
    live = LiveSession(session_id="defer-2", device_id="mac-defer-2", backchannel_enabled=False)
    register_live(live)
    live.engine.state.note_user_speech_start()

    delivered = await speak_on_live(text, device_id="mac-defer-2", db=db_session)

    assert delivered is False
    assert _spoken(live) == []
    row = await _parked_row(db_session, text)
    assert row is not None and row.spoken is False
    live.close()
    reset_live_registry()


async def test_proactive_line_speaks_when_the_owner_is_idle(db_session) -> None:
    reset_live_registry()
    text = "Idle callout: the print job finished."
    live = LiveSession(session_id="defer-3", device_id="mac-defer-3", backchannel_enabled=False)
    register_live(live)

    delivered = await speak_on_live(text, device_id="mac-defer-3", db=db_session)

    assert delivered is True
    assert any(text in spoken for spoken in _spoken(live))
    assert await _parked_row(db_session, text) is None
    live.close()
    reset_live_registry()


async def test_deferred_line_speaks_once_the_offer_clears(db_session) -> None:
    from app.cognitive.intent import clear_pending_offer, pending_offer
    from app.cognitive.session_store import current

    reset_live_registry()
    _arm_read_aloud_offer()
    text = "Deferred callout: the studio render finished."
    live = LiveSession(session_id="defer-4", device_id="mac-defer-4", backchannel_enabled=False)
    register_live(live)

    assert await speak_on_live(text, device_id="mac-defer-4", db=db_session) is False
    assert await deliver_pending_live_mail(db_session, live) == 0
    assert _spoken(live) == []
    row = await _parked_row(db_session, text)
    assert row is not None and row.spoken is False

    clear_pending_offer(current())
    assert pending_offer(current()) is None

    assert await deliver_pending_live_mail(db_session, live) >= 1
    assert any(text in spoken for spoken in _spoken(live))
    await db_session.refresh(row)
    assert row.spoken is True
    live.close()
    reset_live_registry()


async def test_spoken_proactive_line_enters_the_turn_ledger(db_session) -> None:
    from app.cognitive.intent import recent_exchanges
    from app.cognitive.session_store import current

    reset_live_registry()
    text = "Ledger callout: the print job finished."
    live = LiveSession(session_id="defer-5", device_id="mac-defer-5", backchannel_enabled=False)
    register_live(live)

    assert await speak_on_live(text, device_id="mac-defer-5", db=db_session) is True

    rows = recent_exchanges(current())
    assert rows[-1]["evie"] == text
    assert rows[-1]["owner"] == ""
    assert rows[-1]["kind"] == "proactive"
    live.close()
    reset_live_registry()
