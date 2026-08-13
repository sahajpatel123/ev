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

4. **Click "Grant permissions".** Menu-bar icon → `Permissions…` →
   **Grant permissions**. This is the step that makes EV appear in System
   Settings: macOS adds an app to a Privacy pane only after that app *requests*
   the permission. Reading its authorization status — which is all a status
   panel does — registers nothing, so an app that never asks is never listed.

5. **If an item shows "denied".** macOS never prompts twice. EV *is* in the
   pane, with its switch off — flip it, or re-arm the prompt:

   ```sh
   osascript -e 'quit app "EV"'
   tccutil reset Microphone com.ev.suit    # or: tccutil reset All com.ev.suit
   open /Applications/EV.app
   ```

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
`com.apple.security.device.camera`, and
`com.apple.security.automation.apple-events`: without them a hardened build is
refused the microphone regardless of what the user allows. The build is
unsandboxed (`com.apple.security.app-sandbox` is false).

## What the app is

The packaged app is an `LSUIElement` accessory app: it lives in the menu bar
with a status glyph (listening / thinking / speaking / offline), a hotkey
(⇧⌘E), a capture field, streaming chat, mic capture, TTS playback, HUD cards,
and a launch-at-login toggle. It listens continuously for the wake word
"EVIE", which is why `NSMicrophoneUsageDescription` describes always-on
listening rather than press-to-talk.

Configuration is read from the environment or `defaults`:

- `EV_API_URL` (default `http://127.0.0.1:8000`)
- `EV_API_KEY` (default `dev`)
- `EV_DEVICE_ID` (default `mac-<hostname>`)

The notification helper (`EVNotificationHelper`) is the shim Agent 14's PULSE
backend calls; it forwards to the running EV.app via the `ev://` URL scheme so
there is exactly one UNUserNotificationCenter delivery path. It is a second
Mach-O in `Contents/MacOS/`, so `package.sh` signs it before the outer bundle —
signing it afterwards (or with the deprecated `--deep`) leaves the app
signature invalid.

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
/Applications/EV.app/Contents/MacOS/EV --permissions   # detection + deep links
/Applications/EV.app/Contents/MacOS/EV --notify-test   # notification auth state
/Applications/EV.app/Contents/MacOS/EV --mic-test      # 2s real mic capture probe
/Applications/EV.app/Contents/MacOS/EV --tts-test      # AVAudioPlayer output probe
```

Run those through the installed bundle: launched from `.build/release/EV` there
is no bundle identifier for TCC to attribute anything to, so every permission
reads as denied.
