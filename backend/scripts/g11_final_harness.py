"""G1.11 final harness — real provider audio via Evie backend's live voice websocket."""
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

# Use Evie's config loader (same as backend)
sys.path.insert(0, "/Users/sahajpatel/Code/ev/backend")
from app.config import settings

print(f"OPENAI_KEY_CONFIGURED={bool(settings.openai_api_key)}")
print(f"REALTIME_URL={settings.openai_realtime_url}")
print(f"VOICE_MODEL={settings.openai_realtime_model}")
print(f"TURN_GATE_ENABLED={getattr(settings, 'turn_gate_enabled', False)}")
print(f"TIMEZONE={getattr(settings, 'timezone', 'unknown')}")

def synth_pcm(text: str, rate: int = 24000) -> bytes:
    """Generate 24kHz mono PCM16LE via say + ffmpeg (Evie's actual path would use same)."""
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
        aiff = tmp.name
    wav = aiff + ".wav"
    try:
        subprocess.run(["say", "-v", "Samantha", "-o", aiff, text], check=True, timeout=10)
        # Use ffmpeg for reliable conversion
        subprocess.run(
            ["ffmpeg", "-y", "-i", aiff, "-ac", "1", "-ar", str(rate), "-acodec", "pcm_s16le", wav],
            check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        with open(wav, "rb") as f:
            data = f.read()
            idx = data.find(b"data")
            if idx != -1:
                pcm = data[idx+8:]
                # Verify
                assert len(pcm) % 2 == 0, "PCM not 16-bit aligned"
                duration = len(pcm) / 2 / rate
                assert duration > 0.5, f"PCM too short: {duration}s"
                # Check non-zero
                import struct
                samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
                non_zero = sum(1 for s in samples if abs(s) > 100)
                assert non_zero > 100, "PCM appears silent"
                print(f"PCM OK: {len(pcm)} bytes, {duration:.2f}s, non-zero {non_zero}/{len(samples)}")
                return pcm
            return data[44:]
    finally:
        for p in [aiff, wav]:
            try: os.unlink(p)
            except: pass
    raise RuntimeError("PCM generation failed")

async def run_one_turn(text: str, timeout: float = 20.0):
    import websockets
    from app.db import SessionLocal

    pcm = synth_pcm(text, rate=24000)
    print(f"\n=== TURN: {text!r} PCM {len(pcm)} bytes ===")

    # Connect to local Evie backend's live voice websocket (real provider path)
    # Need to create a live session first via HTTP, then connect via websocket
    # For harness, we can directly use the backend's LiveSession + GrokVoiceBridge with real OpenAI
    # Let's do direct GrokVoiceBridge with real OpenAI for minimal harness
    from app.voice.live.grok_voice import GrokVoiceBridge, grok_session_update
    from app.voice.live.session import LiveSession
    from app.voice.live.transport import _grok_tool_runner
    from app.ev.capabilities import build_runtime_projection

    # Capture session.update
    captured = {}

    # Build manifest as backend does
    async with SessionLocal() as s:
        manifest = await build_runtime_projection(s, actor="master", realtime_provider="openai")
        payload = grok_session_update(provider="openai", capability_manifest=manifest, turn_authority_v2=False)
        td = payload["session"]["audio"]["input"]["turn_detection"]
        tools = payload["session"]["tools"]
        print(f"session.update turn_detection: {td}")
        print(f"session.update tools: {len(tools)} {[t.get('name') for t in tools][:5]}")
        assert td["create_response"] is False, "Gate not enabled"
        assert td["interrupt_response"] is False
        assert not any(t.get("name", "").startswith("life_") for t in tools), "Life tools should be absent"
        assert not any(t.get("name") == "evie_turn" for t in tools), "evie_turn should be absent"
        print("session.update OK: create_response false, no life/evie_turn")

    # Now try real websocket to OpenAI via GrokVoiceBridge
    # Use the production bridge with real credentials
    session = LiveSession(session_id=f"harness-{uuid.uuid4().hex[:8]}", device_id="harness", backchannel_enabled=False)
    runner = _grok_tool_runner(actor="master", device_id=None, live=session)

    # Capture events
    events = []
    orig_emit = session.emit
    async def capturing_emit(event):
        events.append(event)
        await orig_emit(event)
    session.emit = capturing_emit

    # Track provider_item_id and turn_id
    provider_item_id = None
    turn_id = None
    transcript = None
    response_created = False
    assistant_output = None

    # Use real OpenAI key from settings
    api_key = settings.openai_api_key
    if not api_key:
        print("No OpenAI key, skipping real websocket")
        return {"blocked": "no_key"}

    # Create bridge with real connect
    bridge = GrokVoiceBridge(
        on_event=session.emit,
        on_tool=runner,
        connect=None,  # Will use default websockets.connect to OpenAI
        api_key=api_key,
        provider="openai",
        now_ms=session.now,
    )
    session.grok_voice = bridge

    # Start bridge (connects to wss://api.openai.com/v1/realtime)
    try:
        await asyncio.wait_for(bridge.start(), timeout=10)
        print("Bridge connected to OpenAI")
        # Get session IDs
        print(f"Bridge provider session: {getattr(bridge, '_provider_session_id', 'unknown')}")
        # Send audio via bridge's append_pcm (which does resampling and input_audio_buffer.append)
        # We need to send PCM via the bridge's upstream
        # The bridge has a method to send audio: append_pcm
        # Let's try to send the PCM
        # Split into chunks
        chunk_size = 4800  # 100ms at 24kHz
        for i in range(0, len(pcm), chunk_size):
            chunk = pcm[i:i+chunk_size]
            # Use bridge's internal send - for OpenAI, it's via _send with input_audio_buffer.append
            # The bridge's append_pcm does resampling and queuing
            # For harness, we can directly call the bridge's method if available
            # Fallback: send via the bridge's websocket directly
            try:
                # Try to use the bridge's method to send audio
                if hasattr(bridge, "append_pcm"):
                    await bridge.append_pcm(chunk)
                elif hasattr(bridge, "_send"):
                    b64 = base64.b64encode(chunk).decode()
                    await bridge._send({"type": "input_audio_buffer.append", "audio": b64})
                else:
                    print("No append_pcm or _send on bridge")
                    break
            except Exception as e:
                print(f"Send audio failed: {e}")
                break
            await asyncio.sleep(0.02)
        # Commit
        try:
            if hasattr(bridge, "_send"):
                await bridge._send({"type": "input_audio_buffer.commit"})
            print("Sent audio and commit")
        except Exception as e:
            print(f"Commit failed: {e}")

        # Wait for transcription and response
        start = time.time()
        got_transcription = False
        got_response = False
        while time.time() - start < 15:
            await asyncio.sleep(0.2)
            for ev in events:
                if ev.type == "final_transcript" and not got_transcription:
                    transcript = ev.text
                    provider_item_id = getattr(ev, "provider_item_id", None) or getattr(ev, "item_id", None)
                    print(f"TRANSCRIPTION: {transcript!r} provider_item_id={provider_item_id}")
                    got_transcription = True
                if ev.type == "response" and not got_response:
                    print(f"RESPONSE: {ev}")
                    got_response = True
            # Check for TurnGate events
            for ev in events:
                if ev.type == "turn_gate":
                    print(f"TURN_GATE: {ev}")
            if got_transcription and got_response:
                break
            # Also check for assistant output
            for ev in events:
                if hasattr(ev, "text") and ev.text and "joke" in ev.text.lower():
                    assistant_output = ev.text
                    print(f"ASSISTANT: {ev.text[:100]}")

        print(f"Harness result: transcription={got_transcription} response={got_response}")
        return {
            "transcript": transcript,
            "provider_item_id": provider_item_id,
            "got_transcription": got_transcription,
            "got_response": got_response,
            "assistant_output": assistant_output,
        }
    except Exception as e:
        print(f"Harness failed: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}
    finally:
        try:
            await bridge.stop()
        except:
            pass

if __name__ == "__main__":
    import asyncio
    # First test PCM
    pcm = synth_pcm("Tell me a very short joke.", rate=24000)
    print(f"PCM test: {len(pcm)} bytes")
    # Then test harness
    asyncio.run(run_one_turn("Tell me a very short joke."))
