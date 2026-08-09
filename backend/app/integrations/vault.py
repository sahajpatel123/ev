"""Encrypted credential vault for integrations.

Uses Fernet (via :mod:`cryptography`) with a key from ``EV_VAULT_KEY``, or a
deterministic derivation from the master key when no separate vault key is
configured. Plaintext tokens exist only in memory for the duration of an
action/webhook call and are never written to logs, prompts, memory rows, or
model context.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_fernet: Fernet | None = None


def _derive_key(raw: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())


def _key() -> bytes:
    return _derive_key(settings.vault_key or settings.master_key)


def _fernet_instance() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_key())
    return _fernet


def encrypt(value: str) -> str:
    return _fernet_instance().encrypt(value.encode("utf-8")).decode("ascii")


def encrypt_with(key: str, value: str) -> str:
    """Encrypt with an explicit vault key (used during key rotation)."""
    return Fernet(_derive_key(key)).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet_instance().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("vault ciphertext invalid or vault key changed") from exc


def reset() -> None:
    """Drop the cached Fernet instance (call after rotating the vault key)."""
    global _fernet
    _fernet = None


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
