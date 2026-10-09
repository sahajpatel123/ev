"""Grok chat-history import: parser, consent gate, pipeline, dedupe."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev import owner_model as owner_store
from app.models import Event, OwnerStateSnapshot
from app.scripts.chat_history_import import foreign_id_for, parse_grok_export, run_import
from app.training.consent import grant_consent

# Fully synthetic Grok-shaped fixture — no real export content.
MS_0 = 1710000000000  # 2024-03-09T16:00:00Z


def _mongo(ms: int) -> dict:
    return {"$date": {"$numberLong": str(ms)}}


def _fixture_payload() -> dict:
    return {
        "conversations": [
            {
                "conversation": {
                    "id": "conv-aaa",
                    "title": "Synthetic chat one",
                    "create_time": _mongo(MS_0),
                },
                "responses": [
                    {
                        "response": {
                            "_id": "r1",
                            "conversation_id": "conv-aaa",
                            "message": "I always write tests first.",
                            "sender": "human",
                            "create_time": _mongo(MS_0 + 1000),
                        }
                    },
                    {
                        "response": {
                            "_id": "r2",
                            "conversation_id": "conv-aaa",
                            "message": "Great habit to keep.",
                            "sender": "assistant",
                            "create_time": _mongo(MS_0 + 2000),
                        }
                    },
                    {
                        # No create_time: falls back to the conversation stamp.
                        "response": {
                            "_id": "r3",
                            "conversation_id": "conv-aaa",
                            "message": "I am so frustrated with this deploy, ugh.",
                            "sender": "human",
                        }
                    },
                    {
                        # Missing message: skipped, not fatal.
                        "response": {
                            "_id": "r4",
                            "conversation_id": "conv-aaa",
                            "sender": "human",
                            "create_time": _mongo(MS_0 + 4000),
                        }
                    },
                ],
            },
            {
                "conversation": {"id": "conv-bbb", "title": "Synthetic chat two"},
                "responses": [
                    {
                        "response": {
                            "_id": "r5",
                            "conversation_id": "conv-bbb",
                            "message": "Noted, thanks.",
                            "sender": "ASSISTANT",
                            "create_time": _mongo(MS_0 + 3000),
                        }
                    },
                    "not-a-response-dict",
                    {"no_response_key": True},
                ],
            },
            "not-a-conversation-dict",
            {"conversation": {"id": ""}, "responses": []},
        ]
    }


@pytest.fixture
def grok_export_path(tmp_path: Path) -> str:
    path = tmp_path / "grok_export.json"
    path.write_text(json.dumps(_fixture_payload()), encoding="utf-8")
    return str(path)


def test_parse_grok_export_roles_timestamps_and_tolerance(
    grok_export_path: str,
) -> None:
    turns = parse_grok_export(grok_export_path)
    assert [(t.conversation_id, t.response_id) for t in turns] == [
        ("conv-aaa", "r1"),
        ("conv-aaa", "r2"),
        ("conv-aaa", "r3"),
        ("conv-bbb", "r5"),
    ]
    assert [t.role for t in turns] == [
        "message.user",
        "message.assistant",
        "message.user",
        "message.assistant",  # uppercase ASSISTANT tolerated
    ]
    assert turns[0].occurred_at == datetime.fromtimestamp((MS_0 + 1000) / 1000, tz=UTC)
    assert turns[0].occurred_at.tzinfo is not None
    # Missing response stamp falls back to the conversation stamp.
    assert turns[2].occurred_at == datetime.fromtimestamp(MS_0 / 1000, tz=UTC)
    assert turns[0].conversation_title == "Synthetic chat one"
    assert turns[3].conversation_title == "Synthetic chat two"
    assert turns[0].text == "I always write tests first."


def test_parse_grok_export_rejects_non_conversation_top_level(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"nope": []}), encoding="utf-8")
    assert parse_grok_export(path) == []


async def test_dry_run_writes_nothing(
    db_session: AsyncSession, grok_export_path: str, capsys
) -> None:
    code = await run_import(
        provider="grok",
        path=grok_export_path,
        apply=False,
        limit=100,
        batch=10,
        after_foreign_id=None,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "I always write tests first." not in out  # never print turn text
    count = (
        await db_session.execute(select(func.count()).select_from(Event))
    ).scalar_one()
    assert count == 0


async def test_apply_without_consent_exits_2(
    db_session: AsyncSession, grok_export_path: str
) -> None:
    code = await run_import(
        provider="grok",
        path=grok_export_path,
        apply=True,
        limit=100,
        batch=10,
        after_foreign_id=None,
    )
    assert code == 2
    count = (
        await db_session.execute(select(func.count()).select_from(Event))
    ).scalar_one()
    assert count == 0


async def test_apply_with_consent_writes_events_and_distills_owner_row(
    db_session: AsyncSession,
    grok_export_path: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setattr(settings, "owner_distill_mode", "shadow")
    # Enabled so the no-snapshot assertion is meaningful: the frustrated
    # user turn WOULD snapshot on the live path.
    monkeypatch.setattr(settings, "owner_state_enabled", True)
    await grant_consent(
        db_session,
        track="life_data_personalization",
        purpose="test",
        scope={},
        source="test",
    )
    await db_session.commit()

    code = await run_import(
        provider="grok",
        path=grok_export_path,
        apply=True,
        limit=100,
        batch=2,
        after_foreign_id=None,
    )
    assert code == 0
    # In-process distill ON is restored after the run.
    assert settings.owner_distill_mode == "shadow"
    out = capsys.readouterr().out
    assert "APPLY" in out
    assert "I always write tests first." not in out
    assert "frustrated" not in out

    events = list(
        (await db_session.execute(select(Event).order_by(Event.occurred_at.asc())))
        .scalars()
        .all()
    )
    assert len(events) == 4
    for event in events:
        assert event.source == "import"
        metadata = event.metadata_ or {}
        assert metadata["provider"] == "grok"
        assert metadata["foreign_id"].startswith("grok:")
        assert metadata["conversation_title"].startswith("Synthetic chat")
    assert {e.event_type for e in events} == {"message.user", "message.assistant"}

    # Full live pipeline: the owner-fact-shaped user turn distilled a trait.
    rows = await owner_store.current_owner_rows(db_session, "trait")
    assert len(rows) == 1
    assert "write tests first" in rows[0].text

    # Historical affect never becomes current state.
    snapshots = (
        await db_session.execute(select(func.count()).select_from(OwnerStateSnapshot))
    ).scalar_one()
    assert snapshots == 0


async def test_rerun_skips_by_foreign_id(
    db_session: AsyncSession, grok_export_path: str, capsys
) -> None:
    await grant_consent(
        db_session,
        track="life_data_personalization",
        purpose="test",
        scope={},
        source="test",
    )
    await db_session.commit()
    kwargs: dict = {
        "provider": "grok",
        "path": grok_export_path,
        "apply": True,
        "limit": 100,
        "batch": 10,
        "after_foreign_id": None,
    }
    assert await run_import(**kwargs) == 0
    first = (
        await db_session.execute(select(func.count()).select_from(Event))
    ).scalar_one()
    assert first == 4

    capsys.readouterr()
    assert await run_import(**kwargs) == 0
    second = (
        await db_session.execute(select(func.count()).select_from(Event))
    ).scalar_one()
    assert second == 4  # no duplicates
    out = capsys.readouterr().out
    assert "skipped_existing=4" in out


async def test_limit_and_after_foreign_id_cursor(
    grok_export_path: str, capsys
) -> None:
    kwargs: dict = {
        "provider": "grok",
        "path": grok_export_path,
        "apply": False,
        "limit": 2,
        "batch": 10,
        "after_foreign_id": None,
    }
    assert await run_import(**kwargs) == 0
    assert "scanned=2" in capsys.readouterr().out

    turns = parse_grok_export(grok_export_path)
    cursor = foreign_id_for("grok", turns[0])
    kwargs["limit"] = 100
    kwargs["after_foreign_id"] = cursor
    assert await run_import(**kwargs) == 0
    out = capsys.readouterr().out
    assert "scanned=3" in out
    assert f"last_foreign_id={foreign_id_for('grok', turns[-1])}" in out
