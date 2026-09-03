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
                '{"type":"transcript","transcript":"Open Calc","final":false}',
                '{"type":"transcript","transcript":"Open Calculator","final":true}',
                '{"type":"speechEnd","transcript":"Open Calculator"}',
                '{"type":"speaker","speaker":"A"}',
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

    await session._receive(_WS())
    assert partials == ["Open Calc"]
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
                '{"type":"speechEnd","transcript":"Open Calculator"}',
                '{"type":"transcript","transcript":"Open Calculator","final":true}',
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
