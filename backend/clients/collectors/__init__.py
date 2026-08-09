"""Privacy-preserving OS-level collectors for EV's perception layer.

Collectors emit only derived, minimal representations (active app, document
title, coarse place/presence, audio-scene hints) over the permissioned live
channel contract.  Raw screen content, audio samples, and exact coordinates
are never collected or transmitted by these agents.
"""

from clients.collectors.agent import collect_once, run_agent

__all__ = ["collect_once", "run_agent"]
