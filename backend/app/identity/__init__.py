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
    list_passkeys,
    redeem_recovery_code,
    register_passkey,
    require_owner,
    revoke_passkey,
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
    "list_passkeys",
    "redeem_recovery_code",
    "register_passkey",
    "require_owner",
    "revoke_passkey",
    "trust_allows",
]
