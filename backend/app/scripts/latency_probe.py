"""Real-provider latency probe (WP12). Measure, never invent a threshold.

Runs N owner turns through the real kernel (``handle_turn``) against a scratch
SQLite database and the real text provider loaded from the repo ``.env``.
Production Evie (:8000) is never called, never restarted, and never written.

Honesty rules:

* a turn only counts when the kernel reports a real model turn
  (``kind == "mimo"`` and the provider-failure counter did not move); a
  reflex answer or an unavailable brain is reported, never averaged in;
* the provider name and the number of trials are printed, so a run without a
  key says OWNER-ACTION instead of a number.

Usage::

    python -m app.scripts.latency_probe                 # 25 trials, writes doc
    python -m app.scripts.latency_probe --trials 30
    python -m app.scripts.latency_probe --stdout        # print only
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
DOC = ROOT / "docs" / "EVIE_LATENCY.md"
PROOF_ROOT = Path.home() / "Desktop" / "evie-live-proof"
MIN_TRIALS = 20
ACK_BUDGET_MS = 1500
REPRO_COMMAND = "cd backend && uv run python -m app.scripts.latency_probe --stdout --trials 20"


def _load_env() -> dict[str, str]:
    """The repo .env + production secrets, exactly like the launchd wrapper."""

    from dotenv import dotenv_values

    env = {**os.environ}
    for path in (ROOT / ".env", Path.home() / ".ev" / "secrets" / "production.env"):
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None and key not in env:
                env[key] = value
    return env


def _provider_ready(env: dict[str, str]) -> tuple[bool, str]:
    """Is a real provider key present? Names and lengths only, never values."""

    key = (env.get("EV_OPENROUTER_API_KEY") or "").strip()
    provider = (env.get("EV_CHAT_PROVIDER") or "").strip() or "auto"
    if not key:
        return False, f"EV_OPENROUTER_API_KEY is empty (provider={provider})"
    return True, f"provider={provider}, key present ({len(key)} chars)"


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (the same rule for p50 and p95, no smoothing)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[rank]


async def _run_trials(trials: int, text: str) -> dict:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.cognitive import telemetry
    from app.cognitive.kernel import handle_turn
    from app.models import Base

    scratch = PROOF_ROOT / "latency-probe"
    with contextlib.suppress(OSError):
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{scratch / 'latency.db'}",
                                 future=True)
    samples: list[float] = []
    model_ms: list[float] = []
    kinds: dict[str, int] = {}
    failures = 0
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        for index in range(trials):
            before = dict(telemetry.snapshot())
            started = time.perf_counter()
            async with maker() as session:
                result = await handle_turn(
                    transcript=f"{text} (attempt {index + 1})",
                    modality="voice", actor="master", session=session,
                )
                await session.commit()
            elapsed = (time.perf_counter() - started) * 1000.0
            after = dict(telemetry.snapshot())
            kinds[result.kind] = kinds.get(result.kind, 0) + 1
            provider_failures = int(after.get("provider_failures", 0)) - int(
                before.get("provider_failures", 0))
            ran_model = (result.kind == "mimo" and provider_failures == 0
                         and int(after.get("mimo_turns", 0)) > int(before.get("mimo_turns", 0)))
            if not ran_model or not str(result.spoken or "").strip():
                failures += 1
                continue
            samples.append(round(elapsed, 1))
            reported = after.get("last_transcript_to_mimo_ms")
            if isinstance(reported, (int, float)):
                model_ms.append(round(float(reported), 1))
    finally:
        await engine.dispose()
        with contextlib.suppress(OSError):
            shutil.rmtree(scratch, ignore_errors=True)
    return {"samples_ms": samples, "model_ms": model_ms, "kinds": kinds,
            "failures": failures}


async def _time_first_token(provider, text: str) -> float | None:
    """Milliseconds until the provider stream yields the first non-empty text.

    This is the model time-to-first-token. It is not the acknowledgement.
    """

    from app.contracts import ChatMessage

    started = time.perf_counter()
    async for chunk in provider.stream_chat([ChatMessage(role="user", content=text)]):
        if getattr(chunk, "error", None):
            raise RuntimeError("provider stream error")
        if str(getattr(chunk, "text", "") or ""):
            return round((time.perf_counter() - started) * 1000.0, 1)
        if getattr(chunk, "done", False):
            break
    return None


async def _run_first_tokens(trials: int, text: str) -> dict:
    from app.gateway.roles import require_text_provider

    provider = require_text_provider()
    samples: list[float] = []
    failures = 0
    name = getattr(provider, "name", type(provider).__name__)
    for _index in range(trials):
        try:
            sample = await _time_first_token(provider, text)
        except Exception:
            failures += 1
            continue
        if sample is None:
            failures += 1
            continue
        samples.append(sample)
    return {"provider_name": name, "samples_ms": samples, "failures": failures}


async def measure_ack_asgi(trials: int = MIN_TRIALS) -> dict:
    """Time the pre-model SSE ack through the real ASGI app.

    ``run_chat_pipeline`` is held open so the sample cannot include model
    time. Refuses a non-sqlite database so this never touches production.
    """

    import asyncio

    import app.api.core as core
    from app.config import settings
    from app.db import engine
    from app.models import Base

    if "sqlite" not in str(settings.database_url):
        raise RuntimeError("ack probe refuses a non-sqlite database")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.main import app

    finished = {"n": 0}

    async def _hang(*_args, **_kwargs):
        await asyncio.Event().wait()
        finished["n"] += 1

    original = core.run_chat_pipeline
    core.run_chat_pipeline = _hang
    samples: list[float] = []

    async def _one(index: int) -> None:
        # Drive the ASGI app's send channel directly. A buffering HTTP client
        # would hide the ack behind the held pipeline; the first body message
        # is what a streaming client receives.
        payload = json.dumps({"message": f"ack only {index}", "stream": True}).encode()
        scope = {
            "type": "http",
            "asgi": {"spec_version": "2.3", "version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat",
            "raw_path": b"/v1/chat",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {settings.master_key}".encode()),
                (b"content-type", b"application/json"),
                (b"host", b"test"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        }
        state = {"delivered": False, "status": 0}
        seen = asyncio.Event()
        started = time.perf_counter()

        async def receive():
            if not state["delivered"]:
                state["delivered"] = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.start":
                state["status"] = int(message["status"])
            elif message["type"] == "http.response.body":
                body = message.get("body") or b""
                if b"accepted" in body and b"status" in body and not seen.is_set():
                    samples.append(round((time.perf_counter() - started) * 1000.0, 1))
                    seen.set()

        task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(seen.wait(), timeout=2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if state["status"] != 200:
            raise RuntimeError(f"ack HTTP {state['status']}")
        if not seen.is_set():
            raise RuntimeError("no accepted event before the pipeline returned")

    try:
        for index in range(trials):
            await _one(index)
    finally:
        core.run_chat_pipeline = original
    if len(samples) != trials:
        raise RuntimeError(f"ack samples {len(samples)} != {trials}")
    return {
        "trials": trials,
        "samples_ms": samples,
        "p50_ms": round(_percentile(samples, 50), 1),
        "p95_ms": round(_percentile(samples, 95), 1),
        "budget_ms": ACK_BUDGET_MS,
        "pipeline_finished": finished["n"],
    }


def _ack_subprocess(trials: int) -> dict:
    """Ack measurement in a child with a scratch sqlite. Never the owner DB."""

    import tempfile

    scratch = Path(tempfile.mkdtemp(prefix="ev-ack-"))
    env = {key: value for key, value in os.environ.items() if not key.startswith("EV_")}
    env.update({
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "EV_DATABASE_URL": f"sqlite+aiosqlite:///{scratch / 'ack.db'}",
        "EV_MASTER_KEY": "ack-probe-key",
        "EV_API_KEY": "ack-probe-key",
        "EV_VAULT_KEY": "ack-vault-key-0123456789abcdef",
        "EV_CHAT_PROVIDER": "echo",
        "EV_SECRETS_FILE": str(scratch / "none.env"),
        "EV_ENVIRONMENT": "test",
        "EV_ENV": "test",
        "EV_PROCESSING_MODE": "sync",
        "EV_REDIS_URL": "redis://127.0.0.1:9/15",
        "EV_EMBEDDING_PROVIDER": "hash",
        "EV_SEARCH_PROVIDER": "none",
        "EV_COGNITIVE_MODE": "legacy_gemini",
        "EV_ACK_TRIALS": str(max(MIN_TRIALS, trials)),
    })
    script = (
        "import asyncio, json, os\n"
        "from app.scripts.latency_probe import measure_ack_asgi\n"
        "print('ACKJSON:' + json.dumps(asyncio.run("
        "measure_ack_asgi(int(os.environ['EV_ACK_TRIALS'])))))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        return {"error": "ack subprocess failed", "tail": tail, "samples_ms": []}
    line = next((item for item in reversed(proc.stdout.splitlines()) if item.startswith("ACKJSON:")), "")
    if not line:
        return {"error": "ack subprocess produced no JSON", "samples_ms": []}
    return json.loads(line.split("ACKJSON:", 1)[1])


def probe(trials: int, text: str, *, measure_ack: bool = False) -> dict:
    env = _load_env()
    for key, value in env.items():
        if key.startswith("EV_") and key not in os.environ:
            os.environ[key] = value
    ready, provider_detail = _provider_ready(env)
    result: dict = {
        "started": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "trials_requested": trials,
        "provider": provider_detail,
        "verdict": "UNVERIFIED",
        "samples_ms": [],
        "repro": REPRO_COMMAND,
    }
    if measure_ack:
        result["ack"] = _ack_subprocess(trials)
    if not ready:
        # Honest gap: no key means no model number. Ack is independent of the key.
        result["verdict"] = "OWNER-ACTION"
        result["blocker"] = provider_detail
        return result
    import asyncio

    measured = asyncio.run(_run_trials(trials, text))
    try:
        first = asyncio.run(_run_first_tokens(trials, text))
    except Exception:
        first = {"provider_name": "unavailable", "samples_ms": [], "failures": trials}
    result["first_token_ms"] = first["samples_ms"]
    result["first_token_failures"] = first["failures"]
    result["first_token_provider"] = first.get("provider_name")
    if first["samples_ms"]:
        result["first_token_p50_ms"] = round(_percentile(first["samples_ms"], 50), 1)
        result["first_token_p95_ms"] = round(_percentile(first["samples_ms"], 95), 1)
    else:
        result["first_token_verdict"] = "UNVERIFIED"
    samples = measured["samples_ms"]
    result["samples_ms"] = samples
    result["model_ms"] = measured["model_ms"]
    result["kinds"] = measured["kinds"]
    result["failures"] = measured["failures"]
    if not samples:
        result["verdict"] = "FAIL"
        result["blocker"] = (f"no real model turn in {trials} attempts "
                            f"(kinds={measured['kinds']}, failures={measured['failures']})")
        return result
    result["p50_ms"] = round(_percentile(samples, 50), 1)
    result["p95_ms"] = round(_percentile(samples, 95), 1)
    result["min_ms"] = round(min(samples), 1)
    result["max_ms"] = round(max(samples), 1)
    result["mean_ms"] = round(statistics.fmean(samples), 1)
    if measured["model_ms"]:
        result["model_p50_ms"] = round(_percentile(measured["model_ms"], 50), 1)
        result["model_p95_ms"] = round(_percentile(measured["model_ms"], 95), 1)
    result["verdict"] = ("PASS" if len(samples) >= MIN_TRIALS and measured["failures"] == 0
                         else "PARTIAL")
    return result


def render(result: dict) -> str:
    lines = [
        "## Real-provider latency probe — " + str(result["verdict"]),
        "",
        f"{result['started']}. Real kernel turns against a scratch SQLite "
        "database; production Evie untouched.",
        "",
        f"Provider: {result['provider']}. Counted turns: "
        f"{len(result.get('samples_ms') or [])}/{result['trials_requested']} "
        f"(unusable: {result.get('failures', 0)}; kinds: "
        f"{json.dumps(result.get('kinds') or {}, sort_keys=True)}).",
    ]
    if result.get("samples_ms"):
        lines += [
            "",
            "| metric | ms |",
            "| --- | --- |",
            f"| min | {result['min_ms']} |",
            f"| p50 | {result['p50_ms']} |",
            f"| mean | {result['mean_ms']} |",
            f"| p95 | {result['p95_ms']} |",
            f"| max | {result['max_ms']} |",
        ]
        if result.get("model_p50_ms") is not None:
            lines += [
                "",
                f"Server-reported transcript→model span: p50 "
                f"{result['model_p50_ms']} ms, p95 {result['model_p95_ms']} ms "
                "(kernel telemetry, not re-derived here).",
            ]
        lines += [
            "",
            "Full turn = provider round-trip + kernel bookkeeping. "
            "It is not the acknowledgement and it is not time-to-first-token.",
        ]
    first = result.get("first_token_ms") or []
    if first:
        lines += [
            "",
            "First token (provider stream, separate from the acknowledgement):",
            "",
            "| metric | ms |",
            "| --- | --- |",
            f"| count | {len(first)} |",
            f"| p50 | {result.get('first_token_p50_ms')} |",
            f"| p95 | {result.get('first_token_p95_ms')} |",
            f"| provider | {result.get('first_token_provider')} |",
        ]
    elif result.get("samples_ms") or result.get("first_token_verdict"):
        lines += [
            "",
            "First token: UNVERIFIED (provider stream produced no text).",
            f"Reproduction: `{result.get('repro') or REPRO_COMMAND}`",
        ]
    ack = result.get("ack") or {}
    if ack.get("samples_ms"):
        lines += [
            "",
            "Acknowledgement (SSE status/accepted through the ASGI app, "
            "pipeline held so model time is excluded):",
            f"trials {ack.get('trials')}, p95 {ack.get('p95_ms')} ms, "
            f"budget {ack.get('budget_ms')} ms, "
            f"pipeline_finished {ack.get('pipeline_finished')}.",
        ]
    elif ack.get("error"):
        lines += ["", f"Acknowledgement measurement failed: {ack['error']}"]
    if result.get("blocker"):
        lines += ["", f"**Blocker:** {result['blocker']}"]
        lines += ["", f"Reproduction: `{result.get('repro') or REPRO_COMMAND}`"]
    return "\n".join(lines).rstrip() + "\n"


def write_doc(result: dict) -> None:
    section = render(result)
    head = ("# Evie latency (evie-fabric final run)\n\n"
            "Generated by `python -m app.scripts.latency_probe`. Measured, not "
            "asserted: a run without a provider key reports OWNER-ACTION instead "
            "of a number.\n")
    DOC.write_text(head + "\n" + section)
    print(f"[latency] wrote {DOC}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="latency-probe")
    parser.add_argument("--trials", type=int, default=25)
    parser.add_argument("--text", default="In one short sentence, name three colours.")
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--json", default="")
    parser.add_argument("--from-json", default="",
                        help="render the doc from a recorded run (no new calls)")
    parser.add_argument("--no-ack", action="store_true",
                        help="skip the ASGI acknowledgement measurement")
    args = parser.parse_args(argv)
    if args.from_json:
        result = json.loads(Path(args.from_json).read_text())
        print(render(result))
        if not args.stdout:
            write_doc(result)
        return 0 if result.get("verdict") in {"PASS", "PARTIAL"} else 1
    trials = max(MIN_TRIALS, int(args.trials))
    result = probe(trials, args.text, measure_ack=not args.no_ack)
    print(render(result))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, sort_keys=True))
    if not args.stdout:
        write_doc(result)
    ok = result["verdict"] in {"PASS", "PARTIAL"}
    ack = result.get("ack") or {}
    if not args.no_ack and (ack.get("error") or float(ack.get("p95_ms") or 10**9) > ACK_BUDGET_MS):
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
