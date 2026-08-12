"""Model CLI: ``python -m app.ml.cli {list,pull,verify,prune,stats,doctor}``."""

from __future__ import annotations

import argparse
import json
import sys

from app.ml import arbiter, registry, store
from app.ml.device import detect_posture
from app.ml.registry import ModelTier
from app.ml.settings import get_ml_settings

API_FIRST_MODELS = ("wake-openwakeword", "speaker-campp", "embed-granite-r2", "face-sface")


def _build_registry() -> registry.ModelRegistry:
    settings = get_ml_settings()
    reg = registry.ModelRegistry(exclusive_limit_mb=settings.ml_exclusive_limit_mb)
    for spec in registry.builtin_models():
        reg.register(spec)
    return reg


def _print_table(rows: list[list[str]]) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=False)))


def cmd_list(args: argparse.Namespace) -> int:
    reg = _build_registry()
    rows = [
        ["name", "task", "tier", "resident_mb", "disk_mb", "optional", "license"],
        *[
            [
                spec.name,
                spec.task,
                spec.tier.value,
                str(spec.resident_mb),
                str(spec.disk_mb),
                "yes" if spec.optional else "no",
                spec.license,
            ]
            for spec in reg.all()
        ],
    ]
    if args.json:
        print(json.dumps([spec.model_dump(mode="json") for spec in reg.all()], indent=2))
    else:
        _print_table(rows)
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    settings = get_ml_settings()
    reg = _build_registry()
    spec = reg.get(args.name)
    path = store.download_model(spec, settings)
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
            ok = store.verify_model(spec, settings)
        except registry.ChecksumError as exc:
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
    a = create_arbiter()
    protected = set(a.active_names())
    removed = store.prune_models(
        settings,
        all_files=args.all,
        dry_run=args.dry_run,
        protected=protected,
    )
    if args.dry_run:
        print(f"would remove {len(removed)} artifact(s)")
    else:
        print(f"removed {len(removed)} artifact(s)")
    for path in removed:
        print(f"  {path}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    a = create_arbiter()
    a.pin_always()
    print(json.dumps(a.stats(), indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_ml_settings()
    a = create_arbiter()
    a.pin_always()
    stats = a.stats()
    reg = _build_registry()
    posture = detect_posture(settings)
    api_first: list[tuple[str, str]] = []
    pinned_mb = 0
    for name in API_FIRST_MODELS:
        spec = reg.get(name)
        try:
            status = "present" if store.verify_model(spec, settings) else "missing"
        except registry.ChecksumError:
            status = "corrupt"
        api_first.append((name, status))
        if spec.tier is ModelTier.ALWAYS:
            pinned_mb += spec.resident_mb
    print(f"posture: {posture}")
    print(f"api_first_pinned_mb: {pinned_mb}")
    for name, status in api_first:
        print(f"  {name}: {status}")
    print(f"backend: {stats['backend']} ({stats['backend_reason']})")
    print(f"ceiling_mb: {stats['ceiling_mb']}")
    print(f"resident_total_mb: {stats['resident_total_mb']}")
    print(f"free_disk_gb: {stats['free_disk_gb']}")
    print(f"model_dir: {settings.ml_model_dir}")
    print(f"on_demand_slot_mb: {stats['on_demand_slot_mb']}")
    return 0


def create_arbiter() -> arbiter.ModelArbiter:
    return arbiter.create_default_arbiter(get_ml_settings())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.ml.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list registered models")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_pull = sub.add_parser("pull", help="download + checksum-verify a model")
    p_pull.add_argument("name")
    p_pull.set_defaults(func=cmd_pull)

    p_verify = sub.add_parser("verify", help="verify cached checksums")
    p_verify.add_argument("name", nargs="?")
    p_verify.set_defaults(func=cmd_verify)

    p_prune = sub.add_parser("prune", help="evict least-recently-used weights")
    p_prune.add_argument("--all", action="store_true", help="remove every cached artifact")
    p_prune.add_argument("--dry-run", action="store_true")
    p_prune.set_defaults(func=cmd_prune)

    sub.add_parser("stats", help="print arbiter stats as JSON").set_defaults(func=cmd_stats)
    sub.add_parser("doctor", help="print backend, ceiling, resident total, free disk").set_defaults(
        func=cmd_doctor
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
