# DEPENDENCY NOTE 09 — to the thread-state owner (from CLI 6 low-telescopium)

## Persist persona per-thread + per-owner state

Please persist the following (names fixed; helpers in parentheses):

Per thread:

- `serious_latch_turns_left: int` — set to `LATCH_TURNS` (3, from
  `app.persona.tone`) when a `ToneDecision` lands with a `rule2:`/`rule3:`
  reason; decay each turn with `update_latch(prev, decision)`
  (`app.persona.tone`), which also handles `latch_cleared` reasons.
- `recent_joke_flags: bool[3]` — append `joke_flag(reply, decision)`
  (`app.persona.tone`) per turn, keep the last three.
- `last_joke_meta: {technique, topic}` — the technique id from the
  decision plus `topic_of(owner_msg)` (`app.persona.reactions`); `None`
  when no joke was told.
- `night_state: {date, nudges, last_nudge_ts, declined, last_signals}` —
  advance with `nudge_recorded(state, now, signals, declined=…)`
  (`app.persona.nightshift`).

Per owner:

- Learning weights — persist via the `CallbackStore` interface
  (`app.persona.callbacks`: `load_weights`/`save_weights`); the
  `EventMetadata` adapters accept your read/write callables when ready.

Acceptance: a new turn can reconstruct `PersonaContext(serious_latch_…,
recent_joke_flags, last_joke_meta, night_state)` plus weights from your
store, with no persona-owned tables.
