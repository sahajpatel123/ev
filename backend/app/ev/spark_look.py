"""MiMo decides whether the owner wants the live camera now.

Evie executes look. Gemini must not refuse a first-try "look at what I'm
holding". MiMo classifies the job via one finite choice; a conservative
fallback covers the obvious camera asks so the first utterance still fires
when MiMo is slow.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

logger = logging.getLogger("ev.spark_look")

CameraAction = Literal["look", "recall"]
ACTIONS: tuple[CameraAction, ...] = ("look", "recall")

_LOOK_UP_RE = re.compile(
    r"\blook(?: this| it)? up\b|\blook this up\b|\bgoogle\b|\bon the web\b",
    re.IGNORECASE,
)
_CAMERA_ISH_RE = re.compile(
    r"("
    r"\blook at\b|"
    r"\bcan you (?:see|look|tell me what)\b|"
    r"\bi want you to look\b|"
    r"\bwhat (?:am i|do you see)\b|"
    r"\bin my hand\b|"
    r"\bi(?:'m| am) holding\b|"
    r"\bthis item\b|"
    r"\bshowing you\b|"
    r"\b(?:use|open|point) (?:the |your )?camera\b|"
    r"\bcamera\b|"
    r"\btell me .{0,48}\b(?:this|that) (?:item|thing|object)\b"
    r")",
    re.IGNORECASE,
)

def fallback_camera_action(utterance: str) -> CameraAction | None:
    """look | recall | None. Never waits on MiMo."""

    from app.memory.visual import (
        is_keep_recall_query,
        is_visual_recall_query,
        wants_current_visual,
        wants_keep_visible,
        wants_past_visual,
    )

    text = (utterance or "").strip()
    if not text:
        return None
    if _LOOK_UP_RE.search(text):
        return None
    if wants_keep_visible(text):
        return "look"
    if is_keep_recall_query(text) or (
        is_visual_recall_query(text) and not wants_current_visual(text)
    ):
        return "recall"
    if wants_past_visual(text) and not wants_current_visual(text):
        return "recall"
    if wants_current_visual(text):
        return "look"
    return None


def should_spark_camera(utterance: str) -> bool:
    """True when MiMo should judge this camera/keep/recall turn."""

    fallback = fallback_camera_action(utterance)
    if fallback in ACTIONS:
        return True
    return maybe_camera_utterance(utterance)


def maybe_camera_utterance(utterance: str) -> bool:
    """True when MiMo should judge a camera ask the phrase book missed."""

    text = (utterance or "").strip()
    if not text or _LOOK_UP_RE.search(text):
        return False
    if fallback_camera_action(text) is not None:
        return False
    if not _CAMERA_ISH_RE.search(text):
        return False
    if re.search(
        r"\bholding (?:a |the )?(?:meeting|call|interview|session)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    from app.ev.laptop_files import looks_like_file_task
    from app.search.live import is_weather_query

    return not (looks_like_file_task(text) or is_weather_query(text))


async def decide_camera_action(utterance: str) -> CameraAction | None:
    """MiMo decides look vs recall. Fallback only if MiMo is dark or late."""

    fallback = fallback_camera_action(utterance)
    if not should_spark_camera(utterance):
        return fallback
    decided = await _mimo_decide(utterance)
    if decided in ACTIONS:
        logger.warning(
            "spark_look action=%s source=mimo fallback=%s",
            decided,
            fallback,
        )
        return decided
    return fallback


async def _mimo_decide(utterance: str) -> str | None:
    """MiMo owns the camera-vs-chat decision: one finite action choice."""

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
    try:
        call = await decide_via_role(
            {
                "transcript": (utterance or "")[:1500],
                "instructions": "Decide whether this turn needs the live camera.",
            },
            {
                "action": DecisionQuestion(
                    type="choice",
                    instructions="What does this turn need?",
                    criteria={
                        "look": "They want Evie to see what is in view NOW (holding, showing, look at this item).",
                        "recall": "They want something Evie already saw or was asked to remember.",
                        "chat": "Not a camera job (weather, files, web search, look this up, small talk, meetings).",
                    },
                )
            },
            actor="spark_look",
        )
    except (MimoUnavailable, MimoEgressDenied):
        logger.info("mimo camera decision unavailable")
        return None
    if call.status != "ok":
        logger.info("mimo camera decision failed: %s", call.error)
        return None
    action = answer_choice(call, "action")
    return action if action in {"look", "recall"} else None
