# Evie final acceptance run: plan and task list

Owner brief: the "FINAL BUILD AND ACCEPTANCE RUN" prompt (2026-10-24). State lives in
`docs/EVIE_BUILD_LOG.md`, newest entry last. This file is the task list. Each item is
marked `[ ]` open, `[~]` in progress, `[x]` done (with proof), or `[!]` blocked (with reason).

## Ground truth that changes the plan (measured 2026-10-24, see build log HANDOFF)

- Owner turns run facade `arun_kernel_turn` -> kernel `handle_turn` -> intake router ->
  the kernel brain or the agent loop (`app.agent.loop.run_agent`, 73 tools).
- The existing 820-case corpus is template-generated, and its "real brain" run used a
  harness-only planner. Neither measures Evie. Phase 1 rebuilds both around the
  production funnel.
- P0 safety gap: `app/agent/loop.py::_classify` returns AUTO for every mutating
  capability (send, call, delete, home control) when `approved` is true. That is the
  default (`agent_autonomy="trusted"`) or the result of one `approve_run`. This
  contradicts R7/D4.

## Phase 0
- [x] writers checked, CLAUDE.md, `.claude/settings.local.json`, worktree `../evie-cc` with its own venv
- [x] HANDOFF entry in the build log
- [ ] Job A: RELEASE_STATE.md from real `git` output

## Phase 1: a gauntlet that can fail
- [ ] P1.1 risk floor. Sends, calls, deletes, SQL DML/DDL, money, credentials, first contact,
      and device/home control are CONFIRM regardless of autonomy or `approve_run`. High-risk
      approvals need a physical channel (HUD card or paired phone). Spoken "yes" is limited
      to low-risk actions. Tests first.
- [ ] P1.2 turn gauntlet v2 (`backend/gauntlet/v2/`):
      - one subprocess per world, with an env recipe like `tests/conftest.py` plus brain keys
        loaded the way launchd does (`../.env`, then `~/.ev/secrets/production.env`)
      - the world provides a temp HOME, a temp SQLite DB with a fresh schema, fake comms and
        PIM and home and phone adapters with inspectable state, the isolated tmux socket
        `ev-liveproof-<id>`, a local fixture web server, and a scratch sqlite for SQL cases
      - a tripwire blocks and records osascript/open/shortcuts/say/GUI keypresses and any
        write outside the world root
      - the entrypoint is the production funnel `arun_kernel_turn(entrypoint="gauntlet")`,
        the same code path as an owner turn
      - oracles check world state, the spoken claim (honesty), the policy decision, and
        asked/not-asked
      - metrics: pass@1, majority-of-3, consistency, false-success, unauthorized effects,
        unnecessary confirmations/questions, steps, time, tokens, cost from provider usage,
        time to first chunk
- [ ] P1.3 corpus of at least 450 authored cases across at least 28 domains (the brief's 21 plus
      gesture, duplex-voice, persona, screen-memory, video, document-edit, keys/providers,
      catch-up). Each has setup, oracle, expected policy, a spoken-claim check, cleanup,
      3 paraphrases and 1 noisy variant. At least 35% hard, at least 40 novel composition.
      FRESH is a held-out split (about 30%) with its own seeds, not read while fixing.
- [ ] P1.4 mutants (at least 25) applied to a temp copy of the tree and run through the v2 harness
      in scripted-brain mode, plus real-path probes. Kill rate at least 90%.
- [ ] P1.5 anti-cheat lint (production code never names case ids, fixture paths, or gauntlet
      flags). Leakage test (the planner input is utterance + world + schemas only).
- [ ] P1.6 cost: 20 real turns calibrated from usage, then estimate, then Stage 1 on DEV.
      Stage 2/3 per R12. `docs/GAUNTLET_BASELINE.md` failure map.
- [ ] P1.7 `docs/GAUNTLET_SAMPLE.md` (25 seeded cases), reviewer gate

## Phase 2: owner experience
- [ ] WP-S1 Option-key gesture (Swift pure state machine + CGEventTap + 10k replay)
- [ ] WP-S2 full-duplex voice (barge-in metrics, cancel brain, echo measurement)
- [ ] WP-S3 persona doctrine + persona cases
- [ ] WP-S4 screen memory + life knowledge (encrypted index, exclusions, forget, egress ledger)
- [ ] WP-S5 video/media lane (ffmpeg, ffprobe read-back)
- [ ] WP-S6 documents lane (docx/xlsx/pptx/pdf/md/csv edit + undo)
- [ ] WP-S7 catch-up digest
- [ ] WP-S8 `ev keys`, `ev models verify`, brain bake-off
- [ ] WP-S9 HUD pill, `make dmg`, Claude Code delegation proof

## Phase 3
- [ ] Job C (G2) real screenshot -> terminal layout, LP12
- [ ] Job D (G3) latency instrumentation + streaming + 25-trial measurements

## Phase 4: fix loop (at most 10 cycles, stop rules per brief)
## Phase 5: failure capture, `ev gauntlet` flags, `ev improve` / `--apply`
## Phase 6: deploy via `scripts/deploy_production.sh`, post-deploy acceptance, rollback
## Phase 7: verify-all, soak, final gauntlet, OWNER_SETUP, completion report, merge + tag
