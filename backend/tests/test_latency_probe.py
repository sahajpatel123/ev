"""WP12: the latency probe counts honestly (no key -> no number, no reflex average)."""

from __future__ import annotations

import pytest


def test_percentile_math() -> None:
    from app.scripts.latency_probe import _percentile

    assert _percentile([], 95) == 0.0
    assert _percentile([10.0], 95) == 10.0
    values = [float(i) for i in range(1, 21)]
    # Nearest-rank: p50 of 1..20 is the 11th value, p95 the 19th.
    assert _percentile(values, 50) == 11.0
    assert _percentile(values, 95) == 19.0


def test_missing_key_reports_owner_action(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scripts import latency_probe as probe_mod

    monkeypatch.setattr(probe_mod, "_load_env",
                        lambda: {"EV_CHAT_PROVIDER": "deepseek",
                                 "EV_OPENROUTER_API_KEY": ""})
    out = probe_mod.probe(25, "hello")
    assert out["verdict"] == "OWNER-ACTION"
    assert out["samples_ms"] == []
    assert "EV_OPENROUTER_API_KEY" in out["blocker"]
    # A run without a provider must never render a latency number.
    assert "p50" not in probe_mod.render(out)


async def test_reflex_and_failed_turns_are_not_counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Only real model turns are averaged; reflexes/failures are reported."""
    from app.cognitive import telemetry
    from app.cognitive.kernel import KernelResult
    from app.scripts import latency_probe as probe_mod

    monkeypatch.setattr("app.scripts.latency_probe.PROOF_ROOT", tmp_path)
    calls = {"n": 0}

    async def fake_handle_turn(**kwargs):
        calls["n"] += 1
        if calls["n"] % 3 == 1:  # every first of three is a reflex
            return KernelResult(spoken="It is noon.", kind="reflex")
        if calls["n"] % 3 == 2:  # a provider failure
            telemetry.inc("provider_failures")
            return KernelResult(spoken="", kind="unavailable", unavailable=True)
        telemetry.inc("mimo_turns")
        return KernelResult(spoken="Red, green, blue.", kind="mimo")

    monkeypatch.setattr("app.cognitive.kernel.handle_turn", fake_handle_turn)

    out = await probe_mod._run_trials(3, "name three colours")
    assert out["kinds"] == {"reflex": 1, "unavailable": 1, "mimo": 1}
    assert out["failures"] == 2
    assert len(out["samples_ms"]) == 1


async def test_first_token_is_the_first_text_chunk() -> None:
    """A later chunk must not be what the probe calls the first token."""
    from app.gateway.streaming import ChatStreamChunk
    from app.scripts.latency_probe import ACK_BUDGET_MS, _time_first_token

    assert ACK_BUDGET_MS == 1500

    class _Provider:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def stream_chat(self, messages, **kwargs):
            del messages, kwargs
            self.sent.append("empty")
            yield ChatStreamChunk(text="")
            self.sent.append("red")
            yield ChatStreamChunk(text="red")
            self.sent.append("later")
            yield ChatStreamChunk(text=" green", done=True)

    provider = _Provider()
    sample = await _time_first_token(provider, "name three colours")
    assert sample is not None and sample >= 0
    # Stopped on the first text chunk. The later chunk was not consumed.
    assert provider.sent == ["empty", "red"]


def test_threshold_is_not_silently_relaxed() -> None:
    """The probe floor stays 20 trials; asking for fewer runs at the floor."""
    from app.scripts import latency_probe as probe_mod

    seen: dict = {}

    real_probe = probe_mod.probe

    def spy(trials, text, **kwargs):
        del text, kwargs
        seen["trials"] = trials
        return {"verdict": "OWNER-ACTION", "provider": "", "trials_requested": trials,
                "started": "", "samples_ms": [], "blocker": "no key"}

    probe_mod.probe = spy
    try:
        probe_mod.main(["--stdout", "--trials", "3"])
    finally:
        probe_mod.probe = real_probe
    assert seen["trials"] == probe_mod.MIN_TRIALS == 20
