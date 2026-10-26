"""A deployed env that still names a retired brain is reported once, with the fix."""

from __future__ import annotations

import pytest

from app.config import settings
from app.gateway.providers import brain_configuration_problems


def test_valid_configuration_has_no_problems(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "muse")
    monkeypatch.setattr(settings, "agent_provider", "auto")
    assert brain_configuration_problems() == []


@pytest.mark.parametrize("retired", ["deepseek", "mimo", "xai", "openai", "openrouter", "local"])
def test_retired_chat_provider_is_reported_with_the_fix(monkeypatch, retired) -> None:
    monkeypatch.setattr(settings, "chat_provider", retired)
    monkeypatch.setattr(settings, "agent_provider", "auto")
    (problem,) = brain_configuration_problems()
    assert f"EV_CHAT_PROVIDER='{retired}'" in problem
    assert "set EV_CHAT_PROVIDER=muse" in problem


@pytest.mark.parametrize("retired", ["deepseek", "mimo", "xai", "openai", "deepseek-direct"])
def test_retired_agent_provider_is_reported_with_the_fix(monkeypatch, retired) -> None:
    monkeypatch.setattr(settings, "chat_provider", "muse")
    monkeypatch.setattr(settings, "agent_provider", retired)
    (problem,) = brain_configuration_problems()
    assert f"EV_AGENT_PROVIDER='{retired}'" in problem
    assert "muse or auto" in problem


def test_both_retired_names_are_reported_together(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "deepseek")
    monkeypatch.setattr(settings, "agent_provider", "deepseek")
    assert len(brain_configuration_problems()) == 2


def test_preflight_names_a_retired_brain_instead_of_calling_it_a_test_double(monkeypatch) -> None:
    from app.scripts.preflight import _check_chat

    monkeypatch.setattr(settings, "chat_provider", "deepseek")
    status, provider, detail = _check_chat()
    assert (status, provider) == ("PARTIAL", "deepseek")
    assert "not a registered brain" in detail and "EV_CHAT_PROVIDER=muse" in detail
    assert "test double" not in detail


def test_preflight_still_reports_the_offline_doubles_as_doubles(monkeypatch) -> None:
    from app.scripts.preflight import _check_chat

    monkeypatch.setattr(settings, "chat_provider", "echo")
    status, _provider, detail = _check_chat()
    assert status == "DOUBLE" and "test double" in detail


async def test_startup_logs_the_problem(monkeypatch, caplog) -> None:
    """The lifespan hook is wired: a retired name is logged at ERROR on boot."""

    import logging

    import app.main as main_mod

    monkeypatch.setattr(settings, "chat_provider", "deepseek")
    with caplog.at_level(logging.ERROR):
        async with main_mod.lifespan(main_mod.app):
            pass
    assert any("brain configuration" in r.getMessage() and "deepseek" in r.getMessage()
               for r in caplog.records)
