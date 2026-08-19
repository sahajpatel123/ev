"""``make preflight`` — is EV actually real right now?

Reads the *active* configuration (environment + .env) and reports, per organ,
whether it is a real engine or an offline double, plus the exact remediation
when it is not real (missing key, missing weight file, missing binary, or a
missing OS permission). One screen, exit 0: the summary line is the answer.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from app.config import settings
from app.ml.settings import get_ml_settings

MODEL_DIR = Path(get_ml_settings().ml_model_dir).expanduser()
REPO_ROOT = Path(__file__).resolve().parents[2]


def _pkg(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _file(*parts: str) -> bool:
    return (MODEL_DIR.joinpath(*parts)).is_file()


def _flag(status: str) -> str:
    return {"REAL": "[REAL]  ", "DOUBLE": "[DOUBLE]", "PARTIAL": "[PARTIAL]"}.get(
        status, f"[{status}]"
    )


def _check_opencode() -> tuple[str, str, str]:
    """--- AGENT OPENCODE --- chat via a local `opencode serve` session API."""

    import httpx

    from app.gateway.opencode import api_key_status

    key_present, key_source = api_key_status()
    label = f"opencode({settings.opencode_provider_id}/{settings.opencode_model})"
    start = (
        "`launchctl kickstart -k gui/$UID/ev.opencode` (plist: "
        "launchd/ev.opencode.plist) or `opencode serve --hostname 127.0.0.1 --port 4096`"
    )
    try:
        response = httpx.get(f"{settings.opencode_base_url}/global/health", timeout=3.0)
        healthy = response.status_code == 200 and response.json().get("healthy") is True
        version = response.json().get("version", "?")
    except Exception:  # noqa: BLE001 - preflight must not crash
        healthy, version = False, "?"
    if not healthy:
        return (
            "PARTIAL",
            label,
            f"server unreachable at {settings.opencode_base_url}; start it with {start}",
        )
    if not key_present:
        return (
            "PARTIAL",
            label,
            f"server {version} is up but no OPENCODE_API_KEY is visible to EV; put it in "
            f"{settings.opencode_env_file} (chmod 600) or EV's .env, then restart it with "
            f"{start}",
        )
    return (
        "REAL",
        label,
        f"opencode {version} reachable, key from {key_source}, agent "
        f"{settings.opencode_agent} (ephemeral sessions: "
        f"{not settings.opencode_session_reuse})",
    )


def _check_chat() -> tuple[str, str, str]:
    provider = settings.chat_provider
    if provider == "opencode":
        return _check_opencode()
    if provider == "deepseek":
        if settings.deepseek_api_key:
            from app.voice.live.grok_voice import grok_voice_enabled

            model = settings.deepseek_model or "deepseek-v4-flash"
            if grok_voice_enabled():
                from app.voice.live.grok_voice import live_realtime_provider

                live = (
                    "live: gpt-realtime-2.1-mini"
                    if live_realtime_provider() == "openai"
                    else "live: Grok Voice Think Fast 2.0"
                )
            elif (settings.xai_api_key or "").strip():
                live = "live: pipeline (EV_VOICE_LIVE_BRAIN=pipeline)"
            else:
                live = "live: DeepSeek pipeline until EV_XAI_API_KEY is set"
            return "REAL", "deepseek", f"DeepSeek API key set ({model}); {live}"
        return (
            "PARTIAL",
            "deepseek",
            "configured DeepSeek but EV_DEEPSEEK_API_KEY is empty — chat will "
            "fail at request time; fill the key in .env",
        )
    if provider == "xai":
        if settings.xai_api_key:
            from app.voice.live.grok_voice import live_realtime_provider

            live_p = live_realtime_provider()
            if live_p == "openai":
                live = "typed: grok-4.6; live: gpt-realtime-2.1-mini"
            elif live_p == "xai":
                live = "typed: grok-4.6; live: Grok Voice Think Fast 2.0"
            else:
                live = f"typed+live pipeline: {settings.xai_model}"
            return "REAL", "xai", f"xAI API key set ({live})"
        return (
            "PARTIAL",
            "xai",
            "configured xAI but EV_XAI_API_KEY is empty — chat/live will "
            "fail at request time; fill the key in .env",
        )
    if provider == "local":
        return (
            "PARTIAL",
            "local",
            "local LLM not recommended on 8 GB; set EV_LOCAL_MODEL_BASE_URL "
            "and EV_LOCAL_MODEL_NAME if you insist",
        )
    return (
        "DOUBLE",
        provider,
        "test double; set EV_CHAT_PROVIDER=deepseek and EV_DEEPSEEK_API_KEY",
    )


def _check_asr() -> tuple[str, str, str]:
    provider = settings.voice_asr_provider
    if provider in ("parakeet", "parakeet_tdt"):
        engine = settings.voice_asr_engine if provider == "parakeet" else settings.voice_asr_alt_engine
        explicit = settings.voice_asr_onnx_path
        present = (
            bool(explicit and Path(explicit).expanduser().is_file())
            or _file(f"asr-{engine}.onnx")
        )
        if present and _pkg("onnxruntime"):
            return "REAL", f"parakeet({engine})", "weights present"
        return (
            "PARTIAL",
            f"parakeet({engine})",
            "weights missing; Agent 2 must pin sha256 and you run "
            f"`uv run python -m app.ml.cli pull asr-{engine}` "
            "(or switch to EV_VOICE_ASR_PROVIDER=openai_compat + "
            "EV_ALLOW_REMOTE_ASR=true with a key)",
        )
    if provider == "openai_compat":
        if settings.voice_asr_base_url and settings.voice_asr_api_key:
            return "REAL", "openai_compat", "hosted ASR configured"
        return (
            "PARTIAL",
            "openai_compat",
            "set EV_VOICE_ASR_BASE_URL + EV_VOICE_ASR_API_KEY (+ EV_ALLOW_REMOTE_ASR=true)",
        )
    if provider == "faster_whisper":
        return (
            "PARTIAL",
            "faster_whisper",
            "weights cached? run `uv run python -m app.ml.cli pull "
            "asr-faster-whisper-base` (seed entry: Agent 2 must pin sha256)",
        )
    return (
        "DOUBLE",
        provider,
        "test double; set EV_VOICE_ASR_PROVIDER=parakeet (recommended) or openai_compat",
    )


def _check_tts() -> tuple[str, str, str]:
    provider = settings.voice_tts_provider
    if provider == "kokoro":
        engine = settings.voice_tts_engine
        present = _file(f"tts-{engine}.onnx")
        if present and _pkg("kokoro"):
            return "REAL", f"kokoro({engine})", "weights + kokoro package present"
        return (
            "PARTIAL",
            f"kokoro({engine})",
            "weights missing; Agent 2 must pin sha256 and you run "
            f"`uv run python -m app.ml.cli pull tts-{engine}` "
            "(kokoro package also required)",
        )
    if provider == "openai_compat":
        if settings.voice_tts_base_url and settings.voice_tts_api_key:
            return "REAL", "openai_compat", "hosted TTS configured"
        return (
            "PARTIAL",
            "openai_compat",
            "set EV_VOICE_TTS_BASE_URL + EV_VOICE_TTS_API_KEY (+ EV_ALLOW_REMOTE_TTS=true)",
        )
    return (
        "DOUBLE",
        provider,
        "test double; set EV_VOICE_TTS_PROVIDER=kokoro (recommended) or openai_compat",
    )


def _check_speaker() -> tuple[str, str, str]:
    provider = settings.voiceprint_provider
    if provider == "campp":
        found = any(
            _file(name)
            for name in (
                "speaker-campp.onnx",
                "speaker-ecapa.onnx",
                "campp.onnx",
                "model.onnx",
                "speaker.onnx",
            )
        )
        if not found:
            configured = settings.voiceprint_model_dir
            if configured:
                candidate = Path(configured).expanduser()
                found = candidate.is_file() or (
                    candidate.is_dir()
                    and any(
                        "speaker" in path.name.lower()
                        or path.name in ("campp.onnx", "model.onnx")
                        for path in candidate.glob("*.onnx")
                    )
                )
        if found and _pkg("onnxruntime"):
            return "REAL", "campp", "weights present"
        return (
            "PARTIAL",
            "campp",
            "weights missing; export CAM++ to .onnx in EV_VOICEPRINT_MODEL_DIR. "
            "The voice path HARD-REFUSES until then (hash double is not a "
            "security control)",
        )
    if provider == "speechbrain":
        if _pkg("speechbrain"):
            return "REAL", "speechbrain", "speechbrain installed"
        return "PARTIAL", "speechbrain", "speechbrain package not installed"
    if provider in ("http",):
        if settings.voiceprint_base_url:
            return "REAL", "http", "remote encoder configured"
        return "PARTIAL", "http", "set EV_VOICEPRINT_BASE_URL"
    return (
        "DOUBLE",
        provider or "(unset)",
        "hash test double — refused outside pytest by "
        "default_speaker_verifier(); set EV_VOICEPRINT_PROVIDER=campp",
    )


def _evvision_binary() -> str | None:
    try:
        from app.vision.providers import find_evvision_binary

        binary = find_evvision_binary()
    except Exception:  # noqa: BLE001 - preflight must not crash
        binary = "evvision"
    if shutil.which(binary):
        return binary
    if Path(binary).expanduser().is_file():
        return binary
    return None


def _check_vision() -> tuple[str, str, str]:
    provider = settings.vision_provider
    if provider == "apple_vision":
        binary = _evvision_binary()
        if binary:
            return "REAL", "apple_vision", f"evvision helper at {binary}"
        return (
            "PARTIAL",
            "apple_vision",
            "helper binary missing; build it: (cd helpers/evvision && "
            "swift build -c release)",
        )
    if provider == "tesseract":
        binary = settings.vision_tesseract_binary
        if shutil.which(binary):
            return "REAL", "tesseract", f"binary at {shutil.which(binary)}"
        return "PARTIAL", "tesseract", "tesseract binary not on PATH"
    return (
        "DOUBLE",
        provider,
        "deterministic double; set EV_VISION_PROVIDER=apple_vision and build "
        "the evvision helper",
    )


def _check_wake() -> tuple[str, str, str]:
    provider = settings.voice_wake_provider
    if provider == "openwakeword":
        configured = settings.voice_wake_openwakeword_model_path
        path = Path(configured).expanduser() if configured else MODEL_DIR / "wake-openwakeword.onnx"
        if path.is_file():
            return "REAL", "openwakeword", f"model at {path}"
        return (
            "PARTIAL",
            "openwakeword",
            "model missing; train/export the custom EVIE head to "
            "EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH (Agent 3 owns the trainer)",
        )
    if provider == "porcupine":
        if settings.voice_wake_access_key and settings.voice_wake_model_path:
            return "REAL", "porcupine", "access key + .ppn configured"
        return "PARTIAL", "porcupine", "set EV_VOICE_WAKE_ACCESS_KEY + EV_VOICE_WAKE_MODEL_PATH"
    return (
        "DOUBLE",
        provider,
        "phrase double; set EV_VOICE_WAKE_PROVIDER=openwakeword with a trained head",
    )


def _check_embeddings() -> tuple[str, str, str]:
    provider = settings.embedding_provider
    if provider == "granite":
        present = _file("embed-granite-r2.onnx") or _file(
            "granite-embedding-97m-multilingual-r2.onnx"
        )
        if present and _pkg("onnxruntime"):
            return "REAL", "granite-r2", "weights present"
        return (
            "PARTIAL",
            "granite-r2",
            "weights missing; run `uv run python -m app.ml.cli pull "
            "embed-granite-r2` (verified entry)",
        )
    if provider == "qwen3":
        if _file("qwen3-embedding-0.6b.onnx") and _pkg("onnxruntime"):
            return "REAL", "qwen3", "weights present"
        return "PARTIAL", "qwen3", "weights missing; pull qwen3-embedding-0.6b"
    if provider == "http":
        if settings.embedding_base_url and settings.embedding_api_key:
            return "REAL", "http", "hosted embeddings configured"
        return "PARTIAL", "http", "set EV_EMBEDDING_BASE_URL + EV_EMBEDDING_API_KEY"
    return (
        "DOUBLE",
        provider,
        "hash double; set EV_EMBEDDING_PROVIDER=granite (Agent 8 recommendation)",
    )


def _check_face() -> tuple[str, str, str]:
    provider = settings.face_provider
    if provider == "sface":
        if _file("face-sface.onnx") and _pkg("onnxruntime") and _pkg("cv2"):
            return "REAL", "sface", "weights present"
        return (
            "PARTIAL",
            "sface",
            "weights/opencv missing; pull face-sface (verified) and install "
            "the face extra",
        )
    return (
        "DOUBLE",
        provider,
        "hash double; set EV_FACE_PROVIDER=sface with weights",
    )


def _check_liveness() -> tuple[str, str, str]:
    if _file("liveness-audio.onnx") and _pkg("onnxruntime"):
        return "REAL", "liveness-audio", "weights present"
    return (
        "PARTIAL",
        "liveness-audio",
        "weights missing (seed entry); voice enrollment fails closed until "
        "Agent 2 pins sha256 and you run `uv run python -m app.ml.cli pull "
        "liveness-audio`",
    )


def _check_storage() -> tuple[str, str, str]:
    backend = settings.object_store_backend
    if backend == "local":
        return "REAL", "local", f"filesystem store at {settings.storage_root}"
    if backend == "s3":
        if settings.s3_endpoint_url and settings.s3_access_key:
            return "REAL", "s3", f"object store at {settings.s3_endpoint_url}"
        return "PARTIAL", "s3", "EV_S3_* credentials incomplete"
    return "DOUBLE", backend, "unknown object store backend"


def _check_database() -> tuple[str, str, str]:
    url = settings.database_url
    if not url.startswith("postgresql"):
        return "DOUBLE", "sqlite", "dev SQLite; native profile requires Postgres"
    try:
        env = os.environ.copy()
        env["PGPASSWORD"] = "ev"
        result = subprocess.run(
            ["psql", "-h", "localhost", "-U", "ev", "-d", "ev", "-tAc", "SELECT 1"],
            capture_output=True,
            text=True,
            check=False,
            timeout=4,
            env=env,
        )
        ok = result.returncode == 0 and result.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        ok = False
    return (
        ("REAL", "postgres", "native Postgres reachable")
        if ok
        else ("PARTIAL", "postgres", "configured but not reachable; `make native-up`")
    )


def _check_redis() -> tuple[str, str, str]:
    try:
        with socket.create_connection(("127.0.0.1", 6379), timeout=2):
            return "REAL", "redis", "reachable on 6379"
    except OSError:
        return "PARTIAL", "redis", "not reachable on 6379; `make native-up`"


CHECKS = [
    ("chat", _check_chat),
    ("asr", _check_asr),
    ("tts", _check_tts),
    ("speaker", _check_speaker),
    ("liveness", _check_liveness),
    ("vision", _check_vision),
    ("wake", _check_wake),
    ("embeddings", _check_embeddings),
    ("face", _check_face),
    ("storage", _check_storage),
    ("database", _check_database),
    ("redis", _check_redis),
]


def main() -> int:
    print("EV preflight — is EV actually real right now?")
    print("=" * 72)
    rows: list[tuple[str, str, str, str]] = []
    counts = {"REAL": 0, "DOUBLE": 0, "PARTIAL": 0}
    for organ, checker in CHECKS:
        status, provider, remediation = checker()
        counts[status] = counts.get(status, 0) + 1
        rows.append((status, organ, provider, remediation))
    for status, organ, provider, remediation in rows:
        print(f"{_flag(status)} {organ:<12} {provider:<24} {remediation}")
    print("=" * 72)
    print(
        f"Summary: {counts['REAL']} REAL, {counts['DOUBLE']} DOUBLE, "
        f"{counts['PARTIAL']} PARTIAL"
    )
    doubles = [organ for status, organ, _, _ in rows if status == "DOUBLE"]
    if not doubles:
        print("EV is real (missing pieces are listed as PARTIAL above).")
    else:
        print(
            "EV is NOT fully real yet — doubles: "
            + ", ".join(doubles)
            + ". Run `make eval-ml` once recordings exist, then re-check."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
