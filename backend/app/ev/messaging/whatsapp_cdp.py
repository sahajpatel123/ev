"""Reliable WhatsApp Web driver over Chrome DevTools Protocol.

AppleScript ``execute javascript`` sends synthetic events that WhatsApp Web
ignores in a background tab: clicks land on recycled rows and Enter does
nothing. CDP delivers *trusted* mouse/keyboard input to the tab without
activating Chrome, which is exactly what a human does — so navigation and the
Send button genuinely work while the owner keeps working in other windows.

Ordinary operations use a dedicated headless browser and cross-process
workspace lock. Explicit setup returns a QR image without opening a window.

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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_PORT = 9222
DEFAULT_PROFILE = "~/.ev/chrome-cdp"
WHATSAPP_URL = "https://web.whatsapp.com/"
CHROME_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CONNECT_TIMEOUT = 6.0
EVAL_TIMEOUT = 20.0

_status_cache: tuple[float, bool, str] | None = None
_workspace_lock = asyncio.Lock()
_headless_verified = False
_linked_ui_verified = False


def _under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def cdp_port() -> int:
    raw = str(os.environ.get("EV_WHATSAPP_CDP_PORT") or "").strip()
    try:
        port = int(raw) if raw else DEFAULT_PORT
        return port if 1 <= port <= 65535 else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def cdp_base() -> str:
    return f"http://127.0.0.1:{cdp_port()}"


def _profile_dir() -> Path:
    raw = str(os.environ.get("EV_WHATSAPP_CDP_PROFILE") or "").strip() or DEFAULT_PROFILE
    return Path(raw).expanduser()


def _linked_marker() -> Path:
    profile = _profile_dir()
    return profile.parent / f"{profile.name}.linked"


def _write_linked_marker() -> None:
    try:
        _linked_marker().parent.mkdir(parents=True, exist_ok=True)
        _linked_marker().write_text("linked\n")
    except OSError:
        pass


async def _pages(timeout: float = 3.0) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.get(f"{cdp_base()}/json")
        data = resp.json()
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [
        item
        for item in data
        if isinstance(item, dict) and urlparse(str(item.get("url") or "")).hostname == "web.whatsapp.com"
        and urlparse(str(item.get("url") or "")).scheme == "https"
    ]


async def whatsapp_target(*, timeout: float = 3.0) -> dict[str, Any] | None:
    pages = await _pages(timeout=timeout)
    for page in pages:
        debug = urlparse(str(page.get("webSocketDebuggerUrl") or ""))
        if (str(page.get("type") or "") == "page" and debug.scheme == "ws"
                and debug.hostname in {"127.0.0.1", "localhost", "::1"}
                and debug.port == cdp_port()):
            return page
    return None


async def _cdp(ws, method: str, params: dict[str, Any], *, msg_id: int) -> dict[str, Any]:
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
    deadline = time.monotonic() + EVAL_TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("cdp_command_timeout")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        message = json.loads(raw)
        if message.get("id") == msg_id:
            if message.get("error"):
                raise RuntimeError("cdp_protocol_error")
            return message


async def evaluate(ws, expression: str, *, msg_id: int) -> Any:
    message = await _cdp(
        ws,
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
        msg_id=msg_id,
    )
    if message.get("result", {}).get("exceptionDetails"):
        raise RuntimeError("cdp_javascript_failed")
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

    global _status_cache, _headless_verified, _linked_ui_verified
    if _under_pytest():
        return False, "cdp_disabled"
    now = time.monotonic()
    if not refresh and _status_cache is not None:
        stamped, linked, diagnosis = _status_cache
        if now - stamped < 3.0:
            return linked, diagnosis
    _headless_verified = False
    _linked_ui_verified = False
    target = await whatsapp_target()
    if target is None:
        _status_cache = (now, False, "cdp_chrome_not_running")
        return False, "cdp_chrome_not_running"
    try:
        import websockets

        async with websockets.connect(target["webSocketDebuggerUrl"], open_timeout=CONNECT_TIMEOUT, max_size=16 * 1024 * 1024) as ws:
            _headless_verified = await _browser_metadata(ws)
            state = await evaluate(
                ws,
                "JSON.stringify({ready:!!document.querySelector('#pane-side, [aria-label=\"Chat list\"]'), qr:!document.querySelector('#pane-side, [aria-label=\"Chat list\"]') && (!!document.querySelector('canvas[aria-label*=\"QR\" i], [data-testid=\"qrcode\"], [data-ref] canvas') || /scan the qr|log in to whatsapp|log into whatsapp|steps to log in|use whatsapp on your computer/i.test(document.body.innerText||''))})",
                msg_id=2,
            )
    except WorkspaceError as exc:
        _status_cache = (now, False, str(exc))
        return False, str(exc)
    except Exception:
        _status_cache = (now, False, "cdp_connect_failed")
        return False, "cdp_connect_failed"
    try:
        parsed = json.loads(state or "{}")
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    _linked_ui_verified = bool(parsed.get("ready")) and not bool(parsed.get("qr"))
    linked = _linked_ui_verified and _headless_verified
    if not _headless_verified:
        diagnosis = "cdp_foreground_browser"
    elif linked:
        _write_linked_marker()
        diagnosis = "ok"
    elif parsed.get("qr"):
        diagnosis = "cdp_qr"
    else:
        diagnosis = "cdp_not_linked"
    _status_cache = (now, linked, diagnosis)
    return linked, diagnosis


async def reveal_window() -> bool:
    """Bring the WhatsApp tab's window forward (for the one-time QR scan)."""

    target = await whatsapp_target(timeout=1.5)
    if target is None:
        return False
    try:
        import websockets

        async with websockets.connect(target["webSocketDebuggerUrl"], open_timeout=CONNECT_TIMEOUT, max_size=16 * 1024 * 1024) as ws:
            await _cdp(ws, "Page.bringToFront", {}, msg_id=1)
    except Exception:
        return False
    return True


async def ensure_running(*, bring_to_front: bool = False) -> bool:
    """Start Evie's dedicated headless Chrome if needed.

    ``bring_to_front=True`` raises the window so the owner can scan the
    WhatsApp QR on first link; normal operation keeps Chrome in the background.
    """

    if _under_pytest():
        return False
    target = await whatsapp_target(timeout=1.5)
    if target is not None:
        if bring_to_front:
            await reveal_window()
        return True
    if not os.path.isfile(CHROME_BINARY):
        return False
    profile = _profile_dir()
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile.chmod(0o700)
    # Never launch a second process against a live debugger: it may belong to
    # an unrelated browser, or an owner-visible setup window. Refuse safely.
    try:
        async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
            probe = await client.get(f"{cdp_base()}/json/version")
        if probe.status_code == 200:
            return False
    except httpx.HTTPError:
        pass
    try:
        command = [
            CHROME_BINARY,
            f"--remote-debugging-port={cdp_port()}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--enable-automation",
        ]
        if not bring_to_front:
            command += ["--headless=new", "--window-size=1280,960"]
        command.append(WHATSAPP_URL)
        subprocess = await asyncio.create_subprocess_exec(
            *command,
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
            if bring_to_front:
                await reveal_window()
            return True
    return False


async def ensure_ready(*, reveal_workspace: bool = False) -> tuple[str, str]:
    """Background preflight: ``linked`` | ``qr`` | ``loading`` | ``unavailable``.

    Launches Evie's Chrome when it is not running and polls through the page
    load. ``qr`` means the window is up with the scan prompt: the owner must
    link the profile once, and no send should be attempted or approved yet.
    """

    if _under_pytest():
        return "unavailable", "cdp_disabled"
    if not os.path.isfile(CHROME_BINARY):
        return "unavailable", "cdp_chrome_missing"
    linked, diagnosis = await available(refresh=True)
    if not linked and diagnosis == "cdp_chrome_not_running":
        await ensure_running(
            bring_to_front=reveal_workspace and not _linked_marker().exists()
        )
        for _ in range(15):
            await _wait(0.8)
            linked, diagnosis = await available(refresh=True)
            if linked or diagnosis == "cdp_qr":
                break
    if linked:
        return "linked", diagnosis
    if diagnosis == "cdp_qr":
        if reveal_workspace:
            await reveal_window()
        return "qr", diagnosis
    if diagnosis == "cdp_not_linked" and await whatsapp_target(timeout=1.5) is not None:
        # The tab is up but the app has not painted yet. Never hand a send to
        # the AppleScript path in this state; it cannot drive the tab anyway.
        return "loading", diagnosis
    return "unavailable", diagnosis


# Only this dedicated browser workspace is changed. Never use OS keyboard,
# Accessibility navigation, Page.bringToFront, or the owner's everyday tab.
_UI_JS = r"""
function norm(s) { return String(s||'').normalize('NFC').replace(/[\uFE00-\uFE0F\u200e\u200f\u2060]/g,'').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim().toLowerCase(); }
function title() { const n=document.querySelector('[data-testid="conversation-info-header-chat-title"], #main header span[title]'); return n?String(n.getAttribute('title')||n.innerText||'').split('\n')[0].trim():''; }
function box() { return document.querySelector('[data-testid="conversation-compose-box-input"], footer [contenteditable="true"]'); }
function rect(n) { if(!n)return null; const r=n.getBoundingClientRect(); if(r.width<4||r.height<4)return null; return {x:r.left+r.width/2,y:r.top+r.height/2}; }
function rows() { const raw=Array.from(document.querySelectorAll('[data-testid="cell-frame-container"],#pane-side [role="row"],#pane-side [role="listitem"]')); return raw.filter(n=>!raw.some(other=>other!==n&&n.contains(other))); }
function name(n) { const t=n.querySelector('[data-testid="cell-frame-title"],span[title],span[dir="auto"]'); return t?String(t.getAttribute('title')||t.innerText||'').split('\n')[0].trim():''; }
function search() { return document.querySelector('input[aria-label="Search or start a new chat"], #side input[type="text"], #side [contenteditable="true"][role="textbox"], #side [contenteditable="true"]'); }
function messages() { return Array.from(document.querySelectorAll('[data-testid="msg-container"]')).map(n=>{
  const idNode=n.closest('[data-id]')||n.querySelector('[data-id]');
  const identifier=idNode?String(idNode.getAttribute('data-id')||''):'';
  const copy=n.querySelector('.selectable-text')||n.querySelector('.copyable-text')||n;
  const pre=n.querySelector('[data-pre-plain-text]');
  const text=String(copy.innerText||'').trim();
  const failed=!!n.querySelector('[data-icon="msg-error"],[data-icon="alert-error"]');
  const clock=!!n.querySelector('[data-icon="msg-time"],[data-icon="msg-clock"],[data-icon*="time"],[aria-label*="Pending" i]');
  const read=!!n.querySelector('[data-icon="msg-dblcheck-ack"],[data-icon*="dblcheck-ack"],[aria-label*="Read" i]');
  const delivered=read||!!n.querySelector('[data-icon="msg-dblcheck"],[data-icon*="dblcheck"],[aria-label*="Delivered" i]');
  const acknowledged=delivered||!!n.querySelector('[data-icon="msg-check"],[data-icon*="check"],[aria-label*="Sent" i]');
  // Self-chat ("Message yourself") renders sent rows on the incoming side but
  // keeps the ack ticks, which only appear on outgoing messages.
  const outgoing=!!(n.closest('.message-out')||n.querySelector('[data-icon="tail-out"]'))
    ||identifier.startsWith('true_')||read||delivered||acknowledged||clock||failed;
  const transport=failed?'failed':clock?'queued':read?'read':delivered?'delivered':acknowledged?'sent':'unknown';
  return {id:identifier,text:text,body:text,from_me:outgoing,transport_state:transport,delivery_confirmed:outgoing&&delivered,
          timestamp:pre?String(pre.getAttribute('data-pre-plain-text')||''):'',sender:outgoing?'owner':title()};
}).filter(n=>n.text); }

"""


def _script(code: str) -> str:
    return "(function(){" + _UI_JS + code + "})()"


class WorkspaceError(RuntimeError):
    """Safe diagnosis without exposing page contents or protocol addresses."""


class _Workspace:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.sequence = 1

    async def eval(self, code: str) -> Any:
        result = await evaluate(self.ws, _script(code), msg_id=self.sequence)
        self.sequence += 1
        return result

    async def command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = await _cdp(self.ws, method, params, msg_id=self.sequence)
        self.sequence += 1
        return result

    async def click(self, point: dict[str, float]) -> None:
        self.sequence = await _click(self.ws, point["x"], point["y"], msg_id=self.sequence)

    async def type(self, point: dict[str, float], text: str) -> None:
        await self.click(point)
        self.sequence = await _key(self.ws, "a", "KeyA", 65, msg_id=self.sequence, modifiers=4)
        self.sequence = await _key(self.ws, "Backspace", "Backspace", 8, msg_id=self.sequence)
        if text:
            await self.command("Input.insertText", {"text": text})

    async def set_search(self, text: str) -> bool:
        """Set the sidebar search box through the DOM.

        Coordinate clicks plus ``Input.insertText`` do not reliably land in the
        headless window, which made every search-by-name open fail
        (``chat_not_found``) and therefore blocked sends. Setting the native
        value and dispatching an input event drives WhatsApp's own search.
        """

        try:
            value = json.dumps(text)
            result = await self.eval(
                "const el=search(); if(!el) return false; el.focus();"
                f"const value={value};"
                "if(el.isContentEditable){el.textContent=value;}"
                "else{const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
                "const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;"
                "if(setter){setter.call(el,value);}else{el.value=value;}}"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "return true;"
            )
            return bool(result)
        except Exception:
            return False

    async def state(self, wanted: str) -> dict[str, Any]:
        return await self.eval(
            "const want=" + json.dumps(wanted) + "; const hits=rows().filter(n=>norm(name(n))===norm(want));"
            "return {header:title(),search:rect(search()),row:hits.length===1?rect(hits[0]):null,"
            "matches:hits.length,compose:rect(box()),body:box()?String(box().innerText||''):'',"
            "send:rect(document.querySelector('footer button[aria-label*=\"Send\" i]')||"
            "(document.querySelector('footer [data-icon=\"send\"],footer [data-icon=\"wds-ic-send-filled\"]')||{}).parentElement)};"
        )

    async def open(self, wanted: str) -> str:
        if not wanted.strip():
            raise WorkspaceError("empty_recipient")
        state = await self.state(wanted)
        if state.get("matches", 0) > 1:
            raise WorkspaceError("ambiguous_recipient")
        if _same_name(state.get("header", ""), wanted) and state.get("compose"):
            return str(state["header"])
        if not state.get("search"):
            raise WorkspaceError("search_input_missing")
        if not await self.set_search(wanted):
            await self.type(state["search"], wanted)
        try:
            for _ in range(16):
                await _wait(0.3)
                state = await self.state(wanted)
                if state.get("matches", 0) > 1:
                    raise WorkspaceError("ambiguous_recipient")
                if state.get("row"):
                    await self.click(state["row"])
                    for _ in range(12):
                        await _wait(0.2)
                        state = await self.state(wanted)
                        if _same_name(state.get("header", ""), wanted) and state.get("compose"):
                            return str(state["header"])
            raise WorkspaceError("chat_not_found")
        finally:
            # Clearing the dedicated sidebar never changes the open thread.
            # Preserve the original exception if the browser disconnected.
            try:
                if not await self.set_search(""):
                    point = await self.eval("return rect(search());")
                    if point:
                        await self.type(point, "")
            except Exception:
                pass

    async def read(self, wanted: str, limit: int) -> list[dict[str, Any]]:
        state = await self.state(wanted)
        if not _same_name(state.get("header", ""), wanted):
            raise WorkspaceError("recipient_changed")
        cap = max(1, min(int(limit), 80))
        value = await self.eval(f"return messages().slice(-{cap});")
        return value if isinstance(value, list) else []

    async def compose(self, wanted: str, body: str) -> None:
        state = await self.state(wanted)
        if not _same_name(state.get("header", ""), wanted) or not state.get("compose"):
            raise WorkspaceError("recipient_changed")
        prior = str(state.get("body") or "").strip()
        if prior and prior != body:
            raise WorkspaceError("foreign_draft")
        if prior != body:
            await self.type(state["compose"], body)
        state = await self.state(wanted)
        if not _same_name(state.get("header", ""), wanted):
            raise WorkspaceError("recipient_changed")
        if str(state.get("body") or "").strip() != body:
            raise WorkspaceError("compose_not_verified")


def _same_name(left: str, right: str) -> bool:
    import re
    import unicodedata

    def clean(value: str) -> str:
        normalized = unicodedata.normalize("NFC", str(value))
        return " ".join(re.sub(r"[\uFE00-\uFE0F\u200e\u200f\u2060]", "", normalized).split()).casefold()

    return bool(clean(left)) and clean(left) == clean(right)


@asynccontextmanager
async def _workspace():
    if _under_pytest():
        raise WorkspaceError("cdp_disabled")
    async with _transaction_lock():
        state, diagnosis = await ensure_ready(reveal_workspace=False)
        if state != "linked":
            raise WorkspaceError(diagnosis)
        target = await whatsapp_target()
        if target is None:
            raise WorkspaceError("target_lost")
        import websockets

        async with websockets.connect(target["webSocketDebuggerUrl"], open_timeout=CONNECT_TIMEOUT, max_size=16 * 1024 * 1024) as ws:
            await _verify_background(ws)
            workspace = _Workspace(ws)
            workspace.sequence = 2
            yield workspace


def _failure(error: str, *, to: str = "", attempted: bool = False) -> dict[str, Any]:
    spoken = "WhatsApp background connection isn't ready. Nothing was sent."
    if error in {"cdp_qr", "cdp_not_linked"}:
        spoken = "WhatsApp needs its one-time linked-device setup before I can access it."
    elif error == "cdp_foreground_browser":
        spoken = "WhatsApp is linked in a visible browser. Background sending needs the private browser running without a window."
    elif error == "foreign_draft":
        spoken = "That WhatsApp chat has another draft. I left it untouched."
    elif error == "ambiguous_recipient":
        spoken = "More than one WhatsApp chat has that name. I need the exact recipient."
    elif attempted:
        spoken = "The WhatsApp send was attempted, but I couldn't confirm it. Check the thread before retrying."
    return {"ok": False, "sent": False, "channel": "whatsapp", "to": to,
            "error": error, "diagnosis": error, "focus_theft": 0,
            "background": True, "driver": "cdp", "send_attempted": attempted,
            "retry_safe": not attempted, "spoken": spoken}


async def status(*, refresh: bool = False) -> dict[str, Any]:
    linked, diagnosis = await available(refresh=refresh)
    return {"ok": linked, "authenticated": linked, "linked": _linked_ui_verified,
            "browser_authenticated": _linked_ui_verified, "send_available": linked,
            "diagnosis": diagnosis, "background": True, "driver": "cdp",
            "headless": _headless_verified,
            "focus_theft": 0, "setup_required": diagnosis in {"cdp_qr", "cdp_not_linked"}}


async def setup(*, include_qr: bool = True, refresh_qr: bool = False) -> dict[str, Any]:
    """Explicit owner setup. QR image works with headless Chrome; never raises a window.

    Treat qr_png_base64 as sensitive, short-lived pairing material. Callers
    must use owner authentication, no-store responses, and never persist it.
    """
    async with _transaction_lock():
        state, diagnosis = await ensure_ready(reveal_workspace=False)
        result: dict[str, Any] = {"state": state, "diagnosis": diagnosis,
                                  "authenticated": state == "linked", "background": True,
                                  "headless": _headless_verified,
                                  "focus_theft": 0}
        if state != "qr" or not include_qr:
            return result
        target = await whatsapp_target()
        if not target:
            return result
        try:
            import websockets
            async with websockets.connect(target["webSocketDebuggerUrl"], open_timeout=CONNECT_TIMEOUT, max_size=16 * 1024 * 1024) as ws:
                await _verify_background(ws)
                workspace = _Workspace(ws)
                workspace.sequence = 2
                # WhatsApp expires inactive pairing codes. Refresh only its
                # official expired-code control; never reload a valid code
                # while the owner is scanning it in another window.
                refresh = await workspace.eval(
                    "const expired=/click to reload|reload qr code|qr code expired/i.test(document.body.innerText||'');"
                    "if(!expired)return {expired:false,point:null};"
                    "const button=Array.from(document.querySelectorAll('button,[role=\"button\"]')).find(n=>/reload|refresh|expired/i.test((n.innerText||'')+' '+(n.getAttribute('aria-label')||'')))||"
                    "(document.querySelector('[data-icon=\"refresh\"]')||{}).parentElement;"
                    "return {expired:true,point:rect(button)};"
                )
                if refresh and refresh.get("expired"):
                    if not refresh.get("point"):
                        result["diagnosis"] = "cdp_qr_expired_refresh_missing"
                        return result
                    await workspace.click(refresh["point"])
                    await _wait(1.0)
                    result["qr_refreshed"] = True
                elif refresh_qr:
                    result["qr_refreshed"] = False
                # Copy native canvas pixels with a white quiet zone. Do not
                # resize or interpolate QR modules, and never expose this
                # short-lived pairing secret to the LLM or ordinary status.
                image = await workspace.eval(
                    "const qr=document.querySelector('canvas[aria-label*=\"QR\" i], [data-ref] canvas, [data-testid=\"qrcode\"] canvas')||"
                    "(document.querySelectorAll('canvas').length===1?document.querySelector('canvas'):null);"
                    "if(!qr||qr.width<64||qr.height<64)return null;"
                    "const padded=document.createElement('canvas');padded.width=qr.width+40;padded.height=qr.height+40;"
                    "const ctx=padded.getContext('2d');ctx.imageSmoothingEnabled=false;ctx.fillStyle='white';"
                    "ctx.fillRect(0,0,padded.width,padded.height);ctx.drawImage(qr,20,20);"
                    "return padded.toDataURL('image/png').split(',')[1];"
                )
                if image:
                    result["qr_png_base64"] = image
                    result["qr_image_source"] = "native_canvas"
                else:
                    result["diagnosis"] = "cdp_qr_image_not_ready"
        except Exception:
            result["diagnosis"] = "cdp_qr_capture_failed"
        return result


async def search_chats(query: str = "", *, limit: int = 30) -> dict[str, Any]:
    try:
        async with _workspace() as workspace:
            point = await workspace.eval("return rect(search());")
            if query and not point:
                raise WorkspaceError("search_input_missing")
            try:
                if point:
                    # Always reset the box, even for an empty query: a stale
                    # term from a previous read leaves the pane in WhatsApp's
                    # "No chats, contacts or messages found" state and every
                    # listing comes back empty. set_search drives the native
                    # input; type() is the fallback for older pages.
                    if not await workspace.set_search(query):
                        await workspace.type(point, query)
                    await _wait(0.7 if query else 0.4)
                cap = max(1, min(int(limit), 80))
                chats = await workspace.eval(
                    "return rows().map(n=>({chat_ref:name(n),name:name(n),"
                    "unread:!!n.querySelector('[aria-label*=\"unread\" i]'),"
                    "gist:String((n.querySelector('[data-testid=\"cell-frame-secondary\"]')||{}).innerText||'').slice(0,160)}))"
                    f".filter(n=>n.name).slice(0,{cap});"
                )
            finally:
                if query and point:
                    await workspace.type(point, "")
        return {"ok": True, "chats": chats, "complete_history": False,
                "scope": "rendered_sidebar", "driver": "cdp", "background": True, "focus_theft": 0}
    except Exception as exc:
        return _failure(str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__)


async def open_chat(to: str) -> dict[str, Any]:
    try:
        async with _workspace() as workspace:
            display = await workspace.open(to)
        return {"ok": True, "chat_ref": display, "name": display, "background": True,
                "focus_theft": 0, "marks_read": True}
    except Exception as exc:
        return _failure(str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__, to=to)


async def read_recent(to: str, *, limit: int = 20) -> dict[str, Any]:
    try:
        async with _workspace() as workspace:
            display = await workspace.open(to)
            messages = await workspace.read(display, limit)
        return {"ok": True, "to": display, "chat_ref": display, "messages": messages,
                "complete_history": False, "scope": "rendered_thread", "marks_read": True,
                "background": True, "focus_theft": 0, "driver": "cdp"}
    except Exception as exc:
        return _failure(str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__, to=to)


async def search_messages(to: str, query: str, *, limit: int = 30) -> dict[str, Any]:
    result = await read_recent(to, limit=80)
    if not result.get("ok"):
        return result
    wanted = query.casefold()
    result["messages"] = [row for row in result["messages"] if wanted in str(row.get("text") or "").casefold()][:max(1, min(limit, 80))]
    result["query"] = query
    result["scope"] = "rendered_thread_search"
    return result


async def compose(to: str, text: str) -> dict[str, Any]:
    body = text.strip()
    if not body:
        return _failure("empty_message", to=to)
    try:
        async with _workspace() as workspace:
            display = await workspace.open(to)
            await workspace.compose(display, body)
        return {"ok": True, "composed": body, "matched": True, "sent": False,
                "chat_ref": display, "background": True, "focus_theft": 0}
    except Exception as exc:
        return _failure(str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__, to=to)


async def send(to: str, text: str) -> dict[str, Any]:
    """Authorized caller's send. Verify a NEW outgoing ID with exact body.

    This transport does not grant permission. The existing policy/approval
    layer must authorize the exact recipient and text before calling it.
    Once a click has been attempted, uncertainty is never a safe resend.
    """
    body, wanted = text.strip(), to.strip()
    if not body or not wanted:
        return _failure("empty_message", to=wanted)
    attempted = False
    try:
        async with _workspace() as workspace:
            display = await workspace.open(wanted)
            previous = await workspace.read(display, 80)
            old_ids = {str(row.get("id")) for row in previous if row.get("id")}
            await workspace.compose(display, body)
            state = await workspace.state(display)
            if not _same_name(state.get("header", ""), display) or str(state.get("body") or "").strip() != body:
                raise WorkspaceError("recipient_or_draft_changed")
            if not state.get("send"):
                raise WorkspaceError("send_button_missing")
            attempted = True  # Even a disconnect during click is uncertain.
            await workspace.click(state["send"])
            # WhatsApp Web round-trips the row through its own store: under
            # load the new bubble can take well over 6s to render. Poll for up
            # to ~24s before declaring the send unconfirmed.
            for _ in range(40):
                await _wait(0.6)
                current = await workspace.read(display, 80)
                for row in current:
                    identifier = str(row.get("id") or "")
                    exact_body = str(row.get("body") or "").strip() == body
                    if not (identifier and identifier not in old_ids and row.get("from_me") is True and exact_body):
                        continue
                    state_name = str(row.get("transport_state") or "unknown")
                    acknowledged = state_name in {"sent", "delivered", "read"}
                    if state_name == "failed":
                        return _failure("send_failed", to=display, attempted=True)
                    if not acknowledged:
                        # A queued/unknown row proves the client accepted the
                        # draft — not that anything left. Never claim a send;
                        # a click was attempted, so retry is unsafe too.
                        return _failure("send_not_confirmed", to=display, attempted=True)
                    return {
                        "ok": True, "sent": True, "channel": "whatsapp", "to": display,
                        "verified_in_thread": True, "message_id": identifier,
                        "verification": "new_acknowledged_outgoing_row",
                        "accepted_by_client": True,
                        "transport_state": state_name,
                        "delivery_confirmed": bool(row.get("delivery_confirmed"))
                        or state_name in {"delivered", "read"},
                        "background": True, "driver": "cdp", "focus_theft": 0,
                        "send_attempted": True, "retry_safe": False,
                        # A row in the thread proves the client accepted the send;
                        # delivery is WhatsApp's job, so never claim it here.
                        "spoken": f"Sent WhatsApp to {display}.",
                    }
            return _failure("send_not_confirmed", to=display, attempted=True)
    except Exception as exc:
        return _failure(str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__, to=wanted, attempted=attempted)


@asynccontextmanager
async def _transaction_lock():
    """Serialize navigation across both Talk and API processes sharing a profile."""
    import fcntl

    async with _workspace_lock:
        profile = _profile_dir()
        profile.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(profile.parent / f"{profile.name}.transaction.lock", os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            deadline = time.monotonic() + 60.0
            while not acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise WorkspaceError("cdp_workspace_busy") from None
                    await _wait(0.05)
            yield
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


async def _browser_metadata(ws: Any) -> bool:
    """Read-only identity check; returns actual headless mode without UI input."""
    try:
        command = await _cdp(ws, "Browser.getBrowserCommandLine", {}, msg_id=1)
        arguments = command.get("result", {}).get("arguments", [])
    except Exception as exc:
        raise WorkspaceError("cdp_background_not_verified") from exc
    expected = str(_profile_dir().resolve())
    profiles = [str(arg).split("=", 1)[1] for arg in arguments if str(arg).startswith("--user-data-dir=")]
    if not profiles or str(Path(profiles[0]).expanduser().resolve()) != expected:
        raise WorkspaceError("cdp_profile_mismatch")
    return any(str(arg).startswith("--headless") for arg in arguments)


async def _verify_background(ws: Any) -> None:
    """Refuse ordinary commands against a visible or unrelated debugger."""
    if not await _browser_metadata(ws):
        raise WorkspaceError("cdp_foreground_browser")


async def link_qr(*, fresh: bool = False) -> bytes | None:
    import base64

    result = await setup(include_qr=True, refresh_qr=fresh)
    encoded = result.get("qr_png_base64")
    if not encoded:
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    return payload if payload.startswith(b"\x89PNG\r\n\x1a\n") else None
