"""WebAuthn ceremony primitives: CBOR, COSE keys, attestation, signatures.

Implemented with the standard library plus ``cryptography``; no new
dependencies. Every check fails closed: a malformed structure, unknown
attestation format, mismatched challenge/origin/RP-ID, invalid signature, or
non-advancing signature counter raises :class:`WebauthnError`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

# Authenticator data flags (WebAuthn spec §6.1).
FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_AT = 0x40
FLAG_ED = 0x80

# COSE key types.
COSE_OKP = 1
COSE_EC2 = 2
COSE_RSA = 3

# Curves: 1=P-256, 2=P-384, 3=P-521.
_EC_CURVES = {
    1: ec.SECP256R1(),
    2: ec.SECP384R1(),
    3: ec.SECP521R1(),
}

SUPPORTED_ATTESTATION_FORMATS = frozenset({"none", "packed", "fido-u2f"})


class WebauthnError(Exception):
    """Rejected WebAuthn input; carries a stable error code."""

    def __init__(self, message: str, *, code: str = "webauthn_rejected") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def b64url_decode(value: str) -> bytes:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError) as exc:
        raise WebauthnError("Invalid base64url encoding", code="webauthn_bad_encoding") from exc


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sha256_raw(raw: bytes) -> bytes:
    return hashlib.sha256(raw).digest()


def sha256_hex_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- #
# Minimal CBOR (RFC 8949) codec for attestation objects and COSE keys
# --------------------------------------------------------------------------- #


def _encode_head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    if value < 1 << 8:
        return bytes([(major << 5) | 24, value])
    if value < 1 << 16:
        return bytes([(major << 5) | 25]) + struct.pack(">H", value)
    if value < 1 << 32:
        return bytes([(major << 5) | 26]) + struct.pack(">I", value)
    return bytes([(major << 5) | 27]) + struct.pack(">Q", value)


def cbor_encode(value: Any) -> bytes:
    if value is None:
        return b"\xf6"
    if value is False:
        return b"\xf4"
    if value is True:
        return b"\xf5"
    if isinstance(value, int):
        if value >= 0:
            return _encode_head(0, value)
        return _encode_head(1, -1 - value)
    if isinstance(value, bytes):
        return _encode_head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _encode_head(3, len(raw)) + raw
    if isinstance(value, list):
        out = _encode_head(4, len(value))
        for item in value:
            out += cbor_encode(item)
        return out
    if isinstance(value, dict):
        out = _encode_head(5, len(value))
        for key, item in value.items():
            out += cbor_encode(key) + cbor_encode(item)
        return out
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    raise WebauthnError(f"Unsupported CBOR value: {type(value).__name__}", code="webauthn_cbor")


def _decode_item(data: bytes, offset: int) -> tuple[Any, int]:
    if offset >= len(data):
        raise WebauthnError("Truncated CBOR input", code="webauthn_cbor")
    initial = data[offset]
    major = initial >> 5
    info = initial & 0x1F
    offset += 1
    if info < 24:
        value: int | None = info
    elif info == 24:
        if offset + 1 > len(data):
            raise WebauthnError("Truncated CBOR head", code="webauthn_cbor")
        value = data[offset]
        offset += 1
    elif info == 25:
        if offset + 2 > len(data):
            raise WebauthnError("Truncated CBOR head", code="webauthn_cbor")
        value = struct.unpack(">H", data[offset : offset + 2])[0]
        offset += 2
    elif info == 26:
        if offset + 4 > len(data):
            raise WebauthnError("Truncated CBOR head", code="webauthn_cbor")
        value = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
    elif info == 27:
        if offset + 8 > len(data):
            raise WebauthnError("Truncated CBOR head", code="webauthn_cbor")
        value = struct.unpack(">Q", data[offset : offset + 8])[0]
        offset += 8
    else:
        raise WebauthnError("Indefinite-length CBOR is not accepted", code="webauthn_cbor")
    assert value is not None

    if major == 0:
        return value, offset
    if major == 1:
        return -1 - value, offset
    if major == 2:
        if offset + value > len(data):
            raise WebauthnError("Truncated CBOR byte string", code="webauthn_cbor")
        return data[offset : offset + value], offset + value
    if major == 3:
        if offset + value > len(data):
            raise WebauthnError("Truncated CBOR text string", code="webauthn_cbor")
        try:
            return data[offset : offset + value].decode("utf-8"), offset + value
        except UnicodeDecodeError as exc:
            raise WebauthnError("Invalid CBOR text string", code="webauthn_cbor") from exc
    if major == 4:
        items: list[Any] = []
        for _ in range(value):
            item, offset = _decode_item(data, offset)
            items.append(item)
        return items, offset
    if major == 5:
        mapping: dict[Any, Any] = {}
        for _ in range(value):
            key, offset = _decode_item(data, offset)
            item, offset = _decode_item(data, offset)
            if key in mapping:
                raise WebauthnError("Duplicate CBOR map key", code="webauthn_cbor")
            mapping[key] = item
        return mapping, offset
    if major == 6:
        # Tags are unwrapped; none of the WebAuthn structures we consume
        # require tag semantics.
        item, offset = _decode_item(data, offset)
        return item, offset
    if major == 7:
        if info == 20:
            return False, offset
        if info == 21:
            return True, offset
        if info == 22:
            return None, offset
        if info == 23:
            return None, offset
        if info == 25:
            if offset + 2 > len(data):
                raise WebauthnError("Truncated CBOR half float", code="webauthn_cbor")
            half = struct.unpack(">H", data[offset : offset + 2])[0]
            offset += 2
            return _half_to_float(half), offset
        if info == 26:
            if offset + 4 > len(data):
                raise WebauthnError("Truncated CBOR float", code="webauthn_cbor")
            value = struct.unpack(">f", data[offset : offset + 4])[0]
            return float(value), offset + 4
        if info == 27:
            if offset + 8 > len(data):
                raise WebauthnError("Truncated CBOR double", code="webauthn_cbor")
            return struct.unpack(">d", data[offset : offset + 8])[0], offset + 8
        raise WebauthnError(f"Unsupported CBOR simple value {info}", code="webauthn_cbor")
    raise WebauthnError(f"Unsupported CBOR major type {major}", code="webauthn_cbor")


def _half_to_float(half: int) -> float:
    sign = -1.0 if half & 0x8000 else 1.0
    exponent = (half >> 10) & 0x1F
    fraction = half & 0x3FF
    if exponent == 0:
        return sign * fraction * (2**-24)
    if exponent == 31:
        return sign * float("inf") if fraction == 0 else sign * float("nan")
    return sign * (1 + fraction / 1024) * (2 ** (exponent - 15))


def cbor_decode(data: bytes) -> Any:
    value, offset = _decode_item(data, 0)
    if offset != len(data):
        raise WebauthnError("Trailing bytes after CBOR item", code="webauthn_cbor")
    return value


# --------------------------------------------------------------------------- #
# COSE public keys
# --------------------------------------------------------------------------- #


def cose_key_to_public_key(cose: dict) -> tuple[Any, int]:
    """Return (public_key, algorithm) from a COSE_Key map."""
    if not isinstance(cose, dict):
        raise WebauthnError("COSE key is not a map", code="webauthn_cose")
    kty = cose.get(1)
    alg = cose.get(3)
    if not isinstance(alg, int):
        raise WebauthnError("COSE key missing algorithm", code="webauthn_cose")
    if kty == COSE_EC2:
        crv = cose.get(-1)
        x = cose.get(-2)
        y = cose.get(-3)
        if not isinstance(crv, int) or crv not in _EC_CURVES:
            raise WebauthnError("Invalid EC2 curve", code="webauthn_cose")
        if not isinstance(x, bytes) or not isinstance(y, bytes):
            raise WebauthnError("Invalid EC2 COSE key", code="webauthn_cose")
        numbers = ec.EllipticCurvePublicNumbers(
            int.from_bytes(x, "big"),
            int.from_bytes(y, "big"),
            _EC_CURVES[crv],
        )
        return numbers.public_key(), alg
    if kty == COSE_RSA:
        n = cose.get(-1)
        e = cose.get(-2)
        if not isinstance(n, bytes) or not isinstance(e, bytes):
            raise WebauthnError("Invalid RSA COSE key", code="webauthn_cose")
        rsa_numbers = rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big"))
        return rsa_numbers.public_key(), alg
    if kty == COSE_OKP:
        if cose.get(-1) != 6 or not isinstance(cose.get(-2), bytes):
            raise WebauthnError("Only Ed25519 OKP keys are supported", code="webauthn_cose")
        return ed25519.Ed25519PublicKey.from_public_bytes(cose[-2]), alg
    raise WebauthnError(f"Unsupported COSE key type {kty}", code="webauthn_cose")


def cose_key_to_json(cose: dict) -> dict:
    """Make a COSE key JSON-serializable (bytes become base64url strings)."""
    return {
        key: (b64url_encode(value) if isinstance(value, bytes) else value)
        for key, value in cose.items()
    }


def cose_key_from_json(data: dict) -> dict:
    """Restore a COSE key from JSON (int keys, base64url byte values)."""
    restored: dict = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lstrip("-").isdigit():
            restored[int(key)] = value
        else:
            restored[key] = value
    for key, value in list(restored.items()):
        if isinstance(value, str):
            try:
                restored[key] = b64url_decode(value)
            except WebauthnError:
                restored[key] = value
    return restored


def _verify_with_algorithm(
    public_key: Any,
    algorithm: int,
    signed_data: bytes,
    signature: bytes,
) -> None:
    try:
        if algorithm == -7:  # ES256
            public_key.verify(signature, signed_data, ec.ECDSA(hashes.SHA256()))
        elif algorithm == -35:  # ES384
            public_key.verify(signature, signed_data, ec.ECDSA(hashes.SHA384()))
        elif algorithm == -36:  # ES512
            public_key.verify(signature, signed_data, ec.ECDSA(hashes.SHA512()))
        elif algorithm == -8:  # EdDSA
            public_key.verify(signature, signed_data)
        elif algorithm == -257:  # RS256
            public_key.verify(
                signature, signed_data, padding.PKCS1v15(), hashes.SHA256()
            )
        elif algorithm == -258:  # RS384
            public_key.verify(
                signature, signed_data, padding.PKCS1v15(), hashes.SHA384()
            )
        elif algorithm == -259:  # RS512
            public_key.verify(
                signature, signed_data, padding.PKCS1v15(), hashes.SHA512()
            )
        elif algorithm == -37:  # PS256
            public_key.verify(
                signature,
                signed_data,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256(),
            )
        elif algorithm == -38:  # PS384
            public_key.verify(
                signature,
                signed_data,
                padding.PSS(mgf=padding.MGF1(hashes.SHA384()), salt_length=48),
                hashes.SHA384(),
            )
        elif algorithm == -39:  # PS512
            public_key.verify(
                signature,
                signed_data,
                padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64),
                hashes.SHA512(),
            )
        else:
            raise WebauthnError(
                f"Unsupported COSE signature algorithm {algorithm}",
                code="webauthn_algorithm",
            )
    except InvalidSignature as exc:
        raise WebauthnError(
            "WebAuthn signature verification failed",
            code="webauthn_signature",
        ) from exc


# --------------------------------------------------------------------------- #
# Authenticator data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuthenticatorData:
    rp_id_hash: bytes
    flags: int
    sign_count: int
    aaguid: bytes | None
    credential_id: bytes | None
    cose_public_key: dict | None


def parse_authenticator_data(auth_data: bytes) -> AuthenticatorData:
    if len(auth_data) < 37:
        raise WebauthnError("Authenticator data too short", code="webauthn_authdata")
    rp_id_hash = auth_data[:32]
    flags = auth_data[32]
    sign_count = int.from_bytes(auth_data[33:37], "big")
    offset = 37
    aaguid: bytes | None = None
    credential_id: bytes | None = None
    cose_key: dict | None = None
    if flags & FLAG_AT:
        if len(auth_data) < offset + 18:
            raise WebauthnError("Attested credential data truncated", code="webauthn_authdata")
        aaguid = auth_data[offset : offset + 16]
        cred_len = int.from_bytes(auth_data[offset + 16 : offset + 18], "big")
        offset += 18
        if len(auth_data) < offset + cred_len:
            raise WebauthnError("Credential ID truncated", code="webauthn_authdata")
        credential_id = auth_data[offset : offset + cred_len]
        offset += cred_len
        if offset >= len(auth_data):
            raise WebauthnError("Missing COSE public key", code="webauthn_authdata")
        try:
            cose_key, offset = _decode_item(auth_data, offset)
        except WebauthnError:
            raise
        if not isinstance(cose_key, dict):
            raise WebauthnError("COSE public key is not a map", code="webauthn_authdata")
    return AuthenticatorData(
        rp_id_hash=rp_id_hash,
        flags=flags,
        sign_count=sign_count,
        aaguid=aaguid,
        credential_id=credential_id,
        cose_public_key=cose_key,
    )


# --------------------------------------------------------------------------- #
# Client data
# --------------------------------------------------------------------------- #


def verify_client_data(
    client_data_raw: bytes,
    *,
    expected_type: str,
    expected_challenge_hash: str,
    allowed_origins: list[str],
) -> dict:
    try:
        data = json.loads(client_data_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebauthnError("Invalid clientDataJSON", code="webauthn_clientdata") from exc
    if not isinstance(data, dict):
        raise WebauthnError("clientDataJSON is not an object", code="webauthn_clientdata")
    if data.get("type") != expected_type:
        raise WebauthnError(
            f"clientDataJSON type mismatch: {data.get('type')!r}",
            code="webauthn_clientdata",
        )
    if data.get("crossOrigin") is True:
        raise WebauthnError("Cross-origin WebAuthn assertion rejected", code="webauthn_origin")
    challenge = data.get("challenge")
    if not isinstance(challenge, str):
        raise WebauthnError("Missing challenge in clientDataJSON", code="webauthn_clientdata")
    challenge_bytes = b64url_decode(challenge)
    if sha256_hex_bytes(challenge_bytes) != expected_challenge_hash:
        raise WebauthnError(
            "Challenge mismatch — response is not for this ceremony",
            code="webauthn_challenge",
        )
    origin = data.get("origin")
    if origin not in allowed_origins:
        raise WebauthnError(
            f"Origin {origin!r} is not allowed",
            code="webauthn_origin",
        )
    return data


# --------------------------------------------------------------------------- #
# Attestation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AttestationResult:
    credential_id: bytes
    credential_id_b64url: str
    cose_public_key: dict
    sign_count: int
    aaguid: str | None
    attestation_format: str
    verification_level: str  # none | self | basic | basic_untrusted_root


def _load_trust_roots(entries: list[str]) -> list[x509.Certificate]:
    roots: list[x509.Certificate] = []
    for entry in entries:
        if not entry:
            continue
        pem = entry
        if "\n" not in entry and entry.endswith(".pem"):
            try:
                with open(entry, encoding="utf-8") as handle:
                    pem = handle.read()
            except OSError as exc:
                raise WebauthnError(
                    f"Cannot read attestation trust root {entry}",
                    code="webauthn_attestation",
                ) from exc
        try:
            roots.append(x509.load_pem_x509_certificate(pem.encode("utf-8")))
        except ValueError as exc:
            raise WebauthnError(
                "Invalid PEM attestation trust root",
                code="webauthn_attestation",
            ) from exc
    return roots


def _now() -> datetime:
    from app.utils.text import utcnow

    return utcnow()


def _verify_cert_chain(
    leaf: x509.Certificate,
    chain: list[bytes],
    roots: list[x509.Certificate],
) -> None:
    certs = [leaf]
    for der in chain[1:]:
        certs.append(x509.load_der_x509_certificate(der))
    now = _now()
    for cert in certs:
        if cert.not_valid_before_utc > now or cert.not_valid_after_utc < now:
            raise WebauthnError(
                "Attestation certificate is outside its validity window",
                code="webauthn_attestation",
            )
    for index, cert in enumerate(certs[:-1]):
        try:
            cert.verify_directly_issued_by(certs[index + 1])
        except ValueError as exc:
            raise WebauthnError(
                "Attestation certificate chain is not signed by the next certificate",
                code="webauthn_attestation",
            ) from exc
    for root in roots:
        try:
            certs[-1].verify_directly_issued_by(root)
            return
        except ValueError:
            continue
    raise WebauthnError(
        "Attestation certificate chain does not terminate at a configured trust root",
        code="webauthn_attestation",
    )


def _u2f_public_key(cose: dict) -> bytes:
    if cose.get(1) != COSE_EC2 or cose.get(-1) != 1:
        raise WebauthnError("fido-u2f requires an EC2 P-256 key", code="webauthn_attestation")
    x = cose.get(-2)
    y = cose.get(-3)
    if not isinstance(x, bytes) or not isinstance(y, bytes):
        raise WebauthnError("fido-u2f COSE key malformed", code="webauthn_attestation")
    return b"\x04" + x + y


def _verify_packed(
    att_stmt: dict,
    auth_data: bytes,
    client_data_hash: bytes,
    credential_cose: dict,
    trust_roots: list[x509.Certificate],
) -> str:
    algorithm = att_stmt.get("alg")
    signature = att_stmt.get("sig")
    if not isinstance(algorithm, int) or not isinstance(signature, bytes):
        raise WebauthnError("packed attestation missing alg/sig", code="webauthn_attestation")
    signed = auth_data + client_data_hash
    x5c = att_stmt.get("x5c")
    if x5c is None:
        # Self attestation: signature is made with the credential private key.
        if credential_cose.get(1) != COSE_EC2:
            raise WebauthnError(
                "packed self attestation requires an EC2 credential key",
                code="webauthn_attestation",
            )
        public_key, _ = cose_key_to_public_key(credential_cose)
        _verify_with_algorithm(public_key, algorithm, signed, signature)
        return "self"
    if not isinstance(x5c, list) or not x5c or not all(isinstance(c, bytes) for c in x5c):
        raise WebauthnError("packed attestation x5c malformed", code="webauthn_attestation")
    leaf = x509.load_der_x509_certificate(x5c[0])
    _verify_with_algorithm(leaf.public_key(), algorithm, signed, signature)
    if trust_roots:
        _verify_cert_chain(leaf, x5c, trust_roots)
        return "basic"
    return "basic_untrusted_root"


def _verify_fido_u2f(
    att_stmt: dict,
    auth_data: bytes,
    client_data_hash: bytes,
    credential_id: bytes,
    credential_cose: dict,
    trust_roots: list[x509.Certificate],
) -> str:
    signature = att_stmt.get("sig")
    x5c = att_stmt.get("x5c")
    if not isinstance(signature, bytes) or not isinstance(x5c, list) or not x5c:
        raise WebauthnError("fido-u2f attestation missing sig/x5c", code="webauthn_attestation")
    leaf = x509.load_der_x509_certificate(x5c[0])
    u2f_key = _u2f_public_key(credential_cose)
    # Verify the certificate public key equals the credential public key.
    cert_public = leaf.public_key()
    if not isinstance(cert_public, ec.EllipticCurvePublicKey):
        raise WebauthnError("fido-u2f certificate is not EC", code="webauthn_attestation")
    cert_numbers = cert_public.public_numbers()
    cose_numbers = ec.EllipticCurvePublicNumbers(
        int.from_bytes(credential_cose[-2], "big"),
        int.from_bytes(credential_cose[-3], "big"),
        ec.SECP256R1(),
    ).public_key().public_numbers()
    if (cert_numbers.x, cert_numbers.y) != (cose_numbers.x, cose_numbers.y):
        raise WebauthnError(
            "fido-u2f certificate key does not match credential key",
            code="webauthn_attestation",
        )
    rp_id_hash = auth_data[:32]
    signed = b"\x00" + rp_id_hash + client_data_hash + credential_id + u2f_key
    _verify_with_algorithm(cert_public, -7, signed, signature)
    if trust_roots:
        _verify_cert_chain(leaf, x5c, trust_roots)
        return "basic"
    return "basic_untrusted_root"


def verify_attestation(
    attestation_object_raw: bytes,
    *,
    client_data_raw: bytes,
    expected_challenge_hash: str,
    rp_id: str,
    allowed_origins: list[str],
    require_attestation: bool,
    trust_roots: list[str] | None = None,
) -> AttestationResult:
    """Fully verify a registration attestation object and return the result."""
    try:
        obj = cbor_decode(attestation_object_raw)
    except WebauthnError:
        raise
    if not isinstance(obj, dict):
        raise WebauthnError("Attestation object is not a map", code="webauthn_attestation")
    fmt = obj.get("fmt")
    att_stmt = obj.get("attStmt") or {}
    auth_data_raw = obj.get("authData")
    if fmt not in SUPPORTED_ATTESTATION_FORMATS:
        raise WebauthnError(
            f"Unsupported attestation format {fmt!r}",
            code="webauthn_attestation",
        )
    if not isinstance(att_stmt, dict) or not isinstance(auth_data_raw, bytes):
        raise WebauthnError("Attestation object malformed", code="webauthn_attestation")

    verify_client_data(
        client_data_raw,
        expected_type="webauthn.create",
        expected_challenge_hash=expected_challenge_hash,
        allowed_origins=allowed_origins,
    )
    auth_data = parse_authenticator_data(auth_data_raw)
    if not auth_data.flags & FLAG_AT:
        raise WebauthnError(
            "Registration authData missing attested credential data",
            code="webauthn_authdata",
        )
    if auth_data.credential_id is None or auth_data.cose_public_key is None:
        raise WebauthnError("Registration authData incomplete", code="webauthn_authdata")
    if sha256_hex_bytes(auth_data.rp_id_hash) != sha256_hex_bytes(sha256_raw(rp_id.encode())):
        raise WebauthnError("RP ID hash mismatch", code="webauthn_rpid")

    client_data_hash = sha256_raw(client_data_raw)
    loaded_roots = _load_trust_roots(trust_roots or [])
    if fmt == "none":
        if att_stmt:
            raise WebauthnError(
                "Attestation format 'none' must have an empty attStmt",
                code="webauthn_attestation",
            )
        level = "none"
    elif fmt == "packed":
        level = _verify_packed(
            att_stmt, auth_data_raw, client_data_hash, auth_data.cose_public_key, loaded_roots
        )
    else:  # fido-u2f
        level = _verify_fido_u2f(
            att_stmt,
            auth_data_raw,
            client_data_hash,
            auth_data.credential_id,
            auth_data.cose_public_key,
            loaded_roots,
        )
    if require_attestation and level in ("none", "basic_untrusted_root"):
        raise WebauthnError(
            "Attestation is required and the statement could not be verified to a trust root",
            code="webauthn_attestation_required",
        )
    return AttestationResult(
        credential_id=auth_data.credential_id,
        credential_id_b64url=b64url_encode(auth_data.credential_id),
        cose_public_key=auth_data.cose_public_key,
        sign_count=auth_data.sign_count,
        aaguid=auth_data.aaguid.hex() if auth_data.aaguid is not None else None,
        attestation_format=fmt,
        verification_level=level,
    )


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


def verify_authentication(
    *,
    client_data_raw: bytes,
    authenticator_data_raw: bytes,
    signature: bytes,
    expected_challenge_hash: str,
    rp_id: str,
    allowed_origins: list[str],
    stored_public_key_cose: dict,
    stored_sign_count: int,
) -> int:
    """Verify an authentication response and return the new sign count."""
    verify_client_data(
        client_data_raw,
        expected_type="webauthn.get",
        expected_challenge_hash=expected_challenge_hash,
        allowed_origins=allowed_origins,
    )
    auth_data = parse_authenticator_data(authenticator_data_raw)
    if sha256_hex_bytes(auth_data.rp_id_hash) != sha256_hex_bytes(sha256_raw(rp_id.encode())):
        raise WebauthnError("RP ID hash mismatch", code="webauthn_rpid")
    if not auth_data.flags & FLAG_UP:
        raise WebauthnError(
            "User presence flag not set in authenticator data",
            code="webauthn_user_presence",
        )
    public_key, algorithm = cose_key_to_public_key(
        cose_key_from_json(stored_public_key_cose)
    )
    client_data_hash = sha256_raw(client_data_raw)
    _verify_with_algorithm(
        public_key,
        algorithm,
        authenticator_data_raw + client_data_hash,
        signature,
    )
    if stored_sign_count != 0 and auth_data.sign_count <= stored_sign_count:
        raise WebauthnError(
            "Signature counter did not advance — possible replay or cloned authenticator",
            code="webauthn_replay",
        )
    return auth_data.sign_count
