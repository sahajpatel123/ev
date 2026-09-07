"""Always-on hands-free client: URL building, WAV pacing, CLI flags."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from clients.hands_free.main import ClientConfig, WavSource, _parse_args, main


@pytest.fixture(autouse=True)
def fresh_db():
    yield


def _mono_wav(path: Path, *, frames: int = 320, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)


def test_ws_url_rewrites_http_and_https_to_the_live_path() -> None:
    http = ClientConfig(api_url="http://127.0.0.1:8000", api_key="k")
    https = ClientConfig(api_url="https://ev.example/api/", api_key="k")
    assert http.ws_url == "ws://127.0.0.1:8000/v1/voice/hands-free"
    assert https.ws_url == "wss://ev.example/api/v1/voice/hands-free"
    assert http.frame_samples == 320


def test_cli_requires_an_api_key(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    monkeypatch.delenv("EV_API_KEY", raising=False)
    monkeypatch.delenv("EV_MASTER_KEY", raising=False)
    assert main([]) == 2
    assert "no API key" in capsys.readouterr().err


def test_cli_accepts_the_master_key_from_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("EV_API_KEY", raising=False)
    monkeypatch.setenv("EV_MASTER_KEY", "from-env")
    args = _parse_args(["--simulate-wav", "x.wav", "--no-audio", "--duration", "0.1"])
    assert args.api_key == "from-env"


async def test_wav_source_paces_frames_then_emits_silence(tmp_path: Path) -> None:
    path = tmp_path / "clip.wav"
    _mono_wav(path, frames=640)
    source = WavSource(
        ClientConfig(api_url="http://127.0.0.1:8000", api_key="k", frame_ms=20),
        str(path),
    )
    source.open()
    try:
        frames = []
        agen = source.frames()
        frames.append(await agen.__anext__())
        frames.append(await agen.__anext__())
        frames.append(await agen.__anext__())
    finally:
        source.close()
    assert [len(frame) for frame in frames] == [640, 640, 640]
    # The file only had two frames; the third is silence so follow-up windows
    # still play out after the clip ends.
    assert frames[2] == b"\x00" * 640


def test_wav_source_rejects_the_wrong_format(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 4)
    source = WavSource(ClientConfig(api_url="http://x", api_key="k"), str(path))
    with pytest.raises(RuntimeError, match="mono 16-bit"):
        source.open()
    assert source._wav is None
