"""Mobile Voice Core: Mac golden fingerprint vs iPhone conversational contract.

Does not change Mac live transport. No Memory OS. Diagnostic transcripts are
session-scoped and never ingested.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.config import settings
from app.voice.live.gemini_live import gemini_live_setup

MOBILE_ASR_LEXICON = (
    "Evie, Wi-Fi, Spotify, MacBook, Calculator, Safari, Notes, Tailscale, "
    "open, close, stop, don't."
)

MOBILE_CONVERSATION_CONTRACT = (
    "MOBILE CONVERSATION CONTRACT: One spoken answer per owner utterance. "
    "Never start a new reply unless they just said something new. Never invent "
    "a second acknowledgement after you already answered (do not follow "
    "'yes I can hear you' with 'yes I got you' on silence or echo). "
    "Do not speak on leftover playback, room echo, or an empty turn. "
    "Answer definitions, trivia, math, and 'what did I just ask' in words "
    "without tools. "
    "Personal facts and live state are not trivia: you MUST call "
    "evie_state_query with their exact words before answering their name, "
    "who they are, weather, date, time, calendar, inbox, contacts, memory, "
    "projects, or what you can do, then speak ONLY that result. Never say "
    "you don't know those things without calling the tool. Never invent a "
    "forecast or a name. "
    "Timers, reminders, opening Calculator or other Mac apps, mail, and "
    "calendar actions also go through evie_state_query or evie_home_action — "
    "Home Station executes them. Do not refuse those as iPhone-only. "
    "Wi-Fi means wireless networking. Spotify is a music app. They are different. "
    "If a sentence is clearly a hearing test (for example it contains "
    "'after I finish this sentence' or 'my test phrase is'), repeat or explain; "
    "do not change any device setting. If you did not hear clearly, say so and "
    "ask them to repeat. Do not guess a similar-sounding app."
)

PHONE_SPEECH_COPROCESSOR_CONTRACT = (
    "You are a voice coprocessor, not Evie's mind. MiMo "
    "decides every answer. You transcribe speech and, when given a Core answer, "
    "speak that text once verbatim. Do not answer owner questions yourself. "
    "Do not plan. Do not call tools. Do not add facts. Do not paraphrase meaning. "
    "Do not greet. Do not acknowledge. Never create an independent spoken reply. "
    "HealthKit is never sent to a model."
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


def _setup_fingerprint(setup: dict[str, Any], *, endpoint: str, transport: str) -> dict[str, Any]:
    """Comparable voice-session surface from a Gemini Live setup payload."""

    generation = setup.get("generationConfig")
    generation = generation if isinstance(generation, dict) else {}
    speech = generation.get("speechConfig")
    speech = speech if isinstance(speech, dict) else {}
    voice_cfg = speech.get("voiceConfig")
    voice_cfg = voice_cfg if isinstance(voice_cfg, dict) else {}
    prebuilt = voice_cfg.get("prebuiltVoiceConfig")
    prebuilt = prebuilt if isinstance(prebuilt, dict) else {}
    system = setup.get("systemInstruction")
    system = system if isinstance(system, dict) else {}
    parts = system.get("parts")
    parts = parts if isinstance(parts, list) else []
    instructions = "".join(
        part.get("text", "") for part in parts if isinstance(part, dict)
    )
    tools: list[str] = []
    blocks = setup.get("tools")
    blocks = blocks if isinstance(blocks, list) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        declarations = block.get("functionDeclarations")
        declarations = declarations if isinstance(declarations, list) else []
        for declaration in declarations:
            if isinstance(declaration, dict) and declaration.get("name"):
                tools.append(str(declaration["name"]))
    realtime_cfg = setup.get("realtimeInputConfig")
    realtime_cfg = realtime_cfg if isinstance(realtime_cfg, dict) else {}
    auto_vad = realtime_cfg.get("automaticActivityDetection")
    auto_vad = auto_vad if isinstance(auto_vad, dict) else {}
    return {
        "endpoint": endpoint,
        "transport": transport,
        "model": setup.get("model"),
        "voice": prebuilt.get("voiceName"),
        "instructions_hash": _hash_text(instructions),
        "response_modalities": generation.get("responseModalities"),
        "input_transcription": "inputAudioTranscription" in setup,
        "output_transcription": "outputAudioTranscription" in setup,
        "manual_vad": bool(auto_vad.get("disabled", False)),
        "compression": "contextWindowCompression" in setup,
        "resumption": "sessionResumption" in setup,
        "thinking": generation.get("thinkingConfig"),
        "tools": sorted(set(tools)),
        "frozen": True,
    }


def mac_voice_golden_fingerprint() -> dict[str, Any]:
    """Normalized Mac Gemini Live contract. Transport stays frozen PCM/WS."""

    payload = gemini_live_setup(function_tools=[])
    setup_raw = payload.get("setup") if isinstance(payload, dict) else None
    setup: dict[str, Any] = setup_raw if isinstance(setup_raw, dict) else {}
    return _setup_fingerprint(
        setup, endpoint="mac", transport="gemini_live_websocket_pcm"
    )


def iphone_voice_fingerprint(message: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.device_gateway.webrtc_live import phone_webrtc_session

    msg = message or phone_webrtc_session()
    msg_dict: dict[str, Any] = msg if isinstance(msg, dict) else {}
    setup_raw = msg_dict.get("setup", msg_dict)
    setup: dict[str, Any] = setup_raw if isinstance(setup_raw, dict) else {}
    finger = _setup_fingerprint(
        setup, endpoint="iphone", transport="gemini_live_server_bridge_pcm_ws"
    )
    system = setup.get("systemInstruction")
    system = system if isinstance(system, dict) else {}
    parts = system.get("parts")
    parts = parts if isinstance(parts, list) else []
    instructions = "".join(
        part.get("text", "") for part in parts if isinstance(part, dict)
    )
    finger["audio_backend"] = getattr(settings, "phone_audio_backend", "pcm_ws")
    finger["mobile_contract"] = MOBILE_CONVERSATION_CONTRACT in instructions
    return finger


def config_diff(mac: dict[str, Any], phone: dict[str, Any]) -> list[dict[str, Any]]:
    keys = [
        "model",
        "voice",
        "response_modalities",
        "input_transcription",
        "output_transcription",
        "manual_vad",
        "compression",
        "resumption",
        "thinking",
        "tools",
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
    """Independent ASR for diagnostic audio, via Gemini. Bytes are not stored."""

    import base64

    import httpx

    key = (settings.google_api_key or "").strip()
    if not key:
        raise RuntimeError("google_missing")
    if not audio or len(audio) > MAX_ORACLE_BYTES:
        raise ValueError("audio_too_large")
    model = (getattr(settings, "phone_asr_model", None) or "gemini-2.5-flash").strip()
    lang = (language or "en").strip() or "en"
    prompt = (
        f"Transcribe this {lang} audio exactly. Reply with only the transcript, "
        f"no commentary. Proper nouns to prefer: {MOBILE_ASR_LEXICON}"
    )
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime or "audio/mp4",
                            "data": base64.b64encode(audio).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 256},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, params={"key": key}, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"asr_failed:{response.status_code}")
    payload = response.json()
    chunks: list[str] = []
    candidates = payload.get("candidates") or []
    if candidates and isinstance(candidates[0], dict):
        content = candidates[0].get("content") or {}
        for part in content.get("parts") or []:
            if isinstance(part, dict) and part.get("text"):
                chunks.append(str(part["text"]))
    text = "".join(chunks).strip()
    return {
        "transcript": text,
        "model": model,
        "language": language,
        "critical_tokens": critical_tokens(text),
        "stored": False,
    }
