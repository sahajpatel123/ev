"""Offline exemplar miner: proposals only, never writes exemplars.yaml.

Reads a results payload (from run.py), groups winning v2 replies by family
tag, and writes the top entries per tag to a timestamped proposal file for
human review. Refuses any output path that would touch exemplars.yaml.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PROPOSALS_DIR = HERE / "proposals"


def mine(results_path: Path, *, top_per_tag: int = 2) -> dict:
    """Pick top v2 replies per family tag from a results payload."""
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    by_tag: dict[str, list[dict]] = {}
    for item in payload.get("items", []):
        if item.get("hard_fails"):
            continue
        tag = str(item.get("family", "general"))
        by_tag.setdefault(tag, []).append(item)
    proposals = {}
    for tag in sorted(by_tag):
        ranked = sorted(
            by_tag[tag],
            key=lambda item: (item.get("verdict") == "v2",
                              item.get("ceiling_match", False)),
            reverse=True,
        )
        proposals[tag] = [
            {
                "scenario": item["id"],
                "verdict": item.get("verdict"),
                "roast": item.get("v2_roast"),
                "technique": item.get("v2_technique"),
                "held_out": item.get("held_out", False),
            }
            for item in ranked[:top_per_tag]
        ]
    return {
        "source": str(results_path),
        "mined_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": "PROPOSAL ONLY — human review required before any exemplar change.",
        "proposals": proposals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mine exemplar proposals (offline)")
    parser.add_argument("--results", required=True, help="results JSON from run.py")
    parser.add_argument("--out", default=None, help="proposal output path")
    parser.add_argument("--top-per-tag", type=int, default=2)
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else PROPOSALS_DIR / (
        f"exemplars_proposal_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.yaml"
    )
    if out.name == "exemplars.yaml" or "exemplars.yaml" in out.parts:
        raise SystemExit("refusing: miner never writes to exemplars.yaml")
    proposals = mine(Path(args.results), top_per_tag=args.top_per_tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(proposals, sort_keys=False), encoding="utf-8")
    print(f"wrote {out} ({len(proposals['proposals'])} tags)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
