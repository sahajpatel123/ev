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
VISION_TOOLS = frozenset({"look", "observe_camera"})
OBSERVE_MAX_SECONDS = 8.0
OBSERVE_MAX_FRAMES = 5
OBSERVE_DEFAULT_SECONDS = 4.0
OBSERVE_DEFAULT_INTERVAL = 1.5

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


def camera_operator_line(readiness: CameraReadiness | dict[str, Any] | None) -> str:
    raw = readiness.as_dict() if isinstance(readiness, CameraReadiness) else dict(readiness or {})
    if raw.get("capture_ready") and raw.get("realtime_image_input_ready"):
        return (
            "CAMERA / VISUAL PERCEPTION: AVAILABLE. Look through the owner's Mac "
            "camera when visual context is needed. Capture a current observation, "
            "reason over that image, and use bounded observation when change over "
            "time matters. The owner does not need an extra confirmation for "
            "normal visual perception."
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
            f"{line} If answering correctly requires seeing something in the "
            "owner's physical environment, call look. Do not claim you cannot "
            "see, and do not guess. The owner does not need to say camera or "
            "look. Examples that require look: what am I holding, read this, "
            "what color is this, does this look damaged, which port should I "
            "use, look at me. Do not call look for weather, timers, or other "
            "non-visual questions. For change over a few seconds, call "
            "observe_camera. After look returns, a camera image may have been "
            "added to this conversation. Describe only what you can actually "
            "see. If no image is present, say you could not receive a camera "
            "frame. Never invent visual contents. Never pass a permission "
            "argument."
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


def clamp_observe_duration(seconds: float | int | None) -> float:
    try:
        value = float(seconds) if seconds is not None else OBSERVE_DEFAULT_SECONDS
    except (TypeError, ValueError):
        value = OBSERVE_DEFAULT_SECONDS
    return min(max(value, 1.0), OBSERVE_MAX_SECONDS)


def now_mono() -> float:
    return time.monotonic()


def _register() -> None:
    from app.ev.capability_registry import RegisteredCapability, register_capability

    register_capability(
        RegisteredCapability(
            name="camera",
            description="Mac camera observation",
            tools=VISION_TOOLS,
            overlay=overlay_vision_entry,
            readiness_key="camera",
            risk_class="R0",
        )
    )


_register()
