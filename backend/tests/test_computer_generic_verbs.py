"""Generic in-app verbs (select/toggle/set/type/menu/save/…) for any app."""

from __future__ import annotations

from app.ev.computer_strategy import (
    resolve_generic_computer_goal,
    resolve_in_app_computer_goal,
)

_ALL = (resolve_in_app_computer_goal, resolve_generic_computer_goal)


def _mapped(utter: str):
    for resolve in _ALL:
        mapped = resolve(utter)
        assert mapped is not None, (utter, resolve.__name__)
        assert mapped[0] == "app_action", (utter, mapped)
    return resolve_in_app_computer_goal(utter)


def test_select_and_toggle_in_any_app() -> None:
    for utter, app, action in (
        ("select Message 3 in Mail", "Mail", "select"),
        ("choose the first conversation in WhatsApp", "WhatsApp", "select"),
        ("toggle dark mode in System Settings", "System Settings", "toggle"),
        ("enable notifications in System Settings", "System Settings", "toggle"),
    ):
        mapped = _mapped(utter)
        assert mapped[1]["app"].lower() == app.lower(), (utter, mapped)
        assert mapped[1]["action"] == action, (utter, mapped)


def test_set_value_and_type_in_any_app() -> None:
    mapped = _mapped("set the subject to hello in Mail")
    assert mapped[1]["action"] == "set_value", mapped
    assert mapped[1].get("value"), mapped

    typed = _mapped("type hello world in Bear")
    assert typed[1]["action"] == "type", typed
    assert "hello world" in str(typed[1].get("query") or "").lower(), typed


def test_menu_and_commit_verbs_in_any_app() -> None:
    for utter, action in (
        ("save in Pages", "save"),
        ("go back in Safari", "back"),
        ("new item in Reminders", "new_item"),
        ("share in Notes", "menu"),
    ):
        mapped = _mapped(utter)
        assert mapped[1]["action"] == action, (utter, mapped)


def test_scroll_to_in_any_app() -> None:
    mapped = _mapped("scroll to Settings in Slack")
    assert mapped[1]["action"] == "scroll_to", mapped
    assert "settings" in str(mapped[1].get("query") or "").lower(), mapped


def test_lookalike_apps_are_not_swallowed_by_adapters() -> None:
    from app.ev.computer_runtime import parse_owner_computer_goal
    from app.ev.computer_strategy import (
        _adapter_owns_content_verb,
        _slot_is_adapter_app,
        adapter_for,
    )

    assert adapter_for("YouTube Music") is None
    assert _slot_is_adapter_app("YouTube Music") is False
    assert _slot_is_adapter_app("GoodNotes") is False
    assert _slot_is_adapter_app("Apple Music") is True
    assert _slot_is_adapter_app("Music") is True
    assert _adapter_owns_content_verb("YouTube Music", "play", "play lofi") is False

    music_goal = parse_owner_computer_goal("play lofi in YouTube Music")
    assert "Music" not in music_goal.target_apps
    assert any("youtube music" in str(app).lower() for app in music_goal.target_apps)

    searched = _mapped("open youtube in YouTube Music")
    assert searched[1]["action"] == "search", searched
    assert searched[1]["app"] == "YouTube Music", searched

    notes_goal = parse_owner_computer_goal("search for cats in GoodNotes")
    assert "Notes" not in notes_goal.target_apps

    searched = _mapped("search for cats in GoodNotes")
    assert searched[1]["action"] == "search", searched
    assert searched[1]["app"] == "GoodNotes", searched

    firefox = _mapped("open youtube.com in Firefox")
    assert firefox[1]["action"] == "navigate", firefox
    assert firefox[1]["app"] == "Firefox", firefox


def test_from_app_phrase_and_random_playback() -> None:
    assert resolve_in_app_computer_goal("open lofi beats from Music") == (
        "app_action",
        {"app": "Music", "action": "search", "query": "lofi beats"},
    )
    playlist = resolve_in_app_computer_goal("open my lofi playlist from Apple Music")
    assert playlist is not None
    assert playlist[1]["action"] == "search", playlist
    assert playlist[1]["query"] == "lofi", playlist

    for utter in (
        "play some song randomly from Chill in Music",
        "play a random song from Chill in Apple Music",
    ):
        mapped = resolve_in_app_computer_goal(utter)
        assert mapped is not None, utter
        assert mapped[1]["action"] == "play", (utter, mapped)
        assert mapped[1].get("playlist") == "Chill", (utter, mapped)
        assert mapped[1].get("random") is True, (utter, mapped)

    bare = resolve_in_app_computer_goal("play random song in Music")
    assert bare is not None
    assert bare[1].get("random") is True, bare

    spotify = resolve_in_app_computer_goal("play random from lofi in Spotify")
    assert spotify is not None
    assert spotify[1]["action"] == "play" and spotify[1].get("random") is True, spotify
