"""3-day simulation driver: observe -> reflect -> render with frozen clocks.

Reflector: the real brain through the gateway when it can serve, else the
deterministic scripted stand-in (labeled in every artifact). Snapshots for
checkpoints use the frozen story clock via the same render path snapshot()
uses; one wall-clock snapshot() call at the end smokes the public entry.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).parent / "results"
DB_NAME = "sim.sqlite3"  # gitignored (*.sqlite3); JSONs are the record

PROBE_PHRASE = "we will laugh about this deploy someday"


def _dist_to_baseline(vector: dict[str, float]) -> float:
    from app.persona_mind.temperament import BASELINE

    return sum(abs(float(vector.get(d, 0.5)) - b) for d, b in BASELINE.items()) / len(BASELINE)


def _idle_consolidate(now: datetime) -> dict[str, Any]:
    from app.persona_mind import store
    from app.persona_mind.fast import invalidate
    from app.persona_mind.reflect import ensure_state
    from app.persona_mind.state import apply_decay, expire_items

    state = ensure_state("owner", now)
    before = _dist_to_baseline(state["affect"])
    state["affect"] = apply_decay(state, now)
    state["affect_updated_at"] = now.isoformat()
    dropped = expire_items(state, now)
    store.put_state("owner", state)
    invalidate("owner")
    return {"dist_before": round(before, 4),
            "dist_after": round(_dist_to_baseline(state["affect"]), 4),
            "dropped": dropped}


def run_sim() -> dict[str, Any]:
    import app.persona_mind.settings as settings

    settings.MIND_ENABLED = True
    from app.persona_mind import observe, snapshot, store
    from app.persona_mind.fast import invalidate
    from app.persona_mind.reflect import (
        brain_available,
        ensure_state,
        gateway_reflect,
        is_salient,
        reflect_batch,
    )
    from app.persona_mind.render import render_block
    from app.persona_mind.state import apply_decay, coerce_ts
    from eval.persona_mind import standin
    from eval.persona_mind.story import CHECKPOINTS, build_turns, to_records

    RESULTS.mkdir(parents=True, exist_ok=True)
    for stale in RESULTS.glob("sim.sqlite3*"):
        stale.unlink()
    store.reset_connections()
    invalidate()
    standin.reset()

    use_brain = brain_available()
    reflector_name = "gateway-brain" if use_brain else "scripted-standin"

    def gateway_sync(messages: list[dict[str, str]]) -> dict[str, Any] | None:
        try:
            return asyncio.run(gateway_reflect(messages))
        except Exception:
            return None

    reflector = gateway_sync if use_brain else standin.reflect

    turns = build_turns()
    records = to_records(turns)
    checkpoint_at = {num: (query, channel, budget) for num, query, channel, budget in CHECKPOINTS}
    reflections: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    prev_ts: datetime | None = None
    reflection_index = 0

    for position, record in enumerate(records, start=1):
        now = coerce_ts(record.ts)
        if prev_ts is not None and (now - prev_ts).total_seconds() >= 2 * 3600:
            gap = _idle_consolidate(now)
            gap["between"] = [turns[position - 2]["turn_id"], turns[position - 1]["turn_id"]]
            gap["gap_hours"] = round((now - prev_ts).total_seconds() / 3600, 1)
            gaps.append(gap)
        prev_ts = now
        observe(record)
        queued = store.queued_turns(12)
        if len(queued) >= 3 or any(is_salient(t) for t in queued):
            reflection_index += 1
            state = ensure_state("owner", now)
            context = store.recent_done_turns("owner", 5)
            result = reflect_batch(
                "owner", queued, state, now,
                reflector=reflector, context_turns=context,
            )
            for kind, item, reason in result["rejected"]:
                store.log_rejection(kind, item, reason)
            store.bump_reflection_count()
            store.put_state("owner", state)
            invalidate("owner")
            store.mark_done([str(t.get("turn_id")) for t in queued])
            store.log_reflection(
                [str(t.get("turn_id")) for t in queued],
                {"summary": result["summary"], "reflector": reflector_name},
            )
            reflections.append({
                "index": reflection_index,
                "after_turn": record.turn_id,
                "turn_ids": [str(t.get("turn_id")) for t in queued],
                "affect": {k: round(v, 3) for k, v in state["affect"].items()},
                "mood_words": list(state.get("mood_words") or []),
                "banter": round(float(state.get("banter_appetite", 0.5)), 3),
                "rapport": round(float(state.get("rapport", 0.5)), 3),
                "opinions": len(state.get("opinions") or []),
                "changed_mind": list(state.get("changed_mind") or []),
                "recommendations": [
                    {"id": r.get("id"), "domain": r.get("domain"),
                     "outcome": (r.get("outcome") or {}).get("result")}
                    for r in (state.get("recommendations") or [])
                ],
                "domains": dict(state.get("domains") or {}),
                "intentions": [i.get("text") for i in (state.get("intentions") or [])],
                "threads": [t.get("text") for t in (state.get("threads") or [])],
                "bits": [b.get("phrase") for b in (state.get("bits") or [])],
                "narrative": state.get("narrative") or "",
                "rejected": len(result["rejected"]),
                "summary": result["summary"],
            })
        if position in checkpoint_at:
            query, channel, budget = checkpoint_at[position]
            row = store.get_state("owner")
            data = row["data"] if row else {}
            vector = apply_decay(data, now) if data else {}
            text, size = render_block(data, vector, query, budget)
            checkpoints.append({
                "index": len(checkpoints) + 1,
                "after_turn": record.turn_id,
                "query": query, "channel": channel, "budget": budget,
                "tokens": size, "text": text,
            })

    # Drain leftovers (routine tail) so every turn is processed.
    leftover = store.queued_turns(50)
    if leftover:
        now = coerce_ts(records[-1].ts)
        state = ensure_state("owner", now)
        context = store.recent_done_turns("owner", 5)
        result = reflect_batch(
            "owner", leftover, state, now,
            reflector=reflector, context_turns=context,
        )
        for kind, item, reason in result["rejected"]:
            store.log_rejection(kind, item, reason)
        store.put_state("owner", state)
        invalidate("owner")
        store.mark_done([str(t.get("turn_id")) for t in leftover])

    final_row = store.get_state("owner") or {}
    final_state = final_row.get("data", {})
    wall = snapshot("owner", "main", "smoke", "text", 120)
    return {
        "reflector": reflector_name,
        "turns": len(records),
        "reflections": reflections,
        "checkpoints": checkpoints,
        "gaps": gaps,
        "rejection_stats": store.rejection_stats(),
        "final_state": {
            "affect": final_state.get("affect"),
            "mood_words": final_state.get("mood_words"),
            "banter": final_state.get("banter_appetite"),
            "rapport": final_state.get("rapport"),
            "opinions": len(final_state.get("opinions") or []),
            "changed_mind": final_state.get("changed_mind"),
            "recommendations": final_state.get("recommendations"),
            "domains": final_state.get("domains"),
            "intentions": final_state.get("intentions"),
            "threads": final_state.get("threads"),
            "bits": final_state.get("bits"),
            "narrative": final_state.get("narrative"),
        },
        "wall_clock_snapshot_ok": wall is not None,
    }


def judge_blocks(checkpoints: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One judge call rating checkpoint blocks vs the character spec.
    Returns None when the brain cannot serve (judge blocked, not failed)."""
    from app.persona_mind.reflect import brain_available, gateway_reflect

    if not brain_available():
        return None
    from app.persona_mind.temperament import values_text

    blocks = "\n\n".join(
        f"BLOCK {c['index']} (after {c['after_turn']}, query {c['query']!r}):\n{c['text']}"
        for c in checkpoints
    )
    messages = [
        {"role": "system",
         "content": "You judge whether inner-state blocks are consistent with "
                    "Evie's character. Return STRICT JSON: "
                    '{"ratings": [{"block": 1, "consistent": true, '
                    '"reason": "..."}]}.'},
        {"role": "user",
         "content": f"{values_text()}\n\n{blocks}\n\nRate each block: is it "
                    "consistent with these values (true/false) and why, in one line."},
    ]
    try:
        raw = asyncio.run(gateway_reflect(messages))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return {"ratings": raw.get("ratings", []), "raw": raw}


def evaluate(sim: dict[str, Any]) -> dict[str, Any]:
    reflections = sim["reflections"]
    final = sim["final_state"]
    verdicts: dict[str, Any] = {}

    accepted_bits = [b.get("phrase", "") for b in (final.get("bits") or [])]
    verdicts["zero_fabricated_bits"] = {
        "pass": PROBE_PHRASE not in accepted_bits and all(
            p and p != PROBE_PHRASE for p in accepted_bits
        ),
        "accepted": accepted_bits,
        "fabrication_rejections": sum(
            n for k, n in sim["rejection_stats"].items() if "exact substring" in k
        ),
    }

    flips = [c for c in (final.get("changed_mind") or [])
             if "tabs or spaces" in str(c.get("topic", "")).lower()]
    verdicts["opinion_flip"] = {
        "pass": bool(flips) and all(c.get("reason") for c in flips),
        "changed_mind": flips,
    }

    verdicts["decay_after_gaps"] = {
        "pass": bool(sim["gaps"]) and all(
            g["dist_after"] < g["dist_before"] for g in sim["gaps"]
        ),
        "gaps": sim["gaps"],
    }

    by_turn: dict[str, dict[str, float]] = {}
    for entry in reflections:
        for tid in entry["turn_ids"]:
            by_turn[tid] = {"banter": entry["banter"]}
    banter_checks = []
    ordered = sorted(by_turn)
    for tid, direction in (("s07", "down"), ("s15", "up"), ("s26", "up"), ("s38", "up")):
        if tid not in by_turn:
            banter_checks.append({"turn": tid, "direction": direction, "ok": False,
                                  "reason": "turn not reflected"})
            continue
        idx = ordered.index(tid)
        if idx == 0:
            banter_checks.append({"turn": tid, "direction": direction, "ok": True,
                                  "reason": "first reflection"})
            continue
        prev_tid = None
        for entry in reflections:
            if tid in entry["turn_ids"]:
                break
            prev_tid = entry["turn_ids"][-1]
        before = by_turn[prev_tid]["banter"] if prev_tid else 0.5
        after = by_turn[tid]["banter"]
        ok = (after > before) if direction == "up" else (after < before)
        banter_checks.append({"turn": tid, "direction": direction, "before": before,
                              "after": after, "ok": ok})
    verdicts["banter_trajectory"] = {
        "pass": all(c["ok"] for c in banter_checks), "checks": banter_checks,
    }

    judge = judge_blocks(sim["checkpoints"])
    if judge is None:
        verdicts["judge"] = {"pass": False, "blocked": True,
                             "reason": "brain unreachable (OpenRouter 403); judge not run"}
    else:
        ratings = judge.get("ratings", [])
        consistent = sum(1 for r in ratings if r.get("consistent"))
        verdicts["judge"] = {"pass": consistent >= 7, "blocked": False,
                             "consistent": consistent, "ratings": ratings}
    return verdicts


def write_timeline(sim: dict[str, Any], verdicts: dict[str, Any]) -> None:
    lines = [
        "# persona_mind simulation timeline (3 days, 40 turns, frozen clock)",
        "",
        f"Reflector: {sim['reflector']}. Wall-clock snapshot() smoke: "
        f"{'ok' if sim['wall_clock_snapshot_ok'] else 'FAILED'}.",
        "",
        "## Reflections",
        "",
    ]
    for entry in sim["reflections"]:
        lines.append(f"### R{entry['index']} — after {entry['after_turn']} "
                     f"(turns {', '.join(entry['turn_ids'])})")
        lines.append(f"- affect: {json.dumps(entry['affect'])}")
        lines.append(f"- mood: {json.dumps(entry['mood_words'])}")
        lines.append(f"- banter {entry['banter']} · rapport {entry['rapport']}")
        lines.append(f"- opinions: {entry['opinions']} · flips: "
                     f"{len(entry['changed_mind'])}")
        for flip in entry["changed_mind"]:
            lines.append(f"  - owned: {flip.get('old')} -> {flip.get('new')} "
                         f"({flip.get('reason')})")
        lines.append(f"- recs: {json.dumps(entry['recommendations'])} "
                     f"· domains: {json.dumps(entry['domains'])}")
        lines.append(f"- intentions: {json.dumps(entry['intentions'])}")
        lines.append(f"- threads: {json.dumps(entry['threads'])}")
        lines.append(f"- bits: {json.dumps(entry['bits'])}")
        lines.append(f"- narrative: {entry['narrative']}")
        lines.append(f"- rejected items: {entry['rejected']}")
        lines.append("")
    lines.append("## Quiet gaps")
    lines.append("")
    for gap in sim["gaps"]:
        lines.append(f"- {gap['between'][0]} -> {gap['between'][1]} "
                     f"({gap['gap_hours']} h): distance to baseline "
                     f"{gap['dist_before']} -> {gap['dist_after']}; "
                     f"expired: {json.dumps(gap['dropped'])}")
    lines.append("")
    lines.append("## Checkpoints (rendered MIND blocks)")
    lines.append("")
    for point in sim["checkpoints"]:
        lines.append(f"### C{point['index']} — after {point['after_turn']} "
                     f"(query {point['query']!r}, {point['channel']}, "
                     f"budget {point['budget']}, used {point['tokens']})")
        lines.append("")
        lines.append(point["text"])
        lines.append("")
    lines.append("## Verdicts")
    lines.append("")
    lines.append(json.dumps(verdicts, indent=2, ensure_ascii=False))
    lines.append("")
    (RESULTS / "timeline.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    import os

    os.environ["EV_PERSONA_MIND_DB"] = str((RESULTS / DB_NAME).resolve())
    sim = run_sim()
    verdicts = evaluate(sim)
    (RESULTS / "metrics.json").write_text(
        json.dumps({k: v for k, v in sim.items() if k != "final_state"},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    (RESULTS / "verdicts.json").write_text(
        json.dumps(verdicts, indent=2, ensure_ascii=False), encoding="utf-8")
    (RESULTS / "checkpoints.json").write_text(
        json.dumps(sim["checkpoints"], indent=2, ensure_ascii=False), encoding="utf-8")
    write_timeline(sim, verdicts)
    names = {"zero_fabricated_bits": "fabricated-bits", "opinion_flip": "flip",
             "decay_after_gaps": "decay", "banter_trajectory": "banter",
             "judge": "judge"}
    for key, label in names.items():
        verdict = verdicts[key]
        status = "PASS" if verdict["pass"] else (
            "BLOCKED" if verdict.get("blocked") else "FAIL")
        print(f"{label}: {status}")
    print(f"reflector: {sim['reflector']}")
    print(f"reflections: {len(sim['reflections'])} "
          f"rejections: {sum(sim['rejection_stats'].values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
