"""Owner Home Station CLI: list, pair, revoke, rename Evie devices. No credentials printed."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _base() -> str:
    return os.environ.get("EV_E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _master() -> str:
    key = os.environ.get("EV_MASTER_KEY") or os.environ.get("EV_E2E_MASTER_KEY")
    if not key:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("EV_MASTER_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"')
                    break
    if not key:
        raise SystemExit("EV_MASTER_KEY is not set")
    return key


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        _base() + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_master()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:400]
        raise SystemExit(f"{exc.code} {path}: {body}") from exc
    except OSError as exc:
        raise SystemExit(f"Home Station API unreachable at {_base()}: {exc}") from exc


def cmd_list(_args: argparse.Namespace) -> int:
    body = _request("GET", "/v1/device-gateway/devices")
    print("Evie Devices")
    for row in body.get("devices") or []:
        print(
            f"  {row.get('display_name')}  role={row.get('role')}  "
            f"scope={row.get('memory_scope')}  trust={row.get('trust_state')}  "
            f"last_seen={row.get('last_seen')}  id={str(row.get('device_id') or '')[:8]}"
        )
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    role = "primary_companion" if args.command == "pair-primary" else "secondary_companion"
    name = args.name or ("Primary iPhone" if role.startswith("primary") else "Secondary iPhone")
    body = _request("POST", "/v1/device-gateway/pairing-tokens", {"role": role, "display_name": name})
    print(f"ROLE: {body.get('role')}")
    print("MEMORY: sandbox")
    print(f"PAIRING CODE: {body.get('pairing_token')}")
    print(f"EXPIRES: {body.get('expires_at')}")
    print("Enter this one-time code in the Evie PWA. Do not reuse it.")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    body = _request("POST", "/v1/device-gateway/admin/revoke", {"device_id": args.device_id, "reason": args.reason})
    print("revoked", (body.get("device") or {}).get("display_name"))
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    body = _request(
        "POST",
        "/v1/device-gateway/admin/rename",
        {"device_id": args.device_id, "display_name": args.name},
    )
    print("renamed", (body.get("device") or {}).get("display_name"))
    return 0


def cmd_home(args: argparse.Namespace) -> int:
    body = _request("POST", "/v1/device-gateway/admin/mark-home-station", {"device_id": args.device_id})
    device = body.get("device") or {}
    print("HOME STATION:", device.get("display_name"), "scope=", device.get("memory_scope"))
    return 0


def cmd_clear(_args: argparse.Namespace) -> int:
    body = _request("POST", "/v1/device-gateway/admin/sandbox/clear", {})
    print(f"cleared {body.get('deleted')} sandbox facts; Memory OS untouched={body.get('memory_os_untouched')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evie device admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    p = sub.add_parser("pair-primary")
    p.add_argument("--name", default="Primary iPhone")
    s = sub.add_parser("pair-secondary")
    s.add_argument("--name", default="Secondary iPhone")
    r = sub.add_parser("revoke")
    r.add_argument("device_id")
    r.add_argument("--reason", default="owner_revoked")
    n = sub.add_parser("rename")
    n.add_argument("device_id")
    n.add_argument("name")
    h = sub.add_parser("mark-home-station")
    h.add_argument("device_id")
    sub.add_parser("clear-sandbox")
    args = parser.parse_args()
    if args.command == "list":
        return cmd_list(args)
    if args.command in {"pair-primary", "pair-secondary"}:
        return cmd_pair(args)
    if args.command == "revoke":
        return cmd_revoke(args)
    if args.command == "rename":
        return cmd_rename(args)
    if args.command == "mark-home-station":
        return cmd_home(args)
    if args.command == "clear-sandbox":
        return cmd_clear(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
