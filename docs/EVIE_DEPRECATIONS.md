# Evie Deprecations — Canonical Modules

| Area | Canonical module | Deprecated / do not use |
|------|------------------|--------------------------|
| Ears (mic owner) | ears mic owner (single mic claimant) | ad-hoc mic opens, parallel listeners, background recorders outside ears |
| Shell + PWA | EvieShell + PWA | legacy web shells, duplicate launchers, standalone debug pages |
| Kernel loop | kernel loop (sole runtime loop) | secondary tickers, ad-hoc schedulers, competing run loops |

Rule: route new mic/audio work through ears, new UI surface through EvieShell+PWA,
new periodic work through the kernel loop. Remove callers of deprecated paths on touch.

## Fabric lanes (2026-10-17, branch evie-fabric)
| Area | Canonical module | Deprecated / do not use |
|------|------------------|--------------------------|
| Files | `backend/app/fabric/files.py` | direct `open()/shutil` writes outside policy roots, silent overwrites |
| Shell | `backend/app/fabric/shell.py` (+ `shell_sessions.py`) | ad-hoc `subprocess` calls bypassing policy/grant checks |
| SQL scratch lane | `backend/app/fabric/sql_lane.py` | raw sqlite writes outside scratch lane, multi-statement exec |
| Screen refs | `backend/app/fabric/screen.py` + `screen_ref.py` | raw pixel dumps without redaction refs |
| Bridge/iPhone | `backend/app/fabric/bridge.py` | unverified device claims, relay without receipts |
| Comms draft/send | `backend/app/fabric/comms.py` | sending without draft→review→send gate |
| Plan/apply builder | `backend/app/fabric/builder.py` | apply without plan diff + confirm |
| Turn orchestration | `backend/app/fabric/run_turn.py` + `turn.py` | calling lanes directly from routes, skipping receipts |
| Policy/grants/gaps | `backend/app/fabric/policy.py`, `grants.py`, `gaps.py` | hard-coded limits, implicit grants, silent gaps |
| Terminal layouts | `backend/app/fabric/terminal.py` (TmuxLayout) | Ghostty CLI scripting on macOS (only `+actions` exist; no pane control — probed 2026-10-17), ad-hoc tmux strings without plan/confirm/readback |
