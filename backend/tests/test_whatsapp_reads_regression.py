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

    wa_file = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    sms_file = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    wa_file.close()
    sms_file.close()
    try:
        _make_wa(wa_file.name)
        _make_sms(sms_file.name)
        daemon = LifeStreamDaemon(chat_db_path=sms_file.name, whatsapp_db_path=wa_file.name)

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
        for path in (wa_file.name, sms_file.name):
            with __import__("contextlib").suppress(Exception):
                os.unlink(path)
