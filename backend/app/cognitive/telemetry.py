"""Cognitive OS V2 counters. No hidden reasoning, no owner secrets."""

from __future__ import annotations

import threading
import time
from typing import Any

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "muse_turns": 0,
    "muse_tool_turns": 0,
    "muse_tool_calls": 0,
    "deterministic_reflex_turns": 0,
    "legacy_general_model_calls": 0,
    "gpt_realtime_semantic_decisions": 0,
    "gpt_realtime_tool_decisions": 0,
    "spark_judge": 0,
    "luna_turns": 0,
    "grok_turns": 0,
    "deepseek_turns": 0,
    "provider_failures": 0,
    "provider_timeouts": 0,
    "provider_429": 0,
    "circuit_opens": 0,
    "replans": 0,
    "goal_contracts_created": 0,
    "background_executions": 0,
    "foreground_required": 0,
    "false_completions": 0,
    "duplicate_effects": 0,
    "stale_mutations_blocked": 0,
    "unavailable": 0,
    "last_transcript_to_muse_ms": None,
    "last_muse_to_speech_ms": None,
    "last_error": "",
    "last_turn_kind": "",
}


def reset_for_tests() -> None:
    with _LOCK:
        for key, value in list(_STATE.items()):
            if isinstance(value, int):
                _STATE[key] = 0
            elif isinstance(value, float) or value is None:
                _STATE[key] = None
            else:
                _STATE[key] = ""


def snapshot() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE)


def note(**kwargs: Any) -> None:
    with _LOCK:
        for key, value in kwargs.items():
            if key not in _STATE:
                continue
            if isinstance(_STATE[key], int) and isinstance(value, int) and key.startswith(
                (
                    "muse_",
                    "deterministic_",
                    "legacy_",
                    "gpt_",
                    "spark_",
                    "luna_",
                    "grok_",
                    "deepseek_",
                    "provider_",
                    "replans",
                    "goal_",
                    "background_",
                    "foreground_",
                    "false_",
                    "duplicate_",
                    "stale_",
                    "unavailable",
                )
            ):
                _STATE[key] = int(_STATE[key]) + value
            else:
                _STATE[key] = value


def inc(name: str, amount: int = 1) -> None:
    note(**{name: amount})


def timed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)
