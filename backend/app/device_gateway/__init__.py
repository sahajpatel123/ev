"""Cross-platform Device Gateway: one Evie, many devices, sandboxed memory."""

from __future__ import annotations

PROTOCOL_VERSION = "1"
PWA_BUILD = "2026.09.02.03"
SANDBOX_NAMESPACE = "cross_platform_test"
OWNER_KEY = "owner"

ROLES = (
    "home_station",
    "primary_companion",
    "secondary_companion",
    "companion",
)
PAIRABLE_ROLES = (
    "primary_companion",
    "secondary_companion",
    "companion",
)
