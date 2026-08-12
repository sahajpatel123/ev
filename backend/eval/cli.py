"""``ev-eval`` console script: canonical ML eval artifacts.

Owned by Agent 2 (Foundry). Each subcommand calls the owning agent's existing
measurement entry point (never reimplementing their maths) and writes a
schema-versioned artifact into ``backend/eval/ml/``. Measurements whose entry
point has not landed, or whose data directories were not supplied, are skipped
with the exact reason string Agent 20's gates produce for a missing artifact.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ML_EVAL_DIR = Path(__file__).resolve().parent / "ml"

# name -> (artifact filename, schema version)
ARTIFACTS: dict[str, tuple[str, str]] = {
    "retrieval": ("retrieval_quality.json", "ev.retrieval.eval.v1"),
    "asr": ("asr_quality.json", "ev.asr.eval.v1"),
    "speaker": ("speaker_security.json", "ev.speaker.eval.v1"),
    "face": ("face_recognition.json", "ev.face.eval.v1"),
    "wake": ("wake_reliability.json", "ev.wake.eval.v1"),
}

# Produce hints mirror the skip strings emitted by
# app/scripts/eval_gates.py::_skip_missing for the same gates.
SKIP_HINTS: dict[str, str] = {
    "asr": (
        "run Agent 4's ASR eval with real weights and write "
        '{"schema":"ev.asr.eval.v1","provider":"parakeet-eou-120m",'
        '"degraded":false,"wer_clean":0.07,"wer_owner_speech":0.10}'
    ),
    "speaker": (
        "run `python -m app.voice.speaker eval --owner-dir ... --impostor-dir ...` "
        "with real CAM++/ECAPA weights and write the JSON to the artifact path"
    ),
    "face": (
        "run `python -m app.people.eval --people-dir ... --strangers-dir ... "
        "--report eval/ml/face_recognition.json` with the SFace model and "
        "consented photo sets"
    ),
    "wake": (
        "run Agent 3's wake eval against the trained openWakeWord head "
        '({"provider":"openwakeword","degraded":false,'
        '"false_accepts_per_12h":0.0,"recall":0.95,"hours_audio":12})'
    ),
}


def artifact_path(name: str) -> Path:
    filename, _schema = ARTIFACTS[name]
    return ML_EVAL_DIR / filename


def skip_reason(name: str) -> str:
    return f"no eval artifact at {artifact_path(name)}; {SKIP_HINTS[name]}"


def canonicalize(name: str, report: dict) -> dict:
    """Stamp the canonical schema/version/producer onto a measured report."""

    schema = ARTIFACTS[name][1]
    payload = dict(report)
    payload.setdefault("schema", schema)
    payload.setdefault("schema_version", schema)
    payload.setdefault("degraded", False)
    payload["producer"] = "ev-eval"
    payload.setdefault("generated_at", datetime.now(UTC).isoformat(timespec="seconds"))
    return payload


def write_artifact(name: str, report: dict, *, path: Path | None = None) -> Path:
    target = path or artifact_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(canonicalize(name, report), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"{name}: report written to {target}")
    return target


def _run_module(module: str, argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, *argv],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _extract_json(text: str) -> dict:
    start = text.index("{")
    value, _end = json.JSONDecoder().raw_decode(text, start)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object in eval output")
    return value


def cmd_retrieval(args: argparse.Namespace) -> int:
    target = artifact_path("retrieval") if args.out is None else args.out
    if args.dry_run:
        print(f"retrieval: would write {target}")
        return 0
    from eval.retrieval.cli import main as retrieval_main

    target.parent.mkdir(parents=True, exist_ok=True)
    argv = ["retrieval", "--out", str(target)]
    if args.k:
        argv += ["--k", str(args.k)]
    if args.rerank:
        argv.append("--rerank")
    if args.questions:
        argv += ["--questions", str(args.questions)]
    if args.database_url:
        argv += ["--database-url", args.database_url]
    code = retrieval_main(argv)
    if code != 0:
        return code
    report = json.loads(target.read_text(encoding="utf-8"))
    write_artifact("retrieval", report, path=target)
    return 0


def cmd_asr(args: argparse.Namespace) -> int:
    target = artifact_path("asr") if args.out is None else args.out
    if args.dry_run:
        print(f"asr: would write {target}")
        return 0
    try:
        asr_module: Any = importlib.import_module("eval.ml.asr_eval")
    except ImportError:
        # Agent 4's harness has not landed yet; skip with the exact gate reason.
        print(skip_reason("asr"))
        return 0
    canonical = artifact_path("asr")
    canonical.parent.mkdir(parents=True, exist_ok=True)
    original_out = asr_module.OUT_PATH
    asr_module.OUT_PATH = canonical
    try:
        code = int(asr_module.main([f"--samples={args.samples}"] if args.samples else []))
    except SystemExit as exc:
        write_artifact(
            "asr",
            {
                "provider": "asr-eval",
                "measured": False,
                "degraded": True,
                "error": str(exc.code) if exc.code is not None else str(exc),
            },
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - record the failure honestly
        write_artifact(
            "asr",
            {
                "provider": "asr-eval",
                "measured": False,
                "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return 2
    finally:
        asr_module.OUT_PATH = original_out
    if canonical.exists():
        report = json.loads(canonical.read_text(encoding="utf-8"))
        if report.get("measured") is False:
            report["degraded"] = True
        write_artifact("asr", report)
        if args.out is not None and args.out.resolve() != canonical.resolve():
            args.out.write_text(canonical.read_text(encoding="utf-8"))
            print(f"asr: copy written to {args.out}")
    return code


def cmd_speaker(args: argparse.Namespace) -> int:
    if args.dry_run:
        target = artifact_path("speaker") if args.out is None else args.out
        if args.owner_dir and args.impostor_dir:
            print(f"speaker: would write {target}")
        else:
            print(skip_reason("speaker"))
        return 0
    if not args.owner_dir or not args.impostor_dir:
        print(skip_reason("speaker"))
        return 0
    argv = [
        "eval",
        "--owner-dir",
        str(args.owner_dir),
        "--impostor-dir",
        str(args.impostor_dir),
    ]
    if args.roc_out:
        argv += ["--roc-out", str(args.roc_out)]
    if args.test_double:
        argv.append("--test-double")
    proc = _run_module("app.voice.speaker", argv)
    if proc.returncode != 0:
        print(proc.stderr.strip() or proc.stdout.strip(), file=sys.stderr)
        return 1
    try:
        report = _extract_json(proc.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"speaker: could not parse eval output: {exc}", file=sys.stderr)
        return 1
    write_artifact("speaker", report, path=args.out)
    return 0


def cmd_face(args: argparse.Namespace) -> int:
    target = artifact_path("face") if args.out is None else args.out
    if args.dry_run:
        if args.people_dir and args.strangers_dir:
            print(f"face: would write {target}")
        else:
            print(skip_reason("face"))
        return 0
    if not args.people_dir or not args.strangers_dir:
        print(skip_reason("face"))
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "--people-dir",
        str(args.people_dir),
        "--strangers-dir",
        str(args.strangers_dir),
        "--report",
        str(target),
    ]
    if args.target_far:
        argv += ["--target-far", str(args.target_far)]
    if args.grant_consent:
        argv.append("--grant-consent")
    proc = _run_module("app.people.eval", argv)
    if proc.returncode != 0:
        print(proc.stderr.strip() or proc.stdout.strip(), file=sys.stderr)
        return 1
    report = json.loads(target.read_text(encoding="utf-8"))
    write_artifact("face", report, path=target)
    return 0


def cmd_wake(args: argparse.Namespace) -> int:
    target = artifact_path("wake") if args.out is None else args.out
    if args.dry_run:
        print(f"wake: would write {target}")
        return 0
    if not args.held_out_dir or not args.ambient or (not args.model_path and not args.test_double):
        print(skip_reason("wake"))
        return 0
    try:
        wake_module: Any = importlib.import_module("app.audio.wake_eval")
    except ImportError:
        # Agent 3's harness has not landed yet; skip with the exact gate reason.
        print(skip_reason("wake"))
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    argv = ["--report", str(target)]
    if args.held_out_dir:
        argv += ["--held-out-dir", str(args.held_out_dir)]
    if args.ambient:
        argv += ["--ambient", str(args.ambient)]
    if args.model_path:
        argv += ["--model-path", str(args.model_path)]
    if args.verifier_path:
        argv += ["--verifier-path", str(args.verifier_path)]
    if args.threshold is not None:
        argv += ["--threshold", str(args.threshold)]
    if args.hours is not None:
        argv += ["--hours", str(args.hours)]
    if args.test_double:
        argv.append("--test-double")
    try:
        code = int(wake_module.main(argv))
    except Exception as exc:  # noqa: BLE001 - CLI reports and exits non-zero
        print(f"wake: eval failed: {exc}", file=sys.stderr)
        return 1
    if target.exists():
        report = json.loads(target.read_text(encoding="utf-8"))
        write_artifact("wake", report, path=target)
    return code


def _all_argv(name: str, args: argparse.Namespace) -> list[str]:
    if name == "retrieval":
        return ["--rerank"] if args.rerank else []
    if name == "speaker":
        argv: list[str] = []
        if args.owner_dir:
            argv += ["--owner-dir", str(args.owner_dir)]
        if args.impostor_dir:
            argv += ["--impostor-dir", str(args.impostor_dir)]
        if args.test_double:
            argv.append("--test-double")
        return argv
    if name == "face":
        argv = []
        if args.people_dir:
            argv += ["--people-dir", str(args.people_dir)]
        if args.strangers_dir:
            argv += ["--strangers-dir", str(args.strangers_dir)]
        if args.grant_consent:
            argv.append("--grant-consent")
        return argv
    if name == "wake":
        argv = []
        if args.held_out_dir:
            argv += ["--held-out-dir", str(args.held_out_dir)]
        if args.ambient:
            argv += ["--ambient", str(args.ambient)]
        if args.model_path:
            argv += ["--model-path", str(args.model_path)]
        if args.verifier_path:
            argv += ["--verifier-path", str(args.verifier_path)]
        if args.threshold is not None:
            argv += ["--threshold", str(args.threshold)]
        if args.hours is not None:
            argv += ["--hours", str(args.hours)]
        if args.test_double:
            argv.append("--test-double")
        return argv
    return []


def cmd_all(args: argparse.Namespace) -> int:
    failures = 0
    for name in ARTIFACTS:
        argv = [name, *_all_argv(name, args)]
        if args.dry_run:
            argv.append("--dry-run")
        code = main(argv)
        # 2 = measurement ran but was degraded/unmeasured; the artifact records
        # it honestly and the gates SKIP. Only unexpected failures count.
        failures += code not in (0, 2)
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ev-eval",
        description="Run ML evaluations and write canonical artifacts to backend/eval/ml/",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_retrieval = sub.add_parser("retrieval", help="Agent 8 retrieval quality eval")
    p_retrieval.add_argument("--out", type=Path)
    p_retrieval.add_argument("--k", type=int)
    p_retrieval.add_argument("--rerank", action="store_true")
    p_retrieval.add_argument("--questions", type=Path)
    p_retrieval.add_argument("--database-url")
    p_retrieval.add_argument("--dry-run", action="store_true")
    p_retrieval.set_defaults(func=cmd_retrieval)

    p_asr = sub.add_parser("asr", help="Agent 4 ASR quality eval")
    p_asr.add_argument("--out", type=Path)
    p_asr.add_argument("--samples", type=int)
    p_asr.add_argument("--dry-run", action="store_true")
    p_asr.set_defaults(func=cmd_asr)

    p_speaker = sub.add_parser("speaker", help="Agent 5 speaker security eval")
    p_speaker.add_argument("--owner-dir", type=Path)
    p_speaker.add_argument("--impostor-dir", type=Path)
    p_speaker.add_argument("--roc-out", type=Path)
    p_speaker.add_argument("--test-double", action="store_true")
    p_speaker.add_argument("--out", type=Path)
    p_speaker.add_argument("--dry-run", action="store_true")
    p_speaker.set_defaults(func=cmd_speaker)

    p_face = sub.add_parser("face", help="Agent 7 face recognition eval")
    p_face.add_argument("--people-dir", type=Path)
    p_face.add_argument("--strangers-dir", type=Path)
    p_face.add_argument("--target-far", type=float)
    p_face.add_argument("--grant-consent", action="store_true")
    p_face.add_argument("--out", type=Path)
    p_face.add_argument("--dry-run", action="store_true")
    p_face.set_defaults(func=cmd_face)

    p_wake = sub.add_parser("wake", help="Agent 3 wake reliability eval")
    p_wake.add_argument("--held-out-dir", type=Path)
    p_wake.add_argument("--ambient", type=Path)
    p_wake.add_argument("--model-path")
    p_wake.add_argument("--verifier-path")
    p_wake.add_argument("--threshold", type=float)
    p_wake.add_argument("--hours", type=float)
    p_wake.add_argument("--test-double", action="store_true")
    p_wake.add_argument("--out", type=Path)
    p_wake.add_argument("--dry-run", action="store_true")
    p_wake.set_defaults(func=cmd_wake)

    p_all = sub.add_parser("all", help="run every available eval, skip the rest")
    p_all.add_argument("--dry-run", action="store_true")
    p_all.add_argument("--rerank", action="store_true")
    p_all.add_argument("--owner-dir", type=Path)
    p_all.add_argument("--impostor-dir", type=Path)
    p_all.add_argument("--test-double", action="store_true")
    p_all.add_argument("--people-dir", type=Path)
    p_all.add_argument("--strangers-dir", type=Path)
    p_all.add_argument("--grant-consent", action="store_true")
    p_all.add_argument("--held-out-dir", type=Path)
    p_all.add_argument("--ambient", type=Path)
    p_all.add_argument("--model-path")
    p_all.add_argument("--verifier-path")
    p_all.add_argument("--threshold", type=float)
    p_all.add_argument("--hours", type=float)
    p_all.set_defaults(func=cmd_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
