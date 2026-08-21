"""Mobile Voice Core: fingerprints, exclusive backends, ASR contract, runtime helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

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
    assert mac["turn_detection"] == "server_vad"
    assert mac["create_response"] is True
    assert mac["interrupt_response"] is False
    assert "pcm" in str(mac["transport"])


def test_iphone_fingerprint_matches_conversation_not_transport() -> None:
    phone = iphone_voice_fingerprint()
    mac = mac_voice_golden_fingerprint()
    assert phone["transport"] != mac["transport"]
    assert phone["model"] == mac["model"]
    assert phone["voice"] == mac["voice"]
    assert phone["turn_detection"] == mac["turn_detection"]
    assert phone["create_response"] is True
    assert phone["interrupt_response"] is False
    assert phone["transcription_language"] == "en"
    assert phone["transcription_prompt"] is True
    assert phone["mobile_contract"] is True
    rows = {row["field"]: row for row in config_diff(mac, phone)}
    assert rows["transport"]["match"] is False
    assert rows["create_response"]["match"] is True


def test_phone_session_uses_strong_asr_and_no_pcm_rate() -> None:
    session = phone_webrtc_session()
    inp = session["audio"]["input"]
    assert inp["transcription"]["model"] == "gpt-4o-transcribe"
    assert inp["transcription"]["language"] == "en"
    assert "Wi-Fi" in inp["transcription"]["prompt"]
    assert "Spotify" in inp["transcription"]["prompt"]
    assert "format" not in inp
    assert inp["noise_reduction"]["type"] == "near_field"
    assert MOBILE_CONVERSATION_CONTRACT in session["instructions"]
    assert session["include"] == ["item.input_audio_transcription.logprobs"]


def test_strict_webrtc_is_default_when_key_present(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-test-not-used")
    monkeypatch.setattr(settings, "phone_audio_backend", "webrtc_strict")
    assert resolve_phone_audio_backend(None) == "webrtc_strict"
    assert resolve_phone_audio_backend("auto") == "webrtc_strict"
    assert resolve_phone_audio_backend("webrtc") == "webrtc"
    assert is_strict_webrtc("webrtc_strict") is True
    status = public_audio_status()
    assert status["strict_webrtc"] is True
    assert status["pcm_fallback_allowed"] is False
    assert status["provider_key_in_browser"] is False
    assert "OWNER FAILURE" in status["mobile_voice_status"]


def test_strict_webrtc_unavailable_without_key(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "phone_audio_backend", "webrtc_strict")
    try:
        resolve_phone_audio_backend("webrtc_strict")
        raise AssertionError("expected HTTPException")
    except HTTPException as exc:
        assert exc.status_code == 503


def test_critical_tokens_and_logprobs() -> None:
    found = critical_tokens("Do not open Spotify; I'm asking about Wi-Fi.")
    assert "spotify" in found
    assert "wi-fi" in found
    assert "do not" in found
    assert logprob_confidence([-0.2, -0.1]) is not None
    assert logprob_confidence(None) is None


def test_pwa_strict_mode_never_falls_back_in_talk() -> None:
    app_js = (ROOT / "clients" / "pwa" / "app.js").read_text()
    webrtc = (ROOT / "clients" / "pwa" / "webrtc.js").read_text()
    html = (ROOT / "clients" / "pwa" / "index.html").read_text()
    assert 'media_backend: "webrtc_strict"' in app_js
    assert "Couldn't connect to Evie Voice." in app_js
    assert "Voice connection failed." not in app_js
    assert "createMediaStreamSource" not in webrtc
    assert "addTrack(this.micTrack" in webrtc
    assert "extraRemoteTracks" in webrtc
    assert "Mic Check" in html
    assert "Speech Recognition Check" in html
    assert "Report misheard phrase" in html
    assert "VOICE HEALTH" in html
    assert "2026.08.21.22" in app_js
    assert "2026.08.21.22" in html
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
