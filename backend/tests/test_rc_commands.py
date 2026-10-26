"""RC CLI commands (G2): thin readers over existing engines."""

from __future__ import annotations

from pathlib import Path

from clients.cli import rc_commands as rc


async def test_trace_list_and_show(tmp_path: Path, monkeypatch, capsys) -> None:
    from app.agent import store as trace_store
    from app.config import settings

    monkeypatch.setattr(settings, "agent_trace_dir", str(tmp_path))
    trace_store.write_run({"run_id": "run-abc", "status": "completed",
                           "goal": "do the thing", "tool_calls": 2,
                           "elapsed_ms": 10, "steps": []})
    assert rc.cmd_trace_list() == 0
    out = capsys.readouterr().out
    assert "run-abc" in out and "do the thing" in out
    assert rc.cmd_trace_show("run-abc") == 0
    assert "do the thing" in capsys.readouterr().out
    assert rc.cmd_trace_show("run-missing") == 1


async def test_keys_never_prints_values(tmp_path: Path, monkeypatch, capsys) -> None:
    secret = "sk-rc-test-secret-value-001"
    monkeypatch.setenv("EV_OPENROUTER_API_KEY", secret)
    monkeypatch.chdir(tmp_path)
    assert rc.cmd_keys() == 0
    out = capsys.readouterr().out
    assert "EV_OPENROUTER_API_KEY" in out and "[SET]" in out
    assert secret not in out


async def test_keys_finds_dotenv_value_without_printing(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / ".env").write_text("EV_GOOGLE_API_KEY=g-rc-test-secret-002\nEV_MASTER_KEY=\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EV_GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("EV_MASTER_KEY", raising=False)
    assert rc.cmd_keys() == 0
    out = capsys.readouterr().out
    assert "g-rc-test-secret-002" not in out
    assert "[SET] EV_GOOGLE_API_KEY via" in out


async def test_cost_and_privacy_read_the_ledger(db_session, capsys) -> None:
    from app.services.model_call import log_brain_call

    await log_brain_call(
        db_session, request_id="rc-cost-1", actor="master", provider="openrouter",
        model="meta/muse-test", effort="medium", latency_ms=12.0,
        usage={"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001},
        data_class="owner_conversation",
    )
    await db_session.commit()
    assert await rc.cmd_cost(window_hours=24) == 0
    cost_out = capsys.readouterr().out
    assert "calls: 1" in cost_out and "openrouter" in cost_out
    assert await rc.cmd_privacy_ledger(limit=5) == 0
    priv_out = capsys.readouterr().out
    assert "owner_conversation" in priv_out and "meta/muse-test" in priv_out


async def test_permissions_runs_prompt_free() -> None:
    code = rc.cmd_permissions()
    assert code in {0, 1}
