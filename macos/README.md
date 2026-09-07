# EV — native macOS menu-bar app (SUIT)

Builds with Command Line Tools only — no Xcode required.

## Install and permissions runbook

1. **Build and sign.**

   ```sh
   ./scripts/package.sh
   ```

   Signs `Contents/MacOS/EVNotificationHelper` first, then `EV.app` with
   `Resources/EV.entitlements`, and fails the build if either signature fails.
   TCC (the privacy database) keys permissions on the code signature, so an
   unsigned or broken bundle can never hold a grant.

2. **Install to `/Applications`.**

   ```sh
   ./scripts/install.sh          # or: ./scripts/package.sh --install
   ```

   Copies the bundle to `/Applications/EV.app` (falling back to
   `~/Applications`) and strips `com.apple.quarantine`.

3. **Launch it from there.**

   ```sh
   open /Applications/EV.app
   ```

4. **Click "Grant permissions" before opening System Settings.** Menu-bar
   icon → `Permissions…` → **Grant permissions**. Answer every dialog that
   appears (Allow / Don't Allow both register EV; Don't Allow leaves the
   switch off).

   macOS adds an app to a Privacy pane only after that app *requests* the
   permission. EV is a menu-bar (`LSUIElement`) app, so those dialogs are
   discarded unless EV comes to the foreground first — which Grant
   permissions now does. Opening Microphone, Speech Recognition, Camera,
   Screen Recording, Automation, Contacts, Calendars, Reminders,
   Notifications, Bluetooth, or Input Monitoring *before* that request
   shows an empty list even though EV is installed. Accessibility, Full
   Disk Access, and Location can appear without this step because they use
   a different prompt (or a + button); the others cannot.

   Full Disk Access has no request API. Use **Reveal EV.app** in the
   Permissions window and add the bundle with + in that pane.

5. **If an item shows "denied".** macOS never prompts twice. EV *is* in the
   pane, with its switch off — flip it, or re-arm the prompt:

   ```sh
   osascript -e 'quit app "EV"'
   tccutil reset Microphone com.ev.suit    # or: tccutil reset All com.ev.suit
   open /Applications/EV.app
   ```

   Other services: `SpeechRecognition`, `Camera`, `ScreenCapture`,
   `AppleEvents`, `AddressBook`, `Calendar`, `Reminders`, `BluetoothAlways`,
   `ListenEvent`, `Accessibility`, `Location`, `SystemPolicyAllFiles`.
   Notifications are not TCC-backed; change them in System Settings →
   Notifications → EV.

6. **If anything still looks wrong.**

   ```sh
   ./scripts/doctor.sh
   ```

   Reports install location, which copy is running, translocation, signature
   (signed / ad-hoc / identifier), quarantine, Info.plist usage strings, and
   the TCC rows for `com.ev.suit`, each with its fix. It only prints; it never
   resets anything. Reading the TCC database needs Full Disk Access for your
   terminal, and says so instead of failing when it does not have it. The same
   facts are in the app under `Permissions… → Diagnostics`.

### Why running from `build/` breaks permissions

A freshly copied bundle carries `com.apple.quarantine`. Launch Services then
runs it through App Translocation (Gatekeeper path randomisation) from a random
read-only mount such as
`/private/var/folders/…/T/AppTranslocation/<UUID>/d/EV.app`. The path — and
with it the identity TCC records against — changes on every launch, so grants
apply to a copy that no longer exists. Installing to `/Applications` with the
quarantine attribute removed is what makes them stick.

### Signing identity

`package.sh` signs ad-hoc (`-`) by default. An ad-hoc identity *is* the
binary's cdhash, so it changes on every rebuild and macOS treats each build as
a new app: permissions have to be granted again and stale `EV` entries collect
in the Privacy panes. For an identity that survives rebuilds, use a Developer
ID or a self-signed codesigning certificate from your keychain
(`security find-identity -v -p codesigning`):

```sh
EV_CODESIGN_IDENTITY="Developer ID Application: You (TEAMID)" ./scripts/package.sh --install
```

With a real identity `package.sh` also signs with `--options runtime`
(hardened runtime) and a secure timestamp. The hardened runtime is why
`Resources/EV.entitlements` declares `com.apple.security.device.audio-input`,
`com.apple.security.device.camera`, `com.apple.security.device.bluetooth`,
`com.apple.security.automation.apple-events`, and the
`com.apple.security.personal-information.*` keys for Contacts, Calendars, and
Location: without them a hardened build is refused those resources
regardless of what the user allows. The build is unsandboxed
(`com.apple.security.app-sandbox` is false).

## What the app is

The packaged app is an `LSUIElement` accessory app: it lives in the menu bar
with a status glyph (listening / thinking / speaking / offline). While the
app is open it streams the microphone on EV LIVE — just talk, no wake word.
`ev.ears` is stopped so it does not share the mic. Mute/unmute is in the
panel; push-to-talk remains only if live cannot connect.

with a status glyph (listening / thinking / speaking / offline), a hotkey
(⇧⌘E), a capture field, streaming chat, mic capture, TTS playback, HUD cards,
and a launch-at-login toggle. It listens continuously for the wake word
"EVIE", which is why `NSMicrophoneUsageDescription` describes always-on
listening rather than press-to-talk.

### Hands-free ("always-on EVIE")

Flip **Hands-free — say “EVIE”** at the top of the menu-bar panel. EV opens a
WebSocket to `/v1/voice/live`, streams 16 kHz mono PCM16, and answers out loud
when it hears the wake word — no key press, no Talk button. After a reply the
mic stays open for a follow-up, so you can keep talking without saying “EVIE”
again. Talking over EV cuts it off mid-sentence.

The line under the switch is what the server is doing (`listening for “EVIE”`,
`heard you`, `listening`, `thinking`, `speaking`, `go ahead`). The meter next
to it is the mic level. The choice is stored in `UserDefaults`
(`ev.handsFree.enabled`) and the loop restarts itself at launch.

- **Microphone.** EV asks the first time you switch it on, after bringing
  itself to the foreground so the dialog is not swallowed. If it was already
  denied, the panel says so and points at **Permissions…** — nothing fails
  silently. Use the runbook above so EV actually appears in System Settings.
- **Server engines.** If `/v1/voice/live` reports `ready: false`, the blockers
  appear in red and the switch turns itself off. Usually that means
  `uv run python -m app.voice.models_setup` on the backend.
- **Reconnects.** If the stream drops while hands-free is on, EV retries with
  backoff (1–30 s) and says so in the panel.
- **Local voice.** When the server’s TTS only returns metadata
  (`speak_locally`), EV speaks the reply with the macOS system voice.
- Push-to-talk (**Talk** / ⇧⌘E) is unchanged and still works alongside it.

Configuration is locked in `Sources/EVAuth/APIAuthKey.swift` (do not invert):
environment → `EV_MASTER_KEY` on disk → `EV_API_KEY` on disk → `defaults`.
Short leftovers (`dev`, `changeme`, keys under 16 chars, `EV_EARS_API_KEY`)
are rejected so the app does not 401 as "Invalid or revoked device token".

- `EV_API_URL` (default `http://127.0.0.1:8000`)
- `EV_MASTER_KEY` (preferred) or a ≥16-character `EV_API_KEY`
- `EV_DEVICE_ID` (default `mac-<hostname>`)

`scripts/package.sh` copies those credentials into Application Support so the
GUI app authenticates even though it is not launched from the repo shell.

The notification helper (`EVNotificationHelper`) is the shim Agent 14's PULSE
backend calls; it forwards to the running EV.app via the `ev://` URL scheme so
there is exactly one UNUserNotificationCenter delivery path. It is a second
Mach-O in `Contents/MacOS/`, so `package.sh` signs it before the outer bundle —
signing it afterwards (or with the deprecated `--deep`) leaves the app
signature invalid.

## EVIE opens a window

EVIE summons floating overlays over any app. Each pane is a translucent
folio (75% window opacity, 30% tint — 70% of the desktop shows through),
not a titled OS window:

```sh
open "ev://present?kind=card&title=Status&body=All%20quiet"
open "ev://present?kind=briefing&title=Brief&recommendation=Do%20this&items=A|B"
open "ev://present?kind=radar&size=lookout&time=lookout&place=upper_right&lookout=1&title=Radar&body=Watching%20deadlines"
open "ev://present?kind=vitals&place=upper_left&lookout=1&title=Vitals&body=Readiness%2068"
open "ev://present?kind=chip&time=flash&ttl=1600&title=Got%20it&body=Saved"
open "ev://present?kind=map&title=Route&lat=23.0&lon=72.5&dest_lat=23.1&dest_lon=72.6"
open "ev://dismiss-all"
```

Kinds include cards, briefings, maps, and persistent lookouts (`radar`,
`vitals`, `horizon`, `scope`, `bench`, `wire`). Sizes and time-types are
independent: a briefing can flash as a chip, a radar can linger as a
lookout. Several panels can be on screen at once. Intelligence chooses
the set via `POST /v1/runtime/lookouts`. Verify with:

```sh
swift macos/scripts/window_probe.swift
```

## Headless smoke test

With a backend running locally, the app binary can prove the client wiring
without a GUI:

```sh
./scripts/smoke.sh
```

It exercises `/v1/health`, streaming chat SSE (`/v1/chat`), voice wake
(`/v1/voice/wake`), and — when a voiceprint enrollment exists — voice
verification, utterance, and TTS audio fetch. The voice verification leg needs
a real liveness model on the backend; without one the smoke reports the wake
path and honestly skips enrollment-dependent steps.

Two system-only probes run without a backend:

```sh
./build/EV.app/Contents/MacOS/EV --permissions   # detection + deep links
./build/EV.app/Contents/MacOS/EV --request-all   # fire every programmatic TCC request
./build/EV.app/Contents/MacOS/EV --request-pending  # register EV in still-undecided panes
./build/EV.app/Contents/MacOS/EV --notify-test   # notification auth state
./build/EV.app/Contents/MacOS/EV --mic-test      # 2s real mic capture probe
./build/EV.app/Contents/MacOS/EV --tts-test      # AVAudioPlayer output probe
./build/EV.app/Contents/MacOS/EV --life-request --permission contacts  # TCC request probe
```

## Permissions — why EV is missing from a System Settings pane

macOS only lists an app in a privacy pane **after the app has actually asked
for that permission**. Installing the app and shipping `Info.plist` usage
strings is not enough: until EV calls the matching API (mic capture,
`CNContactStore`, `EKEventStore`, `SFSpeechRecognizer`, …), the pane is empty
no matter how long it sits in `/Applications`.

The Permissions panel ("Grant EVIE my life") fixes this two ways:

1. **Grant All** fires every programmatic request in sequence — one macOS
   consent dialog per permission, paced so none get dropped. Each answered
   prompt registers EV in the corresponding pane. Screen Recording and Input
   Monitoring register without a dialog (they are toggles), and Accessibility
   and Full Disk Access have no request at all — add EV with the "+" button in
   those two panes.
2. **Per-row "Ask"** re-fires a single undecided permission. Once a permission
   is denied, macOS will not prompt again; the row then only offers "Open
   System Settings".

The first time the panel opens, EV automatically runs a one-time (per app
version) sweep of the *still-undecided* permissions so every pane lists EV.
Re-run `--request-pending` (or reopen the panel) any time a pane is missing —
it only asks about permissions macOS still reports as undecided.

### Grants are tied to the code signature

TCC ties every grant to the app's code signature. With an ad-hoc signature the
CDHash changes on every rebuild, so macOS treats each rebuild as a brand-new
app and **forgets every granted permission**. `package.sh` therefore requires
the stable self-signed `EV Code Signing` identity (created by `signing.sh`)
and fails loudly instead of silently falling back to ad-hoc. Keep the same
build installed while toggling panes, and re-run `package.sh` (not `swift run`,
which runs an unsigned binary) for the grants to survive.

### Step-by-step

```sh
./scripts/package.sh                  # requires the stable signing identity
open ./build/EV.app
# Panel → "Grant All" → answer each prompt
# Accessibility + Full Disk Access: add EV with the "+" button
# Screen Recording + Input Monitoring: toggle EV in their panes
./build/EV.app/Contents/MacOS/EV --permissions   # confirm every row is green
```

## EVLifeHelper

The packaged app ships `EVLifeHelper` at
`EV.app/Contents/MacOS/EVLifeHelper` (also built at
`.build/release/EVLifeHelper`). It exposes a JSON stdout contract for
CONDUIT/CORTEX:

```sh
EVLifeHelper contacts.resolve --query "John"
EVLifeHelper messages.list --limit 10
EVLifeHelper messages.send --to "+15551234567" --text "on my way"
EVLifeHelper mail.send --to "a@example.com" --subject "Hi" --body "Body"
EVLifeHelper call.place --destination "+15551234567" --kind tel
EVLifeHelper call.check --destination "+15551234567" --kind tel
EVLifeHelper apps.frontmost
EVLifeHelper apps.activate --bundle-id com.apple.MobileSMS
```

Exit codes: `0` ok, `3` permission denied, `4` not available, `5` bad
arguments. `messages.send` and `mail.send` accept `--dry-run` (compile +
Automation permission check, no side effects); `call.check` verifies the
scheme handler without placing a call. Set `EV_LIFE_HELPER_PATH` to point
CONDUIT/CORTEX at the binary; the fallback is the bundled path.

/Applications/EV.app/Contents/MacOS/EV --permissions   # detection + deep links
/Applications/EV.app/Contents/MacOS/EV --notify-test   # notification auth state
/Applications/EV.app/Contents/MacOS/EV --mic-test      # 2s real mic capture probe
/Applications/EV.app/Contents/MacOS/EV --tts-test      # AVAudioPlayer output probe
```

Run those through the installed bundle: launched from `.build/release/EV` there
is no bundle identifier for TCC to attribute anything to, so every permission
reads as denied.
