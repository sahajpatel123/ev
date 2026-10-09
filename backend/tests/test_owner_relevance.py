"""Owner relevance: capped retrieval boosts + bounded prompt context."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import OwnerModelContext
from app.ev import owner_model as owner_store
from app.filter.input_filter import compile_context_block
from app.memory.owner_relevance import (
    owner_boost_cap,
    owner_boosts_for,
    owner_context_for_prompt,
)
from app.memory.retrieval import SCORE_WEIGHTS, Retriever
from app.models import Event, Memory
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import token_estimate


async def _seed_event(db_session: AsyncSession, text: str = "I always write tests first.") -> Event:
    event = await EventService(db_session, actor="test").create(
        EventCreate(source="test", event_type="note", text=text)
    )
    await db_session.commit()
    return event


async def _seed_owner_row(db_session: AsyncSession, kind: str, text: str, **kwargs):
    event = await _seed_event(db_session)
    row = await owner_store.write_owner_row(
        db_session, kind, text, source_event_id=event.id, **kwargs
    )
    await db_session.commit()
    return row


async def _seed_memory(
    db_session: AsyncSession, text: str, memory_type: str = "fact", importance: float = 0.5
) -> Memory:
    memory = Memory(
        memory_type=memory_type,
        text=text,
        importance=importance,
        fingerprint=f"test-{uuid4().hex}",
        privacy_level="normal",
        source_type="explicit",
    )
    db_session.add(memory)
    await db_session.commit()
    return memory


async def _grant_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/training/consent", json={"track": "life_data_personalization"}
    )
    assert resp.status_code == 201, resp.text


def _user_state_stub() -> SimpleNamespace:
    return SimpleNamespace(
        activity="testing",
        active_project="owner-memory",
        active_goal="ship",
        current_task="phase3",
        recent_topics=["memory"],
        open_decisions=[],
    )


async def test_boost_empty_without_consent(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    await _seed_owner_row(db_session, "trait", "concise summaries standup")
    memory = await _seed_memory(db_session, "Write concise summaries for standup")
    assert await owner_boosts_for(db_session, [memory]) == {}


async def test_boost_empty_when_disabled(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    assert settings.owner_model_enabled is False
    await _seed_owner_row(db_session, "trait", "concise summaries standup")
    await _grant_consent(client)
    memory = await _seed_memory(db_session, "Write concise summaries for standup")
    assert await owner_boosts_for(db_session, [memory]) == {}


async def test_boost_applies_only_on_token_overlap(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    await _grant_consent(client)
    await _seed_owner_row(db_session, "trait", "concise summaries standup")
    matching = await _seed_memory(db_session, "Write concise summaries for standup")
    other = await _seed_memory(db_session, "Zebra xylophone quantum flux capacitor")
    boosts = await owner_boosts_for(db_session, [matching, other])
    assert 1.0 < boosts[matching.id] <= 1.2
    assert other.id not in boosts


async def test_boost_max_clamped_to_1_2(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    monkeypatch.setattr(settings, "owner_retrieval_boost_max", 2.0)
    assert owner_boost_cap() == 1.2
    await _grant_consent(client)
    await _seed_owner_row(db_session, "value", "alpha beta gamma")
    memory = await _seed_memory(db_session, "alpha beta gamma")
    boosts = await owner_boosts_for(db_session, [memory])
    assert boosts[memory.id] == 1.2


async def test_boost_max_1_disables(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    monkeypatch.setattr(settings, "owner_retrieval_boost_max", 1.0)
    await _grant_consent(client)
    await _seed_owner_row(db_session, "value", "alpha beta gamma")
    memory = await _seed_memory(db_session, "alpha beta gamma")
    assert await owner_boosts_for(db_session, [memory]) == {}


def test_score_weights_sum_unchanged() -> None:
    weighted = ["semantic", "keyword", "recency", "importance", "relationship", "confidence"]
    assert abs(sum(SCORE_WEIGHTS[k] for k in weighted) - 1.0) < 1e-9
    assert SCORE_WEIGHTS["semantic"] == 0.35
    assert SCORE_WEIGHTS["importance"] == 0.15


async def test_search_scores_identical_without_consent(db_session: AsyncSession) -> None:
    memory = await _seed_memory(db_session, "Write concise summaries for standup")
    before = await Retriever(db_session).search("standup summary", k=5)
    assert before
    assert before[0].components["owner_boost"] == 1.0
    # Adding owner rows cannot move scores while consent is absent.
    await _seed_owner_row(db_session, "trait", "concise summaries standup")
    after = await Retriever(db_session).search("standup summary", k=5)
    assert [r.memory_id for r in after] == [r.memory_id for r in before]
    assert [r.score for r in after] == [r.score for r in before]
    assert memory.id is not None


async def test_search_boost_lifts_matching_memory(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    await _seed_owner_row(db_session, "trait", "concise summaries standup")
    await _seed_memory(db_session, "Write concise summaries for standup")
    plain = await Retriever(db_session).search("standup summary", k=5)
    await _grant_consent(client)
    boosted = await Retriever(db_session).search("standup summary", k=5)
    assert boosted[0].memory_id == plain[0].memory_id
    assert boosted[0].components["owner_boost"] > 1.0
    assert boosted[0].score > plain[0].score


async def test_owner_context_none_when_gated(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    await _seed_owner_row(db_session, "trait", "concise summaries")
    assert await owner_context_for_prompt(db_session) is None
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    assert await owner_context_for_prompt(db_session) is None


async def test_owner_context_builds_and_filters_sensitive(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    await _grant_consent(client)
    await _seed_owner_row(db_session, "trait", "concise summaries")
    await _seed_owner_row(db_session, "value", "secret ballot pick", privacy_level="sensitive")
    context = await owner_context_for_prompt(db_session)
    assert context is not None
    assert context.traits == ["concise summaries"]
    assert context.values == []


def test_compile_context_block_owner_section_bounded(monkeypatch) -> None:
    context = OwnerModelContext(
        traits=["concise summaries", "morning deep work"] * 20,
        values=["craft over speed"],
        thinking_style=["decide by writing pros and cons"],
        state_label="focus: deep work",
    )
    text, used = compile_context_block(
        strategy_text="STRATEGY",
        user_state=_user_state_stub(),
        memories=[],
        budget=400,
        owner_model=context,
    )
    assert "OWNER MODEL" in text
    assert used <= 400
    owner_lines = [line for line in text.splitlines() if line.startswith(("-", "OWNER"))]
    assert sum(token_estimate(line) for line in owner_lines) <= settings.owner_context_tokens


def test_compile_context_block_without_owner_model_byte_identical() -> None:
    kwargs: dict = {
        "strategy_text": "STRATEGY",
        "user_state": _user_state_stub(),
        "memories": [],
        "budget": 400,
    }
    text_default, used_default = compile_context_block(**kwargs)
    text_none, used_none = compile_context_block(**kwargs, owner_model=None)
    assert text_default == text_none
    assert used_default == used_none
    assert "OWNER MODEL" not in text_default


def test_compile_context_block_zero_cap_removes_section(monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_context_tokens", 0)
    context = OwnerModelContext(traits=["concise summaries"])
    text, _ = compile_context_block(
        strategy_text="STRATEGY",
        user_state=_user_state_stub(),
        memories=[],
        budget=400,
        owner_model=context,
    )
    assert "OWNER MODEL" not in text
