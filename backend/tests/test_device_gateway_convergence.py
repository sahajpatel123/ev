"""Convergence extras for Cross-Platform Core v1. Run with test_device_gateway.py."""

from __future__ import annotations

from app.voice.live.grok_voice import capability_instructions, grok_voice_instructions


def test_pwa_audio_engine_is_single_scheduled_path() -> None:
    from pathlib import Path

    app_js = (Path(__file__).resolve().parents[1] / "clients" / "pwa" / "app.js").read_text()
    assert "2026.08.21.22" in app_js
    assert "EvieAudioPlaybackEngine" in app_js
    assert "EvieWebRTC" in app_js


def test_sandbox_realtime_instructions_are_compact() -> None:
    manifest = {"memory_scope": "sandbox", "memory_bootstrap": {"relationship": "SECRET"}}
    text = grok_voice_instructions(capability_manifest=manifest) + capability_instructions(manifest)
    assert "sandbox" in text.lower()
    assert "SECRET" not in text
    assert "inspect_ui" not in text
    assert "personal memory" in text.lower()
