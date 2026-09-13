"""Open-X-in-Y works for every installed app, not just Safari/Chrome.

The parser is structural (the app slot proves the words name an app), so no
per-app scripting is needed. Adapter-backed apps keep their dedicated path.
"""

from __future__ import annotations

from app.ev.computer_strategy import (
    _browser_app_from_text,
    looks_like_app_or_web_task,
    parse_open_intent,
    resolve_generic_computer_goal,
    resolve_in_app_computer_goal,
)


def test_open_site_in_any_browser_navigates_there() -> None:
    cases = (
        ("open youtube in firefox", "firefox", "youtube.com"),
        ("open github in Arc", "arc", "github.com"),
        ("open youtube.com in Brave", "brave", "youtube.com"),
    )
    for utter, app, host in cases:
        for resolve in (resolve_in_app_computer_goal, resolve_generic_computer_goal):
            mapped = resolve(utter)
            assert mapped is not None, (utter, resolve.__name__)
            assert mapped[0] == "app_action", (utter, mapped)
            assert mapped[1]["action"] == "navigate", (utter, mapped)
            assert mapped[1]["app"].lower() == app, (utter, mapped)
            url = str(mapped[1].get("url") or mapped[1].get("query") or "")
            assert host in url.lower(), (utter, mapped)
    safari = resolve_in_app_computer_goal("open youtube in Safari")
    assert safari is not None
    assert safari[0] == "app_action" and safari[1]["action"] == "navigate"
    assert safari[1]["app"] == "Safari"
    assert "youtube.com" in str(safari[1].get("url") or "").lower()


def test_open_phrase_in_plain_app_becomes_in_app_search() -> None:
    for resolve in (resolve_in_app_computer_goal, resolve_generic_computer_goal):
        mapped = resolve("open standup notes in slack")
        assert mapped is not None
        assert mapped[0] == "app_action", mapped
        assert mapped[1]["action"] == "search", mapped
        assert mapped[1]["app"].lower() == "slack", mapped
        assert "standup notes" in mapped[1]["query"].lower(), mapped


def test_compound_open_app_and_act_inside() -> None:
    for resolve in (resolve_in_app_computer_goal, resolve_generic_computer_goal):
        searched = resolve("Open Slack and search for standup notes")
        assert searched is not None
        assert searched[1]["app"].lower() == "slack", searched
        assert searched[1]["action"] == "search", searched
        assert searched[1]["query"].lower() == "standup notes", searched

        played = resolve("Open VLC and play Big Buck Bunny in it")
        assert played is not None
        assert played[1]["app"].lower() == "vlc", played
        assert played[1]["action"] == "play", played
        assert "big buck bunny" in played[1]["query"].lower(), played


def test_tab_chrome_in_any_app() -> None:
    mapped = resolve_in_app_computer_goal("open a new tab in Firefox")
    assert mapped is not None
    assert mapped[0] == "app_action"
    assert mapped[1]["app"].lower() == "firefox", mapped
    assert mapped[1]["action"] == "new_tab", mapped

    safari = resolve_in_app_computer_goal("Open a new tab in Safari")
    assert safari is not None
    assert safari[1]["app"] == "Safari"
    assert safari[1]["action"] == "new_tab"


def test_search_phrase_in_browser_becomes_engine_search() -> None:
    mapped = resolve_in_app_computer_goal("search for rust async in Firefox")
    assert mapped is not None
    assert mapped[0] == "app_action"
    assert mapped[1]["app"].lower() == "firefox", mapped
    assert mapped[1]["action"] == "navigate", mapped
    url = str(mapped[1].get("url") or "")
    assert "google.com/search" in url and "rust" in url, mapped


def test_open_file_in_any_app() -> None:
    for resolve in (resolve_in_app_computer_goal, resolve_generic_computer_goal):
        mapped = resolve("open /tmp/report.pdf in Preview")
        assert mapped is not None
        assert mapped[0] == "app_action", mapped
        assert mapped[1]["action"] == "open_item", mapped
        assert mapped[1]["app"].lower() == "preview", mapped
        assert "/tmp/report.pdf" in mapped[1]["query"], mapped


def test_adapter_apps_keep_dedicated_behavior() -> None:
    assert resolve_in_app_computer_goal("open youtube in safari") == (
        "app_action",
        {
            "app": "Safari",
            "action": "navigate",
            "query": "https://www.youtube.com/",
            "url": "https://www.youtube.com/",
        },
    )
    assert resolve_in_app_computer_goal("In Google Chrome, search for OpenAI.") == (
        "app_action",
        {"app": "Chrome", "action": "search", "query": "OpenAI"},
    )
    music = resolve_in_app_computer_goal(
        "Open Music, find the Chess playlist, and play the first track."
    )
    assert music is not None
    assert music[1]["app"] == "Music" and music[1]["playlist"] == "Chess"
    assert resolve_in_app_computer_goal("Open Safari") == ("open_app", {"name": "safari"})


def test_open_intent_routes_any_app_deterministically() -> None:
    opened = parse_open_intent("open youtube in firefox")
    assert opened is not None
    assert opened[0] == "app_action"
    assert opened[1]["action"] == "navigate"
    assert opened[1]["app"].lower() == "firefox"
    assert "youtube.com" in str(opened[1].get("url") or "").lower()
    assert parse_open_intent("open safari and check my email") is None


def test_any_app_goal_tracks_target_and_task() -> None:
    from app.ev.computer_runtime import parse_owner_computer_goal

    goal = parse_owner_computer_goal("open youtube in firefox")
    assert "firefox" in [str(app).lower() for app in goal.target_apps]
    assert looks_like_app_or_web_task("open youtube in firefox") is True
    assert looks_like_app_or_web_task("open standup notes in Slack") is True
    assert looks_like_app_or_web_task("Open Slack and search for standup notes") is True
    assert looks_like_app_or_web_task("open the note") is False


def test_firefox_is_a_first_class_browser() -> None:
    assert _browser_app_from_text("open youtube in firefox", None) == "Firefox"
    assert _browser_app_from_text("anything", "Firefox") == "Firefox"
    assert _browser_app_from_text("search for the story arc", None) is None
