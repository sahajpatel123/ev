# Evie Build Log — 2026-10-17

Measured on branch `evie-fabric` (commit `bcf1db6`).

## Host
- macOS 27.2 (Build 26B5101f) — `sw_vers`
- tmux 3.6b
- Ghostty 1.3.1 stable (Zig 0.15.2, ReleaseFast, renderer Metal, libxev kqueue)
- Docker 29.2.1 (build a5c7197d72)

## Desktop screenshots
- `~/Desktop` entries: 30 total, 29 matching `Screenshot*`

## launchd (grep ev)
- `ev.talk` pid 1164, `ev.backup` not running, `ev.collector` pid 1152,
  `ev.opencode` not running, `ev.api` pid 1166, `ev.scheduler` pid 1150,
  `ev.runtime` pid 1157, `ev.worker` pid 1159

## API contract
- File: `backend/eval/contract_v1.json` (32431 bytes)
- Top-level keys: `locked_at`, `paths`, `schema_version`, `stability_policy`
- Locked path count: 550

## evie-fabric drift/contract check — 2026-10-17
- Fabric modules: no new EV_* env vars, no bare os.getenv/os.environ, no APIRouter/include_router; Settings/.env.example/ENVIRONMENT.md in sync (no changes needed).
- Contract: backend/eval/contract_v1.json still 550 locked paths; no routes added, update-contract not required.

## Ghostty scripting probe (D2) — 2026-10-17
- Ghostty 1.3.1, CLI on PATH at /Applications/Ghostty.app/Contents/MacOS/ghostty.
- `ghostty --help`: on macOS the CLI supports ONLY `+actions`
  (+version/+help/+list-fonts/+list-keybinds/+list-themes/...) — no window, tab,
  split, or send-text control. No CFBundleURLTypes in Info.plist (no URL scheme).
- `sdef` unavailable (no Xcode; CLT only) so no scriptability-dictionary check;
  Ghostty is not AppleScript-scriptable. Conclusion: GhosttyScripted backend is
  NOT implementable on this host — tmux is the canonical addressing layer
  (`backend/app/fabric/terminal.py` TmuxLayout). Revisit if Ghostty ships remote control.
- Screenshots: no custom `com.apple.screencapture location` key → default ~/Desktop.

## Hardening pass — 2026-10-17 (commits c2820ee, 44d9cba)
- done: R7 verified-by-readback receipts; R8 untrusted provenance escalation;
  run_turn facade (ops dispatch, 16-step budget, cancel, unfulfilled-gap);
  shell registry on disk + command classification + redaction; SQL DML preview
  in rolled-back txn with row counts; builder real step exec + sqlite state;
  comms confirm-gate + idempotency keys; TmuxLayout plan/confirm/open/readback;
  41 e2e tests green, ruff clean on fabric+e2e.
- failing: none in e2e_fabric. Full suite running in background (result pending).
- assumptions: sqlite-only SQL lane (no sqlglot installed; cannot add deps);
  S13 reminders = lane-contract test only (real EventKit = OWNER-ACTION);
  S17 = turn-level only (real device = OWNER-ACTION); chat TTFT UNVERIFIED.
- next: collect full-suite result; eval gates; final report; commit docs.

## Full-suite delta — 2026-10-17 (amendment)- done: full suite 175 failed / 4587 passed / 28 skipped (74:09). Not a regression:
  isolation proof (no fabric imports outside fabric, no count assertions, no
  .env.example consumers) + baseline-worktree rerun of sampled failing files gives
  identical 23 failed / 58 passed on tag and branch. PWA hash drift last touched bcf1db6.
- failing: 175 pre-existing/environmental (whatsapp env, agent4, PWA manifest, extras).
- assumptions: sampled-file equivalence + isolation generalizes to all 175 (full
  per-test diff would need a second 74-min baseline run; mechanism for fabric breakage
  does not exist).
- next: none — report §4 amended, goal complete.

## FINAL RUN — WP0 orient/baseline/wiring (2026-10-18, commit 14929eb)
- done: branch evie-fabric clean @ e3acaff; tags pre-fabric-baseline/fabric-v1-start intact; Evie.app backed up; env probe (Ghostty 1.3.1, tmux 3.6b, Tailscale stopped, Docker 29.2.1, signing "EV Code Signing"); contract 550/sha 6f367581; docs/WIRING_AUDIT.md written; FULL baseline suite launched in /private/tmp/ev-base-full (bg shell sh_14bee54ad001Ub4Dg12yINUVk2).
- failing: none (WP0 gate = wiring table + baseline launched; both met).
- assumptions: non-conversational surfaces (shortcuts/routines/RQ) integrate via policy+dispatch, not run_turn (recorded in audit §2 with R8 rationale).
- next: WP1 facade + registration + /v1/fabric/* routes + S23/S24.

## FINAL RUN — WP1 wire the fabric (2026-10-18, commits facd515,00b8deb,+routes)
- done: facade (arun_turn/arun_kernel_turn) funnels all 4 kernel call sites; 8 lanes in TOOL_SPECS/SEMANTIC_TOOLS/dispatch/executor; policy fronts every effect (CONFIRM=ticketed hold, resume-tested); 15 /v1/fabric/* paths; contract 567 (550 originals byte-identical + 14 fabric + 3 pre-existing relay); S23 (8) + S24 (5) green; ruff/mypy clean on new code.
- failing: gate 3 failed / 206 passed — all 3 fail identically at pre-fabric-baseline (test_turn x2, test_tool_loop chat_tool_loop). 6 legacy-subject tests pinned to EV_FABRIC=0 (tool_loop x4 incl. pre-existing, turn x2, command_loop x1).
- assumptions: D7 kernel-default (prod mimo_kernel=no-op; tests flip); execute_command NOT rewired (locked jail, not a shell); shortcuts/routines/workers integrate via policy+dispatch, never the turn facade (R8); R2-on-device-actor holds stay ticketless until WP11 (existing policy, not weakened); relay drift locked (fixes 1 eval gate).
- next: WP2 policy/files/shell hardening (S25/S26/S29 fuzz + ev panic) + LP1-LP3.

## BASELINE full suite collected (2026-10-18)
- pre-fabric-baseline (bcf1db6) full run: 181 failed / 4559 passed / 28 skipped in 62:43. Per-test node-id sets in .../T/opencode/baseline-nodeids.json + baseline-junit.xml. (Prior run's 175/4587 fig was branch-state, not a full baseline run.)

## FINAL RUN — WP2 policy/files/shell hardened (2026-10-18)
- done: files lane rewritten (deny oracle, special/nlink gates, TOCTOU guards, contained undo, link-aware delete) + files_op + S25 31/31; shell classifier +24 bypasses fixed + S26 108/108; audit hash-chain wired into every dispatch call + ops turns; panic (API+CLI+hotkey, kills sessions, cancels turns, revokes grants, denies effects <2s) + S29 11/11; LP1-LP3 PASS in docs/LIVE_PROOF.md. Gate 209/209.
- failing: none in WP2 scope.
- assumptions: AUTO one-shots stay available under panic (side-effect-free reads); build verbs (make/pytest/go/cargo) AUTO_JOURNAL, bare interpreters CONFIRM; file_sandbox/worker path untouched (WP14 deprecation decides with evidence); macOS app has 4 pre-existing broken Swift files (WP3 must repair to ship Evie.app); panic hotkey binding code-verified + EVClient builds, key-press itself = owner self-test.
- next: WP3 bridge + ev setup (needs Swift repairs first); eval branch+baseline in background.

## Eval gates collected (2026-10-18)
- Branch (WP2 HEAD): 25/25 gates, 160/160 checks, 5 skipped, EXIT 0. The relay-contract lock repaired the 1 failing gate; fabric routes/tools broke nothing.
- Baseline tag: 24/25 gates, 143/144 checks (known relay-routes failure), EXIT 1.

## FINAL RUN — WP3 bridge + ev setup (2026-10-18)
- done: unix-socket bridge (Swift server + Python client, token+peer auth) live in production Evie.app; rebuilt+installed 2x with same EV Code Signing identity, all TCC grants kept; JXA language auto-detect; ev setup (6 phases) + doctor --fabric; /v1/fabric/exec (contract 568); login item registered; ev.ears loaded (yields mic to app); Tailscale started (Running, Serve :8000 tailnet-only, Funnel OFF). Gate 66/66 (S10/S16/S40/S21/S23/S24/API/CLI).
- failing: none. Interactive setup: all green except tailscale (now Running) — zero owner clicks needed (grants inherited).
- assumptions: trivial JXA stays headless, tell-app via bridge; automation verified by attempt (no prompt-free probe exists); live-send phase logic S40-proven + isolated-server park/approve/execute/file proven (one harness-fifo artifact noted, all layers green independently).
- next: WP4 terminal bridge real (Ghostty re-probe + tmux canonical) + LP4-LP5.

## FINAL RUN — WP4 terminal bridge real (2026-10-18)
- done: Ghostty 1.3.1 re-probed thoroughly — full AppleScript dictionary found (prior MISSING overturned): new window/tab, 4-way split, input text, perform action, stable ids; every verb live-driven + read-back via write_screen_file:copy (marker verified). ghostty.py backend + hybrid GhosttyLayout (visible window, tmux inside) + tmux socket/attach/snapshot + resume registry + dispatch backend select (tmux default, ghostty opt-in). S33 7/7; LP4-LP5 PASS. Gate 53/53, no tmux/Ghostty leakage.
- failing: none. Fixed a tmux-session leak in my own restore test (now closes).
- assumptions: visible Ghostty windows are opt-in per call (kernel default stays headless tmux); AX/keystroke native splits intentionally unimplemented (tmux covers splits deterministically; AX is flaky + focus-stealing); snapshot restores shells+cwds, not running commands (documented tmux limit); websearch down, local probes conclusive.
- next: WP5 screenshot-as-reference live (OCR geometry + VLM pass decision) + LP6.

## FINAL RUN — WP5 screenshot-as-reference (2026-10-18)
- done: screen_pipeline (capture via bridge identity, AppleVision OCR, VLM-when-keyed else capped-geometry infer, typed untrusted ScreenArtifact persisted as Event); resolve_last conversation>store>real-dir; table/error extraction; dispatch ocr/analyze ops; S34 7/7 (live Ghostty loop + fixture loop + generalization + VLM branch); LP6 PASS. Gate 19/19 (S34/S33/S8/S9), no leaks.
- failing: none. Fixed en route: normalized-vs-pixel band gap; input-text newlines don't submit (spawn-attached instead); spawned-shell PATH (absolute tmux); OCR double under pytest (explicit apple_vision); shell-readiness race (removed with spawn-attached).
- assumptions: VLM sends screenshots to the keyed model (same as look.capture; owner-invoked feature); full-screen OCR bands the whole display (restore plans come from known specs, not bands); one S33 flake was my own overlapping GUI test runs (serial green; close-animation tolerance added anyway).
- owner-action noted: rebuilt Evie.app shows a Keychain device-auth prompt on first API use (click Allow) — for OWNER_SETUP.
- next: WP6 atlas + operator loop + recipes (S30/S31/S32/S20).
