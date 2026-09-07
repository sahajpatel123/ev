"""Probe: speech turn + continuous room noise during the reply wait.

Mirrors the real EV.app condition (live mic keeps streaming PCM while the
Spark reply is being generated). If the turn is cancelled, the owner hears
nothing — the suspected mute.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import random
import struct
import urllib.request
from pathlib import Path

import websockets

BASE = "http://127.0.0.1:18000"
KEY = (
    Path.home()
    / "Library/Application Support/EV/device-token-mac-sahaj’s-macbook-air.cred"
).read_text().strip()


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = resp.read().decode()
        return json.loads(payload) if payload else {}


def noise_pcm(seconds: float, amplitude: int = 900, rate: int = 16000) -> bytes:
    """Low-level babble noise — above VAD floor, below actual speech."""
    rnd = random.Random(7)
    out = bytearray()
    n = int(rate * seconds)
    for i in range(n):
        v = int(amplitude * (0.6 + 0.4 * math.sin(2 * math.pi * 180 * i / rate)) ) + rnd.randint(-amplitude, amplitude) // 2
        out += struct.pack("<h", max(-32768, min(32767, v)))
    return bytes(out)


async def main() -> None:
    sid = api("POST", "/v1/voice/live/open", {"device_id": "mac-ears"})["session_id"]
    print("session:", sid)
    pcm = Path("/tmp/evie-test.pcm").read_bytes()
    babble = noise_pcm(0.4, amplitude=900)
    uri = f"ws://127.0.0.1:18000/v1/voice/live?session_id={sid}&token={KEY}"
    events: list[dict] = []
    async with websockets.connect(uri, open_timeout=20, max_size=None) as ws:
        json.loads(await ws.recv())

        async def pump_out():
            async for msg in ws:
                if isinstance(msg, bytes):
                    continue
                event = json.loads(msg)
                events.append(event)
                kind = event.get("type")
                print(f"<- {kind} {event.get('code','')} {str(event.get('text') or '')[:60]}")

        out = asyncio.create_task(pump_out())
        step = 1280

        async def stream(pcm_bytes: bytes, delay: float = 0.04):
            for i in range(0, len(pcm_bytes), step):
                await ws.send(pcm_bytes[i : i + step])
                await asyncio.sleep(delay)

        await stream(pcm)
        silence = b"\x00" * step
        # Wait for the reply while the LIVE MIC keeps streaming noise bursts
        # every ~2.5 s (chair, keyboard, breath — anything above VAD floor).
        for i in range(60):
            if any(e.get("type") in {"reply"} for e in events):
                break
            if i % 60 == 25 and i > 0:
                print("   (noise burst)")
                await stream(babble, delay=0.04)
            await ws.send(silence)
            await asyncio.sleep(0.04)
        await asyncio.sleep(2)
        out.cancel()
    chunks = [e for e in events if e.get("type") == "tts_chunk"]
    replies = [e for e in events if e.get("type") == "reply"]
    print("tts_chunks:", len(chunks), "replies:", len(replies))


if __name__ == "__main__":
    asyncio.run(main())
