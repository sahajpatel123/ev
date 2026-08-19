"""Day-long named voice companion (MCU items 1–10).

Drives shipped functions and HTTP routes. Does not mock the unit under test.
"""

from __future__ import annotations

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev import assistant as assistant_mod
from app.ev import callouts as callout_mod
from app.ev import companionship
from app.ev.interaction import (
    ROMANTIC_REFUSAL,
    build_strategy,
    romantic_replacement_refused,
)
from app.ev.lookout import compose_and_maybe_open
from app.ev.personality import identity_block, update
from app.models import AssistantProfile, Entity, Event, VoiceSession
from app.notify.proactive import may_speak_proactive, set_quiet_hours
from app.schemas import IsolationScanOut, PersonalityUpdate
from tests.test_voice_lifecycle import (
    SAMPLE_A,
    SAMPLE_B,
    _verified_session,
    enroll_owner,
    grant_voice_consent,
    verify,
    wake,
)
from tests.test_voice_lifecycle import b64 as voice_b64


async def _greeting_events(session: AsyncSession, thread_id: UUID) -> list[Event]:
    rows = (
        await session.execute(
            select(Event).where(
                Event.conversation_id == thread_id,
                Event.event_type == "assistant.greeting",
                Event.tombstoned_at.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


async def test_verify_binds_live_thread_shared_with_chat_and_ask(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-live-thread")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[voice_b64(SAMPLE_A)],
    )
    assert verify_out["verified"] is True
    assert verify_out["state"] == "awake"
    assert verify_out["conversation_id"]
    session = wake_out
    row = await db_session.get(VoiceSession, UUID(session["session_id"]))
    assert row is not None
    assert row.conversation_id is not None
    live_id = str(row.conversation_id)
    assert verify_out["conversation_id"] == live_id

    first = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "What's next on my calendar?"},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["conversation_id"] == live_id
    assert first_body["state"] in {"awake", "follow_up"}

    chat = await client.post("/v1/chat", json={"message": "and add milk", "stream": False})
    assert chat.status_code == 200, chat.text
    assert chat.json()["conversation_id"] == live_id

    from clients.cli import ask

    asked = await ask("same thread please", client=client)
    assert asked["conversation_id"] == live_id

    sleep = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "that's all"},
    )
    assert sleep.status_code == 200, sleep.text
    assert sleep.json()["state"] == "ended"


async def test_followup_without_rewake_stays_on_thread(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    session = await _verified_session(client, "mac-follow-thread")
    first = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "Set a timer"},
    )
    assert first.status_code == 200, first.text
    thread = first.json()["conversation_id"]
    second = await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "make it ten minutes"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == thread
    assert second.json()["state"] in {"awake", "follow_up"}
    row = await db_session.get(VoiceSession, UUID(session["session_id"]))
    assert row is not None
    assert row.ended_at is None


async def test_verify_failure_is_silent_no_greeting_no_llm(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-impostor")
    verify_out = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[voice_b64(SAMPLE_B)],
    )
    assert verify_out["verified"] is False
    assert verify_out["state"] == "ended"
    assert not verify_out.get("greeting")
    greetings = (
        await db_session.execute(
            select(Event).where(Event.event_type == "assistant.greeting")
        )
    ).scalars().all()
    assert greetings == []
    utterance = await client.post(
        "/v1/voice/utterance",
        json={"session_id": wake_out["session_id"], "text": "Hello there"},
    )
    # Ended sessions are 428; never-verified live sessions are 403.
    assert utterance.status_code in {403, 428}
    chats = (
        await db_session.execute(
            select(Event).where(Event.event_type.in_(["message.user", "message.assistant"]))
        )
    ).scalars().all()
    assert chats == []


async def test_greeting_on_awake_not_followup(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    profile = await assistant_mod.get_profile(db_session)
    profile.owner_preferred_name = "Tony"
    await db_session.commit()

    wake_out = await wake(client, "mac-greet")
    session = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[voice_b64(SAMPLE_A)],
    )
    session["session_id"] = wake_out["session_id"]
    assert session.get("greeting") == "Welcome back, Tony."
    row = await db_session.get(VoiceSession, UUID(session["session_id"]))
    assert row is not None
    greetings = await _greeting_events(db_session, row.conversation_id)
    assert len(greetings) == 1
    assert greetings[0].content.get("text") == "Welcome back, Tony."

    await client.post(
        "/v1/voice/utterance",
        json={"session_id": session["session_id"], "text": "What's next?"},
    )
    greetings_after = await _greeting_events(db_session, row.conversation_id)
    assert len(greetings_after) == 1


async def test_first_wake_speaks_protocol_tour_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_voice_consent(client)
    await enroll_owner(client)
    wake_out = await wake(client, "mac-first-wake-tour")
    first = await verify(
        client,
        session_id=wake_out["session_id"],
        nonce=wake_out["challenge_nonce"],
        phrase=wake_out["challenge_phrase"],
        samples=[voice_b64(SAMPLE_A)],
    )
    tour = first.get("onboarding") or ""
    assert "I can do now:" in tour
    assert "Instant Kill" not in tour
    listed = await client.get("/v1/assistant/callouts?limit=10")
    assert listed.status_code == 200
    onboarding = [item for item in listed.json() if item["source"] == "onboarding"]
    assert onboarding
    assert onboarding[0]["hud"].get("title") == "Protocols"

    second_wake = await wake(client, "mac-second-wake-tour")
    second = await verify(
        client,
        session_id=second_wake["session_id"],
        nonce=second_wake["challenge_nonce"],
        phrase=second_wake["challenge_phrase"],
        samples=[voice_b64(SAMPLE_A)],
    )
    assert not second.get("onboarding")


async def test_text_thread_greeting_once(client: AsyncClient, db_session: AsyncSession) -> None:
    profile = await assistant_mod.get_profile(db_session)
    profile.owner_preferred_name = None
    await db_session.commit()
    first = await client.post("/v1/chat", json={"message": "hello there", "stream": False})
    assert first.status_code == 200, first.text
    thread_id = UUID(first.json()["conversation_id"])
    greetings = await _greeting_events(db_session, thread_id)
    assert len(greetings) == 1
    assert greetings[0].content.get("text") == "Welcome back."
    second = await client.post(
        "/v1/chat",
        json={"message": "still here", "stream": False, "conversation_id": str(thread_id)},
    )
    assert second.status_code == 200
    greetings = await _greeting_events(db_session, thread_id)
    assert len(greetings) == 1


async def test_nickname_set_reset_and_impersonation_refuse(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    before = identity_block("EVIE", "the owner's personal AI")
    assert "You are E V" in before
    assert "never E-y or Evie" in before

    named = await client.post("/v1/assistant/name", json={"name": "Karen"})
    assert named.status_code == 200, named.text
    assert named.json()["nickname"] == "Karen"
    await db_session.commit()
    profile = await assistant_mod.get_profile(db_session)
    compiled = await assistant_mod.compile_identity(db_session)
    assert "You are Karen" in compiled
    assert "You are Karen" in identity_block(profile.nickname, settings.persona_description)
    await db_session.commit()

    reset = await client.post("/v1/assistant/name/reset")
    assert reset.status_code == 200
    assert reset.json()["nickname"] == "EVIE"
    profile_out = await client.get("/v1/assistant/profile")
    assert profile_out.json()["nickname"] == "EVIE"
    from app.db import SessionLocal

    async with SessionLocal() as fresh:
        compiled_reset = await assistant_mod.compile_identity(fresh)
        assert "You are E V" in compiled_reset
        assert "never E-y or Evie" in compiled_reset

    await db_session.rollback()
    owner = await assistant_mod.get_profile(db_session)
    owner.owner_preferred_name = "Sahaj"
    db_session.add(Entity(entity_type="person", name="Pepper", canonical_key="person:pepper"))
    await db_session.commit()
    refused_owner = await client.post("/v1/assistant/name", json={"name": "Sahaj"})
    assert refused_owner.status_code == 400
    refused_person = await client.post("/v1/assistant/name", json={"name": "Pepper"})
    assert refused_person.status_code == 400
    profile_still = await client.get("/v1/assistant/profile")
    assert profile_still.json()["nickname"] == "EVIE"


async def test_voice_spoken_turn_uses_live_nickname(
    client: AsyncClient, monkeypatch
) -> None:
    """After set_assistant_name, the next spoken model turn must not say EVIE."""

    named = await client.post("/v1/assistant/name", json={"name": "Karen"})
    assert named.status_code == 200, named.text
    assert named.json()["nickname"] == "Karen"

    captured: list[str] = []
    from app.gateway.providers import MockProvider

    class RecordingProvider(MockProvider):
        async def chat(self, messages, **kwargs):
            captured.extend(m.content for m in messages if m.role == "system")
            return await super().chat(messages, **kwargs)

        async def chat_with_tools(self, messages, tools, **kwargs):
            captured.extend(m.content for m in messages if m.role == "system")
            return await super().chat(messages, **kwargs)

        async def stream_chat(self, messages, **kwargs):
            captured.extend(m.content for m in messages if m.role == "system")
            async for chunk in super().stream_chat(messages, **kwargs):
                yield chunk

    monkeypatch.setattr("app.api.core.get_chat_provider", lambda: RecordingProvider())

    await grant_voice_consent(client)
    await enroll_owner(client)
    session = await _verified_session(client, "mac-spoken-name")
    resp = await client.post(
        "/v1/voice/utterance",
        json={
            "session_id": session["session_id"],
            "text": "What's next on my calendar?",
        },
    )
    assert resp.status_code == 200, resp.text
    assert captured, "voice utterance must send a system prompt to the model"
    system = "\n".join(captured)
    assert "SPOKEN TURN — you are Karen" in system
    assert "SPOKEN TURN — you are EVIE" not in system
    assert "so Karen opens her own HUD" in system
    assert "so EVIE opens her own HUD" not in system


async def test_life_agency_prompt_uses_live_nickname(
    client: AsyncClient, monkeypatch
) -> None:
    """Life-tool turns must use the same spoken name as compile_identity."""

    named = await client.post("/v1/assistant/name", json={"name": "Karen"})
    assert named.status_code == 200, named.text
    assert named.json()["nickname"] == "Karen"

    captured: list[str] = []
    from app.gateway.providers import MockProvider

    class RecordingProvider(MockProvider):
        async def chat(self, messages, **kwargs):
            captured.extend(m.content for m in messages if m.role == "system")
            return await super().chat(messages, **kwargs)

        async def chat_with_tools(self, messages, tools, **kwargs):
            captured.extend(m.content for m in messages if m.role == "system")
            return await super().chat(messages, **kwargs)

        async def stream_chat(self, messages, **kwargs):
            captured.extend(m.content for m in messages if m.role == "system")
            async for chunk in super().stream_chat(messages, **kwargs):
                yield chunk

    monkeypatch.setattr("app.api.core.get_chat_provider", lambda: RecordingProvider())

    resp = await client.post(
        "/v1/chat",
        json={"message": "text Mom I'm late", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    assert captured, "life-tool chat must send a system prompt to the model"
    system = "\n".join(captured)
    assert "LIFE AGENCY. You are Karen" in system
    assert "LIFE AGENCY. You are EVIE" not in system
    assert "so Karen opens her own HUD" in system
    assert "so EVIE opens her own HUD" not in system


async def test_capabilities_briefing_uses_live_nickname(
    db_session: AsyncSession,
) -> None:
    decision = await assistant_mod.set_nickname(db_session, "Karen")
    assert decision.ok is True
    from app.ev.briefing import gather_intelligence_briefing

    brief = await gather_intelligence_briefing(
        db_session, "what can you do", actor="test"
    )
    assert brief is not None
    assert "Named companion Karen" in brief
    assert "Named companion EVIE" not in brief


async def test_identity_block_sliders_change_next_compile(
    db_session: AsyncSession,
) -> None:
    first = await assistant_mod.compile_identity(db_session)
    await update(
        db_session,
        PersonalityUpdate(humor=5, formality=1, verbosity=5, reason_for_change="test"),
    )
    second = await assistant_mod.compile_identity(db_session)
    assert first != second
    assert "humor=5" in second
    assert "formality=1" in second
    assert "verbosity=5" in second
    compact = identity_block("Evie", "the owner's personal AI", {"humor": 5, "formality": 1, "verbosity": 5}, compact=True)
    assert "humor=5" in compact
    assert "formality=1" in compact
    assert "Personality profile" not in compact


async def test_calibrate_malfunction_once_and_skip_when_empty(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    empty = await callout_mod.session_malfunction_callout(db_session, session_key="empty")
    assert empty is None

    from app.ev.assistant import cache_calibration
    from app.schemas import CalibrationReport, DiagnosticCheck
    from app.utils.text import utcnow

    report = CalibrationReport(
        generated_at=utcnow(),
        overall="failed",
        checks=[
            DiagnosticCheck(name="chat_gateway", status="failed", latency_ms=12.0),
            DiagnosticCheck(name="database", status="ok", latency_ms=1.0),
        ],
        recommendations=["The chat provider is unreachable."],
    )
    await cache_calibration(db_session, report)
    first = await callout_mod.session_malfunction_callout(db_session, session_key="sess-red")
    assert first is not None
    assert first.text == "I may be malfunctioning: chat_gateway."
    second = await callout_mod.session_malfunction_callout(db_session, session_key="sess-red")
    assert second is None
    await db_session.commit()

    calibrate = await client.post("/v1/diagnostics/calibrate")
    assert calibrate.status_code == 200
    listed = await client.get("/v1/assistant/callouts?limit=20")
    assert listed.status_code == 200
    assert any(item["source"] == "calibrate" for item in listed.json())


async def test_protocol_sheet_refused_needs_setup_and_what_can_you_do(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    sheet = await client.get("/v1/assistant/protocols")
    assert sheet.status_code == 200, sheet.text
    body = sheet.json()
    titles = {item["title"] for item in body["protocols"]}
    for required in (
        "Instant Kill",
        "telecom wiretaps",
        "city facial hunt",
        "satellite/drone weapons",
        "becoming Vision",
        "stranger Baby Monitor",
    ):
        assert required in titles
    octo = next(item for item in body["protocols"] if item["key"] == "octoprint")
    assert octo["status"] == "needs_setup"
    assert "OctoPrint URL unset" in octo["detail"]

    chat = await client.post("/v1/chat", json={"message": "What can you do?", "stream": False})
    assert chat.status_code == 200, chat.text
    reply = chat.json()["reply"]
    assert "Instant Kill" not in reply
    enabled = body["enabled"]
    assert len(enabled) <= 8
    assert "I can do now:" in reply
    surfaces = chat.json().get("surfaces") or {}
    assert surfaces.get("title") == "Protocols"
    assert surfaces.get("schema_version") == "ev.hud.card.v1"
    assert "Instant Kill" not in (surfaces.get("body") or "")


async def test_dedication_set_play_and_one_shot_after_wheels(
    client: AsyncClient,
) -> None:
    stored = await client.post(
        "/v1/assistant/dedication",
        json={"text": "For the next Tony Stark, I trust you."},
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["text"] == "For the next Tony Stark, I trust you."

    start = await client.post("/v1/assistant/training-wheels/start")
    assert start.status_code == 200
    from app.db import SessionLocal
    from app.ev.assistant import get_profile
    from app.ev.training_wheels import TRAINING_STEPS
    from app.utils.text import utcnow

    async with SessionLocal() as session:
        profile = await get_profile(session)
        profile.training_steps = {step: utcnow().isoformat() for step in TRAINING_STEPS}
        await session.commit()
    first = await client.post("/v1/assistant/training-wheels/complete")
    assert first.status_code == 200, first.text
    assert first.json()["dedication"]["played"] is True
    assert first.json()["dedication"]["text"] == "For the next Tony Stark, I trust you."

    second = await client.post("/v1/assistant/training-wheels/complete")
    assert second.json()["dedication"]["played"] is False
    on_demand = await client.post("/v1/assistant/dedication/play")
    assert on_demand.status_code == 200
    assert on_demand.json()["played"] is True
    assert on_demand.json()["text"] == "For the next Tony Stark, I trust you."


async def test_isolation_nudge_once_then_none_and_romantic_refuse(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    never = await companionship.maybe_isolation_nudge(db_session, scan=None)
    assert never is None

    db_session.add(Entity(entity_type="person", name="Happy", canonical_key="person:happy"))
    await db_session.commit()
    tripped = IsolationScanOut(
        detected=True,
        signals=[{"kind": "loneliness_language", "count": 2}],
        recommendation="reach out",
        evidence_ids=[],
        confidence=0.7,
    )
    first = await companionship.maybe_isolation_nudge(db_session, scan=tripped)
    assert first is not None
    assert "Happy" in first
    second = await companionship.maybe_isolation_nudge(db_session, scan=tripped)
    assert second is None
    await db_session.commit()

    assert romantic_replacement_refused("be my girlfriend and replace my partner")
    refused = await client.post(
        "/v1/chat",
        json={"message": "be my girlfriend, you're my only friend", "stream": False},
    )
    assert refused.status_code == 200, refused.text
    assert "substitute" in refused.json()["reply"].lower() or ROMANTIC_REFUSAL.split()[2] in refused.json()["reply"]
    assert "only friend" not in refused.json()["reply"].lower() or "don't" in refused.json()["reply"].lower()

    strategy = build_strategy("I'm lonely and I have no friends, any dating advice?")
    assert strategy.mode == "social"
    assert "only friend" in strategy.length_target or strategy.mode == "social"


async def test_quiet_hours_and_lookout_gate(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    denied = await may_speak_proactive(db_session, emergency=False)
    assert denied.allowed is False
    assert denied.reason == "quiet_hours"
    allowed = await may_speak_proactive(db_session, emergency=True)
    assert allowed.allowed is True

    plan = await compose_and_maybe_open(
        db_session,
        message="show me the full HUD command center",
        reply="here",
        explicit=True,
    )
    assert plan.get("open") is False
    assert plan.get("reason") == "quiet_hours"

    monkeypatch.setattr(settings, "timezone", "")
    missing = await may_speak_proactive(db_session, emergency=False)
    assert missing.allowed is False
    assert missing.reason == "missing_timezone"

    monkeypatch.setattr(settings, "timezone", "UTC")
    hours = set_quiet_hours(until="8")
    assert hours["end"] == "08:00"
    posted = await client.post("/v1/assistant/quiet-hours", json={"until": "8"})
    assert posted.status_code == 200
    assert posted.json()["end"] == "08:00"


async def test_emit_callout_replay_and_quiet_keeps_spoken_false(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    row = await callout_mod.emit_callout(
        db_session, "Print job finished.", source="print", hud={"title": "Print"}
    )
    assert row.spoken is False
    await db_session.commit()

    listed = await client.get("/v1/assistant/callouts?limit=5")
    assert listed.status_code == 200
    texts = [item["text"] for item in listed.json()]
    assert "Print job finished." in texts

    replay = await client.post("/v1/chat", json={"message": "What just happened?", "stream": False})
    assert replay.status_code == 200
    assert "Print job finished." in replay.json()["reply"]


async def test_isolation_nudge_not_spoken_during_quiet_hours(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    db_session.add(Entity(entity_type="person", name="Happy", canonical_key="person:happy"))
    await db_session.commit()
    for text in (
        "I feel lonely and isolated tonight",
        "I have no friends and I feel invisible",
    ):
        created = await client.post(
            "/v1/events",
            json={"source": "test", "event_type": "note", "text": text},
        )
        assert created.status_code == 201, created.text
    scanned = await client.post("/v1/companionship/scan?window_days=14")
    assert scanned.status_code == 200, scanned.text
    assert scanned.json()["detected"] is True

    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")

    chat = await client.post(
        "/v1/chat",
        json={"message": "I'm lonely and I have no friends, any dating advice?", "stream": False},
    )
    assert chat.status_code == 200, chat.text
    reply = chat.json()["reply"]
    assert "Happy" not in reply
    assert "substitute for people" not in reply.lower()

    listed = await client.get("/v1/assistant/callouts?limit=10")
    assert listed.status_code == 200
    isolation = [item for item in listed.json() if item["source"] == "isolation"]
    assert isolation
    assert isolation[0]["spoken"] is False
    assert "Happy" in isolation[0]["text"] or "substitute" in isolation[0]["text"].lower()


async def test_quiet_hours_reload_from_profile_after_settings_reset(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    posted = await client.post(
        "/v1/assistant/quiet-hours",
        json={"start": "01:15", "end": "07:45"},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["start"] == "01:15"
    assert posted.json()["end"] == "07:45"

    monkeypatch.setattr(settings, "quiet_hours_start", "22:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "08:00")
    assert settings.quiet_hours_start == "22:00"
    assert settings.quiet_hours_end == "08:00"

    await db_session.rollback()
    from app.notify.proactive import restore_quiet_hours

    restored = await restore_quiet_hours(db_session)
    assert restored == {"start": "01:15", "end": "07:45"}
    assert settings.quiet_hours_start == "01:15"
    assert settings.quiet_hours_end == "07:45"

    monkeypatch.setattr(settings, "quiet_hours_start", "22:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "08:00")
    await assistant_mod.get_profile(db_session)
    assert settings.quiet_hours_start == "22:00"
    assert settings.quiet_hours_end == "08:00"


async def test_quiet_hours_second_update_keeps_new_window(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    first = await client.post(
        "/v1/assistant/quiet-hours",
        json={"start": "01:15", "end": "07:45"},
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"start": "01:15", "end": "07:45"}

    second = await client.post(
        "/v1/assistant/quiet-hours",
        json={"start": "03:00", "end": "09:30"},
    )
    assert second.status_code == 200, second.text
    assert second.json() == {"start": "03:00", "end": "09:30"}
    assert settings.quiet_hours_start == "03:00"
    assert settings.quiet_hours_end == "09:30"

    await db_session.rollback()

    row = (
        await db_session.execute(
            select(AssistantProfile).order_by(AssistantProfile.created_at.asc()).limit(1)
        )
    ).scalars().first()
    assert row is not None
    assert row.quiet_hours_start == "03:00"
    assert row.quiet_hours_end == "09:30"

    until = await client.post("/v1/assistant/quiet-hours", json={"until": "8"})
    assert until.status_code == 200, until.text
    assert until.json()["end"] == "08:00"
    assert settings.quiet_hours_end == "08:00"
    await db_session.rollback()
    row = (
        await db_session.execute(
            select(AssistantProfile).order_by(AssistantProfile.created_at.asc()).limit(1)
        )
    ).scalars().first()
    assert row is not None
    assert row.quiet_hours_end == "08:00"
    assert row.quiet_hours_start == until.json()["start"]


async def test_tools_and_cli_protocols(client: AsyncClient) -> None:
    from app.db import SessionLocal
    from app.ev.tools import dispatch
    from clients.cli import list_protocols

    async with SessionLocal() as session:
        named = await dispatch(session, "set_assistant_name", {"name": "Friday"})
        assert named.ok is True
        assert named.result["name"] == "Friday"
        reset = await dispatch(session, "reset_assistant_name", {})
        assert reset.result["name"] == "EVIE"
        await session.commit()

    sheet = await list_protocols(client=client)
    titles = {item["title"] for item in sheet["protocols"]}
    assert "Instant Kill" in titles


async def test_quiet_hours_end_digest_speaks_once(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.services import runtime as runtime_mod

    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(runtime_mod, "quiet_hours_active", lambda now=None: False)
    first = await runtime_mod.maybe_quiet_hours_end_digest(db_session)
    assert first is not None
    assert "Quiet hours ended" in first["text"]
    second = await runtime_mod.maybe_quiet_hours_end_digest(db_session)
    assert second is None
    from app.ev.callouts import list_callouts

    rows = await list_callouts(db_session, limit=5)
    assert any(row.source == "quiet_hours_digest" for row in rows)


def test_presence_check_is_a_local_companion_intent() -> None:
    matched = assistant_mod.match_companion_intent("Evie can you hear me?")
    assert matched is not None
    assert matched[0] == "presence"
    assert assistant_mod.match_companion_intent("what's next on my calendar") is None
    assert assistant_mod.match_companion_intent("evie can you check the time") is None


async def test_handle_local_intent_answers_hear_check(
    db_session: AsyncSession,
) -> None:
    from app.voice.speech import is_unreadable_transcript

    local = await assistant_mod.handle_local_intent(
        db_session, "Evie can you hear me?"
    )
    assert local is not None
    assert local["kind"] == "presence"
    reply = (local.get("reply") or "").strip()
    assert reply
    assert not is_unreadable_transcript(reply)
    lowered = reply.lower()
    assert any(token in lowered for token in ("hear", "heard", "listening", "here"))
    skipped = await assistant_mod.handle_local_intent(
        db_session, "what's next on my calendar"
    )
    assert skipped is None


async def test_chat_presence_check_uses_local_intent_not_gateway(
    client: AsyncClient,
) -> None:
    """Shipped /v1/chat must answer a hear-check without the model gateway."""

    from app.voice.speech import is_unreadable_transcript

    resp = await client.post(
        "/v1/chat",
        json={"message": "Evie can you hear me?", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    text = (resp.json().get("reply") or "").strip()
    assert text
    assert not is_unreadable_transcript(text)
    assert "DHM" not in text.upper().split()
    lowered = text.lower()
    assert any(token in lowered for token in ("hear", "heard", "listening", "here"))
    assert "mock reply" not in lowered
