"""Focused Calendar/Messages life-bridge capability projection probes."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.capabilities import build_runtime_projection
from app.ev.protocols import capability_reply, protocol_sheet
from app.models import Integration
from app.voice.live.layer import build_live_capability_manifest


async def test_granted_calendar_and_messages_bridges_are_enabled_everywhere(
    db_session: AsyncSession,
) -> None:
    """A granted bridge must not be rendered as setup-required or unavailable."""

    db_session.add_all(
        [
            Integration(
                slug="calendar-life-bridge",
                adapter="calendar",
                name="Calendar",
                scopes=["calendar:read"],
                status="active",
                config={"provider": "local"},
            ),
            Integration(
                slug="messages-life-bridge",
                adapter="messaging",
                name="Messages",
                scopes=["messaging:read", "messaging:act"],
                status="active",
                config={"provider": "macos_life"},
            ),
        ]
    )
    await db_session.flush()

    protocols = {item.key: item for item in await protocol_sheet(db_session)}
    projection = await build_runtime_projection(db_session, actor="master")
    by_name = {item["name"]: item for item in projection["capabilities"]}
    live = await capability_reply(db_session, actor="master", channel="action")

    assert protocols["calendar"].status == "enabled"
    assert protocols["messages"].status == "enabled"
    assert protocols["macos_apps"].status == "enabled"
    assert by_name["calendar_read"]["availability"] == "available"
    assert by_name["list_messages"]["availability"] == "available"
    assert by_name["open_app"]["availability"] == "available"
    assert by_name["close_app"]["availability"] == "available"
    assert by_name["open_url"]["availability"] == "available"
    assert "calendar_read" not in {item["name"] for item in live["missing_setup"]}
    assert "list_messages" not in {item["name"] for item in live["missing_setup"]}
    assert "open_app" not in {item["name"] for item in live["missing_setup"]}


def test_enabled_life_bridges_are_not_also_missing_or_unavailable() -> None:
    """A stale mixed-state payload must not render a granted bridge twice."""

    payload = {
        "enabled": ["Calendar / leave-by", "Messages via life bridge"],
        "protocols": [
            {
                "key": "calendar",
                "title": "Calendar / leave-by",
                "status": "enabled",
            },
            {
                "key": "messages",
                "title": "Messages via life bridge",
                "status": "enabled",
            },
        ],
        "missing_permissions": [
            {"key": "calendar", "title": "Calendar / leave-by", "status": "needs_setup"},
            {"key": "messages", "title": "Messages via life bridge", "status": "needs_setup"},
        ],
        "unavailable": [
            {"key": "calendar", "title": "Calendar / leave-by", "status": "needs_setup"},
            {"key": "messages", "title": "Messages via life bridge", "status": "needs_setup"},
        ],
    }

    manifest = build_live_capability_manifest(payload)

    enabled = set(manifest["enabled"])
    assert enabled >= {
        "Calendar / leave-by",
        "Messages via life bridge",
    }
    missing_overlap = {
        item["title"] for item in manifest["missing_permissions"]
    } & enabled
    unavailable_overlap = {item["title"] for item in manifest["unavailable"]} & enabled
    assert not missing_overlap and not unavailable_overlap, {
        "missing_permissions": sorted(missing_overlap),
        "unavailable": sorted(unavailable_overlap),
    }


def test_provider_setup_is_not_reported_as_a_missing_permission() -> None:
    payload = {
        "protocols": [
            {
                "key": "calendar",
                "title": "Calendar / leave-by",
                "status": "needs_setup",
                "detail": "calendar adapter is not installed.",
            },
            {
                "key": "messages",
                "title": "Messages via life bridge",
                "status": "needs_setup",
                "detail": "messaging adapter is not installed.",
            },
            {
                "key": "instant_kill",
                "title": "Instant Kill",
                "status": "refused",
                "detail": "never.",
            },
        ],
        "enabled": [],
    }

    manifest = build_live_capability_manifest(payload)

    assert manifest["missing_permissions"] == []
    assert {item["title"] for item in manifest["unavailable"]} == {
        "Calendar / leave-by",
        "Messages via life bridge",
    }
    assert all(item["status"] != "refused" for item in manifest["unavailable"])


def test_client_does_not_promote_provider_setup_to_missing_permission() -> None:
    """The Swift client must preserve setup-vs-permission semantics."""

    repo = Path(__file__).resolve().parents[2]
    source = (repo / "ios/EVClient/Sources/EVClient/Models.swift").read_text()
    capability_model = source[
        source.index("public struct CapabilityManifest") : source.index(
            "public enum DevicePresence"
        )
    ]
    json_decoder = capability_model[capability_model.index("public init(json") :]
    needs_permission_block = json_decoder.split("needsPermission:", 1)[1].split(
        "unavailable:", 1
    )[0]

    assert 'protocolTitles(with: "needs_setup")' not in needs_permission_block
