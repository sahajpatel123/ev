"""Tests for the deterministic style-profile adapter (Training track 7.3)."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ChatResult
from app.ev.interaction import InteractionStrategy
from app.filter.envelope import SpeakerIdentity
from app.filter.output_filter import enforce_persona
from app.filter.pipeline import run_full_filter_pipeline
from app.gateway.providers import MockProvider
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


class _HedgedProvider(MockProvider):
    """Returns a long hedged draft so the style profile has something to fix."""

    async def chat(self, messages, *, model=None, temperature=0.7) -> ChatResult:
        return ChatResult(
            text=(
                "Maybe the plan could work if we try later, and there is "
                "absolutely no citation here at all."
            ),
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            model=model or self.model,
        )


async def _seed_style_adapter(client: AsyncClient, db_session: AsyncSession) -> dict:
    """Seed a correction+useful corpus and return the registered adapter."""

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
    registered = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-style-draft", "corpus_version": version},
    )
    assert registered.status_code == 201, registered.text
    return registered.json()


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


async def test_pipeline_pre_post_adapter_responses(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The same draft differs before vs after an adapter is activated."""

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
    registered = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-style-pipeline", "corpus_version": version},
    )
    assert registered.status_code == 201, registered.text

    provider = _HedgedProvider()
    speaker = SpeakerIdentity(actor_id="master", verified=True)
    pre = await run_full_filter_pipeline(
        db_session,
        message="Hello!",
        provider=provider,
        speaker=speaker,
    )
    assert pre.style_profile is None
    assert len(pre.final_text.split()) > 10

    activated = await client.post(
        "/v1/training/adapter/activate",
        json={"adapter_id": registered.json()["id"], "reason": "apply learned style"},
    )
    assert activated.status_code == 200, activated.text

    post = await run_full_filter_pipeline(
        db_session,
        message="Hello!",
        provider=provider,
        speaker=speaker,
    )
    assert post.style_profile is not None
    assert post.final_text != pre.final_text
    assert len(post.final_text.split()) <= 10


async def test_filter_evaluate_draft_replay_applies_style_profile(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The draft-replay surface applies the active profile before activation
    is not applied; after activation the same draft is styled differently."""

    draft = (
        "Maybe the plan could work if we try later, and there is "
        "absolutely no citation here at all."
    )
    registered = await _seed_style_adapter(client, db_session)

    before = await client.post(
        "/v1/filter/evaluate",
        json={"message": "Hello!", "draft": draft},
    )
    assert before.status_code == 200, before.text
    assert (before.json()["output"]["persona"] or {}).get("style_profile_applied") is None

    activated = await client.post(
        "/v1/training/adapter/activate",
        json={"adapter_id": registered["id"], "reason": "apply learned style"},
    )
    assert activated.status_code == 200, activated.text

    after = await client.post(
        "/v1/filter/evaluate",
        json={"message": "Hello!", "draft": draft},
    )
    assert after.status_code == 200, after.text
    persona = after.json()["output"]["persona"]
    assert persona["style_profile_applied"] is True
    assert persona.get("length_trimmed") is True
    assert after.json()["output"]["final_text"] != draft
