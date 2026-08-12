"""Extraction quality harness: per-type precision/recall over labeled captures.

Every ``*.json`` corpus in ``backend/eval/extraction/`` is scored. The bundled
``seed_captures.json`` is the CI-safe synthetic seed; the human-owned
100-capture hand-labeled set is consumed automatically once it is dropped into
the same directory (see ``README.md`` there for the schema).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from app.memory.entities import extract_entities_from_text, normalize_entity_name
from app.memory.extraction import Extractor
from app.memory.llm_extractor import should_enrich
from app.models import Event

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval" / "extraction"
ANCHOR = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)

SCORED_TYPES = {"decision", "preference", "goal", "fact", "observation"}


def _corpus_files() -> list[Path]:
    return sorted(EVAL_DIR.glob("*.json"))


def _event(text: str) -> Event:
    return Event(
        source="eval",
        event_type="note",
        content={"text": text},
        occurred_at=ANCHOR,
        sha256="0" * 64,
    )


def _score_corpus(captures: list[dict], *, enrich: bool = False) -> dict:
    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)
    entity_tp = entity_fp = entity_fn = 0
    temporal_hits = temporal_total = 0

    extractor = Extractor()
    for capture in captures:
        event = _event(capture["text"])
        produced = {
            candidate.memory_type
            for candidate in extractor.extract(event)
            if candidate.memory_type in SCORED_TYPES
        }
        expected = set(capture["expected_memory_types"])
        if enrich and should_enrich(event):
            # Perfect, triaged oracle: an ideal LLM pass would add exactly the
            # labels a human gave for captures the rules did not clear.
            produced |= expected
        for memory_type in SCORED_TYPES:
            if memory_type in expected and memory_type in produced:
                tp[memory_type] += 1
            elif memory_type in expected:
                fn[memory_type] += 1
            elif memory_type in produced:
                fp[memory_type] += 1

        expected_entities = capture.get("expected_entities") or []
        produced_entities = {
            (ref.entity_type, normalize_entity_name(ref.name))
            for ref in extract_entities_from_text(capture["text"])
        }
        expected_keys = {
            (e["entity_type"], normalize_entity_name(e["name"]))
            for e in expected_entities
        }
        entity_tp += len(expected_keys & produced_entities)
        entity_fp += len(produced_entities - expected_keys)
        entity_fn += len(expected_keys - produced_entities)

        if capture.get("expected_temporal"):
            temporal_total += 1
            temporal_hits += any(
                candidate.payload.get("temporal")
                for candidate in extractor.extract(event)
            )

    rows = []
    for memory_type in sorted(SCORED_TYPES):
        precision = (
            tp[memory_type] / (tp[memory_type] + fp[memory_type])
            if (tp[memory_type] + fp[memory_type])
            else 1.0
        )
        recall = (
            tp[memory_type] / (tp[memory_type] + fn[memory_type])
            if (tp[memory_type] + fn[memory_type])
            else 1.0
        )
        rows.append(
            {
                "memory_type": memory_type,
                "tp": tp[memory_type],
                "fp": fp[memory_type],
                "fn": fn[memory_type],
                "precision": round(precision, 3),
                "recall": round(recall, 3),
            }
        )
    total_tp = sum(tp.values())
    total_fp = sum(fp.values())
    total_fn = sum(fn.values())
    return {
        "captures": len(captures),
        "rows": rows,
        "total_tp": total_tp,
        "total_fp": total_fp,
        "total_fn": total_fn,
        "overall_precision": round(
            total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 1.0, 3
        ),
        "overall_recall": round(
            total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 1.0, 3
        ),
        "entity_precision": round(
            entity_tp / (entity_tp + entity_fp) if (entity_tp + entity_fp) else 1.0, 3
        ),
        "entity_recall": round(
            entity_tp / (entity_tp + entity_fn) if (entity_tp + entity_fn) else 1.0, 3
        ),
        "entity_tp": entity_tp,
        "entity_fp": entity_fp,
        "entity_fn": entity_fn,
        "temporal_hits": temporal_hits,
        "temporal_total": temporal_total,
    }


def _print_table(name: str, score: dict) -> None:
    print(f"\nEXTRACTION P/R — {name} ({score['captures']} captures):")
    print(f"{'type':<14}{'tp':>4}{'fp':>4}{'fn':>4}{'precision':>11}{'recall':>9}")
    for row in score["rows"]:
        print(
            f"{row['memory_type']:<14}{row['tp']:>4}{row['fp']:>4}{row['fn']:>4}"
            f"{row['precision']:>11.3f}{row['recall']:>9.3f}"
        )
    print(
        f"{'OVERALL':<14}{score['total_tp']:>4}{score['total_fp']:>4}{score['total_fn']:>4}"
        f"{score['overall_precision']:>11.3f}{score['overall_recall']:>9.3f}"
    )
    print(
        f"entities precision={score['entity_precision']:.3f} "
        f"recall={score['entity_recall']:.3f} | "
        f"temporal hits={score['temporal_hits']}/{score['temporal_total']}"
    )


def _print_delta(name: str, before: dict, after: dict) -> None:
    print(f"\nDELTA — {name} (rule+LLM minus rule-only):")
    print(f"{'type':<14}{'precision delta':>16}{'recall delta':>13}")
    before_rows = {row["memory_type"]: row for row in before["rows"]}
    for row in after["rows"]:
        prior = before_rows[row["memory_type"]]
        print(
            f"{row['memory_type']:<14}"
            f"{row['precision'] - prior['precision']:>16.3f}"
            f"{row['recall'] - prior['recall']:>13.3f}"
        )
    print(
        f"{'OVERALL':<14}"
        f"{after['overall_precision'] - before['overall_precision']:>16.3f}"
        f"{after['overall_recall'] - before['overall_recall']:>13.3f}"
    )


def _combine(scores: list[dict]) -> dict:
    if not scores:
        raise AssertionError("no eval corpus files found")
    combined = {
        "captures": sum(s["captures"] for s in scores),
        "total_tp": sum(s["total_tp"] for s in scores),
        "total_fp": sum(s["total_fp"] for s in scores),
        "total_fn": sum(s["total_fn"] for s in scores),
        "entity_tp": sum(s["entity_tp"] for s in scores),
        "entity_fp": sum(s["entity_fp"] for s in scores),
        "entity_fn": sum(s["entity_fn"] for s in scores),
        "temporal_hits": sum(s["temporal_hits"] for s in scores),
        "temporal_total": sum(s["temporal_total"] for s in scores),
    }
    rows: dict[str, dict] = {}
    for s in scores:
        for row in s["rows"]:
            target = rows.setdefault(
                row["memory_type"],
                {"memory_type": row["memory_type"], "tp": 0, "fp": 0, "fn": 0},
            )
            target["tp"] += row["tp"]
            target["fp"] += row["fp"]
            target["fn"] += row["fn"]
    for row in rows.values():
        row["precision"] = round(
            row["tp"] / (row["tp"] + row["fp"]) if (row["tp"] + row["fp"]) else 1.0, 3
        )
        row["recall"] = round(
            row["tp"] / (row["tp"] + row["fn"]) if (row["tp"] + row["fn"]) else 1.0, 3
        )
    combined["rows"] = [rows[key] for key in sorted(rows)]
    total_tp = combined["total_tp"]
    total_fp = combined["total_fp"]
    total_fn = combined["total_fn"]
    combined["overall_precision"] = round(
        total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 1.0, 3
    )
    combined["overall_recall"] = round(
        total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 1.0, 3
    )
    entity_tp, entity_fp, entity_fn = (
        combined["entity_tp"],
        combined["entity_fp"],
        combined["entity_fn"],
    )
    combined["entity_precision"] = round(
        entity_tp / (entity_tp + entity_fp) if (entity_tp + entity_fp) else 1.0, 3
    )
    combined["entity_recall"] = round(
        entity_tp / (entity_tp + entity_fn) if (entity_tp + entity_fn) else 1.0, 3
    )
    return combined


def test_extraction_precision_recall_per_type() -> None:
    files = _corpus_files()
    assert files, "expected at least one eval corpus"
    scores = []
    llm_scores = []
    for path in files:
        with path.open(encoding="utf-8") as handle:
            corpus = json.load(handle)
        captures = corpus["captures"]
        assert len(captures) >= 1
        rule_score = _score_corpus(captures)
        llm_score = _score_corpus(captures, enrich=True)
        scores.append(rule_score)
        llm_scores.append(llm_score)
        _print_table(f"{path.stem} · rule-only", rule_score)
        _print_table(f"{path.stem} · rule+LLM (perfect oracle, triaged)", llm_score)
        _print_delta(path.stem, rule_score, llm_score)

    combined = _combine(scores)
    combined_llm = _combine(llm_scores)
    _print_table("COMBINED · rule-only", combined)
    _print_table("COMBINED · rule+LLM (perfect oracle, triaged)", combined_llm)
    _print_delta("COMBINED", combined, combined_llm)

    # Seed plus any real corpus the owner drops in must meet the acceptance
    # gates; with only the seed present this is the CI-safe floor.
    assert combined["overall_precision"] >= 0.85
    assert combined["overall_recall"] >= 0.75
    assert combined_llm["overall_precision"] >= combined["overall_precision"]
    assert combined_llm["overall_recall"] >= combined["overall_recall"]
    for row in combined["rows"]:
        if row["tp"] + row["fn"] >= 3:
            assert row["recall"] >= 0.7

    # Entity/temporal gates apply to the combined set.
    assert combined["entity_recall"] >= 0.75
    assert combined["temporal_hits"] == combined["temporal_total"]
