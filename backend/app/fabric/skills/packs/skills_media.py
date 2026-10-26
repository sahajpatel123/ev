"""Skills: web media (T2) and browser tabs/pages."""

from __future__ import annotations

import json
from typing import Any

from .. import receipts as R
from ..receipts import Recorder
from ..registry import Ctx, skill
from . import web_media as wm
from .browsers import BrowserDriver, BrowserError, Tab, host_excluded
from .common import browsers

NUM = {"type": "number"}
STR = {"type": "string"}


def _wm(ctx: Ctx) -> wm.WebMedia:
    return wm.WebMedia(ctx.rt, browsers(ctx), window_ids=ctx.window_ids, ax=ctx.ax)


def _berr(e: BrowserError) -> dict[str, Any]:
    return R.fail(e.code, wm._say_error(e), fix=e.fix, retryable=e.code in ("timeout", "not_found"))


# --------------------------------------------------------------- web media ----

@skill("web_media.skip_ad", "Skip the ad playing in a browser tab (waits for the skip button, verifies it ended).",
       ["skip the ad", "skip ad", "skip this ad", "skip the advert", "get rid of the ad", "skip the commercial",
        "skip the youtube ad", "press skip on the ad", "skip ads", "skip the add", "skip add", "skip dat ad",
        "skip the advert", "skip that ad", "get rid of that ad", "get rid of the add"],
       risk="low", channels=["dom", "ax", "pointer"], permissions=["automation:browser", "js_from_apple_events"],
       args_schema={"type": "object", "properties": {"url_contains": STR, "max_ads": {"type": "integer"}}})
def skip_ad(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    return _wm(ctx).skip_ad(max_ads=int(args.get("max_ads") or 3), prefer_url=args.get("url_contains"),
                            provenance=ctx.provenance)


@skill("web_media.status", "What media is playing in the browsers, including ad state.",
       ["what is playing", "what's playing", "is anything playing", "which tab is playing", "is there an ad playing",
        "what tab is playing youtube"], risk="read", effect=False, channels=["dom"],
       permissions=["automation:browser", "js_from_apple_events"])
def status(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    return _wm(ctx).status()


@skill("web_media.ad_state", "Is an ad playing, can it be skipped, how long is left.",
       ["is there an ad", "is it an ad", "can i skip this ad", "how long is the ad", "is the ad skippable"],
       risk="read", effect=False, channels=["dom"], permissions=["automation:browser", "js_from_apple_events"])
def ad_state(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    w = _wm(ctx)
    results, errors = w.survey()
    ads = [(b, pr) for b, pr in results if pr.ad.get("active")]
    if not ads:
        if errors and not results:
            return _berr(errors[0])
        return R.receipt(True, verified=True, skill="web_media.ad_state", channel="dom",
                         evidence={"ad": False}, say="No ad is playing.")
    b, pr = ads[0]
    sk = pr.ad.get("skip") or {}
    if sk.get("state") == "ready":
        say = "There's an ad and it can be skipped now."
    elif sk.get("state") == "countdown":
        say = f"There's an ad; it can be skipped in about {int(sk.get('wait_s') or 0)} seconds."
    else:
        rem = pr.ad.get("remaining_s")
        say = "There's an ad that can't be skipped" + (f"; about {int(rem)} seconds left." if rem else ".")
    return R.receipt(True, verified=True, skill="web_media.ad_state", channel="dom",
                     evidence={"ad": pr.ad, "tab": pr.tab.to_dict()}, say=say)


def _control(name: str, action: str, summary: str, triggers: list[str], schema: dict[str, Any] | None = None,
             value_key: str | None = None, scale: float = 1.0, sign: float = 1.0) -> None:
    def run(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
        value = None
        act = action
        if value_key:
            raw = args.get(value_key)
            if raw is None and action not in ("volume_by",):
                return R.fail("invalid_args", f"{name} needs '{value_key}'.")
            value = None if raw is None else float(raw) * scale * sign
        if action == "volume" and "delta" in args:
            act, value = "volume_by", float(args["delta"]) / 100.0
        elif action == "volume" and "level" in args:
            act, value = "volume_to", float(args["level"]) / 100.0
        elif action == "volume":
            return R.fail("invalid_args", "volume needs 'level' (0-100) or 'delta'.")
        if action == "seek" and "to" in args:
            act, value = "seek_to", float(args["to"])
        elif action == "seek":
            act, value = "seek_by", float(args.get("seconds", 0)) * sign
        return _wm(ctx).control(act, value, tab_hint=args.get("url_contains"), provenance=ctx.provenance)

    skill(name, summary, triggers, risk="low", channels=["dom"],
          permissions=["automation:browser", "js_from_apple_events"],
          args_schema=schema or {"type": "object", "properties": {"url_contains": STR}})(run)


_control("web_media.play", "play", "Play the video or audio in the browser.",
         ["play the video", "resume the video", "press play", "resume playback", "unpause", "play it", "press play again"])
_control("web_media.pause", "pause", "Pause the video or audio in the browser.",
         ["pause the video", "pause it", "pause the music in the browser", "stop the video", "pause youtube"])
_control("web_media.seek", "seek", "Jump forward or back in the video.",
         ["go back 10 seconds", "skip forward 30 seconds", "rewind ten seconds", "jump ahead a minute",
          "seek to", "go forward 10 seconds", "back 10 seconds", "jump to seconds", "go to seconds in",
          "forward thirty seconds", "go forward seconds", "skip forward seconds", "go back seconds"],
         {"type": "object", "properties": {"seconds": NUM, "to": NUM, "url_contains": STR}})
_control("web_media.volume", "volume", "Set or change the volume of the video in the browser tab (0-100).",
         ["turn the video up", "turn the video down", "make the video louder", "make the video quieter",
          "lower the video volume", "video volume", "volume of the video", "make the video volume", "volume forty percent"],
         {"type": "object", "properties": {"level": NUM, "delta": NUM, "url_contains": STR}})
_control("web_media.mute", "mute", "Mute the video tab.", ["mute the video", "mute the tab", "mute youtube", "silence the video"])
_control("web_media.unmute", "unmute", "Unmute the video tab.", ["unmute the video", "unmute the tab", "turn the video sound back on", "unmute youtube"])
_control("web_media.next", "next", "Go to the next video or track.", ["next video", "play the next video", "next episode"])
_control("web_media.previous", "previous", "Go to the previous video or track.", ["previous video", "go back a video"])
_control("web_media.captions", "captions", "Toggle captions or subtitles.",
         ["turn on captions", "turn off captions", "toggle subtitles", "captions on", "subtitles"])
_control("web_media.speed", "rate", "Set the playback speed.", ["playback speed", "speed up the video", "play at 1.5x", "slow it down", "one and a half speed", "video speed"],
         {"type": "object", "required": ["rate"], "properties": {"rate": NUM, "url_contains": STR}}, value_key="rate")


# ------------------------------------------------------------------ browser ----

def _all_tabs(ctx: Ctx) -> tuple[list[tuple[BrowserDriver, Tab]], list[BrowserError]]:
    out: list[tuple[BrowserDriver, Tab]] = []
    errs: list[BrowserError] = []
    for b in browsers(ctx):
        try:
            if b.running():
                out.extend((b, t) for t in b.tabs(window_ids=(ctx.window_ids or {}).get(b.kind)))
        except BrowserError as e:
            errs.append(e)
    return out, errs


def _match(tabs: list[tuple[BrowserDriver, Tab]], args: dict[str, Any]) -> list[tuple[BrowserDriver, Tab]]:
    def ok(t: Tab) -> bool:
        if args.get("browser") and t.browser != args["browser"]:
            return False
        if args.get("url_contains") and args["url_contains"].lower() not in t.url.lower():
            return False
        return not (args.get("title_contains") and args["title_contains"].lower() not in t.title.lower())

    return [(b, t) for b, t in tabs if ok(t)]


def _pick(ctx: Ctx, args: dict[str, Any]) -> tuple[BrowserDriver, Tab] | dict[str, Any]:
    tabs, errs = _all_tabs(ctx)
    hits = _match(tabs, args)
    if not hits:
        if errs and not tabs:
            return _berr(errs[0])
        return R.fail("not_found", "I couldn't find a tab matching that.")
    active = [h for h in hits if h[1].active]
    if len(hits) > 1 and not args.get("first") and len(active) != 1:
        return R.fail("ambiguous", f"{len(hits)} tabs match; tell me which one.",
                      evidence={"matches": [t.to_dict() for _, t in hits[:8]]})
    return (active or hits)[0]


@skill("browser.tabs_list", "List open browser tabs (private windows and excluded sites are never shown).",
       ["list my tabs", "what tabs do i have open", "show my browser tabs", "which tabs are open"],
       risk="read", effect=False, channels=["applescript"], permissions=["automation:browser"])
def tabs_list(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    tabs, errs = _all_tabs(ctx)
    if not tabs and errs:
        return _berr(errs[0])
    rows = [t.to_dict() for _, t in tabs]
    return R.receipt(True, verified=True, skill="browser.tabs_list", channel="applescript",
                     evidence={"tabs": rows, "n": len(rows), "untrusted": True},
                     say=f"You have {len(rows)} tab{'s' if len(rows) != 1 else ''} open.")


@skill("browser.tab_find", "Find a browser tab by part of its title or address.",
       ["find the tab", "which tab has", "find my tab with", "where is the tab"],
       risk="read", effect=False, channels=["applescript"], permissions=["automation:browser"],
       args_schema={"type": "object", "properties": {"title_contains": STR, "url_contains": STR, "browser": STR}})
def tab_find(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    tabs, errs = _all_tabs(ctx)
    hits = _match(tabs, args)
    if not hits and errs and not tabs:
        return _berr(errs[0])
    rows = [t.to_dict() for _, t in hits]
    return R.receipt(True, verified=True, skill="browser.tab_find", channel="applescript",
                     evidence={"tabs": rows, "n": len(rows), "untrusted": True},
                     say=(f"I found {len(rows)} matching tab{'s' if len(rows) != 1 else ''}." if rows
                          else "No open tab matches that."))


@skill("browser.tab_open", "Open a web address in a new tab.",
       ["open a new tab", "open this link", "go to this site in a new tab", "open the site", "open a tab to"],
       risk="low", channels=["applescript"], permissions=["automation:browser"],
       args_schema={"type": "object", "required": ["url"],
                    "properties": {"url": STR, "browser": STR, "new_window": {"type": "boolean"}}})
def tab_open(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("browser.tab_open", clock=ctx.rt.monotonic)
    url = str(args["url"])
    if not url.startswith(("http://", "https://")):
        return R.fail("invalid_args", "I only open http and https addresses.")
    if host_excluded(url.split("//", 1)[1].split("/", 1)[0].split(":")[0].lower()):
        return R.fail("denied", "That site is on your protected list, so I won't open it for you.")
    bs = [b for b in browsers(ctx) if not args.get("browser") or b.kind == args["browser"]]
    drv = next((b for b in bs if b.running()), bs[0] if bs else None)
    if drv is None:
        return R.fail("not_found", "No supported browser is installed.")
    try:
        made = drv.open_url(url, new_window=bool(args.get("new_window")))
        rec.add("act", "open url", True, channel="applescript", evidence=made)
        ctx.rt.sleep(0.6)
        found = [t for t in drv.tabs(window_ids=[made["window_id"]], include_excluded=True)
                 if t.tab_id == made["tab_id"] or t.url.startswith(url.split("#")[0][:60])]
    except BrowserError as e:
        return _berr(e)
    ok = bool(found)
    rec.add("verify", "tab present", ok, channel="applescript", evidence={"n": len(found)})
    return rec.finish(ok, evidence={"window_id": made["window_id"], "tab_id": made["tab_id"], "url": url},
                      say="Opened it in a new tab." if ok else None,
                      undo={"skill": "browser.tab_close", "args": {"url_contains": url, "browser": drv.kind,
                                                                  "first": True, "created_by_hands": True},
                            "description": "close the tab that was opened"})


@skill("browser.tab_switch", "Switch to a browser tab by title or address.",
       ["switch to the tab", "go to my tab with", "bring up the tab", "show me the tab", "switch tabs to"],
       risk="low", channels=["applescript"], permissions=["automation:browser"],
       args_schema={"type": "object", "properties": {"title_contains": STR, "url_contains": STR, "browser": STR}})
def tab_switch(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("browser.tab_switch", clock=ctx.rt.monotonic)
    picked = _pick(ctx, args)
    if isinstance(picked, dict):
        return picked
    b, t = picked
    try:
        b.activate_tab(t)
        ctx.rt.sleep(0.3)
        now = [x for x in b.tabs(window_ids=[t.window_id]) if x.active]
    except BrowserError as e:
        return _berr(e)
    ok = bool(now) and now[0].tab_id == t.tab_id and (b.kind != "safari" or now[0].index == t.index)
    rec.add("act", "activate tab", True, channel="applescript")
    rec.add("verify", "tab is active", ok, channel="applescript")
    return rec.finish(ok, evidence={"tab": t.to_dict()}, say="Switched." if ok else None)


def _close_risk(args: dict[str, Any], ctx: Any = None) -> tuple[str, str | None]:
    return ("write", None) if args.get("created_by_hands") else ("write", None)


@skill("browser.tab_close", "Close a browser tab by title or address.",
       ["close the tab", "close that tab", "close my tab with"], risk="dynamic", risk_fn=_close_risk,
       channels=["applescript"], permissions=["automation:browser"],
       args_schema={"type": "object", "properties": {"title_contains": STR, "url_contains": STR, "browser": STR,
                                                      "first": {"type": "boolean"},
                                                      "created_by_hands": {"type": "boolean"}}})
def tab_close(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("browser.tab_close", clock=ctx.rt.monotonic)
    picked = _pick(ctx, args)
    if isinstance(picked, dict):
        return picked
    b, t = picked
    try:
        b.close_tab(t)
        ctx.rt.sleep(0.4)
        left = b.tabs(window_ids=[t.window_id]) if t.window_id in {x.window_id for x in b.tabs()} else []
    except BrowserError as e:
        return _berr(e)
    ok = all(not (x.tab_id == t.tab_id and x.url == t.url) for x in left) if b.kind == "chrome" else \
        len([x for x in left if x.url == t.url]) < 1 or len(left) < t.index
    rec.add("act", "close tab", True, channel="applescript")
    rec.add("verify", "tab gone", ok, channel="applescript")
    return rec.finish(ok, evidence={"closed": t.to_dict()}, say="Closed it." if ok else None,
                      undo={"skill": "browser.tab_open", "args": {"url": t.url, "browser": t.browser},
                            "description": "reopen the address in a new tab"})


@skill("browser.tab_reopen", "Reopen an address in a new tab (what a closed tab was showing).",
       ["reopen the tab", "reopen the closed tab", "bring back the tab i closed"], risk="low",
       channels=["applescript"], permissions=["automation:browser"],
       args_schema={"type": "object", "required": ["url"], "properties": {"url": STR, "browser": STR}})
def tab_reopen(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    out = tab_open(ctx, args)
    out["skill"] = "browser.tab_reopen"
    return out


READ_PAGE_BODY = r"""
var max=__MAX__;var t=((document.body&&document.body.innerText)||'').replace(/\n{3,}/g,'\n\n').trim();
var hs=[].slice.call(document.querySelectorAll('h1,h2,h3')).slice(0,12).map(function(h){return h.innerText.trim().slice(0,100);});
return {url:location.href,title:document.title,text:t.slice(0,max),truncated:t.length>max,headings:hs,chars:t.length};
"""


@skill("browser.read_page", "Read the text of a browser tab (treated as untrusted data).",
       ["read this page", "what does this page say", "summarize this page", "read the tab", "what's on this page"],
       risk="read", effect=False, channels=["dom"], permissions=["automation:browser", "js_from_apple_events"],
       args_schema={"type": "object", "properties": {"title_contains": STR, "url_contains": STR, "browser": STR,
                                                      "max_chars": {"type": "integer"}}})
def read_page(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    args = {**args, "first": True}
    picked = _pick(ctx, args)
    if isinstance(picked, dict):
        return picked
    b, t = picked
    try:
        data = b.js_json(t, READ_PAGE_BODY.replace("__MAX__", str(int(args.get("max_chars") or 6000))))
    except BrowserError as e:
        return _berr(e)
    data["untrusted"] = True
    return R.receipt(True, verified=True, skill="browser.read_page", channel="dom", evidence=data,
                     say=f"I read {data.get('chars', 0)} characters from “{t.title[:40]}”.")


FILL_BODY = r"""
var fields=__FIELDS__;var DENY=/pass(word|code)?|pwd|card|cvv|cvc|ccnum|cc-|security.?code|ssn|social.?sec|secret|token|otp|2fa|pin\b/i;
function lab(el){var id=el.id&&document.querySelector('label[for="'+el.id+'"]');var l=(id&&id.innerText)||(el.closest&&el.closest('label')&&el.closest('label').innerText)||el.getAttribute('aria-label')||el.placeholder||el.name||el.id||'';return l.replace(/\s+/g,' ').trim();}
var els=[].slice.call(document.querySelectorAll('input,textarea,select')).filter(function(e){return e.type!=='hidden'&&e.type!=='submit'&&e.type!=='button';});
var out=[];Object.keys(fields).forEach(function(key){var k=key.toLowerCase();
 var el=els.filter(function(e){var l=lab(e).toLowerCase();return l===k||l.indexOf(k)>=0||(e.name||'').toLowerCase()===k||(e.id||'').toLowerCase()===k;})[0];
 if(!el){out.push({field:key,status:'not_found'});return;}
 var sig=(el.type||'')+' '+lab(el)+' '+(el.name||'')+' '+(el.id||'')+' '+(el.autocomplete||'');
 if(el.type==='password'||DENY.test(sig)){out.push({field:key,status:'denied'});return;}
 var v=fields[key];
 if(el.tagName==='SELECT'){var o=[].slice.call(el.options).filter(function(x){return x.text.toLowerCase()===String(v).toLowerCase()||x.value.toLowerCase()===String(v).toLowerCase();})[0];if(!o){out.push({field:key,status:'no_such_option'});return;}el.value=o.value;}
 else if(el.type==='checkbox'||el.type==='radio'){el.checked=!!v&&String(v).toLowerCase()!=='false';}
 else{var proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,String(v));}
 el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));
 var back=(el.type==='checkbox'||el.type==='radio')?String(el.checked):el.value;
 out.push({field:key,status:'filled',label:lab(el),readback:back});});
return {fields:out};
"""


@skill("browser.fill_form", "Fill benign form fields on a page (never passwords, card numbers or secrets); does not submit.",
       ["fill in the form", "fill out this form", "type my name into the form", "fill the form fields"],
       risk="low", channels=["dom"], permissions=["automation:browser", "js_from_apple_events"],
       args_schema={"type": "object", "required": ["fields"],
                    "properties": {"fields": {"type": "object"}, "title_contains": STR, "url_contains": STR,
                                   "browser": STR}})
def fill_form(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("browser.fill_form", clock=ctx.rt.monotonic)
    picked = _pick(ctx, {**args, "first": True})
    if isinstance(picked, dict):
        return picked
    b, t = picked
    body = FILL_BODY.replace("__FIELDS__", json.dumps(args["fields"]))
    try:
        res = b.js_json(t, body)
    except BrowserError as e:
        return _berr(e)
    rows = res.get("fields", [])
    rec.add("act", "set fields", True, channel="dom", evidence={"fields": rows})
    wanted = {k: str(v) for k, v in args["fields"].items()}
    filled = [r for r in rows if r["status"] == "filled"]
    ok_fields = [r for r in filled if str(r["readback"]).lower() == wanted[r["field"]].lower()
                 or r["readback"] in ("true", "false")]
    bad = [r for r in rows if r["status"] != "filled"]
    rec.add("verify", "read values back", len(ok_fields) == len(filled) and bool(filled), channel="dom")
    ok = bool(filled) and len(ok_fields) == len(filled) and not bad
    say: str | None
    if bad and filled:
        names = ", ".join(f"{r['field']} ({r['status'].replace('_', ' ')})" for r in bad)
        say = f"I filled {len(filled)} field{'s' if len(filled) != 1 else ''}, but not: {names}."
    elif ok:
        say = f"Filled in {len(filled)} field{'s' if len(filled) != 1 else ''}."
    else:
        say = "I couldn't fill any of those fields." if not filled else None
    denied = [r for r in bad if r["status"] == "denied"]
    err = None if ok else R.error("denied" if denied and not filled else "not_found",
                                  "some fields were not filled", retryable=False)
    return rec.finish(ok, evidence={"fields": rows}, say=say, err=err)


@skill("browser.reopen_from_screenshot", "Reopen the tabs shown in a screenshot of a browser tab bar.",
       ["reopen the tabs from the screenshot", "open the tabs in my screenshot", "restore my tabs from the screenshot"],
       risk="confirm", channels=["ocr", "applescript"], permissions=["automation:browser", "screen_recording"],
       args_schema={"type": "object", "properties": {"image": STR, "browser": STR}},
       plan=lambda ctx, args: __import__("app.fabric.skills.screenref", fromlist=["x"]).browser_tabs_plan(ctx, args))
def reopen_from_screenshot(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    from .. import screenref

    return screenref.browser_tabs_apply(ctx, args)
