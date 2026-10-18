"""`ev setup` (one guided, self-verifying owner flow) and `ev doctor`.

Setup phases: env, app, tcc, launchd, tailscale, live_send. Report-only
mode never changes anything; interactive mode opens the exact Settings
pane per permission, polls until granted, and runs that permission's
live verifier. State in ~/.ev/setup-state.json makes reruns resume.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SETTINGS_BASE = "x-apple.systempreferences:com.apple.preference.security"


def state_path() -> Path:
    return Path.home() / ".ev" / "setup-state.json"


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _bridge_call(op: str, args: dict | None = None, timeout: float = 15.0) -> dict[str, Any]:
    # Local import: setup.py must stay importable without the app on sys.path
    # in --help flows; the ev CLI always runs from the backend tree.
    from app.fabric.bridge_client import BridgeClient

    return BridgeClient(timeout=timeout).call(op, args)


def tcc_map() -> dict[str, dict[str, str]]:
    """App-identity TCC truth keyed by permission name ({} when down)."""
    res = _bridge_call("tcc_status")
    out: dict[str, dict[str, str]] = {}
    if res.get("ok"):
        for row in res.get("evidence", {}).get("permissions", []) or []:
            if isinstance(row, dict) and row.get("permission"):
                out[str(row["permission"])] = {
                    "status": str(row.get("status", "unknown")),
                    "detail": str(row.get("detail", "")),
                }
    return out


@dataclass
class Permission:
    name: str
    title: str
    unlocks: str
    pane: str
    verify: Callable[[], tuple[bool, str]]
    probe: Callable[[], tuple[str, str]] | None = None
    # Automation has no prompt-free probe: the verify attempt itself (a
    # tell-app JXA) is the signal, so these skip the poll and verify twice.
    pollable: bool = True

    def status(self) -> tuple[str, str]:
        if self.probe is not None:
            try:
                return self.probe()
            except Exception as exc:
                return "unknown", f"probe failed: {type(exc).__name__}"
        return "unknown", "no prompt-free probe"


def _tcc_probe(name: str) -> Callable[[], tuple[str, str]]:
    def probe() -> tuple[str, str]:
        row = tcc_map().get(name)
        if row is None:
            return "unknown", "Evie.app bridge not answering"
        return row.get("status", "unknown"), row.get("detail", "")

    return probe


def _tcc_verify(name: str) -> Callable[[], tuple[bool, str]]:
    def verify() -> tuple[bool, str]:
        row = tcc_map().get(name)
        if row is None:
            return False, "bridge not answering"
        ok = row.get("status") == "granted"
        return ok, row.get("detail", "") or row.get("status", "")

    return verify


def _automation_verify(app: str) -> Callable[[], tuple[bool, str]]:
    def verify() -> tuple[bool, str]:
        res = _bridge_call("jxa", {"script": f'tell application "{app}" to return name'})
        if res.get("ok"):
            return True, f"{app} answered"
        if res.get("error") == "automation_denied":
            return False, f"{app}: Automation denied (-1743)"
        return False, str(res.get("error") or "jxa failed")

    return verify


def _screen_verify() -> tuple[bool, str]:
    res = _bridge_call("screenshot", {}, timeout=30.0)
    if not res.get("ok"):
        return False, str(res.get("error") or "capture failed")
    ev = res.get("evidence", {})
    path = ev.get("path", "")
    try:
        size = Path(str(path)).stat().st_size if path else 0
    except OSError:
        size = 0
    if size < 1024:
        return False, "capture blank (permission likely denied)"
    return True, f"{ev.get('width')}x{ev.get('height')} {size} bytes"


def _fda_verify() -> tuple[bool, str]:
    res = _bridge_call("probe_read", {"path": "~/Library/Messages/chat.db"})
    if not res.get("ok"):
        return False, str(res.get("error") or "probe failed")
    readable = bool(res.get("evidence", {}).get("readable"))
    return readable, "Messages db readable" if readable else "Messages db unreadable"


PERMISSIONS: list[Permission] = [
    Permission("accessibility", "Accessibility",
               "global hotkeys (talk + panic) and UI fallback automation",
               f"{SETTINGS_BASE}?Privacy_Accessibility", _tcc_verify("accessibility"),
               _tcc_probe("accessibility")),
    Permission("automation_finder", "Automation: Finder",
               "file reveal, Finder windows, folder actions",
               f"{SETTINGS_BASE}?Privacy_Automation", _automation_verify("Finder"),
               None, False),
    Permission("automation_sysevents", "Automation: System Events",
               "app control, keystrokes, process queries",
               f"{SETTINGS_BASE}?Privacy_Automation", _automation_verify("System Events"),
               None, False),
    Permission("automation_notes", "Automation: Notes",
               "read/create notes (adapter, WP8)",
               f"{SETTINGS_BASE}?Privacy_Automation", _automation_verify("Notes"),
               None, False),
    Permission("full_disk_access", "Full Disk Access",
               "Mail + Messages history reads",
               f"{SETTINGS_BASE}?Privacy_AllFiles", _fda_verify,
               _tcc_probe("full_disk_access")),
    Permission("screen_recording", "Screen Recording",
               "screenshot-as-reference and window grounding",
               f"{SETTINGS_BASE}?Privacy_ScreenCapture", _screen_verify,
               _tcc_probe("screen_recording")),
    Permission("microphone", "Microphone",
               "voice input and the ears daemon",
               f"{SETTINGS_BASE}?Privacy_Microphone", _tcc_verify("microphone"),
               _tcc_probe("microphone")),
    Permission("input_monitoring", "Input Monitoring",
               "system-wide key event observation",
               f"{SETTINGS_BASE}?Privacy_ListenEvent", _tcc_verify("input_monitoring"),
               _tcc_probe("input_monitoring")),
    Permission("contacts", "Contacts",
               "contact resolution for calls/messages",
               f"{SETTINGS_BASE}?Privacy_Contacts", _tcc_verify("contacts"),
               _tcc_probe("contacts")),
    Permission("calendars", "Calendars",
               "calendar reads and event creation",
               f"{SETTINGS_BASE}?Privacy_Calendars", _tcc_verify("calendars"),
               _tcc_probe("calendars")),
    Permission("reminders", "Reminders",
               "reminder reads and creation",
               f"{SETTINGS_BASE}?Privacy_Reminders", _tcc_verify("reminders"),
               _tcc_probe("reminders")),
]


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    failures: int = 0

    def say(self, text: str = "") -> None:
        self.lines.append(text)
        print(text)

    def item(self, label: str, status: str, detail: str = "") -> None:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}.get(status, status)
        line = f"  [{mark}] {label}" + (f" — {detail}" if detail else "")
        self.lines.append(line)
        print(line)
        if status == "FAIL":
            self.failures += 1

    def text(self) -> str:
        return "\n".join(self.lines)


def open_settings_pane(url: str) -> bool:
    try:
        p = subprocess.run(["open", url], capture_output=True, timeout=15)
        return p.returncode == 0
    except Exception:
        return False


def poll_until(permission: Permission, timeout_s: float = 60.0,
               interval_s: float = 2.0) -> tuple[str, str]:
    """Poll a prompt-free probe until granted (interactive)."""
    deadline = time.time() + timeout_s
    status, detail = permission.status()
    while status != "granted" and time.time() < deadline:
        time.sleep(interval_s)
        status, detail = permission.status()
    return status, detail


def phase_env(rep: Report) -> None:
    from app.fabric import probes as probes_mod

    rep.say("== env: tools and versions (info only) ==")
    for e in probes_mod.environment_entries():
        rep.item(e["permission"], "PASS" if e["status"] == "granted" else "SKIP",
                 f"{e['status']}: {e['detail']}")


def _app_running() -> bool:
    try:
        p = subprocess.run(["pgrep", "-f", r"Evie\.app/Contents/MacOS/EV( |$)"],
                           capture_output=True, text=True, timeout=10)
        return p.returncode == 0 and bool((p.stdout or "").strip())
    except Exception:
        return False


def _login_item_plist() -> Path:
    return Path.home() / "Library/LaunchAgents/com.ev.suit.login.plist"


def login_item_registered() -> bool:
    plist = _login_item_plist()
    if not plist.is_file():
        return False
    try:
        p = subprocess.run(["launchctl", "list", "com.ev.suit.login"],
                           capture_output=True, text=True, timeout=10)
        return p.returncode == 0
    except Exception:
        return False


def login_item_register() -> tuple[bool, str]:
    plist = _login_item_plist()
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        if not plist.is_file():
            plist.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                "  <key>Label</key><string>com.ev.suit.login</string>\n"
                "  <key>ProgramArguments</key><array>\n"
                "    <string>/usr/bin/open</string><string>-a</string>"
                "<string>Evie</string>\n"
                "  </array>\n"
                "  <key>RunAtLoad</key><true/>\n"
                "</dict></plist>\n"
            )
        p = subprocess.run(["launchctl", "load", str(plist)], capture_output=True,
                           text=True, timeout=15)
        if p.returncode != 0 and "already loaded" not in ((p.stderr or "") + (p.stdout or "")).lower():
            return False, ((p.stderr or "") + (p.stdout or "")).strip()[:200]
        return True, "login item registered"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def phase_app(rep: Report, *, interactive: bool) -> None:
    from app.fabric import probes as probes_mod

    rep.say("== app: Evie.app installed, running, bridged ==")
    app = probes_mod.probe_evie_app()
    rep.item("installed", "PASS" if app["status"] == "granted" else "FAIL", app["detail"])
    running = _app_running()
    if not running and interactive:
        subprocess.run(["open", "-a", "Evie"], capture_output=True, timeout=15)
        time.sleep(3)
        running = _app_running()
    rep.item("running", "PASS" if running else "FAIL",
             "Evie.app process live" if running else "launch it: open -a Evie")
    bridge = probes_mod.probe_bridge()
    rep.item("bridge", "PASS" if bridge["status"] == "granted" else "FAIL", bridge["detail"])
    if login_item_registered():
        rep.item("login item", "PASS", "com.ev.suit.login loaded")
    elif interactive:
        ok, detail = login_item_register()
        rep.item("login item", "PASS" if ok else "FAIL", detail)
    else:
        rep.item("login item", "SKIP", "report-only: not registered yet")


def phase_tcc(rep: Report, *, interactive: bool, state: dict[str, Any],
              poll_s: float = 60.0) -> None:
    rep.say("== tcc: one permission at a time (grant, verify, next) ==")
    tcc_state = state.setdefault("tcc", {})
    for perm in PERMISSIONS:
        if tcc_state.get(perm.name) == "PASS":
            rep.item(perm.title, "PASS", "already verified (resume skip)")
            continue
        status, detail = perm.status()
        rep.say(f"-- {perm.title}: unlocks {perm.unlocks} --")
        rep.say(f"   status: {status} ({detail})")
        if status == "granted":
            ok, vDetail = perm.verify()
            rep.item(perm.title, "PASS" if ok else "FAIL", f"verifier: {vDetail}")
            tcc_state[perm.name] = "PASS" if ok else "FAIL"
            continue
        if not interactive:
            rep.item(perm.title, "SKIP", f"grant in System Settings, then re-run: {perm.pane}")
            continue
        opened = open_settings_pane(perm.pane)
        rep.say(f"   pane {'opened' if opened else 'OPEN FAILED: ' + perm.pane}")
        if not perm.pollable:
            # Try-to-verify: attempt, invite the grant on denial, retry once.
            ok, vDetail = perm.verify()
            if not ok and "denied" in vDetail.lower():
                rep.say(f"   verifier: {vDetail} — grant Evie, then continue.")
                try:
                    input("   Press Enter after granting (or Ctrl-C to stop here; rerun resumes)... ")
                except (EOFError, OSError):
                    rep.item(perm.title, "SKIP", "no input; rerun interactively to grant")
                    tcc_state[perm.name] = "SKIP"
                    continue
                except KeyboardInterrupt:
                    rep.item(perm.title, "SKIP", "owner stopped; rerun resumes")
                    tcc_state[perm.name] = "SKIP"
                    if interactive:
                        save_state(state)
                    return
                ok, vDetail = perm.verify()
            rep.item(perm.title, "PASS" if ok else "FAIL", f"verifier: {vDetail}")
            tcc_state[perm.name] = "PASS" if ok else "FAIL"
            continue
        rep.say("   grant Evie, then wait.")
        if poll_s > 0:
            status, detail = poll_until(perm, timeout_s=poll_s)
        else:
            try:
                input("   Press Enter after granting (or Ctrl-C to stop here; rerun resumes)... ")
            except EOFError:
                rep.item(perm.title, "SKIP", "no input; rerun interactively to grant")
                tcc_state[perm.name] = "SKIP"
                continue
            except OSError:
                # pytest captures stdin (OSError); a real terminal never hits this.
                rep.item(perm.title, "SKIP", "no input; rerun interactively to grant")
                tcc_state[perm.name] = "SKIP"
                continue
            except KeyboardInterrupt:
                rep.item(perm.title, "SKIP", "owner stopped the poll; rerun resumes")
                tcc_state[perm.name] = "SKIP"
                if interactive:
                    save_state(state)
                return
            status, detail = perm.status()
        if status != "granted":
            rep.item(perm.title, "FAIL", f"still {status}: {detail}")
            tcc_state[perm.name] = "FAIL"
            continue
        ok, vDetail = perm.verify()
        rep.item(perm.title, "PASS" if ok else "FAIL", f"verifier: {vDetail}")
        tcc_state[perm.name] = "PASS" if ok else "FAIL"
    if interactive:
        save_state(state)


def _launchd_loaded(label: str) -> bool:
    try:
        p = subprocess.run(["launchctl", "list", label], capture_output=True, timeout=10)
        return p.returncode == 0
    except Exception:
        return False


def phase_launchd(rep: Report, *, interactive: bool, state: dict[str, Any]) -> None:
    rep.say("== launchd: ears loaded once the mic is granted ==")
    for label in ("ev.api", "ev.worker", "ev.scheduler", "ev.runtime", "ev.collector", "ev.talk"):
        rep.item(label, "PASS" if _launchd_loaded(label) else "FAIL",
                 "loaded" if _launchd_loaded(label) else "not loaded")
    mic = tcc_map().get("microphone", {}).get("status")
    ears_plist = Path.home() / "Library/LaunchAgents/ev.ears.plist"
    if _launchd_loaded("ev.ears"):
        rep.item("ev.ears", "PASS", "loaded")
    elif mic != "granted":
        rep.item("ev.ears", "SKIP", "mic not granted yet; load after the tcc phase")
    elif not ears_plist.is_file():
        rep.item("ev.ears", "FAIL", "no plist at ~/Library/LaunchAgents/ev.ears.plist")
    elif not interactive:
        rep.item("ev.ears", "SKIP", "report-only: would load now")
    else:
        # -w clears any disable override (plain load/bootstrap refuse
        # disabled jobs with error 5 on current macOS).
        p = subprocess.run(["launchctl", "load", "-w", str(ears_plist)],
                           capture_output=True, text=True, timeout=15)
        ok = p.returncode == 0 or "already loaded" in ((p.stderr or "") + (p.stdout or "")).lower()
        rep.item("ev.ears", "PASS" if ok else "FAIL", (p.stderr or p.stdout or "loaded")[:160])
    state["launchd"] = "done"


def phase_tailscale(rep: Report, *, interactive: bool) -> None:
    from app.fabric import probes as probes_mod

    del interactive
    rep.say("== tailscale: installed, running, phone-ready ==")
    entry = probes_mod.probe_tailscale()
    rep.item("tailscale", "PASS" if entry["status"] == "granted" else "FAIL", entry["detail"])
    if entry["status"] != "granted":
        rep.say("   owner: install Tailscale, open it, sign in, then `ev setup phone` (WP11).")


def _tailnet_origin() -> str | None:
    """The tailnet-only HTTPS origin Tailscale Serve publishes for :8000."""

    binary = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not Path(binary).exists() and not shutil.which(binary):
        return None
    try:
        p = subprocess.run([binary, "serve", "status"], capture_output=True,
                           text=True, timeout=15)
    except Exception:
        return None
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("https://") and "ts.net" in line:
            return line.split()[0].rstrip("/")
    return None


def _http_probe(url: str, needle: str, timeout_s: float = 12.0) -> tuple[bool, str]:
    import httpx

    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            res = client.get(url)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"
    body = res.text[:4000]
    ok = res.status_code == 200 and (needle in body if needle else True)
    detail = f"HTTP {res.status_code}" + ("" if ok else f" (needle {needle!r} absent)")
    return ok, detail


def phase_phone(rep: Report, *, interactive: bool) -> None:
    """Phone wiring: tailnet origin, PWA + panel served, pairing code, shortcut."""

    rep.say("== phone: tailnet origin, PWA, pairing ==")
    origin = _tailnet_origin()
    if origin is None:
        rep.item("tailnet origin", "FAIL",
                 "Tailscale Serve is not publishing an https origin (tailscale serve --bg 8000)")
        return
    rep.item("tailnet origin", "PASS", origin)
    ok, detail = _http_probe(f"{origin}/evie", "Evie")
    rep.item("pwa served", "PASS" if ok else "FAIL", detail)
    ok_panel, panel_detail = _http_probe(f"{origin}/evie/evie-phone.js", "window.EviePhone")
    if panel_detail.startswith("HTTP 404"):
        # The origin proxies the DEPLOYED build; a 404 here means that build
        # predates the panel, not that this tree is missing it. Publish with
        # scripts/deploy_production.sh and re-run.
        rep.item("phone panel", "SKIP",
                 "served by this tree; deployed build predates it — deploy, then re-run")
    else:
        rep.item("phone panel", "PASS" if ok_panel else "FAIL", panel_detail)
    # Origin checks are strict (loopback + *.ts.net); a non-tailnet host is
    # rejected on every credentialed call, so say which kind this is.
    from urllib.parse import urlsplit

    from app.device_gateway.security import origin_allowed

    host = urlsplit(origin).hostname or ""
    rep.item("origin allowlist", "PASS" if origin_allowed(origin, host) else "FAIL",
             f"{host}: {'allowed' if origin_allowed(origin, host) else 'rejected by device gateway'}")
    if not interactive:
        rep.item("pairing code", "SKIP", "report-only: `ev setup phone` prints one")
        rep.item("shortcut", "SKIP", "install from the phone: Mobile Actions -> Bridge shortcut")
        return
    rep.say(f"   On each iPhone: open {origin}/evie in Safari -> Share -> Add to Home Screen.")
    try:
        mint = input("   Mint a pairing code for a phone now? [Y/n] ").strip().lower()
    except (EOFError, OSError, KeyboardInterrupt):
        mint = "n"
    if mint in {"", "y", "yes"}:
        code, err = _mint_pairing_code()
        if err:
            rep.item("pairing code", "FAIL", err)
        else:
            rep.item("pairing code", "PASS", "valid 10 minutes; enter it on the phone")
            rep.say(f"   code: {code}")
            rep.say("   The phone stores the device token; the Mac key never leaves the Mac.")
    else:
        rep.item("pairing code", "SKIP", "owner declined")
    rep.item("shortcut", "SKIP",
             "install on the phone: Mobile Actions -> download bridge shortcut (signed)")


def _mint_pairing_code() -> tuple[str, str | None]:
    """Mint one pairing token through the API (master auth), never locally."""

    import httpx

    from clients.cli import api_key, api_url

    try:
        with httpx.Client(base_url=api_url(), timeout=20.0,
                          headers={"Authorization": f"Bearer {api_key()}"}) as client:
            res = client.post("/v1/device-gateway/pairing-tokens",
                              json={"role": "primary_companion",
                                    "display_name": "iPhone"})
    except Exception as exc:
        return "", f"{type(exc).__name__}: {str(exc)[:120]}"
    if res.status_code != 200:
        return "", f"HTTP {res.status_code}: {res.text[:120]}"
    return str(res.json().get("pairing_token") or ""), None


def _mic_owner() -> tuple[str, str]:
    """Who owns the mic right now: ears daemon or the app live session."""
    import os as _os

    marker = Path.home() / "Library/Application Support/EV/live-mic-owner"
    try:
        pid = int(marker.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid:
        try:
            _os.kill(pid, 0)
            return "app", f"Evie.app live session (pid {pid})"
        except (OSError, ProcessLookupError):
            pass
    try:
        p = subprocess.run(["launchctl", "list", "ev.ears"], capture_output=True,
                           timeout=10)
        if p.returncode == 0:
            return "ears", "ev.ears daemon (no live marker)"
    except Exception:
        pass
    return "none", "no live session and ev.ears not loaded"


def _ears_log_tail(n: int = 200) -> str:
    try:
        lines = (Path.home() / "Library/Logs/ev/ears.err.log").read_text().splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def phase_voice(rep: Report, *, interactive: bool, poll_s: float = 45.0) -> None:
    rep.say("== voice: mic owner + wake check ==")
    tcc = tcc_map().get("microphone", {}).get("status", "unknown")
    rep.item("microphone TCC", "PASS" if tcc == "granted" else "FAIL",
             f"status={tcc}")
    owner, detail = _mic_owner()
    rep.item("mic owner", "PASS" if owner in {"ears", "app"} else "FAIL", detail)
    if owner == "none":
        rep.say("   fix: launch Evie.app or load ev.ears, then re-run.")
        return
    if not interactive:
        rep.item("wake check", "SKIP", "report-only: say 'hey Evie' in a guided run")
        return
    if owner == "app":
        rep.say("   The app owns the mic: say 'hey Evie' and watch the orb answer.")
        try:
            seen = input("   Did Evie respond? [y/N] ").strip().lower()
        except (EOFError, OSError, KeyboardInterrupt):
            rep.item("wake check", "SKIP", "no answer; rerun to try again")
            return
        rep.item("wake check", "PASS" if seen in {"y", "yes"} else "FAIL",
                 "owner-confirmed" if seen in {"y", "yes"} else "owner said no")
        return
    rep.say("   Say 'hey Evie' (or 'hey Eve') now; listening to the ears log...")
    before = _ears_log_tail(50)
    deadline = time.time() + poll_s
    while time.time() < deadline:
        time.sleep(2.0)
        after = _ears_log_tail(200)
        new = after[len(before):] if after.startswith(before[:500]) else after
        low = new.lower()
        if "wake" in low and any(w in low for w in ("detect", "trigger", "spot", "hey ev", "hello ev")):
            rep.item("wake check", "PASS", "ears log shows a wake detection")
            return
    rep.item("wake check", "FAIL",
             "no wake in the ears log; check mic input level + `ev models status`")


async def phase_live_send_async(rep: Report, *, client_factory) -> None:
    """Optional guided check: park a harmless CONFIRM, owner approves, verify.

    Nothing leaves the Mac: the parked effect is `touch` inside the live-proof
    dir, approved by the owner in a second terminal, verified, then trashed.
    """

    from pathlib import Path as _Path

    rep.say("== live_send: real CONFIRM path, harmless effect (optional) ==")
    ping = _Path.home() / "Desktop/evie-live-proof/setup-ping.txt"
    client = client_factory()
    try:
        parked = await client.post(
            "/v1/fabric/exec",
            json={"tool": "shell_run",
                  "args": {"command": "touch setup-ping.txt",
                           "cwd": str(ping.parent)}},
        )
    except Exception as exc:
        rep.item("live_send", "FAIL", f"exec unreachable: {type(exc).__name__}")
        return
    if parked.status_code != 200:
        rep.item("live_send", "FAIL", f"exec HTTP {parked.status_code}")
        return
    body = parked.json().get("result", {})
    action_id = body.get("action_id")
    if not action_id:
        rep.item("live_send", "FAIL",
                 f"expected a parked hold, got: {str(body)[:160]}")
        return
    rep.say(f"   parked {action_id}. In a SECOND terminal run: ev approve {action_id}")
    try:
        input("   Press Enter after approving (or Ctrl-C to skip; rerun resumes)... ")
    except (EOFError, KeyboardInterrupt):
        rep.item("live_send", "SKIP", "owner skipped the live check")
        return
    try:
        pending = await client.get("/v1/fabric/approvals/pending")
        still = [a for a in pending.json().get("approvals", []) if a.get("id") == action_id]
    except Exception:
        still = ["unknown"]
    if still:
        rep.item("live_send", "FAIL", "approval not observed; check `ev approvals`")
        return
    if not ping.exists():
        rep.item("live_send", "FAIL", "approved but file missing (phantom denied)")
        return
    with contextlib.suppress(OSError):
        ping.unlink()
    rep.item("live_send", "PASS", "park -> approve -> execute -> verify, then cleaned")


def run_setup(*, interactive: bool, poll_s: float = 60.0,
              phases: list[str] | None = None) -> Report:
    """Run setup phases. report-only (interactive=False) changes nothing."""
    rep = Report()
    state = load_state() if interactive else {}
    wanted = phases or ["env", "app", "tcc", "launchd", "tailscale", "voice",
                        "phone"]
    rep.say("Evie setup — " + ("guided" if interactive else "report only"))
    if "env" in wanted:
        phase_env(rep)
    if "app" in wanted:
        phase_app(rep, interactive=interactive)
    if "tcc" in wanted:
        phase_tcc(rep, interactive=interactive, state=state, poll_s=poll_s)
    if "launchd" in wanted:
        phase_launchd(rep, interactive=interactive, state=state)
    if "tailscale" in wanted:
        phase_tailscale(rep, interactive=interactive)
    if "voice" in wanted:
        phase_voice(rep, interactive=interactive)
    if "phone" in wanted:
        phase_phone(rep, interactive=interactive)
    if "live_send" in wanted and not interactive:
        rep.item("live_send", "SKIP", "report-only: interactive check skipped")
    if interactive:
        save_state(state)
    rep.say(f"setup done: {rep.failures} failing item(s)")
    return rep


def run_doctor(*, fabric_only: bool = False) -> Report:
    """Non-interactive state report. Never changes anything."""
    from app.fabric import probes as probes_mod

    rep = Report()
    rep.say("Evie doctor" + (" --fabric" if fabric_only else ""))
    for e in probes_mod.permission_entries():
        mark = {"granted": "PASS", "denied": "FAIL"}.get(e["status"], "SKIP")
        rep.item(f"{e['permission']} [{e['kind']}]", mark,
                 e["detail"] + (f" | fix: {e['fix']}" if e.get("fix") else ""))
    for label in ("ev.api", "ev.ears", "ev.worker", "ev.scheduler", "ev.runtime",
                  "ev.collector", "ev.talk"):
        loaded = _launchd_loaded(label)
        rep.item(f"launchd:{label}", "PASS" if loaded else "SKIP",
                 "loaded" if loaded else "not loaded")
    try:
        from app.fabric import audit as audit_mod

        v = audit_mod.verify()
        rep.item("audit chain", "PASS" if v.get("ok") else "FAIL",
                 f"{v.get('checked', 0)} entries")
    except Exception as exc:
        rep.item("audit chain", "SKIP", f"{type(exc).__name__}")
    try:
        from app.fabric import panic as panic_mod

        panicked = panic_mod.panicked()
        rep.item("panic", "FAIL" if panicked else "PASS",
                 "ENGAGED" if panicked else "clear")
    except Exception as exc:
        rep.item("panic", "SKIP", f"{type(exc).__name__}")
    if not fabric_only:
        rep.say("(full `ev doctor` checks beyond fabric live in the legacy command)")
    return rep


async def cmd_setup(args: Any) -> int:
    phases = args.phases.split(",") if getattr(args, "phases", "") else None
    interactive = not args.report_only
    rep = run_setup(interactive=interactive, phases=phases)
    if interactive and (phases is None or "live_send" in phases):
        from clients.cli import _client

        await phase_live_send_async(rep, client_factory=lambda: _client(30.0))
    return 0 if rep.failures == 0 else 1


async def cmd_doctor_fabric() -> int:
    rep = run_doctor(fabric_only=True)
    return 0 if rep.failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(prog="ev-setup")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--phases", default="")
    parser.add_argument("--doctor", action="store_true")
    ns = parser.parse_args(argv)
    if ns.doctor:
        return asyncio.run(cmd_doctor_fabric())
    return asyncio.run(cmd_setup(ns))


if __name__ == "__main__":
    sys.exit(main())
