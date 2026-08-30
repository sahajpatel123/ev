"""Surface intelligence: which HUD folios EVIE opens, and for how long.

EVIE does not use one dashboard and does not paint OS window chrome.
She settles independent *folios* on the desk: different sizes, time-types,
placements, and inner layouts. A folio is a sheet with a spine, not a
title-bar window.

Questions and replies get their own type. Layout (ask / reply / split /
stack / pulse / ribbon / field / ledger) is picked from the window id so
compositions look varied while staying in one material language.

This module is deterministic. The model fills wording; this layer decides
whether a folio exists, what kind it is, how it is composed, how long it
lives, and where it sits. It never tells the owner to open a website.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from app.schemas import InteractionStrategy

# --- catalogs (film-adapted) ------------------------------------------------

KINDS = (
    "card",
    "briefing",
    "list",
    "conversation",
    "map",
    "chip",
    "radar",
    "vitals",
    "horizon",
    "scope",
    "bench",
    "trace",
    "pulse",
    "ticker",
    "wire",
)

SIZES = {
    "pip": (180, 72),
    "chip": (280, 148),
    "card": (480, 300),
    "brief": (560, 420),
    "slate": (720, 520),
    "canvas": (960, 680),
    "lookout": (340, 460),
    "ticker": (920, 64),
}

TIME_TYPES = {
    "flash": 1_600,
    "glance": 5_000,
    "linger": 30_000,
    "hold": None,
    "lookout": None,
    "pulse": 12_000,
    "session": None,
}

PLACEMENTS = (
    "center",
    "upper_right",
    "upper_left",
    "lower_right",
    "lower_left",
    "right",
    "left",
    "top",
    "stack",
)

# kind → (size, time_type, placement)
KIND_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "card": ("card", "linger", "center"),
    "briefing": ("brief", "linger", "center"),
    "list": ("slate", "hold", "center"),
    "conversation": ("ticker", "glance", "top"),
    "map": ("canvas", "hold", "center"),
    "chip": ("chip", "flash", "upper_right"),
    "radar": ("lookout", "lookout", "upper_right"),
    "vitals": ("lookout", "lookout", "upper_left"),
    "horizon": ("lookout", "lookout", "lower_right"),
    "scope": ("lookout", "lookout", "right"),
    "bench": ("lookout", "lookout", "left"),
    "trace": ("slate", "hold", "center"),
    "pulse": ("chip", "pulse", "top"),
    "ticker": ("ticker", "glance", "top"),
    "wire": ("lookout", "session", "lower_left"),
}

LOOKOUT_KINDS = {"radar", "vitals", "horizon", "scope", "bench", "wire"}

# Inner compositions. One family; which member appears is hashed from id.
LAYOUTS = (
    "ask",
    "reply",
    "split",
    "stack",
    "pulse",
    "ribbon",
    "field",
    "ledger",
)

ASK_KINDS = {"card", "briefing", "conversation", "trace", "list"}

EXPLICIT_RE = re.compile(
    r"\b(?:show(?:\s+me)?|pull up|put (?:that|it|this) on (?:screen|the screen)|"
    r"open a (?:window|card|lookout)|display|hud|lookout|"
    r"on (?:(?:my|the) )?(?:screen|overlay|visor))\b",
    re.IGNORECASE,
)
WATCH_RE = re.compile(
    r"\b(?:keep an eye|watch (?:this|that|for|over)|monitor|look out for|"
    r"don't let me miss|baby monitor)\b",
    re.IGNORECASE,
)
FULL_HUD_RE = re.compile(
    r"\b(?:full hud|suit hud|command center|all lookouts|jarvis mode|"
    r"karen mode|stack the (?:windows|lookouts))\b",
    re.IGNORECASE,
)
BRIEF_RE = re.compile(r"\b(?:brief me|tactical|talking points|walk me through)\b", re.IGNORECASE)
HEALTH_RE = re.compile(
    r"\b(?:sleep|hrv|readiness|vitals|heart rate|steps|body scan)\b",
    re.IGNORECASE,
)
ALERT_RE = re.compile(r"\b(?:alert|deadline|watchlist|heads[- ]up)\b", re.IGNORECASE)
CAL_RE = re.compile(
    r"\b(?:calendar|schedule|afternoon|morning|this evening|what's next|"
    r"what is next|leave by|today(?:'s)? plan)\b",
    re.IGNORECASE,
)
PERSON_RE = re.compile(
    r"\b(?:where(?:'s| is)|show me where|find|who(?:'s| is)|look up)\b",
    re.IGNORECASE,
)
CLINICAL_HR_RE = re.compile(r"\bheart rate is (\d{2,3})\b", re.IGNORECASE)
ROUTE_RE = re.compile(r"\b(?:route|directions|map|traffic|navigate|leave by)\b", re.IGNORECASE)
RESEARCH_RE = re.compile(
    r"\b(?:research|audit|why do you know|why you know|provenance|source|citation)\b",
    re.IGNORECASE,
)
GEAR_RE = re.compile(
    r"\b(?:battery|gear|ops|devices?|fleet|diagnostics|systems|power usage|suit status)\b",
    re.IGNORECASE,
)
VOICE_RE = re.compile(r"\b(?:voice session|listen in|wire me)\b", re.IGNORECASE)
LIST_RE = re.compile(r"\b(?:list|priorities|checklist|to-?dos?|agenda)\b", re.IGNORECASE)
CONV_RE = re.compile(r"\b(?:conversation|thread|transcript)\b", re.IGNORECASE)
CHIP_RE = re.compile(r"\b(?:flash a chip|put a chip|confirmation chip|peripheral chip)\b", re.IGNORECASE)
TICKER_RE = re.compile(r"\b(?:ticker|glance bar)\b", re.IGNORECASE)
REFUSE_RE = re.compile(
    r"\b(?:deploy (?:the )?drones?|drone (?:strike|fleet)|instant kill|"
    r"scan (?:the )?(?:city|cameras?|nyc)|facial recognition|"
    r"hack (?:their|his|her|the)|target designation|"
    r"stranger(?:'s)? face)\b",
    re.IGNORECASE,
)
MAP_COORDS_RE = re.compile(r"\b(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\b")

MAX_WINDOWS_DEFAULT = 3
MAX_WINDOWS_EXPLICIT = 4
MAX_WINDOWS_FULL = 5


@dataclass
class SurfaceWindow:
    id: str
    kind: str
    size: str
    time_type: str
    placement: str
    title: str
    body: str
    ttl_ms: int | None = None
    items: list[str] = field(default_factory=list)
    recommendation: str | None = None
    source: str | None = None
    lookout: bool = False
    priority: float = 0.4
    origin_lat: float | None = None
    origin_lon: float | None = None
    dest_lat: float | None = None
    dest_lon: float | None = None
    questions: list[str] = field(default_factory=list)
    response: str | None = None
    layout: str = "stack"
    drift_x: int = 0
    drift_y: int = 0
    tilt: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {key: value for key, value in data.items() if value is not None}


@dataclass
class SurfacePlan:
    open: bool
    windows: list[SurfaceWindow] = field(default_factory=list)
    rationale: str = ""
    explicit: bool = False
    needed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "ev.hud.lookout.v1",
            "open": self.open,
            "explicit": self.explicit,
            "needed": self.needed,
            "rationale": self.rationale,
            "windows": [window.as_dict() for window in self.windows],
        }


def normalize_kind(kind: str | None) -> str:
    raw = (kind or "").strip().lower()
    if raw in {"", "auto", "decide", "default"}:
        return "auto"
    if raw in KINDS:
        return raw
    aliases = {
        "status": "card",
        "alert": "radar",
        "alerts": "radar",
        "health": "vitals",
        "calendar": "horizon",
        "next": "horizon",
        "person": "scope",
        "focus": "scope",
        "ops": "bench",
        "audit": "trace",
        "research": "trace",
        "timer": "pulse",
        "countdown": "pulse",
        "bar": "ticker",
        "voice": "wire",
        "chat": "conversation",
        "route": "map",
    }
    return aliases.get(raw, "card")


def normalize_size(size: str | None, kind: str) -> str:
    raw = (size or "").strip().lower()
    if raw in SIZES:
        return raw
    return KIND_DEFAULTS.get(kind, KIND_DEFAULTS["card"])[0]


def normalize_time_type(time_type: str | None, kind: str) -> str:
    raw = (time_type or "").strip().lower().replace("-", "_")
    if raw in TIME_TYPES:
        return raw
    return KIND_DEFAULTS.get(kind, KIND_DEFAULTS["card"])[1]


def normalize_placement(placement: str | None, kind: str) -> str:
    raw = (placement or "").strip().lower().replace("-", "_")
    if raw in PLACEMENTS:
        return raw
    return KIND_DEFAULTS.get(kind, KIND_DEFAULTS["card"])[2]


def normalize_layout(layout: str | None) -> str | None:
    raw = (layout or "").strip().lower().replace("-", "_")
    if raw in LAYOUTS:
        return raw
    return None


def stable_int(key: str) -> int:
    """djb2 — same algorithm as the web/native renderers."""
    value = 5381
    for byte in (key or "").encode("utf-8"):
        value = ((value << 5) + value + byte) & 0xFFFFFFFF
    return value


def extract_questions(*texts: str) -> list[str]:
    found: list[str] = []
    for text in texts:
        for match in re.findall(r"[^.!\n]{8,180}\?", text or ""):
            question = re.sub(r"\s+", " ", match).strip()
            if question and question not in found:
                found.append(question)
    return found[:6]


def _split_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip()[:240] for part in re.split(r"[|\n]", value) if part.strip()][:6]
    return [str(part).strip()[:240] for part in value if str(part).strip()][:6]


def pick_layout(
    *,
    window_id: str,
    kind: str,
    questions: list[str],
    response: str | None,
    body: str,
    items: list[str],
    layout: str | None = None,
) -> str:
    forced = normalize_layout(layout)
    if forced:
        return forced
    if kind in {"ticker", "conversation"}:
        return "ribbon"
    if kind in {"chip", "pulse"}:
        return "pulse"
    if kind == "map":
        return "field"
    asks = [item for item in questions if item.strip()]
    reply = (response or "").strip() or (body or "").strip()
    seed = stable_int(window_id or kind)
    if asks and reply and reply not in asks:
        pool: tuple[str, ...] = ("ask", "reply", "split", "ledger", "stack")
    elif asks:
        pool = ("ask", "stack", "ledger")
    elif items:
        pool = ("stack", "field", "ledger")
    else:
        pool = ("reply", "stack")
    return pool[seed % len(pool)]


def pick_drift(window_id: str, placement: str) -> tuple[int, int, float]:
    seed = stable_int(window_id or placement)
    drift_x = int(seed % 73) - 36
    drift_y = int((seed >> 7) % 61) - 30
    tilt = ((int((seed >> 14) % 29) - 14) / 10.0)
    if placement == "top":
        return int(seed % 41) - 20, 0, 0.0
    if placement == "center":
        return int(seed % 49) - 24, int((seed >> 8) % 37) - 18, tilt * 0.6
    return drift_x, drift_y, tilt


def ttl_for(time_type: str, override: int | None = None) -> int | None:
    if override is not None:
        return max(0, int(override))
    return TIME_TYPES.get(time_type)


def make_window(
    *,
    kind: str,
    title: str,
    body: str,
    size: str | None = None,
    time_type: str | None = None,
    placement: str | None = None,
    ttl_ms: int | None = None,
    items: Iterable[str] | None = None,
    recommendation: str | None = None,
    source: str | None = None,
    window_id: str | None = None,
    lookout: bool | None = None,
    priority: float = 0.4,
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    dest_lat: float | None = None,
    dest_lon: float | None = None,
    questions: Iterable[str] | None = None,
    response: str | None = None,
    layout: str | None = None,
) -> SurfaceWindow:
    kind = normalize_kind(kind)
    if kind == "auto":
        kind = "card"
    resolved_time = normalize_time_type(time_type, kind)
    is_lookout = (kind in LOOKOUT_KINDS) if lookout is None else bool(lookout)
    if is_lookout and resolved_time not in {"lookout", "session", "hold", "pulse"}:
        resolved_time = "lookout"
    ident = window_id or (f"lookout-{kind}" if is_lookout else f"hud-{uuid4().hex[:10]}")
    packed_items = [str(item)[:240] for item in (items or []) if str(item).strip()][:12]
    packed_questions = [str(item)[:240] for item in (questions or []) if str(item).strip()][:6]
    if not packed_questions and kind in ASK_KINDS:
        packed_questions = extract_questions(title, body)
    packed_response = (response[:4000] if response else None)
    if packed_response is None and kind in ASK_KINDS:
        blob = (body or "").strip()
        if blob and blob not in packed_questions:
            packed_response = blob[:4000]
    resolved_place = normalize_placement(placement, kind)
    drift_x, drift_y, tilt = pick_drift(ident, resolved_place)
    resolved_layout = pick_layout(
        window_id=ident,
        kind=kind,
        questions=packed_questions,
        response=packed_response,
        body=body or "",
        items=packed_items,
        layout=layout,
    )
    return SurfaceWindow(
        id=ident[:64],
        kind=kind,
        size=normalize_size(size, kind),
        time_type=resolved_time,
        placement=resolved_place,
        title=(title or "EVIE")[:120],
        body=(body or "")[:4000],
        ttl_ms=ttl_for(resolved_time, ttl_ms),
        items=packed_items,
        recommendation=(recommendation[:400] if recommendation else None),
        source=(source[:160] if source else None),
        lookout=is_lookout,
        priority=max(0.0, min(1.0, float(priority))),
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        dest_lat=dest_lat,
        dest_lon=dest_lon,
        questions=packed_questions,
        response=packed_response,
        layout=resolved_layout,
        drift_x=drift_x,
        drift_y=drift_y,
        tilt=tilt,
    )


def window_from_args(args: dict[str, Any]) -> SurfaceWindow:
    items = args.get("items") or []
    if isinstance(items, str):
        items = [part for part in re.split(r"[|\n]", items) if part.strip()]
    return make_window(
        kind=str(args.get("kind") or "card"),
        title=str(args.get("title") or "EVIE"),
        body=str(args.get("body") or args.get("text") or ""),
        size=args.get("size"),
        time_type=args.get("time_type") or args.get("time"),
        placement=args.get("placement") or args.get("place"),
        ttl_ms=args.get("ttl_ms") if args.get("ttl_ms") is not None else args.get("ttl"),
        items=items,
        recommendation=args.get("recommendation"),
        source=args.get("source"),
        window_id=args.get("id") or args.get("window_id"),
        lookout=args.get("lookout"),
        priority=float(args.get("priority") or 0.4),
        origin_lat=_maybe_float(args.get("lat") or args.get("origin_lat")),
        origin_lon=_maybe_float(args.get("lon") or args.get("origin_lon")),
        dest_lat=_maybe_float(args.get("dest_lat")),
        dest_lon=_maybe_float(args.get("dest_lon")),
        questions=_split_lines(args.get("questions")),
        response=args.get("response") or args.get("reply"),
        layout=args.get("layout"),
    )


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def plan_surfaces(
    message: str,
    *,
    strategy: InteractionStrategy | None = None,
    pending_alert_priority: float = 0.0,
    pending_alert_tier: str | None = None,
    explicit: bool | None = None,
    kind: str | None = None,
    title: str | None = None,
    body: str | None = None,
    items: Iterable[str] | None = None,
    recommendation: str | None = None,
    source: str | None = None,
    size: str | None = None,
    time_type: str | None = None,
    placement: str | None = None,
    ttl_ms: int | None = None,
    lookout: bool | None = None,
    window_id: str | None = None,
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    dest_lat: float | None = None,
    dest_lon: float | None = None,
    health_emergency: bool = False,
    calibration: dict[str, Any] | None = None,
    questions: Iterable[str] | None = None,
    response: str | None = None,
    layout: str | None = None,
) -> SurfacePlan:
    """Decide whether EVIE opens glass, and which windows.

    A sentence is enough for casual Q&A. Windows open when the owner asked
    to see something, asked EVIE to watch, or a signal is actually urgent.
    ``calibration`` is the learned surface policy (gold corpus + owner ratings).
    """

    if calibration is None:
        try:
            from app.training.surface import load_calibration

            calibration = load_calibration()
        except Exception:  # noqa: BLE001 - planner must work without a calibration file
            calibration = {}
    threshold = float((calibration or {}).get("urgency_threshold") or 0.75)
    suppress = {
        key: float(value)
        for key, value in ((calibration or {}).get("suppress_kinds") or {}).items()
        if float(value) >= 0.5
    }
    prefer_size = dict((calibration or {}).get("prefer_size") or {})
    prefer_time = dict((calibration or {}).get("prefer_time") or {})

    text = (message or "").strip()
    if REFUSE_RE.search(text):
        return SurfacePlan(
            open=False,
            windows=[],
            rationale="rejected film harm factor (weapons, city-scale surveillance, or hacking)",
            explicit=False,
            needed=False,
        )
    full = bool(FULL_HUD_RE.search(text))
    asked = bool(explicit) if explicit is not None else bool(
        EXPLICIT_RE.search(text) or WATCH_RE.search(text) or BRIEF_RE.search(text) or full
    )
    intent = strategy.intent if strategy is not None else ""
    mode = strategy.mode if strategy is not None else "casual"
    urgency = strategy.urgency if strategy is not None else 0.0
    spoken_hr = CLINICAL_HR_RE.search(text)
    spoken_hr_emergency = bool(spoken_hr and int(spoken_hr.group(1)) >= 140)
    needed = bool(
        mode == "emergency"
        or urgency >= threshold
        or health_emergency
        or spoken_hr_emergency
        or (pending_alert_tier == "urgent" and pending_alert_priority >= 0.7)
    )
    forced_kind = normalize_kind(kind)
    reasons: list[str] = []
    windows: list[SurfaceWindow] = []

    def add(kind_name: str, win_title: str, win_body: str, **kwargs: Any) -> None:
        if any(existing.kind == kind_name for existing in windows):
            return
        if kind_name in ASK_KINDS and "questions" not in kwargs:
            kwargs["questions"] = list(questions or []) or asked_questions
        if kind_name in ASK_KINDS and "response" not in kwargs:
            if response:
                kwargs["response"] = response
            elif (
                (body or "").strip()
                and (body or "").strip() != text.strip()
            ):
                kwargs["response"] = body
        if layout and "layout" not in kwargs:
            kwargs["layout"] = layout
        windows.append(
            make_window(
                kind=kind_name,
                title=win_title,
                body=win_body,
                items=kwargs.pop("items", items),
                recommendation=kwargs.pop("recommendation", recommendation),
                source=kwargs.pop("source", source),
                **kwargs,
            )
        )

    heading = (title or "").strip() or _title_from_message(text)
    copy = (body or "").strip() or text
    asked_questions = extract_questions(text, heading, copy)

    if forced_kind not in {"auto", ""}:
        add(
            forced_kind,
            heading,
            copy,
            size=size,
            time_type=time_type,
            placement=placement,
            ttl_ms=ttl_ms,
            lookout=lookout,
            window_id=window_id,
            origin_lat=origin_lat,
            origin_lon=origin_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            priority=0.7 if asked or needed else 0.45,
        )
        reasons.append(f"caller chose kind={forced_kind}")
    else:
        if full:
            add("vitals", "Vitals", "Body scan lookout — readiness and overnight signals.")
            add("radar", "Radar", "Watchlist and deadline lookout.")
            add("horizon", "Horizon", "Next commitments and leave-by.")
            add("card", heading or "Command", copy or "Suit HUD stacked from live signals.")
            reasons.append("full HUD / command-center request")
        if HEALTH_RE.search(text):
            add("vitals", heading or "Vitals", copy or "Readiness, sleep, HRV.")
            reasons.append("health / body-scan language")
        if ALERT_RE.search(text) or WATCH_RE.search(text) or (
            pending_alert_tier == "urgent" and pending_alert_priority >= 0.5
        ):
            add("radar", heading or "Radar", copy or "Alerts and deadlines EVIE is watching.")
            reasons.append("watch / alert / baby-monitor language")
        if CAL_RE.search(text):
            add("horizon", heading or "Horizon", copy or "What's next on the day.")
            reasons.append("calendar / next-up language")
        if PERSON_RE.search(text) and not HEALTH_RE.search(text):
            add("scope", heading or "Scope", copy or "Person or focus lock.")
            reasons.append("person / find language")
        if ROUTE_RE.search(text):
            add(
                "map",
                heading or "Route",
                copy or "Route briefing.",
                origin_lat=origin_lat,
                origin_lon=origin_lon,
                dest_lat=dest_lat,
                dest_lon=dest_lon,
            )
            reasons.append("route / map language")
        if BRIEF_RE.search(text):
            add("briefing", heading or "Briefing", copy or "Tactical brief.")
            reasons.append("briefing language")
        if RESEARCH_RE.search(text):
            add("trace", heading or "Trace", copy or "Sources and audit trail.")
            reasons.append("research / audit language")
        if GEAR_RE.search(text):
            add("bench", heading or "Bench", copy or "Gear and ops lookout.")
            reasons.append("gear / ops language")
        if VOICE_RE.search(text):
            add("wire", heading or "Wire", copy or "Live voice session.")
            reasons.append("voice-session language")
        if LIST_RE.search(text) and (asked or intent == "command"):
            add("list", heading or "List", copy, items=items)
            reasons.append("list / checklist language")
        if CONV_RE.search(text) and asked:
            add("conversation", heading or "Thread", copy)
            reasons.append("conversation language")
        if CHIP_RE.search(text):
            add("chip", heading or "Got it", copy or "Saved.", time_type="flash", size="chip")
            reasons.append("JARVIS peripheral chip")
        if TICKER_RE.search(text):
            add("ticker", heading or "Now", copy, time_type="glance", size="ticker")
            reasons.append("JARVIS glance ticker")
        if health_emergency:
            add(
                "vitals",
                "Vitals",
                copy or "A live metric is outside your safe band.",
                priority=0.95,
            )
            reasons.append("Helio/Health clinical flag")
        if needed and not any(window.kind == "pulse" for window in windows):
            add(
                "pulse",
                "Now",
                copy or "Urgent — act on this.",
                time_type="pulse",
                priority=0.95,
            )
            reasons.append("emergency / high urgency")
        if asked and not windows:
            add("card", heading or "EVIE", copy)
            reasons.append("explicit show request; default card")

    if intent == "small_talk" and not asked and not needed:
        windows = []
        reasons = ["small talk stays in the ear; no glass"]

    if suppress and not asked:
        kept = [window for window in windows if window.kind not in suppress]
        if len(kept) != len(windows):
            reasons.append("learned suppress from owner ratings")
            windows = kept

    for window in windows:
        preferred = prefer_size.get(window.kind)
        if preferred:
            window.size = normalize_size(str(preferred), window.kind)
        preferred_time = prefer_time.get(window.kind)
        if preferred_time:
            window.time_type = normalize_time_type(str(preferred_time), window.kind)
            window.ttl_ms = ttl_for(window.time_type)

    cap = int((calibration or {}).get("max_windows") or 0) or (
        MAX_WINDOWS_FULL if full else MAX_WINDOWS_EXPLICIT if asked else MAX_WINDOWS_DEFAULT
    )
    if len(windows) > cap:
        windows = windows[:cap]
        reasons.append(f"attention cap {cap}")

    should_open = bool(windows) and (asked or needed or forced_kind not in {"auto", ""})
    if not should_open:
        rationale = (
            "No HUD: a spoken answer is enough (less intrusive than Karen)."
            if not reasons
            else "; ".join(reasons)
        )
        return SurfacePlan(open=False, windows=[], rationale=rationale, explicit=asked, needed=needed)

    return SurfacePlan(
        open=True,
        windows=windows,
        rationale="; ".join(reasons) or "surface requested",
        explicit=asked,
        needed=needed,
    )


def _title_from_message(message: str) -> str:
    cleaned = re.sub(r"\s+", " ", message).strip()
    if not cleaned:
        return "EVIE"
    return cleaned[:48] + ("…" if len(cleaned) > 48 else "")


async def fill_windows_from_state(session: Any, plan: SurfacePlan) -> SurfacePlan:
    """Replace placeholder copy with live HUD signals when they exist."""

    if not plan.windows:
        return plan
    brief = None
    alerts = None
    calendar = None
    ops = None
    for window in plan.windows:
        try:
            if window.kind == "vitals" and brief is None:
                from app.ev.health_radar import morning_brief

                brief = await morning_brief(session)
            elif window.kind == "radar" and alerts is None:
                from app.ev import alert_radar

                alerts = await alert_radar.list_alerts(session, status="pending", limit=5)
            elif window.kind in {"horizon", "map"} and calendar is None:
                from app.ev import calendar as calendar_feed

                calendar = await calendar_feed.calendar_signals(session)
            elif window.kind == "bench" and ops is None:
                from app.ev.edith import ops_card

                ops = await ops_card(session)
        except Exception:  # noqa: BLE001 - lookout fill must never break a turn
            continue
        if window.kind == "vitals" and brief:
            readiness = brief.get("readiness")
            band = brief.get("band") or "unknown"
            window.title = "Vitals"
            if readiness is None:
                window.body = str(brief.get("recommendation") or "No health snapshot yet.")
            else:
                window.body = (
                    f"Readiness {readiness:.0f} · {band}. "
                    f"{brief.get('recommendation') or ''}"
                ).strip()
            window.items = [
                item
                for item in (
                    f"Sleep {brief['sleep_hours']}h" if brief.get("sleep_hours") is not None else "",
                    f"HRV {brief['hrv_ms']}ms" if brief.get("hrv_ms") is not None else "",
                    f"Resting HR {brief['resting_hr']}" if brief.get("resting_hr") is not None else "",
                    f"HR {brief['heart_rate']}" if brief.get("heart_rate") is not None else "",
                    f"SpO2 {brief['spo2']}%" if brief.get("spo2") is not None else "",
                    f"Stress {brief['stress']}" if brief.get("stress") is not None else "",
                )
                if item
            ]
            window.source = brief.get("source") or "health_radar"
        elif window.kind == "radar" and alerts is not None:
            window.title = "Radar"
            if not alerts:
                window.body = "No pending alerts. Radar is watching."
                window.items = []
            else:
                window.body = f"{len(alerts)} pending · top: {alerts[0].title}"
                window.items = [f"{row.title} — {row.body}"[:200] for row in alerts[:5]]
                window.priority = max(window.priority, float(alerts[0].priority or 0.0))
            window.source = "alert_radar"
        elif window.kind in {"horizon", "map"} and calendar:
            next_event = calendar.get("next_event") or {}
            window.title = "Horizon" if window.kind == "horizon" else window.title
            if next_event.get("summary"):
                start = next_event.get("start") or next_event.get("when") or ""
                window.body = f"Next: {next_event.get('summary')} {start}".strip()
                window.items = [
                    item
                    for item in (
                        f"Leave by {calendar.get('leave_by')}" if calendar.get("leave_by") else "",
                        f"Day density {calendar.get('today')}" if calendar.get("today") is not None else "",
                    )
                    if item
                ]
            elif not window.body:
                window.body = "No live calendar commitment stored."
            window.source = "calendar"
        elif window.kind == "bench" and ops is not None:
            window.title = "Bench"
            window.body = ops.summary
            window.items = list(ops.command_cards or [])[:5]
            window.source = "ops"
    for window in plan.windows:
        window.layout = pick_layout(
            window_id=window.id,
            kind=window.kind,
            questions=window.questions,
            response=window.response,
            body=window.body,
            items=window.items,
        )
        window.drift_x, window.drift_y, window.tilt = pick_drift(window.id, window.placement)
    return plan


async def compose_and_maybe_open(
    session: Any,
    *,
    message: str,
    reply: str | None = None,
    strategy: InteractionStrategy | None = None,
    pending_alert_priority: float = 0.0,
    pending_alert_tier: str | None = None,
    title: str | None = None,
    explicit: bool | None = None,
) -> dict[str, Any]:
    """Plan surfaces for a turn and open them when intelligence says so."""

    from app.notify.presence import open_presence
    from app.utils.text import utcnow

    health_emergency = False
    try:
        from app.ev.health_radar import latest_clinical

        clinical = await latest_clinical(session)
        health_emergency = bool(clinical.get("emergency"))
    except Exception:  # noqa: BLE001
        health_emergency = False
    plan = plan_surfaces(
        message,
        strategy=strategy,
        pending_alert_priority=pending_alert_priority,
        pending_alert_tier=pending_alert_tier,
        explicit=explicit,
        title=title,
        body=reply or "",
        health_emergency=health_emergency,
    )
    payload = plan.as_dict()
    payload["generated_at"] = utcnow().isoformat()
    if not plan.open:
        return payload
    from app.notify.proactive import may_speak_proactive

    emergency = bool(
        health_emergency or pending_alert_tier in {"urgent", "notify_card"}
    )
    decision = await may_speak_proactive(session, emergency=emergency)
    if not decision.allowed:
        payload["open"] = False
        payload["reason"] = decision.reason
        return payload
    await fill_windows_from_state(session, plan)
    if reply:
        for window in plan.windows:
            if window.kind in ASK_KINDS:
                if not window.body or window.body == message:
                    window.body = reply[:4000]
                window.response = reply[:4000]
                if not window.questions:
                    window.questions = extract_questions(message)
                window.layout = pick_layout(
                    window_id=window.id,
                    kind=window.kind,
                    questions=window.questions,
                    response=window.response,
                    body=window.body,
                    items=window.items,
                )
    outcome = await open_presence(windows=plan.windows, plan=plan, title=title or "EVIE", body=reply or message)
    payload = plan.as_dict()
    payload["generated_at"] = utcnow().isoformat()
    payload["opened"] = bool(outcome.get("opened"))
    payload["via"] = outcome.get("via")
    payload["surface"] = outcome.get("surface")
    payload["url"] = outcome.get("url")
    payload["degraded"] = bool(outcome.get("degraded"))
    if outcome.get("reason"):
        payload["reason"] = outcome["reason"]
    return payload
