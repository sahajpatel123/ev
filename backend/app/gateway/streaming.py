"""Streaming contract for model providers (CORTEX / Agent 10).

The canonical :class:`app.contracts.ChatProvider` protocol is additive-only
and owned by Agent 1, so the streaming surface lives here as a separate,
backward-compatible protocol. Every provider in the registry implements both
interfaces; callers that only know the base protocol keep working unchanged.

Chunks are text deltas. A final chunk with ``done=True`` carries usage, the
resolved model name, and any accumulated tool calls so the stream is
self-contained for audit logging.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.contracts import ChatMessage, ToolCall


@dataclass
class ChatStreamChunk:
    """One provider-stream delta (or the terminal chunk)."""

    text: str = ""
    usage: dict = field(default_factory=dict)
    model: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    done: bool = False
    error: str | None = None


@runtime_checkable
class StreamingChatProvider(Protocol):
    """Additive streaming protocol layered on the base chat protocol.

    Implementations MUST guarantee that cancelling the returned async
    generator closes the upstream HTTP stream (no leaked connections), and
    SHOULD yield the first text delta as soon as it is available.
    """

    name: str

    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatStreamChunk]: ...
