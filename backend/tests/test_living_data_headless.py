"""Tests for Living Data Ingestion Daemon, Zero-Window Headless Execution, and Selective Memory Filtering."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.compiler import (
    ContextCompiler,
    is_casual_social_turn,
    wants_deep_dive,
)
from app.ev.actions import get_action_spec
from app.ev.personality import DEFAULT_PROFILE, identity_block
from app.ev.tools import _LIFE_BRIDGES, life_success_reply
from app.services.life_stream_daemon import LifeStreamDaemon

# ---------------------------------------------------------------------------
# 1. LifeStreamDaemon & iMessage SQLite Follower
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_life_stream_daemon_imessage_follower(db_session: AsyncSession) -> None:
    """Verify that LifeStreamDaemon incrementally ingests incoming & outgoing iMessages."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE handle (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL
            );
        """)
        cursor.execute("""
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
                date INTEGER,
                text TEXT,
                handle_id INTEGER,
                is_from_me INTEGER
            );
        """)
        cursor.execute("INSERT INTO handle (id) VALUES ('+15551234567');")
        cursor.execute("INSERT INTO handle (id) VALUES ('alex@example.com');")
        # 1 billion seconds since 2001 (approx 2032 or standard Cocoa timestamp)
        cursor.execute(
            "INSERT INTO message (ROWID, date, text, handle_id, is_from_me) VALUES (1, 750000000000000000, 'Hey Sahaj, are we meeting today?', 1, 0);"
        )
        cursor.execute(
            "INSERT INTO message (ROWID, date, text, handle_id, is_from_me) VALUES (2, 750000060000000000, 'Yes, at 3pm!', 1, 1);"
        )
        conn.commit()
        conn.close()

        daemon = LifeStreamDaemon(chat_db_path=db_path)
        assert daemon.is_chat_db_accessible() is True

        events = await daemon.sync_imessage(db_session, limit=10)
        assert len(events) == 2
        assert daemon.last_message_rowid == 2

        # Check event types & contents
        recv_event = events[0]
        assert recv_event.event_type == "message.imessage.received"
        assert recv_event.content["text"] == "Hey Sahaj, are we meeting today?"
        assert recv_event.content["handle"] == "+15551234567"
        assert recv_event.content["is_from_me"] is False

        sent_event = events[1]
        assert sent_event.event_type == "message.imessage.sent"
        assert sent_event.content["text"] == "Yes, at 3pm!"
        assert sent_event.content["is_from_me"] is True

        # Second poll with no new messages should return empty
        events_empty = await daemon.sync_imessage(db_session, limit=10)
        assert len(events_empty) == 0

        # Adding a new message increases rowid and yields 1 new event
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO message (ROWID, date, text, handle_id, is_from_me) VALUES (3, 750000120000000000, 'Sounds good!', 1, 0);"
        )
        conn.commit()
        conn.close()

        events_new = await daemon.sync_imessage(db_session, limit=10)
        assert len(events_new) == 1
        assert events_new[0].content["text"] == "Sounds good!"
        assert daemon.last_message_rowid == 3

    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_life_stream_daemon_contacts_and_tick(db_session: AsyncSession) -> None:
    """Verify contact delta ingestion and single tick execution."""
    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    assert daemon.is_chat_db_accessible() is False

    # Tick degrades cleanly when chat.db is not present
    result = await daemon.tick(db_session)
    assert result["ok"] is True
    assert result["messages_ingested"] == 0
    assert result["chat_db_accessible"] is False

    # Ingest contacts
    sample_contacts = [
        {"id": "c-1", "name": "Bruce Wayne", "phone": "+15559990000", "company": "Wayne Enterprises"},
        {"id": "c-2", "name": "Peter Parker", "phone": "+15551112222", "company": "Daily Bugle"},
    ]
    c_events = await daemon.sync_contacts_delta(db_session, sample_contacts)
    assert len(c_events) == 2
    assert c_events[0].event_type == "contact.discovered"
    assert c_events[0].content["name"] == "Bruce Wayne"
    assert c_events[0].privacy_level == "sensitive"

    # Subsequent sync of the same unchanged contacts is a no-op
    c_events2 = await daemon.sync_contacts_delta(db_session, sample_contacts)
    assert c_events2 == []

    changed = [
        {"id": "c-1", "name": "Bruce Wayne", "phone": "+15550001111", "company": "Wayne Enterprises"},
        {"id": "c-2", "name": "Peter Parker", "phone": "+15551112222", "company": "Daily Bugle"},
    ]
    c_events3 = await daemon.sync_contacts_delta(db_session, changed)
    assert len(c_events3) == 1
    assert c_events3[0].event_type == "contact.updated"
    assert c_events3[0].content["phone"] == "+15550001111"


# ---------------------------------------------------------------------------
# 2. ContactsAdapter: CRUD Operations (Zero-Window)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contacts_adapter_crud() -> None:
    """Verify local zero-window ContactsAdapter create, update, and resolve."""
    from app.integrations.adapters import registry

    adapter = registry.get("contacts")
    assert adapter is not None
    config = {"provider": "local", "contacts": []}

    # 1. Create contact
    create_res = await adapter.act(
        action="contacts.create",
        args={"name": "Miles Morales", "phone": "+15553334444", "email": "miles@example.com"},
        token="",
        scopes=["contacts:act"],
        config=config,
    )
    assert create_res["ok"] is True
    assert create_res["created"] is True
    assert create_res["contact"]["name"] == "Miles Morales"
    assert create_res["contact"]["phone"] == "+15553334444"

    # 2. Resolve contact
    resolve_res = await adapter.act(
        action="contacts.resolve",
        args={"query": "Miles"},
        token="",
        scopes=["contacts:read"],
        config=config,
    )
    assert resolve_res["ok"] is True
    assert resolve_res["contact"]["name"] == "Miles Morales"

    # 3. Update contact
    update_res = await adapter.act(
        action="contacts.update",
        args={"query": "Miles", "phone": "+15559998888", "company": "Brooklyn Visions"},
        token="",
        scopes=["contacts:act"],
        config=config,
    )
    assert update_res["ok"] is True
    assert update_res["updated"] is True
    assert update_res["contact"]["phone"] == "+15559998888"
    assert update_res["contact"]["company"] == "Brooklyn Visions"


# ---------------------------------------------------------------------------
# 3. MailAdapter: Headless Send & List
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mail_adapter_headless_send_and_list() -> None:
    """Verify MailAdapter send with confirmation and inbox listing."""
    from app.integrations.adapters import registry

    adapter = registry.get("mail")
    assert adapter is not None
    config = {
        "provider": "local",
        "inbox": [
            {"subject": "Project Update", "from": "team@example.com", "snippet": "Sprint completed"},
        ],
    }

    # 1. List mail
    list_res = await adapter.act(
        action="mail.list",
        args={"limit": 5},
        token="",
        scopes=["mail:read"],
        config=config,
    )
    assert list_res["ok"] is True
    assert len(list_res["items"]) == 1
    assert list_res["items"][0]["subject"] == "Project Update"

    # 2. Send mail requires confirm=True in local adapter
    unconfirmed = await adapter.act(
        action="mail.send",
        args={"to": "team@example.com", "subject": "Re: Project Update", "body": "Great job!"},
        token="",
        scopes=["mail:act"],
        config=config,
    )
    assert unconfirmed["ok"] is False
    assert unconfirmed["error"] == "confirm_required"

    # 3. Send mail with confirm=True succeeds headlessly
    confirmed = await adapter.act(
        action="mail.send",
        args={"to": "team@example.com", "subject": "Re: Project Update", "body": "Great job!", "confirm": True},
        token="",
        scopes=["mail:act"],
        config=config,
    )
    assert confirmed["ok"] is True
    assert confirmed["sent"] is True


# ---------------------------------------------------------------------------
# 4. Selective Context & Personality Protection
# ---------------------------------------------------------------------------


def test_casual_turns_suppress_unsolicited_memories() -> None:
    """Verify that casual greetings and small talk suppress unsolicited memory injection."""
    # Greetings & small talk
    assert is_casual_social_turn("hey") is True
    assert is_casual_social_turn("Hey Evie!") is True
    assert is_casual_social_turn("how are you doing?") is True
    assert is_casual_social_turn("what's up") is True
    assert is_casual_social_turn("Good morning") is True
    assert is_casual_social_turn("thanks!") is True
    assert is_casual_social_turn("cool, got it.") is True

    # Genuine questions should NOT be classified as casual small talk
    assert is_casual_social_turn("What was the decision about PostgreSQL?") is False
    assert is_casual_social_turn("Why did we change the architecture?") is False
    assert is_casual_social_turn("Did I get any email from Alex?") is False
    assert is_casual_social_turn("Send a text to Mom saying I arrived") is False

    # wants_deep_dive must NOT trigger on casual small talk
    assert wants_deep_dive("how are you?") is False
    assert wants_deep_dive("what's up?") is False
    assert wants_deep_dive("What was our decision regarding the database?") is True


def test_compile_progressive_casual_turn_shallow_k_zero() -> None:
    """Verify compile_progressive produces zero retrieved memory items on casual turns."""
    compiler = ContextCompiler()
    memories = [
        SimpleNamespace(text="Decided to use PostgreSQL for event storage", memory_type="decision"),
        SimpleNamespace(text="Prefers dark mode for terminal tools", memory_type="preference"),
        SimpleNamespace(text="Had heart rate spike to 120 bpm during workout", memory_type="fact"),
    ]
    user_state = SimpleNamespace(
        activity="coding",
        active_project="ev",
        active_goal=None,
        current_task="implementing living data",
        recent_topics=["python", "swift"],
        open_decisions=[],
        live_context=[],
    )

    # Casual greeting: memory items must be omitted from prompt
    plan_casual = compiler.compile_progressive(
        memories=memories,
        user_state=user_state,
        strategy_text="STRATEGY: concise",
        budget=10000,
        message="Hey Evie, good morning!",
    )
    assert plan_casual.metadata["quiet"] is True
    assert not any(s.name == "relationship" and s.items_included for s in plan_casual.sections)
    # In casual plan, no memory lines should be included
    memory_section = next((s for s in plan_casual.sections if s.name == "retrieved_memory"), None)
    assert memory_section is not None
    assert memory_section.items_included == 0

    # Genuine query: memory items must be included
    plan_query = compiler.compile_progressive(
        memories=memories,
        user_state=user_state,
        strategy_text="STRATEGY: concise",
        budget=10000,
        message="What did we decide about the event storage database?",
    )
    assert plan_query.metadata["shallow_k"] > 0
    memory_section_query = next((s for s in plan_query.sections if s.name == "retrieved_memory"), None)
    assert memory_section_query is not None
    assert memory_section_query.items_included > 0


def test_personality_block_contains_conversational_discipline() -> None:
    """Verify that identity_block enforces casual conversational discipline and headless rules."""
    block = identity_block("E V", "personal AI companion", DEFAULT_PROFILE)
    assert "Do not recite recent emails, texts, or health statistics unprompted" in block
    assert "execute headlessly in the background without opening desktop windows" in block


# ---------------------------------------------------------------------------
# 5. Life Action Registry & Spoken Confirmations
# ---------------------------------------------------------------------------


def test_life_action_specs_and_spoken_confirmations() -> None:
    """Verify action specs for send_mail, save_contact, and their spoken confirmations."""
    # Specs resolve via get_action_spec or get_spec
    mail_spec = get_action_spec("send_mail")
    assert mail_spec is not None
    assert mail_spec["permission"] == "mail:act"
    assert mail_spec["risk_class"] == "R2"

    contact_spec = get_action_spec("save_contact")
    assert contact_spec is not None
    assert contact_spec["permission"] == "contacts:act"

    # Spoken confirmations
    mail_reply = life_success_reply({"to": "bruce@wayne.com"}, tool_name="send_mail")
    assert mail_reply == "Sent email to bruce@wayne.com."

    contact_reply = life_success_reply(
        {"contact": {"full_name": "Selina Kyle"}}, tool_name="save_contact"
    )
    assert contact_reply == "Saved contact for Selina Kyle."

    # _LIFE_BRIDGES entries exist
    assert "send_mail" in _LIFE_BRIDGES
    assert "save_contact" in _LIFE_BRIDGES
    assert "update_contact" in _LIFE_BRIDGES


def test_casual_chat_does_not_select_memory_or_life_tools() -> None:
    from app.ev.tool_select import resolve_live_action, select_tool

    assert select_tool("hey").selected == "chat"
    assert select_tool("How are you?").selected == "chat"
    assert select_tool("thanks").selected == "chat"
    assert resolve_live_action("hey") is None
    assert resolve_live_action("How are you?") is None
    assert select_tool("Call Ned").selected == "place_call"
    assert select_tool("the white remote").selected == "search_memory"


def test_life_helper_source_never_steals_focus() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "macos" / "Sources" / "EVLifeHelper" / "main.swift").read_text(
        encoding="utf-8"
    )
    call_block = source.split('case "call.place":', 1)[1].split('case "call.check":', 1)[0]
    assert "openURLHeadless" in call_block
    assert "NSWorkspace.shared.open(url)" not in call_block
    send_block = source.split('case "messages.send":', 1)[1].split('case "mail.list":', 1)[0]
    mail_send = source.split('case "mail.send":', 1)[1].split('case "call.place":', 1)[0]
    assert "NSWorkspace.shared.open(url)" not in send_block
    assert "NSWorkspace.shared.open(url)" not in mail_send
    assert 'tell application "Messages" to launch' in source
    assert 'tell application "Mail" to launch' in source
    assert "focus_stolen" in source
    assert "system_call_ui" in source


def test_life_stream_stays_off_in_ci() -> None:
    from app.services.life_stream_daemon import life_stream_should_run
    from app.workers.jobs import run_life_stream_tick

    assert life_stream_should_run() is False
    assert run_life_stream_tick()["skipped"] is True


def test_life_stream_cursor_roundtrip(tmp_path) -> None:
    cursor = tmp_path / "cursor.json"
    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    daemon.attach_cursor(cursor)
    daemon.last_message_rowid = 42
    daemon._contact_fps = {"c-1": "c-1|Bruce||"}
    daemon._mail_fps = {"Lunch|Alex|today": "1"}
    daemon._calendar_fps = {"ev-standup": "1"}
    daemon._health_fps = {"snap-1": "1"}
    daemon._last_account_pull = 12.5
    daemon.save_cursor()
    other = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    other.attach_cursor(cursor)
    assert other.last_message_rowid == 42
    assert other._contact_fps["c-1"] == "c-1|Bruce||"
    assert other._mail_fps["Lunch|Alex|today"] == "1"
    assert other._calendar_fps["ev-standup"] == "1"
    assert other._health_fps["snap-1"] == "1"
    assert other._last_account_pull == 12.5


@pytest.mark.asyncio
async def test_mail_delta_skips_unchanged_envelopes(db_session: AsyncSession) -> None:
    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    items = [
        {
            "subject": "Lunch tomorrow",
            "sender": "Alex <alex@example.com>",
            "received": "Wednesday",
        }
    ]
    first = await daemon.sync_mail_delta(db_session, items)
    assert len(first) == 1
    assert first[0].event_type == "mail.envelope.received"
    assert first[0].privacy_level == "sensitive"
    assert first[0].content["subject"] == "Lunch tomorrow"
    second = await daemon.sync_mail_delta(db_session, items)
    assert second == []


@pytest.mark.asyncio
async def test_live_life_is_recalled_only_when_asked(db_session: AsyncSession) -> None:
    from app.memory.extraction import Extractor
    from app.memory.history import recall_history
    from app.memory.life_archive.locate import locate_archive
    from app.memory.live_life import is_live_life_event
    from app.memory.recall import build_explicit_recall_payload
    from app.memory.retrieval import Retriever

    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    mail = await daemon.sync_mail_delta(
        db_session,
        [
            {
                "subject": "Lunch tomorrow",
                "sender": "Alex <alex@example.com>",
                "received": "Wednesday",
            }
        ],
    )
    contacts = await daemon.sync_contacts_delta(
        db_session,
        [{"id": "c-alex", "name": "Alex Rivera", "phone": "+15550100", "email": "alex@example.com"}],
    )
    from app.schemas import EventCreate
    from app.services.event_service import EventService

    chat = await EventService(db_session, actor="test").create(
        EventCreate(
            source="imessage",
            event_type="message.imessage.received",
            content={
                "text": "let's meet at 3",
                "handle": "Alex",
                "is_from_me": False,
                "rowid": 9,
            },
            privacy_level="sensitive",
        )
    )
    await db_session.commit()

    assert is_live_life_event(mail[0])
    assert is_live_life_event(contacts[0])
    assert is_live_life_event(chat)
    assert Extractor().extract(chat) == []
    assert Extractor().extract(mail[0]) == []

    chats = await locate_archive(db_session, "What did Alex text me?")
    assert any("let's meet at 3" in (hit.get("text") or "") for hit in chats)
    assert all(hit.get("kind") == "live_life" for hit in chats)

    inbox = await locate_archive(db_session, "Did I get any email from Alex?")
    assert any("Lunch tomorrow" in (hit.get("text") or "") for hit in inbox)

    book = await locate_archive(db_session, "Is Alex in my contacts?")
    assert any("Alex Rivera" in (hit.get("text") or "") for hit in book)

    assert await locate_archive(db_session, "hey") == []
    assert await locate_archive(db_session, "How are you?") == []

    history = await recall_history(db_session, "What did Alex text me?", k=8)
    history_text = " ".join(item["text"] for item in history["results"])
    assert "let's meet at 3" in history_text

    recalled = await build_explicit_recall_payload(db_session, "Did I get any email from Alex?")
    assert recalled.get("ok") is True
    assert recalled.get("life_shelf") == "mail"
    assert any("Lunch tomorrow" in (item.get("text") or "") for item in recalled.get("evidence") or [])
    assert "let's meet at 3" not in " ".join(recalled.get("lines") or [])

    leaked = await Retriever(db_session).search_events("Alex lunch meet", k=10, access="model")
    assert leaked == []


def test_life_ask_uses_recorded_memory_not_inbox_tools() -> None:
    from app.ev.tool_select import resolve_live_action, select_tool

    assert select_tool("check my inbox").selected == "list_mail"
    assert select_tool("Who texted me?").selected == "list_messages"
    assert select_tool("Did I get any email from Alex?").selected == "recall_history"
    assert select_tool("What did Alex text me?").selected == "recall_history"
    assert select_tool("Is Alex in my contacts?").selected == "recall_history"
    assert resolve_live_action("check my inbox") == ("list_mail", {})
    assert resolve_live_action("Who texted me?") == ("list_messages", {})
    assert resolve_live_action("Did I get any email from Alex?") == (
        "recall",
        {"query": "Did I get any email from Alex?"},
    )
    assert resolve_live_action("What did Alex text me?")[0] == "recall"
    assert select_tool("close Messages").selected == "close_app"
    assert resolve_live_action("close Messages") == ("close_app", {"name": "Messages"})
    assert select_tool("How did I sleep?").selected == "get_health_trends"
    assert select_tool("What's in my health history?").selected == "recall_history"
    assert select_tool("What was on my old calendar?").selected == "recall_history"


@pytest.mark.asyncio
async def test_calendar_delta_skips_unchanged_and_recalls_when_asked(
    db_session: AsyncSession,
) -> None:
    from app.memory.extraction import Extractor
    from app.memory.life_archive.locate import locate_archive
    from app.memory.live_life import is_live_life_event
    from app.memory.retrieval import Retriever

    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    items = [
        {
            "event_id": "g-standup",
            "summary": "Standup with Priya",
            "start": "2026-09-03T10:00:00Z",
            "location": "Zoom",
        }
    ]
    first = await daemon.sync_calendar_delta(db_session, items)
    assert len(first) == 1
    assert first[0].event_type == "calendar.event.recorded"
    assert first[0].privacy_level == "sensitive"
    assert first[0].source == "calendar"
    assert Extractor().extract(first[0]) == []
    second = await daemon.sync_calendar_delta(db_session, items)
    assert second == []

    hits = await locate_archive(db_session, "What was on my old calendar?")
    assert any("Standup with Priya" in (hit.get("text") or "") for hit in hits)
    assert all(hit.get("kind") == "live_life" for hit in hits)
    assert await locate_archive(db_session, "hey") == []
    leaked = await Retriever(db_session).search_events("Priya standup", k=10, access="model")
    assert leaked == []


@pytest.mark.asyncio
async def test_health_snapshot_records_and_recalls_history_only(
    db_session: AsyncSession,
) -> None:
    from app.ev.health_radar import create_snapshot
    from app.memory.extraction import Extractor
    from app.memory.life_archive.locate import classify_shelf, locate_archive
    from app.memory.live_life import is_live_life_event
    from app.memory.retrieval import Retriever
    from app.models import Event
    from sqlalchemy import select

    assert classify_shelf("How did I sleep?") is None
    assert classify_shelf("What's in my health history?") == "health"

    snapshot = await create_snapshot(
        db_session,
        metrics={"sleep_hours": 7.4, "hrv_ms": 48, "steps": 6120},
        source="test",
    )
    assert snapshot.id is not None
    await db_session.commit()

    rows = list(
        (
            await db_session.execute(
                select(Event).where(Event.source == "health", Event.event_type == "health.snapshot.recorded")
            )
        ).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].privacy_level == "sensitive"
    assert is_live_life_event(rows[0])
    assert Extractor().extract(rows[0]) == []
    assert "7.4" in (rows[0].content or {}).get("text", "")

    history = await locate_archive(db_session, "What's in my health history?")
    assert any("7.4" in (hit.get("text") or "") for hit in history)
    assert all(hit.get("kind") == "live_life" for hit in history)
    assert await locate_archive(db_session, "How did I sleep?") == []
    leaked = await Retriever(db_session).search_events("sleep 7.4", k=10, access="model")
    assert leaked == []


@pytest.mark.asyncio
async def test_gmail_list_is_metadata_only_and_does_not_send(monkeypatch) -> None:
    from app.integrations import adapters
    from app.integrations.adapters import registry

    class _FakeResp:
        def __init__(self, status: int, data: dict) -> None:
            self.status_code = status
            self._data = data

        def json(self) -> dict:
            return self._data

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None, headers=None):
            del headers
            path = str(url)
            if path.endswith("/users/me/messages"):
                assert params == {"maxResults": "10", "q": "in:inbox"}
                return _FakeResp(200, {"messages": [{"id": "gm1"}]})
            assert "gm1" in path
            assert params == [
                ("format", "metadata"),
                ("metadataHeaders", "Subject"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "Date"),
            ]
            return _FakeResp(
                200,
                {
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Invoice Q3"},
                            {"name": "From", "value": "billing@example.com"},
                            {"name": "Date", "value": "Thu, 3 Sep 2026"},
                            {"name": "Snippet", "value": "DO-NOT-INGEST-BODY"},
                        ]
                    },
                    "snippet": "secret body text",
                },
            )

    monkeypatch.setattr(adapters, "_make_client", lambda timeout=10.0: _FakeClient())
    adapter = registry.get("mail")
    listed = await adapter.act(
        action="mail.list",
        args={"limit": 10},
        token="gmail-access-token",
        scopes=["mail:read"],
        config={"provider": "google"},
    )
    assert listed["ok"] is True
    assert listed["mode"] == "google"
    assert listed["items"][0]["subject"] == "Invoice Q3"
    assert listed["items"][0]["sender"] == "billing@example.com"
    assert "secret body text" not in str(listed)
    assert "DO-NOT-INGEST-BODY" not in str(listed)

    sent = await adapter.act(
        action="mail.send",
        args={"to": "a@b.com", "subject": "Hi", "body": "nope", "confirm": True},
        token="gmail-access-token",
        scopes=["mail:act"],
        config={"provider": "google"},
    )
    assert sent["ok"] is False
    assert sent["error"] == "gmail_send_not_enabled"


def test_gmail_oauth_stays_off_the_calendar_grant() -> None:
    from app.integrations import oauth

    calendar = oauth.provider_for("calendar")
    mail = oauth.provider_for("mail")
    assert calendar is not None
    assert mail is not None
    assert "https://www.googleapis.com/auth/gmail.readonly" not in calendar.scopes
    assert "https://www.googleapis.com/auth/calendar.readonly" not in mail.scopes
    assert "https://www.googleapis.com/auth/gmail.readonly" in mail.scopes
    assert mail.api_base.startswith("https://gmail.googleapis.com/")

