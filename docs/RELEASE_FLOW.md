# Phone Program Release Flow

The iPhone program (`ev-iphone-cc`) ships as a TailSCALE-hosted PWA at
`/evie/`. There is no IPA; the release unit is a **build number +
coherent release manifest**.

## The one rule

`backend/clients/pwa/release.json` is the contract between the served
HTML and the running API. A device refuses a mixed deploy. Every asset
pin (`?v=BUILD`) and every server-side pin site MUST move together.

## Release steps

1. **Bump** (never hand-sed — cycles 51-85 proved why):

   ```bash
   scripts/bump_build.sh            # auto: date-N, next patch
   scripts/bump_build.sh 2026.09.08.40   # explicit
   ```

   Pins moved: `app/device_gateway/__init__.py` (PWA_BUILD),
   `app/config.py` (pwa_build), `clients/pwa/{index.html,app.js,sw.js}`.

2. **Manifest**: the script regenerates `clients/pwa/release.json` via
   `uv run python -m app.scripts.gen_release_manifest`.

3. **Gates** (must be green before commit):

   ```bash
   make phone-voice-e2e      # E2E canary + capability suite (cycle 81)
   make iphone-parity-check  # JS/bash syntax + release gates
   ```

4. **Contract**: if an endpoint was added/renamed/removed:

   ```bash
   cd backend && uv run python -m app.scripts.update_contract
   ```

   The eval gate fails on any locked route disappearing or any live
   route unlocked (`eval/contract_v1.json`, 453 paths).

5. **Commit** `iphone(release): build <BUILD>` and push to `main`.

## Production

`ev.api` on :8000 is deployed ONLY by `scripts/deploy_production.sh`
in the parent repo (flock + clean tree + origin parity + pinned-SHA
health check). Dev agents never `launchctl kickstart` or migrate the
live DB (AGENTS.md P0 law).

## History

- Cycles 1-50: built the program; bumps were manual seds.
- Cycle 86: this script + doc. Bumps are one command and contract-checked.
