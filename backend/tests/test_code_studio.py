"""Coding studio: long Spark jobs as background goals you can talk over."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.ev.code_studio import (
    last_job,
    load_board,
    load_studio,
    looks_like_code_status,
    looks_like_long_code_goal,
    maybe_handle_code_ops,
    save_studio,
    spoken_studio_progress,
)
from app.ev.luna_code import (
    drain_pending_code_jobs,
    intern_in_flight,
    looks_like_code_request,
    maybe_enqueue_code_intern,
)
from app.ev.tool_select import LIVE_VOICE_TOOLS


def test_clothing_site_is_a_long_goal_not_a_life_goal() -> None:
    ask = (
        "Evie I want you to make a goal which is making a clothing site UI "
        "from scratch looking like a professional company"
    )
    assert looks_like_long_code_goal(ask)
    assert looks_like_code_request(ask)
    assert not looks_like_long_code_goal("create a goal to get fit")
    assert not looks_like_long_code_goal("write a python script that prints hello")
    assert looks_like_long_code_goal("create a calculator app UI")
    assert looks_like_long_code_goal("I need a calculator app UI")
    assert looks_like_long_code_goal("make me a todo app")
    assert not looks_like_long_code_goal("stop building the clothing site UI")
    assert not looks_like_long_code_goal("revoke the running task")
    assert looks_like_code_status("what are you doing")
    assert looks_like_code_status("what's left")
    assert not looks_like_code_status("how are you")
    assert not looks_like_code_status("what are you doing later tonight")
    assert "execute_command" not in LIVE_VOICE_TOOLS


def test_short_hello_script_is_not_backgrounded() -> None:
    assert maybe_handle_code_ops("write a python script that prints hello") is None


def test_calculator_app_ui_auto_backgrounds(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    ack = maybe_handle_code_ops("create a calculator app UI")
    assert ack
    assert "background" in ack.lower()
    assert "calculator" in ack.lower()
    live = load_studio()
    assert live is not None
    assert live.get("kind") == "calculator"
    stopped = maybe_handle_code_ops("stop it")
    assert stopped
    assert "stopped" in stopped.lower()
    assert intern_in_flight() is False


def test_stale_intern_pending_does_not_block_studio(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.luna_code import _pending_code_path, enqueue_code_intern
    from app.memory.paths import read_json

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    enqueue_code_intern("leftover overnight job")
    leftover = read_json(_pending_code_path())
    assert leftover is not None
    assert leftover.get("kind") == "intern"
    ack = maybe_handle_code_ops("create a calculator app UI")
    assert ack
    assert "queued" not in ack.lower()
    assert "calculator" in ack.lower()
    live = load_studio()
    assert live is not None
    assert live.get("kind") == "calculator"
    pending = read_json(_pending_code_path())
    assert pending is not None
    assert pending.get("kind") == "goal_slice"


def test_casual_stop_does_not_need_the_clothing_name(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.voice.live.layer import classify_live_intent

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    maybe_handle_code_ops("make a clothing site UI from scratch")
    never = maybe_handle_code_ops("never mind")
    assert never
    assert "stopped" in never.lower()
    assert "talking" not in never.lower()
    assert intern_in_flight() is False
    maybe_handle_code_ops("make a clothing site UI from scratch")
    enough = maybe_handle_code_ops("that's enough")
    assert enough
    assert "stopped" in enough.lower()
    maybe_handle_code_ops("make a clothing site UI from scratch")
    talking = maybe_handle_code_ops("stop talking")
    assert talking is None
    assert load_studio() is not None
    assert classify_live_intent("stop talking") == "cancel"
    assert classify_live_intent("stop it") == "cancel"


def test_idle_what_are_you_doing_is_not_stolen(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    assert maybe_handle_code_ops("what are you doing") is None


def test_studio_start_status_steer_pause(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    ask = "make a clothing site UI from scratch looking professional"
    ack = maybe_handle_code_ops(ask)
    assert ack
    assert "background" in ack.lower()
    assert "keep talking" not in ack.lower()
    status = maybe_handle_code_ops("what are you doing")
    assert status
    assert "clothing" in status.lower()
    assert "background" in status.lower()
    assert "keep talking" not in status.lower()
    left = maybe_handle_code_ops("what's left")
    assert left
    assert "0 of 5" in left or "0 of" in left
    steer = maybe_handle_code_ops("make the hero darker")
    assert steer
    assert "next slice" in steer.lower() or "fold" in steer.lower()
    queued = maybe_handle_code_ops("build me a professional dashboard")
    assert queued
    assert "queue" in queued.lower()
    skipped = maybe_handle_code_ops("skip this phase")
    assert skipped
    assert "skip" in skipped.lower()
    paused = maybe_handle_code_ops("pause the coding")
    assert "paused" in paused.lower()
    idle = maybe_handle_code_ops("write a python script that prints hi")
    assert idle is None
    intern = maybe_enqueue_code_intern("write a python script that prints hi overnight")
    assert intern
    assert "background" in intern.lower()


@pytest.mark.asyncio
async def test_studio_slices_write_a_real_site(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev import luna_code as luna
    from app.ev.code_runtime import reset_active_project, set_active_project
    from app.ev.code_studio import load_studio

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "code_model", "gpt-5.6-luna")
    monkeypatch.setattr(settings, "intelligence_provider", "echo")
    monkeypatch.setattr(settings, "chat_provider", "echo")
    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: False)
    notes: list[str] = []
    monkeypatch.setattr(luna, "schedule_background_code_notify", lambda spoken: notes.append(spoken))
    token = set_active_project(tmp_path)
    try:
        ack = maybe_handle_code_ops("make a clothing site UI from scratch")
        assert ack
        deadline = time.monotonic() + 15
        studio = load_studio()
        while time.monotonic() < deadline:
            studio = load_studio() or last_job()
            live = [task for task in list(luna._INTERN_TASKS) if not task.done()]
            if (
                studio
                and str(studio.get("status") or "") in {"done", "failed"}
                and load_studio() is None
                and not live
            ):
                break
            if live:
                await asyncio.wait(live, timeout=0.2)
            else:
                await drain_pending_code_jobs()
        studio = last_job()
        assert studio is not None
        assert studio.get("status") == "done"
        assert intern_in_flight() is False
        assert (tmp_path / "atelier" / "index.html").is_file()
        assert (tmp_path / "atelier" / "styles.css").is_file()
        assert (tmp_path / "atelier" / "catalog.html").is_file()
        html = (tmp_path / "atelier" / "index.html").read_text(encoding="utf-8")
        assert "Atelier Noir" in html
        spoken = spoken_studio_progress(studio)
        assert "done" in spoken.lower()
        assert "shipped" in spoken.lower() or "atelier" in spoken.lower()
        review = maybe_handle_code_ops("what did you build")
        assert review
        assert "atelier" in review.lower() or "index.html" in review.lower() or "done" in review.lower()
        assert notes
        assert "shipped" in notes[-1].lower() or "done" in notes[-1].lower()
    finally:
        reset_active_project(token)


def test_stop_and_delete_clear_background_task(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.code_studio import load_studio

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    maybe_handle_code_ops("make a clothing site UI from scratch")
    assert intern_in_flight() is True
    stopped = maybe_handle_code_ops("stop the background task")
    assert stopped
    assert "stopped" in stopped.lower()
    assert intern_in_flight() is False
    finished = last_job()
    assert finished is not None
    assert finished.get("status") == "cancelled"
    maybe_handle_code_ops("make a clothing site UI from scratch")
    removed = maybe_handle_code_ops("delete the background task")
    assert removed
    assert "removed" in removed.lower()
    assert load_studio() is None
    assert intern_in_flight() is False
    assert maybe_handle_code_ops("stop") is None


def test_stop_building_aborts_and_does_not_queue(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.code_studio import looks_like_code_control

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    ask = "stop building the clothing site UI"
    assert looks_like_code_control(ask)
    maybe_handle_code_ops("make a clothing site UI from scratch")
    maybe_handle_code_ops("build me a professional dashboard")
    studio = load_studio()
    assert studio is not None
    assert studio.get("kind") == "clothing_site"
    jobs = list(load_board().get("jobs") or [])
    assert any(str(item.get("kind") or "") == "dashboard" for item in jobs)
    stopped = maybe_handle_code_ops(ask)
    assert stopped
    assert "stopped" in stopped.lower()
    assert "queue" not in stopped.lower()
    assert "chat" not in stopped.lower()
    assert intern_in_flight() is False
    assert load_studio() is None
    clothing = next(
        item for item in (load_board().get("jobs") or []) if str(item.get("kind") or "") == "clothing_site"
    )
    assert clothing.get("status") == "cancelled"
    revoked = maybe_handle_code_ops("make a clothing site UI from scratch")
    assert "background" in (revoked or "").lower()
    broke = maybe_handle_code_ops("break the request")
    assert broke
    assert "stopped" in broke.lower()
    assert intern_in_flight() is False


@pytest.mark.asyncio
async def test_stop_cancels_the_running_drain(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev import luna_code as luna

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    started = asyncio.Event()
    released = asyncio.Event()

    async def fake_drain():
        started.set()
        await released.wait()
        return {"ok": True}

    monkeypatch.setattr(luna, "drain_pending_code_jobs", fake_drain)
    maybe_handle_code_ops("make a clothing site UI from scratch")
    await asyncio.wait_for(started.wait(), 0.5)
    stopped = maybe_handle_code_ops("stop")
    assert "stopped" in stopped.lower()
    live = [task for task in list(luna._INTERN_TASKS) if not task.done()]
    if live:
        await asyncio.wait(live, timeout=0.5)
    assert intern_in_flight() is False
    finished = last_job()
    assert finished is not None
    assert finished.get("status") == "cancelled"
    released.set()


@pytest.mark.asyncio
async def test_completion_notify_speaks_without_being_asked(monkeypatch) -> None:
    from app.ev import luna_code as luna

    heard: list[str] = []

    class _Live:
        _closed = False

        async def _speak_code_receipt(self, text: str) -> None:
            heard.append(text)

    monkeypatch.setattr("app.voice.live.layer.active_lives", lambda: [_Live()])
    await luna._push_background_code_notify(
        "clothing site UI is done. I shipped Foundation, Catalog, Product, Cart, polish. It's all under atelier/ — index.html."
    )
    assert heard
    assert "shipped" in heard[0].lower()
    assert "atelier" in heard[0].lower()


def test_finished_clothing_site_does_not_steal_a_new_task(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    maybe_handle_code_ops("make a clothing site UI from scratch")
    studio = load_studio()
    assert studio is not None
    studio["status"] = "done"
    for phase in list(studio.get("phases") or []):
        phase["status"] = "done"
    save_studio(studio)
    assert load_studio() is None
    assert maybe_handle_code_ops("what are you doing") is None
    leftover = maybe_handle_code_ops("delete the new task")
    assert leftover
    assert "clothing" not in leftover.lower()
    assert "no background task" in leftover.lower()
    loose_delete = maybe_handle_code_ops("I want that new task deleted")
    assert loose_delete
    assert "clothing" not in loose_delete.lower()
    loose_run = maybe_handle_code_ops("I want you to run a new task in the background")
    assert loose_run
    assert "clothing" not in loose_run.lower()
    started = maybe_handle_code_ops("build me a professional dashboard")
    assert started
    assert "dashboard" in started.lower()
    assert "clothing" not in started.lower()
    assert "queue" not in started.lower()
    status = maybe_handle_code_ops("what are you doing")
    assert status
    assert "dashboard" in status.lower()
    assert "clothing" not in status.lower()
    listed = maybe_handle_code_ops("what background tasks do you have")
    assert listed
    assert "dashboard" in listed.lower()
    removed = maybe_handle_code_ops("delete the new background task")
    assert removed
    assert "dashboard" in removed.lower()
    assert "clothing" not in removed.lower()
    history = last_job()
    assert history is not None
    assert history.get("kind") == "clothing_site"
    again = maybe_handle_code_ops("run the new task")
    assert again
    assert "clothing" not in again.lower()
    named = maybe_handle_code_ops("stop the clothing site")
    assert named
    assert "finished" in named.lower() or "nothing running" in named.lower()


def test_run_the_new_task_starts_the_queued_sibling(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    maybe_handle_code_ops("make a clothing site UI from scratch")
    maybe_handle_code_ops("build me a professional dashboard")
    maybe_handle_code_ops("stop building the clothing site UI")
    assert load_studio() is None
    started = maybe_handle_code_ops("run the new task")
    assert started
    assert "dashboard" in started.lower()
    assert "clothing" not in started.lower()
    live = load_studio()
    assert live is not None
    assert live.get("kind") == "dashboard"
    intern = maybe_enqueue_code_intern("write a python script that prints hi overnight")
    assert intern
    assert "already" in intern.lower() or "pause" in intern.lower()
    dropped = maybe_handle_code_ops("delete the new task")
    assert dropped
    assert "dashboard" in dropped.lower()
    assert "clothing" not in dropped.lower()
    assert load_studio() is None


@pytest.mark.asyncio
async def test_calculator_slices_write_and_brief(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev import luna_code as luna
    from app.ev.code_runtime import reset_active_project, set_active_project

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "code_model", "gpt-5.6-luna")
    monkeypatch.setattr(settings, "intelligence_provider", "echo")
    monkeypatch.setattr(settings, "chat_provider", "echo")
    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: False)
    notes: list[str] = []
    monkeypatch.setattr(luna, "schedule_background_code_notify", lambda spoken: notes.append(spoken))
    token = set_active_project(tmp_path)
    try:
        ack = maybe_handle_code_ops("create a calculator app UI")
        assert ack
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            studio = load_studio() or last_job()
            live = [task for task in list(luna._INTERN_TASKS) if not task.done()]
            if (
                studio
                and str(studio.get("status") or "") in {"done", "failed"}
                and load_studio() is None
                and not live
            ):
                break
            if live:
                await asyncio.wait(live, timeout=0.2)
            else:
                await drain_pending_code_jobs()
        studio = last_job()
        assert studio is not None
        assert studio.get("status") == "done"
        assert (tmp_path / "calc" / "index.html").is_file()
        assert (tmp_path / "calc" / "app.js").is_file()
        html = (tmp_path / "calc" / "index.html").read_text(encoding="utf-8")
        assert "Desk calc" in html
        spoken = spoken_studio_progress(studio)
        assert "brief" in spoken.lower()
        assert "done" in spoken.lower()
        assert notes
        assert "brief" in notes[-1].lower() or "done" in notes[-1].lower()
    finally:
        reset_active_project(token)


@pytest.mark.asyncio
async def test_waiting_brief_flushes_when_a_live_mouth_is_here(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev import luna_code as luna
    from app.memory.paths import atomic_write_json

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    heard: list[str] = []

    class _Live:
        _closed = False

        async def _speak_code_receipt(self, text: str) -> None:
            heard.append(text)

    atomic_write_json(
        luna._ready_code_path(),
        {"ok": True, "spoken": "Quick brief: calculator UI is done.", "at": "now"},
    )
    monkeypatch.setattr("app.voice.live.layer.active_lives", lambda: [_Live()])
    luna.flush_background_code_notify()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not heard:
        await asyncio.sleep(0.02)
    assert heard
    assert "brief" in heard[0].lower()
