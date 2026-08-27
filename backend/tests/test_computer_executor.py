"""F2 computer executor: parity, verify contract, stale refs, risk semantics.

Acceptance matrix (F2 directive §30-35, §43):
  - parity matrix old path vs executor path (identical transports)
  - stale-ref: act on stale ref fails closed, verified=false, retryable
  - risk differential: same primitive, different target semantics
  - executed-but-not-verified: primitive ok, effect not observed
  - filesystem jail: allowed path works; traversal denied
  - exec allowlist: named op only; raw/shell-shaped rejected
  - flag matrix: off / shadow / on behaviors incl. no double-mutation
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.ev.computer_executor import (
    ComputerExecutionRequest,
    ComputerExecutor,
    executor_mode,
    is_mutating,
    shadow_validate_tool,
)


class FakeMac:
    """Scripted stand-in for the EV.app transport (LiveSession contract)."""

    def __init__(self, *, frontmost: str = "Finder", responses: dict | None = None):
        self.frontmost = frontmost
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    async def request_computer(self, command: str, arguments=None, *, timeout=12.0, request_id=None):
        self.calls.append((command, dict(arguments or {})))
        if command == "inspect_ui":
            return {"ok": True, "app": self.frontmost, "window_title": "w", "elements": [{"role": "AXButton", "title": "Play"}]}
        if command in self.responses:
            out = dict(self.responses[command])
        else:
            out = {"ok": True}
        if out.get("set_frontmost"):
            self.frontmost = out.pop("set_frontmost")
        return out

    def respond(self, command: str, payload: dict) -> None:
        self.responses[command] = payload


@pytest.fixture(autouse=True)
def _executor_flag():
    previous = settings.computer_executor_v2
    yield
    settings.computer_executor_v2 = previous


def _request(primitive: str, operation: str, **kw) -> ComputerExecutionRequest:
    return ComputerExecutionRequest(primitive=primitive, operation=operation, **kw)


# ---------------------------------------------------------------------------
# Verification contract (§26/§27)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigate_open_app_verifies_foreground() -> None:
    mac = FakeMac(responses={"open_app": {"ok": True, "set_frontmost": "Music"}})
    executor = ComputerExecutor(live=mac)
    result = await executor.execute(
        _request("navigate", "open_app", target="Music", args={"name": "Music"})
    )
    assert result.ok is True
    assert result.executed is True
    assert result.verified is True  # after-observation shows Music foreground
    assert result.evidence["before"]["app"] == "Finder"
    assert result.evidence["after"]["app"] == "Music"


@pytest.mark.asyncio
async def test_executed_but_not_verified() -> None:
    # open_app reports success but the after-observation shows a different
    # foreground app: primitive EXECUTED, semantic effect NOT VERIFIED (§33).
    mac = FakeMac(responses={"open_app": {"ok": True}})
    mac.respond("open_app", {"ok": True, "set_frontmost": "Calculator"})
    executor = ComputerExecutor(live=mac)
    result = await executor.execute(
        _request("navigate", "open_app", target="Music", args={"name": "Music"})
    )
    assert result.executed is True
    assert result.verified is False
    assert result.evidence["after"]["app"] == "Calculator"


@pytest.mark.asyncio
async def test_act_success_without_effect_is_not_verified() -> None:
    mac = FakeMac(responses={"ui_action": {"ok": True}})  # UI unchanged
    executor = ComputerExecutor(live=mac)
    result = await executor.execute(
        _request(
            "act",
            "ui_action",
            args={"action": "press", "element_ref": "e1"},
            expected_effect={"type": "state_equals", "field": "app", "value": "Music"},
        )
    )
    assert result.executed is True
    assert result.verified is False  # click happened; nothing verified


@pytest.mark.asyncio
async def test_no_expected_effect_means_no_verification_claim() -> None:
    mac = FakeMac(responses={"ui_action": {"ok": True}})
    executor = ComputerExecutor(live=mac)
    result = await executor.execute(_request("act", "ui_action", args={"action": "press", "element_ref": "e1"}))
    assert result.executed is True
    assert result.verified is False  # no declared expectation → no verified claim


# ---------------------------------------------------------------------------
# Stale refs (§23/§31)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_element_fails_closed() -> None:
    mac = FakeMac(responses={"ui_action": {"ok": False, "error": "stale_element"}})
    executor = ComputerExecutor(live=mac)
    result = await executor.execute(_request("act", "ui_action", args={"action": "press", "element_ref": "e9"}))
    assert result.verified is False
    assert result.error_code == "stale_element"
    assert result.retryable is True  # re-observe, do not guess a replacement


def test_is_mutating_covers_all_act_verbs() -> None:
    for verb in ("press", "type", "key", "scroll", "drag", "confirm", "cancel", "paste"):
        assert is_mutating("ui_action", {"action": verb}) is True
    assert is_mutating("ui_action", {"action": "read_value"}) is False
    assert is_mutating("open_app", {}) is True


# ---------------------------------------------------------------------------
# Risk differential (§24/§32): semantics, not primitives
# ---------------------------------------------------------------------------


def test_risk_follows_target_semantics_not_primitive() -> None:
    mac = FakeMac()
    executor = ComputerExecutor(live=mac)
    play = executor._risk_for(
        _request("act", "ui_action", args={"action": "press", "element_ref": "e1"},
                 risk_context={"element": {"title": "Play"}})
    )
    destroy = executor._risk_for(
        _request("act", "ui_action", args={"action": "press", "element_ref": "e2"},
                 risk_context={"element": {"title": "Delete Account"}})
    )
    assert "semantic-high" not in str(play)
    assert "semantic-high" in str(destroy)


# ---------------------------------------------------------------------------
# fs jail (§34) + exec allowlist (§35)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_read_write_inside_jail_with_recheck(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EV_SANDBOX_ROOT", str(tmp_path))
    executor = ComputerExecutor(live=None)
    write = await executor.execute(_request("fs", "write", args={"path": "notes/a.txt", "content": "hello"}))
    assert write.ok is True
    assert write.verified is True  # verified by re-read inside the jail
    read = await executor.execute(_request("fs", "read", args={"path": "notes/a.txt"}))
    assert read.ok is True


@pytest.mark.asyncio
async def test_fs_traversal_denied(tmp_path, monkeypatch) -> None:

    monkeypatch.setenv("EV_SANDBOX_ROOT", str(tmp_path))
    (tmp_path / "secret.txt").write_text("outside")
    executor = ComputerExecutor(live=None)
    evil = await executor.execute(_request("fs", "read", args={"path": "../secret.txt"}))
    assert evil.ok is False
    assert evil.error_code == "sandbox_denied"
    abs_escape = await executor.execute(_request("fs", "read", args={"path": str(tmp_path.parent / "escape.txt")}))
    assert abs_escape.ok is False


@pytest.mark.asyncio
async def test_fs_destructive_ops_unavailable() -> None:
    executor = ComputerExecutor(live=None)
    delete = await executor.execute(_request("fs", "delete", args={"path": "x.txt"}))
    assert delete.ok is False
    assert delete.error_code == "fs_operation_unavailable"


@pytest.mark.asyncio
async def test_exec_allowlist_named_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EV_SANDBOX_ROOT", str(tmp_path))
    executor = ComputerExecutor(live=None)
    raw = await executor.execute(_request("exec", "run", args={"command": "echo hello"}))
    assert raw.ok is False and raw.error_code == "operation_not_allowlisted"
    named = await executor.execute(_request("exec", "run", args={"operation": "workspace_smoke_test"}))
    assert named.ok is True
    assert named.verified is True


# ---------------------------------------------------------------------------
# Flag matrix (§28/§29)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_mode_never_mutates() -> None:
    settings.computer_executor_v2 = "shadow"
    mac = FakeMac()
    record = await shadow_validate_tool("open_app", {"name": "Music"}, live=mac)
    assert record["mutating"] is True
    assert record["executed"] is False
    assert not any(c[0] == "open_app" for c in mac.calls), "shadow must not execute mutations"
    observe_record = await shadow_validate_tool("inspect_ui", {}, live=mac)
    assert observe_record["dual_run"] is not None  # read-only dual-run allowed


@pytest.mark.asyncio
async def test_on_mode_routes_through_executor_with_fallback(db_session) -> None:
    from app.ev.computer import handle_computer_tool
    from app.voice.live.layer import reset_live_registry
    from app.voice.live.session import LiveSession

    settings.computer_executor_v2 = "on"
    reset_live_registry()
    session = LiveSession(session_id="f2-on", device_id="mac", backchannel_enabled=False)

    async def script(command, arguments=None, *, timeout=12.0, request_id=None):
        if command == "inspect_ui":
            return {"ok": True, "app": "Music", "window_title": "w", "elements": []}
        if command == "open_app":
            session.frontmost = "Music"
            return {"ok": True, "app": "Music"}
        return {"ok": True}

    session.request_computer = script  # type: ignore[method-assign]
    import app.voice.live.layer as layer

    layer.register_live(session)
    result = await handle_computer_tool(
        db_session, "open_app", {"name": "Music"},
        actor="master", live_session_id="f2-on", device_id="mac",
    )
    assert result["ok"] is True
    assert result["verified"] is True
    assert (result.get("evidence") or {}).get("source") == "computer_executor"
    session.close()
    reset_live_registry()


@pytest.mark.asyncio
async def test_on_mode_falls_back_to_legacy_on_executor_failure(db_session) -> None:
    from app.ev.computer import handle_computer_tool
    from app.voice.live.layer import reset_live_registry
    from app.voice.live.session import LiveSession

    settings.computer_executor_v2 = "on"
    reset_live_registry()
    session = LiveSession(session_id="f2-fallback", device_id="mac", backchannel_enabled=False)

    async def script(command, arguments=None, *, timeout=12.0, request_id=None):
        if command == "inspect_ui":
            return {"ok": False, "error": "boom"}
        if command == "open_app":
            return {"ok": True, "app": "Music"}
        return {"ok": True}

    session.request_computer = script  # type: ignore[method-assign]
    import app.voice.live.layer as layer

    layer.register_live(session)
    result = await handle_computer_tool(
        db_session, "open_app", {"name": "Music"},
        actor="master", live_session_id="f2-fallback", device_id="mac",
    )
    assert result["ok"] is True  # legacy path rescued the call
    assert result.get("source") != "computer_executor"
    session.close()
    reset_live_registry()


def test_mode_default_off() -> None:
    settings.computer_executor_v2 = "off"
    assert executor_mode() == "off"


# ---------------------------------------------------------------------------
# Parity matrix (§30): old path vs executor path on identical transports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "scripted"),
    [
        ("inspect_ui", {"query": "front"}, None),
        ("list_apps", {}, None),
        ("open_app", {"name": "Music"}, {"open_app": {"ok": True, "app": "Music", "set_frontmost": "Music"}}),
        ("activate_app", {"name": "Music"}, {"activate_app": {"ok": True, "app": "Music", "set_frontmost": "Music"}}),
        ("close_app", {"name": "Music"}, {"close_app": {"ok": True, "app": "Music"}}),
        ("ui_action", {"action": "press", "element_ref": "e1"}, {"ui_action": {"ok": True}}),
    ],
)
async def test_executor_parity_with_legacy(tool, args, scripted, db_session) -> None:
    from app.ev.computer import handle_computer_tool
    from app.voice.live.layer import reset_live_registry
    from app.voice.live.session import LiveSession

    reset_live_registry()
    legacy_session = LiveSession(session_id=f"parity-legacy-{tool}", device_id="mac", backchannel_enabled=False)
    executor_session = LiveSession(session_id=f"parity-exec-{tool}", device_id="mac", backchannel_enabled=False)

    async def make(script):
        async def call(command, arguments=None, *, timeout=12.0, request_id=None):
            if command == "inspect_ui":
                return {"ok": True, "app": getattr(call, "_front", "Finder"), "elements": [{"ref": "e1", "role": "AXButton", "title": "Play"}]}
            out = dict((script or {}).get(command, {"ok": True}))
            if out.get("set_frontmost"):
                call._front = out.pop("set_frontmost")  # type: ignore[attr-defined]
            return out
        return call

    legacy_session.request_computer = await make(scripted)  # type: ignore[method-assign]
    executor_session.request_computer = await make(scripted)  # type: ignore[method-assign]

    import app.voice.live.layer as layer

    layer.register_live(legacy_session)
    layer.register_live(executor_session)

    if tool == "ui_action":
        # Populate element snapshots identically on both sessions first.
        await handle_computer_tool(db_session, "inspect_ui", {}, actor="master", live_session_id=legacy_session.session_id, device_id="mac")
        await handle_computer_tool(db_session, "inspect_ui", {}, actor="master", live_session_id=executor_session.session_id, device_id="mac")

    settings.computer_executor_v2 = "off"
    legacy = await handle_computer_tool(db_session, tool, dict(args), actor="master", live_session_id=legacy_session.session_id, device_id="mac")
    settings.computer_executor_v2 = "on"
    modern = await handle_computer_tool(db_session, tool, dict(args), actor="master", live_session_id=executor_session.session_id, device_id="mac")

    assert legacy.get("ok") == modern.get("ok")
    assert modern.get("verified") is not None
    # Key semantics must agree (app name / error family), shapes stay compatible.
    for key in ("app", "name", "url", "error"):
        if legacy.get(key) is not None:
            assert modern.get(key) == legacy.get(key), (tool, key, legacy.get(key), modern.get(key))
    legacy_session.close()
    executor_session.close()
    reset_live_registry()

@pytest.mark.asyncio
async def test_navigate_open_url_executor_contract() -> None:
    mac = FakeMac(responses={"open_url": {"ok": True, "url": "https://example.com"}})
    executor = ComputerExecutor(live=mac)
    result = await executor.execute(
        _request("navigate", "open_url", args={"url": "https://example.com"})
    )
    assert result.ok is True
    assert result.verified is True
