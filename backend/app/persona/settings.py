"""Persona settings: module constants until the shared-config handoff lands (R5).

These are read-only defaults owned by CLI 6. The formal EV_* fields arrive
via handoff to the shared-config owners; nothing here reads env directly.
"""

from __future__ import annotations

PERSONA_VERSION = "persona.v2.1"

# Master switch. Do not flip; cutover happens only after the §13 override.
PERSONA_ENABLED = False

# Per-turn profanity default (spec §2 CANDOR). Forced off for company,
# on_behalf, and ROAST=0 turns regardless of this value.
profanity = False

# Night Shift (spec §8).
night_start = "00:30"
night_end = "06:00"
nudge_gap_min = 90
max_nudges = 2
wake_time: str | None = None
brief_mode = "first_message_after_06:00"
night_scope = "continue_current_task"
checkpoint_every_min = 30

# Learning (spec §6).
joke_rate_bounds = (0.15, 0.7)

# Night Shift honesty gate: while False, Evie may tease toward sleep but
# must NEVER offer to take over work (that capability doesn't exist yet).
# The full offer → plan → brief flow runs in the lab only.
NIGHT_SHIFT_EXECUTION_AVAILABLE = False
