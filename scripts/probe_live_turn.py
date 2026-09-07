"""Drive one real voice turn into the live sidecar at :18000.

Creates a voice session (master key), streams spoken PCM, prints every WS
event. Server-authoritative proof of the Muse pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
import urllib.request
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
BASE = "http://127.0.0.1:18000"
KEY = None


def load_key() -> str:
    cred = Path.home() / "Library/Application Support/EV/device-token-mac-sahaj’s-macbook-air.cred"
    if cred.exists():
        token = cred.read_text().strip()
        if token:
            return token
    env = Path("/Users/sahajpatel/Code/ev/.env")
    for line in env.read_text().splitlines():
        if line.startswith("EV_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no EV_API_KEY")


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = resp.read().decode()
        return json.loads(payload) if payload else {}


async def main() -> None:
    global KEY
    KEY = load_key()
    created = api("POST", "/v1/voice/live/open", {"device_id": "mac-ears"})
    sid = created.get("session_id") or created.get("id")
    print("session:", sid, json.dumps(created)[:200])

    pcm = Path("/tmp/evie-test.pcm").read_bytes()
    uri = f"ws://127.0.0.1:18000/v1/voice/live?session_id={sid}&token={KEY}"
    events: list[dict] = []
    audio_written = 0
    async with websockets.connect(uri, open_timeout=20, max_size=None) as ws:
        ready = json.loads(await ws.recv())
        print("ready:", json.dumps(ready)[:200])

        async def pump_out():
            nonlocal audio_written
            async for msg in ws:
                if isinstance(msg, bytes):
                    audio_written += len(msg)
                    continue
                event = json.loads(msg)
                events.append(event)
                kind = event.get("type")
                text = str(event.get("text") or "")[:80]
                print(f"<- {kind} {event.get('code','')} {text}")
                if kind in {"error"} and event.get("fatal"):
                    return

        out = asyncio.create_task(pump_out())
        # stream PCM in 40ms chunks (1280 bytes) like the client
        step = 1280
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i : i + step])
            await asyncio.sleep(0.04)
        # then silence while the turn commits
        silence = b"\x00" * step
        for _ in range(500):
            await ws.send(silence)
            await asyncio.sleep(0.04)
            if any(e.get("type") == "reply" for e in events):
                break
        await asyncio.sleep(2)
        out.cancel()
    print("binary audio frames from server:", audio_written)
    chunks = [e for e in events if e.get("type") == "tts_chunk"]
    print("tts_chunks:", len(chunks))
    for c in chunks:
        b64 = c.get("audio_b64") or ""
        raw = base64.b64decode(b64) if b64 else b""
        print("  chunk:", c.get("content_type"), len(raw), "bytes, rate", c.get("sample_rate"), "text:", str(c.get("text"))[:60])
        if raw:
            Path("/tmp/evie-out.pcm").write_bytes(raw)
    replies = [e for e in events if e.get("type") == "reply"]
    for r in replies:
        print("reply:", str(r.get("text"))[:200])
    errs = [e for e in events if e.get("type") == "error"]
    for e in errs:
        print("error:", e.get("code"), str(e.get("message"))[:160])


if __name__ == "__main__":
    asyncio.run(main())
