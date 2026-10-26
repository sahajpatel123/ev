"""Browser drivers against a scripted runtime: Chrome and Safari scripts, errors, exclusions."""

from __future__ import annotations

import pytest

from app.fabric.skills.packs.browsers import BrowserError, ChromeDriver, SafariDriver, SEP, host_excluded
from app.fabric.skills.runtime import Proc
from app.fabric.skills.testing import FakeRuntime


def _rt(app: str):
    rt = FakeRuntime()
    rt.when(r"^ps ", Proc(True, out=f"/Applications/{app}.app/Contents/MacOS/{app}\n"), kind="run")
    return rt


def test_chrome_tabs_parse_and_private_windows_are_dropped():
    rt = _rt("Google Chrome")
    rows = [SEP.join(["11", "101", "1", "true", "Inbox, work", "https://mail.example.com/", "normal"]),
            SEP.join(["12", "201", "1", "true", "Private", "https://secret.example.com/", "incognito"]),
            SEP.join(["11", "102", "2", "false", "Bank", "https://www.chase.com/", "normal"])]
    rt.when(r"repeat with w in windows", "\n".join(rows) + "\n")
    tabs = ChromeDriver(rt).tabs()
    assert [t.tab_id for t in tabs] == ["101"]  # incognito and banking hosts never listed
    assert tabs[0].title == "Inbox, work" and tabs[0].active
    assert [t.tab_id for t in ChromeDriver(rt).tabs(include_excluded=True)] == ["101", "102"]


def test_chrome_js_uses_numeric_ids_only_and_quotes_the_code():
    rt = _rt("Google Chrome")
    rt.when(r"execute t javascript", "42")
    d = ChromeDriver(rt)
    rows = [SEP.join(["11", "101", "1", "true", "A", "https://a.example.com/", "normal"])]
    rt.when(r"repeat with w in windows", "\n".join(rows) + "\n")
    t = d.tabs()[0]
    assert d.js(t, 'return "x\\y"') == "42"
    script = rt.scripts("execute t javascript")[0]
    assert "tab id 101 of window id 11" in script and '\\"x\\\\y\\"' in script
    t.tab_id = '1 of window 1" & (do shell script "x") & "'
    with pytest.raises(BrowserError) as e:
        d.js(t, "1")
    assert e.value.code == "invalid_args"


@pytest.mark.parametrize("err,code", [
    ("Executing JavaScript through AppleScript is turned off. To turn it on, from the menu bar, go to View > Developer > Allow JavaScript from Apple Events.", "js_disabled"),
    ("Not authorized to send Apple events to Google Chrome. (-1743)", "permission_missing"),
    ("Application isn't running. (-600)", "not_running"),
    ("Can't get tab id 5 of window id 1. (-1728)", "not_found"),
])
def test_chrome_errors_are_classified_with_a_fix(err, code):
    rt = _rt("Google Chrome")
    rt.when(r"execute t javascript", Proc(False, err=err))
    d = ChromeDriver(rt)
    rt.when(r"repeat with w in windows", SEP.join(["11", "101", "1", "true", "A", "https://a.example.com/", "normal"]) + "\n")
    with pytest.raises(BrowserError) as e:
        d.js(d.tabs()[0], "1")
    assert e.value.code == code
    if code in ("js_disabled", "permission_missing"):
        assert e.value.fix


def test_safari_scripts_and_index_tab_ids():
    rt = _rt("Safari")
    rows = [SEP.join(["7", "1", "1", "false", "One", "https://one.example.com/", "normal"]),
            SEP.join(["7", "2", "2", "true", "Two", "https://two.example.com/", "normal"])]
    rt.when(r"repeat with w in windows", "\n".join(rows) + "\n")
    rt.when(r"do JavaScript", "ok")
    d = SafariDriver(rt)
    tabs = d.tabs()
    assert [t.index for t in tabs] == [1, 2] and tabs[1].active
    assert d.js(tabs[1], "1+1") == "ok"
    assert "in (tab 2 of window id 7)" in rt.scripts("do JavaScript")[0]


def test_safari_js_disabled_names_the_developer_setting():
    rt = _rt("Safari")
    rt.when(r"repeat with w in windows", SEP.join(["7", "1", "1", "true", "One", "https://one.example.com/", "normal"]) + "\n")
    rt.when(r"do JavaScript", Proc(False, err="You must enable 'Allow JavaScript from Apple Events' in the Developer section of Safari Settings."))
    d = SafariDriver(rt)
    with pytest.raises(BrowserError) as e:
        d.js(d.tabs()[0], "1")
    assert e.value.code == "js_disabled" and "Developer" in e.value.fix


def test_not_running_browsers_raise_instead_of_launching():
    rt = FakeRuntime()
    rt.when(r"^ps ", Proc(True, out=""), kind="run")
    with pytest.raises(BrowserError) as e:
        ChromeDriver(rt).tabs()
    assert e.value.code == "not_running" and rt.calls[-1]["kind"] == "run"  # no osascript was sent


def test_host_exclusion_matches_subdomains_only_on_dot_boundaries():
    assert host_excluded("www.chase.com") and host_excluded("chase.com")
    assert not host_excluded("notchase.com")
    assert not host_excluded("chase.com.evil.example")
