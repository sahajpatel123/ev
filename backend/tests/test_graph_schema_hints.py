"""A planner tool hint whose arguments break the tool's schema is dropped.

The voice delegate's planner sees tool names, not argument schemas, so it
guesses argument names. Executed as given, the guess fails every time
(captured live on 2026-10-24, verbatim below). A node whose arguments do not
satisfy the capability schema loses its hint and routes to a worker that
calls the tool through its real schema, which is how the typed path succeeds.
"""

from __future__ import annotations

import pytest

from app.cognitive.graph import TaskTier, validate_dag


def _node(tool: str, arguments: dict, tier: str = "R") -> dict:
    return {
        "id": "n1", "label": "work", "detail": "do the work", "tier": tier,
        "tool": tool, "arguments": arguments,
    }


@pytest.mark.parametrize(
    ("tool", "arguments", "tier"),
    [
        # Live planner outputs that failed with invalid_request / "not on this kernel".
        ("pim.act", {"Oh": "read", "What": "calendar", "When": "today"}, "R"),
        ("pim.act", {"op": "calendar.list", "range": "today"}, "R"),
        ("timer.act", {"action": "set", "duration_seconds": 120}, "W"),
        ("timer.act", {"label": "2 minutes", "duration_seconds": 120}, "W"),
    ],
)
def test_schema_breaking_hints_are_dropped(tool: str, arguments: dict, tier: str) -> None:
    nodes = validate_dag([_node(tool, arguments, tier)], max_nodes=8)
    assert nodes[0].tool is None, f"{tool} {arguments} must route to a schema-aware worker"
    # The planner's extraction is kept for the worker to reuse.
    assert nodes[0].arguments == arguments
    assert nodes[0].tier is TaskTier(tier)


@pytest.mark.parametrize(
    ("tool", "arguments", "tier"),
    [
        ("pim.act", {"op": "calendar_events", "days": 1}, "R"),
        ("pim.act", {"op": "reminder_create", "title": "drink water", "list_name": "Errands"}, "W"),
        ("timer.act", {"op": "start", "seconds": 120, "label": "tea"}, "W"),
        ("memory.search", {"query": "favorite tea"}, "R"),
    ],
)
def test_schema_valid_hints_are_kept(tool: str, arguments: dict, tier: str) -> None:
    nodes = validate_dag([_node(tool, arguments, tier)], max_nodes=8)
    assert nodes[0].tool == tool
    assert nodes[0].arguments == arguments


def test_send_rewrite_still_happens_before_schema_checks() -> None:
    nodes = validate_dag(
        [_node("life.messages", {"to": "Mom", "text": "hi"}, "D")], max_nodes=8,
    )
    assert nodes[0].tool == "life.send"
    assert nodes[0].arguments == {"to": "Mom", "text": "hi"}
