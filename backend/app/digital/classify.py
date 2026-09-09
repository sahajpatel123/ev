"""Inbox intelligence — rules first, Spark only for plausible meaningful mail."""

from __future__ import annotations

import re
from typing import Any

from app.digital.types import InboxClass

_NOISE = re.compile(
    r"(?i)\b(unsubscribe|view in browser|percent off|% off|newsletter|promotion|"
    r"noreply@|no-reply@|donotreply|mailer-daemon|notifications@)\b"
)
_SECURITY = re.compile(r"(?i)\b(password reset|2fa|verification code|otp|security alert|sign-in attempt)\b")
_MEETING = re.compile(r"(?i)\b(meet(?:ing)?|calendar invite|zoom|teams link|wednesday at|thursday at)\b")
_DEADLINE = re.compile(r"(?i)\b(deadline|due (?:on|by)|by friday|before \d|rsvp by)\b")
_DOCUMENT = re.compile(r"(?i)\b(attached|attachment|invoice|pdf|quotation|please find)\b")
_REQUEST = re.compile(r"(?i)\b(please (?:send|share|confirm|review)|can you|could you|need you to)\b")
_COMMIT_TO_ME = re.compile(r"(?i)\b(i(?:'|’)ll (?:send|check|get back|confirm)|we will send)\b")
_NEEDS_REPLY = re.compile(r"(?i)\?|please (?:reply|confirm|let me know)|looking forward")


def classify_message(
    *,
    subject: str = "",
    sender: str = "",
    text: str = "",
    labels: list[str] | None = None,
    unread: bool = False,
    has_attachment: bool = False,
) -> dict[str, Any]:
    blob = f"{subject}\n{sender}\n{text}"
    labels = labels or []
    if _SECURITY.search(blob):
        klass = InboxClass.SECURITY_SENSITIVE
    elif _NOISE.search(blob) or "CATEGORY_PROMOTIONS" in labels or "CATEGORY_SOCIAL" in labels:
        klass = InboxClass.NOISE
    elif has_attachment or _DOCUMENT.search(blob):
        klass = InboxClass.DOCUMENT
    elif _MEETING.search(blob):
        klass = InboxClass.MEETING
    elif _DEADLINE.search(blob):
        klass = InboxClass.DEADLINE
    elif _REQUEST.search(blob):
        klass = InboxClass.REQUEST
    elif unread and _NEEDS_REPLY.search(blob) and not _NOISE.search(sender):
        klass = InboxClass.REQUIRES_REPLY
    elif _COMMIT_TO_ME.search(blob):
        klass = InboxClass.COMMITMENT_TO_ME
    elif unread:
        klass = InboxClass.INFORMATIONAL
    else:
        klass = InboxClass.INFORMATIONAL
    needs_spark = klass not in {InboxClass.NOISE, InboxClass.INFORMATIONAL, InboxClass.SECURITY_SENSITIVE}
    return {
        "class": klass.value,
        "needs_spark": needs_spark and klass in {InboxClass.REQUEST, InboxClass.MEETING, InboxClass.DEADLINE},
        "origin": "EXTERNAL_CONTENT",
        "authority": "DATA",
    }


def batch_triage(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Do not call Spark for newsletters. Return buckets."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    spark_candidates = 0
    for msg in messages:
        result = classify_message(
            subject=str(msg.get("subject") or ""),
            sender=str(msg.get("from") or msg.get("sender") or ""),
            text=str(msg.get("text") or msg.get("snippet") or ""),
            labels=list(msg.get("label_ids") or msg.get("labels") or []),
            unread=bool(msg.get("unread")),
            has_attachment=bool(msg.get("attachments") or msg.get("has_attachment")),
        )
        klass = result["class"]
        buckets.setdefault(klass, []).append({**msg, "inbox_class": klass})
        if result["needs_spark"]:
            spark_candidates += 1
    return {
        "buckets": {k: [{"id": m.get("id"), "subject": m.get("subject"), "from": m.get("from")} for m in v[:20]] for k, v in buckets.items()},
        "counts": {k: len(v) for k, v in buckets.items()},
        "spark_candidates": spark_candidates,
        "spark_skipped_noise": len(buckets.get(InboxClass.NOISE.value, [])),
    }
