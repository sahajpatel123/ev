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
    ):
        assert looks_like_file_task(phrase), phrase


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
