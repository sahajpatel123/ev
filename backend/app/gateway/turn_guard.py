"""Per-turn brain-call guard: dedupe + cap shared by the kernel and agent loops.

Root cause it bounds (0.3): the loops nest — one kernel round can submit an
``agent.run`` that burns up to 24 brain calls of its own, invisibly to the
turn, and a dithering brain can re-issue the same work across rounds. Each
loop's own budget (16 kernel rounds, 24 agent steps) multiplies instead of
adding. The guard gives the whole turn one quota and memoizes identical
requests, so repetition is free and fan-out stops with an honest status
instead of spiraling.

Scope: the guard lives in a contextvar set by the turn funnel
(:func:`app.fabric.facade.arun_kernel_turn`). Background agent runs inherit
it (asyncio copies context at task creation), so nested work consumes the
same quota — by design: a turn's causal tree is bounded as one unit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.contracts import ChatMessage, ChatResult, ToolSpec

logger = logging.getLogger("ev.gateway.turn_guard")


class TurnBudgetExceeded(RuntimeError):
    """The turn's shared brain-call quota is spent."""


def brain_call_key(
    *,
    messages: Sequence[ChatMessage],
    tools: Sequence[ToolSpec] | None,
    model: str | None,
    temperature: float | None,
    effort: str | None,
) -> str:
    """Stable hash of one brain request: messages + tools + params."""

    def _message(message: ChatMessage) -> dict[str, Any]:
        calls = message.tool_calls or []
        return {
            "role": message.role,
            "content": message.content,
            "name": message.name,
            "tool_call_id": message.tool_call_id,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in calls
            ],
            "media": [
                {
                    "kind": part.kind,
                    "sha256": part.sha256,
                    "text": part.text,
                    "ref": part.ref,
                    "size": part.size_bytes,
                }
                for part in (message.media or [])
            ],
        }

    payload = {
        "messages": [_message(message) for message in messages],
        "tools": [
            {"name": spec.name, "parameters": spec.parameters} for spec in (tools or [])
        ],
        "model": model,
        "temperature": temperature,
        "effort": effort,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _copy_result(reply: ChatResult) -> ChatResult:
    return ChatResult(
        text=reply.text,
        tool_calls=list(reply.tool_calls or []),
        usage=dict(reply.usage or {}),
        model=reply.model,
    )


class TurnGuard:
    """One turn's shared brain-call quota and identical-call memo."""

    def __init__(
        self,
        *,
        turn_id: str,
        max_calls: int,
        dedupe_enabled: bool,
    ) -> None:
        self.turn_id = turn_id
        self.max_calls = max(0, int(max_calls))
        self.dedupe_enabled = bool(dedupe_enabled)
        self.calls = 0
        self.dedupe_hits = 0
        self.cap_hit = False
        self._replies: dict[str, ChatResult] = {}
        self._agent_runs: dict[str, dict[str, Any]] = {}

    @property
    def capped(self) -> bool:
        """True when no further provider call may be made on this turn."""

        return self.max_calls > 0 and self.calls >= self.max_calls

    def before_call(self, key: str) -> ChatResult | None:
        """Check the quota; return a memoized reply for an identical request.

        Raises :class:`TurnBudgetExceeded` when the quota is spent. A memo
        hit returns a copy of the first identical call's reply (no provider
        call, no cost); the caller must record zero usage for it.
        """

        if self.dedupe_enabled and key in self._replies:
            self.dedupe_hits += 1
            logger.warning(
                "turn %s deduped identical brain call (hit %d)",
                self.turn_id,
                self.dedupe_hits,
            )
            return _copy_result(self._replies[key])
        if self.capped:
            self.cap_hit = True
            raise TurnBudgetExceeded(
                f"turn {self.turn_id} spent its brain-call quota "
                f"({self.max_calls} calls)"
            )
        return None

    def after_call(self, key: str, reply: ChatResult) -> None:
        """Record one provider call and memoize its reply."""

        self.calls += 1
        if self.dedupe_enabled and key not in self._replies:
            self._replies[key] = _copy_result(reply)

    def check_agent_run(self, goal: str) -> dict[str, Any] | None:
        """A same-turn receipt for an equivalent agent goal, if one ran."""

        return self._agent_runs.get(_normalize_goal(goal))

    def record_agent_run(self, goal: str, receipt: dict[str, Any]) -> None:
        self._agent_runs[_normalize_goal(goal)] = dict(receipt)

    def summary(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "brain_calls": self.calls,
            "dedupe_hits": self.dedupe_hits,
            "cap_hit": self.cap_hit,
            "max_calls": self.max_calls,
        }


def _normalize_goal(goal: str) -> str:
    return " ".join((goal or "").strip().lower().split())


_GUARD: ContextVar[TurnGuard | None] = ContextVar("brain_turn_guard", default=None)


def current_guard() -> TurnGuard | None:
    """The active turn's guard, or None outside a guarded turn."""

    return _GUARD.get()


@contextmanager
def turn_guard(
    turn_id: str | None = None,
    *,
    max_calls: int | None = None,
    dedupe_enabled: bool | None = None,
) -> Iterator[TurnGuard]:
    """Set the turn guard for a funnelled turn (nesting reuses the outer)."""

    outer = _GUARD.get()
    if outer is not None:
        yield outer
        return
    from app.config import settings

    if max_calls is None:
        # 0 disables the cap; never `or` a default over it.
        max_calls = int(getattr(settings, "brain_turn_max_calls", 60))
    if dedupe_enabled is None:
        dedupe_enabled = bool(getattr(settings, "brain_turn_dedupe_enabled", True))
    guard = TurnGuard(
        turn_id=turn_id or f"turn-{uuid.uuid4().hex[:12]}",
        max_calls=max_calls,
        dedupe_enabled=dedupe_enabled,
    )
    token = _GUARD.set(guard)
    try:
        yield guard
    finally:
        _GUARD.reset(token)
