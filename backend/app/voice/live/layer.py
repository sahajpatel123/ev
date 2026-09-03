"""Voice interaction layer: honesty, hold, pause, routing.

Voice is an interface to the operating layer, not the operating layer.
This module consumes Agent 1's ``evaluate_policy`` / ``HOLD_LINE`` contracts
and the existing protocol sheet, quiet-hours gate, device registry, and
``ev.hud.card.v1`` schema. It does not invent capabilities or approvals.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Literal

from app.ev.policy import HOLD_LINE, PolicyDecision
from app.ev.workbench import hud_card
from app.utils.text import utcnow

if TYPE_CHECKING:
    from app.voice.live.session import LiveSession

LiveIntent = Literal[
    "pause",
    "resume",
    "cancel",
    "capability",
    "refused",
    "none",
]

PAUSE_SPOKEN = "Paused. Say resume when you want me listening."
RESUME_SPOKEN = "Listening."
CANCEL_SPOKEN = "Stopped talking."
CAPABILITY_FALLBACK = (
    "I can list enabled, setup-required, and refused protocols from the "
    "assistant sheet. Ask what I can do after the store is available."
)
NO_LIVE_ACTION_TOOLS_SPOKEN = (
    "Live action tools are not available in this session. I can still converse, "
    "but I cannot honestly execute requests until the capability projection reconnects."
)
MIC_UNAVAILABLE = "I can't hear you — the microphone isn't available on this device."
PLAYBACK_UNAVAILABLE = "I heard you, but this device can't play speech."
MISSING_KEY_OPENAI = "Live speech isn't connected. EV_OPENAI_API_KEY is empty."
MISSING_KEY_XAI = "Live speech isn't connected. EV_XAI_API_KEY is empty."

_PAUSE_RE = re.compile(
    r"^(?:please\s+)?(?:pause(?:\s+listening)?|hold on(?: a second| a moment)?)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_RESUME_RE = re.compile(
    r"^(?:please\s+)?(?:resume(?:\s+listening)?|keep listening|continue listening|"
    r"i(?:'| a)?m back|wake(?:\s+up)?)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(
    r"^(?:please\s+)?(?:cancel that|stop talking|stop speaking|never mind that|"
    r"never mind|stop|don'?t click that)\s*[.!]?\s*$",
    re.IGNORECASE,
)

# Process-local live sockets. Unregister on close so tests stay isolated.
_BY_SESSION: dict[str, Any] = {}
_DEVICE_TO_SESSION: dict[str, str] = {}


def spoken_provider_disconnect(provider: str | None) -> str:
    label = _provider_label(provider)
    return f"{label} disconnected. I'll keep this session and reconnect."


def spoken_provider_quota_block(provider: str | None) -> str:
    """Truthful quota/spend-limit voice line.

    Retrying cannot fix this — the owner must raise the provider spend
    limit. Never report it as a generic reconnectable disconnect.
    """

    label = _provider_label(provider)
    return (
        f"{label} paused me: the account spend limit is reached. "
        "Raise the limit and just talk to me again."
    )


def ws_close_fields(exc: BaseException) -> tuple[int | None, str]:
    """Extract (close_code, close_reason) from a websockets failure, else (None, "")."""

    rcvd = getattr(exc, "rcvd", None)
    if rcvd is not None:
        code = getattr(rcvd, "code", None)
        reason = (getattr(rcvd, "reason", "") or "").strip()
        try:
            return int(code) if code is not None else None, reason[:200]
        except (TypeError, ValueError):
            return None, reason[:200]
    return None, ""


def is_quota_close(reason: str, exc_text: str = "") -> bool:
    blob = f"{reason} {exc_text}".lower()
    return (
        "insufficient_quota" in blob
        or "spend_limit" in blob
        or "spend limit" in blob
        or "quota_exceeded" in blob
        or "billing_hard_limit_reached" in blob
    )


def spoken_provider_connect_failed(provider: str | None, detail: str | None = None) -> str:
    label = _provider_label(provider)
    extra = f" {detail}" if detail else ""
    return f"{label} isn't reachable right now.{extra} I'll keep listening and retry."


def spoken_missing_key(provider: str | None) -> str:
    if (provider or "").strip().lower() == "openai":
        return MISSING_KEY_OPENAI
    return MISSING_KEY_XAI


def spoken_hardware_failure(kind: str) -> str:
    if kind in {"microphone", "mic", "capture"}:
        return MIC_UNAVAILABLE
    if kind in {"playback", "speaker", "tts"}:
        return PLAYBACK_UNAVAILABLE
    if kind in {"camera", "vision"}:
        return "I can't use the camera right now — camera access is unavailable."
    return f"This device can't {kind or 'complete that'} right now."


def classify_live_intent(text: str) -> LiveIntent:
    raw = (text or "").strip()
    if not raw:
        return "none"
    if _PAUSE_RE.match(raw):
        return "pause"
    if _RESUME_RE.match(raw):
        return "resume"
    if _CANCEL_RE.match(raw):
        return "cancel"
    from app.ev.protocols import is_capability_intent, is_refused_ask

    if is_refused_ask(raw):
        return "refused"
    if is_capability_intent(raw):
        return "capability"
    return "none"


def hold_result(
    decision: PolicyDecision,
    *,
    name: str,
    arguments: dict | None = None,
) -> dict[str, Any]:
    """Adapter-shaped confirm payload plus a HUD card. Never waits."""

    payload = decision.to_result()
    spoken = decision.spoken or HOLD_LINE
    payload["spoken"] = spoken
    payload["hold"] = True
    payload["audio_loop"] = "alive"
    payload["hud"] = approval_hold_hud(decision, name=name, arguments=arguments)
    return payload


def approval_hold_hud(
    decision: PolicyDecision,
    *,
    name: str,
    arguments: dict | None = None,
) -> dict[str, Any]:
    spoken = decision.spoken or HOLD_LINE
    return hud_card(
        "Confirm on this device",
        spoken,
        {
            "kind": "approval_hold",
            "tool": name,
            "arguments": dict(arguments or {}),
            "risk_class": decision.risk_class,
            "target": decision.target,
            "confirmation_channel": "hud_or_biometric",
            "ttl_seconds": decision.confirmation_ttl_seconds,
            "independent_confirmation": True,
            "wake_verification_insufficient": True,
            "factors": ["hud", "biometric", "webauthn"],
        },
        priority=0.95,
    )


def progress_hud(name: str, *, detail: str | None = None) -> dict[str, Any]:
    body = detail or f"Working on {name.replace('_', ' ')}."
    return hud_card(
        "In progress",
        body,
        {"kind": "progress", "tool": name},
        priority=0.4,
    )


def evidence_hud(
    name: str,
    result: dict[str, Any],
    *,
    spoken: str | None = None,
) -> dict[str, Any]:
    raw_evidence = result.get("evidence")
    evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
    source = evidence.get("source") or result.get("source") or name
    timestamp = evidence.get("timestamp") or utcnow().isoformat()
    body = spoken or str(result.get("spoken") or result.get("body") or f"{name} done.")
    return hud_card(
        str(result.get("title") or name.replace("_", " ").title()),
        body,
        {
            "kind": "evidence",
            "tool": name,
            "source": source,
            "timestamp": timestamp,
            "evidence": evidence,
        },
        priority=0.55,
    )


def tool_result_is_successful(result: dict[str, Any] | None) -> bool:
    """Return whether a tool payload contains evidence of a successful result.

    Tool wrappers are deliberately inconsistent: some put ``ok`` at the
    wrapper level, some inside ``result``, and older adapters only expose
    ``degraded``/``error``.  The voice layer must treat all of those failure
    shapes as failures; otherwise a provider can turn an unavailable action
    into an ``evidence`` card or a confident success sentence.
    """

    if not isinstance(result, dict):
        return False
    body = result.get("result") if isinstance(result.get("result"), dict) else result
    if result.get("ok") is False or body.get("ok") is False:
        return False
    return not (body.get("degraded") or body.get("error"))


_LIVE_TOOL_JSON_LIMIT = 3500
_LIVE_RESULT_KEEP = (
    "ok",
    "executed",
    "verified",
    "spoken",
    "error",
    "goal",
    "app",
    "compact",
    "observed",
    "playlist",
    "track",
    "artist",
    "index",
    "player_state",
    "method",
    "adapter",
    "dialog_present",
    "query",
    "window",
    "next_hint",
    "completion_claim_allowed",
    "must_continue",
    "matches",
    "tracks",
    "playlists",
    "candidates",
    "ordinal",
    "opened",
    "closed",
    "activated",
    "running",
    "count",
    "name",
    "bundle_id",
    "snapshot_id",
    "generation",
    "frame_id",
    "target",
    "action",
    "ui_changed",
    "files_changed",
    "project",
    "workspace",
    "brain",
    "degraded",
    "pending",
    "runs",
    "failure_reason",
    "working",
    "surface_pid",
    "surface_name",
    "control",
    "suggested_fallbacks",
    "goal_complete",
    "complete",
    "goal_progress",
    "target_found",
    "semantic_adapter_available",
    "scrollable_regions",
    "failure",
    "failure_class",
    "url",
    "title",
    "display",
    "body",
    "path",
    "keys",
    "image_delivered",
    "model_image_delivered",
    "labels",
    "lighting",
    "colors",
    "visual_facts",
    "follow_up",
    "frames_summary",
    "saved_path",
    "ocr_text",
    "local_ocr",
    "media_kind",
    "files_changed",
    "project",
    "brain",
    "degraded",
    "lines",
    "grounding",
    "life_shelf",
    "results",
)


_LIVE_GOAL_KEEP = (
    "goal_id",
    "status",
    "verified",
    "must_continue",
    "completion_claim_allowed",
    "requested_outcome",
    "failure_reason",
    "observed",
    "target_apps",
    "strategy",
    "milestone",
)


def _slim_live_goal(goal: Any) -> dict[str, Any] | None:
    if not isinstance(goal, dict):
        return None
    out = {key: goal[key] for key in _LIVE_GOAL_KEEP if key in goal and goal[key] is not None}
    observed = out.get("observed")
    if isinstance(observed, dict):
        out["observed"] = {
            key: observed[key]
            for key in ("app", "action", "query", "url", "title", "track", "playlist")
            if observed.get(key) not in (None, "")
        }
    for key in ("requested_outcome", "remaining"):
        text = str(out.get(key) or "")
        if len(text) > 240:
            out[key] = text[:240]
        elif not text:
            out.pop(key, None)
    remaining = str(goal.get("remaining") or "")
    if remaining and "remaining" not in out:
        out["remaining"] = remaining[:240]
    return out


def compact_live_tool_json(payload: dict[str, Any], *, limit: int = _LIVE_TOOL_JSON_LIMIT) -> str:
    """Serialize a Realtime function output as valid JSON under a hard size cap.

    Never slice a JSON string. A truncated ``{"ok": true, "elements": ...``
    prefix made the live Music turn look successful while the backend logged
    ``result=failed``. A successful Chrome search must not be rewritten as
    ``must_continue: true`` just because the overlay was large.
    """

    slim: dict[str, Any] = dict(payload)
    slim.pop("working", None)
    slim.pop("receipts", None)
    slim.pop("suggested_fallbacks", None)
    memory_tool = str(slim.get("name") or "") in {
        "search_memory",
        "recall",
        "recall_history",
    }
    if not memory_tool:
        slim.pop("evidence", None)
    result = slim.get("result")
    if memory_tool and isinstance(result, dict):
        hits: list[str] = []
        raw_hits = result.get("lines") or result.get("results") or result.get("evidence") or []
        for item in raw_hits[:4]:
            if isinstance(item, str):
                text = " ".join(item.split()).strip()
            elif isinstance(item, dict):
                text = " ".join(str(item.get("text") or "").split()).strip()
            else:
                continue
            if text:
                hits.append(text[:240])
        spoken = str(result.get("spoken") or slim.get("spoken") or "").strip()
        slim["result"] = {
            "count": result.get("count"),
            "grounding": result.get("grounding"),
            "life_shelf": result.get("life_shelf"),
            "spoken": spoken[:400] or None,
            "hits": hits,
        }
        if spoken:
            slim["spoken"] = spoken[:400]
        slim.pop("evidence", None)
    elif isinstance(result, dict):
        kept = {
            key: result[key]
            for key in _LIVE_RESULT_KEEP
            if key in result and result[key] is not None
        }
        apps = result.get("apps")
        if isinstance(apps, list):
            kept["apps"] = apps[:16]
        elements = result.get("elements")
        if isinstance(elements, list) and "compact" not in kept:
            kept["compact"] = str(result.get("compact") or "")[:1200]
        elif isinstance(elements, list) and elements and "elements" not in kept:
            kept["elements"] = [
                {k: item.get(k) for k in ("ref", "role", "title", "value") if item.get(k)}
                for item in elements[:8]
                if isinstance(item, dict)
            ]
        compact = str(kept.get("compact") or "")
        if len(compact) > 1200:
            kept["compact"] = compact[:1200] + "\n…"
        slim["result"] = kept
        slim.setdefault("goal", result.get("goal"))
        slim.setdefault("verified", result.get("verified"))
        slim.setdefault("executed", result.get("executed"))
        slim.setdefault("must_continue", result.get("must_continue"))
        slim.setdefault("completion_claim_allowed", result.get("completion_claim_allowed"))
        slim.setdefault("url", result.get("url"))
        slim.setdefault("query", result.get("query"))
        slim.setdefault("action", result.get("action"))
        slim.setdefault("app", result.get("app"))
        slim.setdefault("spoken", result.get("spoken"))
        slim.setdefault("error", result.get("error"))
    goal = _slim_live_goal(slim.get("goal"))
    if goal is not None:
        slim["goal"] = goal
    blob = json.dumps(slim, default=str, separators=(",", ":"))
    try:
        json.loads(blob)
    except json.JSONDecodeError:
        blob = ""
    if blob and len(blob) <= limit:
        return blob
    verified = slim.get("verified") is True or (goal or {}).get("verified") is True
    executed = slim.get("executed")
    if executed is None:
        executed = slim.get("ok") if not verified else True
    must_continue = slim.get("must_continue")
    if must_continue is None:
        must_continue = (goal or {}).get("must_continue")
    if verified:
        must_continue = False
    error = slim.get("error")
    if verified:
        error = None
    elif not error:
        error = "tool_output_compacted"
    fallback = {
        "ok": True if verified else bool(slim.get("ok")),
        "name": slim.get("name"),
        "executed": bool(executed) if executed is not None else verified,
        "verified": verified,
        "completion_claim_allowed": bool(
            slim.get("completion_claim_allowed") if slim.get("completion_claim_allowed") is not None else verified
        ),
        "must_continue": bool(must_continue) if must_continue is not None else (not verified),
        "spoken": str(slim.get("spoken") or "")[:280],
        "error": error,
        "url": slim.get("url"),
        "query": slim.get("query"),
        "action": slim.get("action"),
        "app": slim.get("app"),
        "goal": goal,
    }
    fallback = {key: value for key, value in fallback.items() if value is not None}
    return json.dumps(fallback, default=str, separators=(",", ":"))


def tool_result_hud(
    name: str,
    result: dict[str, Any],
    *,
    spoken: str | None = None,
) -> dict[str, Any]:
    """Present a completed or failed tool result without implying success."""

    body = result.get("result") if isinstance(result.get("result"), dict) else result
    body = body if isinstance(body, dict) else {}
    success = tool_result_is_successful(result)
    line = spoken or body.get("spoken") or body.get("reason") or body.get("error")
    if not line:
        line = f"{name.replace('_', ' ').capitalize()} completed." if success else (
            f"I couldn't complete {name.replace('_', ' ')} yet."
        )
    return hud_card(
        name.replace("_", " ").title(),
        str(line),
        {
            "kind": "tool_result",
            "tool": name,
            "success": success,
            "verified": success,
            "error": body.get("error") if not success else None,
        },
        priority=0.55 if success else 0.7,
    )


def build_live_capability_manifest(
    payload: dict[str, Any] | None = None,
    *,
    device_id: str | None = None,
    tts_device_id: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Normalize the live capability reply without dropping runtime state.

    ``protocols.capability_reply`` contains both the human protocol tour and
    the actor/device/provider-specific runtime projection. The latter is the
    same source used to build Realtime function tools, so keep it intact in
    the manifest consumed by ready/state events and HUD callers.
    """

    source = payload if isinstance(payload, dict) else {}
    capability_projection_present = any(
        key in source
        for key in (
            "runtime_manifest",
            "runtime_capabilities",
            "live_tool_projection",
            "realtime_tools",
            "executable_tools",
        )
    )
    runtime_manifest = source.get("runtime_manifest")
    if not isinstance(runtime_manifest, dict):
        runtime_manifest = {}

    def runtime_list(key: str, *fallbacks: str) -> list[Any]:
        for container in (source, runtime_manifest):
            value = container.get(key)
            if isinstance(value, list):
                return list(value)
            for fallback in fallbacks:
                value = container.get(fallback)
                if isinstance(value, list):
                    return list(value)
        return []

    def runtime_value(key: str, *fallbacks: str) -> Any:
        for container in (source, runtime_manifest):
            for candidate in (key, *fallbacks):
                if candidate in container and container[candidate] is not None:
                    return container[candidate]
        return None

    protocols = [item for item in source.get("protocols", []) if isinstance(item, dict)]
    # Protocol state and OS permission state are different dimensions.  A
    # ``needs_setup`` protocol means that its provider/integration is not
    # connected; it is not evidence that macOS denied a TCC permission.  Keep
    # those entries in the unavailable/setup bucket and only populate
    # ``missing_permissions`` from an explicit permission projection.
    derived_unavailable = [
        {
            "key": item.get("key"),
            "title": item.get("title"),
            "status": item.get("status"),
            "detail": item.get("detail"),
        }
        for item in protocols
        if item.get("status") in {"needs_setup", "locked", "unavailable"}
    ]
    enabled = source.get("enabled")
    if not isinstance(enabled, list):
        enabled = [item.get("title") for item in protocols if item.get("status") == "enabled"]
    unavailable = source.get("unavailable")
    if not isinstance(unavailable, list):
        unavailable = derived_unavailable
    raw_missing_permissions = source.get("missing_permissions")
    legacy_setup: list[Any] = []
    if isinstance(raw_missing_permissions, list):
        missing_permissions = []
        for value in raw_missing_permissions:
            status = str(value.get("status") or "").strip().casefold() if isinstance(value, dict) else ""
            if status in {"needs_setup", "locked", "unavailable"}:
                legacy_setup.append(value)
            else:
                missing_permissions.append(value)
    else:
        missing_permissions = [
            item for item in protocols if item.get("status") == "needs_permission"
        ]
    unavailable = list(unavailable) + legacy_setup

    def entry_keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return {
                str(value.get(field) or "").strip().casefold()
                for field in ("key", "name", "title")
                if str(value.get(field) or "").strip()
            }
        key = str(value or "").strip().casefold()
        return {key} if key else set()

    def unique_entries(values: list[Any]) -> list[Any]:
        seen: set[str] = set()
        result: list[Any] = []
        for value in values:
            keys = entry_keys(value)
            if not keys or keys & seen:
                continue
            seen.update(keys)
            result.append(value)
        return result

    enabled = unique_entries(enabled)
    enabled_keys = set().union(*(entry_keys(value) for value in enabled))
    missing_permissions = [
        value
        for value in unique_entries(missing_permissions)
        if not entry_keys(value) & enabled_keys
    ]
    permission_keys = set().union(*(entry_keys(value) for value in missing_permissions))
    unavailable = [
        value
        for value in unique_entries(unavailable)
        if not entry_keys(value) & (enabled_keys | permission_keys)
        and not (
            isinstance(value, dict)
            and str(value.get("status") or "").strip().casefold() == "refused"
        )
    ]
    current_devices = source.get("current_devices")
    if not isinstance(current_devices, list):
        current_devices = [
            value
            for value in (
                str(device_id) if device_id else None,
                str(tts_device_id) if tts_device_id else None,
            )
            if value
        ]
    active_providers = source.get("active_providers")
    if not isinstance(active_providers, dict):
        active_providers = {}
    active_providers = {
        **active_providers,
        "realtime": provider or active_providers.get("realtime") or "pipeline",
        "fallback": active_providers.get("fallback") or "pipeline",
    }
    requires_confirmation = runtime_list(
        "requires_confirmation", "required_confirmation", "confirmation_tools"
    )
    required_confirmation = source.get("required_confirmation")
    if not isinstance(required_confirmation, list):
        required_confirmation = list(requires_confirmation)
    fallbacks = source.get("fallbacks")
    if not isinstance(fallbacks, list):
        fallbacks = ["local_pipeline"]

    live_tool_projection = runtime_list("live_tool_projection", "live_tools", "tools")
    realtime_tools = runtime_list("realtime_tools", "live_tools", "tools")
    if not realtime_tools and live_tool_projection:
        # ``capability_reply`` carries the exact runtime projection under
        # ``runtime_manifest``. Derive the flat provider payload here so the
        # ready event can show what will actually enter session.update.
        from app.ev.capabilities import approved_realtime_function_tools

        realtime_tools = approved_realtime_function_tools(live_tool_projection)
    approved_tools = runtime_list("approved_tools")
    executable_tools = runtime_list("executable_tools")
    current_device = runtime_value("current_device", "device")
    current_provider = runtime_value("current_provider", "realtime_provider") or provider
    missing_setup = runtime_list("missing_setup")
    capability_error = runtime_value("capability_error")
    return {
        "schema_version": "ev.voice.capability-manifest.v1",
        "capability_projection_present": capability_projection_present,
        "generated_at": str(
            (source.get("hud") or {}).get("generated_at")
            if isinstance(source.get("hud"), dict)
            else utcnow().isoformat()
        ),
        "enabled": enabled,
        "protocols": protocols,
        "missing_permissions": missing_permissions,
        "current_devices": current_devices,
        "active_providers": active_providers,
        "required_confirmation": required_confirmation,
        "fallbacks": fallbacks,
        "unavailable": unavailable,
        "runtime_manifest": dict(runtime_manifest),
        "live_tool_projection": live_tool_projection,
        "realtime_tools": realtime_tools,
        "realtime_tool_names": [
            str(item.get("name"))
            for item in realtime_tools
            if isinstance(item, dict) and item.get("name")
        ],
        "approved_tools": approved_tools,
        "executable_tools": executable_tools,
        "current_device": current_device,
        "current_provider": current_provider,
        "missing_setup": missing_setup,
        "requires_confirmation": requires_confirmation,
        "capability_error": capability_error,
        "camera": runtime_value("camera") if isinstance(runtime_value("camera"), dict) else {},
    }


def protocol_hud_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    hud = payload.get("hud")
    return hud if isinstance(hud, dict) else None


def proactive_speech_allowed(
    *,
    emergency: bool = False,
    bypass_quiet_hours: bool = False,
) -> bool:
    """Quiet hours suppress unsolicited live speech. Owner-scheduled still speak."""

    if emergency or bypass_quiet_hours:
        return True
    from app.ev.ev_sense import quiet_hours_active

    return not quiet_hours_active()


def _hud_owner_scheduled(hud: dict | None) -> bool:
    payload = hud if isinstance(hud, dict) else {}
    return bool(payload.get(OWNER_SCHEDULED_KEY))


def register_live(live: LiveSession) -> None:
    session_id = getattr(live, "session_id", None)
    if not session_id:
        return
    _BY_SESSION[str(session_id)] = live
    device_id = getattr(live, "device_id", None)
    if device_id:
        _DEVICE_TO_SESSION[str(device_id)] = str(session_id)


def unregister_live(live: LiveSession) -> None:
    session_id = getattr(live, "session_id", None)
    if session_id:
        # Reconnects can briefly overlap while an older WebSocket is being
        # torn down.  An old cleanup must not unregister the newer live
        # session that now owns the same durable session id.
        key = str(session_id)
        if _BY_SESSION.get(key) is live:
            _BY_SESSION.pop(key, None)
    device_id = getattr(live, "device_id", None)
    if (
        device_id
        and _DEVICE_TO_SESSION.get(str(device_id)) == str(session_id)
        and _BY_SESSION.get(str(session_id)) is None
    ):
        _DEVICE_TO_SESSION.pop(str(device_id), None)


def live_for_session(session_id: str | None) -> LiveSession | None:
    if not session_id:
        return None
    return _BY_SESSION.get(str(session_id))


def live_for_device(device_id: str | None) -> LiveSession | None:
    if not device_id:
        return None
    session_id = _DEVICE_TO_SESSION.get(str(device_id))
    return live_for_session(session_id)


def active_lives() -> list[LiveSession]:
    return [live for live in list(_BY_SESSION.values()) if live is not None]


def reset_live_registry() -> None:
    """Test helper — process-local registry must not leak across cases."""

    _BY_SESSION.clear()
    _DEVICE_TO_SESSION.clear()


LIVE_MAIL_KEY = "_live_mail"
LIVE_MAIL_SOURCE = "voice.live.inject"
OWNER_SCHEDULED_KEY = "_owner_scheduled"


def attention_score(live: LiveSession, *, prefer_device_id: str | None = None) -> int:
    """Prefer an unmuted in-process socket, then the named device, then TTS target."""

    if getattr(live, "_closed", False):
        return -1
    score = 0
    muted = bool(getattr(live, "_muted", False))
    paused = bool(getattr(live, "_paused", False))
    if not muted and not paused:
        score += 100
    elif not paused:
        score += 15
    device = str(getattr(live, "device_id", None) or "")
    tts = str(getattr(live, "tts_device_id", None) or "")
    wanted = str(prefer_device_id or "")
    if wanted and device == wanted:
        score += 50
    if wanted and tts == wanted:
        score += 25
    if getattr(live, "_approval_hold", None):
        score += 10
    return score


def pick_in_process_live(
    *,
    device_id: str | None = None,
    emergency: bool = False,
) -> LiveSession | None:
    scored: list[tuple[int, LiveSession]] = []
    for live in active_lives():
        if getattr(live, "_closed", False):
            continue
        muted = bool(getattr(live, "_muted", False))
        paused = bool(getattr(live, "_paused", False))
        if not emergency and (muted or paused):
            continue
        scored.append((attention_score(live, prefer_device_id=device_id), live))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def sidecar_intent(text: str) -> tuple[str, dict] | None:
    """Legacy pipeline-only transcript fallback; Realtime uses function calls."""

    from app.ev.tool_select import resolve_live_action

    return resolve_live_action(text)


def stamp_live_mail(
    row: Any,
    *,
    device_id: str | None = None,
    session_id: str | None = None,
) -> None:
    hud = dict(getattr(row, "hud", None) or {})
    hud[LIVE_MAIL_KEY] = {
        "pending": True,
        "device_id": device_id,
        "session_id": session_id,
    }
    row.hud = hud
    row.spoken = False


async def enqueue_live_mail(
    session: Any,
    text: str,
    *,
    hud: dict | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
    emergency: bool = False,
) -> Any:
    from app.models import Callout

    mail_hud = dict(hud or {})
    mail_hud[LIVE_MAIL_KEY] = {
        "pending": True,
        "device_id": device_id,
        "session_id": session_id,
    }
    row = Callout(
        text=text,
        source=LIVE_MAIL_SOURCE,
        source_item=str(session_id or device_id or "")[:128] or None,
        hud=mail_hud,
        spoken=False,
        emergency=emergency,
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


async def deliver_pending_live_mail(session: Any, live: LiveSession) -> int:
    """Speak parked injects on the socket that owns this process. Never waits."""

    from sqlalchemy import select

    from app.models import Callout

    if getattr(live, "_closed", False):
        return 0
    rows = list(
        (
            await session.execute(
                select(Callout)
                .where(Callout.spoken.is_(False))
                .order_by(Callout.created_at.asc())
                .limit(32)
            )
        )
        .scalars()
        .all()
    )
    delivered = 0
    live_session = str(getattr(live, "session_id", None) or "")
    live_device = str(getattr(live, "device_id", None) or "")
    live_tts = str(getattr(live, "tts_device_id", None) or "")
    for row in rows:
        hud = dict(row.hud or {})
        mail = hud.get(LIVE_MAIL_KEY)
        if not isinstance(mail, dict) or not mail.get("pending"):
            continue
        owner_scheduled = _hud_owner_scheduled(hud)
        if not proactive_speech_allowed(
            emergency=bool(row.emergency),
            bypass_quiet_hours=owner_scheduled,
        ):
            continue
        want_session = str(mail.get("session_id") or "")
        want_device = str(mail.get("device_id") or "")
        if want_session and live_session and want_session != live_session:
            continue
        if want_device and live_device and want_device not in {live_device, live_tts}:
            continue
        visible = {
            key: value
            for key, value in hud.items()
            if key not in {LIVE_MAIL_KEY, OWNER_SCHEDULED_KEY}
        }
        speaker = getattr(live, "speak_proactive", None)
        if speaker is None:
            continue
        await speaker(
            row.text,
            hud=visible or None,
            emergency=bool(row.emergency),
            bypass_quiet_hours=owner_scheduled,
        )
        row.spoken = True
        mail = dict(mail)
        mail["pending"] = False
        hud[LIVE_MAIL_KEY] = mail
        row.hud = hud
        delivered += 1
    if delivered:
        await session.flush()
    return delivered


async def drain_live_mail(live: LiveSession) -> int:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        count = await deliver_pending_live_mail(session, live)
        await session.commit()
    return count


async def speak_on_live(
    text: str,
    *,
    hud: dict | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
    emergency: bool = False,
    db: Any | None = None,
    persist_on_miss: bool = True,
    bypass_quiet_hours: bool = False,
) -> bool:
    """Push a proactive line onto a live socket. Never blocks on approval.

    In-process unmuted sockets win. If this worker has no live websocket,
    the line is parked on the existing Callout table and the owning process
    drains it on the next tick.
    """

    owner_scheduled = bypass_quiet_hours or _hud_owner_scheduled(hud)
    if not text or not proactive_speech_allowed(
        emergency=emergency,
        bypass_quiet_hours=owner_scheduled,
    ):
        return False
    live = None
    if session_id:
        live = live_for_session(session_id)
    if live is None:
        live = pick_in_process_live(device_id=device_id, emergency=emergency)
    if live is None:
        live = live_for_device(device_id)
        if (
            live is not None
            and not emergency
            and (getattr(live, "_muted", False) or getattr(live, "_paused", False))
        ):
            live = None
    if live is not None:
        speaker = getattr(live, "speak_proactive", None)
        if speaker is not None:
            await speaker(
                text,
                hud=(
                    {
                        key: value
                        for key, value in hud.items()
                        if key not in {LIVE_MAIL_KEY, OWNER_SCHEDULED_KEY}
                    }
                    if hud
                    else None
                )
                or None,
                emergency=emergency,
                bypass_quiet_hours=owner_scheduled,
            )
            return True
    if not persist_on_miss:
        return False

    async def _persist(session: Any) -> None:
        await enqueue_live_mail(
            session,
            text,
            hud=hud,
            device_id=device_id,
            session_id=session_id,
            emergency=emergency,
        )

    if db is not None:
        await _persist(db)
        return False
    from app.db import SessionLocal

    async with SessionLocal() as session:
        await _persist(session)
        await session.commit()
    return False


def _provider_label(provider: str | None) -> str:
    kind = (provider or "").strip().lower()
    if kind in {"openai", "openai-realtime"}:
        return "OpenAI Realtime"
    if kind in {"xai", "grok", "grok-voice"}:
        return "Grok Voice"
    if kind in {"pipeline", "local"}:
        return "Local speech"
    return "The voice provider"
