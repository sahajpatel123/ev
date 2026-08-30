"""EVIE presence overlay — she opens a window; she does not send you to a website."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.ev.tool_select import select_tool
from app.ev.tools import dispatch
from app.notify.presence import open_presence, present_url


def test_present_url_is_ev_scheme() -> None:
    url = present_url(title="Today", body="Leave in 12 minutes", kind="briefing")
    assert url.startswith("ev://present?")
    assert "Today" in url
    assert "briefing" in url


def test_present_url_encodes_spaces_not_plus() -> None:
    """Swift URLComponents does not treat + as space — use %20."""

    url = present_url(title="EVIE", body="No update from him on my end")
    assert "No+update" not in url
    assert "No%20update%20from%20him" in url


async def test_open_presence_is_honest_when_helper_missing(monkeypatch) -> None:
    monkeypatch.setenv("EV_PRESENCE_LIVE", "1")
    monkeypatch.setattr("app.notify.presence._helper_path", lambda: None)

    async def fake_run(*_args, **_kwargs):
        class Result:
            returncode = 1
            stderr = "LSOpenURLsWithRole failed"
            stdout = ""

        return Result()

    monkeypatch.setattr("app.notify.presence.asyncio.to_thread", fake_run)
    outcome = await open_presence(title="Hello", body="World")
    assert outcome["ok"] is False
    assert outcome["opened"] is False
    assert outcome["degraded"] is True
    assert "package and launch" in outcome["next_step"]


async def test_open_presence_reports_opened_on_open_success(monkeypatch) -> None:
    monkeypatch.setenv("EV_PRESENCE_LIVE", "1")
    monkeypatch.setattr("app.notify.presence._helper_path", lambda: None)

    async def fake_run(*_args, **_kwargs):
        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        return Result()

    monkeypatch.setattr("app.notify.presence.asyncio.to_thread", fake_run)
    outcome = await open_presence(title="Hello", body="World", kind="card")
    assert outcome["opened"] is True
    assert outcome["via"] in ("open", "helper")
    assert outcome["url"].startswith("ev://present?")


async def test_present_tool_is_selected_for_show_me() -> None:
    choice = select_tool("show me my afternoon on screen")
    assert choice.selected == "present"


async def test_present_tool_dispatch(client, db_session) -> None:
    with patch(
        "app.notify.presence.open_presence",
        new=AsyncMock(
            return_value={
                "ok": True,
                "opened": True,
                "surface": "overlay",
                "url": "ev://present?title=Hi",
                "via": "open",
            }
        ),
    ):
        response = await dispatch(
            db_session,
            "present",
            {"title": "Hi", "body": "This is EVIE."},
            actor="master",
        )
    assert response.ok is True
    assert response.result["opened"] is True


def test_present_url_carries_lookout_params() -> None:
    url = present_url(
        title="Chip",
        body="Got it",
        kind="chip",
        size="chip",
        time_type="flash",
        placement="upper_right",
        ttl_ms=1600,
        lookout=False,
        window_id="hud-chip",
    )
    assert "time=flash" in url
    assert "size=chip" in url
    assert "place=upper_right" in url
    assert "ttl=1600" in url


async def test_runtime_present_endpoint(client) -> None:
    with patch(
        "app.notify.presence.open_presence",
        new=AsyncMock(
            return_value={
                "ok": True,
                "opened": True,
                "surface": "overlay",
                "url": "ev://present?title=Card",
                "via": "helper",
            }
        ),
    ):
        resp = await client.post(
            "/v1/runtime/present",
            json={"title": "Card", "body": "Shown by EVIE", "kind": "card"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["opened"] is True
    assert data["surface"] == "overlay"
