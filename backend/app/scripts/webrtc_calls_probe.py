"""Probe the Gemini Live socket: setup shape offline, handshake with a key.

Offline (no key): builds the Mac and phone setup messages and prints a
shape summary — model, modalities, voice, tool count, resumption and
compression flags. Never prints secrets.

With EV_GOOGLE_API_KEY set: opens the BidiGenerateContent websocket, sends
the Mac setup, and waits for setupComplete. Exit 0 on ack, 1 on failure,
2 when no key is configured (offline shape check still ran).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

from app.config import settings
from app.device_gateway.webrtc_live import phone_webrtc_session
from app.voice.live.gemini_live import gemini_live_setup, gemini_live_ws_url


def redact(text: str, key: str) -> str:
    blob = text or ""
    if key:
        blob = blob.replace(key, "<GOOGLE_KEY>")
    blob = re.sub(r"AIza[A-Za-z0-9_-]+", "AIza<redacted>", blob)
    return blob[:1500]


def summarize_setup(message: dict, *, label: str) -> None:
    setup = message.get("setup") if isinstance(message, dict) else None
    setup = setup if isinstance(setup, dict) else {}
    declarations: list = []
    for block in setup.get("tools") or []:
        if isinstance(block, dict):
            declarations.extend(block.get("functionDeclarations") or [])
    generation = setup.get("generationConfig") or {}
    speech = generation.get("speechConfig") or {}
    voice_cfg = speech.get("voiceConfig") or {}
    prebuilt = voice_cfg.get("prebuiltVoiceConfig") or {}
    print(f"--- {label} ---")
    print("model", setup.get("model"))
    print("modalities", generation.get("responseModalities"))
    print("voice", prebuilt.get("voiceName"))
    print("tools", len(declarations))
    print("input_tx", "inputAudioTranscription" in setup)
    print("output_tx", "outputAudioTranscription" in setup)
    print("compression", "contextWindowCompression" in setup)
    print("resumption", "sessionResumption" in setup)
    print("manual_vad", "realtimeInputConfig" in setup)
    print("thinking", setup.get("thinkingConfig"))


async def handshake(key: str) -> int:
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        print("WEBSOCKETS_MISSING")
        return 1
    url = gemini_live_ws_url(key)
    print("url", redact(url, key))
    setup = gemini_live_setup(function_tools=[])
    try:
        async with asyncio.timeout(30):
            ws = await connect(url, open_timeout=20)
            try:
                await ws.send(json.dumps(setup))
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except (TypeError, ValueError):
                        print("non_json_frame", redact(str(raw), key)[:200])
                        continue
                    if "setupComplete" in event:
                        print("SETUP_COMPLETE")
                        return 0
                    if "error" in event:
                        print("PROVIDER_ERROR", redact(json.dumps(event), key)[:500])
                        return 1
                    keys = sorted(event.keys())
                    print("pre_ack_frame", keys)
            finally:
                await ws.close()
    except TimeoutError:
        print("HANDSHAKE_TIMEOUT")
        return 1
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        print("HANDSHAKE_FAILED", redact(f"{type(exc).__name__}: {exc}", key)[:300])
        return 1
    print("SOCKET_CLOSED_BEFORE_ACK")
    return 1


def main() -> int:
    summarize_setup(gemini_live_setup(function_tools=[]), label="mac setup (offline)")
    summarize_setup(phone_webrtc_session(), label="phone setup (offline)")
    key = (settings.google_api_key or "").strip()
    if not key:
        print("NO_GOOGLE_KEY")
        return 2
    return asyncio.run(handshake(key))


if __name__ == "__main__":
    sys.exit(main())
