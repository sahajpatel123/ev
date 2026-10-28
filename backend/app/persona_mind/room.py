"""READ THE ROOM: deterministic owner-state model on the reply path.

Fast features from the current message plus stored turn history, rule-based
pace/focus with per-owner tunable thresholds, and the ≤30-token ROOM line.
No network, no LLM. Local hour uses system local time (the backend runs on
the owner's own machine); the slow path may refine this later.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.persona_mind import store
from app.persona_mind.render import STOPWORDS, estimate
from app.persona_mind.state import coerce_ts
from app.persona_mind.tune import tunables as active_tunables

URGENCY_WORDS = frozenset({"quick", "asap", "fast", "just", "jaldi", "abhi"})
DEPTH_WORDS = frozenset({"why", "explain", "deep", "dive", "compare", "think"})
DEPTH_PHRASES = ("deep dive", "think through")
IMPERATIVE_VERBS = frozenset({
    "run", "send", "fix", "show", "tell", "give", "make", "do", "go",
    "stop", "ship", "deploy", "write", "add", "set", "open", "close",
    "check", "start", "build", "try", "take", "drop", "skip", "keep",
})
CONFUSIONS = frozenset({
    "teh", "adn", "hte", "taht", "yuo", "waht", "thier", "recieve",
    "seperate", "occured", "beleive", "freind", "jsut", "wnat",
    "nad", "thsi", "wiht", "fro", "tihs", "ahve",
})
_REPEAT_CHAR = re.compile(r"(.)\1\1")
_MID_CAPS = re.compile(r"[a-z]+[A-Z]")
_MID_DIGIT = re.compile(r"[a-z]+\d|\d+[a-z]+")
_WORD_RE = re.compile(r"[A-Za-z']+")

MIN_BASELINE_SAMPLES = 20
MAX_SAMPLES = 200

ROOM_BUDGET_TOKENS = 30


def typo_rate(message: str) -> float:
    """Deterministic typo heuristic. Baseline-relative, so roughness is fine."""
    words = _WORD_RE.findall(message or "")
    if not words:
        return 0.0
    typos = 0
    for word in words:
        lowered = word.lower()
        if lowered in CONFUSIONS or _REPEAT_CHAR.search(word) or len(word) > 3 and _MID_CAPS.search(word) or _MID_DIGIT.search(word):
            typos += 1
    return typos / len(words)


def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, math.sqrt(var)


def _z(value: float, samples: list[float]) -> float:
    mean, std = _mean_std(samples)
    if len(samples) < MIN_BASELINE_SAMPLES:
        return 0.0
    floor = max(0.1 * abs(mean), 0.05)
    return (value - mean) / max(std, floor)


def _sentence_shape(message: str) -> str:
    text = (message or "").strip()
    if not text:
        return "statement"
    if text.endswith("?"):
        return "question"
    first = re.findall(r"[a-z]+", text.lower())
    if first and first[0] in IMPERATIVE_VERBS:
        return "imperative"
    return "statement"


def _failed_recent(turns: list[dict[str, Any]], last_n: int = 3) -> int:
    failed = 0
    for turn in turns[-last_n:]:
        for receipt in turn.get("receipts") or []:
            ok = receipt[1] if isinstance(receipt, (list, tuple)) and len(receipt) > 1 else True
            if not ok:
                failed += 1
                break
    return failed


@dataclass(frozen=True)
class RoomReading:
    pace: str  # blitz | normal | deep
    pace_conf: float
    focus: str  # flow | stuck | exploring | winding_down
    focus_conf: float
    banter: float
    room_line: str
    features: dict[str, Any]


def _classify(
    length: int, gap_s: float, typo: float, length_z: float, gap_z: float,
    typo_z: float, urgency: int, depth: int, shape: str, burst: bool,
    failed: int, repeat: bool, last_reaction: str | None, hour: int,
    tuned: dict[str, Any],
) -> tuple[str, float, str, float]:
    # --- pace ---
    pace_signals = {"blitz": 0, "deep": 0}
    if length_z <= tuned["blitz_len_z"] and gap_z <= tuned["blitz_gap_z"]:
        pace_signals["blitz"] += 2
    if burst:
        pace_signals["blitz"] += 2
    if urgency >= int(tuned["urgency_min"]):
        pace_signals["blitz"] += 2
    if shape == "imperative" and length < 60:
        pace_signals["blitz"] += 1
    if depth >= int(tuned["depth_min"]):
        pace_signals["deep"] += 2
    if length_z >= tuned["deep_len_z"]:
        pace_signals["deep"] += 2
    if shape == "question" and length >= int(tuned["deep_min_chars"]):
        pace_signals["deep"] += 1
    if pace_signals["blitz"] > pace_signals["deep"] and pace_signals["blitz"] >= 2:
        pace, pace_conf = "blitz", min(0.95, 0.6 + 0.1 * pace_signals["blitz"])
    elif pace_signals["deep"] >= 2:
        pace, pace_conf = "deep", min(0.95, 0.6 + 0.1 * pace_signals["deep"])
    else:
        pace, pace_conf = "normal", 0.6

    # --- focus ---
    if failed >= int(tuned["stuck_fails"]) or repeat or last_reaction == "negative":
        focus, focus_conf = "stuck", 0.85 if failed else 0.7
    elif hour >= int(tuned["late_hour_start"]) or hour < int(tuned["early_hour_end"]):
        focus, focus_conf = "winding_down", 0.7
    elif gap_z > 2.0:
        focus, focus_conf = "winding_down", 0.6
    elif depth >= int(tuned["depth_min"]) or shape == "question":
        focus, focus_conf = "exploring", 0.7
    else:
        focus, focus_conf = "flow", 0.6
    return pace, round(pace_conf, 2), focus, round(focus_conf, 2)


_ROOM_LINES = {
    ("blitz", "stuck"): "He's moving fast and he's stuck: fix first, one line, joke only if it costs nothing.",
    ("blitz", "flow"): "He's moving fast: short replies, no questions back.",
    ("blitz", "exploring"): "He's moving fast but curious: answer tight, one sharp detail.",
    ("blitz", "winding_down"): "He's fading: wrap it up, nothing new.",
    ("deep", "stuck"): "He wants depth and he's stuck: explain the why, then the fix.",
    ("deep", "exploring"): "He's curious: go deep, compare properly.",
    ("deep", "flow"): "He's in flow and wants depth: match it.",
    ("deep", "winding_down"): "He's winding down: land the point, sleep soon.",
    ("normal", "stuck"): "He's stuck: own it in a clause, next concrete move, no pile-up.",
    ("normal", "exploring"): "He's curious: answer fully, one follow-up allowed.",
    ("normal", "winding_down"): "He's winding down: brief and warm.",
    ("normal", "flow"): "Steady: answer and move on.",
}

_COMPOSURE_TAIL = " Own the miss in a clause, give the next move, never fake progress."


def _fit_room_line(base: str, composure: bool) -> str:
    text = base + (_COMPOSURE_TAIL if composure else "")
    if estimate(text) <= ROOM_BUDGET_TOKENS:
        return text
    if composure and estimate(base) <= ROOM_BUDGET_TOKENS:
        return base
    words = base.split()
    while words and estimate(" ".join(words)) > ROOM_BUDGET_TOKENS:
        words.pop()
    return " ".join(words)


def _match_pattern(patterns: list[dict[str, Any]], query: str) -> str | None:
    """Best owner-pattern sharing ≥2 content terms with the query."""
    query_terms = {t for t in re.findall(r"[a-z0-9']+", query.lower()) if t not in STOPWORDS}
    if not query_terms or len(query_terms) < 1:
        return None
    best: tuple[int, str] | None = None
    for pattern in patterns:
        if not isinstance(pattern, dict):
            continue
        text = str(pattern.get("text", ""))
        terms = {t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in STOPWORDS}
        shared = len(query_terms & terms)
        if shared >= 2 and (best is None or shared > best[0]):
            best = (shared, text)
    return best[1] if best else None


def read_room(
    owner_id: str,
    message: str,
    *,
    now: datetime | None = None,
    record: bool = False,
    banter: float | None = None,
    patterns: list[dict[str, Any]] | None = None,
    path: Any = None,
) -> RoomReading:
    """Classify the owner's pace/focus. Pure unless record=True (appends one
    baseline sample AFTER computing, so baselines stay past-only)."""
    from app.persona_mind.state import coerce_ts

    moment = coerce_ts(now) if now is not None else coerce_ts(datetime.now().astimezone())
    try:
        local_hour = moment.astimezone().hour
    except (ValueError, OSError):
        local_hour = moment.hour

    samples = store.get_owner_samples(owner_id, path=path)
    tuned = active_tunables(path=path)
    length = len(message or "")
    typo = typo_rate(message)
    gap_s = 0.0
    if samples:
        try:
            gap_s = max(0.0, (moment - coerce_ts(samples[-1].get("ts"))).total_seconds())
        except (ValueError, TypeError, KeyError):
            gap_s = 0.0
    lengths = [float(s.get("len", 0)) for s in samples]
    gaps = [float(s.get("gap", 0)) for s in samples]
    typos = [float(s.get("typo", 0)) for s in samples]
    length_z = _z(float(length), lengths)
    gap_z = _z(gap_s, gaps)
    typo_z = _z(typo, typos)

    words = set(re.findall(r"[a-z']+", (message or "").lower()))
    urgency = len(words & URGENCY_WORDS)
    lowered = (message or "").lower()
    depth = len(words & DEPTH_WORDS)
    depth += sum(1 for phrase in DEPTH_PHRASES if phrase in lowered)
    shape = _sentence_shape(message)

    burst = False
    window = float(tuned["burst_window_s"])
    recent = sum(
        1 for s in samples
        if _sample_age_s(s, moment) <= window
    )
    if recent + 1 >= int(tuned["burst_n"]):
        burst = True

    turns = store.recent_turns(owner_id, 5, path=path)
    failed = _failed_recent(turns, 3)
    normalized = " ".join((message or "").split()).lower()
    repeat = bool(normalized) and any(
        " ".join(str(t.get("owner_msg") or "").split()).lower() == normalized
        for t in turns
    )
    last_reaction = str(turns[-1].get("reaction_to_prev")) if turns else None

    pace, pace_conf, focus, focus_conf = _classify(
        length, gap_s, typo, length_z, gap_z, typo_z, urgency, depth,
        shape, burst, failed, repeat, last_reaction, local_hour, tuned,
    )
    composure = focus == "stuck" or failed > 0
    line = _fit_room_line(_ROOM_LINES[(pace, focus)], composure and focus == "stuck")
    pattern_hit = _match_pattern(patterns or [], message or "")
    if pattern_hit and estimate(line + f" Note: {pattern_hit}") <= ROOM_BUDGET_TOKENS:
        line = line + f" Note: {pattern_hit}"

    if banter is None:
        row = store.get_state(owner_id, path=path)
        try:
            banter = float((row["data"] if row else {}).get("banter_appetite", 0.5))
        except (TypeError, ValueError):
            banter = 0.5

    if record:
        samples.append({
            "ts": moment.isoformat(), "len": length,
            "gap": round(gap_s, 1), "typo": round(typo, 4),
        })
        store.put_owner_samples(owner_id, samples[-MAX_SAMPLES:], path=path)

    return RoomReading(
        pace=pace, pace_conf=pace_conf, focus=focus, focus_conf=focus_conf,
        banter=min(1.0, max(0.0, float(banter))),
        room_line=line,
        features={
            "len": length, "len_z": round(length_z, 2), "gap_s": round(gap_s, 1),
            "gap_z": round(gap_z, 2), "typo": round(typo, 4),
            "typo_z": round(typo_z, 2), "urgency": urgency, "depth": depth,
            "shape": shape, "burst": burst, "failed_last3": failed,
            "repeat": repeat, "hour": local_hour,
            "baseline_n": len(samples), "composure": composure,
        },
    )


def _sample_age_s(sample: dict[str, Any], moment: datetime) -> float:
    try:
        return max(0.0, (moment - coerce_ts(sample.get("ts"))).total_seconds())
    except (ValueError, TypeError, KeyError):
        return float("inf")


def pace_hint(reading: RoomReading) -> dict[str, Any]:
    """Persona-engine shaping hint from a room reading."""
    if reading.pace == "blitz":
        hint = {"max_sentences": 2, "allow_humour": False, "ask_back_allowed": False}
    elif reading.pace == "deep":
        hint = {"max_sentences": 8, "allow_humour": True, "ask_back_allowed": True}
    else:
        hint = {"max_sentences": 4, "allow_humour": True, "ask_back_allowed": True}
    if reading.focus == "stuck":
        hint["allow_humour"] = False
        hint["max_sentences"] = min(hint["max_sentences"], 4)
    if reading.focus == "winding_down":
        hint["max_sentences"] = min(hint["max_sentences"], 2)
    return {"pace": reading.pace, "focus": reading.focus, **hint}
