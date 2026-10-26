"""UI primitives: find / act / wait_until / observe / verify (SKILLS_API §3).

Element identity is a SEMANTIC selector (role + name + ancestry + nth), re-located
at every act so a UI that shifted between find and act never hits the wrong
thing. The channel ladder is applied in order and every receipt names the rung:
browser DOM -> Accessibility tree -> keyboard -> trusted pointer on a located
element -> OCR-located target. Raw coordinates are never an identity.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from . import receipts as R
from .ax import AxClient
from .packs.browsers import BrowserDriver, BrowserError, Tab
from .packs.common import ax as ax_of
from .packs.common import browsers as browsers_of
from .packs.common import state_dir
from .receipts import Recorder
from .registry import Ctx
from .runtime import applescript_quote, process_running

ROLE_AX = {"button": "AXButton", "link": "AXLink", "textbox": "AXTextField", "textarea": "AXTextArea",
           "checkbox": "AXCheckBox", "radio": "AXRadioButton", "menuitem": "AXMenuItem", "tab": "AXRadioButton",
           "combobox": "AXPopUpButton", "heading": "AXHeading", "text": "AXStaticText", "image": "AXImage",
           "slider": "AXSlider", "row": "AXRow", "window": "AXWindow"}

DOM_PRELUDE = r"""
function allEls(root){var out=[],stack=[root],n=0;while(stack.length&&n<40000){var cur=stack.pop();var kids=cur.children||[];for(var i=kids.length-1;i>=0;i--){var c=kids[i];out.push(c);n++;stack.push(c);if(c.shadowRoot)stack.push(c.shadowRoot);}}return out;}
function vis(el){if(!el||!el.getBoundingClientRect)return false;var r=el.getBoundingClientRect();if(r.width<2||r.height<2)return false;var cs=getComputedStyle(el);return !(cs.visibility==='hidden'||cs.display==='none'||parseFloat(cs.opacity)===0);}
function roleOf(e){var r=e.getAttribute&&e.getAttribute('role');if(r)return r.toLowerCase();var t=e.tagName;if(t==='BUTTON'||t==='AD-CONTROLS'||t==='SUMMARY')return'button';if(t==='A'&&e.getAttribute('href')!==null)return'link';if(t==='INPUT'){var ty=(e.type||'text').toLowerCase();return ty==='checkbox'?'checkbox':ty==='radio'?'radio':(ty==='submit'||ty==='button')?'button':'textbox';}if(t==='TEXTAREA')return'textbox';if(t==='SELECT')return'combobox';if(/^H[1-6]$/.test(t))return'heading';if(t==='IMG')return'image';return'';}
function nameOf(e){var a=e.getAttribute&&(e.getAttribute('aria-label')||'');if(a)return a.trim();var lb=e.getAttribute&&e.getAttribute('aria-labelledby');if(lb){var x=document.getElementById(lb);if(x)return(x.textContent||'').trim();}
 if(e.id){var l=document.querySelector('label[for="'+e.id+'"]');if(l)return(l.textContent||'').replace(/\s+/g,' ').trim();}
 var al=e.getAttribute&&(e.getAttribute('alt')||e.getAttribute('title')||e.getAttribute('placeholder')||'');if(al&&al.trim())return al.trim();
 if(e.shadowRoot){var s=(e.shadowRoot.textContent||'').replace(/\s+/g,' ').trim();if(s)return s;}
 var t=((e.innerText||e.textContent||'')+'').replace(/\s+/g,' ').trim();if(t)return t.slice(0,120);return (e.value||e.name||'')+'';}
function ancestryOf(e){var out=[];var p=e.parentElement;for(var i=0;i<6&&p;i++){var r=roleOf(p),n=(p.getAttribute&&(p.getAttribute('aria-label')||p.id))||'';if(r||n)out.push((r+':'+n).toLowerCase());p=p.parentElement;}return out;}
function findAll(sel){var els=allEls(document.documentElement);var want=(sel.name||'').toLowerCase(),txt=(sel.text||'').toLowerCase();var hits=[];
 els.forEach(function(e){if(!e.getAttribute||!vis(e))return;var r=roleOf(e);if(sel.role&&r!==sel.role.toLowerCase())return;if(!sel.role&&!r&&!txt)return;
  var n=nameOf(e),nl=n.toLowerCase();if(want){if(sel.exact?nl!==want:nl.indexOf(want)<0)return;}
  if(txt){var tt=((e.innerText||e.textContent||'')+'').toLowerCase();if(tt.indexOf(txt)<0)return;}
  if(sel.ancestry&&sel.ancestry.length){var an=ancestryOf(e).join('|');if(!sel.ancestry.every(function(a){return an.indexOf(String(a).toLowerCase())>=0;}))return;}
  hits.push(e);});return hits;}
function describe(e,i){var r=e.getBoundingClientRect();return {role:roleOf(e),name:nameOf(e).slice(0,100),value:(e.value!==undefined?String(e.value).slice(0,80):null),enabled:!(e.disabled||e.getAttribute('aria-disabled')==='true'),frame:[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)],nth:i,checked:(e.checked!==undefined?!!e.checked:null),secure:(e.type||'')==='password'};}
"""

DOM_FIND_BODY = DOM_PRELUDE + r"""
var sel=__SEL__;var hits=findAll(sel);return {n:hits.length,matches:hits.slice(0,__LIMIT__).map(describe)};
"""

DOM_ACT_BODY = DOM_PRELUDE + r"""
var sel=__SEL__,act=__ACT__,val=__VAL__;var hits=findAll(sel);var e=hits[sel.nth||0];if(!e)return {ok:false,error:'not_found',n:hits.length};
if(e.type==='password'&&(act==='type'||act==='set_value'))return {ok:false,error:'denied'};
function setv(el,v){var proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,String(v));el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}
if(act==='press'){if(e.scrollIntoView)e.scrollIntoView({block:'nearest'});var tgt=(e.shadowRoot&&e.shadowRoot.querySelector('button,a,[role=button]'))||e;tgt.click();return {ok:true,after:describe(e,sel.nth||0)};}
if(act==='focus'){e.focus();return {ok:true,after:describe(e,sel.nth||0)};}
if(act==='type'||act==='set_value'){e.focus();setv(e,val);return {ok:true,after:describe(e,sel.nth||0)};}
if(act==='select'){if(e.tagName==='SELECT'){var o=[].slice.call(e.options).filter(function(x){return x.text.toLowerCase()===String(val).toLowerCase()||x.value.toLowerCase()===String(val).toLowerCase();})[0];if(!o)return {ok:false,error:'no_such_option'};e.value=o.value;e.dispatchEvent(new Event('change',{bubbles:true}));return {ok:true,after:describe(e,sel.nth||0)};}e.click();return {ok:true,after:describe(e,sel.nth||0)};}
if(act==='scroll'){if(e.scrollIntoView)e.scrollIntoView({block:'center'});else window.scrollBy(0,Number(val)||400);return {ok:true,after:describe(e,sel.nth||0)};}
return {ok:false,error:'unknown_action'};
"""

DOM_SCROLL_BODY = r"""window.scrollBy(0,__DY__);return {ok:true,y:Math.round(window.scrollY)};"""

DOM_OUTLINE_BODY = DOM_PRELUDE + r"""
var els=allEls(document.documentElement).filter(function(e){return e.getAttribute&&vis(e)&&roleOf(e);}).slice(0,__LIMIT__);
return {url:location.href,title:document.title,elements:els.map(function(e,i){var d=describe(e,i);d.i=i;return d;})};
"""

DOM_EXPR_BODY = r"""var v=(function(){return (__EXPR__);})();return {value:v===undefined?null:v};"""


class UI:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.rt = ctx.rt
        self.ax: AxClient = ax_of(ctx)
        self._obs: dict[str, dict[str, Any]] = {}

    # -- tab resolution -------------------------------------------------------------
    def _tab(self, sel: dict[str, Any]) -> tuple[BrowserDriver, Tab] | None:
        want = sel.get("tab") or {}
        browser = sel.get("browser")
        app = str(sel.get("app") or "")
        for b in browsers_of(self.ctx):
            if browser and b.kind != browser:
                continue
            if not browser and app and b.app.lower() != app.lower():
                continue
            if not (browser or app) and not want:
                continue
            try:
                if not b.running():
                    continue
                tabs = b.tabs(window_ids=(self.ctx.window_ids or {}).get(b.kind))
            except BrowserError:
                continue
            for t in tabs:
                if want.get("url_contains") and want["url_contains"].lower() not in t.url.lower():
                    continue
                if want.get("title_contains") and want["title_contains"].lower() not in t.title.lower():
                    continue
                if want or t.active:
                    return b, t
            if not want and tabs:
                return b, tabs[0]
        return None

    def _channels(self, sel: dict[str, Any], channels: list[str] | None) -> list[str]:
        if channels:
            return channels
        order = []
        if sel.get("browser") or sel.get("tab") or any(
                b.app.lower() == str(sel.get("app") or "").lower() for b in browsers_of(self.ctx)):
            order.append("dom")
        order += ["ax", "ocr"]
        return order

    # -- find -----------------------------------------------------------------------
    def find(self, selector: dict[str, Any], *, channels: list[str] | None = None, limit: int = 10) -> dict[str, Any]:
        rec = Recorder("ui.find", clock=self.rt.monotonic)
        matches: list[dict[str, Any]] = []
        tried: list[str] = []
        err: dict[str, Any] | None = None
        for ch in self._channels(selector, channels):
            tried.append(ch)
            try:
                found = {"dom": self._find_dom, "ax": self._find_ax, "ocr": self._find_ocr}[ch](selector, limit)
            except BrowserError as e:
                err = R.error(e.code, e.message, fix=e.fix)
                found = []
            rec.add("perceive", f"find via {ch}", bool(found), channel=ch, evidence={"n": len(found)})
            if found:
                matches = found
                break
        if not matches:
            return rec.finish(False, evidence={"tried": tried, "selector": selector},
                              err=err or R.error("not_found", "No element matches that selector."),
                              say="I couldn't find that on screen.")
        obs = f"obs-{uuid.uuid4().hex[:8]}"
        for i, m in enumerate(matches):
            m["ref"] = f"{obs}:{i}"
        self._obs[obs] = {"at": self.rt.monotonic(), "selector": selector, "matches": matches}
        return rec.finish(True, verified=True, evidence={"matches": matches, "observation_id": obs,
                                                         "channel": matches[0]["source"]},
                          say=None)

    def _find_dom(self, sel: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        got = self._tab(sel)
        if not got:
            return []
        b, t = got
        spec = {k: sel[k] for k in ("role", "name", "text", "exact", "ancestry") if k in sel}
        res = b.js_json(t, DOM_FIND_BODY.replace("__SEL__", json.dumps(spec)).replace("__LIMIT__", str(limit)))
        out = []
        for i, m in enumerate(res.get("matches", [])):
            if m.get("secure"):
                m["value"] = None
            out.append({"source": "dom", "role": m["role"], "name": m["name"], "value": m.get("value"),
                        "enabled": m.get("enabled"), "frame": m["frame"], "confidence": 0.95,
                        "selector": {**sel, "nth": m["nth"]}, "locator": {"tab": t.to_dict(), "browser": b.kind}})
        return out

    def _find_ax(self, sel: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        app = sel.get("app")
        if not app or not self.ax.available():
            return []
        role = ROLE_AX.get(str(sel.get("role") or "").lower(), sel.get("role") if str(sel.get("role", "")).startswith("AX") else None)
        name = sel.get("name") or sel.get("text")
        pattern = None
        if name:
            pattern = f"^{re.escape(str(name))}$" if sel.get("exact") else re.escape(str(name))
        if not role and not pattern:
            return []
        win = (sel.get("window") or {}).get("title")
        res = self.ax.find(app, role=role, name=pattern, title=win, max_hits=limit)
        if not res.get("ok"):
            err = res.get("error") or {}
            if err.get("code") == "permission_missing":
                raise BrowserError("permission_missing", err.get("message", "Accessibility is not granted"), err.get("fix"))
            return []
        out = []
        for i, m in enumerate(res.get("matches", [])):
            pos, size = m.get("pos") or [0, 0], m.get("size") or [0, 0]
            out.append({"source": "ax", "role": m.get("role"), "name": m.get("title") or m.get("desc") or "",
                        "value": None if m.get("role") == "AXSecureTextField" else m.get("value"),
                        "enabled": m.get("enabled"), "frame": [pos[0], pos[1], size[0], size[1]],
                        "confidence": 0.9, "selector": {**sel, "nth": i},
                        "locator": {"app": app, "path": m["path"], "actions": m.get("actions", [])}})
        return out

    def _find_ocr(self, sel: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        text = sel.get("text") or sel.get("name")
        if not text or self.ctx.ocr_fn is None:
            return []
        shot = self._capture(sel)
        if not shot:
            return []
        res = self.ctx.ocr_fn(Path(shot["path"]))
        if not res.get("ok"):
            return []
        out = []
        want = str(text).lower()
        for ln in res.get("lines", []):
            if want in str(ln.get("text", "")).lower():
                b = ln.get("bounding_box") or {}
                fx, fy, fw, fh = shot["frame"]
                out.append({"source": "ocr", "role": "text", "name": ln["text"], "value": None, "enabled": True,
                            "frame": [fx + b.get("x", 0) * fw, fy + b.get("y", 0) * fh,
                                      b.get("width", 0) * fw, b.get("height", 0) * fh],
                            "confidence": round(float(ln.get("confidence", 0.7)) * 0.8, 3),
                            "selector": {**sel, "nth": len(out)}, "locator": {"shot": shot["path"]}})
                if len(out) >= limit:
                    break
        return out

    def _capture(self, sel: dict[str, Any]) -> dict[str, Any] | None:
        if not self.ax.available():
            return None
        app = sel.get("app")
        wins = self.ax.call("cgwindows", *(["--owner", app] if app else []))
        rows = [w for w in wins.get("windows", []) if w.get("layer") == 0]
        if not rows:
            return None
        w = rows[0]
        out = state_dir(self.ctx) / "obs"
        out.mkdir(exist_ok=True)
        path = out / f"{uuid.uuid4().hex[:10]}.png"
        p = self.rt.run(["screencapture", "-x", "-o", "-l", str(w["number"]), str(path)], timeout=15)
        return {"path": str(path), "frame": [w["x"], w["y"], w["w"], w["h"]]} if p.ok and path.exists() else None

    # -- act ----------------------------------------------------------------------------
    def _resolve(self, target: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(match, error receipt). A ref is only a hint: the selector is re-found."""
        if isinstance(target, str):
            obs, _, idx = target.partition(":")
            row = self._obs.get(obs)
            if not row:
                return None, R.fail("stale_ref", "That reference is no longer valid; find it again.")
            try:
                m = row["matches"][int(idx)]
            except (ValueError, IndexError):
                return None, R.fail("stale_ref", "That reference is no longer valid; find it again.")
            sel = dict(m["selector"])
            ch = [m["source"]]
        elif isinstance(target, dict):
            sel, ch = dict(target), None
        else:
            return None, R.fail("invalid_args", "target must be a ref or a selector.")
        found = self.find(sel, channels=ch, limit=max(5, int(sel.get("nth", 0)) + 1))
        if not found["ok"]:
            return None, R.fail("stale_ref" if isinstance(target, str) else "not_found",
                                "I can't find that element any more." if isinstance(target, str)
                                else "No element matches that selector.", evidence=found["evidence"])
        ms = found["evidence"]["matches"]
        nth = int(sel.get("nth") or 0)
        return (ms[nth] if nth < len(ms) else ms[0]), None

    def act(self, target: Any, action: str, args: dict[str, Any] | None = None, *,
            verify: dict[str, Any] | None = None, restore_focus: bool = True) -> dict[str, Any]:
        args = args or {}
        rec = Recorder(f"ui.{action}", provenance=self.ctx.provenance, clock=self.rt.monotonic)
        if action not in {"press", "type", "set_value", "scroll", "select", "menu", "key", "focus", "pointer"}:
            return R.fail("invalid_args", f"Unknown action {action!r}.")
        front_before = self._front_app() if restore_focus else None
        if action == "menu":
            res = self._menu(args, rec)
        elif action == "key":
            res = self._key(args, rec)
        else:
            m, err = self._resolve(target)
            if err:
                return err
            rec.add("resolve", "re-locate element", True, channel=m["source"],
                    evidence={"role": m["role"], "name": m["name"]})
            if m.get("role") in ("AXSecureTextField",) and action in ("type", "set_value"):
                return R.fail("denied", "I won't type into a secure field.")
            res = self._do(m, action, args, rec)
        if not res["ok"]:
            return rec.finish(False, err=R.error(res.get("code", "internal"), res.get("message", "action failed"),
                                                 fix=res.get("fix")), evidence=res.get("evidence"),
                              say=res.get("message"))
        ok = True
        if verify:
            chk = self.verify(verify)
            ok = bool(chk["ok"])
            rec.add("verify", "postcondition", ok, channel=chk.get("channel"), evidence=chk.get("evidence"))
        if restore_focus and front_before and action not in ("focus",):
            after = self._front_app()
            if after and after != front_before:
                self.rt.osascript(f'tell application {applescript_quote(front_before)} to activate', timeout=6)
                rec.add("recover", "restore previous app to the front", True, channel="applescript",
                        evidence={"from": after, "to": front_before})
        return rec.finish(ok, verified=ok and bool(verify), evidence=res.get("evidence"),
                          err=None if ok else R.error("unverified", "the postcondition did not hold"))

    def _front_app(self) -> str | None:
        front = (self.rt.run(["lsappinfo", "front"], timeout=5).out or "").strip()
        if not front:
            return None
        info = self.rt.run(["lsappinfo", "info", "-only", "name", front], timeout=5).out or ""
        m = re.match(r'\s*"([^"]*)"\s+ASN', info)
        return m.group(1) if m else None

    def _do(self, m: dict[str, Any], action: str, args: dict[str, Any], rec: Recorder) -> dict[str, Any]:
        src = m["source"]
        if src == "dom":
            loc = m["locator"]
            b = next((x for x in browsers_of(self.ctx) if x.kind == loc["browser"]), None)
            t = Tab(**{k: loc["tab"][k] for k in ("browser", "window_id", "tab_id", "index", "title", "url", "active")})
            spec = {k: m["selector"][k] for k in ("role", "name", "text", "exact", "ancestry", "nth") if k in m["selector"]}
            body = (DOM_ACT_BODY.replace("__SEL__", json.dumps(spec)).replace("__ACT__", json.dumps(action))
                    .replace("__VAL__", json.dumps(args.get("text", args.get("value")))))
            try:
                res = b.js_json(t, body) if b else {"ok": False, "error": "browser gone"}
            except BrowserError as e:
                return {"ok": False, "code": e.code, "message": e.message, "fix": e.fix}
            rec.add("act", f"dom {action}", bool(res.get("ok")), channel="dom", evidence=res)
            if not res.get("ok"):
                return {"ok": False, "code": "not_found" if res.get("error") == "not_found" else
                        ("denied" if res.get("error") == "denied" else "internal"),
                        "message": f"I couldn't {action} that ({res.get('error')}).", "evidence": res}
            return {"ok": True, "evidence": res}
        if src == "ax":
            loc = m["locator"]
            verb = {"press": "press", "type": "set-value", "set_value": "set-value", "focus": "focus",
                    "select": "press", "scroll": "press"}.get(action)
            if action == "pointer":
                return self._pointer(m, rec)
            if verb is None:
                return {"ok": False, "code": "unsupported", "message": f"{action} isn't supported on this element."}
            if verb == "set-value" and "AXValue" not in (loc.get("actions") or []) and m.get("role") not in (
                    "AXTextField", "AXTextArea", "AXComboBox"):
                pass
            res = self.ax.act(loc["app"], loc["path"], verb,
                              value=str(args.get("text", args.get("value", ""))) if verb == "set-value" else None)
            rec.add("act", f"ax {verb}", bool(res.get("ok")), channel="ax", evidence=res)
            if not res.get("ok") and action == "press":
                # a press AX refused (custom control): a trusted click on the located element
                return self._pointer(m, rec)
            if not res.get("ok"):
                e = res.get("error") or {}
                return {"ok": False, "code": e.get("code", "internal"), "message": e.get("message", "AX action failed"),
                        "fix": e.get("fix")}
            return {"ok": True, "evidence": res}
        if src == "ocr" or action == "pointer":
            return self._pointer(m, rec)
        return {"ok": False, "code": "unsupported", "message": "No way to act on that element."}

    def _pointer(self, m: dict[str, Any], rec: Recorder) -> dict[str, Any]:
        f = m.get("frame")
        if not f or f[2] <= 0 or f[3] <= 0:
            return {"ok": False, "code": "invalid_args", "message": "That element has no location to click."}
        x, y = f[0] + f[2] / 2, f[1] + f[3] / 2
        res = self.ax.click(x, y)
        rec.add("act", "pointer click on located element", bool(res.get("ok")), channel="pointer",
                evidence={"at": [round(x), round(y)]})
        if not res.get("ok"):
            e = res.get("error") or {}
            return {"ok": False, "code": e.get("code", "internal"), "message": e.get("message", "click failed"),
                    "fix": e.get("fix")}
        return {"ok": True, "evidence": {"clicked": [round(x), round(y)]}}

    def _menu(self, args: dict[str, Any], rec: Recorder) -> dict[str, Any]:
        app, path = args.get("app"), args.get("path")
        if not app or not isinstance(path, list) or len(path) < 2:
            return {"ok": False, "code": "invalid_args", "message": "menu needs app and a path like [File, New Tab]."}
        # innermost menu item last: click menu item "X" of menu "File" of menu bar 1
        script = ('tell application "System Events" to tell process ' + applescript_quote(app) + "\n"
                  "  click menu item " + applescript_quote(path[-1]) + " of menu " + applescript_quote(path[-2]) +
                  " of menu bar 1\nend tell")
        p = self.rt.osascript(script, timeout=15)
        rec.add("act", f"menu {' > '.join(path)}", p.ok, channel="ax", evidence={"err": (p.err or "")[:100]})
        if not p.ok:
            return {"ok": False, "code": "permission_missing" if p.permission_denied else "not_found",
                    "message": f"I couldn't use the {' > '.join(path)} menu: {(p.err or '')[:80]}"}
        return {"ok": True, "evidence": {"menu": path}}

    _KEYS = {"return": 36, "enter": 76, "tab": 48, "space": 49, "delete": 51, "escape": 53, "esc": 53,
             "left": 123, "right": 124, "down": 125, "up": 126, "pagedown": 121, "pageup": 116}

    def _key(self, args: dict[str, Any], rec: Recorder) -> dict[str, Any]:
        keys = str(args.get("keys") or "")
        app = args.get("app")
        parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
        if not parts:
            return {"ok": False, "code": "invalid_args", "message": "key needs 'keys' like cmd+t."}
        mods = [p for p in parts[:-1] if p in ("cmd", "command", "shift", "option", "alt", "control", "ctrl")]
        key = parts[-1]
        using = ""
        if mods:
            m = {"cmd": "command down", "command": "command down", "shift": "shift down", "option": "option down",
                 "alt": "option down", "control": "control down", "ctrl": "control down"}
            using = " using {" + ", ".join(m[x] for x in mods) + "}"
        act = f"key code {self._KEYS[key]}{using}" if key in self._KEYS else f"keystroke {applescript_quote(key)}{using}"
        pre = f'tell application {applescript_quote(app)} to activate\ndelay 0.2\n' if app else ""
        p = self.rt.osascript(pre + f'tell application "System Events" to {act}', timeout=15)
        rec.add("act", f"keys {keys}", p.ok, channel="keys")
        if not p.ok:
            return {"ok": False, "code": "permission_missing" if p.permission_denied else "internal",
                    "message": (p.err or "key press failed")[:100]}
        return {"ok": True, "evidence": {"keys": keys}}

    # -- wait / verify ----------------------------------------------------------------------
    def _check(self, pred: dict[str, Any]) -> tuple[bool, dict[str, Any], str | None]:
        kind = pred.get("kind")
        if kind in ("ax", "dom"):
            sel = dict(pred.get("selector") or {})
            found = self.find(sel, channels=[kind], limit=3)
            exists = found["ok"]
            want = pred.get("exists", True)
            ev: dict[str, Any] = {"exists": exists}
            if exists and "value" in pred:
                got = str((found["evidence"]["matches"][0].get("value")) or "")
                ev["value"] = got
                return got == str(pred["value"]), ev, kind
            if exists and "name" in pred:
                got = found["evidence"]["matches"][0].get("name") or ""
                ev["name"] = got
                return pred["name"].lower() in got.lower(), ev, kind
            return exists == bool(want), ev, kind
        if kind == "dom_js":
            got = self._tab(pred.get("selector") or {})
            if not got:
                return False, {"error": "no tab"}, "dom"
            b, t = got
            res = b.js_json(t, DOM_EXPR_BODY.replace("__EXPR__", str(pred["js_expr"])))
            return bool(res.get("value")), {"value": res.get("value")}, "dom"
        if kind == "ocr":
            found = self._find_ocr({"app": pred.get("app"), "text": pred.get("text")}, 3)
            return bool(found) == bool(pred.get("exists", True)), {"found": len(found)}, "ocr"
        if kind == "process":
            ps = process_running(self.rt, str(pred["name"])) if "/" in str(pred["name"]) else \
                bool(re.search(rf"(^|/){re.escape(str(pred['name']))}(\s|$)",
                               self.rt.run(["ps", "-axo", "comm="], timeout=8).out or "", re.M))
            return ps == bool(pred.get("running", True)), {"running": ps}, "process"
        if kind == "file":
            p = Path(str(pred["path"])).expanduser()
            ex = p.exists()
            ev = {"exists": ex, "bytes": p.stat().st_size if ex and p.is_file() else None}
            ok = ex == bool(pred.get("exists", True)) and (
                "min_bytes" not in pred or (ev["bytes"] or 0) >= int(pred["min_bytes"]))
            return ok, ev, "fs"
        if kind == "world":
            from .world import World

            w = self.ctx.extra.setdefault("_world", World(self.ctx))
            res = w.query(pred["select"], pred.get("where"), refresh=True)
            n = res["evidence"]["n"]
            return (n > 0) == bool(pred.get("exists", True)), {"n": n}, None
        return False, {"error": f"unknown predicate kind {kind!r}"}, None

    def verify(self, postcondition: dict[str, Any]) -> dict[str, Any]:
        rec = Recorder("ui.verify", clock=self.rt.monotonic)
        try:
            ok, ev, ch = self._check(postcondition)
        except BrowserError as e:
            return R.fail(e.code, e.message, fix=e.fix)
        rec.add("verify", "postcondition", ok, channel=ch, evidence=ev)
        return rec.finish(ok, verified=ok, evidence=ev, err=None if ok else R.error("unverified", "the condition does not hold"))

    def wait_until(self, predicate: dict[str, Any], *, timeout_s: float = 10.0,
                   poll_s: Any = "adaptive") -> dict[str, Any]:
        rec = Recorder("ui.wait_until", clock=self.rt.monotonic)
        t0 = self.rt.monotonic()
        interval = 0.1 if poll_s == "adaptive" else float(poll_s)
        polls = 0
        ev: dict[str, Any] = {}
        while True:
            polls += 1
            try:
                ok, ev, ch = self._check(predicate)
            except BrowserError as e:
                return R.fail(e.code, e.message, fix=e.fix)
            if ok:
                rec.add("wait", "predicate held", True, channel=ch,
                        ms=int((self.rt.monotonic() - t0) * 1000), evidence={**ev, "polls": polls})
                return rec.finish(True, verified=True, evidence={**ev, "polls": polls,
                                                                 "waited_ms": int((self.rt.monotonic() - t0) * 1000)})
            if self.rt.monotonic() - t0 >= timeout_s:
                rec.add("wait", "timed out", False, channel=ch, evidence={**ev, "polls": polls})
                return rec.finish(False, evidence={**ev, "polls": polls},
                                  err=R.error("timeout", f"The condition did not hold within {timeout_s:g} s.", retryable=True))
            self.rt.sleep(interval)
            if poll_s == "adaptive":
                interval = min(1.0, interval * 1.5)

    # -- observe --------------------------------------------------------------------------------
    def observe(self, scope: dict[str, Any], *, include_image: bool = False) -> dict[str, Any]:
        rec = Recorder("ui.observe", clock=self.rt.monotonic)
        evidence: dict[str, Any] = {"untrusted": True}
        got = self._tab(scope) if (scope.get("browser") or scope.get("tab")) else None
        if got:
            b, t = got
            try:
                out = b.js_json(t, DOM_OUTLINE_BODY.replace("__LIMIT__", str(int(scope.get("limit", 60)))))
            except BrowserError as e:
                return R.fail(e.code, e.message, fix=e.fix)
            evidence.update(kind="page", **out)
            rec.add("perceive", "page outline", True, channel="dom", evidence={"n": len(out.get("elements", []))})
        elif scope.get("app"):
            res = self.ax.tree(scope["app"], depth=int(scope.get("depth", 5)), limit=int(scope.get("limit", 200)),
                               title=(scope.get("window") or {}).get("title"))
            if not res.get("ok"):
                e = res.get("error") or {}
                return R.fail(e.get("code", "internal"), e.get("message", "observe failed"), fix=e.get("fix"))
            evidence.update(kind="ax_tree", windows=res["windows"], count=res.get("count"))
            rec.add("perceive", "ax tree", True, channel="ax", evidence={"count": res.get("count")})
        else:
            return R.fail("invalid_args", "observe needs an app or a browser tab scope.")
        if include_image:
            shot = self._capture(scope)
            evidence["image"] = shot["path"] if shot else None
            rec.add("perceive", "screenshot", bool(shot), channel="ocr")
        return rec.finish(True, verified=True, evidence=evidence)


_ = applescript_quote
