"""T2 flow: find the playing tab, skip the ad, answer honestly. Page behaviour is simulated on a
virtual clock; the real page scripts are exercised live in test_live_*.py."""

from __future__ import annotations

import pytest

from app.fabric.skills.packs.browsers import BrowserError
from app.fabric.skills.packs.web_media import WebMedia
from app.fabric.skills.testing import FakeBrowser, FakeRuntime, PageSim


def _wm(rt, *pages, ax=None):
    fb = FakeBrowser(rt)
    for i, (win, title, url, page, active) in enumerate(pages, start=1):
        fb.add(win, f"t{i}", title, url, page, active=active)
    return WebMedia(rt, [fb], ax=ax), fb


def S(skip_after=5, length=15):
    return {"kind": "S", "skip_after": skip_after, "len": length}


def U(length=8):
    return {"kind": "U", "skip_after": 0, "len": length}


def test_skippable_ad_with_countdown_is_skipped_at_the_first_instant_and_verified():
    rt = FakeRuntime()
    page = PageSim(rt, [S(5, 15)])
    wm, fb = _wm(rt, ("w1", "Video", "https://www.youtube.com/watch?v=x", page, True))
    r = wm.skip_ad()
    assert r["ok"] and r["verified"], r["say"]
    assert r["say"] == "Skipped the ad."
    assert page.skipped == 1 and page.cur is None
    assert r["channel"] == "dom"
    # the in-page armed click fired exactly when the control became available
    assert page.clicked_at == pytest.approx(page.skip_available_at, abs=1e-6)
    assert [s["phase"] for s in r["steps"] if s["phase"] in ("act", "wait", "verify")] == ["act", "wait", "verify"]


def test_skip_already_ready_uses_a_direct_click():
    rt = FakeRuntime()
    page = PageSim(rt, [S(2, 15)])
    rt.sleep(3)  # control is already available
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True))
    r = wm.skip_ad()
    assert r["verified"] and page.skipped == 1
    assert any(s["op"] == "dom click skip" for s in r["steps"])


def test_unskippable_ad_is_reported_with_remaining_time_never_skipped():
    rt = FakeRuntime()
    page = PageSim(rt, [U(12)])
    rt.sleep(1)
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True))
    r = wm.skip_ad()
    assert r["ok"] is False and r["verified"] is False  # nothing was achieved
    assert 8 <= r["evidence"]["remaining_s"] <= 11  # ...but what was perceived is reported
    assert r["error"]["code"] == "precondition_failed"
    assert "can't be skipped" in r["say"] and "seconds left" in r["say"]
    assert page.skipped == 0 and page.clicks == 0


def test_back_to_back_skippable_then_unskippable():
    rt = FakeRuntime()
    page = PageSim(rt, [S(3, 10), U(7)])
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True))
    r = wm.skip_ad()
    assert page.skipped == 1
    assert r["ok"] is False
    assert r["say"].startswith("Skipped 1 ad, then that ad can't be skipped")


def test_two_skippable_ads_in_a_row_are_both_skipped():
    rt = FakeRuntime()
    page = PageSim(rt, [S(2, 10), S(2, 10)])
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True))
    r = wm.skip_ad()
    assert r["ok"] and r["verified"] and page.skipped == 2
    assert r["say"] == "Skipped 2 ads."


@pytest.mark.parametrize("state,code,phrase", [
    ({"locked": True}, "locked", "Your Mac is locked"),
    ({"display_asleep": True}, "display_asleep", "display is asleep"),
])
def test_locked_or_asleep_is_said_exactly_and_nothing_is_touched(state, code, phrase):
    rt = FakeRuntime()
    rt.sys.update(state)
    page = PageSim(rt, [S(5)])
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True))
    r = wm.skip_ad()
    assert r["ok"] is False and r["error"]["code"] == code and phrase in r["say"]
    assert fb.js_calls == [] and page.clicks == 0


def test_no_ad_playing_is_a_true_negative():
    rt = FakeRuntime()
    page = PageSim(rt, [])
    wm, fb = _wm(rt, ("w1", "Cats", "http://localhost/p", page, True))
    r = wm.skip_ad()
    assert r["ok"] and r["verified"] and "No ad" in r["say"] and r["evidence"]["ad"] is False


def test_nothing_playing_at_all():
    rt = FakeRuntime()
    page = PageSim(rt, [], playing=False)
    wm, fb = _wm(rt, ("w1", "Doc", "http://localhost/p", page, True))
    r = wm.skip_ad()
    assert r["ok"] and "Nothing is playing" in r["say"]


def test_background_tab_in_another_window_is_found_and_other_tab_untouched():
    rt = FakeRuntime()
    quiet = PageSim(rt, [])
    ad = PageSim(rt, [S(4, 12)])
    wm, fb = _wm(rt, ("w1", "Docs", "https://docs.example.com/x", None, True),
                 ("w1", "Cats", "https://cats.example.com/", quiet, False),
                 ("w2", "Talk", "https://www.youtube.com/watch?v=a", ad, False))
    r = wm.skip_ad()
    assert r["ok"] and r["verified"] and ad.skipped == 1
    assert quiet.clicks == 0
    assert r["evidence"]["tab"]["window_id"] == "w2"
    # nothing in the control path asks for focus: no activation call was made
    assert not any("activate" in c[1] for c in fb.js_calls)


def test_two_playing_tabs_with_ads_skips_the_visible_one_and_says_so():
    rt = FakeRuntime()
    a = PageSim(rt, [S(3, 12)])
    b = PageSim(rt, [S(3, 12)])
    wm, fb = _wm(rt, ("w1", "Back", "https://a.example.com/", a, False),
                 ("w2", "Front", "https://b.example.com/", b, True))
    r = wm.skip_ad()
    assert b.skipped == 1 and a.skipped == 0
    assert "1 other tab" in r["say"]


def test_js_disabled_is_reported_with_the_exact_setting():
    rt = FakeRuntime()
    err = BrowserError("js_disabled", "Google Chrome blocks JavaScript from Apple Events",
                       "In Chrome choose View > Developer > Allow JavaScript from Apple Events.")
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", err, True))
    r = wm.skip_ad()
    assert r["ok"] is False and r["error"]["code"] == "js_disabled"
    assert "Allow JavaScript from Apple Events" in r["say"] and r["error"]["fix"]


def test_automation_permission_missing_names_the_setting():
    rt = FakeRuntime()
    err = BrowserError("permission_missing", "macOS blocked Apple Events to Google Chrome",
                       "System Settings > Privacy & Security > Automation: allow this app to control Google Chrome.")
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", err, True))
    r = wm.skip_ad()
    assert r["error"]["code"] == "permission_missing" and "Automation" in r["say"]


def test_browser_not_running_says_so():
    rt = FakeRuntime()
    fb = FakeBrowser(rt)
    fb.is_running = False
    r = WebMedia(rt, [fb]).skip_ad()
    assert r["ok"] and "Nothing is playing" in r["say"] or r["error"]["code"] == "not_running"
    assert fb.js_calls == []


class _FakeAx:
    def __init__(self, page, *, press_works=True):
        self.page, self.press_works, self.pressed = page, press_works, 0

    def available(self):
        return True

    def enable(self, app):
        return {"ok": True}

    def find(self, app, **kw):
        return {"ok": True, "matches": [{"path": [0, 3, 1], "pos": [400, 300], "size": [60, 24]}]}

    def act(self, app, path, verb, **kw):
        self.pressed += 1
        if self.press_works:
            self.page.trusted_only = False
            self.page._click(self.page.rt.now(), trusted=True)
        return {"ok": True}

    def click(self, x, y, **kw):
        self.page._click(self.page.rt.now(), trusted=True)
        return {"ok": True}


def test_page_ignoring_scripted_clicks_falls_back_to_a_real_accessibility_press():
    rt = FakeRuntime()
    page = PageSim(rt, [S(2, 20)], trusted_only=True)
    rt.sleep(3)
    ax = _FakeAx(page)
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True), ax=ax)
    r = wm.skip_ad()
    assert r["ok"] and r["verified"], r["say"]
    assert page.rejected >= 1 and ax.pressed == 1
    assert r["channel"] == "ax"


def test_when_every_rung_fails_the_receipt_says_unverified_not_skipped():
    rt = FakeRuntime()
    page = PageSim(rt, [S(2, 60)], trusted_only=True)
    rt.sleep(3)
    ax = _FakeAx(page, press_works=False)
    ax.click = lambda x, y, **kw: {"ok": True}
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True), ax=ax)
    r = wm.skip_ad()
    assert r["ok"] is False and r["verified"] is False
    assert "Skipped" not in (r["say"] or "")
    assert r["error"]["code"] in ("unverified", "internal")


def test_slow_page_that_ends_the_ad_a_moment_after_the_click_still_verifies():
    rt = FakeRuntime()
    page = PageSim(rt, [S(2, 30)], slow=1.2)
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True))
    r = wm.skip_ad()
    assert r["ok"] and r["verified"] and r["say"] == "Skipped the ad."


def test_skip_that_is_too_far_away_is_not_waited_for():
    rt = FakeRuntime()
    page = PageSim(rt, [S(120, 150)])
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True))
    r = wm.skip_ad(wait_cap_s=40)
    assert r["ok"] is False and page.clicks == 0


def test_banking_hosts_and_private_windows_are_never_scripted():
    rt = FakeRuntime()
    bank = PageSim(rt, [S(1, 5)])
    priv = PageSim(rt, [S(1, 5)])
    wm, fb = _wm(rt, ("w1", "Your accounts", "https://www.chase.com/dashboard", bank, True))
    fb.add("w2", "tp", "private", "https://example.com/", priv, incognito=True)
    r = wm.skip_ad()
    assert bank.clicks == 0 and priv.clicks == 0
    assert fb.js_calls == []
    assert r["ok"] and "Nothing is playing" in r["say"]


# ------------------------------------------------------------- controls ---

@pytest.mark.parametrize("action,value,check", [
    ("pause", None, lambda p: p.paused is True),
    ("seek_by", -10, lambda p: abs(p.content_t - 20.0) < 1.0),
    ("seek_to", 45, lambda p: abs(p.content_t - 45.0) < 1.0),
    ("volume_to", 0.4, lambda p: abs(p.volume - 0.4) < 1e-9),
    ("mute", None, lambda p: p.muted is True),
    ("rate", 1.5, lambda p: p.rate == 1.5),
])
def test_controls_act_and_read_back(action, value, check):
    rt = FakeRuntime()
    page = PageSim(rt, [])
    page.content_t = 30.0
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True))
    r = wm.control(action, value)
    assert r["ok"] and r["verified"], r["say"]
    assert check(page)


def test_play_then_toggle_and_captions():
    rt = FakeRuntime()
    page = PageSim(rt, [], playing=True)
    wm, fb = _wm(rt, ("w1", "Video", "http://localhost/p", page, True))
    assert wm.control("toggle")["say"] == "Paused."
    page.paused = True
    # a paused page is not "playing" but still controllable
    r = wm.control("play")
    assert r["ok"] and page.paused is False
    assert wm.control("captions")["ok"] and page.captions is True


def test_control_without_media_is_an_honest_failure():
    rt = FakeRuntime()
    wm, fb = _wm(rt, ("w1", "Doc", "http://localhost/p", None, True))
    r = wm.control("pause")
    assert r["ok"] is False and r["error"]["code"] == "not_found"


def test_status_lists_playing_tabs_and_ad_state():
    rt = FakeRuntime()
    a = PageSim(rt, [S(5, 15)])
    b = PageSim(rt, [])
    wm, fb = _wm(rt, ("w1", "A", "https://www.youtube.com/watch?v=1", a, True),
                 ("w1", "B", "https://example.com/v", b, False))
    r = wm.status()
    states = {x["title"]: x["state"] for x in r["evidence"]["playing"]}
    assert states == {"A": "ad", "B": "playing"}
    assert r["say"] == "2 tabs are playing media."


def test_prefer_url_chooses_among_ads():
    rt = FakeRuntime()
    a = PageSim(rt, [S(2, 12)])
    b = PageSim(rt, [S(2, 12)])
    wm, fb = _wm(rt, ("w1", "A", "https://a.example.com/", a, True),
                 ("w2", "B", "https://b.example.com/", b, False))
    wm.skip_ad(prefer_url="b.example")
    assert b.skipped == 1 and a.skipped == 0
