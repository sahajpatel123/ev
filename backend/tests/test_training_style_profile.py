"""Tests for the deterministic style-profile adapter (Training track 7.3)."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.interaction import InteractionStrategy
from app.filter.output_filter import enforce_persona
from app.models import AdapterRegistration, ResponseLog
from app.training.adapter import active_style_profile
from app.training.style_adapter import build_style_profile


def _strategy() -> InteractionStrategy:
    return InteractionStrategy(
        mode="casual",
        intent="action",
        urgency=0.5,
        emotional_state="neutral",
        length_target="one to two sentences",
        directness="direct",
        assertiveness=2,
        ask_question=False,
        challenge=False,
        rationale="test",
    )


def test_style_profile_is_deterministic_and_evidence_derived() -> None:
    entries = [
        {
            "kind": "response",
            "role": "assistant",
            "text": "I think maybe the plan could work if we try later.",
            "source": "response_log:r1",
            "signals": {"mode": "casual", "was_correction": True},
        },
        {
            "kind": "response",
            "role": "assistant",
            "text": "Do this now: review the checklist.",
            "source": "response_log:r2",
            "signals": {"mode": "casual", "was_correction": False, "was_useful": True},
        },
        {
            "kind": "filter",
            "role": "assistant",
            "text": "Based on your memory from March, the decision was made.",
            "source": "filter_ledger:f1",
            "signals": {"stage": "output"},
        },
    ]

    first = build_style_profile(entries)
    second = build_style_profile(entries[::-1])

    assert first["schema_version"] == "ev.adapter.style_profile.v1"
    assert first["signal_coverage"]["rated"] == 2
    assert first["signal_coverage"]["assistant"] == 3
    assert first["signal_coverage"]["corrected"] == 1
    assert first["prefer_direct"] is True
    assert first["prefer_citations"] is True
    assert first["word_count_targets"] == {"casual": 6}
    assert first["profile_hash"] == second["profile_hash"]
    assert "response_log:r1" not in str(first)


async def test_adapter_registration_stores_profile_and_consent_gates_application(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await client.post("/v1/training/consent", json={"track": "training_corpus"})
    db_session.add(
        ResponseLog(
            request_text="Shorten it and stop hedging.",
            reply_text="I think maybe we should consider the plan.",
            mode="casual",
            strategy={},
            provenance_ids=[],
            context_tokens=10,
            was_correction=True,
        )
    )
    db_session.add(
        ResponseLog(
            request_text="Give me the steps.",
            reply_text="Review the checklist, then approve.",
            mode="casual",
            strategy={},
            provenance_ids=[],
            context_tokens=10,
            was_useful=True,
        )
    )
    await db_session.commit()
    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 201, resp.text
    version = resp.json()["snapshot"]["version"]

    await client.post("/v1/training/consent", json={"track": "adapter_fine_tuning"})
    resp = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-style-v1", "corpus_version": version},
    )
    assert resp.status_code == 201, resp.text
    registered = resp.json()
    profile = registered["eval_metrics"]["profile"]
    assert profile["signal_coverage"]["rated"] == 2
    assert profile["prefer_direct"] is True
    assert profile["profile_hash"] == registered["eval_metrics"]["profile_hash"]

    # Not applied until explicitly activated.
    assert await active_style_profile(db_session) is None
    resp = await client.post(
        "/v1/training/adapter/activate",
        json={"adapter_id": registered["id"], "reason": "apply learned style"},
    )
    assert resp.status_code == 200, resp.text

    active = await active_style_profile(db_session)
    assert active is not None
    assert active["profile_hash"] == profile["profile_hash"]

    # Revoking consent immediately stops application.
    resp = await client.post(
        "/v1/training/consent/adapter_fine_tuning/revoke",
        json={"reason": "privacy"},
    )
    assert resp.status_code == 200, resp.text
    assert await active_style_profile(db_session) is None

    rows = list(
        (
            await db_session.execute(
                select(AdapterRegistration).where(AdapterRegistration.name == "evie-style-v1")
            )
        )
        .scalars()
        .all()
    )
    assert rows and rows[0].eval_metrics["profile"]["profile_hash"]


def test_enforce_persona_applies_style_profile() -> None:
    profile = {
        "word_count_targets": {"casual": 8},
        "prefer_citations": True,
        "prefer_bullets": True,
        "prefer_direct": True,
        "profile_hash": "test-hash",
    }
    text = (
        "Maybe this is a very long draft sentence that keeps going and going and "
        "going and going without ever stopping, and there is no citation at all."
    )
    final, persona, _ = enforce_persona(text, _strategy(), style_profile=profile)

    assert persona["style_profile_applied"] is True
    assert persona["citation_preferred"] is True
    assert persona["bullets_preferred"] is True
    assert persona["hedging_present"] is True
    assert len(final.split()) <= 10
    assert persona.get("length_trimmed") is True

    # Without a profile the existing behavior is unchanged.
    _, default_persona, _ = enforce_persona(text, _strategy())
    assert default_persona.get("style_profile_applied") is None
