"""Self-diagnostics: E.V.-style calibration of database, embeddings, gateway, retrieval, storage."""

from __future__ import annotations

import time
from typing import Literal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import ChatMessage
from app.embeddings import get_embedder
from app.gateway.providers import get_chat_provider
from app.memory.retrieval import Retriever
from app.schemas import CalibrationReport, DiagnosticCheck
from app.storage.object_store import get_object_store
from app.utils.text import utcnow

Status = Literal["ok", "degraded", "failed"]


def _check(name: str, latency_ms: float, ok: bool, details: dict | None = None) -> DiagnosticCheck:
    status: Status = "ok" if ok else ("degraded" if latency_ms > 2000 else "failed")
    if not ok and latency_ms <= 2000:
        status = "failed"
    return DiagnosticCheck(name=name, status=status, latency_ms=round(latency_ms, 1), details=details or {})


async def _hardware_checks(session: AsyncSession) -> list[DiagnosticCheck]:
    """Append-only extension point for printer/radio/telemetry adapters."""

    extra: list[DiagnosticCheck] = []
    started = time.perf_counter()
    try:
        from app.ev.hardware import ping_printer

        ping = await ping_printer(session)
        if ping.get("configured"):
            extra.append(
                _check(
                    "octoprint.ping",
                    (time.perf_counter() - started) * 1000,
                    bool(ping.get("ok")),
                    {"configured": True, "details": ping},
                )
            )
    except Exception as exc:  # pragma: no cover - defensive
        extra.append(
            _check("octoprint.ping", (time.perf_counter() - started) * 1000, False, {"error": str(exc)})
        )

    started = time.perf_counter()
    try:
        from app.ev.hardware import last_sample

        sample = await last_sample(session)
        if sample is not None:
            rssi = (sample.details or {}).get("rssi")
            extra.append(
                _check(
                    "radio.rssi",
                    (time.perf_counter() - started) * 1000,
                    rssi is None or float(rssi) > -90,
                    {"source": sample.source, "rssi": rssi},
                )
            )
    except Exception as exc:  # pragma: no cover - defensive
        extra.append(
            _check("radio.rssi", (time.perf_counter() - started) * 1000, False, {"error": str(exc)})
        )
    return extra


async def run_calibration(
    session: AsyncSession,
    *,
    announce: bool = True,
    cheap: bool = False,
) -> CalibrationReport:
    checks: list[DiagnosticCheck] = []

    # Database.
    started = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
        checks.append(_check("database", (time.perf_counter() - started) * 1000, True))
    except Exception as exc:  # pragma: no cover - defensive
        checks.append(_check("database", (time.perf_counter() - started) * 1000, False, {"error": str(exc)}))

    if not cheap:
        # Embeddings.
        started = time.perf_counter()
        try:
            embedder = get_embedder()
            vec = (await embedder.embed(["EV calibration probe"]))[0]
            checks.append(
                _check(
                    "embeddings",
                    (time.perf_counter() - started) * 1000,
                    True,
                    {"provider": embedder.name, "dim": len(vec)},
                )
            )
        except Exception as exc:
            checks.append(_check("embeddings", (time.perf_counter() - started) * 1000, False, {"error": str(exc)}))

        # Chat gateway.
        started = time.perf_counter()
        try:
            provider = get_chat_provider()
            result = await provider.chat(
                [ChatMessage(role="user", content="EV calibration ping")],
                model=(
                    settings.xai_model
                    if provider.name == "xai"
                    else settings.deepseek_model
                    if provider.name == "deepseek"
                    else None
                ),
            )
            checks.append(
                _check(
                    "chat_gateway",
                    (time.perf_counter() - started) * 1000,
                    True,
                    {"provider": provider.name, "model": result.model or "unknown"},
                )
            )
        except Exception as exc:
            checks.append(_check("chat_gateway", (time.perf_counter() - started) * 1000, False, {"error": str(exc)}))

        # Retrieval.
        started = time.perf_counter()
        try:
            retriever = Retriever(session, embeddings=get_embedder())
            hits = await retriever.search("calibration probe", k=5, access="model")
            checks.append(
                _check(
                    "retrieval",
                    (time.perf_counter() - started) * 1000,
                    True,
                    {"matched_memories": len(hits)},
                )
            )
        except Exception as exc:
            checks.append(_check("retrieval", (time.perf_counter() - started) * 1000, False, {"error": str(exc)}))

    # Object storage.
    started = time.perf_counter()
    try:
        store = get_object_store()
        key = f"probe/{uuid4()}.txt"
        await store.put(key, b"EV calibration")
        data = await store.get(key)
        await store.delete(key)
        checks.append(
            _check(
                "object_storage",
                (time.perf_counter() - started) * 1000,
                data == b"EV calibration",
                {"roundtrip": data == b"EV calibration"},
            )
        )
    except Exception as exc:
        checks.append(_check("object_storage", (time.perf_counter() - started) * 1000, False, {"error": str(exc)}))

    checks.extend(await _hardware_checks(session))

    failed = [c for c in checks if c.status == "failed"]
    degraded = [c for c in checks if c.status == "degraded"]
    overall: Status = "failed" if failed else ("degraded" if degraded else "ok")

    recommendations: list[str] = []
    if any(c.name == "embeddings" and c.status == "failed" for c in checks):
        recommendations.append("Embeddings are unavailable — retrieval will degrade to keyword-only.")
    if any(c.name == "chat_gateway" and c.status == "failed" for c in checks):
        recommendations.append("The chat provider is unreachable. Check EV_DEEPSEEK_API_KEY / network.")
    if any(c.name == "object_storage" and c.status == "failed" for c in checks):
        recommendations.append("Object storage failed — attachments will not persist.")
    if not recommendations:
        recommendations.append("All systems calibrated. Continue as planned.")
    report = CalibrationReport(
        schema_version="ev.calibration.v1",
        generated_at=utcnow(),
        overall=overall,
        checks=checks,
        recommendations=recommendations,
    )
    from app.ev.assistant import cache_calibration, malfunction_line
    from app.ev.callouts import emit_callout

    await cache_calibration(session, report)
    from app.ev.workbench import cache_hud, calibration_hud

    await cache_hud(
        session,
        calibration_hud(report.model_dump(mode="json")),
        source="calibrate",
    )
    if announce:
        line = malfunction_line(report.model_dump(mode="json")) or (
            f"Calibration complete: {overall}."
        )
        await emit_callout(
            session,
            line,
            source="calibrate",
            hud={
                "schema_version": "ev.hud.card.v1",
                "title": "Diagnostics",
                "body": line,
                "generated_at": report.generated_at.isoformat(),
            },
        )
    return report
