"""RC: `ev hands check ...` accepts its documented spelling (with and without `check`)."""

from __future__ import annotations

from app.fabric.skills import check as check_mod


class _Hands:
    def permissions(self):
        return {"evidence": {"permissions": []}, "say": "stub"}


def _run(monkeypatch, argv):
    monkeypatch.setattr(check_mod, "Hands", _Hands)
    return check_mod.main(argv)


def test_check_prefix_spelling_works(monkeypatch, capsys):
    assert _run(monkeypatch, ["check", "permissions"]) == 0
    assert "stub" in capsys.readouterr().out


def test_bare_spelling_still_works(monkeypatch, capsys):
    assert _run(monkeypatch, ["permissions"]) == 0
    assert "stub" in capsys.readouterr().out
