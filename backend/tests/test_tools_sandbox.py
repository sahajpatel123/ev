"""Sandboxed code/file tools (plan 11.4): execution, bounds, traversal, trust."""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.sandbox as sandbox
from app.main import app
from app.models import AccessLog
from app.tools.sandbox import (
    SandboxError,
    effective_isolation,
    read_file,
    run_command,
    sandbox_root,
    write_file,
)


def _require_os_isolation() -> str:
    isolation = effective_isolation()
    if isolation == "process":
        pytest.skip(
            "no OS isolation (seatbelt/docker) available on this host; "
            "the escape cannot be contained by the process jail alone"
        )
    return isolation


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
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The production path fails closed when seatbelt cannot be applied. This
    # test double exercises policy/allowlist behavior on restricted CI hosts.
    monkeypatch.setattr(sandbox, "effective_isolation", lambda: "process")
    held = await client.post(
        "/v1/tools/execute",
        json={"operation": "workspace_smoke_test", "timeout_seconds": 5},
    )
    assert held.status_code == 200, held.text
    held_body = held.json()
    assert held_body.get("needs_confirm") is True
    assert held_body.get("error") == "confirmation_required"
    assert held_body.get("action_id")

    resp = await client.post(
        "/v1/tools/execute",
        json={"operation": "workspace_smoke_test", "timeout_seconds": 5, "confirm": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "EV workspace smoke test passed"
    assert body["operation"] == "workspace_smoke_test"
    assert body.get("ok") is True

    audit = (
        await db_session.execute(
            select(AccessLog).where(AccessLog.action == "tool.execute")
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].details["exit_code"] == 0

    denied = await client.post(
        "/v1/tools/execute",
        json={"operation": "arbitrary_shell", "confirm": True},
    )
    assert denied.status_code == 200
    assert denied.json()["error"] == "operation_not_allowed"

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
            "/v1/tools/execute", json={"operation": "workspace_smoke_test"}
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


# --------------------------------------------------------------------------- #
# CORTEX escape suite: 20 escape attempts, all must be blocked.
# --------------------------------------------------------------------------- #


def test_escape_01_file_read_path_traversal() -> None:
    with pytest.raises(SandboxError, match="escapes"):
        read_file("../../../../etc/passwd")


def test_escape_02_file_read_absolute_path() -> None:
    with pytest.raises(SandboxError, match="escapes"):
        read_file("/etc/passwd")


def test_escape_03_file_write_path_traversal() -> None:
    with pytest.raises(SandboxError, match="escapes"):
        write_file("../../../../tmp/cortex-escape-traversal.txt", "x")


def test_escape_04_file_write_absolute_path() -> None:
    with pytest.raises(SandboxError, match="escapes"):
        write_file("/tmp/cortex-escape-absolute.txt", "x")


def test_escape_05_symlink_read_escape() -> None:
    link = sandbox_root() / "escape-link-read"
    link.symlink_to("/etc/passwd")
    with pytest.raises(SandboxError, match="escapes"):
        read_file("escape-link-read")


def test_escape_06_symlink_write_escape() -> None:
    link = sandbox_root() / "escape-link-write"
    link.symlink_to("/tmp/cortex-escape-symlink-target.txt")
    with pytest.raises(SandboxError, match="escapes"):
        write_file("escape-link-write", "x")


def test_escape_07_command_cwd_traversal() -> None:
    with pytest.raises(SandboxError, match="escapes"):
        run_command("pwd", cwd="../../../../etc")


def test_escape_08_command_reads_host_passwd() -> None:
    _require_os_isolation()
    result = run_command("cat /etc/passwd")
    assert result["exit_code"] != 0
    assert "root:" not in result["stdout"]


def test_escape_09_command_reads_host_marker_file() -> None:
    _require_os_isolation()
    host_dir = Path(tempfile.mkdtemp(prefix="cortex-host-"))
    marker = host_dir / "secret.txt"
    marker.write_text("host-secret")
    result = run_command(f"cat {marker}")
    assert result["exit_code"] != 0
    assert "host-secret" not in result["stdout"]


def test_escape_10_command_writes_outside_scratch() -> None:
    _require_os_isolation()
    host_dir = Path(tempfile.mkdtemp(prefix="cortex-host-"))
    marker = host_dir / "escaped.txt"
    result = run_command(f"touch {marker}")
    assert result["exit_code"] != 0
    assert not marker.exists()


def test_escape_11_network_egress_curl() -> None:
    _require_os_isolation()
    result = run_command("curl -s --max-time 5 http://example.com")
    assert result["exit_code"] != 0
    assert result["network"] == "blocked"


def test_escape_12_network_egress_socket() -> None:
    _require_os_isolation()
    result = run_command(
        "python3 -c \"import socket; socket.create_connection(('example.com', 80), timeout=3)\""
    )
    assert result["exit_code"] != 0
    assert result["network"] == "blocked"


def test_escape_13_fork_bomb_blocked_by_process_limit() -> None:
    result = run_command(
        "python3 -c \"import os\\nwhile True: os.fork()\"",
        timeout_seconds=15,
        process_limit=64,
    )
    assert result["exit_code"] != 0
    assert "Resource" in result["stderr"] or "fork" in result["stderr"]


def test_escape_14_memory_bomb_blocked_by_watchdog() -> None:
    # macOS refuses to lower RLIMIT_AS, so the sandbox enforces memory with a
    # live RSS watchdog that SIGKILLs the process group over budget.
    with pytest.raises(SandboxError, match="memory limit"):
        run_command(
            "python3 -c \"import time; x = bytearray(512 * 1024 * 1024); time.sleep(10)\"",
            timeout_seconds=15,
            memory_limit_mb=256,
        )


def test_escape_15_wall_clock_timeout_kills_process_group() -> None:
    with pytest.raises(SandboxError, match="timed out"):
        run_command("sleep 60", timeout_seconds=2)


def test_escape_16_output_cap() -> None:
    result = run_command("head -c 200000 /dev/zero")
    assert result["exit_code"] == 0
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) == 64 * 1024


def test_escape_17_shell_metacharacter_injection() -> None:
    result = run_command("echo hi; cat /etc/passwd")
    assert result["exit_code"] == 0
    assert "hi; cat /etc/passwd" in result["stdout"]
    assert "root:" not in result["stdout"]


def test_escape_18_environment_exfiltration() -> None:
    result = run_command("python3 -c \"import os; print(os.environ)\"")
    assert result["exit_code"] == 0
    assert "EV_MASTER_KEY" not in result["stdout"]
    assert "OPENAI_API_KEY" not in result["stdout"]
    assert str(Path.home()) not in result["stdout"]
    assert sandbox_root().as_posix() in result["stdout"]  # HOME is the scratch dir


def test_escape_19_command_writes_workspace_outside_scratch() -> None:
    _require_os_isolation()
    marker = sandbox_root().parent / "cortex-escape-workspace.txt"
    result = run_command(f"touch {marker}")
    assert result["exit_code"] != 0
    assert not marker.exists()


def test_escape_20_command_reads_private_etc() -> None:
    _require_os_isolation()
    result = run_command("cat /private/etc/hosts")
    assert result["exit_code"] != 0
    assert "localhost" not in result["stdout"]
