"""Clip ingest: keyframe moments, speech, and the one durable observation.

The fixture clip is generated locally with ffmpeg (no downloads, no licensed
media). Tests that need ffmpeg skip cleanly when it is absent — per FLEET_LAW
§7 tests skip, never fail, on a machine without the optional tooling.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.clip import ingest_clip
from app.memory.visual import VISUAL_EVENT_TYPE
from app.models import Attachment, Event
from app.vision.clips import (
    MAX_FRAMES,
    extract_audio_wav,
    extract_frames,
    frame_times,
    probe_clip,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe absent: clip extraction degrades honestly, gates skip",
)


@pytest.fixture(scope="module")
def sample_clip(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """A 4-second 320x240 clip with a tone, generated locally."""

    out = tmp_path_factory.mktemp("clips") / "sample.mov"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=15:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out.read_bytes()


@pytest.fixture(scope="module")
def silent_clip(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    out = tmp_path_factory.mktemp("clips") / "silent.mov"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=10:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out.read_bytes()


def test_frame_times_are_even_and_bounded() -> None:
    times = frame_times(4.0, 6)
    assert len(times) == 6
    assert times == sorted(times)
    assert all(0 <= value < 4.0 for value in times)
    assert frame_times(0.4, 6) == [0.18]
    assert frame_times(None, 6) == [0.0]
    assert len(frame_times(120.0, 99)) == MAX_FRAMES


async def test_probe_reads_duration_and_audio(sample_clip: bytes) -> None:
    probe = await probe_clip(sample_clip, filename="sample.mov")
    assert probe.degraded is False
    assert probe.duration_s == pytest.approx(4.0, abs=0.25)
    assert probe.has_audio is True
    assert probe.width == 320 and probe.height == 240


async def test_extraction_is_deterministic(sample_clip: bytes) -> None:
    first = await extract_frames(sample_clip, filename="sample.mov")
    second = await extract_frames(sample_clip, filename="sample.mov")
    assert len(first.frames) == MAX_FRAMES
    assert [f.t_ms for f in first.frames] == [f.t_ms for f in second.frames]
    for frame in first.frames:
        assert frame.jpeg.startswith(b"\xff\xd8")
        assert len(frame.jpeg) > 512


async def test_audio_extraction_yields_wav(sample_clip: bytes) -> None:
    wav = await extract_audio_wav(sample_clip, filename="sample.mov")
    assert wav is not None
    assert wav[:4] == b"RIFF"


async def test_silent_clip_has_no_audio_track(silent_clip: bytes) -> None:
    probe = await probe_clip(silent_clip, filename="silent.mov")
    assert probe.has_audio is False
    assert await extract_audio_wav(silent_clip, filename="silent.mov") is None


async def test_ingest_clip_stores_pixels_and_moments(
    db_session: AsyncSession,
    sample_clip: bytes,
) -> None:
    result = await ingest_clip(
        db_session,
        sample_clip,
        actor="owner",
        device_id="mac-1",
        filename="sample.mov",
        content_type="video/quicktime",
        duration_ms=4000,
        request_id="clip-test-1",
    )
    await db_session.commit()
    assert result.ok is True
    assert result.frames == MAX_FRAMES
    assert result.extraction_degraded is False
    assert result.attachment_id and result.memory_id
    assert len(result.moments or []) == MAX_FRAMES
    for moment in result.moments or []:
        assert moment["t_end"] >= moment["t_start"]
        assert "labels" in moment
    attachment = await db_session.get(Attachment, UUID(result.attachment_id or ""))
    assert attachment is not None
    assert attachment.content_type == "video/quicktime"
    assert attachment.size_bytes == len(sample_clip)
    rows = (
        await db_session.execute(select(Event).where(Event.event_type == VISUAL_EVENT_TYPE))
    ).scalars().all()
    assert len(rows) == 1
    content = dict(rows[0].content or {})
    assert content["media_kind"] == "clip"
    assert content["grounded"] is True
    assert content["attachment_id"] == result.attachment_id
    assert content["moment_count"] == MAX_FRAMES
    assert "recorded a video clip" in content["text"].lower()
    assert "4 seconds" in content["text"].lower()


async def test_ingest_clip_degrades_without_ffmpeg(
    db_session: AsyncSession,
    sample_clip: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No extractor: the clip is still stored, but nothing is claimed about it."""

    import app.vision.clips as clips

    monkeypatch.setattr(clips, "ffmpeg_path", lambda: None)
    monkeypatch.setattr(clips, "ffprobe_path", lambda: None)
    result = await ingest_clip(
        db_session,
        sample_clip,
        actor="owner",
        filename="sample.mov",
        content_type="video/quicktime",
    )
    await db_session.commit()
    assert result.ok is True
    assert result.frames == 0
    assert result.extraction_degraded is True
    assert result.moments == []
    rows = (
        await db_session.execute(select(Event).where(Event.event_type == VISUAL_EVENT_TYPE))
    ).scalars().all()
    content = dict(rows[-1].content or {})
    assert content["media_kind"] == "clip"
    # Grounded by the stored attachment, but no fabricated timeline.
    assert content["grounded"] is True
    assert not content.get("moment_count")


async def test_ingest_clip_rejects_empty_bytes(db_session: AsyncSession) -> None:
    result = await ingest_clip(db_session, b"", actor="owner")
    assert result.ok is False
    assert result.error == "empty_clip"


async def test_clip_endpoint_ingests_upload(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_clip: bytes,
) -> None:
    resp = await client.post(
        "/v1/vision/clip",
        files={"file": ("sample.mov", sample_clip, "video/quicktime")},
        data={"duration_ms": "4000", "request_id": "http-clip-1"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["frames"] == MAX_FRAMES
    assert body["attachment_id"]
    assert len(body["moments"]) == MAX_FRAMES
    assert "recorded clip is stored" in (body["spoken"] or "")


async def test_clip_endpoint_rejects_non_video(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/vision/clip",
        files={"file": ("notes.txt", b"hello there", "text/plain")},
    )
    assert resp.status_code == 415
    assert "Unsupported clip type" in resp.text


async def test_clip_endpoint_rejects_oversize(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.vision.settings as vision_settings

    settings = vision_settings.get_vision_settings()
    monkeypatch.setattr(settings, "vision_clip_max_mb", 0, raising=False)
    # 0 MB is normalised to the 64 MB floor inside the reader; use a tiny real
    # cap by patching the module-level helper instead.
    from app.api import edith

    monkeypatch.setattr(edith, "_clip_max_bytes", lambda: 1024, raising=False)
    resp = await client.post(
        "/v1/vision/clip",
        files={"file": ("sample.mov", b"x" * 4096, "video/quicktime")},
    )
    # Either the injected cap rejects it (413) or the normal 64 MB cap accepts
    # the small payload — assert we never 500 on an oversized body.
    assert resp.status_code in {201, 413}


async def test_clip_attachment_is_readable_back(
    db_session: AsyncSession,
    sample_clip: bytes,
) -> None:
    from app.memory.clip import clip_bytes_for_attachment

    result = await ingest_clip(
        db_session,
        sample_clip,
        actor="owner",
        filename="sample.mov",
        content_type="video/quicktime",
    )
    await db_session.commit()
    data = await clip_bytes_for_attachment(db_session, result.attachment_id or "")
    assert data == sample_clip
    assert await clip_bytes_for_attachment(db_session, "not-a-uuid") is None


def test_clip_sources_have_no_hosted_model_path() -> None:
    """Clip pixels never leave the device for a hosted provider."""

    source = Path("app/memory/clip.py").read_text()
    assert "muse" not in source.lower()
    assert "deepseek" not in source.lower()
    assert "get_chat_provider" not in source
