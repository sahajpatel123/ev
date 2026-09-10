"""General computer-use intelligence: doctrine, live state, continuation, app breadth.

These tests pin the behaviors that make Evie usable across arbitrary apps
instead of a per-app script: a live work-state block for the provider, a
general recovery doctrine, continuation of a running goal, and app lookup from
the Mac's real catalog.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.computer_runtime import (
    computer_doctrine,
    computer_model_instructions,
    computer_prompt_state,
    computer_working_state_block,
    ensure_state,
    note_goal,
    reset_computer_states,
)
from app.models import Integration
from tests.test_life_bridges import MOCK_HELPER


@pytest.fixture
def mock_life_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "EVLifeHelper"
    path.write_text(MOCK_HELPER, encoding="utf-8")
    path.chmod(0o755)
    monkeypatch.setattr(settings, "life_helper_path", str(path))
    return path


def test_computer_doctrine_is_general_and_recovery_oriented() -> None:
    doctrine = computer_doctrine()
    assert "every app" in doctrine.lower()
    assert "duplicate windows or tabs" in doctrine.lower()
    assert "switch tactics" in doctrine.lower()
    assert "never repeat the identical failed call" in doctrine.lower()
    assert "verified" in doctrine.lower()


def test_working_state_block_reports_live_goal() -> None:
    reset_computer_states()
    state = ensure_state("generalize-state")
    note_goal(state, "Open Safari and open YouTube in it.")
    state.foreground_app = "Safari"
    state.window_title = "YouTube"

    block = computer_working_state_block(state.prompt_snapshot())

    assert "COMPUTER WORK STATE" in block
    assert "youtube" in block.lower()
    assert "Continue this goal" in block
    assert "Safari" in block


def test_continuation_phrases_revise_same_goal() -> None:
    reset_computer_states()
    state = ensure_state("generalize-continue")
    note_goal(state, "Open Safari and open YouTube in it.")
    assert state.goal is not None
    goal_id = state.goal.goal_id

    for phrase in ("try again", "keep going", "that didn't work", "open it"):
        note_goal(state, phrase)
        assert state.goal is not None
        assert state.goal.goal_id == goal_id
        assert "youtube" in (state.original_owner_request or "").lower()

    note_goal(state, "Open Notes and write hello")
    assert state.goal is not None
    assert state.goal.goal_id != goal_id
    assert "notes" in (state.original_owner_request or "").lower()


def test_stop_still_cancels_but_dont_stop_does_not() -> None:
    reset_computer_states()
    state = ensure_state("generalize-stop")
    note_goal(state, "Open Safari and open YouTube in it.")
    note_goal(state, "don't stop")
    assert state.cancelled is False
    note_goal(state, "stop")
    assert state.cancelled is True


def test_model_instructions_keep_dialog_safety_and_add_doctrine() -> None:
    text = computer_model_instructions(
        {
            "generic_ui_control_ready": True,
            "screen_vision_ready": True,
            "app_lifecycle_ready": True,
            "mac_client_connected": True,
        }
    )
    assert "don't save" in text.lower()
    assert "COMPUTER DOCTRINE" in text


def test_compile_context_injects_live_computer_state() -> None:
    from app.cognitive.context import compile_context
    from app.cognitive.session_store import CognitiveSession

    snapshot = {
        "goal": {
            "status": "acting",
            "verified": False,
            "target_apps": ["Safari"],
            "remaining": "open youtube",
        },
        "goal_request": "Open Safari and open YouTube in it.",
        "foreground_app": "Safari",
    }
    text = compile_context(
        transcript="open youtube",
        modality="voice",
        device_id=None,
        cognition=CognitiveSession(session_id="generalize-context"),
        computer_state=snapshot,
        computer_ready=True,
        capability_names=["computer.perform_effect"],
    )
    assert "COMPUTER WORK STATE" in text
    assert "COMPUTER DOCTRINE" in text
    assert "Open Safari and open YouTube in it." in text


def test_computer_prompt_state_without_live_client() -> None:
    snapshot, ready = computer_prompt_state(live_session_id=None, device_id=None)
    assert snapshot is None
    assert ready is False


async def _install_macos_life(db_session: AsyncSession, helper: Path) -> Integration:
    row = Integration(
        slug="apps-life",
        adapter="messaging",
        name="Messages",
        scopes=["messaging:read", "messaging:act"],
        status="active",
        config={"provider": "macos_life", "helper_path": str(helper)},
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def test_any_installed_app_resolves_from_the_real_catalog(
    db_session: AsyncSession, mock_life_helper: Path
) -> None:
    from app.ev.apps import resolve_installed_app

    assert await resolve_installed_app(
        db_session, "Safari", helper_path=str(mock_life_helper)
    ) == ("Safari", "com.apple.Safari")
    assert await resolve_installed_app(
        db_session, "Not A Real App", helper_path=str(mock_life_helper)
    ) is None


async def test_unknown_app_is_an_honest_miss_not_an_allowlist_refusal(
    db_session: AsyncSession, mock_life_helper: Path
) -> None:
    from app.ev.tools import dispatch

    await _install_macos_life(db_session, mock_life_helper)
    opened = await dispatch(
        db_session, "open_app", {"name": "Not A Real App"}, actor="master"
    )
    assert opened.result["ok"] is False
    assert opened.result["error"] == "app_not_found"
