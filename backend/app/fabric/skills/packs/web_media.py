"""Web media: find the tab that is playing, read ad state, skip ads, control playback (T2).

Everything runs through the page's own DOM via the browser's scripting verb, so
nothing needs focus: a background tab in a background window works. Ad and skip
detection is semantic (roles, labels, visible text, state attributes), with no
reliance on one site's class names, and it searches open shadow roots.

Honesty rules:
* a locked Mac or a sleeping display is reported as exactly that, with no action;
* an unskippable ad is reported with its remaining time, never "skipped";
* "skipped" is only claimed after a read-back shows that ad is gone;
* DOM click is the first rung; Accessibility press and a trusted pointer click
  follow only if the page ignored the first, and the receipt names the rung.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .. import system_state
from ..receipts import Recorder, receipt
from ..runtime import Runtime, default_runtime
from .browsers import BrowserDriver, BrowserError, Tab, drivers

MEDIA_HOSTS = ("youtube.com", "youtu.be", "music.youtube.com", "twitch.tv", "vimeo.com",
               "netflix.com", "primevideo.com", "dailymotion.com", "soundcloud.com", "spotify.com",
               "tv.apple.com", "disneyplus.com", "hulu.com", "max.com")

# ----------------------------------------------------------- page scripts ----
# Shared prelude: element walk (open shadow roots included), label/visibility
# helpers, skip-control detection, ad-signal collection. Returns plain JSON.
PRELUDE = r"""
var SKIP_READY=/^\s*skip(?:\s+(?:ad|ads|advert\w*|intro))?\s*[\u203a\u00bb>\u25b6]*\s*$/i;
var SKIP_WAIT=/^\s*(?:you\s+can\s+)?skip(?:\s+(?:ad|ads))?\s+in\s+(\d+)\s*s?\s*$/i;
var AD_TEXT=/^\s*(?:ad|advertisement|sponsored)(?:\s*[\u00b7\u2022:\-\u2013|]\s*\d*:?\d*|\s+\d+\s*(?:of|\/)\s*\d+)?\s*$/i;
var AD_STRONG_CLASS=/(^|[\s_-])ad[-_]?(showing|interrupting|playing|active|break)([\s_-]|$)/i;
var AD_STATE_VAL=/^(playing|active|true|running|started|ad)$/i;
function allEls(root){var out=[],stack=[root],n=0;while(stack.length&&n<30000){var cur=stack.pop();var kids=cur.children||[];for(var i=kids.length-1;i>=0;i--){var c=kids[i];out.push(c);n++;stack.push(c);if(c.shadowRoot)stack.push(c.shadowRoot);}}return out;}
function vis(el){if(!el||!el.getBoundingClientRect)return false;var r=el.getBoundingClientRect();if(r.width<2||r.height<2)return false;var cs=getComputedStyle(el);return !(cs.visibility==='hidden'||cs.display==='none'||parseFloat(cs.opacity)===0);}
function own(el){var t=(el.getAttribute&&(el.getAttribute('aria-label')||el.getAttribute('title')))||'';if(t)return t.trim();if(el.shadowRoot){var s=(el.shadowRoot.textContent||'').replace(/\s+/g,' ').trim();if(s)return s;}return ((el.innerText||el.textContent||'')+'').replace(/\s+/g,' ').trim();}
function inner(el){if(el.shadowRoot){var b=el.shadowRoot.querySelector('button,[role=button],a');if(b)return b;}return el;}
function playerBox(v){var el=v,best=v,va=Math.max(1,v.getBoundingClientRect().width*v.getBoundingClientRect().height);for(var i=0;i<8&&el.parentElement;i++){el=el.parentElement;var r=el.getBoundingClientRect();if(r.width*r.height<=3*va)best=el;else break;}return best;}
function primaryVideo(){var vs=allEls(document.documentElement).filter(function(e){return e.tagName==='VIDEO';});var best=null,score=-1;vs.forEach(function(v){var r=v.getBoundingClientRect();var s=(v.paused?0:1e9)+r.width*r.height;if(s>score&&(vis(v)||!v.paused)){best=v;score=s;}});return best||vs[0]||null;}
function skipState(scope){var els=allEls(scope||document.documentElement);var found=null;
 for(var i=0;i<els.length;i++){var e=els[i];var tag=e.tagName;var role=e.getAttribute&&e.getAttribute('role');
  var clickable=tag==='BUTTON'||tag==='A'||role==='button'||role==='link'||tag==='AD-CONTROLS'||/skip/i.test(e.className&&e.className.toString?e.className.toString():'');
  if(!clickable||!vis(e))continue;var l=own(e);if(!l||l.length>40)continue;
  var w=SKIP_WAIT.exec(l);var r=SKIP_READY.test(l);
  if(!w&&!r)continue;var tgt=inner(e);var dis=!!(tgt.disabled||e.disabled||(e.getAttribute&&e.getAttribute('aria-disabled')==='true')||(e.classList&&(e.classList.contains('pending')||e.classList.contains('disabled'))));
  var rect=e.getBoundingClientRect();
  var st=w?'countdown':(dis?'countdown':'ready');
  found={state:st,label:l,wait_s:w?parseInt(w[1],10):(dis?1:0),enabled:!dis,el:tgt,rect:[Math.round(rect.left),Math.round(rect.top),Math.round(rect.width),Math.round(rect.height)]};
  if(st==='ready')return found;}
 return found||{state:'none',label:'',wait_s:0,enabled:false,el:null,rect:null};}
function adInfo(v){var box=v?playerBox(v):document.body;var sig=[];var els=[box].concat(allEls(box));var remaining=null,idx=null,total=null;
 var anc=v;for(var k=0;k<10&&anc;k++){var cn=(anc.className&&anc.className.toString)?anc.className.toString():'';if(AD_STRONG_CLASS.test(cn))sig.push('class:'+cn.slice(0,40));var ds=anc.getAttribute&&anc.getAttribute('data-ad-state');if(ds&&AD_STATE_VAL.test(ds))sig.push('attr:data-ad-state='+ds);anc=anc.parentElement;}
 els.forEach(function(e){if(!e.getAttribute)return;var cn=(e.className&&e.className.toString)?e.className.toString():'';if(AD_STRONG_CLASS.test(cn)&&e!==box)sig.push('class:'+cn.slice(0,40));
  var ds=e.getAttribute('data-ad-state');if(ds&&AD_STATE_VAL.test(ds))sig.push('attr:data-ad-state='+ds);
  var al=e.getAttribute('aria-label')||'';if(/^\s*advert(isement)?s?\s*$/i.test(al)&&vis(e)&&e!==document.body)sig.push('aria:'+al);
  if(e.children&&e.children.length>3)return;var t=((e.textContent||'')+'').replace(/\s+/g,' ').trim();
  if(t&&t.length<40&&vis(e)&&AD_TEXT.test(t)){sig.push('text:'+t);var m=/(\d+):(\d{2})/.exec(t);if(m&&remaining===null)remaining=parseInt(m[1],10)*60+parseInt(m[2],10);var o=/(\d+)\s*(?:of|\/)\s*(\d+)/i.exec(t);if(o){idx=parseInt(o[1],10);total=parseInt(o[2],10);}}});
 var sk=skipState(box);if(sk.state!=='none')sig.push('skip-control');
 var uniq=[];sig.forEach(function(s){if(uniq.indexOf(s)<0)uniq.push(s);});
 return {active:uniq.length>0,signals:uniq.slice(0,6),remaining_s:remaining,index:idx,total:total,skip:{state:sk.state,label:sk.label,wait_s:sk.wait_s,enabled:sk.enabled,rect:sk.rect},_sk:sk};}
function mediaInfo(v){if(!v)return null;var d=isFinite(v.duration)?v.duration:null;return {paused:v.paused,ended:v.ended,t:Math.round(v.currentTime*10)/10,duration:d,muted:v.muted,volume:Math.round(v.volume*100)/100,rate:v.playbackRate,ready:v.readyState};}
"""

PROBE_BODY = PRELUDE + r"""
var v=primaryVideo();var vs=allEls(document.documentElement).filter(function(e){return e.tagName==='VIDEO'||e.tagName==='AUDIO';});
var playing=vs.some(function(m){return !m.paused&&!m.ended&&m.readyState>1;});
var ad=v?adInfo(v):{active:false,signals:[],remaining_s:null,index:null,total:null,skip:{state:'none',label:'',wait_s:0,enabled:false,rect:null}};
delete ad._sk;
var cc=null;var ccb=allEls(document.documentElement).filter(function(e){var l=e.getAttribute&&(e.getAttribute('aria-label')||'');return /(subtitles|captions|closed caption)/i.test(l)&&(e.tagName==='BUTTON'||e.getAttribute('role')==='button');})[0];
if(ccb)cc={label:ccb.getAttribute('aria-label'),on:ccb.getAttribute('aria-pressed')==='true'};
return {ok:true,url:location.href,title:document.title,hidden:document.hidden,media_count:vs.length,playing:playing,media:mediaInfo(v),ad:ad,captions:cc};
"""

ARM_BODY = PRELUDE + r"""
var NS='__evHandsSkip';var st=window[NS];if(st&&st.timer)clearInterval(st.timer);
st=window[NS]={armed:true,clicks:0,log:[],deadline:Date.now()+__MAX_MS__,maxClicks:__MAX_CLICKS__,last:0,armedAt:Date.now()};
st.timer=setInterval(function(){
 if(Date.now()>st.deadline||st.clicks>=st.maxClicks){clearInterval(st.timer);st.armed=false;return;}
 var v=primaryVideo();if(!v)return;var box=playerBox(v);var s=skipState(box);
 if(s.state==='ready'&&s.enabled&&Date.now()-st.last>1500){st.last=Date.now();try{s.el.click();}catch(e){}st.clicks++;st.log.push({t:st.last,label:s.label});}
},60);
return {armed:true,armedAt:st.armedAt};
"""

ARM_STATE_BODY = r"""
var st=window['__evHandsSkip'];if(!st)return {armed:false,clicks:0,log:[]};
return {armed:!!st.armed,clicks:st.clicks,log:st.log,armedAt:st.armedAt};
"""

DISARM_BODY = r"""
var st=window['__evHandsSkip'];if(st&&st.timer)clearInterval(st.timer);if(st)st.armed=false;return {armed:false};
"""

CLICK_SKIP_BODY = PRELUDE + r"""
var v=primaryVideo();if(!v)return {ok:false,error:'no video'};var s=skipState(playerBox(v));
if(s.state!=='ready'||!s.enabled)return {ok:false,error:'skip not ready',state:s.state};
s.el.click();return {ok:true,label:s.label,at:Date.now()};
"""

_CTRL_PRELUDE = PRELUDE + r"""
function findCtl(re){var e=allEls(document.documentElement).filter(function(x){if(!x.getAttribute)return false;var t=x.tagName;var r=x.getAttribute('role');if(!(t==='BUTTON'||t==='A'||r==='button'))return false;if(!vis(x))return false;var l=(x.getAttribute('aria-label')||x.getAttribute('title')||x.textContent||'').replace(/\s+/g,' ').trim();return l.length<40&&re.test(l);});return e[0]||null;}
"""

CTRL_BODY = _CTRL_PRELUDE + r"""
var a=__ACTION__,val=__VALUE__;var v=primaryVideo();
if(!v&&a!=='next'&&a!=='previous')return {ok:false,error:'no media element'};
function snap(){return {ok:true,action:a,media:mediaInfo(primaryVideo())};}
if(a==='play'){var p=v.play();if(p&&p.catch)p.catch(function(){v.muted=true;v.play();});return snap();}
if(a==='pause'){v.pause();return snap();}
if(a==='toggle'){if(v.paused)v.play();else v.pause();return snap();}
if(a==='seek_by'){v.currentTime=Math.max(0,v.currentTime+val);return snap();}
if(a==='seek_to'){v.currentTime=Math.max(0,val);return snap();}
if(a==='volume_to'){v.volume=Math.max(0,Math.min(1,val));if(v.volume>0)v.muted=false;return snap();}
if(a==='volume_by'){v.volume=Math.max(0,Math.min(1,v.volume+val));if(v.volume>0)v.muted=false;return snap();}
if(a==='mute'){v.muted=true;return snap();}
if(a==='unmute'){v.muted=false;return snap();}
if(a==='rate'){v.playbackRate=Math.max(0.25,Math.min(4,val));return snap();}
if(a==='captions'){var b=findCtl(/(subtitles|captions|closed caption|^cc$)/i);if(!b)return {ok:false,error:'no captions control'};var before=b.getAttribute('aria-pressed');b.click();return {ok:true,action:a,control:b.getAttribute('aria-label'),before:before,after:b.getAttribute('aria-pressed')};}
if(a==='next'||a==='previous'){var re=a==='next'?/^(next|next (video|track|episode))$/i:/^(previous|prev|previous (video|track|episode))$/i;var c=findCtl(re);if(!c)return {ok:false,error:'no '+a+' control'};c.click();return {ok:true,action:a,control:c.getAttribute('aria-label')||c.textContent.trim()};}
return {ok:false,error:'unknown action'};
"""

SKIP_LABEL = r"^skip( ad| ads)?[ \u203a\u00bb>]*$"


@dataclass
class Probe:
    tab: Tab
    state: dict[str, Any] | None
    error: BrowserError | None = None

    @property
    def playing(self) -> bool:
        return bool(self.state and self.state.get("playing"))

    @property
    def ad(self) -> dict[str, Any]:
        return (self.state or {}).get("ad") or {}


class WebMedia:
    def __init__(self, rt: Runtime | None = None, browsers: list[BrowserDriver] | None = None,
                 *, window_ids: dict[str, list[str]] | None = None, workers: int = 6, ax: Any = None):
        self.rt = rt or default_runtime()
        self._ax = ax
        self.browsers = browsers if browsers is not None else drivers(self.rt)
        self.window_ids = window_ids or {}  # browser kind -> allowed window ids (tests scope themselves)
        self.workers = workers

    # -- discovery ------------------------------------------------------------
    def candidates(self) -> tuple[list[tuple[BrowserDriver, Tab]], list[BrowserError]]:
        out: list[tuple[BrowserDriver, Tab]] = []
        errors: list[BrowserError] = []
        for b in self.browsers:
            try:
                if not b.running():
                    continue
                for t in b.tabs(window_ids=self.window_ids.get(b.kind)):
                    if t.url.startswith(("chrome://", "about:", "chrome-extension://", "view-source:")):
                        continue
                    out.append((b, t))
            except BrowserError as exc:
                errors.append(exc)
        return out, errors

    def probe_tab(self, b: BrowserDriver, tab: Tab) -> Probe:
        try:
            return Probe(tab, b.js_json(tab, PROBE_BODY, timeout=12))
        except BrowserError as exc:
            return Probe(tab, None, exc)

    def survey(self) -> tuple[list[tuple[BrowserDriver, Probe]], list[BrowserError]]:
        cands, errors = self.candidates()
        # likely media hosts first, then everything else (still all probed, in parallel)
        cands.sort(key=lambda bt: 0 if any(bt[1].host == h or bt[1].host.endswith("." + h)
                                           for h in MEDIA_HOSTS) else 1)
        results: list[tuple[BrowserDriver, Probe]] = []
        if len(cands) <= 1 or self.workers <= 1:
            results = [(b, self.probe_tab(b, t)) for b, t in cands]
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futs = [(b, pool.submit(self.probe_tab, b, t)) for b, t in cands]
                results = [(b, f.result()) for b, f in futs]
        for _, pr in results:
            if pr.error and pr.error.code in ("js_disabled", "permission_missing") \
                    and pr.error not in errors:
                errors.append(pr.error)
        return results, errors

    # -- status ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        rec = Recorder("web_media.status")
        sysst = system_state.read(self.rt)
        results, errors = self.survey()
        rec.add("perceive", "survey tabs", True, channel="dom",
                evidence={"tabs": len(results)})
        playing = [pr for _, pr in results if pr.playing]
        items = [{"browser": pr.tab.browser, "window_id": pr.tab.window_id, "tab_id": pr.tab.tab_id,
                  "title": pr.tab.title, "url": pr.tab.url, "active": pr.tab.active,
                  "state": "ad" if pr.ad.get("active") else "playing",
                  "media": pr.state.get("media") if pr.state else None, "ad": pr.ad or None}
                 for pr in playing]
        err = errors[0] if errors and not results else None
        say = None
        if items:
            n = len(items)
            say = f"{n} tab{'s are' if n != 1 else ' is'} playing media."
        elif err:
            say = err.message
        else:
            say = "Nothing is playing in your browsers."
        return rec.finish(err is None, verified=err is None, evidence={"playing": items,
                          "scanned": len(results), "system": sysst}, say=say,
                          err=_err(err) if err else None)

    # -- the T2 flow ----------------------------------------------------------
    def skip_ad(self, *, max_ads: int = 3, wait_cap_s: float = 40.0, verify_s: float = 4.0,
                provenance: str = "owner", prefer_url: str | None = None) -> dict[str, Any]:
        rec = Recorder("web_media.skip_ad", provenance=provenance, clock=self.rt.monotonic)
        sysst = system_state.read(self.rt)
        block = system_state.blocker(sysst)
        rec.add("resolve", "system state", block is None, evidence={"system": sysst})
        if block:
            code, msg = block
            return rec.finish(False, verified=False, say=msg,
                              err={"code": code, "message": msg, "fix": None, "retryable": True},
                              evidence={"system": sysst})
        results, errors = self.survey()
        rec.add("resolve", "find playing tabs", True, channel="dom", evidence={"scanned": len(results)})
        playing = [(b, pr) for b, pr in results if pr.playing or pr.ad.get("active")]
        if not results and errors:
            e = errors[0]
            return rec.finish(False, verified=False, say=_say_error(e), err=_err(e))
        if not playing:
            if errors:
                e = errors[0]
                return rec.finish(False, verified=False, say=_say_error(e), err=_err(e))
            return rec.finish(True, verified=True, evidence={"ad": False, "scanned": len(results)},
                              say="Nothing is playing, so there's no ad to skip.")
        with_ad = [(b, pr) for b, pr in playing if pr.ad.get("active")]
        if prefer_url:
            with_ad = [x for x in with_ad if prefer_url in x[1].tab.url] or with_ad
        if not with_ad:
            title = playing[0][1].tab.title
            return rec.finish(True, verified=True, evidence={"ad": False, "tab": playing[0][1].tab.to_dict()},
                              say=f"No ad is playing right now in {title[:40]!r}." if title else "No ad is playing right now.")
        # several candidates: the visible (active) tab first, then the first seen
        with_ad.sort(key=lambda x: (0 if x[1].tab.active else 1))
        b, pr = with_ad[0]
        extra = len(with_ad) - 1
        skipped = 0
        last_ad: dict[str, Any] = pr.ad
        last_state: dict[str, Any] = pr.state or {}
        for round_no in range(max_ads):
            res = self._skip_one(b, pr.tab, last_ad, rec, wait_cap_s, verify_s, last_state)
            if res["outcome"] == "skipped":
                skipped += 1
                state = res["after"]
                if state.get("ad", {}).get("active"):
                    last_ad, last_state = state["ad"], state  # back-to-back: another ad started
                    continue
                break
            if res["outcome"] == "unskippable":
                rem = res.get("remaining_s")
                msg = ("That ad can't be skipped"
                       + (f"; about {int(rem)} seconds left." if rem else "."))
                if skipped:
                    msg = f"Skipped {skipped} ad{'s' if skipped != 1 else ''}, then " + msg[0].lower() + msg[1:]
                return rec.finish(False, verified=True, say=msg, evidence={
                    "ad": last_ad, "skipped": skipped, "remaining_s": rem, "tab": pr.tab.to_dict()},
                    err={"code": "precondition_failed", "message": "ad is not skippable",
                         "fix": None, "retryable": False})
            if res["outcome"] == "gone":
                return rec.finish(True, verified=True, say="The ad ended on its own." if not skipped
                                  else f"Skipped {skipped} ad{'s' if skipped != 1 else ''}.",
                                  evidence={"skipped": skipped, "ad": False})
            return rec.finish(False, verified=False, say=res["say"], err=res["err"],
                              evidence={"skipped": skipped, "rung": res.get("rung")})
        say = "Skipped the ad." if skipped == 1 else f"Skipped {skipped} ads."
        if extra:
            say += f" ({extra} other tab{'s' if extra != 1 else ''} also had an ad.)"
        final = self.probe_tab(b, pr.tab)
        evidence = {"skipped": skipped, "tab": pr.tab.to_dict(), "after": final.state}
        ok = bool(rec.verified_ok())
        if not ok:
            return rec.finish(False, verified=False, say="I pressed skip but couldn't confirm the ad ended.",
                              err={"code": "unverified", "message": "ad still showing", "fix": None,
                                   "retryable": True}, evidence=evidence)
        return rec.finish(True, verified=True, say=say, evidence=evidence,
                          undo=None)

    def _skip_one(self, b: BrowserDriver, tab: Tab, ad: dict[str, Any], rec: Recorder,
                  wait_cap_s: float, verify_s: float, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        skip = ad.get("skip") or {}
        state = skip.get("state", "none")
        if state == "none":
            # no control yet: re-probe briefly, some players mount it a moment after the ad starts
            for _ in range(3):
                self.rt.sleep(0.4)
                fresh = self.probe_tab(b, tab)
                if not fresh.ad.get("active"):
                    return {"outcome": "gone"}
                ad, snapshot = fresh.ad, fresh.state or snapshot
                skip = ad.get("skip") or {}
                if skip.get("state", "none") != "none":
                    state = skip["state"]
                    break
            if state == "none":
                rec.add("perceive", "ad has no skip control", True, channel="dom",
                        evidence={"remaining_s": ad.get("remaining_s")})
                return {"outcome": "unskippable", "remaining_s": ad.get("remaining_s")}
        if state == "countdown":
            wait = float(skip.get("wait_s") or 0)
            if wait > wait_cap_s:
                return {"outcome": "unskippable", "remaining_s": ad.get("remaining_s"),
                        "say": f"The skip button is {int(wait)} seconds away."}
            armed = self._arm(b, tab, wait + 6.0)
            rec.add("act", "arm in-page skip", armed, channel="dom", evidence={"wait_s": wait})
            if not armed:
                return {"outcome": "failed", "say": "I couldn't arm the skip control.",
                        "err": {"code": "internal", "message": "arm failed", "fix": None, "retryable": True}}
            t0 = self.rt.monotonic()
            clicked = False
            while self.rt.monotonic() - t0 < wait + 6.0:
                self.rt.sleep(0.25)
                try:
                    st = b.js_json(tab, ARM_STATE_BODY, timeout=8)
                except BrowserError:
                    continue
                if st.get("clicks"):
                    clicked = True
                    break
                fresh = self.probe_tab(b, tab)
                if not fresh.ad.get("active"):
                    return {"outcome": "gone"}
            rec.add("wait", "countdown", clicked, channel="dom",
                    ms=int((self.rt.monotonic() - t0) * 1000), evidence={"clicked": clicked})
            if not clicked:
                self._disarm(b, tab)
                return {"outcome": "failed", "say": "The skip button never became available.",
                        "err": {"code": "timeout", "message": "skip never became ready", "fix": None,
                                "retryable": True}}
            rung = "dom"
        else:
            try:
                clicked_res = b.js_json(tab, CLICK_SKIP_BODY, timeout=8)
            except BrowserError as exc:
                return {"outcome": "failed", "say": _say_error(exc), "err": _err(exc)}
            ok = bool(clicked_res.get("ok"))
            rec.add("act", "dom click skip", ok, channel="dom", evidence=clicked_res)
            if not ok:
                return {"outcome": "failed", "say": "I couldn't press the skip button.",
                        "err": {"code": "internal", "message": str(clicked_res.get("error")),
                                "fix": None, "retryable": True}}
            rung = "dom"
        # verify: the ad must be gone (or replaced by a different one)
        t0 = self.rt.monotonic()
        before = snapshot or {"ad": ad}
        quick = 0.6  # a page that accepted the click reacts almost at once
        while True:
            fresh = self.probe_tab(b, tab)
            if not fresh.ad.get("active") or _new_ad(before, fresh.state or {}):
                rec.add("verify", "ad ended", True, channel="dom",
                        ms=int((self.rt.monotonic() - t0) * 1000), evidence={"ad": fresh.ad})
                return {"outcome": "skipped", "after": fresh.state or {}, "rung": rung}
            waited = self.rt.monotonic() - t0
            if waited >= quick and (fresh.ad.get("skip") or {}).get("state") == "ready" \
                    and self._ax_ready():
                break  # control still pressable and nothing changed: the scripted click was ignored
            if waited >= verify_s:
                break
            self.rt.sleep(0.2)
        rec.add("verify", "ad ended", False, channel="dom", evidence={"ad": fresh.ad})
        return self._fallback_rungs(b, tab, rec, verify_s, ad, before)

    def _wait_ended(self, b: BrowserDriver, tab: Tab, before: dict[str, Any], seconds: float,
                    rec: Recorder, channel: str) -> dict[str, Any] | None:
        """Poll until the ad is gone or replaced; records the verify step either way."""
        t0 = self.rt.monotonic()
        while True:
            fresh = self.probe_tab(b, tab)
            if not fresh.ad.get("active") or _new_ad(before, fresh.state or {}):
                rec.add("verify", "ad ended", True, channel=channel,
                        ms=int((self.rt.monotonic() - t0) * 1000), evidence={"ad": fresh.ad})
                return fresh.state or {}
            if self.rt.monotonic() - t0 >= seconds:
                rec.add("verify", "ad ended", False, channel=channel, evidence={"ad": fresh.ad})
                return None
            self.rt.sleep(0.2)

    def _fallback_rungs(self, b: BrowserDriver, tab: Tab, rec: Recorder, verify_s: float,
                        ad: dict[str, Any], before: dict[str, Any] | None = None) -> dict[str, Any]:
        """The page ignored the scripted click: try a real Accessibility press, then a trusted click."""
        before = before or {"ad": ad}
        done = self._wait_ended(b, tab, before, 0.0, rec, "dom")  # it may simply have ended meanwhile
        if done is not None:
            return {"outcome": "skipped", "after": done, "rung": "dom"}
        ax = self._ax
        if ax is None or not ax.available():
            return {"outcome": "failed", "say": "The page ignored the skip button and I have no other way to press it.",
                    "err": {"code": "unverified", "message": "scripted click ignored; AX helper unavailable",
                            "fix": None, "retryable": True}, "rung": "dom"}
        app = b.app
        ax.enable(app)
        found = ax.find(app, role="AXButton", name=SKIP_LABEL, max_hits=3)
        matches = (found.get("matches") or []) if found.get("ok") else []
        if not matches:
            found = ax.find(app, name=SKIP_LABEL, max_hits=3)
            matches = (found.get("matches") or []) if found.get("ok") else []
        if not matches:
            done = self._wait_ended(b, tab, before, verify_s, rec, "ax")
            if done is not None:
                return {"outcome": "skipped", "after": done, "rung": "dom"}
        for rung, action in (("ax", "press"), ("pointer", "click")):
            if not matches:
                break
            m = matches[0]
            if rung == "ax":
                res = ax.act(app, m["path"], "press")
                ok = bool(res.get("ok"))
            else:
                pos, size = m.get("pos"), m.get("size")
                if not pos or not size:
                    continue
                res = ax.click(pos[0] + size[0] / 2, pos[1] + size[1] / 2)
                ok = bool(res.get("ok"))
            rec.add("act", f"{rung} {action} skip", ok, channel=rung, evidence={"match": m.get("path")})
            if not ok:
                continue
            done = self._wait_ended(b, tab, before, verify_s, rec, rung)
            if done is not None:
                return {"outcome": "skipped", "after": done, "rung": rung}
        return {"outcome": "failed", "say": "I pressed skip but the ad kept playing.",
                "err": {"code": "unverified", "message": "ad still showing after every rung",
                        "fix": None, "retryable": True}, "rung": "pointer"}

    def _ax_ready(self) -> bool:
        from ..ax import AxClient

        if self._ax is None:
            self._ax = AxClient(self.rt)
        return bool(self._ax.available())

    def _arm(self, b: BrowserDriver, tab: Tab, seconds: float) -> bool:
        body = ARM_BODY.replace("__MAX_MS__", str(int(seconds * 1000))).replace("__MAX_CLICKS__", "1")
        try:
            return bool(b.js_json(tab, body, timeout=8).get("armed"))
        except BrowserError:
            return False

    def _disarm(self, b: BrowserDriver, tab: Tab) -> None:
        try:
            b.js_json(tab, DISARM_BODY, timeout=8)
        except BrowserError:
            pass

    # -- generic controls -----------------------------------------------------------
    def control(self, action: str, value: float | None = None, *, tab_hint: str | None = None,
                provenance: str = "owner") -> dict[str, Any]:
        rec = Recorder(f"web_media.{action}", provenance=provenance, clock=self.rt.monotonic)
        results, errors = self.survey()
        playing = [(b, pr) for b, pr in results if pr.playing]
        pool = playing or [(b, pr) for b, pr in results if (pr.state or {}).get("media_count")]
        if tab_hint:
            pool = [x for x in pool if tab_hint.lower() in (x[1].tab.url + " " + x[1].tab.title).lower()] or pool
        rec.add("resolve", "find media tab", bool(pool), channel="dom", evidence={"candidates": len(pool)})
        if not pool:
            if errors:
                e = errors[0]
                return rec.finish(False, verified=False, say=_say_error(e), err=_err(e))
            return rec.finish(False, verified=False, say="I don't see any media to control.",
                              err={"code": "not_found", "message": "no media tab", "fix": None, "retryable": True})
        pool.sort(key=lambda x: (0 if x[1].tab.active else 1))
        b, pr = pool[0]
        body = CTRL_BODY.replace("__ACTION__", json.dumps(action)).replace("__VALUE__", json.dumps(value))
        try:
            res = b.js_json(pr.tab, body, timeout=10)
        except BrowserError as exc:
            return rec.finish(False, verified=False, say=_say_error(exc), err=_err(exc))
        rec.add("act", f"dom {action}", bool(res.get("ok")), channel="dom", evidence=res)
        if not res.get("ok"):
            return rec.finish(False, verified=False, say=f"I couldn't {action.replace('_', ' ')}: {res.get('error')}.",
                              err={"code": "not_found", "message": str(res.get("error")), "fix": None,
                                   "retryable": False})
        self.rt.sleep(0.15)
        after = self.probe_tab(b, pr.tab)
        m = (after.state or {}).get("media") or {}
        ok, say = _check_control(action, value, res, m, pr)
        rec.add("verify", f"read back {action}", ok, channel="dom", evidence={"media": m})
        return rec.finish(ok, verified=ok, say=say if ok else f"I tried to {action.replace('_', ' ')} but couldn't confirm it.",
                          evidence={"tab": pr.tab.to_dict(), "media": m},
                          err=None if ok else {"code": "unverified", "message": "read-back mismatch",
                                               "fix": None, "retryable": True})


def _new_ad(before: dict[str, Any], now: dict[str, Any]) -> bool:
    """True when `now` shows a DIFFERENT ad than `before` (the first one ended).

    An ad is a new one when its index changed, its media clock restarted, or its
    remaining time went UP; the same ad only ever counts down.
    """
    b_ad, n_ad = before.get("ad") or {}, now.get("ad") or {}
    if not n_ad.get("active"):
        return False
    if n_ad.get("index") and b_ad.get("index") and n_ad["index"] != b_ad["index"]:
        return True
    bt = ((before.get("media") or {}).get("t"))
    nt = ((now.get("media") or {}).get("t"))
    if bt is not None and nt is not None and float(nt) < float(bt) - 1.0:
        return True
    br, nr = b_ad.get("remaining_s"), n_ad.get("remaining_s")
    return br is not None and nr is not None and float(nr) > float(br) + 1.0


def _check_control(action: str, value: float | None, res: dict[str, Any], m: dict[str, Any],
                   before: Probe) -> tuple[bool, str]:
    if action == "play":
        return (not m.get("paused", True)), "Playing."
    if action == "pause":
        return bool(m.get("paused")), "Paused."
    if action == "toggle":
        was = (before.state or {}).get("media", {}).get("paused")
        return (m.get("paused") != was), "Paused." if m.get("paused") else "Playing."
    if action in ("seek_by", "seek_to"):
        t0 = float(((before.state or {}).get("media") or {}).get("t") or 0.0)
        want = t0 + float(value or 0) if action == "seek_by" else float(value or 0)
        ok = abs(float(m.get("t") or 0) - max(0.0, want)) <= 3.0
        return ok, f"Jumped to {int(float(m.get('t') or 0))} seconds."
    if action in ("volume_to", "volume_by"):
        v0 = float(((before.state or {}).get("media") or {}).get("volume") or 0)
        want = float(value or 0) if action == "volume_to" else v0 + float(value or 0)
        want = max(0.0, min(1.0, want))
        return abs(float(m.get("volume") or 0) - want) <= 0.05, f"Volume at {int(want * 100)} percent."
    if action == "mute":
        return bool(m.get("muted")), "Muted."
    if action == "unmute":
        return not m.get("muted", True), "Unmuted."
    if action == "rate":
        return abs(float(m.get("rate") or 1) - float(value or 1)) < 0.01, f"Speed {value}x."
    if action == "captions":
        return res.get("after") != res.get("before"), "Toggled captions."
    if action in ("next", "previous"):
        return True, "Done." if action == "next" else "Went back."
    return False, ""


def _err(e: BrowserError) -> dict[str, Any]:
    return {"code": e.code, "message": e.message, "fix": e.fix, "retryable": e.code in ("timeout", "not_found")}


def _say_error(e: BrowserError) -> str:
    if e.code == "js_disabled":
        return f"{e.message}. {e.fix}"
    if e.code == "permission_missing":
        return f"{e.message}. {e.fix}"
    if e.code == "not_running":
        return e.message + "."
    return f"I couldn't do that: {e.message}."


_ = receipt
