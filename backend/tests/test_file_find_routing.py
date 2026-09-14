"""Owner file finding stays local: any name, any folder, never web search."""

from __future__ import annotations

from pathlib import Path

from app.ev.computer_strategy import (
    looks_like_web_research,
    resolve_browser_computer_goal,
    resolve_generic_computer_goal,
    resolve_in_app_computer_goal,
)
from app.ev.laptop_files import (
    allowed_roots,
    looks_like_file_task,
    path_denied,
    resolve_file_computer_goal,
)


def test_find_my_file_phrases_are_file_tasks() -> None:
    for phrase in (
        "find my w2 file from laptop",
        "find my w2 file on my Mac",
        "where is my passport file",
        "locate my tax return document on my laptop",
        "search for my resume file",
        "find my thesis folder",
        "find my tax return",
        "look up my resume on my laptop",
        "look up the receipt on my laptop",
        "find the contract I downloaded",
    ):
        assert looks_like_file_task(phrase), phrase


def test_look_up_laptop_file_is_not_web_research() -> None:
    for phrase in (
        "look up my resume on my laptop",
        "look up the receipt on my laptop",
        "find my tax return",
    ):
        assert not looks_like_web_research(phrase), phrase
        assert resolve_file_computer_goal(phrase) is not None, phrase
        goal = resolve_file_computer_goal(phrase)
        assert goal is not None and goal[0] == "file_op"
        assert goal[1].get("action") in {"search", "open"}


def test_file_find_is_never_web_or_app_routing() -> None:
    for phrase in (
        "find my w2 file from laptop",
        "where is my passport file",
        "locate my tax return document on my laptop",
    ):
        assert not looks_like_web_research(phrase), phrase
        assert resolve_browser_computer_goal(phrase) is None, phrase
        assert resolve_in_app_computer_goal(phrase) is None, phrase
        assert resolve_generic_computer_goal(phrase) is None, phrase
        assert resolve_file_computer_goal(phrase) is not None, phrase


def test_public_questions_still_go_to_web() -> None:
    assert looks_like_web_research("search the web for EV charging standards")
    assert resolve_in_app_computer_goal("search the web for python 3.14") is None
    assert not looks_like_file_task("search the web for python 3.14")


def test_file_find_never_selects_web_search() -> None:
    from app.ev.spark_act import fallback_act, live_tool_for_act
    from app.ev.tool_select import resolve_live_action, select_tool

    for phrase in (
        "find my w2 file from laptop",
        "look up my resume on my laptop",
        "find my thesis folder",
        "where is my passport file",
        "search for my quarterly-report file on my Mac",
    ):
        picked = select_tool(phrase).selected
        assert picked != "search_web", (phrase, picked)
        assert picked != "find_gear", (phrase, picked)
        live = resolve_live_action(phrase)
        assert live is not None and live[0] == "computer", (phrase, live)
        decision = fallback_act(phrase)
        assert decision is not None and decision.act == "files", (phrase, decision)
        tool = live_tool_for_act(phrase, decision)
        assert tool is not None and tool[0] == "computer", (phrase, tool)
        assert tool[0] != "search_web"


def test_parse_file_goal_look_up_and_any_name() -> None:
    from app.ev.laptop_files import parse_file_goal

    looked = parse_file_goal("look up my resume on my laptop")
    assert looked is not None and looked["action"] == "search"
    assert "resume" in str(looked.get("query") or "").lower()

    named = parse_file_goal("find my w2 file from laptop")
    assert named is not None and named["action"] == "search"
    assert "w2" in str(named.get("query") or "").lower()

    nested = parse_file_goal("find my quarterly-report file from Documents")
    assert nested is not None and nested["action"] == "search"
    assert "quarterly-report" in str(nested.get("query") or "").lower()


def test_whole_home_is_searchable_with_secret_boundary() -> None:
    home = Path.home().resolve()
    roots = allowed_roots()
    assert home in roots
    assert path_denied(home / "Projects" / "notes.txt") is None
    assert path_denied(home / "Documents" / "tax.pdf") is None
    assert path_denied(home / ".ssh" / "id_rsa") == "path_denied"
    assert path_denied(home / ".aws" / "credentials") == "path_denied"
    assert path_denied(home / "Library" / "Caches" / "x.txt") == "path_denied"
    assert path_denied(home / "Library" / "Keychains" / "login.keychain") == "path_denied"
    icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if icloud.exists():
        assert path_denied(icloud / "note.txt") is None
