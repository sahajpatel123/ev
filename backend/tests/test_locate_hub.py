"""Mac-wide locate hub: no Code prior, sandbox map + file index as sensors."""

from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.ev.code_sandbox import reset_folder_map
from app.ev.locate_hub import hub_owns_ask, locate_named, software_lane, spoken_result
from app.ev.luna_code import looks_like_code_request
from app.ev.spark_act import fallback_act
from app.ev.tool_select import resolve_live_action


def _seed_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    (root / "README.md").write_text(f"# {root.name}\n", encoding="utf-8")
    return root


def _hub_env(tmp_path: Path, monkeypatch):
    from app.ev.code_runtime import clear_sticky_project
    from app.ev.luna_code import _LAST_CODE_JOBS

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    code_home = tmp_path / "Code"
    wish = _seed_project(code_home / "wish")
    (wish / "src").mkdir()
    (wish / "src" / "experience").mkdir()
    desk = tmp_path / "Desktop"
    invoices = desk / "Invoices"
    invoices.mkdir(parents=True)
    (invoices / "april.txt").write_text("receipt\n", encoding="utf-8")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    monkeypatch.setattr(settings, "environment", "dev")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr("app.ev.code_sandbox._desk_roots", lambda: [desk])
    reset_folder_map()
    _LAST_CODE_JOBS.clear()
    clear_sticky_project()
    return wish, invoices


def test_code_name_and_desk_name_are_equal_citizens(tmp_path: Path, monkeypatch) -> None:
    wish, invoices = _hub_env(tmp_path, monkeypatch)

    wish_hit = locate_named("what is the wish folder")
    assert wish_hit.status == "hit"
    assert wish_hit.is_software_unique
    assert wish_hit.best is not None
    assert wish_hit.best.path == wish.resolve()
    assert looks_like_code_request("what is the wish folder")
    assert resolve_live_action("what is the wish folder") == (
        "code",
        {"goal": "what is the wish folder"},
    )

    desk_hit = locate_named("find the Invoices folder")
    assert desk_hit.status == "hit"
    assert desk_hit.best is not None
    assert desk_hit.best.path == invoices.resolve()
    assert not software_lane(desk_hit.best)
    assert not looks_like_code_request("find the Invoices folder")
    assert hub_owns_ask("find the Invoices folder")
    assert resolve_live_action("find the Invoices folder")[0] == "computer"
    decision = fallback_act("find the Invoices folder")
    assert decision is not None and decision.act == "files"


def test_same_name_in_two_origins_is_a_fork_not_code_wins(
    tmp_path: Path, monkeypatch
) -> None:
    _hub_env(tmp_path, monkeypatch)
    twin_code = _seed_project(tmp_path / "Code" / "aurora")
    twin_desk = tmp_path / "Desktop" / "aurora"
    twin_desk.mkdir()
    (twin_desk / "notes.txt").write_text("desk\n", encoding="utf-8")
    reset_folder_map()

    result = locate_named("find the aurora folder")
    assert result.status == "ambiguous"
    origins = {item.origin for item in result.hits}
    assert "code" in origins
    assert "desktop" in origins
    assert not looks_like_code_request("find the aurora folder")
    assert hub_owns_ask("find the aurora folder")
    spoken = spoken_result(result).lower()
    assert "more than one place" in spoken
    assert twin_code.exists() and twin_desk.exists()


def test_unknown_name_misses_the_mac_not_the_code_folder(
    tmp_path: Path, monkeypatch
) -> None:
    _hub_env(tmp_path, monkeypatch)
    result = locate_named("what is the foobarbaz folder")
    assert result.status == "miss"
    from app.ev.locate_hub import spoken_result

    spoken = spoken_result(result).lower()
    assert "foobarbaz" in spoken
    assert "on this mac" in spoken
    assert "code folder" not in spoken
    assert not looks_like_code_request("what is the foobarbaz folder")
    assert not looks_like_code_request("explain gravity")
    assert resolve_live_action("explain gravity") is None or resolve_live_action(
        "explain gravity"
    )[0] not in {"code", "computer"}
