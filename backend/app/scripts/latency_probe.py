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
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
DOC = ROOT / "docs" / "EVIE_LATENCY.md"
PROOF_ROOT = Path.home() / "Desktop" / "evie-live-proof"
MIN_TRIALS = 20


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

    key = (env.get("EV_DEEPSEEK_API_KEY") or "").strip()
    provider = (env.get("EV_CHAT_PROVIDER") or "").strip() or "auto"
    if not key:
        return False, f"EV_DEEPSEEK_API_KEY is empty (provider={provider})"
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


def probe(trials: int, text: str) -> dict:
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
    }
    if not ready:
        # Honest gap: no key means no real measurement. The fabric turn budget
        # tests (S18/S22) still cover the ack/stop path.
        result["verdict"] = "OWNER-ACTION"
        result["blocker"] = provider_detail
        return result
    import asyncio

    measured = asyncio.run(_run_trials(trials, text))
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
            "Full turn = provider round-trip + kernel bookkeeping. Time-to-first-"
            "token is not instrumented yet (UNVERIFIED); the ack budget "
            "(≤1500 ms) and stop budget (≤2 s) are covered by S18/S22.",
        ]
    if result.get("blocker"):
        lines += ["", f"**Blocker:** {result['blocker']}"]
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
    args = parser.parse_args(argv)
    if args.from_json:
        result = json.loads(Path(args.from_json).read_text())
        print(render(result))
        if not args.stdout:
            write_doc(result)
        return 0 if result.get("verdict") in {"PASS", "PARTIAL"} else 1
    trials = max(MIN_TRIALS, int(args.trials))
    result = probe(trials, args.text)
    print(render(result))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, sort_keys=True))
    if not args.stdout:
        write_doc(result)
    return 0 if result["verdict"] in {"PASS", "PARTIAL"} else 1


if __name__ == "__main__":
    sys.exit(main())
