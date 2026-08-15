"""Workbench voice/HUD tools: diagnostics, research, health, plate, sense, locate.

Tools register through ``app.ev.tools`` and share one HUD schema
(``ev.hud.card.v1`` / ``ev.hud.briefing.v1``). Glasses never get a forked schema.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from uuid import UUID
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.assistant import last_calibration_report, malfunction_line, worst_check
from app.ev.hud import validate_hud
from app.models import (
    Alert,
    Callout,
    Delegate,
    Entity,
    HudPush,
    LocationShare,
    MailDraft,
    PublicFeed,
    WatchlistItem,
)
from app.schemas import (
    ResearchConclude,
    ResearchNoteCreate,
    ResearchSessionCreate,
    TacticalBriefRequest,
    WatchlistCreate,
)
from app.utils.text import normalize_text, utcnow

STATUS_TOOLS = frozenset(
    {
        "calibrate",
        "research",
        "get_weather",
        "brief_me",
        "brief_share",
        "gear_power",
        "health_how_do_i_look",
        "head_injury_screen",
        "whats_on_my_plate",
        "why_did_you_ping",
        "print_start",
        "estimate_print",
        "where_is",
        "camera_replay",
        "watchlist_add",
        "alerts_digest",
        "media_check",
        "set_voice",
        "public_lookup",
        "find_gear",
        "estimate_structure",
        "draft_reply",
        "drone",
    }
)

HEAD_INJURY_DISCLAIMER = (
    "I'm not a doctor. Get medical care if symptoms worsen, you lose consciousness, "
    "you vomit, vision changes, or you feel worse."
)
HEAD_INJURY_QUESTIONS = (
    "Did you lose consciousness, even briefly?",
    "Are you nauseous or vomiting?",
    "Any vision changes, worsening headache, or confusion?",
    "Do you want me to call someone?",
)
CONCUSSION_YES_NO_RE = re.compile(
    r"\b(you (?:have|don't have|do not have) a concussion|"
    r"this (?:is|isn't|is not) a concussion|"
    r"diagnosed with (?:a )?concussion|"
    r"concussion[:\s]+(?:yes|no))\b",
    re.IGNORECASE,
)
INTERROGATION_RE = re.compile(r"\b(interrogation|intimidate|intimidation)\b", re.IGNORECASE)
PRIVATE_PERSON_PII_RE = re.compile(
    r"\b(phone|mobile|cell|home(?:\s+address)?|address|ssn|social security)\b",
    re.IGNORECASE,
)
ORG_HINT_RE = re.compile(
    r"\b(inc|llc|ltd|corp|company|agency|university|hospital|ministry|department)\b",
    re.IGNORECASE,
)
PUBLIC_RECORD_HOSTS = (
    "wikipedia.org",
    "sec.gov",
    "edgar.sec.gov",
    "europa.eu",
    "legislation.gov.uk",
    "congress.gov",
    "who.int",
    "gov.uk",
    "canada.ca",
    "companieshouse.gov.uk",
    "open-meteo.com",
)
CITATION_URL_RE = re.compile(r"https?://[^\s\]\)>'\"<>]+", re.IGNORECASE)
BANNED_VOICE_RE = INTERROGATION_RE


def hud_card(title: str, body: str, meta: dict | None = None, *, priority: float = 0.3) -> dict:
    return {
        "schema_version": "ev.hud.card.v1",
        "generated_at": utcnow().isoformat(),
        "title": title,
        "body": body,
        "priority": priority,
        "meta": meta or {},
    }


def calibration_hud(report: dict) -> dict:
    checks = list(report.get("checks") or [])
    rows = [
        f"{c.get('name')}: {c.get('status')}"
        for c in checks
        if isinstance(c, dict)
    ]
    worst = worst_check(report)
    if worst:
        body = f"{worst.get('name')} is {worst.get('status')}."
    else:
        body = f"Calibration {report.get('overall') or 'ok'}."
    if rows:
        body = body + " " + "; ".join(rows)
    return hud_card(
        "Diagnostics",
        body,
        {
            "overall": report.get("overall"),
            "rows": [
                {"name": c.get("name"), "status": c.get("status"), "latency_ms": c.get("latency_ms")}
                for c in checks
                if isinstance(c, dict)
            ],
        },
        priority=0.7 if report.get("overall") == "failed" else 0.3,
    )


def last_diagnostics_payload(report: dict | None) -> dict:
    if not report:
        return {
            "schema_version": "ev.diagnostics.last.v1",
            "generated_at": None,
            "stale": True,
            "overall": "unknown",
            "report": None,
            "hud": hud_card("Diagnostics", "No calibration yet. Strip is stale.", {"stale": True}),
        }
    generated = report.get("generated_at")
    return {
        "schema_version": "ev.diagnostics.last.v1",
        "generated_at": generated,
        "stale": False,
        "overall": report.get("overall") or "unknown",
        "report": report,
        "hud": calibration_hud(report),
    }


async def handle_calibrate(session: AsyncSession) -> dict:
    from app.ev.diagnostics import run_calibration

    report = await run_calibration(session, announce=True)
    payload = report.model_dump(mode="json")
    worst = worst_check(payload)
    if worst:
        spoken = f"{worst.get('name')} is {worst.get('status')}."
    else:
        spoken = f"Calibration {payload.get('overall') or 'ok'}."
    hud = calibration_hud(payload)
    return {
        "ok": True,
        "overall": payload.get("overall"),
        "checks": payload.get("checks") or [],
        "recommendations": payload.get("recommendations") or [],
        "spoken": spoken,
        "hud": hud,
        "report": payload,
    }


def diagnostics_fingerprint(name: str, status: str) -> str:
    return f"diagnostics:{name}:{status}"


async def fingerprint_seen(session: AsyncSession, fingerprint: str) -> bool:
    existing = (
        await session.execute(
            select(Callout.id).where(Callout.source_item == fingerprint).limit(1)
        )
    ).scalar_one_or_none()
    return existing is not None


async def emit_new_failed_diagnostics(session: AsyncSession, report: dict) -> list[Callout]:
    from app.ev.callouts import emit_callout

    emitted: list[Callout] = []
    for check in report.get("checks") or []:
        if not isinstance(check, dict) or check.get("status") != "failed":
            continue
        fp = diagnostics_fingerprint(str(check.get("name") or "system"), "failed")
        if await fingerprint_seen(session, fp):
            continue
        line = f"{check.get('name')} failed."
        row = await emit_callout(
            session,
            line,
            source="diagnostics",
            source_item=fp,
            hud=calibration_hud(report),
        )
        emitted.append(row)
        break
    return emitted


async def maybe_calibration_tick(session: AsyncSession) -> dict:
    from app.ev.diagnostics import run_calibration

    report = await run_calibration(session, announce=False, cheap=True)
    payload = report.model_dump(mode="json")
    rows = await emit_new_failed_diagnostics(session, payload)
    return {
        "overall": payload.get("overall"),
        "callouts": len(rows),
        "fingerprints": [r.source_item for r in rows],
    }


async def cache_hud(session: AsyncSession, payload: dict, *, source: str = "tool") -> dict:
    schema, model = validate_hud(payload)
    dumped = model.model_dump(mode="json")
    dumped["schema_version"] = schema
    session.add(
        HudPush(
            schema_version=schema,
            payload=dumped,
            source=source,
            prefer_haptic=True,
            created_at=utcnow(),
        )
    )
    await session.flush()
    return dumped


def _aware_dt(value):
    """SQLite often returns naive datetimes; never subtract those from aware utcnow()."""

    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=utcnow().tzinfo)
    return value


async def last_hud_payload(session: AsyncSession) -> dict | None:
    row = (
        await session.execute(select(HudPush).order_by(HudPush.created_at.desc()).limit(1))
    ).scalars().first()
    if row is None:
        return None
    created = _aware_dt(row.created_at)
    try:
        age = (utcnow() - created).total_seconds() if created is not None else 0.0
    except TypeError:
        age = 0.0
    if age > 15 * 60:
        return None
    return dict(row.payload or {})


async def push_status_hud(
    session: AsyncSession,
    tool_name: str,
    result: dict,
    *,
    message: str = "",
) -> dict | None:
    if tool_name not in STATUS_TOOLS or not isinstance(result, dict):
        return None
    payload = result.get("hud") or result.get("briefing")
    if not isinstance(payload, dict) or not payload.get("schema_version"):
        return None
    try:
        dumped = await cache_hud(session, payload, source=tool_name)
    except ValueError:
        return None
    result["hud"] = dumped
    try:
        from app.ev.lookout import compose_and_maybe_open

        await compose_and_maybe_open(
            session,
            message=message or tool_name,
            reply=str(result.get("spoken") or payload.get("body") or ""),
            title=str(payload.get("title") or tool_name),
            explicit=True,
        )
    except Exception:
        pass
    return dumped


# --------------------------------------------------------------------------- #
# Research
# --------------------------------------------------------------------------- #


async def handle_research(session: AsyncSession, question: str) -> dict:
    from app.ev.research import ResearchService, list_notes
    from app.memory.retrieval import Retriever
    from app.search.providers import get_search_provider

    svc = ResearchService(session, actor="workbench")
    research = await svc.create_session(ResearchSessionCreate(question=question))
    retriever = Retriever(session)
    memory_hits = await retriever.search(question, k=5, access="model")
    for hit in memory_hits[:3]:
        await svc.add_note(
            research.id,
            ResearchNoteCreate(
                note=hit.text,
                source_title="memory",
                source_url=None,
            ),
        )
    memory_only = False
    web_error = None
    try:
        if get_search_provider() is None:
            raise KeyError("web search is disabled")
        await svc.web_search(research.id, question, limit=5)
    except KeyError as exc:
        memory_only = True
        web_error = str(exc)
    notes = await list_notes(session, research.id)
    citations = []
    for note in notes:
        if note.source_url:
            citations.append({"title": note.source_title or "source", "url": note.source_url})
    if memory_hits and not citations and memory_only:
        answer = (memory_hits[0].text or question)[:280]
    elif notes:
        answer = (notes[-1].note or notes[0].note)[:280]
    else:
        answer = f"No stored notes yet for {question}."
    n = len(citations)
    if memory_only:
        spoken = f"{answer} Memory-only — web search is disabled."
    else:
        spoken = f"{answer} according to {n} sources."
    conclusion = spoken
    try:
        await svc.conclude(research.id, ResearchConclude(conclusion=conclusion[:4000]))
    except ValueError:
        pass
    hud = hud_card(
        "Research",
        spoken,
        {"citations": citations, "memory_only": memory_only, "question": question},
    )
    return {
        "ok": True,
        "answer": answer,
        "citations": citations,
        "spoken": spoken,
        "memory_only": memory_only,
        "web_disabled": memory_only,
        "reason": web_error,
        "hud": hud,
        "session_id": str(research.id),
    }


# --------------------------------------------------------------------------- #
# Weather HUD
# --------------------------------------------------------------------------- #


def weather_hud(results: list[dict]) -> dict:
    first = results[0] if results else {}
    body = str(first.get("snippet") or first.get("title") or "No weather yet.")
    return hud_card(
        str(first.get("title") or "Weather"),
        body,
        {"results": results, "source": "weather_results"},
    )


# --------------------------------------------------------------------------- #
# Brief
# --------------------------------------------------------------------------- #


async def handle_brief_me(session: AsyncSession, topic: str | None = None) -> dict:
    from app.ev import tactical

    brief = await tactical.build_briefing(
        session,
        TacticalBriefRequest(topic=topic or "today", include_options=True),
    )
    payload = brief.model_dump(mode="json")
    spoken = (
        f"{brief.objective}. {brief.recommendation or ''} "
        f"{(brief.risks[0].description if brief.risks else '')}"
    ).strip()
    return {
        "ok": True,
        "objective": brief.objective,
        "context": brief.context,
        "people": payload.get("people") or [],
        "risks": payload.get("risks") or [],
        "options": payload.get("options") or [],
        "recommendation": brief.recommendation,
        "talking_points": payload.get("talking_points") or [],
        "spoken": spoken[:400],
        "hud": payload,
        "briefing": payload,
    }


async def handle_brief_share(session: AsyncSession, delegate: str) -> dict:
    now = utcnow()
    row = (
        await session.execute(
            select(Delegate).where(
                Delegate.person_name.ilike(f"%{delegate}%"),
                Delegate.revoked_at.is_(None),
            ).limit(1)
        )
    ).scalars().first()
    expires = row.not_after if row is not None else None
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=now.tzinfo)
    if row is None or expires is None or expires < now:
        spoken = f"{delegate} is not a current delegate."
        return {
            "ok": False,
            "denied": True,
            "reason": "no_delegate",
            "spoken": spoken,
            "hud": hud_card("Brief share", spoken, {"delegate": delegate}),
        }
    scopes = [str(s) for s in (row.scopes or [])]
    if "briefing:read" not in scopes:
        spoken = f"{row.person_name} does not have briefing:read."
        return {
            "ok": False,
            "denied": True,
            "reason": "missing_scope",
            "scopes": scopes,
            "spoken": spoken,
            "hud": hud_card("Brief share", spoken, {"delegate": row.person_name}),
        }
    brief = await handle_brief_me(session)
    spoken = f"Shared the brief with {row.person_name}."
    return {
        "ok": True,
        "denied": False,
        "delegate": row.person_name,
        "scopes": scopes,
        "briefing": brief.get("briefing"),
        "spoken": spoken,
        "hud": brief.get("hud"),
    }


# --------------------------------------------------------------------------- #
# Public records
# --------------------------------------------------------------------------- #


def host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in PUBLIC_RECORD_HOSTS)


def refuse_private_person_lookup(query: str, kind: str) -> bool:
    if kind == "org":
        return False
    if ORG_HINT_RE.search(query or ""):
        return False
    return bool(PRIVATE_PERSON_PII_RE.search(query or ""))


async def handle_public_lookup(session: AsyncSession, query: str, kind: str = "org") -> dict:
    from app.search.providers import get_search_provider

    if refuse_private_person_lookup(query, kind):
        spoken = "I won't look up a private person's phone or home."
        return {
            "ok": False,
            "refused": "private_person_pii",
            "answer": spoken,
            "citations": [],
            "spoken": spoken,
            "hud": hud_card("Public records", spoken, {"query": query}),
        }
    citations: list[dict] = []
    provider = get_search_provider()
    if provider is not None:
        results = await provider.search(query, limit=5)
        for item in results:
            if item.url and host_allowed(item.url):
                citations.append({"title": item.title, "url": item.url})
    wiki = "https://en.wikipedia.org/wiki/" + query.strip().replace(" ", "_")
    if not any(c["url"] == wiki for c in citations):
        citations.append({"title": f"Wikipedia: {query}", "url": wiki})
    if kind == "filing":
        citations.append(
            {
                "title": "SEC EDGAR search",
                "url": "https://www.sec.gov/edgar/search/",
            }
        )
    answer = f"Public sources for {query}."
    spoken = f"{answer} according to {len(citations)} sources."
    return {
        "ok": True,
        "answer": answer,
        "citations": citations,
        "spoken": spoken,
        "hud": hud_card("Public records", spoken, {"citations": citations, "kind": kind}),
    }


# --------------------------------------------------------------------------- #
# Gear power
# --------------------------------------------------------------------------- #


async def handle_gear_power(session: AsyncSession, device: str | None = None) -> dict:
    from app.ev import gear as gear_mod
    from app.ev.hardware import last_sample
    from app.models import Device, GearSnapshot

    sample = await last_sample(session)
    test_open = sample is not None
    if test_open and sample is not None and (
        device is None
        or device.lower() in {sample.source, "battery", "drone", "vehicle", "phone"}
    ):
        label = "phone stand-in" if sample.source == "phone" else sample.source
        spoken = f"{label} battery {sample.battery:.0f}%." if sample.battery is not None else f"No battery on last {label} sample."
        return {
            "ok": True,
            "source": sample.source,
            "battery_percent": sample.battery,
            "phone_stand_in": sample.source == "phone",
            "spoken": spoken,
            "hud": hud_card("Telemetry battery", spoken, {"source": sample.source}),
        }
    stmt = select(GearSnapshot).order_by(GearSnapshot.reported_at.desc())
    if device:
        stmt = stmt.where(
            GearSnapshot.device_id.ilike(f"%{device}%")
        )
    snap = (await session.execute(stmt.limit(1))).scalars().first()
    if snap is None and device:
        row = (
            await session.execute(select(Device).where(Device.name.ilike(f"%{device}%")).limit(1))
        ).scalars().first()
        if row is not None and row.battery_percent is not None:
            spoken = f"{row.name} battery {row.battery_percent:.0f}%."
            return {
                "ok": True,
                "device": row.name,
                "battery_percent": row.battery_percent,
                "storage_free_bytes": row.storage_free_bytes,
                "spoken": spoken,
                "hud": hud_card("Gear power", spoken, {"device": row.name}),
            }
    if snap is None:
        spoken = "No battery reading yet. Mac scan is the fallback."
        return {
            "ok": False,
            "found": False,
            "spoken": spoken,
            "hud": hud_card("Gear power", spoken, {}),
        }
    spoken = (
        f"{snap.device_id} battery {snap.battery_percent:.0f}%."
        if snap.battery_percent is not None
        else f"No battery on {snap.device_id}."
    )
    if snap.battery_percent is not None and snap.battery_percent <= gear_mod.LOW_BATTERY_PCT:
        await gear_mod.scan_gear(session)
    return {
        "ok": True,
        "found": True,
        "device": snap.device_id,
        "battery_percent": snap.battery_percent,
        "storage_free_bytes": snap.storage_free_bytes,
        "spoken": spoken,
        "hud": hud_card("Gear power", spoken, {"device": snap.device_id}),
    }


async def persist_heartbeat_power(
    session: AsyncSession,
    *,
    device_id: str,
    battery_percent: float | None,
    storage_free_bytes: int | None,
) -> None:
    from app.models import Device, GearSnapshot

    if battery_percent is None and storage_free_bytes is None:
        return
    device = (
        await session.execute(select(Device).where(Device.name == device_id).limit(1))
    ).scalars().first()
    if device is None:
        try:
            device = await session.get(Device, UUID(str(device_id)))
        except ValueError:
            device = None
    if device is not None:
        if battery_percent is not None:
            device.battery_percent = battery_percent
        if storage_free_bytes is not None:
            device.storage_free_bytes = storage_free_bytes
        device.last_seen_at = utcnow()
    session.add(
        GearSnapshot(
            device_id=str(device.name if device is not None else device_id),
            reported_at=utcnow(),
            battery_percent=battery_percent,
            storage_free_bytes=storage_free_bytes,
            details={"source": "heartbeat"},
        )
    )
    await session.flush()
    from app.ev import gear as gear_mod

    if battery_percent is not None and battery_percent <= gear_mod.LOW_BATTERY_PCT:
        await gear_mod.scan_gear(session)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


async def handle_how_do_i_look(session: AsyncSession) -> dict:
    from app.ev.health_radar import latest_clinical, morning_brief

    brief = await morning_brief(session)
    clinical = await latest_clinical(session)
    flags = [f.get("rationale") or f.get("metric") for f in (brief.get("anomalies") or [])][:4]
    readiness = brief.get("readiness")
    band = brief.get("band")
    if readiness is None:
        spoken = "No health snapshot yet. I can only speak from what you posted."
    else:
        flag_text = f" Flags: {'; '.join(str(f) for f in flags)}." if flags else ""
        spoken = f"Readiness {readiness} ({band}).{flag_text}"
    return {
        "ok": True,
        "readiness": readiness,
        "band": band,
        "flags": brief.get("anomalies") or [],
        "clinical": clinical,
        "spoken": spoken,
        "hud": hud_card("How you look", spoken, {"readiness": readiness, "band": band}),
    }


def head_injury_script(*, answers: dict | None = None, abort: bool = False) -> dict:
    if abort:
        spoken = f"Stopping. {HEAD_INJURY_DISCLAIMER}"
        return {
            "ok": True,
            "aborted": True,
            "questions": [],
            "disclaimer": HEAD_INJURY_DISCLAIMER,
            "diagnosis": None,
            "offer_call": False,
            "spoken": spoken,
            "hud": hud_card("Head injury", spoken, {"disclaimer": HEAD_INJURY_DISCLAIMER}),
        }
    answers = answers or {}
    offer_call = bool(answers.get("call") or answers.get("call_someone"))
    spoken = (
        "I hit-my-head check, not a diagnosis. "
        + " ".join(HEAD_INJURY_QUESTIONS)
        + " "
        + HEAD_INJURY_DISCLAIMER
    )
    return {
        "ok": True,
        "aborted": False,
        "questions": list(HEAD_INJURY_QUESTIONS),
        "disclaimer": HEAD_INJURY_DISCLAIMER,
        "diagnosis": None,
        "offer_call": offer_call,
        "spoken": spoken,
        "hud": hud_card("Head injury screen", spoken, {"disclaimer": HEAD_INJURY_DISCLAIMER}),
    }


async def handle_head_injury_screen(
    session: AsyncSession,
    *,
    abort: bool = False,
    call_someone: bool = False,
) -> dict:
    from app.ev.health_radar import create_snapshot
    from app.models import HealthSnapshot

    result = head_injury_script(answers={"call": call_someone}, abort=abort)
    snap = await create_snapshot(session, metrics={}, source="symptom_check")
    if isinstance(snap, HealthSnapshot):
        snap.metrics = {**(snap.metrics or {}), "kind": "symptom_check"}
    result["kind"] = "symptom_check"
    return result


async def maybe_morning_brief_callout(session: AsyncSession) -> dict | None:
    from app.ev.assistant import get_profile
    from app.ev.callouts import emit_callout
    from app.ev.ev_sense import quiet_hours_active
    from app.ev.health_radar import morning_brief
    from app.notify.proactive import may_speak_proactive

    if quiet_hours_active():
        return None
    profile = await get_profile(session)
    today = utcnow().date().isoformat()
    if profile.morning_brief_spoken_on == today:
        return None
    brief = await morning_brief(session)
    if brief.get("readiness") is None:
        return None
    fp = f"morning-brief:{today}"
    decision = await may_speak_proactive(session, emergency=False, fingerprint=fp)
    text = (
        f"Morning brief: readiness {brief.get('readiness')} ({brief.get('band')}). "
        f"{brief.get('recommendation') or ''}"
    ).strip()
    row = await emit_callout(
        session,
        text,
        source="morning_brief",
        source_item=fp,
        hud=hud_card("Morning brief", text, brief),
    )
    profile.morning_brief_spoken_on = today
    await session.flush()
    return {"spoken": row.spoken, "text": text}


# --------------------------------------------------------------------------- #
# Sense fuse
# --------------------------------------------------------------------------- #


async def fused_sense_pass(session: AsyncSession) -> dict:
    from app.ev.assistant import get_profile
    from app.ev.callouts import emit_callout
    from app.ev.companionship import scan_isolation
    from app.ev.ev_sense import apply_attention_policy, generate_predictions
    from app.ev.health_radar import latest_clinical
    from app.models import Prediction
    from app.notify.proactive import may_speak_proactive
    from app.schemas import SensePrediction

    preds = await generate_predictions(session)
    preds = await apply_attention_policy(session, preds)
    clinical = await latest_clinical(session)
    isolation = await scan_isolation(session)
    candidates: list[dict] = []
    for pred in preds:
        if pred.deliver or pred.tier in {"notify", "notify_card"}:
            candidates.append(
                {
                    "kind": pred.kind,
                    "text": pred.text,
                    "why_now": pred.why_now,
                    "source_ids": list(pred.basis_ids or []),
                    "score": pred.intervention_score or 0,
                }
            )
    if clinical.get("emergency") or clinical.get("flags"):
        flags = clinical.get("flags") or []
        candidates.append(
            {
                "kind": "health",
                "text": flags[0].get("rationale") if flags else "A health flag needs a look.",
                "why_now": "latest clinical snapshot",
                "source_ids": [str(item.get("metric")) for item in flags],
                "score": 0.9 if clinical.get("emergency") else 0.6,
            }
        )
    if isolation.detected:
        candidates.append(
            {
                "kind": "isolation",
                "text": isolation.recommendation or "Quiet stretch — checking in.",
                "why_now": "isolation scan",
                "source_ids": list(isolation.evidence_ids or []),
                "score": isolation.confidence or 0.5,
            }
        )
    stored = 0
    for pred in preds:
        session.add(
            Prediction(
                kind=pred.kind,
                text=pred.text,
                confidence=pred.confidence,
                basis_ids=list(pred.basis_ids or []),
                rationale=pred.why_now,
                intervention_score=pred.intervention_score,
                details={"tier": pred.tier, "deliver": pred.deliver},
            )
        )
        stored += 1
    await session.flush()
    if not candidates:
        return {"ok": True, "callout": None, "stored": stored, "candidates": 0}
    candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    chosen = candidates[0]
    fp = f"sense:{chosen['kind']}:{normalize_text(chosen['text'])[:48]}"
    if await fingerprint_seen(session, fp):
        return {"ok": True, "callout": None, "stored": stored, "deduped": True}
    decision = await may_speak_proactive(
        session,
        emergency=chosen["kind"] == "health" and bool(clinical.get("emergency")),
        fingerprint=fp,
    )
    why = f"{chosen['why_now']} sources={','.join(str(s) for s in chosen['source_ids'][:6])}"
    if not decision.allowed:
        return {
            "ok": True,
            "callout": None,
            "stored": stored,
            "policy": decision.reason,
            "why_now": why,
        }
    text = f"{chosen['text']} Why now: {why}"
    row = await emit_callout(
        session,
        text,
        source="sense",
        source_item=fp,
        hud=hud_card("Sense", text, {"why_now": why, "source_ids": chosen["source_ids"]}),
    )
    profile = await get_profile(session)
    profile.last_sense_why = why
    profile.last_sense_source_ids = list(chosen["source_ids"])
    profile.last_sense_callout_id = row.id
    await session.flush()
    return {
        "ok": True,
        "callout": str(row.id),
        "spoken": row.spoken,
        "why_now": why,
        "source_ids": chosen["source_ids"],
        "stored": stored,
        "candidates": 1,
    }


async def handle_why_did_you_ping(session: AsyncSession) -> dict:
    from app.ev.assistant import get_profile

    profile = await get_profile(session)
    row = None
    if profile.last_sense_callout_id is not None:
        row = await session.get(Callout, profile.last_sense_callout_id)
    if row is None:
        row = (
            await session.execute(
                select(Callout)
                .where(Callout.source == "sense")
                .order_by(Callout.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
    if row is None:
        spoken = "I have not pinged you."
        return {"ok": True, "spoken": spoken, "why_now": None, "hud": hud_card("Sense", spoken, {})}
    why = profile.last_sense_why or row.text
    return {
        "ok": True,
        "spoken": why,
        "why_now": why,
        "source_ids": list(profile.last_sense_source_ids or []),
        "hud": hud_card("Why I pinged", why, {"source_ids": profile.last_sense_source_ids}),
    }


# --------------------------------------------------------------------------- #
# Watchlist / feeds
# --------------------------------------------------------------------------- #


async def handle_watchlist_add(
    session: AsyncSession,
    value: str,
    *,
    kind: str = "topic",
) -> dict:
    from app.ev import alert_radar

    item = await alert_radar.upsert_watch_item(
        session,
        WatchlistCreate(kind=kind, value=value, priority=0.6, sources=["voice"]),
    )
    spoken = f"Watching {item.value}."
    return {
        "ok": True,
        "id": str(item.id),
        "value": item.value,
        "kind": item.kind,
        "spoken": spoken,
        "hud": hud_card("Watchlist", spoken, {"id": str(item.id)}),
    }


async def handle_alerts_digest(session: AsyncSession) -> dict:
    from app.ev import alert_radar

    pending = await alert_radar.list_alerts(session, status="pending", limit=20)
    items = [{"id": str(a.id), "title": a.title, "body": a.body, "tier": a.tier} for a in pending]
    spoken = f"{len(items)} alert(s) on the digest." if items else "Nothing on the digest."
    return {
        "ok": True,
        "count": len(items),
        "alerts": items,
        "spoken": spoken,
        "hud": hud_card("Alerts digest", spoken, {"alerts": items}),
    }


async def poll_public_feeds(session: AsyncSession) -> dict:
    from app.ev import alert_radar
    from app.ev.callouts import emit_callout
    from app.utils.text import fingerprint

    feeds = list(
        (await session.execute(select(PublicFeed).where(PublicFeed.active.is_(True)))).scalars().all()
    )
    watch = await alert_radar.list_watch_items(session)
    created = 0
    for feed in feeds:
        items = list(feed.last_items or [])
        if not items:
            acted = await _feed_act(session, feed)
            items = list(acted.get("items") or [])
            if items:
                feed.last_items = items
        feed.last_polled_at = utcnow()
        for item in items:
            text = " ".join(
                str(item.get(k) or "") for k in ("title", "summary", "body")
            )
            for watch_item in watch:
                if normalize_text(watch_item.value) not in normalize_text(text):
                    continue
                fp = fingerprint(
                    {
                        "kind": "public_feed",
                        "feed": str(feed.id),
                        "watch": str(watch_item.id),
                        "title": item.get("title"),
                    }
                )
                existing = (
                    await session.execute(select(Alert.id).where(Alert.fingerprint == fp).limit(1))
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                alert = Alert(
                    kind="watchlist",
                    title=str(item.get("title") or watch_item.value),
                    body=str(item.get("summary") or item.get("body") or text)[:500],
                    priority=0.6,
                    tier="useful",
                    source=f"public_feed:{feed.label}",
                    trigger_ids=[str(feed.id), str(watch_item.id)],
                    rationale="public feed matched an owner watchlist item",
                    fingerprint=fp,
                    details={"feed": feed.label, "url": item.get("url")},
                )
                session.add(alert)
                await session.flush()
                await emit_callout(
                    session,
                    f"Watchlist hit: {alert.title}",
                    source="public_feed",
                    source_item=f"public_feed:{fp[:24]}",
                    hud=hud_card("Watchlist", alert.title, {"alert_id": str(alert.id)}),
                )
                created += 1
    return {"feeds": len(feeds), "created": created}


async def _feed_act(session: AsyncSession, feed: PublicFeed) -> dict:
    from app.ev.hardware import adapter_act

    return await adapter_act(
        session,
        "public_feeds",
        "public_feeds.poll",
        {"url": feed.url, "kind": feed.kind, "label": feed.label},
    )


# --------------------------------------------------------------------------- #
# Where is
# --------------------------------------------------------------------------- #


async def handle_where_is(session: AsyncSession, name: str) -> dict:
    from app.ev import people

    share = (
        await session.execute(
            select(LocationShare)
            .where(LocationShare.person_name.ilike(f"%{name}%"))
            .order_by(LocationShare.consented_at.desc())
            .limit(1)
        )
    ).scalars().first()
    now = utcnow()
    expires = share.token_expires if share is not None else None
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=now.tzinfo)
    if share is not None and expires is not None and expires >= now and share.last_lat is not None:
        spoken = f"{share.person_name} last ping {share.last_lat}, {share.last_lon}."
        return {
            "ok": True,
            "live": True,
            "name": share.person_name,
            "lat": share.last_lat,
            "lon": share.last_lon,
            "spoken": spoken,
            "hud": hud_card("Where", spoken, {"lat": share.last_lat, "lon": share.last_lon}),
        }
    entity = (
        await session.execute(
            select(Entity).where(
                Entity.entity_type == "person",
                Entity.name.ilike(f"%{name}%"),
            ).limit(1)
        )
    ).scalars().first()
    if entity is None and share is None:
        spoken = f"I don't have a person named {name}, and no one opted in."
        return {
            "ok": False,
            "refused": "unknown_person",
            "spoken": spoken,
            "hud": hud_card("Where", spoken, {}),
        }
    info = await people.whereabouts(session, name)
    spoken = f"Memory only for {name}."
    if info.last_seen:
        spoken = f"Memory only. Last noted: {info.last_seen.get('text') or info.last_seen.get('occurred_at')}."
    return {
        "ok": True,
        "live": False,
        "memory_only": True,
        "name": name,
        "last_seen": info.last_seen,
        "spoken": spoken,
        "hud": hud_card("Where", spoken, {"memory_only": True}),
    }


# --------------------------------------------------------------------------- #
# Voice
# --------------------------------------------------------------------------- #


LOWER_VOICES = ("af_sky", "bf_emma", "echo", "onyx")
DEFAULT_VOICE = "alloy"


def resolve_voice_choice(value: str, *, current: str | None, rate: float | None) -> dict:
    raw = (value or "").strip()
    if BANNED_VOICE_RE.search(raw):
        return {"ok": False, "refused": "interrogation", "spoken": "I don't offer that kind of voice."}
    voice = current or settings.voice_tts_voice or DEFAULT_VOICE
    new_rate = rate if rate is not None else float(settings.voice_tts_rate or 1.0)
    lowered = raw.lower()
    if lowered in {"lower", "low", "deeper"}:
        voice = LOWER_VOICES[0]
    elif lowered in {"slower", "slow"}:
        new_rate = 0.85
    elif lowered in {"faster", "faster voice"}:
        new_rate = 1.15
    elif lowered in {"reset", "default"}:
        voice = DEFAULT_VOICE
        new_rate = 1.0
    elif raw:
        voice = raw
    return {"ok": True, "voice_id": voice, "tts_rate": new_rate}


async def handle_set_voice(session: AsyncSession, voice_id: str | None = None) -> dict:
    from app.ev.assistant import get_profile

    profile = await get_profile(session)
    choice = resolve_voice_choice(
        voice_id or "",
        current=profile.tts_voice_id,
        rate=profile.tts_rate,
    )
    if not choice.get("ok"):
        return {**choice, "hud": hud_card("Voice", choice["spoken"], {})}
    profile.tts_voice_id = choice["voice_id"]
    profile.tts_rate = choice["tts_rate"]
    settings.voice_tts_voice = choice["voice_id"]
    settings.voice_tts_rate = float(choice["tts_rate"])
    await session.flush()
    spoken = f"Using the {choice['voice_id']} voice at rate {choice['tts_rate']}."
    return {
        "ok": True,
        "voice_id": choice["voice_id"],
        "tts_rate": choice["tts_rate"],
        "spoken": spoken,
        "hud": hud_card("Voice", spoken, {"voice_id": choice["voice_id"], "rate": choice["tts_rate"]}),
    }


# --------------------------------------------------------------------------- #
# Plate / mail
# --------------------------------------------------------------------------- #


async def handle_whats_on_my_plate(session: AsyncSession) -> dict:
    from app.ev import calendar as calendar_feed
    from app.ev.tools import _active_life_integration, _dispatch_life_action

    cal = await calendar_feed.calendar_signals(session)
    upcoming = []
    if cal.get("next_event"):
        upcoming.append(cal["next_event"])
    mail_items: list[dict] = []
    mail_missing = True
    integration = await _active_life_integration(session, "mail")
    if integration is not None:
        mail_missing = False
        try:
            listed = await _dispatch_life_action(session, "list_mail", {"limit": 5}, actor="workbench")
            mail_items = list(listed.get("items") or listed.get("messages") or [])
        except Exception:
            mail_items = []
    github: list[dict] = []
    gh = (
        await session.execute(
            select(__import__("app.models", fromlist=["Integration"]).Integration).where(
                __import__("app.models", fromlist=["Integration"]).Integration.adapter == "github",
                __import__("app.models", fromlist=["Integration"]).Integration.status == "active",
            ).limit(1)
        )
    ).scalars().first()
    if gh is not None:
        from app.ev.hardware import adapter_act

        listed = await adapter_act(session, "github", "github.list_issues", {"repo": "owner/repo", "limit": 5})
        github = list(listed.get("issues") or listed.get("items") or [])
    deadlines = list(
        (
            await session.execute(
                select(WatchlistItem).where(
                    WatchlistItem.active.is_(True),
                    WatchlistItem.kind == "deadline",
                )
            )
        ).scalars().all()
    )
    deadline_vals = [d.value for d in deadlines]
    parts = []
    if upcoming:
        parts.append(f"Next: {upcoming[0].get('summary') or upcoming[0]}")
    if deadline_vals:
        parts.append("Deadlines: " + ", ".join(deadline_vals[:3]))
    if mail_missing:
        parts.append("Calendar-only plate — mail helper missing.")
    elif mail_items:
        parts.append(f"{len(mail_items)} mail item(s).")
    if github:
        parts.append(f"{len(github)} GitHub item(s).")
    spoken = " ".join(parts) if parts else "Plate is clear."
    return {
        "ok": True,
        "calendar": upcoming,
        "mail": mail_items,
        "github": github,
        "deadlines": deadline_vals,
        "calendar_only": mail_missing,
        "spoken": spoken,
        "hud": hud_card("Plate", spoken, {"calendar_only": mail_missing}),
    }


async def handle_draft_reply(
    session: AsyncSession,
    mail_id: str,
    *,
    body: str | None = None,
    confirm: bool = False,
    send: bool = False,
) -> dict:
    from app.ev.tools import _active_life_integration

    draft = (
        await session.execute(select(MailDraft).where(MailDraft.mail_id == mail_id).limit(1))
    ).scalars().first()
    if draft is None:
        draft = MailDraft(
            mail_id=mail_id,
            body=body or f"Draft reply to {mail_id}.",
            status="draft",
            confirm=False,
            sent=False,
        )
        session.add(draft)
        await session.flush()
    elif body:
        draft.body = body
    if send and not (confirm or draft.confirm):
        spoken = "I drafted it. Confirm before I send."
        return {
            "ok": False,
            "draft_id": str(draft.id),
            "sent": False,
            "confirm": False,
            "spoken": spoken,
            "hud": hud_card("Draft", spoken, {"mail_id": mail_id}),
        }
    if send and (confirm or draft.confirm):
        draft.confirm = True
        helper = await _active_life_integration(session, "mail")
        if helper is None:
            spoken = "Draft saved. No mail helper to send."
            return {
                "ok": True,
                "draft_id": str(draft.id),
                "sent": False,
                "helper_sent": False,
                "spoken": spoken,
                "hud": hud_card("Draft", spoken, {"mail_id": mail_id}),
            }
        from app.integrations import service as integrations

        try:
            outcome = await integrations.execute_action(
                session,
                helper.id,
                "mail.send",
                {"mail_id": mail_id, "body": draft.body, "confirm": True},
                actor="workbench",
            )
            payload = getattr(outcome, "result", None) or {}
            helper_sent = bool(payload.get("sent") is True)
        except Exception as exc:  # noqa: BLE001
            helper_sent = False
            payload = {"error": str(exc)}
        draft.sent = helper_sent
        draft.status = "sent" if helper_sent else "draft"
        spoken = "Sent." if helper_sent else "Draft still unsent — helper did not confirm sent."
        return {
            "ok": helper_sent,
            "draft_id": str(draft.id),
            "sent": helper_sent,
            "helper_sent": helper_sent,
            "spoken": spoken,
            "hud": hud_card("Mail", spoken, {"mail_id": mail_id}),
        }
    spoken = "Draft saved. Say confirm when you want it sent."
    return {
        "ok": True,
        "draft_id": str(draft.id),
        "mail_id": mail_id,
        "body": draft.body,
        "sent": False,
        "confirm": False,
        "spoken": spoken,
        "hud": hud_card("Draft", spoken, {"mail_id": mail_id}),
    }


# --------------------------------------------------------------------------- #
# Lookout utterance (suit + desk)
# --------------------------------------------------------------------------- #


async def post_utterance(
    session: AsyncSession,
    text: str,
    *,
    conversation_id: UUID | None = None,
    prefer_haptic: bool = True,
    actor: str = "watch",
) -> dict:
    from app.ev import assistant as assistant_mod
    from app.ev import conversation
    from app.ev.tool_select import select_tool
    from app.ev.tools import dispatch
    from app.schemas import EventCreate
    from app.services.event_service import EventService

    thread = await assistant_mod.resolve_live_thread(session, conversation_id)
    svc = EventService(session, actor=actor)
    await svc.create(
        EventCreate(
            source=actor,
            event_type="message.user",
            text=text,
            conversation_id=thread.id,
        )
    )
    selected = select_tool(text)
    reply = None
    hud = None
    if selected.selected in STATUS_TOOLS or selected.selected in {
        "search_memory",
        "get_weather",
        "get_upcoming_alerts",
    }:
        args: dict[str, Any] = {}
        if selected.selected in {"research", "public_lookup"}:
            args["question" if selected.selected == "research" else "query"] = text
        if selected.selected == "where_is":
            args["name"] = text.split()[-1]
        if selected.selected == "get_weather":
            args["query"] = text
        response = await dispatch(session, selected.selected, args, actor=actor, allow_sensitive=True)
        if response.ok and isinstance(response.result, dict):
            reply = str(response.result.get("spoken") or response.result.get("answer") or "")
            hud = response.result.get("hud")
    if not reply:
        reply = "Noted."
        hud = hud_card("Lookout", reply, {"prefer_haptic": prefer_haptic})
    await svc.create(
        EventCreate(
            source="assistant",
            event_type="message.assistant",
            text=reply,
            conversation_id=thread.id,
        )
    )
    if isinstance(hud, dict):
        try:
            await cache_hud(session, hud, source="utterance")
        except ValueError:
            pass
    history = await conversation.history(session, thread.id, limit=20)
    return {
        "ok": True,
        "conversation_id": str(thread.id),
        "reply": reply,
        "prefer_haptic": prefer_haptic,
        "tts": None if prefer_haptic else True,
        "hud": hud,
        "turns": [
            {
                "role": "user" if ev.event_type == "message.user" else "assistant",
                "text": (ev.content or {}).get("text"),
                "id": str(ev.id),
            }
            for ev in history
        ],
    }


async def lookout_transcript(session: AsyncSession, conversation_id: UUID | None = None) -> dict:
    from app.ev import assistant as assistant_mod
    from app.ev import conversation

    thread = await assistant_mod.resolve_live_thread(session, conversation_id)
    history = await conversation.history(session, thread.id, limit=50)
    return {
        "conversation_id": str(thread.id),
        "turns": [
            {
                "role": "user" if ev.event_type == "message.user" else "assistant",
                "text": (ev.content or {}).get("text"),
                "occurred_at": ev.occurred_at.isoformat(),
            }
            for ev in history
        ],
    }


# --------------------------------------------------------------------------- #
# Filter helpers (used by output_filter)
# --------------------------------------------------------------------------- #


def protect_citation_urls(text: str) -> tuple[str, list[str]]:
    urls = CITATION_URL_RE.findall(text or "")
    protected = text or ""
    for index, url in enumerate(urls):
        protected = protected.replace(url, f"\x00URL{index}\x00")
    return protected, urls


def restore_citation_urls(text: str, urls: list[str]) -> str:
    restored = text or ""
    for index, url in enumerate(urls):
        restored = restored.replace(f"\x00URL{index}\x00", url)
    return restored


def strip_concussion_diagnosis(text: str) -> str:
    cleaned: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        if CONCUSSION_YES_NO_RE.search(sentence):
            continue
        cleaned.append(sentence)
    return " ".join(cleaned).strip()


def reject_interrogation_copy(text: str) -> str:
    if not INTERROGATION_RE.search(text or ""):
        return text
    return INTERROGATION_RE.sub("voice", text or "")
