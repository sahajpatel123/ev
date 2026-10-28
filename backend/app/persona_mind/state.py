"""Dynamic state: shape, decay math, seeding, expiry. Pure functions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.persona_mind.settings import AFFECT_DIMS, HALF_LIFE_S, MIND_VERSION
from app.persona_mind.temperament import BASELINE


def utcnow() -> datetime:
    return datetime.now(UTC)


def coerce_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        ts = value if value.tzinfo else value.replace(tzinfo=UTC)
        return ts.astimezone(UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return utcnow()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def clamp01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def blank_affect() -> dict[str, float]:
    return dict(BASELINE)


def decayed_vector(
    vector: dict[str, float], baseline: dict[str, float], elapsed_s: float
) -> dict[str, float]:
    """Exponential decay toward baseline with a 6 h half-life (pure math)."""
    if elapsed_s <= 0:
        return {dim: clamp01(vector.get(dim, baseline[dim])) for dim in AFFECT_DIMS}
    factor = 0.5 ** (max(0.0, elapsed_s) / HALF_LIFE_S)
    out = {}
    for dim in AFFECT_DIMS:
        base = baseline[dim]
        out[dim] = clamp01(base + (clamp01(vector.get(dim, base)) - base) * factor)
    return out


def apply_decay(state: dict[str, Any], now: datetime) -> dict[str, float]:
    """Current affect: stored vector decayed from its timestamp to now."""
    vector = state.get("affect") or {}
    updated = coerce_ts(state.get("affect_updated_at") or now.isoformat())
    elapsed = (coerce_ts(now) - updated).total_seconds()
    return decayed_vector(vector, BASELINE, elapsed)


def dominant_dims(vector: dict[str, float], top: int = 3) -> list[str]:
    ordered = sorted(AFFECT_DIMS, key=lambda d: (-clamp01(vector.get(d, 0.0)), d))
    return ordered[: max(1, top)]


def new_state(owner_id: str, now: datetime | None = None) -> dict[str, Any]:
    moment = (now or utcnow()).isoformat()
    return {
        "version": MIND_VERSION,
        "owner_id": owner_id,
        "affect": blank_affect(),
        "affect_updated_at": moment,
        "mood_words": [],
        "opinions": [],
        "changed_mind": [],
        "recommendations": [],
        "rec_seq": 0,
        "domains": {},
        "intentions": [],
        "rapport": 0.5,
        "banter_appetite": 0.5,
        "threads": [],
        "bits": [],
        "attitudes": [],
        "owner_patterns": [],
        "narrative": "",
        "reaction_log": [],
        "recent_replies": [],
        "seeded": False,
        "created_at": moment,
    }


ATTITUDE_HALF_LIFE_S = 7 * 86400


def attitude_strength(attitude: dict[str, Any], now: datetime) -> float:
    """Current strength: exponential decay with a 7-day half-life."""
    try:
        elapsed = (coerce_ts(now) - coerce_ts(attitude.get("updated_at"))).total_seconds()
    except (ValueError, TypeError):
        return 0.0
    if elapsed <= 0:
        return clamp01(float(attitude.get("strength", 0.0)))
    return clamp01(float(attitude.get("strength", 0.0)) * 0.5 ** (elapsed / ATTITUDE_HALF_LIFE_S))


def expire_items(state: dict[str, Any], now: datetime) -> dict[str, int]:
    """Drop expired intentions (72 h), threads (7 d) and spent attitudes."""
    moment = coerce_ts(now)
    dropped = {"intentions": 0, "threads": 0}
    kept_intentions = []
    for item in state.get("intentions") or []:
        try:
            expired = coerce_ts(item.get("expires_at")) <= moment
        except Exception:
            expired = True
        if expired:
            dropped["intentions"] += 1
        else:
            kept_intentions.append(item)
    state["intentions"] = kept_intentions[:3]
    kept_threads = []
    for item in state.get("threads") or []:
        try:
            expired = coerce_ts(item.get("expires_at")) <= moment
        except Exception:
            expired = True
        if expired:
            dropped["threads"] += 1
        else:
            kept_threads.append(item)
    state["threads"] = kept_threads
    kept_attitudes = [
        item for item in (state.get("attitudes") or [])
        if isinstance(item, dict) and attitude_strength(item, now) >= 0.05
    ]
    dropped["attitudes"] = len(state.get("attitudes") or []) - len(kept_attitudes)
    state["attitudes"] = kept_attitudes
    return dropped


def record_reaction(state: dict[str, Any], turn_id: str, reaction: str | None, ts: str) -> None:
    if reaction not in ("positive", "neutral", "negative"):
        return
    log = state.get("reaction_log") or []
    log.append({"turn_id": turn_id, "reaction": reaction, "ts": ts})
    state["reaction_log"] = log[-200:]


def has_positive_since(state: dict[str, Any], turn_id: str, turn_ts: str) -> bool:
    """A positive reaction recorded at/after the cited turn's timestamp."""
    try:
        floor = coerce_ts(turn_ts)
    except Exception:
        return False
    for entry in state.get("reaction_log") or []:
        if entry.get("reaction") != "positive":
            continue
        try:
            if coerce_ts(entry.get("ts")) >= floor:
                return True
        except Exception:
            continue
    return False


def push_reply(state: dict[str, Any], reply: str) -> None:
    replies = state.get("recent_replies") or []
    text = " ".join((reply or "").split())
    if text:
        replies.append(text[:500])
    state["recent_replies"] = replies[-20:]


def domain_rate(state: dict[str, Any], domain: str) -> float | None:
    entry = (state.get("domains") or {}).get((domain or "").strip().lower())
    if not entry or not entry.get("total"):
        return None
    return float(entry.get("hit", 0)) / float(entry["total"])
