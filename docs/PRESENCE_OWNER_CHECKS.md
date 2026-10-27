# PRESENCE OWNER CHECKS — guided, owner-run, after merge

Three flows in one tool. They are **code only** on this branch: no agent
runs them (they need the owner's hands, eyes, and TCC grants). After merge,
the owner runs them in order from the repo root.

```sh
cd /Users/sahajpatel/Code/ev   # merged main, not a worktree
swift run --package-path macos presence-check permissions
swift run --package-path macos presence-check calibrate
swift run --package-path macos presence-check hud
```

Exit codes: `0` PASS · `1` FAIL · `2` cannot run (missing grant) ·
`3` usage. `calibrate` also writes `~/.ev/presence-calibration.json`
(human + `--json` for the record).

## 1. `permissions` — the gate (seconds, prompt-free)

Checks what **this tool** holds: Input Monitoring
(`CGPreflightListenEventAccess`, no prompt) and Accessibility
(`AXIsProcessTrusted`). PASS (exit 0) needs Input Monitoring only.

On FAIL it prints the exact remediation: System Settings > Privacy &
Security > Input Monitoring, plus `tccutil reset ListenEvent` for stale
entries. Note the honest scope line: these are the *tool's* grants.
Evie.app holds its own set — open the menu-bar panel > Permissions >
Grant permissions for the app side.

## 2. `calibrate` — 60 seconds with your Option key

Needs `permissions` green first. Installs a **listen-only** tap (nothing
is consumed) and measures your real hands against the stock thresholds
(hold 220 ms · tap ≤250 ms · double window 350 ms):

1. **Holds (≤15 s):** hold+release Option 5+ times. PASS needs a typical
   (p50) hold clearly above 220 ms.
2. **Doubles (≤15 s):** double-tap Option 3+ times. PASS needs every tap
   ≤250 ms and every gap ≤350 ms.
3. **Typing (30 s):** type normally with heavy Option use — shortcuts,
   Option-click, accented characters (⌥e + vowel). PASS needs **ZERO
   activations** with proof Option typing happened (chord blocks > 0).
   Zero of everything is inconclusive (type more, re-run), never a pass.

The tool never auto-tunes. On PASS it prints the one command that
enables gestures for the app:

```sh
defaults write com.ev.suit EV_GESTURE_ENABLED -bool true
```

(On FAIL it says which phase missed and the exact key to adjust —
`EV_GESTURE_OPTION_SIDE`, `EV_GESTURE_TAP_LATCH` — instead of guessing.)

## 3. `hud` — eyes on the pill

Needs the app built (any state; no backend needed — it replays a script).
Opens a real accessory process, attaches the pill to a paced 6-step
fixture, and asserts the panel contract programmatically: visible while
the turn runs, floating level, on all Spaces, nonactivating, ends in the
completed outcome (not idle). Then it asks the one question only eyes
can answer: *was the pill visible at the top of the screen, with plan
steps when expanded?* Answer `y`/`n`.

## What success looks like

`permissions` PASS → `calibrate` PASS + gestures enabled → `hud` PASS.
At that point the presence face is live: hold Option to talk,
double-tap to latch, Esc to cancel, Review on the pill for confirmations,
Rewind in the menu bar for history. If any flow FAILs, paste its output
(and `~/.ev/presence-calibration.json` for calibration) into the next
presence session instead of forcing it.
