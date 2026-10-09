"""Owner on every turn: chat + kernel compilers, wiring, history backfill."""

from __future__ import annotations

from types import SimpleNamespace

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.cognitive.context import compile_context
from app.cognitive.session_store import CognitiveSession
from app.config import settings
from app.context.compiler import ContextCompiler
from app.ev import owner_model as owner_store
from app.memory.owner_relevance import owner_context_for_prompt
from app.models import Event
from app.schemas import EventCreate
from app.scripts.owner_backfill import run_backfill
from app.services.event_service import EventService


async def _seed_event(
    db_session: AsyncSession, text: str, event_type: str = "note"
) -> Event:
    event = await EventService(db_session, actor="test").create(
        EventCreate(source="test", event_type=event_type, text=text)
    )
    await db_session.commit()
    return event


async def _seed_owner_row(db_session: AsyncSession, kind: str, text: str):
    event = await _seed_event(db_session, "seed")
    row = await owner_store.write_owner_row(
        db_session, kind, text, source_event_id=event.id
    )
    await db_session.commit()
    return row


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
        current_task="turns",
        recent_topics=["memory"],
        open_decisions=[],
        live_context=[],
        entity_mentions=[],
    )


def _cognition_stub() -> CognitiveSession:
    return CognitiveSession(session_id="test-session")


async def test_chat_compiler_includes_owner_snapshot(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    await _grant_consent(client)
    await _seed_owner_row(db_session, "trait", "concise summaries")
    await _seed_owner_row(db_session, "value", "craft over speed")
    snapshot = await owner_context_for_prompt(db_session)
    assert snapshot is not None

    plan = ContextCompiler().compile(
        memories=[],
        user_state=_user_state_stub(),
        strategy_text="STRATEGY",
        budget=4000,
        owner_model=snapshot,
    )
    assert "OWNER MODEL" in plan.text
    assert "concise summaries" in plan.text
    assert "craft over speed" in plan.text
    assert plan.used_tokens <= 4000
    assert any(s.name == "owner_model" and s.items_included > 0 for s in plan.sections)

    bare = ContextCompiler().compile(
        memories=[],
        user_state=_user_state_stub(),
        strategy_text="STRATEGY",
        budget=4000,
    )
    assert "OWNER MODEL" not in bare.text
    assert all(s.name != "owner_model" for s in bare.sections)


async def test_chat_compiler_owner_section_bounded(monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_context_tokens", 40)
    plan = ContextCompiler().compile(
        memories=[],
        user_state=_user_state_stub(),
        strategy_text="STRATEGY",
        budget=4000,
        owner_model=SimpleNamespace(
            traits=["a much longer trait statement about working style"] * 10,
            values=[],
            thinking_style=[],
            state_label=None,
        ),
    )
    section = next(s for s in plan.sections if s.name == "owner_model")
    assert section.tokens <= 40
    assert section.items_dropped > 0
    assert section.truncated is True
    assert plan.used_tokens <= 4000


async def test_kernel_context_includes_owner_snapshot(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    await _grant_consent(client)
    await _seed_owner_row(db_session, "thinking_style", "decide by writing")
    snapshot = await owner_context_for_prompt(db_session)
    assert snapshot is not None

    text = compile_context(
        transcript="hello",
        modality="text",
        device_id=None,
        cognition=_cognition_stub(),
        owner_model=snapshot,
    )
    assert "OWNER MODEL" in text
    assert "decide by writing" in text

    bare = compile_context(
        transcript="hello",
        modality="text",
        device_id=None,
        cognition=_cognition_stub(),
    )
    assert "OWNER MODEL" not in bare


async def test_chat_turn_fetches_owner_snapshot(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    await _grant_consent(client)
    await _seed_owner_row(db_session, "trait", "concise summaries")

    calls: list = []
    import app.memory.owner_relevance as relevance

    real_fetch = relevance.owner_context_for_prompt

    async def _recording(session):
        calls.append(session)
        return await real_fetch(session)

    monkeypatch.setattr(relevance, "owner_context_for_prompt", _recording)
    resp = await client.post("/v1/chat", json={"message": "hello there", "stream": False})
    assert resp.status_code == 200, resp.text
    assert calls, "chat turn must fetch the owner snapshot"


async def test_chat_survives_owner_fetch_failure(
    client: AsyncClient, monkeypatch
) -> None:
    import app.memory.owner_relevance as relevance

    async def _boom(session):
        raise RuntimeError("owner store down")

    monkeypatch.setattr(relevance, "owner_context_for_prompt", _boom)
    resp = await client.post("/v1/chat", json={"message": "hello there", "stream": False})
    assert resp.status_code == 200, resp.text


async def test_kernel_turn_passes_owner_snapshot(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, tmp_path
) -> None:
    import app.cognitive.kernel as kernel
    from app.cognitive.session_store import reset_for_tests
    from app.contracts import ChatResult

    monkeypatch.setattr(settings, "owner_model_enabled", True)
    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "cognitive_role", "kernel")
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()
    await _grant_consent(client)
    await _seed_owner_row(db_session, "trait", "concise summaries")

    async def _scripted(messages, specs, *, model=None, temperature=0.7, **kwargs):
        return ChatResult(text="Noted, keeping it brief.")

    scripted = SimpleNamespace(chat_with_tools=_scripted)
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    monkeypatch.setattr("app.gateway.roles.require_text_provider", lambda: scripted)

    seen: dict = {}
    real_compile = kernel.compile_context

    def _recording(*args, **kwargs):
        seen["owner_model"] = kwargs.get("owner_model")
        return real_compile(*args, **kwargs)

    monkeypatch.setattr(kernel, "compile_context", _recording)
    try:
        result = await kernel.handle_turn(
            transcript="hello there, how are you doing today?",
            modality="text",
            session=db_session,
        )
    finally:
        reset_for_tests()
    assert result.unavailable is False
    assert seen.get("owner_model") is not None
    assert seen["owner_model"].traits == ["concise summaries"]


async def test_backfill_dry_run_writes_nothing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, capsys
) -> None:
    await _seed_event(db_session, "I always write tests first.")
    await _seed_event(db_session, "I believe in craft over speed.")
    code = await run_backfill(apply=False, limit=500, batch=50, after_id=None)
    assert code == 0
    assert await owner_store.current_owner_rows(db_session, "trait") == []
    assert await owner_store.current_owner_rows(db_session, "value") == []
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "candidates=2" in out


async def test_backfill_apply_requires_consent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_event(db_session, "I always write tests first.")
    code = await run_backfill(apply=True, limit=500, batch=50, after_id=None)
    assert code == 2
    assert await owner_store.current_owner_rows(db_session, "trait") == []


async def test_backfill_apply_writes_versioned_rows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _grant_consent(client)
    await _seed_event(db_session, "I always write tests first.")
    await _seed_event(db_session, "I believe in craft over speed.")
    code = await run_backfill(apply=True, limit=500, batch=50, after_id=None)
    assert code == 0
    traits = await owner_store.current_owner_rows(db_session, "trait")
    values = await owner_store.current_owner_rows(db_session, "value")
    assert len(traits) == 1
    assert len(values) == 1
    assert traits[0].source_type == "explicit"
    # Re-runs are safe: fingerprint dedupe, no duplicate rows.
    code = await run_backfill(apply=True, limit=500, batch=50, after_id=None)
    assert code == 0
    assert len(await owner_store.current_owner_rows(db_session, "trait")) == 1
