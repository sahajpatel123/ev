"""MiMo decides HOW Evie should carry out a life task.

MAC LIVE COMPANION. Do not retune this for iPhone PWA / device-gateway.

Evie executes. MiMo chooses manner, focus, and latest from the owner's
meaning via finite choices — not from a phrase book. Hundreds of wordings
can map to the same task. If MiMo cannot run, a conservative fallback
summarises and only treats a small read-aloud *structure* as readout
(never a catalog of sentences).
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger("ev.spark_task")

FAMILIES = ("mail", "messages", "calls", "contacts", "calendar", "other")
MANNERS = ("digest", "particular", "readout", "lookup")
FOCUSES = ("gist", "when", "who", "subject", "readout")

# Read-aloud as a speech-act shape, not a list of owner lines.
_READ_ALOUD = re.compile(
    r"\bread[\s-]?outs?\b|"
    r"\bread(?:\s+\w+){0,4}\s+(?:out|aloud|loud|through)\b|"
    r"\bread(?:\s+it|\s+them|\s+this|\s+that|\s+the)?\s+to\s+me\b|"
    r"\bread\s+me\s+(?:the|that|this|my)\b|"
    r"\bwalk\s+me\s+through\b|"
    r"\bgo\s+through\s+(?:the|that|this|it)\b|"
    r"\bline\s+by\s+line\b|"
    r"\bword\s+for\s+word\b|"
    r"\brecite\b|"
    r"\bthe\s+whole\s+(?:e-?mail|mail|message|chat|thing)\b",
    re.IGNORECASE,
)
# Info questions MiMo must never promote to readout.
_INFO_NOT_READOUT = re.compile(
    r"\b("
    r"what(?:'s| is| are| was| were| did)\b|"
    r"when (?:did|was|is|do|does)|"
    r"what time|"
    r"any (?:new|mail|message|text|chat)|"
    r"latest|most recent|"
    r"who (?:do i|have i|texted|messaged|do i talk)|"
    r"catch me up|any word"
    r")\b",
    re.IGNORECASE,
)

_active: ContextVar[TaskDecision | None] = ContextVar("ev_spark_task", default=None)
_LAST_LIFE: LifeJob | None = None


@dataclass(frozen=True)
class LifeJob:
    """Last Mac life artifact Evie spoke. Follow-ups stay on this job."""

    family: str
    tool: str
    query: str = ""
    who: str = ""
    subject: str = ""
    when: str = ""
    gist: str = ""


@dataclass(frozen=True)
class TaskDecision:
    family: str = "other"
    manner: str = "digest"
    focus: str = "gist"
    who: str = ""
    about: str = ""
    latest: bool = False
    source: str = "fallback"

    def tokens(self) -> list[str]:
        found: list[str] = []
        for part in (self.who, self.about):
            for token in re.findall(r"[a-z0-9]{2,}", (part or "").lower()):
                if token not in found:
                    found.append(token)
        return found

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def wants_readout(utterance: str) -> bool:
    """True only for a read-aloud speech-act, not 'latest' / 'what was it about'."""

    return bool(_READ_ALOUD.search(utterance or ""))


def active_decision() -> TaskDecision | None:
    return _active.get()


def bind_decision(decision: TaskDecision | None) -> Token:
    return _active.set(decision)


def reset_decision(token: Token) -> None:
    _active.reset(token)


def last_life_job() -> LifeJob | None:
    return _LAST_LIFE


def remember_life_job(job: LifeJob | None) -> None:
    global _LAST_LIFE
    _LAST_LIFE = job


def clear_life_job() -> None:
    remember_life_job(None)


def remember_life_from_hits(
    hits: list[dict[str, Any]] | None,
    *,
    family: str,
    tool: str,
    query: str,
) -> None:
    if not hits:
        return
    item = next((row for row in hits if isinstance(row, dict)), None)
    if item is None:
        return
    who = str(item.get("sender") or item.get("handle") or item.get("title") or "").strip()
    subject = str(item.get("subject") or "").strip()
    when = item.get("when") or item.get("received") or item.get("date") or ""
    if hasattr(when, "isoformat"):
        when = when.isoformat()
    gist = str(item.get("gist") or "").strip()
    remember_life_job(
        LifeJob(
            family=family,
            tool=tool,
            query=(query or "")[:400],
            who=who[:80],
            subject=subject[:120],
            when=str(when)[:80],
            gist=gist[:160],
        )
    )


def fallback_task_decision(utterance: str, *, family_hint: str = "") -> TaskDecision:
    """Offline/safe decision. Summary unless the ask is structurally read-aloud."""

    from app.memory.life_archive.locate import life_channel
    from app.memory.mail_speak import mail_selector

    text = (utterance or "").strip()
    channel = life_channel(text)
    family = _family_from_hint(family_hint, channel)
    readout = wants_readout(text)
    if family == "contacts" or (
        channel == "contacts" and not readout
    ):
        return TaskDecision(family="contacts", manner="lookup", source="fallback")
    if family == "mail":
        selector = mail_selector(text)
        prior = last_life_job()
        if readout:
            return TaskDecision(
                family="mail",
                manner="readout",
                focus="readout",
                who=selector.who,
                about=selector.about,
                latest=selector.latest or not bool(selector.who or selector.about),
                source="fallback",
            )
        if selector.particular:
            return TaskDecision(
                family="mail",
                manner="particular",
                focus="gist",
                who=selector.who,
                about=selector.about,
                latest=selector.latest,
                source="fallback",
            )
        # MiMo-dark follow-up: they didn't name a new mailbox job, so stay
        # on the last envelope. A fresh "check my inbox" still names mail.
        if prior is not None and prior.family == "mail" and channel != "mail":
            return TaskDecision(
                family="mail",
                manner="particular",
                focus="gist",
                who=prior.who[:40],
                latest=True,
                source="fallback",
            )
        return TaskDecision(family="mail", manner="digest", focus="gist", source="fallback")
    if readout and family in {"messages", "mail"}:
        return TaskDecision(
            family=family, manner="readout", focus="readout", latest=True, source="fallback"
        )
    if family == "messages":
        from app.memory.life_archive.locate import is_chat_with_other_person

        if is_chat_with_other_person(text):
            return TaskDecision(family="messages", manner="particular", source="fallback")
        prior = last_life_job()
        if (
            prior is not None
            and prior.family == "messages"
            and channel not in {"imessage", "whatsapp"}
        ):
            return TaskDecision(
                family="messages",
                manner="particular",
                focus="gist",
                who=prior.who[:40],
                latest=True,
                source="fallback",
            )
        return TaskDecision(family="messages", manner="digest", source="fallback")
    return TaskDecision(family=family or "other", manner="digest", source="fallback")


async def decide_task(utterance: str, *, family_hint: str = "") -> TaskDecision:
    """Ask MiMo for this task's manner/focus. Always returns a decision."""

    fallback = fallback_task_decision(utterance, family_hint=family_hint)
    decided = await _mimo_decide(utterance, family_hint=family_hint or fallback.family)
    if decided is None:
        return fallback
    logger.info(
        "spark_task family=%s manner=%s focus=%s source=mimo",
        decided.family,
        decided.manner,
        decided.focus,
    )
    return decided


def apply_decision_to_mail_selector(query: str, decision: TaskDecision | None):
    from app.memory.mail_speak import MailSelector, mail_selector

    base = mail_selector(query)
    if decision is None:
        return base
    if decision.manner == "digest" and str(getattr(decision, "focus", "") or "") not in {
        "when",
        "who",
        "subject",
    }:
        return MailSelector()
    who = decision.who or base.who
    about = decision.about or base.about
    latest = bool(decision.latest) or base.latest
    want = base.want
    if decision.manner in {"particular", "readout"} and not (who or about or latest or want):
        latest = True
    if str(getattr(decision, "focus", "") or "") in {"when", "who", "subject"}:
        latest = True
    return MailSelector(who=who, about=about, want=want, latest=latest)


def _family_from_hint(hint: str, channel: str | None) -> str:
    hint = (hint or "").strip().lower()
    if hint in {"mail", "inbox"}:
        return "mail"
    if hint in {"chats", "messages", "imessage", "whatsapp"}:
        return "messages"
    if hint in FAMILIES:
        return hint
    if channel == "mail":
        return "mail"
    if channel in {"imessage", "whatsapp"}:
        return "messages"
    if channel == "contacts":
        return "contacts"
    return channel or "other"


async def _mimo_decide(utterance: str, *, family_hint: str) -> TaskDecision | None:
    """MiMo owns life-task routing: finite choices only, no invented text."""

    from app.gateway.openrouter_mimo import MimoEgressDenied, MimoUnavailable
    from app.gateway.roles import (
        DecisionQuestion,
        answer_choice,
        decide_via_role,
        text_role_available,
    )

    if not (utterance or "").strip():
        return None
    if not text_role_available():
        return None
    hint = f"Likely family: {family_hint}." if family_hint else ""
    prior = last_life_job()
    prior_line = ""
    if prior is not None:
        prior_line = (
            f"Evie just handled a {prior.family} item via {prior.tool}: "
            f"who={prior.who or '(unknown)'}, when={prior.when or 'unknown'}, "
            f"subject={prior.subject or '(none)'}, gist={prior.gist[:120]}. "
            "Follow-ups about that same item stay on this family. "
            "If they ask when it arrived, focus=when."
        )
    try:
        call = await decide_via_role(
            {
                "transcript": (utterance or "")[:1500],
                "family_hint": family_hint or "",
                "context": "\n".join(part for part in (hint, prior_line) if part),
                "instructions": (
                    "Classify the owner's life-task request. Choose family, manner, "
                    "focus, and whether they asked for the latest item. Do not invent "
                    "names or content."
                ),
            },
            {
                "family": DecisionQuestion(
                    type="choice",
                    instructions="Which life family does this request concern?",
                    criteria={
                        "mail": "Email / inbox / mail items.",
                        "messages": "Text messages / iMessage / WhatsApp.",
                        "calls": "Phone calls, missed calls, call history.",
                        "contacts": "Contacts / people records.",
                        "calendar": "Calendar events, meetings, appointments.",
                        "other": "Anything else, or too ambiguous to classify.",
                    },
                ),
                "manner": DecisionQuestion(
                    type="choice",
                    instructions="What kind of handling does the owner want?",
                    criteria={
                        "digest": "Overview of several items (what's new, check inbox).",
                        "particular": "One specific item or sender.",
                        "readout": "Read the item aloud to the owner.",
                        "lookup": "Find a specific fact in the items.",
                    },
                ),
                "focus": DecisionQuestion(
                    type="choice",
                    instructions="What detail are they after?",
                    criteria={
                        "gist": "The summary or content.",
                        "when": "When it arrived / timing.",
                        "who": "Who it is from.",
                        "subject": "The subject line.",
                        "readout": "The full text to read aloud.",
                    },
                ),
                "latest": DecisionQuestion(
                    type="choice",
                    instructions="Did they ask for the latest/most recent item?",
                    criteria={"true": "Yes, the latest item.", "false": "No."},
                ),
            },
            actor="spark_task",
        )
    except (MimoUnavailable, MimoEgressDenied):
        logger.info("mimo life-task decision unavailable")
        return None
    if call.status != "ok":
        logger.info("mimo life-task decision failed: %s", call.error)
        return None

    family = answer_choice(call, "family") or family_hint or "other"
    if family not in FAMILIES:
        family = "other"
    manner = answer_choice(call, "manner") or "digest"
    if manner not in MANNERS:
        manner = "digest"
    focus = answer_choice(call, "focus") or "gist"
    if focus not in FOCUSES:
        focus = "gist"
    latest = answer_choice(call, "latest") == "true"
    return TaskDecision(
        family=family,
        manner=_clamp_manner(utterance, manner),
        focus=focus,
        latest=latest,
        source="mimo",
    )


def _clamp_manner(utterance: str, manner: str) -> str:
    """MiMo may misfire 'latest' as readout. Info asks stay gist."""

    if manner != "readout":
        return manner
    if wants_readout(utterance):
        return "readout"
    if _INFO_NOT_READOUT.search(utterance or ""):
        return "particular"
    return manner
