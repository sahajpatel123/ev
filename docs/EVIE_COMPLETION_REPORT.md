# Evie completion report

Measurement point: branch `evie-fabric` after `make verify-all` exited 0 on
2026-10-18T22:52Z (log `/tmp/ev-verify-all3.log`). The clean full suite is
`/tmp/ev-final-suite2.xml` (5156.60s). Nothing was pushed.

## 1. Executive summary

Evie on this Mac is one local companion: conversational turns go through
`app.fabric.facade.run_turn`, and effects go through one policy engine that
returns a receipt only after a read-back. At the start of this pass the
fabric was already wired and tagged, but the operator hung on
`entireContents()`, phone shortcuts opened an empty shell, and ears did not
recognize `Evie.app`. Those three are fixed and re-tested. `make verify-all`
is green. The full suite's failing set is a strict subset of
`pre-fabric-baseline` (0 regressions, 42 inherited failures, all with a
cause). Acknowledgement p95 is 7.9 ms. First-token time was measured on the
real mimo provider and is not inside 1500 ms; that budget was not moved.
The owner still has to deploy the tailnet build, install the iOS shell, and
say "hey Evie" once. `ev setup` is the guided path for the clicks.

## 2. Wiring

Evidence: `docs/WIRING_AUDIT.md` and `tests/e2e_fabric/test_s23_routetrace.py`,
which is inside the 442 fabric-gate passes. `EV_FABRIC=0` is
`tests/e2e_fabric/test_s24_golden.py` in that same run.

| Entrypoint | Before (tag `e3acaff`) | After | Proof |
| --- | --- | --- | --- |
| CLI `ev ask` | `POST /v1/chat`, fabric not imported | pipeline kernel branch → facade | S23 |
| Hotkey / Talk / PWA voice / `WS /v1/voice/live` | `handle_turn_maybe_remote` | same funnel → facade | S23 `shell.run` |
| PWA text / device-gateway text | kernel or legacy pipeline | facade; legacy fallback remains | S23 `atlas.discover` |
| `POST /v1/chat` | kernel only if `kernel_mode_active()` | same rule; fabric is the effect switch, not a second brain switch | S23, S24 |
| `POST /v1/cognitive/turn` | direct `handle_turn` | facade | S23 |
| `WS /v1/voice/hands-free` | chat pipeline | facade, `source=voice` | S23 |
| Shortcuts, routines, workers | not turns | still not turns; effects hit policy | S23 non-turn rows |

`/v1/chat` does not reroute on `EV_FABRIC=1` alone. That reroute dropped
attachment provenance, the provider seam, the filter report, and the 503
contract. Production `mimo_kernel` still uses the kernel. Recorded in
`docs/WIRING_AUDIT.md`.

## 3. Capabilities

| Capability | Status | Proof | Owner step |
| --- | --- | --- | --- |
| Files create/edit/undo/delete-to-trash | WORKING | S1–S4, S25; LP1 | none |
| Shell sessions, panic, audit hash chain | WORKING | S5–S6, S26, S29; LP2 | none |
| Policy denies secrets, confirms outside roots | WORKING | S4, S25; LP3 | none |
| Ghostty + tmux layouts | WORKING | S33; LP4, LP5 | none |
| Screenshot artifact | WORKING | S8, S9, S34; LP6 | geometry did not invent a layout; restore uses the saved spec |
| Scratch SQL + throwaway Postgres | WORKING | S7, S27; LP7 | none |
| EventKit reminders | WORKING | S35; LP8 | none |
| Notes | WORKING | S36; LP9 | Automation for Notes has no prompt-free probe (`ev setup` prints SKIP) |
| Builder job | WORKING | S14, S15, S37; LP10 | a live coding-agent CLI spends quota; the proof uses the internal loop |
| Comms draft → confirm | PARTIAL | S11–S13, S19, S28 | Gmail OAuth, WhatsApp link, and any real send are owner actions. No message was sent |
| App atlas + operator | WORKING | S30, S31, S32, S20 | TextEdit path is index-then-role because AX indexes go stale |
| Phone panel over HTTP | WORKING | S17, S38; LP11 | tailnet panel is SKIP until `scripts/deploy_production.sh` |
| Phone shortcuts | PARTIAL | `test_s38_shortcuts.py` (source contract); Swift parse only | install EvieShell. No iOS SDK on this Mac |
| Voice doubles | WORKING | S39, S22 | say "hey Evie" in interactive `ev setup` |
| Ears as mic owner | WORKING | menu-bar pgrep test; `ev.ears` loaded | none |
| Ack latency | WORKING | probe `/tmp/ev-latency.log`, p95 7.9 ms | none |
| First token | WORKING as a measurement, not a budget pass | same log, p50 3945.9 ms, p95 12358.2 ms | none; do not treat this as "fast" |
| `ev setup` / `ev doctor --fabric` | WORKING | two report-only runs byte-identical; doctor exit 0 | interactive panes for Automation |

## 4. Scenarios and live proofs

S1–S42 live in `backend/tests/e2e_fabric/`. `make verify-all` ran that
package plus the fabric phone, contract, latency, and models tests:
**442 passed, 0 failed, 518.88s** (`/tmp/ev-verify-all3.log`). Two earlier
fabric-gate runs failed live TextEdit/Calculator/OCR under focus contention.
The passing run is after `act()` falls back from a stale window index to a
role search, and after a failed act is retried inside the step budget.
Isolated re-run of TextEdit and Calculator after that change: 3 passed in
24.50s.

| Live proof | Result | Duration | When |
| --- | --- | --- | --- |
| LP1 files | PASS | 0.3s | 2026-10-18T22:07:02Z |
| LP2 shell reattach | PASS | 0.2s | same run |
| LP3 policy | PASS | 0.0s | same run |
| LP4 Ghostty/tmux | PASS | 4.6s | same run |
| LP5 layout restore | PASS | 2.6s | same run |
| LP6 capture + restore | PASS | see `docs/LIVE_PROOF.md` | re-run after restore was added to the harness |
| LP7 SQL | PASS | 5.2s | same run |
| LP8 EventKit | PASS | 3.8s | same run |
| LP9 Notes | PASS | 1.0s | same run |
| LP10 builder | PASS | 0.7s | same run |
| LP11 phone HTTP | PASS | 11.2s | same run |

LP6 record: markers read back, capture file exists, artifact event stored
with `untrusted=true`, geometry panes=0, then the saved spec restored both
markers. The harness log is `/tmp/ev-live-proof2.log` (11 PASS, exit 0).

## 5. Before and after

| | Baseline `pre-fabric-baseline` | This branch |
| --- | --- | --- |
| Full suite | 4559 passed, 180 failed, 1 error, 28 skipped (`/private/tmp/ev-baseline/junit.xml`) | 5135 passed, 42 failed, 0 errors, 28 skipped (`/tmp/ev-final-suite2.xml`, 5156.60s) |
| Regressions | — | 0 (`python -m app.scripts.test_debt`, exit 0) |
| Fixed vs baseline | — | 139 |
| Eval | the pre-fabric run was not re-measured here | 25/25 gates, 160/160 checks, 5 skips, exit 0 |
| Ack p95 | not re-measured at the tag in this pass | 7.9 ms (budget 1500) |
| First token p50 / p95 | not re-measured at the tag in this pass | 3945.9 / 12358.2 ms (20 stream samples, mimo) |
| Full turn p50 / p95 | — | 3926.4 / 17605.8 ms, PARTIAL (18/20 counted) |
| Contract | 550 paths | 568 paths, 0 original paths changed, 18 added |
| Ruff | 11 findings on this tree, none in files this pass edited | same 11 |
| `make verify-all` | — | exit 0 |

The five eval skips are conditional: speaker weights, face weights, and
three owner-fixture artifacts (`owner_fact_recall`, `owner_provenance_answer`,
`owner_state_no_moralize`). Listed in `/tmp/ev-eval.log`.

A first suite (`/tmp/ev-final-suite.log`) showed 3 extra failures. Two were
`database is locked` on fixture setup. One was `inspect.getsource` reading
`ears/main.py` while this pass was editing it. All three pass alone. They
are not in the clean junit.

## 6. Owner sequence

1. `cd backend && uv run ev setup` (no `--report-only` if you want it to
   load anything still skipped and to open panes).
2. `--report-only` on this machine, twice, byte-identical, exit 0. It does
   not grant Automation. Those three rows stay SKIP until you click Allow
   for Finder, System Events, and Notes. The verifier is the same command.
3. `uv run ev doctor --fabric` — exit 0 after Evie.app is running.
   `ev.ears` was loaded in this pass (`launchctl load -w`, pid 92617).
4. `uv run ev setup phone` — prints the Tailscale Serve URL and a pairing
   QR. The phone-panel line is SKIP until you run
   `scripts/deploy_production.sh`. This pass did not deploy production.
5. On the iPhone: install EvieShell so Shortcuts gains Ask Evie, Send latest
   screenshot (feed it Get Latest Screenshots), Approve pending, Deny
   pending, and Status. This Mac has no iOS SDK, so that build was not
   compiled here.
6. Say "hey Evie" during interactive `ev setup` (report-only skips the wake
   check). Confirm a real send yourself if you want one; this pass sent none.
7. Cellular reachability of the tailnet URL is the one check that has to
   happen off Wi-Fi. `ev phone check` is the verifier on the Mac side.

## 7. Deprecations

Canonical list: `docs/EVIE_DEPRECATIONS.md`.

| Cluster | Canonical |
| --- | --- |
| Turn | `app.fabric.facade` / `app.fabric.run_turn` |
| Policy | `app.fabric.policy` |
| Files, shell, SQL, screen, terminal, comms, builder | `app.fabric.*` as named in the deprecations doc |
| Mic | `backend/clients/ears/` (`EV_VOICE_LEGACY_STACKS` gates the other daemons) |
| Phone UI | `backend/clients/pwa/evie-phone.js` + `ios/EvieShell` |
| iOS app | EvieShell ships; `ios/EVApp` stays frozen |
| Menu-bar app | `/Applications/Evie.app`; `/Applications/EV.app` is a symlink |

## 8. Rollback and kill switches

- `EV_FABRIC=0` keeps the legacy brain. S24 in the green fabric-gate run.
- `ev panic` denies effects until the owner clears it. S29 is in that run.
  Doctor reported panic clear.
- Undo files live under the fabric undo store (`.ev-undo/`, `.ev-trash/`).
  LP1 deleted to trash and restored.
- Tags: `pre-fabric-baseline` (`bcf1db6`), `fabric-v1-start` (`e3acaff`),
  `evie-fabric-v1` (`10fd342`, the earlier merge). This pass did not move
  `evie-fabric-v1`. Moving it would rewrite the tag.
- App backup taken before this pass:
  `~/.ev/backups/Evie.app.before-final-20261019-005655`
  (same signature, `EV Code Signing`, 2026-10-18 12:48).
- Rollback check: `git clone --local` to `/tmp/ev-rollback-clone`, detached
  at `pre-fabric-baseline`. `app.main` imports from that tree.
  `import app.fabric` raises `ModuleNotFoundError`.

## 9. Remaining risks

- First-token p95 is 12.4s on this probe. An earlier probe the same day was
  1.6s. The provider dominates. Speech still waits for the full turn.
- 42 inherited test failures are documented in `docs/TEST_DEBT.md`. They
  failed at the baseline tag too. This pass did not "fix" them by skipping.
- The tailnet origin still serves the previously deployed build. The phone
  panel in this tree is not what the phone hits until you deploy.
- Approve-from-a-device-token returns 403 without a fresh confirmation. The
  shortcut says so. It does not claim the action ran.
- Screenshot geometry did not recover a terminal layout (0 panes,
  confidence 0). Restore used the saved spec.
- Automation permission for Notes/System Events/Finder cannot be probed
  without a prompt. LP9 still completed a Notes create/read/delete.
- Gmail and WhatsApp sends are unwired for a real delivery here (no OAuth,
  WhatsApp not linked). Draft and confirm are tested. Nothing was sent.
- `ruff check app clients tests` still reports 11 findings, all outside
  the files this pass edited (`laptop_files` is undefined in
  `app/digital/adapters/files.py` on the read path; that test already
  failed at baseline).
- The clean full-suite junit was taken before the final TextEdit role
  fallback. Fabric-gate after that fallback is the 442-pass run. A second
  full suite was not started after that one-file change.
