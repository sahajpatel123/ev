"""RC: `ev accept` checklist mechanics (state, resume, report) with temp paths."""

from __future__ import annotations

import json

from clients.cli.rc_commands import _ACCEPT_ITEMS, cmd_accept


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCEPT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ACCEPT_REPORT_PATH", str(tmp_path / "OWNER_ACCEPTANCE.md"))


def test_list_and_report_roundtrip(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    assert cmd_accept(["--list"]) == 0
    out = capsys.readouterr().out
    assert "permissions" in out and "iphone" in out
    assert cmd_accept(["--report"]) == 0
    md = (tmp_path / "OWNER_ACCEPTANCE.md").read_text()
    assert "0/10 required PASS" in md and "TODO" in md


def test_guided_answer_recorded_and_resumable(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    # first item is auto (permissions) — stub the auto runner
    import clients.cli.rc_commands as rc

    monkeypatch.setattr(rc, "_accept_auto", lambda item_id: (True, "stubbed"))
    assert cmd_accept([]) == 0  # records permissions PASS
    monkeypatch.setattr("builtins.input", lambda _p: "y looked calm")
    assert cmd_accept([]) == 0  # records keychain PASS (next pending)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["results"]["permissions"]["status"] == "PASS"
    assert state["results"]["keychain"]["status"] == "PASS"
    assert state["results"]["keychain"]["note"] == "looked calm"
    assert cmd_accept(["--redo=keychain"]) == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert "keychain" not in state["results"]
    assert cmd_accept(["--report"]) == 0
    md = (tmp_path / "OWNER_ACCEPTANCE.md").read_text()
    assert "1/10 required PASS" in md


def test_all_items_have_unique_ids_and_known_kinds():
    ids = [_id for _id, _k, _t, _d in _ACCEPT_ITEMS]
    assert len(ids) == len(set(ids)) == 11
    kinds = {k for _i, k, _t, _d in _ACCEPT_ITEMS}
    assert kinds <= {"auto", "guided", "guided-optional"}
