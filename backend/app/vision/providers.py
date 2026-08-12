"""Vision/OCR providers: attachment bytes -> derived text + suggested labels.

The seam keeps EVIE model-agnostic and privacy-first:

- OCR runs locally (Apple Vision on macOS via the ``evvision`` helper,
  tesseract elsewhere, deterministic double for CI); raw media never leaves
  the device for OCR.
- Only derived text is ever offered to a reasoning provider, and only under
  the existing permission + privacy rules.
- A missing or broken OCR binary raises a typed error instead of masquerading
  as "no text found".
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.config import settings
from app.vision.settings import get_vision_settings


class VisionProviderError(RuntimeError):
    """Base error for the local vision/OCR layer."""


class VisionBinaryError(VisionProviderError):
    """The configured OCR binary is missing or cannot be executed."""


class VisionEngineError(VisionProviderError):
    """The OCR engine ran but failed on the given input."""


@dataclass
class VisionResult:
    """Derived output of one local vision/OCR pass."""

    ocr_text: str | None = None
    labels: list[dict] = field(default_factory=list)
    summary: str | None = None
    provider: str = "deterministic"
    lines: list[dict] = field(default_factory=list)
    degraded: bool = False


class VisionProvider(Protocol):
    name: str

    async def analyze(
        self,
        *,
        data: bytes,
        content_type: str | None = None,
        filename: str | None = None,
        prompt: str | None = None,
    ) -> VisionResult: ...


def _decode_text(data: bytes) -> str | None:
    """Return UTF-8 text if the bytes are largely printable; else None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    if printable < max(1, int(len(text) * 0.8)):
        return None
    return text


def _is_image_content(content_type: str | None, filename: str | None = None) -> bool:
    if (content_type or "").startswith("image/"):
        return True
    suffix = (filename or "").lower()
    return suffix.endswith((".png", ".jpg", ".jpeg", ".heic", ".tif", ".tiff", ".pdf"))


def _temporary_path(data: bytes, content_type: str | None, filename: str | None) -> str:
    if (filename or "").lower().endswith(".pdf") or (content_type or "") == "application/pdf":
        suffix = ".pdf"
    else:
        suffix = Path(filename or "image.png").suffix or ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        return tmp.name


class DeterministicVisionProvider:
    """Zero-dependency provider for tests and offline runs.

    Extracts embedded UTF-8 text (useful for text documents and fake image
    fixtures); real binary images yield no OCR text, which keeps the provider
    honest rather than hallucinating content. This is the offline double, so
    ``degraded=True``.
    """

    name = "deterministic"

    async def analyze(
        self,
        *,
        data: bytes,
        content_type: str | None = None,
        filename: str | None = None,
        prompt: str | None = None,
    ) -> VisionResult:
        text = _decode_text(data)
        return VisionResult(
            ocr_text=text[:4000] if text else None,
            provider=self.name,
            degraded=True,
        )


class AppleVisionProvider:
    """Real OCR via the ``evvision`` Swift helper (Apple Vision framework).

    Darwin only. The helper returns text, per-line confidence and normalized
    bounding boxes as JSON on stdout. A missing or broken binary raises
    :class:`VisionBinaryError`; a failed OCR pass raises
    :class:`VisionEngineError`.
    """

    name = "apple_vision"

    def __init__(self, binary: str | None = None, timeout: float = 60.0) -> None:
        self.binary = find_evvision_binary(binary)
        self.timeout = timeout

    async def analyze(
        self,
        *,
        data: bytes,
        content_type: str | None = None,
        filename: str | None = None,
        prompt: str | None = None,
    ) -> VisionResult:
        if not _is_image_content(content_type, filename):
            text = _decode_text(data)
            return VisionResult(
                ocr_text=text[:4000] if text else None,
                provider=self.name,
                degraded=True,
            )
        tmp_path = _temporary_path(data, content_type, filename)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [self.binary, "ocr", tmp_path],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise VisionBinaryError(
                f"Apple Vision helper binary {self.binary!r} not found; "
                "build it with `swift build -c release` in helpers/evvision"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise VisionEngineError(
                f"Apple Vision helper timed out after {self.timeout:.0f}s"
            ) from exc
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        if proc.returncode != 0:
            message = _error_message(proc.stdout)
            raise VisionEngineError(
                f"Apple Vision helper failed (exit {proc.returncode}): {message}"
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise VisionEngineError(
                "Apple Vision helper returned unparseable output"
            ) from exc
        text = payload.get("text")
        lines = payload.get("lines") or []
        return VisionResult(
            ocr_text=text[:4000] if text else None,
            lines=lines,
            provider=self.name,
            degraded=False,
        )


class TesseractVisionProvider:
    """Local OCR via the tesseract binary (documents/screenshots).

    A missing binary raises :class:`VisionBinaryError`; a failed run raises
    :class:`VisionEngineError`. TSV output is parsed into per-word lines with
    confidence when available.
    """

    name = "tesseract"

    def __init__(self, binary: str = "tesseract", language: str = "eng") -> None:
        self.binary = binary
        self.language = language

    async def analyze(
        self,
        *,
        data: bytes,
        content_type: str | None = None,
        filename: str | None = None,
        prompt: str | None = None,
    ) -> VisionResult:
        if not _is_image_content(content_type, filename):
            text = _decode_text(data)
            return VisionResult(
                ocr_text=text[:4000] if text else None,
                provider=self.name,
                degraded=True,
            )
        tmp_path = _temporary_path(data, content_type, filename)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [self.binary, tmp_path, "stdout", "-l", self.language, "tsv"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise VisionBinaryError(
                f"tesseract binary {self.binary!r} not found on PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise VisionEngineError("tesseract timed out after 30s") from exc
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise VisionEngineError(
                f"tesseract failed (exit {proc.returncode}): {detail[:500]}"
            )
        lines, text = _parse_tesseract_tsv(proc.stdout)
        return VisionResult(
            ocr_text=(text or proc.stdout.strip())[:4000] or None,
            lines=lines,
            provider=self.name,
            degraded=False,
        )


def _error_message(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
        error = payload.get("error") or {}
        return str(error.get("message") or "unknown error")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return (stdout or "").strip()[:500] or "unknown error"


def _parse_tesseract_tsv(output: str) -> tuple[list[dict], str | None]:
    """Parse tesseract TSV into word lines with confidence and boxes."""

    if not output.startswith("level\t"):
        return [], None
    rows: list[dict[str, str]] = []
    for line in output.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 12:
            rows.append(
                {
                    "level": parts[0],
                    "left": parts[6],
                    "top": parts[7],
                    "width": parts[8],
                    "height": parts[9],
                    "conf": parts[10],
                    "text": parts[11],
                }
            )
    page = next((r for r in rows if r["level"] == "1"), None)
    page_width = int(page["width"]) if page and page["width"].isdigit() else None
    page_height = int(page["height"]) if page and page["height"].isdigit() else None
    words = [r for r in rows if r["level"] == "5" and r["text"].strip()]
    lines: list[dict] = []
    for word in words:
        left = int(word["left"])
        top = int(word["top"])
        width = int(word["width"])
        height = int(word["height"])
        confidence = float(word["conf"]) / 100.0 if word["conf"].lstrip("-").isdigit() else 0.0
        box: dict[str, float]
        if page_width and page_height:
            box = {
                "x": left / page_width,
                "y": top / page_height,
                "width": width / page_width,
                "height": height / page_height,
            }
        else:
            box = {
                "x": float(left),
                "y": float(top),
                "width": float(width),
                "height": float(height),
            }
        lines.append(
            {
                "text": word["text"],
                "confidence": round(max(0.0, min(1.0, confidence)), 3),
                "bounding_box": box,
            }
        )
    text = " ".join(word["text"] for word in words) if words else None
    return lines, text


# Provider registry: swapping OCR backends is a configuration change
# (EV_VISION_PROVIDER), matching the chat-provider pattern so EVIE never
# couples perception to a single model or binary.
VISION_PROVIDER_REGISTRY: dict[str, Callable[[], VisionProvider]] = {
    "deterministic": DeterministicVisionProvider,
    "tesseract": lambda: TesseractVisionProvider(
        binary=getattr(settings, "vision_tesseract_binary", "tesseract")
    ),
    "apple_vision": lambda: AppleVisionProvider(),
}


def register_vision_provider(name: str, factory: Callable[[], VisionProvider]) -> None:
    """Register a vision provider factory (tests and future local providers)."""
    VISION_PROVIDER_REGISTRY[name.lower()] = factory


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def find_evvision_binary(configured: str | None = None) -> str:
    """Resolve the evvision helper binary, including the repo-local build."""

    # An explicit caller-supplied path is authoritative, even when missing, so
    # the caller can raise VisionBinaryError instead of silently swapping it.
    if configured is not None and configured != "evvision":
        return configured
    env_value = get_vision_settings().vision_evvision_binary
    if env_value != "evvision":
        return env_value
    repo_root = Path(__file__).resolve().parents[3]
    for candidate in (
        repo_root / "helpers" / "evvision" / ".build" / "release" / "evvision",
        repo_root / "helpers" / "evvision" / ".build" / "debug" / "evvision",
    ):
        if candidate.exists():
            return str(candidate)
    if shutil.which("evvision"):
        return "evvision"
    return env_value


def _default_provider_name() -> str:
    """Darwin default: Apple Vision when the helper is available."""

    if (
        sys.platform == "darwin"
        and get_vision_settings().vision_evvision_auto
        and not _running_under_pytest()
    ):
        binary = find_evvision_binary()
        if shutil.which(binary) or Path(binary).exists():
            return "apple_vision"
    return "deterministic"


def get_vision_provider() -> VisionProvider:
    """Resolve the configured local vision provider."""

    name = (getattr(settings, "vision_provider", "deterministic") or "deterministic").lower()
    if name == "deterministic":
        name = _default_provider_name()
    factory = VISION_PROVIDER_REGISTRY.get(name)
    if factory is None:
        return VISION_PROVIDER_REGISTRY["deterministic"]()
    return factory()
