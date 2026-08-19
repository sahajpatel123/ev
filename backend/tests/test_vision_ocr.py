"""Agent 6 OCR tests: Apple Vision helper, tesseract, doubles, EXIF, boundary."""

from __future__ import annotations

import json
import sys
import zlib
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ChatResult
from app.ev import vision
from app.vision import providers
from app.vision.image_utils import strip_exif_gps
from app.vision.providers import (
    AppleVisionProvider,
    DeterministicVisionProvider,
    TesseractVisionProvider,
    VisionBinaryError,
    VisionEngineError,
    get_vision_provider,
)


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return script


async def upload_attachment(
    client: AsyncClient,
    *,
    content: bytes = b"fake-image-bytes",
    content_type: str = "image/png",
    filename: str = "photo.png",
    metadata: dict | None = None,
    privacy_level: str = "normal",
) -> str:
    resp = await client.post(
        "/v1/attachments",
        files={"file": (filename, content, content_type)},
        data={
            "metadata": json.dumps(metadata or {}),
            "privacy_level": privacy_level,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["attachment"]["id"]


async def test_apple_vision_provider_parses_json(tmp_path) -> None:
    script = _write_script(
        tmp_path,
        "evvision",
        r"""
cat <<'EOF'
{"provider":"apple_vision","text":"INVOICE EV-42","lines":[{"text":"INVOICE EV-42","confidence":0.97,"bounding_box":{"x":0.1,"y":0.2,"width":0.5,"height":0.1}}],"page_count":1}
EOF
""",
    )
    provider = AppleVisionProvider(binary=str(script))
    result = await provider.analyze(
        data=b"png-bytes",
        content_type="image/png",
        filename="scan.png",
    )
    assert result.provider == "apple_vision"
    assert result.ocr_text == "INVOICE EV-42"
    assert result.lines[0]["confidence"] == 0.97
    assert result.lines[0]["bounding_box"]["x"] == 0.1
    assert result.degraded is False


async def test_apple_vision_missing_binary_raises_typed_error(tmp_path) -> None:
    provider = AppleVisionProvider(binary=str(tmp_path / "does-not-exist"))
    with pytest.raises(VisionBinaryError, match="not found"):
        await provider.analyze(data=b"x", content_type="image/png")


async def test_apple_vision_engine_error_raises_typed_error(tmp_path) -> None:
    script = _write_script(
        tmp_path,
        "evvision",
        r"""
echo '{"error":{"code":"invalid_input","message":"cannot decode"}}'
exit 2
""",
    )
    provider = AppleVisionProvider(binary=str(script))
    with pytest.raises(VisionEngineError, match="cannot decode"):
        await provider.analyze(data=b"x", content_type="image/png")


async def test_tesseract_missing_binary_raises_typed_error(tmp_path) -> None:
    provider = TesseractVisionProvider(binary=str(tmp_path / "no-tesseract"))
    with pytest.raises(VisionBinaryError, match="not found"):
        await provider.analyze(data=b"x", content_type="image/png")


async def test_tesseract_engine_error_raises_typed_error(tmp_path) -> None:
    script = _write_script(tmp_path, "tesseract", 'echo "boom" >&2\nexit 1')
    provider = TesseractVisionProvider(binary=str(script))
    with pytest.raises(VisionEngineError, match="boom"):
        await provider.analyze(data=b"x", content_type="image/png")


async def test_tesseract_tsv_parsing_fills_lines(tmp_path) -> None:
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "1\t0\t0\t0\t0\t0\t0\t0\t612\t100\t-1\t",
            "5\t0\t1\t1\t1\t1\t34\t30\t120\t38\t92\tINVOICE",
            "5\t0\t1\t1\t1\t2\t154\t30\t80\t38\t88\tEV-42",
            "",
        ]
    )
    script = _write_script(
        tmp_path,
        "tesseract",
        f"printf '%s\\n' '{tsv}'",
    )
    provider = TesseractVisionProvider(binary=str(script))
    result = await provider.analyze(
        data=b"binary-image-bytes",
        content_type="image/png",
        filename="scan.png",
    )
    assert result.provider == "tesseract"
    assert result.ocr_text == "INVOICE EV-42"
    assert len(result.lines) == 2
    assert result.lines[0]["text"] == "INVOICE"
    assert result.lines[0]["confidence"] == 0.92
    assert result.lines[0]["bounding_box"]["width"] == pytest.approx(120 / 612)
    assert result.degraded is False


async def test_tesseract_plain_output_still_works(tmp_path) -> None:
    script = _write_script(tmp_path, "tesseract", "printf 'OCR: HELLO EV'")
    provider = TesseractVisionProvider(binary=str(script))
    result = await provider.analyze(
        data=b"binary-image-bytes",
        content_type="image/png",
        filename="scan.png",
    )
    assert result.ocr_text == "OCR: HELLO EV"


async def test_deterministic_double_is_degraded() -> None:
    result = await DeterministicVisionProvider().analyze(
        data=b"INVOICE EV-42\nTOTAL 100 USD",
        content_type="image/png",
        filename="scan.png",
    )
    assert result.provider == "deterministic"
    assert "INVOICE" in (result.ocr_text or "")
    assert result.degraded is True


def test_darwin_default_resolves_apple_vision(monkeypatch) -> None:
    class FakeVisionSettings:
        vision_evvision_auto = True
        vision_evvision_binary = "evvision"

    monkeypatch.setattr(providers, "get_vision_settings", lambda: FakeVisionSettings())
    monkeypatch.setattr(providers, "_running_under_pytest", lambda: False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(providers.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}")
    assert get_vision_provider().name == "apple_vision"

    monkeypatch.setattr(providers, "_running_under_pytest", lambda: True)
    assert get_vision_provider().name == "deterministic"


def test_non_darwin_default_stays_deterministic(monkeypatch) -> None:
    class FakeVisionSettings:
        vision_evvision_auto = True
        vision_evvision_binary = "evvision"

    monkeypatch.setattr(providers, "get_vision_settings", lambda: FakeVisionSettings())
    monkeypatch.setattr(providers, "_running_under_pytest", lambda: False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert get_vision_provider().name == "deterministic"


def test_strip_exif_removes_jpeg_gps_segment() -> None:
    app1_data = b"Exif\x00\x00" + b"GPSLatitudeRef" + b"\x00" * 32
    app1 = b"\xff\xe1" + (len(app1_data) + 2).to_bytes(2, "big") + app1_data
    sos = b"\xff\xda" + b"\x00\x08" + b"\x01\x01\x00\x00\x3f\x00"
    jpeg = b"\xff\xd8" + app1 + sos + b"\x12\x34\x56" + b"\xff\xd9"

    cleaned = strip_exif_gps(jpeg)
    assert b"GPSLatitudeRef" not in cleaned
    assert cleaned.count(b"\xff\xe1") == 0
    assert cleaned.startswith(b"\xff\xd8")
    assert cleaned.endswith(b"\xff\xd9")


def test_strip_exif_removes_png_exif_chunk() -> None:
    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big")
            + chunk_type
            + data
            + (zlib.crc32(chunk_type + data) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00")
        + chunk(b"eXIf", b"GPSDATA")
        + chunk(b"IEND", b"")
    )
    cleaned = strip_exif_gps(png)
    assert b"eXIf" not in cleaned
    assert cleaned.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IEND" in cleaned


class SpyProvider:
    """Records every chat call; used to prove the boundary holds."""

    name = "spy-vision"
    supports_media = True

    def __init__(self) -> None:
        self.seen_messages = []

    async def chat(self, messages, *, model=None, temperature=0.7) -> ChatResult:
        self.seen_messages.extend(messages)
        return ChatResult(text="SUMMARY: should never be called", usage={}, model="spy")

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7) -> ChatResult:
        return await self.chat(messages, model=model, temperature=temperature)

    async def list_models(self) -> list[str]:
        return ["spy"]


async def test_never_send_to_model_media_never_reaches_provider(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    attachment_id = await upload_attachment(
        client,
        content=b"NEVER SEND THIS",
        content_type="image/png",
        privacy_level="never_send_to_model",
    )
    provider = SpyProvider()
    row = await vision.analyze_attachment(
        db_session,
        UUID(attachment_id),
        actor="master",
        permission=True,
        allow_raw=True,
        provider=provider,
    )
    await db_session.commit()

    payload = row.payload
    assert payload["raw_sent"] is False
    assert "blocked" in payload["summary"].lower()
    assert provider.seen_messages == []
    assert (await client.get("/v1/vision/log")).json() == []
    assert payload["local_degraded"] is True
    assert payload["face_detections"][0]["count"] == 0


async def test_deepseek_ocr_provider_parses_hosted_json(monkeypatch) -> None:
    from app.vision.providers import DeepSeekOCRProvider

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "text": "MENU PAD THAI 12",
                "lines": [{"text": "MENU", "confidence": 0.9}],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            assert "localhost" in url
            assert json["prompt"]
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    provider = DeepSeekOCRProvider(endpoint="http://localhost:8090/ocr", timeout=1)
    result = await provider.analyze(data=b"png-bytes", content_type="image/png")
    assert result.provider == "deepseek_ocr"
    assert result.ocr_text == "MENU PAD THAI 12"
    assert result.lines[0]["text"] == "MENU"
    assert result.degraded is False


async def test_deepseek_ocr_refuses_official_chat_api() -> None:
    from app.vision.providers import DeepSeekOCRProvider, VisionEngineError

    provider = DeepSeekOCRProvider(endpoint="https://api.deepseek.com/chat/completions")
    with pytest.raises(VisionEngineError, match="text-only"):
        await provider.analyze(data=b"x", content_type="image/png")

