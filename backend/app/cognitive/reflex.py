"""Deterministic reflexes — not cognition. Unambiguous only."""

from __future__ import annotations

import re
from dataclasses import dataclass

_STOP = re.compile(
    r"^\s*(?:evie[, ]*)?(?:stop(?: it)?|that's enough|that is enough|quiet|"
    r"shut up|cancel(?: that| this)?|never mind|nevermind)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_PARK = re.compile(
    r"^\s*(?:evie[, ]*)?(?:park(?: it| this| that)?|pause(?: it| this| that)?|"
    r"hold(?: that| this)?|put that (?:on hold|aside))\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_RESUME = re.compile(
    r"^\s*(?:evie[, ]*)?(?:resume(?: it| this| that)?|continue(?: it| this| that)?|"
    r"keep going|pick that back up)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_STATUS = re.compile(
    r"^\s*(?:evie[, ]*)?(?:status|what are you doing|where were we|"
    r"what(?:'s| is) (?:going on|happening)|current (?:status|task))\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_APPROVE = re.compile(
    r"^\s*(?:evie[, ]*)?(?:approve|accept|deny|reject)\s+(?P<token>[a-z0-9-]{4,})\s*[.!?]*\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Reflex:
    kind: str
    spoken: str
    token: str = ""
    cancel_speech: bool = False
    cancel_work: bool = False
    park: bool = False
    resume: bool = False
    status: bool = False


def match_reflex(transcript: str, *, has_active_goal: bool, status_line: str = "") -> Reflex | None:
    raw = (transcript or "").strip()
    if not raw:
        return None
    if _STOP.match(raw):
        if not has_active_goal:
            return Reflex(
                kind="stop_speech",
                spoken="Okay.",
                cancel_speech=True,
            )
        return Reflex(
            kind="cancel_goal",
            spoken="Stopped. I won't continue that.",
            cancel_speech=True,
            cancel_work=True,
        )
    if _PARK.match(raw) and has_active_goal:
        return Reflex(
            kind="park_goal",
            spoken="Parked. Say resume when you want it back.",
            cancel_speech=True,
            park=True,
        )
    if _RESUME.match(raw) and has_active_goal:
        return Reflex(
            kind="resume_goal",
            spoken="Resuming.",
            resume=True,
        )
    if _STATUS.match(raw):
        line = (status_line or "").strip() or "Nothing is in progress."
        return Reflex(kind="status", spoken=line, status=True)
    hit = _APPROVE.match(raw)
    if hit:
        token = hit.group("token")
        kind = "approve" if raw.lower().find("deny") < 0 and raw.lower().find("reject") < 0 else "deny"
        return Reflex(kind=kind, spoken="Okay.", token=token)
    return None
