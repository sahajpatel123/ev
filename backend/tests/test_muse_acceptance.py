"""Muse Brain V1 acceptance: endpointing, one mouth, policy, research, memory."""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.ev.luna_adapter import classify_intent
from app.ev.policy import MODEL_ACTORS, evaluate_policy
from app.ev.tool_select import resolve_live_action
from app.gateway.muse import muse_counters_snapshot, reset_muse_counters
from app.voice.live.asr_feed import LiveAsrFeed
from app.voice.live.grok_voice import grok_voice_enabled, live_realtime_provider


@pytest.mark.asyncio
async def test_muse_live_protocol_partial_final_endpoint_no_duplicate() -> None:
    from app.voice.muse_voice import _MuseLiveSession

    partials: list[str] = []
    finals: list[str] = []

    async def on_partial(text: str) -> None:
        partials.append(text)

    async def on_final(text: str) -> None:
        finals.append(text)

    session = _MuseLiveSession(
        api_key="test-key",
        model="muse-voice-transcribe-1.0",
        encoding="PCM_16KHZ",
        on_partial=on_partial,
        on_final=on_final,
        on_unusable=None,
    )

    class _WS:
        def __init__(self) -> None:
            self._messages = [
                '{"type":"speechStart","turnId":1}',
                '{"type":"transcript","transcript":"Open Calc","final":false}',
                '{"type":"transcript","transcript":"Open Calculator","final":true}',
                '{"type":"speechEnd","turnId":1}',
                '{"type":"speechComplete","turnId":1,"transcript":"Open Calculator"}',
                '{"type":"speaker","label":"A"}',
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

    await session._receive(_WS())
    assert partials == ["Open Calc", "Open Calculator"]
    assert finals == ["Open Calculator"]
    assert session._got_final is True


def test_muse_live_handshake_puts_bearer_in_first_json_not_http_header() -> None:
    from app.voice.muse_voice import _MuseLiveSession

    session = _MuseLiveSession(
        api_key="test-key",
        model="muse-voice-transcribe-1.0",
        encoding="PCM_16KHZ",
        on_partial=None,
        on_final=None,
        on_unusable=None,
    )
    payload = session.handshake_payload()
    assert payload["authorization"]["accessToken"] == "Bearer test-key"
    assert payload["audioEncoding"] == "PCM_16KHZ"
    assert payload["mode"] == "ENDPOINTING"
    assert payload["model"] == "muse-voice-transcribe-1.0"
    assert "Evie" in payload["keywords"]


@pytest.mark.asyncio
async def test_muse_live_protocol_speech_end_before_final_is_not_duplicated() -> None:
    from app.voice.muse_voice import _MuseLiveSession

    finals: list[str] = []

    async def on_final(text: str) -> None:
        finals.append(text)

    session = _MuseLiveSession(
        api_key="test-key",
        model="muse-voice-transcribe-1.0",
        encoding="PCM_16KHZ",
        on_partial=None,
        on_final=on_final,
        on_unusable=None,
    )

    class _WS:
        def __init__(self) -> None:
            self._messages = [
                '{"type":"speechEnd","turnId":1}',
                '{"type":"transcript","transcript":"open calculator","final":true}',
                '{"type":"speechComplete","turnId":1,"transcript":"Open Calculator"}',
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

    await session._receive(_WS())
    assert finals == ["Open Calculator"]


@pytest.mark.asyncio
async def test_live_asr_feed_native_stream_commits_only_final() -> None:
    partials: list[str] = []
    fed: list[bytes] = []
    ended = {"n": 0}

    class _Native:
        name = "meta_muse_voice"
        native_live_stream = True

        def start_live(self, loop, **kwargs) -> None:
            self._on_partial = kwargs.get("on_partial")
            self._on_final = kwargs.get("on_final")

        def feed_live(self, pcm: bytes) -> None:
            fed.append(pcm)

        def end_live(self) -> None:
            ended["n"] += 1
            if self._on_final:
                asyncio.get_running_loop().create_task(self._on_final("Open Calculator"))

        def abort_live(self) -> None:
            return None

    async def on_partial(text: str) -> None:
        partials.append(text)

    feed = LiveAsrFeed(_Native(), on_partial=on_partial)
    feed.begin()
    feed.feed(b"\x00\x01" * 160)
    await feed._on_native_partial("Open Calc")
    assert "Open Calc" in partials
    assert feed._final_text is None
    feed.end_speech()
    await asyncio.wait_for(feed._final_ready.wait(), timeout=1)
    assert ended["n"] == 1
    text = await feed.final_text(timeout_ms=50)
    assert text == "Open Calculator"


@pytest.mark.asyncio
async def test_live_asr_feed_waits_for_speech_complete_not_partial() -> None:
    class _Native:
        name = "meta_muse_voice"
        native_live_stream = True

        def start_live(self, loop, **kwargs) -> None:
            self._on_final = kwargs.get("on_final")
            self._loop = loop

        def feed_live(self, pcm: bytes) -> None:
            del pcm

        def end_live(self) -> None:
            async def later() -> None:
                await asyncio.sleep(0.04)
                if self._on_final:
                    await self._on_final("Open Calculator")

            self._loop.create_task(later())

        def abort_live(self) -> None:
            return None

    feed = LiveAsrFeed(_Native())
    feed.begin()
    await feed._on_native_partial("open calculator")
    feed.end_speech()
    text = await feed.final_text(timeout_ms=400)
    assert text == "Open Calculator"
    assert text != "open calculator"


@pytest.mark.asyncio
async def test_live_asr_feed_native_timeout_falls_back_to_partial() -> None:
    class _Native:
        name = "meta_muse_voice"
        native_live_stream = True

        def start_live(self, loop, **kwargs) -> None:
            del loop, kwargs

        def feed_live(self, pcm: bytes) -> None:
            del pcm

        def end_live(self) -> None:
            return None

        def abort_live(self) -> None:
            return None

    feed = LiveAsrFeed(_Native())
    feed.begin()
    await feed._on_native_partial("open calculator")
    feed.end_speech()
    text = await feed.final_text(timeout_ms=30)
    assert text == "open calculator"


def test_open_calculator_is_deterministic_not_spark() -> None:
    reset_muse_counters()
    action = resolve_live_action("Open Calculator")
    assert action is not None
    assert action[0] == "open_app"
    assert "calculator" in str(action[1].get("name") or "").lower()
    assert muse_counters_snapshot()["spark_calls"] == 0


def test_capability_router_still_owns_open_app_and_search() -> None:
    from app.ev.capability_router import ALWAYS_AVAILABLE_SEMANTIC, ROUTER_TOOLS

    assert "open_app" in ROUTER_TOOLS
    assert "search_web" in ALWAYS_AVAILABLE_SEMANTIC


def test_privacy_filter_still_excludes_never_send_to_model() -> None:
    import inspect

    from app.memory import retrieval

    source = inspect.getsource(retrieval)
    assert "never_send_to_model" in source


def test_spark_cannot_bypass_policy_for_computer_actions() -> None:
    decision = evaluate_policy("open_app", actor="model", arguments={"name": "Calculator"})
    assert "model" in MODEL_ACTORS
    assert decision.allowed is False or decision.effect in {"deny", "confirm", "refuse"}


def test_open_app_is_computer_executor_navigate_family() -> None:
    from app.ev.computer_executor import family_for_tool, is_mutating

    assert family_for_tool("open_app") == "navigate"
    assert is_mutating("open_app", {"name": "Calculator"}) is True


def test_muse_path_has_one_mouth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voice_live_brain", "pipeline")
    monkeypatch.setattr(settings, "openai_api_key", "sk-present")
    monkeypatch.setattr(settings, "xai_api_key", "xai-present")
    assert live_realtime_provider() is None
    assert grok_voice_enabled() is False
    monkeypatch.setattr(settings, "voice_live_brain", "openai")
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "voice_asr_provider", "meta_muse_voice")
    assert live_realtime_provider() is None
    assert grok_voice_enabled() is False


def test_edge_tts_factory_has_no_openai_or_grok_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.voice.tts import get_synthesizer

    monkeypatch.setattr(settings, "voice_tts_provider", "edge_tts")
    monkeypatch.setattr(settings, "openai_api_key", "sk-must-not-be-used")
    monkeypatch.setattr(settings, "xai_api_key", "xai-must-not-be-used")
    mouth = get_synthesizer()
    assert mouth.name == "edge_tts"


@pytest.mark.asyncio
async def test_research_style_turn_uses_spark_not_deepseek_or_luna(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev.turn_intent import TurnIntent
    from app.gateway import muse as muse_mod

    reset_muse_counters()
    monkeypatch.setattr(muse_mod, "muse_intelligence_active", lambda: True)
    monkeypatch.setattr(muse_mod, "muse_key_loaded", lambda: True)
    calls = {"spark": 0, "deepseek": 0, "luna": 0}

    async def fake_spark(turn, context):
        calls["spark"] += 1
        return TurnIntent(route="RESEARCH_MISSION", operation="UNKNOWN", confidence=0.8)

    async def boom_deepseek(*args, **kwargs):
        calls["deepseek"] += 1
        raise AssertionError("DeepSeek manager must not run")

    async def boom_luna(*args, **kwargs):
        calls["luna"] += 1
        raise AssertionError("OpenAI Luna must not run")

    monkeypatch.setattr("app.ev.luna_adapter._call_spark_intent", fake_spark)
    monkeypatch.setattr("app.ev.luna_adapter._call_responses_api", boom_luna)
    monkeypatch.setattr("app.ev.manager_adapter.DeepSeekManagerAdapter.submit", boom_deepseek)
    intent = await classify_intent("research the current weather in Surat")
    assert calls["spark"] == 1
    assert calls["luna"] == 0
    assert intent.route in {"RESEARCH_MISSION", "DELEGATED_JOB", "CONVERSATION", "CLARIFICATION"}


def test_memory_writer_stays_writer_when_spark_curates() -> None:
    import inspect

    from app.memory import curator
    from app.memory.writer import MemoryWriter

    source = inspect.getsource(curator)
    assert "MemoryWriter" in source
    assert "muse_intelligence_active" in inspect.getsource(curator._call_deepseek)
    assert inspect.isclass(MemoryWriter)


def test_calculate_is_deterministic_not_spark() -> None:
    reset_muse_counters()
    action = resolve_live_action("calculate 19 times 47")
    assert action is not None
    assert action[0] == "calculate"
    assert muse_counters_snapshot()["spark_calls"] == 0


@pytest.mark.asyncio
async def test_v1_chat_fails_closed_without_muse_key(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", None)
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")
    chat = await client.post("/v1/chat", json={"message": "hello there, how are you today"})
    assert chat.status_code == 503, chat.text
    detail = str(chat.json().get("detail") or "").lower()
    assert "unavailable" in detail
    assert chat.headers.get("x-error-code") == "muse_unavailable"


@pytest.mark.asyncio
async def test_v1_chat_local_intent_does_not_require_muse_key(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", None)
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")
    chat = await client.post("/v1/chat", json={"message": "are you there"})
    assert chat.status_code == 200, chat.text
    reply = (chat.json().get("reply") or "").lower()
    assert reply.strip()
    assert "unavailable" not in reply


@pytest.mark.asyncio
async def test_v1_chat_canary_priority_is_core_zero_spark(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.gateway.muse import muse_counters_snapshot, reset_muse_counters

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "turn_control_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", None)
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")

    def boom_provider():
        raise AssertionError("Spark must not run for deterministic Core queries")

    monkeypatch.setattr("app.api.core.get_chat_provider", boom_provider)
    reset_muse_counters()
    chat = await client.post("/v1/chat", json={"message": "what priority is Canary"})
    assert chat.status_code == 200, chat.text
    reply = (chat.json().get("reply") or "").strip()
    assert reply
    assert "unavailable" not in reply.lower()
    assert "canary" in reply.lower() or "project" in reply.lower() or "priority" in reply.lower()
    assert muse_counters_snapshot()["spark_calls"] == 0


@pytest.mark.asyncio
async def test_v1_chat_calculate_is_executor_zero_spark(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.gateway.muse import muse_counters_snapshot, reset_muse_counters

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", None)
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")

    def boom_provider():
        raise AssertionError("Spark must not run for deterministic calculate")

    monkeypatch.setattr("app.api.core.get_chat_provider", boom_provider)
    reset_muse_counters()
    chat = await client.post("/v1/chat", json={"message": "calculate 19 times 47"})
    assert chat.status_code == 200, chat.text
    reply = (chat.json().get("reply") or "").replace(",", "")
    assert "893" in reply
    assert muse_counters_snapshot()["spark_calls"] == 0


@pytest.mark.asyncio
async def test_v1_chat_open_calculator_and_calculate_is_zero_spark(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.gateway.muse import muse_counters_snapshot, reset_muse_counters

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", None)
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")

    def boom_provider():
        raise AssertionError("Spark must not glue deterministic computer pieces")

    monkeypatch.setattr("app.api.core.get_chat_provider", boom_provider)
    reset_muse_counters()
    chat = await client.post(
        "/v1/chat", json={"message": "Open Calculator and calculate 19 times 47"}
    )
    assert chat.status_code == 200, chat.text
    reply = (chat.json().get("reply") or "").replace(",", "")
    assert "893" in reply
    assert muse_counters_snapshot()["spark_calls"] == 0


@pytest.mark.asyncio
async def test_muse_live_stream_failure_is_voice_error_not_nameerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.voice.contracts import VoiceError
    from app.voice.muse_voice import _MuseLiveSession

    errors: list[VoiceError] = []

    async def on_unusable(exc: VoiceError) -> None:
        errors.append(exc)

    monkeypatch.setattr("app.voice.muse_voice.remote_processing_allowed", lambda track: True)

    def boom(*args, **kwargs):
        raise OSError("muse websocket unreachable")

    monkeypatch.setattr("app.voice.muse_voice.websockets.connect", boom)
    session = _MuseLiveSession(
        api_key="test-key",
        model="muse-voice-transcribe-1.0",
        encoding="PCM_16KHZ",
        on_partial=None,
        on_final=None,
        on_unusable=on_unusable,
    )
    await session.run()
    assert errors
    assert isinstance(errors[0], VoiceError)
    assert errors[0].code == "asr_unusable"


@pytest.mark.asyncio
async def test_v1_chat_uses_spark_not_grok(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.contracts import ChatResult
    from app.ev.turn_intent import TurnIntent
    from app.gateway.streaming import ChatStreamChunk

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", "test-key")
    monkeypatch.setattr(settings, "xai_api_key", "xai-must-not-be-used")
    monkeypatch.setattr(settings, "openai_api_key", "sk-must-not-be-used")
    grok = {"n": 0}

    class Spark:
        name = "meta_muse_spark"
        supports_tools = True
        default_model = "muse-spark-1.3-contributor"

        async def chat(self, messages, **kwargs):
            return ChatResult(text="hello from spark", usage={}, model=self.default_model)

        async def chat_with_tools(self, messages, tools, **kwargs):
            return await self.chat(messages)

        async def stream_chat(self, messages, **kwargs):
            yield ChatStreamChunk(text="hello from spark", done=True, model=self.default_model)

    async def fake_spark_intent(turn, context):
        return TurnIntent(route="CONVERSATION", operation="UNKNOWN", confidence=0.9)

    async def boom_grok(*args, **kwargs):
        grok["n"] += 1
        raise AssertionError("Grok must not be the typed-chat brain")

    monkeypatch.setattr("app.api.core.get_chat_provider", lambda: Spark())
    monkeypatch.setattr("app.ev.luna_adapter._call_spark_intent", fake_spark_intent)
    monkeypatch.setattr("app.gateway.providers.XAIProvider.chat", boom_grok)
    resp = await client.post("/v1/chat", json={"message": "hello there, how are you today"})
    assert resp.status_code == 200, resp.text
    assert grok["n"] == 0
    body = resp.json()
    assert "spark" in (body.get("reply") or "").lower()
    assert body.get("model") == "muse-spark-1.3-contributor"


@pytest.mark.asyncio
async def test_laptop_file_rewrite_uses_spark_not_luna_when_muse_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.contracts import ChatResult
    from app.ev import laptop_files

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_key_loaded", lambda: True)
    legacy = {"n": 0}

    class _Prov:
        async def chat(self, messages, **kwargs):
            return ChatResult(
                text='{"content":"hello from spark\\n"}',
                usage={},
                model="muse-spark-1.3-contributor",
            )

    async def boom_legacy(*args, **kwargs):
        legacy["n"] += 1
        raise AssertionError("Luna/DeepSeek must not rewrite files while Muse is the brain")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: _Prov())
    monkeypatch.setattr(laptop_files, "_call_chat_model", boom_legacy)
    text, source = await laptop_files._intelligent_rewrite(
        "", "write a note that says hello", create=True
    )
    assert source == "spark"
    assert "hello from spark" in text
    assert legacy["n"] == 0


@pytest.mark.asyncio
async def test_laptop_file_rewrite_fails_closed_without_muse_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev import laptop_files

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_key_loaded", lambda: False)

    async def boom_legacy(*args, **kwargs):
        raise AssertionError("legacy file intelligence must not run")

    monkeypatch.setattr(laptop_files, "_call_chat_model", boom_legacy)
    with pytest.raises(RuntimeError, match="file_intelligence_unavailable"):
        await laptop_files._intelligent_rewrite("", "write hello", create=True)


def test_memory_enrichment_uses_spark_and_fails_closed_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory.llm_extractor import ENRICHMENT_PROVIDERS, LLMExtractor

    assert "meta_muse_spark" in ENRICHMENT_PROVIDERS
    monkeypatch.setenv("EV_LLM_EXTRACTION_ENABLED", "true")
    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", None)
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")
    assert LLMExtractor().available is False

    class Spark:
        name = "meta_muse_spark"

    assert LLMExtractor(provider=Spark()).available is True

    class XAI:
        name = "xai"

    assert LLMExtractor(provider=XAI()).available is False


@pytest.mark.asyncio
async def test_look_polish_fails_closed_without_muse_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev import look
    from app.gateway.muse import MuseProviderUnavailable

    monkeypatch.setattr(
        "app.ev.look.get_chat_provider",
        lambda: (_ for _ in ()).throw(MuseProviderUnavailable("missing")),
    )
    out = await look._polish_spoken("A red mug is on the desk.", {"ocr_text": "mug"})
    assert out == "A red mug is on the desk."


@pytest.mark.asyncio
async def test_look_polish_refuses_grok_when_muse_is_brain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev import look

    class XAI:
        name = "xai"
        api_key = "xai-must-not-polish"

        async def chat(self, *args, **kwargs):
            raise AssertionError("Grok must not polish look while Muse is the brain")

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr("app.ev.look.get_chat_provider", lambda: XAI())
    out = await look._polish_spoken("A red mug is on the desk.", {"ocr_text": "mug"})
    assert out == "A red mug is on the desk."


@pytest.mark.asyncio
async def test_voice_memory_fallback_uses_muse_not_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.voice.contracts import Transcript
    from app.voice.live import voice_memory

    monkeypatch.setattr(settings, "voice_asr_provider", "meta_muse_voice")
    monkeypatch.setattr(settings, "openai_api_key", "sk-must-not-be-used")
    openai_calls = {"n": 0}

    class MuseEngine:
        async def transcribe(self, audio_b64=None, **kwargs):
            return Transcript(text="Open Calculator", confidence=1.0)

    class BoomOpenAI:
        def __init__(self, *args, **kwargs):
            openai_calls["n"] += 1
            raise AssertionError("OpenAI Whisper must not hear while Muse is the ear")

    monkeypatch.setattr("app.voice.asr.get_transcriber", lambda: MuseEngine())
    monkeypatch.setattr("app.voice.asr.OpenAICompatTranscriber", BoomOpenAI)
    text = await voice_memory.transcribe_utterance_pcm(b"\x00\x01" * 200)
    assert text == "Open Calculator"
    assert openai_calls["n"] == 0


@pytest.mark.asyncio
async def test_openai_whisper_transcriber_blocked_while_muse_is_hearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.voice.asr import OpenAICompatTranscriber
    from app.voice.contracts import VoiceError

    monkeypatch.setattr(settings, "voice_asr_provider", "meta_muse_voice")
    engine = OpenAICompatTranscriber(
        base_url="https://api.openai.com/v1",
        api_key="sk-must-not-be-used",
        model="whisper-1",
    )
    with pytest.raises(VoiceError) as raised:
        await engine.transcribe(audio_b64="AAAA")
    assert raised.value.code == "muse_unavailable"


@pytest.mark.asyncio
async def test_openai_tts_blocked_while_edge_is_the_mouth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.voice.contracts import SpeechStyle
    from app.voice.tts import OpenAICompatSynthesizer

    monkeypatch.setattr(settings, "voice_tts_provider", "edge_tts")
    mouth = OpenAICompatSynthesizer(
        base_url="https://api.openai.com/v1",
        api_key="sk-must-not-be-used",
    )
    with pytest.raises(RuntimeError, match="Edge TTS is the Talk mouth"):
        await mouth.synthesize("pong", style=SpeechStyle())


@pytest.mark.asyncio
async def test_voice_memory_muse_hearing_fails_closed_not_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.voice.live import voice_memory

    monkeypatch.setattr(settings, "voice_asr_provider", "meta_muse_voice")
    monkeypatch.setattr(settings, "openai_api_key", "sk-must-not-be-used")
    openai_calls = {"n": 0}

    class BoomMuse:
        async def transcribe(self, audio_b64=None, **kwargs):
            raise RuntimeError("muse down")

    class BoomOpenAI:
        def __init__(self, *args, **kwargs):
            openai_calls["n"] += 1
            raise AssertionError("Whisper must not substitute for Muse Voice")

        async def transcribe(self, *args, **kwargs):
            raise AssertionError("Whisper must not substitute for Muse Voice")

    monkeypatch.setattr("app.voice.asr.get_transcriber", lambda: BoomMuse())
    monkeypatch.setattr("app.voice.asr.OpenAICompatTranscriber", BoomOpenAI)
    text = await voice_memory.transcribe_utterance_pcm(b"\x00\x01" * 200)
    assert text == ""
    assert openai_calls["n"] == 0


@pytest.mark.asyncio
async def test_multi_step_computer_executor_does_not_call_spark() -> None:
    from app.ev.computer_executor import ComputerExecutor, reset_fence
    from tests.test_computer_executor import FakeMac, _request

    reset_muse_counters()
    reset_fence()
    mac = FakeMac(responses={"open_app": {"ok": True, "set_frontmost": "Calculator"}})
    executor = ComputerExecutor(live=mac)
    first = await executor.execute(
        _request("navigate", "open_app", target="Calculator", args={"name": "Calculator"})
    )
    second = await executor.execute(
        _request("act", "ui_action", args={"action": "press", "element_ref": "e1"})
    )
    assert first.family == "navigate"
    assert second.family == "act"
    assert muse_counters_snapshot()["spark_calls"] == 0
    reset_fence()


def test_live_transport_uses_pipeline_mouth_when_s2s_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    from app.voice.live import transport

    monkeypatch.setattr(settings, "voice_live_brain", "pipeline")
    monkeypatch.setattr(settings, "openai_api_key", "sk-present")
    monkeypatch.setattr(settings, "xai_api_key", "xai-present")
    assert grok_voice_enabled() is False
    source = inspect.getsource(transport)
    assert "make_pipeline_responder" in source
    assert "use_grok = grok_voice_enabled()" in source


def test_vision_analyze_fails_closed_without_muse_instead_of_raising() -> None:
    import inspect

    from app.ev import vision

    source = inspect.getsource(vision.analyze_attachment)
    assert "MuseProviderUnavailable" in source
    assert "Intelligence provider is unavailable" in source


def test_diagnostics_calibration_pings_spark_not_grok() -> None:
    import inspect

    from app.ev import diagnostics

    source = inspect.getsource(diagnostics.run_calibration)
    assert "muse_spark_model" in source
    assert "muse_intelligence_active" in source


def test_preflight_reports_muse_spark_partial_without_key_not_deepseek_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.scripts.preflight import _check_chat

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")
    kind, name, detail = _check_chat()
    assert kind == "PARTIAL"
    assert name == "meta_muse_spark"
    assert "DeepSeek" not in detail or "no silent" in detail
    assert "Grok" in detail or "fails closed" in detail
    assert "test double" not in detail.lower()


def test_preflight_reports_muse_spark_real_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.scripts.preflight import _check_chat

    monkeypatch.setattr(settings, "chat_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "intelligence_provider", "meta_muse_spark")
    monkeypatch.setattr(settings, "meta_model_api_key", "test-key-not-logged")
    kind, name, detail = _check_chat()
    assert kind == "REAL"
    assert name == "meta_muse_spark"
    assert "muse-spark-1.3-contributor" in detail
    assert "pipeline" in detail
    assert "test-key-not-logged" not in detail


def test_preflight_reports_muse_voice_partial_without_key_not_whisper_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.scripts.preflight import _check_asr

    monkeypatch.setattr(settings, "voice_asr_provider", "meta_muse_voice")
    monkeypatch.setattr(settings, "meta_model_api_key", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")
    kind, name, detail = _check_asr()
    assert kind == "PARTIAL"
    assert name == "meta_muse_voice"
    assert "Whisper" in detail or "fails closed" in detail
    assert "test double" not in detail.lower()
    assert "parakeet" not in detail.lower()


def test_preflight_reports_existing_edge_tts_as_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.scripts.preflight import _check_tts

    monkeypatch.setattr(settings, "voice_tts_provider", "edge_tts")
    monkeypatch.setattr(settings, "voice_tts_edge_voice", "en-GB-SoniaNeural")
    kind, name, detail = _check_tts()
    assert kind == "REAL"
    assert name == "edge_tts"
    assert "SoniaNeural" in detail
    assert "test double" not in detail.lower()


@pytest.mark.asyncio
async def test_runtime_health_muse_asr_degraded_without_key_not_base_url_lie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.runtime import _asr_tts_checks

    class MuseTranscriber:
        name = "meta_muse_voice"

    class EdgeSynth:
        name = "edge_tts"

    monkeypatch.setattr(settings, "voice_asr_provider", "meta_muse_voice")
    monkeypatch.setattr(settings, "voice_asr_model", "base")
    monkeypatch.setattr(settings, "muse_voice_model", "muse-voice-transcribe-1.0")
    monkeypatch.setattr(settings, "voice_asr_base_url", None)
    monkeypatch.setattr(settings, "voice_tts_provider", "edge_tts")
    monkeypatch.setattr(settings, "voice_tts_base_url", None)
    monkeypatch.setattr(settings, "meta_model_api_key", "")
    monkeypatch.setenv("META_MODEL_API_KEY", "")
    monkeypatch.setenv("EV_META_MODEL_API_KEY", "")
    monkeypatch.setenv("MODEL_API_KEY", "")
    monkeypatch.setattr("app.voice.asr.get_transcriber", lambda: MuseTranscriber())
    monkeypatch.setattr("app.voice.tts.get_synthesizer", lambda: EdgeSynth())
    checks = await _asr_tts_checks()
    asr = next(c for c in checks if c["name"] == "asr")
    tts = next(c for c in checks if c["name"] == "tts")
    assert asr["status"] == "degraded"
    assert asr["reason"] == "META_MODEL_API_KEY missing"
    assert asr["model"] == "muse-voice-transcribe-1.0"
    assert asr["model"] != "base"
    assert "base_url" not in (asr.get("reason") or "")
    assert tts["status"] == "ok"
    assert tts["provider"] == "edge_tts"
