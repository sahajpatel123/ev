"""Vendor-standard provider key names alias to their EV_ spellings."""

from __future__ import annotations

import os

from app.config import _alias_unprefixed_provider_keys


def test_unprefixed_openrouter_key_is_aliased(monkeypatch) -> None:
    monkeypatch.delenv("EV_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-alias")
    _alias_unprefixed_provider_keys()
    assert os.environ["EV_OPENROUTER_API_KEY"] == "sk-test-alias"


def test_explicit_ev_key_wins_over_alias(monkeypatch) -> None:
    monkeypatch.setenv("EV_OPENROUTER_API_KEY", "sk-explicit")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-other")
    _alias_unprefixed_provider_keys()
    assert os.environ["EV_OPENROUTER_API_KEY"] == "sk-explicit"


def test_empty_ev_key_is_not_an_override(monkeypatch) -> None:
    monkeypatch.setenv("EV_OPENROUTER_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-alias")
    _alias_unprefixed_provider_keys()
    assert os.environ["EV_OPENROUTER_API_KEY"] == "sk-alias"
