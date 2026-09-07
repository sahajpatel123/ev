"""Device arbitration groundwork (W4) — ONE device wins.

If Mac and iPhone hear the same "Evie" they must NOT both answer. Reuse
existing trusted-device / ConversationLease infrastructure.

Conceptually:
  local wake candidate → candidate score + device state → short arbitration
  → ONE device obtains conversation ownership. Other remains silent.

Prefer deterministic factors (not an LLM):
 - accepted wake confidence
 - device availability
 - conversation continuity (already has active lease)
 - active/nearby context (e.g. last_activity, presence)

This module provides the groundwork: a deterministic picker that consumes
candidates and the current lease, and returns the winner. The caller (ears
ingest or lease claimant) then claims the lease for that device.

MAC FIRST (§14): prove on Mac only before porting to iPhone while respecting
iOS audio session / background / battery / permission / lifecycle constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class WakeCandidate:
    device_id: str
    device_name: str | None = None
    confidence: float = 0.0
    # Device-state hints for deterministic picking; none are required.
    last_activity: datetime | None = None
    has_active_session: bool = False
    is_nearby: bool | None = None
    battery_percent: int | None = None


@dataclass(frozen=True)
class ArbitrationResult:
    winner_device_id: str
    winner_confidence: float
    reason: str
    diagnostics: dict


class WakeArbitration:
    """Deterministic single-winner picker (no LLM)."""

    def pick_winner(
        self,
        candidates: list[WakeCandidate],
        current_lease: dict | None = None,
    ) -> ArbitrationResult | None:
        """Return the ONE winner; None when no candidates.

        Priority (deterministic, consensus-free):
         1. Already-owning device with an active session (conversation continuity)
            wins if its candidate confidence is within 0.10 of the top.
         2. Highest accepted wake confidence.
         3. Tie-break by most recent last_activity, then lexicographic device_id.
        """
        if not candidates:
            return None

        # Sort by confidence desc, then recency, then id.
        def sort_key(c: WakeCandidate):
            ts = c.last_activity.timestamp() if c.last_activity else 0
            return (-c.confidence, -ts, c.device_id)

        ranked = sorted(candidates, key=sort_key)
        top = ranked[0]

        # Continuity bias: current lease holder keeps it if close enough.
        if current_lease:
            holder = str(current_lease.get("device_id") or "")
            holder_candidate = next(
                (c for c in candidates if c.device_id == holder), None
            )
            if (
                holder_candidate
                and holder_candidate.has_active_session
                and holder_candidate.confidence >= top.confidence - 0.10
            ):
                return ArbitrationResult(
                    winner_device_id=holder_candidate.device_id,
                    winner_confidence=holder_candidate.confidence,
                    reason="continuity_holder_within_margin",
                    diagnostics={
                        "candidates": len(candidates),
                        "top_confidence": top.confidence,
                        "holder_confidence": holder_candidate.confidence,
                    },
                )

        return ArbitrationResult(
            winner_device_id=top.device_id,
            winner_confidence=top.confidence,
            reason="highest_wake_confidence",
            diagnostics={
                "candidates": len(candidates),
                "top_confidence": top.confidence,
                "all_confidences": [
                    round(c.confidence, 4) for c in ranked[:5]
                ],
            },
        )

    async def claim_for_winner(
        self,
        session,
        *,
        winner: WakeCandidate,
        instance_id: str,
        method: str = "wake_arbitration",
    ):
        """Claim the ConversationLease for the winner (deferred import to avoid cycle)."""
        from app.device_gateway.lease import claim_lease

        # device_id may be a registry string (mac host) or UUID; normalize.
        try:
            device_uuid = UUID(str(winner.device_id))
        except ValueError:
            # Registry device name (e.g. mac-<host>) — resolve via fleet.
            from app.ev.fleet import resolve_registry_device

            resolved = await resolve_registry_device(session, str(winner.device_id))
            if resolved is None:
                raise ValueError(
                    f"cannot resolve device {winner.device_id!r} for lease claim"
                ) from None
            device_uuid = resolved.id  # type: ignore[assignment]
        return await claim_lease(
            session,
            device_id=device_uuid,
            instance_id=instance_id,
            method=method,
        )
