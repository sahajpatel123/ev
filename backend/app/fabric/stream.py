"""Sentence streaming. The first chunk is emitted before the producer finishes."""
from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Iterator

_BOUND = re.compile(r"(?<=[.!?])\s+")


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _BOUND.split(text or "") if p.strip()]
    return parts or ([text.strip()] if (text or "").strip() else [])


def iter_sentences(text: str) -> Iterator[str]:
    yield from sentences(text)


async def consume(chunks: AsyncIterator[str], on_chunk) -> dict:
    """Forward chunks as they arrive. Records when the first non-empty one landed."""
    started = time.perf_counter()
    first = None
    parts: list[str] = []
    async for chunk in chunks:
        text = str(chunk or "")
        if not text:
            continue
        if first is None:
            first = time.perf_counter() - started
        parts.append(text)
        if on_chunk is not None:
            on_chunk(text)
    return {
        "text": "".join(parts),
        "t_first_streamed_chunk_s": first,
        "n_chunks": len(parts),
    }
