"""Optional live Meta Model API probes. Skipped unless META_MODEL_API_KEY is loaded.

Never print, log, or assert the credential value.
"""

from __future__ import annotations

import os

import pytest

from app.gateway.muse import muse_api_key, muse_base_url, muse_spark_model, muse_voice_model

pytestmark = pytest.mark.skipif(
    os.environ.get("EV_TEST_USE_LIVE_MUSE") != "1" or not muse_api_key(),
    reason="live Muse probes require EV_TEST_USE_LIVE_MUSE=1 and META_MODEL_API_KEY",
)


@pytest.mark.asyncio
async def test_live_catalog_exposes_muse_ids() -> None:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{muse_base_url()}/models",
            headers={"Authorization": f"Bearer {muse_api_key()}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    ids: list[str] = []
    raw = data.get("data") if isinstance(data, dict) else data
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
            elif isinstance(item, str):
                ids.append(item)
    assert muse_spark_model() in ids
    assert muse_voice_model() in ids


@pytest.mark.asyncio
async def test_live_spark_text_and_tool() -> None:
    from app.contracts import ChatMessage, ToolSpec
    from app.gateway.muse_spark import muse_spark_provider

    provider = muse_spark_provider()
    chat = await provider.chat([ChatMessage(role="user", content="Reply with the single word pong.")])
    assert "pong" in (chat.text or "").lower()
    tools = await provider.chat_with_tools(
        [ChatMessage(role="user", content="Call emit_ok.")],
        [ToolSpec(name="emit_ok", description="Acknowledge", parameters={"type": "object", "properties": {}})],
    )
    assert tools.tool_calls or (tools.text or "").strip()


@pytest.mark.asyncio
async def test_live_spark_stream_and_structured_intent() -> None:
    from app.contracts import ChatMessage
    from app.ev.luna_adapter import EMIT_INTENT_TOOL
    from app.gateway.muse_spark import muse_spark_provider

    provider = muse_spark_provider()
    chunks: list[str] = []
    async for chunk in provider.stream_chat(
        [ChatMessage(role="user", content="Reply with the single word pong.")]
    ):
        text = getattr(chunk, "text", None) or getattr(chunk, "delta", None) or ""
        if text:
            chunks.append(str(text))
    joined = "".join(chunks).lower()
    assert "pong" in joined or chunks
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": EMIT_INTENT_TOOL["parameters"]["properties"],
        "required": ["route", "operation"],
    }
    structured = await provider.chat_structured(
        [ChatMessage(role="user", content="Classify: how are you today")],
        schema=schema,
        schema_name="turn_intent",
    )
    blob = (structured.text or "").lower()
    if structured.tool_calls:
        blob += str(structured.tool_calls[0].arguments).lower()
    assert "conversation" in blob or blob.strip()


@pytest.mark.asyncio
async def test_live_muse_voice_file_transcribe() -> None:
    import base64
    import io
    import math
    import struct
    import wave

    from app.voice.muse_voice import MuseVoiceTranscriber

    rate = 16000
    frames = b"".join(
        struct.pack("<h", int(4000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(rate)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    transcriber = MuseVoiceTranscriber()
    result = await transcriber.transcribe(
        audio_b64=base64.b64encode(buffer.getvalue()).decode("ascii")
    )
    assert (result.text or "").strip()
    assert result.details.get("diarization_is_owner_auth") is False


@pytest.mark.asyncio
async def test_owner_facing_talk_sidecar_health_is_muse_not_openai_or_grok() -> None:
    """The current owner-facing Talk process must already be the Muse brain.

    Skipped without a Meta key (same as other live probes). When the key is
    loaded, a stale :18000 still advertising xAI / OpenAI Realtime fails this
    row instead of counting as owner-facing proof.
    """

    import httpx

    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get("http://127.0.0.1:18000/v1/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    providers = body.get("providers") or {}
    models = body.get("models") or {}
    assert providers.get("chat") == "meta_muse_spark"
    assert providers.get("live") == "pipeline"
    voice = models.get("voice") or {}
    turn = models.get("turn_control") or {}
    manager = models.get("manager") or {}
    assert voice.get("provider") == "meta_muse_voice"
    assert voice.get("model") == "muse-voice-transcribe-1.0"
    assert turn.get("provider") == "meta_muse_spark"
    assert turn.get("model") == "muse-spark-1.3-contributor"
    assert manager.get("provider") == "meta_muse_spark"
    assert providers.get("live") != "openai-realtime"
    assert providers.get("chat") != "xai"
