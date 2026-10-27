# DEPENDENCY NOTE 03 — to Agent 16, filter (from CLI 6 low-telescopium)

## One ordered persona_guard stage inside enforce_persona

File: `backend/app/filter/output_filter.py`, function `enforce_persona`.

Request: after the existing guardrails and before the length trim, insert:

```python
from app.persona.guard import persona_guard  # top of file
...
text, persona_flags = persona_guard(text, ctx)  # ctx: PersonaContext or None
flags.extend(persona_flags_as_filter_flags(persona_flags))
```

`persona_guard(text, ctx)` returns `(text, flags)` where flags are plain
strings (`filler_stripped`, `humour_in_roast0`, `repeated_line`, …); map
each to your existing `FilterFlag` shape so it lands in the ledger like
every other flag. The guard's only mutations are filler-strip and emoji
cap; it never alters confirmation or receipt-derived text, and it is
idempotent. Gate the call on `PERSONA_ENABLED` (import from
`app.persona.settings`) so flag-off behavior is byte-identical.

Acceptance: existing filter tests pass unchanged with the flag off; with
the flag on, persona flags appear in the ledger on the flagged turns.
