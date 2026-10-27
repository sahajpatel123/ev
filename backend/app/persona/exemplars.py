"""Exemplar bank (spec §9): rhythm and restraint, never content.

Seed shapes come from the spec; facts stay {} placeholders. Selection is
tag-based, deterministic (score desc, id asc), capped at 2, and must fit
the token budget handed down by compile.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.persona import tokens
from app.persona.context import PersonaContext, ToneDecision
from app.persona.reactions import hinglish, topic_of

EXEMPLARS_PATH = Path(__file__).with_name("exemplars.yaml")
MAX_EXEMPLARS = 2


@dataclass(frozen=True)
class Exemplar:
    id: str
    tags: tuple[str, ...]
    roast: int
    channels: tuple[str, ...]
    owner: str
    evie: str

    def rendered(self) -> str:
        owner_line = f"Owner: {self.owner}\n" if self.owner else ""
        return (
            f"[exemplar · {', '.join(self.tags)} · ROAST={self.roast}]\n"
            f"{owner_line}Evie: {self.evie}"
        )

    def estimate(self) -> int:
        return tokens.estimate(self.rendered())


@lru_cache(maxsize=1)
def load() -> tuple[Exemplar, ...]:
    """Load the seed set (cached; the file never changes at runtime)."""
    raw = yaml.safe_load(EXEMPLARS_PATH.read_text(encoding="utf-8")) or []
    exemplars = []
    for entry in raw:
        exemplars.append(
            Exemplar(
                id=str(entry["id"]),
                tags=tuple(str(tag) for tag in entry.get("tags", [])),
                roast=int(entry.get("roast", 0)),
                channels=tuple(str(c) for c in entry.get("channels", ["text"])),
                owner=str(entry.get("owner", "")),
                evie=str(entry.get("evie", "")),
            )
        )
    return tuple(sorted(exemplars, key=lambda item: item.id))


def _wanted_tags(ctx: PersonaContext) -> set[str]:
    tags: set[str] = set()
    topic = topic_of(ctx.owner_msg)
    if topic != "general":
        tags.add(topic)
        if topic == "code":
            tags.add("coding")
    intent = (ctx.intent or "").strip().lower()
    if intent in ("decision", "question", "reflection"):
        tags.add("opinion")
    if intent == "life_action":
        tags.add("confirmation")
    emotion = (ctx.emotion or "").strip().lower()
    if emotion in ("tired",):
        tags.add("late-night")
        tags.add("fatigue")
    if emotion in ("stressed", "frustrated", "sad"):
        tags.add("error")
    if ctx.confirmation_pending:
        tags.add("confirmation")
    if ctx.sensitive_topic:
        tags.add("candor")
    if hinglish(ctx.owner_msg):
        tags.add("hinglish")
    return tags


def select(
    ctx: PersonaContext,
    decision: ToneDecision,
    budget_tokens: int,
) -> list[Exemplar]:
    """Pick up to 2 exemplars that fit the decision and the budget."""
    if budget_tokens <= 0 or decision.audience == "on_behalf":
        return []
    wanted = _wanted_tags(ctx)
    owner_is_hinglish = hinglish(ctx.owner_msg)
    scored: list[tuple[int, str, Exemplar]] = []
    for exemplar in load():
        if ctx.channel not in exemplar.channels:
            continue
        if exemplar.roast > decision.roast:
            continue
        if decision.roast == 0 and exemplar.roast != 0:
            continue
        if "hinglish" in exemplar.tags and not owner_is_hinglish:
            continue
        score = len(wanted & set(exemplar.tags))
        if decision.roast >= 2 and exemplar.roast == 2:
            score += 1
        scored.append((score, exemplar.id, exemplar))
    scored.sort(key=lambda item: (-item[0], item[1]))
    picked: list[Exemplar] = []
    spent = 0
    for _, _, exemplar in scored:
        if len(picked) >= MAX_EXEMPLARS:
            break
        cost = exemplar.estimate()
        if spent + cost <= budget_tokens:
            picked.append(exemplar)
            spent += cost
    return picked


def describe() -> dict[str, Any]:
    """JSON-safe inventory for reports (ids, tags, roast, sizes)."""
    return {
        "count": len(load()),
        "exemplars": [
            {
                "id": item.id,
                "tags": list(item.tags),
                "roast": item.roast,
                "tokens": item.estimate(),
            }
            for item in load()
        ],
    }
