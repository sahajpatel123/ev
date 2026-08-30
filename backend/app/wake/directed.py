"""Directed-speech / false-trigger check (W3).

After a candidate wake, determine whether the utterance was actually
addressed to Evie.

 TRUE:  "Evie, what's the weather?"  (wake + directed command)
 FALSE: "Evie is going to be late." (conversational mention)
 FALSE: "Did you see Evie yesterday?" (past reference, not directed)

Evidence may be acoustic, ASR/transcript, and semantic. This stage operates
after the candidate wake and must never fabricate an action — it only cancels
before meaningful execution, with bounded diagnostics.

Architecture chooses the JOB (directed vs not); evidence chooses the model.
For the W1-W4 scaffold this is a deterministic ASR+semantic check that the
full pipeline can call; a future ML layer can replace the body without
changing the contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DirectedResult:
    directed: bool
    reason: str
    diagnostics: dict


# The wake prefix that must be the utterance anchor. If "Evie" appears
# mid-sentence without leading position, it is not directed.
_WAKE_ANCHORED = re.compile(
    r"^(?:hey|ok|okay|hi|hello)?\s*(?:evie+|eevee|evy|evi)\b",
    re.IGNORECASE,
)

# After stripping the wake name, a true command has a verb/question word or
# imperative framing. A false trigger is a copular/description ("is ...",
# "was ...", "is going to be late") or a past-reference question.
_FALSE_CONTINUATIONS = re.compile(
    r"^(?:is|was|were|has|had|will|would|could|should|might)\b",
    re.IGNORECASE,
)

_QUESTION_WORD = re.compile(
    r"^(?:what|who|where|when|why|how|can|could|will|would|should|do|does|did|is|are|was|were|has|have|had)\b",
    re.IGNORECASE,
)

_IMPERATIVE_STARTERS = frozenset(
    {
        "set",
        "create",
        "add",
        "remind",
        "show",
        "open",
        "play",
        "send",
        "call",
        "schedule",
        "find",
        "tell",
        "help",
        "list",
        "cancel",
        "delete",
        "update",
        "start",
        "stop",
        "pause",
        "resume",
        "search",
        "book",
        "make",
        "write",
        "generate",
        "draft",
    }
)


def _command_after_wake(text: str) -> str:
    stripped = re.sub(
        r"^(?:hey|ok|okay|hi|hello)?\s*(?:evie+|eevee|evy|evi|eve|evil|every|ee\s*vee)"
        r"(?:\s+here)?\b[\s,!.?\-]*",
        "",
        (text or "").strip(),
        count=1,
        flags=re.IGNORECASE,
    )
    return stripped.strip(" ,.!?")


class DirectedSpeechChecker:
    """Acoustic + transcript + semantic directed check.

    Contract:
      directed=True  → utterance was addressed to Evie; allow hand-off to
                       Realtime/Foundation.
      directed=False → not addressed; cancel before meaningful action,
                       silent, bounded diagnostics only.

    The caller provides the full utterance text (ASR result when available)
    and optional acoustic signals. The checker never invents actions.
    """

    def is_directed(
        self,
        text: str,
        *,
        acoustic: dict | None = None,
        asr_confidence: float | None = None,
    ) -> DirectedResult:
        raw = (text or "").strip()
        if not raw:
            return DirectedResult(
                directed=False,
                reason="empty_utterance",
                diagnostics={"text": raw, "acoustic": acoustic or {}},
            )

        anchored = bool(_WAKE_ANCHORED.search(raw))
        if not anchored:
            # "Did you see Evie yesterday?" — Evie not at head → not wake.
            return DirectedResult(
                directed=False,
                reason="not_anchored_at_head",
                diagnostics={
                    "text": raw,
                    "anchored": False,
                    "command": "",
                },
            )

        command = _command_after_wake(raw)
        if not command:
            # Bare "Evie" — wake without command is still directed (follow-up
            # window will handle it); mark as wake_only, not a false trigger.
            return DirectedResult(
                directed=True,
                reason="wake_only_bare_name",
                diagnostics={"text": raw, "command": command, "anchored": True},
            )

        lower_cmd = command.lower()

        # "Evie is going to be late." — copular description → not directed.
        if _FALSE_CONTINUATIONS.match(command):
            # Allow "Evie, what's the weather?" where "what's" is not in false set;
            # but "is" alone is already false. The implicit subject is Evie.
            return DirectedResult(
                directed=False,
                reason="conversational_mention_is",
                diagnostics={"text": raw, "command": command, "anchored": True},
            )

        # Heuristic: question or imperative after wake → directed.
        first_word = lower_cmd.split(None, 1)[0] if lower_cmd else ""
        if _QUESTION_WORD.match(command) or first_word in _IMPERATIVE_STARTERS:
            return DirectedResult(
                directed=True,
                reason="question_or_imperative_after_wake",
                diagnostics={"text": raw, "command": command, "anchored": True},
            )

        # Default: if anchored and non-empty and not the known false patterns,
        # treat as directed — better to have a high-recall second pass then a
        # full-utterance speaker recheck cancel, than to drop a quiet owner.
        # Bounded diagnostics only (no transcript echo beyond 80 chars).
        return DirectedResult(
            directed=True,
            reason="anchored_with_content",
            diagnostics={
                "text": raw[:80],
                "command": command[:80],
                "anchored": True,
                "asr_confidence": asr_confidence,
                "acoustic": {k: v for k, v in (acoustic or {}).items() if k in {"snr", "rms"}},
            },
        )

    def should_cancel(self, result: DirectedResult, *, not_owner: bool = False) -> bool:
        """True when full-utterance evidence says cancel before action.

        Cancel if not_owner OR not directed. Bounded diagnostics only;
        do not announce false wake.
        """
        if not_owner:
            return True
        return not result.directed
