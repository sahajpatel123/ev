"""One-shot WhatsApp CDP workspace supervisor (Executor Plan §2.4 / Phase B).

Runs from launchd on a StartInterval (not a daemon): makes sure Evie's
dedicated headless Chrome is up, records the link state to a status file,
and exits. It never links, never sends, never raises a window, and never
touches the owner's everyday browser profile.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

STATUS_PATH = Path.home() / ".ev" / "whatsapp-cdp-supervisor.json"


async def run() -> int:
    from app.ev.messaging import whatsapp_cdp

    running = await whatsapp_cdp.ensure_running(bring_to_front=False)
    status = await whatsapp_cdp.status(refresh=True)
    payload = {
        "checked_at": datetime.now(UTC).isoformat(),
        "workspace_running": bool(running),
        "authenticated": bool(status.get("authenticated")),
        "diagnosis": str(status.get("diagnosis") or "unknown"),
        "setup_required": bool(status.get("setup_required")),
    }
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STATUS_PATH)
    print(json.dumps(payload))
    # Linked, QR-ready (owner ceremony pending), or loading are all healthy
    # supervisor outcomes. Anything else means the workspace itself is down.
    if payload["authenticated"] or status.get("diagnosis") in {
        "cdp_qr",
        "companion_evicted",
        "cdp_not_linked",
    }:
        return 0
    return 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
