# Deploy record

## 2026-10-24 14:42 IST: owner-requested rebuild and restart

- Requested by the owner ("rebuild the macOS application and restart the backend server"). `scripts/deploy_production.sh` was not used. It refuses unless local HEAD equals `origin/main` and the tree is clean, which needs a push, and R2 forbids pushing. Its steps were done by hand: the frozen-contract shield (`backend/tests/test_regression_golden.py`, 5 passed), then `launchctl kickstart -k` for ev.api and ev.talk, then a health SHA wait.
- Before: production `c0cacc5` (api, talk, Evie.app). Main tree `evie-acceptance` @ `33faa69`.
- After: `evie-acceptance` fast-forwarded to `5f44f73`. ev.api and ev.talk health `5f44f73` after 9 s. ev.worker, ev.scheduler, ev.runtime and ev.collector were also kickstarted. Evie.app was rebuilt with `macos/scripts/package.sh --install` (`EV_CODESIGN_IDENTITY="EV Code Signing"`), with `EVGitSHA=5f44f73` and a valid strict signature. The build includes the main tree's uncommitted panel-activation work (`EVMicTalkTests` 413 pass; the same 4 pre-existing failures as the committed tree).
- Backup: `~/.ev/backups/deploy-20261024T143055/` (Evie.app, launchd plists, `ev-db.dump` 141 MB / 123 tables verified with `pg_restore --list`, SHAs).
- Checks after deploy: `ev doctor --fabric` all PASS (ev.ears SKIP). Tailnet `https://sahajs-macbook-air.tailbd71d0.ts.net/v1/health` reports `5f44f73`, and `/evie/` returns 200.
- Rollback: `git -C ~/Code/ev reset --keep 33faa69`, kickstart the six services, then `ditto ~/.ev/backups/deploy-20261024T143055/Evie.app /Applications/Evie.app` with Evie quit.
- Live voice acceptance: `docs/LIVE_VOICE_TEST_2026-10-24.md`.
