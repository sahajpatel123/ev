from __future__ import annotations

import re

from app.contracts import EntityRef, MemoryCandidate
from app.memory.entities import extract_entities_from_text
from app.models import Event

DECISION_RE = re.compile(
    r"(?:i|we|my)\s+(?:have|had)?\s*decided\s+(?:to|that|on)\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
DECISION_IS_RE = re.compile(r"(?:my|our)\s+decision\s+(?:is|was)\s+(.+?)(?:\.\s*)?$", re.IGNORECASE)

PREFERENCE_RE = re.compile(
    r"i\s+(?:really\s+|definitely\s+|kind of\s+|sort of\s+)?(prefer|like|love|enjoy|hate|dislike)\s+(.+?)(?:\s+over\s+(.+?))?(?:\.\s*)?$",
    re.IGNORECASE,
)
DISLIKE_RE = re.compile(r"i\s+(?:really\s+)?(?:don't|do not|no longer)\s+like\s+(.+?)(?:\.\s*)?$", re.IGNORECASE)

GOAL_RE = re.compile(
    r"(?:i|we)\s+(?:want to|need to|would like to|plan to|aim to|hope to|should)\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
GOAL_IS_RE = re.compile(r"(?:my|our)\s+goal\s+(?:is|was)\s+(.+?)(?:\.\s*)?$", re.IGNORECASE)

FACT_PROPS = re.compile(
    r"my\s+(name|age|birthday|job|role|title|company|employer|city|address|phone|email|partner|relationship)\s+(?:is|was)\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
FACT_AT_RE = re.compile(r"i\s+(?:work|live|study|am based)\s+(?:at|in|for)\s+(.+?)(?:\.\s*)?$", re.IGNORECASE)


def _topic(text: str, entities: list[EntityRef]) -> str | None:
    topics = [e.name for e in entities if e.entity_type in ("topic", "project")]
    if topics:
        return topics[0]
    words = re.findall(r"[A-Za-z][A-Za-z0-9']{2,}", text)
    return " ".join(words[:3]) if words else None


class Extractor:
    """Rule-based extraction from raw events into memory candidates."""

    def extract(self, event: Event) -> list[MemoryCandidate]:
        text = (event.content or {}).get("text") or ""
        entities = extract_entities_from_text(text)
        candidates: list[MemoryCandidate] = []

        # Assistant replies are already captured as events; don't derive memories from them.
        if event.event_type == "message.assistant":
            return candidates

        if text:
            candidates.append(
                MemoryCandidate(
                    memory_type="episodic",
                    text=text,
                    payload={"summary": text},
                    importance=0.6,
                    confidence=1.0,
                    source_type="explicit",
                    privacy_level=event.privacy_level,
                    event_time=event.occurred_at,
                    entities=entities,
                )
            )

        decision = self._decision(text)
        if decision:
            candidates.append(
                MemoryCandidate(
                    memory_type="decision",
                    text=f"Decided: {decision}",
                    payload={"decision": decision, "topic": _topic(decision, entities)},
                    importance=1.0,
                    confidence=0.95,
                    source_type="explicit",
                    privacy_level=event.privacy_level,
                    event_time=event.occurred_at,
                    entities=entities,
                )
            )

        preference = self._preference(text)
        if preference:
            candidates.append(
                MemoryCandidate(
                    memory_type="preference",
                    text=f"Preference: {preference['subject']} — {preference['value']}"
                    + (f" over {preference['over']}" if preference.get("over") else ""),
                    payload=preference,
                    importance=0.8,
                    confidence=0.9,
                    source_type="explicit",
                    privacy_level=event.privacy_level,
                    event_time=event.occurred_at,
                    entities=entities,
                )
            )

        goal = self._goal(text)
        if goal:
            candidates.append(
                MemoryCandidate(
                    memory_type="goal",
                    text=f"Goal: {goal}",
                    payload={"goal": goal, "topic": _topic(goal, entities), "status": "active"},
                    importance=0.9,
                    confidence=0.9,
                    source_type="explicit",
                    privacy_level=event.privacy_level,
                    event_time=event.occurred_at,
                    entities=entities,
                )
            )

        fact = self._fact(text)
        if fact:
            candidates.append(
                MemoryCandidate(
                    memory_type="fact",
                    text=f"{fact['subject']}: {fact['property']} = {fact['value']}",
                    payload=fact,
                    importance=0.85,
                    confidence=0.95,
                    source_type="explicit",
                    privacy_level=event.privacy_level,
                    event_time=event.occurred_at,
                    entities=entities,
                )
            )

        # Inferred observation when the user shared something unstructured.
        if text and not (decision or preference or goal or fact) and len(text.strip()) >= 10:
            candidates.append(
                MemoryCandidate(
                    memory_type="observation",
                    text=f"Observed: {text}",
                    payload={"statement": text, "topic": _topic(text, entities)},
                    importance=0.4,
                    confidence=0.6,
                    source_type="inferred",
                    privacy_level=event.privacy_level,
                    event_time=event.occurred_at,
                    entities=entities,
                )
            )

        # Attachments without text still produce an episodic memory.
        if not text and event.event_type in ("image", "file", "voice", "share", "attachment"):
            filename = (event.content or {}).get("filename") or event.event_type
            candidates.append(
                MemoryCandidate(
                    memory_type="episodic",
                    text=f"Captured {event.event_type}: {filename}",
                    payload={"summary": f"Captured {event.event_type}: {filename}", "attachment": event.content},
                    importance=0.5,
                    confidence=0.9,
                    source_type="explicit",
                    privacy_level=event.privacy_level,
                    event_time=event.occurred_at,
                )
            )

        return candidates

    def _decision(self, text: str) -> str | None:
        for pattern in (DECISION_RE, DECISION_IS_RE):
            match = pattern.search(text)
            if match:
                return match.group(1).strip().rstrip(".")
        return None

    def _preference(self, text: str) -> dict | None:
        match = DISLIKE_RE.search(text)
        if match:
            return {"subject": match.group(1).strip(), "value": "dislike", "over": None, "context": None}
        match = PREFERENCE_RE.search(text)
        if not match:
            return None
        verb, subject, over = match.group(1), match.group(2).strip(), match.group(3)
        return {
            "subject": subject,
            "value": verb,
            "over": over.strip() if over else None,
            "context": None,
        }

    def _goal(self, text: str) -> str | None:
        for pattern in (GOAL_RE, GOAL_IS_RE):
            match = pattern.search(text)
            if match:
                return match.group(1).strip().rstrip(".")
        return None

    def _fact(self, text: str) -> dict | None:
        match = FACT_PROPS.search(text)
        if match:
            return {"subject": "my", "property": match.group(1), "value": match.group(2).strip()}
        match = FACT_AT_RE.search(text)
        if match:
            return {"subject": "I", "property": "location", "value": match.group(1).strip()}
        return None

