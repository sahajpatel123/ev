"""Voice activation preflight (Agent 2 / Foundry).

Read-only diagnostic for speaker, wake, TTS, and ASR readiness with exact
remediation. Run via ``make preflight`` (loads .env) or directly:

    cd backend && uv run python -m app.ml.voice_preflight
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value else None


def _expand(value: str | Path | None) -> Path | None:
    return Path(value).expanduser().resolve() if value else None


def _speaker_model_path() -> tuple[Path | None, str | None]:
    try:
        from app.voice.speaker import CamppSpeakerVerifier

        verifier = CamppSpeakerVerifier(require_available=False)
        return verifier._resolve_model_path(), None
    except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
        return None, f"app.voice.speaker import failed: {exc}"


def _status(label: str, ok: bool, detail: str, remediation: str) -> None:
    mark = "OK " if ok else "MISSING"
    print(f"{label}: [{mark}] {detail}")
    if not ok:
        print(f"    remediation: {remediation}")


def _check_speaker() -> None:
    provider = _env("EV_VOICEPRINT_PROVIDER") or "unset"
    print(f"speaker: provider={provider!r} engine=CamppSpeakerVerifier")
    if not _has_module("onnxruntime"):
        _status(
            "speaker",
            False,
            "onnxruntime not installed",
            "cd backend && uv sync --extra ml --extra face --extra dev",
        )
        return
    path, error = _speaker_model_path()
    if error:
        _status("speaker", False, error, "run via `make preflight` so .env (EV_VAULT_KEY) is loaded")
        return
    if path is not None:
        name = path.name.lower()
        plausible = name in {
            "campp.onnx",
            "model.onnx",
            "speaker.onnx",
            "speaker-campp.onnx",
            "speaker-ecapa.onnx",
        } or "campp" in str(path).lower() or "speaker" in str(path).lower()
        if plausible:
            _status("speaker", True, f"weights present at {path}", "")
        else:
            _status(
                "speaker",
                False,
                f"resolver picked {path.name} from EV_VOICEPRINT_MODEL_DIR, "
                "which is not a CAM++ export",
                "point EV_VOICEPRINT_MODEL_DIR at a dedicated directory and "
                "name the export campp.onnx (Agent 5: tighten the first-*.onnx "
                "heuristic so the shared model cache cannot be misread)",
            )
    else:
        _status(
            "speaker",
            False,
            "CAM++ ONNX weights absent",
            "No community ONNX export matches the raw-waveform contract "
            "(verified 2026-08-12: Alkd/campplus expects fbank [B,T,80]). "
            "Agent 5: run the export steps in docs/MODELS.md §Voice activation "
            "and drop campp.onnx into EV_VOICEPRINT_MODEL_DIR",
        )


def _check_wake() -> None:
    provider = _env("EV_VOICE_WAKE_PROVIDER") or "unset"
    print(f"wake: provider={provider!r} engine=OpenWakeWordEngine")
    if not _has_module("openwakeword"):
        _status(
            "wake",
            False,
            "openwakeword package not installed",
            "cd backend && uv sync --extra ml --extra face --extra dev",
        )
        return
    model_path = _expand(_env("EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH"))
    if model_path is not None and model_path.is_file():
        _status("wake", True, f"head present at {model_path}", "")
    else:
        target = model_path or Path.home() / ".ev" / "models" / "wake-openwakeword.onnx"
        local_spotter = os.environ.get("EV_EARS_WAKE_LOCAL_SPOTTER", "true").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if local_spotter and _has_module("faster_whisper"):
            _status(
                "wake",
                True,
                "custom EVIE head absent; local faster-whisper spotter is active "
                "(first run downloads the configured wake model)",
                f"optional lowest-latency upgrade: export the custom head to {target}",
            )
            return
        _status(
            "wake",
            False,
            "custom EVIE head absent",
            f"Agent 3 owns training: drop the trained head at {target} "
            "(do NOT train it in this task)",
        )


def _check_tts() -> None:
    provider = _env("EV_VOICE_TTS_PROVIDER") or "unset"
    print(f"tts: provider={provider!r}")
    if provider == "edge_tts":
        if not _has_module("edge_tts"):
            _status(
                "tts",
                False,
                "edge-tts package not installed",
                "cd backend && uv sync --extra ml --extra face --extra dev",
            )
        elif (_env("EV_ALLOW_REMOTE_TTS") or "false").lower() == "true":
            _status(
                "tts",
                True,
                "Edge neural TTS configured with the remote voice gate open",
                "",
            )
        else:
            _status(
                "tts",
                False,
                "edge_tts requires explicit remote TTS consent",
                "export EV_ALLOW_REMOTE_TTS=true or use local kokoro",
            )
        return
    if provider == "openai_compat":
        allowed = _env("EV_ALLOW_REMOTE_TTS")
        base_url = _env("EV_VOICE_TTS_BASE_URL")
        api_key = _env("EV_VOICE_TTS_API_KEY")
        if allowed == "true" and base_url and api_key:
            _status("tts", True, "openai_compat configured (remote gate open)", "")
        else:
            _status(
                "tts",
                False,
                "openai_compat needs the remote gate and credentials",
                "export EV_ALLOW_REMOTE_TTS=true and set EV_VOICE_TTS_BASE_URL + "
                "EV_VOICE_TTS_API_KEY",
            )
        return
    if provider == "kokoro":
        if not _has_module("kokoro_onnx"):
            _status(
                "tts",
                False,
                "kokoro-onnx package not installed",
                "cd backend && uv sync --extra ml --extra face --extra dev",
            )
            return
        if not _has_module("kokoro"):
            _status(
                "tts",
                False,
                "kokoro-onnx 0.5.0 installed (module kokoro_onnx) but tts.py "
                "still imports `from kokoro import KPipeline`",
                "TTS owner (Agent 3/5): update tts.py to the kokoro_onnx.Kokoro "
                "API; until then use EV_VOICE_TTS_PROVIDER=openai_compat",
            )
            return
        model = _expand(Path.home() / ".ev" / "models" / "tts-kokoro-82m-int8.onnx")
        voices = _expand(Path.home() / ".ev" / "models" / "tts-kokoro-82m-int8.voices.bin")
        if model is not None and model.is_file() and voices is not None and voices.is_file():
            _status("tts", True, "Kokoro int8 weights + voices present", "")
        else:
            _status(
                "tts",
                False,
                "Kokoro weights absent",
                "cd backend && uv run python -m app.ml.cli pull "
                "tts-kokoro-82m-int8 tts-kokoro-voices-v1.0",
            )
        return
    _status(
        "tts",
        False,
        f"provider={provider!r} needs a decision",
        "recommended production default: openai_compat with EV_ALLOW_REMOTE_TTS=true "
        "(fleet law §13); local fallback: kokoro after `uv run python -m app.ml.cli "
        "pull tts-kokoro-82m-int8 tts-kokoro-voices-v1.0`",
    )


def _check_asr() -> None:
    provider = _env("EV_VOICE_ASR_PROVIDER") or "unset"
    print(f"asr: provider={provider!r}")
    if provider == "faster_whisper":
        if not _has_module("faster_whisper"):
            _status(
                "asr",
                False,
                "faster-whisper package not installed",
                "cd backend && uv sync --extra ml --extra face --extra dev",
            )
            return
        _status(
            "asr",
            True,
            "faster-whisper installed; model downloads on first use "
            "(EV_VOICE_ASR_MODEL, default tiny)",
            "",
        )
        return
    if provider in ("parakeet", "parakeet_tdt", "parakeet-tdt"):
        _status(
            "asr",
            False,
            "Parakeet weights are not pinned (streaming split export does not "
            "match ParakeetOnnxSession's single-session contract)",
            "pragmatic default: export EV_VOICE_ASR_PROVIDER=faster_whisper; "
            "Parakeet needs Agent 4's export + Agent 2 pin before use",
        )
        return
    _status(
        "asr",
        False,
        f"provider={provider!r} is dev/remote",
        "recommended: export EV_VOICE_ASR_PROVIDER=faster_whisper",
    )


def main(argv: list[str] | None = None) -> int:
    print("EV voice activation preflight (Agent 2 / Foundry)")
    print()
    _check_speaker()
    _check_wake()
    _check_tts()
    _check_asr()
    print()
    print("Owner commands (run once):")
    print("  cd backend && uv sync --extra ml --extra face --extra dev")
    print("  uv run python -m app.ml.cli pull tts-kokoro-82m-int8 tts-kokoro-voices-v1.0")
    print("  export EV_VOICE_ASR_PROVIDER=faster_whisper")
    print("  # TTS: export EV_VOICE_TTS_PROVIDER=openai_compat + EV_ALLOW_REMOTE_TTS=true")
    print("  #      (or keep kokoro after the pull above)")
    print("  # Speaker: Agent 5 exports CAM++ ONNX per docs/MODELS.md §Voice activation")
    print("  # Wake: Agent 3 drops the trained head at EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
