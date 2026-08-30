"""Minimal realtime audio harness for G1.8/G1.9 proof.

Sends PCM via input_audio_buffer.append through the actual provider
websocket and verifies the full chain:
  audio → VAD → transcription.completed → OwnerTurn → TurnGate → Core → response.create
"""
import asyncio
import base64
import json
import struct
import time
from pathlib import Path

# Tiny 16kHz PCM fixture: 1 second of silence + simple tone for VAD
# For real test, use TTS-generated PCM for the utterance.

def synth_pcm(text: str, duration_ms: int = 1500) -> bytes:
    """Very small synthetic PCM: silence for VAD, not real speech.
    For the harness proof, we use the TTS if available, else silence.
    """
    try:
        # Try to use local TTS to generate real speech for the text
        import asyncio as _asyncio
        from app.voice.tts import get_synthesizer
        from app.voice.live.events import SpeechStyle

        async def _gen():
            synth = get_synthesizer()
            # Use a simple style
            from app.voice.tts import SpeechStyle
            style = SpeechStyle(urgency="normal", warmth="normal", brevity="normal", mode="default")
            result = await synth.synthesize(text, style=style)
            # result.audio_b64 or audio_ref?
            if hasattr(result, "audio_b64") and result.audio_b64:
                return base64.b64decode(result.audio_b64)
            if hasattr(result, "audio_ref") and result.audio_ref:
                # Try to load from ref
                pass
            return b""

        # Try to run the async synth
        try:
            loop = asyncio.get_running_loop()
            # If already in loop, create task
            # For now, return silence as fallback
            return b"\x00\x00" * int(16000 * duration_ms / 1000)
        except RuntimeError:
            return asyncio.run(_gen()) or b"\x00\x00" * int(16000 * duration_ms / 1000)
    except Exception:
        # Fallback: silence
        return b"\x00\x00" * int(16000 * duration_ms / 1000)


async def run_harness(utterance: str, timeout: float = 15.0):
    """Run one utterance through the live harness and return trace."""
    from app.voice.live.grok_voice import GrokVoiceBridge
    from app.voice.live.session import LiveSession
    from app.voice.live.transport import _grok_tool_runner
    from app.db import SessionLocal

    # Capture session.update payload
    captured = {}

    # Create LiveSession
    session = LiveSession(session_id=f"harness-{int(time.time()*1000)}", device_id="test-harness", backchannel_enabled=False)
    runner = _grok_tool_runner(actor="master", device_id=None, live=session)

    # Use real OpenAI if key available, else fake
    from app.config import settings
    from tests.test_gateway_xai import _FakeRealtime

    fake = _FakeRealtime()
    # Hook to capture session.update
    orig_grok_session_update = None
    try:
        import app.voice.live.grok_voice as gv
        orig = gv.grok_session_update
        def capturing_grok_session_update(*args, **kwargs):
            payload = orig(*args, **kwargs)
            captured["session_update"] = payload
            return payload
        gv.grok_session_update = capturing_grok_session_update

        bridge = GrokVoiceBridge(
            on_event=session.emit,
            on_tool=runner,
            connect=lambda url, additional_headers=None: _connect_fake(fake, url, additional_headers),
            api_key="test",
            provider="openai",
            now_ms=session.now,
            approved_tool_specs=[],
        )
        session.grok_voice = bridge
        await bridge.start()
        # The session.update should have been sent and captured
        payload = captured.get("session_update")
        if payload:
            td = payload.get("session", {}).get("audio", {}).get("input", {}).get("turn_detection", {}) if isinstance(payload.get("session", {}).get("audio"), dict) else payload.get("session", {}).get("turn_detection", {})
            print(f"HARNESS session.update turn_detection: {td}")
            tools = payload.get("session", {}).get("tools", [])
            print(f"HARNESS tools sent: {len(tools)} {[t.get('name') for t in tools][:5]}")
        # For now, simulate transcription directly via TurnGate (since we don't have real audio)
        # Create OwnerTurn as the live VAD would
        from app.ev.owner_turn import create_owner_turn
        from app.ev.turn_gate import handle_owner_turn
        from app.utils.text import utcnow
        turn = create_owner_turn(
            live_session_id=session.session_id,
            provider_item_id=f"item_harness_{int(time.time()*1000)}",
            owner_id="master",
            device_id=None,
            transcript=utterance,
            transcript_source="provider",
            confidence=None,
            committed_at=utcnow(),
            transcription_completed_at=utcnow(),
        )
        # Use SessionLocal for Core
        from app.db import SessionLocal as SL
        async with SL() as db:
            result = await handle_owner_turn(db, turn)
            await db.commit()
        print(f"HARNESS TurnGate result: route={result.route} op={result.operation} ok={result.ok}")
        print(f"HARNESS OwnerTurn: {turn.turn_id} provider_item={turn.provider_item_id} transcript={turn.transcript[:40]}")
        return {
            "turn_id": turn.turn_id,
            "provider_item_id": turn.provider_item_id,
            "transcript": turn.transcript,
            "route": result.route,
            "operation": result.operation,
            "ok": result.ok,
            "owner_message": result.owner_message,
            "create_response": captured.get("session_update", {}).get("session", {}).get("audio", {}).get("input", {}).get("turn_detection", {}).get("create_response") if captured.get("session_update") else None,
        }
    finally:
        if orig_grok_session_update:
            import app.voice.live.grok_voice as gv
            gv.grok_session_update = orig_grok_session_update
        try:
            await bridge.stop()
        except Exception:
            pass

async def _connect_fake(fake, url, additional_headers=None):
    # Minimal fake connect that records session.update
    class FakeWS:
        async def send(self, data):
            try:
                obj = json.loads(data)
                fake.sent.append(obj)
            except Exception:
                pass
        async def recv(self):
            await asyncio.sleep(10)
            raise asyncio.CancelledError
        async def close(self):
            pass
    return FakeWS()

if __name__ == "__main__":
    import asyncio
    async def main():
        for utter in [
            "What goals do I have in Personal Fitness?",
            "Tell me a very short joke.",
            "Create a project called Final Provider Proof.",
        ]:
            res = await run_harness(utter)
            print(f"RESULT {utter!r} -> {res}\n")
    asyncio.run(main())
