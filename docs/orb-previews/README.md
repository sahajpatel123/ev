# Orb previews (R7)

All clips rendered headless: offscreen Metal at 120 pt @2x (240x240) over
flat wallpaper-like backgrounds, H.264 MP4 via ffmpeg. Orb pixels, layout
geometry, and island content views are the real code; only the wallpaper,
menu bar, and notch in the island clips are drawn mocks. Regenerate with
`macos/scripts/orb-render-previews`.

| File | Contents |
| --- | --- |
| `{idle,listening,thinking,acting,speaking,needs-confirm,success,error,offline}-{dark,light}.mp4` | 3–4 s per state (every state arrives from idle, so each clip also shows its entry transition); listening/speaking driven by a synthetic speech-like envelope. `idle`/`offline` clips are single frozen frames — the shipped 0-fps behavior (verified: source PNGs byte-identical). |
| `tour-{dark,light}.mp4` | 24 s full tour with burned-in state labels: idle → listening → thinking → acting → speaking → needs_confirm → success → idle → error → idle → offline → idle |
| `listening-sidebyside.mp4` | 4 s dark + light orbs side by side, same synthetic speech envelope |
| `island-expand-{dark,light}.mp4` | 8.1 s notch-island sequence on a simulated 14" screen: compact → expand (plan steps) → confirm card → collapse |
| `stills-{dark,light}/<state>.png` | One 240x240 still per state |

Total: 23 clips + 18 stills.
