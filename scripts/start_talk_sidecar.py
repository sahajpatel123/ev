"""Talk sidecar on 127.0.0.1:18000.

EV.app reads EV_API_URL from ~/Library/Application Support/EV/api.env.
This process must share the owner Postgres that holds ingested life-archive
events. Isolating it onto an empty sqlite file made live Evie report no
reliable record while the archive lived on :8000's database.

Do not start a bare ``uv run uvicorn … --port 18000``. That process loads
shared ``backend/.env`` with ``EV_LAPTOP_FILES=false`` and Talk then
speaks "Local file access is not enabled on this API."

AGENT LAW — DO NOT WEAKEN (live, proven 2026-09-10):
EV.app authenticates with a Keychain *device token* whose sha256 lives in
Postgres ``devices.token_hash`` on ev.api (:8000). Talk is the process EV.app
actually calls. If Talk inherits a pytest/agent shell
(``EV_DATABASE_URL=sqlite…/test.db``, ``EV_MASTER_KEY=test-key``,
``EV_VAULT_KEY=test-vault-…``), ``_resolve_actor`` cannot find that hash and
returns DEVICE_TOKEN_INVALID. The Mac then shows "This Mac's device token is
invalid. Attempting local repair." The token is not bad — Talk is on the
wrong database. Never ``setdefault`` owner DB/master/vault from a test
leftover. Never boot Talk on sqlite. Never "fix" this by minting a second
Mac device or rewriting Keychain. Pin Talk to the owner Postgres URL in
repo ``.env`` and the master/vault in ``~/.ev/secrets/production.env``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path("/Users/sahajpatel/Code/ev")
SUPPORT = Path.home() / "Library" / "Application Support" / "EV"
LOGS = Path.home() / "Library" / "Logs" / "ev"


def daemonize() -> None:
    """Detach so Talk survives the agent shell that launched this script."""

    if os.environ.get("EV_TALK_SIDECAR_FOREGROUND") == "1":
        return
    LOGS.mkdir(parents=True, exist_ok=True)
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir(str(REPO / "backend"))
    devnull = os.open("/dev/null", os.O_RDONLY)
    out = os.open(
        str(LOGS / "talk-sidecar.out.log"),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    err = os.open(
        str(LOGS / "talk-sidecar.err.log"),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    os.dup2(devnull, 0)
    os.dup2(out, 1)
    os.dup2(err, 2)
    for fd in (devnull, out, err):
        if fd not in (0, 1, 2):
            os.close(fd)


_OWNER_PINNED_KEYS = frozenset({"EV_DATABASE_URL", "EV_MASTER_KEY", "EV_VAULT_KEY"})
_TEST_MASTER_VALUES = frozenset({"test-key", "change-me", "ev-local-dev-key"})
_OVERLAY_SECRET_NAMES = frozenset(
    {
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "EV_GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
        "EV_OPENROUTER_API_KEY",
    }
)


def leftover_owner_runtime(key: str, value: str) -> bool:
    """True when an agent/pytest leftover must not win over owner files."""

    raw = (value or "").strip()
    if not raw:
        return True
    if key == "EV_DATABASE_URL":
        low = raw.lower()
        return (
            "sqlite" in low
            or "test.db" in low
            or "ev_test" in low
            or "/pytest" in low
            or "/tmp/" in low
        )
    if key == "EV_MASTER_KEY":
        return len(raw) < 16 or raw in _TEST_MASTER_VALUES
    if key == "EV_VAULT_KEY":
        return len(raw) < 16 or raw.startswith("test-vault")
    return False
# Owner .env two-model flags must beat leftover exports from an older Talk
# launch (Muse / JEV / Grok / OpenAI / DeepSeek / opencode). setdefault
# would silently keep the stale brain.
_BRAIN_KEYS = frozenset(
    {
        "EV_CHAT_PROVIDER",
        "EV_VOICE_ASR_PROVIDER",
        "EV_VOICE_TTS_PROVIDER",
        "EV_VOICE_LIVE_BRAIN",
        "EV_INTELLIGENCE_LAYER",
        "EV_ALLOW_REMOTE_CHAT",
        "EV_ALLOW_REMOTE_ASR",
        "EV_ALLOW_REMOTE_TTS",
        "EV_PHONE_AUDIO_BACKEND",
        "EV_MIMO_MODEL",
        "EV_GEMINI_LIVE_MODEL",
    }
)
_REMOTE_ALLOW_KEYS = frozenset(
    {"EV_ALLOW_REMOTE_CHAT", "EV_ALLOW_REMOTE_ASR", "EV_ALLOW_REMOTE_TTS"}
)
_LEFTOVER_BRAIN_VALUES = frozenset(
    {
        "meta_muse_spark",
        "muse",
        "muse_spark",
        "meta_muse_voice",
        "muse_voice",
        "jev",
        "openrouter_jev",
        "xai",
        "openai",
        "openai-realtime",
        "openai_compat",
        "grok",
        "grok-voice",
        "xai-realtime",
        "deepseek",
        "opencode",
        "local",
        "qwen",
        "faster_whisper",
        "parakeet",
        "parakeet_tdt",
        "parakeet-tdt",
        "whisper",
        "echo",
        "mock",
    }
)
_TWO_MODEL_VALUES = frozenset(
    {
        "mimo",
        "xiaomi/mimo-v2.6-flash",
        "gemini",
        "gemini-live",
        "gemini-3.8-live",
        "gemini-3.8-live-extended-thinking",
        "google",
        "auto",
        "pipeline",
        "pcm_ws",
        "edge_tts",
    }
)


def _leftover_brain_value(value: str) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return True
    if raw in _LEFTOVER_BRAIN_VALUES:
        return True
    return raw.startswith(("grok", "gpt-", "deepseek", "whisper", "muse", "jev"))


def _two_model_file_value(key: str, val: str) -> bool:
    raw = (val or "").strip().lower()
    if key in _REMOTE_ALLOW_KEYS:
        return raw in {"true", "1", "yes"}
    if raw in _TWO_MODEL_VALUES:
        return True
    return "mimo" in raw or "gemini" in raw


def _legacy_file_value(key: str, val: str) -> bool:
    raw = (val or "").strip().lower()
    if key == "EV_VOICE_LIVE_BRAIN":
        return raw in {"openai", "auto", "xai"}
    if key == "EV_PHONE_AUDIO_BACKEND":
        return raw in {"webrtc_strict", "webrtc", "auto"}
    if key == "EV_CHAT_PROVIDER":
        return raw in {"xai", "openai", "deepseek", "echo", "opencode", "mock", "local"}
    if key == "EV_VOICE_ASR_PROVIDER":
        return raw in {
            "faster_whisper",
            "echo",
            "parakeet",
            "openai_compat",
            "parakeet_tdt",
            "parakeet-tdt",
        }
    return False


def load(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        # An empty shell export must not hide the overlay model keys.
        if key in _OVERLAY_SECRET_NAMES:
            if val and not (os.environ.get(key) or "").strip():
                os.environ[key] = val
            continue
        # AGENT LAW: pytest sqlite / test-key in the parent shell must not
        # become Talk's runtime. EV.app device tokens live on owner Postgres.
        if key in _OWNER_PINNED_KEYS:
            current = os.environ.get(key) or ""
            if val and leftover_owner_runtime(key, current):
                os.environ[key] = val
            elif val and not current.strip():
                os.environ[key] = val
            continue
        if key in _BRAIN_KEYS:
            current = os.environ.get(key) or ""
            if _two_model_file_value(key, val):
                if key in _REMOTE_ALLOW_KEYS:
                    if current.strip().lower() not in {"true", "1", "yes"}:
                        os.environ[key] = val
                        continue
                elif _leftover_brain_value(current):
                    os.environ[key] = val
                    continue
            if _legacy_file_value(key, val) and _leftover_brain_value(current):
                os.environ[key] = val
                continue
        # Empty process env must not hide file values. Agent shells often
        # export EV_VAULT_KEY="" which then crash Settings() after daemonize.
        if val and not (os.environ.get(key) or "").strip():
            os.environ[key] = val
            continue
        os.environ.setdefault(key, val)


def alias_google_keys() -> None:
    """Mirror config.py: docs, overlay, and EV_ names are one secret."""

    docs = (os.environ.get("GOOGLE_API_KEY") or "").strip()
    gemini = (os.environ.get("GEMINI_API_KEY") or "").strip() or docs
    ev_google = (os.environ.get("EV_GOOGLE_API_KEY") or "").strip()
    if docs and not (os.environ.get("GEMINI_API_KEY") or "").strip():
        os.environ["GEMINI_API_KEY"] = docs
    if gemini and not ev_google:
        os.environ["EV_GOOGLE_API_KEY"] = gemini
    elif ev_google and not gemini:
        os.environ["GEMINI_API_KEY"] = ev_google


def mimo_selected() -> bool:
    """True when the MiMo brain slot is on."""

    return (
        os.environ.get("EV_CHAT_PROVIDER") or ""
    ).strip().lower() in {"mimo", "xiaomi/mimo-v2.6-flash"}


def gemini_live_selected() -> bool:
    """True when Talk wants the Gemini Live socket (auto counts: key decides)."""

    return (os.environ.get("EV_VOICE_LIVE_BRAIN") or "").strip().lower() in {
        "auto",
        "gemini",
        "gemini-live",
        "google",
        "",
    }


def openrouter_key_loaded() -> bool:
    return bool(
        (os.environ.get("EV_OPENROUTER_API_KEY") or "").strip()
        or (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    )


def google_key_loaded() -> bool:
    return bool(
        (os.environ.get("EV_GOOGLE_API_KEY") or "").strip()
        or (os.environ.get("GOOGLE_API_KEY") or "").strip()
        or (os.environ.get("GEMINI_API_KEY") or "").strip()
    )


def ensure_talk_mouth_remote_allowed() -> None:
    """Edge TTS is remote; leftover EV_ALLOW_REMOTE_TTS=false would mute Talk."""

    tts = (os.environ.get("EV_VOICE_TTS_PROVIDER") or "").strip().lower()
    if tts == "edge_tts":
        os.environ["EV_ALLOW_REMOTE_TTS"] = "true"


def refuse_talk_without_vault() -> None:
    """Fail closed before daemonize so EV.app is not left retrying :18000."""

    vault = (os.environ.get("EV_VAULT_KEY") or "").strip()
    if len(vault) < 16:
        sys.stderr.write(
            "Talk sidecar refused to start: EV_VAULT_KEY is missing or too short.\n"
        )
        raise SystemExit(2)


def refuse_talk_without_owner_runtime() -> None:
    """Fail closed before daemonize if Talk would not see owner Postgres.

    AGENT LAW: DEVICE_TOKEN_INVALID on EV.app after a Talk restart almost
    always means this process booted on sqlite/test-key. Do not skip this
    gate. Do not "repair" the Mac Keychain instead.
    """

    refuse_talk_without_vault()
    if leftover_owner_runtime("EV_DATABASE_URL", os.environ.get("EV_DATABASE_URL") or ""):
        sys.stderr.write(
            "Talk sidecar refused to start: EV_DATABASE_URL is sqlite/test. "
            "EV.app device tokens live on owner Postgres (same DB as :8000).\n"
        )
        raise SystemExit(2)
    if leftover_owner_runtime("EV_MASTER_KEY", os.environ.get("EV_MASTER_KEY") or ""):
        sys.stderr.write(
            "Talk sidecar refused to start: EV_MASTER_KEY is a test leftover.\n"
        )
        raise SystemExit(2)
    if leftover_owner_runtime("EV_VAULT_KEY", os.environ.get("EV_VAULT_KEY") or ""):
        sys.stderr.write(
            "Talk sidecar refused to start: EV_VAULT_KEY is a test leftover.\n"
        )
        raise SystemExit(2)


def refuse_brain_without_key() -> None:
    """Fail closed before daemonize so a missing model credential is visible."""

    alias_google_keys()
    refuse_talk_without_vault()
    if (
        gemini_live_selected()
        and (os.environ.get("EV_VOICE_LIVE_BRAIN") or "").strip().lower()
        not in {"auto", ""}
        and not google_key_loaded()
    ):
        sys.stderr.write(
            "Talk sidecar refused to start: GOOGLE_API_KEY is missing "
            "while Gemini Live is selected.\n"
        )
        raise SystemExit(2)
    if mimo_selected() and not openrouter_key_loaded():
        sys.stderr.write(
            "Talk sidecar refused to start: EV_OPENROUTER_API_KEY is missing "
            "while MiMo is selected.\n"
        )
        raise SystemExit(2)


def talk_listen_pids() -> list[int]:
    """PIDs listening on Talk's port. Never used to touch production :8000."""

    import subprocess

    try:
        proc = subprocess.run(
            ["lsof", "-nP", "-tiTCP:18000", "-sTCP:LISTEN"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for raw in (proc.stdout or "").split():
        try:
            pid = int(raw)
        except ValueError:
            continue
        if pid > 1:
            pids.append(pid)
    return pids


def stop_existing_talk_sidecar() -> None:
    """Replace the current Talk process so the new brain can bind :18000.

    Called only after refuse_brain_without_key(). Does not touch ev.api :8000.
    """

    import signal
    import time

    me = os.getpid()
    for pid in talk_listen_pids():
        if pid == me:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.time() + 8.0
    while time.time() < deadline:
        left = [pid for pid in talk_listen_pids() if pid != me]
        if not left:
            return
        time.sleep(0.2)
    for pid in talk_listen_pids():
        if pid == me:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue


def selected_talk_cognitive_mode() -> str:
    mode = os.environ.get("EV_TALK_COGNITIVE_MODE", "realtime_delegate").strip().lower()
    if mode not in {"realtime_delegate", "mimo_kernel", "legacy_gemini"}:
        raise SystemExit("Invalid EV_TALK_COGNITIVE_MODE; use realtime_delegate, mimo_kernel, or legacy_gemini.")
    return mode


def main() -> None:
    load(REPO / ".env")
    load(REPO / "backend" / ".env")
    # Production secrets overlay (model API keys). Leftover process env from
    # older brains cannot hide two-model flags from owner .env.
    secrets = Path(os.environ.get("EV_SECRETS_FILE", str(Path.home() / ".ev/secrets/production.env"))).expanduser()
    load(secrets)
    refuse_brain_without_key()
    refuse_talk_without_owner_runtime()
    talk_cognitive_mode = selected_talk_cognitive_mode()
    stop_existing_talk_sidecar()
    daemonize()
    SUPPORT.mkdir(parents=True, exist_ok=True)
    # Keep EV_DATABASE_URL and EV_VOICE_LIVE_MODE from .env so Talk sees the
    # same archive and shadow surface the owner actually uses.
    os.environ["EV_ENVIRONMENT"] = "dev"
    # Must overwrite after load(): shared backend/.env keeps this false so
    # production :8000 never grows a Python-side home-folder writer.
    os.environ["EV_LAPTOP_FILES"] = "true"
    os.environ["EV_HOME_STATION_MODE"] = "false"
    os.environ["EV_PROCESSING_MODE"] = "sync"
    os.environ["EV_MAINTENANCE_MODE"] = "0"
    os.environ["EV_COGNITIVE_MODE"] = talk_cognitive_mode
    os.environ["EV_COGNITIVE_ROLE"] = "voice_edge"
    os.environ.setdefault("EV_COGNITIVE_KERNEL_URL", "http://127.0.0.1:8000")
    os.environ.setdefault("EV_COGNITIVE_MAC_EXECUTE_URL", "http://127.0.0.1:18000")
    # MiMo-V2.6-Flash is the single non-speech brain for the Talk surface too.
    os.environ["EV_CHAT_PROVIDER"] = "mimo"
    os.environ["EV_ALLOW_REMOTE_CHAT"] = "true"
    ensure_talk_mouth_remote_allowed()
    os.environ.setdefault("EV_VOICEPRINT_PROVIDER", "campp")
    os.environ.setdefault("EV_SEARCH_PROVIDER", "live")
    os.environ["PATH"] = (
        "/Users/sahajpatel/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
        + os.environ.get("PATH", "")
    )
    os.chdir(REPO / "backend")
    os.execv(
        "/Users/sahajpatel/.local/bin/uv",
        ["uv", "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "18000"],
    )


if __name__ == "__main__":
    main()
