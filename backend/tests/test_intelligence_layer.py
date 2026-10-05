"""MiMo intelligence layer (EV_INTELLIGENCE_LAYER=mimo).

Additive observer: after each completed spoken turn, MiMo rates the reply
(bluff / filler / steer) and the bridge records the verdict in voice
health. Never on the audio path; never blocks or rewrites speech. These
tests pin that contract offline (no network).
"""

from __future__ import annotations

import json

import pytest

from app.config import settings


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _bridge() -> object:
    from app.voice.live.gemini_live import GeminiLiveBridge

    async def on_event(_event) -> None:
        return None

    async def connect(*_a, **_k):
        return _FakeWS()

    return GeminiLiveBridge(
        on_event=on_event,
        api_key="k",
        provider="gemini",
        connect=connect,
    )


@pytest.mark.asyncio
async def test_judge_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "intelligence_layer", "")
    bridge = _bridge()
    out = await bridge.intelligence_judge_review("hello", "Hey! Good to hear you.")
    assert out == {"skipped": "disabled"}
    assert bridge._voice_health["intelligence_judge_runs"] == 0


@pytest.mark.asyncio
async def test_judge_scores_bluff(monkeypatch) -> None:
    monkeypatch.setattr(settings, "intelligence_layer", "mimo")
    bridge = _bridge()

    class _FakeProvider:
        async def chat(self, _messages, **_kwargs):
            class _R:
                text = '{"verdict": "bluff", "why": "promised follow-through for unstarted work"}'

            return _R()

    class _Factory:
        def __init__(self) -> None:
            self._provider = _FakeProvider()

        def _credential(self) -> str:
            return "k"

        async def chat(self, messages, **kwargs):
            return await self._provider.chat(messages, **kwargs)

    import app.voice.live.gemini_live as gv

    monkeypatch.setattr(gv, "_judge_provider", lambda: _Factory())
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    out = await bridge.intelligence_judge_review(
        "I am going to start a coding project and you have to perform well.",
        "Yes, I'm ready. Tell me what you want. I will get back to you after getting some project details.",
    )
    assert out["verdict"] == "bluff"
    assert bridge._voice_health["intelligence_judge_runs"] == 1
    assert bridge._voice_health["intelligence_judge_replies"] == 1
    assert bridge._voice_health["intelligence_judge_failures"] == 0


@pytest.mark.asyncio
async def test_judge_failure_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(settings, "intelligence_layer", "mimo")
    bridge = _bridge()

    class _Boom:
        def __init__(self) -> None:
            pass

        def _credential(self) -> str:
            return "k"

        async def chat(self, _messages, **_kwargs):
            raise RuntimeError("network down")

    import app.voice.live.gemini_live as gv

    monkeypatch.setattr(gv, "_judge_provider", lambda: _Boom())
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    out = await bridge.intelligence_judge_review("hi", "hey")
    assert out["verdict"] == "error"
    assert bridge._voice_health["intelligence_judge_failures"] == 1


@pytest.mark.asyncio
async def test_judge_unparseable_output_recorded(monkeypatch) -> None:
    monkeypatch.setattr(settings, "intelligence_layer", "mimo")
    bridge = _bridge()

    class _Rambler:
        def __init__(self) -> None:
            pass

        def _credential(self) -> str:
            return "k"

        async def chat(self, _messages, **_kwargs):
            class _R:
                text = "I think this reply was fine, no JSON here."

            return _R()

    import app.voice.live.gemini_live as gv

    monkeypatch.setattr(gv, "_judge_provider", lambda: _Rambler())
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    out = await bridge.intelligence_judge_review("hi", "hey")
    assert out["verdict"] == "unparseable"
    assert bridge._voice_health["intelligence_judge_failures"] == 1
