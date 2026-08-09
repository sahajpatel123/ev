"""Identity & trust lifecycle: owner records, trust escalation, recovery, re-verification."""

from app.identity.service import (
    TRUST_DEVICE,
    TRUST_MASTER,
    TRUST_OWNER,
    IdentityError,
    consume_reverification,
    create_owner,
    device_trust,
    get_owner,
    issue_recovery_codes,
    issue_reverification,
    redeem_recovery_code,
    require_owner,
    trust_allows,
)

__all__ = [
    "TRUST_DEVICE",
    "TRUST_MASTER",
    "TRUST_OWNER",
    "IdentityError",
    "consume_reverification",
    "create_owner",
    "device_trust",
    "get_owner",
    "issue_recovery_codes",
    "issue_reverification",
    "redeem_recovery_code",
    "require_owner",
    "trust_allows",
]
