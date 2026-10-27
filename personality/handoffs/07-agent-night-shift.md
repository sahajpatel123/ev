# DEPENDENCY NOTE 07 — to the agent lane (from CLI 6 low-telescopium)

## A run_night_shift(plan) entry point (`app/agent/loop.py` owner)

CLI 6 owns Night Shift conversation + plan (`app/persona/nightshift.py`:
`NightShiftPlan`, `validate`, `MorningBrief`). Execution is yours:

Request: add `run_night_shift(plan: NightShiftPlan)` with these behaviors:

- Work only in `plan.workspace` (`evie/night-YYYYMMDD`).
- Check `plan.forbidden` before EVERY tool call (hard deny, no override).
- Push any confirmation-class action to `plan.confirmation_queue` and skip
  it; the physical-confirm law still applies.
- Checkpoint every `plan.checkpoint_every_min`.
- Stop when any of `plan.stop_conditions` is met (goal met, time budget,
  wake minus 30 min, 3 consecutive failures on the same step).
- Emit a `MorningBrief` (same module): every `done[]` item carries a
  receipt reference; `waiting_for_you[]` holds the queued confirmations.

The schema + validator are importable from `app.persona.nightshift`;
reject plans where `validate(plan)` is non-empty before starting.

Acceptance: a validated plan runs to a `MorningBrief` with receipt refs;
a plan naming a forbidden capability is refused before any tool call.
