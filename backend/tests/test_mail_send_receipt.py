"""A mail helper receipt is a send only when the helper says sent."""

from __future__ import annotations

from types import SimpleNamespace

from app.ev.tools import _call_place_receipt, _mail_send_receipt


def test_mail_send_receipt_requires_helper_confirmation() -> None:
    missing = _mail_send_receipt(SimpleNamespace(data={}, delivery={}), to="Ada")
    assert missing["ok"] is False
    assert missing["sent"] is False
    assert missing["spoken"] == "The email was not confirmed sent."

    sent = _mail_send_receipt(
        SimpleNamespace(data={"sent": True, "id": "m1"}, delivery={"confirmed": True}),
        to="Ada",
    )
    assert sent["ok"] is True
    assert sent["sent"] is True
    assert sent["id"] == "m1"
    assert sent["spoken"] == "Sent email to Ada."
    assert sent["delivery"] == {"confirmed": True}


def test_call_place_receipt_requires_the_helper_to_open() -> None:
    missing = _call_place_receipt(SimpleNamespace(data={}, delivery={}), name="Ada")
    assert missing["ok"] is False
    assert missing["opened"] is False
    assert missing["spoken"] == "I couldn't confirm the call to Ada."

    ringing = _call_place_receipt(
        SimpleNamespace(data={"opened": True, "destination": "+15551212"}, delivery={"confirmed": True}),
        name="Ada",
    )
    assert ringing["ok"] is True
    assert ringing["opened"] is True
    assert ringing["spoken"] == "Ringing Ada."
    assert ringing["delivery"] == {"confirmed": True}
