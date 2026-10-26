"""Brain usage accounting (0.2): every brain call records tokens and cost.

Regression: the kernel and agent loops dropped all provider usage, so
per-turn spend was unmeasurable from inside Evie. Now each kernel brain
call writes an egress-ledger row (ModelCallLog with data class), and both
loops aggregate usage into the turn/run result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.gateway.costs import cached_tokens, summarize_brain_usage
from app.models import ModelCallLog


# --------------------------------------------------------------------------- #
# Pure aggregation
# --------------------------------------------------------------------------- #


def test_cached_tokens_reads_prompt_details_with_fallback() -> None:
    assert cached_tokens({"prompt_tokens_details": {"cached_tokens": 500}}) == 500
    assert cached_tokens({"cached_tokens": 12}) == 12
    assert cached_tokens({}) == 0
    assert cached_tokens(None) == 0
    assert cached_tokens({"prompt_tokens_details": {"cached_tokens": "junk"}}) == 0
    assert cached_tokens({"prompt_tokens_details": {"cached_tokens": -3}}) == 0


def test_summarize_brain_usage_mixes_reported_and_estimated() -> None:
    summary = summarize_brain_usage([
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "cost": 0.00014,
            "prompt_tokens_details": {"cached_tokens": 500},
        },
        {"prompt_tokens": 4000, "completion_tokens": 100},
    ])
    assert summary["brain_calls"] == 2
    assert summary["prompt_tokens"] == 5000
    assert summary["completion_tokens"] == 300
    assert summary["cached_tokens"] == 500
    # call 1 reported $0.00014; call 2 estimated at muse contributor rates
    # ($0.10/$0.20 per 1M): (4000*0.10 + 100*0.20)/1e6 = $0.00042.
    assert summary["cost_usd"] == pytest.approx(0.00056)
    assert summary["cost_source"] == "mixed"


def test_summarize_brain_usage_empty() -> None:
    summary = summarize_brain_usage([])
    assert summary["brain_calls"] == 0
    assert summary["cost_usd"] == 0.0
    assert summary["cost_source"] == "none"


# --------------------------------------------------------------------------- #
# Ledger rows
# --------------------------------------------------------------------------- #


async def test_log_brain_call_writes_egress_ledger_row(db_session: AsyncSession) -> None:
    from app.services.model_call import DATA_CLASS_OWNER_CONVERSATION, log_brain_call

    row = await log_brain_call(
        db_session,
        provider="muse",
        model="meta/muse-spark-1.3-contributor",
        effort="medium",
        usage={
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "cost": 0.000018,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
        latency_ms=812.5,
        actor="master",
        request_id="kernel-test-1",
        data_class=DATA_CLASS_OWNER_CONVERSATION,
        tool_calls=[{"name": "memory.search"}],
    )
    assert row.prompt_tokens == 120
    assert row.completion_tokens == 30
    metadata = row.envelope["metadata"]
    assert metadata["cost_usd"] == pytest.approx(0.000018)
    assert metadata["cost_source"] == "openrouter_reported"
    assert metadata["cached_tokens"] == 40
    assert metadata["reasoning_effort"] == "medium"
    assert metadata["data_class"] == "owner_conversation"
    assert row.tool_calls == [{"name": "memory.search"}]

    stored = (
        await db_session.execute(select(ModelCallLog).where(ModelCallLog.id == row.id))
    ).scalar_one()
    assert stored.request_id == "kernel-test-1"


# --------------------------------------------------------------------------- #
# Kernel loop: usage reaches the turn result and the ledger
# --------------------------------------------------------------------------- #


class _ScriptedBrain:
    def __init__(self, replies: list[ChatResult]) -> None:
        self.replies = list(replies)
        self.reasoning_effort: str | None = None

    async def chat_with_tools(self, messages, specs, **kwargs):
        del messages, specs, kwargs
        return self.replies.pop(0)


@pytest.fixture
def _kernel_mode(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "cognitive_role", "kernel")
    monkeypatch.setattr(settings, "laptop_files", False)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from app.cognitive import telemetry
    from app.cognitive.session_store import reset_for_tests

    reset_for_tests()
    telemetry.reset_for_tests()
    yield
    reset_for_tests()
    telemetry.reset_for_tests()
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_gemini")


async def test_kernel_turn_records_usage_and_ledger_row(
    _kernel_mode, monkeypatch, db_session: AsyncSession
) -> None:
    from app.cognitive import kernel

    brain = _ScriptedBrain([
        ChatResult(
            text="I'm well.",
            model="meta/muse-spark-1.3-contributor",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost": 0.000014,
                "prompt_tokens_details": {"cached_tokens": 10},
            },
        )
    ])
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    monkeypatch.setattr("app.gateway.roles.require_text_provider", lambda: brain)

    result = await kernel.handle_turn(
        transcript="Tell me how you are.",
        modality="voice",
        session=db_session,
    )
    assert result.kind == "brain"
    assert result.usage["brain_calls"] == 1
    assert result.usage["prompt_tokens"] == 100
    assert result.usage["completion_tokens"] == 20
    assert result.usage["cached_tokens"] == 10
    assert result.usage["cost_usd"] == pytest.approx(0.000014)
    assert result.usage["cost_source"] == "openrouter_reported"
    assert result.as_dict()["usage"] == result.usage

    rows = (
        await db_session.execute(select(ModelCallLog))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].prompt_tokens == 100
    assert rows[0].envelope["metadata"]["data_class"] == "owner_conversation"
    assert rows[0].envelope["metadata"]["reasoning_effort"] == "low"


# --------------------------------------------------------------------------- #
# Agent loop: usage accumulates across steps
# --------------------------------------------------------------------------- #


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, turns: list[ChatResult]) -> None:
        self.turns = list(turns)

    async def chat_with_tools(self, messages, tools, **kwargs):
        del messages, tools, kwargs
        return self.turns.pop(0)


async def test_agent_run_accumulates_usage_across_steps(tmp_path: Path) -> None:
    from app.agent.loop import run_agent

    provider = _ScriptedProvider([
        ChatResult(
            text="",
            tool_calls=[ToolCall(id="c1", name="list_dir", arguments={})],
            usage={"prompt_tokens": 500, "completion_tokens": 50},
        ),
        ChatResult(
            text="",
            tool_calls=[
                ToolCall(
                    id="f1",
                    name="finish",
                    arguments={"summary": "listed.", "ok": True},
                )
            ],
            usage={"prompt_tokens": 700, "completion_tokens": 60, "cost": 0.0001},
        ),
    ])
    result = await run_agent("list the folder", root=tmp_path, provider=provider)
    assert result.ok is True
    assert result.usage["brain_calls"] == 2
    assert result.usage["prompt_tokens"] == 1200
    assert result.usage["completion_tokens"] == 110
    assert result.usage["cost_source"] in {"mixed", "openrouter_reported"}
    assert result.to_dict()["usage"] == result.usage


# --------------------------------------------------------------------------- #
# Brain tiers (B1)
# --------------------------------------------------------------------------- #


def test_brain_tiers_default_to_contributor(monkeypatch) -> None:
    from app.gateway.openrouter_muse import (
        BRAIN_TIER_IDS,
        brain_model_for_tier,
        default_brain_model,
    )

    assert settings.brain_tier_personal == "contributor"
    assert settings.brain_tier_generic == "contributor"
    assert default_brain_model() == BRAIN_TIER_IDS["contributor"]
    assert default_brain_model(tier="personal") == BRAIN_TIER_IDS["contributor"]
    assert brain_model_for_tier("standard") == BRAIN_TIER_IDS["standard"]


def test_brain_tier_is_the_failover_switch(monkeypatch) -> None:
    from app.gateway.openrouter_muse import BRAIN_TIER_IDS, default_brain_model

    monkeypatch.setattr(settings, "brain_tier_generic", "standard")
    assert default_brain_model() == BRAIN_TIER_IDS["standard"]
    # The other tier is untouched: per-class failover, not global.
    assert default_brain_model(tier="personal") == BRAIN_TIER_IDS["contributor"]


def test_explicit_brain_model_wins_over_tiers(monkeypatch) -> None:
    from app.gateway.openrouter_muse import default_brain_model

    monkeypatch.setattr(settings, "brain_model", "meta/some-other-listed-id")
    monkeypatch.setattr(settings, "brain_tier_generic", "standard")
    assert default_brain_model() == "meta/some-other-listed-id"


def test_unknown_brain_tier_fails_closed(monkeypatch) -> None:
    from app.gateway.openrouter_muse import BrainUnavailable, default_brain_model

    monkeypatch.setattr(settings, "brain_tier_generic", "ultra")
    try:
        default_brain_model()
    except BrainUnavailable as exc:
        assert "EV_BRAIN_TIER_GENERIC" in str(exc)
    else:
        raise AssertionError("unknown tier must fail closed")


def test_verify_flags_an_unlisted_tier_id(monkeypatch) -> None:
    from clients.cli.models import verify_models

    monkeypatch.setattr(settings, "brain_model", "meta/muse-spark-1.3-contributor")
    monkeypatch.setattr(settings, "brain_tier_generic", "standard")
    monkeypatch.setattr(settings, "gemini_live_model", "gemini-3.8-live-extended-thinking")
    monkeypatch.setattr(settings, "google_api_key", "AIza-test-key-not-real")
    monkeypatch.setattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")

    listing = {
        "data": [
            {
                "id": "meta/muse-spark-1.3-contributor",
                "context_length": 1048576,
                "supported_parameters": ["tools", "reasoning"],
                "architecture": {"output_modalities": ["text"]},
            },
        ]
    }

    def fetch(url: str, headers: dict[str, Any]):
        if "googleapis" in url:
            return 200, {
                "models": [
                    {
                        "name": "models/gemini-3.8-live-extended-thinking",
                        "supportedGenerationMethods": ["bidiGenerateContent"],
                    }
                ]
            }
        return 200, listing

    report = verify_models(fetch=fetch)
    assert report["checks"][0]["status"] == "missing"
    assert "meta/muse-spark-1.3" in report["checks"][0]["detail"]
