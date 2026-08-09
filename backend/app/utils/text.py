import hashlib
import json
import re
from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def token_estimate(text: str) -> int:
    """Rough token estimate: ~4 characters per token, minimum 1."""
    return max(1, len(text) // 4)


def simple_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", normalize_text(text))
    return set(words)


def fingerprint(payload: dict) -> str:
    """Stable dedup key for a memory payload."""
    return sha256_hex(canonical_json(payload))[:32]

