"""S1-S4: files lane — real create/overwrite/delete, journal, undo, gating."""
import hashlib
import json


def _journal(tmp_path):
    p = tmp_path / ".ev-journal.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_s01_file_create_verified(tmp_path):
    from app.fabric import files
    body = "hello evie"
    r = files.create("note.txt", body, root=tmp_path)
    assert r["ok"] and r["verified"], r
    target = tmp_path / "note.txt"
    assert target.exists()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == r["evidence"]["sha256"]
    assert hashlib.sha256(body.encode()).hexdigest() == r["evidence"]["sha256"]
    entries = [e for e in _journal(tmp_path) if e.get("op") == "create" and e.get("ok")]
    assert entries, "create must be journaled"
    # undo restores prior state (file did not exist -> removed again)
    u = files.undo(r["undo"], root=tmp_path)
    assert u["ok"] and u["verified"], u
    assert not target.exists()


def test_s02_overwrite_snapshot_and_undo(tmp_path):
    from app.fabric import files
    (tmp_path / "a.txt").write_text("v1")
    r = files.overwrite("a.txt", "v2", root=tmp_path)
    assert r["ok"] and r["verified"], r
    assert (tmp_path / "a.txt").read_text() == "v2"
    assert r["undo"] and "snapshot" in r["undo"]
    u = files.undo(r["undo"], root=tmp_path)
    assert u["ok"] and u["verified"], u
    assert (tmp_path / "a.txt").read_text() == "v1"


def test_s03_delete_to_trash_and_bulk_confirm(tmp_path):
    from app.fabric import files
    (tmp_path / "d.txt").write_text("bye")
    r = files.delete("d.txt", root=tmp_path)
    assert r["ok"] and r["verified"], r
    assert not (tmp_path / "d.txt").exists()
    trashed = r["evidence"]["trashed"]
    assert trashed and "d.txt" in trashed
    u = files.undo(r["undo"], root=tmp_path)
    assert u["ok"] and u["verified"] and (tmp_path / "d.txt").read_text() == "bye"
    # bulk >25 needs confirm
    many = [f"f{i}.txt" for i in range(26)]
    for name in many:
        (tmp_path / name).write_text("x")
    b = files.bulk(many, op="delete", root=tmp_path)
    assert not b["ok"] and b.get("confirm"), b
    assert (tmp_path / "f0.txt").exists(), "nothing deleted before approval"
    # outside-root delete needs confirm
    o = files.delete("/tmp/elsewhere.txt", root=tmp_path)
    assert not o["ok"] and o.get("confirm"), o


def test_s04_secrets_deny_outside_confirm(tmp_path):
    from app.fabric import files
    d = files.create("id_token.pem", "x", root=tmp_path)
    assert not d["ok"] and "denied" in (d.get("error") or ""), d
    assert not (tmp_path / "id_token.pem").exists()
    o = files.create("/tmp/outside-root.txt", "x", root=tmp_path)
    assert not o["ok"] and o.get("confirm"), o
    # failures are journaled too
    entries = [e for e in _journal(tmp_path) if not e.get("ok")]
    assert len(entries) >= 2


def test_s04_policy_decide_provenance():
    from app.fabric.policy import decide
    assert decide("write", "a.txt", "owner_turn") == "AUTO_JOURNAL"
    assert decide("write", "a.txt", "untrusted_content") == "CONFIRM"
    assert decide("read", "a.txt", "untrusted_content") == "CONFIRM"
    assert decide("write", "id_token.pem", "owner_turn") == "DENY"


def test_s05_read_retries_transient_deadlock_once(tmp_path, monkeypatch):
    """iCloud materialization races (EDEADLK) retry once instead of failing."""
    import errno
    import os as _os

    from app.fabric import files

    (tmp_path / "r.txt").write_text("hello")
    calls: list[str] = []
    real_open = _os.open

    def flaky_open(path, flags, *args, **kwargs):
        if str(path).endswith("r.txt") and not calls:
            calls.append("flaked")
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(_os, "open", flaky_open)
    r = files.read_file("r.txt", root=tmp_path)
    assert calls == ["flaked"]
    assert r["ok"] and r["verified"], r
    assert "hello" in (r["evidence"] or {}).get("body", "")


def test_s05_read_reports_persistent_deadlock_honestly(tmp_path, monkeypatch):
    import errno
    import os as _os

    from app.fabric import files

    (tmp_path / "r.txt").write_text("hello")

    def always_deadlock(path, flags, *args, **kwargs):
        raise OSError(errno.EDEADLK, "Resource deadlock avoided")

    monkeypatch.setattr(_os, "open", always_deadlock)
    r = files.read_file("r.txt", root=tmp_path)
    assert not r["ok"]
    assert "deadlock" in str(r.get("error", "")).lower()
