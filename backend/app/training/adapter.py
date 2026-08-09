"""Versioned EVIE adapter registry (LoRA-style, plan 7.3).

An adapter registration binds a consent-gated corpus snapshot, runs
deterministic eval gates (non-empty corpus, corrections present, no leaked
secrets), and can be activated or rolled back. Actual weight training is
provider-dependent; this module is the versioning/rollback/eval boundary.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdapterRegistration
from app.services.access_log import log_access
from app.training.consent import require_consent
from app.training.corpus import get_snapshot
from app.utils.text import utcnow

TRACK = "adapter_fine_tuning"
SECRET_PATTERN = re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")


def _eval_gates(entries: list[dict]) -> dict:
    gates = {
        "corpus_nonempty": len(entries) > 0,
        "corrections_present": any(
            (entry.get("signals") or {}).get("was_correction") is True
            for entry in entries
        ),
        "secrets_absent": not any(
            SECRET_PATTERN.search(str(entry.get("text", ""))) for entry in entries
        ),
    }
    return {"gates": gates, "passed": all(gates.values())}


async def _latest_adapter(session: AsyncSession, name: str) -> AdapterRegistration | None:
    result = await session.execute(
        select(AdapterRegistration)
        .where(
            AdapterRegistration.name == name,
            AdapterRegistration.redacted.is_(False),
        )
        .order_by(AdapterRegistration.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _active_adapter(session: AsyncSession, name: str) -> AdapterRegistration | None:
    result = await session.execute(
        select(AdapterRegistration).where(
            AdapterRegistration.name == name,
            AdapterRegistration.is_current.is_(True),
            AdapterRegistration.redacted.is_(False),
        )
    )
    return result.scalar_one_or_none()


async def register(
    session: AsyncSession,
    *,
    name: str,
    provider: str,
    base_model: str | None,
    adapter_ref: str | None,
    corpus_version: int,
    actor: str,
    reason: str | None = None,
) -> AdapterRegistration:
    consent = await require_consent(session, TRACK)
    corpus = await get_snapshot(session, corpus_version)
    if corpus.redacted:
        raise ValueError(f"Corpus snapshot v{corpus_version} is redacted")
    eval_metrics = _eval_gates(list(corpus.entries or []))
    status = "approved" if eval_metrics["passed"] else "rejected"

    versions = (
        (
            await session.execute(
                select(AdapterRegistration.version).where(
                    AdapterRegistration.name == name
                )
            )
        )
        .scalars()
        .all()
    )
    latest = await _latest_adapter(session, name)
    row = AdapterRegistration(
        name=name,
        version=max(versions, default=0) + 1,
        is_current=False,
        status=status,
        provider=provider,
        base_model=base_model,
        adapter_ref=adapter_ref,
        corpus_snapshot_id=corpus.id,
        eval_metrics=eval_metrics,
        reason_for_change=reason or f"registered from corpus v{corpus_version}",
        consent_id=consent.id,
        supersedes_id=latest.id if latest is not None else None,
    )
    session.add(row)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="adapter_register",
        endpoint="POST /v1/training/adapter/register",
        resource_type="adapter",
        resource_ids=[row.id],
        details={
            "name": name,
            "version": row.version,
            "status": status,
            "corpus_snapshot_id": str(corpus.id),
            "gates": eval_metrics,
        },
    )
    return row


async def get_adapter(session: AsyncSession, adapter_id: UUID) -> AdapterRegistration:
    row = await session.get(AdapterRegistration, adapter_id)
    if row is None or row.redacted:
        raise KeyError(f"Adapter {adapter_id} not found")
    return row


async def list_adapters(session: AsyncSession) -> list[AdapterRegistration]:
    result = await session.execute(
        select(AdapterRegistration)
        .where(AdapterRegistration.redacted.is_(False))
        .order_by(AdapterRegistration.created_at.desc())
    )
    return list(result.scalars().all())


async def activate(
    session: AsyncSession,
    *,
    adapter_id: UUID,
    actor: str,
    reason: str,
) -> AdapterRegistration:
    await require_consent(session, TRACK)
    row = await get_adapter(session, adapter_id)
    if row.status != "approved":
        raise ValueError(f"Adapter is {row.status}, only approved adapters can activate")
    active = await _active_adapter(session, row.name)
    if active is not None and active.id != row.id:
        active.is_current = False
        active.status = "rolled_back"
    row.is_current = True
    row.status = "active"
    row.reason_for_change = reason
    row.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="adapter_activate",
        endpoint="POST /v1/training/adapter/activate",
        resource_type="adapter",
        resource_ids=[row.id],
        details={"name": row.name, "version": row.version},
    )
    return row


async def rollback(
    session: AsyncSession,
    *,
    adapter_id: UUID,
    actor: str,
    reason: str,
) -> AdapterRegistration:
    await require_consent(session, TRACK)
    row = await get_adapter(session, adapter_id)
    if row.supersedes_id is None:
        raise ValueError("Adapter has no prior version to roll back to")
    previous = await session.get(AdapterRegistration, row.supersedes_id)
    if previous is None or previous.redacted:
        raise KeyError("Prior adapter version not found")
    row.is_current = False
    row.status = "rolled_back"
    previous.is_current = True
    previous.status = "active"
    previous.reason_for_change = reason
    row.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="adapter_rollback",
        endpoint="POST /v1/training/adapter/rollback",
        resource_type="adapter",
        resource_ids=[row.id, previous.id],
        details={"name": row.name, "from_version": row.version, "to_version": previous.version},
    )
    return previous


async def delete_all(session: AsyncSession, *, actor: str, reason: str) -> int:
    rows = list((await session.execute(select(AdapterRegistration))).scalars().all())
    for row in rows:
        row.redacted = True
        row.is_current = False
        row.status = "deleted"
        row.eval_metrics = {}
        row.reason_for_change = f"{reason} (redacted)"
        row.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="adapter_delete",
        endpoint="POST /v1/training/adapter/delete",
        resource_type="adapter",
        resource_ids=[r.id for r in rows],
        details={"redacted": len(rows), "reason": reason},
    )
    return len(rows)
