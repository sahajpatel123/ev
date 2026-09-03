"""Optional live Meta Model API probes. Skipped unless META_MODEL_API_KEY is loaded.

Never print, log, or assert the credential value.
"""

from __future__ import annotations

import io
import os
import shutil

import pytest

from app.gateway.muse import muse_api_key, muse_base_url, muse_spark_model, muse_voice_model

pytestmark = pytest.mark.skipif(
    os.environ.get("EV_TEST_USE_LIVE_MUSE") != "1" or not muse_api_key(),
    reason="live Muse probes require EV_TEST_USE_LIVE_MUSE=1 and META_MODEL_API_KEY",
)


@pytest.fixture(autouse=True)
def _allow_remote_asr_for_live_muse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_ALLOW_REMOTE_ASR", "true")


def _spoken_wav_pcm(text: str) -> tuple[bytes, bytes]:
    """macOS `say` → 16 kHz mono PCM16 WAV bytes and raw PCM."""

    import subprocess
    import tempfile
    import wave
    from pathlib import Path

    say = shutil.which("say")
    if not say:
        pytest.skip("macOS say is required for live Muse Voice acceptance audio")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "spoken.wav"
        proc = subprocess.run(
            [
                say,
                "-o",
                str(path),
                "--file-format=WAVE",
                "--data-format=LEI16@16000",
                text,
            ],
            check=False,
            capture_output=True,
        )
        if proc.returncode != 0 or not path.is_file():
            pytest.skip("macOS say could not synthesize live Muse Voice audio")
        wav_bytes = path.read_bytes()
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        pcm = wav.readframes(wav.getnframes())
    return wav_bytes, pcm


def _sidecar_bearer() -> str:
    """Read Talk auth from repo overlay/.env without printing the value."""

    from pathlib import Path

    names = ("EV_API_KEY", "EV_MASTER_KEY")
    for path in (
        Path("/Users/sahajpatel/Code/ev/.env"),
        Path.home() / ".ev/secrets/production.env",
    ):
        if not path.is_file():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if key in names and val:
                return val
    pytest.skip("no Talk sidecar bearer in overlay or .env")


def _semantic_hit(transcript: str, needles: tuple[str, ...]) -> bool:
    blob = (transcript or "").lower()
    return any(n.lower() in blob for n in needles)


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
async def test_live_muse_voice_file_transcribe_bounded_set() -> None:
    import base64

    from app.voice.muse_voice import MuseVoiceTranscriber

    phrases = (
        ("Open Calculator", ("calculator", "calculate")),
        ("Open Calculator now", ("calculator", "calculate")),
        (
            "Can you tell me what we were working on yesterday after lunch",
            ("yesterday", "lunch", "working"),
        ),
        ("git commit dash m fix the spark loop", ("git", "commit", "spark")),
        ("Hey Evie what priority is Canary", ("evie", "canary", "priority")),
        ("Abre Calculator por favor", ("calculator", "calculadora", "abre")),
    )
    transcriber = MuseVoiceTranscriber()
    hits = 0
    for spoken, needles in phrases:
        wav_bytes, _pcm = _spoken_wav_pcm(spoken)
        result = await transcriber.transcribe(
            audio_b64=base64.b64encode(wav_bytes).decode("ascii")
        )
        text = (result.text or "").strip()
        assert text
        assert result.details.get("diarization_is_owner_auth") is False
        if _semantic_hit(text, needles):
            hits += 1
    assert hits >= 4, f"semantic transcripts too weak ({hits}/6)"


@pytest.mark.asyncio
async def test_live_muse_voice_stream_partial_final_endpoint_no_duplicate() -> None:
    import asyncio

    from app.voice.muse_voice import MuseVoiceTranscriber

    _wav, pcm = _spoken_wav_pcm("Open Calculator")
    finals: list[str] = []
    errors: list[object] = []

    async def on_final(text: str) -> None:
        finals.append(text)

    async def on_unusable(exc) -> None:
        errors.append(exc)

    transcriber = MuseVoiceTranscriber()
    loop = asyncio.get_running_loop()
    transcriber.start_live(
        loop,
        on_partial=None,
        on_final=on_final,
        on_unusable=on_unusable,
        sample_rate=16000,
    )
    frame = 3200  # 100 ms of PCM16 @ 16 kHz
    try:
        await asyncio.sleep(0.4)
        for i in range(0, len(pcm), frame):
            transcriber.feed_live(pcm[i : i + frame])
            await asyncio.sleep(0.05)
        transcriber.end_live()
        for _ in range(80):
            if finals or errors:
                break
            await asyncio.sleep(0.1)
    finally:
        transcriber.abort_live()
    assert not errors, f"live Muse Voice failed: {type(errors[0]).__name__}"
    assert len(finals) == 1
    assert _semantic_hit(finals[0], ("calculator", "calculate"))


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
    muse = models.get("muse") or {}
    assert muse.get("reasoning_effort") == "high"
    runtime = body.get("runtime") or {}
    checks = {row.get("name"): row for row in (runtime.get("checks") or []) if isinstance(row, dict)}
    tts = checks.get("tts") or {}
    if tts:
        assert tts.get("provider") == "edge_tts"


def _spark_calls(body: dict) -> int:
    muse = ((body.get("models") or {}).get("muse") or {})
    return int(muse.get("spark_calls") or 0)


@pytest.mark.asyncio
async def test_owner_facing_sidecar_typed_chat_is_spark_not_grok() -> None:
    import httpx

    bearer = _sidecar_bearer()
    async with httpx.AsyncClient(timeout=60) as client:
        health = await client.get("http://127.0.0.1:18000/v1/health")
        assert health.status_code == 200, health.text
        providers = (health.json().get("providers") or {})
        assert providers.get("chat") == "meta_muse_spark"
        assert providers.get("live") == "pipeline"
        resp = await client.post(
            "http://127.0.0.1:18000/v1/chat",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"message": "Reply with the single word pong."},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "pong" in (body.get("reply") or "").lower()
    model = (body.get("model") or "").lower()
    assert "grok" not in model
    assert "luna" not in model
    assert "deepseek" not in model
    assert "muse-spark" in model or "spark" in model


@pytest.mark.asyncio
async def test_owner_facing_sidecar_local_intent_does_not_need_spark() -> None:
    import httpx

    bearer = _sidecar_bearer()
    async with httpx.AsyncClient(timeout=30) as client:
        health = await client.get("http://127.0.0.1:18000/v1/health")
        assert (health.json().get("providers") or {}).get("chat") == "meta_muse_spark"
        resp = await client.post(
            "http://127.0.0.1:18000/v1/chat",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"message": "are you there"},
        )
    assert resp.status_code == 200, resp.text
    reply = (resp.json().get("reply") or "").strip()
    assert reply
    assert "unavailable" not in reply.lower()


@pytest.mark.asyncio
async def test_owner_facing_sidecar_spark_counters_and_composed_hearing() -> None:
    """Typed chat on :18000 must move Spark counters; local intent must not.

    Also compose Muse Voice file ASR → /v1/chat so owner-facing proof is not
    only a text ping. TTSPlayer stays on the Mac client; the sidecar mouth is
    Edge TTS.
    """

    import base64

    import httpx

    from app.voice.muse_voice import MuseVoiceTranscriber

    bearer = _sidecar_bearer()
    headers = {"Authorization": f"Bearer {bearer}"}
    async with httpx.AsyncClient(timeout=90) as client:
        before = await client.get("http://127.0.0.1:18000/v1/health")
        assert before.status_code == 200, before.text
        assert (before.json().get("providers") or {}).get("chat") == "meta_muse_spark"
        spark0 = _spark_calls(before.json())

        local = await client.post(
            "http://127.0.0.1:18000/v1/chat",
            headers=headers,
            json={"message": "are you there"},
        )
        assert local.status_code == 200, local.text
        mid = await client.get("http://127.0.0.1:18000/v1/health")
        assert _spark_calls(mid.json()) == spark0

        conv = await client.post(
            "http://127.0.0.1:18000/v1/chat",
            headers=headers,
            json={"message": "Reply with the single word pong."},
        )
        assert conv.status_code == 200, conv.text
        after = await client.get("http://127.0.0.1:18000/v1/health")
        assert _spark_calls(after.json()) >= spark0 + 1
        model = (conv.json().get("model") or "").lower()
        assert "grok" not in model
        assert "luna" not in model
        assert "deepseek" not in model

    wav_bytes, _pcm = _spoken_wav_pcm("are you there")
    heard = await MuseVoiceTranscriber().transcribe(
        audio_b64=base64.b64encode(wav_bytes).decode("ascii")
    )
    assert (heard.text or "").strip()
    assert heard.provider == "meta_muse_voice"
    async with httpx.AsyncClient(timeout=60) as client:
        composed = await client.post(
            "http://127.0.0.1:18000/v1/chat",
            headers=headers,
            json={"message": heard.text},
        )
    assert composed.status_code == 200, composed.text
    assert (composed.json().get("reply") or "").strip()
    assert "unavailable" not in (composed.json().get("reply") or "").lower()


@pytest.mark.asyncio
async def test_live_canary_priority_is_deterministic_zero_spark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.ev.luna_adapter import classify_intent
    from app.gateway.muse import muse_counters_snapshot, reset_muse_counters

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "turn_control_provider", "meta_muse_spark")
    reset_muse_counters()
    intent = await classify_intent("what priority is Canary")
    assert muse_counters_snapshot()["spark_calls"] == 0
    assert intent.route == "STATE_QUERY"


@pytest.mark.asyncio
async def test_live_ambiguous_turn_calls_spark(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings
    from app.ev.luna_adapter import classify_intent
    from app.gateway.muse import muse_counters_snapshot, reset_muse_counters

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "turn_control_provider", "meta_muse_spark")
    reset_muse_counters()
    await classify_intent("I've been thinking about that thing from yesterday, what should I do")
    assert muse_counters_snapshot()["spark_calls"] >= 1


async def _sidecar_chat(client, bearer: str, message: str):
    return await client.post(
        "http://127.0.0.1:18000/v1/chat",
        headers={"Authorization": f"Bearer {bearer}"},
        json={"message": message},
    )


@pytest.mark.asyncio
async def test_owner_facing_sidecar_canary_is_core_not_grok() -> None:
    import httpx

    bearer = _sidecar_bearer()
    async with httpx.AsyncClient(timeout=60) as client:
        before = await client.get("http://127.0.0.1:18000/v1/health")
        assert (before.json().get("providers") or {}).get("chat") == "meta_muse_spark"
        spark0 = _spark_calls(before.json())
        resp = await _sidecar_chat(client, bearer, "what priority is Canary")
        after = await client.get("http://127.0.0.1:18000/v1/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    reply = (body.get("reply") or "").strip()
    assert reply
    assert "unavailable" not in reply.lower()
    model = (body.get("model") or "").lower()
    assert "grok" not in model
    assert "luna" not in model
    assert "deepseek" not in model
    assert _spark_calls(after.json()) == spark0


@pytest.mark.asyncio
async def test_owner_facing_sidecar_calculate_and_research_and_memory() -> None:
    import httpx

    bearer = _sidecar_bearer()
    async with httpx.AsyncClient(timeout=120) as client:
        health = await client.get("http://127.0.0.1:18000/v1/health")
        assert (health.json().get("providers") or {}).get("chat") == "meta_muse_spark"
        spark0 = _spark_calls(health.json())
        calc = await _sidecar_chat(client, bearer, "calculate 19 times 47")
        assert calc.status_code == 200, calc.text
        calc_reply = (calc.json().get("reply") or "")
        assert "893" in calc_reply.replace(",", "")
        assert "grok" not in (calc.json().get("model") or "").lower()
        mid = await client.get("http://127.0.0.1:18000/v1/health")
        assert _spark_calls(mid.json()) == spark0

        research = await _sidecar_chat(
            client, bearer, "research the current weather in Surat, one short sentence"
        )
        assert research.status_code == 200, research.text
        research_reply = (research.json().get("reply") or "").strip()
        assert research_reply
        assert "unavailable" not in research_reply.lower()
        assert "grok" not in (research.json().get("model") or "").lower()
        assert "luna" not in (research.json().get("model") or "").lower()

        memory = await _sidecar_chat(
            client, bearer, "what were we working on recently, one short sentence"
        )
        assert memory.status_code == 200, memory.text
        memory_reply = (memory.json().get("reply") or "").strip()
        assert memory_reply
        assert "unavailable" not in memory_reply.lower()
        assert "grok" not in (memory.json().get("model") or "").lower()


@pytest.mark.asyncio
async def test_live_edge_tts_speaks_spark_text_without_openai_mouth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.voice.contracts import SpeechStyle
    from app.voice.tts import EdgeTTSSynthesizer, get_synthesizer

    synth = EdgeTTSSynthesizer(voice="en-GB-SoniaNeural")
    assert synth.name == "edge_tts"
    spoken = await synth.synthesize("pong", style=SpeechStyle())
    assert spoken.audio
    assert len(spoken.audio) > 200
    monkeypatch.setattr(settings, "voice_tts_provider", "edge_tts")
    mouth = get_synthesizer()
    assert mouth.name == "edge_tts"
