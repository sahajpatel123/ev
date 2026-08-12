"""WebAuthn challenge-response ceremony tests (AGENT 19 VAULT).

The fake authenticator produces *real* cryptographic structures: ECDSA
P-256 credential keys, CBOR attestation objects, valid signatures over
authData||clientDataHash, and advancing signature counters. Nothing here is
mocked at the crypto layer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.identity import webauthn
from app.main import app
from app.models import PasskeyAuthMaterial, PasskeyCredential

ORIGIN = "http://test"
RP_ID = "localhost"


def b64url(raw: bytes) -> str:
    return webauthn.b64url_encode(raw)


def _int_bytes(value: int, length: int) -> bytes:
    return value.to_bytes(length, "big")


def _make_cert(
    *,
    subject_key,
    issuer_key,
    issuer_name: x509.Name,
    subject_name: x509.Name,
    ca: bool = False,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject_name)
        .issuer_name(issuer_name)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


class FakeAuthenticator:
    """A deterministic WebAuthn authenticator backed by real ECDSA keys."""

    def __init__(self, rp_id: str = RP_ID, origin: str = ORIGIN) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.credential_key = ec.generate_private_key(ec.SECP256R1())
        self.attestation_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = b"fake-credential-id-0001"
        self.aaguid = bytes(range(16))
        self.sign_count = 1

    def cose_key(self) -> dict:
        numbers = self.credential_key.public_key().public_numbers()
        return {
            1: 2,  # EC2
            3: -7,  # ES256
            -1: 1,  # P-256
            -2: _int_bytes(numbers.x, 32),
            -3: _int_bytes(numbers.y, 32),
        }

    def client_data(self, challenge_b64url: str, *, typ: str) -> bytes:
        payload = {
            "type": typ,
            "challenge": challenge_b64url,
            "origin": self.origin,
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def auth_data(self, *, attested: bool) -> bytes:
        rp_hash = webauthn.sha256_raw(self.rp_id.encode("utf-8"))
        flags = webauthn.FLAG_UP | (webauthn.FLAG_AT if attested else 0)
        out = rp_hash + bytes([flags]) + self.sign_count.to_bytes(4, "big")
        if attested:
            out += self.aaguid
            out += len(self.credential_id).to_bytes(2, "big")
            out += self.credential_id
            out += webauthn.cbor_encode(self.cose_key())
        return out

    def attestation_object(
        self,
        challenge_b64url: str,
        *,
        fmt: str = "packed",
        x5c: list[bytes] | None = None,
        sign_with_credential: bool = False,
        empty_att_stmt: bool = False,
    ) -> bytes:
        auth_data = self.auth_data(attested=True)
        client_data_hash = webauthn.sha256_raw(
            self.client_data(challenge_b64url, typ="webauthn.create")
        )
        signed = auth_data + client_data_hash
        signer = self.credential_key if sign_with_credential else self.attestation_key
        sig = signer.sign(signed, ec.ECDSA(hashes.SHA256()))
        att_stmt: dict = {} if empty_att_stmt else {"alg": -7, "sig": sig}
        if x5c is not None:
            att_stmt["x5c"] = x5c
        return webauthn.cbor_encode(
            {"fmt": fmt, "attStmt": att_stmt, "authData": auth_data}
        )

    def authentication_response(
        self,
        challenge_b64url: str,
        *,
        sign_count: int | None = None,
    ) -> tuple[bytes, bytes, bytes]:
        if sign_count is not None:
            self.sign_count = sign_count
        auth_data = self.auth_data(attested=False)
        client_data = self.client_data(challenge_b64url, typ="webauthn.get")
        signed = auth_data + webauthn.sha256_raw(client_data)
        signature = self.credential_key.sign(signed, ec.ECDSA(hashes.SHA256()))
        return client_data, auth_data, signature


def _client(headers: dict | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers or {},
    )


async def _create_owner(client: httpx.AsyncClient) -> dict:
    resp = await client.post("/v1/identity/owner", json={"display_name": "Sahaj"})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def webauthn_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "webauthn_origins", [ORIGIN])
    monkeypatch.setattr(settings, "webauthn_require_attestation", False)
    monkeypatch.setattr(settings, "webauthn_attestation_trust_roots_pem", [])


async def test_webauthn_registration_and_authentication_end_to_end(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    webauthn_defaults,
) -> None:
    await _create_owner(client)
    authenticator = FakeAuthenticator()

    options = await client.post("/v1/identity/webauthn/register/options")
    assert options.status_code == 200, options.text
    options_body = options.json()
    assert options_body["rp"]["id"] == RP_ID
    assert options_body["attestation"] == "none"

    client_data = authenticator.client_data(
        options_body["challenge"], typ="webauthn.create"
    )
    attestation = authenticator.attestation_object(
        options_body["challenge"], fmt="none", empty_att_stmt=True
    )
    registered = await client.post(
        "/v1/identity/webauthn/register/verify",
        json={
            "challenge_id": str(options_body["challenge_id"]),
            "credential_id": b64url(authenticator.credential_id),
            "client_data_json": b64url(client_data),
            "attestation_object": b64url(attestation),
            "name": "test passkey",
        },
    )
    assert registered.status_code == 201, registered.text
    passkey_id = registered.json()["passkey"]["id"]

    row = await db_session.get(PasskeyCredential, UUID(passkey_id))
    assert row is not None
    material = (
        await db_session.execute(
            select(PasskeyAuthMaterial).where(
                PasskeyAuthMaterial.passkey_id == row.id
            )
        )
    ).scalar_one()
    assert material.attestation_format == "none"
    assert material.attestation_verified is False
    assert material.sign_count == 1

    # Authentication ceremony, unauthenticated caller.
    anon = _client()
    auth_options = await anon.post("/v1/identity/webauthn/auth/options")
    assert auth_options.status_code == 200, auth_options.text
    auth_body = auth_options.json()
    assert auth_body["rp_id"] == RP_ID

    authenticator.sign_count = 2
    client_data, auth_data, signature = authenticator.authentication_response(
        auth_body["challenge"]
    )
    verified = await anon.post(
        "/v1/identity/webauthn/auth/verify",
        json={
            "challenge_id": str(auth_body["challenge_id"]),
            "credential_id": b64url(authenticator.credential_id),
            "client_data_json": b64url(client_data),
            "authenticator_data": b64url(auth_data),
            "signature": b64url(signature),
            "device_name": "passkey mac",
        },
    )
    assert verified.status_code == 200, verified.text
    auth_result = verified.json()
    assert auth_result["verified"] is True
    assert auth_result["trust_level"] == "owner"
    assert auth_result["device"]["owner_id"] is not None
    token = auth_result["token"]

    # The fresh device token is a working owner-trusted credential.
    status = await _client({"Authorization": f"Bearer {token}"}).get(
        "/v1/identity/status"
    )
    assert status.status_code == 200
    assert status.json()["trust_level"] == "owner"

    # Challenge is single-use.
    replayed = await anon.post(
        "/v1/identity/webauthn/auth/verify",
        json={
            "challenge_id": str(auth_body["challenge_id"]),
            "credential_id": b64url(authenticator.credential_id),
            "client_data_json": b64url(client_data),
            "authenticator_data": b64url(auth_data),
            "signature": b64url(signature),
            "device_name": "passkey mac",
        },
    )
    assert replayed.status_code == 403
    assert replayed.headers.get("X-Error-Code") == "webauthn_challenge_invalid"

    # Signature counter replay: a new challenge with a non-advancing counter.
    auth_options2 = await anon.post("/v1/identity/webauthn/auth/options")
    auth_body2 = auth_options2.json()
    client_data2, auth_data2, signature2 = authenticator.authentication_response(
        auth_body2["challenge"], sign_count=2
    )
    replay = await anon.post(
        "/v1/identity/webauthn/auth/verify",
        json={
            "challenge_id": str(auth_body2["challenge_id"]),
            "credential_id": b64url(authenticator.credential_id),
            "client_data_json": b64url(client_data2),
            "authenticator_data": b64url(auth_data2),
            "signature": b64url(signature2),
            "device_name": "passkey mac",
        },
    )
    assert replay.status_code == 401
    assert replay.headers.get("X-Error-Code") == "webauthn_replay"


async def test_webauthn_registration_rejects_tampered_challenge_and_origin(
    client: httpx.AsyncClient,
    webauthn_defaults,
) -> None:
    await _create_owner(client)
    authenticator = FakeAuthenticator()
    options = (await client.post("/v1/identity/webauthn/register/options")).json()

    # Wrong challenge inside clientDataJSON.
    bad_client = json.dumps(
        {
            "type": "webauthn.create",
            "challenge": b64url(b"attacker-challenge"),
            "origin": ORIGIN,
        },
        separators=(",", ":"),
    ).encode()
    attestation = authenticator.attestation_object(
        options["challenge"], fmt="none", empty_att_stmt=True
    )
    resp = await client.post(
        "/v1/identity/webauthn/register/verify",
        json={
            "challenge_id": str(options["challenge_id"]),
            "credential_id": b64url(authenticator.credential_id),
            "client_data_json": b64url(bad_client),
            "attestation_object": b64url(attestation),
            "name": "bad challenge",
        },
    )
    assert resp.status_code == 422
    assert resp.headers.get("X-Error-Code") == "webauthn_challenge"

    # Wrong origin.
    options2 = (await client.post("/v1/identity/webauthn/register/options")).json()
    bad_origin = FakeAuthenticator(origin="https://evil.example")
    client_data = bad_origin.client_data(options2["challenge"], typ="webauthn.create")
    attestation2 = bad_origin.attestation_object(
        options2["challenge"], fmt="none", empty_att_stmt=True
    )
    resp = await client.post(
        "/v1/identity/webauthn/register/verify",
        json={
            "challenge_id": str(options2["challenge_id"]),
            "credential_id": b64url(bad_origin.credential_id),
            "client_data_json": b64url(client_data),
            "attestation_object": b64url(attestation2),
            "name": "bad origin",
        },
    )
    assert resp.status_code == 422
    assert resp.headers.get("X-Error-Code") == "webauthn_origin"


async def test_webauthn_packed_attestation_with_trusted_root(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "webauthn_origins", [ORIGIN])
    monkeypatch.setattr(settings, "webauthn_require_attestation", True)
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EV Test Root")])
    root_cert = _make_cert(
        subject_key=root_key,
        issuer_key=root_key,
        issuer_name=root_name,
        subject_name=root_name,
        ca=True,
    )
    monkeypatch.setattr(
        settings, "webauthn_attestation_trust_roots_pem", [_pem(root_cert)]
    )

    await _create_owner(client)
    authenticator = FakeAuthenticator()
    leaf_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "EV Test Attestation")]
    )
    leaf_cert = _make_cert(
        subject_key=authenticator.attestation_key,
        issuer_key=root_key,
        issuer_name=root_name,
        subject_name=leaf_name,
    )
    options = (await client.post("/v1/identity/webauthn/register/options")).json()
    assert options["attestation"] == "direct"
    attestation = authenticator.attestation_object(
        options["challenge"],
        fmt="packed",
        x5c=[leaf_cert.public_bytes(serialization.Encoding.DER)],
    )
    client_data = authenticator.client_data(
        options["challenge"], typ="webauthn.create"
    )
    resp = await client.post(
        "/v1/identity/webauthn/register/verify",
        json={
            "challenge_id": str(options["challenge_id"]),
            "credential_id": b64url(authenticator.credential_id),
            "client_data_json": b64url(client_data),
            "attestation_object": b64url(attestation),
            "name": "trusted passkey",
        },
    )
    assert resp.status_code == 201, resp.text
    row = await db_session_get_passkey(resp.json()["passkey"]["id"])
    material = await client_db_material(row)
    assert material.attestation_format == "packed"
    assert material.attestation_verified is True
    assert material.attestation_level == "basic"


async def test_webauthn_attestation_required_rejects_untrusted_and_none(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "webauthn_origins", [ORIGIN])
    monkeypatch.setattr(settings, "webauthn_require_attestation", True)
    monkeypatch.setattr(settings, "webauthn_attestation_trust_roots_pem", [])
    await _create_owner(client)
    authenticator = FakeAuthenticator()

    options = (await client.post("/v1/identity/webauthn/register/options")).json()
    attestation = authenticator.attestation_object(
        options["challenge"], fmt="none", empty_att_stmt=True
    )
    client_data = authenticator.client_data(
        options["challenge"], typ="webauthn.create"
    )
    resp = await client.post(
        "/v1/identity/webauthn/register/verify",
        json={
            "challenge_id": str(options["challenge_id"]),
            "credential_id": b64url(authenticator.credential_id),
            "client_data_json": b64url(client_data),
            "attestation_object": b64url(attestation),
            "name": "none attestation",
        },
    )
    assert resp.status_code == 422
    assert resp.headers.get("X-Error-Code") == "webauthn_attestation_required"

    # A packed statement carrying an x5c certificate that does not chain to a
    # configured trust root fails closed when attestation is required.
    options2 = (await client.post("/v1/identity/webauthn/register/options")).json()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EV Untrusted Leaf")])
    leaf_cert = _make_cert(
        subject_key=authenticator.attestation_key,
        issuer_key=authenticator.attestation_key,
        issuer_name=name,
        subject_name=name,
    )
    attestation2 = authenticator.attestation_object(
        options2["challenge"],
        fmt="packed",
        x5c=[leaf_cert.public_bytes(serialization.Encoding.DER)],
    )
    client_data2 = authenticator.client_data(
        options2["challenge"], typ="webauthn.create"
    )
    resp2 = await client.post(
        "/v1/identity/webauthn/register/verify",
        json={
            "challenge_id": str(options2["challenge_id"]),
            "credential_id": b64url(authenticator.credential_id),
            "client_data_json": b64url(client_data2),
            "attestation_object": b64url(attestation2),
            "name": "untrusted packed",
        },
    )
    assert resp2.status_code == 422
    assert resp2.headers.get("X-Error-Code") == "webauthn_attestation_required"


async def test_webauthn_accepts_self_attestation_without_required_mode(
    client: httpx.AsyncClient,
    webauthn_defaults,
) -> None:
    await _create_owner(client)
    authenticator = FakeAuthenticator()
    options = (await client.post("/v1/identity/webauthn/register/options")).json()
    attestation = authenticator.attestation_object(
        options["challenge"], fmt="packed", sign_with_credential=True
    )
    client_data = authenticator.client_data(
        options["challenge"], typ="webauthn.create"
    )
    resp = await client.post(
        "/v1/identity/webauthn/register/verify",
        json={
            "challenge_id": str(options["challenge_id"]),
            "credential_id": b64url(authenticator.credential_id),
            "client_data_json": b64url(client_data),
            "attestation_object": b64url(attestation),
            "name": "self attested",
        },
    )
    assert resp.status_code == 201, resp.text
    row = await db_session_get_passkey(resp.json()["passkey"]["id"])
    material = await client_db_material(row)
    assert material.attestation_format == "packed"
    assert material.attestation_verified is True
    assert material.attestation_level == "self"


async def test_webauthn_fido_u2f_attestation_verifies(
    client: httpx.AsyncClient,
    webauthn_defaults,
) -> None:
    await _create_owner(client)
    authenticator = FakeAuthenticator()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EV U2F Token")])
    cert = _make_cert(
        subject_key=authenticator.credential_key,
        issuer_key=authenticator.credential_key,
        issuer_name=name,
        subject_name=name,
    )
    options = (await client.post("/v1/identity/webauthn/register/options")).json()
    auth_data = authenticator.auth_data(attested=True)
    client_data = authenticator.client_data(
        options["challenge"], typ="webauthn.create"
    )
    client_data_hash = webauthn.sha256_raw(client_data)
    cose = authenticator.cose_key()
    u2f_key = b"\x04" + cose[-2] + cose[-3]
    signed = (
        b"\x00"
        + webauthn.sha256_raw(RP_ID.encode())
        + client_data_hash
        + authenticator.credential_id
        + u2f_key
    )
    sig = authenticator.credential_key.sign(signed, ec.ECDSA(hashes.SHA256()))
    attestation = webauthn.cbor_encode(
        {
            "fmt": "fido-u2f",
            "attStmt": {
                "sig": sig,
                "x5c": [cert.public_bytes(serialization.Encoding.DER)],
            },
            "authData": auth_data,
        }
    )
    resp = await client.post(
        "/v1/identity/webauthn/register/verify",
        json={
            "challenge_id": str(options["challenge_id"]),
            "credential_id": b64url(authenticator.credential_id),
            "client_data_json": b64url(client_data),
            "attestation_object": b64url(attestation),
            "name": "u2f token",
        },
    )
    assert resp.status_code == 201, resp.text
    row = await db_session_get_passkey(resp.json()["passkey"]["id"])
    material = await client_db_material(row)
    assert material.attestation_format == "fido-u2f"
    assert material.attestation_level == "basic_untrusted_root"


async def test_webauthn_registration_rejects_credential_mismatch(
    client: httpx.AsyncClient,
    webauthn_defaults,
) -> None:
    await _create_owner(client)
    authenticator = FakeAuthenticator()
    options = (await client.post("/v1/identity/webauthn/register/options")).json()
    attestation = authenticator.attestation_object(
        options["challenge"], fmt="none", empty_att_stmt=True
    )
    client_data = authenticator.client_data(
        options["challenge"], typ="webauthn.create"
    )
    resp = await client.post(
        "/v1/identity/webauthn/register/verify",
        json={
            "challenge_id": str(options["challenge_id"]),
            "credential_id": b64url(b"some-other-credential-id"),
            "client_data_json": b64url(client_data),
            "attestation_object": b64url(attestation),
            "name": "mismatch",
        },
    )
    assert resp.status_code == 422
    assert resp.headers.get("X-Error-Code") == "webauthn_credential_mismatch"


# Small helpers that keep the tests above readable.
async def db_session_get_passkey(passkey_id: str):
    from app.db import SessionLocal

    async with SessionLocal() as session:
        return await session.get(PasskeyCredential, UUID(passkey_id))


async def client_db_material(row):
    from app.db import SessionLocal

    async with SessionLocal() as session:
        return (
            await session.execute(
                select(PasskeyAuthMaterial).where(
                    PasskeyAuthMaterial.passkey_id == row.id
                )
            )
        ).scalar_one()
