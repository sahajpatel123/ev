"""Operator CLI for the EV MLX LoRA trainer.

Usage:

    python -m app.training.cli dry-run --dataset sft.jsonl
    python -m app.training.cli train --dataset sft.jsonl --out ~/.ev/adapters
    python -m app.training.cli evaluate --adapter-ref ~/.ev/adapters/runs/...
    python -m app.training.cli rollback --adapter-ref ~/.ev/adapters/adapters/...
    python -m app.training.cli status --run-dir ~/.ev/adapters/runs/...

The real consent/cost/activation gates live in the API
(``POST /v1/training/adapter/*``); this CLI is the operator's dry-run/train/
rollback surface against the same provider boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.training import corpus as corpus_service
from app.training.adapter import (
    TrainingDataset,
    TrainingRunError,
    local_inference_target_configured,
)
from app.training.eval import overfit_report
from app.training.lora import LoRASettings, MLXLoRAProvider
from app.training.lora import rollback as lora_rollback
from app.utils.text import sha256_hex


def _read_dataset(path: Path, *, fmt: str = "canonical") -> TrainingDataset:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]
    formatted = corpus_service.format_records(records, fmt=fmt)
    required = {"sft": ("instruction", "output"), "preference": ("chosen", "rejected")}.get(fmt)
    if not formatted and required and all(
        any(key in record for key in required) for record in records
    ):
        formatted = records
        for record in formatted:
            record.setdefault("hash", corpus_service._entry_hash(record))
    return TrainingDataset(
        corpus_version=0,
        records=formatted,
        jsonl=corpus_service.to_jsonl(formatted),
        content_hash=sha256_hex("".join(sorted(lines))),
    )


def _settings(args: argparse.Namespace) -> LoRASettings:
    kwargs = {}
    if args.base_model:
        kwargs["base_model"] = args.base_model
    if args.out:
        kwargs["output_root"] = Path(args.out).expanduser()
    if args.min_pairs is not None:
        kwargs["min_owner_pairs"] = args.min_pairs
    if args.force_double:
        kwargs["force_double"] = True
    if args.train_mode:
        kwargs["train_mode"] = args.train_mode
    return LoRASettings(**kwargs)


def cmd_dry_run(args: argparse.Namespace) -> int:
    dataset = _read_dataset(args.dataset, fmt=args.format)
    provider = MLXLoRAProvider(_settings(args))
    try:
        plan = asyncio.run(provider.estimate(dataset, base_model=args.base_model))
    except TrainingRunError as exc:
        print(f"dry-run refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    dataset = _read_dataset(args.dataset, fmt=args.format)
    provider = MLXLoRAProvider(_settings(args))
    if getattr(provider, "staged", False) and not local_inference_target_configured():
        print(
            "refused: MLX LoRA weight training is staged and no servable local "
            "inference target is configured (EV_CHAT_PROVIDER=local + "
            "EV_LOCAL_MODEL_BASE_URL). Use the active prompt-level personalization "
            "instead.",
            file=sys.stderr,
        )
        return 2
    result = asyncio.run(
        provider.train(
            dataset,
            base_model=args.base_model,
            adapter_ref=args.adapter_ref or "evie-mlx",
            cost_approved=args.cost_approved,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    run_dir = Path(args.adapter_ref).expanduser()
    eval_path = run_dir / "eval.json"
    if not eval_path.exists():
        print(f"no eval.json at {run_dir}", file=sys.stderr)
        return 2
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    evaluation = payload.get("evaluation", payload)
    print(json.dumps(evaluation, indent=2, sort_keys=True))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser()
    status_path = run_dir / "status.jsonl"
    if not status_path.exists():
        print(f"no status.jsonl at {run_dir}", file=sys.stderr)
        return 2
    for line in status_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            print(line)
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    result = lora_rollback(args.adapter_ref)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result.get("base_byte_identical", False):
        return 1
    return 0


def cmd_losses(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser()
    losses_path = run_dir / "losses.jsonl"
    if not losses_path.exists():
        print(f"no losses.jsonl at {run_dir}", file=sys.stderr)
        return 2
    train: list[float] = []
    val: list[float] = []
    for line in losses_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        target = val if row.get("split") == "val" else train
        target.append(float(row["loss"]))
    print(json.dumps({"train": train, "val": val}, indent=2))
    print(json.dumps(overfit_report(train, val), indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.training.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("dry-run", "train"):
        p = sub.add_parser(name)
        p.add_argument("--dataset", required=True, type=Path)
        p.add_argument("--format", default="sft", choices=("canonical", "sft", "preference", "tool"))
        p.add_argument("--base-model", default=None)
        p.add_argument("--out", default=None)
        p.add_argument("--min-pairs", type=int, default=None)
        p.add_argument("--force-double", action="store_true")
        p.add_argument("--train-mode", choices=("sft", "dpo"), default="sft")
        if name == "train":
            p.add_argument("--adapter-ref", default=None)
            p.add_argument("--cost-approved", action="store_true")
        p.set_defaults(func=cmd_dry_run if name == "dry-run" else cmd_train)

    p = sub.add_parser("evaluate")
    p.add_argument("--adapter-ref", required=True)
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("status")
    p.add_argument("--run-dir", required=True, type=Path)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("losses")
    p.add_argument("--run-dir", required=True, type=Path)
    p.set_defaults(func=cmd_losses)

    p = sub.add_parser("rollback")
    p.add_argument("--adapter-ref", required=True)
    p.set_defaults(func=cmd_rollback)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
