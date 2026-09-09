"""WAITING ON — first-class Presence-adjacent semantics with provenance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.digital.types import WaitingDirection


@dataclass
class WaitingItem:
    id: str
    direction: WaitingDirection
    person: str
    what: str
    channel: str | None
    when_due: str | None
    source_ref: dict[str, Any]
    confidence: float
    state: str = "open"  # open | resolved | expired
    contract_id: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def as_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "direction": self.direction.value,
            "person": self.person,
            "what": self.what,
            "channel": self.channel,
            "when_due": self.when_due,
            "source_ref": self.source_ref,
            "confidence": self.confidence,
            "state": self.state,
            "contract_id": self.contract_id,
        }


class WaitingStore:
    def __init__(self) -> None:
        self.items: dict[str, WaitingItem] = {}

    def add(self, item: WaitingItem) -> WaitingItem:
        self.items[item.id] = item
        return item

    def waiting_on(self) -> list[WaitingItem]:
        return [i for i in self.items.values() if i.state == "open" and i.direction != WaitingDirection.ON_OWNER]

    def waiting_on_owner(self) -> list[WaitingItem]:
        return [i for i in self.items.values() if i.state == "open" and i.direction == WaitingDirection.ON_OWNER]

    def resolve_from_message(
        self,
        *,
        person: str | None,
        channel: str | None,
        text: str,
        source_ref: dict[str, Any],
    ) -> list[WaitingItem]:
        resolved: list[WaitingItem] = []
        blob = (text or "").lower()
        for item in list(self.items.values()):
            if item.state != "open":
                continue
            person_ok = not person or person.lower() in item.person.lower() or item.person.lower() in (person or "").lower()
            channel_ok = not channel or not item.channel or item.channel.upper() == channel.upper()
            what_ok = any(tok in blob for tok in _tokens(item.what) if len(tok) > 3)
            if person_ok and channel_ok and (what_ok or "attached" in blob or "please find" in blob or "quotation" in blob):
                item.state = "resolved"
                item.evidence.append({"at": datetime.now(UTC).isoformat(), "source_ref": source_ref, "text": text[:200]})
                resolved.append(item)
        return resolved

    async def record(
        self,
        *,
        kind: str,
        person_label: str,
        what: str,
        channel: str | None,
        source_ref: Any,
        evidence: str = "",
        confidence: float = 0.8,
        contract_id: str | None = None,
    ) -> WaitingItem:
        direction = WaitingDirection.ON_OWNER if "ON_ME" in kind or kind.endswith("ON_OWNER") else WaitingDirection.ON_PERSON
        ref = source_ref if isinstance(source_ref, dict) else {"ref": str(source_ref or "")}
        if evidence:
            ref = {**ref, "evidence": evidence[:280]}
        item = new_waiting(
            direction=direction,
            person=person_label,
            what=what,
            channel=channel,
            when_due=None,
            source_ref=ref,
            confidence=confidence,
            contract_id=contract_id,
        )
        return self.add(item)

    async def who_am_i_waiting_on(self) -> dict[str, Any]:
        items = self.waiting_on()
        return {"count": len(items), "items": [i.as_public() for i in items]}

    async def who_is_waiting_on_owner(self) -> dict[str, Any]:
        items = self.waiting_on_owner()
        return {"count": len(items), "items": [i.as_public() for i in items]}

    async def resolve_from_event(self, event: dict[str, Any]) -> list[str]:
        resolved = self.resolve_from_message(
            person=str(event.get("from") or event.get("person") or "") or None,
            channel=str(event.get("service") or event.get("channel") or "") or None,
            text=str(event.get("text") or event.get("snippet") or ""),
            source_ref={"id": event.get("id"), "service": event.get("service")},
        )
        return [i.id for i in resolved]


def new_waiting(
    *,
    direction: WaitingDirection,
    person: str,
    what: str,
    channel: str | None,
    when_due: str | None,
    source_ref: dict[str, Any],
    confidence: float,
    contract_id: str | None = None,
) -> WaitingItem:
    return WaitingItem(
        id=str(uuid4()),
        direction=direction,
        person=person,
        what=what,
        channel=channel,
        when_due=when_due,
        source_ref=source_ref,
        confidence=confidence,
        contract_id=contract_id,
    )


def owner_brief(store: WaitingStore) -> dict[str, Any]:
    on = [i.as_public() for i in store.waiting_on()]
    me = [i.as_public() for i in store.waiting_on_owner()]
    return {
        "waiting_on": on,
        "waiting_on_owner": me,
        "spoken": _speak(on, me),
    }


def _speak(on: list[dict[str, Any]], me: list[dict[str, Any]]) -> str:
    bits = []
    if on:
        bits.append(
            "Waiting on: "
            + "; ".join(f"{i['person']} → {i['what']}" + (f" ({i['channel']})" if i.get("channel") else "") for i in on[:6])
        )
    else:
        bits.append("You are not waiting on anyone recorded.")
    if me:
        bits.append(
            "Waiting on you: "
            + "; ".join(f"{i['person']} → {i['what']}" for i in me[:6])
        )
    return " ".join(bits)


def _tokens(text: str) -> list[str]:
    return [t for t in (text or "").lower().replace(",", " ").split() if t]


# Process-local store for hermetic tests + single-process Home Station.
# Durable rows also live in digital_waiting when a session is present.
GLOBAL_WAITING = WaitingStore()
