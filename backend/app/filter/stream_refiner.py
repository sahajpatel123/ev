"""Chunk-level stream refinement protocol (Agent 16 CONSCIENCE ↔ Agent 10 CORTEX).

Agent 10 owns ``app/api/core.py::_stream_chat`` and
``app/gateway/streaming.py``. The current protocol emits raw ``delta`` events
and then a ``refined`` event with ``replaces: true``, so a chunk that already
left the server cannot be retracted; the only correction is the final replace.

This module is the seam Agent 10 can adopt for per-chunk refinement:

    refiner = StreamRefiner(grounding=material, strategy=strategy)
    for chunk in chunks:
        emitted, buffered, events = refiner.feed(chunk)
        if emitted:
            yield _sse("delta", {"text": emitted, "final": False})
    emitted, buffered, events = refiner.flush()
    if emitted:
        yield _sse("delta", {"text": emitted, "final": True})
    yield _sse("refined", {"text": refiner.final_text(), "replaces": True})

Rules:
* Never emit an ungrounded personal claim: once a claim starts, the refiner
  buffers until the sentence boundary and the claim audit has run.
* Never stall the stream: the buffer is bounded (``BUFFER_LIMIT``). If a claim
  still has no boundary when the limit is reached, the text is emitted anyway
  and flagged; the final ``refined`` pass still removes it.
* Envelope hashing is untouched: hashing happens at the input boundary, not on
  stream chunks, and ``refined`` carries the final filtered text.

DEPENDENCY NOTE (Agent 10): wiring this into ``_stream_chat`` is outside Agent
16's OWNS paths; the module and its tests land here ready to adopt.
"""

from __future__ import annotations

import re

from app.filter.envelope import GroundingMaterial
from app.filter.output_filter import (
    BIO_VERBS,
    apply_claim_actions,
    audit_grounding,
)
from app.schemas import InteractionStrategy

BUFFER_LIMIT = 600

PERSONAL_CLAIM_START_RE = re.compile(
    rf"\b(?:I|we|you)\s+(?:{BIO_VERBS})\b",
    re.IGNORECASE,
)


class StreamRefiner:
    """Deterministic claim-buffering stream refiner.

    The refiner only *delays* emission; it never fabricates text. Unsupported
    claims are removed by the same ``apply_claim_actions`` used by the
    full-draft filter, so chunk-level and final output agree.
    """

    def __init__(
        self,
        *,
        grounding: list[GroundingMaterial] | None = None,
        strategy: InteractionStrategy | None = None,
        min_evidence: float = 0.5,
        date_evidence: float = 0.9,
        buffer_limit: int = BUFFER_LIMIT,
    ) -> None:
        self.grounding = list(grounding or [])
        self.strategy = strategy
        self.min_evidence = min_evidence
        self.date_evidence = date_evidence
        self.buffer_limit = buffer_limit
        self.buffer = ""
        self.emitted = ""
        self.events: list[dict] = []

    def _next_boundary(self, text: str) -> int | None:
        positions = [m.end() for m in re.finditer(r"[.!?](?:\s|$)", text)]
        if not positions:
            return None
        claim = PERSONAL_CLAIM_START_RE.search(text)
        if claim is None:
            return positions[0]
        for pos in positions:
            if pos >= claim.start():
                return pos
        return None

    def _refine_sentence(self, sentence: str) -> str:
        claims, flags = audit_grounding(
            sentence,
            self.grounding,
            min_evidence=self.min_evidence,
            date_evidence=self.date_evidence,
        )
        refined, edits = apply_claim_actions(sentence, claims)
        for flag in flags:
            self.events.append({"type": "flag", "name": flag.name, "action": flag.action})
        for edit in edits:
            self.events.append({"type": "edit", **edit})
        return refined

    def feed(self, chunk: str) -> tuple[str, str, list[dict]]:
        """Append a chunk; return (emitted_text, buffered_text, new_events)."""

        event_start = len(self.events)
        self.buffer += chunk
        emitted = ""
        while True:
            boundary = self._next_boundary(self.buffer)
            if boundary is None:
                if len(self.buffer) >= self.buffer_limit:
                    emitted += self.buffer
                    self.events.append(
                        {
                            "type": "buffer_overflow",
                            "reason": "claim had no sentence boundary within limit; "
                            "final refined pass will re-audit",
                        }
                    )
                    self.buffer = ""
                    break
                break
            sentence, rest = self.buffer[:boundary], self.buffer[boundary:]
            emitted += self._refine_sentence(sentence)
            self.buffer = rest
        self.emitted += emitted
        return emitted, self.buffer, list(self.events[event_start:])

    def flush(self) -> tuple[str, str, list[dict]]:
        """Emit whatever remains, through the same claim audit."""

        event_start = len(self.events)
        emitted = ""
        if self.buffer.strip():
            emitted = self._refine_sentence(self.buffer)
        self.buffer = ""
        self.emitted += emitted
        self.events.append({"type": "flush"})
        return emitted, self.buffer, list(self.events[event_start:])

    def final_text(self) -> str:
        return self.emitted
