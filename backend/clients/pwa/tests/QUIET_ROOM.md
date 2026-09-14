# Quiet room — design and acceptance record

Historical intermediate design. The current material and craft pass is recorded
in [ATELIER.md](ATELIER.md); the procedural breath mark described below was replaced.

The owner's brief: replace Atlas's crowded dashboard with something minimal,
subtle, attractive, and distinctive. This is a new interaction composition for
Evie, not a claim that no other product has ever used a similar idea.

## Design decisions

- One space, four idle controls: Thread, Talk, Write a thought, Tools. No home
  cards, bottom tab bar, permanent feature grid, or navigation gestures to learn.
- A procedurally drawn, folded breath mark is the focal point. It responds to
  existing voice amplitude and state; it does not imply microphone access when
  idle. Muted sage and lavender, restrained serif typography, generous space.
- Replies pull the mark into a smaller form. Native disclosure lets the owner
  set a reply aside without deleting content. More streamed text respects that
  choice; new turns and actionable recovery states can reopen it.
- Tools are an on-demand searchable list using existing actions, not mock
  destinations. Camera setup lives in Settings. Look and Weather remain reachable.
- Stop means stop, including blocked playback. Enabling audio is a separate
  action. An active session gets a Stop control inside an open modal's accessible
  scope. UI simplification must not hide permission or action confirmations.
- Reduced motion freezes the mark and stops its animation loop. Idle animation
  is throttled; document visibility stops rendering. No extra runtime dependency,
  downloaded font, raster asset, new model, or inference call is required.

Research anchors: Apple's [layout guidance](https://developer.apple.com/design/human-interface-guidelines/layout)
on progressive disclosure and [Reduce Motion guidance](https://support.apple.com/en-us/111781).
The implementation applies these as constraints, not as evidence of originality.

## Verified on 2026-09-09

`quiet_room_ui.py` boots the actual static client in isolated WebKit contexts.
All network responses, example replies, camera tracks, and voice state are
explicitly synthetic fixtures; no production credentials, live audio, or real
camera data are used.

Seven passing configurations: 375×667 and 402×874 in light/dark, 320×568 light,
667×375 landscape, and 375×667 with explicitly doubled computed font sizes.
The latter checks reflow, not physical iOS Dynamic Type.

Checks cover pairing visibility, exactly four idle controls, no horizontal
overflow, no idle scroll on standard portrait text, tool filtering/no results,
nested panel routing, focus trapping/restoration, immediate writing focus,
failed draft preservation, reply folding and recovery visibility, active-session
Stop in tools, reduced-motion pixels/loop shutdown, amplitude response,
camera-denial cleanup, cancellation/late-track cleanup, theme metadata, and
absence of uncaught browser errors. Screenshots are in `/private/tmp/evie-quiet-ui`.

Commands from repository root unless noted:

```sh
node --check backend/clients/pwa/app.js
node --check backend/clients/pwa/presence.js
git diff --check
/opt/homebrew/opt/python@3.14/bin/python3.14 backend/clients/pwa/tests/quiet_room_ui.py
# From backend:
uv run ruff check clients/pwa/tests/quiet_room_ui.py
uv run mypy clients/pwa/tests/quiet_room_ui.py
uv run python -m app.scripts.gen_release_manifest
uv run pytest -q tests/test_release_contract.py tests/test_pwa_audio.py tests/test_iphone_capability_plan.py
```

Focused regression result: 68 passed. The full repository suite was not run.
The Python browser harness is optional local QA, not a production dependency.

## What is still not real

This patch is not deployed. Physical Safari keyboard/safe-area behavior,
VoiceOver, live microphone/camera permission flows, voice quality, battery cost,
and owner preference need on-device validation. WebKit fixture checks do not
prove those outcomes. Market-wide uniqueness is not established.

No commits, pushes, production restarts, backend-engine changes, or dependency
manifest changes were made. Agent 1: reconcile aggregate baseline changes when
integrating this tree; unrelated worktree changes were preserved.
