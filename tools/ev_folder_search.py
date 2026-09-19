"""Search inside the EV folder.

Usage:
    python3 tools/ev_folder_search.py "pattern" [--root .] [--glob *.py]

Local-first helper: regex search across the EV repo while skipping
.git, node_modules, .venv, storage/data artifacts and binaries.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".DS_Store"}
SKIP_SUFFIXES = {".pyc", ".db", ".sqlite", ".wav", ".m4a", ".mp3", ".png", ".jpg"}


def iter_files(root: Path, glob: str):
    for p in root.rglob(glob or "*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIXES:
            continue
        # skip large binaries / lockfiles that drown results
        if p.name in {"uv.lock", "package-lock.json"}:
            continue
        yield p


def search(root: Path, pattern: str, glob: str, limit: int) -> int:
    rx = re.compile(pattern)
    hits = 0
    for path in iter_files(root, glob):
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                print(f"{path}:{lineno}:{line.strip()[:300]}")
                hits += 1
                if hits >= limit:
                    return hits
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Search inside the EV folder")
    ap.add_argument("pattern", help="regex to search for")
    ap.add_argument("--root", default=".", help="project-relative root (default: .)")
    ap.add_argument("--glob", default="*", help="glob filter e.g. '*.py'")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not root.exists():
        print(f"root not found: {root}", file=sys.stderr)
        return 2
    hits = search(root, args.pattern, args.glob, args.limit)
    print(f"{hits} hit(s) for {args.pattern!r} under {root}")
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
