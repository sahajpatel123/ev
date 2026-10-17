# Evie Fabric — Final Report (2026-10-17, branch `evie-fabric`)

## 1. Executive summary (<=12 lines)
Evie Fabric adds policy-gated local lanes (files, shell, SQL scratch, screenshots,
bridge/iPhone, comms, builder) behind one turn orchestrator with receipts.
22/22 e2e tests pass in 12.04s on backend/.venv (macOS 27.2 arm64).
Settings drift fixed: 4 `EV_FABRIC_*` vars added to Settings + .env.example + ENVIRONMENT.md.
Contract unchanged: `backend/eval/contract_v1.json` still `ev.contract.v1`, 550 locked paths, no routes added.
Chat first-token honest measurement: UNVERIFIED (no provider keys; echo double-baseline not runnable here, never faked).
iPhone relay is PARTIAL (turn-level test only; needs a real paired device).
Before/after: lanes were ad-hoc/direct; now every lane goes through policy+grants+gaps with receipts.

## 2. Capability table (proof test-ids)

| Capability | Canonical module | Proof test-id | Status |
|---|---|---|---|
| File read/write/list + policy deny | `backend/app/fabric/files.py`, `policy.py` | test_s01_file_read, test_s02_file_write, test_s03_file_list, test_s04_policy_denies | WORKING |
| Shell run + deny | `shell.py`, `shell_sessions.py` | test_s05_shell_run, test_s06_shell_deny | WORKING |
| SQL scratch select | `sql_lane.py` | test_s07_sql_select | WORKING |
| Screenshot + redact | `screen.py`, `screen_ref.py` | test_s08_screenshot, test_s09_screenshot_redact | WORKING |
| Bridge roundtrip + receipt | `bridge.py` | test_s10_bridge_roundtrip, test_s16_bridge_receipt | WORKING |
| Comms draft + send gate | `comms.py` | test_s11_comms_draft, test_s13_comms_send | WORKING |
| Builder plan + apply | `builder.py` | test_s14_builder_plan, test_s15_builder_apply | WORKING |
| iPhone turn relay | `bridge.py` | test_s17_iphone_turn | PARTIAL (no real device) |
| Latency budget + receipt | `run_turn.py`, `policy.py` | test_s18_latency, test_s22_latency_receipt | WORKING |
| Grants | `grants.py` | test_s19_grant | WORKING |
| Gap surfacing | `gaps.py` | test_s20_gap | WORKING |
| Contract version + receipt | `policy.py` | test_s21_contract_version, test_s21_contract_receipt | WORKING |
| Turn orchestration | `run_turn.py`, `turn.py` | (via s18/s22) | WORKING |
| Chat first-token TTFT | — | — | UNVERIFIED (no provider keys) |

## 3. S1–S22 results (22 passed + timings)
Full run: `backend/.venv/bin/python -m pytest backend/tests/e2e_fabric -q` → **22 passed in 12.04s**
(wall 14.41s; per-test body is ms-scale, ~0.42–0.63s/test is pytest setup overhead).

| # | Test-id | File | Result |
|---|---|---|---|
| S01 | test_s01_file_read | test_s01_s04_files.py | pass |
| S02 | test_s02_file_write | test_s01_s04_files.py | pass |
| S03 | test_s03_file_list | test_s01_s04_files.py | pass |
| S04 | test_s04_policy_denies | test_s01_s04_files.py | pass |
| S05 | test_s05_shell_run | test_s05_s06_shell.py | pass |
| S06 | test_s06_shell_deny | test_s05_s06_shell.py | pass |
| S07 | test_s07_sql_select | test_s07_sql.py | pass |
| S08 | test_s08_screenshot | test_s08_s09_screenshot.py | pass |
| S09 | test_s09_screenshot_redact | test_s08_s09_screenshot.py | pass |
| S10 | test_s10_bridge_roundtrip | test_s10_s16_bridge.py | pass |
| S11 | test_s11_comms_draft | test_s11_s13_comms.py | pass |
| S12/S16 | test_s16_bridge_receipt | test_s10_s16_bridge.py | pass |
| S13 | test_s13_comms_send | test_s11_s13_comms.py | pass |
| S14 | test_s14_builder_plan | test_s14_s15_builder.py | pass |
| S15 | test_s15_builder_apply | test_s14_s15_builder.py | pass |
| S17 | test_s17_iphone_turn | test_s17_iphone.py | pass (fake lane; real-device = OWNER-ACTION) |
| S18 | test_s18_latency | test_s18_s22_latency.py | pass |
| S19 | test_s19_grant | test_s19_grants.py | pass |
| S20 | test_s20_gap | test_s20_gap.py | pass |
| S21a | test_s21_contract_version | test_s21_contract.py | pass |
| S21b/S22 | test_s21_contract_receipt, test_s22_latency_receipt | test_s21_contract.py, test_s18_s22_latency.py | pass |

Note: S12 has no dedicated test id; bridge-receipt coverage is via S16.

## 4. Before/after metrics
- Before: file/shell/sql/screen/comms ops were direct calls with no uniform policy, grant, or receipt envelope.
- After: all lanes route through `policy.py` (roots, budgets, deny) + `grants.py` (confirm gates) + `gaps.py` (explicit gap surfacing); every turn returns a receipt (`_receipt`, `verified` key asserted in S22/S21b).
- Latency: S18 asserts `run_turn("quick op")` latency_ms ≤ `latency_budget_ms()` — passes locally.
- Chat first-token: no honest baseline exists in this env — recorded UNVERIFIED, no numbers invented.

## 5. OWNER_ACTIONS (ordered)
1. Set `EV_FABRIC_ROOTS` to allowlisted workspace roots (empty denies all file/shell lanes).
2. Review `EV_FABRIC_BULK_CONFIRM_N=25` / `EV_FABRIC_TRASH_DAYS=30` / `EV_FABRIC_JOURNAL=true` defaults.
3. Chat TTFT: with provider keys configured, run the echo double-baseline (two identical chat prompts, API vs CLI, record first-token ms) and paste results here.
4. iPhone relay: pair a real device and confirm an end-to-end relay receipt (S17 currently fake-lane only).
(Full list in `docs/OWNER_ACTIONS.md`; TCC checklist unchanged.)

## 6. Deprecations + canonical modules
See `docs/EVIE_DEPRECATIONS.md` § "Fabric lanes": files→`fabric/files.py`, shell→`fabric/shell.py`+`shell_sessions.py`,
sql→`fabric/sql_lane.py`, screen→`fabric/screen.py`+`screen_ref.py`, bridge→`fabric/bridge.py`,
comms→`fabric/comms.py`, builder→`fabric/builder.py`, turn→`fabric/run_turn.py`+`turn.py`,
policy/grants/gaps→respective modules. Do not add ad-hoc subprocess/sqlite/pixel-dump paths.

## 7. Gaps / risks / assumptions
- Gaps: no S12-dedicated test id (covered by S16); no provider-keyed chat path in fabric scope; trash purge retention honors `fabric_trash_days` by config only.
- Risks: `EV_FABRIC_ROOTS=""` default denies everything — safe but owner must configure or all lanes refuse; fake fixtures (FakeShell/FakeScreen) mean lane tests prove orchestration, not real subprocess/pixel behavior.
- Assumptions: sqlite scratch lane only; single-owner local device; contract file untouched (550 paths verified).

## 8. Run / rollback / disable instructions
- Run: `backend/.venv/bin/python -m pytest backend/tests/e2e_fabric -q` (22 passed expected).
- Configure: export `EV_FABRIC_ROOTS=/path/to/workspace` (comma-separated); optional `EV_FABRIC_TRASH_DAYS`, `EV_FABRIC_BULK_CONFIRM_N`, `EV_FABRIC_JOURNAL`.
- Rollback: `git revert` the fabric commits on `evie-fabric` (small commits, see log); fabric is additive — no existing routes touched.
- Disable: unset `EV_FABRIC_ROOTS` (empty = deny-all) and/or set `EV_FABRIC_JOURNAL=false`; no daemon or launchd unit to stop.
- Branch: `evie-fabric` only; no other branches touched.
