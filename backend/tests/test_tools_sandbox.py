"""Sandboxed code/file tools (plan 11.4): execution, bounds, traversal, trust."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models import AccessLog
from app.tools.sandbox import SandboxError, read_file, run_command, write_file


def test_run_command_captures_output() -> None:
    result = run_command("echo hello")
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello"
    assert result["stdout_truncated"] is False


def test_run_command_truncates_output() -> None:
    result = run_command("head -c 200000 /dev/zero")
    assert result["exit_code"] == 0
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) == 64 * 1024


def test_run_command_times_out() -> None:
    with pytest.raises(SandboxError, match="timed out"):
        run_command("sleep 5", timeout_seconds=1)


def test_run_command_rejects_unknown() -> None:
    with pytest.raises(SandboxError, match="command not found"):
        run_command("definitely-not-a-command-xyz")


def test_cwd_traversal_rejected() -> None:
    with pytest.raises(SandboxError, match="escapes"):
        run_command("pwd", cwd="../../../../etc")


def test_file_read_traversal_rejected() -> None:
    with pytest.raises(SandboxError, match="escapes"):
        read_file("../../../../etc/passwd")


def test_file_write_read_roundtrip_within_sandbox() -> None:
    written = write_file("scratch/notes.txt", "hello sandbox")
    assert written["path"] == "scratch/notes.txt"
    assert written["bytes"] == len("hello sandbox")

    read = read_file("scratch/notes.txt")
    assert read["content"] == "hello sandbox"
    assert read["truncated"] is False


async def test_execute_api_requires_owner_trust_and_audits(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    resp = await client.post(
        "/v1/tools/execute",
        json={"command": "echo via-api", "timeout_seconds": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "via-api"

    audit = (
        await db_session.execute(
            select(AccessLog).where(AccessLog.action == "tool.execute")
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].details["exit_code"] == 0

    device = await client.post(
        "/v1/devices", json={"name": "plain-watch", "capabilities": []}
    )
    assert device.status_code == 201, device.text
    token = device.json()["token"]
    plain = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )
    async with plain:
        resp = await plain.post(
            "/v1/tools/execute", json={"command": "echo nope"}
        )
    assert resp.status_code == 403


async def test_file_api_roundtrip(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/tools/files/write",
        json={"path": "scratch/api.txt", "content": "api file"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "scratch/api.txt"

    resp = await client.post(
        "/v1/tools/files/read", json={"path": "scratch/api.txt"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "api file"
