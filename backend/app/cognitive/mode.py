"""Cognitive OS V2 mode flags. Rollback: EV_COGNITIVE_MODE=legacy_mini."""

from __future__ import annotations

from app.config import settings

MUSE_KERNEL = "muse_kernel"
LEGACY_MINI = "legacy_mini"


def cognitive_mode() -> str:
    raw = (getattr(settings, "cognitive_mode", None) or LEGACY_MINI).strip().lower()
    if raw in {"muse", "muse_kernel", "kernel", "v2"}:
        return MUSE_KERNEL
    return LEGACY_MINI


def muse_kernel_active() -> bool:
    return cognitive_mode() == MUSE_KERNEL


def cognitive_role() -> str:
    raw = (getattr(settings, "cognitive_role", None) or "auto").strip().lower()
    if raw in {"kernel", "voice_edge"}:
        return raw
    # Talk sidecar always enables laptop files; production :8000 does not.
    if muse_kernel_active() and bool(getattr(settings, "laptop_files", False)):
        return "voice_edge"
    return "kernel" if muse_kernel_active() else "legacy"


def is_voice_edge() -> bool:
    return muse_kernel_active() and cognitive_role() == "voice_edge"


def is_kernel_process() -> bool:
    return muse_kernel_active() and not is_voice_edge()


def kernel_url() -> str:
    return (getattr(settings, "cognitive_kernel_url", None) or "http://127.0.0.1:8000").rstrip("/")


def mac_execute_url() -> str:
    return (
        getattr(settings, "cognitive_mac_execute_url", None) or "http://127.0.0.1:18000"
    ).rstrip("/")
