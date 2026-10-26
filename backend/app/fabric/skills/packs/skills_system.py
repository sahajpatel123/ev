"""Skills: Finder, system, music, calls, editors."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .. import receipts as R
from .. import system_state
from ..receipts import Recorder
from ..registry import Ctx, skill
from ..runtime import applescript_quote, process_running
from .browsers import BrowserError
from .common import browsers, card_plan, shell_risk

STR = {"type": "string"}
NUM = {"type": "number"}


def _roots(ctx: Ctx) -> list[Path]:
    from app.fabric.roots import default_roots

    home = Path(ctx.home) if ctx.home else ctx.rt.home()
    return default_roots(home=home, extras=str(ctx.extra.get("roots_extra", "")))


def _inside(path: Path, roots: list[Path]) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    return any(rp == r or r in rp.parents for r in roots)


def _denied(path: str) -> bool:
    from app.fabric import policy

    return policy.decide("read", path) == "DENY"


# ------------------------------------------------------------------ finder ----

def _reveal_check(ctx: Ctx, path: Path) -> tuple[bool, str]:
    p = ctx.rt.osascript('tell application "Finder"\n  set s to selection\n'
                         "  if (count of s) = 0 then return \"\"\n"
                         "  return POSIX path of (item 1 of s as alias)\nend tell", timeout=10)
    got = (p.out or "").strip().rstrip("/")
    return got == str(path).rstrip("/"), got


@skill("finder.reveal", "Show a file or folder in Finder, selected.",
       ["reveal in finder", "show it in finder", "show me that file in finder", "open the folder containing",
        "show this file in finder", "reveal the file"], risk="low", channels=["applescript", "cli"],
       permissions=["automation:Finder"],
       args_schema={"type": "object", "required": ["path"], "properties": {"path": STR}})
def reveal(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("finder.reveal", clock=ctx.rt.monotonic)
    path = Path(str(args["path"])).expanduser()
    if _denied(str(path)):
        return R.fail("denied", "That location is protected.")
    if not path.exists():
        return R.fail("not_found", f"{path.name} doesn't exist.")
    p = ctx.rt.run(["open", "-R", str(path)], timeout=15)
    rec.add("act", "open -R", p.ok, channel="cli")
    if not p.ok:
        return rec.finish(False, err=R.error("internal", (p.err or "open failed")[:120]))
    ctx.rt.sleep(0.6)
    ok, got = _reveal_check(ctx, path.resolve())
    rec.add("verify", "selected in Finder", ok, channel="applescript", evidence={"selection": got})
    return rec.finish(ok, evidence={"path": str(path)}, say=f"Showing {path.name} in Finder." if ok else None,
                      err=None if ok else R.error("unverified", "Finder did not select it"))


@skill("finder.select", "Select a file in Finder without bringing Finder forward.",
       ["select the file in finder", "highlight that file", "select it in finder"], risk="low",
       channels=["applescript"], permissions=["automation:Finder"],
       args_schema={"type": "object", "required": ["path"], "properties": {"path": STR}})
def select(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("finder.select", clock=ctx.rt.monotonic)
    path = Path(str(args["path"])).expanduser()
    if _denied(str(path)):
        return R.fail("denied", "That location is protected.")
    if not path.exists():
        return R.fail("not_found", f"{path.name} doesn't exist.")
    p = ctx.rt.osascript('tell application "Finder"\n'
                         f"  reveal (POSIX file {applescript_quote(str(path.resolve()))} as alias)\nend tell", timeout=15)
    rec.add("act", "reveal", p.ok, channel="applescript")
    if not p.ok:
        return rec.finish(False, err=R.error("permission_missing" if p.permission_denied else "internal",
                                              (p.err or "")[:120]))
    ok, got = _reveal_check(ctx, path.resolve())
    rec.add("verify", "selected", ok, channel="applescript", evidence={"selection": got})
    return rec.finish(ok, say="Selected." if ok else None)


def _fs_risk(args: dict[str, Any], ctx: Any = None) -> tuple[str, str | None]:
    if ctx is None:
        return "run", None
    roots = _roots(ctx)
    for key in ("src", "path", "dst"):
        if args.get(key) and not _inside(Path(str(args[key])).expanduser(), roots):
            return "run", None  # outside the roots: confirm
    if args.get("overwrite"):
        return "run", None
    return "write", None


@skill("finder.move", "Move a file or folder (inside your folders; asks first outside them or when overwriting).",
       ["move the file to", "move this to", "put that file in", "move it into", "move that folder"],
       risk="dynamic", risk_fn=_fs_risk, channels=["fs"],
       args_schema={"type": "object", "required": ["src", "dst"],
                    "properties": {"src": STR, "dst": STR, "overwrite": {"type": "boolean"}}},
       plan=lambda ctx, args: card_plan(
           "Move a file or folder" + (" (replacing what is there)" if args.get("overwrite") else ""),
           [{"label": "move", "from": str(args.get("src")), "to": str(args.get("dst")),
             "overwrite": bool(args.get("overwrite"))}], op="move", ctx=ctx))
def move(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("finder.move", clock=ctx.rt.monotonic)
    src, dst = Path(str(args["src"])).expanduser(), Path(str(args["dst"])).expanduser()
    if _denied(str(src)) or _denied(str(dst)):
        return R.fail("denied", "That location is protected.")
    if not src.exists():
        return R.fail("not_found", f"{src.name} doesn't exist.")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists() and not args.get("overwrite"):
        return R.fail("precondition_failed", f"{dst.name} already exists there.")
    before = src.stat().st_size if src.is_file() else None
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    rec.add("act", "move", True, channel="fs", evidence={"to": str(dst)})
    ok = dst.exists() and not src.exists() and (before is None or dst.stat().st_size == before)
    rec.add("verify", "at destination, gone from source", ok, channel="fs")
    return rec.finish(ok, evidence={"src": str(src), "dst": str(dst)}, say=f"Moved {dst.name}." if ok else None,
                      undo={"skill": "finder.move", "args": {"src": str(dst), "dst": str(src)},
                            "description": "move it back"})


@skill("finder.rename", "Rename a file or folder in place.",
       ["rename the file", "rename this to", "change the file name", "call that file"],
       risk="dynamic", risk_fn=_fs_risk, channels=["fs"],
       args_schema={"type": "object", "required": ["path", "new_name"], "properties": {"path": STR, "new_name": STR}},
       plan=lambda ctx, args: card_plan(
           "Rename a file or folder",
           [{"label": "rename", "from": str(args.get("path")), "to": str(args.get("new_name"))}],
           op="rename", ctx=ctx))
def rename(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("finder.rename", clock=ctx.rt.monotonic)
    path = Path(str(args["path"])).expanduser()
    name = str(args["new_name"])
    if "/" in name or name in ("", ".", ".."):
        return R.fail("invalid_args", "A new name can't contain a slash.")
    if _denied(str(path)) or _denied(name):
        return R.fail("denied", "That location is protected.")
    if not path.exists():
        return R.fail("not_found", f"{path.name} doesn't exist.")
    target = path.with_name(name)
    if target.exists():
        return R.fail("precondition_failed", f"{name} already exists.")
    path.rename(target)
    rec.add("act", "rename", True, channel="fs")
    ok = target.exists() and not path.exists()
    rec.add("verify", "new name present, old gone", ok, channel="fs")
    return rec.finish(ok, evidence={"path": str(target)}, say=f"Renamed it to {name}." if ok else None,
                      undo={"skill": "finder.rename", "args": {"path": str(target), "new_name": path.name},
                            "description": "restore the old name"})


LABELS = {"none": 0, "orange": 1, "red": 2, "yellow": 3, "blue": 4, "purple": 5, "green": 6, "gray": 7}


@skill("finder.tag", "Set a Finder colour tag on a file or folder.",
       ["tag the file", "colour the file", "color label", "mark the file red", "label that file"],
       risk="low", channels=["applescript"], permissions=["automation:Finder"],
       args_schema={"type": "object", "required": ["path", "color"],
                    "properties": {"path": STR, "color": {"type": "string", "enum": list(LABELS)}}})
def tag(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("finder.tag", clock=ctx.rt.monotonic)
    path = Path(str(args["path"])).expanduser()
    if _denied(str(path)):
        return R.fail("denied", "That location is protected.")
    if not path.exists():
        return R.fail("not_found", f"{path.name} doesn't exist.")
    ref = f"(POSIX file {applescript_quote(str(path.resolve()))} as alias)"
    before = ctx.rt.osascript(f'tell application "Finder" to get label index of {ref}', timeout=10)
    want = LABELS[args["color"]]
    p = ctx.rt.osascript(f'tell application "Finder" to set label index of {ref} to {want}', timeout=10)
    rec.add("act", "set label", p.ok, channel="applescript")
    if not p.ok:
        return rec.finish(False, err=R.error("permission_missing" if p.permission_denied else "internal", (p.err or "")[:120]))
    back = ctx.rt.osascript(f'tell application "Finder" to get label index of {ref}', timeout=10)
    ok = (back.out or "").strip() == str(want)
    rec.add("verify", "label read back", ok, channel="applescript", evidence={"label": back.out})
    prev = (before.out or "0").strip()
    undo = {"skill": "finder.tag", "args": {"path": str(path), "color": next((k for k, v in LABELS.items() if str(v) == prev), "none")},
            "description": "restore the previous tag"}
    return rec.finish(ok, say=f"Tagged it {args['color']}." if ok else None, undo=undo)


# ------------------------------------------------------------------ system ----

def _vol_read(ctx: Ctx) -> tuple[int | None, bool | None]:
    st = system_state.read(ctx.rt, volume=True)
    return st.get("volume"), st.get("muted")


@skill("system.volume", "Set or change the Mac's output volume (0-100).",
       ["turn the volume up", "turn the volume down", "set the volume to", "volume up", "volume down", "make it louder",
        "make it quieter", "turn it up", "turn it down", "set volume"],
       risk="low", channels=["applescript"],
       args_schema={"type": "object", "properties": {"level": NUM, "delta": NUM}})
def volume(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("system.volume", clock=ctx.rt.monotonic)
    cur, muted = _vol_read(ctx)
    if cur is None:
        return R.fail("unsupported", "This Mac's output volume isn't readable (for example HDMI or some displays).")
    if "level" in args:
        want = int(args["level"])
    elif "delta" in args:
        want = cur + int(args["delta"])
    else:
        return R.fail("invalid_args", "volume needs 'level' or 'delta'.")
    want = max(0, min(100, want))
    p = ctx.rt.osascript(f"set volume output volume {want}", timeout=8)
    rec.add("act", "set volume", p.ok, channel="applescript", evidence={"from": cur, "to": want})
    if not p.ok:
        return rec.finish(False, err=R.error("internal", (p.err or "")[:120]))
    ctx.rt.sleep(0.15)
    now, _ = _vol_read(ctx)
    ok = now is not None and abs(now - want) <= 1
    rec.add("verify", "volume read back", ok, channel="applescript", evidence={"now": now})
    return rec.finish(ok, evidence={"before": cur, "after": now}, say=f"Volume is at {now}." if ok else None,
                      undo={"skill": "system.volume", "args": {"level": cur}, "description": f"set the volume back to {cur}"})


@skill("system.mute", "Mute or unmute the Mac's sound.",
       ["mute the mac", "mute my computer", "unmute the mac", "mute the sound", "unmute the sound", "mute everything"],
       risk="low", channels=["applescript"], args_schema={"type": "object", "properties": {"muted": {"type": "boolean"}}})
def mute(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("system.mute", clock=ctx.rt.monotonic)
    want = bool(args.get("muted", True))
    _, was = _vol_read(ctx)
    p = ctx.rt.osascript(f"set volume output muted {'true' if want else 'false'}", timeout=8)
    rec.add("act", "set muted", p.ok, channel="applescript")
    if not p.ok:
        return rec.finish(False, err=R.error("internal", (p.err or "")[:120]))
    _, now = _vol_read(ctx)
    ok = now == want
    rec.add("verify", "muted read back", ok, channel="applescript", evidence={"muted": now})
    return rec.finish(ok, say=("Muted." if want else "Unmuted.") if ok else None,
                      undo={"skill": "system.mute", "args": {"muted": bool(was)}, "description": "restore the previous mute state"})


@skill("system.brightness", "Change the display brightness (needs the `brightness` tool).",
       ["brightness up", "turn the brightness down", "make the screen brighter", "dim the screen", "set brightness"],
       risk="low", channels=["cli"], args_schema={"type": "object", "properties": {"level": NUM, "delta": NUM}})
def brightness(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    exe = ctx.rt.which("brightness")
    if not exe:
        return R.fail("unsupported", "I can't set brightness here: this Mac has no scriptable brightness control.",
                      fix="Install the `brightness` command line tool (brew install brightness).")
    rec = Recorder("system.brightness", clock=ctx.rt.monotonic)

    def read() -> float | None:
        p = ctx.rt.run([exe, "-l"], timeout=8)
        m = re.search(r"brightness\s+([0-9.]+)", p.out or "")
        return float(m.group(1)) if m else None

    cur = read()
    if cur is None:
        return R.fail("unsupported", "I can't read the display brightness on this Mac.")
    want = float(args["level"]) / 100 if "level" in args else cur + float(args.get("delta", 0)) / 100
    want = max(0.0, min(1.0, want))
    p = ctx.rt.run([exe, f"{want:.2f}"], timeout=8)
    rec.add("act", "set brightness", p.ok, channel="cli")
    now = read()
    ok = now is not None and abs(now - want) < 0.03
    rec.add("verify", "brightness read back", ok, channel="cli", evidence={"now": now})
    return rec.finish(ok, say=f"Brightness is at {int((now or 0) * 100)} percent." if ok else None,
                      undo={"skill": "system.brightness", "args": {"level": int(cur * 100)}, "description": "restore brightness"})


@skill("system.focus_mode", "Turn a Focus (Do Not Disturb) on or off by running your Focus shortcut.",
       ["turn on do not disturb", "turn off do not disturb", "do not disturb on", "set focus mode", "turn on focus"],
       risk="low", channels=["cli"],
       args_schema={"type": "object", "properties": {"on": {"type": "boolean"}, "shortcut": STR}})
def focus_mode(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    exe = ctx.rt.which("shortcuts")
    if not exe:
        return R.fail("unsupported", "I can't change Focus: the Shortcuts command isn't available.")
    listing = ctx.rt.run([exe, "list"], timeout=15)
    names = [n.strip() for n in (listing.out or "").splitlines() if n.strip()]
    on = bool(args.get("on", True))
    want = args.get("shortcut")
    pool = [n for n in names if n == want] if want else [
        n for n in names if re.search(r"(do not disturb|focus|dnd)", n, re.I)
        and re.search(r"\bon\b|enable|start" if on else r"\boff\b|disable|stop", n, re.I)]
    if not pool:
        return R.fail("unsupported", "There's no Focus shortcut I can run. macOS doesn't let apps flip Focus directly.",
                      fix='Create a Shortcut named "Focus On" and one named "Focus Off" using the Set Focus action.')
    rec = Recorder("system.focus_mode", clock=ctx.rt.monotonic)
    p = ctx.rt.run([exe, "run", pool[0]], timeout=30)
    rec.add("act", "run shortcut", p.ok, channel="cli", evidence={"shortcut": pool[0]})
    # macOS exposes no readable Focus state to apps: say what happened, claim nothing more
    return rec.finish(p.ok, verified=False, evidence={"shortcut": pool[0]},
                      say=None, err=None if p.ok else R.error("internal", (p.err or "")[:120]))


@skill("system.sleep_display", "Put the display to sleep.",
       ["sleep the display", "turn off the screen", "turn the screen off", "put the display to sleep", "screen off"],
       risk="low", channels=["cli"])
def sleep_display(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("system.sleep_display", clock=ctx.rt.monotonic)
    p = ctx.rt.run(["pmset", "displaysleepnow"], timeout=8)
    rec.add("act", "pmset displaysleepnow", p.ok, channel="cli")
    ctx.rt.sleep(1.0)
    st = system_state.read(ctx.rt)
    ok = bool(st.get("display_asleep"))
    rec.add("verify", "display asleep", ok, channel="cli", evidence={"display_asleep": st.get("display_asleep")})
    return rec.finish(ok, say="The display is off." if ok else None,
                      err=None if ok else R.error("unverified", "the display did not report asleep"))


@skill("system.lock_state", "Is the Mac locked, is the display asleep, how long has it been idle?",
       ["is my mac locked", "is the screen locked", "is the display asleep", "am i idle", "how long have i been away",
        "is the mac awake"], risk="read", effect=False, channels=["cli"])
def lock_state(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    st = system_state.read(ctx.rt, volume=True)
    block = system_state.blocker(st)
    say = block[1] if block else f"The Mac is awake and unlocked; idle for {int(st.get('idle_s') or 0)} seconds."
    return R.receipt(True, verified=True, skill="system.lock_state", channel="cli", evidence=st, say=say)


# ------------------------------------------------------------------- music ----

MUSIC_APPS = ("Music", "Spotify")


def _music_app(ctx: Ctx, which: str | None) -> str | None:
    for app in ([which] if which else list(MUSIC_APPS)):
        if app and process_running(ctx.rt, f"{app}.app/Contents/MacOS/{app}"):
            return app
    return None


def _music_state(ctx: Ctx, app: str) -> tuple[str, str]:
    p = ctx.rt.osascript(
        f'tell application "{app}"\n  set st to player state as text\n  set nm to ""\n'
        "  try\n    set nm to (name of current track) & \" - \" & (artist of current track)\n  end try\n"
        "  return st & character id 31 & nm\nend tell", timeout=8)
    st, _, nm = (p.out or "").partition("\x1f")
    return st.strip(), nm.strip()


def _music(action: str, verb: str, want: str | None, name: str, summary: str, triggers: list[str]) -> None:
    def run(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
        rec = Recorder(name, clock=ctx.rt.monotonic)
        app = _music_app(ctx, args.get("app"))
        if not app:
            return R.fail("not_running", "Music isn't running, so I haven't started anything.",
                          fix="Open Music or Spotify first; I won't launch it unasked.")
        before, track = _music_state(ctx, app)
        p = ctx.rt.osascript(f'tell application "{app}" to {verb}', timeout=8)
        rec.add("act", verb, p.ok, channel="applescript")
        if not p.ok:
            return rec.finish(False, err=R.error("permission_missing" if p.permission_denied else "internal", (p.err or "")[:120]))
        ctx.rt.sleep(0.5)
        after, track2 = _music_state(ctx, app)
        ok = (after == want) if want else (track2 != track or after != "")
        rec.add("verify", "player state read back", ok, channel="applescript",
                evidence={"state": after, "track": track2})
        say = {"play": "Playing.", "pause": "Paused."}.get(action) or (f"Now playing {track2}." if track2 else "Done.")
        return rec.finish(ok, evidence={"app": app, "state": after, "track": track2}, say=say if ok else None)

    skill(name, summary, triggers, risk="low", channels=["applescript"], permissions=["automation:music"],
          args_schema={"type": "object", "properties": {"app": {"type": "string", "enum": list(MUSIC_APPS)}}})(run)


_music("play", "play", "playing", "music.play", "Play music in Music or Spotify.",
       ["play the music", "resume the music", "play my music", "start the music"])
_music("pause", "pause", "paused", "music.pause", "Pause music.", ["pause the music", "pause my music", "stop the music"])
_music("next", "next track", None, "music.next", "Skip to the next track.", ["next song", "next track", "skip this song"])
_music("previous", "previous track", None, "music.previous", "Go back a track.", ["previous song", "last track", "go back a song"])


@skill("music.now_playing", "What song is playing in Music or Spotify.",
       ["what song is this", "what's playing in music", "what is playing on spotify", "now playing"],
       risk="read", effect=False, channels=["applescript"], permissions=["automation:music"])
def now_playing(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    app = _music_app(ctx, args.get("app"))
    if not app:
        return R.fail("not_running", "Neither Music nor Spotify is running.")
    st, nm = _music_state(ctx, app)
    return R.receipt(True, verified=True, skill="music.now_playing", channel="applescript",
                     evidence={"app": app, "state": st, "track": nm},
                     say=f"{app} is {st}" + (f": {nm}." if nm else "."))


# ------------------------------------------------------------------- calls ----

CALL_HOSTS = ("meet.google.com", "zoom.us", "teams.microsoft.com", "whereby.com", "app.slack.com", "facetime.apple.com")

_CALL_BODY = r"""
var want=__WANT__;var all=[].slice.call(document.querySelectorAll('button,[role=button]')).filter(function(e){var r=e.getBoundingClientRect();return r.width>1&&r.height>1;});
function lab(e){return (e.getAttribute('aria-label')||e.getAttribute('title')||e.textContent||'').replace(/\s+/g,' ').trim();}
var mic=all.filter(function(e){return /(turn (on|off) microphone|^mute$|^unmute$|mute (my )?mic|unmute (my )?mic|microphone)/i.test(lab(e));})[0];
var leave=all.filter(function(e){return /(leave (call|meeting)|hang up|end (call|meeting))/i.test(lab(e));})[0];
function micOn(e){var l=lab(e).toLowerCase();var p=e.getAttribute('aria-pressed');if(/turn off|^mute|mute (my )?mic/.test(l)&&!/unmute/.test(l))return true;if(/turn on|unmute/.test(l))return false;return p==='false';}
if(want==='state'){return {mic:mic?{label:lab(mic),on:micOn(mic)}:null,leave:!!leave};}
if(want==='mute'||want==='unmute'){if(!mic)return {ok:false,error:'no microphone control'};var on=micOn(mic);if((want==='mute'&&!on)||(want==='unmute'&&on))return {ok:true,noop:true,mic:{label:lab(mic),on:on}};mic.click();return {ok:true,clicked:true};}
if(want==='leave'){if(!leave)return {ok:false,error:'no leave control'};leave.click();return {ok:true,clicked:true};}
return {ok:false,error:'unknown'};
"""


def _call_tab(ctx: Ctx, args: dict[str, Any]):
    for b in browsers(ctx):
        try:
            if not b.running():
                continue
            for t in b.tabs(window_ids=(ctx.window_ids or {}).get(b.kind)):
                hay = t.url.lower()
                if args.get("url_contains"):
                    if args["url_contains"].lower() in hay:
                        return b, t
                elif any(h in t.host for h in CALL_HOSTS):
                    return b, t
        except BrowserError:
            continue
    return None


def _call(action: str, name: str, summary: str, triggers: list[str], risk: str = "low") -> None:
    def run(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
        rec = Recorder(name, clock=ctx.rt.monotonic)
        found = _call_tab(ctx, args)
        if not found:
            return R.fail("not_found", "I don't see a call open in a browser tab.",
                          fix="Zoom and FaceTime are not scriptable for mute here; open the call in a browser tab.")
        b, t = found
        try:
            res = b.js_json(t, _CALL_BODY.replace("__WANT__", json.dumps(action)))
        except BrowserError as e:
            return R.fail(e.code, e.message, fix=e.fix)
        rec.add("act", action, bool(res.get("ok")), channel="dom", evidence=res)
        if not res.get("ok"):
            return rec.finish(False, err=R.error("not_found", str(res.get("error"))))
        ctx.rt.sleep(0.4)
        state = b.js_json(t, _CALL_BODY.replace("__WANT__", json.dumps("state")))
        mic = state.get("mic") or {}
        if action == "leave":
            ok = True
            rec.add("verify", "leave pressed", True, channel="dom")
            return rec.finish(ok, verified=False, say=None, evidence={"state": state})
        want_on = action == "unmute"
        ok = bool(mic) and bool(mic.get("on")) == want_on
        rec.add("verify", "microphone state read back", ok, channel="dom", evidence={"mic": mic})
        return rec.finish(ok, evidence={"mic": mic, "tab": t.to_dict()},
                          say=("You're unmuted." if want_on else "You're muted.") if ok else None,
                          undo={"skill": "calls.unmute" if action == "mute" else "calls.mute", "args": {},
                                "description": "flip the microphone back"})

    def plan(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
        found = _call_tab(ctx, args)
        row = {"label": summary.rstrip("."), "target": (found[1].title or found[1].host)[:80] if found else None}
        return card_plan(summary.rstrip("."), [row], op=action, ctx=ctx)

    skill(name, summary, triggers, risk=risk, channels=["dom"],
          permissions=["automation:browser", "js_from_apple_events"],
          args_schema={"type": "object", "properties": {"url_contains": STR}},
          plan=plan if risk == "confirm" else None)(run)


_call("mute", "calls.mute", "Mute your microphone in the call.", ["mute me", "mute my mic", "mute the call", "mute my microphone"])
_call("unmute", "calls.unmute", "Unmute your microphone in the call.", ["unmute me", "unmute my mic", "unmute the call"])
_call("leave", "calls.leave", "Leave the call.", ["leave the call", "hang up", "end the call", "leave the meeting"], risk="confirm")


# ----------------------------------------------------------------- editors ----

EDITORS = {"vscode": ("Visual Studio Code", "vscode"), "cursor": ("Cursor", "cursor")}


def _editor(ctx: Ctx, pref: str | None) -> tuple[str, str] | None:
    order = [pref] if pref in EDITORS else list(EDITORS)
    for key in order:
        app, scheme = EDITORS[key]
        if ctx.rt.run(["test", "-d", f"/Applications/{app}.app"], timeout=5).ok:
            return app, scheme
    return None


def _open_with_editor(ctx: Ctx, name: str, args: dict[str, Any], target: str, line: int | None, col: int | None
                      ) -> dict[str, Any]:
    rec = Recorder(name, clock=ctx.rt.monotonic)
    path = Path(target).expanduser()
    if _denied(str(path)):
        return R.fail("denied", "That location is protected.")
    if not path.exists():
        return R.fail("not_found", f"{path.name} doesn't exist.")
    ed = _editor(ctx, args.get("editor"))
    if not ed:
        return R.fail("not_found", "VS Code or Cursor isn't installed.")
    app, scheme = ed
    if line:
        url = f"{scheme}://file{path.resolve()}:{int(line)}:{int(col or 1)}"
        p = ctx.rt.run(["open", url], timeout=20)
    else:
        p = ctx.rt.run(["open", "-a", app, str(path.resolve())], timeout=20)
    rec.add("act", "open in editor", p.ok, channel="url_scheme" if line else "cli", evidence={"editor": app})
    if not p.ok:
        return rec.finish(False, err=R.error("internal", (p.err or "")[:120]))
    want = path.name if path.is_file() else path.name
    ok = False
    for _ in range(12):
        ctx.rt.sleep(0.5)
        wins: dict[str, Any] = ctx.ax.call("cgwindows") if ctx.ax is not None and ctx.ax.available() else {"ok": False}
        if any(w["owner"] == app and want in w["title"] for w in wins.get("windows", [])):
            ok = True
            break
    rec.add("verify", "an editor window shows it", ok, channel="ax", evidence={"want": want})
    return rec.finish(ok, evidence={"path": str(path), "editor": app, "line": line},
                      say=f"Opened {want} in {app}." if ok else None,
                      err=None if ok else R.error("unverified", "no editor window title shows that name"))


@skill("editors.open_folder", "Open a folder in VS Code or Cursor.",
       ["open this folder in vs code", "open the project in vscode", "open the folder in cursor", "open it in the editor"],
       risk="low", channels=["cli", "ax"], permissions=["accessibility"],
       args_schema={"type": "object", "required": ["path"], "properties": {"path": STR, "editor": STR}})
def open_folder(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    return _open_with_editor(ctx, "editors.open_folder", args, str(args["path"]), None, None)


@skill("editors.open_file", "Open a file in VS Code or Cursor, optionally at a line.",
       ["open the file in vs code", "open that file in the editor", "open it in cursor", "edit that file"],
       risk="low", channels=["cli", "url_scheme", "ax"], permissions=["accessibility"],
       args_schema={"type": "object", "required": ["path"],
                    "properties": {"path": STR, "line": {"type": "integer"}, "column": {"type": "integer"}, "editor": STR}})
def open_file(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    return _open_with_editor(ctx, "editors.open_file", args, str(args["path"]), args.get("line"), args.get("column"))


@skill("editors.goto_line", "Open a file at a specific line in VS Code or Cursor.",
       ["go to line", "jump to line", "open the file at line", "take me to line"],
       risk="low", channels=["url_scheme", "ax"], permissions=["accessibility"],
       args_schema={"type": "object", "required": ["path", "line"],
                    "properties": {"path": STR, "line": {"type": "integer"}, "column": {"type": "integer"}, "editor": STR}})
def goto_line(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    return _open_with_editor(ctx, "editors.goto_line", args, str(args["path"]), int(args["line"]), args.get("column"))


_ = shell_risk
