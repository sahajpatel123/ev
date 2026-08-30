"""PURE_PWA_WITH_NO_NATIVE_SHELL — auth must not require a native body."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings, get_settings
from app.device_gateway import PWA_BUILD
from app.device_gateway.mobile_actions.engine import status_snapshot
from app.device_gateway.mobile_actions.store import put_handshake, reset_for_tests

PWA = Path(__file__).resolve().parents[1] / "clients" / "pwa"
DEVICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def setup_function() -> None:
    reset_for_tests()


def test_server_and_pwa_build_pins_match() -> None:
    """Build mismatch + unbounded reload = Authenticating forever (owner regression).

    The full coherence contract lives in test_release_contract.py; this keeps
    the original four-way file pin as a fast local guard.
    """


    app_js = (PWA / "app.js").read_text()
    sw = (PWA / "sw.js").read_text()
    html = (PWA / "index.html").read_text()
    assert f'CLIENT_BUILD = "{PWA_BUILD}"' in app_js
    assert f'const BUILD = "{PWA_BUILD}"' in sw
    assert PWA_BUILD in html
    assert Settings.model_fields["pwa_build"].default == PWA_BUILD


def test_hello_build_mismatch_reload_is_bounded() -> None:
    """Repairs are one-shot per page session; failures are terminal, never loops."""
    app_js = (PWA / "app.js").read_text()
    # Bounded one-shot budgets for each repair path
    assert "oneShot(" in app_js
    assert 'sessionStorage.setItem(key, "1")' in app_js
    assert "location.reload()" in app_js
    # Terminal states are named, not generic auth failure
    for kind in (
        "MIXED_ASSET_BUILD",
        "CLIENT_UPDATE_REQUIRED",
        "AUTHENTICATION_FAILED",
        "CLIENT_PROTOCOL_UNSUPPORTED",
    ):
        assert kind in app_js
    # A cosmetic build difference must NOT block READY: update is advisory.
    assert "state.updateAvailable" in app_js
    assert "body.update_required" in app_js  # only genuine protocol skew blocks


def test_pure_pwa_status_native_unavailable_not_required() -> None:
    """Web PWA with no native handshake is healthy: READY candidate, native=false."""

    snap = status_snapshot(
        device_id=DEVICE,
        role="primary_companion",
        display_name="Primary iPhone",
    )
    assert snap["native_shell_connected"] is False
    assert snap["native_shell"] in {"SCAFFOLDED", "INTEGRATED"}
    assert snap["voice_backend"] == "pwa_golden"
    # Low-risk native ops unavailable; maps/web handoffs can still be ready.
    by_op = {row["operation"]: row for row in snap["capabilities"]}
    assert by_op["create_timer"]["available"] is False
    assert "Needs the Evie iPhone app" in (by_op["create_timer"]["reason"] or "")


def test_pwa_handshake_without_native_shell_does_not_claim_broker() -> None:
    put_handshake(
        DEVICE,
        {
            "native_shell": False,
            "protocol": 1,
            "capabilities": [],
        },
    )
    snap = status_snapshot(
        device_id=DEVICE,
        role="primary_companion",
        display_name="Primary iPhone",
    )
    assert snap["native_shell_connected"] is False


def test_native_actions_kill_switch_does_not_remove_pwa_surface() -> None:
    """Kill switch is for action execution, not device auth surface."""

    assert hasattr(get_settings(), "native_actions_enabled")
    app_js = (PWA / "app.js").read_text()
    # READY is set before / independently of native shell post.
    assert "setConn(\"READY\")" in app_js
    assert "EvieNativeShell" in (PWA / "mobile-actions.js").read_text()
    # Handshake is fire-and-forget after READY path (configure then .then, not await).
    hello_idx = app_js.index("async function hello()")
    ready_idx = app_js.index('setConn("READY")', hello_idx)
    handshake_idx = app_js.index("EvieMobileActions.handshake()", hello_idx)
    assert handshake_idx < ready_idx or "handshake().then" in app_js[hello_idx:ready_idx + 40]
    # Must not await native bridge before READY.
    chunk = app_js[hello_idx:ready_idx]
    assert "await window.EvieNativeShell" not in chunk
    assert "await EvieNativeShell" not in chunk
