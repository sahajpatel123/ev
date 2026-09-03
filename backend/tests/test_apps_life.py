"""POL Phase 3: allowlisted Mac open/close through the macos_life helper."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.apps import parse_owner_url, resolve_app
from app.ev.capabilities import build_runtime_projection
from app.ev.protocols import protocol_sheet, spoken_ready_capability_line
from app.ev.tool_select import resolve_live_action
from app.ev.tools import dispatch
from app.models import Integration
from tests.test_life_bridges import MOCK_HELPER


@pytest.fixture
def mock_life_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "EVLifeHelper"
    path.write_text(MOCK_HELPER, encoding="utf-8")
    path.chmod(0o755)
    monkeypatch.setattr(settings, "life_helper_path", str(path))
    return path


async def _install_macos_life(db_session: AsyncSession, helper: Path) -> Integration:
    row = Integration(
        slug="apps-life",
        adapter="messaging",
        name="Messages",
        scopes=["messaging:read", "messaging:act"],
        status="active",
        config={"provider": "macos_life", "helper_path": str(helper)},
    )
    db_session.add(row)
    await db_session.commit()
    return row


def test_app_and_url_allowlists_are_narrow() -> None:
    assert resolve_app("Safari") == ("safari", "com.apple.Safari")
    assert resolve_app("not-a-real-app") is None
    assert parse_owner_url("https://example.com/path") == "https://example.com/path"
    assert parse_owner_url("file:///etc/passwd") is None
    assert parse_owner_url("javascript:alert(1)") is None
    assert parse_owner_url("spotify:search:lofi") == "spotify:search:lofi"
    assert parse_owner_url("mailto:owner@example.com") == "mailto:owner@example.com"
    assert parse_owner_url("spotify:") is None
    assert resolve_live_action("open Safari") == ("open_app", {"name": "Safari"})
    assert resolve_live_action("can you open safari for me") == (
        "open_app",
        {"name": "safari"},
    )
    assert resolve_live_action("close Messages") == ("close_app", {"name": "Messages"})


async def test_open_close_unavailable_without_macos_life(db_session: AsyncSession) -> None:
    opened = await dispatch(db_session, "open_app", {"name": "Safari"}, actor="master")
    assert opened.result["degraded"] is True
    assert "open-url bridge" in opened.result["next_step"]

    linked = await dispatch(
        db_session, "open_url", {"url": "https://example.com"}, actor="master"
    )
    assert linked.result["degraded"] is True
    assert linked.result["ok"] is False


async def test_open_and_close_allowlisted_app_via_helper(
    db_session: AsyncSession, mock_life_helper: Path
) -> None:
    await _install_macos_life(db_session, mock_life_helper)
    opened = await dispatch(db_session, "open_app", {"name": "Safari"}, actor="master")
    assert opened.ok is True, opened.error
    assert opened.result["ok"] is True
    assert opened.result["opened"] is True
    assert opened.result["spoken"] == "Opened Safari."
    assert opened.result["evidence"]["source"] == "macos_life"
    assert opened.result["evidence"]["accepted"] is True

    closed = await dispatch(db_session, "close_app", {"name": "Safari"}, actor="master")
    assert closed.result["ok"] is True
    assert closed.result["closed"] is True
    assert closed.result["spoken"] == "Closed Safari."

    blocked = await dispatch(db_session, "close_app", {"name": "Finder"}, actor="master")
    assert blocked.result["ok"] is False
    assert blocked.result["error"] == "protected"


async def test_open_url_via_helper(
    client: AsyncClient, db_session: AsyncSession, mock_life_helper: Path
) -> None:
    await _install_macos_life(db_session, mock_life_helper)
    opened = await client.post(
        "/v1/gateway/tools",
        json={"name": "open_url", "arguments": {"url": "https://example.com"}},
    )
    assert opened.status_code == 200, opened.text
    body = opened.json()["result"]
    assert body["ok"] is True, body
    assert body["opened"] is True
    assert body["spoken"] == "Opened https://example.com."


async def test_live_sheet_lists_open_close_only_when_macos_life_connected(
    db_session: AsyncSession, mock_life_helper: Path
) -> None:
    empty = await build_runtime_projection(db_session, actor="master", channel="voice")
    empty_names = {item["name"] for item in empty["live_tool_projection"]}
    assert "open_app" not in empty_names
    assert "close_app" not in empty_names
    assert "open_url" not in empty_names
    empty_protocols = {item.key: item for item in await protocol_sheet(db_session)}
    assert empty_protocols["macos_apps"].status == "needs_setup"

    local = Integration(
        slug="local-messages",
        adapter="messaging",
        name="Local",
        scopes=["messaging:read"],
        status="active",
        config={"provider": "local"},
    )
    db_session.add(local)
    await db_session.commit()
    local_proj = await build_runtime_projection(db_session, actor="master", channel="voice")
    local_names = {item["name"] for item in local_proj["live_tool_projection"]}
    assert "open_app" not in local_names

    await db_session.delete(local)
    await db_session.commit()
    await _install_macos_life(db_session, mock_life_helper)
    connected = await build_runtime_projection(db_session, actor="voice", channel="voice")
    connected_names = {item["name"] for item in connected["live_tool_projection"]}
    assert {"open_url", "open_app", "close_app"} <= connected_names
    sheet = spoken_ready_capability_line(connected)
    assert "open apps" in sheet
    assert "close apps" in sheet
    assert "open_app" not in sheet
    protocols = {item.key: item for item in await protocol_sheet(db_session)}
    assert protocols["macos_apps"].status == "enabled"


async def test_open_app_prefers_helper_bearing_macos_life_row(
    db_session: AsyncSession, mock_life_helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "life_helper_path", "")
    db_session.add(
        Integration(
            slug="stale-phone",
            adapter="phone",
            name="Calls",
            scopes=["phone:act"],
            status="active",
            config={"provider": "macos_life"},
        )
    )
    await db_session.commit()
    await _install_macos_life(db_session, mock_life_helper)
    opened = await dispatch(db_session, "open_app", {"name": "Safari"}, actor="master")
    assert opened.ok is True, opened.error
    assert opened.result["spoken"] == "Opened Safari."
    assert opened.result["opened"] is True

