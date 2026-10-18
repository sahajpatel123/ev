"""S35/S36: real EventKit reminders + Notes lifecycles (self-cleaning)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


def _bridge_ok() -> bool:
    try:
        from app.fabric.bridge_client import BridgeClient

        return bool(BridgeClient(timeout=8).call("ping").get("ok"))
    except Exception:
        return False


def _ensure_evie() -> bool:
    import subprocess
    import time as _time

    from app.fabric.bridge_client import BridgeClient

    if BridgeClient(timeout=5).call("ping").get("ok"):
        return True
    subprocess.run(["open", "-a", "Evie"], capture_output=True, timeout=30)
    deadline = _time.time() + 20.0
    while _time.time() < deadline:
        if BridgeClient(timeout=5).call("ping").get("ok"):
            return True
        _time.sleep(2.0)
    return False


needs_bridge = pytest.mark.skipif(not _bridge_ok(),
                                  reason="Evie.app bridge down (OWNER-ACTION: launch Evie.app)")


@pytest.fixture()
def fabric_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    monkeypatch.setattr(settings, "fabric_audit_file", str(tmp_path / "audit.jsonl"))
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    from app.fabric import panic

    panic.clear()
    return home


async def _dispatch(db_session: AsyncSession, name: str, args: dict) -> dict[str, Any]:
    from app.ev.tools import dispatch

    response = await dispatch(
        db_session, name, args, actor="master", allow_sensitive=True)
    assert isinstance(response.result, dict)
    return response.result


@needs_bridge
async def test_s35_eventkit_reminder_lifecycle(db_session: AsyncSession, fabric_on) -> None:
    """Real EventKit reminder create/read/complete/delete (default list).

    Self-cleaning: the probe reminder is deleted in `finally`, so a green
    run leaves no trace. The named "Evie Live Proof" list belongs to LP8.
    """
    if not _ensure_evie():
        pytest.skip("Evie.app bridge unavailable")
    created = await _dispatch(db_session, "pim_act",
                              {"op": "reminder_create", "title": "s35-probe",
                               "notes": "wp8 test", "backend": "eventkit"})
    assert created["ok"], created
    assert created["evidence"].get("backend") == "eventkit"
    rid = created["evidence"]["id"]
    try:
        listed = await _dispatch(db_session, "pim_act",
                                 {"op": "reminder_list", "backend": "eventkit"})
        assert listed["ok"], listed
        assert rid in [r.get("id") for r in listed["evidence"]["reminders"]]
        done = await _dispatch(db_session, "pim_act",
                               {"op": "reminder_complete", "id": rid,
                                "backend": "eventkit"})
        assert done["ok"], done
    finally:
        deleted = await _dispatch(db_session, "pim_act",
                                  {"op": "reminder_delete", "id": rid,
                                   "backend": "eventkit"})
        assert deleted["ok"], deleted


@needs_bridge
async def test_s35_calendar_reads(db_session: AsyncSession, fabric_on) -> None:
    if not _ensure_evie():
        pytest.skip("Evie.app bridge unavailable")
    out = await _dispatch(db_session, "pim_act",
                          {"op": "calendar_events", "days": 7,
                           "backend": "eventkit"})
    assert out["ok"], out
    assert isinstance(out["evidence"].get("events"), list)


@needs_bridge
async def test_s36_notes_lifecycle_throwaway_folder(db_session: AsyncSession, fabric_on) -> None:
    """Real Notes create/read/delete in a throwaway folder (JXA)."""
    if not _ensure_evie():
        pytest.skip("Evie.app bridge unavailable")
    from app.fabric.bridge import Bridge

    bridge = Bridge()
    folder = "Evie Test Throwaway"
    created = bridge.note_create(folder, "s36-probe", "wp8 notes body")
    assert created["ok"], created
    try:
        listed = bridge.note_list(folder)
        assert listed["ok"], listed
        assert created["evidence"]["id"] in [n.get("id") for n in listed["evidence"]["notes"]]
    finally:
        deleted = bridge.note_delete(folder, created["evidence"]["id"])
        assert deleted["ok"], deleted
        cleaned = bridge.note_folder_delete(folder)
        assert cleaned["ok"], cleaned
