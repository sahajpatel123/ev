"""Muse Spark 1.3 decides whether the owner wants the live camera now.

Evie executes look. Mini must not refuse a first-try "look at what I'm
holding". Spark classifies the job; a conservative fallback covers the
obvious camera asks so the first utterance still fires when Spark is slow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Literal

logger = logging.getLogger("ev.spark_look")

CameraAction = Literal["look", "recall"]
ACTIONS: tuple[CameraAction, ...] = ("look", "recall")
_SPARK_BUDGET_S = 2.5

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

_ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["look", "recall", "chat"]},
    },
    "required": ["action"],
}

_SPARK_SYSTEM = """You are Evie's camera brain (Muse Spark 1.3 Contributor). Evie will execute; you only decide WHETHER to use the live camera.

The owner may ramble. Classify the *job*, not a keyword.

action:
- look: they want you to see what is in view NOW — holding, showing, look at this item, describe this thing, use the camera. A long sentence that includes look + holding is still look.
- recall: they want something she already saw or was asked to remember.
- chat: not a camera job (weather, files, web search, look this up, small talk, meetings).

Look-up / google / search the web is chat, not look.
Return JSON only.
"""


def fallback_camera_action(utterance: str) -> CameraAction | None:
    """look | recall | None. Never waits on Spark."""

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
    """True when Muse Spark 1.3 should poke this camera/keep/recall turn."""

    fallback = fallback_camera_action(utterance)
    if fallback in ACTIONS:
        return True
    return maybe_camera_utterance(utterance)


def maybe_camera_utterance(utterance: str) -> bool:
    """True when Spark should judge a camera ask the phrase book missed."""

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

    if looks_like_file_task(text) or is_weather_query(text):
        return False
    return True


async def decide_camera_action(utterance: str) -> CameraAction | None:
    """Spark decides look vs recall. Fallback only if Spark is dark or late."""

    fallback = fallback_camera_action(utterance)
    if not should_spark_camera(utterance):
        return fallback
    sparked = await _spark_decide(utterance)
    if sparked in ACTIONS:
        logger.warning(
            "spark_look action=%s source=spark fallback=%s",
            sparked,
            fallback,
        )
        return sparked
    return fallback


async def _spark_decide(utterance: str) -> str | None:
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

        result = await asyncio.wait_for(
            muse_spark_provider().chat_structured(
                [
                    ChatMessage(role="system", content=_SPARK_SYSTEM),
                    ChatMessage(
                        role="user",
                        content=f"Owner said: {(utterance or '')[:1500]}",
                    ),
                ],
                schema=_ACTION_SCHEMA,
                schema_name="camera_action",
                model=muse_spark_model(),
            ),
            timeout=_SPARK_BUDGET_S,
        )
    except (MuseProviderUnavailable, asyncio.TimeoutError):
        logger.info("spark_look unavailable")
        return None
    except Exception:  # noqa: BLE001 - first-try look must still run via fallback
        logger.info("spark_look failed", exc_info=True)
        return None
    return _parse_action(result.text or "")


def _parse_action(raw: str) -> str | None:
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
            lowered = text.lower()
            if "look" in lowered and "recall" not in lowered and "chat" not in lowered:
                return "look"
            if "recall" in lowered:
                return "recall"
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "").strip().lower()
    if action in ACTIONS:
        return action
    return None
