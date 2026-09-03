"""Health radar: readiness scoring, anomaly detection, trends, morning brief."""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HealthSnapshot
from app.utils.text import utcnow


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# Amazfit Helio Strap → Zepp → Apple Health writes these (DC Rainmaker / firmware notes).
HELIO_ALIASES = {
    "hrv": "hrv_ms",
    "hrv_rmssd": "hrv_ms",
    "heart_rate_variability": "hrv_ms",
    "resting_heart_rate": "resting_hr",
    "hr": "heart_rate",
    "spo2_pct": "spo2",
    "blood_oxygen": "spo2",
    "oxygen_saturation": "spo2",
    "respiratory_rate": "resp_rate",
    "breathing_rate": "resp_rate",
    "vo2max": "vo2_max",
    "active_energy": "active_kcal",
    "calories": "active_kcal",
    "skin_temperature": "skin_temp_c",
    "biocharge": "readiness_raw",
    "pai": "pai",
    "stress_score": "stress",
}

HEALTH_STALE_AFTER_SECONDS = 24 * 60 * 60
DEFAULT_UNITS = {
    "heart_rate": "bpm",
    "resting_hr": "bpm",
    "hrv_ms": "ms",
    "steps": "count",
    "active_kcal": "kcal",
    "sleep_hours": "hours",
    "spo2": "percent",
    "resp_rate": "breaths/min",
    "vo2_max": "mL/kg/min",
    "stress": "score",
    "readiness_raw": "score",
    "recovery": "score",
    "workout_minutes": "minutes",
    "workout_count": "count",
}


def freshness_state(
    synced_at: datetime | None,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = HEALTH_STALE_AFTER_SECONDS,
) -> str:
    """Classify sync age so a health answer can never imply current data silently."""

    if synced_at is None:
        return "unknown"
    now = now or utcnow()
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    age = (now.astimezone(UTC) - synced_at.astimezone(UTC)).total_seconds()
    return "fresh" if age <= max(0, stale_after_seconds) else "stale"


def snapshot_freshness(snapshot: HealthSnapshot) -> str:
    """A sample is stale when either its measurement or sync is stale."""

    states = [
        freshness_state(snapshot.occurred_at),
        freshness_state(snapshot.synced_at),
    ]
    if "stale" in states:
        return "stale"
    return "fresh" if "fresh" in states else "unknown"


def normalize_metrics(metrics: dict) -> dict[str, float]:
    """Map Helio / HealthKit names onto the radar's canonical keys."""

    out: dict[str, float] = {}
    for key, value in (metrics or {}).items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        name = HELIO_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
        out[name] = float(value)
    if "resting_hr" not in out and "heart_rate" in out:
        out["resting_hr"] = out["heart_rate"]
    return out


# Absolute bands (not z-scores). Used when a single live sample is dangerous
# even if we do not yet have a 14-day baseline — Karen-style body scan.
CLINICAL_RULES: list[tuple[str, str, float, str]] = [
    ("heart_rate", "high", 140.0, "Heart rate is very high at rest."),
    ("heart_rate", "low", 40.0, "Heart rate is unusually low."),
    ("resting_hr", "high", 110.0, "Resting heart rate is elevated."),
    ("resting_hr", "low", 38.0, "Resting heart rate is unusually low."),
    ("spo2", "low", 92.0, "Blood oxygen is below a safe band."),
    ("hrv_ms", "low", 15.0, "HRV has crashed — recovery looks poor."),
    ("stress", "high", 85.0, "Stress reading is in the top band."),
]


def clinical_flags(metrics: dict) -> list[dict]:
    flags: list[dict] = []
    data = normalize_metrics(metrics)
    for metric, side, bound, text in CLINICAL_RULES:
        value = data.get(metric)
        if value is None:
            continue
        hit = value >= bound if side == "high" else value <= bound
        if hit:
            flags.append(
                {
                    "metric": metric,
                    "value": float(value),
                    "side": side,
                    "bound": bound,
                    "rationale": text,
                    "clinical": True,
                    "emergency": metric in {"heart_rate", "resting_hr", "spo2"},
                }
            )
    return flags


def readiness_score(metrics: dict) -> tuple[float, str]:
    """0-100 readiness from sleep, HRV ratio, resting HR, activity, mood."""
    metrics = normalize_metrics(metrics)
    sleep_hours = metrics.get("sleep_hours")
    hrv_ms = metrics.get("hrv_ms")
    resting_hr = metrics.get("resting_hr")
    steps = metrics.get("steps")
    mood = metrics.get("mood")

    sleep_quality = clamp(sleep_hours / 7.5) if sleep_hours is not None else 0.7
    sleep_quality = 0.8 * sleep_quality + (0.2 if sleep_hours is not None and sleep_hours >= 6.0 else 0.0)

    hrv_ratio = clamp(hrv_ms / 60.0, 0.0, 1.2) if hrv_ms is not None else 1.0
    resting_norm = clamp(1.0 - abs(resting_hr - 62.0) / 62.0) if resting_hr is not None else 1.0
    activity = clamp(steps / 8000.0) if steps is not None else 0.7
    if mood is not None:
        # Accept both 0..1 and -2..2 conventions.
        mood_norm = clamp((mood + 2.0) / 4.0) if mood < 0 else clamp(mood)
    else:
        mood_norm = 0.7

    readiness = (
        0.35 * sleep_quality
        + 0.25 * hrv_ratio
        + 0.20 * resting_norm
        + 0.10 * activity
        + 0.10 * mood_norm
    )
    readiness = round(clamp(readiness) * 100.0, 1)
    if readiness < 45:
        band = "Low"
    elif readiness < 65:
        band = "Moderate"
    elif readiness < 85:
        band = "Good"
    else:
        band = "Excellent"
    return readiness, band


async def detect_anomalies(session: AsyncSession, snapshot: HealthSnapshot) -> list[dict]:
    """z-score anomalies against the prior 14-day window; |z|>=3 or |z|>=2 sustained."""
    anomalies: list[dict] = []
    if not snapshot.metrics:
        return anomalies
    since = snapshot.occurred_at - timedelta(days=14)
    result = await session.execute(
        select(HealthSnapshot)
        .where(
            HealthSnapshot.occurred_at >= since,
            HealthSnapshot.occurred_at < snapshot.occurred_at,
        )
        .order_by(HealthSnapshot.occurred_at.asc())
    )
    prior = list(result.scalars().all())
    for metric, value in snapshot.metrics.items():
        if not isinstance(value, (int, float)):
            continue
        values: list[float] = []
        for p in prior:
            raw = p.metrics.get(metric)
            if isinstance(raw, (int, float)):
                values.append(float(raw))
        if len(values) < 2:
            continue
        mean = statistics.mean(values)
        stdev = statistics.stdev(values)
        if stdev == 0:
            continue
        z = (float(value) - mean) / stdev
        if abs(z) >= 3.0:
            anomalies.append(
                {
                    "metric": metric,
                    "value": float(value),
                    "z": round(z, 2),
                    "baseline_mean": round(mean, 2),
                    "rationale": f"|z|={abs(z):.2f} >= 3 against the 14-day window",
                    "sustained": False,
                }
            )
        elif abs(z) >= 2.0:
            # Sustained check: the immediately preceding snapshot also outside 2 sigma.
            sustained = False
            if prior:
                prev = prior[-1]
                prev_value = prev.metrics.get(metric)
                if isinstance(prev_value, (int, float)) and len(values) >= 3:
                    prev_mean = statistics.mean(values[:-1])
                    prev_sd = statistics.stdev(values[:-1])
                    if prev_sd:
                        prev_z = (float(prev_value) - prev_mean) / prev_sd
                        sustained = abs(prev_z) >= 2.0
            anomalies.append(
                {
                    "metric": metric,
                    "value": float(value),
                    "z": round(z, 2),
                    "baseline_mean": round(mean, 2),
                    "rationale": f"|z|={abs(z):.2f} >= 2{' and sustained for a second day' if sustained else ''}",
                    "sustained": sustained,
                }
            )
    return anomalies


async def create_snapshot(
    session: AsyncSession,
    *,
    metrics: dict,
    source: str = "api",
    device_id: str | None = None,
    occurred_at=None,
    permission_state: str = "authorized",
    synced_at: datetime | None = None,
    units: dict | None = None,
    source_metadata: dict | None = None,
) -> HealthSnapshot:
    occurred_at = occurred_at or utcnow()
    synced_at = synced_at or utcnow()
    metrics = normalize_metrics(metrics) if permission_state == "authorized" else {}
    readiness, band = readiness_score(metrics) if metrics else (None, None)
    source_metadata = dict(source_metadata or {})
    source_metadata.setdefault(
        "provider_chain",
        ["Amazfit Helio", "Zepp", "Apple Health", "HealthKit", "EV iOS bridge"]
        if source in {"amazfit_helio", "zepp", "healthkit"}
        else [source],
    )
    resolved_units = {**DEFAULT_UNITS, **(units or {})}
    snapshot = HealthSnapshot(
        occurred_at=occurred_at,
        source=source,
        device_id=device_id,
        metrics=metrics,
        readiness=readiness,
        band=band,
        anomalies=[],
        permission_state=permission_state,
        synced_at=synced_at,
        units=resolved_units,
        source_metadata=source_metadata,
        freshness_state=(
            "stale"
            if freshness_state(occurred_at) == "stale" or freshness_state(synced_at) == "stale"
            else "fresh"
        ),
    )
    session.add(snapshot)
    await session.flush()
    try:
        from app.services.event_service import EventService
        from app.services.life_stream_daemon import health_snapshot_event_create

        payload = health_snapshot_event_create(snapshot)
        if payload is not None:
            await EventService(session, actor="health_radar").create(payload)
    except Exception:  # noqa: BLE001 - memory mirror must not drop the snapshot
        pass
    z_flags = await detect_anomalies(session, snapshot)
    snapshot.anomalies = [*clinical_flags(metrics), *z_flags]
    return snapshot


async def trend(
    session: AsyncSession,
    *,
    metric: str,
    window_days: int = 14,
) -> dict:
    since = utcnow() - timedelta(days=window_days)
    result = await session.execute(
        select(HealthSnapshot)
        .where(HealthSnapshot.occurred_at >= since)
        .order_by(HealthSnapshot.occurred_at.asc())
    )
    rows = list(result.scalars().all())
    points = []
    values = []
    anomalies: list[dict] = []
    for row in rows:
        value = row.metrics.get(metric)
        if not isinstance(value, (int, float)):
            continue
        points.append({"occurred_at": row.occurred_at, "value": float(value)})
        values.append(float(value))
        for anomaly in row.anomalies:
            if anomaly.get("metric") == metric:
                anomalies.append({**anomaly, "occurred_at": row.occurred_at})
    baseline_median = statistics.median(values) if values else None
    current = values[-1] if values else None
    z_scores = []
    if len(values) >= 3:
        mean = statistics.mean(values)
        stdev = statistics.stdev(values)
        z_scores = [round((v - mean) / stdev, 2) if stdev else 0.0 for v in values]
    return {
        "metric": metric,
        "points": points,
        "baseline_median": baseline_median,
        "current": current,
        "z_scores": z_scores,
        "anomalies": anomalies,
        "freshness_state": snapshot_freshness(rows[-1]) if rows else "unknown",
        "last_sync_at": rows[-1].synced_at if rows else None,
        "source": rows[-1].source if rows else None,
        "permission_state": rows[-1].permission_state if rows else "unknown",
    }


def morning_recommendation(readiness: float | None, metrics: dict, anomalies: list[dict]) -> tuple[str, str | None]:
    if anomalies:
        metric = anomalies[0].get("metric", "")
        return (
            f"One metric is off today ({metric}); keep the load light and re-check before a hard push.",
            f"What made {metric} move — is it external or recoverable?",
        )
    if readiness is not None and readiness < 45:
        return "Protect an early night — readiness is low and a heavy day will compound it.", None
    if metrics.get("sleep_hours") is not None and metrics["sleep_hours"] < 6.5:
        return "Sleep is under target; schedule the hard work earlier and guard recovery time.", None
    if readiness is not None and readiness >= 75:
        return "Readiness is high — this is the window to make progress on the active goal.", "What is the one thing that must move today?"
    return "Steady state. Protect one focused block and defer non-essential decisions.", None


async def morning_brief(session: AsyncSession) -> dict:
    result = await session.execute(select(HealthSnapshot).order_by(HealthSnapshot.occurred_at.desc()).limit(1))
    latest = result.scalars().first()
    if latest is None:
        return {
            "generated_at": utcnow(),
            "readiness": None,
            "band": None,
            "recommendation": "No health data yet. Share a snapshot and I'll start tracking trends.",
            "anomalies": [],
            "freshness_state": "unknown",
            "last_sync_at": None,
            "permission_state": "unknown",
        }
    recommendation, question = morning_recommendation(latest.readiness, latest.metrics, latest.anomalies or [])
    metrics = normalize_metrics(latest.metrics or {})
    return {
        "generated_at": utcnow(),
        "readiness": latest.readiness,
        "band": latest.band,
        "sleep_hours": metrics.get("sleep_hours"),
        "hrv_ms": metrics.get("hrv_ms"),
        "resting_hr": metrics.get("resting_hr"),
        "heart_rate": metrics.get("heart_rate"),
        "spo2": metrics.get("spo2"),
        "stress": metrics.get("stress"),
        "source": latest.source,
        "permission_state": latest.permission_state,
        "freshness_state": snapshot_freshness(latest),
        "last_sync_at": latest.synced_at,
        "source_metadata": latest.source_metadata or {},
        "recommendation": recommendation,
        "open_question": question,
        "anomalies": latest.anomalies or [],
        "emergency": any(flag.get("emergency") for flag in (latest.anomalies or [])),
    }


async def latest_clinical(session: AsyncSession) -> dict:
    """Latest snapshot's clinical/emergency state, or empty if none."""

    result = await session.execute(select(HealthSnapshot).order_by(HealthSnapshot.occurred_at.desc()).limit(1))
    latest = result.scalars().first()
    if latest is None:
        return {"emergency": False, "flags": [], "readiness": None}
    flags = [row for row in (latest.anomalies or []) if row.get("clinical") or row.get("emergency")]
    return {
        "emergency": any(row.get("emergency") for row in flags) or (
            latest.readiness is not None and latest.readiness < 35
        ),
        "flags": flags,
        "readiness": latest.readiness,
        "source": latest.source,
        "metrics": normalize_metrics(latest.metrics or {}),
        "freshness_state": snapshot_freshness(latest),
        "last_sync_at": latest.synced_at,
        "permission_state": latest.permission_state,
        "source_metadata": latest.source_metadata or {},
    }
