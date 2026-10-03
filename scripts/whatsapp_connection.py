"""Manage Evie's background WhatsApp connection without opening a window.

Run with backend/.venv/bin/python scripts/whatsapp_connection.py [--connect]
[--qr /private/tmp/ev-whatsapp-link.png]. Linking images stay local, outside
model conversations. This script never sends a message.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


async def run(*, connect: bool, qr_path: Path | None) -> int:
    from app.ev.messaging import whatsapp_cdp, whatsapp_local

    if connect:
        await whatsapp_cdp.setup(include_qr=False)
    state = await whatsapp_cdp.status()
    local = await whatsapp_local.status()
    linked = bool(state.get("authenticated"))
    state.update(read_available=linked or bool(local.get("read_available")),
                 draft_available=linked or bool(local.get("draft_available")),
                 send_available=linked,
                 read_source="cdp" if linked else "desktop_cache" if local.get("read_available") else None)
    fields = ("authenticated", "diagnosis", "backing", "background", "headless", "qr_required", "setup_required", "running", "read_available", "draft_available", "send_available", "read_source")
    print(json.dumps({key: state[key] for key in fields if key in state}))
    if qr_path:
        png = await whatsapp_cdp.link_qr()
        if not png:
            print("No linking QR is available. Check the connection status.", file=sys.stderr)
            return 1
        # Do not overwrite an existing QR or follow a symlink into another file.
        fd = os.open(qr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(png)
        print(f"Owner linking QR saved locally: {qr_path}")
    return 0 if state.get("read_available") or state.get("send_available") or qr_path else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true", help="Start the dedicated background browser")
    parser.add_argument("--qr", type=Path, help="Save a private local QR image for owner linking")
    args = parser.parse_args()
    # Read only connection options; no model or owner credentials are needed.
    from dotenv import dotenv_values

    root = Path(__file__).resolve().parents[1]
    for source in (root / ".env", root / "backend/.env"):
        values = dotenv_values(source)
        for key in ("EV_WHATSAPP_CDP_PORT", "EV_WHATSAPP_CDP_PROFILE", "EV_WHATSAPP_LOCAL_DB"):
            if values.get(key) and key not in os.environ:
                os.environ[key] = str(values[key])
    return asyncio.run(run(connect=args.connect, qr_path=args.qr))


if __name__ == "__main__":
    raise SystemExit(main())
