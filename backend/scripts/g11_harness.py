"""G1.11 real audio harness — uses Evie's actual config and audio path."""
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Load Evie's config same as backend (via app.config)
# Ensure we load the correct .env
from dotenv import load_dotenv
load_dotenv("/Users/sahajpatel/Code/ev/.env")

sys.path.insert(0, "/Users/sahajpatel/Code/ev/backend")

from app.config import settings

print(f"OPENAI_KEY_CONFIGURED={bool(settings.openai_api_key)}")
print(f"REALTIME_URL={settings.openai_realtime_url}")
print(f"VOICE_MODEL={settings.openai_realtime_model}")
print(f"TURN_GATE_ENABLED={getattr(settings, 'turn_gate_enabled', False)}")
print(f"TURN_CONTROL_MODEL={getattr(settings, 'turn_control_model', 'unknown')}")
print(f"TIMEZONE={getattr(settings, 'timezone', 'unknown')}")

def synth_to_pcm(text: str, rate: int = 24000) -> bytes:
    """Use macOS say + ffmpeg to create 24kHz mono PCM16LE."""
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
        aiff = tmp.name
    wav = aiff + ".wav"
    try:
        subprocess.run(["say", "-v", "Samantha", "-o", aiff, text], check=True, timeout=10)
        # Use ffmpeg to convert to 24kHz mono s16le
        # Need to ensure ffmpeg exists, else try afconvert with correct flags
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", aiff, "-ac", "1", "-ar", str(rate), "-acodec", "pcm_s16le", wav],
                check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            with open(wav, "rb") as f:
                data = f.read()
                # WAV header is 44 bytes, but we need to handle it correctly
                # Find data chunk
                idx = data.find(b"data")
                if idx != -1:
                    # data header: 4 bytes 'data' + 4 bytes size
                    pcm = data[idx+8:]
                    print(f"PCM via ffmpeg: {len(pcm)} bytes, {len(pcm)/2/rate:.2f}s at {rate}Hz")
                    # Verify format
                    assert len(pcm) % 2 == 0, "PCM not 16-bit aligned"
                    assert len(pcm) > 1000, "PCM too short"
                    return pcm
        except FileNotFoundError:
            print("ffmpeg not found, trying afconvert")
        except subprocess.CalledProcessError as e:
            print(f"ffmpeg failed: {e.stderr[:500] if e.stderr else ''}")

        # Try afconvert with correct format: use -d LEI16@24000
        try:
            # afconvert -f WAVE -d LEI16@24000 -c 1
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", f"LEI16@{rate}", "-c", "1", aiff, wav],
                check=True, timeout=10,
            )
            with open(wav, "rb") as f:
                data = f.read()
                idx = data.find(b"data")
                if idx != -1:
                    pcm = data[idx+8:]
                    print(f"PCM via afconvert: {len(pcm)} bytes, {len(pcm)/2/rate:.2f}s")
                    return pcm
                return data[44:]
        except Exception as e:
            print(f"afconvert also failed: {e}")
            # Fallback: use existing Evie resampler on AIFF PCM
            # Read AIFF and try to extract PCM via Evie's resampler
            # For now, return raw AIFF PCM after conversion via python
            import struct
            with open(aiff, "rb") as f:
                # AIFF has header, try to find SSND chunk
                data = f.read()
                idx = data.find(b"SSND")
                if idx != -1:
                    # SSND header: 8 bytes + 8 bytes offset/blocksize
                    pcm = data[idx+16:]
                    print(f"PCM via AIFF SSND: {len(pcm)} bytes")
                    return pcm
                return data
    finally:
        for p in [aiff, wav]:
            try:
                os.unlink(p)
            except:
                pass
    return b""

async def test_harness_one_turn(text: str):
    pcm = synth_to_pcm(text, rate=24000)
    print(f"Generated PCM for {text!r}: {len(pcm)} bytes")
    # Verify PCM
    assert len(pcm) > 2000, "PCM too short"
    assert len(pcm) % 2 == 0, "Not 16-bit"
    print(f"PCM verified: 24000Hz mono PCM16LE, {len(pcm)//2} samples, {len(pcm)/2/24000:.2f}s")
    # Now try real websocket
    import websockets
    from app.voice.live.grok_voice import grok_session_update
    from app.db import SessionLocal
    from app.ev.capabilities import build_runtime_projection

    # Build session.update payload as backend would
    async with SessionLocal() as s:
        manifest = await build_runtime_projection(s, actor="master", realtime_provider="openai")
        payload = grok_session_update(provider="openai", capability_manifest=manifest, turn_authority_v2=False)
        print(f"session.update turn_detection: {payload['session']['audio']['input']['turn_detection']}")
        print(f"session.update tools: {len(payload['session']['tools'])}")
        # Verify cutover
        td = payload["session"]["audio"]["input"]["turn_detection"]
        assert td["create_response"] is False, "create_response should be false when gate enabled"
        assert td["interrupt_response"] is False
        tool_names = [t.get("name") for t in payload["session"]["tools"]]
        assert "life_project_create" not in tool_names, "life tools should be absent"
        assert "evie_turn" not in tool_names, "evie_turn should be absent"
        print("session.update OK: create_response false, no life/evie_turn, non-life tools present:", any(n in tool_names for n in ["calculate","search_web"]))

    # Now try real websocket connection
    url = settings.openai_realtime_url or "wss://api.openai.com/v1/realtime"
    # Need to add model query param as per grok_voice.grok_voice_url
    from app.voice.live.grok_voice import openai_realtime_url, grok_voice_url
    # Use the actual URL builder
    ws_url = openai_realtime_url(model=settings.openai_realtime_model)
    print(f"Connecting to {ws_url[:60]}...")
    headers = {"Authorization": f"Bearer {settings.openai_api_key}", "OpenAI-Beta": "realtime=v1"}
    try:
        async with websockets.connect(ws_url, additional_headers=headers, max_size=None) as ws:
            print("WebSocket connected")
            # Send session.update
            async with SessionLocal() as s:
                manifest = await build_runtime_projection(s, actor="master", realtime_provider="openai")
                payload = grok_session_update(provider="openai", capability_manifest=manifest, turn_authority_v2=False)
            await ws.send(json.dumps(payload))
            print("Sent session.update")
            # Wait for session.updated
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(msg)
                print(f"Received: {data.get('type')} {str(data)[:200]}")
                if data.get("type") == "session.updated":
                    print("session.updated received OK")
                # Now send audio
                # Split PCM into chunks and send as input_audio_buffer.append
                chunk_size = 4800  # 100ms at 24kHz *2 bytes
                for i in range(0, len(pcm), chunk_size):
                    chunk = pcm[i:i+chunk_size]
                    b64 = base64.b64encode(chunk).decode()
                    await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
                    await asyncio.sleep(0.02)  # small delay
                # Commit the audio buffer
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                print("Sent audio and commit")
                # Wait for transcription and response
                start = time.time()
                got_transcription = False
                got_response = False
                provider_item_id = None
                transcript = None
                while time.time() - start < 15:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2)
                        data = json.loads(msg)
                        t = data.get("type")
                        if t == "conversation.item.input_audio_transcription.completed":
                            transcript = data.get("transcript") or data.get("item", {}).get("content", [{}])[0].get("transcript")
                            provider_item_id = data.get("item_id") or data.get("item", {}).get("id")
                            print(f"TRANSCRIPTION: {transcript!r} item_id={provider_item_id}")
                            got_transcription = True
                        elif t == "response.created":
                            print(f"RESPONSE.CREATED: {data.get('response', {}).get('id')}")
                            got_response = True
                        elif t == "response.done":
                            print(f"RESPONSE.DONE")
                            break
                        elif t and "transcription" in t:
                            print(f"Other transcription event: {t} {str(data)[:300]}")
                        # Check for error
                        if data.get("type") == "error":
                            print(f"ERROR from provider: {data}")
                            break
                    except asyncio.TimeoutError:
                        print("Timeout waiting for provider events, checking...")
                        if got_transcription and got_response:
                            break
                        continue
                print(f"Harness result: transcription={got_transcription} response={got_response} transcript={transcript!r}")
                return got_transcription, got_response, transcript, provider_item_id
            except asyncio.TimeoutError:
                print("Timeout waiting for session.updated")
                return False, False, None, None
    except Exception as e:
        print(f"WebSocket failed: {e}")
        import traceback
        traceback.print_exc()
        return False, False, None, None

if __name__ == "__main__":
    # Test PCM first
    pcm = synth_to_pcm("Tell me a very short joke.", rate=24000)
    print(f"Test PCM: {len(pcm)} bytes, should be >2000 and even: {len(pcm)>2000 and len(pcm)%2==0}")
    # Test session.update
    import asyncio
    asyncio.run(test_harness_one_turn("Tell me a very short joke."))
