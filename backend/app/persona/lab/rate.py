"""Blind A/B rater: 20 pairs, randomized and unlabelled.

  python -m app.persona.lab.rate [--rater NAME] [--pairs FILE] [--limit 20]

Stores choices + optional one-word reasons, prints the win rate, and
appends a section to personality/results/RESULTS.md (created if missing).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PAIRS = HERE / "pairs.json"


def load_pairs(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("pairs", payload) if isinstance(payload, dict) else payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Blind A/B rate v4 vs v2.1")
    parser.add_argument("--rater", default="owner")
    parser.add_argument("--pairs", default=str(DEFAULT_PAIRS))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    pairs = load_pairs(Path(args.pairs))[: args.limit]
    if not pairs:
        print("no pairs found", file=sys.stderr)
        return 2
    rng = random.Random(args.seed)
    order = list(range(len(pairs)))
    rng.shuffle(order)
    results: list[dict] = []
    print(f"blind rating: {len(pairs)} pairs. Reply A or B (optionally: 'A witty').",
          flush=True)
    ended_early = False
    for position, index in enumerate(order, 1):
        pair = pairs[index]
        v2_first = rng.random() < 0.5
        first, second = ("v2.1", "v4") if v2_first else ("v4", "v2.1")
        print(f"\n--- pair {position}/{len(pairs)} [{pair.get('id', index)}] ---")
        print(f"owner: {pair.get('owner_msg', '')}")
        print(f"A: {pair[first]}")
        print(f"B: {pair[second]}")
        pick, reason = "", ""
        while True:
            try:
                raw = input("pick> ").strip()
            except EOFError:
                print("\n[rating ended early]")
                ended_early = True
                break
            if not raw:
                continue
            parts = raw.split(None, 1)
            if parts[0].upper() in ("A", "B"):
                pick = parts[0].upper()
                reason = parts[1].strip() if len(parts) > 1 else ""
                break
            print("reply A or B")
        if ended_early:
            break
        winner = first if pick == "A" else second
        results.append({"pair": pair.get("id", index), "shown_first": first,
                        "pick": pick, "winner": winner, "reason": reason})
    v2 = sum(1 for item in results if item["winner"] == "v2.1")
    total = len(results)
    print(f"\nrated {total}: v2.1 {v2} ({v2 / total:.0%})" if total else "\nno ratings")
    repo = HERE.parent.parent.parent.parent
    out_dir = repo / "personality" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"ratings_{args.rater}_{stamp}.json"
    out_path.write_text(json.dumps({"rater": args.rater, "results": results,
                                    "v2_wins": v2, "total": total}, indent=2),
                        encoding="utf-8")
    results_md = out_dir / "RESULTS.md"
    section = (f"\n## Blind ratings ({args.rater}, {stamp})\n\n"
               f"v2.1 wins {v2}/{total}"
               + (f" ({v2 / total:.0%})" if total else "") + ".\n"
               + "".join(f"- {item['pair']}: {item['winner']}"
                         + (f" ({item['reason']})" if item["reason"] else "")
                         + "\n" for item in results))
    if results_md.exists():
        with open(results_md, "a", encoding="utf-8") as handle:
            handle.write(section)
    else:
        results_md.write_text("# RESULTS (ratings preface — full report lands here)\n" + section,
                              encoding="utf-8")
    print(f"saved {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
