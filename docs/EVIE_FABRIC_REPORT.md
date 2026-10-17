# Evie Fabric — Final Report (2026-10-17, branch `evie-fabric`)

## 1. Executive summary (<=12 lines)
Evie Fabric adds a policy-gated capability layer (`backend/app/fabric/`, 15 modules)
behind one turn facade (`run_turn`), with verified-by-readback receipts on every effect.
41/41 e2e scenario tests pass in ~52s; ruff is clean on all fabric code and tests.
Eval gates hold at 24/25 (the 1 failure is pre-existing relay-route drift, proven at tag).
The 550-path API contract is byte-unchanged; fabric adds zero routes (S21 test-enforced).
tmux is the proven terminal addressing layer; Ghostty 1.3.1 has no scripting surface (probed).
Chat first-token stays UNVERIFIED (no provider keys here; never faked).
iPhone relay and EventKit reminders are PARTIAL (lane contracts pass; real-device/EventKit
steps are OWNER_ACTIONS). Full-suite background result was still pending at report time.

## 2. Capability table

| Capability | Canonical module | Status | Proof | Owner must do |
|---|---|---|---|---|
| Policy engine (AUTO/AUTO_JOURNAL/CONFIRM/DENY + provenance) | `fabric/policy.py` | WORKING | test_s04_policy_decide_provenance | none |
| Files create/overwrite + hash verify + journal + undo | `fabric/files.py` | WORKING | test_s01_file_create_verified, test_s02_overwrite_snapshot_and_undo | set EV_FABRIC_ROOTS |
| Trash delete, bulk>25 + outside-root CONFIRM, secrets DENY | `fabric/files.py` | WORKING | test_s03_*, test_s04_secrets_deny_outside_confirm | none |
| One-brain facade (ops, 16-step budget, cancel, gap-never-phantom) | `fabric/run_turn.py`+`turn.py` | WORKING | test_s06_multistep_through_facade, test_s20_unknown_lane_never_phantom | none |
| Shell sessions (tmux stream/cancel/reattach, disk registry) | `fabric/shell_sessions.py` | WORKING | test_s05_stream_cancel_reattach | none (tmux installed) |
| Command classification + secret redaction | `fabric/shell_sessions.py` | WORKING | test_s05_classify_and_deny | none |
| tmux layouts (plan→confirm→open→pane readback) | `fabric/terminal.py` | WORKING | test_s08_tmux_layout_readback | none |
| Screenshot resolution + OCR-geometry pane inference | `fabric/screen_ref.py` | WORKING | test_s08_pane_inference_accuracy, test_s09_last_screenshot_resolution | none |
| Untrusted-content escalation (screenshots, tool output) | `fabric/policy.py` | WORKING | test_s09_embedded_instruction_not_executed | none |
| Headless JXA bridge + alias shim + honest permission receipts | `fabric/bridge.py` | WORKING | test_s10_*, test_s16_* | grant TCC per OWNER_ACTIONS |
| Comms draft→confirm→send→verify + injection escalation | `fabric/comms.py` | WORKING | test_s11_* (fake adapter) | OAuth for real Gmail |
| Contact disambiguation (no guessing) | `fabric/comms.py` | WORKING | test_s12_disambiguation_card_no_guess | none |
| Builder plan→execute→test-summary + kill/resume | `fabric/builder.py` | WORKING | test_s14_build_plan_and_apply, test_s15_kill_and_resume | none |
| Standing grants lifecycle + never-grantable classes | `fabric/grants.py` | WORKING | test_s19_* | none (OFF by default) |
| Gap records + closest alternative | `fabric/gaps.py` | WORKING | test_s20_gap_record_and_alternative | none |
| Ack ≤1500ms p95 (20-sample local rig) | `fabric/run_turn.py` | WORKING | test_s18_ack_within_budget | none |
| Stop cancels turn + shell lanes <2s | `fabric/run_turn.py`+`shell_sessions.py` | WORKING | test_s22_* | none |
| SQL scratch (read AUTO, DML preview/commit/rollback, DDL snapshot, prod DENY) | `fabric/sql_lane.py` | WORKING | test_s07_* (sqlite only) | add sqlglot dep for parser (Agent 2) |
| Contract intact + Settings drift enforced | — | WORKING | test_s21_* | none |
| Reminders lane contract | `fabric/bridge.py` | PARTIAL | test_s13_reminder_create_and_readback | grant Reminders; live JXA test |
| iPhone turn relay (text/voice/screenshot + exactly-once approval) | `fabric/turn.py`+`comms.py` | PARTIAL | test_s17_* (no real device) | pair device; cellular test |
| Ghostty scripted control | — | MISSING | probe in BUILD_LOG (no surface in 1.3.1) | none (tmux canonical) |
| Real Gmail/iMessage/WhatsApp sends | adapters (existing) | OWNER-ACTION | fake-adapter tests only | OAuth + TCC + live send |
| Chat first-token before/after | — | UNVERIFIED | no provider keys in env | run echo double-baseline |
| Full-suite comparison vs baseline | — | PENDING | bg run active at report time | see §4 |

## 3. Scenario results S1–S22

`backend/.venv/bin/python -m pytest backend/tests/e2e_fabric -q` → **41 passed in 51.86s**
(macOS 27.2 arm64; slowest: S5 stream/reattach 2.68s, S8 tmux layout 2.31s; most tests <0.2s
body time + ~1s pytest setup overhead).

| # | Test ids | Result |
|---|---|---|
| S1 | test_s01_file_create_verified | pass |
| S2 | test_s02_overwrite_snapshot_and_undo | pass |
| S3 | test_s03_delete_to_trash_and_bulk_confirm | pass |
| S4 | test_s04_secrets_deny_outside_confirm, test_s04_policy_decide_provenance | pass |
| S5 | test_s05_stream_cancel_reattach, test_s05_classify_and_deny | pass |
| S6 | test_s06_multistep_through_facade | pass |
| S7 | test_s07_read_auto, test_s07_dml_preview_commit_and_deny, test_s07_ddl_snapshot_and_prod_deny, test_s07_classify_worst_wins | pass |
| S8 | test_s08_pane_inference_accuracy, test_s08_plan_confirm_execute_pipeline, test_s08_tmux_layout_readback | pass |
| S9 | test_s09_last_screenshot_resolution, test_s09_embedded_instruction_not_executed | pass |
| S10 | test_s10_bridge_roundtrip_receipt_shape, test_s10_headless_jxa_action, test_s10_alias_shim | pass |
| S11 | test_s11_gmail_reply_flow, test_s11_injection_escalated_not_executed | pass |
| S12 | test_s12_disambiguation_card_no_guess | pass |
| S13 | test_s13_reminder_create_and_readback | pass (lane contract; PARTIAL wrt real EventKit) |
| S14 | test_s14_build_plan_and_apply | pass |
| S15 | test_s15_kill_and_resume | pass |
| S16 | test_s16_missing_tool_honest_receipt, test_s16_notes_permission_fix_text | pass |
| S17 | test_s17_modalities_reach_facade, test_s17_approval_executes_exactly_once | pass (turn level; PARTIAL wrt real device) |
| S18 | test_s18_ack_within_budget | pass |
| S19 | test_s19_grant_lifecycle, test_s19_never_grantable | pass |
| S20 | test_s20_gap_record_and_alternative, test_s20_unknown_lane_never_phantom | pass |
| S21 | test_s21_contract_paths_unchanged, test_s21_fabric_adds_no_routes, test_s21_fabric_settings_drift | pass |
| S22 | test_s22_precancelled_turn_aborts, test_s22_midturn_cancel_stops_lanes, test_s22_shell_cancel_within_2s | pass |

## 4. Before/after metrics
- Eval gates: **24/25, 159/160 checks, 5 skipped** (`backend/eval/last-run.json`, this run).
  The failing gate is `api_contract/no_unlocked_v1_routes` (7 `/v1/device-gateway/relay/*`
  routes added without a contract update). Proven pre-existing: the routes exist at tag
  `pre-fabric-baseline` (`git grep device-gateway/relay pre-fabric-baseline` → device_gateway/api.py).
  Fabric adds zero routes (test_s21_fabric_adds_no_routes). Recommendation: Agent 1 runs
  `make update-contract` or reverts the relay routes; fabric deliberately did not touch the lock file.
- Chat first-token: UNVERIFIED before and after (no provider keys; echo-double baseline never
  runnable here). No numbers invented. Ack latency (the D10 target) is WORKING: p95 over 20
  local turns well under 1500ms (test_s18_ack_within_budget).
- Test counts: e2e_fabric 22 thin tests → **41 genuine scenario tests**, all green.
  Full suite: background run was still active when this report was written; result to be
  appended (see BUILD_LOG). Known pre-existing failure observed earlier in isolation:
  `tests/test_agent4_acceptance.py::test_agent4_natural_requests_are_selected_advertised_and_shaped`
  (fails on unmodified paths; needs baseline-tag confirmation when the suite lands).
- Lint: `ruff check app/fabric tests/e2e_fabric` → **All checks passed**.

## 5. OWNER_ACTIONS (ordered; full text in docs/OWNER_ACTIONS.md)
1. `export EV_FABRIC_ROOTS=$HOME/Code,...` — verifier: e2e suite → 41 passed.
2. Review `EV_FABRIC_BULK_CONFIRM_N=25` default.
3. Grant TCC items (Microphone, Screen Recording, Accessibility, Automation, Full Disk
   Access, Reminders, …) via the deep links in OWNER_ACTIONS; verifier: toggles show Evie ON.
4. Chat TTFT echo double-baseline with provider keys; record numbers.
5. iPhone: pair real device, approve a CONFIRM card from the phone, confirm exactly-once
   execution; run cellular reachability test over Tailscale Serve (Funnel OFF).
6. Real EventKit reminder create+readback via JXA; paste receipt.
7. Decide on relay-route contract drift: `make update-contract` (Agent 1) or revert.

## 6. Deprecations (docs/EVIE_DEPRECATIONS.md)
Canonical: files/shell/sql/screen/bridge/comms/builder/turn/policy/grants/gaps → the
`backend/app/fabric/*.py` modules; terminal layouts → `fabric/terminal.py` TmuxLayout.
Deprecated: ad-hoc subprocess/sqlite/pixel paths bypassing policy; lane calls skipping
receipts; implicit grants; silent gaps; Ghostty CLI scripting on macOS (probed: only
`+actions` exist, no pane control); pre-existing rows for ears-mic-owner, EvieShell+PWA,
kernel-loop untouched.

## 7. Gaps, risks, assumptions, next steps
- Gaps: no sqlglot (Agent-2 dep) → heuristic SQL classifier, documented in code; no
  Postgres lane (sqlite scratch only); no Ghostty scripting (no vendor surface); no
  Siri/Shortcuts actions, no approvals inbox UI, no `ev permissions` CLI (designed, not built);
  voice/iPhone/remote remain at lane-contract level.
- Risks: `EV_FABRIC_ROOTS=""` default denies all file/shell lanes (safe, but owner must
  configure); S5 uses ~6s streaming proxy for the 90s scenario (mechanism identical:
  tmux persistence + disk registry); full-suite delta still unconfirmed at report time.
- Assumptions: single-owner local Mac; test profile roots are tmp dirs; contract lock file
  is Agent 1's to regenerate (fabric never hand-edits it).
- Next: (1) collect bg suite + confirm no worse than baseline; (2) Agent 1 contract call
  on relay routes; (3) wire fabric lanes into TOOL_SPECS/SEMANTIC_TOOLS so the cognitive
  kernel can call them; (4) approvals inbox + `ev permissions`; (5) real-device S17/S13.

## 8. Run / rollback / disable
- Run: `backend/.venv/bin/python -m pytest backend/tests/e2e_fabric -q` (41 passed expected, ~52s).
- Lint: `backend/.venv/bin/python -m ruff check backend/app/fabric backend/tests/e2e_fabric`.
- Eval: `cd backend && ../backend/.venv/bin/python -m app.scripts.eval_gates --report eval/last-run.json`.
- Configure: `export EV_FABRIC_ROOTS=/path/to/workspace` (comma-separated).
- Rollback: `git reset --hard pre-fabric-baseline` **only on the owner's say-so** (drops all
  fabric commits); per-file undo lives in each root's `.ev-undo/` + `.ev-trash/`; journal at
  `.ev-journal.jsonl`; builder jobs in `builder_jobs.db`; shell registry in `.ev-shell-sessions.json`.
- Disable: unset `EV_FABRIC_ROOTS` (empty = deny-all file/shell lanes); fabric is additive —
  no routes, daemons, or launchd units to stop; `git revert` of the fabric commits is clean.
- Branch: `evie-fabric` only (commits f4f1a22…44d9cba + docs); tag `pre-fabric-baseline` intact.
