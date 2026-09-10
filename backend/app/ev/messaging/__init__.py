"""Generalized outbound messaging: channels, recipients, routing.

Approval and WhatsApp Web providers import that layer lazily from their
submodules (``app.ev.messaging.approval``, ``app.ev.messaging.whatsapp_web``)
so this package stays a light, cycle-free import surface.
"""

from app.ev.messaging.channels import (
    CHANNELS,
    ChannelSpec,
    channel_aliases,
    channel_label,
    channel_spec,
    detect_channel,
    normalize_channel,
    spoken_channel,
    unknown_channel,
)
from app.ev.messaging.recipients import (
    RecipientMatch,
    ambiguous_recipient_spoken,
    choose_handle,
    match_recipient,
    name_tokens,
    score_person_name,
    verify_peer,
)
from app.ev.messaging.routing import (
    DEFAULT_CHANNEL,
    ChannelRouting,
    route_channel,
)

__all__ = [
    "CHANNELS",
    "ChannelRouting",
    "ChannelSpec",
    "DEFAULT_CHANNEL",
    "RecipientMatch",
    "ambiguous_recipient_spoken",
    "channel_aliases",
    "channel_label",
    "channel_spec",
    "choose_handle",
    "detect_channel",
    "match_recipient",
    "name_tokens",
    "normalize_channel",
    "route_channel",
    "score_person_name",
    "spoken_channel",
    "unknown_channel",
    "verify_peer",
]
