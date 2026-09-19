"""Evie file sandbox: one jail, every verb, brain runner degraded path."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.ev import brain_file_runner, file_sandbox


@pytest.fixture
def jail(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "laptop_files", True)
    monkeypatch.setattr(settings, "laptop_files_root", str(tmp_path))
    monkeypatch.setattr(settings, "file_sandbox_autonomy", "confirm")
    monkeypatch.setenv("EV_FILE_SANDBOX_AUTONOMY", "confirm")
    return tmp_path


def test_discover_lists_jail(jail: Path) -> None:
    receipt = file_sandbox.discover(origin="mac")
    assert receipt["ok"] is True
    assert any(str(jail) in root for root in receipt["roots"])


def test_index_status(jail: Path) -> None:
    (jail / "hello.txt").write_text("hello", encoding="utf-8")
    receipt = file_sandbox.index_status(rebuild=True, origin="mac")
    assert receipt["ok"] is True
    assert receipt["entries"] >= 1


def test_dry_run_changes_nothing(jail: Path) -> None:
    target = jail / "dry.txt"
    receipt = file_sandbox.execute_op("write", {"path": str(target), "content": "hi"}, origin="mac", dry_run=True)
    assert receipt["ok"] is True
    assert receipt["dry_run"] is True
    assert not target.exists()


def test_write_needs_confirm_then_executes(jail: Path) -> None:
    target = jail / "note.txt"
    parked = file_sandbox.execute_op("write", {"path": str(target), "content": "buy milk"}, origin="iphone")
    assert parked["needs_confirm"] is True
    assert not target.exists()
    ran = file_sandbox.execute_op("write", {"path": str(target), "content": "buy milk"}, origin="iphone", confirm=True)
    assert ran["ok"] is True
    assert target.read_text(encoding="utf-8") == "buy milk"
    assert ran["origin"] == "iphone"


def test_read_search_edit_append_cycle(jail: Path) -> None:
    target = jail / "list.txt"
    assert file_sandbox.execute_op("write", {"path": str(target), "content": "alpha"}, origin="mac", confirm=True)["ok"]
    assert file_sandbox.search("list", origin="mac")["count"] >= 1
    read = file_sandbox.execute_op("read", {"path": str(target)}, origin="mac")
    assert read["ok"] and "alpha" in str(read.get("content") or "")
    assert file_sandbox.execute_op("append", {"path": str(target), "content": "bravo"}, origin="mac", confirm=True)["ok"]
    assert "bravo" in target.read_text(encoding="utf-8")
    assert file_sandbox.execute_op("edit", {"path": str(target), "content": "solo"}, origin="mac", confirm=True)["ok"]
    assert target.read_text(encoding="utf-8") == "solo"


def test_mkdir_copy_move_rename_delete_undo(jail: Path) -> None:
    folder = jail / "sub"
    assert file_sandbox.execute_op("mkdir", {"path": str(folder)}, origin="mac", confirm=True)["ok"]
    assert folder.is_dir()
    src = jail / "a.txt"
    assert file_sandbox.execute_op("write", {"path": str(src), "content": "data"}, origin="mac", confirm=True)["ok"]
    assert file_sandbox.execute_op("copy", {"path": str(src), "dest": str(folder / "b.txt")}, origin="mac", confirm=True)["ok"]
    assert (folder / "b.txt").exists()
    assert file_sandbox.execute_op("move", {"path": str(folder / "b.txt"), "dest": str(folder / "c.txt")}, origin="mac", confirm=True)["ok"]
    assert (folder / "c.txt").exists()
    assert file_sandbox.execute_op("rename", {"path": str(folder / "c.txt"), "dest": str(folder / "d.txt")}, origin="mac", confirm=True)["ok"]
    assert (folder / "d.txt").exists()
    assert file_sandbox.execute_op("delete", {"path": str(folder / "d.txt")}, origin="mac", confirm=True)["ok"]
    assert not (folder / "d.txt").exists()
    undone = file_sandbox.undo(origin="mac")
    assert undone["ok"] is True
    assert (folder / "d.txt").exists()


def test_run_allowlisted_script(jail: Path) -> None:
    script = jail / "job.py"
    script.write_text("print('evie-run-ok')\n", encoding="utf-8")
    parked = file_sandbox.execute_op("run", {"path": str(script)}, origin="mac")
    assert parked["needs_confirm"] is True
    ran = file_sandbox.execute_op("run", {"path": str(script)}, origin="mac", confirm=True)
    assert ran["ok"] is True
    assert "evie-run-ok" in str(ran.get("output") or "")


def test_deny_list_holds(jail: Path) -> None:
    receipt = file_sandbox.execute_op(
        "read", {"path": str(Path.home() / ".ssh" / "id_rsa")}, origin="mac",
    )
    assert receipt["ok"] is False


async def test_brain_fallback_is_degraded_and_honest(jail: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)
    plan, source, degraded = await brain_file_runner.plan_with_brain("find my packing list")
    assert degraded is True
    assert plan["ops"] and plan["ops"][0]["op"] == "search"


async def test_brain_runs_plan_through_same_jail(jail: Path, monkeypatch) -> None:
    target = jail / "brain-note.txt"
    assert file_sandbox.execute_op("write", {"path": str(target), "content": "seed"}, origin="mac", confirm=True)["ok"]

    async def _plan(_text: str):
        return ({"ops": [{"op": "search", "args": {"query": "brain-note"}}]}, "muse-spark-1.3-contributor", False)

    monkeypatch.setattr(brain_file_runner, "plan_with_brain", _plan)
    receipt = await brain_file_runner.run_brain_command("find the brain note", origin="iphone")
    assert receipt["ok"] is True
    assert receipt["model"] == "muse-spark-1.3-contributor"
    assert receipt["receipts"][0]["origin"] == "brain:iphone"


async def test_brain_test_lane_validates_then_runs(jail: Path, monkeypatch) -> None:
    async def _plan(_text: str):
        return ({"ops": [{"op": "search", "args": {"query": "nothing-here-zzz"}}]}, "muse-spark-1.3-contributor", False)

    monkeypatch.setattr(brain_file_runner, "plan_with_brain", _plan)
    receipt = await brain_file_runner.test_brain_command("find nothing-here-zzz", origin="mac")
    assert receipt["test"]["dry_run"] in (True, False)
    assert receipt["test"]["ops"][0]["op"] == "search"


def _api_client():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    )


async def test_api_discover_same_jail_for_iphone(jail: Path) -> None:
    async with _api_client() as client:
        resp = await client.post("/v1/file-sandbox/discover", json={"origin": "iphone"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["origin"] == "iphone"
    assert any(str(jail) in root for root in body["roots"])


async def test_api_write_confirm_gate_over_http(jail: Path) -> None:
    target = str(jail / "http-note.txt")
    async with _api_client() as client:
        parked = await client.post(
            "/v1/file-sandbox/write",
            json={"path": target, "content": "hello", "origin": "iphone"},
        )
        assert parked.json()["needs_confirm"] is True
        assert not Path(target).exists()
        ran = await client.post(
            "/v1/file-sandbox/write",
            json={"path": target, "content": "hello", "origin": "iphone", "confirm": True},
        )
    assert ran.status_code == 200, ran.text
    assert ran.json()["ok"] is True
    assert Path(target).read_text(encoding="utf-8") == "hello"


async def test_api_brain_test_degraded_without_key(jail: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)
    async with _api_client() as client:
        resp = await client.post(
            "/v1/file-sandbox/brain/test",
            json={"text": "find my http note", "origin": "iphone"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["degraded"] is True
    assert body["test"]["ops"][0]["op"] == "search"


async def test_search_and_index_degrade_to_walk_without_shared_index(jail: Path, monkeypatch) -> None:
    import sys

    target = jail / "walk-note.txt"
    assert file_sandbox.execute_op("write", {"path": str(target), "content": "seed"}, origin="mac", confirm=True)["ok"]
    monkeypatch.setitem(sys.modules, "app.ev.file_index", None)
    monkeypatch.setattr("app.ev.file_index", None, raising=False)
    assert file_sandbox._file_index() is None
    found = file_sandbox.search("walk-note", origin="mac")
    assert found["ok"] is True
    assert found["count"] >= 1
    assert found["degraded"] is True
    status = file_sandbox.index_status(origin="mac")
    assert status["ok"] is True
    assert status["entries"] >= 1
