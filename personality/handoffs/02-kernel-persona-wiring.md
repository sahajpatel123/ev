# DEPENDENCY NOTE 02 — to the kernel/brain owner (from CLI 6 low-telescopium)

## Wire persona into the kernel path (`app/cognitive/`)

The kernel path assembles its system prompt without the persona contract
(`compile_context`, `context.py:81`) and emits `spoken` with no output
filter. Requested changes (all flag-gated, fallback = today's behavior):

1. In `compile_context`, accept the persona block and append it before the
   TOOL ROUTING suffix:
   `build_persona_block(ctx).text` from `app.persona.compile`.
2. Call `persona_guard(spoken, ctx)` from `app.persona.guard` on `spoken`
   before emitting (returns `(text, flags)`; flags join your existing
   telemetry).
3. Pass the NOW-line signals you already hold into the context:
   session start, last receipts, `user_state` task, first calendar item if
   available. No new detection needed — see `app/persona/context.py`
   (`PersonaContext`) and `app/persona/awareness.py` (`signals_from_context`).

Exact diff for the competing identity at `context.py:98` is in
`personality/proposals/0001-persona-v2-cutover.md` part C (you execute).
Acceptance: with the flag off, kernel prompts are byte-identical; with the
flag on, the v2 core + TURN TONE line appear before TOOL ROUTING.
