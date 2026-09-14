"""Cognitive OS V2 single-brain invariant: muse_kernel means Spark thinks.

Wiring contract being locked (owner-ordered, 2026-09-10):
- ``EV_COGNITIVE_MODE=muse_kernel`` makes Muse Spark 1.3 Contributor the one
  mind on every THINKING surface: generic chat resolution, gateway routing,
  TURN/MANAGER roles, legacy-brain refusal — even when leftover legacy slots
  (xAI / OpenAI / DeepSeek) are still named in config.
- ``muse_intelligence_active()`` stays slot-driven because the voice
  data-plane gates (S2S mouth, TTS mouth lock, phone media transport) key on
  it; kernel mode alone must never rewire how Evie hears or speaks.
- ``EV_COGNITIVE_MODE=legacy_mini`` restores the legacy split byte-for-byte.
- Without the Meta credential every Spark path fails closed
  (``MuseProviderUnavailable``) — never a silent legacy fallback.
"""

from __future__ import annotations

import pytest

from app.cognitive.mode import cognitive_mode, muse_kernel_active
from app.config import settings
from app.ev import model_router
from app.gateway.muse import (
    MUSE_SPARK_PROVIDERS,
    MuseProviderUnavailable,
    configured_intelligence_provider,
    muse_brain_active,
    muse_intelligence_active,
    muse_spark_key_loaded,
    refuse_legacy_cloud_brain,
)


def _kernel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner-like leftover slots under kernel mode: xai chat, openai turn."""
    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")
    monkeypatch.setattr(settings, "intelligence_provider", "")
    monkeypatch.setattr(settings, "chat_provider", "xai")
    monkeypatch.setattr(settings, "turn_control_provider", "openai")


def test_kernel_mode_makes_spark_the_brain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _kernel_env(monkeypatch)
    assert cognitive_mode() == "muse_kernel"
    assert muse_kernel_active() is True
    assert muse_brain_active() is True
    # Slot-faithful predicate stays False: transport must not rewire.
    assert muse_intelligence_active() is False
    assert configured_intelligence_provider() == "meta_muse_spark"


def test_kernel_mode_resolves_roles_to_spark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _kernel_env(monkeypatch)
    turn = model_router.turn_control_model_info()
    manager = model_router.manager_model_info()
    assert turn.provider == "meta_muse_spark"
    assert turn.model == "muse-spark-1.3-contributor"
    assert manager.provider == "meta_muse_spark"
    assert manager.model == "muse-spark-1.3-contributor"


def test_kernel_mode_blocks_legacy_cloud_brains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _kernel_env(monkeypatch)
    with pytest.raises(MuseProviderUnavailable):
        refuse_legacy_cloud_brain("openai")
    with pytest.raises(MuseProviderUnavailable):
        refuse_legacy_cloud_brain("xai")
    with pytest.raises(MuseProviderUnavailable):
        refuse_legacy_cloud_brain("deepseek")
    with pytest.raises(MuseProviderUnavailable):
        refuse_legacy_cloud_brain(None)
    # Spark itself is allowed.
    refuse_legacy_cloud_brain("meta_muse_spark")


def test_generic_chat_resolves_spark_under_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.gateway.providers import get_chat_provider

    _kernel_env(monkeypatch)
    assert muse_spark_key_loaded() is False  # conftest blanks Meta keys
    # Fail-closed: no key means no Spark provider, never a legacy fallback.
    with pytest.raises(MuseProviderUnavailable):
        get_chat_provider()

    monkeypatch.setenv("EV_META_MODEL_API_KEY", "test-spark-key")
    assert get_chat_provider().name in MUSE_SPARK_PROVIDERS


def test_transport_gates_stay_slot_driven_under_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S2S mouth stays available under muse_kernel when no Muse slot is set."""
    from app.voice.live.grok_voice import live_realtime_provider

    _kernel_env(monkeypatch)
    monkeypatch.setattr(settings, "voice_live_brain", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    assert muse_kernel_active() is True
    assert muse_intelligence_active() is False
    assert live_realtime_provider() == "openai"


def test_legacy_mini_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_mini")
    monkeypatch.setattr(settings, "intelligence_provider", "")
    monkeypatch.setattr(settings, "chat_provider", "xai")
    monkeypatch.setattr(settings, "turn_control_provider", "openai")
    assert muse_kernel_active() is False
    assert muse_brain_active() is False
    assert muse_intelligence_active() is False
    assert configured_intelligence_provider() == "xai"
    assert model_router.turn_control_model_info().provider == "openai"
    assert model_router.manager_model_info().provider == "deepseek"
    # Legacy brains are not refused in legacy mode.
    refuse_legacy_cloud_brain("openai")


def test_explicit_muse_slot_still_activates_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting a Muse slot keeps the slot-driven semantics (Muse Talk path)."""
    from app.voice.live.grok_voice import live_realtime_provider

    monkeypatch.setattr(settings, "cognitive_mode", "legacy_mini")
    monkeypatch.setattr(settings, "intelligence_provider", "")
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "turn_control_provider", "openai")
    assert muse_intelligence_active() is True
    assert muse_brain_active() is True
    assert live_realtime_provider() is None


def test_kernel_with_muse_slot_and_no_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _kernel_env(monkeypatch)
    monkeypatch.setattr(settings, "chat_provider", "muse_spark")
    assert muse_brain_active() is True
    assert muse_intelligence_active() is True
    assert muse_spark_key_loaded() is False
    from app.gateway.muse_spark import muse_spark_provider

    with pytest.raises(MuseProviderUnavailable):
        muse_spark_provider()
