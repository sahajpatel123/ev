# EV — native macOS menu-bar app (SUIT)

Builds with Command Line Tools only — no Xcode required:

```sh
swift build -c release
./scripts/package.sh
open ./build/EV.app
```

The packaged app is an `LSUIElement` accessory app: it lives in the menu bar
with a status glyph (listening / thinking / speaking / offline), a hotkey
(⇧⌘E), a capture field, streaming chat, mic capture, TTS playback, HUD cards,
and a launch-at-login toggle.

Configuration is read from the environment or `defaults`:

- `EV_API_URL` (default `http://127.0.0.1:8000`)
- `EV_API_KEY` (default `dev`)
- `EV_DEVICE_ID` (default `mac-<hostname>`)

The notification helper (`EVNotificationHelper`) is the shim Agent 14's PULSE
backend calls; it forwards to the running EV.app via the `ev://` URL scheme so
there is exactly one UNUserNotificationCenter delivery path.

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
./build/EV.app/Contents/MacOS/EV --notify-test   # notification auth state
./build/EV.app/Contents/MacOS/EV --mic-test      # 2s real mic capture probe
./build/EV.app/Contents/MacOS/EV --tts-test      # AVAudioPlayer output probe
```
