# Owner setup

One command finishes what this build cannot click for you. From the repo:

```sh
cd backend
uv run ev setup
```

That is the guided flow. It is safe to stop and run again: finished steps are
recorded, and a step that is already granted is verified instead of re-prompted.
Nothing here sends a message, email, or call.

To see the same checks without changing the machine:

```sh
cd backend
uv run ev setup --report-only
uv run ev doctor --fabric
```

`--report-only` prints `Evie setup — report only`. It never loads a job, never
opens a System Settings pane, and never mints a pairing code. Those steps
print `SKIP`, not `PASS`. `ev doctor --fabric` only reads state. Run either
twice; the verdicts match. Screen Recording's verifier still requires a
non-blank capture, and the line it prints is the resolution (`captured`),
not the byte size, which changes every shot.

## What each step unlocks

| Step | Unlocks | If it is not granted |
| --- | --- | --- |
| Accessibility | Global hotkeys (talk and panic) and UI fallback automation | System Settings → Privacy & Security → Accessibility. Deep link `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility` |
| Automation: Finder | File reveal, Finder windows, folder actions | Privacy → Automation. No prompt-free probe exists, so report-only stays `SKIP` until a guided run is allowed to ask Finder |
| Automation: System Events | App control, keystrokes, process queries | Same Automation pane. Report-only stays `SKIP` |
| Automation: Notes | Read and create notes | Same Automation pane. Report-only stays `SKIP` |
| Full Disk Access | Mail and Messages history reads | Privacy → Full Disk Access. Verifier: Messages `chat.db` is readable |
| Screen Recording | Screenshot-as-reference and window grounding | Privacy → Screen Recording. Verifier: a non-blank capture |
| Microphone | Voice input and the ears daemon | Privacy → Microphone |
| Input Monitoring | System-wide key event observation | Privacy → Input Monitoring |
| Contacts | Contact resolution for calls and messages | Privacy → Contacts |
| Calendars | Calendar reads and event creation | Privacy → Calendars |
| Reminders | Reminder reads and creation | Privacy → Reminders |

A denied permission is a `FAIL` with the fix. The command does not report
success for a permission it could not verify.

Guided mode then loads `ev.ears` once the microphone is granted and
`~/Library/LaunchAgents/ev.ears.plist` exists (`launchctl load -w`).
Report-only prints `ev.ears — SKIP — report-only: would load now` and does
not load it. While ears is unloaded and Evie.app is not holding the mic,
the voice phase prints `mic owner — FAIL — no live session and ev.ears not
loaded`. That is the honest state. Load ears by re-running `ev setup`
without `--report-only`, then `uv run ev doctor --fabric` should show
`launchd:ev.ears` loaded. Do not `launchctl kickstart` `ev.api`; the API
job is already the production service.

## This Mac, measured 2026-10-18

`uv run ev doctor --fabric` (twice, both exit 0, Evie.app running): tmux
3.6b, Ghostty 1.3.1, Tailscale, Docker, osascript, Evie.app signed
`EV Code Signing`, bridge answering, Accessibility, Full Disk Access,
Screen Recording, Microphone, Input Monitoring, Contacts, Calendars, and
Reminders all `PASS`. `launchd:ev.ears` is `SKIP` (not loaded). Audit chain
intact, panic clear. Login item `com.ev.suit.login` is loaded.

`uv run ev setup --report-only` names every permission above. Automation
for Finder, System Events, and Notes is `SKIP` (no prompt-free probe).
`ev.ears` is `SKIP` in report-only. Pairing code and the shortcut install
are `SKIP`. The tailnet origin is printed by the phone phase; open that
URL on the iPhone. The cellular-network test is the one check this flow
cannot run.

## Phone

```sh
cd backend
uv run ev setup phone
uv run ev phone check
```

`ev setup phone` prints the Tailscale Serve URL and a pairing QR. Serve
must be on and Funnel off. Add the PWA to the iPhone home screen from that
URL. If the phone panel line says the deployed build predates this tree,
publish with `scripts/deploy_production.sh` and run the check again. That
line is `SKIP`, not a fake `PASS`.

The shell app exposes Shortcuts: Ask Evie, Send latest screenshot (feed it
Get Latest Screenshots), Approve pending, Deny pending, and Status. They
call the Mac over the paired device token. Installing that build on the
iPhone is the owner step; this Mac cannot compile or install it. A device
token cannot approve without a fresh confirmation, and the shortcut says
so instead of claiming the action ran.

## Voice weights

```sh
cd backend
uv run ev models status
uv run ev models install
```

`ev models install` fetches the STT, TTS, and wake weights the network
allows and smoke-tests each one. Missing weights stay an honest degraded
double. The live microphone check is the guided `ev setup` wake step.

## Kill switch and rollback

```sh
cd backend
uv run ev panic
uv run ev panic clear
```

`ev panic` stops sessions and effect lanes and denies further effects until
`ev panic clear`. The same switch is `POST /v1/fabric/panic` and the panic
hotkey (needs Accessibility).

`EV_FABRIC=0` restores the legacy brain path. File edits keep a pre-write
snapshot under `~/.ev/undo`; deletes go to the trash, not `rm`.

The previous app binary is at
`~/.ev/backups/Evie.app.pre-final-20261018-034404`. Re-signing Evie.app
with a different identity resets the TCC grants above. This tree uses the
stable identity `EV Code Signing`.

## What you still do by hand

1. Run `uv run ev setup` in a terminal and click the System Settings prompts
   it opens. On this Mac the TCC rows above are already granted; Automation
   for Finder, System Events, and Notes has no silent probe, so only a
   guided run can confirm those three.
2. Let that guided run load `ev.ears`, then say "hey Evie" when it asks.
3. On the iPhone, open the URL `ev setup phone` prints, add it to the home
   screen, and enter the pairing code. Confirm it loads on cellular. That
   last hop is yours.
4. Gmail, WhatsApp, and any send still go through a confirm card. Standing
   grants stay off until you opt in per channel and recipient.
