"""Reliable WhatsApp Web driver over Chrome DevTools Protocol.

AppleScript ``execute javascript`` sends synthetic events that WhatsApp Web
ignores in a background tab: clicks land on recycled rows and Enter does
nothing. CDP delivers *trusted* mouse/keyboard input to the tab without
activating Chrome, which is exactly what a human does — so navigation and the
Send button genuinely work while the owner keeps working in other windows.

Chrome 136+ refuses remote debugging on the default profile, so Evie uses a
dedicated profile (``~/.ev/chrome-cdp``) that the owner links to WhatsApp once.
Without a linked profile every call degrades honestly: ``cdp_not_linked``.

No new dependency: ``websockets`` already ships with the backend.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_PORT = 9222
DEFAULT_PROFILE = "~/.ev/chrome-cdp"
WHATSAPP_URL = "https://web.whatsapp.com/"
CHROME_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CONNECT_TIMEOUT = 6.0
EVAL_TIMEOUT = 20.0

_status_cache: tuple[float, bool, str] | None = None


def cdp_port() -> int:
    raw = str(os.environ.get("EV_WHATSAPP_CDP_PORT") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def cdp_base() -> str:
    return f"http://127.0.0.1:{cdp_port()}"


def _profile_dir() -> Path:
    raw = str(os.environ.get("EV_WHATSAPP_CDP_PROFILE") or "").strip() or DEFAULT_PROFILE
    return Path(raw).expanduser()


async def _pages(timeout: float = 3.0) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{cdp_base()}/json")
        data = resp.json()
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [
        item
        for item in data
        if isinstance(item, dict) and str(item.get("url") or "").startswith("https://web.whatsapp.com")
    ]


async def whatsapp_target(*, timeout: float = 3.0) -> dict[str, Any] | None:
    pages = await _pages(timeout=timeout)
    for page in pages:
        if str(page.get("type") or "") == "page" and page.get("webSocketDebuggerUrl"):
            return page
    return None


async def _cdp(ws, method: str, params: dict[str, Any], *, msg_id: int) -> dict[str, Any]:
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=EVAL_TIMEOUT)
        message = json.loads(raw)
        if message.get("id") == msg_id:
            return message


async def evaluate(ws, expression: str, *, msg_id: int) -> Any:
    message = await _cdp(
        ws,
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
        msg_id=msg_id,
    )
    result = message.get("result", {}).get("result", {})
    if result.get("subtype") == "error":
        raise RuntimeError(result.get("description") or "js_error")
    return result.get("value")


async def _click(ws, x: float, y: float, *, msg_id: int) -> int:
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        await _cdp(
            ws,
            "Input.dispatchMouseEvent",
            {
                "type": kind,
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1 if kind != "mouseMoved" else 0,
            },
            msg_id=msg_id,
        )
        msg_id += 1
    return msg_id


async def _key(ws, key: str, code: str, keycode: int, *, msg_id: int, modifiers: int = 0) -> int:
    for kind in ("rawKeyDown", "keyUp"):
        await _cdp(
            ws,
            "Input.dispatchKeyEvent",
            {
                "type": kind,
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": keycode,
                "nativeVirtualKeyCode": keycode,
                "modifiers": modifiers,
            },
            msg_id=msg_id,
        )
        msg_id += 1
    return msg_id


async def _wait(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def available(*, refresh: bool = False) -> tuple[bool, str]:
    """(linked, diagnosis). Cached briefly; never launches Chrome."""

    global _status_cache
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False, "cdp_disabled"
    now = time.monotonic()
    if not refresh and _status_cache is not None:
        stamped, linked, diagnosis = _status_cache
        if now - stamped < 3.0:
            return linked, diagnosis
    target = await whatsapp_target()
    if target is None:
        _status_cache = (now, False, "cdp_chrome_not_running")
        return False, "cdp_chrome_not_running"
    try:
        import websockets

        async with websockets.connect(target["webSocketDebuggerUrl"], open_timeout=CONNECT_TIMEOUT) as ws:
            state = await evaluate(
                ws,
                "JSON.stringify({ready:!!document.querySelector('#pane-side'), qr:/scan the qr|log in to whatsapp/i.test(document.body.innerText||'')})",
                msg_id=1,
            )
    except Exception:
        _status_cache = (now, False, "cdp_connect_failed")
        return False, "cdp_connect_failed"
    try:
        parsed = json.loads(state or "{}")
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    linked = bool(parsed.get("ready")) and not bool(parsed.get("qr"))
    _status_cache = (now, linked, "ok" if linked else "cdp_not_linked")
    return linked, ("ok" if linked else "cdp_not_linked")


async def ensure_running() -> bool:
    """Start Evie's dedicated debug Chrome in the background if needed."""

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if await whatsapp_target(timeout=1.5) is not None:
        return True
    if not os.path.isfile(CHROME_BINARY):
        return False
    profile = _profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    try:
        subprocess = await asyncio.create_subprocess_exec(
            "/usr/bin/open",
            "-g",
            "-n",
            "-a",
            "Google Chrome",
            "--args",
            f"--remote-debugging-port={cdp_port()}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            WHATSAPP_URL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        del subprocess
    except Exception:
        return False
    for _ in range(12):
        await _wait(0.8)
        if await whatsapp_target(timeout=1.5) is not None:
            return True
    return False


_FIND_JS = r"""
(function () {
  function norm(s) { return String(s||'').normalize('NFC').replace(/[\uFE00-\uFE0F\u200e\u200f\u2060]/g,'').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim().toLowerCase(); }
  function chatName(n) { const t=n.querySelector('[data-testid="cell-frame-title"]'); const raw=(t&&(t.getAttribute('title')||t.innerText))||n.getAttribute('title')||''; return String(raw).split('\n')[0].trim(); }
  function chatNodes() { return Array.from(document.querySelectorAll('[data-testid="cell-frame-container"], #pane-side [role="listitem"], [aria-label="Chat list"] [role="listitem"], #pane-side [role="row"], [data-testid^="list-item"]')); }
  function rect(el) { if(!el) return null; const r=el.getBoundingClientRect(); if(r.width<4||r.height<4) return null; return {x:r.left+r.width/2, y:r.top+r.height/2}; }
  const want = __WANT__;
  const exact = chatNodes().find((n)=>norm(chatName(n))===norm(want));
  return JSON.stringify({
    search: rect(document.querySelector('input[aria-label="Search or start a new chat"]')||document.querySelector('#side input[type="text"]')||document.querySelector('#side [contenteditable="true"]')),
    row: rect(exact || null),
    header: (function(){const h=document.querySelector('[data-testid="conversation-info-header-chat-title"]'); return h?(h.innerText||'').split('\n')[0].trim():null;})(),
    compose: rect(document.querySelector('[data-testid="conversation-compose-box-input"]')||document.querySelector('footer [contenteditable="true"]')),
    send: rect((function(){const b=document.querySelector('footer button[aria-label*="Send" i]'); if(b) return b; const i=document.querySelector('[data-icon="wds-ic-send-filled"],[data-icon="send"]'); return i?(i.closest('button')||i):null;})()),
    body: (document.querySelector('[data-testid="conversation-compose-box-input"]')||document.querySelector('footer [contenteditable="true"]')||{}).innerText || ''
  });
})()
"""

_VERIFY_JS = r"""
(function () {
  const want = __WANT__;
  const rows = Array.from(document.querySelectorAll('[data-testid="msg-container"]'));
  const fromMe = rows.filter((r)=>r.querySelector('[data-icon="tail-out"]'));
  const tail = fromMe.slice(-3).map((r)=>(r.innerText||''));
  return JSON.stringify({appeared: tail.some((t)=>t.includes(want))});
})()
"""


def _js(template: str, want: str) -> str:
    return template.replace("__WANT__", json.dumps(want))


async def send(to: str, text: str) -> dict[str, Any]:
    """Trusted-input send through the CDP tab. Never claims a tap-free send."""

    body = (text or "").strip()
    target_name = (to or "").strip()
    if not body or not target_name:
        return {
            "ok": False,
            "sent": False,
            "channel": "whatsapp",
            "error": "empty_message",
            "spoken": "There's nothing to send.",
            "focus_theft": 0,
        }

    linked, diagnosis = await available()
    if (
        not linked
        and diagnosis == "cdp_chrome_not_running"
        and await ensure_running()
    ):
        linked, diagnosis = await available(refresh=True)
    if not linked:
        return {
            "ok": False,
            "sent": False,
            "channel": "whatsapp",
            "error": "whatsapp_cdp_unavailable",
            "diagnosis": diagnosis,
            "spoken": (
                "WhatsApp needs to be linked once in Evie's Chrome profile. "
                "Open the window Evie just started, scan the WhatsApp QR, then ask me again."
            ),
            "focus_theft": 0,
        }

    target = await whatsapp_target()
    if target is None:
        return {
            "ok": False,
            "sent": False,
            "channel": "whatsapp",
            "error": "whatsapp_cdp_unavailable",
            "diagnosis": "target_lost",
            "spoken": "I lost the WhatsApp tab. Ask me again.",
            "focus_theft": 0,
        }

    import websockets

    try:
        async with websockets.connect(target["webSocketDebuggerUrl"], open_timeout=CONNECT_TIMEOUT) as ws:
            msg_id = 1

            async def find() -> dict[str, Any]:
                nonlocal msg_id
                value = await evaluate(ws, _js(_FIND_JS, target_name), msg_id=msg_id)
                msg_id += 1
                try:
                    return json.loads(value or "{}")
                except (TypeError, json.JSONDecodeError):
                    return {}

            state = await find()
            header = str(state.get("header") or "")
            if header.casefold() == target_name.casefold():
                compose = state.get("compose")
            else:
                search = state.get("search")
                if not search:
                    return _fail("search_input_missing", target_name)
                msg_id = await _click(ws, search["x"], search["y"], msg_id=msg_id)
                await _key(ws, "a", "KeyA", 65, msg_id=msg_id, modifiers=4)  # Cmd+A
                msg_id += 2
                await _key(ws, "Backspace", "Backspace", 8, msg_id=msg_id)
                msg_id += 2
                # Trusted typing into the focused search box.
                await _cdp(ws, "Input.insertText", {"text": target_name}, msg_id=msg_id)
                msg_id += 1

                row = None
                for _ in range(16):
                    await _wait(0.5)
                    state = await find()
                    if state.get("row"):
                        row = state["row"]
                        break
                if not row:
                    return _fail("exact_row_missing", target_name)
                msg_id = await _click(ws, row["x"], row["y"], msg_id=msg_id)
                compose = None
                for _ in range(16):
                    await _wait(0.5)
                    state = await find()
                    if (state.get("header") or "").casefold() == target_name.casefold():
                        compose = state.get("compose")
                        break
                if not compose:
                    return _fail("chat_open_failed", target_name)

            if not compose:
                return _fail("compose_box_missing", target_name)
            msg_id = await _click(ws, compose["x"], compose["y"], msg_id=msg_id)
            await _key(ws, "a", "KeyA", 65, msg_id=msg_id, modifiers=4)
            msg_id += 2
            await _key(ws, "Backspace", "Backspace", 8, msg_id=msg_id)
            msg_id += 2
            await _cdp(ws, "Input.insertText", {"text": body}, msg_id=msg_id)
            msg_id += 1
            await _wait(0.3)

            state = await find()
            send_btn = state.get("send")
            if not send_btn:
                return _fail("send_button_missing", target_name)
            msg_id = await _click(ws, send_btn["x"], send_btn["y"], msg_id=msg_id)

            appeared = False
            for _ in range(10):
                await _wait(0.7)
                value = await evaluate(ws, _js(_VERIFY_JS, body), msg_id=msg_id)
                msg_id += 1
                try:
                    if json.loads(value or "{}").get("appeared"):
                        appeared = True
                        break
                except (TypeError, json.JSONDecodeError):
                    continue
    except Exception as exc:
        return {
            "ok": False,
            "sent": False,
            "channel": "whatsapp",
            "error": "whatsapp_cdp_failed",
            "diagnosis": type(exc).__name__,
            "spoken": f"I couldn't send that WhatsApp to {target_name}.",
            "focus_theft": 0,
        }

    return {
        "ok": appeared,
        "sent": appeared,
        "channel": "whatsapp",
        "to": target_name,
        "verified_in_thread": appeared,
        "driver": "cdp",
        "focus_theft": 0,
        "spoken": (
            f"Sent WhatsApp to {target_name}."
            if appeared
            else f"I typed the message to {target_name} but couldn't confirm it was sent."
        ),
    }


def _fail(code: str, target_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "sent": False,
        "channel": "whatsapp",
        "error": code,
        "diagnosis": code,
        "spoken": (
            f"I found {target_name} on WhatsApp but couldn't finish opening the chat, "
            "so nothing was sent. Reload WhatsApp and ask me again."
        ),
        "focus_theft": 0,
    }
