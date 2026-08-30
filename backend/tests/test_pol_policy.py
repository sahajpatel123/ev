"""Permissioned Operating Layer Phase 0: shared evaluate_policy path."""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.policy import (
    ROUTED_CAPABILITIES,
    Confirmation,
    evaluate_policy,
)
from app.ev.training_wheels import TRAINING_STEPS, complete_step
from app.models import Integration
from app.utils.text import utcnow


async def _unlock_life(db_session: AsyncSession) -> None:
    from app.ev.assistant import get_profile
    from app.ev.protocols import complete_training_wheels

    profile = await get_profile(db_session)
    profile.training_steps = {step: utcnow().isoformat() for step in TRAINING_STEPS}
    done = await complete_training_wheels(db_session)
    assert done.get("completed") is True
    await db_session.commit()


def test_unknown_capability_is_rejected() -> None:
    decision = evaluate_policy("not_a_real_capability")
    assert decision.allowed is False
    assert decision.effect == "reject"
    assert "unknown" in decision.reason


def test_forbidden_capability_is_refused_permanently() -> None:
    decision = evaluate_policy("instant_kill")
    assert decision.allowed is False
    assert decision.effect == "refuse"
    assert decision.risk_class == "forbidden"


def test_r0_owner_reads_do_not_require_confirmation() -> None:
    for name in ("get_weather", "calibrate", "calendar_read", "list_messages"):
        decision = evaluate_policy(
            name,
            actor="master",
            channel="action",
            training_wheels_complete=True,
            provider_connected=True,
        )
        assert name in ROUTED_CAPABILITIES
        assert decision.allowed is True
        assert decision.risk_class == "R0"
        assert decision.confirmation_required is False
        assert "source" in decision.evidence_fields
        assert "timestamp" in decision.evidence_fields


def test_r2_standing_owner_scope_after_training_wheels() -> None:
    decision = evaluate_policy(
        "send_message",
        actor="master",
        channel="action",
        arguments={"to": "Mom", "text": "I'm late"},
        training_wheels_complete=True,
        provider_connected=True,
    )
    assert decision.allowed is True
    assert decision.risk_class == "R2"
    assert decision.confirmation_policy == "standing"
    assert decision.audit["training_wheels_complete"] is True


def test_r3_voice_cannot_skip_fresh_confirmation() -> None:
    decision = evaluate_policy(
        "place_call",
        actor="voice",
        channel="voice",
        arguments={"name": "Ned", "confirm": True},
        confirmation=Confirmation(factor="voice", confirmed=True, target="Ned"),
        training_wheels_complete=True,
        provider_connected=True,
    )
    assert decision.allowed is False
    assert decision.effect == "confirm"
    assert decision.independent_confirmation is True
    assert decision.confirmation_ttl_seconds == 120
    assert decision.target == "Ned"


def test_r3_independent_confirmation_is_target_bound() -> None:
    now = utcnow()
    allowed = evaluate_policy(
        "place_call",
        actor="voice",
        channel="voice",
        arguments={"name": "Ned"},
        confirmation=Confirmation(
            factor="hud",
            confirmed=True,
            target="Ned",
            issued_at=now,
            expires_at=now + timedelta(seconds=60),
        ),
        provider_connected=True,
        now=now,
    )
    assert allowed.allowed is True

    wrong_target = evaluate_policy(
        "place_call",
        actor="voice",
        channel="voice",
        arguments={"name": "Ned"},
        confirmation=Confirmation(
            factor="hud",
            confirmed=True,
            target="Mom",
            issued_at=now,
            expires_at=now + timedelta(seconds=60),
        ),
        provider_connected=True,
        now=now,
    )
    assert wrong_target.allowed is False
    assert wrong_target.effect == "confirm"

    expired = evaluate_policy(
        "place_call",
        actor="master",
        channel="action",
        arguments={"name": "Ned"},
        confirmation=Confirmation(
            factor="hud",
            confirmed=True,
            target="Ned",
            issued_at=now - timedelta(seconds=300),
            expires_at=now - timedelta(seconds=1),
        ),
        provider_connected=True,
        now=now,
    )
    assert expired.allowed is False
    assert expired.effect == "confirm"


def test_confirmation_expiring_at_current_time_is_rejected() -> None:
    now = utcnow()
    decision = evaluate_policy(
        "place_call",
        actor="master",
        channel="action",
        arguments={"name": "Ned"},
        confirmation=Confirmation(
            factor="hud",
            confirmed=True,
            target="Ned",
            issued_at=now - timedelta(seconds=120),
            expires_at=now,
        ),
        provider_connected=True,
        now=now,
    )
    assert decision.effect == "confirm"


def test_provider_scope_is_checked_separately_from_actor_scope() -> None:
    decision = evaluate_policy(
        "calendar_read",
        actor="master",
        channel="action",
        granted_scopes=("calendar:read",),
        provider_scopes=("calendar:write",),
        provider_connected=True,
    )
    assert decision.effect == "deny"
    assert "provider missing scopes" in decision.reason


def test_r1_owner_change_is_automatic() -> None:
    decision = evaluate_policy(
        "set_quiet_hours",
        actor="master",
        channel="action",
        arguments={"until": "22:00"},
        training_wheels_complete=False,
        provider_connected=True,
    )
    assert decision.allowed is True
    assert decision.risk_class == "R1"


def test_r4_requires_fresh_confirmation() -> None:
    decision = evaluate_policy(
        "ticket_buy",
        actor="voice",
        channel="voice",
        arguments={"query": "opera", "confirm": True},
        confirmation=Confirmation(factor="voice", confirmed=True, target="opera"),
        provider_connected=True,
    )
    assert decision.allowed is False
    assert decision.risk_class == "R4"
    assert decision.independent_confirmation is True
    assert decision.confirmation_ttl_seconds == 60


def test_missing_provider_is_not_connected() -> None:
    decision = evaluate_policy(
        "calendar_read",
        actor="master",
        channel="action",
        provider_connected=False,
    )
    assert decision.allowed is False
    assert decision.effect == "not_connected"
    assert decision.provider == "calendar"


def test_missing_scopes_are_denied() -> None:
    decision = evaluate_policy(
        "calendar_read",
        actor="device:ned",
        channel="action",
        granted_scopes=("research:read",),
        provider_connected=True,
    )
    assert decision.allowed is False
    assert decision.effect == "deny"
    assert "calendar:read" in decision.reason


def test_policy_rejects_invalid_arguments_before_authorization() -> None:
    decision = evaluate_policy(
        "calendar_read",
        actor="master",
        channel="action",
        arguments={"limit": 0},
        provider_connected=True,
    )
    assert decision.allowed is False
    assert decision.effect == "invalid_request"
    assert "limit" in decision.reason


def test_untrusted_r0_requires_a_scoped_read_or_owner_trust() -> None:
    decision = evaluate_policy(
        "calendar_read",
        actor="device:ned",
        channel="action",
        provider_connected=True,
    )
    assert decision.allowed is False
    assert decision.effect == "deny"
    assert "verified owner" in decision.reason


def test_r2_standing_scope_waits_for_training_wheels() -> None:
    decision = evaluate_policy(
        "send_message",
        actor="master",
        channel="action",
        arguments={"to": "Mom", "text": "late"},
        training_wheels_complete=False,
        provider_connected=True,
    )
    assert decision.allowed is False
    assert decision.effect == "confirm"


def test_high_risk_confirmation_requires_target_and_unavailable_is_explicit() -> None:
    missing_target = evaluate_policy(
        "place_call",
        actor="master",
        channel="action",
        arguments={},
        provider_connected=True,
    )
    assert missing_target.effect == "invalid_request"
    assert "target" in missing_target.reason

    unavailable = evaluate_policy(
        "calendar_read",
        actor="master",
        channel="action",
        provider_connected=None,
    )
    assert unavailable.effect == "unavailable"


def test_delegate_calendar_scope_allows_calendar_read() -> None:
    decision = evaluate_policy(
        "calendar_read",
        actor="device:ned",
        channel="action",
        granted_scopes=("calendar:read",),
        provider_connected=True,
    )
    assert decision.allowed is True


def test_live_manifest_contains_policy_contract_fields() -> None:
    from app.ev.policy import annotate_spec
    from app.ev.tools import get_spec

    spec = annotate_spec(get_spec("calendar_read") or {})
    for field in (
        "version",
        "parameters",
        "output",
        "required_scopes",
        "risk_class",
        "confirmation",
        "target_ownership",
        "provider",
        "fallback",
        "evidence",
        "idempotency",
        "timeout_seconds",
        "cancellation",
        "audit_event",
    ):
        assert field in spec, field


async def test_capability_manifest_endpoint_is_live(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/capabilities?session_id=diagnostic-session")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "ev.capability-manifest.v1"
    by_name = {item["name"]: item for item in body["capabilities"]}
    for name in ("get_weather", "calibrate", "calendar_read", "list_messages"):
        item = by_name[name]
        assert item["current_provider"]
        assert item["availability"] in {"available", "not_connected", "unavailable"}
        assert item["required_scopes"]
        assert item["evidence_requirements"]
    diagnostics = body["diagnostics"]
    assert diagnostics["session_id"] == "diagnostic-session"
    assert diagnostics["actor"] == "master"
    assert diagnostics["provider"]
    assert isinstance(diagnostics["projected_tool_names"], list)
    assert isinstance(diagnostics["executable_tool_names"], list)
    assert isinstance(diagnostics["confirmation_required_tool_names"], list)
    assert isinstance(diagnostics["unavailable_tool_names"], list)
    assert diagnostics["capability_error"] is None
    assert body["projection_timestamp"]


async def test_diagnostics_http_entrypoint_uses_real_audit_endpoint(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from sqlalchemy import select

    from app.models import AccessLog

    response = await client.post("/v1/diagnostics/calibrate")
    assert response.status_code == 200, response.text
    row = (
        await db_session.execute(
            select(AccessLog)
            .where(AccessLog.action == "tool_call")
            .order_by(AccessLog.occurred_at.desc())
            .limit(1)
        )
    ).scalar_one()
    assert row.endpoint == "POST /v1/diagnostics/calibrate"
    assert row.details["provider"] == "local"
    assert row.details["result"]["evidence"]


async def test_gateway_rejects_unknown_tool(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "not_a_real_capability", "arguments": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "Unknown tool" in body["error"]


async def test_gateway_refuses_forbidden_tool(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "instant_kill", "arguments": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "refused"
    assert body["result"]["error"] == "refused"


async def test_weather_and_calibrate_include_evidence(
    client: AsyncClient, monkeypatch
) -> None:
    async def fake_weather(query: str, *, limit: int = 3):
        return [
            type(
                "R",
                (),
                {
                    "title": "Weather in Surat",
                    "url": "https://open-meteo.com/",
                    "snippet": "Surat: mainly clear. 31°C",
                },
            )()
        ]

    monkeypatch.setattr("app.search.live.weather_results", fake_weather)
    weather = await client.post(
        "/v1/gateway/tools",
        json={"name": "get_weather", "arguments": {"place": "Surat"}},
    )
    assert weather.status_code == 200, weather.text
    payload = weather.json()
    assert payload["ok"] is True
    evidence = (payload["result"] or {}).get("evidence") or {}
    assert evidence.get("source")
    assert evidence.get("timestamp")

    calibrate = await client.post(
        "/v1/gateway/tools",
        json={"name": "calibrate", "arguments": {}},
    )
    assert calibrate.status_code == 200, calibrate.text
    body = calibrate.json()
    assert body["ok"] is True
    cal_evidence = (body["result"] or {}).get("evidence") or {}
    assert cal_evidence.get("source")
    assert cal_evidence.get("timestamp")


async def test_calendar_read_and_list_messages_not_connected(
    client: AsyncClient,
) -> None:
    calendar = await client.post(
        "/v1/gateway/tools",
        json={"name": "calendar_read", "arguments": {}},
    )
    assert calendar.status_code == 200, calendar.text
    cal = calendar.json()
    assert cal["ok"] is True
    assert cal["result"]["error"] == "not_connected"
    assert cal["result"]["ok"] is False
    assert "calendar" in (cal["result"].get("next_step") or "").lower()

    messages = await client.post(
        "/v1/gateway/tools",
        json={"name": "list_messages", "arguments": {}},
    )
    assert messages.status_code == 200, messages.text
    listed = messages.json()
    assert listed["ok"] is True
    assert listed["result"]["error"] == "not_connected"
    assert listed["result"]["degraded"] is True


async def test_calendar_read_with_local_adapter_has_evidence(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add(
        Integration(
            slug="calendar",
            adapter="calendar",
            name="calendar",
            scopes=["calendar:read"],
            status="active",
            config={"provider": "local"},
        )
    )
    await db_session.commit()
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "calendar_read", "arguments": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body
    result = body["result"] or {}
    assert result.get("ok") is True
    assert result.get("error") != "not_connected"
    evidence = result.get("evidence") or {}
    assert evidence.get("source")
    assert evidence.get("timestamp")
    assert result.get("spoken")


async def test_list_messages_with_local_adapter_has_evidence(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    from types import SimpleNamespace

    from app.integrations import service as integrations

    db_session.add(
        Integration(
            slug="messaging",
            adapter="messaging",
            name="messaging",
            scopes=["messaging:read"],
            status="active",
            config={"provider": "local"},
        )
    )
    await db_session.commit()

    async def fake_execute(session, integration_id, action, args, *, actor):
        return SimpleNamespace(
            result={
                "ok": True,
                "items": [{"from": "Mom", "text": "hi"}],
            }
        )

    monkeypatch.setattr(integrations, "execute_action", fake_execute)
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "list_messages", "arguments": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body
    result = body["result"] or {}
    assert result.get("ok") is True
    evidence = result.get("evidence") or {}
    assert evidence.get("source")
    assert evidence.get("timestamp")


async def test_delegate_missing_scope_denied_at_gateway(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from httpx import ASGITransport

    from app.ev.assistant import get_profile
    from app.ev.protocols import complete_training_wheels
    from app.main import app

    profile = await get_profile(db_session)
    profile.training_steps = {step: utcnow().isoformat() for step in TRAINING_STEPS}
    await complete_training_wheels(db_session)
    await db_session.commit()

    created = await client.post(
        "/v1/devices",
        json={"name": "Ned Phone", "capabilities": ["voice"], "trust_level": "device"},
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    device_id = created.json()["device"]["id"]
    granted = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "delegate_grant",
            "arguments": {
                "name": "Ned",
                "scopes": ["research:read"],
                "device_id": device_id,
            },
            "allow_sensitive": True,
        },
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["ok"] is True, granted.text
    assert (granted.json().get("result") or {}).get("ok") is True, granted.text

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ned:
        denied = await ned.post(
            "/v1/gateway/tools",
            json={"name": "calendar_read", "arguments": {}},
        )
    assert denied.status_code == 200, denied.text
    body = denied.json()
    assert body["ok"] is False
    err = body.get("error") or ""
    assert "calendar:read" in err or err == "delegate_scope"


async def test_runtime_action_path_runs_calibrate(client: AsyncClient) -> None:
    routed = await client.post(
        "/v1/runtime/actions",
        json={"action_type": "calibrate", "auto_approve": True, "payload": {}},
    )
    assert routed.status_code == 201, routed.text
    action = routed.json()
    assert action["status"] == "approved"
    assert action["requires_approval"] is False
    executed = await client.post(f"/v1/runtime/actions/{action['id']}/execute")
    assert executed.status_code == 200, executed.text
    body = executed.json()
    assert body["status"] == "executed"
    result = body.get("result") or {}
    evidence = result.get("evidence") or {}
    assert evidence.get("source")
    assert evidence.get("timestamp")


async def test_voice_and_action_auth_are_separated(client: AsyncClient, db_session: AsyncSession) -> None:
    from app.ev import tools as ev_tools
    from app.models import Integration

    db_session.add(
        Integration(
            slug="phone",
            adapter="phone",
            name="phone",
            scopes=["phone:act"],
            status="active",
            config={"provider": "local"},
        )
    )
    await db_session.commit()
    await _unlock_life(db_session)

    voice = await ev_tools.dispatch(
        db_session,
        "place_call",
        {"name": "Ned", "confirm": True},
        actor="master",
        allow_sensitive=True,
        channel="voice",
    )
    assert voice.ok is False
    assert voice.error == "confirmation_required"
    result = voice.result or {}
    assert result.get("independent_confirmation") is True
    assert result.get("confirmation_channel") == "hud_or_biometric"

    action = await ev_tools.dispatch(
        db_session,
        "place_call",
        {"name": "Ned", "confirm": True},
        actor="master",
        allow_sensitive=True,
        channel="action",
    )
    assert action.error != "confirmation_required"


async def test_training_wheels_blocks_r3_before_confirmation_hold(
    db_session: AsyncSession,
) -> None:
    from app.ev import tools as ev_tools
    from app.ev.training_wheels import ensure_seed_gates

    await ensure_seed_gates(db_session)
    await db_session.commit()
    held = await ev_tools.dispatch(
        db_session,
        "place_call",
        {"name": "Ned"},
        actor="voice",
        allow_sensitive=True,
        channel="voice",
    )
    assert held.ok is False
    assert held.error == "training_wheels"
    assert (held.result or {}).get("action_id") is None
    assert (held.result or {}).get("error") != "confirmation_required"


async def test_owner_r0_after_training_wheels(client: AsyncClient, db_session: AsyncSession) -> None:
    for step in TRAINING_STEPS:
        await complete_step(db_session, step)
    await db_session.commit()
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "calibrate", "arguments": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert (body["result"] or {}).get("evidence", {}).get("timestamp")


def test_model_confidence_is_not_authorization() -> None:
    decision = evaluate_policy(
        "place_call",
        actor="voice",
        channel="voice",
        arguments={"name": "Ned", "confidence": 0.99, "confirm": True},
        confirmation=Confirmation(factor="voice", confirmed=True, target="Ned"),
        provider_connected=True,
    )
    assert decision.allowed is False
    assert decision.effect == "confirm"
    assert decision.audit.get("confidence_ignored") is True


def test_r3_confirmation_without_target_is_rejected() -> None:
    now = utcnow()
    decision = evaluate_policy(
        "place_call",
        actor="master",
        channel="action",
        arguments={"name": "Ned"},
        confirmation=Confirmation(
            factor="hud",
            confirmed=True,
            target=None,
            issued_at=now,
            expires_at=now + timedelta(seconds=60),
        ),
        provider_connected=True,
        now=now,
    )
    assert decision.allowed is False
    assert decision.effect == "confirm"


def test_r3_confirmation_session_mismatch_is_rejected() -> None:
    now = utcnow()
    decision = evaluate_policy(
        "place_call",
        actor="master",
        channel="action",
        arguments={"name": "Ned"},
        confirmation=Confirmation(
            factor="hud",
            confirmed=True,
            target="Ned",
            session_id="sess-a",
            issued_at=now,
            expires_at=now + timedelta(seconds=60),
        ),
        session_id="sess-b",
        provider_connected=True,
        now=now,
    )
    assert decision.allowed is False
    assert decision.effect == "confirm"


def test_r2_standing_is_owner_only() -> None:
    owner = evaluate_policy(
        "send_message",
        actor="master",
        channel="action",
        arguments={"to": "Mom", "text": "late"},
        owner_trusted=True,
        provider_connected=True,
    )
    assert owner.allowed is True

    stranger = evaluate_policy(
        "send_message",
        actor="device:ned",
        channel="action",
        arguments={"to": "Mom", "text": "late"},
        owner_trusted=False,
        granted_scopes=None,
        provider_connected=True,
    )
    assert stranger.allowed is False
    assert stranger.effect == "confirm"


async def test_http_place_call_without_confirm_is_held(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.ev.training_wheels import complete_step

    for step in TRAINING_STEPS:
        await complete_step(db_session, step)
    await db_session.commit()
    resp = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "place_call",
            "arguments": {"name": "Ned"},
            "allow_sensitive": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "confirmation_required"
    result = body.get("result") or {}
    assert result.get("independent_confirmation") is True
    assert result.get("needs_confirm") is True
    assert result.get("action_id")
    assert result.get("audio_loop") == "alive"
    assert result.get("hold") is True


async def test_http_place_call_with_confirm_is_not_wake_auth(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    for step in TRAINING_STEPS:
        await complete_step(db_session, step)
    await db_session.commit()
    resp = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "place_call",
            "arguments": {"name": "Ned", "confirm": True},
            "allow_sensitive": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("error") != "confirmation_required"


async def test_policy_decision_is_audited(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from sqlalchemy import select

    from app.models import AccessLog

    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "instant_kill", "arguments": {}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    rows = list(
        (
            await db_session.execute(
                select(AccessLog).where(AccessLog.action == "tool_call")
            )
        ).scalars().all()
    )
    assert rows
    details = rows[-1].details or {}
    assert details.get("risk_class") == "forbidden"
    assert details.get("policy_effect") == "refuse"


def test_annotate_spec_fills_unannotated_tools() -> None:
    from app.ev.actions import get_action_spec
    from app.ev.tools import get_spec

    calc = get_spec("calculate")
    assert calc is not None
    assert calc["risk_class"] == "R0"
    assert calc["confirmation"] == "none"
    assert "source" in calc["evidence"]

    health = get_spec("get_health_trends")
    assert health is not None
    assert health["risk_class"] == "R2"
    assert health["confirmation"] == "standing"

    shell = get_action_spec("execute_command")
    assert shell is not None
    assert shell["risk_class"] == "R4"
    assert shell["confirmation"] == "fresh"


async def test_voice_r3_parks_hold_without_pausing_audio_loop(
    db_session: AsyncSession,
) -> None:
    from uuid import UUID

    from app.ev.confirm import pol_meta
    from app.ev.tools import dispatch
    from app.models import ApprovedAction
    from app.voice.live.layer import register_live
    from app.voice.live.session import LiveSession

    await _unlock_life(db_session)
    live = LiveSession(session_id="live-hold-1", device_id=None, backchannel_enabled=False)
    register_live(live)
    try:
        held = await dispatch(
            db_session,
            "place_call",
            {"name": "Ned"},
            actor="voice",
            allow_sensitive=True,
            channel="voice",
            live_session_id=live.session_id,
        )
        assert held.ok is False
        assert held.error == "confirmation_required"
        result = held.result or {}
        assert result.get("hold") is True
        assert result.get("audio_loop") == "alive"
        assert result.get("action_id")
        assert live._paused is False
        assert live._closed is False
        assert live._approval_hold is not None
        row = await db_session.get(ApprovedAction, UUID(str(result["action_id"])))
        assert row is not None
        assert row.status == "pending"
        meta = pol_meta(row.payload)
        assert meta.get("target") == "Ned"
        assert meta.get("expires_at")
        assert meta.get("resume_on_approve") is True
        assert meta.get("args_fingerprint")
        assert "name" in row.payload
        assert "_pol" in row.payload
    finally:
        live.close()


async def test_hud_approve_resumes_parked_voice_hold(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from uuid import UUID

    from app.ev.tools import dispatch
    from app.models import ApprovedAction
    from app.voice.live.layer import register_live
    from app.voice.live.session import LiveSession

    await _unlock_life(db_session)
    live = LiveSession(session_id="live-hold-2", backchannel_enabled=False)
    register_live(live)
    try:
        held = await dispatch(
            db_session,
            "place_call",
            {"name": "Ned"},
            actor="voice",
            allow_sensitive=True,
            channel="voice",
            live_session_id=live.session_id,
        )
        action_id = (held.result or {}).get("action_id")
        assert action_id
        await db_session.commit()
        approved = await client.post(f"/v1/runtime/actions/{action_id}/approve")
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["status"] == "executed"
        assert body.get("error") != "confirmation_required"
        row = await db_session.get(ApprovedAction, UUID(str(action_id)))
        assert row is not None
        assert row.status == "executed"
        assert live._approval_hold is None
        assert live._paused is False
        assert live._closed is False
    finally:
        live.close()


async def test_expired_confirmation_ticket_cannot_be_approved(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from datetime import timedelta
    from uuid import UUID

    from sqlalchemy.orm.attributes import flag_modified

    from app.ev.tools import dispatch
    from app.models import ApprovedAction
    from app.utils.text import utcnow

    await _unlock_life(db_session)
    held = await dispatch(
        db_session,
        "place_call",
        {"name": "Ned"},
        actor="voice",
        allow_sensitive=True,
        channel="voice",
        live_session_id="live-hold-expired",
    )
    action_id = UUID(str((held.result or {})["action_id"]))
    row = await db_session.get(ApprovedAction, action_id)
    assert row is not None
    payload = dict(row.payload)
    meta = dict(payload["_pol"])
    meta["expires_at"] = (utcnow() - timedelta(seconds=5)).isoformat()
    payload["_pol"] = meta
    row.payload = payload
    flag_modified(row, "payload")
    await db_session.commit()

    denied = await client.post(f"/v1/runtime/actions/{action_id}/approve")
    assert denied.status_code == 409, denied.text
    assert "expired" in denied.json()["detail"]
    await db_session.commit()
    db_session.expire_all()
    refreshed = await db_session.get(ApprovedAction, action_id)
    assert refreshed is not None
    assert refreshed.status == "denied"
    assert refreshed.denied_reason == "confirmation_expired"


async def test_tampered_confirmation_target_cannot_be_approved(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from uuid import UUID

    from sqlalchemy.orm.attributes import flag_modified

    from app.ev.tools import dispatch
    from app.models import ApprovedAction

    await _unlock_life(db_session)
    held = await dispatch(
        db_session,
        "place_call",
        {"name": "Ned"},
        actor="voice",
        allow_sensitive=True,
        channel="voice",
    )
    action_id = UUID(str((held.result or {})["action_id"]))
    row = await db_session.get(ApprovedAction, action_id)
    assert row is not None
    payload = dict(row.payload)
    payload["name"] = "Mom"
    row.payload = payload
    flag_modified(row, "payload")
    await db_session.commit()

    denied = await client.post(f"/v1/runtime/actions/{action_id}/approve")
    assert denied.status_code == 409, denied.text
    assert "target" in denied.json()["detail"]


def test_training_wheels_is_a_policy_predicate() -> None:
    decision = evaluate_policy(
        "place_call",
        actor="voice",
        channel="voice",
        arguments={"name": "Ned"},
        training_wheels_complete=False,
        provider_connected=True,
    )
    assert decision.allowed is False
    assert decision.reason == "training_wheels"
    assert decision.effect == "deny"


def test_execute_command_is_r4_and_off_the_model_catalog() -> None:
    from app.ev.tools import get_spec, list_tools

    spec = get_spec("execute_command")
    assert spec is not None
    assert spec["risk_class"] == "R4"
    assert spec["confirmation"] == "fresh"
    assert "confirm" in spec["parameters"]["properties"]
    assert "execute_command" not in {item["name"] for item in list_tools()}


def test_camera_replay_accepts_confirm_argument() -> None:
    from app.ev.tools import get_spec

    spec = get_spec("camera_replay")
    assert spec is not None
    assert spec["risk_class"] == "R3"
    assert "confirm" in spec["parameters"]["properties"]


def test_live_intent_resolver_is_high_precision() -> None:
    from app.ev.tool_select import resolve_live_action

    assert resolve_live_action("Call Ned") == ("place_call", {"name": "Ned"})
    name, args = resolve_live_action("start a timer for 5 minutes") or ("", {})
    assert name == "start_timer"
    assert args.get("minutes") == 5
    assert resolve_live_action("how are you") is None
    assert resolve_live_action("run rm -rf /") is None
    assert resolve_live_action("remind me to call mom") == (
        "set_reminder",
        {"text": "call mom"},
    )
    assert resolve_live_action("check my inbox") == ("list_mail", {})


async def test_pipeline_transcript_dispatches_pol_tool() -> None:
    import json

    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, args, call_id))
        return json.dumps(
            {
                "ok": False,
                "error": "confirmation_required",
                "result": {"spoken": "Confirm it on your phone."},
            }
        )

    live = LiveSession(session_id="pipe-intent", backchannel_enabled=False)
    live.run_live_tool = runner
    try:
        await live.emit(FinalTranscriptEvent(at_ms=1, text="Call Ned", provider="dev"))
        assert seen == [("place_call", {"name": "Ned"}, "local-intent")]
        seen.clear()

        class _OpenAI:
            _provider = "openai"

            async def cancel(self) -> None:
                return None

        live.grok_voice = _OpenAI()
        await live.emit(
            FinalTranscriptEvent(at_ms=2, text="Call Ned", provider="openai-realtime")
        )
        assert seen == [("place_call", {"name": "Ned"}, "openai-sidecar")]
        seen.clear()
        live.grok_voice = object()
        await live.emit(
            FinalTranscriptEvent(at_ms=3, text="Call Ned", provider="grok-voice")
        )
        assert seen == []
    finally:
        live.close()


async def test_live_open_safari_runs_on_grok_transcript() -> None:
    """Open Safari via the helper without cancelling Grok or blocking speech."""

    import json

    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    seen: list[tuple[str, dict, str]] = []
    cancelled = {"n": 0}

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, args, call_id))
        return json.dumps({"ok": True, "result": {"spoken": "Opened Safari.", "opened": True}})

    class _Grok:
        _provider = "xai"
        supports_function_calls = True

        async def cancel(self) -> None:
            cancelled["n"] += 1

    live = LiveSession(session_id="grok-open-safari", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _Grok()
    try:
        await live.emit(
            FinalTranscriptEvent(at_ms=1, text="open Safari", provider="grok-voice")
        )
        task = live._life_action_task
        assert task is not None
        await task
        assert seen == [("open_app", {"name": "Safari"}, "deterministic-life")]
        assert cancelled["n"] == 0
    finally:
        live.close()


async def test_live_open_safari_does_not_block_grok_audio_pump() -> None:
    import asyncio
    import json
    import time

    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    started = {"helper": False}

    async def runner(name: str, args: dict, call_id: str) -> str:
        del name, args, call_id
        started["helper"] = True
        await asyncio.sleep(0.25)
        return json.dumps({"ok": True, "result": {"spoken": "Opened Safari."}})

    live = LiveSession(session_id="grok-open-smooth", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = object()
    try:
        t0 = time.monotonic()
        await live.emit(
            FinalTranscriptEvent(at_ms=1, text="open Safari", provider="grok-voice")
        )
        assert time.monotonic() - t0 < 0.1
        assert live._life_action_task is not None
        await live._life_action_task
        assert started["helper"] is True
    finally:
        live.close()
