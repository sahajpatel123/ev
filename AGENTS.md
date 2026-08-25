
# PRODUCTION DEPLOYMENT LAW (P0 CLOSURE, 2026-08-25)

Production (`ev.api` on :8000) has ONE deploy authority:
`scripts/deploy_production.sh` (flock-guarded, verifies clean tree +
origin parity, restarts once, waits for health to report the pinned SHA).

Development agents MUST NOT independently:
- `launchctl kickstart` ev.api or any production service,
- run production migrations against the live database,
- invoke destructive endpoints (restore/wipe/reseed — these are also
  gated server-side by EV_MAINTENANCE_MODE + confirmation tokens),
- run test suites with EV_TEST_USE_LIVE_DB=1 when EV_ENV=production
  (conftest fails closed).

Prepare patches + isolated tests freely; coordinate deploys through
Project Head / the wrapper only.
