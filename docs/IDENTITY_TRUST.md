# Identity & Trust Lifecycle

**Status:** implemented; WebAuthn ceremony, automated recovery drill,
escalation matrix, and erasure completeness are covered by automated tests.

## Owner identity record

`owner_identities` is the single authoritative "this is my owner" row. Devices,
voice enrollments, voice sessions, recovery codes, passkeys, and WebAuthn
challenges all anchor to `owner_id`, so a second identity is additive later
without reworking checks.

- `POST /v1/identity/owner` (master key only) creates the owner and returns a
  one-time recovery code set.
- `GET /v1/identity/status` reports owner binding, trust level, active devices,
  remaining recovery codes, and active passkeys.

## Trust levels

`guest < device < owner < master`. A plain device can capture lightweight
context; owner-level operations (voice enrollment/export, device management,
identity management, face enrollment/revocation, adapter activation, fleet
writes, person deletion) require an owner-trusted device or the master key.
The master key is the recovery root and bypasses device re-verification.

## Re-verification for sensitive actions

Even inside an unlocked voice session, sensitive actions require a fresh,
purpose-bound proof (`identity_reverifications`):

- `POST /v1/identity/reverification` issues a 5-minute, single-use proof bound
  to the device and purpose. Issuance itself requires owner-level trust, so a
  plain device cannot mint proofs.
- Purpose-bound proofs are enforced by `require_reverification(purpose)` on
  memory delete, voice revoke/delete, integration actions, runtime actions,
  and sensitive voice actions.
- The full matrix (also exposed by `GET /v1/identity/trust`) declares these
  re-verification purposes: `memory.delete`, `memory.export`, `voice.revoke`,
  `voice.delete`, `voice.sensitive_action`, `face.revoke`, `face.delete`,
  `recovery.rotate`, `integration.action`, `runtime.action`, `vault.rotate`,
  `backup.restore`, `compliance.erasure`, `adapter.activate`,
  `adapter.delete`, `person.delete`, `fleet.write`.
- Memory export, vault rotation, backup restore, and compliance erasure are
  master-key-only surfaces today: a device token alone (even owner-trusted and
  inside an active session) is refused. The master key is the fresh factor.
  `adapter.activate`/`adapter.delete`, `person.delete`, `face.revoke`,
  `face.delete`, and `fleet.write` are declared in the matrix; their
  endpoint-level proof enforcement is a dependency note for Agents 11, 7, and
  12 respectively (see "Open dependency notes").

## WebAuthn passkeys (real ceremony)

Passkeys are no longer credential-ID hash binding only. The full ceremony is
implemented in `app/identity/webauthn.py`:

- **Registration:** `POST /v1/identity/webauthn/register/options` issues a
  single-use, 300-second challenge (only its SHA-256 is stored).
  `POST /v1/identity/webauthn/register/verify` parses the CBOR attestation
  object, checks `clientDataJSON` type/challenge/origin, verifies the RP-ID
  hash, and verifies the attestation signature for `none`, `packed`, and
  `fido-u2f`. With `EV_WEBAUTHN_REQUIRE_ATTESTATION=true`, registration
  additionally requires the attestation chain to terminate at a configured
  trust root (`EV_WEBAUTHN_ATTESTATION_TRUST_ROOTS_PEM`); untrusted or
  non-attested statements are rejected.
- **Authentication:** `POST /v1/identity/webauthn/auth/options` is deliberately
  unauthenticated (the challenge is the proof of possession).
  `POST /v1/identity/webauthn/auth/verify` checks challenge, origin, RP-ID
  hash, user-presence flag, and verifies the ECDSA/RSA/EdDSA signature over
  `authenticatorData || SHA-256(clientDataJSON)` with the stored COSE public
  key. The signature counter must advance; a replay or cloned-authenticator
  response is rejected with `webauthn_replay`. Success issues a fresh
  owner-trusted device token.
- The COSE public key, counter, AAGUID, attestation format, and attestation
  verification level live in `identity_passkey_materials`; the existing
  `identity_passkeys` binding rows and CLI are unchanged.

## Recovery drill (automated)

`test_recovery_drill_is_repeatable` runs the full drill as a test: enrolled
device lost → recovery code redeemed → voiceprint re-enrolled → old token
provably dead (401) → new token works including voice verification → the drill
repeats with the next code, and the previous post-recovery token is revoked in
turn. Recovery codes are stored only as SHA-256 hashes, single-use, 30-day
expiry, with a brute-force lockout after 5 failures. Redemption revokes the
entire device fleet.

## Erasure completeness

`POST /v1/compliance/erasure` destroys biometric and derived-personal data
across every covered table (the manifest's `covered_tables` enumerates them):
voice enrollments/voiceprints (ciphertext nulled), face enrollments (redacted,
ciphertext nulled), face samples and recognition sightings (deleted),
public-figure biodata cache (deleted), adapter registrations (eval metrics
destroyed), personalization calibrations (evidence/calibrations cleared),
filter recalibrations (metrics/proposals/policy cleared), training corpus
snapshots (entries/hash cleared), filter-ledger drafts, model-call envelopes,
voice events (tombstoned), attachments (physically deleted), and consent
records (all revoked). `test_erasure_completeness.py` seeds every table and
asserts the post-erasure state of each one, plus the audit manifest.

## Consent tracks & chat egress transparency

- `face_enrollment` is a consent track with its own regional remote-processing
  gate (`EV_ALLOW_REMOTE_FACE_PROCESSING`). Agent 7's face enrollment service
  calls `require_consent(session, "face_enrollment")`, so a template can never
  be written without a live consent record; the compliance layer also exposes
  BIPA/GDPR-grade disclosure text (`face_enrollment_disclosure()` in
  `app/compliance/policy.py`).
- Chat egress has a consent track (`chat_egress`) with gate
  `EV_ALLOW_REMOTE_CHAT` declared in policy. `GET /v1/compliance/transparency`
  reports the chat destination with its consent and gate state instead of
  `consent_track: None`; `GET /v1/compliance/transparency/summary` returns a
  plain-language report. Enforcing the gate at the chat call site is an open
  dependency note (gateway owner); the docs do not claim it is enforced.

## Session security & single-owner invariant

Voice sessions are bound to the device that woke them and to the owner record.
Verify, utterance, follow-up, status, and end reject any other device
(`session_device_mismatch`) or any device of a different owner
(`session_owner_mismatch`). An unenrolled or unknown speaker gets a polite
refusal: `owner_verified` stays false, the session ends, and no privileged
operation is possible (asserted by
`test_single_owner_invariant_unknown_voice_cannot_start_privileged_session`).

## Intelligence filter

Voice-originated turns flow through the input filter as
`SpeakerIdentity(method="voiceprint", confidence=<verified session score>)`
instead of a generic `auth_token` identity, so low-confidence owner
verification flags command/decision intents. Text/API turns keep `auth_token`
semantics.

## Threat model & residual risks

See `docs/SECURITY.md` §8c for the threat-model pass against the real topology
(localhost native services, Tailscale, TLS termination, launchd, filesystem
object store) and the accepted residual risks.

## Open dependency notes

- Agent 4 (VOICE): remote-voiceprint gate raises at verifier construction,
  which surfaces as an internal error instead of a graceful 403; the gate is
  fail-closed and tested, but the HTTP status should be `403`.
- Agent 7 (ROSTER): face enrollment consent is enforced. The shipped
  `DELETE /v1/people/{id}` person-deletion endpoint and the face
  revoke/delete endpoints use owner trust but not a purpose-bound proof; they
  should use `person.delete`, `face.revoke`, and `face.delete` respectively.
  Agent 7's `erase_all_face_biometrics` keeps user-confirmed sightings while
  compliance erasure deletes all sightings; the stronger behavior is the
  one tested for data-subject erasure.
- Agent 11 (FORGE): `POST /v1/training/adapter/activate` and consent
  grant/revoke currently accept any registered device; they should require
  `adapter.activate` (or owner/master) re-verification.
- Agent 12 (CONDUIT): `POST /v1/edith/fleet/tasks` currently lets a device
  dispatch tasks to itself; fleet writes should require the `fleet.write`
  re-verification purpose.
