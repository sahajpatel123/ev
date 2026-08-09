"""Encryption at rest for biometric voice data.

Voiceprints are authenticated-encrypted with Fernet before they ever touch the
database. The key is derived from EV_MASTER_KEY via scrypt so a database leak
does not expose biometric material. There is intentionally no plaintext
fallback: if cryptography is unavailable the system fails closed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.fernet import Fernet, InvalidToken

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32


def _derive_key(master_key: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        master_key.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
    )


def _fernet_for(master_key: str, salt: bytes) -> Fernet:
    raw = _derive_key(master_key, salt)
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_payload(payload: dict, *, master_key: str) -> tuple[str, str]:
    """Encrypt a JSON-serializable payload. Returns (token, salt_hex)."""
    salt = os.urandom(16)
    token = _fernet_for(master_key, salt).encrypt(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return token.decode("utf-8"), salt.hex()


def decrypt_payload(token: str, salt_hex: str, *, master_key: str) -> dict:
    """Decrypt a payload previously produced by :func:`encrypt_payload`."""
    try:
        raw = _fernet_for(master_key, bytes.fromhex(salt_hex)).decrypt(token.encode("utf-8"))
    except (InvalidToken, ValueError) as exc:
        raise ValueError("Voiceprint decryption failed: bad key, salt, or ciphertext") from exc
    return json.loads(raw.decode("utf-8"))
