"""Mac TCC identity regression: EV.app must have stable bundle id and signing."""
import pathlib
import subprocess

import pytest

APP = pathlib.Path(__file__).resolve().parents[2] / "macos" / "build" / "EV.app"

@pytest.mark.skipif(not APP.exists(), reason="EV.app not built (run macos/scripts/package.sh)")
def test_mac_app_stable_identity():
    # Bundle identifier must be stable
    out = subprocess.run(["/usr/bin/plutil", "-p", str(APP / "Contents" / "Info.plist")], capture_output=True, text=True)
    assert "com.ev.suit" in out.stdout, f"CFBundleIdentifier must be com.ev.suit, got {out.stdout[:500]}"
    # Code signature must not be ad-hoc
    dv = subprocess.run(["codesign", "-dv", str(APP)], capture_output=True, text=True)
    combined = dv.stderr + dv.stdout
    assert "Signature=adhoc" not in combined, f"EV.app must not be ad-hoc signed: {combined}"
    assert "Identifier=com.ev.suit" in combined, f"codesign Identifier must be com.ev.suit: {combined}"
    # Designated requirement must be stable (identifier + cert leaf), not cdhash
    dr = subprocess.run(["codesign", "-dr", "-", str(APP)], capture_output=True, text=True)
    dr_combined = dr.stderr + dr.stdout
    assert 'identifier "com.ev.suit"' in dr_combined, f"designated requirement must contain com.ev.suit: {dr_combined}"
    assert "certificate leaf" in dr_combined, f"stable cert leaf must be in designated requirement: {dr_combined}"
    # Entitlements must be present
    ent = subprocess.run(["codesign", "-d", "--entitlements", "-", str(APP)], capture_output=True, text=True)
    ent_combined = ent.stderr + ent.stdout
    assert "com.apple.security.device.audio-input" in ent_combined
    assert "com.apple.security.device.camera" in ent_combined

@pytest.mark.skipif(not APP.exists(), reason="EV.app not built")
def test_permission_center_uses_local_authorities():
    # Ensure PermissionCenter.swift uses local OS APIs, not backend
    src = (pathlib.Path(__file__).resolve().parents[2] / "macos" / "Sources" / "EV" / "PermissionCenter.swift").read_text()
    # Must use AXIsProcessTrusted for accessibility
    assert "AXIsProcessTrusted" in src
    # Must use CGPreflightScreenCaptureAccess for screen
    assert "CGPreflightScreenCaptureAccess" in src
    # Must use UNUserNotificationCenter for notifications
    assert "UNUserNotificationCenter" in src
    # Must use AVCaptureDevice for camera
    assert "AVCaptureDevice.authorizationStatus" in src
    # Must NOT infer from backend capabilities
    assert "ConsentRecord" not in src
    assert "G2 bootstrap" not in src
