"""Coding-CLI session discovery, resume-syntax verification, id grounding."""

from __future__ import annotations

import random

import pytest

from app.fabric.skills.packs import coding_sessions as cs
from app.fabric.skills.runtime import Proc
from app.fabric.skills.testing import fake_cli_runtime, make_fake_home


@pytest.fixture
def world(tmp_path):
    truth = make_fake_home(tmp_path)
    rt = fake_cli_runtime(tmp_path)
    return tmp_path, truth, rt


def test_every_store_is_read_with_cwd_and_id(world):
    home, truth, rt = world
    rows = cs.all_sessions(rt, home)
    by_cli = {}
    for r in rows:
        by_cli.setdefault(r.cli, {})[r.id] = r
    for cli, items in truth.items():
        assert len(by_cli[cli]) == len(items), cli
        for item in items:
            assert by_cli[cli][item["id"]].cwd == item["cwd"], (cli, item)


def test_resume_syntax_is_verified_from_help_not_assumed(world):
    home, _, rt = world
    info = cs.discover(rt, home)
    assert info["claude"]["resume_template"] == "claude --resume {id}"
    assert info["codex"]["resume_template"] == "codex resume {id}"
    assert info["opencode"]["resume_template"] == "opencode -s {id}"
    assert info["muse"]["resume_template"] == "muse resume {id}"
    assert info["gemini"]["resume_mode"] == "index"
    assert all(i["resume_verified"] for i in info.values())


def test_changed_cli_help_makes_syntax_unverified(tmp_path):
    make_fake_home(tmp_path)
    # a future claude release that dropped --resume: nothing may be assumed
    rt = fake_cli_runtime(tmp_path, help_overrides={"claude": "Usage: claude [options]\n  --print\n"})
    prov = cs.ClaudeProvider(rt, tmp_path)
    assert prov.syntax().verified is False
    assert prov.resume_command("11111111-1111-1111-1111-111111111111") is None


def test_missing_cli_is_reported_not_installed(tmp_path):
    rt = fake_cli_runtime(tmp_path, installed=("codex",))
    info = cs.discover(rt, tmp_path)
    assert info["claude"]["installed"] is False and info["claude"]["resume_verified"] is False
    assert info["codex"]["installed"] is True


def test_resume_command_quotes_ids(world):
    home, truth, rt = world
    sid = truth["codex"][0]["id"]
    assert cs.CodexProvider(rt, home).resume_command(sid) == f"codex resume {sid}"
    assert cs.OpencodeProvider(rt, home).resume_command("ses_AbC123") == "opencode -s ses_AbC123"
    # an id with shell metacharacters can never become a command
    cmd = cs.CodexProvider(rt, home).resume_command("x; rm -rf ~")
    assert cmd == "codex resume 'x; rm -rf ~'"


@pytest.mark.parametrize("cli", ["claude", "codex", "muse"])
def test_exact_id_is_exact(world, cli):
    home, truth, rt = world
    rows = cs.all_sessions(rt, home)
    sid = truth[cli][0]["id"]
    g = cs.ground_id(sid, rows, cli=cli)
    assert g["status"] == "exact" and g["executable"] and g["session"]["id"] == sid


def test_opencode_exact(world):
    home, truth, rt = world
    rows = cs.all_sessions(rt, home)
    sid = truth["opencode"][2]["id"]
    assert cs.ground_id(sid, rows, cli="opencode")["status"] == "exact"


# OCR confusions: 0/O, 1/l/I, 5/S, 8/B. Hex ids contain 0,1,5,8 so the OCR error is the
# letter that looks like the digit.
@pytest.mark.parametrize("digit,wrong", [("0", "O"), ("0", "o"), ("1", "l"), ("1", "I"),
                                         ("5", "S"), ("8", "B")])
def test_confusable_misread_is_corrected_to_the_real_id(tmp_path, digit, wrong):
    truth = make_fake_home(tmp_path, seed=11)
    rt = fake_cli_runtime(tmp_path)
    rows = cs.all_sessions(rt, tmp_path)
    cand = next(t for t in truth["codex"][:4] if digit in t["id"])
    raw = cand["id"].replace(digit, wrong, 1)
    g = cs.ground_id(raw, rows, cli="codex")
    assert g["status"] == "corrected", g
    assert g["session"]["id"] == cand["id"] and g["executable"]
    assert g["changes"], "a correction must list what changed"


def test_rn_for_m_digraph_in_a_title_like_token(tmp_path):
    # opencode ids are mixed-case alphanumerics: rn <-> m is the classic OCR split
    truth = make_fake_home(tmp_path, seed=3)
    rt = fake_cli_runtime(tmp_path)
    rows = cs.all_sessions(rt, tmp_path)
    cand = next((t for t in truth["opencode"] if "m" in t["id"][4:]), None)
    if cand is None:
        pytest.skip("seed produced no m")
    i = cand["id"].index("m", 4)
    raw = cand["id"][:i] + "rn" + cand["id"][i + 1:]
    g = cs.ground_id(raw, rows, cli="opencode")
    assert g["status"] == "corrected" and g["session"]["id"] == cand["id"]


def test_confusable_real_pair_is_ambiguous_and_never_executable(tmp_path):
    truth = make_fake_home(tmp_path, seed=7)
    rt = fake_cli_runtime(tmp_path)
    rows = cs.all_sessions(rt, tmp_path)
    a, b = truth["codex"][-2]["id"], truth["codex"][-1]["id"]
    diff = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
    assert len(diff) == 1
    i = diff[0]
    # the OCR read cannot say whether the glyph was 6 or b
    raw = a[:i] + "G" + a[i + 1:]
    g = cs.ground_id(raw, rows, cli="codex")
    assert g["status"] == "ambiguous" and not g["executable"]
    assert {c["id"] for c in g["candidates"]} == {a, b}
    # cwd evidence may break the tie only when it is unique
    cwd_a = truth["codex"][-2]["cwd"]
    cwd_b = truth["codex"][-1]["cwd"]
    if cwd_a != cwd_b:
        g2 = cs.ground_id(raw, rows, cli="codex", cwd=cwd_a)
        assert g2["status"] == "corrected" and g2["session"]["id"] == a


def test_unknown_id_is_unmatched_not_guessed(world):
    home, _, rt = world
    rows = cs.all_sessions(rt, home)
    rng = random.Random(99)
    fake = "".join(rng.choice("0123456789abcdef") for _ in range(32))
    fake = f"{fake[:8]}-{fake[8:12]}-{fake[12:16]}-{fake[16:20]}-{fake[20:]}"
    g = cs.ground_id(fake, rows, cli="codex")
    assert g["status"] == "unmatched" and not g["executable"] and g["session"] is None


def test_unique_prefix_is_completed_but_short_prefix_is_not(world):
    home, truth, rt = world
    rows = cs.all_sessions(rt, home)
    sid = truth["claude"][0]["id"]
    g = cs.ground_id(sid[:13] + "…", rows, cli="claude")
    assert g["status"] == "completed" and g["session"]["id"] == sid
    short = cs.ground_id(sid[:4], rows, cli="claude")
    assert short["status"] == "unreadable" and not short["executable"]


def test_unreadable_garbage_is_never_executable(world):
    home, _, rt = world
    rows = cs.all_sessions(rt, home)
    for raw in ("", "???", "ab", "........."):
        assert not cs.ground_id(raw, rows)["executable"]


def test_case_difference_is_a_correction(world):
    home, truth, rt = world
    rows = cs.all_sessions(rt, home)
    sid = truth["muse"][0]["id"]
    g = cs.ground_id(sid.upper(), rows, cli="muse")
    assert g["status"] in {"corrected", "exact"} and g["session"]["id"] == sid


# ------------------------------------------------------- pane text parsing --

def test_find_resume_commands_all_clis_and_ocr_noise():
    text = (
        "Resume this session with:\n claude --resume 0d9c8f5e-1111-4222-8333-444455556666\n"
        "codex resume 01a02222-84cd-7d73-9c85-22ec969143b1\n"
        "c0dex resume 01a02278-12e5-7013-aa68-7e34659a9a71\n"
        "opencode -s ses_ea2d55b1affeOzR7a8LMb3sQq1\n"
        "muse resume 01a1785a-2190-7d4f-b3a2-5a0e5f7c1d2e\n"
        "gemini --resume 5\n")
    found = cs.find_resume_commands(text)
    assert [f["cli"] for f in found] == ["claude", "codex", "codex", "opencode", "muse", "gemini"]
    assert found[0]["id_raw"] == "0d9c8f5e-1111-4222-8333-444455556666"
    assert found[2]["cli"] == "codex"  # the OCR'd c0dex was still read as codex
    assert found[5]["id_raw"] == "5"


def test_wrapped_uuid_across_lines_is_rejoined():
    text = "codex resume 01a02222-84cd-7d73-\n9c85-22ec969143b1\n$ "
    (f,) = cs.find_resume_commands(text)
    assert f["id_raw"] == "01a02222-84cd-7d73-9c85-22ec969143b1"


def test_find_paths_prefers_real_looking_paths():
    text = "~/Code/ev  on main\n/Users/sahajpatel/Code/orb\nnot a path: a/b\n"
    assert cs.find_paths(text) == ["~/Code/ev", "/Users/sahajpatel/Code/orb"]


# ------------------------------------------------------------- running ------

def test_running_elsewhere_strong_by_argv(world):
    home, truth, rt = world
    sid = truth["opencode"][0]["id"]
    rt.when(r"ps -axo", Proc(True, out=f"  4242 ttys003 /home/x/.opencode/bin/opencode.real -s {sid}\n"
                                       "  17 ?? /usr/sbin/cfprefsd agent\n"), kind="run")
    row = next(r for r in cs.all_sessions(rt, home) if r.id == sid)
    hit = cs.running_elsewhere(row, rt, home)
    assert hit and hit["level"] == "strong" and hit["tty"] == "ttys003"


def test_running_elsewhere_none_when_not_running(world):
    home, truth, rt = world
    rt.when(r"ps -axo", Proc(True, out="  17 ?? /usr/sbin/cfprefsd agent\n"), kind="run")
    row = next(r for r in cs.all_sessions(rt, home) if r.cli == "codex")
    assert cs.running_elsewhere(row, rt, home) is None


def test_running_elsewhere_weak_needs_fresh_session_and_same_cwd(world):
    home, truth, rt = world
    row = next(r for r in cs.all_sessions(rt, home) if r.cli == "claude")
    rt.when(r"ps -axo", Proc(True, out="  900 ttys005 claude\n"), kind="run")
    rt.when(r"lsof -a -p 900", Proc(True, out=f"p900\nn{row.cwd}\n"), kind="run")
    stale = cs.running_elsewhere(row, rt, home, now=row.updated_at + 5000)
    assert stale is None  # the session has not changed recently: no claim
    fresh = cs.running_elsewhere(row, rt, home, now=row.updated_at + 30)
    assert fresh and fresh["level"] == "weak" and fresh["pid"] == 900


def test_muse_lock_held_is_strong(world):
    home, truth, rt = world
    sid = truth["muse"][0]["id"]
    lock = home / ".local/share/muse/session-admission-locks" / f"session-v1-{sid}.lock"
    lock.write_text("")
    rt.when(r"ps -axo", Proc(True, out=""), kind="run")
    rt.when(r"lsof -t", Proc(True, out="777\n"), kind="run")
    row = next(r for r in cs.all_sessions(rt, home) if r.id == sid)
    hit = cs.running_elsewhere(row, rt, home)
    assert hit and hit["level"] == "strong" and hit["pid"] == 777


def test_session_listing_survives_a_corrupt_store(tmp_path):
    make_fake_home(tmp_path)
    (tmp_path / ".local/share/muse/session-index.db").write_bytes(b"not a database")
    rt = fake_cli_runtime(tmp_path)
    rows = cs.all_sessions(rt, tmp_path)
    assert {r.cli for r in rows} == {"claude", "codex", "opencode"}
