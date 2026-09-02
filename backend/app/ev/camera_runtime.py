"""Live camera observation runtime: frames, correlation, and readiness.

Physical capture stays on the connected Mac client. This module is the
process-local handoff so the Realtime bridge can inject actual image bytes
without stuffing pixels through truncated tool JSON or the attachment store.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("ev.camera")

JPEG_SOI = b"\xff\xd8"
MAX_JPEG_BYTES = 1_500_000
MIN_JPEG_BYTES = 64
VISION_TOOLS = frozenset({"look", "observe_camera", "capture_photo", "record_video"})
OBSERVE_MAX_SECONDS = 8.0
OBSERVE_MAX_FRAMES = 5
OBSERVE_DEFAULT_SECONDS = 4.0
OBSERVE_DEFAULT_INTERVAL = 1.0
RECORD_MIN_SECONDS = 2.0
RECORD_MAX_SECONDS = 30.0
RECORD_DEFAULT_SECONDS = 8.0
RECORD_MAX_POSTERS = 4
VISION_ARGUMENT_ALIASES = {
    "duration": "duration_seconds",
    "seconds": "duration_seconds",
    "length": "duration_seconds",
    "clip_seconds": "duration_seconds",
}
DARK_EXCUSE_RE = (
    "too dark",
    "too dim",
    "a bit dark",
    "a bit darker",
    "cannot see",
    "can't see",
    "could not see",
    "couldn't see",
    "unreadable",
    "poor lighting",
    "not enough light",
    "image is dark",
    "photo is dark",
)

_PENDING: dict[str, list[CameraObservation]] = {}


@dataclass
class LookFrame:
    """One camera observation returned by the connected client."""

    request_id: str
    jpeg: bytes | None = None
    attachment_id: str | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None
    permission: str | None = None
    camera_name: str | None = None
    sequence: int = 0
    last: bool = True
    t1_client_ms: float | None = None
    t2_capture_start_ms: float | None = None
    t3_captured_ms: float | None = None
    encoded_bytes: int = 0
    labels: list[str] | None = None
    ocr_text: str | None = None
    luminance: float | None = None
    face_count: int | None = None
    person_count: int | None = None
    lighting: str | None = None
    colors: list[str] | None = None
    saved_path: str | None = None
    media_kind: str | None = None
    duration_ms: int | None = None


@dataclass
class CameraObservation:
    """Bytes stashed for Realtime image injection; never logged."""

    request_id: str
    call_id: str
    jpeg: bytes
    mime: str = "image/jpeg"
    width: int | None = None
    height: int | None = None
    detail: str = "high"
    camera_name: str | None = None
    sequence: int = 0
    t0: float = 0.0
    t1: float = 0.0
    t3: float = 0.0
    t4: float = 0.0


@dataclass
class CameraReadiness:
    capability_declared: bool = True
    client_connected: bool = False
    permission: str = "unknown"
    capture_ready: bool = False
    realtime_image_input_ready: bool = False
    last_capture_status: str | None = None
    last_error: str | None = None
    reason: str | None = None
    camera_name: str | None = None
    device_id: str | None = None
    session_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_declared": self.capability_declared,
            "client_connected": self.client_connected,
            "permission": self.permission,
            "capture_ready": self.capture_ready,
            "realtime_image_input_ready": self.realtime_image_input_ready,
            "last_capture_status": self.last_capture_status,
            "last_error": self.last_error,
            "reason": self.reason,
            "camera_name": self.camera_name,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "model_image_path_ready": bool(
                self.capture_ready and self.realtime_image_input_ready
            ),
        }


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read SOF width/height from a JPEG without decoding pixels."""

    if len(data) < 20 or not data.startswith(JPEG_SOI):
        return None
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in {0xD8, 0xD9}:
            i += 2
            continue
        if marker in {0xC0, 0xC1, 0xC2, 0xC3}:
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            if width > 0 and height > 0:
                return width, height
            return None
        if marker >= 0xD0 and marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            return None
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length < 2:
            return None
        i += 2 + length
    return None


def validate_jpeg(data: bytes | None) -> tuple[bytes, int | None, int | None] | None:
    """Accept a real JPEG payload or return None. Never logs bytes."""

    if not data:
        return None
    if len(data) < MIN_JPEG_BYTES or len(data) > MAX_JPEG_BYTES:
        return None
    if not data.startswith(JPEG_SOI):
        return None
    width, height = jpeg_dimensions(data) or (None, None)
    return data, width, height


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        item = value.strip()
        return [item] if item else []
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text.lower() not in {name.lower() for name in names}:
            names.append(text)
    return names[:8]


def name_rgb_color(r: float, g: float, b: float) -> str:
    """Name one 0–255 RGB sample. Matches the Mac camera color buckets."""

    red = max(0.0, min(255.0, float(r))) / 255.0
    green = max(0.0, min(255.0, float(g))) / 255.0
    blue = max(0.0, min(255.0, float(b))) / 255.0
    peak = max(red, green, blue)
    floor = min(red, green, blue)
    chroma = peak - floor
    light = (peak + floor) / 2.0
    sat = 0.0 if peak <= 1e-6 else chroma / peak
    if peak < 0.18:
        return "black"
    if floor > 0.82:
        return "white"
    if sat < 0.18:
        if light > 0.72:
            return "white"
        if light < 0.28:
            return "black"
        return "gray"
    if chroma <= 1e-6:
        return "gray"
    if peak == red:
        hue = (green - blue) / chroma
    elif peak == green:
        hue = 2.0 + (blue - red) / chroma
    else:
        hue = 4.0 + (red - green) / chroma
    hue = (hue / 6.0) % 1.0
    if light < 0.28 and sat < 0.55:
        return "brown"
    if hue < 0.04 or hue >= 0.93:
        return "red" if light > 0.35 else "brown"
    if hue < 0.10:
        return "orange" if light > 0.45 else "brown"
    if hue < 0.18:
        return "yellow"
    if hue < 0.45:
        return "green"
    if hue < 0.55:
        return "cyan"
    if hue < 0.73:
        return "blue"
    if hue < 0.85:
        return "purple"
    return "pink"


def dominant_color_names(samples: list[tuple[float, float, float]]) -> list[str]:
    """Return up to three color names from RGB samples, most common first."""

    counts: dict[str, int] = {}
    for sample in samples:
        if len(sample) < 3:
            continue
        name = name_rgb_color(sample[0], sample[1], sample[2])
        counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    minimum = max(1, int(len(samples) * 0.06))
    names = [name for name, count in ranked if count >= minimum]
    return names[:3] or ([ranked[0][0]] if ranked else [])


def parse_look_frame_meta(message: dict[str, Any]) -> dict[str, Any]:
    """Client-side vision metadata that travels with a look_frame."""

    labels = _as_string_list(message.get("labels") or message.get("objects"))
    colors = _as_string_list(message.get("colors") or message.get("dominant_colors"))
    ocr = str(message.get("ocr_text") or message.get("ocr") or "").strip() or None
    lighting = str(message.get("lighting") or "").strip() or None
    saved = str(message.get("saved_path") or message.get("path") or "").strip() or None
    kind = str(message.get("media_kind") or message.get("kind") or "").strip().lower() or None
    luminance = None
    raw_lum = message.get("luminance")
    try:
        if raw_lum is not None:
            luminance = float(raw_lum)
    except (TypeError, ValueError):
        luminance = None
    if lighting is None:
        lighting = lighting_from_luminance(luminance)

    def _int(key: str) -> int | None:
        try:
            value = message.get(key)
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "labels": labels,
        "colors": colors,
        "ocr_text": ocr,
        "luminance": luminance,
        "face_count": _int("face_count"),
        "person_count": _int("person_count"),
        "lighting": lighting,
        "saved_path": saved,
        "media_kind": kind,
        "duration_ms": _int("duration_ms") or _int("clip_duration_ms"),
    }


def decode_frame_payload(raw: str | None) -> bytes | None:
    """Decode a look_frame jpeg_b64 / data-URL field."""

    if not raw or not isinstance(raw, str):
        return None
    blob = raw.strip()
    if not blob:
        return None
    if "," in blob and blob.lower().startswith("data:"):
        blob = blob.split(",", 1)[1]
    try:
        import base64

        return base64.b64decode(blob, validate=False)
    except Exception:  # noqa: BLE001 - malformed client payload
        return None


def build_realtime_image_item(
    jpeg: bytes,
    *,
    mime: str = "image/jpeg",
    detail: str = "high",
    event_id: str | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    """OpenAI Realtime conversation.item.create payload for one image."""

    import base64

    image_url = f"data:{mime};base64,{base64.b64encode(jpeg).decode('ascii')}"
    text = (prompt or "Camera observation from the owner's MacBook.").strip()
    item: dict[str, Any] = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": text},
                {
                    "type": "input_image",
                    "image_url": image_url,
                    "detail": detail if detail in {"auto", "low", "high"} else "high",
                },
            ],
        },
    }
    if event_id:
        item["event_id"] = event_id
    return item


def stash_observation(observation: CameraObservation) -> None:
    bucket = _PENDING.setdefault(observation.call_id, [])
    bucket.append(observation)
    log_camera(
        "camera.frame_received_server",
        request_id=observation.request_id,
        extra={
            "width": observation.width,
            "height": observation.height,
            "encoded_bytes": len(observation.jpeg),
            "sequence": observation.sequence,
            "camera_name": observation.camera_name,
        },
    )


def pop_observations(call_id: str | None) -> list[CameraObservation]:
    if not call_id:
        return []
    return _PENDING.pop(str(call_id), [])


def clear_observations(call_id: str | None) -> None:
    if call_id:
        _PENDING.pop(str(call_id), None)


def reset_pending_observations() -> None:
    """Test helper."""

    _PENDING.clear()


def log_camera(event: str, *, request_id: str | None = None, extra: dict[str, Any] | None = None) -> None:
    payload = dict(extra or {})
    payload.pop("jpeg", None)
    payload.pop("image_url", None)
    payload.pop("image_b64", None)
    payload.pop("jpeg_b64", None)
    logger.warning(
        "camera_trace event=%s request_id=%s %s",
        event,
        request_id or "-",
        " ".join(f"{key}={value}" for key, value in payload.items() if value is not None),
    )


def normalize_permission(value: str | None) -> str:
    raw = str(value or "unknown").strip().lower()
    aliases = {
        "authorized": "authorized",
        "granted": "authorized",
        "allowed": "authorized",
        "denied": "denied",
        "restricted": "denied",
        "notdetermined": "not_determined",
        "not_determined": "not_determined",
        "undetermined": "not_determined",
        "unknown": "unknown",
    }
    compact = raw.replace("-", "_").replace(" ", "")
    return aliases.get(raw) or aliases.get(compact) or "unknown"


def readiness_from_camera_state(
    state: dict[str, Any] | None,
    *,
    client_connected: bool,
    realtime_provider: str | None,
    device_id: str | None = None,
    session_id: str | None = None,
    connecting_device: bool = False,
) -> CameraReadiness:
    raw = dict(state or {})
    permission = normalize_permission(
        str(raw.get("permission_state") or raw.get("permission") or "")
    )
    camera_state = str(raw.get("state") or "").strip().lower()
    if camera_state == "denied":
        permission = "denied"
    connected = bool(client_connected or connecting_device)
    provider = str(realtime_provider or "").strip().lower()
    image_ready = provider in {"openai", "openai-realtime"}
    denied = permission == "denied"
    capture_ready = connected and not denied
    reason: str | None = None
    if not connected:
        reason = "no_camera_client"
    elif denied:
        reason = "macos_permission_denied"
    elif not image_ready:
        reason = "realtime_image_input_unavailable"
    elif permission in {"unknown", "not_determined"}:
        reason = "permission_not_determined"
    return CameraReadiness(
        capability_declared=True,
        client_connected=connected,
        permission=permission,
        capture_ready=capture_ready,
        realtime_image_input_ready=image_ready,
        last_error=str(raw.get("last_error") or "") or None,
        reason=reason,
        camera_name=str(raw.get("camera_name") or raw.get("device_name") or "") or None,
        device_id=str(raw.get("device_id") or device_id or "") or None,
        session_id=session_id,
    )


def overlay_vision_entry(entry: dict[str, Any], readiness: CameraReadiness) -> dict[str, Any]:
    """Bind look/observe exposure to a real capture path, not a static vision slug."""

    out = dict(entry)
    out["capture_ready"] = readiness.capture_ready
    out["camera"] = readiness.as_dict()
    if not readiness.client_connected:
        out["availability"] = "not_connected"
        out["availability_reason"] = "no camera source is currently connected"
        out["model_exposed"] = False
        out["realtime_eligible"] = False
        out["executable"] = False
        out["fallback_reason"] = out["availability_reason"]
        return out
    if not readiness.realtime_image_input_ready:
        out["availability"] = "not_connected"
        out["availability_reason"] = "realtime image input is not ready on this live provider"
        out["model_exposed"] = False
        out["realtime_eligible"] = False
        out["executable"] = False
        out["fallback_reason"] = out["availability_reason"]
        return out
    out["availability"] = "available"
    if readiness.permission == "denied":
        out["availability_reason"] = "macOS has not granted EV camera access"
    elif readiness.permission == "not_determined":
        out["availability_reason"] = "macOS camera authorization will be requested on first look"
    else:
        out["availability_reason"] = "live Mac camera path ready"
    return out


def lighting_from_luminance(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        luminance = float(value)
    except (TypeError, ValueError):
        return None
    if luminance >= 0.18:
        return "normally lit"
    if luminance >= 0.10:
        return "moderately lit"
    return "dim"


def looks_like_dark_excuse(text: str | None) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return any(token in lowered for token in DARK_EXCUSE_RE)


def camera_operator_line(readiness: CameraReadiness | dict[str, Any] | None) -> str:
    raw = readiness.as_dict() if isinstance(readiness, CameraReadiness) else dict(readiness or {})
    if raw.get("capture_ready") and raw.get("realtime_image_input_ready"):
        return (
            "CAMERA: AVAILABLE. Look to see the current scene, capture_photo to "
            "take and save a still, record_video to save a clip, and "
            "observe_camera for a few seconds of change. The owner does not "
            "need an extra confirmation for normal camera use."
        )
    if raw.get("permission") == "denied":
        return (
            "CAMERA / VISUAL PERCEPTION: UNAVAILABLE. macOS has not granted EV "
            "camera access."
        )
    if not raw.get("client_connected"):
        return (
            "CAMERA / VISUAL PERCEPTION: UNAVAILABLE. No camera source is "
            "currently connected."
        )
    if not raw.get("realtime_image_input_ready"):
        return (
            "CAMERA / VISUAL PERCEPTION: UNAVAILABLE. The live model cannot "
            "receive camera images on this provider."
        )
    return (
        "CAMERA / VISUAL PERCEPTION: UNAVAILABLE. "
        + str(raw.get("reason") or "camera path is not ready")
        + "."
    )


def camera_model_instructions(readiness: CameraReadiness | dict[str, Any] | None) -> str:
    raw = readiness.as_dict() if isinstance(readiness, CameraReadiness) else dict(readiness or {})
    available = bool(raw.get("capture_ready") and raw.get("realtime_image_input_ready"))
    line = camera_operator_line(raw)
    if available:
        return (
            f"{line} If answering requires seeing the room, a person, clothing, "
            "or what they are holding, or they ask you to memorize or remember "
            "something they are showing, call look. Read printed names and "
            "titles on whatever they are holding. That look is stored across app restarts — "
            "never say you cannot memorize a glance or that you cannot guarantee "
            "future recall. If they ask about the Mac "
            "screen, window, desktop, display, or which app is open, do not "
            "call look — that is computer. If the "
            "owner asks to take a photo, picture, or selfie of the room, call "
            "capture_photo. "
            "If they ask to record a video or film something, call record_video. "
            "Do not open the Camera app for those jobs. Do not guess. Do not "
            "claim you cannot see. After look, capture_photo, record_video, or "
            "observe_camera returns, attached images are already in the "
            "conversation. Speak two to four natural sentences about people, "
            "clothing and its colors, pose, objects, and the overall scene. If "
            "a garment or object is visible, name its color from the image; "
            "labels may miss it. Listed colors are scene hints, not a reason "
            "to hedge. For a recorded clip, say what they are doing. Do not "
            "read the function JSON aloud. Mention printed text only when the "
            "output actually includes it. Missing text is not a failure and is "
            "not what 'how was the image' means. Do not say the photo is too "
            "dark, darkened, blurry, or unreadable when people, objects, or "
            "colors are visible. After describing, mention saved_path if "
            "present. Follow-up questions about that image must keep describing "
            "what was seen; do not look again unless they ask to look again. "
            "If they ask later about a photo, clip, what they were wearing, "
            "what they asked you to remember from a look, whether you memorized "
            "or remembered something they showed, or when you last saw an object, "
            "that is search_memory, not a new "
            "look, unless they ask to look now. Do not say you have no record "
            "until search_memory returns empty evidence. When they are heading "
            "out, leaving the house, or gotta go, that is one heading-out beat, "
            "not separate weather and calendar chats. Never invent visual "
            "contents that contradict the attached image. Never pass a "
            "permission argument."
        )
    if raw.get("permission") == "denied":
        return (
            f"{line} If the owner asks you to look or identify something in "
            "view, call look so the failure is recorded, then say that macOS "
            "has not granted EV camera access. Do not claim you saw anything."
        )
    return (
        f"{line} If the owner asks you to look, say that no camera source is "
        "connected rather than inventing what is in front of the camera."
    )


def camera_image_prompt(name: str, *, index: int = 0, total: int = 1) -> str:
    """Text that travels with each Realtime input_image for a camera tool."""

    count = max(int(total or 1), 1)
    slot = index + 1
    if name in {"look", "capture_photo"}:
        kind = (
            "photo you just took with the owner's camera"
            if name == "capture_photo"
            else "current photo from the owner's MacBook camera"
        )
        return (
            f"This is a {kind}. Look at the image and describe it in natural "
            "speech: people, clothing, pose, objects, colors, and the setting. "
            "Do not only list labels. Mention printed text only if you can read "
            "it. Missing text is not a failure. Do not say it is too dark, "
            "darkened, or that you cannot see the image when people, objects, "
            "or colors are visible. This look is stored as memory. If they asked "
            "you to remember what they showed, say you will remember it. Never "
            "say you cannot guarantee future recall. Follow-up questions about this image should "
            "talk about those visual facts, not darkness or missing text. Do "
            "not name people unless enrolled."
        )
    if name in {"screen_look", "see", "click", "double_click", "right_click", "drag", "ui_action"}:
        return "Window screenshot from the owner's Mac. Describe only visible UI."
    if name == "record_video":
        lead = f"Frame {slot} of {count} from " if count > 1 else ""
        return (
            f"{lead}a video the owner just recorded on the camera. Look at this "
            "frame together with the others. Describe the clip as if you watched "
            "it: who is in it, clothing, objects, colors, and what they are doing. "
            "Do not only say the clip was saved. Do not name people unless enrolled."
        )
    return (
        f"Camera observation {slot} from a bounded watch. "
        "Describe people, objects, and colors in this frame. Compare "
        "with other frames. Object color is not room brightness."
    )


def clamp_observe_duration(seconds: float | int | None) -> float:
    try:
        value = float(seconds) if seconds is not None else OBSERVE_DEFAULT_SECONDS
    except (TypeError, ValueError):
        value = OBSERVE_DEFAULT_SECONDS
    return min(max(value, 1.0), OBSERVE_MAX_SECONDS)


def coerce_vision_arguments(
    name: str,
    arguments: dict[str, Any] | None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep a camera call running when the model copies look/capture fields.

    Live models often pass ``detail`` or ``duration`` on ``record_video``.
    Those extras must not abort recording.
    """

    args = dict(arguments or {})
    if name not in VISION_TOOLS:
        return args
    props = dict(properties or {})
    if not props:
        from app.ev.tools import get_spec

        props = ((get_spec(name) or {}).get("parameters") or {}).get("properties") or {}
    for alias, canonical in VISION_ARGUMENT_ALIASES.items():
        if alias not in args:
            continue
        if canonical in props and canonical not in args:
            args[canonical] = args.pop(alias)
        elif alias not in props:
            args.pop(alias, None)
    if "duration_seconds" in props and "duration_seconds" in args:
        try:
            args["duration_seconds"] = float(args["duration_seconds"])
        except (TypeError, ValueError):
            args.pop("duration_seconds", None)
    if not props:
        return args
    return {key: value for key, value in args.items() if key in props}


def clamp_record_duration(seconds: float | int | None) -> float:
    try:
        value = float(seconds) if seconds is not None else RECORD_DEFAULT_SECONDS
    except (TypeError, ValueError):
        value = RECORD_DEFAULT_SECONDS
    return min(max(value, RECORD_MIN_SECONDS), RECORD_MAX_SECONDS)


def now_mono() -> float:
    return time.monotonic()


def _register() -> None:
    from app.ev.capability_registry import RegisteredCapability, register_capability

    register_capability(
        RegisteredCapability(
            name="camera",
            description="Mac camera look, photo capture, and video recording",
            tools=VISION_TOOLS,
            overlay=overlay_vision_entry,
            readiness_key="camera",
            risk_class="R0",
        )
    )


_register()
