# OWNER ACTIONS NOW (growth engine, rocky-sculptor)

## Step 0 status: DEPLOY ALREADY DONE (owner-ordered last turn)

Live ev.api + ev.talk report 6f0eecc (release code); local main ==
origin/main == 06a1795; tag evie-v1.0 on abd7107 is on the remote.
The skew warning is resolved by the restart. No live-turn restriction remains.

## The exact commands (verified from scripts/deploy_production.sh + remote config)

Remote checked by the script: `origin` = https://github.com/sahajpatel123/ev.git
(the script runs `git -C /Users/sahajpatel/code/ev fetch origin main` and
requires local HEAD == origin/main, then shield + kickstart + health wait).

```sh
cd ~/Code/ev
git push origin main && git push origin evie-v1.0   # already done
DEPLOY_ACTOR=owner ./scripts/deploy_production.sh   # already done (ran as rocky-sculptor under owner order)
```

Why these existed: live processes ran d6cba41 while backend files changed
under them at merge, so no live turns until restart. Done: both services
now report 6f0eecc.

## What still needs the owner (nothing here can be done by agents)

1. `launchctl kickstart -k gui/$UID/ev.worker ev.scheduler ev.runtime ev.collector`
   (one line each if preferred) — the wrapper restarts api+talk only; these
   four still run pre-merge code under the new tree.
2. Launch Evie.app (bridge health + all app-visible accept items).
3. Run `ev accept` until 10/10 (2/10 auto items already PASS).
4. Raise the OpenRouter key limit (~$0.02 left) AND create the dedicated eval
   key (GE2, ~$20/month suggested) + cap the live key (~$10/month).
5. Weekly (once the engine notifies): `ev review` (a few minutes).
