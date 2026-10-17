"""S25: file-policy fuzz — every bypass attempt fails closed, legit use works.

All scenarios run through the live dispatch path (files_op); a few probe
lane helpers directly for cases dispatch cannot express.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


@pytest.fixture()
def fabric_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    from app.fabric import panic

    panic.clear()
    return home


async def _op(db_session, args: dict) -> dict[str, Any]:
    from app.ev.tools import dispatch

    response = await dispatch(
        db_session, "files_op", args, actor="master", allow_sensitive=True
    )
    result = response.result
    assert isinstance(result, dict)
    return result


def _root(fabric_on: Path) -> Path:
    return fabric_on / "Desktop"


# --- outside-root: parked, never executed ---


async def test_s25_outside_absolute_parked(db_session: AsyncSession, fabric_on, tmp_path) -> None:
    outside = tmp_path / "evil.txt"
    outside.write_text("S25-OUTSIDE-BYTES")
    body = await _op(db_session, {"op": "read", "path": str(outside)})
    assert body["ok"] is False
    assert body.get("action_id"), body
    assert "S25-OUTSIDE-BYTES" not in str(body)


async def test_s25_traversal_parked(db_session: AsyncSession, fabric_on) -> None:
    body = await _op(db_session, {"op": "read", "path": "a/../../etc/hosts"})
    assert body["ok"] is False
    assert body.get("action_id"), body


async def test_s25_symlink_outside_parked(db_session: AsyncSession, fabric_on, tmp_path) -> None:
    secret = tmp_path / "real-outside.txt"
    secret.write_text("TOP-SECRET-BYTES")
    link = _root(fabric_on) / "evil-link"
    link.symlink_to(secret)
    body = await _op(db_session, {"op": "read", "path": "evil-link"})
    assert body["ok"] is False
    assert body.get("action_id"), body
    assert "TOP-SECRET-BYTES" not in str(body)


async def test_s25_symlink_chain_parked(db_session: AsyncSession, fabric_on, tmp_path) -> None:
    secret = tmp_path / "chained.txt"
    secret.write_text("CHAINED-BYTES")
    mid = _root(fabric_on) / "mid-link"
    mid.symlink_to(secret)
    outer = _root(fabric_on) / "outer-link"
    outer.symlink_to(mid)
    body = await _op(db_session, {"op": "read", "path": "outer-link"})
    assert body["ok"] is False
    assert "CHAINED-BYTES" not in str(body)


async def test_s25_symlink_inside_allowed(db_session: AsyncSession, fabric_on) -> None:
    root = _root(fabric_on)
    (root / "real.txt").write_text("inside-bytes")
    (root / "ok-link").symlink_to(root / "real.txt")
    body = await _op(db_session, {"op": "read", "path": "ok-link"})
    assert body["ok"] is True
    assert "inside-bytes" in body["evidence"]["body"]


# --- secrets: DENY, never parked (no ticket to approve) ---


@pytest.mark.parametrize("name", [
    "token.txt", "id_rsa", "id_ed25519", "vault.json", "credentials.json",
    "SeCrEt.TxT", "sub/token.txt", ".ssh/config", "x.pem", "y.key", ".env",
])
async def test_s25_secrets_denied(db_session: AsyncSession, fabric_on, name: str) -> None:
    body = await _op(db_session, {"op": "create", "path": name, "content": "x"})
    assert body["ok"] is False
    assert body.get("action_id") is None, body
    assert "secret" in str(body.get("error", "")).lower() or "denied" in str(body.get("error", "")).lower()
    read = await _op(db_session, {"op": "read", "path": name})
    assert read["ok"] is False
    assert read.get("action_id") is None


# --- special files: never touched ---


async def test_s25_fifo_denied_fast(db_session: AsyncSession, fabric_on) -> None:
    fifo = _root(fabric_on) / "hang.fifo"
    os.mkfifo(fifo)
    t0 = time.time()
    body = await _op(db_session, {"op": "read", "path": "hang.fifo"})
    assert time.time() - t0 < 5, "lane must not block on a FIFO"
    assert body["ok"] is False


def test_s25_device_node_unsafe() -> None:
    from app.fabric.files import _is_safe_target

    assert _is_safe_target(Path("/dev/null")) is False
    assert _is_safe_target(Path("/dev/zero")) is False


async def test_s25_hardlink_outside_parked(
    db_session: AsyncSession, fabric_on, tmp_path
) -> None:
    outside = tmp_path / "hl-outside.txt"
    outside.write_text("HL-BYTES")
    link = _root(fabric_on) / "hl-link.txt"
    try:
        os.link(outside, link)
    except OSError:
        pytest.skip("hardlinks unsupported on this volume")
    body = await _op(db_session, {"op": "read", "path": "hl-link.txt"})
    assert body["ok"] is False
    assert "HL-BYTES" not in str(body)


@pytest.mark.parametrize("path", ["", "a\x00b", "x" * 300])
async def test_s25_malformed_paths_rejected(
    db_session: AsyncSession, fabric_on, path: str
) -> None:
    body = await _op(db_session, {"op": "read", "path": path})
    assert body["ok"] is False


# --- TOCTOU: swap-while-reading never leaks outside bytes ---


def test_s25_toctou_hammer(fabric_on) -> None:
    from app.fabric import files as lane

    root = _root(fabric_on)
    inside = root / "inside.txt"
    inside.write_text("INSIDE-BYTES")
    outside = fabric_on / "outside.txt"
    outside.write_text("OUTSIDE-BYTES")
    link = root / "flip"
    link.symlink_to(inside)
    stop = threading.Event()

    def swapper() -> None:
        i = 0
        while not stop.is_set():
            with contextlib.suppress(OSError):
                link.unlink()
            with contextlib.suppress(OSError):
                link.symlink_to(outside if i % 2 else inside)
            i += 1

    worker = threading.Thread(target=swapper, daemon=True)
    worker.start()
    try:
        for _ in range(200):
            res = lane.read_file("flip", root=root)
            if res.get("ok"):
                assert "OUTSIDE-BYTES" not in res["evidence"]["body"]
    finally:
        stop.set()
        worker.join(timeout=5)


# --- legit use keeps working ---


async def test_s25_legit_roundtrip(db_session: AsyncSession, fabric_on) -> None:
    created = await _op(db_session, {"op": "create", "path": "a/b/c.txt", "content": "deep"})
    assert created["ok"] is True
    read = await _op(db_session, {"op": "read", "path": "a/b/c.txt"})
    assert read["evidence"]["body"] == "deep"
    over = await _op(db_session, {"op": "overwrite", "path": "a/b/c.txt", "content": "deeper"})
    assert over["ok"] is True
    undone = await _op(db_session, {"op": "undo", "undo": over["undo"]})
    assert undone["ok"] is True
    read2 = await _op(db_session, {"op": "read", "path": "a/b/c.txt"})
    assert read2["evidence"]["body"] == "deep"
    deleted = await _op(db_session, {"op": "delete", "path": "a/b/c.txt"})
    assert deleted["ok"] is True
    restored = await _op(db_session, {"op": "undo", "undo": deleted["undo"]})
    assert restored["ok"] is True


async def test_s25_unicode_nfd_and_spaces(db_session: AsyncSession, fabric_on) -> None:
    nfd = unicodedata.normalize("NFD", "café (2).txt")
    created = await _op(db_session, {"op": "create", "path": nfd, "content": "u"})
    assert created["ok"] is True, created
    nfc = unicodedata.normalize("NFC", "café (2).txt")
    read_nfc = await _op(db_session, {"op": "read", "path": nfc})
    read_nfd = await _op(db_session, {"op": "read", "path": nfd})
    assert read_nfc["ok"] is True or read_nfd["ok"] is True
    for res in (read_nfc, read_nfd):
        if res.get("ok"):
            assert res["evidence"]["body"] == "u"


async def test_s25_case_variants_consistent(db_session: AsyncSession, fabric_on) -> None:
    created = await _op(db_session, {"op": "create", "path": "CaseFile.txt", "content": "cased"})
    assert created["ok"] is True
    read = await _op(db_session, {"op": "read", "path": "casefile.txt"})
    # APFS case-insensitive: same bytes. Case-sensitive fs: honest not-found.
    if read.get("ok"):
        assert read["evidence"]["body"] == "cased"
    else:
        assert "not found" in str(read.get("error", "")).lower()


async def test_s25_bulk_threshold_and_list(db_session: AsyncSession, fabric_on) -> None:
    for i in range(3):
        body = await _op(db_session, {"op": "create", "path": f"b{i}.txt", "content": "x"})
        assert body["ok"] is True
    listed = await _op(db_session, {"op": "list"})
    assert listed["ok"] is True
    assert "b0.txt" in listed["evidence"]["files"]
    many = await _op(db_session, {"op": "bulk", "paths": [f"z{i}.txt" for i in range(30)]})
    assert many["ok"] is False
    assert many.get("action_id"), many


async def test_s25_undo_snapshot_escape_denied(db_session: AsyncSession, fabric_on) -> None:
    body = await _op(db_session, {
        "op": "undo",
        "undo": {"snapshot": "/etc/hosts", "rel": "owned.txt"},
    })
    assert body["ok"] is False
    assert not (_root(fabric_on) / "owned.txt").exists()


async def test_s25_undo_rel_escape_denied(db_session: AsyncSession, fabric_on, tmp_path) -> None:
    created = await _op(db_session, {"op": "create", "path": "v.txt", "content": "v"})
    assert created["ok"] is True
    evil = dict(created["undo"])
    evil["rel"] = "../escape.txt"
    body = await _op(db_session, {"op": "undo", "undo": evil})
    assert body["ok"] is False
    assert not (fabric_on / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
