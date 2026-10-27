# Deploy record (release captain, rocky-sculptor)

## evie-v1.0 (2026-10-27)

- Merge: evie-rc1 → main, no-ff, commit abd7107. Tag evie-v1.0 on the merge.
- Backups in ~/.ev/rc-backups/v1/: Evie.app copy, ev.api/ev.talk plists,
  live Postgres dump ev-livedb.sql (1.48GB, dump-complete markers verified).
- App: rebuilt on merged main (macos/scripts/package.sh) + installed to
  /Applications/Evie.app (install.sh). Identifier com.ev.suit,
  Authority "EV Code Signing" (existing identity). Old app quit by
  installer before replace; backup copy kept.
- Services: deploy script REFUSED as designed — local HEAD abd7107 !=
  origin/main 577c77e ("push first"), and the captain does not push (G7).
  No service was restarted. Live ev.api :8000 + ev.talk :18000 remain on
  main d6cba41 (healthy, untouched all session).
- Post-merge verification (substitute for post-deploy, on merge HEAD):
  isolated :18005 smoke (health/turn-unavailable/presence/panic) green;
  67/67 key suites on main (shield, S21, drift, presence, web_media,
  accept, rc_commands, spelling).
- Rollback: restore ~/.ev/rc-backups/v1/Evie.app over /Applications/Evie.app;
  services need no rollback (never deployed). Full rollback drill recorded
  when the owner deploys.

## Owner steps to finish the deploy

1. Review the merge: `git -C ~/Code/ev show abd7107 --stat` (+ docs/RELEASE_REPORT.md).
2. `git -C ~/Code/ev push origin main && git push origin evie-v1.0`
3. `DEPLOY_ACTOR=owner ~/Code/ev/scripts/deploy_production.sh`
4. Run `ev accept` and work the checklist.
