"""Mobile Voice Core: Mac golden fingerprint vs iPhone conversational contract.

Does not change Mac live transport. No Memory OS. Diagnostic transcripts are
session-scoped and never ingested.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.config import settings
from app.voice.live.grok_voice import grok_session_update

MOBILE_ASR_LEXICON = (
    "Evie, Wi-Fi, Spotify, MacBook, Calculator, Safari, Notes, Tailscale, "
    "open, close, stop, don't."
)

MOBILE_CONVERSATION_CONTRACT = (
    "MOBILE CONVERSATION CONTRACT: Answer ordinary questions in spoken words. "
    "Do not call tools for definitions, trivia, math, or 'what did I just ask'. "
    "Wi-Fi means wireless networking. Spotify is a music app. They are different. "
    "If a sentence is clearly a hearing test (for example it contains "
    "'after I finish this sentence' or 'my test phrase is'), repeat or explain; "
    "do not change any device setting. If you did not hear clearly, say so and "
    "ask them to repeat. Do not guess a similar-sounding app."
)

EVAL_PHRASES = (
    "Turn off the Wi-Fi after I finish this sentence.",
    "Tell me what Wi-Fi is.",
    "Open Spotify.",
    "Do not open Spotify; I'm asking about Wi-Fi.",
    "What is the capital of France?",
    "Tell me one fact about Saturn.",
    "What's 2 plus 2?",
    "Explain what Wi-Fi means.",
    "What did I just ask you?",
    "Say exactly: Violet seven four nine.",
    "My test phrase is Amber Three Eight Two.",
    "Open Calculator on my Mac.",
    "Don't open Calculator.",
    "Look at this.",
    "Evie, are you listening?",
    "MacBook versus Mac.",
    "fifteen not fifty.",
    "first not third.",
    "Stop talking.",
    "Never turn off Wi-Fi.",
)


def _hash_text(value: str) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()[:16]


def mac_voice_golden_fingerprint() -> dict[str, Any]:
    """Normalized Mac OpenAI Realtime contract. Transport stays frozen PCM/WS."""

    payload = grok_session_update(provider="openai", function_tools=[])
    session = payload.get("session") if isinstance(payload, dict) else {}
    audio = session.get("audio") if isinstance(session, dict) else {}
    inp = audio.get("input") if isinstance(audio, dict) else {}
    out = audio.get("output") if isinstance(audio, dict) else {}
    vad = inp.get("turn_detection") if isinstance(inp, dict) else {}
    tx = inp.get("transcription") if isinstance(inp, dict) else {}
    instructions = str(session.get("instructions") or "")
    return {
        "endpoint": "mac",
        "transport": "openai_realtime_websocket_pcm",
        "model": session.get("model"),
        "voice": out.get("voice"),
        "instructions_hash": _hash_text(instructions),
        "output_modalities": session.get("output_modalities"),
        "input_format": inp.get("format"),
        "output_format": out.get("format"),
        "noise_reduction": inp.get("noise_reduction"),
        "transcription_model": tx.get("model") if isinstance(tx, dict) else None,
        "transcription_language": tx.get("language") if isinstance(tx, dict) else None,
        "transcription_prompt": bool(tx.get("prompt")) if isinstance(tx, dict) else False,
        "turn_detection": vad.get("type") if isinstance(vad, dict) else None,
        "create_response": vad.get("create_response") if isinstance(vad, dict) else None,
        "interrupt_response": vad.get("interrupt_response") if isinstance(vad, dict) else None,
        "vad_threshold": vad.get("threshold") if isinstance(vad, dict) else None,
        "silence_duration_ms": vad.get("silence_duration_ms") if isinstance(vad, dict) else None,
        "prefix_padding_ms": vad.get("prefix_padding_ms") if isinstance(vad, dict) else None,
        "tool_choice": session.get("tool_choice"),
        "frozen": True,
    }


def iphone_voice_fingerprint(session: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.device_gateway.webrtc_live import phone_webrtc_session

    sess = session or phone_webrtc_session()
    audio = sess.get("audio") if isinstance(sess, dict) else {}
    inp = audio.get("input") if isinstance(audio, dict) else {}
    out = audio.get("output") if isinstance(audio, dict) else {}
    vad = inp.get("turn_detection") if isinstance(inp, dict) else {}
    tx = inp.get("transcription") if isinstance(inp, dict) else {}
    nr = inp.get("noise_reduction") if isinstance(inp, dict) else None
    instructions = str(sess.get("instructions") or "")
    return {
        "endpoint": "iphone",
        "transport": "openai_realtime_webrtc",
        "model": sess.get("model"),
        "voice": out.get("voice"),
        "instructions_hash": _hash_text(instructions),
        "output_modalities": sess.get("output_modalities"),
        "input_format": inp.get("format"),
        "output_format": out.get("format"),
        "noise_reduction": nr,
        "transcription_model": tx.get("model") if isinstance(tx, dict) else None,
        "transcription_language": tx.get("language") if isinstance(tx, dict) else None,
        "transcription_prompt": bool(tx.get("prompt")) if isinstance(tx, dict) else False,
        "turn_detection": vad.get("type") if isinstance(vad, dict) else None,
        "create_response": vad.get("create_response") if isinstance(vad, dict) else None,
        "interrupt_response": vad.get("interrupt_response") if isinstance(vad, dict) else None,
        "vad_threshold": vad.get("threshold") if isinstance(vad, dict) else None,
        "silence_duration_ms": vad.get("silence_duration_ms") if isinstance(vad, dict) else None,
        "prefix_padding_ms": vad.get("prefix_padding_ms") if isinstance(vad, dict) else None,
        "tool_choice": sess.get("tool_choice"),
        "audio_backend": getattr(settings, "phone_audio_backend", "webrtc_strict"),
        "mobile_contract": MOBILE_CONVERSATION_CONTRACT in instructions,
    }


def config_diff(mac: dict[str, Any], phone: dict[str, Any]) -> list[dict[str, Any]]:
    keys = [
        "model",
        "voice",
        "turn_detection",
        "create_response",
        "interrupt_response",
        "vad_threshold",
        "silence_duration_ms",
        "prefix_padding_ms",
        "transcription_model",
        "transcription_language",
        "transcription_prompt",
        "noise_reduction",
        "output_modalities",
    ]
    rows = []
    for key in keys:
        left = mac.get(key)
        right = phone.get(key)
        rows.append({"field": key, "mac": left, "iphone": right, "match": left == right})
    rows.append(
        {
            "field": "transport",
            "mac": mac.get("transport"),
            "iphone": phone.get("transport"),
            "match": False,
            "note": "behavioral parity, not transport parity",
        }
    )
    rows.append(
        {
            "field": "instructions_hash",
            "mac": mac.get("instructions_hash"),
            "iphone": phone.get("instructions_hash"),
            "match": mac.get("instructions_hash") == phone.get("instructions_hash"),
            "note": "phone adds MOBILE CONVERSATION CONTRACT; Mac frozen",
        }
    )
    return rows


def fingerprint_report() -> dict[str, Any]:
    mac = mac_voice_golden_fingerprint()
    phone = iphone_voice_fingerprint()
    return {
        "mac": mac,
        "iphone": phone,
        "diff": config_diff(mac, phone),
        "eval_phrases": list(EVAL_PHRASES),
        "asr_lexicon": MOBILE_ASR_LEXICON,
    }


def critical_tokens(text: str) -> list[str]:
    lowered = (text or "").lower()
    wanted = (
        "evie",
        "wi-fi",
        "wifi",
        "spotify",
        "macbook",
        "calculator",
        "safari",
        "don't",
        "do not",
        "stop",
        "open",
        "close",
    )
    return [token for token in wanted if token in lowered]


def logprob_confidence(logprobs: list | None) -> float | None:
    if not isinstance(logprobs, list) or not logprobs:
        return None
    scores = []
    for item in logprobs:
        if isinstance(item, dict) and isinstance(item.get("logprob"), (int, float)):
            scores.append(float(item["logprob"]))
        elif isinstance(item, (int, float)):
            scores.append(float(item))
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    return round(max(0.0, min(1.0, 1.0 + avg / 5.0)), 3)


_DIAG: dict[str, dict[str, Any]] = {}
_DIAG_TTL_S = 900
MAX_ORACLE_BYTES = 1_200_000


def remember_diag(device_id: str, payload: dict[str, Any]) -> None:
    import time

    _DIAG[str(device_id)] = {**payload, "expires_at": time.time() + _DIAG_TTL_S}


def take_diag(device_id: str) -> dict[str, Any] | None:
    import time

    row = _DIAG.pop(str(device_id), None)
    if row is None:
        return None
    if float(row.get("expires_at") or 0) < time.time():
        return None
    return row


async def transcribe_oracle(*, audio: bytes, mime: str, language: str = "en") -> dict[str, Any]:
    """Independent ASR for diagnostic audio. Bytes are not stored."""

    import httpx

    key = (settings.openai_api_key or "").strip()
    if not key:
        raise RuntimeError("openai_missing")
    if not audio or len(audio) > MAX_ORACLE_BYTES:
        raise ValueError("audio_too_large")
    ext = "m4a"
    if "webm" in mime:
        ext = "webm"
    elif "wav" in mime:
        ext = "wav"
    elif "mpeg" in mime or "mp3" in mime:
        ext = "mp3"
    filename = f"diag.{ext}"
    model = (getattr(settings, "phone_asr_model", None) or "gpt-4o-transcribe").strip()
    files = {"file": (filename, audio, mime or "application/octet-stream")}
    data = {
        "model": model,
        "language": language or "en",
        "prompt": MOBILE_ASR_LEXICON,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files=files,
            data=data,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"asr_failed:{response.status_code}")
    payload = response.json()
    text = str(payload.get("text") or "").strip()
    return {
        "transcript": text,
        "model": model,
        "language": language,
        "critical_tokens": critical_tokens(text),
        "stored": False,
    }
