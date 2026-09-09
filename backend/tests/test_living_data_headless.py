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
    wa_reply = life_success_reply(
        {"to": "+15551212", "channel": "whatsapp", "opened": True, "sent": False},
        tool_name="send_message",
    )
    assert "WhatsApp" in wa_reply
    assert "Sent to" not in wa_reply

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
    mail_list = source.split('case "mail.list":', 1)[1].split('case "mail.send":', 1)[0]
    mail_send = source.split('case "mail.send":', 1)[1].split('case "call.place":', 1)[0]
    wa_block = source.split('case "whatsapp.send":', 1)[1].split('case "mail.list":', 1)[0]
    assert "openURLHeadless" in wa_block
    assert "NSWorkspace.shared.open(url)" not in send_block
    assert "NSWorkspace.shared.open(url)" not in mail_send
    assert "NSWorkspace.shared.open(url)" not in wa_block
    assert 'tell application "Messages" to launch' in source
    assert "listMail" in mail_list
    assert 'tell application "Mail"' not in mail_list
    assert "launchBundleHeadless" not in mail_list
    assert "Envelope Index" in source
    assert "launchBundleHeadless" in mail_send
    assert "quitBundle" in mail_send
    assert "focus_stolen" in source
    assert "system_call_ui" in source


def test_mail_list_reads_envelope_index_without_opening_mail() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "macos" / "Sources" / "EVLifeHelper" / "main.swift").read_text(
        encoding="utf-8"
    )
    mail_list = source.split('case "mail.list":', 1)[1].split('case "mail.send":', 1)[0]
    assert "listMail" in mail_list
    assert 'tell application "Mail" to launch' not in source.split(
        'case "mail.list":', 1
    )[1].split('case "mail.send":', 1)[0]
    assert "com.apple.mail" not in mail_list


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
    daemon.last_whatsapp_pk = 9
    daemon.last_call_pk = 7
    daemon.last_photo_pk = 3
    daemon._contact_fps = {"c-1": "c-1|Bruce||"}
    daemon._mail_fps = {"Lunch|Alex|today": "1"}
    daemon._calendar_fps = {"ev-standup": "1"}
    daemon._health_fps = {"snap-1": "1"}
    daemon._last_account_pull = 12.5
    daemon.save_cursor()
    other = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    other.attach_cursor(cursor)
    assert other.last_message_rowid == 42
    assert other.last_whatsapp_pk == 9
    assert other.last_call_pk == 7
    assert other.last_photo_pk == 3
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
    assert select_tool("any new WhatsApp messages").selected == "list_messages"
    assert select_tool("What's Rahul's email?").selected == "resolve_contact"
    assert select_tool("Did I get any email from Alex?").selected == "list_mail"
    assert select_tool("What did Alex text me?").selected == "recall_history"
    assert select_tool("Is Alex in my contacts?").selected == "recall_history"
    assert select_tool("Who is Alex in my contacts?").selected == "recall_history"
    assert select_tool("who is Maya?").selected == "get_person"
    assert resolve_live_action("who is Maya?") != (
        "recall",
        {"query": "who is Maya?"},
    )
    assert resolve_live_action("Is Alex in my contacts?") == (
        "recall",
        {"query": "Is Alex in my contacts?"},
    )
    assert select_tool("What's Rahul's number?").selected == "resolve_contact"
    assert resolve_live_action("What's Rahul's number?") == (
        "resolve_contact",
        {"name": "Rahul"},
    )
    assert select_tool("any new mail").selected == "list_mail"
    assert resolve_live_action("check my inbox") == (
        "list_mail",
        {"query": "check my inbox"},
    )
    assert resolve_live_action("Who texted me?") == (
        "list_messages",
        {"query": "Who texted me?"},
    )
    assert resolve_live_action("Did I get any email from Alex?") == (
        "list_mail",
        {"query": "Did I get any email from Alex?"},
    )
    assert resolve_live_action("Do I get any email from Alex?") == (
        "list_mail",
        {"query": "Do I get any email from Alex?"},
    )
    assert resolve_live_action("What did Alex text me?")[0] == "recall"
    assert select_tool("close Messages").selected == "close_app"
    assert resolve_live_action("close Messages") == ("close_app", {"name": "Messages"})
    assert select_tool("How did I sleep?").selected == "get_health_trends"
    assert select_tool("What's in my health history?").selected == "recall_history"
    assert select_tool("What was on my old calendar?").selected == "recall_history"
    assert select_tool("Who called me?").selected == "recall_history"
    assert select_tool("What did Alex say on WhatsApp?").selected == "recall_history"
    assert select_tool("any new WhatsApp messages").selected == "list_messages"
    assert select_tool("Is there any new notification for me in WhatsApp?").selected == "list_messages"
    assert select_tool("any new notifications").selected == "recall_history"
    assert select_tool("Call Ned").selected == "place_call"
    assert resolve_live_action("Who called me?")[0] == "recall"
    assert resolve_live_action("Call Ned") == ("place_call", {"name": "Ned"})
    assert resolve_live_action("close Messages") == ("close_app", {"name": "Messages"})


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


@pytest.mark.asyncio
async def test_whatsapp_and_call_history_follow_then_recall(db_session: AsyncSession) -> None:
    """Local WhatsApp + CallHistory copies ingest as live life, only when asked."""
    from app.memory.extraction import Extractor
    from app.memory.life_archive.locate import locate_archive
    from app.memory.live_life import is_live_life_event
    from app.memory.retrieval import Retriever

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as wa_file:
        wa_path = wa_file.name
    with tempfile.NamedTemporaryFile(suffix=".storedata", delete=False) as call_file:
        call_path = call_file.name
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as photo_file:
        photo_path = photo_file.name
    try:
        wa = sqlite3.connect(wa_path)
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
            INSERT INTO ZWACHATSESSION VALUES (1, 'Alex', 'alex@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE VALUES (1, 750000000.0, 'see you at the gate', 0, 'alex@s.whatsapp.net', '', 'Alex', 1);
            INSERT INTO ZWAMESSAGE VALUES (2, 750000060.0, 'on my way', 1, '', 'alex@s.whatsapp.net', '', 1);
            """
        )
        wa.commit()
        wa.close()

        calls = sqlite3.connect(call_path)
        calls.executescript(
            """
            CREATE TABLE ZCALLRECORD (
                Z_PK INTEGER PRIMARY KEY,
                ZDATE REAL,
                ZDURATION INTEGER,
                ZADDRESS TEXT,
                ZNAME TEXT,
                ZORIGINATED INTEGER,
                ZANSWERED INTEGER,
                ZCALLTYPE INTEGER
            );
            INSERT INTO ZCALLRECORD VALUES (1, 750000000.0, 0, '+15550100', 'Alex', 0, 0, 1);
            INSERT INTO ZCALLRECORD VALUES (2, 750000100.0, 42, '+15550100', 'Alex', 1, 1, 1);
            """
        )
        calls.commit()
        calls.close()

        photos = sqlite3.connect(photo_path)
        photos.executescript(
            """
            CREATE TABLE ZASSET (
                Z_PK INTEGER PRIMARY KEY,
                ZFILENAME TEXT,
                ZDATECREATED REAL,
                ZTRASHEDSTATE INTEGER
            );
            INSERT INTO ZASSET VALUES (1, 'IMG_1001.HEIC', 750000000.0, 0);
            INSERT INTO ZASSET VALUES (2, 'trashed.jpg', 750000010.0, 1);
            """
        )
        photos.commit()
        photos.close()

        daemon = LifeStreamDaemon(
            chat_db_path="/nonexistent/chat.db",
            whatsapp_db_path=wa_path,
            call_db_path=call_path,
            photos_db_path=photo_path,
        )
        wa_events = await daemon.sync_whatsapp(db_session, limit=10)
        assert len(wa_events) == 2
        assert daemon.last_whatsapp_pk == 2
        assert wa_events[0].source == "whatsapp"
        assert wa_events[0].event_type == "message.whatsapp.received"
        assert wa_events[0].content["text"] == "see you at the gate"
        assert wa_events[0].privacy_level == "sensitive"
        assert is_live_life_event(wa_events[0])
        assert Extractor().extract(wa_events[0]) == []
        assert await daemon.sync_whatsapp(db_session, limit=10) == []

        call_events = await daemon.sync_calls(db_session, limit=10)
        assert len(call_events) == 2
        assert daemon.last_call_pk == 2
        assert call_events[0].source == "calls"
        assert "missed incoming call Alex" in call_events[0].content["text"]
        assert is_live_life_event(call_events[0])
        assert Extractor().extract(call_events[0]) == []
        assert await daemon.sync_calls(db_session, limit=10) == []

        photo_events = await daemon.sync_photos(db_session, limit=10)
        assert len(photo_events) == 1
        assert daemon.last_photo_pk == 1
        assert photo_events[0].content["filename"] == "IMG_1001.HEIC"
        assert "Pictures" not in str(photo_events[0].content)
        assert await daemon.sync_photos(db_session, limit=10) == []

        wa = sqlite3.connect(wa_path)
        wa.execute(
            "INSERT INTO ZWAMESSAGE VALUES (3, 750000120.0, 'bring the charger', 0, 'alex@s.whatsapp.net', '', 'Alex', 1)"
        )
        wa.commit()
        wa.close()
        more = await daemon.sync_whatsapp(db_session, limit=10)
        assert len(more) == 1
        assert more[0].content["text"] == "bring the charger"

        chats = await locate_archive(db_session, "What did Alex say on WhatsApp?")
        assert any("see you at the gate" in (hit.get("text") or "") for hit in chats)
        assert all(hit.get("kind") == "live_life" for hit in chats)

        history = await locate_archive(db_session, "Who called me?")
        assert any("Alex" in (hit.get("text") or "") for hit in history)
        assert all(hit.get("kind") == "live_life" for hit in history)

        pics = await locate_archive(db_session, "what photos do I have")
        assert any("IMG_1001.HEIC" in (hit.get("text") or "") for hit in pics)

        leaked = await Retriever(db_session).search_events("gate charger Alex", k=10, access="model")
        assert leaked == []
        assert await locate_archive(db_session, "hey") == []
    finally:
        for path in (wa_path, call_path, photo_path):
            if os.path.exists(path):
                os.unlink(path)


def test_peek_mac_life_stays_off_in_ci() -> None:
    from app.memory.live_life import peek_mac_life
    from app.services.life_stream_daemon import life_stream_should_run

    assert life_stream_should_run() is False
    assert peek_mac_life("any new WhatsApp", shelf="chats", tokens=["alex"]) == []
    assert peek_mac_life("Is Alex in my contacts?", shelf="contacts") == []
    assert peek_mac_life("any new email", shelf="mail") == []


@pytest.mark.asyncio
async def test_ask_reads_live_whatsapp_not_only_ingest(db_session: AsyncSession) -> None:
    """Asking about WhatsApp reads ChatStorage now, even with no ingested events."""
    from app.memory.life_archive.locate import locate_archive
    from app.memory.live_life import peek_mac_life

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as wa_file:
        wa_path = wa_file.name
    try:
        wa = sqlite3.connect(wa_path)
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
            INSERT INTO ZWACHATSESSION VALUES (1, 'Alex', 'alex@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE VALUES (1, 750000000.0, 'live gate ping', 0, 'alex@s.whatsapp.net', '', 'Alex', 1);
            """
        )
        wa.commit()
        wa.close()
        daemon = LifeStreamDaemon(
            chat_db_path="/nonexistent/chat.db",
            whatsapp_db_path=wa_path,
            call_db_path="",
            photos_db_path="",
        )
        hits = daemon.peek_whatsapp(tokens=["alex"], limit=8)
        assert hits
        assert hits[0]["kind"] == "live_mac"
        assert "live gate ping" in hits[0]["text"]
        assert peek_mac_life(
            "What did Alex say on WhatsApp?",
            shelf="chats",
            tokens=["alex"],
            daemon=daemon,
        )
        from app.memory import live_life as live_mod

        original = live_mod.peek_mac_life
        fixture = daemon

        def forced_peek(query, *, shelf, tokens=None, k=8, daemon=None):
            del daemon
            return original(query, shelf=shelf, tokens=tokens, k=k, daemon=fixture)

        live_mod.peek_mac_life = forced_peek
        try:
            found = await locate_archive(db_session, "What did Alex say on WhatsApp?")
            assert any("live gate ping" in (hit.get("text") or "") for hit in found)
            assert any(hit.get("kind") == "live_mac" for hit in found)
            notices = await locate_archive(
                db_session, "Is there any new notification for me in WhatsApp?"
            )
            assert any("live gate ping" in (hit.get("text") or "") for hit in notices)
        finally:
            live_mod.peek_mac_life = original
    finally:
        if os.path.exists(wa_path):
            os.unlink(wa_path)


def test_whatsapp_notification_ask_does_not_require_the_word_notification() -> None:
    from app.filter.output_filter import APP_CHECK_HEDGE_RE, HONEST_LIVE_APP, _apply_wave_life_policy
    from app.memory.life_archive.locate import _CHAT_ASK_WEAK, classify_shelf, locate_tokens
    from app.memory.recall import _spoken_from_evidence

    query = "Is there any new notification for me in WhatsApp?"
    assert classify_shelf(query) == "chats"
    leftover = [token for token in locate_tokens(query) if token not in _CHAT_ASK_WEAK]
    assert leftover == []
    spoken = _spoken_from_evidence(
        [
            {
                "text": "Alex: live gate ping",
                "kind": "live_mac",
                "memory_type": "message.whatsapp.received",
            }
        ],
        query,
    )
    assert "live gate ping" in spoken.lower()
    assert "whatsapp web" not in spoken.lower()
    assert APP_CHECK_HEDGE_RE.search("You can just open WhatsApp Web to see your notifications.")
    rewritten, persona, _flags = _apply_wave_life_policy(
        "You can just open WhatsApp Web to see your notifications."
    )
    assert rewritten == HONEST_LIVE_APP
    assert persona.get("app_check_hedge_rewritten")
    overview = _spoken_from_evidence(
        [
            {
                "memory_type": "life.chat.thread",
                "text": "WhatsApp thread: Alex. 12 messages (2026).",
            }
        ],
        query,
    )
    assert "live gate ping" not in overview.lower()
    assert "whatsapp web" not in overview.lower()
    assert "i don't see new whatsapp" in overview.lower()
    calls = _spoken_from_evidence(
        [
            {
                "text": "missed incoming call Alex",
                "kind": "live_mac",
                "memory_type": "call.history.recorded",
            }
        ],
        "Who called me?",
    )
    assert "alex" in calls.lower()
    assert "whatsapp web" not in calls.lower()
    photos = _spoken_from_evidence(
        [
            {
                "text": "IMG_1001.HEIC",
                "kind": "live_mac",
                "memory_type": "photo.library.indexed",
            }
        ],
        "any new photos",
    )
    assert "img_1001.heic" in photos.lower()
    rewritten_manual, persona_manual, _flags_manual = _apply_wave_life_policy(
        "You can just manually open WhatsApp to see that."
    )
    assert rewritten_manual == HONEST_LIVE_APP
    assert persona_manual.get("app_check_hedge_rewritten")


@pytest.mark.asyncio
async def test_live_now_asks_read_whatsapp_calls_and_photos(
    db_session: AsyncSession,
) -> None:
    """Notification asks read the live Mac copies for every connected stream."""
    from app.memory.life_archive.locate import classify_shelf, is_live_now_ask, locate_archive
    from app.memory.live_life import peek_mac_life

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as wa_file:
        wa_path = wa_file.name
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as call_file:
        call_path = call_file.name
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as photo_file:
        photo_path = photo_file.name
    try:
        wa = sqlite3.connect(wa_path)
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
            INSERT INTO ZWACHATSESSION VALUES (1, 'Alex', 'alex@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE VALUES (1, 750000000.0, 'live gate ping', 0, 'alex@s.whatsapp.net', '', 'Alex', 1);
            """
        )
        wa.commit()
        wa.close()
        calls = sqlite3.connect(call_path)
        calls.executescript(
            """
            CREATE TABLE ZCALLRECORD (
                Z_PK INTEGER PRIMARY KEY,
                ZDATE REAL,
                ZDURATION REAL,
                ZADDRESS TEXT,
                ZNAME TEXT,
                ZORIGINATED INTEGER,
                ZANSWERED INTEGER,
                ZCALLTYPE INTEGER
            );
            INSERT INTO ZCALLRECORD VALUES (1, 750000000.0, 0, '+15550100', 'Alex', 0, 0, 1);
            """
        )
        calls.commit()
        calls.close()
        photos = sqlite3.connect(photo_path)
        photos.executescript(
            """
            CREATE TABLE ZASSET (
                Z_PK INTEGER PRIMARY KEY,
                ZFILENAME TEXT,
                ZDATECREATED REAL,
                ZTRASHEDSTATE INTEGER
            );
            INSERT INTO ZASSET VALUES (1, 'IMG_1001.HEIC', 750000000.0, 0);
            """
        )
        photos.commit()
        photos.close()
        daemon = LifeStreamDaemon(
            chat_db_path="/nonexistent/chat.db",
            whatsapp_db_path=wa_path,
            call_db_path=call_path,
            photos_db_path=photo_path,
        )
        assert classify_shelf("any new notifications") == "inbox"
        assert is_live_now_ask("any new notifications")
        assert is_live_now_ask("Who called me?")
        assert is_live_now_ask("any new photos")
        from app.memory import live_life as live_mod

        original = live_mod.peek_mac_life
        fixture = daemon

        def forced_peek(query, *, shelf, tokens=None, k=8, daemon=None):
            del daemon
            return original(query, shelf=shelf, tokens=tokens, k=k, daemon=fixture)

        live_mod.peek_mac_life = forced_peek
        try:
            assert peek_mac_life(
                "any new notifications",
                shelf="inbox",
                tokens=[],
                daemon=daemon,
            )
            notices = await locate_archive(db_session, "any new notifications")
            assert any("live gate ping" in (hit.get("text") or "") for hit in notices)
            assert any("Alex" in (hit.get("text") or "") and "call" in (hit.get("text") or "").lower() for hit in notices)
            history = await locate_archive(db_session, "Who called me?")
            assert any("Alex" in (hit.get("text") or "") for hit in history)
            pics = await locate_archive(db_session, "any new photos")
            assert any("IMG_1001.HEIC" in (hit.get("text") or "") for hit in pics)
        finally:
            live_mod.peek_mac_life = original
    finally:
        for path in (wa_path, call_path, photo_path):
            if os.path.exists(path):
                os.unlink(path)


@pytest.mark.asyncio
async def test_live_contacts_and_mail_read_now(db_session: AsyncSession) -> None:
    """Contacts and mail asks read the live Mac copies, not only ingested events."""
    from app.memory.life_archive.locate import classify_shelf, locate_archive
    from app.memory.live_life import peek_account_life
    from app.memory.recall import _spoken_from_evidence

    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    contacts = daemon.peek_contacts(
        [{"id": "c1", "full_name": "Alex", "phone_numbers": ["+15550100"]}],
        tokens=["alex"],
        limit=8,
    )
    assert contacts
    assert contacts[0]["kind"] == "live_mac"
    assert "Alex" in contacts[0]["text"]
    mail = daemon.peek_mail(
        [
            {
                "subject": "Lunch tomorrow",
                "sender": "Alex",
                "received": "2026-09-05T10:00:00+00:00",
            }
        ],
        tokens=["alex"],
        limit=8,
    )
    assert mail
    assert "Lunch tomorrow" in mail[0]["text"]
    assert classify_shelf("Is Alex in my contacts?") == "contacts"
    assert classify_shelf("any new email") == "mail"
    spoken = _spoken_from_evidence(contacts, "Is Alex in my contacts?")
    assert "alex" in spoken.lower()
    spoken_mail = _spoken_from_evidence(mail, "Did I get any email from Alex?")
    assert "lunch tomorrow" in spoken_mail.lower()
    from app.memory import live_life as live_mod

    original_mac = live_mod.peek_mac_life
    original_account = live_mod.peek_account_life

    async def forced_account(query, *, shelf, tokens=None, k=8, **_kwargs):
        del query
        if shelf == "contacts":
            return daemon.peek_contacts(
                [{"id": "c1", "full_name": "Alex", "phone_numbers": ["+15550100"]}],
                tokens=tokens,
                limit=k,
            )
        if shelf == "mail":
            return daemon.peek_mail(
                [
                    {
                        "subject": "Lunch tomorrow",
                        "sender": "Alex",
                        "received": "2026-09-05T10:00:00+00:00",
                    }
                ],
                tokens=tokens,
                limit=k,
            )
        return []

    def forced_peek(query, *, shelf, tokens=None, k=8, daemon=None):
        del daemon
        if shelf in {"contacts", "mail"}:
            return []
        return original_mac(query, shelf=shelf, tokens=tokens, k=k, daemon=None)

    live_mod.peek_account_life = forced_account
    live_mod.peek_mac_life = forced_peek
    try:
        found = await locate_archive(db_session, "Is Alex in my contacts?")
        assert any("Alex" in (hit.get("text") or "") for hit in found)
        inbox = await locate_archive(db_session, "Did I get any email from Alex?")
        assert any("Lunch tomorrow" in (hit.get("text") or "") for hit in inbox)
    finally:
        live_mod.peek_account_life = original_account
        live_mod.peek_mac_life = original_mac
    assert await peek_account_life("Is Alex in my contacts?", shelf="contacts") == []


@pytest.mark.asyncio
async def test_list_mail_uses_mac_copy_when_mail_bridge_missing(monkeypatch) -> None:
    """Inbox asks must not die just because Gmail/mail adapter is not installed."""
    from app.ev.tools import _mac_hub_life_read

    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    hits = daemon.peek_mail(
        [
            {
                "subject": "Lunch tomorrow",
                "sender": "Alex",
                "received": "2026-09-05T10:00:00+00:00",
            }
        ]
    )

    def forced_peek(query, *, shelf, tokens=None, k=8, daemon=None):
        del query, tokens, k, daemon
        return hits if shelf == "mail" else []

    monkeypatch.setattr(
        "app.services.life_stream_daemon.life_stream_should_run", lambda: True
    )
    monkeypatch.setattr(
        "app.services.life_stream_daemon.get_life_stream_daemon", lambda: daemon
    )
    monkeypatch.setattr("app.memory.live_life.peek_mac_life", forced_peek)

    result = await _mac_hub_life_read("list_mail", {"limit": 5})
    assert result is not None
    assert result["ok"] is True
    assert result["count"] >= 1
    assert "lunch tomorrow" in str(result.get("spoken") or "").lower()
    assert "whatsapp web" not in str(result.get("spoken") or "").lower()


@pytest.mark.asyncio
async def test_list_mail_dispatch_reads_mac_without_mail_adapter(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Talk must not stop at 'Mail is not connected' when this Mac has envelopes."""
    from app.ev.tools import dispatch

    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    hits = daemon.peek_mail(
        [
            {
                "subject": "Lunch tomorrow",
                "sender": "Alex",
                "received": "2026-09-05T10:00:00+00:00",
            }
        ]
    )

    def forced_peek(query, *, shelf, tokens=None, k=8, daemon=None):
        del query, tokens, k, daemon
        return hits if shelf == "mail" else []

    monkeypatch.setattr(
        "app.services.life_stream_daemon.life_stream_should_run", lambda: True
    )
    monkeypatch.setattr(
        "app.services.life_stream_daemon.get_life_stream_daemon", lambda: daemon
    )
    monkeypatch.setattr("app.memory.live_life.peek_mac_life", forced_peek)

    response = await dispatch(db_session, "list_mail", {"limit": 5}, actor="master")
    body = response.result or {}
    assert body.get("error") != "not_connected"
    assert body.get("ok") is True
    assert body.get("count") >= 1
    assert "lunch tomorrow" in str(body.get("spoken") or "").lower()


@pytest.mark.asyncio
async def test_resolve_contact_dispatch_reads_mac_without_contacts_adapter(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Name lookup must read CNContactStore/cache, not require a contacts adapter."""
    from app.ev.tools import dispatch

    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    daemon.remember_contacts(
        [{"full_name": "Alex Rivera", "phone_numbers": ["+15550100"]}]
    )
    hits = daemon.peek_contacts(daemon._cached_contacts, tokens=["alex"], limit=8)

    def forced_peek(query, *, shelf, tokens=None, k=8, daemon=None):
        del query, tokens, k, daemon
        return hits if shelf == "contacts" else []

    monkeypatch.setattr(
        "app.services.life_stream_daemon.life_stream_should_run", lambda: True
    )
    monkeypatch.setattr(
        "app.services.life_stream_daemon.get_life_stream_daemon", lambda: daemon
    )
    monkeypatch.setattr("app.memory.live_life.peek_mac_life", forced_peek)

    response = await dispatch(
        db_session, "resolve_contact", {"name": "Alex"}, actor="master"
    )
    body = response.result or {}
    assert body.get("error") != "not_connected"
    assert body.get("ok") is True
    assert body.get("count") >= 1
    assert "alex" in str(body.get("spoken") or "").lower()


def test_peek_mac_life_keeps_imessage_and_whatsapp_apart() -> None:
    """A messages ask must not leak WhatsApp lines, and the reverse."""
    from app.memory.live_life import peek_mac_life

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as wa_file:
        wa_path = wa_file.name
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as chat_file:
        chat_path = chat_file.name
    try:
        wa = sqlite3.connect(wa_path)
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
            INSERT INTO ZWACHATSESSION VALUES (1, 'Alex', 'alex@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE VALUES (1, 750000000.0, 'whatsapp only ping', 0, 'alex@s.whatsapp.net', '', 'Alex', 1);
            """
        )
        wa.commit()
        wa.close()
        chat = sqlite3.connect(chat_path)
        chat.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                date INTEGER,
                text TEXT,
                handle_id INTEGER,
                is_from_me INTEGER
            );
            INSERT INTO handle VALUES (1, '+15550100');
            INSERT INTO message VALUES (1, 750000000000000000, 'imessage only ping', 1, 0);
            """
        )
        chat.commit()
        chat.close()
        daemon = LifeStreamDaemon(chat_db_path=chat_path, whatsapp_db_path=wa_path)
        texts = peek_mac_life("who texted me", shelf="chats", tokens=[], k=8, daemon=daemon)
        wa_hits = peek_mac_life(
            "any new WhatsApp messages", shelf="chats", tokens=[], k=8, daemon=daemon
        )
        mixed = peek_mac_life("recent messages", shelf="chats", tokens=[], k=8, daemon=daemon)
        assert any("imessage only ping" in (hit.get("text") or "") for hit in texts)
        assert all("whatsapp only ping" not in (hit.get("text") or "") for hit in texts)
        assert any("whatsapp only ping" in (hit.get("text") or "") for hit in wa_hits)
        assert all("imessage only ping" not in (hit.get("text") or "") for hit in wa_hits)
        assert {hit.get("channel") for hit in texts} == {"imessage"}
        assert {hit.get("channel") for hit in wa_hits} == {"whatsapp"}
        assert {hit.get("channel") for hit in mixed} == {"imessage", "whatsapp"}
    finally:
        os.unlink(wa_path)
        os.unlink(chat_path)


def test_notification_asks_route_to_recall_not_files_or_memory_search() -> None:
    from app.ev.tool_select import resolve_live_action, select_tool

    # The desk parser treats any phrase as a note name when Desktop notes
    # exist — notification asks must reach the live inbox shelf instead.
    assert select_tool("any new notifications").selected == "recall_history"
    assert resolve_live_action("any new notifications") == (
        "recall",
        {"query": "any new notifications"},
    )
    assert select_tool("what did I miss").selected == "recall_history"
    assert resolve_live_action("what did I miss") == (
        "recall",
        {"query": "what did I miss"},
    )
    assert select_tool("catch me up on everything").selected == "recall_history"
    assert select_tool("up to speed on whatsapp").selected == "list_messages"
    assert select_tool("did I miss anything on whatsapp").selected == "list_messages"


def test_recent_reads_beat_desk_file_hijack(monkeypatch) -> None:
    """Desktop notes must not steal live message/mail reads into computer."""
    from app.ev.tool_select import select_tool

    monkeypatch.setattr(
        "app.ev.laptop_files.looks_like_file_task",
        lambda *_args, **_kwargs: True,
    )
    assert select_tool("recent messages").selected == "list_messages"
    assert select_tool("recent mails").selected == "list_mail"
    assert select_tool("what are my recent mails").selected == "list_mail"
    assert select_tool("recent whatsapp").selected == "list_messages"
    assert select_tool("any new notifications").selected == "recall_history"
    # A real file job still routes to computer.
    assert select_tool("read the packing.md file").selected == "computer"


def test_imessage_digest_demotes_transactional_sms() -> None:
    from app.services.life_stream_daemon import _is_transactional_sms

    assert _is_transactional_sms("Your OTP is 147166.", "INVCPP-T") is True
    assert _is_transactional_sms("Want to know number of SIMs in your name?", "ISATHI-G") is True
    assert _is_transactional_sms("Lien of Rs 36896 removed.", "ICICIT-S") is True
    assert _is_transactional_sms("x", "HDFCBK") is True
    assert _is_transactional_sms("x", "56767") is True
    assert _is_transactional_sms("Your OTP is 147166", "+919876543210") is True
    # Mixed-case DLT headers as Apple Messages stores them.
    assert _is_transactional_sms("Hi! Kindly make your Vi postpaid bill payments.", "ViCARE-S") is True
    assert _is_transactional_sms("bill due", "ViBill-S") is True
    assert _is_transactional_sms("voter details", "ECIsms-G") is True
    assert _is_transactional_sms("x", "JD-ANKIAB") is True
    assert _is_transactional_sms("￾", "koth49@botplatform.sarcs.jio") is True
    assert _is_transactional_sms(
        "Dear Customer, You have a missed call from +919428772945",
        "+919428772945",
    ) is True
    assert _is_transactional_sms("evie-pair.dt-abc", "patelsahaj01@gmail.com") is True
    # Real people are never transactional.
    assert _is_transactional_sms("see you at the gate", "+919876543210") is False
    assert _is_transactional_sms("on my way", "alex@example.com") is False
    assert _is_transactional_sms("lets meet at 3", "Alex") is False
    assert _is_transactional_sms("are we still on for lunch", "Mary-Jane") is False
    # Accepted tradeoff: OTP-shaped bodies demote even from people/names.
    # Digest order only — token searches ("what was my OTP") still find them.
    assert _is_transactional_sms("door code is 4410", "Mary-Jane") is True


def test_mail_digest_ranks_by_mailbox_shape_not_brands() -> None:
    from app.services.life_stream_daemon import _is_bulk_mail

    # Role mailbox / mail-roll host / "Name from Brand" display — any domain.
    assert _is_bulk_mail("Sahaj Patel <notifications@github.com>", "Run failed") is True
    assert _is_bulk_mail("Alerts <notifications@us.example.net>", "New Jobs") is True
    assert _is_bulk_mail("noreply@example.com", "verify") is True
    assert _is_bulk_mail("Ollama <hello@ollama.com>", "Team plan") is True
    assert _is_bulk_mail("Eric <eric@updates.firecrawl.dev>", "pricing") is True
    assert _is_bulk_mail("Kai from Newsroom <kai@example.com>", "digest") is True
    # A person mailbox is not bulk just because the domain is unfamiliar.
    assert _is_bulk_mail("Ada <ada@example.com>", "lunch?") is False
    assert _is_bulk_mail("Seb <seb@substack.com>", "week") is False


def test_mail_digest_drops_notification_firehose_when_personal_exists() -> None:
    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    items = [
        {
            "subject": "Run failed: CI",
            "sender": "Sahaj Patel <notifications@github.com>",
            "received": "2026-09-09T12:00:00+00:00",
        },
        {
            "subject": "Lunch tomorrow",
            "sender": "Alex <alex@example.com>",
            "received": "2026-09-09T11:00:00+00:00",
        },
        {
            "subject": "New Jobs at Anthropic",
            "sender": "MyGreenhouse <notifications@us.greenhouse-jobs.com>",
            "received": "2026-09-09T10:00:00+00:00",
        },
    ]
    digest = daemon.peek_mail(items, query="recent mails", limit=8)
    senders = [str(hit.get("sender") or "") for hit in digest]
    assert any("Alex" in sender for sender in senders)
    assert all("notifications@" not in sender.lower() for sender in senders)
    github = daemon.peek_mail(items, query="any mail about github", limit=8)
    assert any("github" in str(hit.get("sender") or "").lower() for hit in github)


def test_mail_digest_puts_address_book_people_ahead_of_strangers() -> None:
    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    daemon._cached_contacts = [
        {"full_name": "Ada Lovelace", "email": "ada@example.com"}
    ]
    items = [
        {
            "subject": "Stock up",
            "sender": "Shop <shop@store.example>",
            "received": "2026-09-09T12:00:00+00:00",
        },
        {
            "subject": "Lunch tomorrow",
            "sender": "Ada Lovelace <ada@example.com>",
            "received": "2026-09-09T11:00:00+00:00",
        },
    ]
    digest = daemon.peek_mail(items, query="recent mails", limit=8)
    senders = [str(hit.get("sender") or "") for hit in digest]
    assert senders
    assert "Ada Lovelace" in senders[0]


def test_imessage_digest_surfaces_people_through_dlt_flood() -> None:
    """A 48-row DLT flood must not hide the last real person in the digest."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                date INTEGER,
                text TEXT,
                handle_id INTEGER,
                is_from_me INTEGER
            );
            INSERT INTO handle VALUES (1, '+15551234567');
            INSERT INTO handle VALUES (2, 'ViCARE-S');
            INSERT INTO message VALUES (1, 750000000000000000, 'see you at the gate', 1, 0);
            """
        )
        for i in range(2, 62):
            conn.execute(
                "INSERT INTO message VALUES (?, 750000100000000000, 'bill payment reminder', 2, 0)",
                (i,),
            )
        conn.commit()
        conn.close()
        daemon = LifeStreamDaemon(chat_db_path=db_path)
        digest = daemon.peek_imessage(limit=3)
        handles = [str(hit.get("handle") or "") for hit in digest]
        texts = [str(hit.get("preview") or "") for hit in digest]
        assert "+15551234567" in handles
        assert any("see you at the gate" in text for text in texts)
        assert handles[0] == "+15551234567"
        assert "ViCARE-S" not in handles
    finally:
        os.unlink(db_path)


def test_whatsapp_digest_prefers_named_chats_over_ads_and_channels() -> None:
    """Unsaved phone ads and Channels newsletters must not headline recents."""
    from app.services.life_stream_daemon import (
        _is_unnamed_phone_handle,
        _is_whatsapp_broadcast_channel,
    )

    assert _is_unnamed_phone_handle("\u202a+91\xa081988\xa045888\u202c") is True
    assert _is_unnamed_phone_handle("Mansi…!!") is False
    assert _is_unnamed_phone_handle("Job") is False
    assert _is_whatsapp_broadcast_channel(5, "120363158322445192@newsletter") is True
    assert _is_whatsapp_broadcast_channel(0, "alex@s.whatsapp.net") is False

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        wa_path = f.name
    try:
        wa = sqlite3.connect(wa_path)
        wa.executescript(
            """
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZPARTNERNAME TEXT,
                ZCONTACTJID TEXT,
                ZSESSIONTYPE INTEGER
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
            INSERT INTO ZWACHATSESSION VALUES (1, '+918198845888', '181875339432050@lid', 0);
            INSERT INTO ZWACHATSESSION VALUES (2, 'Mansi', 'mansi@s.whatsapp.net', 0);
            INSERT INTO ZWACHATSESSION VALUES (3, 'DS ML', '120363158322445192@newsletter', 5);
            INSERT INTO ZWAMESSAGE VALUES (
                1, 750000120.0, '*Full-sleeve polos*', 0, '181875339432050@lid', '', '', 1
            );
            INSERT INTO ZWAMESSAGE VALUES (
                2, 750000100.0, 'on my way', 0, 'mansi@s.whatsapp.net', '', 'Mansi', 2
            );
            INSERT INTO ZWAMESSAGE VALUES (
                3, 750000110.0, 'roadmap blast', 0, '120363158322445192@newsletter', '', 'DS ML', 3
            );
            """
        )
        wa.commit()
        wa.close()
        daemon = LifeStreamDaemon(
            chat_db_path="/nonexistent/chat.db",
            whatsapp_db_path=wa_path,
        )
        digest = daemon.peek_whatsapp(limit=8)
        handles = [str(hit.get("handle") or "") for hit in digest]
        texts = [str(hit.get("preview") or "") for hit in digest]
        assert handles == ["Mansi"]
        assert "on my way" in texts[0]
        assert all("polo" not in text.lower() for text in texts)
        assert all("roadmap" not in text.lower() for text in texts)
        named = daemon.peek_whatsapp(tokens=["mansi"], limit=8)
        assert any("on my way" in str(hit.get("preview") or "") for hit in named)
    finally:
        os.unlink(wa_path)


def test_digest_groups_latest_line_per_person() -> None:
    from app.services.life_stream_daemon import _group_per_person

    hits = [
        {"handle": "Mansi", "text": "Mansi: three"},
        {"handle": "Mansi", "text": "Mansi: two"},
        {"handle": "Alex", "text": "Alex: hi"},
        {"handle": "Mansi", "text": "Mansi: one"},
    ]
    grouped = _group_per_person(hits)
    assert [hit["text"] for hit in grouped] == ["Mansi: three", "Alex: hi"]


def test_mail_and_call_envelopes_are_never_chat_hits() -> None:
    from app.memory.message_speak import is_chat_hit

    # Mail subjects with colons ("Run failed: ...") must not smuggle
    # envelopes into chat digests.
    assert (
        is_chat_hit(
            {
                "memory_type": "mail.envelope.received",
                "text": "[org/repo] Run failed: CI - main",
                "channel": "mail",
            }
        )
        is False
    )
    assert (
        is_chat_hit(
            {"memory_type": "call.history.recorded", "text": "missed call", "channel": "calls"}
        )
        is False
    )
    assert (
        is_chat_hit(
            {
                "memory_type": "message.whatsapp.received",
                "text": "Alex: hi",
                "channel": "whatsapp",
            }
        )
        is True
    )


def test_peek_filter_matches_person_tokens_case_insensitively() -> None:
    from app.services.life_stream_daemon import _filter_peek

    rows = [
        {"text": "Mansi: see you at 3", "handle": "Mansi"},
        {"text": "Alex: on my way", "handle": "Alex"},
    ]
    # Capitalized caller tokens must match the lowercased blob.
    capped = _filter_peek(rows, ["Mansi"], 5)
    assert [hit["handle"] for hit in capped] == ["Mansi"]
    assert [hit["handle"] for hit in _filter_peek(rows, ["mansi"], 5)] == ["Mansi"]
    assert [hit["handle"] for hit in _filter_peek(rows, ["ALEX"], 5)] == ["Alex"]
    # Empty/blank tokens stay digest mode (no filtering).
    assert len(_filter_peek(rows, [], 5)) == 2
    assert len(_filter_peek(rows, ["  "], 5)) == 2


def test_mail_index_refresh_only_when_auto_resolved(
    monkeypatch, tmp_path
) -> None:
    from app.services import life_stream_daemon as daemon_mod
    from app.services.life_stream_daemon import LifeStreamDaemon

    fresh = tmp_path / "Envelope Index"
    fresh.write_bytes(b"x")
    monkeypatch.setattr(daemon_mod, "find_mail_envelope_index", lambda: str(fresh))
    # Explicit path (tests): never swapped for another file.
    explicit = LifeStreamDaemon(
        chat_db_path=str(tmp_path / "chat.db"),
        mail_index_path="/nonexistent/Envelope Index",
    )
    assert explicit.refresh_mail_index_path() == "/nonexistent/Envelope Index"
    # Auto hub with a stale path (Mail.app V-upgrade, rebuild): re-resolves.
    auto = LifeStreamDaemon()
    assert auto._mail_index_auto is True
    auto.mail_index_path = "/stale/Envelope Index"
    assert auto.refresh_mail_index_path() == str(fresh)
    assert auto.mail_index_path == str(fresh)
    # Readable path is left alone (no redundant scan).
    assert auto.refresh_mail_index_path() == str(fresh)


def test_inbox_digest_speaks_chats_mail_and_calls_together() -> None:
    from app.memory.recall import _spoken_from_evidence

    evidence = [
        {
            "text": "Alex: live gate ping",
            "kind": "live_mac",
            "memory_type": "message.whatsapp.received",
            "channel": "whatsapp",
            "handle": "Alex",
            "preview": "live gate ping",
        },
        {
            "text": "[org/repo] Run failed: CI - main from Sahaj",
            "kind": "live_mac",
            "memory_type": "mail.envelope.received",
            "channel": "mail",
            "subject": "[org/repo] Run failed: CI - main",
            "sender": "Sahaj",
        },
        {
            "text": "missed incoming call Mom",
            "kind": "live_mac",
            "memory_type": "call.history.recorded",
            "channel": "calls",
        },
    ]
    spoken = _spoken_from_evidence(evidence, "any new notifications")
    lowered = spoken.lower()
    # The chat digest must not swallow the mail/calls half of the inbox.
    assert "live gate ping" in lowered
    assert "run failed" in lowered
    assert "missed incoming call mom" in lowered
    # Pure-channel asks keep their single lead (no inbox mixing).
    wa_only = _spoken_from_evidence(evidence[:1], "what are my recent whatsapp messages")
    assert wa_only.lower().startswith("latest whatsapp:")
    assert "run failed" not in wa_only.lower()


def test_empty_messages_name_the_person_asked_about(monkeypatch) -> None:
    from app.memory import recall as recall_mod
    from app.memory.recall import _spoken_empty_connected

    # Sync state is a separate test; here the stores are readable.
    monkeypatch.setattr(recall_mod, "_freshness_diag", lambda kind: "")
    assert (
        _spoken_empty_connected("messages from Mansi")
        == "I don't see messages from Mansi on this Mac right now."
    )
    assert (
        _spoken_empty_connected("texts from mom")
        == "I don't see messages from mom on this Mac right now."
    )
    assert (
        _spoken_empty_connected("whatsapp from Mansi")
        == "I don't see WhatsApp from Mansi on this Mac right now."
    )
    # Digest asks keep the generic lines.
    assert (
        _spoken_empty_connected("any new messages")
        == "I don't see new messages on this Mac right now."
    )
    assert _spoken_empty_connected("what are my recent whatsapp messages") == (
        "I don't see new WhatsApp on this Mac right now. "
        "WhatsApp Desktop has to be logged in here."
    )


@pytest.mark.asyncio
async def test_list_messages_filters_person_and_falls_back_across_aisles(
    monkeypatch,
) -> None:
    import app.ev.spark_task as spark_task_mod
    from app.ev.spark_task import TaskDecision, clear_life_job
    from app.ev.tools import _mac_hub_life_read
    from app.services import life_stream_daemon as daemon_mod

    wa_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    wa_path = wa_file.name
    wa_file.close()
    chat_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    chat_path = chat_file.name
    chat_file.close()
    try:
        wa = sqlite3.connect(wa_path)
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
            INSERT INTO ZWACHATSESSION VALUES (1, 'Mansi', 'mansi@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE VALUES (1, 750000000.0, 'see you at 3', 0, 'mansi@s.whatsapp.net', '', 'Mansi', 1);
            """
        )
        wa.commit()
        wa.close()
        chat = sqlite3.connect(chat_path)
        chat.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                date INTEGER,
                text TEXT,
                handle_id INTEGER,
                is_from_me INTEGER
            );
            INSERT INTO handle VALUES (1, '+15550100');
            INSERT INTO message VALUES (1, 750000000000000000, 'imessage only ping', 1, 0);
            """
        )
        chat.commit()
        chat.close()
        daemon = LifeStreamDaemon(chat_db_path=chat_path, whatsapp_db_path=wa_path)
        monkeypatch.setattr(daemon_mod, "get_life_stream_daemon", lambda: daemon)
        monkeypatch.setattr(daemon_mod, "life_stream_should_run", lambda: True)

        async def _digest_decision(utterance, *, family_hint=""):
            return TaskDecision(family="messages", manner="digest", source="test")

        monkeypatch.setattr(spark_task_mod, "decide_task", _digest_decision)
        from app.memory import recall as recall_mod

        # Stores under test are the temp DBs above, not the Mac libraries.
        monkeypatch.setattr(recall_mod, "_freshness_diag", lambda kind: "")
        clear_life_job()
        try:
            # "messages" names the iMessage aisle, but Mansi only WhatsApps:
            # the person filter must search the other aisle, not go empty.
            mansi = await _mac_hub_life_read(
                "list_messages", {"query": "messages from Mansi", "limit": 5}
            )
            assert mansi["count"] >= 1
            assert mansi["channel"] == "whatsapp"
            assert "mansi" in (mansi["spoken"] or "").lower()
            assert "see you at 3" in str(mansi["messages"]).lower()
            # Unknown person: honest empty, no digest leak.
            nobody = await _mac_hub_life_read(
                "list_messages", {"query": "messages from XyzzyQqq", "limit": 5}
            )
            assert nobody["count"] == 0
            assert "xyzzyqqq" in (nobody["spoken"] or "").lower()
            # Digest asks stay unfiltered.
            digest = await _mac_hub_life_read(
                "list_messages", {"query": "who texted me", "limit": 5}
            )
            assert digest["count"] >= 1
            assert "imessage only ping" in str(digest["messages"]).lower()
        finally:
            clear_life_job()
    finally:
        os.unlink(wa_path)
        os.unlink(chat_path)


@pytest.mark.asyncio
async def test_imessage_cursor_stays_put_when_commit_fails(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed commit must not skip messages on the next tick."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                date INTEGER,
                text TEXT,
                handle_id INTEGER,
                is_from_me INTEGER
            );
            INSERT INTO handle VALUES (1, '+15551234567');
            INSERT INTO message VALUES (1, 750000000000000000, 'do not lose me', 1, 0);
            """
        )
        conn.commit()
        conn.close()
        daemon = LifeStreamDaemon(chat_db_path=db_path)

        async def boom() -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(db_session, "commit", boom)
        with pytest.raises(RuntimeError, match="disk full"):
            await daemon.sync_imessage(db_session, limit=10)
        assert daemon.last_message_rowid == 0

        monkeypatch.undo()
        await db_session.rollback()
        events = await daemon.sync_imessage(db_session, limit=10)
        assert len(events) == 1
        assert events[0].content["text"] == "do not lose me"
        assert daemon.last_message_rowid == 1
        assert await daemon.sync_imessage(db_session, limit=10) == []
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_whatsapp_status_skips_still_advance_cursor(
    db_session: AsyncSession,
) -> None:
    """Status broadcasts are not events, but the cursor must walk past them."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        wa_path = f.name
    try:
        wa = sqlite3.connect(wa_path)
        wa.executescript(
            """
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZPARTNERNAME TEXT,
                ZCONTACTJID TEXT,
                ZSESSIONTYPE INTEGER
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
            INSERT INTO ZWACHATSESSION VALUES (1, 'Status', 'status@broadcast', 3);
            INSERT INTO ZWACHATSESSION VALUES (2, 'Alex', 'alex@s.whatsapp.net', 0);
            INSERT INTO ZWAMESSAGE VALUES (
                1, 750000000.0, 'story not a chat', 0, 'status@broadcast', '', 'Status', 1
            );
            INSERT INTO ZWAMESSAGE VALUES (
                2, 750000060.0, 'real ping', 0, 'alex@s.whatsapp.net', '', 'Alex', 2
            );
            """
        )
        wa.commit()
        wa.close()
        daemon = LifeStreamDaemon(
            chat_db_path="/nonexistent/chat.db",
            whatsapp_db_path=wa_path,
        )
        events = await daemon.sync_whatsapp(db_session, limit=10)
        assert [e.content["text"] for e in events] == ["real ping"]
        assert daemon.last_whatsapp_pk == 2
        assert await daemon.sync_whatsapp(db_session, limit=10) == []
    finally:
        os.unlink(wa_path)


@pytest.mark.asyncio
async def test_tick_isolates_failing_stream(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One exploding stream must not skip the rest or skip save_cursor."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        wa_path = f.name
    cursor_path = wa_path + ".cursor.json"
    try:
        wa = sqlite3.connect(wa_path)
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
            INSERT INTO ZWACHATSESSION VALUES (1, 'Alex', 'alex@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE VALUES (
                1, 750000000.0, 'keep this', 0, 'alex@s.whatsapp.net', '', 'Alex', 1
            );
            """
        )
        wa.commit()
        wa.close()
        daemon = LifeStreamDaemon(
            chat_db_path="/nonexistent/chat.db",
            whatsapp_db_path=wa_path,
        )
        daemon.attach_cursor(cursor_path)

        async def boom(session, *, limit=25):
            raise RuntimeError("imessage exploded")

        monkeypatch.setattr(daemon, "sync_imessage", boom)
        result = await daemon.tick(db_session)
        assert result["ok"] is True
        assert result["messages_ingested"] == 0
        assert result["whatsapp_ingested"] == 1
        assert daemon.last_whatsapp_pk == 1
        saved = Path(cursor_path).read_text(encoding="utf-8")
        assert '"last_whatsapp_pk": 1' in saved
    finally:
        os.unlink(wa_path)
        if os.path.exists(cursor_path):
            os.unlink(cursor_path)


def test_background_sync_relaunches_quit_app_even_if_db_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A quit WhatsApp cannot ingest. Hidden relaunch, no 30-minute wait."""
    from types import SimpleNamespace

    import app.services.life_stream_daemon as daemon_mod

    db = tmp_path / "ChatStorage.sqlite"
    db.write_bytes(b"x")
    daemon = SimpleNamespace(
        whatsapp_db_path=str(db),
        mail_index_path="",
        refresh_mail_index_path=lambda: None,
    )
    daemon_mod._last_bg_launch.clear()
    monkeypatch.setattr(daemon_mod, "life_stream_should_run", lambda: True)
    monkeypatch.setattr(daemon_mod, "get_life_stream_daemon", lambda: daemon)
    monkeypatch.setattr(daemon_mod, "_mac_app_running", lambda *args: False)
    real_isdir = os.path.isdir

    def fake_isdir(path: str) -> bool:
        if str(path).endswith("WhatsApp.app"):
            return True
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", fake_isdir)
    launched: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        launched.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("subprocess.run", fake_run)
    out = daemon_mod.ensure_background_sync()
    assert "whatsapp" in out["launched"]
    assert any(cmd[:2] == ["open", "-jg"] for cmd in launched)

    launched.clear()
    again = daemon_mod.ensure_background_sync()
    assert again["launched"] == []

    daemon_mod._last_bg_launch.clear()
    monkeypatch.setattr(daemon_mod, "life_stream_should_run", lambda: False)
    off = daemon_mod.ensure_background_sync()
    assert off["launched"] == []
    assert off["checked"] == []


