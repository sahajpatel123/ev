"""Owner language → Gmail search operators. Owner never needs Gmail syntax."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

_PERSON = re.compile(
    r"(?i)\b(?:from|by|sent by|emailed me|email from|mail from)\s+([A-Za-z][\w.\-]+(?:\s+[A-Za-z][\w.\-]+)?)"
)
_TO_PERSON = re.compile(r"(?i)\b(?:to|emailed)\s+([A-Za-z][\w.\-]+)")
_SUBJECT = re.compile(
    r"(?i)\b(?:about|subject|regarding|re:?)\s+['\"]?([^'\"]+?)['\"]?(?:\s|$)"
)
_INVOICE = re.compile(r"(?i)\binvoice|receipt|quotation|quote\b")
_PDF = re.compile(r"(?i)\bpdf\b")
_UNREAD = re.compile(r"(?i)\bunread|not read|haven't read|need(?:s)? a reply|to reply|waiting on me")
_IMPORTANT = re.compile(r"(?i)\bimportant|starred|priority\b")
_TODAY = re.compile(r"(?i)\btoday|this morning|tonight\b")
_YESTERDAY = re.compile(r"(?i)\byesterday\b")
_LAST_WEEK = re.compile(r"(?i)\blast week\b")
_LAST_MONTH = re.compile(r"(?i)\blast month\b")
_ATTACHMENT = re.compile(r"(?i)\battachment|attached|file they sent|the pdf|the (?:doc|file)\b")
_NEWSLETTER = re.compile(r"(?i)\bnewsletter|promotions?|unsubscribe\b")
_THREAD = re.compile(r"(?i)\bthread|whole conversation|entire (?:mail|email)\b")


def compile_gmail_query(text: str, *, now: datetime | None = None, person_email: str | None = None) -> dict[str, Any]:
    """Return Gmail `q` plus semantic hints. Never requires owner Gmail syntax."""
    now = now or datetime.now(UTC)
    raw = (text or "").strip()
    parts: list[str] = []
    hints: dict[str, Any] = {"source": "digital.query"}

    if person_email:
        parts.append(f"from:{person_email}")
        hints["person_email"] = person_email
    else:
        m = _PERSON.search(raw)
        sent = re.search(r"(?i)\b([A-Za-z][\w.\-]+)\s+sent\b", raw)
        name = None
        if m:
            name = m.group(1).strip()
        elif sent:
            name = sent.group(1).strip()
        if name and name.lower() not in {"the", "an", "a", "email", "mail"}:
            parts.append(f"from:{name}")
            hints["person"] = name

    if _UNREAD.search(raw):
        parts.append("is:unread")
        hints["unread"] = True
        if re.search(r"(?i)need(?:s)? a reply|to reply|waiting on me|asking for", raw):
            hints["needs_reply"] = True
    if _IMPORTANT.search(raw):
        parts.append("is:important")
        hints["important"] = True
    if _PDF.search(raw):
        parts.append("filename:pdf")
        hints["pdf"] = True
    if _ATTACHMENT.search(raw):
        parts.append("has:attachment")
        hints["attachment"] = True
    if _INVOICE.search(raw):
        parts.append("(invoice OR receipt OR quotation OR quote)")
        hints["invoice"] = True
    if _NEWSLETTER.search(raw):
        parts.append("category:promotions")
        hints["newsletter"] = True
    if _TODAY.search(raw):
        parts.append(f"after:{now.strftime('%Y/%m/%d')}")
        hints["window"] = "today"
    elif _YESTERDAY.search(raw):
        day = (now - timedelta(days=1)).strftime("%Y/%m/%d")
        parts.append(f"after:{day}")
        parts.append(f"before:{now.strftime('%Y/%m/%d')}")
        hints["window"] = "yesterday"
    elif _LAST_WEEK.search(raw):
        parts.append("newer_than:7d")
        hints["window"] = "week"
    elif _LAST_MONTH.search(raw):
        start = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
        end = now.replace(day=1)
        parts.append(f"after:{start.strftime('%Y/%m/%d')}")
        parts.append(f"before:{end.strftime('%Y/%m/%d')}")
        hints["window"] = "last_month"

    about = None
    sm = _SUBJECT.search(raw)
    if sm:
        about = sm.group(1).strip(" .")
        if about.lower() in {"the", "a", "an", "my", "email", "mail", "message"}:
            about = None
    if about is None:
        topical = re.search(
            r"(?i)\b(exhibition|invoice|university|admission|quotation|quote)\b", raw
        )
        about = topical.group(1) if topical else None
    if about and about.lower() not in {"email", "mail", "message"}:
        token = about.replace('"', "")
        if " " in token:
            parts.append(f'"{token}"')
        else:
            parts.append(token)
        hints["about"] = token

    q = " ".join(parts).strip() or raw
    hints["thread"] = bool(_THREAD.search(raw))
    return {"q": q, "hints": hints}


def compile_semantic_owner_ask(text: str) -> str:
    """Classify the owner ask so orchestrate can pick operations."""
    t = (text or "").lower()
    if re.search(r"\bdraft\b", t) and re.search(r"\breply\b", t):
        return "draft_reply"
    if re.search(r"\breply\b", t) and re.search(r"\bsaying\b|\bsay\b|\btell them\b", t):
        return "reply"
    if re.search(r"\bsend\b", t) and not re.search(r"\bwhat (?:did|was)\b", t):
        return "send"
    if re.search(r"\barchive\b", t):
        return "archive"
    if re.search(r"\bdownload\b|\bsave the (?:pdf|file|attachment)\b", t):
        return "download"
    if re.search(r"\bsummarize\b|\bsummary\b|\bwhole thread\b", t):
        return "summarize"
    if re.search(r"waiting on me|need(?:s)? a reply|who emailed me asking", t):
        return "needs_reply"
    if re.search(r"important emails|what(?:'s| is) (?:in )?my (?:inbox|mail)", t):
        return "list_important"
    return "search"
