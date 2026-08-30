"""Evie OS G2 — Evie Everywhere.

ONE EVIE · MANY TRUSTED DEVICES.

This package is infrastructure, not a second brain. It gives every trusted
endpoint (Mac, primary phone, secondary phone, future agents) the same
canonical Evie state through:

- owner:      the ONE canonical owner scope for all life state
- sync:       snapshot + event-cursor delta over the canonical Event history
- devices:    device roster / presence / health projection
- capabilities: device-aware capability universe + CapabilityRouter
- continuity: logical conversation-thread continuation across devices

Laws:
- Devices may cache / present / request / act. They never invent truth.
- Canonical authority stays in Postgres via the existing G1 services.
- No parallel mobile stores, no duplicated business logic.
"""
