"""Generic in-app actions work in every app, not just Safari.

Every non-browser app gets an in-app search for generic content, and the
semantic adapters (Music, Notes) stop swallowing or hijacking generic verbs.
"""

from __future__ import annotations

from app.ev.computer_strategy import (
    resolve_generic_computer_goal,
    resolve_in_app_computer_goal,
)

_ALL = (resolve_in_app_computer_goal, resolve_generic_computer_goal)

_LIVE_APPS = (
    "Mail",
    "WhatsApp",
    "Apple Music",
    "App Store",
    "Messages",
    "Notes",
    "Phone",
    "Calendar",
    "Reminders",
)


def test_search_in_any_app_stays_in_that_app() -> None:
    for app in _LIVE_APPS:
        for resolve in _ALL:
            mapped = resolve(f"search for youtube in {app}")
            assert mapped is not None, (app, resolve.__name__)
            assert mapped[0] == "app_action", (app, mapped)
            assert mapped[1]["action"] == "search", (app, mapped)
            assert mapped[1]["app"].lower() == app.lower(), (app, mapped)
            assert mapped[1]["query"] == "youtube", (app, mapped)


def test_open_web_url_in_non_browser_app_becomes_search() -> None:
    for app in ("WhatsApp", "Mail", "Apple Music", "Messages", "Notes"):
        for resolve in _ALL:
            mapped = resolve(f"open youtube.com in {app}")
            assert mapped is not None, (app, resolve.__name__)
            assert mapped[0] == "app_action", (app, mapped)
            assert mapped[1]["action"] == "search", (app, mapped)
            assert mapped[1]["app"].lower() == app.lower(), (app, mapped)
            assert mapped[1]["query"] == "youtube", (app, mapped)


def test_find_in_app_is_a_search() -> None:
    for utter in ("find youtube in WhatsApp", "locate youtube in Mail"):
        mapped = resolve_in_app_computer_goal(utter)
        assert mapped is not None, utter
        assert mapped[1]["action"] == "search", (utter, mapped)
        assert mapped[1]["query"] == "youtube", (utter, mapped)


def test_app_store_name_is_not_mangled() -> None:
    mapped = resolve_in_app_computer_goal("search for youtube in App Store")
    assert mapped is not None
    assert mapped[1]["app"] == "App Store", mapped
    assert mapped[1]["action"] == "search", mapped


def test_music_adapter_keeps_playlist_playback() -> None:
    music = resolve_in_app_computer_goal(
        "Open Music, find the Chess playlist, and play the first track."
    )
    assert music is not None
    assert music[1]["app"] == "Music"
    assert music[1]["playlist"] == "Chess"
    assert music[1]["action"] == "play"


def test_music_and_notes_generic_search_is_not_swallowed() -> None:
    music = resolve_in_app_computer_goal("search for youtube in Apple Music")
    assert music is not None
    assert music[1]["app"] == "Apple Music"
    assert music[1]["action"] == "search"

    notes = resolve_in_app_computer_goal("search for youtube in Notes")
    assert notes is not None
    assert notes[1]["app"] == "Notes"
    assert notes[1]["action"] == "search"


def test_notes_authoring_still_routes_to_the_adapter() -> None:
    created = resolve_in_app_computer_goal("Create a note that says Evie live computer use")
    assert created is not None
    assert created[1]["app"] == "Notes"
    assert created[1]["action"] == "create"

    read = resolve_in_app_computer_goal("Read the current note in Notes")
    assert read == ("app_action", {"app": "Notes", "action": "read"})


def test_browsers_keep_web_behavior() -> None:
    safari = resolve_in_app_computer_goal("search for youtube in Safari")
    assert safari is not None
    assert safari[1]["app"] == "Safari"
    assert safari[1]["action"] == "search"
    assert safari[1]["query"] == "youtube"

    firefox = resolve_in_app_computer_goal("search for youtube in Firefox")
    assert firefox is not None
    assert firefox[1]["action"] == "navigate"
    assert "google.com/search" in str(firefox[1].get("url") or "")
