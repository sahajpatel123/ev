"""Channel routing: which transport carries a send, and whether it can.

An explicitly requested channel is honored or refused — never silently
downgraded to a different one. "Send WhatsApp to Sam" must open WhatsApp or
say so; it must never become an SMS just because the default is Messages.

The routing decision is pure (settings in, plan out) so every send path —
tool loop, integrations adapter, device queue — can share it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

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


_MODES: frozenset[str] = frozenset({"send", "compose", "queue", "unavailable"})
_PROVIDERS: frozenset[str] = frozenset({"macos_life", "device_proxy", "web", "none"})
_ADDRESSES: frozenset[str] = frozenset({"phone", "handle", "email"})


@dataclass(frozen=True)
class RouteBinding:
    """The route the owner approved, in a form that survives storage.

    A physical-world approval is only meaningful if execution uses the same
    transport the question was about. This value is what makes that check
    possible: it is written into the parked action at park time and compared
    against the freshly computed route at execution time, so a probe that
    flips (Chrome closed, helper gone) produces an honest failure instead of
    a silent switch to a different app or a different channel.
    """

    channel: str
    provider: RouteProvider
    mode: RouteMode
    address: AddressKind

    @classmethod
    def of(cls, routing: ChannelRouting) -> RouteBinding:
        return cls(
            channel=routing.channel,
            provider=routing.provider,
            mode=routing.mode,
            address=routing.address,
        )

    def satisfies(self, routing: ChannelRouting) -> str | None:
        """``None`` when ``routing`` honors this binding, else the broken part."""

        if self.channel != routing.channel:
            return "channel"
        if self.provider != routing.provider:
            return "provider"
        return None

    def as_payload(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "provider": self.provider,
            "mode": self.mode,
            "address": self.address,
        }

    @classmethod
    def from_payload(cls, raw: object) -> RouteBinding | None:
        """Parse a stored binding; ``None`` when absent or unusable."""

        if not isinstance(raw, dict):
            return None
        channel = str(raw.get("channel") or "").strip()
        provider = str(raw.get("provider") or "").strip()
        if not channel or provider not in _PROVIDERS:
            return None
        mode = str(raw.get("mode") or "").strip()
        address = str(raw.get("address") or "").strip()
        return cls(
            channel=channel,
            provider=cast(RouteProvider, provider),
            mode=cast(RouteMode, mode if mode in _MODES else "send"),
            address=cast(AddressKind, address if address in _ADDRESSES else "handle"),
        )


def provider_label(provider: str | None) -> str:
    """Owner-facing name for a transport, used when a route is unavailable."""

    return {
        "web": "WhatsApp Web (the browser tab)",
        "macos_life": "the WhatsApp app on this Mac",
        "device_proxy": "your phone",
        "none": "",
    }.get(str(provider or ""), "")


def route_unavailable_spoken(binding: RouteBinding, routing: ChannelRouting) -> str:
    """Honest sentence for “the transport I approved is gone”."""

    label = channel_label(binding.channel)
    if binding.provider == "web" and routing.provider != "web":
        return (
            f"I didn't send it: {label} Web isn't open and signed in on this Mac "
            "right now. Open WhatsApp Web in Chrome and tell me to send it again."
        )
    approved = provider_label(binding.provider) or label
    return (
        f"I didn't send it: the {approved} route I used to prepare that "
        f"{label} message isn't available any more. Tell me to try again."
    )



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
