"""WhatsApp Desktop AX transport: policy, honesty, and helper mapping.

Desktop is the owner's only WhatsApp autosend transport. These tests pin the
transport-selection rule (no Web fallback), the refusal paths, and the rule
that a send is never reported successful without helper thread evidence.
"""

from __future__ import annotations

import pytest

from app.ev.messaging import whatsapp_desktop
from app.ev.messaging.routing import route_channel
from app.integrations.life_helper import LifeHelperError, LifeHelperResult


def test_desktop_is_the_only_whatsapp_autosend_transport() -> None:
    desktop = route_channel("whatsapp", helper_available=True, desktop_available=True)
    assert desktop.provider == "desktop"
    assert desktop.mode == "send"
    assert desktop.requires_approval is True

    # A failed desktop probe refuses — it never silently becomes Web.
    refused = route_channel("whatsapp", helper_available=True, desktop_available=False)
    assert refused.mode == "unavailable"
    assert refused.provider == "none"
    assert "didn't send" in refused.spoken

    # Legacy callers that did not probe keep the old web/compose routing.
    legacy = route_channel("whatsapp", helper_available=True, web_available=True)
    assert legacy.provider == "web"


@pytest.mark.asyncio
async def test_available_maps_helper_status_async(monkeypatch) -> None:
    monkeypatch.setattr(whatsapp_desktop, "_under_pytest", lambda: False)
    cases = [
        (
            {"ok": True, "running": True, "installed": True, "accessibility_trusted": True},
            (True, "ok"),
        ),
        (
            {"ok": True, "running": False, "installed": True, "accessibility_trusted": True},
            (True, "cold"),
        ),
        (
            {"ok": True, "running": False, "installed": False, "accessibility_trusted": True},
            (False, "whatsapp_not_installed"),
        ),
        (
            {"ok": True, "running": True, "installed": True, "accessibility_trusted": False},
            (False, "accessibility_not_granted"),
        ),
        ({"ok": False, "error": "helper_missing"}, (False, "helper_missing")),
    ]
    for payload, expected in cases:
        async def run(_command, _args=None, _payload=payload, **_kwargs):
            return dict(_payload)

        monkeypatch.setattr(whatsapp_desktop, "_run", run)
        assert await whatsapp_desktop.available(refresh=True) == expected


async def _result(data: dict) -> LifeHelperResult:
    return LifeHelperResult(command="whatsapp.ax_send", data=data, delivery={})


@pytest.mark.asyncio
async def test_send_requires_thread_evidence(monkeypatch) -> None:
    monkeypatch.setattr(whatsapp_desktop, "helper_path", lambda: "/tmp/ev-helper")

    async def unverified(*_args, **_kwargs):
        return await _result(
            {"sent": True, "verified_in_thread": False, "to": "John", "focus_stolen": False}
        )

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", unverified)
    outcome = await whatsapp_desktop.send("John", "running late")
    assert outcome["ok"] is False
    assert outcome["sent"] is False
    assert outcome["error"] == "send_not_confirmed"

    async def verified(*_args, **_kwargs):
        return await _result(
            {
                "sent": True,
                "verified_in_thread": True,
                "to": "John",
                "focus_stolen": True,
                "focus_restored": True,
                "hidden_restored": True,
            }
        )

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", verified)
    outcome = await whatsapp_desktop.send("John", "running late")
    assert outcome["ok"] is True
    assert outcome["sent"] is True
    # Brief activation is reported, and the restore is recorded.
    assert outcome["focus_theft"] == 1
    assert outcome["focus_restored"] is True
    assert "John" in outcome["spoken"]


@pytest.mark.asyncio
async def test_send_maps_not_found_and_ambiguous(monkeypatch) -> None:
    monkeypatch.setattr(whatsapp_desktop, "helper_path", lambda: "/tmp/ev-helper")

    async def not_found(*_args, **_kwargs):
        return await _result({"sent": False, "error": "chat_not_found", "to": "Nobody"})

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", not_found)
    outcome = await whatsapp_desktop.send("Nobody", "hi")
    assert outcome["ok"] is False
    assert outcome["error"] == "chat_not_found"
    assert "Nobody" in outcome["spoken"]

    async def ambiguous(*_args, **_kwargs):
        return await _result(
            {"sent": False, "error": "ambiguous_chat", "to": "John", "candidates": ["John A", "John B"]}
        )

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", ambiguous)
    outcome = await whatsapp_desktop.send("John", "hi")
    assert outcome["ok"] is False
    assert outcome["candidates"] == ["John A", "John B"]
    assert "John A" in outcome["spoken"]


@pytest.mark.asyncio
async def test_outdated_helper_is_named(monkeypatch) -> None:
    monkeypatch.setattr(whatsapp_desktop, "helper_path", lambda: "/tmp/ev-helper")

    async def outdated(*_args, **_kwargs):
        raise LifeHelperError("unsupported", error_code="unsupported_command")

    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", outdated)
    outcome = await whatsapp_desktop.send("John", "hi")
    assert outcome["ok"] is False
    assert outcome["error"] == "helper_outdated"


def test_unavailable_next_step_names_the_fix() -> None:
    assert "Accessibility" in whatsapp_desktop.unavailable_next_step(
        "accessibility_not_granted"
    )
    assert "installed" in whatsapp_desktop.unavailable_next_step("whatsapp_not_installed")
