"""Regression tests for the independent review's findings (B1, B2, S1-S5): each one pins a behaviour the
reviewer showed to be wrong, so it cannot come back."""

from __future__ import annotations

import time

import pytest

from app.fabric import policy
from app.fabric.skills import registry
from app.fabric.skills.api import Hands
from app.fabric.skills.packs import ghostty as gh
from app.fabric.skills.packs import load_all
from app.fabric.skills.packs.skills_terminal import _send_risk
from app.fabric.skills.packs.web_media import WebMedia
from app.fabric.skills.runtime import Proc
from app.fabric.skills.testing import FakeBrowser, FakeRuntime, PageSim

CONTRACT_PROVENANCE = ["screenshot", "web", "email", "message", "ocr", "audio", "terminal_output",
                       "untrusted", "untrusted_content"]


@pytest.fixture(autouse=True)
def _packs():
    load_all()
    registry._issued.clear()
    yield
    registry._issued.clear()


def hands(rt=None, **kw):
    rt = rt or FakeRuntime()
    return Hands(rt, state_dir=kw.pop("state_dir", None), **kw), rt


# ------------------------------------------------------------------ B1: provenance is fail-closed ---

def test_every_contract_provenance_value_is_judged_untrusted():
    assert registry.effective_provenance("owner") == "owner"
    assert registry.effective_provenance(None) == "owner"
    for label in CONTRACT_PROVENANCE + ["something_new", "Agent_Task"]:
        assert registry.effective_provenance(label) in policy.UNTRUSTED_PROVENANCE, label
        assert policy.decide("run", "hands:x", registry.effective_provenance(label)) == "CONFIRM"


@pytest.mark.parametrize("label", CONTRACT_PROVENANCE + ["agent_task", "voice"])
def test_skill_run_asks_for_every_non_owner_provenance(label):
    rt = FakeRuntime()
    page = PageSim(rt, [{"kind": "S", "skip_after": 2, "len": 10}])
    fb = FakeBrowser(rt)
    fb.add("w", "t", "Video", "http://localhost/p", page, active=True)
    h, _ = hands(rt, browsers=[fb])
    r = h.skill_run("web_media.skip_ad", provenance=label)
    assert r["needs_confirm"] and r["error"]["code"] == "confirm_required", label
    assert page.clicks == 0, "an untrusted-triggered effect must not run before confirmation"


@pytest.mark.parametrize("label", ["ocr", "audio", "terminal_output", "agent_task"])
def test_ui_act_and_watcher_create_ask_for_every_non_owner_provenance(label):
    h, _ = hands()
    a = h.ui_act({"app": "Notes", "role": "button", "name": "Save"}, "press", provenance=label)
    assert a["needs_confirm"], label
    w = h.watcher_create({"kind": "skip_ads"}, provenance=label)
    assert w["needs_confirm"], label


# ------------------------------------------------------------------ B2: enter=False never presses Enter ---

def test_input_text_without_enter_never_presses_enter_even_with_a_trailing_newline():
    rt = FakeRuntime()
    rt.when(r"^ps ", Proc(True, out="/Applications/Ghostty.app/Contents/MacOS/ghostty\n"), kind="run")
    rt.when(r"input text", "")
    rt.when(r"send key", "")
    d = gh.GhosttyDriver(rt)
    d.input_text("T", "rm -rf ./build\n", enter=False)
    d.input_text("T", "echo hi\r\n", enter=False)
    assert rt.scripts("send key") == []
    assert len(rt.scripts("input text")) == 2
    d.input_text("T", "ls\n", enter=True)
    assert len(rt.scripts("send key")) == 1


def test_typing_that_can_submit_a_line_is_classified_as_running_a_command():
    assert _send_risk({"text": "partial", "enter": False}) == ("write", None)
    assert _send_risk({"text": "partial\n", "enter": False}) == ("write", None)  # trailing break is dropped
    for text in ("a\nb", "a\rb", "a\x0bb", "a b"):
        action, _cls = _send_risk({"text": text, "enter": False})
        assert action == "run", repr(text)
    assert _send_risk({"text": "git push origin main", "enter": True}) == ("run", "publish")
    assert _send_risk({"text": "git push\nls", "enter": False}) == ("run", "publish")


# ------------------------------------------------------------------ S1: an approval cannot arm itself ---

def _outside_move(tmp_path):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    src = home / "Documents" / "a.txt"
    src.write_text("x")
    out = tmp_path / "elsewhere"
    out.mkdir()
    return home, src, out, {"src": str(src), "dst": str(out / "a.txt")}


def test_approved_call_without_a_prior_card_neither_runs_nor_arms_one(tmp_path):
    home, src, out, args = _outside_move(tmp_path)
    h, _ = hands(home=home)
    pid = h.skill_run("finder.move", args)["confirm"]["plan_id"]
    registry._issued.clear()  # as if the card had never been shown (or had been dropped)
    for _ in range(2):
        r = h.skill_run("finder.move", args, approved=True, approval_id=pid)
        assert r["needs_confirm"] and not (out / "a.txt").exists()
        assert not registry._issued, "an approved call must not issue a card for itself"


def test_a_card_is_single_use_and_an_expired_one_is_not_refreshed_by_approval(tmp_path, monkeypatch):
    home, src, out, args = _outside_move(tmp_path)
    h, _ = hands(home=home)
    card = h.skill_run("finder.move", args)["confirm"]
    pid = card["plan_id"]
    assert pid in registry._issued
    armed_until = registry._issued[pid]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + registry.PLAN_TTL_S + 60)
    late = h.skill_run("finder.move", args, approved=True, approval_id=pid)
    assert late["needs_confirm"] and "no longer current" in late["error"]["message"]
    assert not (out / "a.txt").exists()
    assert registry._issued.get(pid) == armed_until, "expiry must stay expired"
    monkeypatch.setattr(time, "time", real)
    fresh = h.skill_run("finder.move", args)["confirm"]["plan_id"]
    ok = h.skill_run("finder.move", args, approved=True, approval_id=fresh)
    assert ok["ok"] and (out / "a.txt").exists()
    assert fresh not in registry._issued
    again = h.skill_run("finder.move", {"src": str(out / "a.txt"), "dst": str(src)}, approved=True, approval_id=fresh)
    assert again["needs_confirm"], "an approval is spent by the call it approved"


# ------------------------------------------------------------------ S2: ui_act / watcher approvals bind to a plan ---

def test_ui_act_approval_needs_the_card_id_and_the_same_plan():
    h, _ = hands()
    target = {"app": "Notes", "role": "button", "name": "Save"}
    first = h.ui_act(target, "press", provenance="email")
    pid = first["confirm"]["plan_id"]
    bare = h.ui_act(target, "press", provenance="email", approved=True)
    assert bare["needs_confirm"], "approved=True alone is not an approval"
    other = h.ui_act({**target, "name": "Share"}, "press", provenance="email", approved=True, approval_id=pid)
    assert other["needs_confirm"] and other["confirm"]["plan_id"] != pid
    good = h.ui_act(target, "press", provenance="email", approved=True, approval_id=pid)
    assert not good.get("needs_confirm")


def test_watcher_create_approval_is_bound_to_the_spec():
    h, _ = hands()
    spec = {"kind": "skip_ads"}
    first = h.watcher_create(spec, provenance="web")
    pid = first["confirm"]["plan_id"]
    assert h.watcher_create(spec, provenance="web", approved=True)["needs_confirm"]
    assert h.watcher_create({"kind": "skip_ads", "expires_in_s": 60}, provenance="web", approved=True,
                            approval_id=pid)["needs_confirm"]
    ok = h.watcher_create(spec, provenance="web", approved=True, approval_id=pid)
    assert not ok.get("needs_confirm") and ok["ok"]


# ------------------------------------------------------------------ S3: ui_act on send / pay / delete controls ---

@pytest.mark.parametrize("name,cls", [("Send", "send"), ("Reply all", "send"), ("Pay now", "money"),
                                      ("Place order", "money"), ("Delete", "delete"), ("Empty Trash", "delete")])
def test_pressing_a_send_pay_or_delete_control_needs_a_physical_confirmation_even_for_the_owner(name, cls):
    h, _ = hands()
    r = h.ui_act({"app": "Mail", "role": "button", "name": name}, "press")
    assert r["needs_confirm"], name
    assert r["confirm"]["physical"] is True and r["confirm"]["risk"] == "physical"
    assert h.ui.risk_class({"name": name}, "press") == cls


def test_send_and_delete_key_chords_are_classified_but_plain_keys_are_not():
    h, _ = hands()
    for keys, cls in (("cmd+return", "send"), ("cmd+shift+d", "send"), ("cmd+delete", "delete")):
        assert h.ui.risk_class(None, "key", {"keys": keys}) == cls, keys
        assert h.ui_act(None, "key", {"keys": keys})["needs_confirm"]
    assert h.ui.risk_class(None, "key", {"keys": "cmd+t"}) is None
    assert h.ui.risk_class(None, "key", {"keys": "return"}) is None
    assert h.ui.risk_class({"name": "Save"}, "press") is None
    assert h.ui.risk_class({"name": "Send"}, "scroll") is None


# ------------------------------------------------------------------ S4: every confirming skill has a card ---

def test_every_confirming_or_dynamic_skill_can_describe_itself():
    blind = [n for n, s in registry.REGISTRY.items() if s.risk in ("dynamic", "confirm") and s.plan is None]
    assert blind == []


def test_dry_run_of_send_input_lists_what_would_be_typed():
    h, rt = hands()
    r = h.skill_run("ghostty.send_input", {"terminal_id": "T1", "text": "git push origin main"}, dry_run=True)
    row = r["confirm"]["rows"][0]
    assert row["text"] == "git push origin main" and row["will_run"] is True
    assert r["confirm"]["physical"] is True
    assert rt.calls == []


def test_a_claimed_created_by_hands_is_only_believed_for_ids_this_process_created():
    h, _ = hands()
    claim = {"tab_id": "owner-tab", "window_id": "owner-window", "created_by_hands": True}
    r = h.skill_run("ghostty.close", claim)
    assert r["needs_confirm"] and r["confirm"]["physical"] is True
    gh.note_created("tab", "our-tab")
    own = {"tab_id": "our-tab", "window_id": "owner-window", "created_by_hands": True}
    from app.fabric.skills.packs.skills_terminal import _close_risk

    assert _close_risk(own) == ("write", None)
    assert _close_risk({"window_id": "owner-window", "created_by_hands": True}) == ("run", "delete")
    assert _close_risk({"terminal_id": "x", "created_by_hands": True}) == ("run", "delete")


# ------------------------------------------------------------------ S5: a skip needs time the click could save ---

def _wm(rt, page):
    fb = FakeBrowser(rt)
    fb.add("w1", "t1", "Video", "http://localhost/p", page, active=True)
    return WebMedia(rt, [fb])


def test_an_ad_that_was_about_to_end_is_not_credited_to_the_click():
    rt = FakeRuntime()
    page = PageSim(rt, [{"kind": "S", "skip_after": 2, "len": 2.4}])
    r = _wm(rt, page).skip_ad()
    assert r["ok"] and r["verified"]
    assert r["say"] == "The ad ended on its own."
    assert r["evidence"]["skipped"] == 0


def test_an_ad_with_real_time_left_is_still_reported_as_skipped():
    rt = FakeRuntime()
    page = PageSim(rt, [{"kind": "S", "skip_after": 3, "len": 15}])
    r = _wm(rt, page).skip_ad()
    assert r["say"] == "Skipped the ad." and r["verified"] and page.skipped == 1
