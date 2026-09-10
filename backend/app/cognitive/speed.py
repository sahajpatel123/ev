"""Spoken-speed policy for Muse kernel turns.

Global ``EV_MUSE_SPARK_REASONING_EFFORT`` stays ``high`` for non-kernel Spark
callers. Kernel conversation must not wait on that high-effort chain before
the first spoken word.
"""

from __future__ import annotations

from app.config import settings
from app.contracts import ToolSpec

_EFFORTS = frozenset({"low", "medium", "high"})

# Enough to answer, look, time, weather, mail, and send. The rest of the bus
# is one capability.discover away instead of riding every hello.
CONVERSATION_TOOL_NAMES: tuple[str, ...] = (
    "capability.discover",
    "memory.search",
    "life.mail",
    "life.messages",
    "life.send",
    "people.lookup",
    "look.capture",
    "weather.get",
    "timer.act",
)


def _effort(raw: str | None, fallback: str) -> str:
    value = (raw or "").strip().lower()
    if value in _EFFORTS:
        return value
    value = (fallback or "").strip().lower()
    return value if value in _EFFORTS else "low"


def compact_turn(*, text: str, domain: str, has_work: bool) -> bool:
    """True when this utterance should take the fast Muse path."""

    if has_work:
        return False
    work = (domain or "open").strip().lower()
    if work in {"file", "send", "look", "computer"}:
        return False
    try:
        from app.ev.computer_strategy import looks_like_app_or_web_task

        if looks_like_app_or_web_task(text or ""):
            return False
    except Exception:
        pass
    return len((text or "").split()) < 16


def reasoning_effort(*, domain: str, compact: bool, has_work: bool) -> str:
    work = (domain or "open").strip().lower() in {"file", "send", "look", "computer"} or has_work
    if work and not compact:
        return _effort(
            getattr(settings, "cognitive_work_reasoning_effort", None),
            "medium",
        )
    return _effort(
        getattr(settings, "cognitive_conversation_reasoning_effort", None),
        "low",
    )


def should_prefetch_memory(*, compact: bool) -> bool:
    """Skip serial recall on compact turns — Muse can call memory.search."""

    return not compact


def max_tool_turns(*, compact: bool) -> int:
    if compact:
        bound = int(getattr(settings, "cognitive_conversation_max_tool_turns", 4) or 4)
        return max(1, min(bound, 6))
    bound = int(getattr(settings, "cognitive_max_tool_turns", 12) or 12)
    return max(1, min(bound, 16))


def tool_specs_for_turn(*, compact: bool, expand: bool = False) -> list[ToolSpec]:
    from app.cognitive.capabilities import tool_specs

    specs = tool_specs()
    if expand or not compact:
        return specs
    allow = set(CONVERSATION_TOOL_NAMES)
    return [spec for spec in specs if spec.name in allow]
