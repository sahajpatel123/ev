"""Backup coverage regression: canonical tables must survive round-trip."""
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Commitment, ConsentRecord, Goal, Project

@pytest.mark.asyncio
async def test_backup_includes_canonical(tmp_path, db_session: AsyncSession):
    from app.life import service as life
    from app.training.consent import grant_consent

    # create canonical rows
    p = await life.create_project(db_session, actor="master", title="BackupCanary", priority="HIGH")
    pid = p["project"]["id"]
    g = await life.create_goal(db_session, actor="master", title="BackupGoal", project_ref="BackupCanary")
    gid = g["goal"]["id"]
    c = await life.create_commitment(db_session, actor="master", description="BackupCommit")
    cid = c["commitment"]["id"]
    await grant_consent(db_session, track="voice_enrollment", purpose="test", scope={}, source="test")
    await db_session.commit()

    # create backup via service
    from app.services.backup import create_backup, verify_backup, load_backup
    passphrase = "test-passphrase-123"
    dest = str(tmp_path / "test.evbackup")
    result = await create_backup(db_session, passphrase=passphrase, destination=dest)
    assert result["counts"]["projects"] == 1 or result["counts"]["projects"] >= 1
    assert result["counts"]["goals"] >= 1
    assert result["counts"]["commitments"] >= 1
    assert result["counts"]["consent_records"] >= 1

    v = verify_backup(dest, passphrase)
    assert v["valid"] and v["checksum_match"]

    payload = load_backup(dest, passphrase)
    assert any(p["title"] == "BackupCanary" for p in payload["projects"])
    assert any(g["title"] == "BackupGoal" for g in payload["goals"])
    assert any(c["description"] == "BackupCommit" for c in payload["commitments"])
    assert any(r["track"] == "voice_enrollment" for r in payload["consent_records"])

@pytest.mark.asyncio
async def test_isolated_restore_drill(tmp_path, db_session: AsyncSession):
    """Restore drill into non-production sqlite must preserve canonical rows."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db import Base
    from app.services.backup import create_backup, restore_backup

    # Need a separate isolated engine for drill
    isolated_url = f"sqlite+aiosqlite:///{tmp_path}/drill.db"
    engine2 = create_async_engine(isolated_url)
    async with engine2.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session2 = async_sessionmaker(engine2, expire_on_commit=False)

    # Use current prod session to create backup (already has data from previous test)
    # For isolation, create a small backup from db_session
    passphrase = "drill-pass-123"
    dest = str(tmp_path / "drill.evbackup")
    # ensure at least one project/consent in source
    from app.life import service as life
    from app.training.consent import grant_consent
    p = await life.create_project(db_session, actor="master", title="DrillCanary", priority="HIGH")
    await grant_consent(db_session, track="voice_enrollment", purpose="test", scope={}, source="test-drill")
    await db_session.commit()
    await create_backup(db_session, passphrase=passphrase, destination=dest)

    # Now restore into isolated DB
    async with Session2() as s2:
        res = await restore_backup(s2, path=dest, passphrase=passphrase, mode="wipe", confirm_wipe=True, actor="test-drill")
        await s2.commit()
        assert res["projects_restored"] >= 1 or res["events_restored"] >= 1
        # verify via direct query
        cnt = await s2.execute(text("SELECT count(*) FROM projects"))
        assert cnt.scalar() >= 1
        cnt2 = await s2.execute(text("SELECT count(*) FROM consent_records"))
        assert cnt2.scalar() >= 1
        # StateEpoch should exist
        se = await s2.execute(text("SELECT count(*) FROM state_epoch"))
        assert se.scalar() >= 1
    await engine2.dispose()
