"""WP1 foundation: trace spans, panic switch, facade routing, lane roots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cognitive.kernel import KernelResult
from app.config import settings


def _result(spoken: str = "hi") -> KernelResult:
    return KernelResult(spoken=spoken, kind="brain")


# --- trace ---


def test_trace_turn_records_spans_and_counter() -> None:
    from app.fabric import trace

    trace.clear()
    with trace.turn("unit_probe", "hello") as turn_id:
        assert turn_id
        with trace.span("files", "create", path="a.txt"):
            pass
        with trace.span("shell", "run") as span:
            span["ok"] = True
    assert trace.turn_count("unit_probe") == 1
    found_id, spans = trace.recent_turn("unit_probe")
    assert found_id == turn_id
    assert [s["lane"] for s in spans] == ["files", "shell"]
    assert spans[0]["op"] == "create"
    assert spans[0]["attrs"]["path"] == "a.txt"
    assert spans[1]["ok"] is True
    # JSON-serializable for receipts/logs.
    json.dumps(spans)


def test_trace_recent_turn_unknown_entrypoint() -> None:
    from app.fabric import trace

    trace.clear()
    assert trace.recent_turn("nope") is None
    assert trace.turn_count("nope") == 0


# --- roots (D1) ---


def test_roots_default_is_d1_list(tmp_path: Path) -> None:
    from app.fabric import roots

    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Code").mkdir(parents=True)
    got = roots.default_roots(home=home, extras="")
    names = [p.name for p in got]
    assert "Desktop" in names and "Code" in names
    # Missing D1 dirs are skipped, never created.
    assert "Documents" not in names


def test_roots_extras_appended(tmp_path: Path) -> None:
    from app.fabric import roots

    home = tmp_path / "home"
    home.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    got = roots.default_roots(home=home, extras=str(extra))
    assert extra.resolve() in [p.resolve() for p in got]


def test_lane_root_explicit_or_first_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import roots

    explicit = tmp_path / "w"
    explicit.mkdir()
    assert roots.lane_root(explicit=explicit) == explicit.resolve()
    monkeypatch.setattr(settings, "fabric_roots", str(explicit))
    monkeypatch.setattr("app.fabric.roots._home", lambda: tmp_path / "nohome")
    assert roots.lane_root() == explicit.resolve()


# --- panic (D10) ---


def test_panic_set_and_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import panic

    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    panic.clear()
    assert panic.panicked() is False
    receipt = panic.panic("unit test")
    assert receipt["ok"] is True and receipt["verified"] is True
    assert panic.panicked() is True
    # Persisted: a fresh read (simulated restart) still panics.
    assert json.loads((tmp_path / "panic.json").read_text())["panicked"] is True
    panic.clear()
    assert panic.panicked() is False


def test_panic_revokes_grants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import grants, panic

    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    panic.clear()
    assert grants.grant("comms:test", ttl_s=3600)["ok"] is True
    assert grants.check_grant("comms:test")["ok"] is True
    panic.panic("unit test")
    assert grants.check_grant("comms:test")["ok"] is False
    panic.clear()


def test_grants_revoke_all() -> None:
    from app.fabric import grants

    grants.grant("a:x", ttl_s=60)
    grants.grant("b:y", ttl_s=60)
    assert grants.revoke_all()["ok"] is True
    assert grants.active_grants() == {}


# --- facade ---


def test_fabric_enabled_honors_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import facade

    monkeypatch.setattr(settings, "fabric", True)
    assert facade.fabric_enabled() is True
    monkeypatch.setattr(settings, "fabric", False)
    assert facade.fabric_enabled() is False


async def test_facade_off_calls_handle_turn_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import facade, trace

    monkeypatch.setattr(settings, "fabric", False)
    trace.clear()
    calls: list[dict] = []

    async def stub(**kwargs):
        calls.append(kwargs)
        return _result("legacy")

    monkeypatch.setattr("app.fabric.facade.handle_turn", stub)
    out = await facade.arun_kernel_turn(
        transcript="hi", session=None, actor="master",
        device_id=None, modality="typed", live_session_id=None,
        entrypoint="unit",
    )
    assert out.spoken == "legacy"
    assert len(calls) == 1 and calls[0]["transcript"] == "hi"
    # Legacy path records no fabric turn.
    assert trace.turn_count("unit") == 0


async def test_facade_on_records_turn_and_passes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.fabric import facade, panic, trace

    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    panic.clear()
    trace.clear()

    async def stub(**kwargs):
        return _result("kernel says hi")

    monkeypatch.setattr("app.fabric.facade.handle_turn", stub)
    out = await facade.arun_kernel_turn(
        transcript="hi", session=None, actor="master",
        device_id=None, modality="typed", live_session_id=None,
        entrypoint="unit",
    )
    assert out.spoken == "kernel says hi"
    assert trace.turn_count("unit") == 1
    turn_id, spans = trace.recent_turn("unit")
    assert turn_id


async def test_facade_panicked_denies_without_calling_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.fabric import facade, panic

    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    panic.panic("unit test")
    called = False

    async def stub(**kwargs):
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr("app.fabric.facade.handle_turn", stub)
    out = await facade.arun_kernel_turn(
        transcript="hi", session=None, actor="master",
        device_id=None, modality="typed", live_session_id=None,
        entrypoint="unit",
    )
    assert called is False
    assert out.unavailable is True and out.kind == "denied"
    panic.clear()


async def test_arun_turn_ops_mode_runs_lanes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import facade, panic

    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    panic.clear()
    out = await facade.arun_turn(
        "write hi", entrypoint="unit", root=tmp_path,
        ops=[{"lane": "files", "op": "create",
              "args": {"path": "hi.txt", "content": "hi"}}],
    )
    assert out["ok"] is True
    assert (tmp_path / "hi.txt").read_text() == "hi"
    assert out["evidence"]["n_steps"] == 1


async def test_arun_turn_text_mode_returns_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.fabric import facade, panic

    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    panic.clear()

    async def stub(**kwargs):
        return _result("spoken!")

    monkeypatch.setattr("app.fabric.facade.handle_turn", stub)
    out = await facade.arun_turn("hi", entrypoint="unit", session=None)
    assert out["ok"] is True and out["verified"] is True
    assert out["spoken"] == "spoken!"
    assert out["fabric_turn_id"]
