"""Python client for the Evie.app unix-socket bridge.

The bridge runs inside Evie.app's TCC identity, so permission-gated ops
(screenshots, Automation JXA, TCC status) execute with the app's grants
instead of the caller's. When the app is down the client fails honestly —
never by silently running the op under the wrong identity.
"""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from typing import Any


def socket_path() -> Path:
    return Path.home() / ".ev" / "bridge.sock"


def token_path() -> Path:
    return Path.home() / ".ev" / "bridge-token"


def bridge_available() -> bool:
    return socket_path().exists() and token_path().is_file()


def _read_token() -> str | None:
    try:
        token = token_path().read_text().strip()
    except OSError:
        return None
    return token or None


class BridgeClient:
    """One-shot JSON-over-unix-socket calls. No persistent connection."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def call(self, op: str, args: dict[str, Any] | None = None,
             timeout: float | None = None) -> dict[str, Any]:
        token = _read_token()
        if token is None or not socket_path().exists():
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "bridge unavailable",
                    "fix": "Launch Evie.app, then retry.",
                    "undo": None}
        request = {"id": f"bc-{uuid.uuid4().hex[:8]}", "token": token,
                   "op": op, "args": dict(args or {})}
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout if timeout is not None else self.timeout)
            with sock:
                sock.connect(str(socket_path()))
                sock.sendall((json.dumps(request) + "\n").encode())
                chunks: list[bytes] = []
                while True:
                    piece = sock.recv(65536)
                    if not piece:
                        break
                    chunks.append(piece)
                    if b"\n" in piece:
                        break
        except (OSError, TimeoutError) as exc:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": f"bridge unreachable: {type(exc).__name__}",
                    "fix": "Relaunch Evie.app, then retry.",
                    "undo": None}
        raw = b"".join(chunks).decode("utf-8", "replace").strip().split("\n", 1)[0]
        try:
            response = json.loads(raw)
        except ValueError:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "bridge bad response", "undo": None}
        if not isinstance(response, dict) or response.get("id") != request["id"]:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "bridge id mismatch", "undo": None}
        if response.get("ok"):
            result = response.get("result")
            return {"ok": True, "verified": True,
                    "evidence": result if isinstance(result, dict) else {"result": result},
                    "error": None, "undo": None}
        out: dict[str, Any] = {"ok": False, "verified": True, "evidence": {},
                               "error": str(response.get("error") or "bridge error"),
                               "undo": None}
        if response.get("fix"):
            out["fix"] = str(response["fix"])
        return out


def tcc_status(timeout: float = 10.0) -> dict[str, Any]:
    """App-identity TCC truth for every permission. Honest when down."""
    return BridgeClient(timeout=timeout).call("tcc_status")
