# Gauntlet

The headline number is the **real-brain behavior pass rate**. Scripted mode is
the **plumbing pass rate**: the lanes do the right thing when a planner has
already chosen the tool call. A plumbing score cannot fail a model, so it is
not evidence that Evie does what a person asked.

`backend/gauntlet/brain.py` `plan()` receives only the utterance, the world,
and the tool schemas. It does not receive the case id, class, or oracle.
Cases a blind planner cannot solve are marked `real_brain_only` and are not
counted as plumbing passes.

```sh
cd backend
uv run ev gauntlet --split dev --limit 60
make gauntlet-smoke
make gauntlet
make gauntlet-mutants
```

`make gauntlet-mutants` applies at least 25 faults, one at a time, on a temp
copy of the code. A fault is killed when the affected cases fail. The required
kill rate is 90%.

Real-brain runs:

```sh
ev gauntlet --real-brain --budget 10
```

`GAUNTLET_BUDGET_USD` (default 10) is separate from the app's monthly cap. The
harness loads credentials the same way the app does (repo `.env`, then
`~/.ev/secrets/production.env`). It runs 20 real turns, prices them from the
provider usage receipt, prints the estimate, then continues stage 1 (every
model-eligible DEV case once), stage 2 (failures x3), and stage 3 (a
stratified 25% x3). Chaos and policy-only cases stay on the scripted brain.
A stage that would pass the budget stops, and the report records the coverage.
