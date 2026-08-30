"""Tests for the EV web workbench (static SPA served at /app)."""

from __future__ import annotations

import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "docs" / "schemas"


def loopback_client(host: str = "127.0.0.1", port: int = 55555) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=(host, port)),
        base_url="http://127.0.0.1:8000",
    )


async def test_web_app_served_with_strict_csp(client: AsyncClient) -> None:
    resp = await client.get("/app")
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    csp = resp.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert resp.headers["x-frame-options"] == "DENY"
    assert "no-referrer" in resp.headers["referrer-policy"]
    html = resp.text
    assert "I’m in your menu bar" in html or "I'm in your menu bar" in html
    assert "connection-form" not in html
    assert "Ask EV" not in html


async def test_lookout_glass_is_translucent(client: AsyncClient) -> None:
    css = (await client.get("/app/lookout.css")).text
    assert "--window-opacity: 0.75" in css
    assert "opacity: var(--window-opacity)" in css
    assert "rgba(14, 11, 9, 0.30)" in css
    assert "70%" in css
    assert "backdrop-filter" in css
    assert "background: transparent" in css
    assert "body.visor .hud" in css
    assert "--cyan" not in css
    assert "hud-corners" not in css
    html = (await client.get("/app/lookout")).text
    assert "folio" in html
    assert "hud-corners" not in html
    overlay = (
        Path(__file__).resolve().parents[2]
        / "macos"
        / "Sources"
        / "EV"
        / "PresenceOverlay.swift"
    ).read_text(encoding="utf-8")
    assert "windowOpacity: CGFloat = 0.75" in overlay
    assert "opacity(0.30)" in overlay
    assert "isOpaque = false" in overlay
    assert "alphaValue = EVPalette.windowOpacity" in overlay
    assert ".hudWindow" not in overlay
    assert ".borderless" in overlay
    assert ".titled" not in overlay
    assert "PresenceLayout" in overlay


async def test_web_gallery_shows_window_examples(client: AsyncClient) -> None:
    resp = await client.get("/app/gallery")
    assert resp.status_code == 200, resp.text
    assert "EVIE HUD gallery" in resp.text
    assert "/app/lookout.js" in resp.text
    js = (await client.get("/app/lookout.js")).text
    assert "GALLERY" in js
    assert "renderGallery" in js
    assert "amazfit_helio" in js


async def test_web_lookout_and_stage_are_independent_windows(client: AsyncClient) -> None:
    lookout = await client.get("/app/lookout")
    assert lookout.status_code == 200, lookout.text
    assert "EVIE lookout" in lookout.text
    assert "/app/lookout.css" in lookout.text
    assert "/app/lookout.js" in lookout.text
    assert "connection-form" not in lookout.text
    stage = await client.get("/app/stage?demo=1")
    assert stage.status_code == 200, stage.text
    assert "EVIE visor" in stage.text
    css = await client.get("/app/lookout.css")
    assert css.status_code == 200
    js = await client.get("/app/lookout.js")
    assert js.status_code == 200
    assert "DEMO_WINDOWS" in js.text
    assert "window.open" in js.text


async def test_web_ops_console_is_not_the_default(client: AsyncClient) -> None:
    resp = await client.get("/app/ops")
    assert resp.status_code == 200, resp.text
    html = resp.text
    for marker in (
        "ev-hud-card",
        "ev-memory-browser",
        "ev-timeline",
        "capture-form",
        "ask-form",
        "connection-form",
    ):
        assert marker in html


async def test_web_app_has_no_third_party_resources(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    js = (await client.get("/app/app.js")).text
    assert 'src="http' not in html and 'src="https' not in html
    assert 'href="http' not in html and 'href="https' not in html
    assert "<script src=\"http" not in js


async def test_web_assets_served_and_allowlisted(client: AsyncClient) -> None:
    js = await client.get("/app/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    for endpoint in (
        "/v1/health",
        "/v1/events",
        "/v1/chat",
        "/v1/memories",
        "/v1/audit/",
        "/v1/timeline",
        "/v1/hud/card",
    ):
        assert endpoint in js.text
    assert js.headers["content-security-policy"].startswith("default-src 'self'")

    css = await client.get("/app/style.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]

    assert (await client.get("/app/secret.txt")).status_code == 404
    assert (await client.get("/app/../app.py")).status_code == 404


async def test_web_uses_idempotent_capture_keys(client: AsyncClient) -> None:
    js = (await client.get("/app/app.js")).text
    assert "Idempotency-Key" in js
    assert "crypto.randomUUID()" in js
    assert "ev.offlineQueue" in js
    assert "ev.quarantine" in js
    assert "queueCapture" in js
    assert "syncQueue" in js
    assert "window.EV" in js
    html = (await client.get("/app/ops")).text
    assert 'id="sync-queue"' in html
    assert 'id="queue-status"' in html


async def test_web_has_onboarding_panel(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    assert 'id="ev-onboarding"' in html
    assert 'id="onboarding-text"' in html
    assert 'id="onboarding-finish"' in html
    assert 'id="onboarding-check"' in html
    assert 'id="onboarding-readiness"' in html

    js = (await client.get("/app/app.js")).text
    assert "ev.onboarding" in js
    assert "finishOnboarding" in js
    assert "readOnboardingTexts" in js
    # The panel captures through the same idempotent event path as web capture
    # and demonstrates the first audit (UX onboarding steps 3-4).
    assert 'source: "web"' in js
    assert "Idempotency-Key" in js
    assert "/v1/audit/" in js
    assert "onboardingReadiness" in js
    for endpoint in (
        "/v1/health",
        "/v1/training/consent",
        "/v1/identity/status",
        "/v1/voice/enrollments",
    ):
        assert endpoint in js


async def test_web_setup_wizard(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    assert 'id="ev-wizard"' in html
    assert 'id="wizard-next"' in html
    assert 'id="wizard-back"' in html
    assert 'id="wizard-steps"' in html
    js = (await client.get("/app/app.js")).text
    assert "WIZARD_STEPS" in js
    assert "wizardNext" in js
    assert "wizardBack" in js
    for track in ("voice_enrollment", "training_corpus", "life_data_personalization"):
        assert track in js
    assert "/v1/identity/recovery/codes" in js


async def test_web_voice_enrollment_sends_liveness_proof(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    assert 'id="voice-liveness"' in html
    assert 'id="voice-live-score"' in html
    js = (await client.get("/app/app.js")).text
    assert "liveness_proof" in js
    assert "live_score" in js
    assert 'audio_b64: sample' in js
    assert "/v1/voice/enroll" in js


async def test_web_memory_browser_supports_editing(client: AsyncClient) -> None:
    js = (await client.get("/app/app.js")).text
    assert "edit-btn" in js
    assert 'data-action="correct"' in js
    assert 'data-action="forget"' in js
    assert 'data-action="restore"' in js
    assert "/v1/memories/${id}/correct" in js
    assert "/v1/memories/${id}/forget" in js
    assert "/v1/memories/${id}/restore" in js
    assert 'id="memory-result"' in (await client.get("/app/ops")).text


async def test_web_conversation_view(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    assert 'id="ev-conversation"' in html
    assert 'id="conversation-form"' in html
    assert 'id="conversation-messages"' in html
    js = (await client.get("/app/app.js")).text
    assert "/v1/conversation?limit=50" in js
    assert "conversation_id" in js
    assert "sendConversation" in js


async def test_web_settings_panel(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    assert 'id="ev-settings"' in html
    assert 'id="personality-save"' in html
    assert 'id="recovery-codes"' in html
    assert 'id="vault-rotate"' in html
    js = (await client.get("/app/app.js")).text
    for endpoint in (
        "/v1/personality",
        "/v1/training/consent",
        "/v1/runtime/status",
        "/v1/identity/recovery/codes",
        "/v1/integrations/vault/rotate",
    ):
        assert endpoint in js


async def test_web_hud_briefings_panel(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    assert 'id="ev-hud-more"' in html
    assert 'id="hud-topic"' in html
    assert 'id="hud-briefing"' in html
    assert 'id="hud-focus"' in html
    assert 'id="hud-route"' in html
    js = (await client.get("/app/app.js")).text
    for endpoint in (
        "/v1/tactical/quick?topic=",
        "/v1/tactical/brief",
        "/v1/hud/focus",
        "/v1/hud/route",
    ):
        assert endpoint in js


async def test_web_console_surface(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    js = (await client.get("/app/app.js")).text
    for marker in (
        'id="ev-console"',
        'id="ticker-bar"',
        'id="health-tiles"',
        'id="focus-tile"',
        'id="gear-tiles"',
        'id="model-tiles"',
        'id="notification-list"',
        'id="ev-voice-session"',
        'id="ev-people"',
        'id="ev-integrations"',
        'id="ev-routines"',
    ):
        assert marker in html
    for endpoint in (
        "/v1/live/status",
        "/v1/health/summary",
        "/v1/gear",
        "/v1/gateway/models",
        "/v1/gateway/stats",
        "/v1/ops/metrics",
        "/v1/runtime/notify/status",
        "/v1/runtime/notifications",
        "/v1/people/enrollments",
        "/v1/integrations/catalog",
        "/v1/routines/overview",
    ):
        assert endpoint in js


async def test_web_streaming_cancellation_and_provenance(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    js = (await client.get("/app/app.js")).text
    assert 'id="ask-cancel"' in html
    assert 'id="conversation-cancel"' in html
    assert 'id="ask-retry"' in html
    assert 'id="conversation-retry"' in html
    assert "AbortController" in js
    assert "stream: true" in js
    assert "postSse" in js
    assert "askRetry" in js
    assert "conversationRetry" in js
    assert "audit-chip" in js
    assert "showAudit" in js
    assert 'id="ask-provenance"' in html


async def test_web_voice_roundtrip_path(client: AsyncClient) -> None:
    js = (await client.get("/app/app.js")).text
    html = (await client.get("/app/ops")).text
    assert 'id="voice-wake"' in html
    assert 'id="voice-verify"' in html
    assert 'id="voice-talk"' in html
    assert 'id="voice-end"' in html
    assert 'id="voice-retry"' in html
    assert 'id="voice-session-refresh"' in html
    assert 'id="voice-audio-test"' in html
    for marker in (
        "/v1/voice/wake",
        "/v1/voice/verify",
        "/v1/voice/utterance/stream",
        "/v1/voice/audio/",
        "navigator.mediaDevices.getUserMedia",
        "MediaRecorder",
        "AudioContext",
        "playAudioBuffer",
        "test tone",
        "liveness_proof",
        "captureClip",
    ):
        assert marker in js


async def test_web_people_integrations_routines_markers(client: AsyncClient) -> None:
    js = (await client.get("/app/app.js")).text
    html = (await client.get("/app/ops")).text
    assert 'id="people-photos"' in html
    assert 'id="people-correct"' in html
    assert 'id="integration-adapter"' in html
    assert 'id="routine-create"' in html
    assert 'id="people-recognition-id"' in html
    for marker in (
        "/v1/people/recognitions/",
        "/v1/integrations/",
        "/v1/integrations/oauth/authorize",
        "/sync?days=7",
        "/v1/routines/",
        "/v1/routines/templates",
        "person-delete",
    ):
        assert marker in js


async def test_web_schema_faithful_renderer(client: AsyncClient) -> None:
    js = (await client.get("/app/app.js")).text
    assert "HUD_SCHEMAS" in js
    assert "renderSchemaCard" in js
    assert '"ev.hud.card.v1"' in js
    assert '"ev.hud.briefing.v1"' in js
    assert '"ev.hud.focus.v1"' in js
    assert '"ev.hud.route.v1"' in js
    assert '"ev.hud.quickcard.v1"' in js
    assert '"ev.hud.lookout.v1"' in js
    assert "schema_version" in js


async def test_web_accessibility_landmarks(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    assert 'class="skip-link"' in html
    assert '<main id="main" role="main">' in html


def test_web_embedded_schemas_match_docs() -> None:
    """The console renderer embeds the exact docs/schemas definitions."""
    js_source = (REPO_ROOT / "backend" / "clients" / "web" / "app.js").read_text(
        encoding="utf-8"
    )
    for schema_path in SCHEMA_DIR.glob("ev-hud-*.json"):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        version = schema["properties"]["schema_version"]["const"]
        assert f'"{version}"' in js_source
        for required in schema.get("required", []):
            assert required in js_source


async def test_web_csp_posture_unchanged(client: AsyncClient) -> None:
    resp = await client.get("/app")
    csp = resp.headers["content-security-policy"]
    assert "media-src" not in csp
    assert "blob:" not in csp
    assert "connect-src 'self'" in csp
    html = resp.text
    js = (await client.get("/app/app.js")).text
    assert "https://" not in html.replace("https://github.com/sahajpatel123/ev", "")
    assert "src=\"https" not in js and "src='https" not in js


async def test_web_bootstrap_loopback_issues_device_token_without_key() -> None:
    async with loopback_client() as loopback:
        resp = await loopback.get("/app/bootstrap")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["authenticated"] is True
        assert data["mode"] == "loopback"
        assert data["label"] == "connected (this Mac)"
        assert data["token"]
        assert data["token"] != "test-key"
        assert "master_key" not in data
        # The issued token is a real device credential the API accepts.
        proof = await loopback.get(
            "/v1/timeline",
            params={"limit": 1},
            headers={"Authorization": "Bearer " + data["token"]},
        )
        assert proof.status_code == 200, proof.text


async def test_web_bootstrap_rotates_previous_token() -> None:
    async with loopback_client() as loopback:
        first = (await loopback.get("/app/bootstrap")).json()["token"]
        second = (await loopback.get("/app/bootstrap")).json()["token"]
        assert first != second
        old = await loopback.get(
            "/v1/timeline",
            params={"limit": 1},
            headers={"Authorization": "Bearer " + first},
        )
        assert old.status_code == 401
        new = await loopback.get(
            "/v1/timeline",
            params={"limit": 1},
            headers={"Authorization": "Bearer " + second},
        )
        assert new.status_code == 200


async def test_web_bootstrap_rejects_non_loopback_client() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.7", 1234)),
        base_url="http://test",
    ) as remote:
        resp = await remote.get("/app/bootstrap")
        assert resp.status_code == 403
        assert "token" not in resp.text


async def test_web_bootstrap_rejects_remote_client_spoofing_localhost_host_header() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.7", 1234)),
        base_url="http://test",
        headers={"Host": "127.0.0.1:8000"},
    ) as remote:
        resp = await remote.get("/app/bootstrap")
        assert resp.status_code == 403
        assert "token" not in resp.text


async def test_web_static_assets_contain_no_master_secret(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    js = (await client.get("/app/app.js")).text
    combined = html + js
    assert "EV_MASTER_KEY" not in combined
    assert "EV_API_KEY=" not in combined
    assert "test-key" not in combined
    assert "master_key" not in js


async def test_web_local_auto_connect_markers(client: AsyncClient) -> None:
    html = (await client.get("/app/ops")).text
    js = (await client.get("/app/app.js")).text
    for marker in (
        'id="connection-note"',
        'id="disconnect-local"',
        'id="reconnect-local"',
        'id="manual-switch"',
    ):
        assert marker in html
    for marker in (
        "autoConnectLocal",
        "fetchBootstrap",
        "/app/bootstrap",
        "connected (this Mac)",
        "isLoopbackOrigin",
        "disconnectLocal",
        "manualSwitch",
        "reconnectLocal",
        "ev.connectionMode",
    ):
        assert marker in js


async def test_web_manual_connect_still_supported(client: AsyncClient) -> None:
    js = (await client.get("/app/app.js")).text
    html = (await client.get("/app/ops")).text
    assert 'id="api-key"' in html
    assert 'id="api-url"' in html
    assert "connection-form" in js
    assert "device token recommended" in html
    assert "ev.apiKey" in js
