"""Requirement-driven regression probes for the live-tool projection plan.

These tests deliberately exercise entry points outside the normal gateway
dispatch path.  They are not replacements for the existing policy/unit
coverage; they make direct execution paths fail loudly until they share the
canonical policy boundary.
"""

from __future__ import annotations

import inspect
from contextlib import suppress
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.policy import PolicyDecision
from app.models import Integration, LifeOutboundAction, LiveEvent
from app.schemas import IntegrationActionOut, ToolCallResponse
from app.utils.text import utcnow


def _deny_decision() -> PolicyDecision:
    return PolicyDecision(
        allowed=False,
        effect="deny",
        reason="regression test policy denial",
        risk_class="R2",
    )


def _deny_policy(monkeypatch, seen: list[dict], *modules) -> None:
    """Make every likely import shape fail closed and record the boundary call."""

    import app.ev.policy as policy

    async def deny_authorize(*args, **kwargs):
        seen.append({"kind": "authorize", "args": args, "kwargs": kwargs})
        return _deny_decision()

    def deny_evaluate(*args, **kwargs):
        seen.append({"kind": "evaluate_policy", "args": args, "kwargs": kwargs})
        return _deny_decision()

    monkeypatch.setattr(policy, "authorize", deny_authorize)
    monkeypatch.setattr(policy, "evaluate_policy", deny_evaluate)
    for module in modules:
        monkeypatch.setattr(module, "authorize", deny_authorize, raising=False)
        monkeypatch.setattr(module, "evaluate_policy", deny_evaluate, raising=False)


async def test_http_calendar_action_cannot_bypass_policy_before_adapter(
    client,
    monkeypatch,
) -> None:
    """Calendar writes from the integration HTTP surface need POL first."""

    from app.api import integrations as integrations_api

    installed = await client.post(
        "/v1/integrations",
        json={
            "adapter": "calendar",
            "name": "Calendar policy probe",
            "scopes": ["calendar:act"],
        },
    )
    assert installed.status_code == 201, installed.text
    integration_id = installed.json()["id"]

    adapter_calls: list[dict] = []

    async def fake_execute(*args, **kwargs):
        adapter_calls.append({"args": args, "kwargs": kwargs})
        return IntegrationActionOut(
            adapter="calendar",
            action="calendar.create_event",
            result={"ok": True},
            executed_at=utcnow(),
        )

    monkeypatch.setattr(integrations_api.integrations, "execute_action", fake_execute)
    policy_calls: list[dict] = []
    _deny_policy(monkeypatch, policy_calls, integrations_api)

    response = await client.post(
        f"/v1/integrations/{integration_id}/actions",
        json={
            "action": "calendar.create_event",
            "args": {"summary": "Lunch", "start": "2026-08-17T12:00:00Z"},
        },
    )

    assert policy_calls, (
        "HTTP calendar action did not consult the canonical policy; "
        f"status={response.status_code}, adapter_calls={adapter_calls}"
    )
    assert not adapter_calls, "calendar adapter ran after policy denial"


async def test_research_http_entrypoint_records_policy_denial_before_provider(
    client,
    monkeypatch,
) -> None:
    """A research run must not reach the web provider without POL."""

    from app.api import companion as companion_api
    from app.ev import research

    created = await client.post(
        "/v1/research/jobs",
        json={"goal": "policy-gated research", "allowed_tools": ["web_search"]},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    provider_calls: list[dict] = []

    class RecordingProvider:
        name = "recording-provider"

        async def search(self, query: str, *, limit: int = 5):
            provider_calls.append({"query": query, "limit": limit})
            return [{"title": "result", "url": "https://example.test", "snippet": "evidence"}]

    monkeypatch.setattr(research, "get_search_provider", lambda: RecordingProvider())
    policy_calls: list[dict] = []
    _deny_policy(monkeypatch, policy_calls, companion_api)

    response = await client.post(f"/v1/research/jobs/{job_id}/run")

    assert policy_calls, (
        "HTTP research run did not consult the canonical policy; "
        f"status={response.status_code}, provider_calls={provider_calls}"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["evidence"]["source"] == "policy"
    assert not provider_calls, "research provider ran after policy denial"


def test_research_worker_uses_the_policy_gated_service() -> None:
    """The synchronous worker delegates to the service that gates providers."""

    from app.ev.research import ResearchService
    from app.workers import jobs

    worker_source = inspect.getsource(jobs.run_research_job)
    service_source = inspect.getsource(ResearchService.run_job)
    assert 'ResearchService(session, actor="worker").run_job' in worker_source
    assert "authorize(" in service_source


async def test_device_proxy_service_cannot_queue_after_policy_denial(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """Device-triggered life actions must not rely only on legacy life policy."""

    from app.integrations import service as integrations_service

    integration = Integration(
        slug="device-proxy-policy-probe",
        adapter="device_proxy",
        name="Device proxy policy probe",
        scopes=["messaging:act"],
        status="active",
        config={"provider": "device_proxy", "contact_allowlist": "any"},
    )
    db_session.add(integration)
    await db_session.flush()

    policy_calls: list[dict] = []
    _deny_policy(monkeypatch, policy_calls, integrations_service)

    with suppress(PermissionError, ValueError):
        await integrations_service.execute_action(
            db_session,
            integration.id,
            action="messaging.send",
            args={"to": "Mom", "text": "policy probe"},
            actor="device:iphone",
        )

    queued = (
        await db_session.execute(
            select(LifeOutboundAction).where(
                LifeOutboundAction.integration_id == integration.id
            )
        )
    ).scalars().all()
    assert policy_calls, (
        "device-proxy action did not consult the canonical policy; "
        f"queued={len(queued)}"
    )
    assert not queued, "device-proxy action was queued after policy denial"


async def test_vision_http_entrypoint_cannot_analyze_after_policy_denial(
    client,
    monkeypatch,
) -> None:
    """Owner permission in the body is not a substitute for camera POL."""

    from app.api import edith as edith_api
    from app.ev import vision

    vision_calls: list[dict] = []

    async def fake_analyze(*args, **kwargs):
        vision_calls.append({"args": args, "kwargs": kwargs})
        now = utcnow()
        return LiveEvent(
            id=uuid4(),
            channel_id=uuid4(),
            occurred_at=now,
            ingested_at=now,
            event_type="vision.summary",
            payload={"attachment_id": str(uuid4()), "summary": "private clip"},
            sha256="camera-policy-probe",
        )

    monkeypatch.setattr(vision, "analyze_attachment", fake_analyze)
    policy_calls: list[dict] = []
    _deny_policy(monkeypatch, policy_calls, edith_api)

    response = await client.post(
        "/v1/vision/analyze",
        json={"attachment_id": str(uuid4()), "permission": True},
    )

    assert policy_calls, (
        "camera analysis did not consult the canonical policy; "
        f"status={response.status_code}, vision_calls={vision_calls}"
    )
    assert not vision_calls, "camera analysis ran after policy denial"


async def test_approved_software_action_is_dispatched_to_typed_operation(
    client,
    monkeypatch,
) -> None:
    """An approved runtime software action must not become a false success."""

    from app.ev import tools

    created = await client.post(
        "/v1/runtime/actions",
        json={
            "action_type": "execute_command",
            "title": "workspace smoke test",
            "payload": {"command": "echo hello"},
            "auto_approve": True,
        },
    )
    assert created.status_code == 201, created.text
    action_id = created.json()["id"]
    approved = await client.post(f"/v1/runtime/actions/{action_id}/approve")
    assert approved.status_code == 200, approved.text

    dispatch_calls: list[dict] = []

    async def fake_dispatch(*args, **kwargs):
        dispatch_calls.append({"args": args, "kwargs": kwargs})
        return ToolCallResponse(
            name="execute_command",
            ok=True,
            result={"ok": True, "operation": "workspace_smoke_test"},
            latency_ms=0,
        )

    monkeypatch.setattr(tools, "dispatch", fake_dispatch)
    executed = await client.post(
        f"/v1/runtime/actions/{action_id}/execute",
        json={"result": {}},
    )

    assert executed.status_code == 200, executed.text
    assert dispatch_calls, "approved execute_command was marked executed without typed dispatch"
    assert dispatch_calls[0]["args"][1:] == (
        "execute_command",
        {"command": "echo hello"},
    )


async def test_protocol_sheet_does_not_call_uncredentialed_calendar_enabled(
    db_session: AsyncSession,
) -> None:
    """An active row without provider credentials is setup-required, not enabled."""

    db_session.add(
        Integration(
            slug="uncredentialed-calendar",
            adapter="calendar",
            name="Uncredentialed calendar",
            scopes=["calendar:read"],
            status="active",
            config={},
        )
    )
    await db_session.flush()

    from app.ev.protocols import protocol_sheet

    calendar = next(
        item for item in await protocol_sheet(db_session) if item.key == "calendar"
    )
    assert calendar.status != "enabled"
    assert any(
        word in calendar.detail.lower()
        for word in ("credential", "connect", "authorize", "setup")
    )


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("EVIE, WHAT'S ON MY CALENDAR?!", ("calendar_read", {})),
        ("EVIE, WHAT ARE YOUR CAPABILITIES?!", None),
        ("EVIE, SHOW THE LAB CAMERA FROM 4PM", None),
    ],
)
def test_regex_fallback_is_punctuation_tolerant_and_actuator_safe(transcript, expected) -> None:
    """The no-tools fallback handles safe reads but never selects camera/control actions."""

    from app.ev.tool_select import resolve_live_action

    assert resolve_live_action(transcript) == expected
