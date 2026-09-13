"""A short phone-lane answer stays bound to Evie's own live question.

Owner report: on the iPhone realtime lane the owner answered Evie's own
question with "yes" and heard a topic-free greeting. Tool results on that lane
go straight to the speech provider, so the referent has to ride in the handback
itself. These tests drive the real phone-lane entry point (``run_phone_tool``
-> ``run_trusted_device_turn`` -> TurnGate) and read the JSON the provider
would receive; only the device row lookup and the live-provider object are
stubbed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.device_gateway import webrtc_live

BARE_HINT = "No canonical state matched; answer the owner conversationally yourself."
MAIL_OFFER = (
    "The newest mail is from GitHub about a failed CI run on main, "
    "arrived Friday at 2:21 am. Do you want me to read out the full mail?"
)
STATE_OFFER = "Your Mac is online. Do you want me to check the Canary project's priority?"
MAIL_ACTION = {"tool": "evie_home_action", "args": {"capability": "list_mail"}}


class _SessionCtx:
    """Yield the test's real session; the phone lane must not close it."""

    def __init__(self, db: object) -> None:
        self._db = db

    async def __aenter__(self) -> object:
        return self._db

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def _phone_turn(
    monkeypatch, db_session, *, said: str, offer: str | None = None
) -> dict:
    """One real phone-lane tool call, ending in the provider's handback."""

    from app.cognitive.intent import set_pending_offer
    from app.cognitive.session_store import current
    from app.models import Device

    if offer is not None:
        set_pending_offer(current(), offer, action=MAIL_ACTION)

    device = Device(
        name="Offer Phone",
        token_hash="offer-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(device)
    await db_session.commit()

    live = SimpleNamespace(
        device_id=device.id,
        memory_scope="owner",
        device_role="primary_companion",
        run_live_tool=AsyncMock(),
    )
    monkeypatch.setattr(webrtc_live, "live_for_session", lambda session_id: live)
    monkeypatch.setattr("app.db.SessionLocal", lambda: _SessionCtx(db_session))

    raw = await webrtc_live.run_phone_tool(
        session_id="phone-live",
        name="evie_state_query",
        arguments={"query_text": said},
        call_id="call-offer",
    )
    return json.loads(raw)


@pytest.mark.asyncio
async def test_bare_yes_hands_the_speaker_evies_own_question(monkeypatch, db_session) -> None:
    payload = await _phone_turn(monkeypatch, db_session, said="yes", offer=MAIL_OFFER)

    assert payload["conversational"] is True
    # The answer itself is still the provider's to speak: never fabricated here.
    assert payload["spoken"] == ""
    assert payload["hint"] != BARE_HINT
    assert "failed CI run on main" in payload["hint"]
    assert "Do you want me to read out the full mail?" in payload["hint"]


@pytest.mark.asyncio
async def test_read_aloud_offer_is_handed_over_as_a_readout(monkeypatch, db_session) -> None:
    payload = await _phone_turn(monkeypatch, db_session, said="yes", offer=MAIL_OFFER)

    hint = payload["hint"].lower()
    assert "read-aloud" in hint
    assert "read it out, the whole thing" in hint
    assert "never the same short gist" in hint


@pytest.mark.asyncio
async def test_non_readout_offer_is_not_handed_over_as_a_readout(
    monkeypatch, db_session
) -> None:
    payload = await _phone_turn(monkeypatch, db_session, said="yes", offer=STATE_OFFER)

    hint = payload["hint"]
    assert "Canary project's priority" in hint
    assert "read-aloud" not in hint.lower()
    # The offer's own action rides along so the speaker can carry it out.
    assert "evie_home_action" in hint


@pytest.mark.asyncio
async def test_bare_no_declines_the_offer_without_claiming_agreement(
    monkeypatch, db_session
) -> None:
    payload = await _phone_turn(monkeypatch, db_session, said="no", offer=MAIL_OFFER)

    hint = payload["hint"].lower()
    assert "declined" in hint
    assert "read-aloud" not in hint


@pytest.mark.asyncio
async def test_no_live_offer_keeps_the_previous_handback(monkeypatch, db_session) -> None:
    payload = await _phone_turn(monkeypatch, db_session, said="yes")

    assert payload == {
        "ok": True,
        "conversational": True,
        "spoken": "",
        "hint": BARE_HINT,
    }


@pytest.mark.asyncio
async def test_a_real_question_is_unaffected_by_a_live_offer(monkeypatch, db_session) -> None:
    payload = await _phone_turn(
        monkeypatch, db_session, said="Tell me a joke", offer=MAIL_OFFER
    )

    assert payload == {
        "ok": True,
        "conversational": True,
        "spoken": "",
        "hint": BARE_HINT,
    }
