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
