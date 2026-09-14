"""Speaker verification for consequential phone actions.

Texting or calling someone is an OUTWARD action on someone else's phone —
the owner's voice, not just a paired device token, must vouch for it. A
paired token proves the device is trusted; the voiceprint proves the
PERSON is present. Law:

- The verified state is a SHORT window (120 s), refreshed by a spoken
  challenge; it never outlives the device row and never rides on
  the raw audio (samples are never stored).
- No enrollment → verification honestly reports no_voiceprint_enrolled;
  the send is refused with that next step, never silently allowed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.utils.text import utcnow

SPEAKER_VERIFY_WINDOW = timedelta(seconds=120)


def speaker_verified(device: Any, *, now: Any = None) -> bool:
    profile = getattr(device, "endpoint_profile", None)
    raw = profile.get("speaker_verified_at") if isinstance(profile, dict) else None
    if not raw:
        return False
    try:
        from datetime import datetime

        verified_at = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False
    current = now or utcnow()
    if verified_at.tzinfo is None and current.tzinfo is not None:
        from datetime import timezone

        verified_at = verified_at.replace(tzinfo=timezone.utc)
    return (current - verified_at) <= SPEAKER_VERIFY_WINDOW


def mark_speaker_verified(device: Any, *, now: Any = None) -> None:
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    profile["speaker_verified_at"] = (now or utcnow()).isoformat()
    device.endpoint_profile = profile
