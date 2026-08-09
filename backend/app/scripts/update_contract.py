"""Regenerate the locked v1 contract manifest from the live OpenAPI spec.

Run after adding, renaming, or removing a v1 endpoint:

    uv run python -m app.scripts.update_contract

The gate in `app/scripts/eval_gates.py` fails when a live v1 route is not in
the manifest or a locked route disappears, so this script keeps the contract
deliberate and machine-checkable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.main import app

CONTRACT_MANIFEST = Path(__file__).resolve().parents[2] / "eval" / "contract_v1.json"


def regenerate() -> dict:
    spec = app.openapi()
    paths: dict[str, list[str]] = {}
    for route, methods in spec.get("paths", {}).items():
        if not route.startswith("/v1"):
            continue
        paths[route] = sorted(
            method.lower()
            for method in methods
            if method.lower() in ("get", "post", "put", "patch", "delete")
        )
    manifest = json.loads(CONTRACT_MANIFEST.read_text())
    manifest["paths"] = dict(sorted(paths.items()))
    manifest["locked_at"] = datetime.now(UTC).isoformat()
    CONTRACT_MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    manifest = regenerate()
    print(f"Updated {CONTRACT_MANIFEST} with {len(manifest['paths'])} v1 paths.")
