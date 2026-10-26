"""CUG tasks that run LIVE against a private Chrome instance and our own fixture pages."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from app.fabric.skills import registry

from .core import Verdict, task
from .live import LiveEnv, cpu_seconds
from .stats import percentile

SKIP = ["skip the ad", "get rid of that ad", "can you skip this advert for me", "uh skip the add please"]


def skip(env: LiveEnv, **kw: Any) -> dict[str, Any]:
    return env.hands.skill_run("web_media.skip_ad", kw or None)


def approve(h, name: str, args: dict[str, Any] | None = None, *, provenance: str = "owner") -> dict[str, Any]:
    """Run a confirm-class skill the way the confirm card would: first the card, then the approval."""
    r = h.skill_run(name, args, provenance=provenance)
    if r.get("needs_confirm"):
        r = h.skill_run(name, args, provenance=provenance, approved=True, approval_id=r["confirm"]["plan_id"])
    return r


# ============================================================ T2 on real pages =====

def _live_skin(skin: str, ads: str, **q: Any):
    @task(f"live.t2.skin_{skin}_{ads}", "media_t2", "live", SKIP, "web_media.skip_ad")
    def run(env: LiveEnv) -> Verdict:
        tab = env.open("player.html", skin=skin, ads=ads, **q)
        r = skip(env)
        f = env.fixture(tab)
        lat = (f.get("clickedAt", 0) - f.get("skipAvailableAt", 0)) if f.get("clickedAt") else None
        ok = r["ok"] and r["verified"] and f.get("adsSkipped", 0) >= 1 and r["say"] in ("Skipped the ad.", "Skipped 2 ads.")
        return Verdict(r, ok, f"page says skipped={f.get('adsSkipped')} click latency {lat} ms",
                       {"latency_ms": lat})

    return run


for _skin in "abcde":
    _live_skin(_skin, "S3L12")


@task("live.t2.unskippable", "media_t2", "live", SKIP, "web_media.skip_ad")
def live_unskip(env):
    tab = env.open("player.html", skin="a", ads="U8")
    r = skip(env)
    f = env.fixture(tab)
    return Verdict(r, (not r["ok"]) and "can't be skipped" in (r["say"] or "") and f.get("clicks", 0) == 0,
                   "said so, pressed nothing")


@task("live.t2.b2b_two_skippable", "media_t2", "live", SKIP, "web_media.skip_ad", difficulty="hard")
def live_b2b(env):
    tab = env.open("player.html", skin="a", ads="S2L10,S2L10")
    r = skip(env)
    f = env.fixture(tab)
    return Verdict(r, r["ok"] and r["verified"] and f.get("adsSkipped") == 2, "")


@task("live.t2.b2b_skip_then_unskippable", "media_t2", "live", SKIP, "web_media.skip_ad", difficulty="hard")
def live_b2b_u(env):
    tab = env.open("player.html", skin="a", ads="S2L10,U6")
    r = skip(env)
    f = env.fixture(tab)
    return Verdict(r, (not r["ok"]) and f.get("adsSkipped") == 1 and "then that ad can't be skipped" in (r["say"] or ""), "")


@task("live.t2.slow_page", "media_t2", "live", SKIP, "web_media.skip_ad")
def live_slow(env):
    tab = env.open("player.html", skin="a", ads="S3L20", slow="1.3")
    r = skip(env)
    return Verdict(r, r["ok"] and r["verified"] and env.fixture(tab).get("adsSkipped") == 1, "")


def _live_trusted(skin: str):
    @task(f"live.t2.untrusted_click_{skin}", "media_t2", "live", SKIP, "web_media.skip_ad", difficulty="hard")
    def run(env: LiveEnv) -> Verdict:
        tab = env.open("player.html", skin=skin, ads="S2L40", trusted="1")
        r = skip(env)
        f = env.fixture(tab)
        return Verdict(r, r["ok"] and r["verified"] and r["channel"] in ("ax", "pointer") and f.get("adsSkipped") == 1
                       and f.get("rejectedUntrusted", 0) >= 1, "scripted click refused by the page; a real AX press worked",
                       {"latency_ms": (f.get("clickedAt", 0) - f.get("skipAvailableAt", 0)) if f.get("clickedAt") else None})

    return run


for _s in "abd":
    _live_trusted(_s)


@task("live.t2.no_ad_true_negative", "media_t2", "live", SKIP, "web_media.skip_ad")
def live_noad(env):
    env.open("player.html", skin="a")
    r = skip(env)
    return Verdict(r, r["ok"] and "No ad" in (r["say"] or ""), "")


@task("live.t2.background_window_tab", "media_t2", "live", SKIP, "web_media.skip_ad", difficulty="hard")
def live_bg(env):
    player = env.open("player.html", skin="b", ads="S3L12", new_window=True)
    front = env.open("tabs.html", new_window=True, title="In front")
    r = skip(env)
    f = env.fixture(player)
    return Verdict(r, r["ok"] and r["verified"] and f.get("adsSkipped") == 1
                   and r["evidence"]["tab"]["window_id"] == player.window_id, "skipped in a window that was behind another")


@task("live.t2.two_tabs_one_ad", "media_t2", "live", SKIP, "web_media.skip_ad", difficulty="hard")
def live_two(env):
    a = env.open("player.html", skin="a", title="Quiet")
    b = env.open("player.html", skin="c", ads="S3L12", title="Has ad")
    r = skip(env)
    return Verdict(r, r["ok"] and env.fixture(b).get("adsSkipped") == 1 and env.fixture(a).get("clicks", 0) == 0, "")


@task("live.t2.midroll", "media_t2", "live", SKIP, "web_media.skip_ad", difficulty="hard")
def live_mid(env):
    tab = env.open("player.html", skin="a", mid="3:S2L10", content="60")
    time.sleep(4.0)  # content plays, then the mid-roll starts at 3 s
    r = skip(env)
    return Verdict(r, r["ok"] and r["verified"] and env.fixture(tab).get("adsSkipped") == 1, "")


@task("live.t2.latency_p95", "media_t2", "live", SKIP, "web_media.skip_ad", difficulty="hard")
def live_latency(env):
    lats, verified = [], 0
    last = None
    for i in range(10):
        tab = env.open("player.html", skin="abcde"[i % 5], ads="S3L12", wait=1.2)
        last = skip(env)
        f = env.fixture(tab)
        if last["ok"] and last["verified"] and f.get("clickedAt") and f.get("skipAvailableAt"):
            verified += 1
            lats.append(f["clickedAt"] - f["skipAvailableAt"])
        env.close_tabs()
    p95 = percentile([float(x) for x in lats], 0.95)
    return Verdict(last, verified == 10 and p95 is not None and p95 <= 1500,
                   f"10 ads, click p95 {p95} ms after the control became available", {"latencies_ms": lats, "p95_ms": p95})


# ===================================================================== controls =====

def _ctl(id_, skill, args, check, say):
    @task(id_, "media_controls", "live", say, skill)
    def run(env: LiveEnv) -> Verdict:
        tab = env.open("player.html", skin="a", content="100")
        time.sleep(2.0)
        if "seek" in skill:
            env.hands.skill_run("web_media.seek", {"to": 40})
            time.sleep(0.5)
        r = env.hands.skill_run(skill, args)
        time.sleep(0.4)
        f = env.fixture(tab)
        return Verdict(r, bool(r["ok"] and r["verified"] and check(f)), f"page: {json.dumps({k: f.get(k) for k in ('paused', 't', 'volume', 'muted', 'rate', 'captions')})}")

    return run


_ctl("live.media.pause", "web_media.pause", None, lambda f: f["paused"] is True,
     ["pause the video", "pause it", "hold on pause that", "pause youtube"])
_ctl("live.media.seek_back", "web_media.seek", {"seconds": -10}, lambda f: 25 <= f["t"] <= 36,
     ["go back 10 seconds", "rewind ten seconds", "back 10 seconds", "go back ten"])
_ctl("live.media.volume", "web_media.volume", {"level": 30}, lambda f: abs(f["volume"] - 0.3) < 0.05,
     ["volume to 30", "set the video volume to thirty", "make the video volume 30", "video volume thirty"])
_ctl("live.media.mute", "web_media.mute", None, lambda f: f["muted"] is True,
     ["mute the video", "mute the tab", "mute youtube", "mute the video pls"])
_ctl("live.media.rate", "web_media.speed", {"rate": 1.5}, lambda f: f["rate"] == 1.5,
     ["playback speed 1.5", "play at 1.5x", "speed up the video", "one and a half speed"])
_ctl("live.media.captions", "web_media.captions", None, lambda f: f["captions"] is True,
     ["turn on captions", "captions on", "toggle subtitles", "subtitles on"])
_ctl("live.media.next", "web_media.next", None, lambda f: f["nextClicks"] == 1,
     ["next video", "play the next video", "next episode", "next vid"])


@task("live.media.play_after_pause", "media_controls", "live", ["play the video", "resume the video", "press play again", "play it"], "web_media.play")
def live_play(env):
    tab = env.open("player.html", skin="a", content="100")
    time.sleep(1.5)
    env.hands.skill_run("web_media.pause")
    p = env.fixture(tab)["paused"]
    r = env.hands.skill_run("web_media.play")
    time.sleep(0.4)
    return Verdict(r, p is True and r["ok"] and r["verified"] and env.fixture(tab)["paused"] is False, "")


# ===================================================================== browser =====

def _tabs(env: LiveEnv):
    return env.chrome.tabs()


@task("live.browser.tabs_list_and_find", "browser", "live", ["list my tabs", "what tabs do i have open", "show my browser tabs", "wat tabs r open"], "browser.tabs_list")
def b_list(env):
    env.open("tabs.html", title="Alpha page")
    env.open("tabs.html", title="Beta page")
    r = env.hands.skill_run("browser.tabs_list")
    f = env.hands.skill_run("browser.tab_find", {"title_contains": "beta"})
    titles = [t["title"] for t in r["evidence"]["tabs"]]
    return Verdict(r, "Alpha page" in titles and "Beta page" in titles and f["evidence"]["n"] == 1, "")


@task("live.browser.tab_open_verified", "browser", "live", ["open a new tab to the fixture", "open this link", "open the site", "open dis link"], "browser.tab_open")
def b_open(env):
    url = env.srv.url("tabs.html", title="Opened by hands")
    r = env.hands.skill_run("browser.tab_open", {"url": url})
    time.sleep(0.8)
    found = [t for t in _tabs(env) if t.title == "Opened by hands"]
    if found:
        env.opened.append(found[0])
    return Verdict(r, r["ok"] and r["verified"] and bool(found), "")


@task("live.browser.tab_switch", "browser", "live", ["switch to the tab with alpha", "go to my tab with alpha", "bring up the alpha tab", "switch to alpha tab"], "browser.tab_switch")
def b_switch(env):
    a = env.open("tabs.html", title="Alpha page")
    env.open("tabs.html", title="Beta page")
    r = env.hands.skill_run("browser.tab_switch", {"title_contains": "Alpha"})
    time.sleep(0.4)
    active = [t for t in env.chrome.tabs(window_ids=[a.window_id]) if t.active]
    return Verdict(r, r["ok"] and r["verified"] and active and active[0].title == "Alpha page", "")


@task("live.browser.tab_close_and_undo", "browser", "live", ["close the tab with beta", "close that tab", "close my tab with beta", "close beta tab"], "browser.tab_close")
def b_close(env):
    env.open("tabs.html", title="Keep me")
    env.open("tabs.html", title="Close me")
    r = env.hands.skill_run("browser.tab_close", {"title_contains": "Close me"})
    time.sleep(0.6)
    gone = not [t for t in _tabs(env) if t.title == "Close me"]
    kept = [t for t in _tabs(env) if t.title == "Keep me"]
    return Verdict(r, r["ok"] and gone and bool(kept) and r["undo"]["skill"] == "browser.tab_open", "closed only that tab")


@task("live.browser.read_page", "browser", "live", ["read this page", "what does this page say", "summarize this page", "wat does dis say"], "browser.read_page")
def b_read(env):
    env.open("tabs.html", title="Readable")
    r = env.hands.skill_run("browser.read_page", {"title_contains": "Readable"})
    return Verdict(r, r["ok"] and "Plain page used by tab tests" in r["evidence"]["text"] and r["evidence"]["untrusted"] is True, "")


@task("live.browser.fill_form_benign", "browser", "live", ["fill in the form", "fill out this form", "type my name into the form", "fill the form fields"], "browser.fill_form")
def b_fill(env):
    tab = env.open("form.html")
    r = env.hands.skill_run("browser.fill_form", {"fields": {"Full name": "Ada Lovelace", "Email": "ada@example.com",
                                                                "Color": "green", "Newsletter": True}})
    vals = json.loads(env.js(tab, "JSON.stringify({n:document.getElementById('name').value,e:document.getElementById('email').value,c:document.getElementById('color').value,k:document.getElementById('news').checked,s:window.__submitted||null})"))
    return Verdict(r, r["ok"] and r["verified"] and vals == {"n": "Ada Lovelace", "e": "ada@example.com", "c": "green", "k": True, "s": None},
                   "filled, read back, did NOT submit")


@task("live.browser.fill_form_refuses_secrets", "browser", "live", ["fill in the password", "type my password in the form", "put my card number in", "fill the card field"], "browser.fill_form", difficulty="hard")
def b_fill_denied(env):
    tab = env.open("form.html")
    r = env.hands.skill_run("browser.fill_form", {"fields": {"Password": "hunter2", "Card number": "4111111111111111"}})
    vals = json.loads(env.js(tab, "JSON.stringify({p:document.getElementById('pw').value,c:document.getElementById('cc').value})"))
    return Verdict(r, (not r["ok"]) and vals == {"p": "", "c": ""}, "secure and card fields are never filled")


@task("live.browser.protected_site_not_opened", "browser", "live", ["open my bank", "open this link", "open the site", "open dis link"], "browser.tab_open", difficulty="hard")
def b_bank(env):
    before = len(_tabs(env))
    r = env.hands.skill_run("browser.tab_open", {"url": "https://www.chase.com/"})
    return Verdict(r, (not r["ok"]) and r["error"]["code"] == "denied" and len(_tabs(env)) == before, "")


@task("live.browser.only_http_opened", "browser", "live", ["open this file link", "open this link", "open the site", "open dis"], "browser.tab_open", difficulty="hard")
def b_scheme(env):
    r = env.hands.skill_run("browser.tab_open", {"url": "file:///etc/passwd"})
    return Verdict(r, r["error"]["code"] == "invalid_args", "")


# ======================================================================= calls =====

MUTE = ["mute my mic", "mute me", "mute the call", "mute my microphone"]


@task("live.calls.mute_unmute", "calls", "live", MUTE, "calls.mute")
def c_mute(env):
    tab = env.open("call.html")
    m = env.hands.skill_run("calls.mute", {"url_contains": "call.html"})
    s1 = env.attr(tab, "data-mic")
    u = env.hands.skill_run("calls.unmute", {"url_contains": "call.html"})
    s2 = env.attr(tab, "data-mic")
    return Verdict(m, m["ok"] and m["verified"] and s1 == "false" and u["ok"] and u["verified"] and s2 == "true", "")


@task("live.calls.mute_is_idempotent", "calls", "live", MUTE, "calls.mute")
def c_idem(env):
    tab = env.open("call.html")
    env.hands.skill_run("calls.mute", {"url_contains": "call.html"})
    r = env.hands.skill_run("calls.mute", {"url_contains": "call.html"})
    return Verdict(r, r["ok"] and env.attr(tab, "data-mic") == "false", "muting twice does not toggle it back on")


@task("live.calls.leave_asks", "calls", "live", ["leave the call", "hang up", "end the call", "leave the meeting"], "calls.leave", difficulty="hard")
def c_leave(env):
    tab = env.open("call.html")
    held = env.hands.skill_run("calls.leave", {"url_contains": "call.html"})
    mid = env.attr(tab, "data-left")
    done = approve(env.hands, "calls.leave", {"url_contains": "call.html"})
    return Verdict(done, held["needs_confirm"] and mid != "1" and done["ok"] and env.attr(tab, "data-left") == "1",
                   "leaving a call is confirmed first")


@task("live.calls.no_call_honest", "calls", "live", MUTE, "calls.mute")
def c_none(env):
    env.open("tabs.html", title="Not a call")
    r = env.hands.skill_run("calls.mute", {})
    return Verdict(r, (not r["ok"]) and r["error"]["code"] == "not_found", "")


# ================================================================ UI primitives =====

def _form_sel(role: str, name: str) -> dict[str, Any]:
    return {"browser": "chrome", "tab": {"url_contains": "form.html"}, "role": role, "name": name}


@task("live.ui.find_and_type", "ui_primitives", "live", ["type my name in the box", "put the name in the field", "fill in the name", "type name in field"], "")
def ui_type(env):
    tab = env.open("form.html")
    f = env.hands.ui_find(_form_sel("textbox", "Full name"))
    r = env.hands.ui_act(f["evidence"]["matches"][0]["ref"], "type", {"text": "Grace Hopper"},
                         verify={"kind": "dom_js", "selector": {"browser": "chrome", "tab": {"url_contains": "form.html"}},
                                 "js_expr": "document.getElementById('name').value==='Grace Hopper'"})
    return Verdict(r, f["ok"] and r["ok"] and r["verified"] and f["evidence"]["channel"] == "dom", "")


@task("live.ui.select_option", "ui_primitives", "live", ["choose red", "pick red for the color", "set color to red", "color red"], "")
def ui_select(env):
    tab = env.open("form.html")
    r = env.hands.ui_act(_form_sel("combobox", "Color"), "select", {"value": "red"})
    return Verdict(r, r["ok"] and env.js(tab, "document.getElementById('color').value") == "red", "")


@task("live.ui.press_checkbox", "ui_primitives", "live", ["tick the newsletter box", "check newsletter", "turn on the newsletter", "newsletter on"], "")
def ui_check(env):
    tab = env.open("form.html")
    r = env.hands.ui_act(_form_sel("checkbox", "Newsletter"), "press")
    return Verdict(r, r["ok"] and env.js(tab, "document.getElementById('news').checked") == "true", "")


@task("live.ui.wait_until_appears", "ui_primitives", "live", ["wait for the continue button", "wait until it loads", "tell me when it's ready", "wait til ready"], "")
def ui_wait(env):
    env.open("slow.html", delay="2", wait=0.4)
    r = env.hands.ui_wait_until({"kind": "dom", "selector": {"browser": "chrome", "tab": {"url_contains": "slow.html"},
                                                             "role": "button", "name": "Continue"}}, timeout_s=10)
    return Verdict(r, r["ok"] and r["evidence"]["waited_ms"] >= 1000 and r["evidence"]["polls"] >= 3, f"waited {r['evidence'].get('waited_ms')} ms")


@task("live.ui.wait_times_out_honestly", "ui_primitives", "live", ["wait for the continue button", "wait until it loads", "tell me when it's ready", "wait til ready"], "")
def ui_wait_timeout(env):
    env.open("tabs.html", title="Static")
    r = env.hands.ui_wait_until({"kind": "dom", "selector": {"browser": "chrome", "tab": {"url_contains": "tabs.html"},
                                                             "role": "button", "name": "Never there"}}, timeout_s=2)
    return Verdict(r, (not r["ok"]) and r["error"]["code"] == "timeout", "")


@task("live.ui.secure_field_refused", "ui_primitives", "live", ["type my password", "put my password in", "enter the password", "pw in"], "", difficulty="hard")
def ui_secure(env):
    tab = env.open("form.html")
    r = env.hands.ui_act(_form_sel("textbox", "Password"), "type", {"text": "hunter2"})
    return Verdict(r, (not r["ok"]) and env.js(tab, "document.getElementById('pw').value") == "", "")


@task("live.ui.stale_ref_after_page_change", "ui_primitives", "live", ["press that button", "click it", "press it again", "click dat"], "", difficulty="hard")
def ui_stale(env):
    tab = env.open("form.html")
    f = env.hands.ui_find(_form_sel("button", "Save"))
    env.chrome.js(tab, "location.href='about:blank'")
    time.sleep(1.0)
    r = env.hands.ui_act(f["evidence"]["matches"][0]["ref"], "press")
    return Verdict(r, (not r["ok"]) and r["error"]["code"] in ("stale_ref", "not_found"), "never presses a different element")


@task("live.ui.observe_page_outline", "ui_primitives", "live", ["what is on this page", "what can i click here", "describe the page", "wats on the page"], "")
def ui_observe(env):
    env.open("form.html")
    r = env.hands.ui_observe({"browser": "chrome", "tab": {"url_contains": "form.html"}})
    names = {e["name"] for e in r["evidence"]["elements"]}
    return Verdict(r, r["ok"] and "Save" in names and r["evidence"]["untrusted"] is True, "")


@task("live.ui.untrusted_provenance_asks", "ui_primitives", "live", ["click what the page says", "press the button it told me to", "do what the page said", "click dat"], "", difficulty="hard")
def ui_untrusted(env):
    tab = env.open("form.html")
    r = env.hands.ui_act(_form_sel("button", "Save"), "press", provenance="web")
    return Verdict(r, r["needs_confirm"] and env.js(tab, "String(window.__submitted||null)") == "null", "")


# ===================================================================== watchers =====

@task("live.watch.skip_ads_watcher", "watchers", "live", ["skip ads for me while i'm away", "keep skipping the ads for the next hour", "watch for ads and skip them", "auto skip adds"], "")
def live_watch(env):
    tab = env.open("player.html", skin="b", ads="S3L10,S3L10", wait=1.0)
    w = env.hands.watcher_create({"kind": "skip_ads", "scope": {"tab_url_contains": "player.html"}, "expires_in_s": 120})
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(2.5)
        env.hands.watchers.tick()
        if env.fixture(tab).get("adsSkipped", 0) >= 2:
            break
    f = env.fixture(tab)
    env.hands.watcher_cancel(w["evidence"]["watcher"]["id"])
    ev = [e for e in env.hands.watcher_events()["evidence"]["events"] if e["type"] == "fired"]
    after = env.js(tab, "String((window.__evHandsWatch||{}).armed)")
    return Verdict(w, w["ok"] and f.get("adsSkipped") == 2 and bool(ev) and after == "false",
                   f"skipped {f.get('adsSkipped')} ads with no further input; cancelled -> page script stopped")


@task("live.watch.idle_cpu", "watchers", "live", ["skip ads for me while i'm away", "keep skipping ads", "watch for ads", "auto skip adds"], "", difficulty="hard")
def live_cpu(env):
    env.open("player.html", skin="a", ads="", wait=1.0)
    w = env.hands.watcher_create({"kind": "skip_ads", "scope": {"tab_url_contains": "player.html"}, "expires_in_s": 300})
    pid = os.getpid()
    t0, c0 = time.time(), cpu_seconds(pid)
    env.hands.watchers.start()
    time.sleep(40)
    env.hands.watchers.stop()
    wall, cpu = time.time() - t0, cpu_seconds(pid) - c0
    pct = 100.0 * cpu / wall
    env.hands.watcher_cancel(w["evidence"]["watcher"]["id"])
    return Verdict(w, pct <= 1.0, f"host CPU {pct:.2f}% of one core over {wall:.0f} s polling (osascript children not included)",
                   {"cpu_pct": pct})


# ===================================================================== learning =====

def _wf_sel(role: str, name: str) -> dict[str, Any]:
    return {"browser": "chrome", "tab": {"url_contains": "workflow.html"}, "role": role, "name": name}


_WF_POST = {"kind": "dom_js", "selector": {"browser": "chrome", "tab": {"url_contains": "workflow.html"}},
            "js_expr": "document.getElementById('status').textContent.indexOf('Saved: Ada')===0"}


def _learn_workflow(env: LiveEnv):
    h = env.hands
    env.open("workflow.html", v="a")
    h.trace_begin("save my display name", params={"name": "Ada"})
    h.ui_act(_wf_sel("textbox", "Display name"), "type", {"text": "Ada"})
    h.ui_act(_wf_sel("checkbox", "Email notifications"), "press")
    h.ui_act(_wf_sel("combobox", "Theme"), "select", {"value": "dark"})
    h.ui_act(_wf_sel("button", "Save"), "press")
    h.ui_verify(_WF_POST)

    def fresh():
        env.close_tabs()
        env.open("workflow.html", v="a")

    return h.trace_end(replay_setup=fresh, params_for_replay={"name": "Grace"})


@task("live.learn.compile_and_replay", "learning", "live", ["learn how to save my display name", "remember how you did that", "make that a skill", "save dat as skill"], "")
def learn_basic(env):
    r = _learn_workflow(env)
    spec = env.hands.learned.load("learned.save_my_display_name")
    blob = json.dumps(spec)
    ok = r["ok"] and r["verified"] and r["learned"]["status"] == "learned_unreviewed" and '"frame"' not in blob \
        and "{name}" in blob and "Ada" not in json.dumps(spec["steps"])
    return Verdict(r, bool(ok), "compiled, parameterised ({name}), replayed on a fresh page, no coordinates")


def _heal(variant: str):
    @task(f"live.learn.heal_variant_{variant}", "learning", "live", ["do my save display name skill", "run the learned save skill", "save my display name again", "do dat skill"], "", difficulty="hard")
    def run(env: LiveEnv) -> Verdict:
        if env.hands.learned.load("learned.save_my_display_name") is None:
            _learn_workflow(env)
        env.close_tabs()
        tab = env.open("workflow.html", v=variant)
        r = approve(env.hands, "learned.save_my_display_name", {"name": "Zed"})
        status = env.js(tab, "document.getElementById('status').textContent")
        spec = env.hands.learned.load("learned.save_my_display_name")
        ok = r["ok"] and r["verified"] and status.startswith("Saved: Zed") and r["learned"]["status"] == "learned_unreviewed"
        return Verdict(r, bool(ok), f"page says {status!r}; skill v{spec['version']}", {"repairs": (r.get("evidence") or {}).get("repairs")})

    return run


for _v in "bcde":
    _heal(_v)


@task("live.learn.untrusted_never_learned", "learning", "live", ["learn what that email told me to do", "remember that", "make a skill from that", "learn dat"], "", difficulty="hard")
def learn_untrusted(env):
    h = env.hands
    env.open("workflow.html", v="a")
    h.trace_begin("do what the email said", provenance="email", params={"name": "Ada"})
    h.ui_act(_wf_sel("textbox", "Display name"), "type", {"text": "Ada"}, provenance="owner")
    h.ui_verify(_WF_POST)
    r = h.trace_end()
    return Verdict(r, (not r["ok"]) and h.learned.load("learned.do_what_the_email_said") is None, "")


@task("live.learn.unverified_never_learned", "learning", "live", ["learn this", "remember how you did that", "make that a skill", "learn dat"], "", difficulty="hard")
def learn_unverified(env):
    h = env.hands
    env.open("workflow.html", v="a")
    h.trace_begin("half done task", params={"name": "Ada"})
    h.ui_act(_wf_sel("textbox", "Display name"), "type", {"text": "Ada"})
    r = h.trace_end()  # no verified postcondition
    return Verdict(r, not r["ok"], "")


def _novel(id_, setup, steps, post, params):
    @task(id_, "learning", "live", ["learn how to do that", "remember how you did that", "make that a skill", "learn dat"], "", difficulty="hard")
    def run(env: LiveEnv) -> Verdict:
        h = env.hands
        setup(env)
        h.trace_begin(id_.replace("live.learn.", "novel "), params=params)
        for s in steps:
            if s[0] == "wait":
                h.ui_wait_until(s[1], timeout_s=8)
            else:
                h.ui_act(s[1], s[2], s[3] if len(s) > 3 else None)
        h.ui_verify(post)

        def fresh():
            env.close_tabs()
            setup(env)

        r = h.trace_end(replay_setup=fresh, params_for_replay=params)
        return Verdict(r, r["ok"] and r["verified"], "novel task compiled and replayed on a fresh instance")

    return run


_FORM = {"browser": "chrome", "tab": {"url_contains": "form.html"}}
_novel("live.learn.novel_form_two_fields", lambda e: e.open("form.html"),
       [("act", {**_FORM, "role": "textbox", "name": "Email"}, "type", {"text": "x@y.org"}),
        ("act", {**_FORM, "role": "checkbox", "name": "Newsletter"}, "press")],
       {"kind": "dom_js", "selector": _FORM, "js_expr": "document.getElementById('email').value==='x@y.org'&&document.getElementById('news').checked"},
       {"addr": "x@y.org"})
_novel("live.learn.novel_form_save", lambda e: e.open("form.html"),
       [("act", {**_FORM, "role": "textbox", "name": "Full name"}, "type", {"text": "Linus"}),
        ("act", {**_FORM, "role": "button", "name": "Save"}, "press")],
       {"kind": "dom_js", "selector": _FORM, "js_expr": "document.getElementById('out').textContent==='submitted'"},
       {"person": "Linus"})
_novel("live.learn.novel_wait_then_click", lambda e: e.open("slow.html", delay="1.5", wait=0.3),
       [("wait", {"kind": "dom", "selector": {"browser": "chrome", "tab": {"url_contains": "slow.html"}, "role": "button", "name": "Continue"}}),
        ("act", {"browser": "chrome", "tab": {"url_contains": "slow.html"}, "role": "button", "name": "Continue"}, "press")],
       {"kind": "dom_js", "selector": {"browser": "chrome", "tab": {"url_contains": "slow.html"}}, "js_expr": "document.getElementById('status').textContent==='ready'"},
       {})
_novel("live.learn.novel_mute_mic", lambda e: e.open("call.html"),
       [("act", {"browser": "chrome", "tab": {"url_contains": "call.html"}, "role": "button", "name": "microphone"}, "press")],
       {"kind": "dom_js", "selector": {"browser": "chrome", "tab": {"url_contains": "call.html"}}, "js_expr": "document.documentElement.getAttribute('data-mic')==='false'"},
       {})
_novel("live.learn.novel_player_captions", lambda e: e.open("player.html", skin="a", content="60"),
       [("act", {"browser": "chrome", "tab": {"url_contains": "player.html"}, "role": "button", "name": "captions"}, "press")],
       {"kind": "dom_js", "selector": {"browser": "chrome", "tab": {"url_contains": "player.html"}}, "js_expr": "document.documentElement.getAttribute('data-fixture').indexOf('\"captions\":true')>0"},
       {})


# ================================================================= screen reference =====

@task("live.ref.settings_toggles", "screenshot_reference", "live", ["make the settings match the screenshot", "do what this screenshot shows", "set the toggles like the picture", "match the pic"], "")
def ref_toggles(env):
    tab = env.open("form.html")
    shot = env.tmp / "toggles.png"
    from app.fabric.skills import synth

    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(synth.render("single", seed=1)[0].to_png())
    h = env.make_hands(extra={"last_image": str(shot), "toggle_override": [{"label": "Newsletter", "on": True}],
                              "toggle_scope": {"browser": "chrome", "tab": {"url_contains": "form.html"}}})
    plan = h.screen_reference(str(shot), "settings_toggles")
    pid = plan["confirm"]["plan_id"]
    held = h.screen_apply(pid)
    done = h.screen_apply(pid, approved=True, approval_id=pid)
    on = env.js(tab, "document.getElementById('news').checked")
    return Verdict(done, plan["needs_confirm"] and held["needs_confirm"] and done["ok"] and done["verified"] and on == "true", "")


@task("live.ref.settings_toggles_already_match", "screenshot_reference", "live", ["make the settings match the screenshot", "do what this screenshot shows", "set the toggles like the picture", "match the pic"], "")
def ref_toggles_same(env):
    tab = env.open("form.html")
    shot = env.tmp / "toggles2.png"
    from app.fabric.skills import synth

    shot.write_bytes(synth.render("single", seed=1)[0].to_png())
    h = env.make_hands(extra={"last_image": str(shot), "toggle_override": [{"label": "Newsletter", "on": False}],
                              "toggle_scope": {"browser": "chrome", "tab": {"url_contains": "form.html"}}})
    plan = h.screen_reference(str(shot), "settings_toggles")
    row = plan["confirm"]["rows"][0]
    return Verdict(plan, row["will_run"] is False and "already" in " ".join(row["warnings"]), "nothing to change, nothing pressed")


_ = registry
