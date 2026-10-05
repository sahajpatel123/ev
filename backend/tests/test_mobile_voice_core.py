"""Mobile Voice Core: fingerprints, exclusive backends, ASR contract, runtime helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.device_gateway import PWA_BUILD
from app.device_gateway.mobile_voice import (
    MOBILE_CONVERSATION_CONTRACT,
    config_diff,
    critical_tokens,
    fingerprint_report,
    iphone_voice_fingerprint,
    logprob_confidence,
    mac_voice_golden_fingerprint,
)
from app.device_gateway.webrtc_live import (
    is_strict_webrtc,
    phone_webrtc_session,
    public_audio_status,
    resolve_phone_audio_backend,
)

ROOT = Path(__file__).resolve().parents[1]


def test_mac_golden_fingerprint_is_frozen_contract() -> None:
    mac = mac_voice_golden_fingerprint()
    assert mac["endpoint"] == "mac"
    assert mac["frozen"] is True
    assert str(mac["model"]).startswith("models/")
    assert mac["voice"]
    assert mac["response_modalities"] == ["AUDIO"]
    assert mac["input_transcription"] is True
    assert mac["output_transcription"] is True
    assert mac["manual_vad"] is False
    assert mac["compression"] is True
    assert mac["resumption"] is True
    assert "pcm" in str(mac["transport"])


def test_iphone_fingerprint_matches_conversation_not_transport() -> None:
    phone = iphone_voice_fingerprint()
    mac = mac_voice_golden_fingerprint()
    assert phone["transport"] != mac["transport"]
    assert phone["model"] == mac["model"]
    assert phone["voice"] == mac["voice"]
    assert phone["response_modalities"] == mac["response_modalities"]
    assert phone["manual_vad"] is False
    assert phone["input_transcription"] is True
    assert phone["mobile_contract"] is True
    rows = {row["field"]: row for row in config_diff(mac, phone)}
    assert rows["transport"]["match"] is False
    assert rows["manual_vad"]["match"] is True
    assert rows["model"]["match"] is True


def test_phone_session_carries_mobile_contract_without_per_session_asr_knobs() -> None:
    message = phone_webrtc_session()
    setup = message["setup"]
    text = setup["systemInstruction"]["parts"][0]["text"]
    assert MOBILE_CONVERSATION_CONTRACT in text
    assert "Personal facts and live state are not trivia" in MOBILE_CONVERSATION_CONTRACT
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert "inputAudioTranscription" in setup
    # The Live API has no per-session ASR model / language / VAD-threshold
    # knobs: transcription is server-side with server defaults.
    assert "audio" not in setup
    names = {
        tool["name"]
        for tool in setup["tools"][0]["functionDeclarations"]
    }
    assert {"look", "phone_action"} <= names


def test_pcm_ws_is_default_when_key_present(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "google_api_key", "[REDACTED]")
    monkeypatch.setattr(settings, "phone_audio_backend", "auto")
    assert resolve_phone_audio_backend(None) == "pcm_ws"
    assert resolve_phone_audio_backend("auto") == "pcm_ws"
    assert resolve_phone_audio_backend("pcm_ws") == "pcm_ws"
    assert is_strict_webrtc("pcm_ws") is False
    status = public_audio_status()
    assert status["strict_webrtc"] is False
    assert status["recommended_backend"] == "pcm_ws"
    assert status["provider_key_in_browser"] is False
    assert "OWNER FAILURE" in status["mobile_voice_status"]


def test_retired_webrtc_always_unavailable(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.config import settings

    monkeypatch.setattr(settings, "google_api_key", "[REDACTED]")
    monkeypatch.setattr(settings, "phone_audio_backend", "webrtc_strict")
    try:
        resolve_phone_audio_backend("webrtc_strict")
        raise AssertionError("expected HTTPException")
    except HTTPException as exc:
        assert exc.status_code == 503
    status = public_audio_status()
    assert status["strict_webrtc"] is True
    assert status["pcm_fallback_allowed"] is False


def test_critical_tokens_and_logprobs() -> None:
    found = critical_tokens("Do not open Spotify; I'm asking about Wi-Fi.")
    assert "spotify" in found
    assert "wi-fi" in found
    assert "do not" in found
    assert logprob_confidence([-0.2, -0.1]) is not None
    assert logprob_confidence(None) is None


def test_pwa_uses_server_selected_audio_lane() -> None:
    app_js = (ROOT / "clients" / "pwa" / "app.js").read_text()
    webrtc = (ROOT / "clients" / "pwa" / "webrtc.js").read_text()
    html = (ROOT / "clients" / "pwa" / "index.html").read_text()
    assert 'media_backend: "pcm_ws"' in app_js
    assert "Couldn't connect to Evie Voice." in app_js
    assert "Voice connection failed." not in app_js
    assert "createMediaStreamSource" not in webrtc
    assert "addTrack(this.micTrack" in webrtc
    assert "extraRemoteTracks" in webrtc
    assert "Mic Check" in html
    assert "Speech Recognition Check" in html
    assert "Report misheard phrase" in html
    assert "VOICE HEALTH" in html
    assert PWA_BUILD in app_js
    assert PWA_BUILD in html
    assert "sampleRate: 16000" not in webrtc


def test_fingerprint_report_shape() -> None:
    report = fingerprint_report()
    assert report["mac"]["endpoint"] == "mac"
    assert report["iphone"]["endpoint"] == "iphone"
    assert "Turn off the Wi-Fi" in " ".join(report["eval_phrases"])


def test_js_runtime_helpers() -> None:
    script = ROOT / "clients" / "pwa" / "webrtc.js"
    result = subprocess.run(
        [
            "node",
            "-e",
            "const mv=require(process.argv[1]);"
            "if(mv.exclusivePlaybackIllegal(true,true)!==true) process.exit(2);"
            "if(mv.exclusivePlaybackIllegal(true,false)!==false) process.exit(3);"
            "if(mv.classifyLevel(0.05,0.4,0)!=='NORMAL') process.exit(4);"
            "if(mv.classifyLevel(0.001,0.01,0)!=='TOO_QUIET') process.exit(5);"
            "const h=mv.voiceHealth({micActive:true,micReadyState:'live',audioSenders:1,"
            "peerConnections:1,remoteAudioTracks:1,audioElements:1,pcmFallback:'off',"
            "fallbackTts:'off',ice:'connected',sessionActive:true,playbackOwner:'THIS_PHONE'});"
            "if(!h.ready) process.exit(6);"
            "const bad=mv.voiceHealth({micActive:true,micReadyState:'live',audioSenders:1,"
            "peerConnections:1,remoteAudioTracks:1,audioElements:1,pcmFallback:'on',"
            "fallbackTts:'off',ice:'connected',sessionActive:true});"
            "if(bad.fallback!=='ILLEGAL') process.exit(7);"
            "console.log('ok');",
            str(script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
