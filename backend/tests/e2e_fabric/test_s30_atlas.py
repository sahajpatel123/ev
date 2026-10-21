"""S30: atlas-driven lane choice — no hardcoded app lists."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


def _atlas_data(apps=(), shortcuts=()):
    return {"apps": list(apps), "shortcuts": list(shortcuts), "clis": {}}


def _app(name, scriptable=False, schemes=(), bundle=""):
    return {"name": name, "bundle_id": bundle or f"com.test.{name.lower()}",
            "version": "1", "path": f"/Applications/{name}.app",
            "scriptable": scriptable, "url_schemes": list(schemes)}


def test_s30_route_follows_data_not_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import atlas

    monkeypatch.setattr("app.fabric.gaps.central_path", lambda: tmp_path / "gaps.jsonl")
    notes_world = _atlas_data([_app("Notes", scriptable=True)])
    memopad_world = _atlas_data([_app("MemoPad", scriptable=True)])
    shortcut_world = _atlas_data([_app("TextEdit")], ["Quick Note"])
    assert atlas.route("note", data=notes_world)["evidence"]["target"] == "Notes"
    assert atlas.route("note", data=memopad_world)["evidence"]["target"] == "MemoPad"
    routed = atlas.route("note", data=shortcut_world)
    assert routed["evidence"]["lane"] == "mac_act"
    assert routed["evidence"]["op"] == "shortcuts_run"
    assert routed["evidence"]["target"] == "Quick Note"


def test_s30_route_timer_url_reminder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import atlas

    monkeypatch.setattr("app.fabric.gaps.central_path", lambda: tmp_path / "gaps.jsonl")
    data = _atlas_data()
    assert atlas.route("timer", data=data)["evidence"]["lane"] == "timer.act"
    assert atlas.route("reminder", data=data)["evidence"]["lane"] == "pim.act"
    opened = atlas.route("url", "https://example.com", data=data)
    assert opened["evidence"]["lane"] == "mac_act"
    assert opened["evidence"]["op"] == "open_url"
    owned = atlas.route("url", "myapp://x", data=_atlas_data(
        [_app("MyApp", schemes=["myapp"])]))
    assert owned["evidence"]["target"] == "MyApp"


def test_s30_route_gap_recorded_centrally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import atlas

    gaps = tmp_path / "gaps.jsonl"
    monkeypatch.setattr("app.fabric.gaps.central_path", lambda: gaps)
    out = atlas.route("teleport", data=_atlas_data())
    assert "gap" in out["evidence"]
    assert gaps.is_file() and "teleport" in gaps.read_text()


def test_s30_no_app_names_in_router() -> None:
    import app.fabric.atlas as atlas_mod

    src = Path(atlas_mod.__file__).read_text()
    for app_name in ("Notes", "TextEdit", "Ghostty", "Safari", "Chrome", "Spotify",
                     "Finder", "Calculator", "WhatsApp", "Messages"):
        assert app_name not in src, app_name


def test_s30_real_scan_finds_scriptable_apps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import atlas

    monkeypatch.setattr("app.fabric.atlas.cache_path", lambda: tmp_path / "atlas.json")
    data = atlas.scan(force=True)
    assert data["apps"], "must find installed apps"
    by_name = {a["name"]: a for a in data["apps"]}
    assert "Ghostty" in by_name
    assert by_name["Ghostty"]["scriptable"] is True
    assert isinstance(data["shortcuts"], list)
    assert data["clis"].get("shortcuts"), "shortcuts CLI must exist"
    # Cache hit: second scan reuses the file (no rescan).
    again = atlas.scan()
    assert again["scanned_at"] == data["scanned_at"]


@pytest.fixture()
def fabric_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    monkeypatch.setattr(settings, "fabric_audit_file", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr("app.fabric.atlas.cache_path", lambda: tmp_path / "atlas.json")
    monkeypatch.setattr("app.fabric.gaps.central_path", lambda: tmp_path / "gaps.jsonl")
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    from app.fabric import panic

    panic.clear()
    return home


async def test_s30_discover_returns_live_route(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    response = await dispatch(db_session, "atlas_discover", {"domain": "notes"},
                              actor="master", allow_sensitive=True)
    body = response.result
    assert body["ok"] is True
    route = body["evidence"].get("route") or {}
    assert route.get("lane") in {"mac_act", "operator.act"}, route
    assert "why" in route, route


async def test_s30_mac_act_jxa_parks_and_approves(
    db_session: AsyncSession, fabric_on
) -> None:
    import shutil as _shutil

    from app.ev.tools import dispatch
    from app.services import runtime as runtime_service

    if _shutil.which("osascript") is None:
        pytest.skip("macOS osascript required")
    parked = await dispatch(db_session, "mac_act",
                            {"op": "jxa", "script": 'return "mac-ok"'},
                            actor="master", allow_sensitive=True)
    assert parked.result.get("action_id"), parked.result
    await db_session.commit()
    from uuid import UUID

    action = await runtime_service.decide_action(
        db_session, UUID(str(parked.result["action_id"])), actor="master",
        decision="approve")
    await db_session.commit()
    assert action.status == "executed"
    assert action.result["evidence"]["out"] == "mac-ok"


async def test_s30_mac_act_open_url_validates(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    bad = await dispatch(db_session, "mac_act", {"op": "open_url", "url": "notaurl!!"},
                         actor="master", allow_sensitive=True)
    assert bad.result["ok"] is False


def test_open_url_rejects_local_and_empty_targets(tmp_path) -> None:
    from app.fabric.bridge import Bridge

    bridge = Bridge(root=tmp_path)
    for url in ("notaurl!!", "file:///etc/passwd", "javascript:alert(1)", ""):
        result = bridge.open_url(url)
        assert result["ok"] is False, url
        assert result["verified"] is False, url


async def test_s30_kernel_discovers_then_runs_suggested_lane(
    db_session, fabric_on, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted kernel: atlas.discover -> suggested mac_act -> executed."""
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession, reset_for_tests

    reset_for_tests()
    try:
        cognition = CognitiveSession(session_id="s30")
        found = await execute_semantic(
            db_session, "atlas.discover", {"domain": "notes"},
            cognition=cognition, actor="master", live_session_id=None,
            steering_seen=0)
        assert found["ok"] is True
        route = (found.get("route") or found.get("evidence", {}).get("route")) or {}
        if not route.get("lane"):
            route = found.get("evidence", {}).get("route") or {}
        assert route.get("lane") == "mac_act", found
        ran = await execute_semantic(
            db_session, "mac.act", {"op": "paste_set", "text": "s30-paste"},
            cognition=cognition, actor="master", live_session_id=None,
            steering_seen=0)
        assert ran["ok"] is True, ran
        back = await execute_semantic(
            db_session, "mac.act", {"op": "paste_get"},
            cognition=cognition, actor="master", live_session_id=None,
            steering_seen=0)
        assert back["evidence"]["text"] == "s30-paste"
    finally:
        reset_for_tests()
