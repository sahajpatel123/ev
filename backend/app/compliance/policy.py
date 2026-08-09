"""Regional compliance policy: retention, residency, and remote-processing gates.

Policies are configuration-driven (``EV_*`` environment variables) rather than
hard-coded per call site, so the same binaries enforce GDPR/EU, Illinois BIPA,
and general residency rules by deploying different configuration. Every
function reads configuration at call time so tests can override behavior
without a process restart.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from app.utils.text import utcnow

# Data categories with distinct legal treatment.
VOICEPRINT = "voiceprint"
TRAINING_SNAPSHOT = "training_snapshot"
LIVE_AUDIO = "live_audio"
ACCESS_LOG = "access_log"
EVENT = "event"
INTEGRATION_CACHE = "integration_cache"

CATEGORIES = (VOICEPRINT, TRAINING_SNAPSHOT, LIVE_AUDIO, ACCESS_LOG, EVENT, INTEGRATION_CACHE)

TRACKS = (
    "voice_enrollment",
    "training_corpus",
    "life_data_personalization",
    "adapter_fine_tuning",
    "filter_self_improvement",
)

REGIONS = ("eu", "uk", "us", "us-il", "in", "global")

# Default retention windows in days. -1 keeps data until the user deletes it;
# 0 means destroy as soon as the purpose ends (e.g. revocation).
_DEFAULT_RETENTION_DAYS: dict[str, dict[str, int]] = {
    "global": {
        VOICEPRINT: -1,
        TRAINING_SNAPSHOT: -1,
        LIVE_AUDIO: 0,
        ACCESS_LOG: 730,
        EVENT: -1,
        INTEGRATION_CACHE: 0,
    },
    "eu": {
        VOICEPRINT: 0,
        TRAINING_SNAPSHOT: 0,
        LIVE_AUDIO: 0,
        ACCESS_LOG: 365,
        EVENT: -1,
        INTEGRATION_CACHE: 0,
    },
    "uk": {
        VOICEPRINT: 0,
        TRAINING_SNAPSHOT: 0,
        LIVE_AUDIO: 0,
        ACCESS_LOG: 365,
        EVENT: -1,
        INTEGRATION_CACHE: 0,
    },
    "us": {
        VOICEPRINT: 0,
        TRAINING_SNAPSHOT: 0,
        LIVE_AUDIO: 0,
        ACCESS_LOG: 730,
        EVENT: -1,
        INTEGRATION_CACHE: 0,
    },
    "us-il": {
        VOICEPRINT: 0,
        TRAINING_SNAPSHOT: 0,
        LIVE_AUDIO: 0,
        ACCESS_LOG: 730,
        EVENT: -1,
        INTEGRATION_CACHE: 0,
    },
    "in": {
        VOICEPRINT: 0,
        TRAINING_SNAPSHOT: 0,
        LIVE_AUDIO: 0,
        ACCESS_LOG: 365,
        EVENT: -1,
        INTEGRATION_CACHE: 0,
    },
}

_ENV_RETENTION = {
    VOICEPRINT: "EV_RETENTION_VOICEPRINT_DAYS",
    TRAINING_SNAPSHOT: "EV_RETENTION_TRAINING_SNAPSHOT_DAYS",
    LIVE_AUDIO: "EV_RETENTION_LIVE_AUDIO_DAYS",
    ACCESS_LOG: "EV_RETENTION_ACCESS_LOG_DAYS",
    EVENT: "EV_RETENTION_EVENT_DAYS",
    INTEGRATION_CACHE: "EV_RETENTION_INTEGRATION_CACHE_DAYS",
}

_REMOTE_PROCESSING_ENV = {
    "voice_enrollment": "EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING",
    "training_corpus": "EV_ALLOW_REMOTE_TRAINING",
    "life_data_personalization": "EV_ALLOW_REMOTE_LIFE_DATA",
    "adapter_fine_tuning": "EV_ALLOW_REMOTE_TRAINING",
    "filter_self_improvement": "EV_ALLOW_REMOTE_FILTER_TRAINING",
}

_DISCLOSURES = {
    "eu": [
        "GDPR Art. 13/14 privacy notice",
        "Rights of access, rectification, erasure, restriction, and portability",
        "Purpose limitation and data minimization",
    ],
    "uk": [
        "UK GDPR privacy notice",
        "Right of erasure under UK GDPR Art. 17",
    ],
    "us": [
        "State biometric notice where applicable",
        "Retention and destruction schedule",
    ],
    "us-il": [
        "Illinois BIPA notice and written consent",
        "Retention/destruction schedule required by 740 ILCS 14/15",
        "No collection of biometric identifiers without a release",
    ],
    "in": [
        "DPDP Act notice and consent",
        "Right to erasure under Section 12",
    ],
    "global": [
        "General privacy notice",
        "User-controlled retention and deletion",
    ],
}


def region() -> str:
    value = os.getenv("EV_REGION", "global")
    return value.strip().lower() or "global"


def retention_days(category: str) -> int:
    if category not in CATEGORIES:
        raise ValueError(f"Unknown retention category: {category}")
    override = os.getenv(_ENV_RETENTION[category])
    if override is not None:
        try:
            return int(override)
        except ValueError as exc:
            raise ValueError(f"{_ENV_RETENTION[category]} must be an integer") from exc
    defaults = _DEFAULT_RETENTION_DAYS.get(region(), _DEFAULT_RETENTION_DAYS["global"])
    return defaults[category]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


def deletion_due(category: str, reference: datetime, *, now: datetime | None = None) -> bool:
    """True when the retention window for *reference* has expired."""
    days = retention_days(category)
    if days < 0:
        return False
    reference = _aware(reference)
    now = _aware(now or utcnow())
    return now >= reference + timedelta(days=days)


def remote_processing_allowed(track: str) -> bool:
    """Fail closed: remote processing is denied unless explicitly enabled."""
    if track not in TRACKS:
        raise ValueError(f"Unknown training track: {track}")
    raw = os.getenv(_REMOTE_PROCESSING_ENV[track])
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def residency_mode() -> str:
    value = os.getenv("EV_RESIDENCY_MODE", "local")
    return value.strip().lower() or "local"


def local_residency_required() -> bool:
    return residency_mode() == "local"


def disclosures() -> list[str]:
    return list(_DISCLOSURES.get(region(), _DISCLOSURES["global"]))


def policy_summary() -> dict:
    return {
        "region": region(),
        "residency_mode": residency_mode(),
        "local_residency_required": local_residency_required(),
        "retention_days": {category: retention_days(category) for category in CATEGORIES},
        "remote_processing": {track: remote_processing_allowed(track) for track in TRACKS},
        "disclosures": disclosures(),
    }
