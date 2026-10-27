# HANDS recon (CLI 4) — measured on this Mac, 2026-10-26

macOS 27.2 arm64, Ghostty 1.3.1, Chrome 155, Safari, tmux 3.6b, Python 3.14.6. Every number below was
measured here; "brittle" lists failure modes actually observed, not guessed.

## What was there before HANDS

| Lane | State at `main` bf0412c |
| --- | --- |
| `fabric/terminal.py`, `ghostty.py` | Opens Ghostty **windows** (not tabs) and tmux-tiled layouts; ratios are tmux `tiled`, not the owner's. Read-back by clipboard dump. |
| `fabric/screen_layout.py` | Pixel splitter that needs a luminance jump > 40 on both sides of a divider: finds **no** divider in a real Ghostty capture (dividers are ~12-20 levels darker than the pane). Grid-only. |
| `fabric/screen_pipeline.py` | OCR + banded geometry; no session grounding; command text is taken from the image. |
| `fabric/operator.py` | AX through System Events JXA, front window only, index paths that go stale. |
| `digital/chrome_session.py`, `adapters/browser.py` | One-off Chrome JS for WhatsApp; no tab inventory, no media or ad logic. |
| Bridge (`BridgeSocketServer.swift`) | `jxa`, `screenshot`, `tcc_status`, EventKit; no AX read/act op, no OCR op. |

## Facts that shaped the design

* **Ghostty AppleScript is real** (`/Applications/Ghostty.app/Contents/Resources/Ghostty.sdef`): window > tab > terminal
  with stable UUID terminal ids, `working directory`, `new tab in window`, `split <terminal> direction`, `input text`,
  `send key`, `perform action`. `sdef` itself needs full Xcode here, so the dictionary is read from the bundle.
* **No split ratio in the dictionary.** `perform action "resize_split:<dir>,<points>"` moves the *nearest same-axis ancestor's*
  divider by exactly that many Accessibility points (verified on a 3-column chain). Pane frames come from Accessibility
  (one `AXTextArea` per terminal, in split-tree order), so a ratio can be set closed-loop: 5-6 pane layouts converge to
  <= 0.4% error in 1 round, ~4 s end to end.
* **`input text` is a paste.** zsh's bracketed paste keeps a pasted newline literal, so a command typed with `\n` sat on the
  prompt and never ran. Enter must be `send key "enter"`. (Found live; the old echo-only read-back could not see it.)
* **System Events JXA is slow** (4.5 s to read a 2-pane Ghostty tab). A 280-line native helper (`helpers/evhands`, AX attribute
  reads in microseconds) does the same in 0.11 s.
* **Chrome**: tabs list by AppleScript; page JS via `execute javascript` works only when "Allow JavaScript from Apple Events"
  is on (pref `browser.allow_javascript_apple_events`). The script runs in an *isolated world*: it sees the DOM but not the
  page's own globals. Chrome 155 exposes web content to Accessibility without any enabling step, so `AXPress` on a page
  button works (and arrives as a trusted event, which scripted `click()` does not).
* **`pgrep` misses GUI-session processes** in this shell; `ps -axo command=` does not. A shell command line that merely
  *mentions* an app name must not count as the app running (self-match hazard), so process checks anchor to the app path.
* **Screen state without permissions**: `CGSessionCopyCurrentDictionary` (locked), `CGDisplayIsAsleep`, idle seconds, all via
  ctypes. `AEDeterminePermissionToAutomateTarget(askUserIfNeeded: false)` reads Automation consent without prompting.
* **OCR**: Apple Vision (`evvision`) `fast` ~1 s, `accurate` ~1 s idle but 86 s on a saturated machine (load 15-20). `fast`
  garbles UUIDs ("Ola114cf", eaten `--`, spaces inside ids); `accurate` reads small fonts. T1 uses fast first and accurate
  only for panes it could not resolve.
* **Coding CLIs on this Mac** (5, five different resume syntaxes, none assumed): `claude --resume <id>`, `codex resume <id>`,
  `opencode -s <id>`, `muse resume <id|name>`, `gemini --resume <index>` (index, not id: cannot be automated by id). Stores:
  claude `~/.claude/projects/*/<uuid>.jsonl` (cwd in the records); codex `~/.codex/sessions/Y/M/D/rollout-*-<uuid>.jsonl`;
  opencode sqlite `session`; muse sqlite `sessions` + `session-admission-locks/*.lock` (a held lock = running).
* **Safari**: tabs have no ids (index only); private windows are not scriptable at all; JS needs the Developer setting.
  It was not running during tests and was never launched (Safari restores the owner's session on launch).

## Observed failure modes (and what handles them)

| Failure | Seen | Handling |
| --- | --- | --- |
| A test window vanished | once, 19:31, while another session ran terminal e2e without the lock | every call re-resolves ids; GUI lock adopted by both sessions at 19:44 |
| Scripted click ignored by the page | fixture with `isTrusted` check | 0.6 s after the click the ad is unchanged -> real AX press -> trusted pointer |
| Back-to-back ad read as "the same ad" | first live run | new-ad test = index changed, media clock restarted, or remaining time went up |
| Armed in-page click ended the ad between two reads | live | re-read the armed counter before saying "ended on its own" |
| Dimmed unfocused pane makes a divider low-contrast | real captures | divider contrast is judged against the panes on both sides, per flat segment |
| Window title bar mistaken for content | real captures | vertical divider run start gives the content top |
| Typed commands never executed | live | Enter as a key; a resumed pane verifies only if the session process exists with the id in argv |
| Shim PATH reordered by the login shell | live | shell setup is typed in the pane (`prelude`) instead of an env var |

## Plan (status in `docs/HANDS_BUILD_LOG.md`)

SKILLS_API v1.0.0 -> world model -> action engine and primitives -> packs -> T1 -> T2 -> watchers -> learning -> CUG -> owner checks.
