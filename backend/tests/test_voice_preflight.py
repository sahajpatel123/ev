"""Voice activation preflight diagnostics (Agent 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ml import voice_preflight
from app.ml.settings import get_ml_settings


def test_preflight_prints_status_and_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("EV_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("EV_VOICEPRINT_PROVIDER", "campp")
    monkeypatch.setenv("EV_VOICE_WAKE_PROVIDER", "openwakeword")
    monkeypatch.setenv("EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH", str(tmp_path / "wake.onnx"))
    monkeypatch.setenv("EV_VOICE_TTS_PROVIDER", "kokoro")
    monkeypatch.setenv("EV_VOICE_ASR_PROVIDER", "faster_whisper")
    get_ml_settings.cache_clear()
    try:
        assert voice_preflight.main([]) == 0
    finally:
        get_ml_settings.cache_clear()
    out = capsys.readouterr().out
    assert "speaker:" in out
    assert "wake:" in out
    assert "tts:" in out
    assert "asr:" in out
    assert "remediation:" in out
    assert "faster_whisper" in out
    assert "openwakeword" in out
    assert "Owner commands" in out
    assert "Agent 5" in out
    assert "Agent 3" in out
