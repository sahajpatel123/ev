"""Owner-work shape: one artifact vs open multi-step.

Simple create/save of one list, note, or file is one write. Verification is
the executor receipt (or a read of that path), not another create. True
multi-step work — organize many files, research-then-act, send, two named
artifacts — stays open and may loop.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.cognitive.session_store import CognitiveSession, save
from app.ev.file_coalesce import (
    already_written_goal,
    is_rephrase_create,
    is_verify_only,
    kinds_compatible,
    label_from_path,
    looks_like_distinct_new_file,
    wants_more_content,
)

_ORGANIZE = re.compile(
    r"\b(?:organize|sort(?:ing)?|tidy|file away|move all|batch(?:es)?|"
    r"rename all|group (?:the |these )?)\b",
    re.I,
)
_RESEARCH_THEN = re.compile(
    r"\b(?:look up|research|search (?:the )?web|find out)\b.{0,120}\b(?:then|and then)\b",
    re.I,
)
_CREATE_ONE = re.compile(
    r"\b(?:create|make|write|save|put|draft|leave|drop|start|build|jot|prepare)\b"
    r".{0,100}\b(?:lists?|notes?|files?|check-?lists?|txt)\b",
    re.I,
)
_KIND_SPAN = re.compile(
    r"\b([A-Za-z][\w+\-]{1,32}(?:\s+[A-Za-z][\w+\-]{1,20}){0,2})\s+"
    r"(?:to-?do\s+lists?|todo\s+lists?|check-?lists?|check\s*lists?|lists?|notes?)\b",
    re.I,
)
_FOLLOW_HINT = re.compile(
    r"\b(?:add|append|include|check(?:ed)? off|cross(?:ed)? off|undo|"
    r"what's on|what is on|read(?:\s+it)?|open it)\b",
    re.I,
)

_BOUND_KEY = "bound_artifact"
_SHAPE_KEY = "work_shape"
_UTTER_KEY = "owner_utterance"


def classify_owner_work(text: str) -> str:
    """Return 'one_artifact' or 'open'. Never grocery-specific."""

    raw = (text or "").strip()
    if not raw:
        return "open"
    if _multiple_named_kinds(raw) or looks_like_distinct_new_file(raw):
        return "open"
    if _ORGANIZE.search(raw) or _RESEARCH_THEN.search(raw):
        return "open"
    try:
        from app.ev.desk_meaning import list_create_parts, note_create_parts

        if list_create_parts(raw) is not None or note_create_parts(raw) is not None:
            return "one_artifact"
    except Exception:
        pass
    if _CREATE_ONE.search(raw):
        return "one_artifact"
    return "open"


def begin_owner_turn(cognition: CognitiveSession, text: str) -> CognitiveSession:
    """Start an owner utterance. Clear a prior bind unless this is a follow-up."""

    from app.cognitive.intent import begin_owner_turn as _begin

    return _begin(cognition, text)


def bound_artifact(cognition: CognitiveSession) -> dict[str, Any]:
    row = cognition.constraints.get(_BOUND_KEY)
    return dict(row) if isinstance(row, dict) else {}


def bind_written_artifact(cognition: CognitiveSession, body: dict[str, Any]) -> CognitiveSession:
    path = str((body or {}).get("path") or "").strip()
    action = str((body or {}).get("action") or "").strip().lower()
    if action == "delete":
        cognition.constraints.pop(_BOUND_KEY, None)
        return save(cognition)
    if not path:
        return cognition
    if not (body.get("ok") or body.get("verified") or body.get("artifact_complete")):
        return cognition
    if action not in {"write", "append"} and body.get("receipt") not in {
        "named_list",
        "write_note",
        "dated_note",
    }:
        return cognition
    cognition.constraints[_BOUND_KEY] = {
        "path": path,
        "label": label_from_path(path) or str(body.get("label") or ""),
        "mode": str(cognition.constraints.get(_SHAPE_KEY) or "open"),
    }
    return save(cognition)


def decide_file_effect(effect: str, cognition: CognitiveSession) -> dict[str, Any]:
    """Decide skip / last_path / pass-through for files.act and computer file goals."""

    raw = (effect or "").strip()
    bound = bound_artifact(cognition)
    path = str(bound.get("path") or "").strip()
    shape = str(cognition.constraints.get(_SHAPE_KEY) or "open")
    if not path:
        return {"goal": raw, "last_path": None, "skip": False}
    if looks_like_distinct_new_file(raw, path):
        if shape == "one_artifact":
            return {
                "goal": raw,
                "last_path": path,
                "skip": True,
                "body": _skip_receipt(path, raw),
            }
        return {"goal": raw, "last_path": None, "skip": False}
    if is_verify_only(raw) and not wants_more_content(raw):
        return {
            "goal": raw,
            "last_path": path,
            "skip": True,
            "body": _skip_receipt(path, raw),
        }
    compatible = kinds_compatible(_effect_kind(raw), str(bound.get("label") or label_from_path(path)))
    if shape == "one_artifact" or compatible:
        if is_rephrase_create(raw, path) and not wants_more_content(raw):
            return {
                "goal": raw,
                "last_path": path,
                "skip": True,
                "body": _skip_receipt(path, raw),
            }
        return {"goal": raw, "last_path": path, "skip": False}
    return {"goal": raw, "last_path": path, "skip": False}


def mark_artifact_complete(body: dict[str, Any], cognition: CognitiveSession) -> dict[str, Any]:
    out = dict(body or {})
    bound = bound_artifact(cognition)
    path = str(out.get("path") or bound.get("path") or "").strip()
    shape = str(cognition.constraints.get(_SHAPE_KEY) or "")
    written = str(out.get("action") or "").lower() in {"write", "append", "read"} and (
        out.get("ok") or out.get("verified") or out.get("artifact_complete")
    )
    owner = str(cognition.constraints.get(_UTTER_KEY) or "")
    if (
        shape == "one_artifact"
        and path
        and written
        and not looks_like_distinct_new_file(owner)
        and (out.get("ok") or out.get("verified") or out.get("artifact_complete"))
    ):
        out["artifact_complete"] = True
        out["must_continue"] = False
        out["goal_complete"] = True
        out["instruction"] = (
            "ARTIFACT_COMPLETE: the requested file is written and verified. "
            "Do not create another file. Speak to the owner about that one path."
        )
        from app.cognitive.intent import close_finished_file_goal

        close_finished_file_goal(cognition)
    return out


def work_shape_policy(cognition: CognitiveSession) -> str:
    shape = str(cognition.constraints.get(_SHAPE_KEY) or "open")
    bound = bound_artifact(cognition)
    if shape == "one_artifact":
        extra = ""
        if bound.get("path"):
            extra = f" Bound path: {bound['path']}. Later file effects must reuse it."
        return (
            "WORK SHAPE: one-artifact. The owner asked for one file. Compose the "
            "complete contents and write once. Verification is the executor receipt "
            "or a read of that same path — never a second create, never a 'text list' "
            "or 'desktop … list' sibling, never a verify-by-writing-again. Do not "
            "rephrase the filename across tool calls. After ARTIFACT_COMPLETE, speak."
            + extra
        )
    return (
        "WORK SHAPE: open. Multi-step work may loop (organize many, research then act, "
        "send, or two named artifacts the owner actually asked for). Do not invent extra "
        "files to 'verify' a write that already returned verified evidence."
    )


def _skip_receipt(path: str, effect: str) -> dict[str, Any]:
    goal = already_written_goal(path, effect)
    name = Path(path).name
    return {
        "ok": True,
        "executed": True,
        "verified": True,
        "must_continue": False,
        "goal_complete": True,
        "artifact_complete": True,
        "action": "read",
        "path": path,
        "query": name,
        "receipt": "already_written",
        "spoken": f"It's already saved as {name}.",
        "instruction": (
            "ARTIFACT_COMPLETE: the requested file is written and verified. "
            "Do not create another file. Speak to the owner."
        ),
        **{k: v for k, v in goal.items() if k not in {"action", "path", "query"}},
    }


def _is_followup(text: str, bound: dict[str, Any]) -> bool:
    raw = (text or "").strip()
    path = str(bound.get("path") or "")
    if not raw or not path:
        return False
    try:
        from app.ev.laptop_files import looks_like_file_followup

        if looks_like_file_followup(raw, last_path=path):
            return True
    except Exception:
        pass
    return bool(_FOLLOW_HINT.search(raw) and not _CREATE_ONE.search(raw))


def _multiple_named_kinds(text: str) -> bool:
    hits = []
    for match in _KIND_SPAN.finditer(text or ""):
        label = re.sub(r"\s+", " ", match.group(1).strip().lower())
        if label and label not in {"text", "desktop", "documents", "new", "the"}:
            hits.append(label)
    return len(set(hits)) >= 2


def _effect_kind(text: str) -> str:
    try:
        from app.ev.desk_meaning import kind_label, occasion_label
    except Exception:
        return ""
    return (kind_label(text) or occasion_label(text) or "").strip().lower()
