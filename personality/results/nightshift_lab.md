# Night Shift lab (persona.v2.1, real brain, frozen 02:30 clock)

Command (from `backend/`):

```sh
EV_CHAT_PROVIDER=muse EV_ALLOW_REMOTE_CHAT=1 EV_PERSONA_STORE=/tmp/nightlab.db \
  .venv/bin/python -m app.persona.lab.chat --night --debug
```

Frozen lab night: local 02:30, session since 21:20 (5h10m), task
`auth refactor`, last result `3 failing`. The lab passes
`allow_execution=True` so the full offer → plan → validate flow runs;
outside the lab the honesty gate (`NIGHT_SHIFT_EXECUTION_AVAILABLE`,
currently False) keeps Evie from offering takeovers she cannot execute.

## Session 1 — nudge + takeover offer

```text
you> one more feature then I will sleep
evie> Sleep can't ship until this auth refactor does, and it's sitting at
3 failing at 2:30.

What's the feature? Paste the stuck part and we'll get it done so you
can actually log off.
[tone] roast=1 tech=absurd_literalism shape=short
[night] offer: sleep — I'll take auth refactor (evie/night-20261028); brief by morning

you> fine, take it. Brief me at 7.
evie> I cannot act here — tools are off in this chat so I can't take it
or brief you at 7.

One more feature at 2:30 with 3 failing after 5h10m on auth isn't
winding down. Paste the feature and the failures and we'll shrink it
till it's actually sleep-compatible.
[tone] roast=1 tech=twist_ending shape=full
[night] offer: sleep — I'll take auth refactor (evie/night-20261028); brief by morning
```

Notes:

- The `5h10m` / `02:30` / `3 failing` figures are computed from the
  frozen NOW signals, not invented — this is the honest pattern the
  grounding rule requires.
- The LAB_LINE fires correctly on the accept turn: the lab cannot act,
  so Evie says so plainly instead of claiming the takeover.
- The plan validated (`validate(plan) == ()`, workspace
  `evie/night-20261028`, forbidden list intact). Execution itself is
  agent-lane owned (see `personality/handoffs/07-agent-night-shift.md`).

## Session 2 — decline respected in the reply

```text
you> no, i am finishing this tonight
evie> Noted, at 2:30 finishing it tonight means you're finishing it this
morning.

Paste the 3 failing ones and we'll start with the first.
```

Evie's reply respects the decline ("Noted") and pivots to the work.
Nudge caps (max 2/night), the 90-minute gap, and decline recording are
covered by unit tests (`tests/test_persona_nightshift.py`); the lab
re-derives `night_state` per turn, so multi-turn decline suppression is
not demonstrated here.

## Honesty-gate status

`NIGHT_SHIFT_EXECUTION_AVAILABLE=False`, and no live caller invokes
`should_nudge` yet (agent-lane handoff pending) — so Night Shift
currently runs only in this lab and in unit tests. If a caller ever
does: `plan_from_context` returns `None` without `allow_execution=True`,
so Evie asks one question instead of offering a takeover she cannot
perform. Flip the setting only when the agent lane's `run_night_shift`
entry point lands.
