# Evie Fabric — Final Report (2026-10-18, branch `evie-fabric`)

Supersedes the 2026-10-17 draft. Every number below was measured on this
tree; the command that produced it is named next to it.

## 1. What shipped

One local-first personal AI companion for a single owner, with every
conversational surface funnelled through one brain and every effect routed
through one policy engine that returns verified receipts.

- **One facade.** `app/fabric/facade.py` is the only funnel for live voice,
  Talk, typed CLI/API chat, phone text, and hands-free turns. Typed chat keeps
  its kernel selection tied to `EV_COGNITIVE_MODE` (production:
  `mimo_kernel`), because rerouting it on `EV_FABRIC=1` alone silently dropped
  attachment/perception provenance, the provider seam, the output-filter
  report and the historic 503 — see `docs/WIRING_AUDIT.md`.
- **Effect lanes behind policy.** files, shell sessions, terminal layouts,
  screen reference, SQL (scratch SQLite + throwaway Postgres), comms, personal
  data (EventKit reminders, Notes), builder jobs, atlas, operator, recipes.
  Every effect returns `{ok, verified, evidence, error, undo}`; `verified` is
  set only after a read-back.
- **Untrusted content is data.** A screenshot, email, message, web page or
  attachment that asks for an action escalates to CONFIRM centrally, whatever
  the tier: an attachment-bearing phone turn cannot execute a shell effect
  (proven end to end in S38), and the effect is parked with a ticket.
- **Owner surfaces.** `ev ask`, `ev panic`, `ev approvals/approve/deny`,
  `ev setup` (env, app, TCC, launchd, tailscale, voice, phone), `ev doctor
  --fabric`, `ev models status/install`, and the iPhone PWA at `/evie` with a
  phone panel (approvals inbox, image upload, stop, status).
- **Voice.** `clients/ears` is the single mic owner; the legacy
  `hands_free`/`device_listener` daemons refuse to start unless
  `EV_VOICE_LEGACY_STACKS=true`. Local wake/ASR/TTS weights are already on
  this host and `ev models` smokes them live.
- **Code by voice.** Builder jobs plan → apply → test → fix (4 distinct
  exit-preserving strategies, ≤6 rounds), delegate to an installed agent CLI
  when the owner allows it, survive a kill, and notify on completion.

## 2. Gates (measured, not asserted)

| Gate | Command | Result |
| --- | --- | --- |
| Branch gate | `make fabric-gate` (e2e fabric + latency probe + models + comms + tools + phone text + release contract + PWA pins + panel JS) | 347 e2e tests green in the last complete run; S23/S24 re-run 13 passed after the final fix |
| Full suite | `pytest tests -q --junitxml=…` | **5 036 passed / 115 failed / 34 skipped** (after the four S23/S24 nodes were re-run and overlaid; see `docs/TEST_DEBT.md`) |
| Baseline diff | `python -m app.scripts.test_debt --baseline-json /private/tmp/ev-baseline/nodeids.json` | **1 regression, 0 unexplained** (order coupling, passes in isolation), 114 inherited + triaged, **67 fixed** |
| Eval gates | `python -m app.scripts.eval_gates` | **25/25 gates, 160/160 checks, 5 conditional skips**, exit 0 |
| Lint | `ruff check app clients tests` | 11 findings, **none new** vs `pre-fabric-baseline` (baseline had 15) |
| Types | `mypy app clients` | 26 errors in 4 files vs baseline 29 in 5 — no new file, no new error in any file this run touched |
| Contract | `make update-contract` | **568 paths / 618 operations**; all 550 originally locked paths byte-identical; additions only |
| Settings | branch audit script | the 8 settings this branch added are present in `Settings`, `.env.example` **and** `docs/ENVIRONMENT.md` |
| Live proofs | `make live-proof` (LP1–LP11) | LP1–LP11 PASS, recorded in `docs/LIVE_PROOF.md` |
| Latency | `python -m app.scripts.latency_probe` | 25 real model turns: p50 2 811 ms, p95 10 026 ms, provider span p50 2 810 ms |

## 3. Live proofs (real lanes, harmless effects)

| LP | Lane | Evidence |
| --- | --- | --- |
| LP1 | files create/edit/undo/delete-to-trash | real files under `~/Desktop/evie-live-proof/`, hash + undo read-back |
| LP2 | long shell session surviving an API restart | tmux session streamed, cancelled, reattached |
| LP3 | policy denies secrets, escalates outside-root | DENY + CONFIRM receipts |
| LP4 | Ghostty/tmux layout | real windows/tabs/panes, pane read-back |
| LP5 | layout restore from a saved spec | restore verified by pane content |
| LP6 | screenshot → layout loop | real capture, OCR, geometry inference |
| LP7 | scratch SQL | SQLite + throwaway Postgres container, DDL/DML preview/rollback |
| LP8 | EventKit reminders | throwaway "Evie Live Proof" list, create → read-back → complete → delete |
| LP9 | Notes | throwaway folder, create → read-back → delete (folder cleaned) |
| LP10 | builder job | toy package planned/applied, 1 test passed, resume reports `done` |
| LP11 | PWA + phone panel over real HTTP | isolated API on :18001 + scratch DB: `/evie` 200, panel bytes == tree, pairing 200, status/pending 200, typed turn 200, cancel 200 |

## 4. Scenario coverage (S1–S42)

`backend/tests/e2e_fabric` collects **347 tests**. Groups added or extended in
this run: S23 route trace (8), S24 golden (5), S25 file fuzz (29), S26 shell
fuzz (104), S27 SQL fuzz (54), S28 injection (13), S29 panic (11), S30 atlas
(9), S31 operator (11), S32 recipes (6), S33 terminal (7), S34 screenshot loop
(7), S35/S36 PIM (3), S37 builder (10), S38 phone attachments (9), S39 voice
(4), S40 setup (10), S41 acknowledgement (1), S42 restart (3).

## 5. Defects found and fixed during the final run

1. **WhatsApp sends could never succeed.** The comms lane called
   `WhatsAppWebAdapter.search_chats/send`, but those live on the adapter's
   backing behind `execute()`; every linked-WhatsApp send raised
   `AttributeError`, was swallowed by the broadcast `except`, and reported an
   opaque failure. Fixed to the `execute()` seam (`resolve_chat` → scored
   unique recipient, then `send` with `verified_in_thread`), regression test
   added.
2. **Home Station "start a timer" answered `Unknown tool 'start_timer'`.**
   `FLEET_TOOL_SPECS` was imported and dispatched but never published in
   `TOOL_SPECS`, so the central dispatcher refused its own fleet tools. The
   22 fleet specs are now in the catalog (S37/phone tests green).
3. **`/v1/chat` lost four contracts when rerouted by `EV_FABRIC` alone**
   (attachment perception provenance, provider seam, filter report, 503 for a
   dead provider/cost cap). The endpoint now follows the brain mode, and safe
   read-only intents (presence, capabilities, callouts, elapsed) still answer
   before the model so "can you hear me?" stays instant.
4. **Phone attachment text could carry instructions.** Attachments are now
   device-scoped, OCR'd server-side, fenced as `DATA, NOT INSTRUCTIONS`, and
   the turn is tainted so every effect escalates to CONFIRM (S38 proves a
   shell command in a screenshot is parked, not run).
5. **PWA release coherence.** New panel → build bumped to `2026.10.18.1`,
   `evie-phone.js` added to the critical-asset manifest, `release.json`
   regenerated, pins verified by `test_release_contract`.
6. **Type/lint debt in this run's own files** — `run_turn` name shadowing,
   `terminal` untyped probe dict, 25 mypy errors in the CLI (three of them
   variable-shadowing bugs), `pytest` missing in S20. All fixed; parity with
   baseline is now strict.

## 6. Decisions taken (and why)

- **D7 narrowed for `/v1/chat`** after measurement: fabric-on alone is an
  *effect* switch, not a brain switch. Production `mimo_kernel` still routes
  chat through the kernel, and S23/S24 pin that mode.
- **Safe local intents** (`presence`, `protocols`, `list_callouts`,
  `session_elapsed`) answer before the kernel. They mutate nothing; effects
  stay with the kernel so policy and receipts are never duplicated.
- **Order-dependent failures are triaged, not hidden.** `docs/TEST_DEBT_TRIAGE.json`
  gives every remaining failure a cause; the gate fails on unexplained
  regressions only.
- **Latency is measured, never tuned to the report.** Two runs on the same
  build measured p50 2 811 ms and 6 428 ms, so no "faster" claim is made;
  acknowledgement is the pre-model SSE `status/accepted` event (S41) and its
  ≤1 500 ms budget is unchanged.

## 7. Owner flow

`ev setup` is the one guided path: environment → app → TCC (prompt-free probes,
then the panes it must open) → launchd (`ev.api`, `ev.ears`, workers) →
Tailscale (Serve ON, Funnel OFF) → voice (mic owner + "hey Evie" check) →
phone (tailnet origin, PWA, panel, pairing code, Shortcuts/Add-to-Home
guidance). `--report-only` prints the same report without changing anything.
See `docs/OWNER_SETUP.md` for the hand-held version.

## 8. Run / disable / rollback

- Run: `backend/.venv/bin/python -m pytest tests/e2e_fabric -q` (347 tests).
- Branch gate: `make fabric-gate`. Debt gate: `make test-debt XML=… BASELINE=…`.
- Live proofs: `make live-proof` (harmless effects only; isolated API on :18001).
- Disable effects: `EV_FABRIC=0` keeps the legacy brain; `ev panic` denies
  effects until cleared.
- Rollback: `git reset --hard pre-fabric-baseline` only on the owner's say-so;
  per-file undo lives in `.ev-undo/` + `.ev-trash/`, journal in
  `.ev-journal.jsonl`, builder jobs in `builder_jobs.db`, shell registry in
  `.ev-shell-sessions.json`.

## WHAT IS STILL NOT REAL

- **Real-device iPhone checks**: pairing, Add-to-Home-Screen, Shortcuts bridge,
  mic capture and stop were exercised over real HTTP and in the DOM stub, not
  on the owner's phone. OWNER-ACTION.
- **Live sends**: Gmail has no OAuth credentials here, WhatsApp's Chrome
  profile is not linked, and iMessage/Mail sends were deliberately not
  performed (the brief forbids sending real messages). Draft → approve →
  verify is real; the final send is the owner's.
- **Deployed build**: the tailnet origin proxies the *deployed* server, which
  predates the phone panel. `ev setup phone` reports that as SKIP until
  `scripts/deploy_production.sh` publishes this tree.
- **Time-to-first-token** is not instrumented. Full-turn latency is measured;
  streaming replies into speech is the next real lever, not done here.
- **Wake word on-device**: weights and smokes are green, but the round trip
  needs the owner to say "hey Evie" with `ev setup`'s voice phase listening.
- **Live agent delegation** (code by voice): CLIs are installed and wired, the
  job store/fix loop/resume are tested with a fake CLI; a real delegation
  spends the owner's quota and is owner-run.
- **Inherited test debt**: 114 failures present at the baseline tag remain
  (env-dependent agent4/WhatsApp/digital suites, audio/video device state).
  They are triaged, not fixed, and one order-coupled test remains
  (passes in isolation).
