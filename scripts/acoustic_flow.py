"""Acoustic end-to-end voice gate: the check that would have caught the 2026-10-08 silence outage.

Plays spoken prompt fixtures through the Mac speakers into the RUNNING Evie.app
and asserts each turn completes with heard transcript + played audio + reply text,
with no onset self-interrupt and no response watchdog.

Preconditions (checked, not started): Evie.app running, Talk :18000 healthy,
live provider ready (ST14 newer than any ST16 in the client trace).

Do not run scripted WS probes against the app's live session while the gate
runs: session reuse lets a second socket's bridge fight the app's Gemini
connection (upstream 1008 kick wars) and flaps the provider mid-turn.

Usage:
    python3 scripts/acoustic_flow.py [--short-only] [--turn-timeout 90]

Exit 0 when every turn passes, 1 otherwise. Nothing here touches production
data; it drives the same live session the owner talks to.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIX_SHORT = REPO / "backend/tests/fixtures/voice/acoustic_short.wav"
FIX_LONG = REPO / "backend/tests/fixtures/voice/acoustic_long.wav"
TRACE = Path.home() / "Library/Logs/EV/startup-trace.jsonl"
TALK_HEALTH = "http://127.0.0.1:18000/v1/health"


@dataclass
class TurnResult:
    name: str
    heard: bool = False
    played: bool = False
    replied: bool = False
    onset_fires: int = 0
    watchdogs: int = 0
    transcript_chars: int = 0
    reply_chars: int = 0
    reply_lag_s: float | None = None
    notes: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.heard
            and self.played
            and self.replied
            and self.onset_fires == 0
            and self.watchdogs == 0
        )


def fail(msg: str) -> int:
    print(f"ACOUSTIC_GATE FAIL: {msg}")
    return 1


def check_preconditions() -> int:
    app = subprocess.run(
        ["pgrep", "-f", "Evie.app/Contents/MacOS/EV"],
        capture_output=True,
        text=True,
    )
    if not app.stdout.strip():
        return fail("Evie.app is not running (launch it first)")
    for wav in (FIX_SHORT, FIX_LONG):
        if not wav.exists():
            return fail(f"missing fixture {wav}")
    last_exc: Exception | None = None
    for _ in range(4):
        try:
            with urllib.request.urlopen(TALK_HEALTH, timeout=10) as resp:
                if resp.status != 200:
                    return fail(f"Talk health returned {resp.status}")
                break
        except Exception as exc:  # noqa: BLE001 - precondition probe
            last_exc = exc
            time.sleep(5)
    else:
        return fail(f"Talk :18000 unhealthy: {last_exc}")
    if not TRACE.exists():
        return fail(f"missing client trace {TRACE}")
    try:
        content = TRACE.read_text()
    except OSError as exc:
        return fail(f"cannot read client trace: {exc}")
    last_ready = content.rfind("ST14_PROVIDER_READY_FORWARDING_OPEN")
    last_lost = content.rfind("ST16_PROVIDER_LOST_FORWARD_CLOSED")
    if last_ready < 0:
        return fail("provider never became ready (no ST14 in trace)")
    if last_lost > last_ready:
        return fail(
            "live provider is down (ST16 newer than ST14); "
            "relaunch Evie.app for a fresh provider connection and rerun"
        )
    route = _default_output_transport()
    if route is None:
        return fail("cannot determine the default output route")
    if route.lower() != "built-in":
        return fail(
            f"default output is '{route}' (need MacBook speakers): prompts "
            "played elsewhere never reach the room mic. Switch output to the "
            "built-in speakers in System Settings > Sound and rerun."
        )
    return 0


def _default_output_transport() -> str | None:
    # Parses `system_profiler SPAudioDataType`: device stanzas list the
    # transport after the default-output marker.
    try:
        proc = subprocess.run(
            ["system_profiler", "SPAudioDataType"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = proc.stdout.splitlines()
    for i, line in enumerate(lines):
        if "Default Output Device: Yes" in line:
            for follow in lines[i + 1:i + 12]:
                if "Transport:" in follow:
                    return follow.split("Transport:")[1].strip()
            return None
    return None


def trace_events_since(offset: int) -> tuple[list[dict], int]:
    events: list[dict] = []
    with TRACE.open() as f:
        f.seek(offset)
        for line in f:
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
        return events, f.tell()


def wait_settled(timeout_s: int = 180, stable_s: int = 15) -> int:
    """Wait until Eve is truly quiet before a probe: playback gate open,
    status listening, and no fresh reply text, stable for `stable_s`.
    Returns the trace offset to read from.

    Probes played over an active reply are half-duplex-gated (mic held),
    so the turn records zeros that look like breakage — and a 5 s gap
    between multi-part replies looks idle but is not a turn end. Patience
    here is the difference between a flake and a signal.
    """
    offset = TRACE.stat().st_size
    deadline = time.time() + timeout_s
    gate_open: bool | None = None
    listening = False
    # Seed from recent history: an already-idle session emits nothing new.
    try:
        backlog = TRACE.read_text().splitlines()[-60:]
    except OSError:
        backlog = []
    for line in backlog:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        kind = event.get("event")
        reason = str(event.get("reason") or "")
        if kind == "ST15_PLAYBACK_GATE":
            gate_open = reason == "open"
        elif kind == "ST00_STATUS":
            listening = "->listening" in reason or (reason.split() or [""])[0] == "listening"
    stable_since: float | None = None
    while time.time() < deadline:
        events, offset = trace_events_since(offset)
        for event in events:
            kind = event.get("event")
            reason = str(event.get("reason") or "")
            if kind == "ST15_PLAYBACK_GATE":
                gate_open = reason == "open"
            elif kind == "ST00_STATUS":
                listening = "->listening" in reason or (reason.split() or [""])[0] == "listening"
            elif kind == "ST23_ASSISTANT_TEXT":
                # Fresh reply text restarts the clock even across an
                # idle-looking gap between multi-part answers.
                stable_since = None
        settled = gate_open is not False and listening
        if settled:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= stable_s:
                return offset
        else:
            stable_since = None
        time.sleep(1)
    return offset


def run_turn(name: str, wav: Path, timeout_s: int) -> TurnResult:
    """One probe turn with ORDERED attribution: heard, then played, then
    replied must arrive in that causal order after the probe starts.

    Leftover multi-part replies routinely straddle a probe window; counting
    any global closed/ST23 as this turn's reply attributes Eve's old answer
    to the new probe (and vice versa). Only the first ST22 after the probe,
    the first gate-closed after that ST22, and text (or gate-open) after
    that closed belong to this turn.

    Replied accepts a completed playback cycle (closed then open) without
    text: the provider sometimes answers audio-only, and eight seconds of
    played speech IS a reply even when no ST23 arrives.
    """
    result = TurnResult(name=name)
    start_offset = wait_settled()
    play = subprocess.run(["afplay", str(wav)], capture_output=True, text=True)
    if play.returncode != 0:
        result.notes.append(f"afplay failed: {play.stderr.strip()[:120]}")
        return result
    probe_end_wall = time.time()
    deadline = probe_end_wall + timeout_s
    offset = start_offset
    closed_at_wall: float | None = None
    unprompted_audio = False
    while time.time() < deadline:
        events, offset = trace_events_since(offset)
        for event in events:
            kind = event.get("event")
            reason = str(event.get("reason") or "")
            if kind == "ST22_USER_HEARD":
                result.heard = True
                result.transcript_chars = max(
                    result.transcript_chars, _chars(reason)
                )
            elif kind == "ST15_PLAYBACK_GATE" and reason == "closed":
                if result.heard and not result.played:
                    result.played = True
                    closed_at_wall = time.time()
                    result.reply_lag_s = round(closed_at_wall - probe_end_wall, 1)
                elif not result.heard:
                    unprompted_audio = True
            elif kind == "ST23_ASSISTANT_TEXT":
                result.reply_chars = max(result.reply_chars, _chars(reason))
                if result.played:
                    result.replied = True
            elif kind == "ST15_PLAYBACK_GATE" and reason == "open":
                if result.played and not result.replied:
                    # Full playback cycle without text: audio-only reply.
                    result.replied = True
            elif kind == "ST25_ONSET_CONFIRM":
                result.onset_fires += 1
            elif kind == "WDOG_NO_RESPONSE":
                result.watchdogs += 1
        if result.replied and result.played:
            break
        if result.replied and time.time() > deadline - timeout_s / 3:
            # Reply arrived but audio never started: wait out the watchdog
            # window so a late WDOG is observed, then stop early on it.
            if result.watchdogs:
                break
        time.sleep(2)
    # Final drain for trailing markers.
    events, _ = trace_events_since(offset)
    for event in events:
        kind = event.get("event")
        reason = str(event.get("reason") or "")
        if kind == "ST15_PLAYBACK_GATE" and reason == "closed":
            if result.heard and not result.played:
                result.played = True
        elif kind == "ST23_ASSISTANT_TEXT":
            result.reply_chars = max(result.reply_chars, _chars(reason))
            if result.played:
                result.replied = True
        elif kind == "ST15_PLAYBACK_GATE" and reason == "open":
            if result.played and not result.replied:
                result.replied = True
        elif kind == "ST25_ONSET_CONFIRM":
            result.onset_fires += 1
        elif kind == "WDOG_NO_RESPONSE":
            result.watchdogs += 1
    if not result.heard:
        if unprompted_audio:
            result.notes.append(
                "probe overlapped by unprompted reply audio (rerun, not breakage)"
            )
        else:
            result.notes.append("no ST22 transcript (mic did not pick up the prompt)")
    if result.heard and not result.played:
        result.notes.append("no ST15 playback gate after transcript (reply produced no audio)")
    if result.played and not result.replied:
        result.notes.append("reply audio never completed and no reply text arrived")
    if result.onset_fires:
        result.notes.append(f"{result.onset_fires} onset self-interrupt(s)")
    if result.watchdogs:
        result.notes.append(f"{result.watchdogs} response watchdog(s)")
    return result


def _chars(reason: str) -> int:
    # reason looks like "chars=126".
    try:
        return int(reason.split("chars=")[1].split()[0])
    except (IndexError, ValueError):
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--short-only", action="store_true")
    parser.add_argument("--turn-timeout", type=int, default=90)
    args = parser.parse_args()

    if (code := check_preconditions()) != 0:
        return code

    turns = [("short", FIX_SHORT)] if args.short_only else [
        ("short", FIX_SHORT),
        ("long", FIX_LONG),
    ]
    results = [run_turn(name, wav, args.turn_timeout) for name, wav in turns]

    print("=== acoustic flow gate ===")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        lag = f"{result.reply_lag_s:.1f}s" if result.reply_lag_s is not None else "-"
        print(
            f"{status} {result.name}: heard={result.heard} "
            f"played={result.played} replied={result.replied} "
            f"onset={result.onset_fires} wdog={result.watchdogs} "
            f"tx_chars={result.transcript_chars} rx_chars={result.reply_chars} "
            f"lag={lag}"
        )
        for note in result.notes:
            print(f"  - {note}")
    failed = [r for r in results if not r.passed]
    if failed:
        print(f"ACOUSTIC_GATE FAIL: {len(failed)}/{len(results)} turns failed")
        return 1
    print(f"ACOUSTIC_GATE PASS: {len(results)}/{len(results)} turns clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
