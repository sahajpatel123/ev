"""Phone audio architecture: pcm_ws bridge, retired WebRTC, diagnostic ladder."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.device_gateway.audio_diag import format_truth, known_clean_pcm16
from app.device_gateway.webrtc_live import (
    DESIGN_VERSION,
    phone_webrtc_session,
    public_audio_status,
    resolve_phone_audio_backend,
)


def test_known_pcm_format_truth() -> None:
    pcm = known_clean_pcm16()
    truth = format_truth(pcm)
    assert truth["odd_trailing_byte"] == 0
    assert truth["declared_rate"] == 16000
    assert truth["sample_count"] == 32000
    assert truth["endian"] == "little"
    assert truth["matches_declared"] is True


def test_phone_webrtc_session_is_sandbox_live_setup() -> None:
    message = phone_webrtc_session()
    setup = message["setup"]
    declarations = setup["tools"][0]["functionDeclarations"]
    names = {tool["name"] for tool in declarations}
    assert names == {"activate_app", "close_app", "computer_status", "list_apps", "look", "open_app", "phone_action"}
    text = setup["systemInstruction"]["parts"][0]["text"]
    assert "SECRET" not in text
    assert setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert "inputAudioTranscription" in setup
    assert "outputAudioTranscription" in setup


def test_backend_resolution_without_key(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "google_api_key", None)
    monkeypatch.setattr(settings, "phone_audio_backend", "auto")
    assert resolve_phone_audio_backend("auto") == "pcm_ws"
    assert resolve_phone_audio_backend("pcm_ws") == "pcm_ws"


def test_backend_resolution_is_pcm_ws_even_with_key(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "google_api_key", "[REDACTED]")
    monkeypatch.setattr(settings, "phone_audio_backend", "auto")
    assert resolve_phone_audio_backend("auto") == "pcm_ws"
    status = public_audio_status()
    assert status["provider_key_in_browser"] is False
    assert status["sdp_proxy"] is False
    assert status["webrtc_available"] is False
    assert status["design_version"] == DESIGN_VERSION
    assert status["recommended_backend"] == "pcm_ws"


def test_backend_resolution_rejects_retired_webrtc() -> None:
    with pytest.raises(HTTPException) as excinfo:
        resolve_phone_audio_backend("webrtc_strict")
    assert excinfo.value.status_code == 503


def test_pwa_assets_do_not_embed_provider_credentials() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "clients" / "pwa"
    blob = ""
    for name in ("app.js", "webrtc.js", "audio.js", "presence.js", "index.html"):
        blob += (root / name).read_text()
    css = (root / "style.css").read_text()
    assert "OPENAI_API_KEY" not in blob
    assert "api.openai.com" not in blob
    assert "sk-proj-" not in blob
    assert "GOOGLE_API_KEY" not in blob
    assert "generativelanguage.googleapis.com" not in blob
    assert "EviePresence" in blob
    assert "--veil-pearl" in css
    assert "Quiet Material" not in blob
    assert "Quiet Material" not in css
