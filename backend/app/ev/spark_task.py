"""Muse Spark 1.3 decides HOW Evie should carry out a life task.

MAC LIVE COMPANION. Do not retune this for iPhone PWA / device-gateway.

Evie executes. Spark chooses manner, focus, and who/about/latest from the
owner's meaning — not from a phrase book. Hundreds of wordings can map to
the same task. If Spark cannot run, a conservative fallback summarises and
only treats a small read-aloud *structure* as readout (never a catalog of
sentences).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger("ev.spark_task")

FAMILIES = ("mail", "messages", "calls", "contacts", "calendar", "other")
MANNERS = ("digest", "particular", "readout", "lookup")
FOCUSES = ("gist", "when", "who", "subject", "readout")
_SPARK_BUDGET_S = 2.5

_TASK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "family": {"type": "string", "enum": list(FAMILIES)},
        "manner": {"type": "string", "enum": list(MANNERS)},
        "focus": {"type": "string", "enum": list(FOCUSES)},
        "who": {"type": "string"},
        "about": {"type": "string"},
        "latest": {"type": "boolean"},
    },
    "required": ["family", "manner"],
}

_SPARK_SYSTEM = """You are Evie's task brain (Muse Spark 1.3 Contributor). Evie will execute; you only decide HOW.

The owner may phrase the same job a hundred ways. Classify the *job*, not a keyword.
Follow-ups about an item Evie just spoke about (this / that / it / the last one) stay on that family. Do not wait for the word mail/inbox/message to appear again.

family: mail | messages | calls | contacts | calendar | other

manner:
- digest: overview of several items (what's new, check inbox, any mail/messages)
- particular: one item (last mail, that email, from Alex, when it arrived, who sent it)
- readout: they want the artifact spoken through — read it out, read it to me, go through it, line by line, the whole thing. NOT "what was it about". NOT when it arrived.
- lookup: a fact (someone's number/email), not a body

focus — what to speak about the selected item:
- gist: who + subject + short gist (default). Include the received time in the gist line.
- when: when it arrived / was sent. Use this whenever they ask about time, arrival, or "when did I get that".
- who: who sent it / who it was with
- subject: the subject/topic only
- readout: they asked to hear the body (same as manner readout)

Default for questions about what arrived / what it was about / last talk with someone is digest or particular with focus gist. That is a HEADER + short gist, never the body.

Readout is the exception: they asked to hear the content itself (read it out, read it to me, walk me through, line by line, the whole thing). NOT "latest", NOT "most recent", NOT "what did I talk with X last", NOT "what's new", NOT "when did I get it".

who: person or sender they named, else "".
about: topic they named, else "".
latest: true when they mean the last/that/this one, including follow-ups about that item.

Return JSON only.
"""

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
# Info questions Spark must never promote to readout.
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

_active: ContextVar["TaskDecision | None"] = ContextVar("ev_spark_task", default=None)
_LAST_LIFE: "LifeJob | None" = None


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
        # Spark-dark follow-up: they didn't name a new mailbox job, so stay
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
    """Wake Muse Spark 1.3 Contributor for this task. Always returns a decision."""

    fallback = fallback_task_decision(utterance, family_hint=family_hint)
    sparked = await _spark_decide(utterance, family_hint=family_hint or fallback.family)
    if sparked is None:
        return fallback
    logger.info(
        "spark_task family=%s manner=%s focus=%s source=spark",
        sparked.family,
        sparked.manner,
        sparked.focus,
    )
    return sparked


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


async def _spark_decide(utterance: str, *, family_hint: str) -> TaskDecision | None:
    from app.gateway.muse import (
        MuseProviderUnavailable,
        muse_spark_key_loaded,
        muse_spark_model,
    )

    if not (utterance or "").strip():
        return None
    if not muse_spark_key_loaded():
        return None
    try:
        from app.contracts import ChatMessage
        from app.gateway.muse_spark import muse_spark_provider

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
        result = await asyncio.wait_for(
            muse_spark_provider().chat_structured(
                [
                    ChatMessage(role="system", content=_SPARK_SYSTEM),
                    ChatMessage(
                        role="user",
                        content="\n".join(
                            part
                            for part in (
                                hint,
                                prior_line,
                                f"Owner said: {(utterance or '')[:1500]}",
                            )
                            if part
                        ),
                    ),
                ],
                schema=_TASK_SCHEMA,
                schema_name="life_task",
                model=muse_spark_model(),
                reasoning_effort="low",
            ),
            timeout=_SPARK_BUDGET_S,
        )
    except (MuseProviderUnavailable, asyncio.TimeoutError):
        logger.info("spark_task unavailable")
        return None
    except Exception:  # noqa: BLE001 - task must still run
        logger.info("spark_task failed", exc_info=True)
        return None
    parsed = _parse_decision(
        result.text or "", family_hint=family_hint, utterance=utterance
    )
    if parsed is None:
        return None
    return parsed


def _clamp_manner(utterance: str, manner: str) -> str:
    """Spark may misfire 'latest' as readout. Info asks stay gist."""

    if manner != "readout":
        return manner
    if wants_readout(utterance):
        return "readout"
    if _INFO_NOT_READOUT.search(utterance or ""):
        return "particular"
    return manner


def _parse_decision(raw: str, *, family_hint: str, utterance: str = "") -> TaskDecision | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    family = str(data.get("family") or family_hint or "other").strip().lower()
    if family not in FAMILIES:
        family = _family_from_hint(family, None)
        if family not in FAMILIES:
            family = "other"
    manner = str(data.get("manner") or "digest").strip().lower()
    if manner not in MANNERS:
        manner = "digest"
    manner = _clamp_manner(utterance, manner)
    focus = str(data.get("focus") or "gist").strip().lower()
    if focus not in FOCUSES:
        focus = "gist"
    if manner == "readout":
        focus = "readout"
    elif focus == "readout":
        manner = _clamp_manner(utterance, "readout")
        focus = "readout" if manner == "readout" else "gist"
    if focus in {"when", "who", "subject"} and manner == "digest":
        manner = "particular"
    latest = bool(data.get("latest"))
    if focus in {"when", "who", "subject"}:
        latest = True
    who = str(data.get("who") or "").strip()[:40]
    about = str(data.get("about") or "").strip()[:80]
    return TaskDecision(
        family=family,
        manner=manner,
        focus=focus if manner != "readout" else "readout",
        who=who,
        about=about,
        latest=latest,
        source="spark",
    )
