"""D6 shortcuts in EvieShell call the Mac API. The iPhone install is owner-run."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INTENTS = ROOT / "ios" / "EvieShell" / "App" / "AppIntents.swift"
CLIENT = ROOT / "ios" / "EvieShell" / "App" / "DeviceAuth.swift"


def test_s38_shortcuts_name_the_four_owner_actions() -> None:
    src = INTENTS.read_text()
    for title in (
        "Ask Evie",
        "Send latest screenshot",
        "Approve pending",
        "Deny pending",
        "Status",
    ):
        assert title in src, title
    for path in (
        "/v1/device-gateway/text",
        "/v1/fabric/attach",
        "/v1/fabric/approvals/pending",
        "/v1/fabric/status",
    ):
        assert path in src, path
    # A 403 from approve must stay a dialog, not a claimed success.
    assert "fresh confirmation" in src
    assert "sha256" in src


def test_s38_gateway_client_returns_http_status() -> None:
    src = CLIENT.read_text()
    assert "static func send(" in src
    assert "statusCode" in src
