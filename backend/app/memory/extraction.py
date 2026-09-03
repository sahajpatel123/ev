from __future__ import annotations

import re

from app.contracts import EntityRef, MemoryCandidate
from app.ev.continuity import FORGET_INTENT, PIN_INTENT, is_hypothetical
from app.memory.entities import extract_entities_from_text
from app.memory.loops import (
    extract_hypothesis_candidate,
    extract_loop_candidates,
    extract_rejection_candidate,
)
from app.memory.temporal import resolve_temporal_expressions
from app.models import Event

_SENTENCE_START = (
    r"(?:^|[.;!?]\s+)"
    r"(?:(?:please )?remember (?:that|this),?\s+)?"
    r"(?:(?:and|but|so|then|also|actually|instead),?\s+)?"
    r"(?:(?:last|this|next|yesterday|today)\s+"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|month|year)\s+|"
    r"(?:in|during|by)\s+[a-z]+\s+)?"
    r"(?:(?:and|but|so|then|also|actually|instead),?\s+)?"
)
_SUBJECT_TAIL = re.compile(
    r"\s+(now|currently|these days|from now on)\.?$",
    re.IGNORECASE,
)

_CLAUSE_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+|\s+(?:and|but|so|then)\s+(?=(?:i|we|my|i'|i'm)\b)",
    re.IGNORECASE,
)

_EPHEMERAL_UI_RE = re.compile(
    r"^(?:element\s+)?(?:e\d+|frame_\d+|window_\d+|s\d+)$",
    re.IGNORECASE,
)


def _is_ephemeral_ui_text(text: str) -> bool:
    blob = (text or "").strip()
    if not blob:
        return False
    if _EPHEMERAL_UI_RE.fullmatch(blob):
        return True
    if re.search(r"\b(prefer|always|whenever)\b", blob, re.I):
        return False
    return bool(re.search(r"\b(?:e\d+|frame_\d+|window_\d+|element_ref|frame_id|snapshot_id)\b", blob, re.I))

DECISION_RE = re.compile(
    _SENTENCE_START
    + r"(?:(?:i|we)\s*(?:'ve\s+|have\s+|had\s+)?)?decided\s+(?:to|that|on)\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
DECISION_IS_RE = re.compile(
    _SENTENCE_START + r"(?:my|our)\s+decision\s+(?:is|was)\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
DIRECTION_RE = re.compile(
    _SENTENCE_START
    + r"(?:we(?:'re| are) going with|let's keep|we'll (?:use|keep))\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)

PREFERENCE_RE = re.compile(
    _SENTENCE_START
    + r"i\s+(?:really\s+|definitely\s+|kind of\s+|sort of\s+)?"
    r"(prefer|like|love|enjoy|hate|dislike)\s+(.+?)(?:\s+over\s+(.+?))?(?:\.\s*)?$",
    re.IGNORECASE,
)
DISLIKE_RE = re.compile(
    _SENTENCE_START + r"i\s+(?:really\s+)?(?:don't|do not|no longer)\s+like\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
RATHER_RE = re.compile(
    _SENTENCE_START + r"i'?d\s+rather\s+(.+?)\s+than\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
PREFER_NO_PRONOUN_RE = re.compile(
    _SENTENCE_START + r"prefer\s+(.+?)(?:\s+over\s+(.+?))?(?:\.\s*)?$",
    re.IGNORECASE,
)
SWITCHED_RE = re.compile(
    _SENTENCE_START + r"i(?:'ve| have)\s+switched\s+to\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)


def _clean_subject(text: str) -> str:
    return _SUBJECT_TAIL.sub("", (text or "").strip()).strip()

GOAL_RE = re.compile(
    _SENTENCE_START
    + r"(?:(?:i|we)\s+(?:want to|need to|would like to|plan to|aim to|hope to|should|'?d\s+like to)"
    r"|i'?m\s+(?:planning to|going to|hoping to))\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
GOAL_IS_RE = re.compile(
    _SENTENCE_START + r"(?:my|our)\s+goal\s+(?:is|was)\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
GOAL_COLON_RE = re.compile(
    _SENTENCE_START + r"goal\s*:\s*(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)

FACT_PROPS = re.compile(
    _SENTENCE_START
    + r"my\s+(?:fav(?:orite)?\s+)?(name|age|birthday|job|role|title|company|employer|city|address|phone|email|partner|relationship)"
    r"\s+(?:[A-Z][a-z]+\s+)?(?:is|was)\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
FACT_AT_RE = re.compile(
    _SENTENCE_START
    + r"i\s+(?:work|live|study|am based|moved)\s+(?:at|in|for|to)\s+(.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
FACT_AGE_RE = re.compile(
    _SENTENCE_START + r"i'?m\s+(\d{1,3})\s+(?:years?\s+)?old(?:\.\s*)?$",
    re.IGNORECASE,
)
CALLING_RE = re.compile(
    _SENTENCE_START
    + r"i(?:'m| am) calling (?:this|it)"
    r"(?:\s+(?P<kind>experiment|project|feature|system|thing|one))?\s+"
    r"(?P<value>.+?)(?:\s+now)?(?:\.\s*)?$",
    re.IGNORECASE,
)
IS_CALLED_RE = re.compile(
    _SENTENCE_START
    + r"(?:the\s+)?(?P<subject>.{3,80}?)\s+is called\s+(?P<value>.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
NAME_IS_RE = re.compile(
    _SENTENCE_START + r"(?:the )?name is\s+(?P<value>.+?)(?:\.\s*)?$",
    re.IGNORECASE,
)
CALL_IT_RE = re.compile(
    _SENTENCE_START
    + r"call (?:this|it)\s+(?P<value>.+?)(?:\s+now)?(?:\.\s*)?$",
    re.IGNORECASE,
)
PERSON_HELP_RE = re.compile(
    _SENTENCE_START
    + r"(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+is (?:the )?(?:person|someone) "
    r"(?:who )?(?:helping|working|assisting)\b(?P<rest>.*)$",
)
CODE_IS_RE = re.compile(
    _SENTENCE_START + r"(?:the )?(?:test )?code is\s+(?P<value>\d{3,8})(?:\.\s*)?$",
    re.IGNORECASE,
)
_NEGATED_NAMING = re.compile(
    r"\b(?:i(?:'m| am) not calling|not calling (?:it|this)|"
    r"(?:don't|do not) call (?:it|this)|isn't called|is not called)\b",
    re.IGNORECASE,
)
_QUOTED_OTHER = re.compile(
    r"\b(?:said|says|told me)\b.{0,60}['\"]",
    re.IGNORECASE,
)
_LABEL_KINDS = {
    "experiment": "experiment",
    "project": "experiment",
    "feature": "experiment",
    "system": "experiment",
    "thing": "experiment",
    "one": "experiment",
}


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
        temporal = [item.to_dict() for item in resolve_temporal_expressions(text, event.occurred_at)]
        candidates: list[MemoryCandidate] = []

        # Assistant replies are already captured as events; don't derive memories from them.
        if event.event_type == "message.assistant":
            return candidates

        from app.memory.live_life import is_live_life_event

        # iMessage / Mail / Contacts stay on the on-demand locator. Promoting
        # them to general memories would dump the firehose into casual turns.
        if is_live_life_event(event):
            return candidates

        from app.ev.laptop_files import is_system_confirmation
        from app.memory.visual import is_memory_hedge_scene

        if is_system_confirmation(text) or is_memory_hedge_scene(text):
            return candidates

        # The Event row is the timeline. Do not promote every utterance to a
        # semantic memory. Hypotheticals and questions are not owner facts.
        if is_hypothetical(text) or FORGET_INTENT.search(text or ""):
            return candidates

        pinned = bool(PIN_INTENT.search(text or ""))

        # Typed extraction runs per sentence so multi-sentence captures yield
        # one memory per statement instead of only the final sentence.
        sentences = [
            sentence.strip()
            for sentence in _CLAUSE_SPLIT_RE.split(text)
            if sentence.strip()
        ]
        typed_found = False
        for sentence in sentences:
            if sentence.endswith("?") or is_hypothetical(sentence) or _is_ephemeral_ui_text(sentence):
                continue
            decision = self._decision(sentence)
            if decision:
                typed_found = True
                candidates.append(
                    MemoryCandidate(
                        memory_type="decision",
                        text=f"Decided: {decision}",
                        payload={
                            "decision": decision,
                            "topic": _topic(decision, entities),
                            "evidence_type": "owner_asserted",
                            "source_event_ids": [str(event.id)],
                            **({"temporal": temporal} if temporal else {}),
                        },
                        importance=1.0 if pinned else 0.95,
                        confidence=0.95,
                        source_type="explicit",
                        privacy_level=event.privacy_level,
                        event_time=event.occurred_at,
                        entities=entities,
                    )
                )

            preference = self._preference(sentence)
            if preference:
                typed_found = True
                payload = {**preference, **({"temporal": temporal} if temporal else {})}
                candidates.append(
                    MemoryCandidate(
                        memory_type="preference",
                        text=f"Preference: {preference['subject']} — {preference['value']}"
                        + (f" over {preference['over']}" if preference.get("over") else ""),
                        payload=payload,
                        importance=min(1.0, 0.95 if pinned else 0.8),
                        confidence=0.9,
                        source_type="explicit",
                        privacy_level=event.privacy_level,
                        event_time=event.occurred_at,
                        entities=entities,
                    )
                )

            goal = self._goal(sentence)
            if goal:
                typed_found = True
                candidates.append(
                    MemoryCandidate(
                        memory_type="goal",
                        text=f"Goal: {goal}",
                        payload={
                            "goal": goal,
                            "topic": _topic(goal, entities),
                            "status": "active",
                            **({"temporal": temporal} if temporal else {}),
                        },
                        importance=min(1.0, 0.95 if pinned else 0.9),
                        confidence=0.9,
                        source_type="explicit",
                        privacy_level=event.privacy_level,
                        event_time=event.occurred_at,
                        entities=entities,
                    )
                )

            fact = self._fact(sentence)
            if fact:
                typed_found = True
                candidates.append(
                    MemoryCandidate(
                        memory_type="fact",
                        text=f"{fact['subject']}: {fact['property']} = {fact['value']}",
                        payload={**fact, **({"temporal": temporal} if temporal else {})},
                        importance=min(1.0, 0.95 if pinned else 0.85),
                        confidence=0.95,
                        source_type="explicit",
                        privacy_level=event.privacy_level,
                        event_time=event.occurred_at,
                        entities=entities,
                    )
                )

            label = self._owner_label(sentence)
            if label:
                typed_found = True
                label_entities = list(entities)
                if label.get("value"):
                    label_entities = [
                        *label_entities,
                        EntityRef(name=str(label["value"])[:80], entity_type="project"),
                    ]
                candidates.append(
                    MemoryCandidate(
                        memory_type="fact",
                        text=f"{label['subject']}: {label['property']} = {label['value']}",
                        payload={
                            **label,
                            "owner_asserted": True,
                            **({"temporal": temporal} if temporal else {}),
                        },
                        importance=1.0 if pinned else 0.92,
                        confidence=0.96,
                        source_type="explicit",
                        privacy_level=event.privacy_level,
                        event_time=event.occurred_at,
                        entities=label_entities,
                    )
                )

            for loop in extract_loop_candidates(event, sentence, entities):
                typed_found = True
                candidates.append(loop)
            rejection = extract_rejection_candidate(event, sentence, entities)
            if rejection:
                typed_found = True
                candidates.append(rejection)
            hypothesis = extract_hypothesis_candidate(event, sentence, entities)
            if hypothesis:
                typed_found = True
                candidates.append(hypothesis)

        # Inferred observation when the user shared something unstructured.
        from app.memory.visual import wants_keep_visible

        if (
            text
            and not typed_found
            and len(text.strip()) >= 20
            and not text.strip().endswith("?")
            and not is_hypothetical(text)
            and not _is_ephemeral_ui_text(text)
            and not wants_keep_visible(text)
        ):
            candidates.append(
                MemoryCandidate(
                    memory_type="observation",
                    text=f"Observed: {text}",
                    payload={
                        "statement": text,
                        "topic": _topic(text, entities),
                        **({"temporal": temporal} if temporal else {}),
                    },
                    importance=0.55 if pinned else 0.4,
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
        for pattern in (DECISION_RE, DECISION_IS_RE, DIRECTION_RE):
            match = pattern.search(text)
            if match:
                return match.group(1).strip().rstrip(".")
        return None

    def _preference(self, text: str) -> dict | None:
        correction = bool(re.search(r"\b(actually|instead|switched)\b", text, re.IGNORECASE))
        match = SWITCHED_RE.search(text)
        if match:
            return {
                "subject": _clean_subject(match.group(1)),
                "value": "prefer",
                "over": None,
                "context": None,
                "replaces_latest": True,
            }
        match = DISLIKE_RE.search(text)
        if match:
            return {
                "subject": _clean_subject(match.group(1)),
                "value": "dislike",
                "over": None,
                "context": None,
                "replaces_latest": correction,
            }
        match = RATHER_RE.search(text)
        if match:
            return {
                "subject": _clean_subject(match.group(1)),
                "value": "rather",
                "over": _clean_subject(match.group(2)),
                "context": None,
                "replaces_latest": correction,
            }
        match = PREFERENCE_RE.search(text)
        if not match:
            match = PREFER_NO_PRONOUN_RE.search(text)
            if not match:
                return None
            return {
                "subject": _clean_subject(match.group(1)),
                "value": "prefer",
                "over": _clean_subject(match.group(2)) if match.group(2) else None,
                "context": None,
                "replaces_latest": correction,
            }
        verb, subject, over = match.group(1), match.group(2).strip(), match.group(3)
        if _is_ephemeral_ui_text(subject):
            return None
        return {
            "subject": _clean_subject(subject),
            "value": verb,
            "over": _clean_subject(over) if over else None,
            "context": None,
            "replaces_latest": correction,
        }

    def _goal(self, text: str) -> str | None:
        for pattern in (GOAL_RE, GOAL_IS_RE, GOAL_COLON_RE):
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
        match = FACT_AGE_RE.search(text)
        if match:
            return {"subject": "I", "property": "age", "value": match.group(1).strip()}
        return None

    def _owner_label(self, text: str) -> dict | None:
        if _NEGATED_NAMING.search(text) or _QUOTED_OTHER.search(text):
            return None
        match = CALLING_RE.search(text)
        if match:
            kind = (match.group("kind") or "").strip().lower()
            value = _clean_label_value(match.group("value"))
            if value and (kind or _looks_like_label(value)):
                return {
                    "subject": _LABEL_KINDS.get(kind, "experiment"),
                    "property": "name",
                    "value": value,
                }
        match = IS_CALLED_RE.search(text)
        if match:
            value = _clean_label_value(match.group("value"))
            if value and _looks_like_label(value):
                return {"subject": "experiment", "property": "name", "value": value}
        match = NAME_IS_RE.search(text)
        if match:
            value = _clean_label_value(match.group("value"))
            if value and _looks_like_label(value):
                return {"subject": "experiment", "property": "name", "value": value}
        match = CALL_IT_RE.search(text)
        if match:
            value = _clean_label_value(match.group("value"))
            if value and _looks_like_label(value):
                return {"subject": "experiment", "property": "name", "value": value}
        match = PERSON_HELP_RE.search(text)
        if match:
            name = match.group("name").strip()
            rest = (match.group("rest") or "").strip().rstrip(".")
            return {
                "subject": name,
                "property": "role",
                "value": ("helping " + rest).strip() if rest else "person",
            }
        match = CODE_IS_RE.search(text)
        if match:
            return {"subject": "test_code", "property": "value", "value": match.group("value")}
        return None


def _clean_label_value(value: str | None) -> str:
    text = (value or "").strip().rstrip(".").strip()
    text = re.sub(r"^(?:this|it|the)\s+", "", text, flags=re.IGNORECASE)
    return text[:120]


def _looks_like_label(value: str) -> bool:
    text = (value or "").strip()
    if not text or len(text) > 80:
        return False
    if re.match(r"^project\b", text, re.IGNORECASE):
        return True
    if re.match(r"^\d{3,8}$", text):
        return True
    return bool(re.match(r"^[A-Z][a-zA-Z0-9']+(?:\s+[A-Z][a-zA-Z0-9']+)+$", text))
