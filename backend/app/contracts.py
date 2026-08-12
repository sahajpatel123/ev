"""Provider contracts and lightweight data structures shared across EV."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

# --------------------------------------------------------------------------- #
# Retrieval / memory layer
# --------------------------------------------------------------------------- #


@dataclass
class RetrievedMemory:
    memory_id: str
    text: str
    memory_type: str
    payload: dict
    importance: float
    confidence: float
    event_time: datetime | None
    privacy_level: str
    source_type: str
    score: float
    components: dict[str, float] = field(default_factory=dict)
    source_event_ids: list[str] = field(default_factory=list)


@dataclass
class EntityRef:
    name: str
    entity_type: str = "other"
    role: str = "related"
    weight: float = 1.0


@dataclass
class MemoryCandidate:
    memory_type: str
    text: str
    payload: dict
    importance: float = 0.5
    confidence: float = 0.7
    source_type: str = "inferred"
    privacy_level: str = "normal"
    event_time: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    entities: list[EntityRef] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Chat / gateway
# --------------------------------------------------------------------------- #


@dataclass
class MediaPart:
    """One typed media reference attached to a chat message.

    ``data_url`` is the raw inline representation (base64 data URL) and may
    only be populated when explicit permission allows raw transmission.  When
    raw content is unnecessary, providers receive ``text`` (a derived/minimal
    representation) instead, preserving provenance via ``ref`` and ``sha256``.
    """

    kind: str  # image | audio | text | document
    content_type: str = "application/octet-stream"
    data_url: str | None = None
    text: str | None = None
    ref: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    name: str | None = None
    media: list[MediaPart] = field(default_factory=list)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    model: str | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    sensitive: bool = False
    read_only: bool = True
    permission: str = "memory:read"
    undoable: bool = False
    output: dict = field(default_factory=dict)


@dataclass
class MemoryRef:
    """A memory included in a request envelope, sufficient for audit traceability."""

    memory_id: str
    memory_type: str
    text: str
    score: float = 0.0
    event_time: str | None = None

    def to_dict(self, *, text_limit: int | None = None) -> dict:
        text = self.text
        if text_limit is not None and len(text) > text_limit:
            text = f"{text[:text_limit]}…"
        return {
            "memory_id": self.memory_id,
            "memory_type": self.memory_type,
            "text": text,
            "score": round(self.score, 4),
            "event_time": self.event_time,
        }


@dataclass
class RequestEnvelope:
    """Complete request envelope: strategy, memories, request id, required metadata.

    The gateway carries this envelope on every model call so downstream filters can
    audit what was sent, why, and to which provider/model.
    """

    request_id: str
    strategy: dict
    memories: list[MemoryRef] = field(default_factory=list)
    conversation_id: str | None = None
    device_id: str | None = None
    context_tokens: int = 0
    metadata: dict = field(default_factory=dict)
    media_refs: list[dict] = field(default_factory=list)

    def to_dict(self, *, memory_text_limit: int | None = None) -> dict:
        return {
            "request_id": self.request_id,
            "strategy": self.strategy,
            "memories": [m.to_dict(text_limit=memory_text_limit) for m in self.memories],
            "conversation_id": self.conversation_id,
            "device_id": self.device_id,
            "context_tokens": self.context_tokens,
            "metadata": self.metadata,
            "media_refs": self.media_refs,
        }


class ChatProvider(Protocol):
    name: str
    supports_media: bool = False

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult: ...

    async def chat_with_tools(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult: ...

    async def list_models(self) -> list[str]: ...


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...
