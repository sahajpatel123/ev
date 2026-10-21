"""Bounded context for a long agent run.

Why this module exists
----------------------
The reference harness this workspace reverse-engineered keeps a run inside
one context window by compacting it: Grok's harness auto-compacts at
``session.auto_compact_threshold_percent = 85`` and leaves the compacted
turns on disk under the session's ``compaction/`` directory.

Evie's agent loop had no equivalent. Every tool result is appended to the
transcript verbatim, and a run may take 24 steps and 40 tool calls with up
to ``agent_max_output_chars`` (8 000) characters per result. The worst case
is a request that no provider will accept, and the failure arrives as a
provider error with nothing to say which part of the run was too big.

What compaction does here
-------------------------
It compacts the *older* part of a transcript and never the recent part, and
it does so **factually**:

- the system prompt, the owner's goal, and the model's own assistant turns
  are kept verbatim — the reasoning is the run;
- the tool results in the compacted region are replaced by a receipt digest
  that states what was called, whether it succeeded, and a short preview of
  what came back;
- the digest is labelled, so the model can see that older output was
  shortened rather than lost, and can re-run a tool whose detail it needs.

Nothing here invents a summary. A model-generated summary of a tool result
can state something the tool never returned; a digest cannot. That is the
same rule the rest of the tree follows: a receipt shows what happened, and
only a receipt says what happened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.contracts import ChatMessage

#: Characters per token, used for the size estimate. Deliberately coarse:
#: the estimate only has to notice that a transcript grew past its budget.
CHARS_PER_TOKEN = 4

#: How many trailing messages are always kept verbatim.
DEFAULT_KEEP_RECENT = 12

#: How much of each compacted tool result survives in the digest.
DEFAULT_PREVIEW_CHARS = 320


@dataclass(frozen=True)
class CompactionReport:
    """What compaction did, so the run can say so honestly."""

    compacted: bool
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    tool_results_digested: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "compacted": self.compacted,
            "messages_before": self.messages_before,
            "messages_after": self.messages_after,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "tool_results_digested": self.tool_results_digested,
        }


def estimate_tokens(messages: list[ChatMessage]) -> int:
    """A cheap size estimate for one transcript."""
    total = 0
    for message in messages or []:
        total += len(message.content or "")
        for call in message.tool_calls or []:
            total += len(call.name or "") + len(json.dumps(call.arguments or {}, default=str))
    return total // CHARS_PER_TOKEN


def _digest(content: str, *, preview_chars: int) -> str:
    """One tool result, reduced to what a receipt states plus a short preview.

    A result that is already short is left alone: compaction must never make
    a transcript longer, and a digest that adds words to a two-line receipt
    is a digest that costs more than it saves.
    """
    text = (content or "").strip()
    if not text:
        return json.dumps({"ok": None, "note": "empty tool result"})
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        if len(text) <= preview_chars:
            return text
        return json.dumps(
            {"note": "earlier tool output, shortened", "preview": text[:preview_chars]}
        )
    if not isinstance(payload, dict):
        if len(text) <= preview_chars:
            return text
        return json.dumps(
            {"note": "earlier tool output, shortened", "preview": text[:preview_chars]}
        )

    digest: dict[str, Any] = {}
    ok = payload.get("ok")
    if ok is not None:
        digest["ok"] = ok
    error = payload.get("error")
    if error:
        digest["error"] = str(error)[:preview_chars]
    result = payload.get("result")
    if result is not None:
        rendered = json.dumps(result, default=str, ensure_ascii=False)
        if len(rendered) > preview_chars:
            digest["result_chars"] = len(rendered)
            digest["preview"] = rendered[:preview_chars]
        else:
            digest["result"] = result
    if not digest:
        # Nothing recognisable to keep: keep the text if it is small enough.
        if len(text) <= preview_chars:
            return text
        return json.dumps(
            {"note": "earlier tool output, shortened", "preview": text[:preview_chars]}
        )
    if len(rendered := json.dumps(digest, default=str, ensure_ascii=False)) >= len(text):
        # The digest is no smaller than the receipt it replaces: keep the
        # original. Compaction that does not compact is not compaction.
        return text
    if len(digest.get("preview", "")) or "result_chars" in digest:
        digest["note"] = "shortened"
    return json.dumps(digest, default=str, ensure_ascii=False)


def compact_messages(
    messages: list[ChatMessage],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> tuple[list[ChatMessage], CompactionReport]:
    """Compact the older part of a transcript, keeping the recent part verbatim.

    The system prompt and the owner's own goal are never touched: they are
    the run's contract with the owner, not its history.
    """
    before = estimate_tokens(messages)
    count = len(messages)
    if count <= keep_recent + 2:
        return list(messages), CompactionReport(
            compacted=False,
            messages_before=count,
            messages_after=count,
            tokens_before=before,
            tokens_after=before,
            tool_results_digested=0,
        )
    head: list[ChatMessage] = []
    tail_start = count - keep_recent
    # The contract: system prompt, then the owner's goal. Everything between
    # the contract and the recent window is history.
    contract_end = 0
    for index, message in enumerate(messages):
        if message.role == "system":
            contract_end = index + 1
            continue
        if message.role == "user" and contract_end:
            contract_end = index + 1
            break
    head = list(messages[:contract_end])
    middle = messages[contract_end:tail_start]
    tail = list(messages[tail_start:])

    digested = 0
    shortened = 0
    compacted_middle: list[ChatMessage] = []
    for message in middle:
        if message.role == "tool":
            digested += 1
            digest = _digest(message.content, preview_chars=preview_chars)
            if len(digest) < len(message.content or ""):
                shortened += 1
            compacted_middle.append(
                ChatMessage(
                    role="tool",
                    content=digest,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                )
            )
        else:
            compacted_middle.append(message)

    if shortened:
        compacted_middle.insert(
            0,
            ChatMessage(
                role="user",
                content=(
                    f"[{shortened} earlier tool result(s) were shortened. Each one "
                    "still says whether the call succeeded and what it returned. "
                    "Re-run a tool if you need its full output.]"
                ),
            ),
        )

    out = head + compacted_middle + tail
    after = estimate_tokens(out)
    return out, CompactionReport(
        compacted=shortened > 0,
        messages_before=count,
        messages_after=len(out),
        tokens_before=before,
        tokens_after=after,
        tool_results_digested=shortened,
    )


def context_budget_exceeded(
    messages: list[ChatMessage],
    *,
    context_tokens: int,
    threshold_percent: int,
    tool_schema_tokens: int = 0,
) -> bool:
    """True when the transcript plus its tool schemas would overflow the window.

    The tool schemas are counted because they are sent on every request: a
    harness with a wide surface spends most of its window on the surface.
    """
    if context_tokens <= 0:
        return False
    limit = int(context_tokens * max(1, min(threshold_percent, 99)) / 100)
    used = estimate_tokens(messages) + max(0, int(tool_schema_tokens))
    return used > limit


def tool_schema_tokens(specs: list[Any]) -> int:
    """A size estimate for the tool surface sent with every request."""
    total = 0
    for spec in specs or []:
        total += len(getattr(spec, "name", "") or "")
        total += len(getattr(spec, "description", "") or "")
        parameters = getattr(spec, "parameters", None)
        if parameters:
            total += len(json.dumps(parameters, default=str))
    return total // CHARS_PER_TOKEN


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_KEEP_RECENT",
    "DEFAULT_PREVIEW_CHARS",
    "CompactionReport",
    "compact_messages",
    "context_budget_exceeded",
    "estimate_tokens",
    "tool_schema_tokens",
]
