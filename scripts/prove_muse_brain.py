"""Owner-facing Muse Brain V1 proof against Talk :18000.

Requires META_MODEL_API_KEY in the secrets overlay. Never prints the value.
Does not touch production ev.api on :8000.

Usage: python3 scripts/prove_muse_brain.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path("/Users/sahajpatel/Code/ev")
SIDECAR = "http://127.0.0.1:18000"


def _sidecar():
    import importlib.util

    path = REPO / "scripts" / "start_talk_sidecar.py"
    spec = importlib.util.spec_from_file_location("start_talk_sidecar", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _health() -> dict | None:
    try:
        with urllib.request.urlopen(f"{SIDECAR}/v1/health", timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _head_sha() -> str:
    out = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=str(REPO),
        text=True,
    )
    return (out or "").strip()


def _is_muse(body: dict | None) -> bool:
    if not isinstance(body, dict):
        return False
    providers = body.get("providers") or {}
    models = body.get("models") or {}
    voice = models.get("voice") or {}
    turn = models.get("turn_control") or {}
    manager = models.get("manager") or {}
    muse = models.get("muse") or {}
    runtime = body.get("runtime") or {}
    checks = {
        row.get("name"): row
        for row in (runtime.get("checks") or [])
        if isinstance(row, dict)
    }
    tts = checks.get("tts") or {}
    tts_ok = (not tts) or tts.get("provider") == "edge_tts"
    sha = (body.get("git") or {}).get("sha")
    return (
        providers.get("chat") == "meta_muse_spark"
        and providers.get("live") == "pipeline"
        and voice.get("provider") == "meta_muse_voice"
        and voice.get("model") == "muse-voice-transcribe-1.0"
        and turn.get("provider") == "meta_muse_spark"
        and turn.get("model") == "muse-spark-1.3-contributor"
        and manager.get("provider") == "meta_muse_spark"
        and manager.get("model") == "muse-spark-1.3-contributor"
        and muse.get("reasoning_effort") == "high"
        and tts_ok
        and sha == _head_sha()
    )


def wait_for_muse(*, seconds: float = 90.0) -> dict:
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        last = _health()
        if _is_muse(last):
            return last
        time.sleep(0.5)
    sys.stderr.write(
        "Talk sidecar on :18000 is not the current Muse brain "
        "(need chat=meta_muse_spark live=pipeline voice=meta_muse_voice "
        "turn_control=meta_muse_spark/1.3 manager=meta_muse_spark/1.3 "
        "tts=edge_tts reasoning_effort=high and git.sha=HEAD).\n"
    )
    raise SystemExit(3)


def main() -> None:
    mod = _sidecar()
    mod.load(REPO / ".env")
    mod.load(REPO / "backend" / ".env")
    secrets = Path(
        os.environ.get("EV_SECRETS_FILE", str(Path.home() / ".ev/secrets/production.env"))
    ).expanduser()
    mod.load(secrets)
    mod.refuse_muse_without_key()
    # Export Muse Talk selection before (re)starting the sidecar so its
    # launcher keeps the Muse brain even when the repo .env selects S2S
    # voice. The sidecar child inherits this process env.
    for _key, _value in {
        "EV_CHAT_PROVIDER": "meta_muse_spark",
        "EV_INTELLIGENCE_PROVIDER": "meta_muse_spark",
        "EV_TURN_CONTROL_PROVIDER": "meta_muse_spark",
        "EV_VOICE_ASR_PROVIDER": "meta_muse_voice",
        "EV_VOICE_TTS_PROVIDER": "edge_tts",
        "EV_VOICE_LIVE_BRAIN": "pipeline",
    }.items():
        os.environ[_key] = _value
    if not _is_muse(_health()):
        subprocess.check_call(
            [sys.executable, str(REPO / "scripts" / "start_talk_sidecar.py")],
            cwd=str(REPO),
        )
        wait_for_muse()
    env = os.environ.copy()
    env["EV_TEST_USE_LIVE_MUSE"] = "1"
    env["EV_ALLOW_REMOTE_ASR"] = "true"
    env["EV_CHAT_PROVIDER"] = "meta_muse_spark"
    env["EV_INTELLIGENCE_PROVIDER"] = "meta_muse_spark"
    env["EV_TURN_CONTROL_PROVIDER"] = "meta_muse_spark"
    env["EV_VOICE_ASR_PROVIDER"] = "meta_muse_voice"
    env["EV_VOICE_TTS_PROVIDER"] = "edge_tts"
    env["EV_VOICE_LIVE_BRAIN"] = "pipeline"
    env["EV_XAI_API_KEY"] = ""
    env["EV_OPENAI_API_KEY"] = ""
    env["EV_DEEPSEEK_API_KEY"] = ""
    env["EV_OPENCODE_API_KEY"] = ""
    result = subprocess.run(
        [
            "/Users/sahajpatel/.local/bin/uv",
            "run",
            "pytest",
            "tests/test_muse_live.py",
            "-q",
            "--tb=short",
        ],
        cwd=str(REPO / "backend"),
        env=env,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
