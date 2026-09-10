"""Spoken-speed policy for Muse kernel turns."""

from __future__ import annotations

from app.cognitive.speed import (
    CONVERSATION_TOOL_NAMES,
    compact_turn,
    max_tool_turns,
    reasoning_effort,
    should_prefetch_memory,
    tool_specs_for_turn,
)


def test_compact_hello_is_low_effort_without_prefetch() -> None:
    assert compact_turn(text="How are you?", domain="open", has_work=False) is True
    assert reasoning_effort(domain="open", compact=True, has_work=False) == "low"
    assert should_prefetch_memory(compact=True) is False
    assert max_tool_turns(compact=True) == 4
    names = {spec.name for spec in tool_specs_for_turn(compact=True)}
    assert names == set(CONVERSATION_TOOL_NAMES)
    assert "code.act" not in names


def test_file_and_send_keep_work_effort_and_full_bus() -> None:
    assert compact_turn(text="Make a packing list on my Desktop", domain="file", has_work=False) is False
    assert reasoning_effort(domain="file", compact=False, has_work=False) == "medium"
    assert should_prefetch_memory(compact=False) is True
    names = {spec.name for spec in tool_specs_for_turn(compact=False)}
    assert "files.act" in names
    assert "code.act" in names
    assert "life.send" in names


def test_computer_open_is_not_compact() -> None:
    utter = "open safari and open youtube"
    assert compact_turn(text=utter, domain="computer", has_work=False) is False
    assert compact_turn(text=utter, domain="open", has_work=False) is False
    assert reasoning_effort(domain="computer", compact=False, has_work=False) == "medium"
    names = {spec.name for spec in tool_specs_for_turn(compact=False)}
    assert "computer.perform_effect" in names
