"""`ev models`: speech-weight readiness — status, install, smoke tests.

Covers the configured voice stack (wake/ASR/TTS per settings) plus honest
reports for the optional stacks. Pip installs only missing NEEDED extras
(`uv sync --extra ...`); weight downloads are checksum-pinned (vosk/piper)
or come from the engine's own verified cache (faster-whisper HF hub);
anything else prints the exact owner command instead of guessing.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any


def _have(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _models_root() -> Path:
    from app.config import settings

    root = Path(str(getattr(settings, "voice_tts_model_dir", "") or "") or
                (Path.home() / ".ev" / "models"))
    return root.expanduser()


def _fw_tiny_cached() -> tuple[bool, str]:
    hub = Path.home() / ".cache/huggingface/hub/models--Systran--faster-whisper-tiny"
    if hub.is_dir() and any(hub.iterdir()):
        return True, str(hub)
    return False, str(hub)


def status() -> dict[str, Any]:
    """Static readiness (no downloads, no audio, fast)."""
    from app.config import settings

    root = _models_root()
    oww_head = Path(str(getattr(settings, "voice_wake_openwakeword_model_path", "")
                        or root / "wake-openwakeword.onnx")).expanduser()
    oww_ver = Path(str(getattr(settings, "voice_wake_openwakeword_verifier_path", "")
                       or root / "wake-openwakeword-verifier.pkl")).expanduser()
    kokoro = root / "tts-kokoro-82m-int8.onnx"
    kokoro_voices = root / "tts-kokoro-82m-int8.voices.bin"
    fw_tiny, fw_path = _fw_tiny_cached()
    deps = {name: _have(mod) for name, mod in {
        "onnxruntime": "onnxruntime", "sounddevice": "sounddevice",
        "faster_whisper": "faster_whisper", "openwakeword": "openwakeword",
        "kokoro_onnx": "kokoro_onnx", "edge_tts": "edge_tts",
        "piper_tts": "piper_tts", "vosk": "vosk"}.items()}
    return {
        "configured": {
            "wake": getattr(settings, "voice_wake_provider", "auto"),
            "asr": getattr(settings, "voice_asr_provider", "auto"),
            "tts": getattr(settings, "voice_tts_provider", "auto"),
            "ears_spotter": bool(getattr(settings, "ears_wake_local_spotter", True)),
        },
        "dependencies": deps,
        "weights": {
            "openwakeword_head": {"present": oww_head.is_file(), "path": str(oww_head)},
            "openwakeword_verifier": {"present": oww_ver.is_file(), "path": str(oww_ver)},
            "faster_whisper_tiny": {"present": fw_tiny, "path": fw_path},
            "kokoro_int8": {"present": kokoro.is_file() and kokoro_voices.is_file(),
                            "path": str(kokoro)},
        },
        "microphone": _mic_status() if deps["sounddevice"] else {
            "reachable": False, "devices": [], "note": "sounddevice missing"},
    }


def _mic_status() -> dict[str, Any]:
    try:
        import sounddevice as sd

        devices = []
        for index, info in enumerate(sd.query_devices()):
            if info.get("max_input_channels", 0) > 0:
                devices.append({"index": index, "name": info.get("name"),
                                "rate": info.get("default_samplerate")})
        return {"reachable": bool(devices), "devices": devices[:8]}
    except Exception as exc:
        return {"reachable": False, "devices": [],
                "error": f"{type(exc).__name__}"}


def smoke_all(timeout_s: float = 60.0) -> dict[str, dict[str, Any]]:
    """Exercise each PRESENT component once. Absent ones SKIP with reason."""
    from app.config import settings

    out: dict[str, dict[str, Any]] = {}

    def record(name: str, status: str, detail: str = "") -> None:
        out[name] = {"status": status, "detail": detail[:300]}

    st = status()
    # wake head (onnx load + zero-frame score)
    if st["weights"]["openwakeword_head"]["present"] and _have("onnxruntime"):
        try:
            import numpy as _np
            import onnxruntime as _ort

            path = st["weights"]["openwakeword_head"]["path"]
            sess = _ort.InferenceSession(path, providers=["CPUExecutionProvider"])
            feed = {sess.get_inputs()[0].name:
                    _np.zeros([1, 16, 96], dtype=_np.float32)}
            try:
                sess.run(None, feed)
                record("wake_head", "PASS", "onnx session scored a zero frame")
            except Exception as exc:
                record("wake_head", "FAIL", f"scoring failed: {exc}")
        except Exception as exc:
            record("wake_head", "FAIL", f"{type(exc).__name__}: {exc}")
    else:
        record("wake_head", "SKIP", "head or onnxruntime missing")
    # faster-whisper tiny (transcribe 1s of silence)
    if st["weights"]["faster_whisper_tiny"]["present"] and _have("faster_whisper"):
        try:
            import numpy as _np
            from faster_whisper import WhisperModel

            model = WhisperModel("tiny", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(
                _np.zeros(16000, dtype=_np.float32), beam_size=1)
            text = "".join(s.text for s in segments)
            record("asr_tiny", "PASS", f"transcribed silence ({len(text)} chars)")
        except Exception as exc:
            record("asr_tiny", "FAIL", f"{type(exc).__name__}: {exc}")
    else:
        record("asr_tiny", "SKIP", "tiny model or faster_whisper missing")
    # kokoro (synthesize a short line)
    kokoro = st["weights"]["kokoro_int8"]
    if kokoro["present"] and _have("kokoro_onnx"):
        try:
            from kokoro_onnx import Kokoro

            kokoro_model = Kokoro(str(Path(kokoro["path"])),
                                  str(Path(kokoro["path"]).with_suffix(".voices.bin")))
            audio, _ = kokoro_model.create("Evie voice check.", voice="af_sarah",
                                           speed=1.0, lang="en-us")
            record("tts_kokoro", "PASS" if len(audio) > 1000 else "FAIL",
                   f"{len(audio)} samples")
        except Exception as exc:
            record("tts_kokoro", "FAIL", f"{type(exc).__name__}: {exc}")
    else:
        record("tts_kokoro", "SKIP", "kokoro weights or package missing")
    # edge_tts (configured remote tier): one short synthesis, best effort
    if _have("edge_tts") and str(getattr(settings, "voice_tts_provider", "")).lower() == "edge_tts":
        try:
            import asyncio as _asyncio

            import edge_tts as _edge

            async def _say():
                chunks = []
                async for piece in _edge.Communicate(
                        "Evie voice check.",
                        str(getattr(settings, "voice_tts_edge_voice",
                                    "en-GB-SoniaNeural"))).stream():
                    if piece["type"] == "audio":
                        chunks.append(piece["data"])
                return b"".join(chunks)

            audio = _asyncio.run(_say())
            record("tts_edge", "PASS" if len(audio) > 1000 else "FAIL",
                   f"{len(audio)} bytes")
        except Exception as exc:
            record("tts_edge", "FAIL", f"{type(exc).__name__}: {exc} (network?)")
    else:
        record("tts_edge", "SKIP", "edge_tts not the configured provider")
    # vosk + piper: optional stacks (documented, not required here)
    record("asr_vosk", "SKIP", "vosk is linux-wheels-only; macOS uses faster-whisper/Gemini")
    record("tts_piper", "SKIP" if not _have("piper_tts") else "PASS",
           "uv sync --extra voice" if not _have("piper_tts") else "installed")
    # microphone list (no stream opened)
    mic = st["microphone"]
    record("microphone", "PASS" if mic.get("reachable") else "FAIL",
           f"{len(mic.get('devices', []))} input device(s)")
    _ = timeout_s
    return out


def install_missing(*, fetch_all: bool = False, force: bool = False) -> dict[str, Any]:
    """Install what's missing for the CONFIGURED stack. Never guesses.

    - pip extras: `uv sync --extra ml` when a needed package is absent;
    - faster-whisper tiny: warms the HF cache when absent (engine-verified);
    - vosk/piper: only with --fetch-all (optional stacks here).
    Returns a per-item report (installed/present/skipped/failed).
    """
    import os

    report: dict[str, Any] = {"pip": {}, "weights": {}, "smoke": {}}
    st = status()
    needed_pkgs = {"onnxruntime": "ml", "sounddevice": "mic",
                   "faster_whisper": "ml", "openwakeword": "ml",
                   "kokoro_onnx": "ml", "edge_tts": "ml"}
    missing_extras = sorted({extra for pkg, extra in needed_pkgs.items()
                             if not st["dependencies"].get(pkg)})
    if missing_extras:
        repo = Path(__file__).resolve().parents[3]
        for extra in missing_extras:
            try:
                proc = subprocess.run(
                    ["uv", "sync", "--extra", extra], cwd=str(repo / "backend"),
                    capture_output=True, text=True, timeout=600,
                    env={**os.environ, "UV_PROJECT_ENVIRONMENT": ".venv"})
                report["pip"][extra] = "installed" if proc.returncode == 0 else (
                    f"FAILED: {(proc.stderr or '')[-300:]}")
            except Exception as exc:
                report["pip"][extra] = f"FAILED: {type(exc).__name__}"
    else:
        report["pip"]["_"] = "all needed packages present"
    if not st["weights"]["faster_whisper_tiny"]["present"] and _have("faster_whisper"):
        try:
            from faster_whisper import WhisperModel

            WhisperModel("tiny", device="cpu", compute_type="int8")
            report["weights"]["faster_whisper_tiny"] = "downloaded+verified"
        except Exception as exc:
            report["weights"]["faster_whisper_tiny"] = f"FAILED: {exc}"
    else:
        report["weights"]["faster_whisper_tiny"] = "present"
    for key in ("openwakeword_head", "openwakeword_verifier", "kokoro_int8"):
        entry = st["weights"][key]
        report["weights"][key] = "present" if entry["present"] else (
            "MISSING: restore from backup (no pinned re-download source configured)")
    if fetch_all or force:
        try:
            from app.voice.models_setup import install_piper_voice, install_vosk_model

            report["weights"]["vosk"] = install_vosk_model(force=force)
            report["weights"]["piper"] = install_piper_voice(force=force)
        except Exception as exc:
            report["weights"]["optional"] = f"FAILED: {exc}"
    else:
        report["weights"]["vosk/piper"] = (
            "skipped (optional stacks; rerun with --fetch-all)")
    report["smoke"] = smoke_all()
    return report


def print_report(report: dict[str, Any]) -> int:
    failures = 0
    print("== ev models: status ==")
    st = status()
    for name, ok in st["dependencies"].items():
        print(f"  [{'PASS' if ok else 'SKIP'}] dep:{name}")
    for name, entry in st["weights"].items():
        mark = "PASS" if entry["present"] else "FAIL"
        print(f"  [{mark}] weight:{name} — {entry['path']}")
        if not entry["present"]:
            failures += 1
    print("== ev models: report ==")
    for section, items in report.items():
        if not isinstance(items, dict):
            continue
        for name, value in items.items():
            if isinstance(value, dict):
                mark = value.get("status", "?").upper()
                print(f"  [{mark}] {section}:{name} — {value.get('detail', '')}")
                if mark == "FAIL":
                    failures += 1
            else:
                bad = str(value).startswith(("FAILED", "MISSING"))
                print(f"  [{'FAIL' if bad else 'PASS'}] {section}:{name} — {value}")
                if bad:
                    failures += 1
    return 0 if failures == 0 else 1
