"""Owner-Mac host commands: allowlisted argv, never the R4 jail."""

from __future__ import annotations

from pathlib import Path

from app.ev.mac_host import (
    looks_like_mac_command,
    mac_context,
    open_url_in_app,
    parse_mac_argv,
    run_argv,
    run_owner_mac_goal,
)


def test_mac_context_names_this_machine() -> None:
    ctx = mac_context()
    assert ctx["home"] == str(Path.home())
    assert ctx["os"]
    assert "hostname" in ctx


def test_looks_like_mac_command_for_terminal_asks() -> None:
    assert looks_like_mac_command("run ls ~ in the terminal")
    assert looks_like_mac_command("what's my hostname")
    assert looks_like_mac_command("mdfind resume")
    assert not looks_like_mac_command("find my resume file on my laptop")
    assert not looks_like_mac_command("search the web for python")
    assert not looks_like_mac_command("rm -rf /")


def test_parse_mac_argv_allowlist_and_denials() -> None:
    argv = parse_mac_argv("run uname")
    assert argv is not None
    assert Path(argv[0]).name == "uname"

    listed = parse_mac_argv("ls ~")
    if listed is not None:
        assert Path(listed[0]).name == "ls"
        assert str(Path.home()) in listed[1] or listed[1] == str(Path.home())

    assert parse_mac_argv("rm -rf /") is None
    denied = run_argv(["/usr/bin/find", str(Path.home()), "-delete"])
    assert denied["ok"] is False
    assert denied["error"] == "command_denied"


def test_run_uname_on_this_mac() -> None:
    argv = parse_mac_argv("run uname")
    assert argv is not None
    result = run_argv(argv)
    assert result["ok"] is True
    assert result["source"] == "mac_host"
    assert result.get("output")


def test_open_url_in_app_refuses_non_urls() -> None:
    refused = open_url_in_app("Safari", "not a link")
    assert refused["ok"] is False
    assert refused["error"] == "not_a_url"


def test_hostname_ask_returns_context_not_web() -> None:
    result = run_owner_mac_goal("what's my hostname")
    assert result["ok"] is True
    assert result["source"] == "mac_host"
    assert Path.home().as_posix() in str(result.get("spoken") or result.get("context") or "")
