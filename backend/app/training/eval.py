"""Deterministic evaluation harness for trained EV adapters.

Every adapter version ships numbers from this harness: a held-out split,
win-rate against the base model, tool-call validity, HUD schema conformance,
and an overfitting check. The judge is a disclosed deterministic function
(reference matching + the corpus-derived style profile); no confidence value
is ever fabricated.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence

from app.training.style_adapter import (
    BULLET_RE,
    CITATION_RE,
    HEDGE_RE,
    QUESTION_RE,
)

HUD_SCHEMA_PREFIX = "ev.hud."
HUD_REQUIRED_FIELDS = ("schema_version", "generated_at", "title")


def held_out_split(
    records: Sequence[dict],
    *,
    eval_fraction: float = 0.2,
    seed: int = 42,
    min_eval: int = 1,
) -> tuple[list[dict], list[dict]]:
    """Deterministic held-out split keyed on source + hash.

    The same corpus and seed always produce the same train/eval split, and the
    split is stable regardless of record order.
    """

    ordered = sorted(
        records,
        key=lambda r: (
            str(r.get("source", "")),
            str(r.get("hash", "")),
            str(r.get("instruction", "")),
        ),
    )
    count = len(ordered)
    if count <= min_eval:
        return list(ordered), []
    step = max(1, round(1 / eval_fraction)) if eval_fraction > 0 else 1
    # Rotate the sampling start by seed so different seeds land on different
    # rows, while the same seed remains byte-for-byte reproducible.
    offset = seed % step
    eval_indices = set(range(offset, count, step))
    eval_rows = [row for i, row in enumerate(ordered) if i in eval_indices]
    train_rows = [row for i, row in enumerate(ordered) if i not in eval_indices]
    return train_rows, eval_rows


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def reference_score(text: str, reference: str) -> float:
    """Reference-match score: exact = 1.0, containment = 0.6, else 0.0."""

    text_norm = _norm(text)
    ref_norm = _norm(reference)
    if not ref_norm:
        return 0.0
    if text_norm == ref_norm:
        return 1.0
    if ref_norm in text_norm:
        return 0.6
    return 0.0


def style_score(text: str, profile: dict | None = None) -> float:
    """Deterministic style alignment score from the corpus-derived profile."""

    profile = profile or {}
    score = 0.0
    hedges = len(HEDGE_RE.findall(text))
    has_citation = bool(CITATION_RE.search(text))
    has_bullets = bool(BULLET_RE.search(text))
    has_question = bool(QUESTION_RE.search(text))
    words = text.split()

    if profile.get("prefer_direct"):
        score += 1.0 if hedges == 0 else 0.0
    if profile.get("prefer_citations"):
        score += 1.0 if has_citation else 0.0
    if profile.get("prefer_bullets"):
        score += 1.0 if has_bullets else 0.0
    if profile.get("prefer_questions"):
        score += 1.0 if has_question else 0.0

    targets = profile.get("word_count_targets") or {}
    if targets and words:
        mode = profile.get("mode") or next(iter(targets))
        target = targets.get(str(mode))
        if target:
            score += max(0.0, 1.0 - abs(len(words) - int(target)) / max(1, int(target)))
    return round(score / max(1, len([k for k in ("prefer_direct", "prefer_citations", "prefer_bullets", "prefer_questions") if profile.get(k)]) + (1 if targets and words else 0)), 4)


def judge(
    text: str,
    reference: str,
    *,
    profile: dict | None = None,
    reference_weight: float = 0.6,
) -> float:
    """Disclosed deterministic judge: reference match + style alignment."""

    return round(
        reference_weight * reference_score(text, reference)
        + (1 - reference_weight) * style_score(text, profile),
        4,
    )


def validate_tool_call(call: dict, *, allow_json_string_args: bool = True) -> dict:
    """Check that a tool call is structurally valid JSON."""

    issues: list[str] = []
    if not isinstance(call, dict):
        issues.append("tool call is not an object")
        return {"valid": False, "issues": issues}
    name = call.get("name")
    arguments = call.get("arguments", call.get("input"))
    if not isinstance(name, str) or not name.strip():
        issues.append("tool call has no string name")
    if arguments is None:
        issues.append("tool call has no arguments")
    elif isinstance(arguments, str):
        if allow_json_string_args:
            try:
                json.loads(arguments)
            except (TypeError, ValueError):
                issues.append("arguments string is not valid JSON")
        else:
            issues.append("arguments must be a JSON string")
    elif not isinstance(arguments, dict):
        issues.append("arguments must be a JSON object")
    return {"valid": not issues, "issues": issues}


def validate_hud(text: str) -> dict:
    """Structural HUD schema conformance check for embedded briefing JSON."""

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {"valid": True, "contract": None, "issues": []}
    try:
        payload = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return {"valid": False, "contract": None, "issues": ["embedded JSON is malformed"]}
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.startswith(HUD_SCHEMA_PREFIX):
        return {"valid": True, "contract": None, "issues": []}
    missing = [field for field in HUD_REQUIRED_FIELDS if field not in payload]
    return {
        "valid": not missing,
        "contract": schema_version,
        "issues": [f"missing {field}" for field in missing],
    }


def tool_call_validity(texts: Sequence[str]) -> dict:
    """Aggregate tool-call validity across generated responses."""

    calls = 0
    valid = 0
    issues: list[str] = []
    for text in texts:
        for line in (text or "").splitlines():
            stripped = line.strip()
            if not (stripped.startswith("{") and stripped.endswith("}")):
                continue
            try:
                payload = json.loads(stripped)
            except (TypeError, ValueError):
                continue
            if "name" not in payload or "arguments" not in payload:
                continue
            calls += 1
            result = validate_tool_call(payload)
            if result["valid"]:
                valid += 1
            else:
                issues.extend(result["issues"])
    return {
        "tool_calls": calls,
        "valid": valid,
        "validity": round(valid / calls, 4) if calls else None,
        "issues": issues[:20],
    }


def hud_conformance(texts: Sequence[str]) -> dict:
    """Aggregate HUD schema conformance across generated responses."""

    checked = 0
    valid = 0
    issues: list[str] = []
    for text in texts:
        result = validate_hud(text)
        if result["contract"] is None:
            continue
        checked += 1
        if result["valid"]:
            valid += 1
        else:
            issues.extend(result["issues"])
    return {
        "hud_blocks": checked,
        "valid": valid,
        "conformance": round(valid / checked, 4) if checked else None,
        "issues": issues[:20],
    }


def overfit_report(
    train_losses: Sequence[float],
    val_losses: Sequence[float],
    *,
    ratio_threshold: float = 1.15,
) -> dict:
    """Detect overfitting from train/val loss curves.

    Overfitting is flagged when validation loss diverges from training loss
    (val/train ratio beyond threshold) while training loss is still falling.
    """

    train = [float(v) for v in train_losses if v is not None]
    val = [float(v) for v in val_losses if v is not None]
    if not train or not val:
        return {
            "overfit_detected": False,
            "reason": "insufficient loss curves",
            "train_points": len(train),
            "val_points": len(val),
        }
    last_train = train[-1]
    last_val = val[-1]
    ratio = round(last_val / last_train, 4) if last_train else None
    falling = len(train) >= 2 and train[-1] < train[0]
    overfit = bool(ratio and ratio > ratio_threshold and falling)
    return {
        "overfit_detected": overfit,
        "reason": (
            f"val/train loss ratio {ratio} > {ratio_threshold} while train loss falls"
            if overfit
            else "no divergence detected"
        ),
        "ratio": ratio,
        "train_last": last_train,
        "val_last": last_val,
        "train_points": len(train),
        "val_points": len(val),
    }


def evaluate_models(
    prompts: Sequence[str],
    references: Sequence[str],
    *,
    base_predict: Callable[[str], str],
    adapter_predict: Callable[[str], str],
    profile: dict | None = None,
) -> dict:
    """Win-rate evaluation: adapter output vs base output on held-out prompts.

    The judge is ``judge()``: a deterministic blend of reference matching and
    the corpus-derived style profile. The method is disclosed in the returned
    ``method`` field, never hidden behind an opaque model.
    """

    base_wins = 0
    adapter_wins = 0
    ties = 0
    samples: list[dict] = []
    for prompt, reference in zip(prompts, references, strict=False):
        base_text = base_predict(prompt)
        adapter_text = adapter_predict(prompt)
        base_score = judge(base_text, reference, profile=profile)
        adapter_score = judge(adapter_text, reference, profile=profile)
        if adapter_score > base_score:
            adapter_wins += 1
        elif base_score > adapter_score:
            base_wins += 1
        else:
            ties += 1
        samples.append(
            {
                "prompt": prompt[:200],
                "base_score": base_score,
                "adapter_score": adapter_score,
            }
        )
    total = max(1, len(prompts))
    return {
        "prompts": total,
        "adapter_wins": adapter_wins,
        "base_wins": base_wins,
        "ties": ties,
        "win_rate": round(adapter_wins / total, 4),
        "method": (
            "deterministic judge: 0.6*reference-match + 0.4*style-profile "
            "alignment; no LLM-as-judge"
        ),
        "samples": samples,
    }
