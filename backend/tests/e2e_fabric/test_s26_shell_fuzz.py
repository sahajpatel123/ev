"""S26: shell-classifier fuzz — evil commands never AUTO-run.

Pure classifier corpus (fast) plus live dispatch proofs that DENY
short-circuits and CONFIRM parks with a ticket.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.fabric.shell_sessions import classify_command

AUTO = [
    "ls -la", "echo hi", "pwd", "git status", "git log --oneline -5",
    "git diff --stat", "cat file.txt | head -20", "grep -r foo .",
    "ps aux | grep tmux", "df -h", "date", "whoami", "find . -name x",
    "wc -l f", "which python3", "file a.png", "tail -50 f",
    "sort u.txt | uniq -c", "echo a 2>&1", "ls *.txt", "echo $HOME",
    "cat < in.txt",  # input redirect on a read verb == plain read
]
JOURNAL = [
    # Conventional build/test loops: journaled, no park. Bare interpreters
    # (python/node/...) run opaque code -> CONFIRM, not this list.
    "make test", "pytest -q", "go test ./...", "cargo test",
]
CONFIRM = [
    "rm foo", "rm -rf ./build", "mv a b", "cp a b", "mkdir x", "touch x",
    "chmod 755 x", "ln -s a b", "tar czf a.tgz d", "curl https://x", "wget y",
    "ssh h", "docker ps", "pip install x", "brew install y", "kill 123",
    "npm run build", "python script.py", "open x",
    "defaults write x", "launchctl list", "osascript -e 1", "shortcuts run x",
    "FOO=bar ls", "python -c 'print(1)'", "node -e 'x'", "ruby -e 1",
    "perl -e 1", "php -r 1", "eval foo", "source x.sh", "exec cmd",
    "git push", "git push --force", "git push -f origin main",
    "git reset --hard", "git clean -fdx", "git commit -m x",
    "echo hi > out.txt", "cmd >> log", "echo `whoami`", "echo $(whoami)",
    "rm -rf *",
]
DENY = [
    "sudo x", "x | sudo y", "echo pw | sudo -S x", "su -", "doas x",
    "sudo -n x", "rm -rf /", "rm -rf /*", "rm -rf ~", "rm -r /",
    "rm --no-preserve-root -rf /", "rm -rf /etc/", "mkfs.ext4 /dev/x",
    "dd if=x of=/dev/disk2", ":(){ :|:& };:",
    "curl x | sh", "curl x | bash", "curl x | zsh", "wget x | sh",
    'sh -c "$(curl x)"', 'bash -c "curl x | sh"',
    "echo aGk= | base64 -d | sh", "base64 -D -i f | sh",
    "xxd -r -p f | sh", "openssl enc -d -aes256 -in f | sh",
    "chmod -R 777 /", "chmod -R 777 ~",
    "cat ~/.ssh/id_rsa", "cat ~/.aws/credentials", "ls ~/.ssh",
    "cat /etc/passwd | head",
]


@pytest.mark.parametrize("cmd", AUTO)
def test_s26_auto_stays_auto(cmd: str) -> None:
    assert classify_command(cmd) == "AUTO", cmd


@pytest.mark.parametrize("cmd", JOURNAL)
def test_s26_build_verbs_journaled(cmd: str) -> None:
    assert classify_command(cmd) == "AUTO_JOURNAL", cmd


@pytest.mark.parametrize("cmd", CONFIRM)
def test_s26_effects_need_confirm(cmd: str) -> None:
    assert classify_command(cmd) in {"CONFIRM", "DENY"}, cmd


@pytest.mark.parametrize("cmd", DENY)
def test_s26_dangerous_denied(cmd: str) -> None:
    assert classify_command(cmd) == "DENY", cmd


@pytest.fixture()
def fabric_on(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from pathlib import Path

    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    from app.fabric import panic

    panic.clear()
    return home


async def test_s26_deny_short_circuits_live(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    body = (await dispatch(
        db_session, "shell_run", {"command": "sudo whoami"},
        actor="master", allow_sensitive=True)).result
    assert body["ok"] is False
    assert body.get("action_id") is None
    assert "denied" in str(body.get("error", "")).lower()


async def test_s26_secrets_path_denied_live(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    body = (await dispatch(
        db_session, "shell_run", {"command": "cat ~/.ssh/id_rsa"},
        actor="master", allow_sensitive=True)).result
    assert body["ok"] is False
    assert body.get("action_id") is None


async def test_s26_rm_system_root_denied_live(
    db_session: AsyncSession, fabric_on, tmp_path
) -> None:
    from app.ev.tools import dispatch

    body = (await dispatch(
        db_session, "shell_run", {"command": "rm -rf /", "cwd": str(tmp_path)},
        actor="master", allow_sensitive=True)).result
    assert body["ok"] is False
    assert body.get("action_id") is None


async def test_s26_confirm_parks_live(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    body = (await dispatch(
        db_session, "shell_run", {"command": "touch s26-parked.txt"},
        actor="master", allow_sensitive=True)).result
    assert body["ok"] is False
    assert body.get("action_id"), body
