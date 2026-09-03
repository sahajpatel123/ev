"""Life-archive catalog, skip/quarantine filters, and ingest/index."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.life_archive.catalog import catalog_tree, summarize
from app.memory.life_archive.classify import classify_path
from app.memory.life_archive.ingest import ingest_records
from app.memory.life_archive.parse import parse_record
from app.models import Event, Memory


def _archive(tmp_path: Path) -> Path:
    apple = tmp_path / "apple"
    google = tmp_path / "google"
    contacts = apple / "iCloud Contacts" / "vCards"
    contacts.mkdir(parents=True)
    (contacts / "Ada.vcf").write_text(
        "BEGIN:VCARD\nVERSION:3.0\nFN:Ada Lovelace\nTEL:+15550100\nEMAIL:ada@example.com\nEND:VCARD\n",
        encoding="utf-8",
    )
    calendars = apple / "iCloud Calendars and Reminders"
    calendars.mkdir(parents=True)
    (calendars / "Home.ics").write_text(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:home-1\nSUMMARY:Family dinner\n"
        "DTSTART:20240115T190000Z\nEND:VEVENT\nEND:VCALENDAR\n",
        encoding="utf-8",
    )
    photos = apple / "iCloud Photos Part 1 of 9" / "Photos"
    photos.mkdir(parents=True)
    (photos / "IMG_1.jpg").write_bytes(b"fake-jpeg")
    (photos / "Photo Details.csv").write_text(
        "imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,importDate\n"
        'IMG_1.jpg,x,no,no,no,"Friday January 15,2019 7:00 PM GMT",1,'
        '"Friday January 15,2019 7:01 PM GMT"\n',
        encoding="utf-8",
    )
    deleted = apple / "iCloud Photos Part 1 of 9" / "Recently Deleted"
    deleted.mkdir(parents=True)
    (deleted / "gone.jpg").write_bytes(b"deleted")
    (tmp_path / "apple" / "iCloud Photos Part 2 of 9.zip").write_bytes(b"zip")

    drive = google / "Takeout-3" / "Drive"
    drive.mkdir(parents=True)
    (drive / "notes.md").write_text("Ship the memory ingest pipeline.\n", encoding="utf-8")
    wallet = drive / "TrustWalletBackup"
    wallet.mkdir()
    (wallet / "secret.txt").write_text("SEED PHRASE DO NOT READ", encoding="utf-8")
    (drive / "eAadhaar_1764.pdf").write_bytes(b"%PDF-fake")

    keep = google / "Takeout-6" / "Keep"
    keep.mkdir(parents=True)
    (keep / "note.json").write_text(
        '{"title":"Milk","textContent":"Buy milk","isTrashed":false,'
        '"createdTimestampUsec":1700000000000000}',
        encoding="utf-8",
    )
    tasks = google / "Takeout-6" / "Tasks"
    tasks.mkdir(parents=True)
    (tasks / "Tasks.json").write_text(
        '{"kind":"tasks#taskLists","items":[{"title":"My Tasks","items":'
        '[{"title":"Call the bank","status":"needsAction"}]}]}',
        encoding="utf-8",
    )
    (google / "Takeout-6" / "Calendar").mkdir(parents=True)
    (google / "Takeout-6" / "Calendar" / "owner.ics").write_text(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:g-1\nSUMMARY:Office hours\n"
        "DTSTART:20240201T090000Z\nEND:VEVENT\nEND:VCALENDAR\n",
        encoding="utf-8",
    )
    chrome = google / "Takeout-6" / "Chrome"
    chrome.mkdir(parents=True)
    (chrome / "Bookmarks.html").write_text(
        '<a href="https://example.com/evie">Evie docs</a>',
        encoding="utf-8",
    )
    (chrome / "Addresses and more.json").write_text("{}", encoding="utf-8")
    gemini = google / "Takeout-6" / "My Activity" / "Gemini Apps"
    gemini.mkdir(parents=True)
    (gemini / "image.png").write_bytes(b"png")
    mail = google / "Takeout-4" / "Mail"
    mail.mkdir(parents=True)
    (mail / "All mail Including Spam and Trash.mbox").write_bytes(
        b"From x@example.com Wed Jan 01 00:00:00 2020\n"
        b"X-Gmail-Labels: Inbox\n"
        b"Subject: Project update\n"
        b"Date: Wed, 1 Jan 2020 00:00:00 +0000\n"
        b"\n"
        b"secret body with card 4111111111111111\n"
        b"From y@example.com Thu Jan 02 00:00:00 2020\n"
        b"X-Gmail-Labels: Spam, Inbox\n"
        b"Subject: You won money\n"
        b"\n"
        b"spam body\n"
    )
    gphotos = google / "Takeout-2" / "Google Photos" / "Album"
    gphotos.mkdir(parents=True)
    (gphotos / "trip.jpg").write_bytes(b"photo-bytes")
    (gphotos / "trip.jpg.json").write_text(
        '{"title":"Trip","photoTakenTime":{"timestamp":"1704067200"}}',
        encoding="utf-8",
    )
    from zipfile import ZipFile

    chats = tmp_path / "whatsapp-chat"
    chats.mkdir()
    with ZipFile(chats / "WhatsApp Chat - _Mummy.zip", "w") as archive:
        archive.writestr(
            "_chat.txt",
            "[8/11/22, 7:48:08 PM] .Mummy: Come home for dinner tonight please\n"
            "[8/11/22, 7:49:00 PM] Sahaj Patel: Haa I will start in some time\n"
            "[8/11/22, 7:50:00 PM] .Mummy: Call me when you leave the station\n",
        )
    vendor = (
        tmp_path
        / "apple"
        / "iCloud Drive Part 2 of 3"
        / "Drive"
        / "Weekend Projects"
        / "dispatch"
        / "phone-app"
        / "node_modules"
        / "left-pad"
    )
    vendor.mkdir(parents=True)
    (vendor / "index.js").write_text("module.exports=function(){}", encoding="utf-8")
    ids = tmp_path / "apple/iCloud Drive Part 2 of 3/Drive/Documents/ID/Sahaj"
    ids.mkdir(parents=True)
    (ids / "scan.pdf").write_bytes(b"%PDF-fake")
    return tmp_path


def test_quarantine_and_skip_filters(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    wallet = classify_path(root / "google/Takeout-3/Drive/TrustWalletBackup/secret.txt", root=root)
    assert wallet.disposition == "quarantine"
    aadhaar = classify_path(root / "google/Takeout-3/Drive/eAadhaar_1764.pdf", root=root)
    assert aadhaar.disposition == "quarantine"
    deleted = classify_path(
        root / "apple/iCloud Photos Part 1 of 9/Recently Deleted/gone.jpg", root=root
    )
    assert deleted.disposition == "skip"
    gemini = classify_path(
        root / "google/Takeout-6/My Activity/Gemini Apps/image.png", root=root
    )
    assert gemini.disposition == "skip"
    zipped = classify_path(root / "apple/iCloud Photos Part 2 of 9.zip", root=root)
    assert zipped.disposition == "index"
    assert zipped.reason == "photos_zip"
    details = classify_path(
        root / "apple/iCloud Photos Part 1 of 9/Photos/Photo Details.csv", root=root
    )
    assert details.disposition == "index"
    assert details.reason == "photo_details_csv"
    covered = classify_path(
        root / "apple/iCloud Photos Part 1 of 9/Photos/IMG_1.jpg", root=root
    )
    assert covered.reason == "csv_covers_photos"
    sidecar = classify_path(
        root / "google/Takeout-2/Google Photos/Album/trip.jpg.json", root=root
    )
    assert sidecar.reason == "photo_sidecar"
    orphan_json = tmp_path / "google/Takeout-8/Google Photos/orphan.json"
    orphan_json.parent.mkdir(parents=True, exist_ok=True)
    orphan_json.write_text("{}", encoding="utf-8")
    assert classify_path(orphan_json, root=root).reason == "photo_sidecar"
    addresses = classify_path(
        root / "google/Takeout-6/Chrome/Addresses and more.json", root=root
    )
    assert addresses.disposition == "quarantine"
    vendor = classify_path(
        root
        / "apple/iCloud Drive Part 2 of 3/Drive/Weekend Projects/dispatch/phone-app/node_modules/left-pad/index.js",
        root=root,
    )
    assert vendor.disposition == "skip"
    ident = classify_path(
        root / "apple/iCloud Drive Part 2 of 3/Drive/Documents/ID/Sahaj/scan.pdf",
        root=root,
    )
    assert ident.disposition == "quarantine"


def test_catalog_totals(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    records = catalog_tree(root)
    summary = summarize(records)
    assert summary["ingest_count"] >= 6
    assert summary["index_count"] >= 2
    assert summary["quarantine_count"] >= 3
    assert summary["skip_count"] >= 3
    ingest_rels = {row.rel for row in records if row.disposition == "ingest"}
    assert any(rel.endswith("Ada.vcf") for rel in ingest_rels)
    assert any(rel.endswith("notes.md") for rel in ingest_rels)
    unclassified = [row for row in records if row.reason == "unclassified"]
    assert unclassified == []


def test_contact_parse_omits_phone_and_email(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    items = parse_record(root, "apple/iCloud Contacts/vCards/Ada.vcf", "contacts")
    assert len(items) == 1
    assert items[0].text == "Owner contact: Ada Lovelace."
    assert "15550100" not in items[0].text
    assert "ada@example.com" not in items[0].text
    blob = str(items[0].content)
    assert "15550100" not in blob
    assert "example.com" not in blob


def test_mail_skips_spam_and_drops_body(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    rel = "google/Takeout-4/Mail/All mail Including Spam and Trash.mbox"
    items = parse_record(root, rel, "mail")
    assert len(items) == 1
    assert items[0].content["subject"] == "Project update"
    assert "4111111111111111" not in items[0].text
    assert items[0].privacy_level == "sensitive"


def test_whatsapp_parse_is_a_person_card_not_a_transcript(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    items = parse_record(root, "whatsapp-chat/WhatsApp Chat - _Mummy.zip", "whatsapp")
    types = {item.event_type for item in items}
    assert "life.person" in types
    assert "life.chat.thread" in types
    assert "life.owner.voice" in types
    assert len(items) <= 12
    person = next(item for item in items if item.event_type == "life.person")
    assert person.content["name"] == "Mummy"
    assert person.text.startswith("Person: Mummy")
    assert person.content["relation"] == "parent"
    assert "Owner is casual and brief with them." in person.text or "conversational" in person.text
    bodies = " ".join(item.text for item in items)
    assert "[8/11/22" not in bodies


@pytest.mark.asyncio
async def test_ingest_writes_events_not_blobs(tmp_path: Path, db_session: AsyncSession) -> None:
    root = _archive(tmp_path)
    records = catalog_tree(root)
    report = await ingest_records(db_session, root=root, records=records)
    assert report["created"] >= 8
    events = list((await db_session.execute(select(Event))).scalars().all())
    types = {event.event_type for event in events}
    assert "life.contact" in types
    assert "life.calendar.event" in types
    assert "life.note" in types
    assert "life.task" in types
    assert "life.bookmark" in types
    assert "life.photo.index" in types
    assert "life.mail.envelope" in types
    assert "life.person" in types
    assert "life.chat.thread" in types
    texts = " ".join(str((event.content or {}).get("text") or "") for event in events)
    assert "SEED PHRASE" not in texts
    assert "4111111111111111" not in texts
    assert "Ada Lovelace" in texts
    photo = next(event for event in events if event.event_type == "life.photo.index")
    assert "fake-jpeg" not in str(photo.content)
    assert (photo.content or {}).get("bytes") == 9
    assert (photo.content or {}).get("year") == 2019
    # Photos/mail are index-only: no derived memories from the pointer text.
    photo_memories = list(
        (
            await db_session.execute(select(Memory).where(Memory.text.ilike("%Photo in archive%")))
        ).scalars().all()
    )
    assert photo_memories == []
    contact_memories = list(
        (
            await db_session.execute(select(Memory).where(Memory.text.ilike("%Owner contact:%")))
        ).scalars().all()
    )
    assert contact_memories == []
    from app.memory.retrieval import Retriever

    timeline = await Retriever(db_session).search_events("Ada", k=8)
    assert all(item.get("source") != "life_archive" for item in timeline)
    hits = await Retriever(db_session).search("Ada Lovelace", k=8)
    assert not any("Owner contact:" in (hit.text or "") for hit in hits)
    again = await ingest_records(db_session, root=root, records=records)
    assert again["created"] == 0
    assert again["skipped_existing"] >= report["created"]


def test_classify_shelf_is_a_short_path() -> None:
    from app.memory.life_archive.locate import (
        classify_shelf,
        is_owner_history_query,
        locate_tokens,
        strip_owner_ask,
    )

    assert classify_shelf("Who is in my contacts?") == "contacts"
    assert classify_shelf("Who is Ada Lovelace?") == "contacts"
    assert classify_shelf("Who is Marcus?") == "contacts"
    assert classify_shelf("Find an email I received") == "mail"
    assert classify_shelf("Show my photos") == "photos"
    assert classify_shelf("What notes do I have?") == "notes"
    assert classify_shelf("What tasks do I have?") == "tasks"
    assert classify_shelf("What is on my calendar?") is None
    assert classify_shelf("What was on my old calendar?") == "calendar"
    assert classify_shelf("what did we decide about postgres") is None
    assert classify_shelf("What did I call that experiment?") is None
    assert classify_shelf("What did I prefer before?") is None
    assert classify_shelf("What did we solve?") is None
    assert classify_shelf("What did I prefer before?", people=["Maya", "Before"]) is None
    assert classify_shelf("What did we solve?", people=["Solve", "Maya"]) is None
    assert classify_shelf("Which one do I prefer?") is None
    assert classify_shelf("Where did we leave off?") is None
    assert classify_shelf("Do you remember what I told you about Rahul?") is None
    assert classify_shelf("what's the weather?") is None
    assert classify_shelf("Who is Mummy?", people=["Mummy"]) == "people"
    assert classify_shelf("Who is Mummy?") == "people"
    assert classify_shelf("how's mummy") == "people"
    assert classify_shelf("how's mummy", people=["Mummy"]) == "people"
    assert classify_shelf("what did mummy tell me", people=["Mummy"]) == "chats"
    assert classify_shelf("what did my mummy tell me") == "chats"
    assert classify_shelf("what did mummy say") == "chats"
    assert classify_shelf("what did my mommy tell me") == "chats"
    assert classify_shelf("what did Marcus tell me") == "chats"
    assert classify_shelf("whatsapp with mummy", people=["Mummy"]) == "chats"
    assert classify_shelf("what's the weather?", people=["Mummy"]) is None
    assert classify_shelf("text Mom I'm late") is None
    assert classify_shelf("send a whatsapp to Mom") is None
    assert classify_shelf("call mummy") is None
    assert classify_shelf("Evie remind me to call mom") is None
    assert classify_shelf("do you know me") == "familiarity"
    assert classify_shelf("do you remember me") == "familiarity"
    assert classify_shelf("do you know who I am?") == "familiarity"
    assert classify_shelf("what do you know about me") == "familiarity"
    assert classify_shelf("how well do you know me") == "familiarity"
    assert classify_shelf("do you remember what I said about postgres") is None
    assert classify_shelf("who am i texting") is None
    assert classify_shelf("tell me about my conversations with different people") == "chats"
    assert classify_shelf("Hey Eve, can you tell me about my conversations with different people?") == "chats"
    assert strip_owner_ask(
        "Hey Eve, can you tell me about my conversations with different people?"
    ) == "tell me about my conversations with different people?"
    assert "eve" not in locate_tokens(
        "Hey Eve, can you tell me about my conversations with different people?"
    )
    assert "can" not in locate_tokens(
        "Hey Eve, can you tell me about my conversations with different people?"
    )
    assert classify_shelf("what conversations have I had with people") == "chats"
    assert classify_shelf("tell me about my chats") == "chats"
    assert classify_shelf("what's in my whatsapp") == "chats"
    assert classify_shelf("who do I talk to") == "people"
    assert classify_shelf("who have I been talking to") == "people"
    assert classify_shelf("what did I talk about with people") == "chats"
    assert classify_shelf("what did we talk about") is None
    assert classify_shelf("this conversation") is None
    assert classify_shelf("let's have a conversation") is None
    assert classify_shelf("have a conversation with me") is None
    last_with = "What did I talk about with Ada last time?"
    assert classify_shelf(last_with) == "chats"
    assert classify_shelf(last_with, people=["Ada"]) == "chats"
    assert classify_shelf("What did I talk about with Mansi last time?") == "chats"
    assert classify_shelf("the chat I did with Ada") == "chats"
    assert classify_shelf("last chat with Ada") == "chats"
    assert classify_shelf("what were we talking about with Ada") == "chats"
    assert classify_shelf("catch me up on Ada") == "chats"
    assert classify_shelf("any word from Ada") == "chats"
    from app.memory.life_archive.locate import is_chat_with_other_person

    assert is_chat_with_other_person(last_with) is True
    assert is_chat_with_other_person("what did I tell you about Rahul") is False
    assert is_chat_with_other_person("what did we talk about") is False
    assert is_owner_history_query(last_with) is False
    assert is_owner_history_query("what did I tell you about Rahul") is True
    assert is_owner_history_query("what did we decide about postgres") is True
    assert is_owner_history_query("What did I prefer before?") is True
    assert is_owner_history_query("What did we solve?") is True
    assert is_owner_history_query("Where did we leave off?") is True
    assert is_owner_history_query("who is Maya?") is False
    assert is_owner_history_query("who is Maya") is False
    assert is_owner_history_query("see you before lunch") is False
    assert is_owner_history_query("what do you know about me") is False
    assert is_owner_history_query("what conversations have I had with people") is False
    assert is_owner_history_query("tell me about my conversations with different people") is False
    assert is_owner_history_query("what's in my whatsapp") is False
    from app.ev.continuity import classify_memory_intent
    from app.ev.tool_select import resolve_live_action, select_tool

    assert classify_memory_intent("do you know me") == "explicit_recall"
    assert classify_memory_intent("what's the weather?") == "fresh"
    assert select_tool("what did my mummy tell me").selected == "recall_history"
    assert select_tool("what did mummy say").selected == "recall_history"
    assert select_tool("Who is Mummy?").selected == "recall_history"
    assert select_tool("how's mummy").selected == "recall_history"
    assert select_tool("do you know me").selected == "recall_history"
    assert select_tool("do you remember me").selected == "recall_history"
    assert select_tool("what do you know about me").selected == "recall_history"
    assert select_tool("who is Maya?").selected == "get_person"
    assert select_tool("What did I prefer before?").selected == "search_memory"
    assert select_tool("What did we solve?").selected == "search_memory"
    assert select_tool("text Mom I'm late").selected == "send_message"
    assert select_tool("send a whatsapp to Mom").selected == "send_message"
    assert select_tool("what's the weather?").selected == "get_weather"
    assert select_tool("tell me about my conversations with different people").selected == "recall_history"
    assert select_tool("what's in my whatsapp").selected == "recall_history"
    assert select_tool("who do I talk to").selected == "recall_history"
    assert resolve_live_action("tell me about my conversations with different people") == (
        "recall",
        {"query": "tell me about my conversations with different people"},
    )
    hey = "Hey Eve, can you tell me about my conversations with different people?"
    assert resolve_live_action(hey) == ("recall", {"query": hey})
    assert resolve_live_action("who do I talk to") == (
        "recall",
        {"query": "who do I talk to"},
    )
    assert resolve_live_action("what's in my whatsapp") == (
        "recall",
        {"query": "what's in my whatsapp"},
    )
    long_q = ("please " * 40) + "tell me about my conversations with different people"
    assert len(long_q) > 240
    assert resolve_live_action(long_q) == ("recall", {"query": long_q[:1000]})
    assert resolve_live_action("what did I talk about with people") == (
        "recall",
        {"query": "what did I talk about with people"},
    )
    assert resolve_live_action(last_with) == ("recall", {"query": last_with})
    assert resolve_live_action("catch me up on Ada") == (
        "recall",
        {"query": "catch me up on Ada"},
    )
    assert resolve_live_action("What did I prefer before?")[0] == "search_memory"
    assert resolve_live_action("text Mom I'm late")[0] == "send_message"
    assert resolve_live_action("what's the weather?")[0] == "get_weather"
    assert resolve_live_action("what did we talk about") != (
        "recall",
        {"query": "what did we talk about"},
    )


def test_spoken_life_pack_names_people_not_raw_cards() -> None:
    from app.memory.recall import _spoken_from_evidence

    spoken = _spoken_from_evidence(
        [
            {
                "memory_type": "life.chat.thread",
                "text": "WhatsApp thread: Ada. 11500 messages (2023–2026).",
            },
            {
                "memory_type": "life.chat.thread",
                "text": "WhatsApp thread: Maya. 1022 messages (2025–2026).",
            },
            {
                "memory_type": "life.chat.thread",
                "text": "WhatsApp thread: Ned. 80 messages (2024–2025).",
            },
        ]
    )
    assert "Ada" in spoken
    assert "Maya" in spoken
    assert "Ned" in spoken
    assert "11500" not in spoken
    assert "I cannot find that particular record." not in spoken
    people = _spoken_from_evidence(
        [
            {
                "memory_type": "life.person",
                "text": "Person: Ada (family). WhatsApp, 12 messages.",
            }
        ]
    )
    assert people.startswith("People you talk with include Ada")
    mixed = _spoken_from_evidence(
        [
            {
                "memory_type": "fact",
                "text": (
                    "Owner asked Evie to remember what they showed. "
                    "So, I am holding a remote. I want you to memorize it."
                ),
            },
            {
                "memory_type": "event",
                "text": (
                    "Evie can talk, visually observe through the Mac camera, "
                    "and help with memory and history questions."
                ),
            },
            {
                "memory_type": "life.chat.excerpt",
                "text": "WhatsApp with Ada — Ada: Let's meet after work about the project.",
                "when": "2026-08-11T19:48:08+00:00",
            },
            {
                "memory_type": "life.chat.excerpt",
                "text": "WhatsApp with Ada — Owner: I'll be there after six.",
                "when": "2026-08-11T19:50:00+00:00",
            },
            {
                "memory_type": "life.chat.thread",
                "text": "WhatsApp thread: Ada. 12 messages (2023–2026).",
            },
        ],
        "What did I talk about with Ada last time?",
    )
    lowered = mixed.lower()
    assert "remote" not in lowered
    assert "camera" not in lowered
    assert "memorize" not in lowered
    assert "visually observe" not in lowered
    assert "whatsapp with ada —" not in lowered
    assert "ada" in lowered
    assert "after work" in lowered
    assert "after six" in lowered
    assert "last you talked" in lowered
    assert "you said" in lowered


def test_attach_evidence_keeps_recall_card_list() -> None:
    from datetime import UTC, datetime

    from app.ev.policy import PolicyDecision, attach_evidence

    cards = [
        {"id": "1", "text": "WhatsApp thread: Ada.", "memory_type": "life.chat.thread"},
        {"id": "2", "text": "WhatsApp thread: Maya.", "memory_type": "life.chat.thread"},
    ]
    decision = PolicyDecision(
        allowed=True,
        effect="allow",
        reason="test",
        risk_class="R0",
        provider="memory",
        audit={"name": "recall"},
    )
    stamped = attach_evidence(
        {
            "ok": True,
            "count": 2,
            "spoken": "You talk on WhatsApp with Ada and Maya.",
            "evidence": cards,
        },
        decision,
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert stamped is not None
    assert stamped["ok"] is True
    assert stamped["evidence"] == cards
    assert stamped.get("source") == "memory"


@pytest.mark.asyncio
async def test_locator_opens_one_shelf_not_the_whole_archive(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    from app.memory.bootstrap import build_bootstrap, reset_bootstrap_cache
    from app.memory.life_archive.locate import MAX_HITS, locate_archive, rebuild_locator
    from app.memory.recall import build_explicit_recall_payload
    from app.schemas import EventCreate
    from app.services.event_service import EventService

    root = _archive(tmp_path)
    await ingest_records(db_session, root=root, records=catalog_tree(root))
    service = EventService(db_session, actor="test")
    for index in range(12):
        await service.create(
            EventCreate(
                source="life_archive",
                event_type="life.contact",
                text=f"Owner contact: Extra Person {index}.",
            )
        )
    await service.create(
        EventCreate(
            source="life_archive",
            event_type="life.contact",
            text="Owner contact: Hidden Secret.",
            privacy_level="never_send_to_model",
        )
    )
    await service.create(
        EventCreate(
            source="voice",
            event_type="message.user",
            text="Ada Lovelace is my favorite mathematician because of postgres.",
        )
    )
    await db_session.commit()

    contacts = await locate_archive(db_session, "Who is in my contacts?", k=20)
    assert contacts
    assert len(contacts) <= MAX_HITS
    assert all(item["memory_type"] == "life.contact" for item in contacts)
    assert all(item["kind"] == "life" for item in contacts)
    blob = " ".join(item["text"] for item in contacts)
    assert "Ada Lovelace" in blob or "Extra Person" in blob
    assert "15550100" not in blob
    assert "Hidden Secret" not in blob
    assert "SEED PHRASE" not in blob

    mail = await locate_archive(db_session, "Find an email I received")
    assert mail
    assert all(item["memory_type"] == "life.mail.envelope" for item in mail)
    assert "Project update" in mail[0]["text"]
    assert "4111111111111111" not in mail[0]["text"]

    photos = await locate_archive(db_session, "Show my photos")
    assert photos
    assert all(item["memory_type"] == "life.photo.index" for item in photos)
    assert "fake-jpeg" not in photos[0]["text"]
    dated = await locate_archive(db_session, "photos from 2019")
    assert dated
    assert "2019" in dated[0]["text"]
    assert await locate_archive(db_session, "photo ZXPHOTO99MISSING") == []

    calendar = await locate_archive(db_session, "What was on my old calendar?")
    assert calendar
    assert all(
        item["memory_type"] in {"life.calendar.event", "life.task"} for item in calendar
    )

    empty = await locate_archive(db_session, "what did we decide about postgres")
    assert empty == []
    mummy = await locate_archive(db_session, "Who is Mummy?")
    assert mummy
    assert all(item["memory_type"] == "life.person" for item in mummy)
    assert "Mummy" in " ".join(item["text"] for item in mummy)
    casual = await locate_archive(db_session, "how's mummy")
    assert casual
    assert all(item["memory_type"] == "life.person" for item in casual)
    assert await locate_archive(db_session, "what's the weather?") == []
    drive_notes = await locate_archive(db_session, "What notes do I have on iCloud Drive?")
    assert drive_notes
    assert all(item["memory_type"] == "life.note" for item in drive_notes)

    from app.memory.history import build_shadow_memory, recall_history

    history = await recall_history(db_session, "Who is in my contacts?", k=8)
    assert history["ok"] is True
    assert history["count"] <= MAX_HITS
    history_types = {item["memory_type"] for item in history["results"]}
    assert history_types == {"life.contact"}
    history_text = " ".join(item["text"] for item in history["results"])
    assert "Owner contact:" in history_text
    assert "15550100" not in history_text
    assert "favorite mathematician" not in history_text

    block = await build_shadow_memory(db_session, "Who is in my contacts?", k=5)
    assert "SHADOW MEMORY" in block
    assert "life.contact" in block
    assert "Owner contact:" in block
    assert "favorite mathematician" not in block
    assert await build_shadow_memory(db_session, "what's the weather?", k=5) == ""
    mummy_block = await build_shadow_memory(db_session, "Who is Mummy?", k=5)
    assert "SHADOW MEMORY" in mummy_block
    assert "Person:" in mummy_block
    told = await locate_archive(db_session, "what did my mummy tell me")
    assert told
    assert all(str(item["memory_type"]).startswith("life.chat") for item in told)
    told_text = " ".join(item["text"] for item in told)
    assert "WhatsApp" in told_text
    assert "Come home for dinner" in told_text or "Call me when you leave" in told_text
    last_with_mum = await locate_archive(
        db_session, "What did I talk about with Mummy last time?"
    )
    assert last_with_mum
    assert all(str(item["memory_type"]).startswith("life.chat") for item in last_with_mum)
    last_with_text = " ".join(item["text"] for item in last_with_mum)
    assert "WhatsApp" in last_with_text
    assert "Come home for dinner" in last_with_text or "Call me when you leave" in last_with_text
    assert "remote" not in last_with_text.lower()
    mommy = await locate_archive(db_session, "what did my mommy tell me")
    assert mommy
    assert all(str(item["memory_type"]).startswith("life.chat") for item in mommy)
    said_block = await build_shadow_memory(db_session, "what did mummy say", k=5)
    assert "SHADOW MEMORY" in said_block
    assert "WhatsApp" in said_block
    history_told = await recall_history(db_session, "what did my mummy tell me", k=8)
    assert history_told["ok"] is True
    assert history_told["count"] > 0
    assert any("life.chat" in str(item["memory_type"]) for item in history_told["results"])

    named = await build_explicit_recall_payload(db_session, "Who is Ada Lovelace?", k=8)
    assert named.get("life_shelf") == "contacts"
    assert named["count"] <= MAX_HITS
    assert named["count"] == len(named["evidence"])
    assert all(item.get("kind") == "life" for item in named["evidence"])
    assert all(item.get("memory_type") == "life.contact" for item in named["evidence"])
    named_text = " ".join(str(item.get("text") or "") for item in named["evidence"])
    assert "Ada Lovelace" in named_text
    assert "Owner contact:" in named_text
    assert "favorite mathematician" not in named_text
    assert "postgres" not in named_text.lower()

    mum = await build_explicit_recall_payload(db_session, "Who is Mummy?", k=8)
    assert mum.get("life_shelf") == "people"
    mum_text = " ".join(str(item.get("text") or "") for item in mum["evidence"])
    assert "Mummy" in mum_text
    assert mum["count"] <= MAX_HITS
    quotes = await build_explicit_recall_payload(db_session, "what did my mummy tell me", k=8)
    assert quotes.get("life_shelf") == "chats"
    assert quotes["count"] > 0
    assert quotes.get("grounding") == "evidence"
    quote_text = " ".join(str(item.get("text") or "") for item in quotes["evidence"])
    assert "WhatsApp" in quote_text
    last_recall = await build_explicit_recall_payload(
        db_session, "What did I talk about with Mummy last time?", k=8
    )
    assert last_recall.get("life_shelf") == "chats"
    assert last_recall.get("grounding") == "evidence"
    assert last_recall["count"] > 0
    spoken_last = str(last_recall.get("spoken") or "").lower()
    assert "cannot find" not in spoken_last
    assert "remote" not in spoken_last
    assert "camera" not in spoken_last
    assert "whatsapp with mummy —" not in spoken_last
    assert "last you talked" in spoken_last
    assert "dinner" in spoken_last or "leave" in spoken_last or "whatsapp" in spoken_last
    overview = await build_explicit_recall_payload(
        db_session, "tell me about my conversations with different people", k=8
    )
    assert overview.get("life_shelf") == "chats"
    assert overview.get("grounding") == "evidence"
    assert overview["count"] > 0
    assert all(item.get("memory_type") == "life.chat.thread" for item in overview["evidence"])
    spoken_ask = await build_explicit_recall_payload(
        db_session,
        "Hey Eve, can you tell me about my conversations with different people?",
        k=8,
    )
    assert spoken_ask.get("life_shelf") == "chats"
    assert spoken_ask.get("grounding") == "evidence"
    assert spoken_ask["count"] > 0
    assert "cannot find" not in str(spoken_ask.get("spoken") or "").lower()
    had = await build_explicit_recall_payload(
        db_session, "what conversations have I had with people", k=8
    )
    assert had.get("life_shelf") == "chats"
    assert had.get("grounding") == "evidence"
    assert had["count"] > 0
    assert all(item.get("memory_type") == "life.chat.thread" for item in had["evidence"])
    who = await build_explicit_recall_payload(db_session, "who do I talk to", k=8)
    assert who.get("life_shelf") == "people"
    assert who.get("grounding") == "evidence"
    assert who["count"] > 0
    assert all(item.get("memory_type") == "life.person" for item in who["evidence"])

    unrelated = await build_explicit_recall_payload(
        db_session, "what did we decide about postgres", k=8
    )
    assert unrelated.get("life_shelf") is None
    unrelated_text = " ".join(str(item.get("text") or "") for item in unrelated["evidence"])
    assert "Owner contact: Extra Person" not in unrelated_text

    familiar = await locate_archive(db_session, "do you know me")
    assert familiar
    familiar_kinds = {item["memory_type"] for item in familiar}
    assert familiar_kinds <= {"life.person", "life.owner.voice"}
    assert "life.person" in familiar_kinds
    assert "Mummy" in " ".join(item["text"] for item in familiar)
    know_me = await build_explicit_recall_payload(db_session, "do you know me", k=8)
    assert know_me.get("life_shelf") == "familiarity"
    assert know_me.get("grounding") == "evidence"
    assert "Mummy" in " ".join(str(item.get("text") or "") for item in know_me["evidence"])
    remember_me = await build_explicit_recall_payload(db_session, "do you remember me", k=8)
    assert remember_me.get("life_shelf") == "familiarity"
    assert remember_me.get("grounding") == "evidence"
    assert "Mummy" in " ".join(str(item.get("text") or "") for item in remember_me["evidence"])

    reset_bootstrap_cache()
    pack = await build_bootstrap(db_session)
    relationship = pack.get("relationship") or ""
    assert "Owner contact:" not in relationship
    assert "Photo in archive:" not in relationship
    assert "Close people already known" in relationship
    assert "Mummy" in relationship
    assert "not a first meeting" in relationship
    locator = await rebuild_locator(db_session)
    assert locator["shelves"]["contacts"] >= 13
    assert locator["shelves"]["people"] >= 1
    assert locator["max_hits"] == MAX_HITS
    assert any(row.get("name") == "Mummy" for row in locator.get("people") or [])
