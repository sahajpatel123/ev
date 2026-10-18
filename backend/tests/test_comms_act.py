"""comms_act: drafts, grants, parks, disambiguation (no real sends)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


@pytest.fixture()
def fabric_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    monkeypatch.setattr(settings, "fabric_audit_file", str(tmp_path / "audit.jsonl"))
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    from app.fabric import grants, panic

    grants.revoke_all()
    panic.clear()
    return home


async def _dispatch(db_session: AsyncSession, name: str, args: dict) -> dict[str, Any]:
    from app.ev.tools import dispatch

    response = await dispatch(
        db_session, name, args, actor="master", allow_sensitive=True)
    assert isinstance(response.result, dict)
    return response.result


async def test_comms_draft_flags_and_first_contact(
    db_session: AsyncSession, fabric_on
) -> None:
    money = await _dispatch(db_session, "comms_act",
                            {"op": "draft", "channel": "messages",
                             "to": "+15551234567", "body": "pay you $50 now"})
    assert money["ok"] is True
    assert money["evidence"]["flags"]["money"] is True
    assert money["evidence"]["needs_confirm"] is True
    creds = await _dispatch(db_session, "comms_act",
                            {"op": "draft", "channel": "mail",
                             "to": "x@y.z", "body": "the password: hunter2"})
    assert creds["evidence"]["flags"]["credential"] is True


async def test_comms_send_parks_without_grant(
    db_session: AsyncSession, fabric_on
) -> None:
    parked = await _dispatch(db_session, "comms_act",
                             {"op": "send", "channel": "messages",
                              "to": "nobody-new-xyz", "body": "hello"})
    assert parked["ok"] is False
    assert parked.get("action_id"), parked


async def test_comms_grant_lifecycle_allows_known_recipient(
    db_session: AsyncSession, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.fabric import dispatch_tools as dt

    granted = await _dispatch(db_session, "comms_act",
                              {"op": "grant", "channel": "whatsapp",
                               "to": " wa-friend ", "ttl_s": 600})
    assert granted["ok"], granted
    # Known recipient + active grant + clean content: no park. The WhatsApp
    # backend is unlinked here, so the send fails honestly AFTER the gate.
    async def fake_first_contact(self, session, channel, to):
        assert channel == "whatsapp"
        return False

    monkeypatch.setattr(dt._CommsAct, "_first_contact", fake_first_contact)
    sent = await _dispatch(db_session, "comms_act",
                           {"op": "send", "channel": "whatsapp",
                            "to": "wa-friend", "body": "ping"})
    assert sent.get("action_id") is None, sent
    assert "WhatsApp" in str(sent.get("error", "")) or "whatsapp" in str(
        sent.get("error", "")).lower() or "chat" in str(sent.get("error", "")).lower(), sent
    revoked = await _dispatch(db_session, "comms_act",
                              {"op": "revoke", "channel": "whatsapp",
                               "to": "wa-friend"})
    assert revoked["ok"] is True
    parked = await _dispatch(db_session, "comms_act",
                             {"op": "send", "channel": "whatsapp",
                              "to": "wa-friend", "body": "ping2"})
    assert parked.get("action_id"), parked


async def test_comms_grant_never_covers_money_first_contact(
    db_session: AsyncSession, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.fabric import dispatch_tools as dt

    await _dispatch(db_session, "comms_act",
                    {"op": "grant", "channel": "messages",
                     "to": "friend", "ttl_s": 600})

    async def known(self, session, channel, to):
        return False

    monkeypatch.setattr(dt._CommsAct, "_first_contact", known)
    money = await _dispatch(db_session, "comms_act",
                            {"op": "send", "channel": "messages",
                             "to": "friend", "body": "pay you $50 now"})
    assert money.get("action_id"), money

    async def fresh(self, session, channel, to):
        return True

    monkeypatch.setattr(dt._CommsAct, "_first_contact", fresh)
    first = await _dispatch(db_session, "comms_act",
                            {"op": "send", "channel": "messages",
                             "to": "friend", "body": "hello stranger"})
    assert first.get("action_id"), first


async def test_comms_resolve_never_guesses(
    db_session: AsyncSession, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.fabric import dispatch_tools as dt

    async def no_contacts(*a, **k):
        from types import SimpleNamespace

        return SimpleNamespace(data={"contacts": []})

    import app.integrations.life_helper as lh

    monkeypatch.setattr(lh, "run_life_helper", no_contacts)
    none = await _dispatch(db_session, "comms_act",
                           {"op": "resolve", "query": "zzz-nobody"})
    assert none["ok"] is False


async def test_comms_read_is_tainted(db_session: AsyncSession, fabric_on) -> None:
    out = await _dispatch(db_session, "comms_act",
                          {"op": "read", "channel": "whatsapp",
                           "query": "hello", "limit": 3})
    # Backend may be unlinked: either honest failure or tainted content.
    if out["ok"]:
        assert out["evidence"]["provenance"] == "untrusted_content"
        assert out["evidence"]["tainted"]["authority"] == "DATA"
    else:
        assert out["error"], out
