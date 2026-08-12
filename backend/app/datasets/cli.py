"""Dataset CLI: ``python -m app.datasets.cli {list,pull,verify,prune}``."""

from __future__ import annotations

import argparse
import json
import sys

from app.datasets import registry, store
from app.ml.registry import ChecksumError
from app.ml.settings import get_ml_settings


def _build_registry() -> registry.DatasetRegistry:
    reg = registry.DatasetRegistry()
    for spec in registry.builtin_datasets():
        reg.register(spec)
    return reg


def cmd_list(args: argparse.Namespace) -> int:
    reg = _build_registry()
    if args.json:
        print(json.dumps([spec.model_dump(mode="json") for spec in reg.all()], indent=2))
    else:
        for spec in reg.all():
            flag = " [eval-only]" if spec.eval_only else ""
            print(f"{spec.name:32} {spec.bytes:>12,}B{flag}  {spec.license}")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    settings = get_ml_settings()
    reg = _build_registry()
    spec = reg.get(args.name)
    path = store.download_dataset(spec, settings, stream=args.stream)
    print(f"verified {spec.name}: {path}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    settings = get_ml_settings()
    reg = _build_registry()
    names = [args.name] if args.name else reg.names()
    missing: list[str] = []
    for name in names:
        spec = reg.get(name)
        try:
            ok = store.verify_dataset(spec, settings)
        except ChecksumError as exc:
            print(f"corrupt {name}: {exc}")
            return 1
        if ok:
            print(f"ok {name}")
        else:
            missing.append(name)
    if missing:
        print(f"not cached: {', '.join(missing)}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    settings = get_ml_settings()
    removed = store.prune_datasets(
        settings,
        all_files=args.all,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(f"would remove {len(removed)} artifact(s)")
    else:
        print(f"removed {len(removed)} artifact(s)")
    for path in removed:
        print(f"  {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.datasets.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list registered datasets")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_pull = sub.add_parser("pull", help="download + checksum-verify a dataset")
    p_pull.add_argument("name")
    p_pull.add_argument("--stream", action="store_true", help="stream subsets in chunks")
    p_pull.set_defaults(func=cmd_pull)

    p_verify = sub.add_parser("verify", help="verify cached dataset checksums")
    p_verify.add_argument("name", nargs="?")
    p_verify.set_defaults(func=cmd_verify)

    p_prune = sub.add_parser("prune", help="evict least-recently-used dataset artifacts")
    p_prune.add_argument("--all", action="store_true")
    p_prune.add_argument("--dry-run", action="store_true")
    p_prune.set_defaults(func=cmd_prune)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
