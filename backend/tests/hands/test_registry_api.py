"""Registry, policy gate (one approval authority), world model, and the Hands facade."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.fabric.skills import registry
from app.fabric.skills.api import Hands, hands_api_version
from app.fabric.skills.packs import load_all
from app.fabric.skills.runtime import Proc
from app.fabric.skills.testing import (
    FakeBrowser,
    FakeRuntime,
    PageSim,
    fake_cli_runtime,
    make_fake_home,
)
from app.fabric.skills.world import match, parse_lsappinfo


@pytest.fixture(autouse=True)
def _packs():
    load_all()


def hands(rt=None, **kw):
    rt = rt or FakeRuntime()
    return Hands(rt, state_dir=kw.pop("state_dir", None), **kw), rt


def test_api_version_and_skill_list_shape():
    h, _ = hands()
    assert hands_api_version() == "1.0.0"
    r = h.skill_list()
    assert r["evidence"]["api_version"] == "1.0.0" and r["evidence"]["n"] >= 55
    names = {s["name"] for s in r["evidence"]["skills"]}
    for must in ("web_media.skip_ad", "coding_sessions.restore_from_screenshot", "ghostty.new_tab_with_layout",
                 "browser.fill_form", "finder.reveal", "system.volume", "music.play", "calls.mute", "editors.goto_line"):
        assert must in names
    assert all({"name", "pack", "summary", "args_schema", "risk", "permissions", "channels", "status"} <= set(s)
               for s in r["evidence"]["skills"])


@pytest.mark.parametrize("intent,expect", [
    ("skip the ad", "web_media.skip_ad"),
    ("could you skip that ad for me please", "web_media.skip_ad"),
    ("open a new tab with the same layout and resume those coding sessions", "coding_sessions.restore_from_screenshot"),
    ("go back 10 seconds", "web_media.seek"),
    ("pause the video", "web_media.pause"),
    ("reveal that file in finder", "finder.reveal"),
    ("mute my mic", "calls.mute"),
    ("is my mac locked", "system.lock_state"),
    ("list my recent coding sessions", "coding_sessions.list_recent"),
])
def test_find_ranks_the_right_skill_first(intent, expect):
    h, _ = hands()
    got = h.skill_find(intent)["evidence"]["skills"]
    assert got[0]["name"] == expect and got[0]["score"] >= 0.7


def test_find_has_expected_args_schema_and_preconditions():
    h, _ = hands()
    top = h.skill_find("skip the ad")["evidence"]["skills"][0]
    assert top["risk"] == "low" and "automation:browser" in top["permissions"]


def test_unknown_skill_and_bad_args_are_honest():
    h, _ = hands()
    assert h.skill_run("nope.nothing")["error"]["code"] == "not_found"
    r = h.skill_run("ghostty.split", {})
    assert r["error"]["code"] == "invalid_args"
    r = h.skill_run("system.mute", {"muted": "yes"})
    assert r["error"]["code"] == "invalid_args"


def test_low_risk_runs_without_a_card_for_the_owner_but_asks_for_untrusted_provenance():
    rt = FakeRuntime()
    page = PageSim(rt, [{"kind": "S", "skip_after": 2, "len": 10}])
    fb = FakeBrowser(rt)
    fb.add("w", "t", "Video", "http://localhost/p", page, active=True)
    h, _ = hands(rt, browsers=[fb])
    owner = h.skill_run("web_media.skip_ad")
    assert owner["ok"] and owner["verified"] and not owner["needs_confirm"]
    page2 = PageSim(rt, [{"kind": "S", "skip_after": 2, "len": 10}])
    fb2 = FakeBrowser(rt)
    fb2.add("w", "t", "Video", "http://localhost/p", page2, active=True)
    h2, _ = hands(rt, browsers=[fb2])
    web = h2.skill_run("web_media.skip_ad", provenance="web")
    assert web["needs_confirm"] and web["error"]["code"] == "confirm_required"
    assert page2.clicks == 0, "an untrusted-triggered effect must not run before confirmation"
    assert web["confirm"]["plan_id"].startswith("plan-")


def test_confirm_flow_binds_the_approval_to_the_exact_plan():
    h, rt = hands()
    args = {"layout": {"kind": "split", "dir": "right", "ratio": 0.5,
                       "a": {"kind": "pane", "id": 1}, "b": {"kind": "pane", "id": 2}},
            "panes": [{"cwd": "/tmp", "command": "echo hi"}, {"cwd": "/tmp", "command": ""}]}
    first = h.skill_run("ghostty.new_tab_with_layout", args)
    assert first["needs_confirm"] and not first["ok"]
    card = first["confirm"]
    assert card["rows"][0]["command"] == "echo hi" and card["physical"] is False
    # approved without the card's id: still held
    again = h.skill_run("ghostty.new_tab_with_layout", args, approved=True)
    assert again["needs_confirm"]
    # different args with the old id: held again (the approval does not transfer)
    other = dict(args, panes=[{"cwd": "/tmp", "command": "echo OTHER"}, {"cwd": "/tmp", "command": ""}])
    swapped = h.skill_run("ghostty.new_tab_with_layout", other, approved=True, approval_id=card["plan_id"])
    assert swapped["needs_confirm"] and swapped["confirm"]["plan_id"] != card["plan_id"]
    assert not rt.ran("new window|new tab")  # nothing was built


def test_dangerous_commands_require_a_physical_confirmation():
    h, _ = hands()
    args = {"layout": {"kind": "pane", "id": 1}, "panes": [{"cwd": "/tmp", "command": "git push origin main"}]}
    r = h.skill_run("ghostty.new_tab_with_layout", args)
    assert r["confirm"]["physical"] is True and r["confirm"]["risk"] == "physical"
    safe = h.skill_run("ghostty.new_tab_with_layout",
                       {"layout": {"kind": "pane", "id": 1}, "panes": [{"cwd": "/tmp", "command": "echo ok"}]})
    assert safe["confirm"]["physical"] is False


def test_dry_run_returns_the_plan_and_performs_nothing():
    h, rt = hands()
    args = {"layout": {"kind": "pane", "id": 1}, "panes": [{"cwd": "/tmp", "command": "echo ok"}]}
    r = h.skill_run("ghostty.new_tab_with_layout", args, dry_run=True)
    assert r["ok"] and r["evidence"]["dry_run"] and r["plan"] and r["needs_confirm"]
    assert rt.calls == []


def test_policy_denies_secret_paths_everywhere():
    h, rt = hands()
    assert h.skill_run("finder.reveal", {"path": "/Users/x/.ssh/secret.pem"})["error"]["code"] in ("denied", "not_found")
    assert h.skill_run("finder.reveal", {"path": "/tmp/my-token-file"})["error"]["code"] in ("denied", "not_found")


def test_finder_move_inside_roots_is_auto_and_outside_asks(tmp_path):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    f = home / "Documents" / "a.txt"
    f.write_text("hello")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    h, rt = hands(home=home)
    ok = h.skill_run("finder.move", {"src": str(f), "dst": str(home / "Documents" / "b.txt")})
    assert ok["ok"] and ok["verified"] and (home / "Documents" / "b.txt").read_text() == "hello"
    assert ok["undo"]["skill"] == "finder.move"
    asked = h.skill_run("finder.move", {"src": str(home / "Documents" / "b.txt"), "dst": str(outside / "b.txt")})
    assert asked["needs_confirm"]
    assert (home / "Documents" / "b.txt").exists() and not (outside / "b.txt").exists()
    # undo restores it
    undo = ok["undo"]
    back = h.skill_run(undo["skill"], undo["args"])
    assert back["ok"] and f.exists()


def test_finder_rename_rejects_traversal_and_collisions(tmp_path):
    home = tmp_path / "h"
    (home / "Desktop").mkdir(parents=True)
    a, b = home / "Desktop" / "a.txt", home / "Desktop" / "b.txt"
    a.write_text("1")
    b.write_text("2")
    h, _ = hands(home=home)
    assert h.skill_run("finder.rename", {"path": str(a), "new_name": "../evil"})["error"]["code"] == "invalid_args"
    assert h.skill_run("finder.rename", {"path": str(a), "new_name": "b.txt"})["error"]["code"] == "precondition_failed"
    r = h.skill_run("finder.rename", {"path": str(a), "new_name": "c.txt"})
    assert r["ok"] and r["verified"] and (home / "Desktop" / "c.txt").exists()


def test_success_claims_are_stripped_when_nothing_was_verified():
    registry.register(registry.Skill(name="test.unverified", summary="x", triggers=["unverified thing"],
                                     run=lambda ctx, args: R_ok_unverified(), risk="low"))
    h, _ = hands()
    r = h.skill_run("test.unverified")
    assert r["ok"] and not r["verified"] and r["say"] is None


def R_ok_unverified():
    from app.fabric.skills import receipts as R

    return R.receipt(True, verified=False, say="All done!")


def test_a_crashing_skill_becomes_an_internal_receipt():
    def boom(ctx, args):
        raise RuntimeError("kaput")

    registry.register(registry.Skill(name="test.boom", summary="x", triggers=["boom"], run=boom, risk="low"))
    h, _ = hands()
    r = h.skill_run("test.boom")
    assert r["ok"] is False and r["error"]["code"] == "internal" and "kaput" in r["error"]["message"]


def test_async_facade_runs_the_same_methods():
    h, _ = hands()
    r = asyncio.run(h.aio.skill_find("skip the ad"))
    assert r["evidence"]["skills"][0]["name"] == "web_media.skip_ad"


# ------------------------------------------------------------------ world ----

def test_where_filters_exact_contains_and_in():
    item = {"app": "Ghostty", "title": "ev main", "pid": 7, "ad": {"active": True}}
    assert match(item, {"app": "ghostty"}) and match(item, {"title_contains": "MAIN"})
    assert match(item, {"pid_in": [1, 7]}) and match(item, {"ad.active": True})
    assert not match(item, {"app": "Safari"}) and not match(item, {"title_contains": "zzz"})


LSAPP = ''' 1) "Ghostty" ASN:0x0-0x11011:
    bundleID="com.mitchellh.ghostty"
    bundle path="/Applications/Ghostty.app"
    pid = 769 token=[sess=1 pid=769] type="Foreground" flavor=3
 2) "universalaccessd" ASN:0x0-0xe00e:
    bundleID=[ NULL ]
    pid = 737 token=[x] !signalled type="BackgroundOnly" flavor=3
 3) "1Password" ASN:0x0-0xf00f:
    bundleID="com.1password.1password"
    pid = 900 token=[x] type="Foreground" flavor=3
'''


def test_world_apps_hides_password_managers_and_background_helpers():
    assert [a["name"] for a in parse_lsappinfo(LSAPP)] == ["Ghostty", "universalaccessd", "1Password"]
    rt = FakeRuntime()
    rt.when(r"^lsappinfo list", Proc(True, out=LSAPP), kind="run")
    rt.when(r"^lsappinfo front", Proc(True, out="ASN:0x0-0x11011:"), kind="run")
    h = Hands(rt)
    r = h.world_query("apps")
    names = [a["name"] for a in r["evidence"]["items"]]
    assert names == ["Ghostty"] and r["evidence"]["excluded"] == 1
    assert r["evidence"]["items"][0]["frontmost"] is True


def test_world_tabs_playing_enriches_and_excludes_private_and_banking():
    rt = FakeRuntime()
    ad = PageSim(rt, [{"kind": "S", "skip_after": 5, "len": 15}])
    fb = FakeBrowser(rt)
    fb.add("w1", "t1", "Cats", "https://www.youtube.com/watch?v=1", ad, active=True)
    fb.add("w1", "t2", "Bank", "https://www.chase.com/x", PageSim(rt, []))
    fb.add("w2", "t3", "Secret", "https://example.com/", PageSim(rt, []), incognito=True)
    fb.add("w1", "t4", "Docs", "https://docs.example.com/", None)
    h = Hands(rt, browsers=[fb])
    r = h.world_query("tabs", {"playing": True, "url_contains": "youtube.com"})
    items = r["evidence"]["items"]
    assert [t["tab_id"] for t in items] == ["t1"] and items[0]["ad"] is True
    allt = h.world_query("tabs")["evidence"]["items"]
    assert {t["tab_id"] for t in allt} == {"t1", "t4"}  # banking and private never appear
    media = h.world_query("media")["evidence"]["items"]
    assert media and media[0]["state"] == "ad"


def test_world_system_reports_lock_and_volume_and_caches_within_ttl():
    rt = FakeRuntime()
    rt.sys.update(locked=True, idle_s=12.0)
    rt.when(r"get volume settings", "35,false")
    h = Hands(rt)
    s1 = h.world_query("system")["evidence"]["items"][0]
    assert s1["locked"] is True and s1["volume"] == 35 and s1["muted"] is False
    n = len(rt.calls)
    h.world_query("system")
    assert len(rt.calls) == n  # served from cache
    h.world_query("system", refresh=True)
    assert len(rt.calls) > n


def test_world_terminals_merges_ghostty_and_tmux():
    from app.fabric.skills.packs.ghostty import SEP

    rt = FakeRuntime()
    rt.when(r"^ps ", Proc(True, out="/Applications/Ghostty.app/Contents/MacOS/ghostty\n"), kind="run")
    rt.when(r"repeat with w in windows", SEP.join(["W", "T", "1", "true", "TERM", "/Users/x/Code/ev", "title"]) + "\n")
    rt.when(r"^ls /private/tmp/tmux", Proc(True, out="default\nev-liveproof\n"), kind="run")
    rt.when(r"^tmux -L ev-liveproof list-panes",
            Proc(True, out="sess\x1f0\x1f%1\x1f/tmp\x1fzsh\x1ft\x1f123\n"), kind="run")
    h = Hands(rt)
    items = h.world_query("terminals")["evidence"]["items"]
    assert {i["app"] for i in items} == {"Ghostty", "tmux"}
    assert any(i.get("socket") == "ev-liveproof" for i in items)


# ---------------------------------------------------------- coding skills ----

def test_coding_sessions_skills_end_to_end(tmp_path):
    truth = make_fake_home(tmp_path)
    rt = fake_cli_runtime(tmp_path)
    rt.when(r"^ps ", Proc(True, out=""), kind="run")
    h = Hands(rt, home=tmp_path)
    d = h.skill_run("coding_sessions.discover")
    assert d["ok"] and "codex" in d["say"]
    sid = truth["codex"][0]["id"]
    v = h.skill_run("coding_sessions.validate_id", {"id": sid.upper(), "cli": "codex"})
    assert v["evidence"]["session"]["id"] == sid
    c = h.skill_run("coding_sessions.resume_command", {"id": sid})
    assert c["say"] == f"codex resume {sid}"
    bogus = h.skill_run("coding_sessions.resume_command", {"id": "01a99999-aaaa-7bbb-8ccc-dddd00001111"})
    assert bogus["ok"] is False and bogus["error"]["code"] == "not_found"
    rr = h.skill_run("coding_sessions.list_recent", {"cli": "muse", "limit": 3})
    assert len(rr["evidence"]["sessions"]) == 3 and rr["evidence"]["untrusted"] is True
    run = h.skill_run("coding_sessions.detect_running", {"id": sid})
    assert run["ok"] and "isn't open" in run["say"]


def test_restore_from_screenshot_dry_run_gives_the_confirm_card(tmp_path):
    from app.fabric.skills import synth

    truth = make_fake_home(tmp_path)
    rt = fake_cli_runtime(tmp_path)
    rt.when(r"^ps ", Proc(True, out=""), kind="run")
    img, tree, boxes = synth.render("two_cols", seed=3, return_boxes=True)
    shot = tmp_path / "Screenshot 2026-10-26.png"
    shot.write_bytes(img.to_png())
    # real directories for the sessions' cwds
    for items in truth.values():
        for it in items:
            (tmp_path / "w" / it["cwd"].strip("/")).mkdir(parents=True, exist_ok=True)
    _repoint(tmp_path, truth)
    texts = [f"codex resume {truth['codex'][0]['id']}", f"claude --resume {truth['claude'][1]['id']}"]
    lines = synth.ocr_lines(boxes, texts, img.w, img.h)
    h = Hands(rt, home=tmp_path, ocr_fn=lambda p: {"ok": True, "lines": lines},
              extra={"last_image": str(shot)})
    r = h.skill_run("coding_sessions.restore_from_screenshot", dry_run=True)
    assert r["ok"] and r["needs_confirm"] and r["evidence"]["dry_run"]
    assert [row["command"].split()[0] for row in r["confirm"]["rows"]] == ["codex", "claude"]
    assert all(row["untrusted_source"] for row in r["confirm"]["rows"])
    assert "Open a new Ghostty tab with 2 panes" in r["confirm"]["title"]
    # without approval nothing is built
    held = h.skill_run("coding_sessions.restore_from_screenshot")
    assert held["needs_confirm"] and not rt.ran("new tab in window|new window")


def _repoint(tmp, truth):
    import json as _j

    base = tmp / "w"
    for f in (tmp / ".claude/projects").glob("*/*.jsonl"):
        rows = [_j.loads(x) for x in f.read_text().splitlines()]
        for r in rows:
            r["cwd"] = str(base / r["cwd"].strip("/"))
        f.write_text("\n".join(_j.dumps(r) for r in rows) + "\n")
    for f in (tmp / ".codex/sessions").glob("*/*/*/*.jsonl"):
        row = _j.loads(f.read_text().splitlines()[0])
        row["payload"]["cwd"] = str(base / row["payload"]["cwd"].strip("/"))
        f.write_text(_j.dumps(row) + "\n")
    for db, sql in (((tmp / ".local/share/opencode/opencode.db"), "update session set directory=? where directory=?"),
                    ((tmp / ".local/share/muse/session-index.db"), "update sessions set workspace_root=? where workspace_root=?")):
        con = sqlite3.connect(db)
        for w in {it["cwd"] for items in truth.values() for it in items}:
            con.execute(sql, (str(base / w.strip("/")), w))
        con.commit()
        con.close()


_ = json
