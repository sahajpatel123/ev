"""S24: EV_FABRIC=0 reproduces legacy behavior (golden-tested).

Goldens captured from tag pre-fabric-baseline (indisputably legacy code):
- POST /v1/chat {"message": "hello golden"} → 'EV: Mock reply. ...'
- POST /v1/cognitive/turn {"transcript": "hello golden"} → unavailable line.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from app.config import settings

CHAT_GOLDEN = "EV: Mock reply. Last user message: hello golden"
COG_GOLDEN = (
    "I can't think that through right now. Give me a moment and ask again — I won't guess."
)


@pytest.fixture()
def fabric_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "fabric", False)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    from app.cognitive.session_store import reset_for_tests
    from app.fabric import trace

    reset_for_tests()
    trace.clear()
    yield tmp_path
    reset_for_tests()
    trace.clear()


async def test_s24_chat_golden_legacy(client: AsyncClient, fabric_off) -> None:
    resp = await client.post("/v1/chat", json={"message": "hello golden"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reply"] == CHAT_GOLDEN


async def test_s24_cognitive_passthrough_golden(client: AsyncClient, fabric_off) -> None:
    resp = await client.post("/v1/cognitive/turn", json={"transcript": "hello golden"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["spoken"] == COG_GOLDEN
    assert body["kind"] == "unavailable"


async def test_s24_no_fabric_turns_when_off(
    client: AsyncClient, fabric_off, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.fabric import trace

    calls: list[str] = []
    real = __import__("app.fabric.facade", fromlist=["arun_kernel_turn"]).arun_kernel_turn

    async def spy(**kwargs):
        calls.append(kwargs.get("entrypoint", "?"))
        return await real(**kwargs)

    monkeypatch.setattr("app.fabric.facade.arun_kernel_turn", spy)
    # Chat with the legacy cognitive mode takes the direct path (no facade).
    await client.post("/v1/chat", json={"message": "hello golden"})
    assert calls == [], calls
    assert trace.recent_entries() == []
    # cognitive/turn always funnels the facade, but as a pure passthrough:
    # the golden above proves the bytes are legacy-identical.
    await client.post("/v1/cognitive/turn", json={"transcript": "hello golden"})
    assert calls == ["v1_cognitive_turn"]


async def test_s24_facade_in_path_when_on(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.fabric import trace

    monkeypatch.setattr(settings, "fabric", True)
    # The production brain mode is what puts the kernel (and thus the facade)
    # in the chat path; fabric-on alone keeps the legacy pipeline.
    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    trace.clear()
    calls: list[str] = []
    real = __import__("app.fabric.facade", fromlist=["arun_kernel_turn"]).arun_kernel_turn

    async def spy(**kwargs):
        calls.append(kwargs.get("entrypoint", "?"))
        return await real(**kwargs)

    monkeypatch.setattr("app.fabric.facade.arun_kernel_turn", spy)
    resp = await client.post("/v1/chat", json={"message": "hello golden"})
    assert resp.status_code == 200, resp.text
    assert calls == ["v1_chat_pipeline"], calls
    assert trace.turn_count("v1_chat_pipeline") == 1


async def test_s24_fabric_tools_fail_closed_when_off(
    client: AsyncClient, fabric_off, db_session
) -> None:
    from app.ev.tools import dispatch

    response = await dispatch(
        db_session, "shell_run", {"command": "echo hi"},
        actor="master", allow_sensitive=True,
    )
    assert response.result["error"] == "CAPABILITY_UNAVAILABLE"
    # The routes still serve; the lanes refuse.
    status = await client.get("/v1/fabric/status")
    assert status.json()["fabric_enabled"] is False
