"""Evie Mobile Actions: intent-driven iPhone capabilities.

Product path:

  Realtime → phone_action → Core/POL → confirmation → ActionRequest
  → Native Capability Broker → iOS → NativeReceipt → speech

The Shortcuts Bridge remains as regression reference. It is not the
intended actuator. Native shell + typed broker is the body.
"""

from __future__ import annotations

BRIDGE_NAME = "Evie Mobile Bridge"
BRIDGE_PROTOCOL = 1
BRIDGE_VERSION = "1.0.0"
NATIVE_BROKER_VERSION = "1.0.0"
ACTION_TTL_S = 90.0
RESOLVE_RETRY_S = 8.0
NATIVE_SHELL = "SCAFFOLDED"
