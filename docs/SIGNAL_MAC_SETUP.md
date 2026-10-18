# Signal on the Mac — owner setup (Executor Plan Phase D)

Signal has no API by design, so Evie drives Signal Desktop through
macOS Accessibility (same `AXUIElement` pattern as the WhatsApp AX family
in `EVLifeHelper`). Honest limits first: if Signal redesigns a screen, the
lane says `ax_unwalkable` instead of guessing clicks; if a safety number
changes, Evie holds the message and asks you to verify — it never sends
past a changed safety number.

## One-time setup

1. Install Signal Desktop and link it as a linked device (Signal on the
   iPhone → Settings → Linked Devices → `+` → scan). Keep it running —
   the AX lane cannot wake a quit app.
2. Grant Accessibility to the process running `EVLifeHelper`:
   System Settings → Privacy & Security → Accessibility → add and enable it.
3. Grant Automation for Signal (the lane reads chat lists through UI
   scripting): same pane → Automation → enable the helper under Signal.

## Probe sequence (run after setup, before trusting the lane)

```bash
# 1. Helper sees the app and the Accessibility grant
EVLifeHelper whatsapp.ax_status   # pattern reference; signal.ax_status lands with the driver
```

The `signal.ax_*` command family (`ax_status`, `ax_chats`, `ax_read`,
`ax_send`, all `--dry-run` capable) is implemented against the installed
app — selectors are pinned by probing the live UI, never guessed. Until it
lands, Signal requests fail loud with `not_connected` ("Signal Desktop
isn't set up on the Mac yet") rather than pretending to work.

## Safety-number rule

A changed safety number is a `checkpoint`-class stop: the pending send is
held, you hear exactly whose number changed, and nothing goes out until you
confirm after verifying with the contact directly.
