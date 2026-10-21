"""Model-first intake routing: the brain routes what static regexes drop.

Hermetic: a scripted provider answers the route call; no network, no keys.
The legacy regex classifier is the offline fallback (no brain, timeout,
crash, unreadable reply, privacy refusal) — never the decider when a brain
answers.
"""

from __future__ import annotations

import pytest

from app.agent import intake
from app.agent.brain import BrainChoice
from app.config import settings
from app.contracts import ChatResult


class RouteProvider:
    """Scripted router brain: records calls, replays verdicts."""

    name = "route-scripted"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.seen: list[list] = []

    async def chat(self, messages, *, model=None, temperature=0.7):
        del model, temperature
        self.seen.append(list(messages))
        text = self._texts.pop(0) if self._texts else '{"route": "chat"}'
        return ChatResult(text=text, model="route-scripted")


def _install_route_brain(monkeypatch, provider: RouteProvider) -> None:
    choice = BrainChoice("scripted", "scripted", "test", True)
    monkeypatch.setattr("app.agent.service.resolve_agent_brain", lambda *a, **k: choice)
    monkeypatch.setattr(intake, "build_agent_provider", lambda c: provider)


def _go_dark(monkeypatch) -> None:
    for key in ("xai_api_key", "deepseek_api_key", "agent_openai_api_key",
                "openrouter_api_key", "local_model_base_url"):
        monkeypatch.setattr(settings, key, None, raising=False)
    monkeypatch.setattr(settings, "agent_provider", "auto")


@pytest.mark.asyncio
async def test_model_routes_regex_missed_goal_to_task(monkeypatch) -> None:
    goal = "put my desktop screenshots into dated folders"
    assert intake.looks_like_general_task(goal) is False
    provider = RouteProvider(['{"route": "task"}'])
    assert await intake.route_goal(goal, source="voice", provider=provider) == "task"
    assert len(provider.seen) == 1


@pytest.mark.asyncio
async def test_model_routes_small_talk_to_chat(monkeypatch) -> None:
    provider = RouteProvider(['{"route": "chat"}'])
    assert await intake.route_goal("thanks, that is all for now", provider=provider) == "chat"


@pytest.mark.asyncio
async def test_regex_hit_skips_model_call(monkeypatch) -> None:
    goal = "find every duplicate photo and delete the extras"
    assert intake.looks_like_general_task(goal) is True
    provider = RouteProvider(['{"route": "chat"}'])
    _install_route_brain(monkeypatch, provider)
    assert await intake.route_goal(goal) == "task"
    assert provider.seen == []


@pytest.mark.asyncio
async def test_brain_dark_falls_back_to_legacy(monkeypatch) -> None:
    _go_dark(monkeypatch)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    def _boom(choice):
        raise AssertionError("router must not build a provider when dark")

    monkeypatch.setattr(intake, "build_agent_provider", _boom)
    assert await intake.route_goal("put my desktop screenshots into dated folders") == "chat"
    assert await intake.route_goal("find every duplicate photo and delete the extras") == "task"


@pytest.mark.asyncio
async def test_provider_build_failure_falls_back_to_legacy(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    choice = BrainChoice("scripted", "scripted", "test", True)
    monkeypatch.setattr("app.agent.service.resolve_agent_brain", lambda *a, **k: choice)

    def _boom(choice):
        raise RuntimeError("no such provider")

    monkeypatch.setattr(intake, "build_agent_provider", _boom)
    assert await intake.route_goal("put my desktop screenshots into dated folders") == "chat"


@pytest.mark.asyncio
async def test_hermetic_guard_skips_ambient_keys(monkeypatch) -> None:
    import os

    assert os.environ.get("PYTEST_CURRENT_TEST")
    choice = BrainChoice("scripted", "scripted", "test", True)
    monkeypatch.setattr("app.agent.service.resolve_agent_brain", lambda *a, **k: choice)

    def _boom(choice):
        raise AssertionError("hermetic run must not touch ambient keys")

    monkeypatch.setattr(intake, "build_agent_provider", _boom)
    assert await intake.route_goal("put my desktop screenshots into dated folders") == "chat"


@pytest.mark.asyncio
async def test_unparseable_reply_falls_back_to_legacy(monkeypatch) -> None:
    provider = RouteProvider(["maybe later, I am not sure"])
    assert await intake.route_goal("put my desktop screenshots into dated folders", provider=provider) == "chat"


@pytest.mark.asyncio
async def test_brain_exception_falls_back_to_legacy(monkeypatch) -> None:
    provider = RouteProvider(['{"route": "task"}'])

    async def _boom(messages, *, model=None, temperature=0.7):
        raise RuntimeError("router is down")

    provider.chat = _boom  # type: ignore[method-assign]
    assert await intake.route_goal("put my desktop screenshots into dated folders", provider=provider) == "chat"


@pytest.mark.asyncio
async def test_privacy_marker_never_reaches_provider(monkeypatch) -> None:
    goal = "read my [never_send_to_model] note"
    provider = RouteProvider(['{"route": "task"}'])
    expected = "task" if intake.looks_like_general_task(goal) else "chat"
    assert await intake.route_goal(goal, provider=provider) == expected
    assert provider.seen == []


@pytest.mark.asyncio
async def test_handle_task_submits_model_routed_voice_goal(monkeypatch) -> None:
    import app.agent.intake as intake_module

    goal = "put my desktop screenshots into dated folders"
    assert intake.looks_like_general_task(goal) is False
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    _install_route_brain(monkeypatch, RouteProvider(['{"route": "task"}']))
    calls: list[str] = []

    async def fake_wait(task_id: str, timeout: float):
        return {"id": "t1", "status": "completed", "summary": "Sorted."}

    def fake_submit(submitted: str, **kwargs):
        calls.append(submitted)
        return {"id": "t1", "status": "running"}

    monkeypatch.setattr(intake_module.runner, "submit_task", fake_submit)
    monkeypatch.setattr(intake_module.runner, "wait_task", fake_wait)
    monkeypatch.setattr(intake_module.runner, "mark_consumed", lambda task_id: None)
    out = await intake.handle_task(goal, source="voice")
    assert out is not None
    assert calls == [goal]


@pytest.mark.asyncio
async def test_static_mode_uses_legacy_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_routing", "static", raising=False)
    provider = RouteProvider(['{"route": "task"}'])
    _install_route_brain(monkeypatch, provider)
    assert await intake.route_goal("thanks, that is all for now") == "chat"
    assert provider.seen == []


@pytest.mark.asyncio
async def test_model_first_asks_about_everything(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_routing", "model_first", raising=False)
    provider = RouteProvider(['{"route": "chat"}'])
    assert await intake.route_goal("find every duplicate photo and delete the extras", provider=provider) == "chat"
    assert len(provider.seen) == 1
