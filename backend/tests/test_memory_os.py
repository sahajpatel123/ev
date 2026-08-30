"""Evie Memory OS: disposable Realtime sessions, persistent owner mind."""

from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.bootstrap import bootstrap_instructions, build_bootstrap, get_bootstrap
from app.memory.curator import process_curation_jobs, validate_curator_payload
from app.memory.materialize import rebuild_memory_cards
from app.memory.outbox import enqueue_for_event
from app.memory.paths import memory_root
from app.memory.recall import build_explicit_recall_payload
from app.memory.relationship import attach_relationship_memory, live_memory_instructions
from app.memory.router import observe_turn, select_context
from app.memory.service import MemoryService
from app.memory.turns import record_conversation_turn
from app.models import MemoryCurationJob
from app.voice.live.grok_voice import grok_session_update


def _evidence_blob(payload: dict) -> str:
    parts = [str(item.get("text") or "") for item in payload.get("evidence") or []]
    parts.extend(str(item.get("text") or "") for item in payload.get("results") or [])
    return " ".join(parts)


@pytest.mark.asyncio
async def test_capture_enqueues_outbox_without_deepseek(db_session: AsyncSession) -> None:
    settings.memory_curator_enabled = False
    name = f"Project {secrets.token_hex(3).title()}"
    event = await record_conversation_turn(
        db_session,
        text=f"I'm calling this experiment {name}.",
        role="user",
        source="voice",
        conversation_id=uuid4(),
        modality="live_realtime",
    )
    assert event is not None
    await db_session.commit()
    jobs = list((await db_session.execute(select(MemoryCurationJob))).scalars().all())
    assert jobs
    assert jobs[0].event_id == event.id
    processed = await process_curation_jobs(db_session, limit=4)
    assert processed >= 1
    row = await db_session.get(MemoryCurationJob, jobs[0].id)
    assert row is not None
    assert row.status == "retryable_failed"
    assert row.last_error in {"provider_unavailable", "curator_unavailable"}
    payload = await build_explicit_recall_payload(
        db_session, "What did I call that experiment?"
    )
    assert name in _evidence_blob(payload)


@pytest.mark.asyncio
async def test_outbox_job_key_is_idempotent(db_session: AsyncSession) -> None:
    event = await record_conversation_turn(
        db_session,
        text="Remember that this is a one-time fact.",
        role="user",
        source="chat",
        conversation_id=uuid4(),
    )
    assert event is not None
    first = await enqueue_for_event(db_session, event)
    second = await enqueue_for_event(db_session, event)
    await db_session.commit()
    assert first is not None and second is not None
    assert first.id == second.id
    count = (
        await db_session.execute(select(func.count()).select_from(MemoryCurationJob))
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_session_destroy_recall_does_not_need_provider_history(
    db_session: AsyncSession,
) -> None:
    name = f"Project {secrets.token_hex(4).title()} {secrets.token_hex(2).title()}"
    session_a = uuid4()
    event = await record_conversation_turn(
        db_session,
        text=f"I'm calling this experiment {name}.",
        role="user",
        source="voice",
        conversation_id=session_a,
        live_session_id="provider-session-a",
        modality="live_realtime",
    )
    assert event is not None
    await db_session.commit()
    service = MemoryService()
    payload = await service.recall(db_session, "What did I call that experiment?")
    assert name in _evidence_blob(payload)
    assert payload.get("grounding") == "evidence"


@pytest.mark.asyncio
async def test_five_restart_recalls_from_raw_events(db_session: AsyncSession) -> None:
    service = MemoryService()
    trials = [
        (
            f"I'm calling this experiment Project {secrets.token_hex(3).title()}.",
            "What did I call that experiment?",
        ),
        (
            f"I'm calling this lock code {secrets.randbelow(9000) + 1000}.",
            "What did I call that lock code?",
        ),
        (
            f"I prefer {secrets.token_hex(3)} tea now.",
            "Which one do I prefer?",
        ),
        (
            "I decided the Mac control browser is Safari.",
            "Do you remember what I decided about Mac control?",
        ),
        (
            "Rahul is my brother.",
            "Do you remember what I told you about Rahul?",
        ),
    ]
    hits = 0
    for statement, question in trials:
        event = await record_conversation_turn(
            db_session,
            text=statement,
            role="user",
            source="voice",
            conversation_id=uuid4(),
            modality="live_realtime",
        )
        assert event is not None
        await db_session.commit()
        payload = await service.recall(db_session, question)
        needle = statement.split()[-1].rstrip(".")
        blob = _evidence_blob(payload)
        if statement.split()[0] == "Rahul":
            needle = "Rahul"
        elif "Safari" in statement:
            needle = "Safari"
        elif "prefer" in statement:
            needle = statement.split()[2]
        elif "lock code" in statement:
            needle = statement.rsplit(" ", 1)[-1].rstrip(".")
        elif "experiment" in statement:
            start = statement.index("Project ")
            needle = statement[start:].rstrip(".")
        if needle.lower() in blob.lower():
            hits += 1
    assert hits == 5


@pytest.mark.asyncio
async def test_fresh_questions_do_not_inject_history(db_session: AsyncSession) -> None:
    await record_conversation_turn(
        db_session,
        text="We spent hours designing Evie memory architecture.",
        role="user",
        source="voice",
        conversation_id=uuid4(),
    )
    await db_session.commit()
    await build_bootstrap(db_session)
    questions = [
        "what's the weather?",
        "How many calories are in an apple?",
        "what time is it?",
        "tell me a joke",
        "is it going to rain?",
        "how far is the moon?",
        "what's 2 plus 2?",
        "who won the game?",
        "translate hello to french",
        "play some music",
    ]
    for question in questions:
        packet = await select_context(db_session, question)
        assert packet["mode"] == "fresh"
        assert packet["would_inject"] is False
        assert packet["historical_evidence"] == []


@pytest.mark.asyncio
async def test_continuation_uses_bootstrap_not_lifetime_transcript(
    db_session: AsyncSession,
) -> None:
    await record_conversation_turn(
        db_session,
        text="We should keep working on Evie memory architecture tonight.",
        role="user",
        source="voice",
        conversation_id=uuid4(),
    )
    await db_session.commit()
    pack = await build_bootstrap(db_session)
    assert pack["tokens"] < settings.memory_bootstrap_max_tokens + 50
    packet = await select_context(
        db_session, "I think I figured out what to do with that memory architecture."
    )
    assert packet["mode"] == "continuation"
    text = bootstrap_instructions(pack)
    assert "MEMORY BOOTSTRAP" in text or "Owner:" in pack.get("relationship", "")
    assert "lifetime transcript" not in text.lower()


@pytest.mark.asyncio
async def test_materialized_cards_rebuild_from_postgres(db_session: AsyncSession) -> None:
    event = await record_conversation_turn(
        db_session,
        text="I'm calling this experiment Project Copper Falcon.",
        role="user",
        source="voice",
        conversation_id=uuid4(),
    )
    assert event is not None
    await db_session.commit()
    first = await rebuild_memory_cards(db_session)
    root = Path(first["root"])
    card = root / "cards" / "relationship.json"
    assert card.is_file()
    before = json.loads(card.read_text(encoding="utf-8"))
    shutil.rmtree(root / "cards")
    assert not card.exists()
    second = await rebuild_memory_cards(db_session)
    assert Path(second["root"]).joinpath("cards", "relationship.json").is_file()
    after = json.loads(card.read_text(encoding="utf-8"))
    assert after.get("kind") == "relationship"
    assert after.get("schema_version") == before.get("schema_version")
    journal = list((root / "journal").rglob("*.jsonl"))
    assert journal
    texts = []
    for path in journal:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                texts.append(json.loads(raw).get("text"))
    assert "I'm calling this experiment Project Copper Falcon." in texts
    line = json.loads(journal[0].read_text(encoding="utf-8").splitlines()[-1])
    assert "pcm" not in line
    assert "jpeg" not in line


@pytest.mark.asyncio
async def test_memory_root_is_private_not_the_git_checkout() -> None:
    root = memory_root()
    assert root == Path(settings.memory_dir).expanduser().resolve()
    assert "/Code/ev/memory" not in str(root)
    assert root.name == "ev-memory" or "Application Support" in str(root)


@pytest.mark.asyncio
async def test_attach_bootstrap_stays_compact(db_session: AsyncSession) -> None:
    await record_conversation_turn(
        db_session,
        text="I prefer Cursor now.",
        role="user",
        source="chat",
        conversation_id=uuid4(),
    )
    await db_session.commit()
    pack = await get_bootstrap(db_session)
    manifest = await attach_relationship_memory(db_session, {"capabilities": []})
    assert manifest.get("memory_bootstrap")
    text = live_memory_instructions(manifest)
    assert "continuous relationship" in text.lower()
    assert "memory.json" not in text.lower()
    assert "deepseek" not in text.lower()
    assert pack.get("tokens", 0) < 2000


@pytest.mark.asyncio
async def test_shadow_gate_records_metrics_without_changing_vad(
    db_session: AsyncSession,
) -> None:
    settings.memory_gate = "shadow"
    packet = await observe_turn(db_session, "what's the weather?")
    assert packet is not None
    assert packet["would_inject"] is False
    assert packet["mode"] == "fresh"
    assert "query" not in packet
    session = grok_session_update(
        provider="openai",
        capability_manifest={"live_tool_projection": [], "capabilities": []},
    )["session"]
    assert session["audio"]["input"]["turn_detection"]["create_response"] is True
    assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"


@pytest.mark.asyncio
async def test_health_omits_private_memory_text(client: AsyncClient) -> None:
    resp = await client.get("/v1/health")
    assert resp.status_code == 200, resp.text
    runtime = resp.json().get("runtime") or {}
    memory = runtime.get("memory") or {}
    blob = json.dumps(memory)
    assert "Copper Falcon" not in blob
    assert "Silver Lantern" not in blob
    assert memory.get("memory_gate_mode") == "off"
    assert memory.get("raw_event_store_ready") is True


def test_curator_schema_rejects_speculation() -> None:
    payload = validate_curator_payload(
        {
            "memories": [
                {
                    "kind": "fact",
                    "text": "",
                    "confidence": 0.9,
                },
                {
                    "kind": "fact",
                    "text": "maybe they like jazz",
                    "confidence": 0.2,
                },
                {
                    "kind": "decision",
                    "subject": "Evie",
                    "property": "memory architecture",
                    "value": "disposable realtime sessions",
                    "text": "Realtime sessions are disposable.",
                    "importance": 0.8,
                    "confidence": 0.96,
                },
            ],
            "entities": [{"name": "Evie", "entity_type": "project"}],
        },
        event_ids=["421"],
    )
    kinds = {item["kind"] for item in payload["memories"]}
    assert "decision" in kinds
    low = [item for item in payload["memories"] if "jazz" in item["text"]]
    assert not low or low[0]["kind"] == "observation"
    assert payload["event_ids"] == ["421"]
