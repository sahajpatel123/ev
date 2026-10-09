"""Owner contact enrollment: explicit facts + fill-blank identity + masked CLI."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.owner_enroll import enroll_owner_contact
from app.models import Event, Memory, MemoryEvent, OwnerIdentity
from app.scripts.owner_enroll_cli import main as enroll_main
from app.scripts.owner_enroll_cli import run_cli as enroll_run_cli

# Synthetic fixtures only — never real contact values.
NAME = "Test Owner"
PHONE = "+91 9000000001"
EMAIL_PRIMARY = "owner@example.com"
EMAIL_SECONDARY = "owner.alt@example.com"


def _set_enroll_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_OWNER_ENROLL_NAME", NAME)
    monkeypatch.setenv("EV_OWNER_ENROLL_PHONE", PHONE)
    monkeypatch.setenv("EV_OWNER_ENROLL_EMAIL_PRIMARY", EMAIL_PRIMARY)


async def _count(db_session: AsyncSession, model) -> int:
    return (await db_session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_enroll_writes_facts_with_explicit_provenance(
    db_session: AsyncSession,
) -> None:
    result = await enroll_owner_contact(
        db_session,
        display_name=NAME,
        primary_phone=PHONE,
        emails={"primary": EMAIL_PRIMARY, "secondary": EMAIL_SECONDARY},
    )
    await db_session.commit()

    assert set(result) == {"event_id", "memory_ids", "display_name_set"}
    assert len(result["memory_ids"]) == 3

    event = await db_session.get(Event, UUID(result["event_id"]))
    assert event is not None
    assert event.source == "owner"
    assert event.event_type == "owner.enrollment"

    memories = list((await db_session.execute(select(Memory))).scalars().all())
    assert len(memories) == 3
    contact_fields = set()
    for memory in memories:
        assert memory.memory_type == "fact"
        assert memory.source_type == "explicit"
        assert memory.importance == 0.9
        assert memory.confidence == 1.0
        assert memory.privacy_level == "normal"
        assert memory.payload["enrolled_by"] == "owner"
        assert memory.payload["evidence_type"] == "owner_enrolled"
        contact_fields.add(memory.payload["contact_field"])
    assert contact_fields == {"primary_phone", "email_primary", "email_secondary"}
    texts = " ".join(m.text for m in memories)
    assert PHONE in texts
    assert EMAIL_PRIMARY in texts
    assert EMAIL_SECONDARY in texts

    links = (
        await db_session.execute(
            select(MemoryEvent).where(MemoryEvent.event_id == event.id)
        )
    ).scalars().all()
    assert {str(link.memory_id) for link in links} == set(result["memory_ids"])


async def test_display_name_fills_blank_identity(db_session: AsyncSession) -> None:
    assert await _count(db_session, OwnerIdentity) == 0
    result = await enroll_owner_contact(
        db_session,
        display_name=NAME,
        primary_phone=PHONE,
        emails={"primary": EMAIL_PRIMARY},
    )
    await db_session.commit()

    assert result["display_name_set"] is True
    rows = list((await db_session.execute(select(OwnerIdentity))).scalars().all())
    assert len(rows) == 1
    assert rows[0].display_name == NAME


async def test_display_name_leaves_existing_identity(db_session: AsyncSession) -> None:
    db_session.add(OwnerIdentity(display_name="Original Name"))
    await db_session.commit()
    result = await enroll_owner_contact(
        db_session,
        display_name="Different Name",
        primary_phone=PHONE,
        emails={"primary": EMAIL_PRIMARY},
    )
    await db_session.commit()

    assert result["display_name_set"] is False
    rows = list((await db_session.execute(select(OwnerIdentity))).scalars().all())
    assert len(rows) == 1
    assert rows[0].display_name == "Original Name"


async def test_dry_run_masks_values_and_writes_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _set_enroll_env(monkeypatch)
    monkeypatch.setenv("EV_OWNER_ENROLL_EMAIL_SECONDARY", EMAIL_SECONDARY)
    monkeypatch.delenv("EV_OWNER_ENROLL_EMAIL_WORK", raising=False)

    assert await enroll_run_cli(apply=False) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    for full in (NAME, PHONE, EMAIL_PRIMARY, EMAIL_SECONDARY):
        assert full not in out
        assert full[-2:] in out
    assert "(not set)" in out  # unset optional email_work

    assert await _count(db_session, Event) == 0
    assert await _count(db_session, Memory) == 0
    assert await _count(db_session, OwnerIdentity) == 0


def test_cli_missing_vars_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    for var in (
        "EV_OWNER_ENROLL_NAME",
        "EV_OWNER_ENROLL_PHONE",
        "EV_OWNER_ENROLL_EMAIL_PRIMARY",
        "EV_OWNER_ENROLL_EMAIL_SECONDARY",
        "EV_OWNER_ENROLL_EMAIL_WORK",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EV_OWNER_ENROLL_PHONE", PHONE)

    assert enroll_main([]) == 2
    err = capsys.readouterr().err
    assert "EV_OWNER_ENROLL_NAME" in err
    assert "EV_OWNER_ENROLL_EMAIL_PRIMARY" in err
    assert "EV_OWNER_ENROLL_PHONE" not in err  # provided, so not listed
    assert PHONE not in err


async def test_cli_apply_writes_and_prints_no_values(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _set_enroll_env(monkeypatch)

    assert await enroll_run_cli(apply=True) == 0
    out = capsys.readouterr().out
    assert "APPLY" in out
    for full in (NAME, PHONE, EMAIL_PRIMARY):
        assert full not in out

    assert await _count(db_session, Event) == 1
    assert await _count(db_session, Memory) == 2  # phone + primary email
    rows = list((await db_session.execute(select(OwnerIdentity))).scalars().all())
    assert len(rows) == 1
    assert rows[0].display_name == NAME
