"""Focused tests for the authoritative runtime capability projection."""

from __future__ import annotations

from datetime import timedelta

from app.config import settings
from app.ev.capabilities import (
    RUNTIME_FIELDS,
    approved_realtime_function_tools,
    build_runtime_projection,
)
from app.ev.tool_select import LIVE_VOICE_TOOLS
from app.models import Device, Integration, VoiceSession
from app.utils.text import utcnow


def test_realtime_tools_are_state_filtered_and_auto_selected() -> None:
    projection = {
        "capabilities": [
            {
                "name": "available_read",
                "description": "Read a current value.",
                "json_schema": {"type": "object", "additionalProperties": False},
                "availability": "available",
                "risk_class": "R0",
            },
            {
                "name": "disconnected_write",
                "description": "Unavailable write.",
                "parameters": {"type": "object"},
                "availability": "not_connected",
                "risk_class": "R2",
            },
            {
                "name": "forbidden_action",
                "description": "Never expose.",
                "parameters": {"type": "object"},
                "availability": "available",
                "risk_class": "forbidden",
            },
        ]
    }

    tools = approved_realtime_function_tools(projection)

    assert tools == [
        {
            "type": "function",
            "name": "available_read",
            "description": "Read a current value.",
            "parameters": {"type": "object", "additionalProperties": False},
        }
    ]


def test_live_allowlist_includes_real_daily_tools() -> None:
    assert {
        "search_web",
        "calendar_add",
        "list_protocols",
        "get_health_trends",
        "get_gear_status",
        "brief_me",
        "open_url",
        "open_app",
        "close_app",
        "look",
    } <= LIVE_VOICE_TOOLS


def test_realtime_daily_tools_are_openai_function_payloads() -> None:
    from app.ev.tools import get_spec

    names = {
        "search_web",
        "calendar_add",
        "list_protocols",
        "get_health_trends",
        "get_gear_status",
        "brief_me",
    }
    projection = {
        "live_tool_projection": [
            {
                **get_spec(name),
                "availability": "available",
                "model_exposed": True,
                "realtime_eligible": True,
            }
            for name in sorted(names)
        ]
    }

    tools = approved_realtime_function_tools(projection)
    assert {tool["name"] for tool in tools} == names
    assert all(tool["type"] == "function" for tool in tools)
    assert all(tool["parameters"].get("type") == "object" for tool in tools)


def test_realtime_projection_does_not_expose_unavailable_daily_tools() -> None:
    projection = {
        "live_tool_projection": [
            {
                "name": "search_web",
                "description": "Search the web.",
                "parameters": {"type": "object"},
                "availability": "not_connected",
                "model_exposed": False,
                "realtime_eligible": False,
            },
            {
                "name": "brief_me",
                "description": "Brief me.",
                "parameters": {"type": "object"},
                "availability": "available",
                "model_exposed": True,
                "realtime_eligible": True,
            },
        ]
    }

    tools = approved_realtime_function_tools(projection)
    assert [tool["name"] for tool in tools] == ["brief_me"]


async def test_projection_contains_runtime_contract_and_filters_unavailable_tools(
    db_session,
) -> None:
    now = utcnow()
    device = Device(
        name="owner-phone",
        device_type="phone",
        platform="ios",
        capabilities=["voice", "camera"],
        last_seen_at=now - timedelta(seconds=5),
    )
    db_session.add(device)
    await db_session.commit()

    projection = await build_runtime_projection(db_session, device_id=device.id, now=now)
    by_name = {entry["name"]: entry for entry in projection["capabilities"]}

    assert projection["schema_version"] == "ev.capability-manifest.v1"
    assert projection["device"]["id"] == str(device.id)
    assert projection["device"]["online"] is True
    assert {
        "name",
        "version",
        "description",
        "parameters",
        "json_schema",
        "output",
        "provider",
        "device",
        "availability",
        "availability_reason",
        "required_scopes",
        "risk_class",
        "confirmation",
        "evidence",
        "cancellation",
        "fallback",
    } <= set(RUNTIME_FIELDS)
    assert set(RUNTIME_FIELDS) <= set(by_name["get_weather"])
    assert by_name["get_weather"]["provider"] == "open-meteo"
    assert by_name["get_weather"]["availability"] == "available"
    assert by_name["calendar_read"]["availability"] == "not_connected"
    assert by_name["camera_replay"]["provider"] == "camera"
    assert by_name["camera_replay"]["availability"] == "not_connected"
    assert by_name["ticket_buy"]["risk_class"] == "R4"
    assert by_name["ticket_buy"]["model_exposed"] is False

    realtime = projection["realtime"]
    assert realtime["tool_choice"] == "auto"
    realtime_names = {tool["name"] for tool in realtime["tools"]}
    assert "get_weather" in realtime_names
    assert "calendar_read" not in realtime_names
    assert "camera_replay" not in realtime_names
    assert "ticket_buy" not in realtime_names
    assert all(tool["type"] == "function" for tool in realtime["tools"])


async def test_projection_tracks_provider_scopes_and_credentials(db_session) -> None:
    row = Integration(
        slug="local-calendar",
        adapter="calendar",
        name="Local calendar bridge",
        scopes=["calendar:read"],
        config={"provider": "local"},
        status="active",
    )
    db_session.add(row)
    await db_session.commit()

    projection = await build_runtime_projection(db_session)
    calendar = next(
        entry for entry in projection["capabilities"] if entry["name"] == "calendar_read"
    )

    assert calendar["availability"] == "available"
    assert calendar["current_provider"] == "local"
    assert calendar["provider_scopes"] == ["calendar:read"]
    assert calendar["missing_provider_scopes"] == []
    assert calendar["provider_credential_ready"] is True


async def test_calendar_add_requires_calendar_write_or_act_scope(db_session) -> None:
    row = Integration(
        slug="calendar-write-gate",
        adapter="calendar",
        name="Calendar write gate",
        scopes=["calendar:read"],
        config={"provider": "local"},
        status="active",
    )
    db_session.add(row)
    await db_session.commit()

    projection = await build_runtime_projection(db_session, actor="master")
    calendar_add = next(
        entry for entry in projection["capabilities"] if entry["name"] == "calendar_add"
    )
    assert calendar_add["availability"] == "not_connected"
    assert calendar_add["model_exposed"] is False
    assert "calendar_add" not in projection["approved_tools"]

    row.scopes = ["calendar:act"]
    await db_session.commit()
    projection = await build_runtime_projection(db_session, actor="master")
    calendar_add = next(
        entry for entry in projection["capabilities"] if entry["name"] == "calendar_add"
    )
    assert calendar_add["availability"] == "available"
    assert calendar_add["provider_scopes"] == ["calendar:act"]
    assert calendar_add["required_scopes"] == ["calendar:write"]
    assert calendar_add["model_exposed"] is True
    assert "calendar_add" in projection["approved_tools"]


async def test_brave_search_without_key_is_not_exposed(monkeypatch, db_session) -> None:
    monkeypatch.setattr(settings, "search_provider", "brave")
    monkeypatch.setattr(settings, "brave_search_api_key", None)

    projection = await build_runtime_projection(db_session, realtime_provider="openai")
    by_name = {entry["name"]: entry for entry in projection["capabilities"]}

    assert by_name["search_web"]["availability"] == "not_connected"
    assert by_name["search_web"]["provider_credential_ready"] is False
    assert "Brave Search API key" in by_name["search_web"]["availability_reason"]
    assert "search_web" not in {tool["name"] for tool in projection["tools"]}
    assert projection["realtime"]["provider"] == "openai"


async def test_actor_and_device_policy_state_controls_executable_and_live_fields(
    db_session,
) -> None:
    now = utcnow()
    device = Device(
        name="unscoped-phone",
        device_type="phone",
        trust_level="device",
        last_seen_at=now,
    )
    db_session.add(device)
    await db_session.commit()

    projection = await build_runtime_projection(
        db_session,
        actor="device:unscoped-phone",
        device_id=device.id,
        realtime_provider="openai",
        now=now,
    )
    by_name = {entry["name"]: entry for entry in projection["capabilities"]}
    calibrate = by_name["calibrate"]

    assert projection["actor_kind"] == "device"
    assert projection["device_id"] == str(device.id)
    assert calibrate["device_status"] == "bound"
    assert calibrate["approved"] is False
    assert calibrate["executable"] is False
    assert calibrate["approval_state"] == "denied"
    assert calibrate["model_exposed"] is False
    assert "calibrate" not in {tool["name"] for tool in projection["tools"]}


async def test_tools_alias_is_live_allowlist_only(db_session) -> None:
    projection = await build_runtime_projection(db_session, actor="master")
    live_names = {tool["name"] for tool in projection["tools"]}
    projected_names = {entry["name"] for entry in projection["live_tool_projection"]}

    assert live_names <= LIVE_VOICE_TOOLS
    assert projected_names <= LIVE_VOICE_TOOLS
    assert live_names == projected_names
    all_names = {tool["name"] for tool in projection["all_realtime_tools"]}
    assert all_names >= live_names
    assert all_names - live_names


def _assert_openai_function_schema(tool: dict) -> None:
    assert tool["type"] == "function"
    assert isinstance(tool["name"], str) and tool["name"]
    assert isinstance(tool["description"], str)
    schema = tool["parameters"]
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    assert isinstance(schema.get("properties", {}), dict)
    required = schema.get("required", [])
    assert isinstance(required, list)
    assert set(required) <= set(schema.get("properties", {}))


async def test_live_session_projection_contains_required_callable_functions(db_session) -> None:
    """The live projection is executable metadata, not the protocol label list."""

    projection = await build_runtime_projection(
        db_session,
        actor="master",
        realtime_provider="openai",
        session_id="live-required-tools",
    )
    from app.ev.protocols import protocol_sheet

    protocol_by_key = {item.key: item for item in await protocol_sheet(db_session)}
    assert protocol_by_key["weather"].status == "enabled"
    by_name = {entry["name"]: entry for entry in projection["capabilities"]}
    required = ("get_weather", "start_timer", "calibrate")

    for name in required:
        entry = by_name[name]
        assert entry["availability"] == "available"
        assert entry["model_exposed"] is True
        assert entry["realtime_eligible"] is True
        assert entry["executable"] is True
        assert entry["parameters"] == entry["json_schema"]

    assert projection["session_id"] == "live-required-tools"
    projected_names = projection["diagnostics"]["projected_tool_names"]
    assert projected_names == sorted(projected_names)
    assert set(required) <= set(projected_names)
    executable_names = projection["diagnostics"]["executable_tool_names"]
    assert set(executable_names) <= set(projected_names)
    pending_names = set(projected_names) - set(executable_names)
    assert pending_names
    assert all(
        by_name[name]["confirmation_required"] for name in pending_names
    )
    assert projection["capability_error"] is None
    for tool in projection["realtime"]["tools"]:
        _assert_openai_function_schema(tool)
    assert {tool["name"] for tool in projection["realtime"]["tools"]} >= set(required)


async def test_calendar_read_enters_live_projection_only_with_provider_and_scope(
    db_session,
) -> None:
    missing = await build_runtime_projection(db_session, actor="master")
    missing_calendar = next(
        item for item in missing["capabilities"] if item["name"] == "calendar_read"
    )
    assert missing_calendar["availability"] == "not_connected"
    assert missing_calendar["model_exposed"] is False
    assert "calendar_read" in missing["unavailable_tool_names"]
    assert "calendar_read" not in missing["approved_tools"]

    calendar_provider = Integration(
        slug="live-calendar",
        adapter="calendar",
        name="Live calendar",
        scopes=[],
        config={"provider": "local"},
        status="active",
    )
    db_session.add(calendar_provider)
    await db_session.commit()
    missing_scope = await build_runtime_projection(db_session, actor="master")
    missing_scope_calendar = next(
        item for item in missing_scope["capabilities"] if item["name"] == "calendar_read"
    )
    assert missing_scope_calendar["availability"] == "not_connected"
    assert "missing provider scope" in missing_scope_calendar["availability_reason"]

    calendar_provider.scopes = ["calendar:read"]
    await db_session.commit()
    ready = await build_runtime_projection(
        db_session, actor="master", realtime_provider="openai"
    )
    calendar = next(item for item in ready["capabilities"] if item["name"] == "calendar_read")
    assert calendar["availability"] == "available"
    assert calendar["model_exposed"] is True
    assert calendar["realtime_eligible"] is True
    assert calendar["required_scopes"] == ["calendar:read"]
    assert "calendar_read" in ready["approved_tools"]
    _assert_openai_function_schema(
        next(tool for tool in ready["realtime"]["tools"] if tool["name"] == "calendar_read")
    )


async def test_stale_device_identity_cannot_become_available(db_session) -> None:
    projection = await build_runtime_projection(
        db_session,
        actor="master",
        device_id="stale-mac-device",
        session_id="stale-session",
    )
    weather = next(item for item in projection["capabilities"] if item["name"] == "get_weather")
    assert weather["device_status"] == "unknown"
    assert weather["availability"] == "unavailable"
    assert weather["executable"] is False
    assert weather["model_exposed"] is False
    assert projection["tools"] == []
    assert "get_weather" in projection["diagnostics"]["unavailable_tool_names"]


async def test_confirmation_gated_r3_r4_tools_are_not_live_exposed(db_session) -> None:
    projection = await build_runtime_projection(db_session, actor="master")
    by_name = {entry["name"]: entry for entry in projection["capabilities"]}
    for name in ("place_call", "home_act", "ticket_buy", "execute_command"):
        entry = by_name[name]
        assert entry["risk_class"] in {"R3", "R4"}
        assert entry["model_exposed"] is False
        assert entry["realtime_eligible"] is False
        assert name not in projection["approved_tools"]
    assert {
        name
        for name in ("place_call", "home_act", "ticket_buy", "execute_command")
        if by_name[name]["confirmation_required"]
    } <= set(projection["confirmation_required_tool_names"])


async def test_projection_failure_returns_explicit_safe_capability_error(
    db_session, monkeypatch
) -> None:
    import app.ev.capabilities as capabilities

    def fail_registry() -> list[dict]:
        raise RuntimeError("registry probe failed")

    monkeypatch.setattr(capabilities, "_declared_specs", fail_registry)
    projection = await build_runtime_projection(
        db_session, actor="master", session_id="failed-session"
    )
    assert projection["live_tool_projection"] == []
    assert projection["capability_error"] == "RuntimeError: registry probe failed"
    assert projection["diagnostics"]["capability_error"] == projection["capability_error"]
    assert projection["diagnostics"]["session_id"] == "failed-session"
    from app.ev.protocols import capability_reply

    reply = await capability_reply(db_session, actor="master")
    assert "Live capability projection failed" in reply["reply"]
    assert reply["capability_error"] == projection["capability_error"]


async def test_real_live_session_receives_required_runtime_projection(
    db_session, monkeypatch
) -> None:
    """Exercise the bind path that hands the projection to a live session."""

    from app.auth import ActorContext
    from app.voice.live import transport

    now = utcnow()
    device = Device(
        name="live-mac",
        device_type="computer",
        platform="macos",
        trust_level="owner",
        capabilities=["voice", "wake", "attention"],
        last_seen_at=now,
    )
    db_session.add(device)
    await db_session.flush()
    voice = VoiceSession(
        device_id=str(device.id),
        state="awake",
        owner_verified=True,
        verifier_name="app_open",
        expires_at=now + timedelta(minutes=5),
        follow_up_until=now + timedelta(minutes=5),
    )
    db_session.add(voice)
    await db_session.commit()

    # No upstream provider or audio device is needed to prove projection
    # delivery; the live bind path still creates the actual session object.
    monkeypatch.setattr(transport, "live_transcriber", lambda: None)
    live = await transport.bind_live_session(
        session_id=voice.id,
        ctx=ActorContext(actor="master", is_master=True),
    )
    try:
        manifest = live._capability_manifest
        assert manifest is not None
        runtime = manifest["runtime_manifest"]
        names = set(manifest["realtime_tool_names"])
        assert {"get_weather", "start_timer", "calibrate"} <= names
        assert manifest["live_tool_projection"]
        assert runtime["actor"] == "master"
        assert runtime["device_id"] == str(device.id)
        assert manifest["capability_error"] is None
        assert all(
            tool["type"] == "function"
            and tool["name"] in names
            and tool["parameters"].get("type") == "object"
            for tool in manifest["realtime_tools"]
        )
        ready_manifest = live.ready_event().config["capability_manifest"]
        assert set(ready_manifest["realtime_tool_names"]) == names
    finally:
        live.close()
