"""Spoken mail: particular gist or short digest, never the full body.

Selection is from query structure (who / about / that-or-latest), not a
phrase list. Gists are extractive and capped so Evie can say what a
message was about without reciting it.
"""

from __future__ import annotations

import email
import html as html_lib
import plistlib
import re
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, Iterable

SPOKEN_GIST_CAP = 220
DIGEST_HEADLINES = 3
SPOKEN_MAIL_CAP = 520
SPOKEN_READOUT_CAP = 1600
EMLX_READ_CAP = 8000

_FROM_WHO = re.compile(
    r"\b(?:from|by)\s+(?:my\s+)?([A-Za-z][A-Za-z0-9.'-]{1,40})",
    re.IGNORECASE,
)
_WHOSE_MAIL = re.compile(
    r"\b([A-Za-z][A-Za-z'-]{1,30})'s\s+(?:e-?mails?|mails?)\b",
    re.IGNORECASE,
)
_ABOUT = re.compile(
    r"\b(?:about|regarding|re)\s+(?:the\s+|an?\s+|my\s+)?"
    r"([A-Za-z][A-Za-z0-9'&-]{1,40}(?:\s+[A-Za-z][A-Za-z0-9'&-]{1,40}){0,3})",
    re.IGNORECASE,
)
_LATEST = re.compile(
    r"\b(?:last|latest|newest|most recent)\b|"
    r"\b(?:that|this|the)\s+(?:e-?mail|mail|one|message)\b|"
    r"\bthe one\b",
    re.IGNORECASE,
)
_MAIL_HIT_TYPES = frozenset(
    {
        "mail.envelope.received",
        "life.mail.envelope",
    }
)
_CHANNEL_WORDS = frozenset(
    {
        "e-mail",
        "e-mails",
        "email",
        "emails",
        "gmail",
        "inbox",
        "mail",
        "mailbox",
        "mails",
        "message",
        "messages",
    }
)
_ASK_WEAK = frozenset(
    {
        "about",
        "any",
        "anything",
        "been",
        "check",
        "did",
        "does",
        "explain",
        "fetch",
        "fetched",
        "find",
        "found",
        "get",
        "gist",
        "give",
        "got",
        "here",
        "into",
        "just",
        "let",
        "list",
        "look",
        "looking",
        "need",
        "new",
        "one",
        "open",
        "out",
        "please",
        "read",
        "readout",
        "aloud",
        "recite",
        "receive",
        "received",
        "recent",
        "search",
        "searched",
        "see",
        "show",
        "still",
        "summarise",
        "summarize",
        "summary",
        "tell",
        "than",
        "that",
        "them",
        "then",
        "there",
        "these",
        "this",
        "those",
        "unread",
        "was",
        "were",
        "what",
        "which",
        "whom",
        "whole",
        "aloud",
        "loud",
        "through",
        "word",
        "words",
        "line",
        "lines",
    }
)
_TIMEISH = frozenset(
    {
        "afternoon",
        "evening",
        "morning",
        "night",
        "today",
        "tonight",
        "tomorrow",
        "week",
        "weekend",
        "yesterday",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
)
_SKIP_SENTENCE = re.compile(
    r"unsubscribe|view in browser|privacy policy|copyright\s+\d{4}|"
    r"all rights reserved|manage preferences|this email was sent|"
    r"^sent from |^get outlook|^leaking this email",
    re.IGNORECASE,
)
_HTML_TAG = re.compile(r"<[^>]+>", re.DOTALL)
_QUOTE_LINE = re.compile(r"^\s*>")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_SENDER_NAME = re.compile(r"^\s*(?:\"([^\"]+)\"|([^<]+?))\s*<")


@dataclass(frozen=True)
class MailSelector:
    """Who / about / latest — structure of the ask, not a canned phrasing."""

    who: str = ""
    about: str = ""
    want: tuple[str, ...] = ()
    latest: bool = False

    @property
    def particular(self) -> bool:
        return bool(self.who or self.about or self.latest or self.want)

    def tokens(self) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for part in (self.who, self.about, *self.want):
            for token in re.findall(r"[a-z0-9]{2,}", (part or "").lower()):
                if token in seen or token in _CHANNEL_WORDS or token in _ASK_WEAK:
                    continue
                seen.add(token)
                found.append(token)
        return found


def mail_selector(query: str) -> MailSelector:
    text = (query or "").strip()
    if not text:
        return MailSelector()
    from app.memory.life_archive.locate import life_channel

    if life_channel(text) != "mail":
        return MailSelector()
    who = ""
    whose = _WHOSE_MAIL.search(text)
    if whose:
        who = whose.group(1).strip()
    if not who:
        from_who = _FROM_WHO.search(text)
        if from_who:
            who = from_who.group(1).strip()
    about = ""
    about_match = _ABOUT.search(text)
    if about_match:
        about = about_match.group(1).strip()
        about = re.sub(
            r"\b(?:e-?mails?|mails?|inbox|gmail)\b",
            "",
            about,
            flags=re.IGNORECASE,
        ).strip(" .,")
    latest = bool(_LATEST.search(text))
    want: list[str] = []
    for token in re.findall(r"[a-z0-9']+", text.lower()):
        token = token.replace("'", "")
        if len(token) < 3:
            continue
        if token in _CHANNEL_WORDS or token in _ASK_WEAK or token in _TIMEISH:
            continue
        if who and token == re.sub(r"[^a-z0-9]+", "", who.lower()):
            continue
        if about and token in about.lower():
            continue
        want.append(token)
    # Leftover content words make this a particular ask (a person or topic),
    # not an inbox digest. Time-only leftovers stay a digest.
    want_tuple = tuple(want[:4])
    return MailSelector(
        who=who,
        about=about,
        want=want_tuple,
        latest=latest,
    )


def selector_tokens(query: str) -> list[str]:
    return mail_selector(query).tokens()


def is_mail_hit(item: dict[str, Any] | None) -> bool:
    if not isinstance(item, dict):
        return False
    kind = str(item.get("memory_type") or item.get("kind") or "")
    if kind in _MAIL_HIT_TYPES:
        return True
    if str(item.get("channel") or item.get("shelf") or "") == "mail":
        return True
    if str(item.get("source") or "") == "mail":
        return True
    return False


def is_mail_ask(query: str) -> bool:
    from app.memory.life_archive.locate import life_channel

    return life_channel(query) == "mail"


def display_sender(raw: str) -> str:
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return "someone"
    named = _SENDER_NAME.match(text)
    if named:
        name = (named.group(1) or named.group(2) or "").strip().strip('"')
        if name:
            return name[:40]
    if "@" in text and " " not in text:
        return text.split("@", 1)[0].replace(".", " ")[:40]
    return text[:40]


def gist_from_preview(
    preview: str,
    *,
    subject: str = "",
    want: Iterable[str] = (),
    cap: int = SPOKEN_GIST_CAP,
) -> str:
    """One or two spoken sentences about the mail. Never the whole artifact."""

    text = _plain_preview(preview)
    subject = " ".join(str(subject or "").split()).strip()
    needed = [token.lower() for token in want if token and len(token) >= 3]
    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    usable: list[str] = []
    for sentence in sentences:
        compact = " ".join(sentence.split())
        if len(compact) < 8:
            continue
        if _SKIP_SENTENCE.search(compact):
            continue
        usable.append(compact)
    if needed and usable:
        matched = [item for item in usable if any(token in item.lower() for token in needed)]
        if matched:
            usable = matched + [item for item in usable if item not in matched]
    if not usable and text:
        usable = [" ".join(text.split())]
    if not usable:
        return _cap_words(subject, cap) if subject else ""
    picked: list[str] = []
    used = 0
    for sentence in usable[:4]:
        piece = sentence if sentence.endswith((".", "!", "?")) else sentence.rstrip(".,;:") + "."
        if used and used + len(piece) + 1 > cap:
            break
        if not picked and len(piece) > cap:
            picked.append(_cap_words(piece, cap))
            break
        picked.append(piece)
        used += len(piece) + 1
        if len(picked) >= 2 or used >= cap:
            break
    gist = " ".join(picked).strip()
    if subject and gist.lower().startswith(subject.lower()) and len(gist) <= len(subject) + 2:
        return _cap_words(subject, cap)
    return gist[: cap + 8].rstrip()


def generated_summary_text(blob: Any) -> str:
    """Apple Intelligence topLine from Envelope Index generated_summaries."""

    if isinstance(blob, str) and blob.strip() and blob.strip() != "$null":
        return " ".join(blob.split()).strip()
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 8:
        return ""
    try:
        data = plistlib.loads(bytes(blob))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    objects = data.get("$objects") or []
    top = data.get("$top") or {}
    if not isinstance(objects, list) or not isinstance(top, dict):
        return ""

    def _uid_index(value: Any) -> int | None:
        index = getattr(value, "data", None)
        return index if isinstance(index, int) else None

    def _nsstring(value: Any) -> str:
        if isinstance(value, str):
            text = value.strip()
            return "" if text in {"$null", ""} else " ".join(text.split())
        index = _uid_index(value)
        if index is None or index < 0 or index >= len(objects):
            return ""
        node = objects[index]
        if isinstance(node, str):
            return _nsstring(node)
        if isinstance(node, dict):
            return _nsstring(node.get("NSString"))
        return ""

    for key in ("generatedSummary.topLine", "generatedSummary.synopsis"):
        text = _nsstring(top.get(key))
        if len(text) >= 12:
            return text
    longest = ""
    for node in objects:
        if isinstance(node, dict):
            text = _nsstring(node.get("NSString"))
        elif isinstance(node, str):
            text = _nsstring(node)
        else:
            continue
        if len(text) > len(longest) and text not in {"$null", "NSMutableString", "NSString"}:
            longest = text
    return longest


def preview_from_emlx(path: str | Path, *, cap: int = EMLX_READ_CAP) -> str:
    """Ask-time body preview from a local .emlx. Never persist the result."""

    target = Path(path)
    try:
        raw = target.read_bytes()[: cap + 80]
    except OSError:
        return ""
    newline = raw.find(b"\n")
    payload = raw[newline + 1 :] if newline >= 0 else raw
    try:
        message = email.message_from_bytes(payload)
    except Exception:
        return _plain_preview(payload.decode("utf-8", errors="replace"))
    return _plain_preview(_email_text(message))


def find_emlx(mail_root: str | Path, rowid: int) -> Path | None:
    if not rowid:
        return None
    root = Path(mail_root)
    if not root.is_dir():
        return None
    name = f"{int(rowid)}.emlx"
    for inbox in root.glob("*/INBOX.mbox"):
        for hit in inbox.rglob(name):
            return hit
    for mailbox in root.glob("*/*.mbox"):
        if mailbox.name.upper() == "INBOX.MBOX":
            continue
        for hit in mailbox.rglob(name):
            return hit
    return None


def mail_fields(item: dict[str, Any]) -> dict[str, str]:
    subject = str(item.get("subject") or "").strip()
    sender = str(item.get("sender") or "").strip()
    gist = str(item.get("gist") or "").strip()
    text = " ".join(str(item.get("text") or "").split()).strip()
    if (not subject or not sender) and " from " in text.lower():
        head, tail = re.split(r"\s+from\s+", text, maxsplit=1, flags=re.IGNORECASE)
        subject = subject or head.strip().rstrip(".")
        sender = sender or tail.strip().rstrip(".")
    if not subject and text.lower().startswith("mail:"):
        subject = text.split(":", 1)[1].strip().rstrip(".")
    if not gist:
        gist = gist_from_preview(
            str(item.get("preview") or item.get("snippet") or ""),
            subject=subject,
        )
    return {
        "subject": subject,
        "sender": sender,
        "gist": gist,
        "text": text,
    }


def fill_readout(item: dict[str, Any], *, mail_index_path: str = "") -> None:
    """Ask-time body for a Spark readout. Never persist. Cap hard."""

    if str(item.get("readout") or "").strip():
        return
    preview = str(item.get("snippet") or item.get("preview") or item.get("body") or "").strip()
    if len(preview) >= 12:
        item["readout"] = _plain_preview(preview)[:2400]
        return
    rowid = item.get("rowid")
    path = mail_index_path
    if not path:
        try:
            from app.services.life_stream_daemon import find_mail_envelope_index

            path = find_mail_envelope_index()
        except Exception:
            path = ""
    if not rowid or not path:
        return
    version_root = str(Path(path).resolve().parent.parent)
    emlx = find_emlx(version_root, int(rowid))
    if emlx is None:
        return
    body = preview_from_emlx(emlx)
    if body:
        item["readout"] = body[:2400]


def _speak_readout(item: dict[str, Any]) -> str:
    fields = mail_fields(item)
    sender = display_sender(fields["sender"]) if fields["sender"] else "someone"
    subject = fields["subject"]
    body = _plain_preview(str(item.get("readout") or item.get("snippet") or item.get("preview") or ""))
    if not body:
        body = fields["gist"] or subject
        if not body:
            return ""
        line = f"I only have the subject of the mail from {sender}: {body}"
        if not line.endswith((".", "!", "?")):
            line += "."
        return line[:SPOKEN_READOUT_CAP]
    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(body) if part.strip()]
    usable: list[str] = []
    for sentence in sentences:
        compact = " ".join(sentence.split())
        if len(compact) < 8 or _SKIP_SENTENCE.search(compact):
            continue
        if not compact.endswith((".", "!", "?")):
            compact = compact.rstrip(".,;:") + "."
        usable.append(compact)
    if not usable:
        usable = [" ".join(body.split())]
    lead = f"Reading it out. Mail from {sender}"
    if subject:
        lead += f", {subject}"
    lead += "."
    spoken = lead
    for sentence in usable:
        if len(spoken) + 1 + len(sentence) > SPOKEN_READOUT_CAP - 36:
            spoken += " That's as far as I'll read."
            break
        spoken += " " + sentence
    return spoken[:SPOKEN_READOUT_CAP]


def speak_mail(query: str, items: list[dict[str, Any]], decision: Any | None = None) -> str:
    """Spark (or fallback) chooses readout vs gist vs digest. Never dumps by default."""

    rows = [item for item in items if isinstance(item, dict)]
    if not rows:
        return ""
    if decision is None:
        try:
            from app.ev.spark_task import active_decision, apply_decision_to_mail_selector

            decision = active_decision()
            selector = apply_decision_to_mail_selector(query, decision)
        except Exception:
            selector = mail_selector(query)
            decision = None
    else:
        from app.ev.spark_task import apply_decision_to_mail_selector

        selector = apply_decision_to_mail_selector(query, decision)
    manner = str(getattr(decision, "manner", "") or "")
    if manner == "readout":
        picked = _pick_particular(rows, selector) if selector.particular or selector.tokens() else rows[0]
        if picked is None:
            return ""
        fill_readout(picked)
        return _speak_readout(picked)
    if manner == "digest":
        return _speak_digest(rows)
    if manner in {"particular", "lookup"} or selector.particular:
        picked = _pick_particular(rows, selector)
        if picked is None:
            return ""
        return _speak_particular(picked, selector)
    return _speak_digest(rows)


def shape_mail_payload(payload: dict[str, Any], query: str) -> dict[str, Any]:
    """Keep tool JSON to gist/headlines so the model cannot recite a body."""

    items = payload.get("messages") or payload.get("items") or []
    hits: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            hits.append(_public_mail_hit(item, query))
        elif item:
            hits.append(
                _public_mail_hit({"text": str(item), "memory_type": "mail.envelope.received"}, query)
            )
    spoken = str(payload.get("spoken") or "").strip() or speak_mail(query, hits)
    shaped = dict(payload)
    shaped["messages"] = [
        {
            "text": hit.get("text"),
            "when": hit.get("when"),
            "subject": hit.get("subject"),
            "sender": hit.get("sender"),
            "gist": hit.get("gist"),
        }
        for hit in hits
    ]
    cap = SPOKEN_READOUT_CAP if spoken.lower().startswith("reading it out") else SPOKEN_MAIL_CAP
    shaped["spoken"] = spoken[:cap]
    shaped.pop("items", None)
    return shaped


def _public_mail_hit(item: dict[str, Any], query: str) -> dict[str, Any]:
    fields = mail_fields(item)
    selector = mail_selector(query)
    preview = str(
        item.get("preview")
        or item.get("snippet")
        or item.get("body")
        or item.get("html")
        or ""
    )[:4000]
    gist = fields["gist"] or gist_from_preview(
        preview,
        subject=fields["subject"],
        want=selector.tokens(),
    )
    subject = fields["subject"]
    sender = fields["sender"]
    text = subject
    if subject and sender:
        text = f"{subject} from {sender}"
    elif sender:
        text = sender
    if gist and gist.lower() not in text.lower():
        text = f"{text}. {gist}".strip(" .")
    hit = {
        "id": item.get("id"),
        "when": item.get("when") or item.get("received") or item.get("date"),
        "text": text[:400],
        "subject": subject,
        "sender": sender,
        "gist": gist[:SPOKEN_GIST_CAP + 16],
        "kind": item.get("kind") or "live_mac",
        "memory_type": item.get("memory_type") or "mail.envelope.received",
        "channel": "mail",
        "shelf": "mail",
        "rowid": item.get("rowid"),
    }
    return hit


def _pick_particular(rows: list[dict[str, Any]], selector: MailSelector) -> dict[str, Any] | None:
    tokens = selector.tokens()
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(rows):
        fields = mail_fields(item)
        blob = " ".join(
            part for part in (fields["subject"], fields["sender"], fields["gist"], fields["text"]) if part
        ).lower()
        hits = sum(1 for token in tokens if token and token in blob) if tokens else 1
        if tokens and hits <= 0:
            continue
        scored.append((hits, -index, item))
    if not scored:
        return rows[0]
    scored.sort(reverse=True)
    return scored[0][2]


def _speak_particular(item: dict[str, Any], selector: MailSelector) -> str:
    fields = mail_fields(item)
    sender = display_sender(fields["sender"]) if fields["sender"] else "someone"
    gist = gist_from_preview(
        fields["gist"] or fields["subject"],
        subject=fields["subject"],
        want=selector.tokens(),
    ) or fields["subject"]
    if not gist:
        return ""
    subject = fields["subject"]
    if subject and gist.lower() != subject.lower() and subject.lower() not in gist.lower():
        line = f"Mail from {sender} — {subject}. {gist}"
    else:
        line = f"Mail from {sender}: {gist}"
    if not line.endswith((".", "!", "?")):
        line = line.rstrip(".") + "."
    return line[:SPOKEN_MAIL_CAP]


def _speak_digest(rows: list[dict[str, Any]]) -> str:
    bits: list[str] = []
    seen: set[str] = set()
    for item in rows:
        fields = mail_fields(item)
        sender = display_sender(fields["sender"]) if fields["sender"] else ""
        subject = _cap_words(fields["subject"], 72)
        if not subject and not sender:
            continue
        headline = f"{sender} — {subject}" if sender and subject else (subject or sender)
        key = headline.lower()
        if key in seen:
            continue
        seen.add(key)
        bits.append(headline.rstrip(".") + ".")
        if len(bits) >= DIGEST_HEADLINES:
            break
    if not bits:
        return ""
    return ("Recent mail: " + " ".join(bits))[:SPOKEN_MAIL_CAP]


def _plain_preview(raw: str) -> str:
    text = str(raw or "")
    if "<" in text and ">" in text and re.search(r"</?[a-z]{1,12}\b", text, re.IGNORECASE):
        text = _HTML_TAG.sub(" ", text)
        text = html_lib.unescape(text)
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "--" or stripped.startswith("-- "):
            break
        if _QUOTE_LINE.match(stripped) or stripped.lower().startswith("on ") and " wrote:" in stripped.lower():
            break
        lines.append(stripped)
    return " ".join(lines)


def _email_text(message: Message) -> str:
    if message.is_multipart():
        parts: list[str] = []
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            subtype = (part.get_content_subtype() or "").lower()
            payload = _decoded_part(part)
            if not payload:
                continue
            if subtype == "html" and not parts:
                parts.append(_plain_preview(payload))
            elif subtype == "plain":
                return _plain_preview(payload)
        return " ".join(parts)
    return _plain_preview(_decoded_part(message))


def _decoded_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    if isinstance(payload, str):
        return payload
    return ""


def _cap_words(text: str, cap: int) -> str:
    value = " ".join(str(text or "").split()).strip()
    if len(value) <= cap:
        return value
    clipped = value[: max(1, cap - 1)].rsplit(" ", 1)[0].rstrip(",;:")
    return (clipped or value[: cap - 1]).rstrip() + "…"
