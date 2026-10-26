"""Voice lock: Evie is female on every engine, permanently.

The "voice flipped to male by itself" bug came from unpinned voices:
clients spoke with the platform default (which the OS changes on its
own) and the server's Piper default was the male Lessac voice. Every
default below is female; changing Evie's voice requires an explicit
setting, never happens automatically.
"""

from __future__ import annotations

from app.config import settings
from app.voice.tts import DEFAULT_PIPER_VOICE

# Pinned female defaults, one per engine. Change any of these only as a
# deliberate, owner-approved voice change — never silently.
PINNED_FEMALE_VOICES = {
    "piper_default": "en_US-amy-medium",
    "kokoro": "bf_alice",
    "edge": "en-GB-SoniaNeural",
    "say": "Ava",
    "gemini_live": "Aoede",
}


def test_piper_default_is_female() -> None:
    assert PINNED_FEMALE_VOICES["piper_default"] == DEFAULT_PIPER_VOICE
    assert "lessac" not in DEFAULT_PIPER_VOICE.lower()


def test_kokoro_default_is_female() -> None:
    assert settings.voice_tts_kokoro_voice == PINNED_FEMALE_VOICES["kokoro"]


def test_edge_default_is_female() -> None:
    assert settings.voice_tts_edge_voice == PINNED_FEMALE_VOICES["edge"]


def test_say_default_is_female() -> None:
    assert settings.voice_tts_say_voice == PINNED_FEMALE_VOICES["say"]


def test_gemini_live_default_is_female() -> None:
    assert settings.gemini_live_voice == PINNED_FEMALE_VOICES["gemini_live"]


def test_explicit_override_paths_exist() -> None:
    """Every engine offers an explicit setting; nothing auto-switches voice."""

    fields = set(type(settings).model_fields)
    assert {
        "voice_tts_voice",
        "voice_tts_kokoro_voice",
        "voice_tts_edge_voice",
        "voice_tts_say_voice",
        "gemini_live_voice",
    } <= fields


def test_piper_downloader_fetches_female_voice_with_checksum() -> None:
    from app.voice.models_setup import PIPER_VOICE, PIPER_VOICE_CONFIG

    assert PIPER_VOICE.name == "en_US-amy-medium.onnx"
    assert "/amy/" in PIPER_VOICE.url
    assert len(PIPER_VOICE.sha256) == 64
    assert PIPER_VOICE_CONFIG.name == "en_US-amy-medium.onnx.json"
    assert len(PIPER_VOICE_CONFIG.sha256) == 64


def test_model_registry_pins_female_piper_voice() -> None:
    from app.ml.registry import builtin_models

    names = [spec.name for spec in builtin_models()]
    assert "tts-piper-en-amy-medium" in names
    assert "tts-piper-en-lessac-medium" not in names


def test_compat_synthesizer_default_is_female() -> None:
    import inspect

    from app.voice.tts import OpenAICompatSynthesizer

    params = inspect.signature(OpenAICompatSynthesizer.__init__).parameters
    assert params["voice"].default == "nova"


def test_female_defaults_have_no_male_markers() -> None:
    male_markers = (
        "lessac",
        "ryan",
        "daniel",
        "david",
        "mark",
        "guy",
        "tony",
        "onyx",
        "echo",
        "fable",
    )
    for name, voice in PINNED_FEMALE_VOICES.items():
        assert not any(marker in voice.lower() for marker in male_markers), name
