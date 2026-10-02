"""Cognitive OS V2 mode flags. Rollback: EV_COGNITIVE_MODE=legacy_mini."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from app.config import settings

MUSE_KERNEL = "muse_kernel"
JEV_KERNEL = "jev_kernel"
JEV = "jev"
MIMO_KERNEL = "mimo_kernel"
MIMO = "mimo"
LEGACY_MINI = "legacy_mini"
REALTIME_DELEGATE = "realtime_delegate"
_DELEGATE_TASK: ContextVar[str] = ContextVar("delegated_task_hint", default="")
_DELEGATE_BINDING: ContextVar[Any] = ContextVar("delegated_phone_binding", default=None)
_WORKER_MODE: ContextVar[bool] = ContextVar("delegated_mimo_worker", default=False)

def realtime_delegate_active() -> bool:
    return cognitive_mode() == REALTIME_DELEGATE


realtime_delegate_mode_active = realtime_delegate_active


def delegated_worker_active() -> bool:
    return _WORKER_MODE.get()



def cognitive_mode() -> str:
    if _WORKER_MODE.get():
        return MIMO_KERNEL
    raw = (getattr(settings, "cognitive_mode", None) or LEGACY_MINI).strip().lower()
    if raw in {"realtime_delegate", "realtime_first"}:
        return REALTIME_DELEGATE
    if raw in {"muse", "muse_kernel", "kernel", "v2"}:
        return MUSE_KERNEL
    if raw in {"jev", "jev_kernel"}:
        return JEV_KERNEL
    if raw in {"mimo", "mimo_kernel", "single", "single_brain"}:
        return MIMO_KERNEL
    return LEGACY_MINI


def kernel_mode_active() -> bool:
    """True for any single-mind kernel mode (Muse or MiMo)."""

    return cognitive_mode() in {MUSE_KERNEL, MIMO_KERNEL}


def muse_kernel_active() -> bool:
    return cognitive_mode() == MUSE_KERNEL


def mimo_kernel_active() -> bool:
    """MiMo-V2.6-Flash owns all non-speech reasoning."""

    return cognitive_mode() == MIMO_KERNEL


def jev_kernel_active() -> bool:
    """True only when the owner explicitly enabled the JEV decision role.

    Requires BOTH the mode flag and the provider opt-in, so a stale
    ``EV_COGNITIVE_MODE=jev_kernel`` can never silently rewire reasoning
    while the JEV provider itself is disabled.
    """

    return cognitive_mode() == JEV_KERNEL and bool(
        getattr(settings, "jev_enabled", False)
    )


def cognitive_role() -> str:
    raw = (getattr(settings, "cognitive_role", None) or "auto").strip().lower()
    if raw in {"kernel", "voice_edge"}:
        return raw
    # Talk sidecar always enables laptop files; production :8000 does not.
    if kernel_mode_active() and bool(getattr(settings, "laptop_files", False)):
        return "voice_edge"
    return "kernel" if kernel_mode_active() else "legacy"


def is_voice_edge() -> bool:
    return kernel_mode_active() and cognitive_role() == "voice_edge"


def is_kernel_process() -> bool:
    return kernel_mode_active() and not is_voice_edge()


def kernel_url() -> str:
    return (getattr(settings, "cognitive_kernel_url", None) or "http://127.0.0.1:8000").rstrip("/")


def mac_execute_url() -> str:
    return (
        getattr(settings, "cognitive_mac_execute_url", None) or "http://127.0.0.1:18000"
    ).rstrip("/")
