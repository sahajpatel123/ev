# ORB OWNER CHECK (R8 — code only, not run by automation)

The standalone preview app lets the owner see the orb live. Automation never
launches it (O3/O4: no windows, no mic); the owner runs it when ready.

## Run

```sh
macos/scripts/orb-preview
```

This builds `OrbPreviewApp` (release) and launches it. Close the window to stop.

## What the app shows

- A 220 pt live orb on a dark/light backdrop (toggle).
- State picker: all 9 states (`idle` … `offline`).
- Input + output level sliders. Set `listening` and drag Input, or `speaking`
  and drag Output, to feel the breathing. (One slider would hide the
  input/output channel split, so there are two.)
- "Use microphone" (optional): requests mic permission and only then starts
  an `AVAudioEngine` tap feeding the orb's input level. The mic never starts
  by itself. "Stop microphone" releases it.
- Island buttons: present the placeholder plan/confirm islands in the real
  `IslandHost` panel (notch-aware on this Mac), then dismiss.
- Presence buttons: each `evie-presence/1` event name; clicking one runs it
  through the real `PresenceMapping` and shows the resulting orb state.
- Live readout line (also printed to stdout every second):

```text
[orb-preview] cpu=1.23% fps=60.0 gpu=0.160ms state=listening
```

## What to look for

1. Idle is a calm static ring; listening/speaking flare smoothly with the
   slider or voice; thinking rotates gently; needs_confirm pulses calmly.
2. Success blooms once and settles; error dims coolly (never red flashing);
   offline is a thin dim ring.
3. CPU stays ≈0% on idle/offline and ≤2% animated; GPU ≤1.5 ms/frame.
4. The island expands from the notch with a smooth morph and dismisses back
   to the compact orb beside the notch.

Built, never launched, by CLI 3: `swift build --product OrbPreviewApp`
succeeds (see ORB_MERGE_READINESS for the exact verification).
