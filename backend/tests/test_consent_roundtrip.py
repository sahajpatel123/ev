"""Consent round-trip regression: voice_enrollment consent lifecycle."""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConsentRecord

@pytest.mark.asyncio
async def test_consent_roundtrip(client, db_session: AsyncSession):
    # initial absent → Live Listen blocked
    from app.voice.lifecycle import VoiceRuntime
    from app.config import settings
    from sqlalchemy import select

    # ensure no consent
    rows = (await db_session.execute(select(ConsentRecord).where(ConsentRecord.track=="voice_enrollment", ConsentRecord.revoked_at.is_(None)))).scalars().all()
    for r in rows:
        r.revoked_at = __import__("app.utils.text", fromlist=["utcnow"]).utcnow()
    await db_session.commit()

    vr = VoiceRuntime(db_session, master_key=settings.master_key)
    try:
        await vr._require_voice_consent()
        assert False, "should have required consent"
    except Exception as e:
        assert "consent_required" in str(e).lower() or "Voice enrollment" in str(e)
    
    # grant via API (Privacy Centre path)
    resp = await client.post("/v1/training/consent", json={"track":"voice_enrollment"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["track"]=="voice_enrollment" and data["revoked_at"] is None

    # immediate readback via list
    resp2 = await client.get("/v1/training/consent")
    assert resp2.status_code==200
    active = [c for c in resp2.json() if c["track"]=="voice_enrollment" and not c["revoked_at"]]
    assert len(active)==1

    # Live Listen allowed without restart
    vr2 = VoiceRuntime(db_session, master_key=settings.master_key)
    rec = await vr2._require_voice_consent()
    assert rec.track=="voice_enrollment" and rec.revoked_at is None

    # permission UI vs runtime: both read same source (transparency)
    from app.compliance.transparency import transparency_report
    report = await transparency_report(db_session)
    trained = {t["track"]: t["consent_active"] for t in report["trained"]}
    assert trained["voice_enrollment"] is True

    # revoke → blocked
    resp3 = await client.post("/v1/training/consent/voice_enrollment/revoke", json={"reason":"test revoke"})
    assert resp3.status_code==200
    vr3 = VoiceRuntime(db_session, master_key=settings.master_key)
    try:
        await vr3._require_voice_consent()
        assert False
    except Exception as e:
        assert "consent_required" in str(e).lower() or "Voice enrollment" in str(e)
    
    # transparency shows not active
    report2 = await transparency_report(db_session)
    trained2 = {t["track"]: t["consent_active"] for t in report2["trained"]}
    assert trained2["voice_enrollment"] is False

    # re-grant for owner state repair (so live listen works after test)
    await client.post("/v1/training/consent", json={"track":"voice_enrollment"})
