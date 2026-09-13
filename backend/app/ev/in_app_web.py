"""Browser-tab driver for in-app item actions.

Opens a named item (chat, channel, note, playlist) inside a web app that is
already signed in in Chrome. It drives the existing tab with page JavaScript:
no EV.app session, no new foreground window, and no focus theft unless the
owner explicitly asked the app to come forward (``background=False``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from app.digital.chrome_session import (
    chrome_running,
    eval_in_tab,
    focus_tab,
    open_background_tab,
    wrap_js,
)

VERIFY_ATTEMPTS = 6
VERIFY_SLEEP = 0.8
TAB_WAIT_ATTEMPTS = 10
TAB_WAIT_SLEEP = 1.0
READY_WAIT_ATTEMPTS = 15
READY_WAIT_SLEEP = 1.0

_READY_WHATSAPP_JS = r"""
function si(){
  return document.querySelector('input[aria-label="Search or start a new chat"]')
    || document.querySelector('[data-testid="chat-list-search-container"] input')
    || document.querySelector('#side input[type="text"]')
    || document.querySelector('#side [contenteditable="true"]');
}
return JSON.stringify({ok:true, ready: !!si()});
"""

_READY_GENERIC_JS = r"""
function si(){
  return document.querySelector('input[type="search"]')
    || document.querySelector('input[placeholder*="Search" i]')
    || document.querySelector('[role="searchbox"]')
    || document.querySelector('h1')
    || document.querySelector('[role="heading"]');
}
return JSON.stringify({ok:true, ready: !!si()});
"""


@dataclass(frozen=True)
class WebProfile:
    label: str
    url_contains: str
    url: str | None = None
    kind: str = "generic"
    search_selectors: tuple[str, ...] = ()
    title_selectors: tuple[str, ...] = ()
    row_selectors: tuple[str, ...] = ()


WHATSAPP = WebProfile(
    label="WhatsApp",
    url_contains="web.whatsapp.com",
    url="https://web.whatsapp.com/",
    kind="whatsapp",
    search_selectors=(
        'input[aria-label="Search or start a new chat"]',
        '[data-testid="chat-list-search-container"] input',
        '[data-testid="chat-list-search"]',
        '#side input[type="text"]',
    ),
    title_selectors=(
        '[data-testid="conversation-info-header-chat-title"]',
    ),
    row_selectors=(
        "#pane-side [role=\"listitem\"]",
        "[data-testid=\"cell-frame-container\"]",
        "[aria-label=\"Chat list\"] [role=\"listitem\"]",
    ),
)

SLACK = WebProfile(
    label="Slack",
    url_contains="app.slack.com",
    url="https://app.slack.com/client",
    kind="channel",
    search_selectors=(
        '[data-qa="top_nav_search"]',
        'button[aria-label*="Search" i]',
        'input[aria-label*="Search" i]',
    ),
    title_selectors=(
        '[data-qa="channel_name"]',
        '[data-qa="channel_header"]',
    ),
    row_selectors=(
        '[data-qa="channel_sidebar_name_button"]',
        '[role="treeitem"]',
    ),
)

TELEGRAM = WebProfile(
    label="Telegram",
    url_contains="web.telegram.org",
    url="https://web.telegram.org/",
    kind="chat",
    search_selectors=(
        'input[placeholder*="Search" i]',
        '[contenteditable="true"][data-placeholder*="Search" i]',
    ),
    title_selectors=(
        ".chat-info .peer-title",
        ".Topbar .peer-title",
    ),
    row_selectors=(
        ".chatlist-chat",
        ".ListItem",
    ),
)

GENERIC = WebProfile(
    label="Web",
    url_contains="",
    kind="generic",
)

PROFILES: dict[str, WebProfile] = {
    WHATSAPP.label: WHATSAPP,
    SLACK.label: SLACK,
    TELEGRAM.label: TELEGRAM,
}


def profile_for(app_label: str) -> WebProfile | None:
    return PROFILES.get((app_label or "").strip())


_GENERIC_OPEN_JS = r"""
const want = __ITEM__;
const searchSels = __SEARCH__;
const titleSels = __TITLE__;
const rowSels = __ROWS__;
function q(sel){ try { return document.querySelector(sel); } catch(e) { return null; } }
function qa(sel){ try { return Array.from(document.querySelectorAll(sel)); } catch(e) { return []; } }
function searchInput(){
  for (const sel of searchSels) { const el = q(sel); if (el) return el; }
  return document.querySelector('input[type="search"]')
    || document.querySelector('input[placeholder*="Search" i]')
    || document.querySelector('[role="searchbox"]')
    || document.querySelector('input[aria-label*="search" i]');
}
function titleEl(){
  for (const sel of titleSels) { const el = q(sel); if (el) return el; }
  return document.querySelector('h1') || document.querySelector('[role="heading"]') || document.querySelector('header h2');
}
function rows(){
  for (const sel of rowSels) { const list = qa(sel); if (list.length) return list; }
  return qa('[role="listitem"], li, [role="row"]').slice(0, 150);
}
function setValue(el, v){
  el.focus();
  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(el, v);
  } else { el.textContent = v; }
  el.dispatchEvent(new InputEvent('input', {bubbles:true, data:v, inputType:'insertText'}));
  el.dispatchEvent(new Event('change', {bubbles:true}));
}
function pressEnter(el){
  for (const type of ['keydown','keypress','keyup']) {
    el.dispatchEvent(new KeyboardEvent(type, {key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true, cancelable:true}));
  }
}
const t = titleEl();
if (t && (t.innerText||'').toLowerCase().includes(want.toLowerCase())) {
  return JSON.stringify({ok:true, matched:true, already:true, title:(t.innerText||'').trim(), activated:false, focus_theft:0});
}
const input = searchInput();
if (input) {
  setValue(input, want);
  pressEnter(document.activeElement || input);
  return JSON.stringify({ok:true, typed:true, activated:false, focus_theft:0});
}
const hit = rows().find((n) => (n.innerText||'').toLowerCase().includes(want.toLowerCase()));
if (hit) { hit.click(); return JSON.stringify({ok:true, clicked:true, activated:false, focus_theft:0}); }
return JSON.stringify({ok:false, error:'item_control_missing', diagnosis:'ui_changed', activated:false, focus_theft:0});
"""

_GENERIC_VERIFY_JS = r"""
const want = __ITEM__;
const titleSels = __TITLE__;
const rowSels = __ROWS__;
function q(sel){ try { return document.querySelector(sel); } catch(e) { return null; } }
function qa(sel){ try { return Array.from(document.querySelectorAll(sel)); } catch(e) { return []; } }
function titleText(){
  for (const sel of titleSels) { const el = q(sel); if (el) return (el.innerText||'').trim(); }
  const el = document.querySelector('h1') || document.querySelector('[role="heading"]') || document.querySelector('header h2');
  return el ? (el.innerText||'').trim() : '';
}
const title = titleText();
let matched = !!title && title.toLowerCase().includes(want.toLowerCase());
let selected = '';
if (!matched) {
  for (const sel of rowSels) {
    const hit = qa(sel).find((n) => {
      const aria = String(n.getAttribute('aria-selected') || n.getAttribute('aria-current') || '');
      return aria === 'true' || aria === 'page';
    });
    if (hit) { selected = (hit.innerText||'').trim(); break; }
  }
  matched = !!selected && selected.toLowerCase().includes(want.toLowerCase());
}
return JSON.stringify({ok:true, matched:matched, title:title, selected:selected, activated:false, focus_theft:0});
"""

_GENERIC_CLICK_ROW_JS = r"""
const want = __ITEM__;
const rowSels = __ROWS__;
function qa(sel){ try { return Array.from(document.querySelectorAll(sel)); } catch(e) { return []; } }
let rows = [];
for (const sel of rowSels) { const list = qa(sel); if (list.length) { rows = list; break; } }
if (!rows.length) rows = qa('[role="listitem"], li, [role="row"]');
const hit = rows.find((n) => (n.innerText||'').toLowerCase().includes(want.toLowerCase()));
if (!hit) return JSON.stringify({ok:false, error:'item_not_found', activated:false, focus_theft:0});
hit.click();
return JSON.stringify({ok:true, clicked:true, activated:false, focus_theft:0});
"""


def _render(template: str, item: str, profile: WebProfile) -> str:
    return (
        template.replace("__ITEM__", json.dumps(item))
        .replace("__SEARCH__", json.dumps(list(profile.search_selectors)))
        .replace("__TITLE__", json.dumps(list(profile.title_selectors)))
        .replace("__ROWS__", json.dumps(list(profile.row_selectors)))
    )


async def _tab_present(url_contains: str) -> bool:
    probe = await eval_in_tab(
        url_contains=url_contains,
        javascript=wrap_js("return JSON.stringify({ok:true, present:true});"),
        timeout=10.0,
    )
    return bool(probe.get("ok") and probe.get("present"))


async def _wait_for_tab(url_contains: str) -> bool:
    for _ in range(TAB_WAIT_ATTEMPTS):
        if await _tab_present(url_contains):
            return True
        await asyncio.sleep(TAB_WAIT_SLEEP)
    return False


async def _wait_for_ready(profile: WebProfile) -> bool:
    """Cold Chrome renders after the tab exists; drive only once it can respond."""

    script = _READY_WHATSAPP_JS if profile.kind == "whatsapp" else _READY_GENERIC_JS
    for _ in range(READY_WAIT_ATTEMPTS):
        probe = await eval_in_tab(
            url_contains=profile.url_contains, javascript=wrap_js(script)
        )
        if probe.get("ready"):
            return True
        await asyncio.sleep(READY_WAIT_SLEEP)
    return False


async def _drive_whatsapp(item: str) -> dict[str, Any]:
    from app.digital import whatsapp_js as wjs

    result = await eval_in_tab(
        url_contains=WHATSAPP.url_contains,
        javascript=wrap_js(wjs.open_chat_js(item)),
    )
    if result.get("ok") and (result.get("already") or result.get("opened")):
        await eval_in_tab(
            url_contains=WHATSAPP.url_contains,
            javascript=wrap_js(wjs.type_search_js("")),
        )
        return {
            "ok": True,
            "matched": True,
            "title": str(result.get("name") or item),
            "driver": "web_tab",
        }
    if result.get("ok") and result.get("entered"):
        # Enter was pressed; give the pane one more beat, then verify.
        for _ in range(VERIFY_ATTEMPTS):
            await asyncio.sleep(VERIFY_SLEEP)
            again = await eval_in_tab(
                url_contains=WHATSAPP.url_contains,
                javascript=wrap_js(wjs.open_chat_js(item)),
            )
            if again.get("ok") and again.get("already"):
                await eval_in_tab(
                    url_contains=WHATSAPP.url_contains,
                    javascript=wrap_js(wjs.type_search_js("")),
                )
                return {
                    "ok": True,
                    "matched": True,
                    "title": str(again.get("name") or item),
                    "driver": "web_tab",
                }
        await eval_in_tab(
            url_contains=WHATSAPP.url_contains,
            javascript=wrap_js(wjs.type_search_js("")),
        )
    return {
        "ok": False,
        "error": str(result.get("error") or "chat_open_unverified"),
        "driver": "web_tab",
    }


async def _drive_generic(item: str, profile: WebProfile) -> dict[str, Any]:
    opened = await eval_in_tab(
        url_contains=profile.url_contains,
        javascript=wrap_js(_render(_GENERIC_OPEN_JS, item, profile)),
    )
    if opened.get("matched"):
        return {
            "ok": True,
            "matched": True,
            "title": str(opened.get("title") or item),
            "driver": "web_tab",
        }
    verify = _render(_GENERIC_VERIFY_JS, item, profile)
    for _ in range(VERIFY_ATTEMPTS):
        await asyncio.sleep(VERIFY_SLEEP)
        check = await eval_in_tab(
            url_contains=profile.url_contains, javascript=wrap_js(verify)
        )
        if check.get("matched"):
            return {
                "ok": True,
                "matched": True,
                "title": str(check.get("title") or check.get("selected") or item),
                "driver": "web_tab",
            }
    if opened.get("typed"):
        # Search+Enter did not land on the item; a direct list click is the
        # last in-page gesture before refusing.
        click = await eval_in_tab(
            url_contains=profile.url_contains,
            javascript=wrap_js(_render(_GENERIC_CLICK_ROW_JS, item, profile)),
        )
        if click.get("clicked"):
            for _ in range(VERIFY_ATTEMPTS):
                await asyncio.sleep(VERIFY_SLEEP)
                check = await eval_in_tab(
                    url_contains=profile.url_contains, javascript=wrap_js(verify)
                )
                if check.get("matched"):
                    return {
                        "ok": True,
                        "matched": True,
                        "title": str(check.get("title") or check.get("selected") or item),
                        "driver": "web_tab",
                    }
    return {
        "ok": False,
        "error": str(opened.get("error") or "item_not_found"),
        "driver": "web_tab",
    }


async def _launch_chrome_background(url: str) -> bool:
    """Start Chrome (or just the URL) without bringing it to the front."""

    import shutil

    if not shutil.which("open"):
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/open",
            "-g",
            "-a",
            "Google Chrome",
            url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, _err = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except Exception:
        return False
    return proc.returncode == 0


async def drive_item_in_web(
    app_label: str,
    item: str,
    *,
    kind: str | None = None,
    background: bool = True,
) -> dict[str, Any] | None:
    """Open one named item in the app's existing browser tab, or None when
    this app has no browser profile on this Mac."""

    profile = profile_for(app_label)
    if profile is None:
        return None
    created_window = False
    if not await chrome_running():
        # Chrome may simply be closed: start it in the background, then drive.
        if not profile.url or not await _launch_chrome_background(profile.url):
            return None
        if not await _wait_for_tab(profile.url_contains):
            return None
    elif not await _tab_present(profile.url_contains):
        if not profile.url:
            return None
        opened = await open_background_tab(profile.url)
        if not opened.get("ok"):
            return None
        created_window = bool(opened.get("created_window"))
        if not await _wait_for_tab(profile.url_contains):
            return None
        await asyncio.sleep(TAB_WAIT_SLEEP)
    if not await _wait_for_ready(profile):
        return None
    del kind
    if profile.kind == "whatsapp":
        result = await _drive_whatsapp(item)
    else:
        result = await _drive_generic(item, profile)
    focused = False
    if result.get("ok") and not background:
        focus = await focus_tab(url_contains=profile.url_contains)
        focused = bool(focus.get("ok"))
    return {
        **result,
        "app": profile.label,
        "item": item,
        "focused": focused,
        "created_window": created_window,
        "focus_theft": 1 if focused else 0,
    }
