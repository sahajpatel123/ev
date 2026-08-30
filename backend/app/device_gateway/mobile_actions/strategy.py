"""Choose the strongest legitimate method. Native broker is the product path."""

from __future__ import annotations

from typing import Any

from .registry import (
    CLASS_BLOCKED,
    get_capability,
    is_blocked,
)


def route(
    operation: str,
    *,
    handshake: dict[str, Any] | None,
    has_explicit_number: bool = False,
    has_url: bool = False,
) -> dict[str, Any]:
    if is_blocked(operation):
        return {
            "method": "unsupported",
            "reason": "HIGH_RISK",
            "class_level": CLASS_BLOCKED,
        }
    cap = get_capability(operation)
    if cap is None:
        return {"method": "unsupported", "reason": "ACTION_UNAVAILABLE", "class_level": 3}
    native = bool((handshake or {}).get("native_shell"))
    reported = set((handshake or {}).get("capabilities") or ())
    legacy_bridge = bool((handshake or {}).get("bridge_installed")) and bool(
        (handshake or {}).get("legacy_bridge")
    )

    if "native_broker" in cap.methods and native:
        if reported and cap.operation not in reported and cap.operation not in {"self_test", "haptic"}:
            if "web_handoff" in cap.methods and (has_url or has_explicit_number or not cap.needs_native):
                return {
                    "method": "web_handoff",
                    "reason": None,
                    "class_level": cap.class_level,
                    "capability": cap,
                }
            return {
                "method": "unsupported",
                "reason": "ACTION_UNAVAILABLE",
                "class_level": cap.class_level,
                "capability": cap,
            }
        return {
            "method": "native_broker",
            "reason": None,
            "class_level": cap.class_level,
            "capability": cap,
        }

    if cap.needs_native and not native:
        if operation in {"call_contact", "facetime_contact", "message_contact"} and has_explicit_number:
            return {
                "method": "web_handoff",
                "reason": None,
                "class_level": cap.class_level,
                "capability": cap,
            }
        if "web_handoff" in cap.methods and (has_url or operation in {
            "start_directions",
            "open_maps",
            "share_content",
            "copy_to_clipboard",
            "open_app",
        }):
            return {
                "method": "web_handoff",
                "reason": None,
                "class_level": cap.class_level,
                "capability": cap,
            }
        return {
            "method": "unsupported",
            "reason": "NATIVE_SHELL_REQUIRED",
            "class_level": cap.class_level,
            "capability": cap,
        }

    for method in cap.methods:
        if method == "web_handoff" and (has_url or has_explicit_number or operation in {
            "start_directions",
            "open_maps",
            "share_content",
            "copy_to_clipboard",
            "open_app",
        }):
            return {
                "method": method,
                "reason": None,
                "class_level": cap.class_level,
                "capability": cap,
            }
        if method == "app_url" and has_url:
            return {
                "method": method,
                "reason": None,
                "class_level": cap.class_level,
                "capability": cap,
            }
        if method == "shortcuts_bridge" and legacy_bridge:
            return {
                "method": method,
                "reason": None,
                "class_level": cap.class_level,
                "capability": cap,
            }
    if "web_handoff" in cap.methods:
        return {
            "method": "web_handoff",
            "reason": None,
            "class_level": cap.class_level,
            "capability": cap,
        }
    return {
        "method": "unsupported",
        "reason": "ACTION_UNAVAILABLE",
        "class_level": cap.class_level,
        "capability": cap,
    }
