"""Install the local speech models EVIE needs to actually hear and answer.

Two artifacts, both optional and both checksum-pinned:

* **wake + ASR** — the Vosk small en-US model (a *directory*, shipped as a zip).
  Backs the always-on "EVIE" spotter and the command transcriber.
* **reply voice** — a Piper voice (``.onnx`` + ``.onnx.json``) so replies are
  spoken by the server. Without it clients fall back to the platform voice.

Downloads never happen implicitly at request time: a human runs

    uv run python -m app.voice.models_setup

Re-running is safe; already-installed artifacts are verified, not refetched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import settings
from app.voice.vosk_engine import DEFAULT_MODEL_DIRNAME, default_model_path

DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0)
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Artifact:
    name: str
    url: str
    sha256: str
    size_mb: int
    license: str
    archive: bool = False


VOSK_SMALL_EN = Artifact(
    name=DEFAULT_MODEL_DIRNAME,
    url="https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    sha256="30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498",
    size_mb=40,
    license="Apache-2.0 (Vosk / alphacephei)",
    archive=True,
)

PIPER_VOICE = Artifact(
    name="en_US-lessac-medium.onnx",
    url=(
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
        "en/en_US/lessac/medium/en_US-lessac-medium.onnx"
    ),
    sha256="5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f",
    size_mb=63,
    license="MIT (Piper voice, Lessac corpus CC BY 4.0)",
)

PIPER_VOICE_CONFIG = Artifact(
    name="en_US-lessac-medium.onnx.json",
    url=(
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
        "en/en_US/lessac/medium/en_US-lessac-medium.onnx.json"
    ),
    sha256="efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0",
    size_mb=1,
    license="MIT (Piper voice config)",
)


def models_root() -> Path:
    root = Path(settings.voice_tts_model_dir or (Path.home() / ".ev" / "models")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _download(artifact: Artifact, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    partial = destination.with_suffix(destination.suffix + ".part")
    with httpx.stream(
        "GET", artifact.url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT
    ) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes(CHUNK):
                digest.update(chunk)
                handle.write(chunk)
    actual = digest.hexdigest()
    if actual != artifact.sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"{artifact.name}: checksum mismatch (expected {artifact.sha256}, got {actual})"
        )
    partial.replace(destination)
    return destination


def install_vosk_model(*, force: bool = False) -> dict:
    """Download + extract the wake/ASR model directory."""

    target = Path(default_model_path()).expanduser()
    if target.is_dir() and (target / "am").is_dir() and not force:
        return {"artifact": VOSK_SMALL_EN.name, "path": str(target), "status": "present"}
    with tempfile.TemporaryDirectory(prefix="ev-vosk-") as tmp:
        archive = Path(tmp) / "model.zip"
        _download(VOSK_SMALL_EN, archive)
        extract_root = Path(tmp) / "extract"
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                # Refuse absolute paths and traversal in the archive.
                if member.startswith("/") or ".." in Path(member).parts:
                    raise RuntimeError(f"unsafe path in archive: {member!r}")
            bundle.extractall(extract_root)
        extracted = extract_root / DEFAULT_MODEL_DIRNAME
        if not extracted.is_dir():
            candidates = [item for item in extract_root.iterdir() if item.is_dir()]
            if len(candidates) != 1:
                raise RuntimeError("unexpected Vosk archive layout")
            extracted = candidates[0]
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), str(target))
    return {"artifact": VOSK_SMALL_EN.name, "path": str(target), "status": "installed"}


def install_piper_voice(*, force: bool = False) -> dict:
    """Download the local reply voice (Piper ONNX + config)."""

    root = models_root()
    results = []
    for artifact in (PIPER_VOICE, PIPER_VOICE_CONFIG):
        target = root / artifact.name
        if target.is_file() and not force:
            results.append("present")
            continue
        _download(artifact, target)
        results.append("installed")
    return {
        "artifact": PIPER_VOICE.name,
        "path": str(root / PIPER_VOICE.name),
        "status": "present" if set(results) == {"present"} else "installed",
    }


def status() -> dict:
    from app.voice.tts import piper_binary_path, piper_voice_path
    from app.voice.vosk_engine import vosk_status

    voice = piper_voice_path()
    return {
        "wake_and_asr": vosk_status(),
        "reply_voice": {
            "engine": "piper",
            "ready": bool(voice and piper_binary_path()),
            "voice_path": voice,
            "binary": piper_binary_path(),
            "detail": (
                "ready"
                if voice and piper_binary_path()
                else "run `uv sync --extra voice` and `python -m app.voice.models_setup`"
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.voice.models_setup",
        description="Install the local wake-word/ASR model and reply voice for EVIE.",
    )
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument("--status", action="store_true", help="print readiness and exit")
    parser.add_argument("--skip-voice", action="store_true", help="only install wake/ASR")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(status(), indent=2))
        return 0

    report = [install_vosk_model(force=args.force)]
    if not args.skip_voice:
        report.append(install_piper_voice(force=args.force))
    for item in report:
        print(f"{item['status']:>9}  {item['artifact']}  -> {item['path']}")
    print()
    print(json.dumps(status(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
