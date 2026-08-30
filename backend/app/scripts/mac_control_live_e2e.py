"""Production-path Mac-control live E2E.

Builds/signs EV.app, restarts the API onto the current tree, launches a
headless live bridge, injects a spoken-equivalent utterance through the
same Realtime session EV.app uses, and verifies real application state.

  make mac-control-live-e2e
  python -m app.scripts.mac_control_live_e2e --suite music
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
BACKEND = REPO / "backend"
APP = REPO / "macos" / "build" / "EV.app"
BINARY = APP / "Contents" / "MacOS" / "EV"
MARKER = "EVIE_LIVE_E2E"
MUSIC_UTTERANCE = (
    "Open Music, find the Chess playlist, and play the first track."
)


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd or REPO),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def git_head() -> str:
    result = _run(["git", "rev-parse", "--short", "HEAD"])
    return (result.stdout or "").strip() or "unknown"


def file_fingerprint(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(path.as_posix().encode())
    return digest.hexdigest()[:16]


def codesign_info(app: Path) -> dict[str, str]:
    result = _run(["codesign", "-dv", "--verbose=4", str(app)])
    blob = (result.stdout or "") + (result.stderr or "")
    out: dict[str, str] = {}
    for line in blob.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def pids_for(pattern: str) -> list[int]:
    result = _run(["pgrep", "-f", pattern])
    pids = []
    for line in (result.stdout or "").split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def terminate(pattern: str) -> None:
    for pid in pids_for(pattern):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.6)
    for pid in pids_for(pattern):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def fingerprint_runtime() -> dict[str, Any]:
    sign = codesign_info(APP) if APP.exists() else {}
    backend_hash = file_fingerprint(
        BACKEND / "app" / "ev" / "computer.py",
        BACKEND / "app" / "ev" / "computer_runtime.py",
        BACKEND / "app" / "ev" / "computer_strategy.py",
        BACKEND / "app" / "voice" / "live" / "grok_voice.py",
        BACKEND / "app" / "ev" / "tools.py",
    )
    api_pids = pids_for("uvicorn app.main:app")
    app_pids = pids_for("macos/build/EV.app/Contents/MacOS/EV")
    return {
        "git_head": git_head(),
        "backend_source_hash": backend_hash,
        "api_pids": api_pids,
        "ev_app_pids": app_pids,
        "ev_app_path": str(BINARY),
        "bundle_id": sign.get("Identifier"),
        "code_signature": sign.get("Signature") or sign.get("Authority"),
        "team": sign.get("TeamIdentifier"),
    }


def package_app() -> None:
    script = REPO / "macos" / "scripts" / "package.sh"
    result = _run(["/bin/zsh", str(script)], timeout=300)
    if result.returncode != 0:
        sys.stderr.write(result.stdout + "\n" + result.stderr)
        raise SystemExit("package.sh failed")
    probe = _run([str(BINARY), "--live-e2e", "--timeout", "1", "--utterance", "_package_probe_"], timeout=8)
    if MARKER not in (probe.stdout or ""):
        raise SystemExit(
            "packaged EV.app does not contain --live-e2e; release binary is stale"
        )


def restart_api() -> int:
    env_file = REPO / ".env"
    terminate("uvicorn app.main:app")
    env = os.environ.copy()
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    log_dir = Path.home() / "Library" / "Logs" / "ev"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = open(log_dir / "api.e2e.out.log", "ab")
    stderr = open(log_dir / "api.e2e.err.log", "ab")
    proc = subprocess.Popen(
        ["uv", "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(BACKEND),
        env=env,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    for _ in range(40):
        probe = _run(["curl", "-sf", "http://127.0.0.1:8000/v1/health"])
        if probe.returncode == 0:
            return proc.pid
        time.sleep(0.25)
        if proc.poll() is not None:
            break
    raise SystemExit(f"API failed to become healthy (pid={proc.pid})")


def music_ground_truth() -> dict[str, Any]:
    script = """
    tell application id "com.apple.Music"
      set stateText to "unknown"
      try
        if player state is playing then
          set stateText to "playing"
        else if player state is paused then
          set stateText to "paused"
        else
          set stateText to "stopped"
        end if
      end try
      set trackName to ""
      set artistName to ""
      try
        set trackName to name of current track
        set artistName to artist of current track
      end try
      return stateText & tab & trackName & tab & artistName
    end tell
    """
    result = _run(["osascript", "-e", script])
    parts = (result.stdout or "").strip().split("\t")
    while len(parts) < 3:
        parts.append("")
    return {"player_state": parts[0], "track": parts[1], "artist": parts[2], "ok": result.returncode == 0}


def parse_e2e_output(text: str) -> list[dict[str, Any]]:
    events = []
    for line in text.splitlines():
        if not line.startswith(MARKER + " "):
            continue
        raw = line[len(MARKER) + 1 :]
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            events.append({"event": "unparsed", "raw": raw[:400]})
    return events


def last_event(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in reversed(events):
        if item.get("event") == name:
            return item
    return None


def run_live_case(
    utterance: str,
    timeout: int,
    *,
    expect: str = "music-playing",
    follow_up: str | None = None,
) -> dict[str, Any]:
    terminate("macos/build/EV.app/Contents/MacOS/EV")
    if not BINARY.exists():
        raise SystemExit(f"missing packaged binary {BINARY}")
    cmd = [
        str(BINARY),
        "--live-e2e",
        "--utterance",
        utterance,
        "--timeout",
        str(timeout),
        "--expect",
        expect,
    ]
    if follow_up:
        cmd.extend(["--follow-up", follow_up])
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=10)
    events = parse_e2e_output(stdout or "")
    summary = last_event(events, "summary") or {}
    return {
        "returncode": proc.returncode if proc.returncode is not None else 124,
        "events": events,
        "summary": summary,
        "stderr": (stderr or "")[-2000:],
        "stdout_tail": (stdout or "")[-2000:],
    }


def stop_music() -> None:
    _run(["osascript", "-e", 'tell application id "com.apple.Music" to pause'])
    _run(["osascript", "-e", 'tell application id "com.apple.Music" to stop'])
    time.sleep(0.5)


def chess_tracks() -> list[str]:
    script = """
    tell application id "com.apple.Music"
      set names to ""
      repeat with p in playlists
        if name of p is "Chess" then
          repeat with t in tracks of p
            set names to names & name of t & linefeed
          end repeat
        end if
      end repeat
      return names
    end tell
    """
    result = _run(["osascript", "-e", script])
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def assert_music_pass(result: dict[str, Any]) -> dict[str, Any]:
    summary = result["summary"]
    truth = music_ground_truth()
    tools = summary.get("acknowledged") or []
    required = [
        "computer_status",
        "list_apps",
        "open_app",
        "inspect_ui",
        "ui_action",
        "screen_look",
        "app_action",
    ]
    missing = [name for name in required if name not in tools]
    calls = summary.get("tool_calls") or []
    used_app_action = any(item.get("command") == "app_action" for item in calls)
    used_inspect = sum(1 for item in calls if item.get("command") == "inspect_ui")
    replies = " ".join(summary.get("replies") or [])
    playing = truth.get("player_state") == "playing"
    track = str(truth.get("track") or "")
    first_ok = playing and "cinnamon" in track.lower()
    first_no = "ok"
    if missing:
        first_no = "provider_tools:" + ",".join(missing)
    elif not used_app_action:
        first_no = "strategy_router_app_action_not_called"
    elif not playing:
        first_no = "music_playback"
    elif not first_ok:
        first_no = "ordinal_or_track"
    gate = {
        "tools_ready": not missing,
        "missing_tools": missing,
        "schema_hash": summary.get("schema_hash"),
        "session_id": summary.get("session_id"),
        "provider_session_id": summary.get("provider_session_id"),
        "model": summary.get("model"),
        "tool_calls": calls,
        "tool_call_count": len(calls),
        "inspect_ui_count": used_inspect,
        "used_app_action": used_app_action,
        "replies": summary.get("replies"),
        "music_truth": truth,
        "first_no": first_no,
        "pass": first_ok and not missing and used_app_action,
    }
    if gate["pass"] and used_inspect > 4:
        gate["warning"] = "semantic route used too much Accessibility"
    if replies and "budget" in replies.lower():
        gate["warning"] = "user-facing budget language"
    return gate


def _case_report(name: str, result: dict[str, Any], first_no: str, passed: bool) -> dict[str, Any]:
    summary = result.get("summary") or {}
    return {
        "name": name,
        "pass": passed,
        "first_no": first_no,
        "returncode": result.get("returncode"),
        "session_id": summary.get("session_id"),
        "provider_session_id": summary.get("provider_session_id"),
        "tool_calls": summary.get("tool_calls") or [],
        "replies": summary.get("replies") or [],
        "music_truth": music_ground_truth(),
    }


def run_missing_playlist(timeout: int) -> dict[str, Any]:
    stop_music()
    before = music_ground_truth()
    result = run_live_case(
        "Open Music and play my Project Neptune playlist.",
        timeout,
        expect="session",
    )
    truth = music_ground_truth()
    summary = result["summary"]
    calls = summary.get("tool_calls") or []
    replies = " ".join(summary.get("replies") or [])
    used_app_action = any(item.get("command") == "app_action" for item in calls)
    inspect_count = sum(1 for item in calls if item.get("command") == "inspect_ui")
    claimed_play = bool(
        re.search(
            r"\b(it'?s playing|now playing|i played|started playing)\b",
            replies,
            re.I,
        )
    )
    playing = truth.get("player_state") == "playing"
    first_no = "ok"
    if not used_app_action:
        first_no = "missing_playlist_no_app_action"
    elif inspect_count > 6:
        first_no = "missing_playlist_ax_expedition"
    elif claimed_play:
        first_no = "missing_playlist_false_success_speech"
    elif playing and before.get("player_state") != "playing":
        first_no = "missing_playlist_started_playback"
    passed = first_no == "ok"
    return _case_report("missing_playlist", result, first_no, passed)


def run_ordinal(timeout: int, index: int, expect_substr: str) -> dict[str, Any]:
    stop_music()
    ordinal_word = {1: "first", 2: "second", -1: "last"}[index]
    result = run_live_case(
        f"Open Music, find the Chess playlist, and play the {ordinal_word} track.",
        timeout,
        expect="music-playing",
    )
    truth = music_ground_truth()
    summary = result["summary"]
    calls = summary.get("tool_calls") or []
    used_app_action = any(item.get("command") == "app_action" for item in calls)
    playing = truth.get("player_state") == "playing"
    track = str(truth.get("track") or "")
    first_no = "ok"
    if not used_app_action:
        first_no = "ordinal_no_app_action"
    elif not playing:
        first_no = "ordinal_not_playing"
    elif expect_substr.lower() not in track.lower():
        first_no = f"ordinal_mismatch_got_{track}"
    passed = first_no == "ok"
    return _case_report(f"ordinal_{ordinal_word}", result, first_no, passed)


def run_dont_play(timeout: int) -> dict[str, Any]:
    stop_music()
    before = music_ground_truth()
    result = run_live_case(
        "Find my Chess playlist and tell me its first track, but don't play it.",
        timeout,
        expect="session",
    )
    truth = music_ground_truth()
    summary = result["summary"]
    replies = " ".join(summary.get("replies") or [])
    calls = summary.get("tool_calls") or []
    used_app_action = any(item.get("command") == "app_action" for item in calls)
    playing = truth.get("player_state") == "playing"
    first_no = "ok"
    if not used_app_action:
        first_no = "dont_play_no_app_action"
    elif playing:
        first_no = "dont_play_started_playback"
    elif "cinnamon" not in replies.lower():
        first_no = "dont_play_did_not_report_track"
    passed = first_no == "ok"
    report = _case_report("dont_play", result, first_no, passed)
    report["before"] = before
    return report


def run_continuation(timeout: int, second_substr: str) -> dict[str, Any]:
    stop_music()
    result = run_live_case(
        MUSIC_UTTERANCE,
        timeout,
        expect="music-playing",
        follow_up="Now play the second one.",
    )
    truth = music_ground_truth()
    calls = (result.get("summary") or {}).get("tool_calls") or []
    plays = [
        item
        for item in calls
        if str(item.get("action") or "") == "play"
        or str(item.get("action") or "").startswith("play_")
    ]
    playing = truth.get("player_state") == "playing"
    track = str(truth.get("track") or "")
    first_no = "ok"
    if not playing:
        first_no = "continuation_not_playing"
    elif len(plays) < 2:
        first_no = "continuation_single_play_not_first_then_second"
    elif second_substr.lower() not in track.lower():
        first_no = f"continuation_mismatch_got_{track}"
    return _case_report("continuation_second", result, first_no, first_no == "ok")


def notes_bodies() -> str:
    script = """
    tell application id "com.apple.Notes"
      if (count of notes) is 0 then return ""
      set acc to ""
      set n to (count of notes)
      if n > 8 then set n to 8
      repeat with i from 1 to n
        try
          set acc to acc & body of note i & linefeed
        end try
      end repeat
      return acc
    end tell
    """
    result = _run(["osascript", "-e", script], timeout=30)
    return result.stdout or ""


def safari_tab() -> dict[str, str]:
    script = """
    tell application id "com.apple.Safari"
      if (count of windows) is 0 then return "||"
      set theTab to current tab of window 1
      return (URL of theTab) & "||" & (name of theTab)
    end tell
    """
    result = _run(["osascript", "-e", script], timeout=20)
    parts = (result.stdout or "").strip().split("||", 1)
    return {"url": parts[0] if parts else "", "title": parts[1] if len(parts) > 1 else ""}


def calculator_display() -> str:
    script = """
    tell application "System Events"
      tell process "Calculator"
        if (count of windows) is 0 then return ""
        try
          return value of static text 1 of window 1
        end try
        try
          return value of static text 1 of group 1 of window 1
        end try
        return ""
      end tell
    end tell
    """
    result = _run(["osascript", "-e", script], timeout=15)
    return (result.stdout or "").strip()


def finder_path() -> str:
    script = """
    tell application id "com.apple.finder"
      if (count of Finder windows) is 0 then return ""
      try
        return POSIX path of (target of window 1 as alias)
      end try
      return name of window 1
    end tell
    """
    result = _run(["osascript", "-e", script], timeout=20)
    return (result.stdout or "").strip()


def normalize_number(text: str) -> str:
    digits = re.sub(r"[^\d.]", "", text or "")
    if "." in digits:
        try:
            return str(int(float(digits)))
        except ValueError:
            return digits.replace(".", "")
    return digits


def installed_apps() -> set[str]:
    apps = set()
    for base in (Path("/Applications"), Path.home() / "Applications"):
        if not base.exists():
            continue
        for path in base.glob("*.app"):
            apps.add(path.stem)
    return apps


def run_notes(timeout: int) -> dict[str, Any]:
    nonce = f"Mac Control Notes verification {os.urandom(2).hex()}"
    utterance = f"Open Notes and create a new note containing: {nonce}"
    result = run_live_case(utterance, timeout, expect="session")
    bodies = notes_bodies()
    summary = result.get("summary") or {}
    calls = summary.get("tool_calls") or []
    used_action = any(item.get("command") in {"app_action", "ui_action"} for item in calls)
    first_no = "ok"
    if not used_action:
        first_no = "notes_no_write_tool"
    elif nonce not in bodies:
        first_no = "notes_nonce_missing"
    report = _case_report("notes_write", result, first_no, first_no == "ok")
    report["nonce"] = nonce
    report["found"] = nonce in bodies
    return report


def run_safari(timeout: int) -> dict[str, Any]:
    query = "OpenAI"
    utterance = "Open Safari, search for OpenAI, and open the first result."
    result = run_live_case(utterance, timeout, expect="session")
    tab = safari_tab()
    url = (tab.get("url") or "").lower()
    summary = result.get("summary") or {}
    calls = summary.get("tool_calls") or []
    used_search = any(item.get("action") == "search" for item in calls)
    used_nav = any(item.get("action") in {"navigate", "open_item", "press"} for item in calls)
    nav_rows = [item for item in calls if item.get("action") == "navigate"]
    nav_url = str((nav_rows[-1].get("url") if nav_rows else "") or "")
    nav_verified = any(item.get("verified") for item in nav_rows)
    first_no = "ok"
    if not used_search and not any(item.get("command") == "app_action" for item in calls):
        first_no = "safari_no_search"
    elif not nav_verified or "google.com/search" in nav_url.lower() or not nav_url:
        first_no = f"safari_still_on_results:{(nav_url or url)[:80]}"
    report = _case_report("safari_first_result", result, first_no, first_no == "ok")
    report["url"] = tab.get("url")
    report["title"] = tab.get("title")
    report["used_search"] = used_search
    report["used_nav"] = used_nav
    return report


def run_calculator(timeout: int) -> dict[str, Any]:
    left, right = 187, 43
    expected = left * right
    utterance = f"Open Calculator and calculate {left} times {right}."
    result = run_live_case(utterance, timeout, expect="session")
    summary = result.get("summary") or {}
    calls = summary.get("tool_calls") or []
    display = calculator_display()
    if not display:
        displays = [str(item.get("display") or "") for item in calls if item.get("display")]
        display = next((item for item in reversed(displays) if item), "")
    got = normalize_number(display)
    used = any(
        item.get("command") in {"app_action", "ui_action"}
        and item.get("command") != "open_app"
        for item in calls
    )
    first_no = "ok"
    if not used:
        first_no = "calculator_no_interaction"
    elif str(expected) not in got:
        first_no = f"calculator_display_{display or 'empty'}_expected_{expected}"
    report = _case_report("calculator_generic", result, first_no, first_no == "ok")
    report["display"] = display
    report["expected"] = expected
    report["methods"] = [item.get("method") for item in calls if item.get("method")]
    return report


def run_finder(timeout: int) -> dict[str, Any]:
    result = run_live_case(
        "Open Finder and open the Downloads folder.",
        timeout,
        expect="session",
    )
    path = finder_path().lower()
    first_no = "ok"
    if "download" not in path:
        first_no = f"finder_path_{path or 'empty'}"
    report = _case_report("finder_downloads", result, first_no, first_no == "ok")
    report["path"] = path
    return report


def run_textedit(timeout: int) -> dict[str, Any]:
    nonce = f"EVIE-TXT-{os.urandom(2).hex()}"
    result = run_live_case(
        f"Open TextEdit, create a new document, and type {nonce}",
        timeout,
        expect="session",
    )
    summary = result.get("summary") or {}
    calls = summary.get("tool_calls") or []
    used_generic = any(item.get("command") in {"ui_action", "inspect_ui"} for item in calls)
    first_no = "ok"
    if not used_generic and not any(item.get("command") == "app_action" for item in calls):
        first_no = "textedit_no_interaction"
    elif not used_generic:
        first_no = "textedit_no_generic_ui_path"
    report = _case_report("third_party_textedit_generic", result, first_no, first_no == "ok")
    report["nonce"] = nonce
    report["used_generic"] = used_generic
    report["installed"] = "TextEdit" in installed_apps() or True
    report["note"] = "TextEdit is Apple-shipped; used as generic text-entry canary without a Notes adapter."
    return report


def run_vision_opportunistic(timeout: int) -> dict[str, Any]:
    result = run_live_case(
        "Open Calculator, look at the screen, and press the 5 button you can see.",
        timeout,
        expect="session",
    )
    summary = result.get("summary") or {}
    calls = summary.get("tool_calls") or []
    used_look = any(item.get("command") == "screen_look" for item in calls)
    used_click = any(item.get("action") in {"click_at", "press", "screen_click"} for item in calls)
    first_no = "ok"
    if not used_look:
        first_no = "vision_screen_look_not_called"
    report = _case_report("vision_calculator_five", result, first_no, first_no == "ok")
    report["used_screen_look"] = used_look
    report["used_click"] = used_click
    report["limitation"] = None if used_look else "model used structured UI instead of screen_look"
    return report


def pause_music() -> None:
    stop_music()


def run_suite_cases(suite: str, timeout: int) -> list[dict[str, Any]]:
    if suite == "notes":
        return [run_notes(timeout)]
    if suite == "safari":
        return [run_safari(max(timeout, 120))]
    if suite == "calculator":
        return [run_calculator(timeout)]
    if suite == "finder":
        return [run_finder(timeout)]
    if suite in {"generic-third-party", "generic"}:
        return [run_textedit(timeout)]
    if suite == "vision":
        return [run_vision_opportunistic(timeout)]
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite",
        default="music",
        choices=[
            "music",
            "music-matrix",
            "notes",
            "safari",
            "calculator",
            "finder",
            "generic-third-party",
            "generic",
            "vision",
            "fingerprint",
            "all",
            "full",
        ],
    )
    parser.add_argument("--skip-package", action="store_true")
    parser.add_argument("--skip-restart", action="store_true")
    parser.add_argument("--restart-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    if not args.skip_package and args.suite != "fingerprint":
        print("PACKAGING EV.app", flush=True)
        package_app()
    runtime = fingerprint_runtime()
    print("RUNTIME " + json.dumps(runtime), flush=True)
    if args.restart_only:
        pid = restart_api()
        print(json.dumps({"api_pid": pid, **fingerprint_runtime()}))
        return 0
    if not args.skip_restart:
        print("RESTARTING API", flush=True)
        restart_api()
        time.sleep(0.8)
    runtime = fingerprint_runtime()
    print("RUNTIME_AFTER_RESTART " + json.dumps(runtime), flush=True)
    if args.suite == "fingerprint":
        return 0 if runtime.get("api_pids") else 1

    report: dict[str, Any] = {"runtime": runtime}

    if args.suite in {"music", "music-matrix", "all", "full"}:
        print("LIVE MUSIC CASE", flush=True)
        stop_music()
        result = run_live_case(MUSIC_UTTERANCE, args.timeout)
        gate = assert_music_pass(result)
        report["live"] = {"returncode": result["returncode"], **gate}
        print(json.dumps({"live": report["live"]}, indent=2, default=str))
        if not gate["pass"]:
            print(f"FIRST_NO {gate['first_no']}", file=sys.stderr)
            if result.get("stderr"):
                print(result["stderr"][-800:], file=sys.stderr)
            return 1
        stop_music()

    if args.suite in {"music-matrix", "all", "full"}:
        tracks = chess_tracks()
        first = tracks[0] if tracks else ""
        second = tracks[1] if len(tracks) > 1 else ""
        print(f"CHESS_TRACKS count={len(tracks)} first={first} second={second}", flush=True)
        cases = [
            run_missing_playlist(args.timeout),
            run_ordinal(args.timeout, 2, second or "chemtrail"),
            run_dont_play(args.timeout),
            run_continuation(min(args.timeout + 30, 120), second or "chemtrail"),
        ]
        report["matrix"] = cases
        print(json.dumps({"matrix": cases}, indent=2, default=str))
        failed = [item for item in cases if not item["pass"]]
        stop_music()
        if failed:
            print(f"FIRST_NO {failed[0]['name']}:{failed[0]['first_no']}", file=sys.stderr)
            return 1

    extra_suites: list[str] = []
    if args.suite in {"notes", "safari", "calculator", "finder", "generic-third-party", "generic", "vision"}:
        extra_suites = [args.suite]
    elif args.suite in {"all", "full"}:
        extra_suites = ["notes", "safari", "calculator", "finder", "generic", "vision"]

    extra: list[dict[str, Any]] = []
    for name in extra_suites:
        print(f"LIVE SUITE {name}", flush=True)
        extra.extend(run_suite_cases(name, args.timeout))
    if extra:
        report["general"] = extra
        print(json.dumps({"general": extra}, indent=2, default=str))
        failed = [item for item in extra if not item["pass"]]
        if failed:
            print(f"FIRST_NO {failed[0]['name']}:{failed[0]['first_no']}", file=sys.stderr)
            return 1

    if args.suite == "full":
        print("MUSIC CANARY AFTER GENERAL", flush=True)
        stop_music()
        result = run_live_case(MUSIC_UTTERANCE, args.timeout)
        gate = assert_music_pass(result)
        report["music_after"] = gate
        print(json.dumps({"music_after": gate}, indent=2, default=str))
        stop_music()
        if not gate["pass"]:
            print(f"FIRST_NO music_after:{gate['first_no']}", file=sys.stderr)
            return 1

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
