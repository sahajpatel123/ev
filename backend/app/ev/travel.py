"""Maps ETA (honest estimate), indoor routes, and whereabouts that never mix live share."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta
from typing import Any

from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IndoorEdge, IndoorNode
from app.utils.text import utcnow

LOCAL_ETA_MINUTES = 30
LEAVE_BUFFER_MINUTES = 5
NO_INDOOR_MAP = "I don't have an indoor map"


def maps_eta(
    *,
    origin: str | None = None,
    destination: str | None = None,
    provider: str = "local",
    travel_minutes: int | None = None,
) -> dict:
    """local returns 30 min with estimate=true. Configured maps may return a real ETA."""

    _ = origin
    mode = (provider or "local").lower()
    if travel_minutes is not None:
        return {
            "minutes": int(travel_minutes),
            "estimate": False,
            "provider": "explicit",
            "destination": destination,
            "origin": origin,
        }
    if mode in {"google", "apple"}:
        # No live key in this slice — keep the honest local estimate.
        return {
            "minutes": LOCAL_ETA_MINUTES,
            "estimate": True,
            "provider": "local",
            "honesty": "Maps is not configured; this is a 30 minute estimate.",
            "destination": destination,
            "origin": origin,
        }
    return {
        "minutes": LOCAL_ETA_MINUTES,
        "estimate": True,
        "provider": "local",
        "honesty": "Travel time is an estimate (30 min); no live routing API is connected.",
        "destination": destination,
        "origin": origin,
    }


def leave_by_iso(start: datetime_like, eta_minutes: int, buffer_minutes: int = LEAVE_BUFFER_MINUTES) -> str | None:
    when = _as_dt(start)
    if when is None:
        return None
    return (when - timedelta(minutes=int(eta_minutes) + int(buffer_minutes))).isoformat()


datetime_like = Any


def _as_dt(value: Any):
    if value is None:
        return None
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value
    try:
        parsed = date_parser.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed


async def weather_note(place: str | None = None) -> str | None:
    """Overlay existing get_weather / Open-Meteo. Do not add a provider."""

    try:
        from app.ev.policy import evaluate_policy
        from app.ev.tools import get_spec
        from app.search.live import weather_results
    except Exception:
        return None
    decision = evaluate_policy(
        "get_weather",
        spec=get_spec("get_weather"),
        actor="master",
        channel="action",
        arguments={"place": place or "home"},
        provider_connected=True,
    )
    if not decision.allowed:
        return None
    query = f"weather in {place}" if place else "weather"
    try:
        results = await weather_results(query, limit=1)
    except Exception:
        return None
    if not results:
        return None
    snippet = (results[0].snippet or results[0].title or "").lower()
    if "rain" in snippet or "storm" in snippet:
        return "rain, add 5 min"
    if snippet:
        return snippet[:120]
    return None


async def owner_coarse_origin(session: AsyncSession) -> str | None:
    from app.ev.user_state import build_user_state

    try:
        state = await build_user_state(session)
    except Exception:
        return None
    for line in state.live_context or []:
        if "] location " in line:
            return line.split("] location ", 1)[-1].strip() or None
    return None


async def indoor_route(session: AsyncSession, to_room: str) -> dict:
    target = (to_room or "").strip()
    nodes = list((await session.execute(select(IndoorNode))).scalars().all())
    if not nodes:
        return {
            "ok": False,
            "error": "no_map",
            "spoken": NO_INDOOR_MAP,
            "rooms": [],
            "photo": None,
            "steps": [],
        }
    room_names = [node.name for node in nodes]
    dest = _match_node(nodes, target)
    if dest is None:
        return {
            "ok": False,
            "error": "unknown_room",
            "spoken": f"I don't have a room called {target}. Rooms: {', '.join(room_names)}.",
            "rooms": room_names,
            "photo": None,
            "steps": [],
        }
    edges = list((await session.execute(select(IndoorEdge))).scalars().all())
    graph: dict[str, list[IndoorEdge]] = defaultdict(list)
    for edge in edges:
        graph[str(edge.from_node_id)].append(edge)
        graph[str(edge.to_node_id)].append(edge)
    start = nodes[0]
    path = _walk(str(start.id), str(dest.id), graph)
    if path is None:
        photo = dest.photo_ref
        spoken = NO_INDOOR_MAP if not edges else f"I can't route to {dest.name} yet."
        return {
            "ok": False,
            "error": "no_route",
            "spoken": spoken,
            "rooms": room_names,
            "photo": photo,
            "steps": [],
            "to": dest.name,
        }
    steps = []
    by_id = {str(node.id): node for node in nodes}
    for edge in path:
        nxt = by_id.get(str(edge.to_node_id)) or by_id.get(str(edge.from_node_id))
        label = edge.instruction or (f"Go to {nxt.name}" if nxt else "Continue")
        steps.append({"instruction": label, "meters": edge.meters})
    spoken = " ".join(str(step["instruction"]) for step in steps if step.get("instruction")) or f"You are at {dest.name}."
    return {
        "ok": True,
        "spoken": spoken,
        "rooms": room_names,
        "photo": dest.photo_ref,
        "steps": steps,
        "to": dest.name,
        "schema_version": "ev.hud.route.v1",
    }


def _match_node(nodes: list[IndoorNode], target: str) -> IndoorNode | None:
    lowered = target.lower()
    for node in nodes:
        names = [node.name, *(node.aliases or [])]
        if any(lowered == str(name).lower() for name in names):
            return node
    for node in nodes:
        names = [node.name, *(node.aliases or [])]
        if any(lowered in str(name).lower() for name in names):
            return node
    return None


def _walk(
    start_id: str,
    dest_id: str,
    graph: dict[str, list[IndoorEdge]],
) -> list[IndoorEdge] | None:
    if start_id == dest_id:
        return []
    seen = {start_id}
    queue: deque[tuple[str, list[IndoorEdge]]] = deque([(start_id, [])])
    while queue:
        current, path = queue.popleft()
        for edge in graph.get(current, []):
            nxt = str(edge.to_node_id) if str(edge.from_node_id) == current else str(edge.from_node_id)
            if nxt in seen:
                continue
            step = path + [edge]
            if nxt == dest_id:
                return step
            seen.add(nxt)
            queue.append((nxt, step))
    return None


async def upsert_node(
    session: AsyncSession,
    name: str,
    *,
    aliases: list[str] | None = None,
    photo_ref: str | None = None,
    x: float | None = None,
    y: float | None = None,
) -> IndoorNode:
    row = IndoorNode(
        name=name.strip(),
        aliases=list(aliases or []),
        photo_ref=photo_ref,
        x=x,
        y=y,
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


async def connect_nodes(
    session: AsyncSession,
    from_node_id,
    to_node_id,
    *,
    instruction: str = "",
    meters: float | None = None,
) -> IndoorEdge:
    edge = IndoorEdge(
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        instruction=instruction,
        meters=meters,
    )
    session.add(edge)
    await session.flush()
    return edge


async def whereabouts_honest(session: AsyncSession, name: str) -> dict:
    """Memory last-seen only. Never claim live share (item 39 is out of slice)."""

    from app.ev import people

    info = await people.whereabouts(session, name)
    dump = info.model_dump() if hasattr(info, "model_dump") else dict(info)
    last_seen = dump.get("last_seen")
    if last_seen:
        spoken = (
            f"I don't have a live share from {name}. "
            f"Last I remember: {last_seen.get('text') or last_seen.get('occurred_at')}."
        )
        source_kind = "memory"
    else:
        spoken = f"I don't have a live share from {name}, and I have no last-seen memory."
        source_kind = "none"
    dump["source_kind"] = source_kind
    dump["live_share"] = False
    dump["spoken"] = spoken
    return dump
