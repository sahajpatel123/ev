"""Hosted voice latency probe: TTS first byte + ASR first partial/final.

Times the OpenAI-compatible production path over a real network. Run after
setting a valid hosted credential::

    EV_VOICE_ASR_PROVIDER=openai_compat EV_VOICE_ASR_BASE_URL=... \
    EV_VOICE_ASR_API_KEY=... EV_VOICE_ASR_MODEL=whisper-1 \
    EV_VOICE_TTS_PROVIDER=openai_compat EV_VOICE_TTS_BASE_URL=... \
    EV_VOICE_TTS_API_KEY=... EV_ALLOW_REMOTE_TTS=true \
    EV_ALLOW_REMOTE_ASR=true \
    cd backend && uv run python -m eval.ml.voice_latency_probe --audio <file>

Writes ``backend/eval/ml/voice_latency.json``. On auth/network failure the
artifact records ``measured: false`` with the exact reason; it never invents a
latency.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.compliance.policy import remote_processing_allowed
from app.config import settings
from app.voice.contracts import Transcript, TranscriptPartial

OUT_PATH = Path(__file__).resolve().parent / "voice_latency.json"
DEFAULT_AUDIO = (
    Path.home()
    / ".ev"
    / "datasets"
    / "librispeech"
    / "LibriSpeech"
    / "test-clean"
    / "1089"
    / "134686"
    / "1089-134686-0000.flac"
)


def _tts_payload(text: str) -> dict:
    payload: dict = {
        "model": settings.voice_tts_model,
        "voice": settings.voice_tts_voice,
        "input": text,
        "response_format": settings.voice_tts_format,
    }
    if "tts" in settings.voice_tts_model.lower():
        payload["speed"] = 1.0
        payload["instructions"] = "Speak in a steady, measured register."
    return payload


async def _probe_tts_first_byte() -> dict:
    if settings.voice_tts_provider != "openai_compat":
        return {
            "ok": False,
            "reason": (
                f"voice_tts_provider={settings.voice_tts_provider!r}; "
                "set EV_VOICE_TTS_PROVIDER=openai_compat"
            ),
        }
    if not settings.voice_tts_base_url:
        return {"ok": False, "reason": "EV_VOICE_TTS_BASE_URL is not set"}
    if not remote_processing_allowed("voice_tts"):
        return {
            "ok": False,
            "reason": "remote TTS denied by policy; set EV_ALLOW_REMOTE_TTS=true",
        }
    headers = (
        {"Authorization": f"Bearer {settings.voice_tts_api_key}"}
        if settings.voice_tts_api_key
        else {}
    )
    started = time.perf_counter()
    try:
        async with (
            httpx.AsyncClient(timeout=60) as client,
            client.stream(
                "POST",
                f"{settings.voice_tts_base_url.rstrip('/')}/audio/speech",
                headers=headers,
                json=_tts_payload("Hello from the EV companion."),
            ) as response,
        ):
            headers_ms = (time.perf_counter() - started) * 1000
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                body = (await response.aread()).decode("utf-8", "replace")[:200]
                return {
                    "ok": False,
                    "reason": f"remote-http-{exc.response.status_code}",
                    "detail": body,
                    "headers_ms": round(headers_ms, 1),
                }
            first_byte_ms = None
            async for chunk in response.aiter_bytes():
                if chunk:
                    first_byte_ms = (time.perf_counter() - started) * 1000
                    break
            return {
                "ok": True,
                "first_byte_ms": (
                    round(first_byte_ms, 1) if first_byte_ms is not None else None
                ),
                "headers_ms": round(headers_ms, 1),
            }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "reason": type(exc).__name__,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }


async def _probe_asr(audio_path: Path) -> dict:
    from app.voice.asr import get_transcriber

    transcriber = get_transcriber()
    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    started = time.perf_counter()
    first_partial_ms: float | None = None
    final: Transcript | None = None
    try:
        async for item in transcriber.stream(audio_b64=audio_b64, language="en"):
            if isinstance(item, TranscriptPartial) and first_partial_ms is None:
                first_partial_ms = (time.perf_counter() - started) * 1000
            if isinstance(item, Transcript):
                final = item
        if final is None:
            final = await transcriber.transcribe(audio_b64=audio_b64, language="en")
    except Exception as exc:  # noqa: BLE001 - record the failure honestly
        return {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    final_ms = (time.perf_counter() - started) * 1000
    return {
        "ok": not final.degraded,
        "provider": transcriber.name,
        "first_partial_ms": (
            round(first_partial_ms, 1) if first_partial_ms is not None else None
        ),
        "final_ms": round(final_ms, 1),
        "degraded": final.degraded,
        "reason": (
            (final.details or {}).get("reason")
            if final.degraded
            else None
        ),
    }


async def _run(audio_path: Path) -> dict:
    tts = await _probe_tts_first_byte()
    asr = await _probe_asr(audio_path)
    measured = bool(tts.get("ok") and asr.get("ok"))
    return {
        "schema": "ev.voice.latency.v1",
        "measured": measured,
        "degraded": not measured,
        "asr": asr,
        "tts_first_byte": tts,
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voice-latency-probe", description=__doc__)
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args(argv)
    if not args.audio.is_file():
        print(f"audio file not found: {args.audio}", file=sys.stderr)
        return 2
    payload = asyncio.run(_run(args.audio))
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if payload["measured"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
