# DEPENDENCY NOTE 05 — to shared-config owners (from CLI 6 low-telescopium)

## Append the CLI-6 settings block (config.py, .env.example, ENVIRONMENT.md)

Per R5 (every env var in Settings + `.env.example` + `docs/ENVIRONMENT.md`)
and the shared append-only rule, please append this block (mark it
`CLI 6 PERSONA`); CLI 6 will not edit these files itself.

Settings (`app/config.py`, `EV_` prefix is automatic):

```python
# --- CLI 6 PERSONA (persona.v2; formal fields, flag default off) ---
persona_enabled: bool = False
persona_profanity: bool = False
persona_night_start: str = "00:30"
persona_nudge_gap_min: int = 90
persona_max_nudges: int = 2
persona_wake_time: str | None = None
persona_brief_mode: str = "first_message_after_06:00"
persona_night_scope: str = "continue_current_task"
persona_joke_rate_min: float = 0.15
persona_joke_rate_max: float = 0.7
```

Mirror each as `EV_PERSONA_*` in `.env.example` with the same defaults, and
document them in `docs/ENVIRONMENT.md`. Until this lands, `app/persona/`
reads its module constants in `app/persona/settings.py` (same values).

Acceptance: `make baseline-check` passes; persona settings importable from
`app.config.settings` with flag default False.
