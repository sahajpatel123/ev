"""Local work detection: the router that sends machine tasks to the agent.

These cases are the difference between "Evie narrates the request" and "Evie
does it". Each one is a real owner phrasing; the assertion is only about the
route, not about any model call.
"""

from __future__ import annotations

import pytest

from app.agent.intake import looks_like_general_task
from app.cognitive.local_route import (
    describe_route,
    looks_like_local_work,
    looks_like_pure_question,
    needs_full_agent,
)


@pytest.mark.parametrize(
    "text",
    [
        "what files are on my desktop",
        "open my downloads folder",
        "create two files on my desktop called a.txt and b.txt",
        "I have a screenshot of ghostty in my desktop folder, launch ghostty and run those commands",
        "read this screenshot and tell me what it says",
        "list the sessions open in my terminal",
        "find all invoices in my downloads folder and move them to documents",
        "fix the bug in backend/app/x.py and run the tests",
        "resume my opencode session in ghostty",
    ],
)
def test_machine_work_routes_to_the_agent(text: str) -> None:
    assert looks_like_local_work(text) is True
    assert looks_like_general_task(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "append today's notes to the file on my desktop",
        "create a checklist and save it in a text document on my desktop",
    ],
)
def test_single_file_write_keeps_the_file_lane(text: str) -> None:
    assert looks_like_local_work(text) is True
    assert looks_like_general_task(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "what is ghostty",
        "how does tmux work",
        "open safari",
        "send an email to mom",
        "read my last email",
        "what's the weather",
        "who is Ada Lovelace",
        "remind me to call the dentist",
        "explain how the memory filter works",
    ],
)
def test_talk_and_simple_capabilities_keep_their_fast_path(text: str) -> None:
    assert looks_like_local_work(text) is False


def test_describe_route_names_the_signal() -> None:
    route = describe_route("I have a screenshot of ghostty, run those commands")
    assert route["local"] is True
    assert route["vision"] is True
    assert route["terminal"] is True


@pytest.mark.parametrize(
    "text",
    [
        "in one sentence, who is Ada Lovelace",
        "who is Ada Lovelace",
        "explain the file structure of this repo",
        "what is the capital of France",
    ],
)
def test_knowledge_questions_never_hit_the_file_parser(text: str) -> None:
    assert looks_like_pure_question(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "what files are on my desktop",
        "summarize this pdf on my desktop",
        "read the screenshot on my desktop",
        "what is my terminal showing",
    ],
)
def test_local_state_questions_are_not_pure(text: str) -> None:
    assert looks_like_pure_question(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "open Ghostty with two panes and run my usual checks",
        "open Ghosty with two panes and run my usual checks",
        "open a new Ghostty window and run echo hello in it",
        "resume my opencode session in ghostty",
        "read this screenshot and run the commands it shows",
    ],
)
def test_compound_machine_work_needs_the_full_agent(text: str) -> None:
    assert needs_full_agent(text) is True


@pytest.mark.parametrize(
    "text",
    ["open Ghostty", "open safari", "what files are on my desktop"],
)
def test_single_step_work_keeps_the_fast_path(text: str) -> None:
    assert needs_full_agent(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "check what is eating my disk",
        "what is using the most disk space on this mac",
        "how much free space is on my disk",
        "diagnose why my mac is slow",
    ],
)
def test_system_health_goals_route_to_the_agent(text: str) -> None:
    # Live-voice evidence: a disk-health goal misrouted to the research graph
    # (and its standing-scope approval) because no verb/noun/anchor fired.
    assert looks_like_local_work(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "am I free tomorrow",
        "check my memory of yesterday",
    ],
)
def test_health_verbs_do_not_capture_non_machine_turns(text: str) -> None:
    assert looks_like_local_work(text) is False
