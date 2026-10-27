"""GE2: key separation, caps, credit watch (no network, no real keys)."""

from __future__ import annotations

import json
from datetime import UTC

import pytest

from app.growth import keys as K


def test_suite_env_removes_live_key_and_routes_eval(monkeypatch, tmp_path):
    monkeypatch.setenv("EV_OPENROUTER_API_KEY", "live-SECRET-123")
    monkeypatch.setenv("EV_MASTER_KEY", "master-SECRET-456")
    p = tmp_path / "eval.env"
    K.write_eval_key("eval-KEY-789", path=p)
    monkeypatch.setattr(K, "eval_key_file", lambda: p)
    from app.config import settings

    monkeypatch.setattr(settings, "openrouter_eval_key", None, raising=False)
    env = K.suite_env()
    blob = json.dumps(env)
    assert "live-SECRET-123" not in blob and "master-SECRET-456" not in blob
    assert env["EV_OPENROUTER_API_KEY"] == "eval-KEY-789"
    assert env["EV_GROWTH_EVAL"] == "1"


def test_suite_env_without_eval_key_has_no_provider_key(monkeypatch, tmp_path):
    monkeypatch.delenv("EV_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(K, "eval_key_file", lambda: tmp_path / "missing.env")
    from app.config import settings

    monkeypatch.setattr(settings, "openrouter_eval_key", None, raising=False)
    env = K.suite_env()
    assert "EV_OPENROUTER_API_KEY" not in env


def test_write_eval_key_chmod_600(tmp_path):
    import os

    p = K.write_eval_key("  abc  ", path=tmp_path / "e.env")
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert "abc" in p.read_text() and p.read_text().startswith("EV_OPENROUTER_EVAL_KEY=")


def test_preflight_caps_and_spend(monkeypatch, tmp_path):
    from app.config import settings
    from app.growth import budget as B

    monkeypatch.setattr(settings, "growth_store_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "growth_eval_daily_cap_usd", 1.0, raising=False)
    monkeypatch.setattr(settings, "growth_eval_monthly_cap_usd", 20.0, raising=False)
    ok, sample, note = B.preflight("journeys", None, 1.0)
    assert ok and sample == 1.0 and "room" in note
    B.record_spend("journeys", 0.0)
    d, m = B.spent("2099-01-01", "2099-01")
    assert (d, m) == (0.0, 0.0)


def test_preflight_refuses_exhausted_day(monkeypatch, tmp_path):
    from datetime import datetime

    from app.config import settings
    from app.growth import budget as B

    monkeypatch.setattr(settings, "growth_store_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "growth_eval_daily_cap_usd", 1.0, raising=False)
    monkeypatch.setattr(settings, "growth_eval_monthly_cap_usd", 20.0, raising=False)
    B.record_spend("x", 5.0)
    today = datetime.now(UTC).isoformat()[:10]
    d, _m = B.spent(today, today[:7])
    assert d == 5.0
    ok, _s, note = B.preflight("x", 1.0, 1.0)
    assert not ok and "daily" in note


def test_key_credit_parses_numbers_only(monkeypatch):
    import urllib.request

    body = json.dumps({"data": {"label": "t", "usage": 4.98, "limit": 5,
                                "is_free_tier": False}}).encode()

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return body

    seen = {}
    def fake_open(req, timeout=20):
        seen["auth"] = req.get_header("Authorization")
        return FakeResp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    out = K.key_credit("SECRET-VALUE")
    assert seen["auth"] == "Bearer SECRET-VALUE"
    assert out["usage"] == 4.98 and out["remaining"] == pytest.approx(0.02)
    assert "SECRET-VALUE" not in json.dumps(out)


def test_credit_warns_without_key(monkeypatch, tmp_path, capsys):
    from app.growth import credit as C

    monkeypatch.setattr(K, "load_eval_key", lambda: "")
    monkeypatch.setattr(C, "_owner_actions_path", lambda: tmp_path / "O.md")
    assert C.main([]) == 2
    assert "no eval key" in capsys.readouterr().out
    assert "ev keys set openrouter-eval" in (tmp_path / "O.md").read_text()


def test_push_notice_success_and_failure(monkeypatch):
    import clients.cli
    from app.growth import credit as C

    async def ok(*a, **k):
        return {"id": "n1"}

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(clients.cli, "notify_send", ok)
    assert C._push_notice("x") is True
    monkeypatch.setattr(clients.cli, "notify_send", boom)
    assert C._push_notice("x") is False
