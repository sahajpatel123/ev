"""HUD surface learner — when to open glass, and which lookout.

This is real training in the sense EV already trains: labeled examples +
owner ratings → a versioned calibration that the planner applies on every
turn. It is **not** DeepSeek weight training (that API has no adapter).
The same gold corpus is exported as SFT/tool records so a future local
model can be fine-tuned on the same data.

Smoke: ``evaluate_planner`` scores the live planner against
``eval/hud/surface_corpus.json``. Calibration writes
``storage_root/training/surface_calibration.json``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.ev.lookout import plan_surfaces
from app.utils.text import utcnow

CORPUS_PATH = (
    Path(__file__).resolve().parents[2] / "eval" / "hud" / "surface_corpus.json"
)
SHIPPED_CALIBRATION_PATH = (
    Path(__file__).resolve().parents[2] / "eval" / "hud" / "surface_calibration.json"
)


def _training_dir() -> Path:
    root = Path(settings.storage_root).expanduser()
    path = root / "training"
    path.mkdir(parents=True, exist_ok=True)
    return path


def calibration_path() -> Path:
    return _training_dir() / "surface_calibration.json"


def ratings_path() -> Path:
    return _training_dir() / "surface_ratings.jsonl"


def load_corpus() -> list[dict[str, Any]]:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    return list(payload.get("examples") or [])


REQUIRED_MECHANICS = {
    "quiet": ("stay quiet vs open glass — E.V. less intrusive than Karen",),
    "explicit_show": ("request-or-needed situational HUD — explicit show",),
    "needed": ("request-or-needed situational HUD — needed/emergency",),
    "watch": ("Baby Monitor watch-without-command",),
    "vitals": ("body-scan / vitals lookout",),
    "full_hud": ("multiple sizes/time-types/lookouts — full HUD stack",),
    "diagnostics": ("JARVIS diagnostics / suit-system status",),
    "briefing": ("tactical briefing",),
    "navigation": ("navigation / route canvas",),
    "refuse": ("weapons / city-scale surveillance / hacking stay out",),
}


def corpus_inventory() -> dict[str, Any]:
    """Count gold examples per defining mechanic. Used by smoke and tests."""

    examples = load_corpus()
    by_mechanic: dict[str, list[str]] = {key: [] for key in REQUIRED_MECHANICS}
    extra: dict[str, list[str]] = {}
    for example in examples:
        mechanic = str(example.get("mechanic") or "unlabeled")
        ident = str(example.get("id") or "")
        if mechanic in by_mechanic:
            by_mechanic[mechanic].append(ident)
        else:
            extra.setdefault(mechanic, []).append(ident)
    missing = [key for key, ids in by_mechanic.items() if not ids]
    thin = [key for key, ids in by_mechanic.items() if len(ids) < 3]
    messages_by_mechanic: dict[str, list[str]] = {key: [] for key in REQUIRED_MECHANICS}
    for example in examples:
        mechanic = str(example.get("mechanic") or "")
        if mechanic in messages_by_mechanic:
            messages_by_mechanic[mechanic].append(str(example.get("message") or ""))
    duplicate_thin = [
        key
        for key, msgs in messages_by_mechanic.items()
        if len(set(msgs)) < 3
    ]
    uncited = [
        str(example.get("id") or "")
        for example in examples
        if not str(example.get("source_url") or "").startswith("http")
    ]
    held_out = [str(ex.get("id") or "") for ex in examples if ex.get("split") == "held_out"]
    return {
        "total": len(examples),
        "by_mechanic": by_mechanic,
        "extra": extra,
        "required": list(REQUIRED_MECHANICS),
        "missing": missing,
        "thin": thin,
        "duplicate_thin": duplicate_thin,
        "uncited": uncited,
        "held_out": held_out,
        "complete": not missing and not uncited and not thin and not duplicate_thin,
    }


def _merge_calibration(data: dict[str, Any]) -> dict[str, Any]:
    base = default_calibration()
    for key, value in data.items():
        if key in base or key in {"version", "updated_at", "evidence", "prefer_time"}:
            base[key] = value
    return base


def load_calibration() -> dict[str, Any]:
    """Live policy: storage_root override, else the repo-shipped fit."""

    for path in (calibration_path(), SHIPPED_CALIBRATION_PATH):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return _merge_calibration(data)
    return default_calibration()


def default_calibration() -> dict[str, Any]:
    return {
        "version": 0,
        "urgency_threshold": 0.75,
        "max_windows": 4,
        "boost_kinds": {},
        "suppress_kinds": {},
        "prefer_size": {},
        "prefer_time": {},
        "evidence": {"corpus": 0, "ratings": 0, "useful": 0, "too_much": 0},
        "updated_at": None,
    }


def fit_policy_from_corpus() -> dict[str, Any]:
    """Derive prefer_size / prefer_time / boosts from gold labels. No web fetch."""

    prefer_size: dict[str, str] = {}
    prefer_time: dict[str, str] = {}
    boost: dict[str, float] = {}
    for example in load_corpus():
        if example.get("split") == "held_out":
            continue
        kinds = list(example.get("kinds") or [])
        sizes = list(example.get("sizes") or [])
        times = list(example.get("time_types") or [])
        for index, kind in enumerate(kinds):
            boost[kind] = round(min(1.0, float(boost.get(kind, 0.0)) + 0.1), 3)
            if index < len(sizes):
                prefer_size[kind] = str(sizes[index])
            if index < len(times):
                prefer_time[kind] = str(times[index])
    return {
        "prefer_size": prefer_size,
        "prefer_time": prefer_time,
        "boost_kinds": boost,
        "corpus": sum(1 for row in load_corpus() if row.get("split") != "held_out"),
        "held_out": sum(1 for row in load_corpus() if row.get("split") == "held_out"),
    }


def record_rating(
    *,
    kind: str,
    useful: bool,
    message: str | None = None,
    preferred_kind: str | None = None,
    window_id: str | None = None,
) -> dict[str, Any]:
    """Append one owner rating. Next calibrate() folds it into policy."""

    row = {
        "at": utcnow().isoformat(),
        "kind": kind,
        "useful": bool(useful),
        "message": (message or "")[:500],
        "preferred_kind": preferred_kind,
        "window_id": window_id,
    }
    path = ratings_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load_ratings() -> list[dict[str, Any]]:
    path = ratings_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def evaluate_planner(
    calibration: dict[str, Any] | None = None,
    *,
    split: str | None = None,
) -> dict[str, Any]:
    """Smoke score: gold corpus vs live planner. No fabricated confidence."""

    examples = load_corpus()
    if split:
        examples = [row for row in examples if row.get("split") == split]
    hits = 0
    kind_hits = 0
    kind_total = 0
    misses: list[dict[str, Any]] = []
    from app.ev.interaction import build_strategy

    policy = load_calibration() if calibration is None else calibration
    for example in examples:
        message = str(example["message"])
        plan = plan_surfaces(
            message,
            strategy=build_strategy(message),
            calibration=policy,
        )
        open_ok = bool(plan.open) == bool(example["open"])
        gold_kinds = set(example.get("kinds") or [])
        got_kinds = {window.kind for window in plan.windows}
        got_times = {window.time_type for window in plan.windows}
        got_sizes = {window.size for window in plan.windows}
        if gold_kinds:
            kind_total += 1
            kinds_ok = gold_kinds.issubset(got_kinds)
            if kinds_ok:
                kind_hits += 1
        else:
            kinds_ok = not got_kinds if not example["open"] else True
        gold_times = set(example.get("time_types") or [])
        gold_sizes = set(example.get("sizes") or [])
        times_ok = gold_times.issubset(got_times) if gold_times else True
        sizes_ok = gold_sizes.issubset(got_sizes) if gold_sizes else True
        if open_ok and kinds_ok and times_ok and sizes_ok:
            hits += 1
        else:
            misses.append(
                {
                    "id": example.get("id"),
                    "mechanic": example.get("mechanic"),
                    "message": example["message"],
                    "expected_open": example["open"],
                    "got_open": plan.open,
                    "expected_kinds": sorted(gold_kinds),
                    "got_kinds": sorted(got_kinds),
                    "expected_time_types": sorted(gold_times),
                    "got_time_types": sorted(got_times),
                    "expected_sizes": sorted(gold_sizes),
                    "got_sizes": sorted(got_sizes),
                }
            )
    total = len(examples) or 1
    return {
        "total": len(examples),
        "hits": hits,
        "accuracy": round(hits / total, 3),
        "kind_hits": kind_hits,
        "kind_total": kind_total,
        "kind_recall": round(kind_hits / kind_total, 3) if kind_total else 1.0,
        "misses": misses,
        "passed": hits == len(examples),
        "split": split,
    }


def calibrate(*, actor: str = "master") -> dict[str, Any]:
    """Fold gold corpus + owner ratings into the live surface policy."""

    current = load_calibration()
    fitted = fit_policy_from_corpus()
    ratings = load_ratings()
    boost: dict[str, float] = dict(fitted.get("boost_kinds") or current.get("boost_kinds") or {})
    suppress: dict[str, float] = dict(current.get("suppress_kinds") or {})
    prefer_size = dict(fitted.get("prefer_size") or current.get("prefer_size") or {})
    prefer_time = dict(fitted.get("prefer_time") or current.get("prefer_time") or {})
    useful = 0
    too_much = 0
    for row in ratings:
        kind = str(row.get("kind") or "").strip()
        if not kind:
            continue
        if row.get("useful") is True:
            useful += 1
            boost[kind] = round(min(1.0, float(boost.get(kind, 0.0)) + 0.15), 3)
            suppress.pop(kind, None)
            preferred = row.get("preferred_kind")
            if preferred:
                boost[str(preferred)] = round(min(1.0, float(boost.get(preferred, 0.0)) + 0.1), 3)
        elif row.get("useful") is False:
            too_much += 1
            suppress[kind] = round(min(1.0, float(suppress.get(kind, 0.0)) + 0.2), 3)

    smoke = evaluate_planner(current)
    urgency = float(current.get("urgency_threshold") or 0.75)
    if smoke["accuracy"] < 1.0 and any(
        miss["got_open"] and not miss["expected_open"] for miss in smoke["misses"]
    ):
        urgency = min(0.95, urgency + 0.05)
    if too_much > useful and useful + too_much >= 3:
        urgency = min(0.95, urgency + 0.05)
    if useful >= 3 and too_much == 0:
        urgency = max(0.55, urgency - 0.05)

    payload = {
        "version": int(current.get("version") or 0) + 1,
        "urgency_threshold": round(urgency, 3),
        "max_windows": int(current.get("max_windows") or 4),
        "boost_kinds": boost,
        "suppress_kinds": suppress,
        "prefer_size": prefer_size,
        "prefer_time": prefer_time,
        "evidence": {
            "corpus": len(load_corpus()),
            "ratings": len(ratings),
            "useful": useful,
            "too_much": too_much,
            "smoke_accuracy": smoke["accuracy"],
            "actor": actor,
            "fitted_from": "eval/hud/surface_corpus.json",
        },
        "updated_at": datetime.now(UTC).isoformat(),
    }
    calibration_path().write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    smoke_after = evaluate_planner(payload)
    payload["smoke"] = smoke_after
    return payload


def sft_records() -> list[dict[str, Any]]:
    """Instruction/response pairs for a future local adapter (and corpus export)."""

    records = []
    for example in load_corpus():
        kinds = example.get("kinds") or []
        if example["open"]:
            response = (
                "Open HUD windows: "
                + ", ".join(kinds or ["card"])
                + f". Rationale: {example.get('why')}"
            )
        else:
            response = f"Do not open a window. {example.get('why')}"
        records.append(
            {
                "kind": "surface",
                "instruction": example["message"],
                "response": response,
                "signals": {
                    "open": example["open"],
                    "kinds": kinds,
                    "source": f"surface_corpus:{example.get('id')}",
                },
            }
        )
    return records
