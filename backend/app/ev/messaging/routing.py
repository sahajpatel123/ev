"""Channel routing: which transport carries a send, and whether it can.

An explicitly requested channel is honored or refused — never silently
downgraded to a different one. "Send WhatsApp to Sam" must open WhatsApp or
say so; it must never become an SMS just because the default is Messages.

The routing decision is pure (settings in, plan out) so every send path —
tool loop, integrations adapter, device queue — can share it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.ev.messaging.channels import (
    AddressKind,
    ChannelSpec,
    channel_label,
    channel_spec,
    normalize_channel,
)

RouteMode = Literal["send", "compose", "queue", "unavailable"]
RouteProvider = Literal["macos_life", "device_proxy", "web", "none"]

DEFAULT_CHANNEL = "messages"


@dataclass(frozen=True)
class ChannelRouting:
    channel: str
    mode: RouteMode
    provider: RouteProvider
    helper_command: str | None
    address: AddressKind
    service: str | None
    requires_approval: bool = False
    spoken: str = ""

    @property
    def can_execute(self) -> bool:
        return self.mode in {"send", "compose"}

    @property
    def delivers_without_tap(self) -> bool:
        return self.mode == "send"


def _unavailable(channel: str, requested: str | None) -> ChannelRouting:
    label = channel_label(channel)
    if requested and channel_spec(requested) is None:
        label = requested.strip().title() if requested.strip() else label
    spoken = (
        f"I don't have {label} connected yet. I can do WhatsApp, Messages, "
        "or email on this Mac."
    )
    return ChannelRouting(
        channel=channel,
        mode="unavailable",
        provider="none",
        helper_command=None,
        address="handle",
        service=None,
        spoken=spoken,
    )


def route_channel(
    channel: str | None,
    *,
    helper_available: bool,
    device_proxy: bool = False,
    web_available: bool = False,
) -> ChannelRouting:
    """Resolve a channel id/alias to an executable route.

    ``helper_available`` means EVLifeHelper (or its live daemon) can run.
    ``device_proxy`` means the iPhone actuator queue is the transport.
    ``web_available`` means an authenticated WhatsApp Web tab exists; the
    WhatsApp route then autosends through it, but only behind human
    approval (``requires_approval``).
    """

    requested = (channel or "").strip() or None
    if requested is None:
        canonical = DEFAULT_CHANNEL
    else:
        normalized = normalize_channel(requested)
        if normalized is None:
            # A named channel that is not registered is refused, never
            # remapped onto the default transport.
            return _unavailable(requested, requested)
        canonical = normalized
    spec: ChannelSpec | None = channel_spec(canonical)
    if spec is None or not spec.wired:
        return _unavailable(canonical, requested)

    if device_proxy:
        return ChannelRouting(
            channel=spec.id,
            mode="queue",
            provider="device_proxy",
            helper_command=spec.helper_command,
            address=spec.address,
            service=spec.service,
            spoken="",
        )
    if spec.id == "whatsapp" and web_available:
        return ChannelRouting(
            channel=spec.id,
            mode="send",
            provider="web",
            helper_command=spec.helper_command,
            address=spec.address,
            service=None,
            requires_approval=True,
            spoken="",
        )
    if not helper_available:
        spoken = (
            f"I can't reach the {channel_label(spec.id)} bridge on this Mac "
            "right now. Check the EVLifeHelper permissions and try again."
        )
        return ChannelRouting(
            channel=spec.id,
            mode="unavailable",
            provider="none",
            helper_command=spec.helper_command,
            address=spec.address,
            service=spec.service,
            spoken=spoken,
        )
    mode: RouteMode = "send" if spec.autosend else "compose"
    return ChannelRouting(
        channel=spec.id,
        mode=mode,
        provider="macos_life",
        helper_command=spec.helper_command,
        address=spec.address,
        service=spec.service,
        spoken="",
    )
