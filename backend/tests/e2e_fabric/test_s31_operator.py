"""S31: operator loop — bounded, frame-bound, policy-gated, honest."""

from __future__ import annotations

import subprocess

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


def _frame(elements: list[dict]) -> str:
    from app.fabric import operator as op

    fid = "fr-test"
    op._frames[fid] = elements
    return fid


@pytest.fixture()
def fabric_on(tmp_path, monkeypatch: pytest.MonkeyPatch):

    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    monkeypatch.setattr(settings, "fabric_audit_file", str(tmp_path / "audit.jsonl"))
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    from app.fabric import operator as op
    from app.fabric import panic

    op.clear_frames()
    panic.clear()
    yield home
    op.clear_frames()


def _area(i=0, name=""):
    return {"i": i, "role": "AXTextArea", "subrole": "", "name": name, "value": ""}


def test_s31_plan_type_into_textarea() -> None:
    from app.fabric import operator as op

    elements = [{"i": 0, "role": "AXWindow", "name": "w", "value": ""},
                _area(1), _area(2, "notes")]
    assert op._plan_for_goal("type hello into notes", elements) == {
        "kind": "set_value", "ref": 2, "text": "hello"}
    assert op._plan_for_goal("type hi", elements)["ref"] == 1
    assert op._plan_for_goal("click OK", elements) is None


def test_s31_plan_click_and_press() -> None:
    from app.fabric import operator as op

    elements = [{"i": 0, "role": "AXButton", "name": "one", "value": ""},
                {"i": 1, "role": "AXButton", "name": "equals", "value": ""}]
    assert op._plan_for_goal("click one", elements) == {"kind": "click", "ref": 0}
    assert op._plan_for_goal("click seven", elements) is None
    assert op._plan_for_goal("press z", elements) == {"kind": "keystroke", "text": "z"}


def test_s31_stale_frame_rejected() -> None:
    from app.fabric import operator as op

    out = op.act("TextEdit", "fr-nope", {"kind": "click", "ref": 0}, approved=True)
    assert out["ok"] is False and "stale frame" in out["error"]


def test_s31_secure_field_denied_even_when_approved() -> None:
    from app.fabric import operator as op

    fid = _frame([{"i": 0, "role": "AXSecureTextField", "name": "pw", "value": ""}])
    out = op.act("App", fid, {"kind": "set_value", "ref": 0, "text": "x"},
                 approved=True)
    assert out["ok"] is False and "secure field" in out["error"]


def test_s31_unapproved_act_parks() -> None:
    from app.fabric import operator as op

    fid = _frame([_area(0)])
    out = op.act("TextEdit", fid, {"kind": "set_value", "ref": 0, "text": "x"})
    assert out["ok"] is False and out.get("confirm") is True


def test_s31_drive_unapproved_parks() -> None:
    from app.fabric import operator as op

    out = op.drive("type hi", "TextEdit")
    assert out["ok"] is False and out.get("confirm") is True


def test_s31_drive_no_plan_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import operator as op

    fid = _frame([_area(0)])

    def fake_observe(app, **k):
        return {"ok": True, "verified": True,
                "evidence": {"frame_id": fid, "elements": op._frames[fid]},
                "error": None, "undo": None}

    monkeypatch.setattr(op, "observe", fake_observe)
    out = op.drive("click nonexistent-button-xyz", "App", approved=True)
    assert out["ok"] is False
    assert "no plannable action" in str(out["evidence"]["steps"])


def test_s31_drive_budget_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric import operator as op

    calls = {"n": 0}

    def fake_observe(app, **k):
        fid = f"fr-{calls['n']}"
        op._frames[fid] = [_area(0)]
        return {"ok": True, "verified": True,
                "evidence": {"frame_id": fid, "elements": op._frames[fid]},
                "error": None, "undo": None}

    def fake_verify(app, expect):
        calls["n"] += 1
        return {"ok": False, "verified": True, "evidence": {},
                "error": "never", "undo": None}

    async def _noop(*a, **k):
        return {}

    monkeypatch.setattr(op, "observe", fake_observe)
    monkeypatch.setattr(op, "verify", fake_verify)
    monkeypatch.setattr(op, "_jxa", lambda *a, **k: {"ok": True, "evidence": {"out": "x"}})
    out = op.drive("type hi", "App", approved=True, max_steps=3,
                   expect={"contains_text": "never-appears"})
    assert out["ok"] is False
    acts = [s for s in out["evidence"]["steps"] if s["phase"] == "act"]
    assert len(acts) == 3, out["evidence"]["steps"]


def _ax_granted() -> bool:
    try:
        from app.fabric.bridge_client import BridgeClient

        res = BridgeClient(timeout=10).call("tcc_status")
        rows = (res.get("evidence") or {}).get("permissions") or []
        return any(r.get("permission") == "accessibility" and r.get("status") == "granted"
                   for r in rows if isinstance(r, dict))
    except Exception:
        return False


def _ensure_evie(timeout_s: float = 20.0) -> bool:
    """The menu-bar app occasionally quits on its own (pre-existing
    instability, under investigation); relaunch it for live tests rather
    than failing the suite on a dead bridge."""
    import time as _time

    from app.fabric.bridge_client import BridgeClient

    if BridgeClient(timeout=5).call("ping").get("ok"):
        return True
    subprocess.run(["open", "-a", "Evie"], capture_output=True, timeout=30)
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        if BridgeClient(timeout=5).call("ping").get("ok"):
            return True
        _time.sleep(2.0)
    return False


needs_ax = pytest.mark.skipif(not _ax_granted(),
                              reason="Accessibility not granted (OWNER-ACTION: ev setup)")


def _jxa_direct(script: str) -> str:
    p = subprocess.run(["osascript", "-l", "JavaScript", "-e", script],
                       capture_output=True, text=True, timeout=30)
    return (p.stdout or "").strip()


@needs_ax
async def test_s31_live_textedit_via_dispatch(
    db_session: AsyncSession, fabric_on, tmp_path
) -> None:
    """LIVE TextEdit through the real CONFIRM path: park -> approve -> drive."""
    import uuid as _uuid
    from uuid import UUID

    from app.ev.tools import dispatch
    from app.services import runtime as runtime_service

    tag = f"OP-{_uuid.uuid4().hex[:6]}"
    if not _ensure_evie() or not _ax_granted():
        pytest.skip("Evie.app bridge unavailable for live operator proof")
    _jxa_direct('Application("TextEdit").activate()')
    _jxa_direct('const t = Application("TextEdit"); t.documents.length; '
                't.Document().make(); t.documents[0].name();')
    try:
        parked = await dispatch(
            db_session, "operator_act",
            {"op": "drive", "app": "TextEdit",
             "goal": f"type {tag} into text area",
             "expect": {"contains_text": tag}, "max_steps": 4},
            actor="master", allow_sensitive=True)
        assert parked.result.get("action_id"), parked.result
        await db_session.commit()
        action = await runtime_service.decide_action(
            db_session, UUID(str(parked.result["action_id"])), actor="master",
            decision="approve")
        await db_session.commit()
        assert action.status == "executed", action.result
        assert action.result["ok"] is True, action.result
    finally:
        _jxa_direct('const t = Application("TextEdit"); '
                    'for (const d of t.documents()) { try { d.close({saving: "no"}); } catch (e) {} }')


@needs_ax
def test_s31_live_calculator() -> None:
    """LIVE Calculator: press 1+2=, read 3 (direct loop, pre-approved plan).

    Calculator's buttons expose no AX names (verified by dump), so this
    proof drives keys; the click path stays covered by planner units plus
    any app with named buttons.
    """
    import time as _time

    from app.fabric import operator as op

    if not _ensure_evie() or not _ax_granted():
        pytest.skip("Evie.app bridge unavailable for live operator proof")
    was_running = subprocess.run(["pgrep", "-x", "Calculator"],
                                 capture_output=True).returncode == 0
    subprocess.run(["open", "-a", "Calculator"], capture_output=True, timeout=30)
    _time.sleep(2.5)
    _jxa_direct('Application("Calculator").activate()')
    _time.sleep(1.0)
    try:
        for keys in ("press 1", "press +", "press 2", "press ="):
            out = op.drive(keys, "Calculator", approved=True, max_steps=2)
            assert out["ok"], (keys, out)
        checked = op.verify("Calculator", {"contains_text": "3"})
        assert checked["ok"], checked
    finally:
        if not was_running:
            subprocess.run(["osascript", "-e", 'tell application "Calculator" to quit'],
                           capture_output=True, timeout=15)


async def test_s31_drive_parks_without_approval(
    db_session: AsyncSession, fabric_on
) -> None:
    from app.ev.tools import dispatch

    parked = await dispatch(
        db_session, "operator_act",
        {"op": "drive", "app": "TextEdit", "goal": "type hi"},
        actor="master", allow_sensitive=True)
    assert parked.result.get("action_id"), parked.result
