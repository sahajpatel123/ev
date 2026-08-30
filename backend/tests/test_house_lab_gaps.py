"""Skeptic-gap tests: real HTTP + vault + device tokens, not fake actor strings."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.training_wheels import TRAINING_STEPS, complete_step
from app.main import app
from app.models import FeatureGate, Integration
from app.utils.text import utcnow


async def _unlock(db_session: AsyncSession) -> None:
    from app.ev.assistant import get_profile

    profile = await get_profile(db_session)
    profile.training_steps = {step: utcnow().isoformat() for step in TRAINING_STEPS}
    from app.ev.protocols import complete_training_wheels

    done = await complete_training_wheels(db_session)
    assert done.get("completed") is True
    await db_session.commit()


def _device_client(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_complete_training_wheels_refuses_until_checklist(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.ev.training_wheels import ensure_seed_gates

    await ensure_seed_gates(db_session)
    await db_session.commit()
    start = await client.post("/v1/assistant/training-wheels/start")
    assert start.status_code == 200
    refused = await client.post("/v1/assistant/training-wheels/complete")
    assert refused.status_code == 200, refused.text
    body = refused.json()
    assert body["completed"] is False
    assert body["error"] == "training_wheels_incomplete"
    assert set(body["remaining"]) == set(TRAINING_STEPS)
    row = (
        await db_session.execute(select(FeatureGate).where(FeatureGate.key == "life.call"))
    ).scalar_one()
    assert row.status == "locked"


async def test_delegate_device_token_cannot_use_home_or_call(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _unlock(db_session)
    created = await client.post(
        "/v1/devices",
        json={"name": "Ned Phone", "capabilities": ["attention"], "trust_level": "device"},
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
                "scopes": ["calendar:read"],
                "device_id": device_id,
            },
            "allow_sensitive": True,
        },
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["ok"] is True, granted.text
    assert granted.json()["result"]["ok"] is True
    assert granted.json()["result"]["device_id"] == device_id

    async with _device_client(token) as ned:
        home = await ned.post(
            "/v1/gateway/tools",
            json={"name": "home_status", "arguments": {}},
        )
        assert home.status_code == 200, home.text
        assert home.json()["ok"] is False
        home_err = home.json().get("error") or ""
        assert home_err == "delegate_scope" or "home:read" in home_err or "home:read" in (
            home.json().get("error") or ""
        )

        call = await ned.post(
            "/v1/gateway/tools",
            json={
                "name": "place_call",
                "arguments": {"name": "Mom", "confirm": True},
                "allow_sensitive": True,
                "life_verified": True,
            },
        )
        assert call.status_code == 200, call.text
        assert call.json()["ok"] is False
        assert call.json()["error"] in {
            "delegate_scope",
            "biometric_required",
            "missing scopes: phone:act",
        } or "phone:act" in (call.json().get("error") or "")


async def test_home_act_uses_vault_token_on_homeassistant(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    await _unlock(db_session)
    installed = await client.post(
        "/v1/integrations",
        json={
            "adapter": "smart_home",
            "name": "Lab HA",
            "scopes": ["home:read", "home:act"],
            "config": {
                "provider": "homeassistant",
                "base_url": "http://ha.example:8123",
            },
        },
    )
    assert installed.status_code == 201, installed.text
    integration_id = installed.json()["id"]
    stored = await client.post(
        f"/v1/integrations/{integration_id}/credentials",
        json={"access_token": "ha-vault-token-xyz"},
    )
    assert stored.status_code in {200, 201}, stored.text

    calls: list[tuple[str, str, str | None]] = []

    class DummyClient:
        posted = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, headers=None, json=None):
            type(self).posted = True
            calls.append(("POST", str(url), (headers or {}).get("Authorization")))
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: [])

        async def get(self, url, headers=None):
            calls.append(("GET", str(url), (headers or {}).get("Authorization")))
            state = "on" if type(self).posted else "off"
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"state": state, "entity_id": "light.lab"},
            )

    monkeypatch.setattr("app.ev.home.httpx.AsyncClient", DummyClient)

    acted = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "home_act",
            "arguments": {"entity": "lab lights", "action": "on", "confirm": True},
            "allow_sensitive": True,
        },
    )
    assert acted.status_code == 200, acted.text
    body = acted.json()
    assert body["ok"] is True, body
    assert body["result"]["ok"] is True
    assert body["result"]["simulated"] is False
    assert body["result"]["new_state"] == "on"
    assert calls
    assert any(auth == "Bearer ha-vault-token-xyz" for _m, _u, auth in calls)
    assert any("/api/services/" in url for _m, url, _a in calls)
    assert any("/api/states/" in url for _m, url, _a in calls)


async def test_device_life_action_requires_reverify_not_body_flag(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _unlock(db_session)
    db_session.add(
        Integration(
            slug="phone-reverify",
            adapter="phone",
            name="phone",
            scopes=["phone:act"],
            status="active",
            config={"provider": "local"},
        )
    )
    await db_session.commit()
    owner = await client.post("/v1/identity/owner", json={"display_name": "Sahaj"})
    assert owner.status_code == 201, owner.text
    created = await client.post(
        "/v1/devices",
        json={"name": "Owner Phone", "capabilities": ["voice"], "trust_level": "owner"},
    )
    token = created.json()["token"]

    async with _device_client(token) as phone:
        denied = await phone.post(
            "/v1/gateway/tools",
            json={
                "name": "place_call",
                "arguments": {"name": "Ned", "confirm": True},
                "allow_sensitive": True,
                "life_verified": True,
            },
        )
        assert denied.status_code == 200, denied.text
        assert denied.json()["ok"] is False
        assert denied.json()["error"] == "biometric_required"

        issued = await phone.post(
            "/v1/identity/reverification",
            json={"purpose": "life.action"},
        )
        assert issued.status_code == 200, issued.text
        proof = issued.json()["token"]
        allowed = await phone.post(
            "/v1/gateway/tools",
            json={
                "name": "place_call",
                "arguments": {"name": "Ned", "confirm": True},
                "allow_sensitive": True,
            },
            headers={"X-EV-Reverify": proof},
        )
        assert allowed.status_code == 200, allowed.text
        payload = allowed.json()
        assert payload.get("error") != "biometric_required"
        result = payload.get("result") or {}
        assert result.get("error") != "biometric_required"


async def test_speaker_verified_voice_actor_cannot_authorize_r3_alone(
    db_session: AsyncSession,
) -> None:
    """Voice wake is not action authorization. R3 needs HUD/biometric confirmation."""

    from app.ev import tools as ev_tools
    from app.ev.turn import execute_requested_actions

    await _unlock(db_session)
    db_session.add(
        Integration(
            slug="phone-r3",
            adapter="phone",
            name="phone",
            scopes=["phone:act"],
            status="active",
            config={"provider": "local"},
        )
    )
    await db_session.commit()
    dispatched = await ev_tools.dispatch(
        db_session,
        "place_call",
        {"name": "Ned", "confirm": True},
        actor="voice",
        allow_sensitive=True,
        channel="voice",
    )
    assert dispatched.ok is False
    assert dispatched.error == "confirmation_required"
    result = dispatched.result or {}
    assert result.get("independent_confirmation") is True
    assert result.get("error") != "biometric_required"
    assert "confirm it on your phone" in str(result.get("spoken") or "").lower()

    receipts = await execute_requested_actions(
        db_session,
        "Call Ned",
        actor="voice",
        allow_sensitive=True,
    )
    assert receipts
    for receipt in receipts:
        payload = receipt.result if isinstance(getattr(receipt, "result", None), dict) else {}
        assert getattr(receipt, "error", None) != "biometric_required"
        if receipt.name == "place_call":
            assert receipt.error == "confirmation_required" or payload.get("error") == "confirmation_required"
            assert payload.get("independent_confirmation") is True
