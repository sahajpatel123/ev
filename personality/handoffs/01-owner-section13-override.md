# DEPENDENCY NOTE 01 — to the project owner (from CLI 6 low-telescopium)

## Record the FLEET_LAW §13 override, scoped to the character layer

FLEET_LAW rule 13 freezes `app/ev/personality.py` (`SPEECH_STYLE_INSTRUCTIONS`
+ `DEFAULT_PROFILE`) as owner-only. CLI 6's persona.v2 work needs a recorded
override to proceed past Phase 1 (build-only) into cutover.

Requested scope (character layer only):

- `backend/app/ev/personality.py` — the flag-gated delegation in
  `personality/proposals/0001-persona-v2-cutover.md` part A (flag off =
  byte-identical).
- `backend/tests/test_personality_lock.py`,
  `backend/tests/test_dynamic_personality.py`,
  `backend/tests/test_personality_style.py` — deliberate-update branches for
  the v2 strings (flag-off branches unchanged).
- No other fleet file. No behavior change while `PERSONA_ENABLED` is False.

Interfaces this unlocks for every lane: `build_persona_block(ctx)` compiles
the prompt block; `persona_guard(text, ctx)` checks the finished reply.
Both live in `backend/app/persona/` (CLI 6 owned), deterministic, offline,
under 5 ms.

Please reply with the recorded scope (or a narrower one); until then CLI 6
writes nothing outside its own paths.
