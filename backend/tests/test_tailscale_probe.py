"""Regression: tailscale probe must never raise on odd CLI output.

Production 500 on POST /v1/device-gateway/hello traced to probe(): a truthy
but whitespace-only `tailscale version` output made `.splitlines()[0]` raise
IndexError. The probe runs on hot phone paths (hello/status/devices), so it
must be total — degraded output, never an exception.
"""

from __future__ import annotations

from app.device_gateway import tailscale


def _stub_env(monkeypatch, version_out: str, calls: list):
    monkeypatch.setattr(tailscale, "find_binary", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale, "_json_cmd", lambda cmd: None)

    def fake_text(cmd: list[str]) -> str:
        calls.append(cmd)
        if cmd[-1] == "version":
            return version_out
        return ""

    monkeypatch.setattr(tailscale, "_text_cmd", fake_text)


def test_flaky_version_output_does_not_raise(monkeypatch) -> None:
    """The production IndexError: first `version` call truthy, second empty."""
    calls: list = []
    monkeypatch.setattr(tailscale, "find_binary", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale, "_json_cmd", lambda cmd: None)
    outputs = iter(["1.86.2\n", ""])

    def fake_text(cmd: list[str]) -> str:
        calls.append(cmd)
        if cmd[-1] == "version":
            return next(outputs, "")
        return ""

    monkeypatch.setattr(tailscale, "_text_cmd", fake_text)
    probed = tailscale.probe()
    assert probed["installed"] is True
    assert probed["tailscale_version"] == "1.86.2"
    version_calls = [c for c in calls if c[-1] == "version"]
    assert len(version_calls) == 1, "version command must run exactly once"


def test_normal_version_output_takes_first_line(monkeypatch) -> None:
    calls: list = []
    _stub_env(monkeypatch, "1.86.2\n  tailscale commit: abc\n", calls)
    probed = tailscale.probe()
    assert probed["tailscale_version"] == "1.86.2"


def test_empty_version_output_stays_empty(monkeypatch) -> None:
    calls: list = []
    _stub_env(monkeypatch, "", calls)
    probed = tailscale.probe()
    assert probed["tailscale_version"] == ""
