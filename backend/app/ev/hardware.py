"""Owner-leashed shop/field hardware: printer, cameras, drone, beacons, telemetry.

Local/sim adapters are the CI pass bar. Real vendor URLs stay in the vault.
Never LAN-discover cameras, never hunt people, never fire weapons.
"""

from __future__ import annotations

import math
import re
import struct
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Attachment,
    Beacon,
    HardwareAudit,
    Integration,
    OwnerCamera,
    PrintJob,
    TelemetrySample,
    TelemetrySession,
)
from app.schemas import PrintJobCreate, PrintJobStatusUpdate
from app.utils.text import utcnow

WEAPONS_RE = re.compile(
    r"\b(weapon|weapons|fire|firing|shoot|shot|missile|armed|arm|kill|attack|bomb|payload)\b",
    re.IGNORECASE,
)
PRINTER_TERMINAL = frozenset({"done", "failed", "cancelled", "complete"})


async def audit(
    session: AsyncSession,
    *,
    kind: str,
    command: str,
    args: dict,
    result: dict,
    actor: str = "owner",
) -> HardwareAudit:
    row = HardwareAudit(
        kind=kind,
        command=command,
        args=args or {},
        result=result or {},
        actor=actor,
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


async def active_integration(session: AsyncSession, adapter: str) -> Integration | None:
    return (
        await session.execute(
            select(Integration)
            .where(Integration.adapter == adapter, Integration.status == "active")
            .order_by(Integration.created_at.asc())
            .limit(1)
        )
    ).scalars().first()


async def adapter_act(
    session: AsyncSession,
    adapter: str,
    action: str,
    args: dict | None = None,
    *,
    actor: str = "owner",
    device_id=None,
    policy_checked: bool = False,
) -> dict:
    from app.integrations.adapters import registry

    integration = await active_integration(session, adapter)
    spec = registry.get(adapter)
    if integration is None or spec is None:
        return {
            "ok": False,
            "configured": False,
            "error": f"no {adapter} connected",
        }
    if not policy_checked:
        from app.integrations.service import authorize_integration_action

        try:
            await authorize_integration_action(
                session,
                integration.id,
                action=action,
                args=args or {},
                actor=actor,
                device_id=device_id,
            )
        except (PermissionError, LookupError, ValueError) as exc:
            return {
                "ok": False,
                "configured": True,
                "error": "policy_denied",
                "reason": str(exc),
            }
    config = dict(integration.config or {})
    token = ""
    try:
        from app.integrations import vault as vault_mod

        if hasattr(vault_mod, "read_secret"):
            token = await vault_mod.read_secret(session, integration.id) or ""
    except Exception:
        token = ""
    try:
        result = await spec.act(
            action=action,
            args=args or {},
            token=token,
            scopes=list(integration.scopes or spec.default_scopes),
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - adapter boundary
        result = {"ok": False, "error": str(exc), "configured": True}
    if not isinstance(result, dict):
        result = {"ok": False, "error": "adapter returned a non-object"}
    result.setdefault("configured", True)
    await audit(
        session,
        kind=adapter,
        command=action,
        args=args or {},
        result={k: v for k, v in result.items() if k != "blob"},
        actor=actor,
    )
    return result


# --------------------------------------------------------------------------- #
# Printer / OctoPrint
# --------------------------------------------------------------------------- #


async def ping_printer(session: AsyncSession) -> dict:
    integration = await active_integration(session, "octoprint")
    if integration is None:
        return {"ok": False, "configured": False, "status": "skipped"}
    result = await adapter_act(session, "octoprint", "octoprint.ping", {})
    result.setdefault("configured", True)
    return result


async def print_start(
    session: AsyncSession,
    *,
    project: str | None = None,
    gcode: str | None = None,
    confirm: bool = False,
    actor: str = "owner",
) -> dict:
    from app.ev import maker
    from app.ev.assistant import get_profile

    name = (project or gcode or "print").strip()
    found = await maker.find_project_by_name(session, name) if project else None
    if found is None:
        from app.schemas import MakerProjectCreate

        found = await maker.create_project(
            session,
            MakerProjectCreate(name=name, description="voice print queue", status="building"),
        )
    job = await maker.create_print_job(
        session,
        found.id,
        PrintJobCreate(name=name, estimated_minutes=None, filament_grams=None),
    )
    adapter = await active_integration(session, "octoprint")
    if adapter is None:
        spoken = "Queued. No printer connected."
        hud = _card("Print queue", spoken, {"job_id": str(job.id), "status": job.status})
        return {
            "ok": True,
            "queued": True,
            "started": False,
            "job_id": str(job.id),
            "status": job.status,
            "spoken": spoken,
            "hud": hud,
        }
    profile = await get_profile(session)
    if profile.training_wheels_completed_at is None:
        spoken = "Training wheels are still on. Finish setup before starting a print."
        return {
            "ok": False,
            "queued": True,
            "started": False,
            "job_id": str(job.id),
            "status": job.status,
            "refused": "training_wheels",
            "spoken": spoken,
            "hud": _card("Print locked", spoken, {"job_id": str(job.id)}),
        }
    if not confirm:
        spoken = f"Confirm to start printing {name}."
        return {
            "ok": False,
            "queued": True,
            "started": False,
            "needs_confirm": True,
            "job_id": str(job.id),
            "status": job.status,
            "spoken": spoken,
            "hud": _card("Confirm print", spoken, {"job_id": str(job.id)}),
        }
    result = await adapter_act(
        session,
        "octoprint",
        "octoprint.start",
        {"project": name, "gcode": gcode, "job_id": str(job.id)},
        actor=actor,
        policy_checked=True,
    )
    if not result.get("ok"):
        spoken = result.get("error") or "No printer connected."
        return {
            "ok": False,
            "queued": True,
            "started": False,
            "job_id": str(job.id),
            "status": job.status,
            "spoken": spoken,
            "hud": _card("Print", spoken, {"job_id": str(job.id)}),
        }
    job.vendor_job_id = str(result.get("vendor_job_id") or "")
    job.adapter = "octoprint"
    job.status = str(result.get("status") or "printing")
    job.details = {**(job.details or {}), "vendor": result}
    await session.flush()
    spoken = f"Starting {name}."
    return {
        "ok": True,
        "queued": True,
        "started": True,
        "job_id": str(job.id),
        "vendor_job_id": job.vendor_job_id,
        "status": job.status,
        "spoken": spoken,
        "hud": _card("Print started", spoken, {"job_id": str(job.id), "status": job.status}),
    }


async def poll_print_jobs(session: AsyncSession) -> dict:
    from app.ev import maker
    from app.ev.callouts import emit_callout

    rows = list(
        (
            await session.execute(
                select(PrintJob).where(PrintJob.status.in_(("printing", "started", "running")))
            )
        ).scalars().all()
    )
    updated = 0
    for job in rows:
        result = await adapter_act(
            session,
            "octoprint",
            "octoprint.status",
            {"vendor_job_id": job.vendor_job_id or str(job.id)},
        )
        if not result.get("ok"):
            continue
        raw_status = str(result.get("status") or job.status)
        status = {
            "complete": "done",
            "finished": "done",
            "cancelled": "failed",
            "error": "failed",
            "started": "printing",
            "running": "printing",
        }.get(raw_status, raw_status)
        if status not in {"queued", "printing", "done", "failed"}:
            status = "printing"
        print_status = cast(Literal["queued", "printing", "done", "failed"], status)
        remaining = result.get("filament_remaining_g")
        job = await maker.update_print_job(
            session,
            job.id,
            PrintJobStatusUpdate(status=print_status, error_log=result.get("error")),
        )
        if remaining is not None:
            job.details = {**(job.details or {}), "filament_remaining_g": remaining}
        updated += 1
        if status in PRINTER_TERMINAL:
            line = f"Print {job.name} is {status}."
            await emit_callout(
                session,
                line,
                source="print",
                source_item=f"print:{job.id}:{status}",
                hud=_card("Print", line, {"job_id": str(job.id), "status": status}),
            )
    return {"updated": updated}


def parse_slicer_stdout(text: str) -> dict:
    minutes = None
    grams = None
    time_match = re.search(
        r"(?:estimated(?: print)? time|print time)[:\s]+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?",
        text or "",
        re.IGNORECASE,
    )
    if time_match:
        hours = int(time_match.group(1) or 0)
        mins = int(time_match.group(2) or 0)
        minutes = hours * 60 + mins
    gram_match = re.search(
        r"(?:filament|used)[^\n]{0,40}?(\d+(?:\.\d+)?)\s*g",
        text or "",
        re.IGNORECASE,
    )
    if gram_match:
        grams = float(gram_match.group(1))
    return {"estimated_minutes": minutes, "filament_grams": grams}


def stl_bounding_box(data: bytes) -> tuple[float, float, float] | None:
    if not data or len(data) < 84:
        return None
    count = struct.unpack_from("<I", data, 80)[0]
    need = 84 + count * 50
    if count <= 0 or len(data) < min(need, 84 + 50):
        # ASCII STL fallback: look for vertex lines
        text = data[:4000].decode("ascii", errors="ignore")
        xs, ys, zs = [], [], []
        for match in re.finditer(
            r"vertex\s+([-\d.eE]+)\s+([-\d.eE]+)\s+([-\d.eE]+)", text
        ):
            xs.append(float(match.group(1)))
            ys.append(float(match.group(2)))
            zs.append(float(match.group(3)))
        if not xs:
            return None
        return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    xs, ys, zs = [], [], []
    offset = 84
    for _ in range(min(count, 20_000)):
        if offset + 50 > len(data):
            break
        _n1, _n2, _n3, x1, y1, z1, x2, y2, z2, x3, y3, z3, _attr = struct.unpack_from(
            "<ffffffffffffH", data, offset
        )
        xs.extend((x1, x2, x3))
        ys.extend((y1, y2, y3))
        zs.extend((z1, z2, z3))
        offset += 50
    if not xs:
        return None
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def heuristic_print_estimate(data: bytes, *, filename: str = "") -> dict:
    box = stl_bounding_box(data)
    if box is None:
        minutes = 30
        grams = 8.0
    else:
        volume = max(box[0], 0.1) * max(box[1], 0.1) * max(box[2], 0.1)
        minutes = max(8, int(round(volume * 0.08)))
        grams = round(max(2.0, volume * 0.04), 1)
    return {
        "estimated_minutes": minutes,
        "filament_grams": grams,
        "estimate": "owner_or_heuristic",
        "bounding_box_mm": list(box) if box else None,
        "filename": filename,
    }


async def estimate_print(session: AsyncSession, attachment_id: str) -> dict:
    from app.storage.object_store import get_object_store
    from app.tools.sandbox import SandboxError, run_command, sandbox_root

    try:
        aid = UUID(str(attachment_id))
    except ValueError:
        return {
            "ok": False,
            "spoken": "That attachment id is not valid.",
            "hud": _card("Print estimate", "Invalid attachment.", {}),
        }
    row = await session.get(Attachment, aid)
    if row is None:
        spoken = "I don't have that file."
        return {"ok": False, "spoken": spoken, "hud": _card("Print estimate", spoken, {})}
    store = get_object_store()
    data = await store.get(row.storage_key)
    parsed = None
    try:
        dest = sandbox_root() / f"estimate-{row.id}-{row.filename[-32:]}"
        dest.write_bytes(data)
        result = run_command(
            f"prusa-slicer --info {dest.name}",
            timeout_seconds=20,
            isolation="process",
        )
        stdout = ""
        if isinstance(result, dict):
            stdout = str(result.get("stdout") or result.get("output") or "")
        parsed = parse_slicer_stdout(stdout)
        if not parsed.get("estimated_minutes"):
            parsed = None
    except (SandboxError, FileNotFoundError, OSError, TypeError, ValueError):
        parsed = None
    if parsed and parsed.get("estimated_minutes") is not None:
        estimate = {**parsed, "estimate": "slicer"}
    else:
        estimate = heuristic_print_estimate(data, filename=row.filename)
    minutes = estimate.get("estimated_minutes")
    grams = estimate.get("filament_grams")
    label = estimate.get("estimate")
    spoken = f"About {minutes} min, {grams} g"
    if label == "owner_or_heuristic":
        spoken += " — owner or heuristic estimate."
    else:
        spoken += "."
    return {
        "ok": True,
        "attachment_id": str(row.id),
        **estimate,
        "spoken": spoken,
        "hud": _card("Print estimate", spoken, estimate),
    }


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #


async def start_test_session(session: AsyncSession, *, label: str = "test") -> TelemetrySession:
    open_row = (
        await session.execute(
            select(TelemetrySession)
            .where(TelemetrySession.status == "open")
            .order_by(TelemetrySession.started_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if open_row is not None:
        return open_row
    row = TelemetrySession(label=label, status="open", started_at=utcnow())
    session.add(row)
    await session.flush()
    return row


async def record_sample(
    session: AsyncSession,
    *,
    source: str,
    battery: float | None = None,
    alt: float | None = None,
    speed: float | None = None,
    lat: float | None = None,
    lon: float | None = None,
    session_id: UUID | None = None,
    details: dict | None = None,
) -> TelemetrySample:
    test = None
    if session_id is not None:
        test = await session.get(TelemetrySession, session_id)
    if test is None:
        test = (
            await session.execute(
                select(TelemetrySession)
                .where(TelemetrySession.status == "open")
                .order_by(TelemetrySession.started_at.desc())
                .limit(1)
            )
        ).scalars().first()
    row = TelemetrySample(
        session_id=test.id if test is not None else None,
        source=source,
        battery=battery,
        alt=alt,
        speed=speed,
        lat=lat,
        lon=lon,
        reported_at=utcnow(),
        details=details or {},
    )
    session.add(row)
    await session.flush()
    return row


async def last_sample(session: AsyncSession) -> TelemetrySample | None:
    return (
        await session.execute(
            select(TelemetrySample).order_by(TelemetrySample.reported_at.desc()).limit(1)
        )
    ).scalars().first()


async def telemetry_weather_overlay(sample: TelemetrySample | None) -> list[dict]:
    from app.ev.policy import evaluate_policy
    from app.ev.tools import get_spec
    from app.search.live import weather_results

    decision = evaluate_policy(
        "get_weather",
        spec=get_spec("get_weather"),
        actor="master",
        channel="action",
        arguments={"place": "home"},
        provider_connected=True,
    )
    if not decision.allowed:
        return []

    lat = sample.lat if sample is not None else settings.home_lat
    lon = sample.lon if sample is not None else settings.home_lon
    results = await weather_results("weather", limit=1, lat=lat, lon=lon)
    return [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results]


# --------------------------------------------------------------------------- #
# Cameras
# --------------------------------------------------------------------------- #


async def camera_replay(
    session: AsyncSession,
    *,
    camera: str,
    at: str | None = None,
    actor: str = "owner",
) -> dict:
    from app.storage.object_store import get_object_store

    row = (
        await session.execute(
            select(OwnerCamera).where(OwnerCamera.name.ilike(f"%{camera}%")).limit(1)
        )
    ).scalars().first()
    if row is None:
        spoken = "No owner camera is configured. Upload a clip if you want me to review it."
        return {
            "ok": False,
            "configured": False,
            "discovered_lan": False,
            "spoken": spoken,
            "hud": _card("Camera", spoken, {"camera": camera}),
        }
    blob_id = None
    summary = None
    if row.clip_attachment_id is not None:
        attachment = await session.get(Attachment, row.clip_attachment_id)
        if attachment is not None:
            blob_id = str(attachment.id)
            store = get_object_store()
            data = await store.get(attachment.storage_key)
            try:
                from app.ev.vision import analyze_attachment

                summary_event = await analyze_attachment(
                    session,
                    attachment.id,
                    actor=actor,
                    permission=True,
                )
                summary = {
                    "summary": ((summary_event.payload or {}).get("summary") if summary_event else None)
                    or f"clip {attachment.filename}"
                }
            except Exception:
                summary = {"note": f"clip {attachment.filename} ({len(data)} bytes)"}
    result = await adapter_act(
        session,
        "cameras",
        "cameras.clip",
        {"camera": row.name, "at": at},
        actor=actor,
        policy_checked=True,
    )
    if result.get("blob_id"):
        blob_id = result.get("blob_id")
    spoken = f"{row.name} from {at or 'the last clip'}."
    if summary and isinstance(summary, dict):
        spoken = str(summary.get("summary") or summary.get("note") or spoken)
    return {
        "ok": True,
        "camera": row.name,
        "at": at,
        "blob_id": blob_id,
        "summary": summary,
        "discovered_lan": False,
        "spoken": spoken,
        "hud": _card("Camera replay", spoken, {"camera": row.name, "blob_id": blob_id}),
        "adapter": result,
    }


# --------------------------------------------------------------------------- #
# Drone
# --------------------------------------------------------------------------- #


_DRONE_STATE: dict[str, str] = {}


def refuse_weapons(command: str) -> bool:
    return bool(WEAPONS_RE.search(command or ""))


def inside_geofence(lat: float | None, lon: float | None) -> bool:
    if settings.home_lat is None or settings.home_lon is None:
        return True
    if lat is None or lon is None:
        return True
    radius = float(settings.home_geofence_m or 80.0)
    return haversine_m(settings.home_lat, settings.home_lon, lat, lon) <= radius


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


async def drone_command(
    session: AsyncSession,
    command: str,
    *,
    confirm: bool = False,
    lat: float | None = None,
    lon: float | None = None,
    actor: str = "owner",
) -> dict:
    from app.ev.assistant import get_profile

    cmd = (command or "").strip().lower()
    if refuse_weapons(cmd):
        spoken = "I won't run weapons commands."
        await audit(
            session,
            kind="drone",
            command=cmd,
            args={"confirm": confirm},
            result={"ok": False, "refused": "weapons"},
            actor=actor,
        )
        return {
            "ok": False,
            "refused": "weapons",
            "spoken": spoken,
            "hud": _card("Drone", spoken, {"command": cmd}),
        }
    if cmd not in {"takeoff", "hover", "land", "rtl"}:
        spoken = "I only take off, hover, land, or return to launch."
        return {
            "ok": False,
            "refused": "unknown_command",
            "spoken": spoken,
            "hud": _card("Drone", spoken, {"command": cmd}),
        }
    integration = await active_integration(session, "drone")
    if integration is None:
        spoken = "No drone is paired. Using a labeled sim is required first."
        return {
            "ok": False,
            "configured": False,
            "sim": False,
            "spoken": spoken,
            "hud": _card("Drone", spoken, {}),
        }
    profile = await get_profile(session)
    config = dict(integration.config or {})
    if not config.get("configured") and profile.training_wheels_completed_at is None:
        spoken = "Training wheels are locked until the drone is configured."
        return {
            "ok": False,
            "refused": "training_wheels",
            "spoken": spoken,
            "hud": _card("Drone locked", spoken, {}),
        }
    if cmd == "takeoff" and not confirm:
        spoken = "Confirm takeoff."
        return {
            "ok": False,
            "needs_confirm": True,
            "spoken": spoken,
            "hud": _card("Confirm takeoff", spoken, {}),
        }
    if cmd == "takeoff" and not inside_geofence(lat, lon):
        spoken = "That takeoff is outside the home geofence."
        await audit(
            session,
            kind="drone",
            command=cmd,
            args={"lat": lat, "lon": lon},
            result={"ok": False, "refused": "geofence"},
            actor=actor,
        )
        return {
            "ok": False,
            "refused": "geofence",
            "spoken": spoken,
            "hud": _card("Geofence", spoken, {}),
        }
    result = await adapter_act(
        session,
        "drone",
        f"drone.{cmd}",
        {"lat": lat, "lon": lon},
        actor=actor,
        policy_checked=True,
    )
    sim = bool(result.get("sim") or (integration.config or {}).get("provider", "local") == "local")
    _DRONE_STATE[str(integration.id)] = cmd
    spoken = f"{'Sim ' if sim else ''}{cmd}."
    return {
        "ok": bool(result.get("ok", True)),
        "command": cmd,
        "sim": sim,
        "status": result.get("status") or cmd,
        "spoken": spoken,
        "hud": _card("Drone", spoken, {"command": cmd, "sim": sim}),
        "audited": True,
    }


# --------------------------------------------------------------------------- #
# Beacons / find gear
# --------------------------------------------------------------------------- #


async def find_gear(session: AsyncSession, label: str) -> dict:
    from app.models import Device, Entity, GearSnapshot

    person = (
        await session.execute(
            select(Entity).where(
                Entity.entity_type == "person",
                Entity.name.ilike(f"%{label}%"),
            ).limit(1)
        )
    ).scalars().first()
    beacon = (
        await session.execute(
            select(Beacon).where(Beacon.label.ilike(f"%{label}%")).limit(1)
        )
    ).scalars().first()
    if person is not None and beacon is None:
        spoken = f"{person.name} is a person, not a beacon. I won't hunt people."
        return {
            "ok": False,
            "refused": "person_without_beacon",
            "spoken": spoken,
            "hud": _card("Find gear", spoken, {}),
        }
    if beacon is not None:
        spoken = (
            f"{beacon.label} last seen at {beacon.last_lat}, {beacon.last_lon}."
            if beacon.last_lat is not None
            else f"{beacon.label} is registered but has no last ping."
        )
        return {
            "ok": True,
            "label": beacon.label,
            "kind": beacon.kind,
            "last_lat": beacon.last_lat,
            "last_lon": beacon.last_lon,
            "spoken": spoken,
            "hud": _card(
                "Find gear",
                spoken,
                {"label": beacon.label, "lat": beacon.last_lat, "lon": beacon.last_lon},
            ),
        }
    device = (
        await session.execute(
            select(Device).where(Device.name.ilike(f"%{label}%")).limit(1)
        )
    ).scalars().first()
    snap = None
    if device is not None:
        snap = (
            await session.execute(
                select(GearSnapshot)
                .where(GearSnapshot.device_id.in_((str(device.id), device.name)))
                .order_by(GearSnapshot.reported_at.desc())
                .limit(1)
            )
        ).scalars().first()
    if device is None and snap is None:
        spoken = f"No owner beacon named {label}."
        return {"ok": False, "spoken": spoken, "hud": _card("Find gear", spoken, {})}
    loc = (snap.details or {}).get("lat") if snap is not None else None
    lon = (snap.details or {}).get("lon") if snap is not None else None
    spoken = f"{device.name if device else label} last seen as a registered EV device."
    return {
        "ok": True,
        "label": device.name if device else label,
        "kind": "ev_device",
        "last_lat": loc,
        "last_lon": lon,
        "fallback": "ev_device",
        "spoken": spoken,
        "hud": _card("Find gear", spoken, {"device": device.name if device else label}),
    }


# --------------------------------------------------------------------------- #
# Media authenticity / structure
# --------------------------------------------------------------------------- #


GENERATOR_MARKERS = (
    ("sora", "likely_edited"),
    ("runway", "likely_edited"),
    ("midjourney", "likely_edited"),
    ("openai", "likely_edited"),
    ("synthetic", "likely_edited"),
    ("deepfake", "likely_edited"),
    ("ffmpeg", "likely_edited"),
    ("lavf", "likely_edited"),
)


def analyze_media_authenticity(blob: bytes, *, filename: str = "", content_type: str = "") -> dict:
    text = ""
    try:
        text = blob[:8192].decode("latin-1", errors="ignore")
    except Exception:
        text = ""
    hay = f"{filename} {content_type} {text}".lower()
    reasons: list[str] = []
    label = "inconclusive"
    for marker, tagged in GENERATOR_MARKERS:
        if marker in hay:
            reasons.append(f"encoder or watermark hint: {marker}")
            label = tagged
    if not blob:
        label = "inconclusive"
        reasons.append("empty file")
    elif not reasons:
        if b"ftyp" in blob[:64] or b"moov" in blob[:256] or filename.lower().endswith(
            (".mp4", ".mov", ".jpg", ".png", ".webp")
        ):
            label = "no_known_artifacts"
            reasons.append("container looks ordinary; no known generator tags")
        else:
            label = "inconclusive"
            reasons.append("metadata-only check; not enough signal")
    return {
        "label": label,
        "reasons": reasons,
        "disclaimer": "this is not proof",
    }


async def media_check(session: AsyncSession, attachment_id: str) -> dict:
    from app.storage.object_store import get_object_store

    try:
        aid = UUID(str(attachment_id))
    except ValueError:
        spoken = "That attachment id is not valid. This is not proof."
        return {"ok": False, "spoken": spoken, "label": "inconclusive", "reasons": []}
    row = await session.get(Attachment, aid)
    if row is None:
        spoken = "I don't have that file. This is not proof."
        return {
            "ok": False,
            "label": "inconclusive",
            "reasons": ["missing attachment"],
            "spoken": spoken,
            "hud": _card("Media check", spoken, {}),
        }
    data = await get_object_store().get(row.storage_key)
    report = analyze_media_authenticity(
        data, filename=row.filename, content_type=row.content_type or ""
    )
    spoken = (
        f"Best effort: {report['label'].replace('_', ' ')}. "
        f"{'; '.join(report['reasons'][:2])}. This is not proof."
    )
    if "this is real" in spoken.lower():
        spoken = spoken.replace("this is real", "this is not proof")
    return {
        "ok": True,
        "attachment_id": str(row.id),
        **report,
        "spoken": spoken,
        "hud": _card("Media check", spoken, report),
    }


async def estimate_structure(
    session: AsyncSession,
    attachment_id: str,
    *,
    reference_length: float | None = None,
) -> dict:
    from app.storage.object_store import get_object_store

    disclaimer = (
        "Low confidence visual guess only — not a structural analysis. "
        "If the reference object is the length you said, scale from that."
    )
    try:
        aid = UUID(str(attachment_id))
    except ValueError:
        return {
            "ok": False,
            "guess": None,
            "unit": "cm",
            "confidence": "low",
            "disclaimer": disclaimer,
            "spoken": f"I need a valid photo. {disclaimer}",
        }
    row = await session.get(Attachment, aid)
    if row is None:
        spoken = f"I need a photo of the object. {disclaimer}"
        return {
            "ok": False,
            "guess": None,
            "unit": "cm",
            "confidence": "low",
            "disclaimer": disclaimer,
            "spoken": spoken,
            "hud": _card("Structure guess", spoken, {}),
        }
    data = await get_object_store().get(row.storage_key)
    box = stl_bounding_box(data)
    if reference_length is None:
        spoken = (
            "Low confidence. If you give me a reference object length "
            "(for example if the bottle is 20 cm) I can scale a rough width."
        )
        guess = None
        if box is not None:
            guess = round(max(box), 1)
            spoken = (
                f"Rough longest span looks like {guess} units — low confidence. "
                "If the bottle is 20 cm I can scale from that."
            )
        return {
            "ok": True,
            "guess": guess,
            "unit": "cm" if reference_length else "relative",
            "confidence": "low",
            "disclaimer": disclaimer,
            "spoken": spoken,
            "hud": _card("Structure guess", spoken, {"guess": guess}),
        }
    guess = float(reference_length) * 2.4
    if box is not None and max(box) > 0:
        guess = round(float(reference_length) * (max(box) / max(min(box), 0.1)), 1)
    spoken = (
        f"Rough width about {guess} cm — low confidence. "
        f"If the bottle is {reference_length} cm this is only a scale guess."
    )
    return {
        "ok": True,
        "guess": guess,
        "unit": "cm",
        "confidence": "low",
        "disclaimer": disclaimer,
        "spoken": spoken,
        "hud": _card("Structure guess", spoken, {"guess": guess, "unit": "cm"}),
    }


def _card(title: str, body: str, meta: dict[str, Any]) -> dict:
    return {
        "schema_version": "ev.hud.card.v1",
        "generated_at": utcnow().isoformat(),
        "title": title,
        "body": body,
        "priority": 0.4,
        "meta": meta,
    }
