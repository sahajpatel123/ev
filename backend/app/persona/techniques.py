"""Comedy technique registry (spec §2 HOW YOU'RE FUNNY).

Seven tools, each with an id, a one-line definition, the minimum ROAST
level that may use it, and the audiences that allow it. On-behalf text
carries no Evie voice, so no technique allows it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Technique:
    id: str
    definition: str
    min_roast: int
    audiences: tuple[str, ...]


TECHNIQUES: tuple[Technique, ...] = (
    Technique(
        "understatement",
        "say less than the situation deserves; let the gap do the work",
        1,
        ("private", "company"),
    ),
    Technique(
        "absurd_literalism",
        "take his words at face value until they collapse",
        1,
        ("private", "company"),
    ),
    Technique(
        "escalation",
        "agree so hard the idea visibly breaks",
        2,
        ("private",),
    ),
    Technique(
        "twist_ending",
        "land the turn in the last few words",
        1,
        ("private", "company"),
    ),
    Technique(
        "status_flip",
        "roast yourself, then him",
        2,
        ("private",),
    ),
    Technique(
        "callback",
        "revive an earlier landed moment that fits right now",
        1,
        ("private", "company"),
    ),
    Technique(
        "brutal_honest",
        "one true line with no padding",
        1,
        ("private",),
    ),
)

_BY_ID = {technique.id: technique for technique in TECHNIQUES}


def get(technique_id: str) -> Technique | None:
    """Return the technique with this id, or None."""
    return _BY_ID.get(technique_id)


def eligible(*, roast: int, audience: str) -> tuple[Technique, ...]:
    """Techniques allowed at this roast level and audience, registry order."""
    return tuple(
        technique
        for technique in TECHNIQUES
        if roast >= technique.min_roast and audience in technique.audiences
    )
