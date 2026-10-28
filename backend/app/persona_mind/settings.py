"""Mind settings: module constants. The formal EV_* fields are a future
handoff; nothing here reads env directly (paths.py owns the DB override)."""

from __future__ import annotations

MIND_VERSION = "mind.v1"
TEMPERAMENT_VERSION = "temperament.v1"

# Master switch. Do not flip; CLI 6 wires the calls after integration review.
MIND_ENABLED = False

# Independent flags, each with a one-line rollback (M6 sets them on evidence).
PACE_ENABLED = False
DREAM_ENABLED = False
DREAM_MORNING_NOTE = False

# Affect model.
AFFECT_DIMS = (
    "playfulness",
    "warmth",
    "concern",
    "energy",
    "impatience",
    "satisfaction",
    "curiosity",
)
HALF_LIFE_S = 6 * 3600
MAX_AFFECT_DELTA = 0.25

# Opinions.
SEED_CONFIDENCE = 0.6
MAX_CONF_MOVE = 0.3

# Intentions / threads.
MAX_INTENTIONS = 3
INTENTION_TTL_S = 72 * 3600
THREAD_TTL_S = 7 * 86400
MAX_MOOD_WORDS = 3

# Slow loop triggers.
REFLECT_BATCH = 3
IDLE_REFLECT_S = 2 * 3600
# Brain calls never compete with a live reply: non-salient batches wait for
# this much owner quiet. Salient turns reflect immediately.
QUIET_S = 20.0
AUDIT_EVERY = 50
WORKER_POLL_S = 5.0

# Render budgets (tokens, repo estimator len//4).
TEXT_BUDGET = 120
VOICE_BUDGET = 60
