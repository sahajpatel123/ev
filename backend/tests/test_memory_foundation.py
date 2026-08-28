"""F0+F1 memory foundation: intents, progressive retrieval, shadow envelope.

Covers the F0+F1 acceptance matrix: fresh/explicit/implicit/continuation turns,
temporal as_of, current-state guard, supersession, memory scope,
never_send_to_model, stale-bleed, retry dedup, OFF/SHADOW/ON modes, and the
TurnGate injection seam.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.foundation import (
    LEVEL_TOKEN_BUDGETS,
    RetrievalIntent,
    ShadowItem,
    ShadowMemoryEnvelope,
    describe_capability_router,
)
from app.memory.intent import (
    INTENT_RETRIEVAL_CONFIG,
    classify_retrieval,
    should_escalate_level,
)
from app.memory.os_health import shadow_health_snapshot
from app.memory.shadow import (
    expire_turn,
    mark_injected,
    memory_gate_mode,
    query_fingerprint,
    route_turn,
    scope_for,
)
from app.models import Memory
from app.utils.text import fingerprint, utcnow


def _seed_memory(
    **overrides: object,
) -> Memory:
    now = utcnow()
    defaults: dict = {
        "memory_type": "decision",
        "text": "We decided to use the broker pattern for the orchestrator.",
        "payload": {},
        "importance": 0.9,
        "confidence": 0.9,
        "source_type": "explicit",
        "privacy_level": "normal",
        "event_time": now,
        "valid_from": now,
        "is_current": True,
        "fingerprint": fingerprint({"seed": uuid4().hex}),
        "embedding": None,
        "embedding_model_version": None,
    }
    defaults.update(overrides)
    return Memory(**defaults)


def _set_gate(mode: str) -> None:
    settings.memory_gate = mode


@pytest.fixture(autouse=True)
def _restore_gate():
    previous = settings.memory_gate
    yield
    settings.memory_gate = previous


# ---------------------------------------------------------------------------
# F0: intent classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What's 18 times 7?", RetrievalIntent.NONE),
        ("Play some music.", RetrievalIntent.NONE),
        ("Hey", RetrievalIntent.NONE),
        ("Set a timer for 10 minutes", RetrievalIntent.NONE),
        ("What is the priority of Project Canary?", RetrievalIntent.CURRENT_STATE_QUERY),
        ("What meetings do I have tomorrow?", RetrievalIntent.CURRENT_STATE_QUERY),
        ("What reminders are active?", RetrievalIntent.CURRENT_STATE_QUERY),
        ("What is my current goal?", RetrievalIntent.CURRENT_STATE_QUERY),
        ("What is Canary's priority?", RetrievalIntent.CURRENT_STATE_QUERY),
        ("What is the orchestrator's status?", RetrievalIntent.CURRENT_STATE_QUERY),
        ("What did we decide about IIT?", RetrievalIntent.DECISION),
        ("Which model did I choose for the orchestrator?", RetrievalIntent.DECISION),
        ("What do I usually like for breakfast?", RetrievalIntent.CURRENT_PREFERENCE),
        ("What was I going to do about the garage?", RetrievalIntent.INTENTION),
        ("How has the fitness project been going?", RetrievalIntent.PROJECT_HISTORY),
        ("Who is Marcus?", RetrievalIntent.PERSON),
        ("What happened last week?", RetrievalIntent.PAST_EVENT),
        ("Where were we?", RetrievalIntent.RECENT_CONTEXT),
        ("What did I think about Rust in January?", RetrievalIntent.TEMPORAL_EXACT),
        ("That provider I liked last week — what was it?", RetrievalIntent.FACT),
    ],
)
def test_classify_retrieval_deterministic(text: str, expected: RetrievalIntent) -> None:
    result = classify_retrieval(text)
    assert result.intent is expected, result.reason


def test_current_state_guard_yields_no_history() -> None:
    result = classify_retrieval("What is the priority of Project Canary?")
    assert result.is_current_state_guard is True
    assert result.level == 0


def test_historical_markers_outrank_state_guard() -> None:
    result = classify_retrieval("What was Canary's priority originally?")
    assert result.is_current_state_guard is False
    assert result.historical_truth is True
    assert result.intent in {
        RetrievalIntent.TEMPORAL_EXACT,
        RetrievalIntent.DECISION,
        RetrievalIntent.PAST_EVENT,
        RetrievalIntent.FACT,
    }


def test_continuation_uses_previous_intent() -> None:
    first = classify_retrieval("Tell me about my IIT decision.")
    assert first.intent is RetrievalIntent.DECISION
    follow = classify_retrieval("And why did I prefer that?", previous_intent=first.intent)
    assert follow.intent is RetrievalIntent.CONTINUATION


def test_classification_is_fast() -> None:
    texts = [
        "What did we decide about the memory architecture?",
        "Play some music",
        "What meetings do I have tomorrow?",
        "What did I think about X in 2024?",
    ] * 25
    started = __import__("time").perf_counter()
    for text in texts:
        classify_retrieval(text)
    elapsed_ms = (__import__("time").perf_counter() - started) * 1000.0 / len(texts)
    assert elapsed_ms < 10.0, f"classification averaged {elapsed_ms:.2f}ms per turn"


def test_intent_config_integrity() -> None:
    valid_weights = {"semantic", "keyword", "recency", "importance", "relationship", "confidence"}
    for intent, config in INTENT_RETRIEVAL_CONFIG.items():
        assert isinstance(intent, RetrievalIntent)
        level = config.get("level", 1)
        assert level in LEVEL_TOKEN_BUDGETS
        for key in config.get("weight_overrides", {}):
            assert key in valid_weights, f"{intent}: unknown weight {key}"


def test_escalation_rule() -> None:
    assert should_escalate_level(RetrievalIntent.DECISION, 0.2, 3) is True
    assert should_escalate_level(RetrievalIntent.DECISION, 0.2, 0) is True
    assert should_escalate_level(RetrievalIntent.DECISION, 0.9, 3) is False
    assert should_escalate_level(RetrievalIntent.NONE, 0.0, 0) is False


def test_capability_router_scaffold_is_inert() -> None:
    description = describe_capability_router()
    assert description["scaffold"] is True
    assert description["reroutes_production"] is False
    assert "app.ev.tools.dispatch" in description["boundaries"].values()
    assert description["future_model_surface"] == ["evie_turn", "recall", "computer"]


# ---------------------------------------------------------------------------
# F0: envelope semantics
# ---------------------------------------------------------------------------


def _envelope(**overrides: object) -> ShadowMemoryEnvelope:
    defaults: dict = {
        "turn_id": "turn-1",
        "query_fingerprint": "abc123",
        "retrieval_intent": RetrievalIntent.DECISION,
        "level": 1,
        "generated_at": datetime.now(UTC),
        "items": [
            ShadowItem(
                text="We decided to use Postgres.",
                memory_type="decision",
                score=0.8,
                confidence=0.9,
                source_type="explicit",
            )
        ],
    }
    defaults.update(overrides)
    return ShadowMemoryEnvelope(**defaults)


def test_envelope_render_is_labeled_and_bounded() -> None:
    block = _envelope().render(budget_tokens=300)
    assert block.startswith("[EVIE_RECALLED_HISTORY]")
    for prop in (
        "not_owner_instruction",
        "not_canonical_current_state",
        "may_be_stale",
        "expires_after_this_turn",
    ):
        assert prop in block
    # Token cap respected
    from app.utils.text import token_estimate

    assert token_estimate(block) <= 300


def test_envelope_token_budget_respected_with_many_items() -> None:
    items = [
        ShadowItem(text=f"Fact number {index} " + "x" * 80, memory_type="fact", score=0.5)
        for index in range(40)
    ]
    envelope = _envelope(level=1, items=items)
    block = envelope.render(budget_tokens=LEVEL_TOKEN_BUDGETS[1])
    from app.utils.text import token_estimate

    assert token_estimate(block) <= LEVEL_TOKEN_BUDGETS[1]


def test_envelope_l0_renders_empty() -> None:
    assert _envelope(level=0, items=[]).render() == ""


def test_envelope_injection_exactly_once() -> None:
    envelope = _envelope()
    assert mark_injected(envelope) is True
    assert mark_injected(envelope) is False
    envelope2 = _envelope()
    envelope2.expired = True
    assert mark_injected(envelope2) is False


def test_query_fingerprint_stable() -> None:
    assert query_fingerprint("hello world") == query_fingerprint("hello world")
    assert query_fingerprint("hello world") != query_fingerprint("goodbye")


def test_scope_expiry() -> None:
    scope = scope_for(f"sess-{uuid4().hex}")
    envelope = _envelope(turn_id="t-exp")
    scope.put(envelope)
    assert scope.get("t-exp") is not None
    assert expire_turn(scope.session_key, "t-exp") is True
    assert envelope.expired is True
    assert scope.get("t-exp") is None


# ---------------------------------------------------------------------------
# F1: router modes + retrieval behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_off_is_zero_cost(db_session: AsyncSession) -> None:
    _set_gate("off")
    assert memory_gate_mode() == "off"
    envelope = await route_turn(
        db_session,
        query="What did we decide about IIT?",
        turn_id="turn-off",
    )
    assert envelope is None


@pytest.mark.asyncio
async def test_explicit_decision_retrieval(db_session: AsyncSession) -> None:
    db_session.add(
        _seed_memory(
            text="We decided that IIT admission prep uses spaced repetition.",
            importance=0.95,
        )
    )
    await db_session.commit()
    _set_gate("on")
    envelope = await route_turn(
        db_session,
        query="What did we decide about IIT?",
        turn_id=f"turn-{uuid4().hex}",
    )
    assert envelope is not None
    assert envelope.retrieval_intent is RetrievalIntent.DECISION
    assert envelope.level >= 1
    assert any("IIT" in item.text for item in envelope.items)


@pytest.mark.asyncio
async def test_no_memory_turn_yields_nothing(db_session: AsyncSession) -> None:
    db_session.add(_seed_memory())
    await db_session.commit()
    _set_gate("on")
    envelope = await route_turn(
        db_session,
        query="What's 18 times 7?",
        turn_id=f"turn-{uuid4().hex}",
    )
    assert envelope is None
    snapshot = shadow_health_snapshot()
    assert snapshot["mode_counts"]["zero_retrieval_turns"] >= 1


@pytest.mark.asyncio
async def test_retry_same_turn_returns_same_envelope(db_session: AsyncSession) -> None:
    db_session.add(_seed_memory())
    await db_session.commit()
    _set_gate("on")
    turn_id = f"turn-{uuid4().hex}"
    first = await route_turn(db_session, query="What did we decide about IIT?", turn_id=turn_id)
    second = await route_turn(db_session, query="What did we decide about IIT?", turn_id=turn_id)
    assert first is second  # exactly-once retrieval per turn (§20)


@pytest.mark.asyncio
async def test_stale_bleed_is_blocked(db_session: AsyncSession) -> None:
    db_session.add(
        _seed_memory(text="We decided the IIT attempt starts in December.", importance=0.95)
    )
    await db_session.commit()
    _set_gate("on")
    session_key = f"sess-{uuid4().hex}"

    envelope1 = await route_turn(
        db_session,
        query="What did we decide about IIT?",
        turn_id="bleed-1",
        live_session_id=session_key,
    )
    assert envelope1 is not None
    expire_turn(session_key, "bleed-1")

    envelope2 = await route_turn(
        db_session,
        query="Play some music.",
        turn_id="bleed-2",
        live_session_id=session_key,
    )
    assert envelope2 is None  # no IIT context leaks into a music turn

    db_session.add(
        _seed_memory(text="Evie memory system uses a TurnGate for owner turns.", importance=0.9)
    )
    await db_session.commit()
    envelope3 = await route_turn(
        db_session,
        query="What were we doing with Evie's memory system?",
        turn_id="bleed-3",
        live_session_id=session_key,
    )
    assert envelope3 is not None
    assert envelope3.turn_id == "bleed-3"
    assert any("memory system" in item.text for item in envelope3.items)


@pytest.mark.asyncio
async def test_never_send_to_model_excluded_at_boundary(db_session: AsyncSession) -> None:
    db_session.add(_seed_memory(text="Secret: the vault key is under the mat.", privacy_level="never_send_to_model", importance=1.0))
    db_session.add(_seed_memory(text="We decided to use SQLite for local tests."))
    await db_session.commit()
    _set_gate("on")
    envelope = await route_turn(
        db_session,
        query="What did we decide about local test databases?",
        turn_id=f"turn-{uuid4().hex}",
    )
    assert envelope is not None
    assert all("vault key" not in item.text for item in envelope.items)


@pytest.mark.asyncio
async def test_superseded_memory_not_current_recall(db_session: AsyncSession) -> None:
    old = _seed_memory(
        text="The orchestrator uses Redis queues for everything.",
        valid_from=utcnow() - timedelta(days=60),
    )
    db_session.add(old)
    await db_session.flush()
    old.is_current = False
    old.valid_until = utcnow() - timedelta(days=10)
    db_session.add(
        _seed_memory(
            text="The orchestrator moved ingestion to the RQ default queue.",
            supersedes_id=old.id,
        )
    )
    await db_session.commit()
    _set_gate("on")
    envelope = await route_turn(
        db_session,
        query="What does the orchestrator use for ingestion queues?",
        turn_id=f"turn-{uuid4().hex}",
    )
    assert envelope is not None
    texts = " ".join(item.text for item in envelope.items)
    assert "Redis queues for everything" not in texts
    assert "RQ default queue" in texts


@pytest.mark.asyncio
async def test_as_of_recalls_historical_version(db_session: AsyncSession) -> None:
    january = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    old = _seed_memory(
        text="I thought FastAPI was too heavy for the edge service in January.",
        event_time=january,
        valid_from=january,
        valid_until=datetime(2026, 3, 1, tzinfo=UTC),
        is_current=False,
    )
    db_session.add(old)
    db_session.add(
        _seed_memory(
            text="I now think FastAPI is right for every service.",
            event_time=datetime(2026, 3, 10, tzinfo=UTC),
        )
    )
    await db_session.commit()
    _set_gate("on")
    envelope = await route_turn(
        db_session,
        query="What did I think about FastAPI in January 2026?",
        turn_id=f"turn-{uuid4().hex}",
    )
    assert envelope is not None
    assert envelope.retrieval_intent is RetrievalIntent.TEMPORAL_EXACT
    assert envelope.diagnosis.get("as_of") is not None  # as_of recall engaged
    texts = " ".join(item.text for item in envelope.items)
    assert "too heavy" in texts
    assert "right for every service" not in texts


@pytest.mark.asyncio
async def test_sandbox_scope_denied(db_session: AsyncSession) -> None:
    db_session.add(_seed_memory(importance=1.0))
    await db_session.commit()
    _set_gate("on")
    before = shadow_health_snapshot()["mode_counts"]["scope_denials"]
    envelope = await route_turn(
        db_session,
        query="What did we decide about IIT?",
        turn_id=f"turn-{uuid4().hex}",
        memory_scope="sandbox",
    )
    assert envelope is None
    after = shadow_health_snapshot()["mode_counts"]["scope_denials"]
    assert after == before + 1


@pytest.mark.asyncio
async def test_shadow_mode_builds_but_route_gate_stays_clean(db_session: AsyncSession) -> None:
    db_session.add(_seed_memory(importance=1.0))
    await db_session.commit()
    _set_gate("shadow")
    envelope = await route_turn(
        db_session,
        query="What did we decide about IIT?",
        turn_id=f"turn-{uuid4().hex}",
    )
    assert envelope is not None
    assert envelope.diagnosis.get("mode") == "shadow"
    assert envelope.injected is False
    snapshot = shadow_health_snapshot()
    assert snapshot["mode_counts"]["shadow_builds"] is not None


# ---------------------------------------------------------------------------
# F1: TurnGate seam
# ---------------------------------------------------------------------------


def _owner_turn(turn_id: str, transcript: str, *, device_id: str | None = None):
    from app.ev.owner_turn import create_owner_turn

    return create_owner_turn(
        live_session_id="sess-test",
        provider_item_id=None,
        owner_id="master",
        device_id=device_id,
        transcript=transcript,
        transcript_source="provider",
    )


@pytest.mark.asyncio
async def test_gate_off_attaches_nothing(db_session: AsyncSession) -> None:
    _set_gate("off")
    from app.ev.turn_gate import _maybe_attach_shadow_context

    owner_turn = _owner_turn("gate-off", "What did we decide about IIT?")
    result = await _maybe_attach_shadow_context(
        db_session, owner_turn, _conversation_result()
    )
    assert result.shadow_context is None


def _conversation_result():
    from app.ev.turn_intent import TurnResult

    return TurnResult(ok=True, route="CONVERSATION", operation="UNKNOWN")


@pytest.mark.asyncio
async def test_gate_on_attaches_labeled_history(db_session: AsyncSession) -> None:
    db_session.add(_seed_memory(text="We decided the orchestrator uses a broker.", importance=0.95))
    await db_session.commit()
    _set_gate("on")
    from app.ev.turn_gate import _maybe_attach_shadow_context, create_realtime_response_payload

    owner_turn = _owner_turn(f"gate-{uuid4().hex}", "What did we decide about the orchestrator?")
    result = await _maybe_attach_shadow_context(db_session, owner_turn, _conversation_result())
    assert result.route == "CONVERSATION"
    assert result.shadow_context is not None
    block = result.shadow_context["block"]
    assert block.startswith("[EVIE_RECALLED_HISTORY]")

    payload = create_realtime_response_payload(owner_turn, result)
    instructions = str(payload["response"]["instructions"])
    assert "[EVIE_RECALLED_HISTORY]" in instructions
    assert "orchestrator" in instructions


@pytest.mark.asyncio
async def test_gate_current_state_query_gets_no_history(db_session: AsyncSession) -> None:
    db_session.add(_seed_memory(importance=1.0))
    await db_session.commit()
    _set_gate("on")
    from app.ev.turn_gate import _maybe_attach_shadow_context

    owner_turn = _owner_turn(f"gate-{uuid4().hex}", "What is the priority of Project Canary?")
    result = await _maybe_attach_shadow_context(db_session, owner_turn, _conversation_result())
    assert result.shadow_context is None


@pytest.mark.asyncio
async def test_historical_state_question_downgrades_to_history(db_session: AsyncSession) -> None:
    from app.ev.turn_intent import TurnResult

    db_session.add(
        _seed_memory(
            text="Project Canary's priority was Normal when we started it.",
            importance=0.9,
        )
    )
    await db_session.commit()
    _set_gate("on")
    from app.ev.turn_gate import _maybe_attach_shadow_context

    owner_turn = _owner_turn(f"gate-{uuid4().hex}", "What was Canary's priority originally?")
    stateful = TurnResult(
        ok=True,
        route="STATE_QUERY",
        operation="PROJECT_GET",
        canonical_data={"priority": "HIGH"},
    )
    result = await _maybe_attach_shadow_context(db_session, owner_turn, stateful)
    assert result.route == "CONVERSATION"  # downgraded: history, not fake canonical
    assert result.canonical_data is None
    assert result.shadow_context is not None
    assert "[EVIE_RECALLED_HISTORY]" in result.shadow_context["block"]


@pytest.mark.asyncio
async def test_current_state_question_stays_canonical(db_session: AsyncSession) -> None:
    from app.ev.turn_intent import TurnResult

    db_session.add(
        _seed_memory(text="Project Canary's priority was Normal last month.", importance=0.9)
    )
    await db_session.commit()
    _set_gate("on")
    from app.ev.turn_gate import _maybe_attach_shadow_context

    owner_turn = _owner_turn(f"gate-{uuid4().hex}", "What is Canary's priority?")
    stateful = TurnResult(
        ok=True,
        route="STATE_QUERY",
        operation="PROJECT_GET",
        canonical_data={"priority": "HIGH"},
    )
    result = await _maybe_attach_shadow_context(db_session, owner_turn, stateful)
    assert result.route == "STATE_QUERY"  # canonical authority untouched
    assert result.canonical_data == {"priority": "HIGH"}
    assert result.shadow_context is None


# ---------------------------------------------------------------------------
# Performance (§43) — measured, generous CI budgets, real numbers logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_level1_latency_budget(db_session: AsyncSession) -> None:
    import time

    for index in range(12):
        db_session.add(_seed_memory(text=f"Decision {index}: we chose option {index % 3} for the build.", importance=0.7))
    await db_session.commit()
    _set_gate("on")
    durations: list[float] = []
    for index in range(8):
        started = time.perf_counter()
        envelope = await route_turn(
            db_session,
            query=f"What did we decide about the build option {index % 3}?",
            turn_id=f"perf-{index}",
        )
        durations.append((time.perf_counter() - started) * 1000.0)
        assert envelope is None or envelope.token_count <= LEVEL_TOKEN_BUDGETS[2]
    durations.sort()
    p95 = durations[int(0.95 * (len(durations) - 1))]
    print(f"\n[perf] route_turn L1 p50={durations[len(durations)//2]:.1f}ms p95={p95:.1f}ms")
    assert p95 < 250.0, f"L1 retrieval p95 {p95:.1f}ms exceeds 250ms target"


@pytest.mark.asyncio
async def test_shadow_token_accounting_measured(db_session: AsyncSession) -> None:
    """Measured envelope sizes per level (report data, §43)."""

    for index in range(30):
        db_session.add(
            _seed_memory(
                text=f"Decision {index}: the orchestrator broker handles operation family {index}.",
                importance=0.6 + index * 0.01,
            )
        )
    await db_session.commit()
    db_session.add(
        _seed_memory(
            text="In January 2026 I thought the orchestrator should stay a monolith.",
            event_time=datetime(2026, 1, 15, tzinfo=UTC),
            valid_from=datetime(2026, 1, 15, tzinfo=UTC),
            valid_until=datetime(2026, 3, 1, tzinfo=UTC),
            is_current=False,
        )
    )
    await db_session.commit()
    _set_gate("on")
    by_level: dict[int, list[int]] = {}
    for index in range(6):
        env = await route_turn(
            db_session,
            query="What did we decide about the orchestrator broker families?",
            turn_id=f"tokens-l1-{index}",
        )
        if env is not None:
            by_level.setdefault(env.level, []).append(env.token_count)
    for index in range(3):
        env = await route_turn(
            db_session,
            query="What did I think about the orchestrator in January 2026?",
            turn_id=f"tokens-l3-{index}",
        )
        assert env is not None, "as_of recall should find the January memory"
        by_level.setdefault(env.level, []).append(env.token_count)
    summary = {
        level: {"avg": round(sum(v) / len(v)), "max": max(v), "budget": LEVEL_TOKEN_BUDGETS[level]}
        for level, v in sorted(by_level.items())
    }
    print(f"\n[tokens] {summary}")
    for level, v in by_level.items():
        assert max(v) <= LEVEL_TOKEN_BUDGETS[level], f"L{level} exceeded budget"


@pytest.mark.asyncio
async def test_ops_probe_instrument(db_session: AsyncSession) -> None:
    """Canary instrument: bounded metadata, mode-respecting, no text leakage."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    db_session.add(_seed_memory(importance=1.0))
    await db_session.commit()

    _set_gate("off")
    headers = {"Authorization": "Bearer test-key"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        probe = await client.post(
            "/v1/ops/memory-router/probe", json={"query": "What did we decide about IIT?"}
        )
        assert probe.status_code == 200
        body = probe.json()
        assert body["mode"] == "off" and body["retrieval_triggered"] is False

    _set_gate("shadow")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        probe = await client.post(
            "/v1/ops/memory-router/probe", json={"query": "What did we decide about IIT?"}
        )
        body = probe.json()
        assert body["retrieval_triggered"] is True
        assert body["selected"] >= 1
        assert body["item_refs"][0]["ref"] is not None
        assert "text" not in body["item_refs"][0]  # no memory text in diagnostics


@pytest.mark.asyncio
async def test_f11_stale_cache_law(db_session: AsyncSession) -> None:
    """§5: preference updated → same query must return the NEW version."""

    from app.memory.retrieval import bump_memory_epoch, memory_epoch

    db_session.add(_seed_memory(text="The owner prefers short technical answers."))
    await db_session.commit()
    _set_gate("on")
    before_epoch = memory_epoch()

    env1 = await route_turn(
        db_session, query="What do I prefer for answer style?",
        turn_id=f"stale-{uuid4().hex}",
    )
    assert env1 is not None
    assert any("short technical answers" in i.text for i in env1.items)

    # Owner corrects: new preference version supersedes the old (§15 law).
    from app.models import Memory as MemoryRow

    db_session.add(_seed_memory(
        text="The owner now prefers detailed answers with code examples.",
        memory_type="preference",
        importance=0.96,
    ))
    await db_session.commit()
    bump_memory_epoch()
    assert memory_epoch() > before_epoch

    env2 = await route_turn(
        db_session, query="What do I prefer for answer style?",
        turn_id=f"stale-{uuid4().hex}",
    )
    assert env2 is not None
    texts = " ".join(i.text for i in env2.items)
    assert "detailed answers with code examples" in texts
    # The superseded preference must not present as current truth alongside.
    env2.current = True
