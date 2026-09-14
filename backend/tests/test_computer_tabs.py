"""Tab intelligence: close the right tab, open a named site in another tab.

Pins the fixes for the live failures:
- "close that specific tab" must reach close_tab, not a garbage close_app
- "open youtube.com in another tab" must navigate there in a fresh tab,
  never open a blank tab or treat "another tab" as an app name
"""

from __future__ import annotations

from app.ev.computer_runtime import (
    close_tab_query_hint,
    ensure_state,
    remember_tab,
    reset_computer_states,
)
from app.ev.computer_strategy import (
    _resolve_open_content_in_app,
    looks_like_app_or_web_task,
    looks_like_computer_task,
    parse_open_intent,
    resolve_generic_computer_goal,
    resolve_in_app_computer_goal,
)

_ALL = (resolve_in_app_computer_goal, resolve_generic_computer_goal)


def test_close_demonstrative_tabs_reach_close_tab() -> None:
    for utter in (
        "close that specific tab",
        "close that tab",
        "close this tab",
        "close the current tab",
        "close the tab I just opened",
        "close the other tab",
    ):
        for resolve in _ALL:
            mapped = resolve(utter)
            assert mapped is not None, (utter, resolve.__name__)
            assert mapped[0] == "app_action", (utter, mapped)
            assert mapped[1]["action"] == "close_tab", (utter, mapped)


def test_close_named_tab_carries_the_target_host() -> None:
    for resolve in _ALL:
        mapped = resolve("close the youtube tab")
        assert mapped is not None, resolve.__name__
        assert mapped[1]["action"] == "close_tab"
        assert "youtube.com" in str(mapped[1].get("query") or ""), mapped


def test_close_all_tabs_is_a_close_all_action() -> None:
    for resolve in _ALL:
        mapped = resolve("close all tabs")
        assert mapped is not None, resolve.__name__
        assert mapped[0] == "app_action", resolve.__name__
        assert mapped[1]["action"] == "close_tab", mapped
        assert mapped[1].get("all") is True, mapped


def test_named_site_in_another_tab_is_navigated_not_blank() -> None:
    for utter, host in (
        ("open youtube.com in another tab", "youtube.com"),
        ("open youtube.com in a new tab", "youtube.com"),
        ("open youtube in another tab", "youtube.com"),
        ("open youtube.com in Firefox in another tab", "youtube.com"),
        ("open github.com in a new tab in Chrome", "github.com"),
    ):
        for resolve in _ALL:
            mapped = resolve(utter)
            assert mapped is not None, (utter, resolve.__name__)
            assert mapped[0] == "app_action", (utter, mapped)
            assert mapped[1]["action"] == "navigate", (utter, mapped)
            assert mapped[1].get("new_tab") is True, (utter, mapped)
            url = str(mapped[1].get("url") or mapped[1].get("query") or "").lower()
            assert host in url, (utter, mapped)


def test_named_browser_kept_when_another_tab_requested() -> None:
    mapped = resolve_in_app_computer_goal("open youtube.com in Firefox in another tab")
    assert mapped is not None
    assert mapped[1]["app"] == "Firefox"
    assert mapped[1].get("new_tab") is True


def test_bare_new_tab_stays_new_tab() -> None:
    for utter in ("open a new tab", "new tab"):
        for resolve in _ALL:
            mapped = resolve(utter)
            assert mapped is not None, (utter, resolve.__name__)
            assert mapped[1]["action"] == "new_tab", (utter, mapped)
    safari = resolve_in_app_computer_goal("open a new tab in Safari")
    assert safari is not None
    assert safari[1] == {"app": "Safari", "action": "new_tab"}


def test_tab_words_are_never_app_names() -> None:
    for utter in (
        "open youtube.com in another tab",
        "open youtube in a new tab",
        "open youtube.com in Firefox in another tab",
    ):
        assert _resolve_open_content_in_app(utter, None) is None, utter


def test_search_phrase_in_new_tab_requests_a_tab() -> None:
    mapped = resolve_in_app_computer_goal("search for cats in a new tab")
    assert mapped is not None
    assert mapped[1]["action"] == "search"
    assert mapped[1]["query"] == "cats"
    assert mapped[1].get("new_tab") is True


def test_close_tab_phrases_are_computer_tasks() -> None:
    for utter in ("close that specific tab", "close the youtube tab", "open a new tab"):
        assert looks_like_app_or_web_task(utter), utter
        assert looks_like_computer_task(utter), utter
    closed = parse_open_intent("close that specific tab")
    assert closed is not None
    assert closed[0] == "app_action"
    assert closed[1]["action"] == "close_tab"


def test_last_tab_hint_tracks_the_tab_we_opened() -> None:
    reset_computer_states()
    state = ensure_state("tab-hint")
    remember_tab(
        state,
        {"action": "navigate"},
        {"ok": True, "app": "Safari", "url": "https://www.youtube.com/watch?v=1"},
    )
    assert close_tab_query_hint(state) == "www.youtube.com"
    remember_tab(state, {"action": "navigate"}, {"ok": False, "url": "https://bad.example"})
    assert close_tab_query_hint(state) == "www.youtube.com"


def test_working_state_block_mentions_last_tab() -> None:
    from app.ev.computer_runtime import computer_working_state_block

    reset_computer_states()
    state = ensure_state("tab-block")
    remember_tab(
        state,
        {"action": "navigate"},
        {"ok": True, "app": "Safari", "url": "https://www.youtube.com/"},
    )
    block = computer_working_state_block(state.prompt_snapshot())
    assert "youtube.com" in block
