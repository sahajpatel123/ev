"""Hard payload boundary between EV data and model providers.

Model providers are treated as untrusted for storage. This module is the
last-line payload gate: before any message is handed to a provider it must
contain no ``never_send_to_model`` content, and credentials are redacted
deterministically. The checks are plain-text and dependency-free so they can
run in the gateway, in tests, and on any future client path.

The data-layer filters (retrieval, history, rollup, user state) are the
primary enforcement points; this module is the defense-in-depth guarantee that
even a misconfigured caller cannot push forbidden content across the boundary.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from app.contracts import ChatMessage, MediaPart, RequestEnvelope

MODEL_FORBIDDEN_MARKERS: tuple[str, ...] = (
    "never_send_to_model",
    "[never_send_to_model]",
    "privacy_level=never_send_to_model",
    "privacy=never_send_to_model",
)

SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "api_key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "api_key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "api_key"),
    (
        re.compile(
            r"\b(?:api[_-]?key|secret|password|passwd|token)\s*(?:[=:]|\s+(?:is|was)\s+)"
            r"[A-Za-z0-9_\-./+]{8,}\b",
            re.IGNORECASE,
        ),
        "credential",
    ),
    (re.compile(r"\b(?:[0-9][ -]?){13,19}\b"), "card_number"),
]

SECRET_REDACTION = "[credential redacted]"


class ModelBoundaryViolation(Exception):
    """A provider-bound payload contained forbidden content and was blocked."""


def _contains_forbidden(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in MODEL_FORBIDDEN_MARKERS)


def _walk_strings(obj: Any, path: str, out: list[tuple[str, str]]) -> None:
    """Yield (path, value) for every string reachable from *obj*."""
    if obj is None:
        return
    if isinstance(obj, str):
        out.append((path, obj))
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "privacy_level":
                # Classification labels are audit metadata, not provider-bound
                # content. Scanning them would block legitimate redacted
                # payloads whose label is never_send_to_model.
                continue
            _walk_strings(value, f"{path}.{key}", out)
        return
    if isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            _walk_strings(value, f"{path}[{index}]", out)
        return


def _envelope_strings(envelope: RequestEnvelope) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    _walk_strings(envelope.strategy, "strategy", items)
    _walk_strings(envelope.metadata, "metadata", items)
    for index, memory in enumerate(envelope.memories):
        _walk_strings(memory.to_dict(), f"memories[{index}]", items)
    return items


def redact_secrets(text: str) -> str:
    """Deterministically redact credential-like content from provider-bound text."""
    for pattern, _label in SECRET_PATTERNS:
        text = pattern.sub(SECRET_REDACTION, text)
    return text


def guard_model_payload(
    messages: Sequence[ChatMessage],
    envelope: RequestEnvelope | None,
) -> list[ChatMessage]:
    """Validate the exact payload that is about to cross the model boundary.

    Raises :class:`ModelBoundaryViolation` if any string in the messages or
    envelope carries a ``never_send_to_model`` marker, so the provider call
    never happens. Otherwise returns messages with credentials redacted.
    """

    for message in messages:
        if _contains_forbidden(message.content):
            raise ModelBoundaryViolation(
                f"Blocked provider payload: message role={message.role!r} contains "
                "never_send_to_model content"
            )
        if message.name and _contains_forbidden(message.name):
            raise ModelBoundaryViolation(
                f"Blocked provider payload: message name={message.name!r} contains "
                "never_send_to_model content"
            )
        for part in message.media:
            if part.text and _contains_forbidden(part.text):
                raise ModelBoundaryViolation(
                    f"Blocked provider payload: message role={message.role!r} media "
                    f"part kind={part.kind!r} contains never_send_to_model content"
                )
            if part.data_url and _contains_forbidden(part.data_url):
                raise ModelBoundaryViolation(
                    f"Blocked provider payload: message role={message.role!r} media "
                    f"part kind={part.kind!r} data_url contains forbidden content"
                )

    if envelope is not None:
        for path, value in _envelope_strings(envelope):
            if _contains_forbidden(value):
                raise ModelBoundaryViolation(
                    f"Blocked provider payload: envelope field {path} contains "
                    "never_send_to_model content"
                )

    sanitized: list[ChatMessage] = []
    for message in messages:
        sanitized.append(
            ChatMessage(
                role=message.role,
                content=redact_secrets(message.content),
                name=message.name,
                media=[
                    MediaPart(
                        kind=part.kind,
                        content_type=part.content_type,
                        data_url=part.data_url,
                        text=redact_secrets(part.text) if part.text else None,
                        ref=part.ref,
                        sha256=part.sha256,
                    )
                    for part in message.media
                ],
            )
        )
    return sanitized
