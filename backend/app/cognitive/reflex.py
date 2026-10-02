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
_GREETING = re.compile(
    r"^\s*(?:evie[, ]*)?(?:hi|hello|hey|yo|good (?:morning|afternoon|evening))"
    r"(?:[, ]*evie)?\s*[.!?]*\s*$",
    re.IGNORECASE,
)
# ASR often prefixes a filler ("You, how are you?" / "So, how's it going?")
# and owners often phrase identity/social turns politely ("Can you give me
# your introduction?").
_LEAD = (
    r"^\s*(?:(?:evie|you|so|well|okay|ok)[, ]*)*"
    r"(?:(?:can|could|would) you(?: please)?[ ,]*|please[ ,]*)*"
)
_SOCIAL = re.compile(
    _LEAD
    + r"(?:how are you(?: doing)?|how(?:'s| is) it going|how are things|"
    r"what(?:'s| is) up|how(?:'s| is) your day)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_IDENTITY = re.compile(
    _LEAD
    + r"(?:who are you|what are you|what(?:'s| is) your name|"
    r"tell me your name|"
    r"(?:give|tell) me your (?:introduction|intro)|"
    r"introduce yourself|tell me about yourself|"
    r"give your (?:introduction|intro)|what can you do)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_THANKS = re.compile(
    r"^\s*(?:evie[, ]*)?(?:thanks|thank you|thx|ty)\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def identity_line() -> str:
    """Deterministic self-introduction. No model round trip, no overclaiming."""

    return (
        "I'm Evie — your personal AI companion. I keep your memories and timeline, "
        "watch your mail, messages, and calendar, run tasks on your Mac, and answer "
        "from what you've told me."
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
    if _GREETING.match(raw):
        # A bare hello (including the Mac client's synthetic "Hi." on live
        # open) never needs a model round trip. Answer instantly; the turn
        # ledger still records it and the pending offer survives because a
        # greeting is not a substantive turn.
        return Reflex(kind="greeting", spoken="Hello!")
    return match_role_reflex(raw)





def match_role_reflex(raw: str) -> Reflex | None:
    """MiMo-role admission for social/identity turns. See _run_cognitive_kernel.

    Dynamic roles keep Evie conversational while the kernel stays authoritative:
    the reflex admits the turn, then the role wording call (chat with an empty
    tool surface, bounded retries, ~2.5s give-up) replaces the placeholder with
    the model's own words. If the model path ever fails, the placeholder still
    speaks — never silence.
    """

    return match_social_identity(raw)

def match_social_identity(transcript: str) -> Reflex | None:
    """Recognize only anchored social turns using existing fallback wording."""

    raw = (transcript or "").strip()
    if _SOCIAL.match(raw):
        return Reflex(
            kind="social",
            spoken="Doing well — thanks for asking. What can I do for you?",
        )
    if _IDENTITY.match(raw):
        return Reflex(kind="identity", spoken=identity_line())
    if _THANKS.match(raw):
        return Reflex(kind="thanks", spoken="You're welcome.")
    return None
