"""Cross-surface HUD contract: one ev.hud.card.v1 schema for every renderer.

The same card JSON is produced by the server, rendered by the CLI and web
workbench, and validated by the shared iOS/watchOS client. This test pins the
contract so no surface can drift.
"""

from __future__ import annotations

import json
from pathlib import Path

from httpx import AsyncClient

from clients.cli import card as cli_card
from clients.cli import quickcard as cli_quickcard

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "ev-hud-card-v1.json"
QUICKCARD_SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "ev-hud-quickcard-v1.json"
BRIEFING_SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "ev-hud-briefing-v1.json"
FOCUS_SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "ev-hud-focus-v1.json"
ROUTE_SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "ev-hud-route-v1.json"
ALERT_SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "ev-hud-alert-v1.json"
OPS_SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "ev-hud-ops-v1.json"
LOOKOUT_SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "ev-hud-lookout-v1.json"
WEB_ROOT = REPO_ROOT / "backend" / "clients" / "web"
SWIFT_CLIENT = REPO_ROOT / "ios" / "EVClient" / "Sources" / "EVClient" / "HUDCard.swift"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_quickcard_schema() -> dict:
    return json.loads(QUICKCARD_SCHEMA_PATH.read_text(encoding="utf-8"))


def load_schema_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_card(payload: dict, schema: dict) -> list[str]:
    """Minimal JSON-Schema validation for the ev.hud.card.v1 contract."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload is not an object"]
    required = schema.get("required", [])
    for key in required:
        if key not in payload:
            errors.append(f"missing required field: {key}")

    def matches_type(value: object, expected: str) -> bool:
        if expected == "string":
            return isinstance(value, str)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "object":
            return isinstance(value, dict)
        return True

    props = schema.get("properties", {})
    for key, rule in props.items():
        if key not in payload:
            continue
        value = payload[key]
        expected_types = rule.get("type")
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if isinstance(expected_types, list):
            if value is None:
                if "null" not in expected_types:
                    errors.append(f"{key} must not be null")
            elif not any(matches_type(value, expected) for expected in expected_types):
                errors.append(f"{key} has the wrong type")
        if "const" in rule and value != rule["const"]:
            errors.append(f"{key} must be {rule['const']!r}, got {value!r}")
        if "enum" in rule and isinstance(rule["enum"], list) and value not in rule["enum"]:
            errors.append(f"{key} must be one of {rule['enum']}, got {value!r}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in rule and value < rule["minimum"]:
                errors.append(f"{key} must be >= {rule['minimum']}, got {value!r}")
            if "maximum" in rule and value > rule["maximum"]:
                errors.append(f"{key} must be <= {rule['maximum']}, got {value!r}")
    return errors


async def seed_pending_alert(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/alerts/watchlist",
        json={"kind": "topic", "value": "sqlite", "priority": 0.8},
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": "I decided to use SQLite."},
    )
    assert resp.status_code == 201, resp.text
    resp = await client.get("/v1/alerts/scan?window_days=7")
    assert resp.status_code == 200, resp.text
    assert resp.json()["alerts_created"]


async def test_schema_is_canonical() -> None:
    schema = load_schema()
    assert schema["properties"]["schema_version"]["const"] == "ev.hud.card.v1"
    assert set(schema["required"]) == {
        "schema_version",
        "generated_at",
        "title",
        "body",
        "priority",
    }


async def test_server_hud_card_validates_against_schema(client: AsyncClient) -> None:
    schema = load_schema()
    resp = await client.get("/v1/hud/card")
    assert resp.status_code == 200, resp.text
    assert validate_card(resp.json(), schema) == []


async def test_cli_hud_render_matches_schema(client: AsyncClient) -> None:
    schema = load_schema()
    hud = await cli_card(client=client)
    assert validate_card(hud, schema) == []
    # The CLI prints the exact schema identity so scripted consumers can branch.
    assert hud["schema_version"] == "ev.hud.card.v1"


async def test_web_workbench_renders_schema_badge() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    js = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert 'id="hud-schema"' in html
    assert "hud.schema_version" in js
    assert 'id="hud-card-body"' in html
    assert "renderSchemaCard(hud.value)" in js


async def test_ios_client_validates_same_schema_version() -> None:
    source = SWIFT_CLIENT.read_text(encoding="utf-8")
    assert 'static let schemaVersionV1 = "ev.hud.card.v1"' in source
    assert "unsupportedSchema" in source
    assert "renderText" in source
    quickcard_source = (
        REPO_ROOT / "ios" / "EVClient" / "Sources" / "EVClient" / "HUDQuickCard.swift"
    ).read_text(encoding="utf-8")
    assert 'static let schemaVersionV1 = "ev.hud.quickcard.v1"' in quickcard_source
    assert "quickCard" in (
        REPO_ROOT / "ios" / "EVClient" / "Sources" / "EVClient" / "EVAPIClient.swift"
    ).read_text(encoding="utf-8")


async def test_quickcard_schema_is_canonical() -> None:
    schema = load_quickcard_schema()
    assert schema["properties"]["schema_version"]["const"] == "ev.hud.quickcard.v1"
    assert set(schema["required"]) == {"schema_version", "generated_at", "objective", "summary"}


async def test_server_quickcard_validates_against_schema(client: AsyncClient) -> None:
    schema = load_quickcard_schema()
    resp = await client.get("/v1/tactical/quick", params={"topic": "Renegotiation with X"})
    assert resp.status_code == 200, resp.text
    assert validate_card(resp.json(), schema) == []


async def test_cli_quickcard_matches_schema(client: AsyncClient) -> None:
    schema = load_quickcard_schema()
    hud = await cli_quickcard("Renegotiation with X", client=client)
    assert validate_card(hud, schema) == []
    assert hud["schema_version"] == "ev.hud.quickcard.v1"


async def test_server_briefing_validates_against_schema(client: AsyncClient) -> None:
    schema = load_schema_file(BRIEFING_SCHEMA_PATH)
    assert schema["properties"]["schema_version"]["const"] == "ev.hud.briefing.v1"
    resp = await client.post(
        "/v1/tactical/brief",
        json={"topic": "Renegotiation with X"},
    )
    assert resp.status_code == 200, resp.text
    assert validate_card(resp.json(), schema) == []


async def test_server_focus_validates_against_schema(client: AsyncClient) -> None:
    schema = load_schema_file(FOCUS_SCHEMA_PATH)
    assert schema["properties"]["schema_version"]["const"] == "ev.hud.focus.v1"
    resp = await client.get("/v1/hud/focus")
    assert resp.status_code == 200, resp.text
    assert validate_card(resp.json(), schema) == []


async def test_server_route_validates_against_schema(client: AsyncClient) -> None:
    schema = load_schema_file(ROUTE_SCHEMA_PATH)
    assert schema["properties"]["schema_version"]["const"] == "ev.hud.route.v1"
    resp = await client.get("/v1/hud/route")
    assert resp.status_code == 200, resp.text
    assert validate_card(resp.json(), schema) == []


async def test_alert_schema_is_canonical() -> None:
    schema = load_schema_file(ALERT_SCHEMA_PATH)
    assert schema["properties"]["schema_version"]["const"] == "ev.hud.alert.v1"
    assert set(schema["required"]) == {
        "schema_version",
        "generated_at",
        "alert_id",
        "title",
        "body",
        "priority",
        "tier",
    }


async def test_server_alert_validates_against_schema(client: AsyncClient) -> None:
    await seed_pending_alert(client)
    schema = load_schema_file(ALERT_SCHEMA_PATH)
    resp = await client.get("/v1/hud/alerts")
    assert resp.status_code == 200, resp.text
    cards = resp.json()
    assert cards
    for card in cards:
        assert validate_card(card, schema) == []


async def test_ops_schema_is_canonical() -> None:
    schema = load_schema_file(OPS_SCHEMA_PATH)
    assert schema["properties"]["schema_version"]["const"] == "ev.hud.ops.v1"
    assert set(schema["required"]) == {
        "schema_version",
        "generated_at",
        "title",
        "summary",
        "focus_locked",
        "online_devices",
        "pending_alerts",
        "open_decisions",
    }


async def test_server_ops_validates_against_schema(client: AsyncClient) -> None:
    schema = load_schema_file(OPS_SCHEMA_PATH)
    resp = await client.get("/v1/hud/ops")
    assert resp.status_code == 200, resp.text
    assert validate_card(resp.json(), schema) == []


async def test_lookout_schema_is_canonical() -> None:
    schema = load_schema_file(LOOKOUT_SCHEMA_PATH)
    assert schema["properties"]["schema_version"]["const"] == "ev.hud.lookout.v1"
    assert set(schema["required"]) == {
        "schema_version",
        "generated_at",
        "open",
        "windows",
        "rationale",
    }


async def test_swift_hud_briefing_models_match_schemas() -> None:
    source = (
        REPO_ROOT / "ios" / "EVClient" / "Sources" / "EVClient" / "HUDBriefings.swift"
    ).read_text(encoding="utf-8")
    assert 'static let schemaVersionV1 = "ev.hud.briefing.v1"' in source
    assert 'static let schemaVersionV1 = "ev.hud.focus.v1"' in source
    assert 'static let schemaVersionV1 = "ev.hud.route.v1"' in source
    assert "renderText" in source
