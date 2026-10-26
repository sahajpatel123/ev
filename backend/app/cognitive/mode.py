"""Cognitive mode flags — two-model workspace.

Three selectable topologies over two models (Gemini Live + Muse Spark):

- ``realtime_delegate``: Gemini Live fronts the conversation and assigns
  medium-high work to brain delegation workers via ``delegate_task``.
  The kernel does not preempt live turns; Gemini decides.
- ``mimo_kernel`` (and aliases): single-brain mouth topology — Gemini is
  VAD/ASR/TTS only and the brain kernel supplies spoken text.
  (``mimo_kernel`` is the retained topology name; the brain is Muse Spark.)
- legacy/default (``legacy_mini``, ``legacy_gemini``, ...): supervised
  Gemini Live with the direct EV tool surface; transcript brokers handle
  memory/computer/code turns and the kernel does not preempt live turns.

Non-speech model calls resolve through the gateway registry
(``EV_CHAT_PROVIDER``), so they reach Muse Spark — or an offline double —
in every topology.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from app.config import settings

MIMO_KERNEL = "mimo_kernel"
LEGACY = "legacy"
REALTIME_DELEGATE = "realtime_delegate"
_DELEGATE_TASK: ContextVar[str] = ContextVar("delegated_task_hint", default="")
_DELEGATE_BINDING: ContextVar[Any] = ContextVar("delegated_phone_binding", default=None)
_WORKER_MODE: ContextVar[bool] = ContextVar("delegated_brain_worker", default=False)


def realtime_delegate_active() -> bool:
    return cognitive_mode() == REALTIME_DELEGATE


realtime_delegate_mode_active = realtime_delegate_active


def delegated_worker_active() -> bool:
    return _WORKER_MODE.get()



def cognitive_mode() -> str:
    if _WORKER_MODE.get():
        return MIMO_KERNEL
    raw = (getattr(settings, "cognitive_mode", None) or LEGACY).strip().lower()
    if raw in {"realtime_delegate", "realtime_first"}:
        return REALTIME_DELEGATE
    if raw in {"mimo_kernel", "single", "single_brain"}:
        return MIMO_KERNEL
    return LEGACY


def kernel_mode_active() -> bool:
    """True when the single-brain brain kernel owns replies (mouth topology)."""

    return cognitive_mode() == MIMO_KERNEL


def mimo_kernel_active() -> bool:
    """Muse Spark owns replies in the mouth topology.

    The function and ``mimo_kernel`` topology names are retained for
    backwards compatibility; only the brain changed.
    """

    return cognitive_mode() == MIMO_KERNEL


def mouth_topology_selected() -> bool:
    """True only when the operator explicitly selected the mouth topology.

    Legacy/default values keep the supervised direct-tool surface; only an
    explicit mouth value makes Gemini VAD/ASR/TTS-only with the kernel
    supplying spoken text.
    """

    return cognitive_mode() == MIMO_KERNEL


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
