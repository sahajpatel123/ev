"""Isolated world for one turn-gauntlet case.

A world is a temporary root that holds everything a case can touch:

- ``home/`` — HOME for the worker (Desktop, Documents, Downloads, Code, Projects)
- ``ev.db`` — a fresh SQLite database (never the owner's Postgres)
- ``state.json`` — the fixture server's inspectable outside world
- ``tmux/`` — TMUX_TMPDIR, so tmux starts its own server, never the owner's
- ``agent/`` — the agent ledger and traces
- ``tripwire.jsonl`` — child processes the world refused to start

Brain credentials are read the way the live service reads them (launchd
sources the repo ``.env`` and then ``~/.ev/secrets/production.env``). Every
behavior setting is mirrored from those files. Every resource setting is
overridden with a world-local one. Secret values are never printed.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

HOME_DIRS = ("Desktop", "Documents", "Downloads", "Code", "Projects")

#: Settings whose production value names a live resource. A world blanks or
#: replaces every one of them, so it can never reach :8000, :18000, Redis or
#: the owner's database.
_RESOURCE_KEYS = (
    "EV_DATABASE_URL", "EV_REDIS_URL", "EV_EARS_API_URL", "EV_EARS_API_KEY",
    "EV_COGNITIVE_KERNEL_URL", "EV_COGNITIVE_MAC_EXECUTE_URL", "EV_GOOGLE_OAUTH_REDIRECT_URI",
    "EV_STORAGE_ROOT", "EV_BACKUP_DIR", "EV_LIFE_HELPER_PATH",
    "EV_NOTIFY_MACOS_HELPER_PATH", "EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH",
    "EV_VOICE_WAKE_OPENWAKEWORD_VERIFIER_PATH",
)


def production_env_files() -> list[Path]:
    """The two files launchd's ev.api job sources, in the same order."""
    raw = os.environ.get("GAUNTLET_PROD_ENV_FILES", "").strip()
    if raw:
        return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
    repo = Path(os.environ.get("GAUNTLET_LIVE_REPO", str(Path.home() / "Code" / "ev")))
    return [repo / ".env", Path.home() / ".ev" / "secrets" / "production.env"]


def load_production_env() -> dict[str, str]:
    from dotenv import dotenv_values

    found: dict[str, str] = {}
    for path in production_env_files():
        if not path.is_file():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None and key.startswith("EV_"):
                found[key] = value
    return found


def brain_status(env: dict[str, str]) -> dict[str, Any]:
    """What the world's brain will be. Lengths only, never values."""
    key = (env.get("EV_OPENROUTER_API_KEY") or "").strip()
    return {
        "agent_provider": env.get("EV_AGENT_PROVIDER") or "auto",
        "chat_provider": env.get("EV_CHAT_PROVIDER") or "unset",
        "brain_model": env.get("EV_BRAIN_MODEL") or "",
        "openrouter_key": f"present ({len(key)} chars)" if key else "missing",
    }


class World:
    def __init__(self, case_id: str, *, keep: bool = False) -> None:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in case_id)[:60]
        self.root = Path(tempfile.mkdtemp(prefix=f"evg-{safe}-"))
        self.home = self.root / "home"
        for name in HOME_DIRS:
            (self.home / name).mkdir(parents=True, exist_ok=True)
        (self.home / ".ev").mkdir(parents=True, exist_ok=True)
        (self.root / "tmux").mkdir()
        (self.root / "agent").mkdir()
        (self.root / "storage").mkdir()
        self.state_path = self.root / "state.json"
        self.master_key = secrets.token_urlsafe(24)
        self.keep = keep

    # ------------------------------------------------------------------ env

    def env(self, *, brain: str = "real", base_url: str = "") -> dict[str, str]:
        """The worker's complete environment. The parent env is not inherited."""
        keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env.update(load_production_env())
        for key in _RESOURCE_KEYS:
            env.pop(key, None)
        root = str(self.root)
        env.update({
            "HOME": str(self.home),
            "TMUX_TMPDIR": str(self.root / "tmux"),
            "EV_DATABASE_URL": f"sqlite+aiosqlite:///{root}/ev.db",
            "EV_REDIS_URL": "redis://127.0.0.1:9/15",
            "EV_PROCESSING_MODE": "sync",
            "EV_SECRETS_FILE": f"{root}/no-secrets.env",
            "EV_MASTER_KEY": self.master_key,
            "EV_API_KEY": self.master_key,
            "EV_VAULT_KEY": secrets.token_urlsafe(48),
            "EV_STORAGE_ROOT": f"{root}/storage",
            "EV_BACKUP_DIR": f"{root}/backups",
            "EV_MEMORY_DIR": f"{root}/memory",
            "EV_CODE_WORKSPACE": str(self.home / "Code"),
            "EV_AGENT_STATE_DB": f"{root}/agent/tasks.db",
            "EV_AGENT_TRACE_DIR": f"{root}/agent/traces",
            "EV_AGENT_ALLOWED_ROOTS": str(self.home),
            "EV_AGENT_ALLOW_LOCAL_FETCH": "true",
            "EV_FABRIC_ROOTS": str(self.home / "Code"),
            "EV_FABRIC_FAILURE_QUEUE": f"{root}/failures.jsonl",
            "EV_LIFE_HELPER_PATH": "",
            "EV_LIFE_STREAM_ENABLED": "false",
            "EV_LIFE_STREAM_AUTO_LAUNCH_APPS": "false",
            "EV_MESSAGING_PROVIDER": "local",
            "EV_NOTIFY_BACKEND": "console",
            "EV_NOTIFY_MACOS_ALLOW_OSASCRIPT": "false",
            "EV_EARS_CONSENT": "false",
            "EV_COGNITIVE_KERNEL_URL": "",
            "EV_COGNITIVE_MAC_EXECUTE_URL": "",
            "EV_ENVIRONMENT": "gauntlet-world",
            "EV_MAINTENANCE_MODE": "false",
            "EV_ACCESS_LOG_ENABLED": "false",
            "EV_SEARCH_PROVIDER": "none",
            "EV_VOICE_ASR_PROVIDER": "echo",
            "EV_VOICE_TTS_PROVIDER": "meta",
            "EV_VOICE_WAKE_PROVIDER": "phrase",
            "EV_VOICEPRINT_PROVIDER": "hash",
            "EV_ML_MODEL_DIR": f"{root}/models",
            "EV_EMBEDDING_PROVIDER": "hash",
            "EV_EMBEDDING_DIM": "64",
            "GAUNTLET_WORLD_ROOT": root,
            # Local, read-only helpers a world may run even though they live
            # under the owner's home (Apple Vision OCR).
            "GAUNTLET_TRIPWIRE_ALLOW": os.pathsep.join(
                p for p in [env.get("EV_VISION_EVVISION_BINARY", "")] if p
            ),
            "GAUNTLET_REAL_HOME": str(Path.home()),
            "GAUNTLET_BRAIN": brain,
            "GAUNTLET_FIXTURE_URL": base_url,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if brain != "real":
            for key in list(env):
                if key.endswith("_API_KEY") and key not in {"EV_API_KEY"}:
                    env[key] = ""
        return env

    # ---------------------------------------------------------------- setup

    def apply_setup(self, setup: list[dict[str, Any]]) -> None:
        for item in setup or []:
            kind = item.get("kind") or ("file" if "path" in item else "")
            if kind == "file":
                path = self.home / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                if "bytes" in item:
                    path.write_bytes(bytes(item["bytes"]) if isinstance(item["bytes"], list)
                                     else b"x" * int(item["bytes"]))
                else:
                    path.write_text(str(item.get("text") or ""), encoding="utf-8")
                if item.get("mtime"):
                    os.utime(path, (float(item["mtime"]), float(item["mtime"])))
            elif kind == "dir":
                (self.home / item["path"]).mkdir(parents=True, exist_ok=True)
            elif kind == "git":
                self._git_repo(item)
            elif kind == "sqlite":
                self._sqlite(item)
            elif kind == "symlink":
                link = self.home / item["path"]
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(item["target"])
            elif kind in {"docx", "xlsx", "pdf", "png", "media"}:
                from gauntlet.v2 import fixtures

                path = self.home / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                getattr(fixtures, f"make_{kind}")(path, item)

    def _git_repo(self, item: dict[str, Any]) -> None:
        repo = self.home / item["path"]
        repo.mkdir(parents=True, exist_ok=True)
        env = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "HOME": str(self.home),
               "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": "owner@example.com",
               "GIT_COMMITTER_NAME": "Owner", "GIT_COMMITTER_EMAIL": "owner@example.com"}
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, env=env, capture_output=True,  # noqa: E731
                                        check=False, timeout=30)
        run("init", "-q", "-b", "main")
        commits = item.get("commits") or [{"message": "initial", "files": item.get("files") or {"README.md": "x"}}]
        for commit in commits:
            for rel, text in (commit.get("files") or {}).items():
                target = repo / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            run("add", "-A")
            run("-c", "commit.gpgsign=false", "commit", "-q", "-m", commit.get("message") or "change")
        for rel, text in (item.get("dirty") or {}).items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def _sqlite(self, item: dict[str, Any]) -> None:
        import sqlite3

        path = self.home / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(item.get("script") or "")
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------------- reads

    def read(self, rel: str) -> str:
        return (self.home / rel).read_text(encoding="utf-8", errors="replace")

    def exists(self, rel: str) -> bool:
        return (self.home / rel).exists()

    def state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    # -------------------------------------------------------------- cleanup

    def kill_tmux(self) -> None:
        sock_dir = self.root / "tmux"
        for sock in sock_dir.glob("tmux-*/*"):
            subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True,
                           check=False, timeout=10)

    def destroy(self) -> list[str]:
        """Remove the world. Returns anything that survived (the leak list)."""
        self.kill_tmux()
        if self.keep:
            return []
        shutil.rmtree(self.root, ignore_errors=True)
        return [str(self.root)] if self.root.exists() else []


def real_home_snapshot(home: Path | None = None) -> dict[str, tuple[int, float]]:
    """Fingerprint of the owner's real Desktop/Documents/Downloads (size, mtime)."""
    base_home = home or Path(os.environ.get("GAUNTLET_REAL_HOME") or Path.home())
    out: dict[str, tuple[int, float]] = {}
    for name in ("Desktop", "Documents", "Downloads"):
        base = base_home / name
        if not base.is_dir():
            continue
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for path in entries:
            try:
                stat = path.stat()
            except OSError:
                continue
            out[str(path)] = (stat.st_size, stat.st_mtime)
    return out


def owner_tmux_sessions() -> list[str]:
    """Sessions on the owner's default tmux server (names only)."""
    try:
        proc = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                              capture_output=True, text=True, timeout=5, check=False,
                              env={**os.environ, "TMUX": ""})
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted(line for line in proc.stdout.splitlines() if line.strip())
