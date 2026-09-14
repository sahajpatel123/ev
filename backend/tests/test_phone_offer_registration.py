"""The phone lane's own question stays answerable on the next turn.

Owner report: on the iPhone realtime lane Evie found the mail, asked whether to
read it out, and the owner's "yes" came back as a topic-free greeting. Tool
results on that lane go straight to the speech provider and never pass through
the cognitive kernel's handle_turn, so the question was stored nowhere and the
"yes" had no referent. These tests drive the real phone-lane tool entry point
(``run_phone_tool`` -> ``maybe_phone_mac_act`` -> the mail read's spoken
shaping) and prove the offer lands on the durable session — and that a flat
statement does not arm one.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from app.device_gateway import webrtc_live

DEVICE_ID = UUID("11111111-1111-1111-1111-111111111111")
OWNER_UTTERANCE = "search for x in mail"
MAIL_OFFER = (
    "The newest mail is from GitHub about a failed CI run on main, "
    "arrived Friday at 2:21 am. Do you want me to read out the full mail?"
)
MAIL_FLAT = "Recent mail: GitHub — failed CI run on main."


class _SessionCtx:
    """Stands in for ``AsyncSession`` as an async context manager."""

    def __init__(self, db: object) -> None:
        self._db = db

    async def __aenter__(self) -> object:
        return self._db

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def _mail_read(monkeypatch, tmp_path, *, spoken: str) -> dict:
    """One trusted-phone mail read, straight through the phone lane."""

    from app.cognitive.session_store import reset_for_tests
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()

    live = SimpleNamespace(
        device_id=DEVICE_ID,
        memory_scope="owner",
        device_role="primary_companion",
        run_live_tool=AsyncMock(),
        grok_voice=SimpleNamespace(_last_input_transcript=OWNER_UTTERANCE),
    )
    monkeypatch.setattr(webrtc_live, "live_for_session", lambda session_id: live)
    device = SimpleNamespace(
        id=DEVICE_ID,
        name="Owner phone",
        memory_scope="owner",
        role="primary_companion",
        platform="ios",
        revoked_at=None,
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(first=lambda: device)
            )
        ),
        commit=AsyncMock(),
    )
    monkeypatch.setattr("app.db.SessionLocal", lambda: _SessionCtx(db))
    monkeypatch.setattr(
        "app.ev.messaging.approval.handle_send_approval",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.ev.tools.dispatch",
        AsyncMock(
            return_value=SimpleNamespace(
                ok=True,
                error=None,
                result={"spoken": spoken, "messages": [{"subject": "failed CI run"}]},
            )
        ),
    )
    raw = await webrtc_live.run_phone_tool(
        session_id="phone-live",
        name="evie_home_action",
        arguments={"capability": "list_mail"},
        call_id="call-mail",
    )
    return json.loads(raw)


def _durable_session():
    from app.cognitive.session_store import current, forget_live_cache

    # Reload from disk: the offer must survive the process, not just a cache.
    forget_live_cache()
    return current()


async def test_phone_mail_question_arms_the_durable_offer(monkeypatch, tmp_path) -> None:
    payload = await _mail_read(monkeypatch, tmp_path, spoken=MAIL_OFFER)
    assert payload["spoken"] == MAIL_OFFER

    from app.cognitive.intent import continuation_readout, pending_offer, recent_exchanges

    session = _durable_session()
    offer = pending_offer(session)
    assert offer is not None, "the question Evie asked on the phone was stored nowhere"
    assert offer["text"] == MAIL_OFFER
    assert offer["readout"] is True
    assert offer["action"]["tool"] == "evie_home_action"
    assert offer["action"]["args"]["capability"] == "list_mail"
    assert continuation_readout("yes", session) is True
    rows = recent_exchanges(session)
    assert rows[-1]["owner"] == OWNER_UTTERANCE
    assert rows[-1]["evie"] == MAIL_OFFER


async def test_phone_mail_statement_arms_no_offer(monkeypatch, tmp_path) -> None:
    payload = await _mail_read(monkeypatch, tmp_path, spoken=MAIL_FLAT)
    assert payload["spoken"] == MAIL_FLAT

    from app.cognitive.intent import pending_offer, recent_exchanges

    session = _durable_session()
    assert pending_offer(session) is None
    # The exchange itself is still recorded, so a later follow-up about that
    # mail keeps its referent even though Evie asked nothing.
    assert recent_exchanges(session)[-1]["evie"] == MAIL_FLAT


async def test_unreadable_session_does_not_break_the_owner_answer(
    monkeypatch, tmp_path
) -> None:
    """Continuity bookkeeping must never turn a spoken answer into an error."""

    def _boom():
        raise OSError("session file unreadable")

    monkeypatch.setattr("app.cognitive.session_store.current", _boom)
    payload = await _mail_read(monkeypatch, tmp_path, spoken=MAIL_OFFER)
    assert payload["spoken"] == MAIL_OFFER
    assert payload["executed"] is True


async def test_phone_state_query_question_arms_the_durable_offer(
    monkeypatch, tmp_path
) -> None:
    from app.cognitive.session_store import reset_for_tests
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()
    live = SimpleNamespace(
        device_id=DEVICE_ID,
        memory_scope="owner",
        device_role="primary_companion",
        run_live_tool=AsyncMock(),
    )
    monkeypatch.setattr(webrtc_live, "live_for_session", lambda session_id: live)
    device = SimpleNamespace(
        id=DEVICE_ID,
        name="Owner phone",
        memory_scope="owner",
        role="primary_companion",
        platform="ios",
        revoked_at=None,
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(first=lambda: device)
            )
        ),
        commit=AsyncMock(),
    )
    monkeypatch.setattr("app.db.SessionLocal", lambda: _SessionCtx(db))
    monkeypatch.setattr(
        "app.device_gateway.pipeline.run_trusted_device_turn",
        AsyncMock(return_value={"reply": MAIL_OFFER, "ok": True, "route": "MAIL"}),
    )

    raw = await webrtc_live.run_phone_tool(
        session_id="phone-live",
        name="evie_state_query",
        arguments={"query_text": OWNER_UTTERANCE},
        call_id="call-state",
    )
    assert json.loads(raw)["spoken"] == MAIL_OFFER

    from app.cognitive.intent import pending_offer

    offer = pending_offer(_durable_session())
    assert offer is not None and offer["text"] == MAIL_OFFER
