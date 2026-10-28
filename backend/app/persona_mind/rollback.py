"""Rollback: restore any earlier config version in one command.

Usage: python -m app.persona_mind.rollback --to <version>
       python -m app.persona_mind.rollback --list
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    from app.persona_mind import tune

    parser = argparse.ArgumentParser(description="Roll back mind config.")
    parser.add_argument("--to", type=int, default=None)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list or args.to is None:
        active = tune.active()["version"]
        for entry in tune.history():
            mark = "*" if entry["version"] == active else " "
            print(f"{mark} v{entry['version']} {entry['created_at']} {entry['diff']}")
        return 0
    if tune.rollback_to(args.to):
        print(f"active config is now v{args.to}")
        return 0
    print(f"unknown config version: {args.to}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
