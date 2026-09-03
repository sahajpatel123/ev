"""Walk an archive tree and write an aggregate catalog. No DB writes."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

from app.memory.life_archive.classify import SKIP_WALK_DIR_NAMES, CatalogRecord, classify_path
from app.memory.paths import atomic_write_json, ensure_tree
from app.utils.text import utcnow


def catalog_tree(root: Path) -> list[CatalogRecord]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"archive root not found: {root}")
    records: list[CatalogRecord] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name.lower() not in SKIP_WALK_DIR_NAMES and not name.startswith(".")
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if not path.is_file():
                continue
            records.append(classify_path(path, root=root))
    return records


def summarize(records: list[CatalogRecord]) -> dict[str, Any]:
    by_disposition: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    by_adapter: Counter[str] = Counter()
    by_origin: Counter[str] = Counter()
    bytes_by_disposition: Counter[str] = Counter()
    ingest_adapters: Counter[str] = Counter()
    index_adapters: Counter[str] = Counter()
    for row in records:
        by_disposition[row.disposition] += 1
        by_reason[f"{row.disposition}:{row.reason}"] += 1
        by_adapter[row.adapter] += 1
        by_origin[row.origin] += 1
        bytes_by_disposition[row.disposition] += row.size
        if row.disposition == "ingest":
            ingest_adapters[row.adapter] += 1
        elif row.disposition == "index":
            index_adapters[row.adapter] += 1
    return {
        "schema_version": 1,
        "generated_at": utcnow().isoformat(),
        "file_count": len(records),
        "by_disposition": dict(by_disposition),
        "bytes_by_disposition": dict(bytes_by_disposition),
        "by_origin": dict(by_origin),
        "by_adapter": dict(by_adapter),
        "by_reason": dict(by_reason.most_common()),
        "ingest_adapters": dict(ingest_adapters),
        "index_adapters": dict(index_adapters),
        "quarantine_count": by_disposition.get("quarantine", 0),
        "skip_count": by_disposition.get("skip", 0),
        "ingest_count": by_disposition.get("ingest", 0),
        "index_count": by_disposition.get("index", 0),
    }


def write_catalog(records: list[CatalogRecord], *, root: Path) -> dict[str, Any]:
    """Write aggregate summary + per-file manifest into the private memory tree."""
    memory = ensure_tree()
    catalog_dir = memory / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(records)
    summary["archive_root"] = str(root.expanduser().resolve())
    atomic_write_json(catalog_dir / "last-dry-run.summary.json", summary)
    manifest = catalog_dir / "last-dry-run.manifest.jsonl"
    if manifest.exists():
        manifest.unlink()
    # Paths only — never file contents.
    lines = [row.to_dict() for row in records]
    manifest.write_text(
        "".join(_json_line(item) for item in lines),
        encoding="utf-8",
    )
    summary["manifest_path"] = str(manifest)
    summary["summary_path"] = str(catalog_dir / "last-dry-run.summary.json")
    atomic_write_json(catalog_dir / "last-dry-run.summary.json", summary)
    return summary


def _json_line(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"
