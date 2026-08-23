"""Release contract: build identity, protocol compatibility, asset coherence.

Regression guard for two owner-verified outages:
  * 22.20 client vs 22.12 server (first A01 lockout)
  * 22.21 client vs 22.20 server (second A01 lockout — stale backend process
    advertising process-start settings while serving newer disk assets)

Laws enforced here:
  L1  ONE generated manifest (clients/pwa/release.json) is the runtime truth;
      hello/health read it from disk at request time.
  L2  Build identity is NOT authorization: a compatible client with a
      different build authenticates and reaches READY (update_recommended).
  L3  Only genuine protocol incompatibility blocks READY, with a structured,
      non-auth error (CLIENT_PROTOCOL_UNSUPPORTED / CLIENT_UPDATE_REQUIRED).
  L4  All pin sites (config default, gateway constant, sw.js, app.js,
      index.html marker) must equal the generated manifest — a partial bump
      fails CI instead of bricking phones.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.device_gateway import PROTOCOL_VERSION, PWA_BUILD
from app.device_gateway.release import (
    WEB_PROTOCOL_MAX,
    WEB_PROTOCOL_MIN,
    current_web_release,
    evaluate_version_compat,
    load_release_manifest,
)

PWA = Path(__file__).resolve().parents[1] / "clients" / "pwa"


# ---------------------------------------------------------------------------
# L1/L4 — single source of truth and deploy coherence
# ---------------------------------------------------------------------------


def test_release_manifest_exists_and_is_current() -> None:
    manifest = load_release_manifest()
    assert manifest is not None, (
        "clients/pwa/release.json missing — run `make pwa-release-manifest`"
    )
    assert manifest["web_build"] == PWA_BUILD


def test_all_pin_sites_equal_generated_manifest() -> None:
    """A partial bump must fail here, not on two iPhones."""
    manifest = load_release_manifest()
    assert manifest is not None
    build = manifest["web_build"]

    assert Settings.model_fields["pwa_build"].default == build
    assert build == PWA_BUILD

    sw = (PWA / "sw.js").read_text()
    app_js = (PWA / "app.js").read_text()
    html = (PWA / "index.html").read_text()
    assert f'const BUILD = "{build}"' in sw
    assert f'CLIENT_BUILD = "{build}"' in app_js
    assert f'<meta name="evie-build" content="{build}">' in html


async def test_hello_serves_disk_release_not_process_memory(client, monkeypatch) -> None:
    """THE incident test: even if the process booted with an old env value,
    hello must advertise what is actually served from disk."""
    import httpx

    from app.device_gateway import api as gw_api
    from app.main import app

    monkeypatch.setattr(gw_api.settings, "pwa_build", "2000.01.01.01")
    release = gw_api.current_web_release()
    assert release["web_build"] != "2000.01.01.01"  # disk wins over settings

    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": "Release Truth"},
    )
    assert minted.status_code == 200, minted.text
    phone = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": minted.json()["pairing_token"],
            "display_name": "Release Truth",
            "protocol_version": PROTOCOL_VERSION,
            "platform": "web",
        },
    )
    assert paired.status_code == 200, paired.text
    token = paired.json()["device_token"]

    hello = await client.post(
        "/v1/device-gateway/hello",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "protocol_version": PROTOCOL_VERSION,
            "client_build": "2000.01.01.01",  # ancient client
            "instance_id": "t1",
        },
    )
    assert hello.status_code == 200
    body = hello.json()
    # Old-but-compatible client: authenticated AND ready-worthy…
    assert body["ok"] is True
    assert body["update_required"] is False
    assert body["update_recommended"] is True
    # …while the server advertises the real served build, not its boot memory.
    assert body["pwa_build"] == PWA_BUILD
    assert body["latest_web_build"] == PWA_BUILD


# ---------------------------------------------------------------------------
# L2 — compatible build mismatch never bricks authentication (A13)
# ---------------------------------------------------------------------------


def test_evaluate_compat_build_differs_protocol_same() -> None:
    decision = evaluate_version_compat(
        client_build="2026.08.22.21",
        client_protocol="1",
        release={"web_build": "2026.08.23.22", "web_protocol_min": 1, "web_protocol_max": 1},
    )
    assert decision["protocol_supported"] is True
    assert decision["update_required"] is False
    assert decision["update_recommended"] is True
    assert decision["latest_web_build"] == "2026.08.23.22"


def test_evaluate_compat_newer_client_older_server() -> None:
    """The exact 22.21-client/22.20-server shape: must be RECOMMENDED, not fatal."""
    decision = evaluate_version_compat(
        client_build="2026.08.22.21",
        client_protocol="1",
        release={"web_build": "2026.08.22.20", "web_protocol_min": 1, "web_protocol_max": 1},
    )
    assert decision["update_required"] is False
    assert decision["update_recommended"] is True


def test_evaluate_compat_exact_match_is_quiet() -> None:
    decision = evaluate_version_compat(
        client_build=PWA_BUILD,
        client_protocol="1",
        release=current_web_release(),
    )
    assert decision["update_required"] is False
    assert decision["update_recommended"] is False
    assert decision["build_matches"] is True


# ---------------------------------------------------------------------------
# L3 — genuine incompatibility is structured, never AUTH_FAILED (A14)
# ---------------------------------------------------------------------------


def test_evaluate_compat_unsupported_protocol_blocks_with_reason() -> None:
    decision = evaluate_version_compat(
        client_build="2025.01.01.01",
        client_protocol="0",
        release={"web_build": PWA_BUILD, "web_protocol_min": 1, "web_protocol_max": 1},
    )
    assert decision["protocol_supported"] is False
    assert decision["update_required"] is True
    assert decision["update_reason"] == "CLIENT_PROTOCOL_UNSUPPORTED"
    # Even now, this is a compatibility verdict — never an auth verdict.


async def test_hello_rejects_unsupported_protocol_structured(client) -> None:
    import httpx

    from app.main import app

    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": "Old Proto"},
    )
    assert minted.status_code == 200, minted.text
    phone = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": minted.json()["pairing_token"],
            "display_name": "Old Proto",
            "protocol_version": PROTOCOL_VERSION,
            "platform": "web",
        },
    )
    assert paired.status_code == 200, paired.text
    token = paired.json()["device_token"]

    res = await phone.post(
        "/v1/device-gateway/hello",
        headers={"Authorization": f"Bearer {token}"},
        json={"protocol_version": "0", "client_build": PWA_BUILD, "instance_id": "t2"},
    )
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["error_code"] == "CLIENT_PROTOCOL_UNSUPPORTED"
    assert detail["failed_stage"] == "B02"
    assert res.headers.get("X-Evie-Update-Required") == "true"


# ---------------------------------------------------------------------------
# Mixed-asset detection inputs (A15) — server-side coherence half
# ---------------------------------------------------------------------------


def test_asset_manifest_covers_critical_assets_and_hashes_match_disk() -> None:
    import hashlib

    manifest = load_release_manifest()
    assert manifest is not None
    files = manifest["files"]
    for name in ("index.html", "app.js", "sw.js"):
        digest = hashlib.sha256((PWA / name).read_bytes()).hexdigest()
        assert files[name] == digest, f"{name} changed after manifest generation"


def test_web_protocol_range_defaults_are_sane() -> None:
    assert 1 <= WEB_PROTOCOL_MIN <= WEB_PROTOCOL_MAX
    release = current_web_release()
    assert release["web_protocol_min"] <= release["web_protocol"] <= release["web_protocol_max"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------



