"""Health radar: readiness scoring, anomaly detection, trends, morning brief."""

from __future__ import annotations

import statistics
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HealthSnapshot
from app.utils.text import utcnow


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def readiness_score(metrics: dict) -> tuple[float, str]:
    """0-100 readiness from sleep, HRV ratio, resting HR, activity, mood."""
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
) -> HealthSnapshot:
    occurred_at = occurred_at or utcnow()
    readiness, band = readiness_score(metrics)
    snapshot = HealthSnapshot(
        occurred_at=occurred_at,
        source=source,
        device_id=device_id,
        metrics=metrics,
        readiness=readiness,
        band=band,
        anomalies=[],
    )
    session.add(snapshot)
    await session.flush()
    snapshot.anomalies = await detect_anomalies(session, snapshot)
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
        }
    recommendation, question = morning_recommendation(latest.readiness, latest.metrics, latest.anomalies or [])
    return {
        "generated_at": utcnow(),
        "readiness": latest.readiness,
        "band": latest.band,
        "sleep_hours": latest.metrics.get("sleep_hours"),
        "hrv_ms": latest.metrics.get("hrv_ms"),
        "resting_hr": latest.metrics.get("resting_hr"),
        "recommendation": recommendation,
        "open_question": question,
        "anomalies": latest.anomalies or [],
    }
