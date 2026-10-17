"""WP1 slice 4: /v1/fabric/* additive routes."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


@pytest.fixture()
def fabric_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    monkeypatch.setattr(
        "app.api.fabric._attachments_root", lambda: tmp_path / "attachments"
    )
    from app.fabric import cancel as cancel_mod
    from app.fabric import panic, trace

    panic.clear()
    cancel_mod.reset()
    trace.clear()
    return home


async def test_status(client: AsyncClient, fabric_on) -> None:
    resp = await client.get("/v1/fabric/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fabric_enabled"] is True
    assert body["contract"] == "evie-fabric/1"
    assert len(body["lanes"]) == 9
    assert body["panic"]["panicked"] is False
    assert isinstance(body["recent_turns"], list)


async def test_approvals_park_pending_approve_loop(
    client: AsyncClient, db_session: AsyncSession, fabric_on
) -> None:
    from app.ev.tools import dispatch

    target = fabric_on / "Desktop" / "api-parked.txt"
    parked = await dispatch(
        db_session, "shell_run", {"command": "touch api-parked.txt"},
        actor="master", allow_sensitive=True,
    )
    await db_session.commit()
    assert parked.result["action_id"]

    pending = await client.get("/v1/fabric/approvals/pending")
    assert pending.status_code == 200, pending.text
    ids = [a["id"] for a in pending.json()["approvals"]]
    assert parked.result["action_id"] in ids
    assert not target.exists()

    approved = await client.post(
        f"/v1/fabric/approvals/{parked.result['action_id']}/approve", json={}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "executed"
    assert target.exists()


async def test_approvals_deny(
    client: AsyncClient, db_session: AsyncSession, fabric_on
) -> None:
    from app.ev.tools import dispatch

    parked = await dispatch(
        db_session, "shell_run", {"command": "touch api-denied.txt"},
        actor="master", allow_sensitive=True,
    )
    await db_session.commit()
    denied = await client.post(
        f"/v1/fabric/approvals/{parked.result['action_id']}/deny", json={"reason": "unit"}
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["status"] == "denied"
    assert not (fabric_on / "Desktop" / "api-denied.txt").exists()


async def test_approvals_rejects_non_fabric_ticket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.models import ApprovedAction

    action = ApprovedAction(
        action_type="send_message", title="not fabric", payload={},
        requires_approval=True, status="pending", requested_by="master",
    )
    db_session.add(action)
    await db_session.commit()
    resp = await client.post(f"/v1/fabric/approvals/{action.id}/approve", json={})
    assert resp.status_code == 404


async def test_attach_roundtrip_and_validation(
    client: AsyncClient, fabric_on, tmp_path: Path
) -> None:
    payload = base64.b64encode(b"fake-png-bytes").decode()
    resp = await client.post(
        "/v1/fabric/attach",
        json={"filename": "shot.png", "content_base64": payload,
              "turn_id": "ft-unit", "kind": "screenshot"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["artifact_id"]
    assert Path(body["path"]).read_bytes() == b"fake-png-bytes"

    bad_ext = await client.post(
        "/v1/fabric/attach", json={"filename": "x.exe", "content_base64": payload}
    )
    assert bad_ext.status_code == 422
    bad_b64 = await client.post(
        "/v1/fabric/attach", json={"filename": "x.png", "content_base64": "!!!"}
    )
    assert bad_b64.status_code == 422


async def test_cancel_unknown_and_all(client: AsyncClient, fabric_on) -> None:
    from app.fabric import cancel as cancel_mod

    unknown = await client.post("/v1/fabric/cancel", json={"turn_id": "ft-nope"})
    assert unknown.status_code == 200
    assert unknown.json()["ok"] is False

    token = cancel_mod.new_token("ft-live")
    assert token.cancelled is False
    cleared = await client.post("/v1/fabric/cancel", json={})
    assert cleared.json()["ok"] is True
    assert cleared.json()["evidence"]["cancelled"] == 1
    assert token.cancelled is True


async def test_cancel_named_turn(client: AsyncClient, fabric_on) -> None:
    from app.fabric import cancel as cancel_mod

    token = cancel_mod.new_token("ft-named")
    resp = await client.post("/v1/fabric/cancel", json={"turn_id": "ft-named"})
    assert resp.json()["ok"] is True
    assert token.cancelled is True


async def test_panic_engage_deny_clear(
    client: AsyncClient, db_session: AsyncSession, fabric_on
) -> None:
    from app.ev.tools import dispatch

    state0 = await client.get("/v1/fabric/panic")
    assert state0.json()["panicked"] is False

    engaged = await client.post("/v1/fabric/panic", json={"reason": "unit"})
    assert engaged.json()["ok"] is True
    state1 = await client.get("/v1/fabric/panic")
    assert state1.json()["panicked"] is True

    denied = await dispatch(
        db_session, "shell_run", {"command": "touch panic-nope.txt"},
        actor="master", allow_sensitive=True,
    )
    assert denied.result["ok"] is False
    assert "panic" in str(denied.result.get("error", "")).lower()

    cleared = await client.post("/v1/fabric/panic/clear")
    assert cleared.json()["ok"] is True
    state2 = await client.get("/v1/fabric/panic")
    assert state2.json()["panicked"] is False


async def test_permissions_shape(client: AsyncClient, fabric_on) -> None:
    resp = await client.get("/v1/fabric/permissions")
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    assert len(entries) >= 10
    for entry in entries:
        assert entry["status"] in {"granted", "denied", "unknown"}
        if entry["status"] != "granted":
            assert entry.get("fix"), entry
    by_name = {e["permission"]: e for e in entries}
    assert by_name["tmux"]["status"] == "granted"
    assert by_name["screen_recording"]["status"] == "unknown"


async def test_undo_list_and_restore(client: AsyncClient, fabric_on) -> None:
    from app.fabric import files as fabric_files

    root = fabric_on / "Desktop"
    created = fabric_files.create("doomed.txt", "bye", root=root)
    assert created["ok"] is True
    deleted = fabric_files.delete("doomed.txt", root=root)
    assert deleted["ok"] is True
    assert not (root / "doomed.txt").exists()

    listed = await client.get("/v1/fabric/undo")
    assert listed.status_code == 200, listed.text
    entries = listed.json()["entries"]
    assert entries, "delete must leave a restorable undo entry"

    restored = await client.post("/v1/fabric/undo/restore", json={"id": entries[0]["id"]})
    assert restored.status_code == 200, restored.text
    assert restored.json()["ok"] is True
    assert (root / "doomed.txt").exists()


async def test_layouts_save_list(client: AsyncClient, fabric_on) -> None:
    saved = await client.post(
        "/v1/fabric/layouts/save",
        json={"name": "unit", "panes": [{"command": "echo a"}]},
    )
    assert saved.status_code == 200, saved.text
    listed = await client.get("/v1/fabric/layouts")
    assert listed.status_code == 200
    assert "unit" in [row["name"] for row in listed.json()["layouts"]]


async def test_layout_restore_parks_without_approval(
    client: AsyncClient, fabric_on
) -> None:
    await client.post(
        "/v1/fabric/layouts/save",
        json={"name": "unit2", "panes": [{"command": "echo a"}]},
    )
    resp = await client.post("/v1/fabric/layouts/restore", json={"name": "unit2"})
    assert resp.status_code == 200, resp.text
    # echo-only layout still opens panes (an effect) — hold or receipt, never phantom.
    body = resp.json()
    assert "result" in body
