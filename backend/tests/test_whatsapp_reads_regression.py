"""WhatsApp reads speak WhatsApp content, never SMS (regression)."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


def _make_wa(path: str) -> None:
    wa = sqlite3.connect(path)
    wa.executescript(
        """
        CREATE TABLE ZWACHATSESSION (
            Z_PK INTEGER PRIMARY KEY,
            ZPARTNERNAME TEXT,
            ZCONTACTJID TEXT
        );
        CREATE TABLE ZWAMESSAGE (
            Z_PK INTEGER PRIMARY KEY,
            ZMESSAGEDATE REAL,
            ZTEXT TEXT,
            ZISFROMME INTEGER,
            ZFROMJID TEXT,
            ZTOJID TEXT,
            ZPUSHNAME TEXT,
            ZCHATSESSION INTEGER
        );
        INSERT INTO ZWACHATSESSION VALUES (1, 'Zara', 'zara@s.whatsapp.net');
        INSERT INTO ZWAMESSAGE VALUES (1, 750000000.0, 'zara gate secret pineapple', 0, 'zara@s.whatsapp.net', '', 'Zara', 1);
        """
    )
    wa.commit()
    wa.close()


def _make_sms(path: str) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, text TEXT, handle_id INTEGER, is_from_me INTEGER);
        INSERT INTO handle VALUES (1, '+15550100');
        INSERT INTO message VALUES (1, 780000000000000000, 'sms decoy zebra stripes', 1, 0);
        """
    )
    db.commit()
    db.close()


@pytest.mark.asyncio
async def test_whatsapp_read_speaks_whatsapp_not_sms(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.memory import live_life as live_mod
    from app.memory.live_life import peek_mac_life as real_peek
    from app.memory.recall import build_explicit_recall_payload
    from app.services.life_stream_daemon import LifeStreamDaemon

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as wa_file:
        wa_path = wa_file.name
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as sms_file:
        sms_path = sms_file.name
    try:
        _make_wa(wa_path)
        _make_sms(sms_path)
        daemon = LifeStreamDaemon(chat_db_path=sms_path, whatsapp_db_path=wa_path)

        daemon_fixture = daemon

        def forced_peek(query, *, shelf, tokens=None, k=8, daemon=None):
            del daemon
            return real_peek(query, shelf=shelf, tokens=tokens, k=k, daemon=daemon_fixture)

        daemon_fixture = daemon
        monkeypatch.setattr(live_mod, "peek_mac_life", forced_peek)

        for query in (
            "tell me about my recent message on whatsapp",
            "what is my most recent message on whatsapp",
            "tell me about my conversation with Zara on whatsapp",
        ):
            pack = await build_explicit_recall_payload(db_session, query, k=8)
            spoken = str(pack.get("spoken") or "")
            assert "pineapple" in spoken.lower(), (query, spoken)
            assert "zebra" not in spoken.lower(), (query, spoken)
    finally:
        for path in (wa_path, sms_path):
            with __import__("contextlib").suppress(Exception):
                os.unlink(path)


class _FakeWhatsAppWorkspace:
    def __init__(self, before, after, *, error=None):
        self.before = before
        self.after = after
        self.error = error
        self.clicked = False
        self.composed = False

    async def open(self, _to):
        return "Ada"

    async def read(self, _to, _limit):
        return self.after if self.clicked else self.before

    async def compose(self, _to, _body):
        from app.ev.messaging.whatsapp_cdp import WorkspaceError

        if self.error:
            raise WorkspaceError(self.error)
        self.composed = True

    async def state(self, _to):
        return {"header": "Ada", "body": "hello", "send": {"x": 1, "y": 2}}

    async def click(self, _point):
        self.clicked = True


def _stub_whatsapp_workspace(monkeypatch, workspace):
    from contextlib import asynccontextmanager

    from app.ev.messaging import whatsapp_cdp

    @asynccontextmanager
    async def fake_workspace():
        yield workspace

    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(whatsapp_cdp, "_workspace", fake_workspace)
    monkeypatch.setattr(whatsapp_cdp, "_wait", no_wait)


async def test_cdp_send_requires_new_outgoing_exact_message_id(monkeypatch):
    from app.ev.messaging import whatsapp_cdp

    old = {"id": "old", "body": "hello", "from_me": True}
    incoming = {"id": "incoming", "body": "hello", "from_me": False}
    substring = {"id": "substring", "body": "hello there", "from_me": True}
    fake = _FakeWhatsAppWorkspace([old], [old, incoming, substring])
    _stub_whatsapp_workspace(monkeypatch, fake)
    result = await whatsapp_cdp.send("Ada", "hello")
    assert result["sent"] is False
    assert result["send_attempted"] is True
    assert result["retry_safe"] is False

    new = {"id": "new", "body": "hello", "from_me": True, "transport_state": "sent"}
    fake = _FakeWhatsAppWorkspace([old], [old, new])
    _stub_whatsapp_workspace(monkeypatch, fake)
    result = await whatsapp_cdp.send("Ada", "hello")
    assert result["sent"] is True
    assert result["message_id"] == "new"
    assert result["verification"] == "new_acknowledged_outgoing_row"
    assert result["delivery_confirmed"] is False
    assert result["focus_theft"] == 0


async def test_cdp_foreign_draft_never_clicks_send(monkeypatch):
    from app.ev.messaging import whatsapp_cdp

    fake = _FakeWhatsAppWorkspace([], [], error="foreign_draft")
    _stub_whatsapp_workspace(monkeypatch, fake)
    result = await whatsapp_cdp.send("Ada", "hello")
    assert result["error"] == "foreign_draft"
    assert result["send_attempted"] is False
    assert fake.clicked is False
    assert fake.composed is False


async def test_cdp_read_search_are_honestly_partial(monkeypatch):
    from app.ev.messaging import whatsapp_cdp

    fake = _FakeWhatsAppWorkspace([{"id": "1", "body": "hello", "text": "hello"}], [])
    _stub_whatsapp_workspace(monkeypatch, fake)
    read = await whatsapp_cdp.read_recent("Ada")
    assert read["complete_history"] is False
    assert read["marks_read"] is True
    assert read["scope"] == "rendered_thread"
    search = await whatsapp_cdp.search_messages("Ada", "hello")
    assert len(search["messages"]) == 1
    assert search["complete_history"] is False


async def test_cdp_rejects_foreground_and_wrong_profile(monkeypatch, tmp_path):
    from app.ev.messaging import whatsapp_cdp

    monkeypatch.setenv("EV_WHATSAPP_CDP_PROFILE", str(tmp_path / "workspace"))

    async def command(_ws, _method, _params, *, msg_id):
        assert msg_id == 1
        return {"result": {"arguments": ["--user-data-dir=" + str(tmp_path / "workspace")]}}

    monkeypatch.setattr(whatsapp_cdp, "_cdp", command)
    with pytest.raises(whatsapp_cdp.WorkspaceError, match="cdp_foreground_browser"):
        await whatsapp_cdp._verify_background(None)

    async def other_profile(_ws, _method, _params, *, msg_id):
        return {"result": {"arguments": ["--headless=new", "--user-data-dir=/tmp/unrelated-profile"]}}

    monkeypatch.setattr(whatsapp_cdp, "_cdp", other_profile)
    with pytest.raises(whatsapp_cdp.WorkspaceError, match="cdp_profile_mismatch"):
        await whatsapp_cdp._verify_background(None)


async def test_cdp_normal_send_never_reveals_pairing(monkeypatch):
    from app.ev.messaging import whatsapp_cdp

    monkeypatch.setattr(whatsapp_cdp, "_under_pytest", lambda: False)
    seen = []

    async def needs_qr(*, reveal_workspace):
        seen.append(reveal_workspace)
        return "qr", "cdp_qr"

    monkeypatch.setattr(whatsapp_cdp, "ensure_ready", needs_qr)
    # Replace only locking: normal real workspace admission still executes.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lock():
        yield

    monkeypatch.setattr(whatsapp_cdp, "_transaction_lock", lock)
    result = await whatsapp_cdp.send("Ada", "hello")
    assert result["sent"] is False
    assert result["diagnosis"] == "cdp_qr"
    assert seen == [False]


async def test_cdp_qr_wrapper_validates_png(monkeypatch):
    import base64

    from app.ev.messaging import whatsapp_cdp

    async def fake_setup(*, include_qr, refresh_qr=False):
        assert include_qr is True
        return {"qr_png_base64": base64.b64encode(b"not a png").decode()}

    monkeypatch.setattr(whatsapp_cdp, "setup", fake_setup)
    assert await whatsapp_cdp.link_qr() is None


async def test_cdp_profile_lock_releases_after_exception(monkeypatch, tmp_path):
    import fcntl

    from app.ev.messaging import whatsapp_cdp

    monkeypatch.setenv("EV_WHATSAPP_CDP_PROFILE", str(tmp_path / "workspace"))
    with pytest.raises(RuntimeError, match="cancelled transaction"):
        async with whatsapp_cdp._transaction_lock():
            raise RuntimeError("cancelled transaction")
    descriptor = os.open(tmp_path / "workspace.transaction.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


async def test_whatsapp_policy_connection_is_channel_specific(db_session, monkeypatch):
    from app.ev import policy

    async def linked(*, refresh=False):
        return True

    monkeypatch.setattr("app.ev.messaging.whatsapp_web.web_available", linked)
    monkeypatch.setattr("app.services.life_stream_daemon.life_stream_should_run", lambda: False)
    monkeypatch.setattr("app.ev.apps.discover_life_helper_path", lambda: None)
    spec = {"provider": "messaging"}
    assert await policy.provider_connected(db_session, "send_message", spec, {"channel": "whatsapp"}) is True
    assert await policy.provider_connected(db_session, "send_message", spec, {"channel": "sms"}) is False


async def test_whatsapp_phone_resolution_never_uses_message_preview():
    from app.ev.messaging.whatsapp_web import score_chat

    assert score_chat("+1 555 0100", {"name": "Ada", "gist": "call +1 555 0100"}) == 0


@pytest.mark.parametrize("state", ["queued", "unknown", "failed"])
async def test_cdp_new_local_outgoing_row_is_not_a_sent_receipt(monkeypatch, state):
    from app.ev.messaging import whatsapp_cdp

    row = {"id": "new", "body": "hello", "from_me": True, "transport_state": state}
    fake = _FakeWhatsAppWorkspace([], [row])
    _stub_whatsapp_workspace(monkeypatch, fake)
    result = await whatsapp_cdp.send("Ada", "hello")
    assert result["sent"] is False
    assert result["send_attempted"] is True
    assert result["retry_safe"] is False


async def test_cdp_missing_message_identity_cannot_verify_send(monkeypatch):
    from app.ev.messaging import whatsapp_cdp

    fake = _FakeWhatsAppWorkspace([], [{"body": "hello", "from_me": True, "transport_state": "sent"}])
    _stub_whatsapp_workspace(monkeypatch, fake)
    result = await whatsapp_cdp.send("Ada", "hello")
    assert result["sent"] is False


async def test_uncertain_whatsapp_send_cannot_reuse_approval(db_session):
    from app.ev.messaging.approval import park_send, recent_failed_send
    from app.utils.text import utcnow

    ticket = await park_send(db_session, to="Ada", text="hello", display="Ada", actor="voice",
                             live_session_id="session-a")
    ticket.status = "executed"
    ticket.executed_at = utcnow()
    ticket.result = {"ok": False, "sent": False, "send_attempted": True, "retry_safe": False}
    await db_session.flush()
    assert await recent_failed_send(db_session, to="Ada", text="hello", actor="voice",
                                    live_session_id="session-a") is None
    ticket.result = {"ok": False, "sent": False, "send_attempted": False, "retry_safe": True}
    await db_session.flush()
    assert await recent_failed_send(db_session, to="Ada", text="hello", actor="voice",
                                    live_session_id="session-a") is ticket
    assert await recent_failed_send(db_session, to="Ada", text="hello", actor="voice",
                                    live_session_id="session-b") is None
    assert await recent_failed_send(db_session, to="Ada", text="hello", actor="master",
                                    live_session_id="session-a") is None


@pytest.fixture
def local_whatsapp_cache(monkeypatch, tmp_path):
    from app.ev.messaging import whatsapp_local

    path = tmp_path / "ChatStorage.sqlite"
    _make_wa(str(path))
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO ZWACHATSESSION VALUES (2, 'Duplicate', 'first@s.whatsapp.net')")
    connection.execute("INSERT INTO ZWACHATSESSION VALUES (3, 'Duplicate', 'second@s.whatsapp.net')")
    connection.execute("INSERT INTO ZWACHATSESSION VALUES (4, 'Status', 'status@broadcast')")
    connection.execute("INSERT INTO ZWAMESSAGE VALUES (2, 750000001, ?, 1, '', 'zara@s.whatsapp.net', '', 1)",
                       ("whole context " * 100,))
    connection.commit()
    connection.close()
    monkeypatch.setenv("EV_WHATSAPP_LOCAL_DB", str(path))
    monkeypatch.setattr(whatsapp_local, "_under_pytest", lambda: False)
    return path


async def test_local_whatsapp_cache_read_has_full_bodies_without_pairing(local_whatsapp_cache):
    from app.ev.messaging import whatsapp_local

    before = local_whatsapp_cache.read_bytes()
    state = await whatsapp_local.status()
    assert state["read_available"] is True
    assert state["draft_available"] is True
    assert state["send_available"] is False
    assert state["authenticated"] is False
    assert state["freshness"] == "desktop_sync_unknown"
    result = await whatsapp_local.read_recent("Zara", limit=20)
    assert result["ok"] is True
    assert result["chat_ref"] == "local:1"
    assert result["messages"][1]["body"] == "whole context " * 100
    assert result["messages"][1]["body_truncated"] is False
    assert result["marks_read"] is False
    assert result["complete_history"] is False
    assert local_whatsapp_cache.read_bytes() == before


async def test_local_whatsapp_chat_identity_is_unique_or_ambiguous(local_whatsapp_cache):
    from app.ev.messaging import whatsapp_local

    ambiguous = await whatsapp_local.open_chat("Duplicate")
    assert ambiguous["ok"] is False
    assert ambiguous["error"] == "ambiguous_recipient"
    exact = await whatsapp_local.open_chat("local:2")
    assert exact["ok"] is True
    assert exact["name"] == "Duplicate"
    chats = await whatsapp_local.search_chats("")
    assert not any(row["name"] == "Status" for row in chats["chats"])


async def test_local_whatsapp_search_filters_cached_text_without_ui(local_whatsapp_cache):
    from app.ev.messaging import whatsapp_local

    result = await whatsapp_local.search_messages("local:1", "pineapple")
    assert result["ok"] is True
    assert len(result["messages"]) == 1
    assert result["messages"][0]["body"] == "zara gate secret pineapple"
    assert result["marks_read"] is False
    assert result["scope"] == "desktop_cache_text_messages"


async def test_local_whatsapp_cache_read_does_not_admit_sends(db_session, local_whatsapp_cache, monkeypatch):
    from app.ev import policy

    async def unlinked(*, refresh=False):
        return False

    monkeypatch.setattr("app.ev.messaging.whatsapp_web.web_available", unlinked)
    spec = {"provider": "messaging"}
    assert await policy.provider_connected(db_session, "list_messages", spec, {"channel": "whatsapp"}) is True
    assert await policy.provider_connected(db_session, "send_message", spec, {"channel": "whatsapp"}) is False


async def test_linked_visible_browser_is_not_background_send_authority(monkeypatch):
    from app.ev.messaging import whatsapp_cdp

    async def available(*, refresh=False):
        return False, "cdp_foreground_browser"

    monkeypatch.setattr(whatsapp_cdp, "available", available)
    monkeypatch.setattr(whatsapp_cdp, "_headless_verified", False)
    monkeypatch.setattr(whatsapp_cdp, "_linked_ui_verified", True)
    result = await whatsapp_cdp.status(refresh=True)
    assert result["linked"] is True
    assert result["browser_authenticated"] is True
    assert result["authenticated"] is False
    assert result["send_available"] is False
    assert result["headless"] is False


async def test_fresh_qr_request_is_owner_setup_only(monkeypatch):
    import base64

    from app.ev.messaging import whatsapp_cdp

    calls = []
    png = b"\x89PNG\r\n\x1a\n" + b"synthetic pairing fixture"

    async def setup(*, include_qr, refresh_qr):
        calls.append((include_qr, refresh_qr))
        return {"qr_png_base64": base64.b64encode(png).decode()}

    monkeypatch.setattr(whatsapp_cdp, "setup", setup)
    assert await whatsapp_cdp.link_qr(fresh=True) == png
    assert calls == [(True, True)]
