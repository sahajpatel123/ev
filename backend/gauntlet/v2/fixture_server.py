"""One world's fake outside world, served over loopback HTTP.

Evie's integrations speak a real provider mode (``provider=http``): every
adapter action is ``POST {base_url}/actions/{action}``. A world registers its
messaging, mail, phone, contacts, calendar and smart-home integrations against
this server, so production code runs unchanged while every send, call and
calendar write lands in an inspectable JSON state file instead of on a real
device. ``GET /web/<path>`` serves the world's fixture pages.

Nothing here is production behavior. The server never forwards anything.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

EMPTY_STATE: dict[str, Any] = {
    "contacts": [],
    "messages": [],
    "whatsapp": [],
    "mail": [],
    "calendar": [],
    "devices": [],
    "web": {},
    "outbox": [],
    "mail_outbox": [],
    "drafts": [],
    "calls": [],
    "calendar_writes": [],
    "contact_writes": [],
    "device_writes": [],
    "web_hits": [],
    "requests": [],
}


class WorldState:
    """JSON-file state, guarded by one lock. Oracles read the same file."""

    def __init__(self, path: Path, seed: dict[str, Any] | None = None) -> None:
        self.path = path
        self.lock = threading.Lock()
        state = json.loads(json.dumps(EMPTY_STATE))
        for key, value in (seed or {}).items():
            state[key] = value
        self._write(state)

    def _write(self, state: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def read(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(self.path.read_text(encoding="utf-8"))

    def mutate(self, fn) -> Any:
        with self.lock:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            out = fn(state)
            self._write(state)
            return out


def _match(query: str, *fields: Any) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return True
    hay = " ".join(str(f or "") for f in fields).lower()
    return all(part in hay for part in q.split())


def _contact_for(state: dict[str, Any], who: str) -> dict[str, Any] | None:
    w = (who or "").strip().lower()
    if not w:
        return None
    for contact in state.get("contacts") or []:
        names = [str(contact.get("name") or "").lower(), *[str(a).lower() for a in contact.get("aliases") or []]]
        if w in names or w == str(contact.get("phone") or "").lower() or w == str(contact.get("email") or "").lower():
            return contact
    for contact in state.get("contacts") or []:
        if w in str(contact.get("name") or "").lower().split():
            return contact
    return None


def handle_action(state: dict[str, Any], action: str, args: dict[str, Any]) -> dict[str, Any]:
    """The fake provider. Writes are recorded; reads come from the seed."""
    now = time.time()
    state["requests"].append({"action": action, "args": args, "ts": now})
    if action == "messaging.send":
        to = str(args.get("to") or "")
        contact = _contact_for(state, to)
        row = {
            "id": f"msg-{uuid.uuid4().hex[:8]}", "to": to, "text": str(args.get("text") or ""),
            "service": str(args.get("service") or "imessage"), "confirm": bool(args.get("confirm")),
            "resolved": (contact or {}).get("name"), "ts": now,
        }
        state["outbox"].append(row)
        return {"ok": True, "sent": True, "delivered": True, "message_id": row["id"], "to": to,
                "recipient": (contact or {}).get("name") or to}
    if action == "messaging.list_messages":
        rows = [m for m in state.get("messages") or [] if _match(str(args.get("query") or ""), m.get("from"), m.get("text"))]
        limit = int(args.get("limit") or 20)
        return {"ok": True, "messages": rows[-limit:]}
    if action == "whatsapp.list_chats":
        chats = sorted({str(m.get("chat") or m.get("from") or "") for m in state.get("whatsapp") or []})
        return {"ok": True, "chats": [{"name": c} for c in chats if c]}
    if action == "whatsapp.read_chat":
        to = str(args.get("to") or "").lower()
        rows = [m for m in state.get("whatsapp") or [] if to in str(m.get("chat") or m.get("from") or "").lower()]
        return {"ok": True, "messages": rows[-int(args.get("limit") or 20):]}
    if action == "mail.list":
        rows = [m for m in state.get("mail") or [] if _match(str(args.get("query") or ""), m.get("from"), m.get("subject"), m.get("body"))]
        return {"ok": True, "items": rows[-int(args.get("limit") or 20):]}
    if action in {"mail.send", "mail.reply", "mail.forward"}:
        row = {"id": f"mail-{uuid.uuid4().hex[:8]}", "action": action, **{k: args.get(k) for k in args}, "ts": now}
        state["mail_outbox"].append(row)
        return {"ok": True, "sent": True, "message_id": row["id"]}
    if action == "mail.draft":
        row = {"id": f"draft-{uuid.uuid4().hex[:8]}", **dict(args), "ts": now}
        state["drafts"].append(row)
        return {"ok": True, "drafted": True, "sent": False, "draft_id": row["id"]}
    if action in {"phone.call", "facetime.call"}:
        row = {"to": str(args.get("to") or ""), "kind": action, "ts": now}
        state["calls"].append(row)
        return {"ok": True, "opened": True, "to": row["to"]}
    if action == "contacts.resolve":
        q = str(args.get("query") or "")
        rows = [c for c in state.get("contacts") or [] if _match(q, c.get("name"), " ".join(c.get("aliases") or []), c.get("email"), c.get("phone"))]
        return {"ok": True, "contacts": rows, "matches": rows}
    if action == "contacts.list":
        return {"ok": True, "contacts": list(state.get("contacts") or [])}
    if action in {"contacts.create", "contacts.update"}:
        row = {"action": action, **dict(args), "ts": now}
        state["contact_writes"].append(row)
        if action == "contacts.create":
            state["contacts"].append({k: args.get(k) for k in ("name", "phone", "email", "company")})
        return {"ok": True, "saved": True}
    if action == "calendar.list_upcoming":
        return {"ok": True, "events": list(state.get("calendar") or [])}
    if action == "calendar.create_event":
        row = {k: args.get(k) for k in ("summary", "start", "end")}
        state["calendar_writes"].append({**row, "ts": now})
        state["calendar"].append(row)
        return {"ok": True, "created": True, "event": row}
    if action in {"home.list_devices", "home.status"}:
        return {"ok": True, "devices": list(state.get("devices") or [])}
    if action in {"home.set_device", "light.set", "lock.set", "cover.set"}:
        state["device_writes"].append({"action": action, **dict(args), "ts": now})
        return {"ok": True, "applied": True}
    return {"ok": False, "error": f"fixture server: unknown action {action}"}


def _handler(state: WorldState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: D401 - silence stdlib access log
            return

        def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            raw = body if isinstance(body, bytes) else (
                body.encode() if isinstance(body, str) else json.dumps(body).encode()
            )
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802 - stdlib name
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                args = json.loads(raw or b"{}")
            except ValueError:
                args = {}
            if self.path.startswith("/actions/"):
                action = self.path[len("/actions/"):].split("?", 1)[0]
                out = state.mutate(lambda s: handle_action(s, action, args if isinstance(args, dict) else {}))
                self._send(200, out)
                return
            if self.path.startswith("/oauth/"):
                self._send(200, {"access_token": "world-token-refreshed", "token_type": "Bearer"})
                return
            self._send(404, {"ok": False, "error": "not found"})

        def do_GET(self):  # noqa: N802 - stdlib name
            if self.path.startswith("/web/"):
                key = self.path[len("/web/"):].split("?", 1)[0]
                snapshot = state.read()
                page = (snapshot.get("web") or {}).get(key)
                state.mutate(lambda s: s["web_hits"].append({"path": key, "ts": time.time()}))
                if page is None:
                    self._send(404, "<html><body>not found</body></html>", "text/html")
                    return
                ctype = "application/json" if key.endswith(".json") else (
                    "text/plain" if key.endswith(".txt") else "text/html"
                )
                self._send(200, page if isinstance(page, str) else json.dumps(page), ctype)
                return
            if self.path == "/healthz":
                self._send(200, {"ok": True})
                return
            self._send(404, {"ok": False})

    return Handler


class FixtureServer:
    def __init__(self, state: WorldState) -> None:
        self.state = state
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> FixtureServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
