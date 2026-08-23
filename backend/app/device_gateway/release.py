"""Web release identity: ONE generated manifest is the runtime truth.

Incident history that shaped this module:

* 2026.08.21/22 — served assets were build .21 while the long-running backend
  process still advertised .22.20 from its process-start settings. Both paired
  iPhones failed authentication ("FAILED AT A01 BUILD MISMATCH"). An earlier
  .20-vs-.12 outage had already happened.

Root cause: two independently synchronized sources of truth (frontend file
constants vs process-start ``settings.pwa_build``). This module removes the
second one:

* ``clients/pwa/release.json`` is generated next to the assets it describes
  (see ``app.scripts.gen_release_manifest``). Hello and health endpoints read
  it FROM DISK AT REQUEST TIME, so a forgotten backend restart can no longer
  diverge from what is actually served.

* Build identity is deployment metadata, NOT an authorization input. Device
  authentication answers "is this a valid paired device"; version negotiation
  answers "is this client compatible". A cosmetic build difference yields
  ``update_recommended`` while the client stays READY.

* Hard compatibility is protocol-driven (``web_protocol`` range), matching the
  existing device ``protocol_version`` gate.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, PWA_BUILD

logger = logging.getLogger("ev.device_gateway.release")

# Web (PWA) protocol contract. Bump when a client change breaks the hello /
# auth / voice handshake for older clients; exact build strings never gate.
WEB_PROTOCOL = 1
WEB_PROTOCOL_MIN = 1
WEB_PROTOCOL_MAX = 1

# Stages reported to clients so diagnostics separate concerns:
#   B00 APP_BOOT / B01 ASSET_INTEGRITY / B02 VERSION_COMPATIBILITY
#   A00 DEVICE_CREDENTIAL / A01 AUTH_REQUEST / A02 AUTHENTICATED
STAGE_APP_BOOT = "B00"
STAGE_ASSET_INTEGRITY = "B01"
STAGE_VERSION_COMPATIBILITY = "B02"
STAGE_DEVICE_CREDENTIAL = "A00"
STAGE_AUTH_REQUEST = "A01"
STAGE_AUTHENTICATED = "A02"

_UPDATE_REASON_PROTOCOL = "CLIENT_PROTOCOL_UNSUPPORTED"


def _manifest_path() -> Path:
    """release.json lives beside the assets it describes."""
    return Path(__file__).resolve().parents[2] / "clients" / "pwa" / "release.json"


def load_release_manifest() -> dict[str, Any] | None:
    """Read the generated manifest from disk. Never cached across deploys."""
    try:
        raw = _manifest_path().read_text()
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def current_web_release() -> dict[str, Any]:
    """The web release identity the server actually serves, read at call time.

    Falls back to the compiled-in default only when no generated manifest is
    present (fresh checkouts before the first ``make pwa-release-manifest``).
    """
    manifest = load_release_manifest()
    if manifest and str(manifest.get("web_build") or "").strip():
        return {
            "web_build": str(manifest["web_build"]),
            "web_protocol": int(manifest.get("web_protocol") or WEB_PROTOCOL),
            "api_protocol": str(manifest.get("api_protocol") or PROTOCOL_VERSION),
            "web_protocol_min": int(
                manifest.get("web_protocol_min", WEB_PROTOCOL_MIN)
            ),
            "web_protocol_max": int(
                manifest.get("web_protocol_max", WEB_PROTOCOL_MAX)
            ),
            "generated_at": manifest.get("generated_at"),
            "asset_manifest_hash": manifest.get("asset_manifest_hash"),
        }
    return {
        "web_build": PWA_BUILD,
        "web_protocol": WEB_PROTOCOL,
        "api_protocol": PROTOCOL_VERSION,
        "web_protocol_min": WEB_PROTOCOL_MIN,
        "web_protocol_max": WEB_PROTOCOL_MAX,
        "generated_at": None,
        "asset_manifest_hash": None,
    }


def evaluate_version_compat(
    *,
    client_build: str | None,
    client_protocol: str | int | None,
    release: dict[str, Any],
) -> dict[str, Any]:
    """Pure compatibility decision. Authentication NEVER depends on this.

    Returns structured state so clients can distinguish:
      * protocol unsupported -> hard update required (CLIENT_UPDATE_REQUIRED)
      * build merely older/newer  -> stay READY, surface "Update available"
    """
    try:
        proto = int(str(client_protocol or release.get("api_protocol")).split(".")[0])
    except (ValueError, TypeError):
        proto = None
    lo = int(release.get("web_protocol_min", WEB_PROTOCOL_MIN))
    hi = int(release.get("web_protocol_max", WEB_PROTOCOL_MAX))
    protocol_supported = proto is not None and lo <= proto <= hi

    latest = str(release.get("web_build") or "")
    client = str(client_build or "").strip()
    build_matches = bool(client) and client == latest
    update_required = not protocol_supported

    return {
        "latest_web_build": latest,
        "server_release": latest,
        "protocol_supported": protocol_supported,
        "build_matches": build_matches,
        # Cosmetic skew never bricks the client; protocol skew does.
        "update_required": update_required,
        "update_reason": None if protocol_supported else _UPDATE_REASON_PROTOCOL,
        "update_recommended": protocol_supported and not build_matches,
    }


def asset_files() -> list[str]:
    """Critical frontend assets whose coherence defines one release."""
    return [
        "index.html",
        "app.js",
        "sw.js",
        "audio.js",
        "webrtc.js",
        "mobile-actions.js",
        "presence.js",
        "orb.js",
        "pcm-worklet.js",
        "playback-worklet.js",
        "feedback.js",
        "style.css",
        "manifest.webmanifest",
    ]


def hash_asset(name: str) -> str | None:
    path = _manifest_path().parent / name
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def build_asset_manifest() -> dict[str, Any]:
    """Used by the generator script; also the shape clients may verify against."""
    files: dict[str, str | None] = {}
    hasher = hashlib.sha256()
    for name in asset_files():
        digest = hash_asset(name)
        files[name] = digest
        if digest:
            hasher.update(digest.encode())
    return {
        "files": files,
        "asset_manifest_hash": hasher.hexdigest(),
    }
