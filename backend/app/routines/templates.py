"""Reusable routine template library (plan 19.4).

Templates are deterministic, versioned definitions that instantiate real
``Routine`` rows with a ``metadata.template_slug`` provenance trail.  They are
curated in code so they are always available, auditable, and testable; a
personalized routine is created by instantiating + overriding fields, never by
editing the template itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RoutineTemplate:
    slug: str
    name: str
    description: str
    kind: str
    action_type: str
    schedule: str | None = None
    timezone: str = "UTC"
    quiet_hours_skip: bool = True
    backfill_max: int = 1
    cooldown_seconds: int = 0
    trigger: dict = field(default_factory=dict)
    action_title: str | None = None
    action_payload: dict = field(default_factory=dict)
    requires_approval: bool = False
    undoable: bool = False
    tags: tuple[str, ...] = ()
    personalization_hints: str | None = None


ROUTINE_TEMPLATES: tuple[RoutineTemplate, ...] = (
    RoutineTemplate(
        slug="morning-brief",
        name="Morning brief",
        description=(
            "Weekday morning card with the day's top context: events, decisions, "
            "and anything EV is watching."
        ),
        kind="scheduled",
        schedule="0 8 * * 1-5",
        action_type="hud_card",
        action_title="Morning brief",
        action_payload={"title": "Morning brief", "sections": ["focus", "decisions", "alerts"]},
        tags=("daily", "briefing"),
        personalization_hints="Use focus, pending decisions, and overnight alerts as sections.",
    ),
    RoutineTemplate(
        slug="weekly-review",
        name="Weekly review",
        description="Friday evening review of the week: decisions, follow-ups, and lessons learned.",
        kind="scheduled",
        schedule="0 17 * * 5",
        action_type="hud_card",
        action_title="Weekly review",
        action_payload={"title": "Weekly review", "sections": ["decisions", "outcomes", "lessons"]},
        tags=("weekly", "review"),
        personalization_hints="Include decision outcomes and lessons from the past 7 days.",
    ),
    RoutineTemplate(
        slug="backup-reminder",
        name="Backup reminder",
        description="Weekly nudge to verify backups are current and healthy.",
        kind="scheduled",
        schedule="0 21 * * 0",
        action_type="hud_card",
        action_title="Backup check",
        action_payload={"title": "Backup check", "sections": ["storage", "backups"]},
        tags=("weekly", "maintenance", "backup"),
        personalization_hints="Check object-store and local backup status, then prompt for fixes.",
    ),
    RoutineTemplate(
        slug="decision-followup",
        name="Decision follow-up",
        description="Weekly review of open decisions that are still waiting on an expected outcome.",
        kind="scheduled",
        schedule="0 9 * * 1",
        action_type="send_message",
        action_title="Decision follow-up",
        action_payload={"channel": "default", "subject": "Open decisions"},
        requires_approval=True,
        tags=("weekly", "decisions", "communication"),
        personalization_hints="List decisions with pending outcomes; require approval before sending.",
    ),
    RoutineTemplate(
        slug="deadline-brief",
        name="Deadline brief",
        description="When a deadline is 24h out, prepare a compact brief so nothing sneaks up.",
        kind="trigger",
        trigger={
            "event_type": "deadline",
            "conditions": [{"path": "hours_until", "op": "lte", "value": 24}],
        },
        action_type="hud_card",
        action_title="Deadline brief",
        action_payload={"title": "Deadline brief", "sections": ["requirements", "risks", "next_steps"]},
        tags=("trigger", "deadlines", "briefing"),
        personalization_hints="Pull the deadline's linked goals and blocking risks from memory.",
    ),
    RoutineTemplate(
        slug="low-readiness-reschedule",
        name="Low-readiness reschedule",
        description="When health readiness drops, suggest rescheduling heavy work.",
        kind="trigger",
        trigger={
            "channel_kind": "health",
            "event_type": "readiness",
            "conditions": [{"path": "readiness", "op": "lt", "value": 40}],
        },
        action_type="hud_card",
        action_title="Low readiness",
        action_payload={"title": "Low readiness", "sections": ["reschedule", "recovery"]},
        undoable=True,
        tags=("trigger", "health", "reschedule"),
        personalization_hints="Suggest moving heavy focus blocks to a later slot and track the swap.",
    ),
    RoutineTemplate(
        slug="deployment-checklist",
        name="Deployment checklist",
        description="Pre-deployment card with the standard verification checklist.",
        kind="scheduled",
        schedule="0 9 * * 1-5",
        action_type="hud_card",
        action_title="Deployment checklist",
        action_payload={
            "title": "Deployment checklist",
            "items": ["tests green", "migrations applied", "backup taken", "release notes drafted"],
        },
        tags=("daily", "deployment", "checklist"),
        personalization_hints="Fill each checklist item from current CI/build state.",
    ),
)


def list_templates() -> list[RoutineTemplate]:
    return list(ROUTINE_TEMPLATES)


def get_template(slug: str) -> RoutineTemplate:
    for template in ROUTINE_TEMPLATES:
        if template.slug == slug:
            return template
    raise KeyError(f"Routine template {slug!r} not found")
