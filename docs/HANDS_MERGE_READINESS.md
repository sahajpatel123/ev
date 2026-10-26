# HANDS merge readiness (CLI 4, branch `evie-hands`)

Base: `main` @ bf0412c (the newest integration base; `evie-brain-program` is CLI 1's branch and is not one).
Worktree `/Users/sahajpatel/Code/evie-hands`, own venv. Nothing merged, pushed or deployed.

**Verdict: mergeable as additive code with one known gap (Safari/Arc/Zoom/FaceTime are not live-tested) and three owner actions.
T1 and T2 are proven on my own fixtures and real captures, not yet on the owner's real screen and real YouTube.**

## What was built

* `docs/contracts/SKILLS_API.md` v1.0.0 and `docs/contracts/GUI_LOCK.md`; `scripts/gui-lock` (stdlib; adopted by CLI 1 on its own).
* `backend/app/fabric/skills/` (30 modules, ~9k lines): receipts, runtime abstraction (fake machine for tests), world model, action
  engine primitives (`ui_find/act/wait_until/observe/verify` over DOM, Accessibility, keys, trusted pointer, OCR), registry with
  deterministic `skill_find` and the policy gate, 61 skills in 9 packs, watchers, learning and self-healing, screenshot-as-reference
  (terminal layout, browser tabs, window arrangement, settings toggles), diagnostics, owner checks.
* `helpers/evhands/` (one Swift file): Accessibility panes/find/act/setframe, trusted click/key, CG window list, prompt-free
  Automation/Screen Recording probes. 40x faster than System Events JXA. Built on demand with `swiftc` into `~/.ev/hands/bin`.
* `backend/hands_cug/` + `backend/tests/hands/`: the gauntlet (255 tasks, 4 phrasings each), fixture server and pages (own markup),
  real-screenshot generator, T1 evaluation, report, real-brain harness (not run, see below). 234 offline tests.
* Safari private windows are dropped via Accessibility window titles (fail-closed: without an AX grant Safari is not
  listed at all); windows AX cannot see are treated as private. `SafariDriver(rt, ax=None)` takes an injectable AX client.
* 34 real Ghostty captures (22 dev + 12 held-out) and their ground truth under `backend/tests/fixtures/hands/screens`.
* `EV_HANDS_*` settings (12) in `config.py` (one appended block), `.env.example`, `docs/ENVIRONMENT.md`.

## Results (full table with intervals: `docs/HANDS_CUG_RESULTS.md`, data in `docs/cug/*.json`)

| Target | Result | n, 95% Wilson |
| --- | --- | --- |
| CUG pass >= 90% fixtures | 100% (after fixes; first run 145/146) | 146/146, 97.4-100 |
| CUG pass >= 85% live apps | 100% (after fixes; first run 58/75) | 75/75, 95.1-100 |
| 0 false-success claims | 0 | 0 of 255 tasks |
| 0 effects outside the task | 0 | owner's Ghostty/Safari state snapshotted around every live task |
| T1 layout tree exact >= 90% | 100% (held-out blind first pass 10/12) | 34/34, 89.8-100 |
| T1 ratios within 8% | worst 0.52% | 34/34 |
| T1 ids exact-or-flagged, never a wrong one | 0 wrong executed; unreadable/ambiguous/duplicate flagged | 118/118 panes, 96.9-100 |
| T1 restore verified by pane read-back | 4/4 on real captures, each pane: cwd, command, session process with the id | 4/4, 51-100 |
| T2 skip within 1.5 s p95 of availability | p95 54 ms (median 21 ms) | n=15 |
| T2 on pages that refuse scripted clicks | **1.6-1.8 s: misses 1.5 s** (real AX press after a 0.6 s grace) | n=3 |
| single-step media <= 3 s median | 941 ms | n=8 |
| watcher idle CPU <= 1% | 0.10% host CPU | one 40 s run |
| learned skills >= 80% compile + replay | 6/6 | 61-100 |
| self-heal >= 70% of changed variants | 4/4 completed (3 needed a repair) | 51-100 |
| real-brain CUG | **not run: OWNER-ACTION raise the OpenRouter key limit** ($0.21 left of $5; shared with the live assistant) | harness tested with a scripted model |

Targets missed: the untrusted-click latency (cause above), and the real-brain run (budget). Nothing was loosened.

## Integration steps for CLI 1

1. `from app.fabric.skills.api import Hands` (see SKILLS_API §0). One `Hands()` per process; calls block, use `.aio` from async code.
2. Expose 5-6 tools (names yours): `world`, `find`, `act`, `skill` (find/run), `watch`, `screen_reference`. Keep `skill_find` -> `skill_run` the
   dominant path; the primitives are for novel tasks. Feed back `say`, `ok`, `verified`, `error{code,message,fix}`, `needs_confirm`, `confirm`.
3. Show `confirm` on CLI 2's card; re-call the same method with `approved=True, approval_id=<plan_id>`. If `confirm.physical` only a physical
   approval may set `approved`. Pass `provenance` for anything quoted from a page/screenshot/terminal.
4. Route `set_event_sink` events (watchers) to the thread and the originating channel (B5).
5. `ev hands check permissions|t1|t2` -> `app.fabric.skills.check` (`scripts/ev-hands` already wraps it).
6. For the Evie.app bridge: optional new op that launches `~/.ev/hands/bin/evhands` so Accessibility runs under the app's grant (today it runs
   under the caller's; without a grant every AX call returns `permission_missing` with the Settings path, nothing is faked).
7. `make baseline-write` / `update-contract`: no API route was added, so the contract is unchanged; baseline numbers (modules, tests, settings: +12) need the usual refresh.

## Owner actions

1. Chrome **View > Developer > Allow JavaScript from Apple Events** (Safari: Settings > Developer). Without it T2 answers `js_disabled` and names the setting.
2. Grant Accessibility and Automation (Ghostty, Chrome/Safari, Finder, System Events) to Evie.app, then `scripts/ev-hands check permissions`.
3. Raise the OpenRouter key limit, then run the real-brain sample (`hands_cug/brain.py`, ~$0.001/call).
4. Run `scripts/ev-hands check t1` on a real screenshot and `check t2` on a real YouTube ad (docs/HANDS_OWNER_CHECKS.md).

## Known gaps (honest)

* Safari, Zoom, FaceTime, Music, Spotify, VS Code/Cursor are not live-tested (scripted machine only); Arc is not installed.
* gemini resumes by list index, not id: reported `unverifiable`, never automated.
* Divider detection assumes lossless screenshots; real YouTube markup is unseen (my fixture has five skins incl. aria-only and shadow DOM).
* `ocr helper`: needs `helpers/evvision` built (it is, in the main checkout); the worktree finds it via `EV_HANDS_OCR_BINARY`.
* Learned skills live in `~/.ev/hands/learned`, not in the routines/recipes DB (different step model).
* Another session (CLI 1) ran terminal e2e without the GUI lock once and a test window of mine disappeared at that moment; the lock is now shared.
* Process-level facts measured under load average 15-20: `evvision accurate` took 86 s once; HANDS runs `fast` first.

## Merge command (do not run from here)

```sh
cd /Users/sahajpatel/Code/ev          # only when no process reads this tree (R1)
git merge --no-ff evie-hands -m "Merge branch 'evie-hands': HANDS skills package, SKILLS_API v1.0.0, CUG"
```
Conflicts expected only in `backend/app/config.py` (my appended `AGENT 4 HANDS` block), `.env.example` and `docs/ENVIRONMENT.md`: keep both sides.

## Independent re-run (fresh session, 2026-10-26)

* Offline: `pytest tests/hands` 234 passed; `ruff` clean; `mypy app/fabric/skills` clean.
* Sim lane re-run 146/146 and corpus lane re-run 34/34, both byte-identical to `docs/cug/*.json` except machine timings
  (recorded files restored afterwards); 0 false-success, 0 crashed, 0 scope violations.
* Live Ghostty re-run under the GUI lock: all 12 `live_term` tasks pass (4 T1 restores + 8 driver tasks), 0 scope violations.
* Live Chrome was NOT re-run: another session's headless Chromes (`evg-smoke-*`, WhatsApp CDP) are running and the harness
  refuses to start while any Chrome runs (it would `quit` someone else's browser on exit). Recorded `live.json` (63/63) stands.
* Receipt spot-check: `skill.find("skip the ad")` ranks `web_media.skip_ad` top; rows carry preconditions/permissions/risk/args;
  every receipt carries contract tag `evie-hands/1`. SKILLS_API method names verified against `api.py` (one doc nit fixed:
  `skill_list()`, not `skill.list()`).

## Scope check (H-I2)

Files touched outside my own paths: `backend/app/config.py` (one appended block), `.env.example`, `docs/ENVIRONMENT.md` (R5 requires the three).
Not touched: kernel, context assembly, tool schemas, doctrine, `capabilities.py`, CLI 2/3 files, root Makefile, API contract, CLAUDE.md, AGENTS.md,
launchd, `/Applications/Evie.app`, the live API on :8000.
