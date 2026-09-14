"""High-confidence semantic extraction from external messages.

Does not silently create durable commitments from casual language.
Does not convert every email into Memory/Goals.
"""

from __future__ import annotations

import re
from typing import Any

from app.digital.taint import scan_injection

_REQUEST = re.compile(
    r"(?i)\b(?:please (?:send|share|forward|confirm|review|sign)|can you (?:send|share|confirm)|could you)\b"
)
_COMMIT = re.compile(
    r"(?i)\b(?:i(?:'|’)ll (?:send|check|get back|confirm|share)|i will (?:send|check|get back)|we(?:'|’)ll send)\b"
)
_DEADLINE = re.compile(r"(?i)\b(?:by (?:tomorrow|monday|tuesday|wednesday|thursday|friday|tonight)|deadline|due (?:on|by))\b")
_MEETING = re.compile(r"(?i)\b(?:(?:can we|shall we|let(?:'|’)s) meet|meeting (?:on|at)|wednesday at|thursday at|zoom|invite)\b")
_DECISION = re.compile(r"(?i)\b(?:we(?:'|’)ve decided|approved|rejected|going with|final (?:price|quote|decision))\b")
_DOC = re.compile(r"(?i)\b(?:attached|please find attached|invoice|quotation|pdf)\b")
_WAITING_RESOLVED = re.compile(r"(?i)\b(?:as promised|here (?:is|it is)|please find the (?:file|quote|quotation))\b")

_WEAK = re.compile(r"(?i)\b(?:maybe|might|not sure|if i can|we(?:'|’)ll see)\b")


def extract_email_events(message: dict[str, Any]) -> list[dict[str, Any]]:
    text = " ".join(
        str(message.get(k) or "") for k in ("subject", "text", "snippet", "body")
    )
    inj = scan_injection(text)
    events: list[dict[str, Any]] = []
    base = {
        "source_message_id": message.get("id"),
        "thread_id": message.get("thread_id"),
        "sender": message.get("from") or message.get("sender"),
        "timestamp": message.get("date") or message.get("internal_date"),
        "origin": "EXTERNAL_CONTENT",
        "authority": "DATA",
        "potential_external_instruction": inj["potential_external_instruction"],
    }
    if inj["potential_external_instruction"]:
        events.append({**base, "kind": "POTENTIAL_EXTERNAL_INSTRUCTION", "confidence": 0.95, "evidence": text[:240]})
        # Still extract other semantics; never execute the instruction.
    if _MEETING.search(text):
        events.append({**base, "kind": "MEETING_PROPOSAL", "confidence": 0.82, "evidence": _clip(_MEETING, text)})
    if _DEADLINE.search(text):
        events.append({**base, "kind": "DEADLINE", "confidence": 0.8, "evidence": _clip(_DEADLINE, text)})
    if _DOC.search(text) or message.get("attachments"):
        events.append({**base, "kind": "DOCUMENT_RECEIVED", "confidence": 0.85, "evidence": _clip(_DOC, text) or "attachment"})
    if _REQUEST.search(text):
        events.append({**base, "kind": "REQUEST", "confidence": 0.8, "evidence": _clip(_REQUEST, text)})
    if _COMMIT.search(text) and not _WEAK.search(text):
        from_me = bool(message.get("from_me"))
        kind = "COMMITMENT_BY_OWNER" if from_me else "COMMITMENT_TO_OWNER"
        events.append({**base, "kind": kind, "confidence": 0.78, "evidence": _clip(_COMMIT, text)})
    if _DECISION.search(text):
        events.append({**base, "kind": "DECISION", "confidence": 0.76, "evidence": _clip(_DECISION, text)})
    if _WAITING_RESOLVED.search(text):
        events.append({**base, "kind": "WAITING_RESOLVED", "confidence": 0.74, "evidence": _clip(_WAITING_RESOLVED, text)})
    if re.search(r"(?i)\b(important|urgent|asap)\b", text) and not events:
        events.append({**base, "kind": "IMPORTANT_CHANGE", "confidence": 0.6, "evidence": "urgent/important marker"})
    return events


def extract_commitments(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidates only. Weak/casual language does not become durable."""
    text = str(message.get("text") or message.get("snippet") or "")
    if _WEAK.search(text) and not _COMMIT.search(text):
        return []
    events = [e for e in extract_email_events(message) if e["kind"] in {"COMMITMENT_TO_OWNER", "COMMITMENT_BY_OWNER", "REQUEST"}]
    out = []
    for e in events:
        if e["confidence"] < 0.75:
            continue
        out.append(
            {
                "speaker": e.get("sender") if e["kind"] != "COMMITMENT_BY_OWNER" else "owner",
                "recipient": "owner" if e["kind"] == "COMMITMENT_TO_OWNER" else e.get("sender"),
                "what": e.get("evidence"),
                "when": None,
                "source_ref": {"service": "message", "id": e.get("source_message_id"), "thread_id": e.get("thread_id")},
                "confidence": e["confidence"],
                "candidate": True,
                "durable": False,
            }
        )
    return out


def _clip(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    if not m:
        return ""
    start = max(0, m.start() - 40)
    end = min(len(text), m.end() + 80)
    return text[start:end].strip()
