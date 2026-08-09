# Identity & Trust Lifecycle

**Status:** implemented (hardening in progress)

## Owner identity record

`owner_identities` is the single authoritative "this is my owner" row. Devices,
voice enrollments, voice sessions, recovery codes, and passkeys all anchor to
`owner_id`, so a second identity is additive later without reworking checks.

- `POST /v1/identity/owner` (master key only) creates the owner and returns a
  one-time recovery code set.
- `GET /v1/identity/status` reports owner binding, trust level, active devices,
  remaining recovery codes, and active passkeys.

## Trust levels

`guest < device < owner < master`. A plain device can capture lightweight
context; owner-level operations (voice enrollment/export, device management,
identity management) require an owner-trusted device or the master key. The
master key is the recovery root and bypasses re-verification.

## Re-verification for sensitive actions

Even inside an unlocked voice session, sensitive actions require a fresh,
purpose-bound proof (`identity_reverifications`):

- `POST /v1/identity/reverification` issues a 5-minute, single-use proof bound
  to the device and purpose (`memory.delete`, `voice.revoke`, `voice.delete`,
  `recovery.rotate`).
- `DELETE /v1/events/{id}` requires a `memory.delete` proof for device actors.
- Voice revoke/delete require matching proofs.
- Proofs are consumed once, expire, and are rejected from a different device.

## Recovery

Recovery codes are stored only as SHA-256 hashes, single-use, 30-day expiry,
with a brute-force lockout after 5 failures. `POST /v1/identity/recovery/redeem`
is deliberately unauthenticated (it is the path back in after every credential
is lost): a valid code revokes the prior fleet and issues a fresh owner-trusted
device token.

## Passkey binding

`identity_passkeys` anchors WebAuthn credential IDs (hashed at rest) to the
owner and optionally to a device. Register/list require owner trust; revocation
is master-only. Possession is proven by a future WebAuthn challenge-response;
the row provides the binding anchor.

## Session security

Voice sessions are bound to the device that woke them and to the owner record.
Verify, utterance, follow-up, status, and end reject any other device
(`session_device_mismatch`) or any device of a different owner
(`session_owner_mismatch`), so an unlocked session cannot be silently inherited.
